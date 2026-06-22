# app/services/network_subset.py
"""
Sub-network extraction service.

Supports the interactive workflow:

    1. fetch_raw_pipes()        — load all (or status-filtered) pipes from
                                   the .gpkg, one RawPipe per geometry part.
    2. filter_by_ids() /
       filter_by_point() /
       filter_by_polygon()      — narrow down to the user's map selection
                                   (clicked pipes / point+radius / drawn
                                   polygon).
    3. snap_and_build_graph()   — merge pipe endpoints that sit within
                                   `tolerance_m` of each other into a single
                                   node (fixes small digitising gaps so two
                                   pipes that *should* connect actually do),
                                   then build an undirected graph.
    4. connected_components()   — split the graph into its connected
                                   pieces. The caller (router) returns every
                                   piece; the frontend lets the user pick
                                   which one to simulate.
    5. build_inp_from_subset()  — re-fetch the chosen pipe fids from the
                                   .gpkg (source of truth, not whatever the
                                   client cached), re-run the same snap +
                                   graph step, snap the user-chosen
                                   reservoir point to the nearest node in
                                   that component, and write an EPANET .inp.

This module deliberately reuses the low-level helpers already in
network_builder.py (WKB parsing, haversine, the .inp writer) so both the
"whole network" build path and this "subset" build path stay consistent.
"""

import logging
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.exceptions import InvalidGpkgError
from app.services.network_builder import (
    _haversine_m,
    _parse_gpkg_geometry,
    _write_inp,
    gpkg_full_path,
)

logger = logging.getLogger(__name__)

# Hard safety cap so a runaway polygon selection (e.g. the whole city) can't
# blow up the snapping step, which is O(n) with a small constant but still
# not meant for tens of thousands of pipes in one interactive call.
MAX_SELECTION_PIPES = 5000


# ── data shapes ──────────────────────────────────────────────────────────────

@dataclass
class RawPipe:
    pipe_uid:   str                       # unique per geometry part, e.g. "123:0"
    fid:        int
    start:      Tuple[float, float]       # (lon, lat), unrounded
    end:        Tuple[float, float]
    full_coords: List[Tuple[float, float]]
    length_m:   float
    diam_mm:    float
    roughness:  float
    status:     str
    material:   Optional[str]


@dataclass
class SnappedGraph:
    node_coords:         Dict[str, Tuple[float, float]]   # node_key -> (lon, lat) centroid
    edges:               List[dict]                        # pipe_uid, fid, node_a, node_b, length_m, diam_mm, roughness, status, material
    dropped_self_loops:  int


@dataclass
class Component:
    component_id:    int
    node_keys:        List[str]
    pipe_fids:        List[int]
    pipe_uids:        List[str]
    total_length_m:   float
    bbox:             Tuple[float, float, float, float]
    degree:           Dict[str, int] = field(default_factory=dict)


# ── step 1: load pipes ──────────────────────────────────────────────────────

def fetch_raw_pipes(
    filename:    str,
    fids:        Optional[List[int]] = None,
    pipe_status: Optional[str]       = None,
) -> List[RawPipe]:
    """
    Load pipes from the .gpkg as RawPipe objects (one per geometry part).

    With no filters this returns the whole layer — used for the initial
    Leaflet display. With `fids` it returns only those assets — used to
    rebuild a previously-selected component from the source data.
    """
    path = gpkg_full_path(filename)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    layer_row = cur.execute("SELECT table_name FROM gpkg_contents LIMIT 1").fetchone()
    if not layer_row:
        conn.close()
        raise InvalidGpkgError("No layers found.")
    layer = layer_row[0]

    where  = ["geom IS NOT NULL", "length3dig > 0"]
    params: List = []
    if fids:
        placeholders = ",".join("?" for _ in fids)
        where.append(f"fid IN ({placeholders})")
        params.extend(int(f) for f in fids)
    if pipe_status:
        where.append("status = ?")
        params.append(pipe_status)

    sql = (
        f"SELECT fid, geom, intdiammm, roughness, length3dig, status, material "
        f"FROM {layer} WHERE {' AND '.join(where)}"
    )
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    raw_pipes: List[RawPipe] = []
    for row in rows:
        rings = _parse_gpkg_geometry(bytes(row["geom"]))
        if not rings:
            continue

        length_m  = max(1.0,  min(float(row["length3dig"]), 50_000.0))
        diam_mm   = max(25.0, min(float(row["intdiammm"] or 100.0), 2000.0))
        roughness = max(60.0, min(float(row["roughness"]  or 130.0), 160.0))

        for part_idx, ring in enumerate(rings):
            if len(ring) < 2:
                continue
            start, end = ring[0], ring[-1]
            if abs(start[0] - end[0]) < 1e-9 and abs(start[1] - end[1]) < 1e-9:
                continue
            raw_pipes.append(RawPipe(
                pipe_uid    = f"{row['fid']}:{part_idx}",
                fid         = int(row["fid"]),
                start       = start,
                end         = end,
                full_coords = ring,
                length_m    = length_m,
                diam_mm     = diam_mm,
                roughness   = roughness,
                status      = row["status"] or "",
                material    = row["material"],
            ))
    return raw_pipes


# ── step 2: spatial selection filters ───────────────────────────────────────

def filter_by_ids(pipes: List[RawPipe], fids: List[int]) -> List[RawPipe]:
    fid_set = {int(f) for f in fids}
    return [p for p in pipes if p.fid in fid_set]


def _point_segment_distance_m(plon, plat, alon, alat, blon, blat) -> float:
    """Distance from point P to segment AB, using a local equirectangular
    projection (accurate enough at city scale, much cheaper than true
    geodesic point-to-line)."""
    lat0 = math.radians((alat + blat) / 2.0)
    mx = 111_320.0 * max(0.05, math.cos(lat0))
    my = 111_320.0
    ax, ay = alon * mx, alat * my
    bx, by = blon * mx, blat * my
    px, py = plon * mx, plat * my
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def filter_by_point(pipes: List[RawPipe], lat: float, lon: float, radius_m: float) -> List[RawPipe]:
    out = []
    for p in pipes:
        coords = p.full_coords
        min_d = min(
            _point_segment_distance_m(lon, lat, coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
            for i in range(len(coords) - 1)
        )
        if min_d <= radius_m:
            out.append(p)
    return out


def _point_in_polygon(lon: float, lat: float, polygon: List[Tuple[float, float]]) -> bool:
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > lat) != (yj > lat):
            x_at_lat = xi + (lat - yi) * (xj - xi) / ((yj - yi) or 1e-15)
            if lon < x_at_lat:
                inside = not inside
        j = i
    return inside


def _segments_intersect(p1, p2, p3, p4) -> bool:
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def filter_by_polygon(pipes: List[RawPipe], polygon: List[Tuple[float, float]]) -> List[RawPipe]:
    edges = list(zip(polygon, polygon[1:] + polygon[:1]))
    out = []
    for p in pipes:
        coords = p.full_coords
        hit = any(_point_in_polygon(lon, lat, polygon) for lon, lat in coords)
        if not hit:
            for i in range(len(coords) - 1):
                a, b = coords[i], coords[i + 1]
                if any(_segments_intersect(a, b, e[0], e[1]) for e in edges):
                    hit = True
                    break
        if hit:
            out.append(p)
    return out


# ── step 3: endpoint snapping + graph ───────────────────────────────────────

def snap_and_build_graph(pipes: List[RawPipe], tolerance_m: float = 2.0) -> SnappedGraph:
    """
    Cluster pipe endpoints within `tolerance_m` of each other into shared
    nodes (a grid-accelerated union-find on the raw endpoints), then build
    the resulting undirected pipe graph.

    Pipes whose two ends land in the same cluster after snapping (true
    self-loops, or duplicate-vertex digitising artifacts) are dropped and
    counted in `dropped_self_loops`.
    """
    if not pipes:
        return SnappedGraph({}, [], 0)

    raw_points: Dict[str, Tuple[float, float]] = {}

    def pid(lon: float, lat: float) -> str:
        key = f"{round(lon, 7)},{round(lat, 7)}"
        raw_points[key] = (lon, lat)
        return key

    for p in pipes:
        pid(*p.start)
        pid(*p.end)

    keys = list(raw_points.keys())
    parent = {k: k for k in keys}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    lat_ref = sum(v[1] for v in raw_points.values()) / len(raw_points)
    deg_per_m_lat = 1.0 / 111_320.0
    deg_per_m_lon = 1.0 / (111_320.0 * max(0.05, math.cos(math.radians(lat_ref))))
    cell_lon = max(tolerance_m, 0.01) * deg_per_m_lon
    cell_lat = max(tolerance_m, 0.01) * deg_per_m_lat

    def cell_of(lon: float, lat: float) -> Tuple[int, int]:
        return (int(math.floor(lon / cell_lon)), int(math.floor(lat / cell_lat)))

    grid: Dict[Tuple[int, int], List[str]] = {}
    for k in keys:
        lon, lat = raw_points[k]
        grid.setdefault(cell_of(lon, lat), []).append(k)

    if tolerance_m > 0:
        for k in keys:
            lon, lat = raw_points[k]
            cx, cy = cell_of(lon, lat)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in grid.get((cx + dx, cy + dy), []):
                        if other == k:
                            continue
                        olon, olat = raw_points[other]
                        if _haversine_m(lon, lat, olon, olat) <= tolerance_m:
                            union(k, other)

    clusters: Dict[str, List[str]] = {}
    for k in keys:
        clusters.setdefault(find(k), []).append(k)

    # deterministic ordering so repeated calls on the same input produce the
    # same node_keys (build_inp_from_subset relies on this)
    cluster_items = []
    for members in clusters.values():
        lons = [raw_points[m][0] for m in members]
        lats = [raw_points[m][1] for m in members]
        centroid = (sum(lons) / len(lons), sum(lats) / len(lats))
        cluster_items.append((centroid, members))
    cluster_items.sort(key=lambda c: (c[0][1], c[0][0]))

    node_coords: Dict[str, Tuple[float, float]] = {}
    point_to_node: Dict[str, str] = {}
    for i, (centroid, members) in enumerate(cluster_items):
        node_key = f"SN_{i}"
        node_coords[node_key] = centroid
        for m in members:
            point_to_node[m] = node_key

    edges: List[dict] = []
    dropped = 0
    for p in pipes:
        a_key = point_to_node[pid(*p.start)]
        b_key = point_to_node[pid(*p.end)]
        if a_key == b_key:
            dropped += 1
            continue
        edges.append({
            "pipe_uid":  p.pipe_uid,
            "fid":       p.fid,
            "node_a":    a_key,
            "node_b":    b_key,
            "length_m":  p.length_m,
            "diam_mm":   p.diam_mm,
            "roughness": p.roughness,
            "status":    p.status,
            "material":  p.material,
        })

    return SnappedGraph(node_coords=node_coords, edges=edges, dropped_self_loops=dropped)


# ── step 4: connected components ────────────────────────────────────────────

def connected_components(graph: SnappedGraph) -> List[Component]:
    adj: Dict[str, List[int]] = {k: [] for k in graph.node_coords}
    for idx, e in enumerate(graph.edges):
        adj[e["node_a"]].append(idx)
        adj[e["node_b"]].append(idx)

    visited: set = set()
    components: List[Component] = []

    for start in graph.node_coords:
        if start in visited:
            continue
        stack = [start]
        comp_nodes: set = set()
        comp_edge_idxs: set = set()
        while stack:
            n = stack.pop()
            if n in comp_nodes:
                continue
            comp_nodes.add(n)
            for eidx in adj[n]:
                comp_edge_idxs.add(eidx)
                e = graph.edges[eidx]
                other = e["node_b"] if e["node_a"] == n else e["node_a"]
                if other not in comp_nodes:
                    stack.append(other)
        visited |= comp_nodes

        if not comp_edge_idxs:
            continue  # isolated node with no surviving edges — not a usable component

        degree: Dict[str, int] = {n: 0 for n in comp_nodes}
        pipe_fids, pipe_uids = [], []
        total_len = 0.0
        for eidx in comp_edge_idxs:
            e = graph.edges[eidx]
            degree[e["node_a"]] += 1
            degree[e["node_b"]] += 1
            pipe_fids.append(e["fid"])
            pipe_uids.append(e["pipe_uid"])
            total_len += e["length_m"]

        lons = [graph.node_coords[n][0] for n in comp_nodes]
        lats = [graph.node_coords[n][1] for n in comp_nodes]
        bbox = (min(lons), min(lats), max(lons), max(lats))

        components.append(Component(
            component_id   = 0,  # renumbered below
            node_keys      = sorted(comp_nodes),
            pipe_fids      = sorted(set(pipe_fids)),
            pipe_uids      = sorted(pipe_uids),
            total_length_m = round(total_len, 1),
            bbox           = bbox,
            degree         = degree,
        ))

    components.sort(key=lambda c: -len(c.pipe_fids))
    for i, c in enumerate(components):
        c.component_id = i
    return components


def component_nodes(component: Component, graph: SnappedGraph) -> List[dict]:
    return [
        {
            "node_key": n,
            "lon":      round(graph.node_coords[n][0], 7),
            "lat":      round(graph.node_coords[n][1], 7),
            "degree":   component.degree[n],
        }
        for n in component.node_keys
    ]


def component_geojson(component: Component, graph: SnappedGraph, raw_by_uid: Dict[str, RawPipe]) -> dict:
    uid_set = set(component.pipe_uids)
    edge_by_uid = {e["pipe_uid"]: e for e in graph.edges if e["pipe_uid"] in uid_set}
    features = []
    for uid in component.pipe_uids:
        e = edge_by_uid[uid]
        raw = raw_by_uid.get(uid)
        coords = raw.full_coords if raw else [graph.node_coords[e["node_a"]], graph.node_coords[e["node_b"]]]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in coords]},
            "properties": {
                "fid":           e["fid"],
                "component_id":  component.component_id,
                "diam_mm":       e["diam_mm"],
                "length_m":      e["length_m"],
                "material":      e["material"],
                "status":        e["status"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ── step 5: build .inp for one chosen component ─────────────────────────────

def build_inp_from_subset(
    filename:         str,
    pipe_fids:        List[int],
    reservoir_lat:    float,
    reservoir_lon:    float,
    snap_tolerance_m: float           = 2.0,
    base_demand:      float           = 0.001,
    reservoir_head:   float           = 50.0,
    duration_hrs:     int             = 24,
    time_step_min:    int             = 60,
    extra_demands:    Optional[List[dict]] = None,
    inp_dir:          Optional[str]   = None,
) -> str:
    """
    Rebuild the chosen sub-network straight from the .gpkg (by fid) and
    write an EPANET .inp with the user-chosen reservoir as the single
    source. Demand units: m³/s in the API → m³/h (CMH) in the .inp.
    """
    if not pipe_fids:
        raise InvalidGpkgError("pipe_ids is empty — nothing to build.")
    if len(pipe_fids) > MAX_SELECTION_PIPES:
        raise InvalidGpkgError(f"Too many pipe_ids ({len(pipe_fids)} > {MAX_SELECTION_PIPES}).")

    raw_pipes = fetch_raw_pipes(filename, fids=pipe_fids)
    if not raw_pipes:
        raise InvalidGpkgError("None of the given pipe_ids were found with valid geometry.")

    graph = snap_and_build_graph(raw_pipes, tolerance_m=snap_tolerance_m)
    components = connected_components(graph)
    if not components:
        raise InvalidGpkgError("Selected pipes do not form a connected graph.")

    component = components[0]
    if len(components) > 1:
        logger.warning(
            "build_inp_from_subset: %d pipe_ids re-snapped into %d components "
            "(tolerance=%.2fm) — using the largest (%d pipes). The selection "
            "endpoint should normally already isolate a single component.",
            len(pipe_fids), len(components), snap_tolerance_m, len(component.pipe_fids),
        )

    node_set    = set(component.node_keys)
    node_coords = {k: graph.node_coords[k] for k in node_set}

    reservoir_key = min(
        node_set,
        key=lambda k: _haversine_m(reservoir_lon, reservoir_lat, node_coords[k][0], node_coords[k][1]),
    )
    dist = _haversine_m(reservoir_lon, reservoir_lat, node_coords[reservoir_key][0], node_coords[reservoir_key][1])
    if dist > max(snap_tolerance_m * 5, 25.0):
        logger.warning(
            "Reservoir point is %.1fm from the nearest node in the chosen component — using it anyway.",
            dist,
        )

    uid_set = set(component.pipe_uids)
    pipe_defs = [
        {
            "start_key": e["node_a"], "end_key": e["node_b"],
            "length_m": e["length_m"], "diam_mm": e["diam_mm"], "roughness": e["roughness"],
        }
        for e in graph.edges if e["pipe_uid"] in uid_set
    ]

    base_cmh = base_demand * 3600.0
    node_demands: Dict[str, float] = {k: (0.0 if k == reservoir_key else base_cmh) for k in node_set}
    if extra_demands:
        for ed in extra_demands:
            elon, elat = ed.get("lon", 0.0), ed.get("lat", 0.0)
            key = min(node_set, key=lambda k: _haversine_m(elon, elat, node_coords[k][0], node_coords[k][1]))
            if key != reservoir_key:
                node_demands[key] = node_demands.get(key, 0.0) + ed.get("demand_m3s", 0.005) * 3600.0

    if inp_dir is None:
        inp_dir = tempfile.gettempdir()
    os.makedirs(inp_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(filename))[0]
    inp_path  = os.path.join(inp_dir, f"{base_name}_subset.inp")

    _write_inp(
        node_coords    = node_coords,
        node_demands   = node_demands,
        reservoir_key  = reservoir_key,
        reservoir_head = reservoir_head,
        pipe_defs      = pipe_defs,
        duration_hrs   = duration_hrs,
        time_step_min  = time_step_min,
        out_path       = inp_path,
    )
    logger.info(
        "build_inp_from_subset: %d pipes, %d nodes, reservoir=%s → %s",
        len(pipe_defs), len(node_set), reservoir_key, inp_path,
    )
    return inp_path