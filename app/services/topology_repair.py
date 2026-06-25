# app/services/topology_repair.py
"""
DMA pipe-network topology repair.

Three-stage pipeline
---------------------
Stage 1 — Geometric snap (shapely)
    Project pipe geometries to UTM 32737, then call shapely.ops.snap()
    on each line against the union of all others at `snap_tol_m` (default
    20 m).  This closes sub-metre digitising gaps and near-misses.

Stage 2 — T-intersection splitting
    After snapping, some endpoints land on the interior of another pipe
    (a T-junction).  linemerge(unary_union(...)) breaks those lines at
    the shared vertex, so every segment now starts and ends at a proper
    graph node.

Stage 3 — MST connector insertion (Prim's / Kruskal's)
    After stages 1-2 the network may still be split into several
    disconnected components (because the true gap is wider than snap_tol).
    We run a minimum-spanning-tree merge: for each pair of components we
    find the closest node-to-node pair and add a synthetic CONNECTOR pipe
    between them.  We add the minimum number of connectors that make the
    whole network fully connected (|components| − 1 pipes, always).

Every synthetic connector is stored as a `SyntheticConnector` dataclass
(lon/lat of both ends in WGS-84, length, default material/diameter) so
the caller can:
  • draw them on the Leaflet map in a distinct colour
  • log them in the database for audit
  • let the user accept / reject / tweak each one via the frontend

The repaired network is returned as a `RepairedNetwork` dataclass whose
`edges` list is ready for the EPANET .inp builder.

All metric calculations are done in EPSG:32737 (UTM zone 37S, covering
the Dodoma/Arusha region of Tanzania).  Results are converted back to
WGS-84 lon/lat for storage and display.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── UTM ↔ WGS-84 conversion (pure stdlib, no pyproj needed) ─────────────────
# Accurate enough at city scale (< 1 cm error within Tanzania).
# Zone 37S (central meridian 39°E, southern hemisphere).

_A = 6_378_137.0        # WGS-84 semi-major axis
_F = 1 / 298.257223563
_B = _A * (1 - _F)
_E2 = 1 - (_B / _A) ** 2
_E  = math.sqrt(_E2)
_K0 = 0.9996
_LON0 = math.radians(39.0)   # zone 37 central meridian
_FALSE_EASTING  = 500_000.0
_FALSE_NORTHING = 10_000_000.0  # southern hemisphere


def _wgs84_to_utm37s(lon_deg: float, lat_deg: float) -> Tuple[float, float]:
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    N = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
    T = math.tan(lat) ** 2
    C = (_E2 / (1 - _E2)) * math.cos(lat) ** 2
    A = math.cos(lat) * (lon - _LON0)
    e2 = _E2
    M = _A * (
        (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256)  * lat
      - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*lat)
      + (15*e2**2/256 + 45*e2**3/1024)         * math.sin(4*lat)
      - (35*e2**3/3072)                          * math.sin(6*lat)
    )
    x = _K0 * N * (A + (1-T+C)*A**3/6 + (5-18*T+T**2+72*C-58*(e2/(1-e2)))*A**5/120) + _FALSE_EASTING
    y = _K0 * (M + N*math.tan(lat)*(A**2/2 + (5-T+9*C+4*C**2)*A**4/24
               + (61-58*T+T**2+600*C-330*(e2/(1-e2)))*A**6/720)) + _FALSE_NORTHING
    return x, y


def _utm37s_to_wgs84(x: float, y: float) -> Tuple[float, float]:
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    M  = (y - _FALSE_NORTHING) / _K0
    mu = M / (_A * (1 - _E2/4 - 3*_E2**2/64 - 5*_E2**3/256))
    p1 = mu + (3*e1/2 - 27*e1**3/32)*math.sin(2*mu)
    p1 += (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
    p1 += (151*e1**3/96) * math.sin(6*mu)
    N1 = _A / math.sqrt(1 - _E2*math.sin(p1)**2)
    T1 = math.tan(p1) ** 2
    C1 = (_E2/(1-_E2)) * math.cos(p1)**2
    R1 = _A*(1-_E2) / (1-_E2*math.sin(p1)**2)**1.5
    D  = (x - _FALSE_EASTING) / (N1 * _K0)
    lat = p1 - (N1*math.tan(p1)/R1)*(D**2/2 - (5+3*T1+10*C1-4*C1**2-9*_E2/(1-_E2))*D**4/24
              + (61+90*T1+298*C1+45*T1**2-252*_E2/(1-_E2)-3*C1**2)*D**6/720)
    lon = _LON0 + (D - (1+2*T1+C1)*D**3/6 + (5-2*C1+(28*T1)-3*C1**2+8*_E2/(1-_E2)+24*T1**2)*D**5/120) / math.cos(p1)
    return math.degrees(lon), math.degrees(lat)


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class NetworkEdge:
    """One pipe segment in the repaired graph (always in WGS-84 lon/lat)."""
    edge_id:       str
    node_a:        str
    node_b:        str
    start_lonlat:  Tuple[float, float]
    end_lonlat:    Tuple[float, float]
    length_m:      float
    diam_mm:       float
    hw_c:          float
    material:      str
    is_synthetic:  bool = False   # True = added by topology repair


@dataclass
class NetworkNode:
    """Junction / endpoint in the repaired graph."""
    node_id:  str
    lon:      float
    lat:      float
    x_m:      float   # UTM 37S
    y_m:      float


@dataclass
class SyntheticConnector:
    """One connector pipe inserted by MST topology repair."""
    connector_id:    str
    from_node:       str
    to_node:         str
    from_lonlat:     Tuple[float, float]
    to_lonlat:       Tuple[float, float]
    length_m:        float
    diam_mm:         float
    material:        str
    hw_c:            float
    reason:          str   # human-readable: "Gap between comp-0 and comp-2"


@dataclass
class RepairReport:
    """Summary of what topology repair did."""
    original_pipe_count:    int
    original_component_count: int
    snap_tol_m:             float
    segments_after_snap:    int
    segments_after_split:   int
    final_component_count:  int
    connectors_added:       List[SyntheticConnector]
    total_connector_length_m: float
    warnings:               List[str] = field(default_factory=list)


@dataclass
class RepairedNetwork:
    nodes:    Dict[str, NetworkNode]      # node_id → NetworkNode
    edges:    List[NetworkEdge]
    report:   RepairReport


# ── main repair function ───────────────────────────────────────────────────────

def repair_topology(
    pipe_coords:   List[List[Tuple[float, float]]],   # [[( lon,lat),…],…] WGS-84
    pipe_meta:     List[dict],                         # parallel list: {diam_mm, hw_c, material, length_m}
    snap_tol_m:    float = 20.0,
    node_grid_m:   float = 5.0,
    default_diam:  float = 50.0,
    default_mat:   str   = "PE",
    default_hw_c:  float = 150.0,
) -> RepairedNetwork:
    """
    Parameters
    ----------
    pipe_coords  : One entry per original pipe; each entry is the ordered
                   list of (lon, lat) vertices.
    pipe_meta    : Same-length list of dicts with diam_mm / hw_c / material.
    snap_tol_m   : Stage-1 endpoint snap tolerance in metres (default 20 m).
    node_grid_m  : Grid cell size for clustering nodes (default 5 m).

    Returns
    -------
    RepairedNetwork  — fully connected graph ready for the .inp builder.
    """
    if not pipe_coords:
        raise ValueError("No pipe coordinates provided.")

    warnings_out: List[str] = []

    # ── Stage 1: convert to UTM, snap, split ─────────────────────────────────

    # Project all coordinates to UTM 37S
    utm_pipes: List[List[Tuple[float, float]]] = []
    for ring in pipe_coords:
        utm_pipes.append([_wgs84_to_utm37s(lon, lat) for lon, lat in ring])

    # Shapely-based snap + split
    try:
        from shapely.geometry import LineString, MultiLineString
        from shapely.ops import unary_union, linemerge
        from shapely.ops import snap as shp_snap

        shp_lines = [LineString(pts) for pts in utm_pipes]
        pipe_union = unary_union(shp_lines)

        snapped = []
        for line in shp_lines:
            try:
                snapped.append(shp_snap(line, pipe_union, snap_tol_m))
            except Exception:
                snapped.append(line)

        combined = unary_union(snapped)
        try:
            merged = linemerge(combined)
        except (ValueError, Exception):
            # shapely 2.x raises ValueError for a bare LineString (not a collection)
            merged = combined

        segments_shp = (
            list(merged.geoms) if merged.geom_type == "MultiLineString"
            else [merged] if merged.geom_type == "LineString"
            else []
        )
        segments_utm = [list(s.coords) for s in segments_shp if len(list(s.coords)) >= 2]
        n_after_snap   = len(snapped)
        n_after_split  = len(segments_utm)
        logger.info(
            "Topology repair stage 1+2: %d raw pipes → snap → %d snapped → split → %d segments",
            len(utm_pipes), n_after_snap, n_after_split,
        )

    except ImportError:
        # Shapely not available — fall through with raw segments unchanged
        warnings_out.append("shapely not available; using raw pipe endpoints without snap/split.")
        segments_utm   = utm_pipes
        n_after_snap   = len(utm_pipes)
        n_after_split  = len(utm_pipes)

    # ── Stage 2: build graph from segments ───────────────────────────────────

    ep_map: Dict[Tuple[int, int], str] = {}   # grid_cell → node_id
    nodes:  Dict[str, NetworkNode]     = {}

    def _cell(x: float, y: float) -> Tuple[int, int]:
        return (int(x / node_grid_m), int(y / node_grid_m))

    def _get_node(x: float, y: float) -> str:
        cell = _cell(x, y)
        if cell not in ep_map:
            nid = f"J{len(ep_map)}"
            ep_map[cell] = nid
            lon, lat = _utm37s_to_wgs84(x, y)
            nodes[nid] = NetworkNode(node_id=nid, lon=lon, lat=lat, x_m=x, y_m=y)
        return ep_map[cell]

    edges: List[NetworkEdge] = []

    # Assign metadata to split segments by matching their midpoint back to
    # the nearest original pipe's metadata.
    def _nearest_meta(mx: float, my: float) -> dict:
        best_d, best_m = 1e18, pipe_meta[0] if pipe_meta else {}
        for i, pts in enumerate(utm_pipes):
            if len(pts) < 2:
                continue
            # midpoint of original pipe
            n = len(pts)
            pmx = sum(p[0] for p in pts) / n
            pmy = sum(p[1] for p in pts) / n
            d = math.hypot(mx - pmx, my - pmy)
            if d < best_d:
                best_d = d
                best_m = pipe_meta[i] if i < len(pipe_meta) else {}
        return best_m

    for idx, seg in enumerate(segments_utm):
        a = _get_node(seg[0][0],  seg[0][1])
        b = _get_node(seg[-1][0], seg[-1][1])
        if a == b:
            continue
        # Compute true arc length of the segment in metres
        length = sum(
            math.hypot(seg[i+1][0]-seg[i][0], seg[i+1][1]-seg[i][1])
            for i in range(len(seg)-1)
        )
        length = max(0.5, length)
        # midpoint for meta lookup
        mx = (seg[0][0] + seg[-1][0]) / 2
        my = (seg[0][1] + seg[-1][1]) / 2
        meta = _nearest_meta(mx, my)
        edges.append(NetworkEdge(
            edge_id      = f"P{idx}",
            node_a       = a,
            node_b       = b,
            start_lonlat = (nodes[a].lon, nodes[a].lat),
            end_lonlat   = (nodes[b].lon, nodes[b].lat),
            length_m     = length,
            diam_mm      = meta.get("diam_mm", default_diam),
            hw_c         = meta.get("hw_c",    default_hw_c),
            material     = meta.get("material", default_mat),
            is_synthetic = False,
        ))

    # ── Stage 3: find connected components ───────────────────────────────────

    def _find_components() -> List[List[str]]:
        from collections import defaultdict
        adj: Dict[str, List[str]] = defaultdict(list)
        for e in edges:
            adj[e.node_a].append(e.node_b)
            adj[e.node_b].append(e.node_a)
        visited: set = set()
        comps: List[List[str]] = []
        for start in nodes:
            if start in visited:
                continue
            stack = [start]; comp: List[str] = []
            while stack:
                n = stack.pop()
                if n in visited:
                    continue
                visited.add(n); comp.append(n)
                stack.extend(adj[n])
            comps.append(comp)
        comps.sort(key=lambda c: -len(c))
        return comps

    comps = _find_components()
    original_comp_count = len(comps)
    logger.info("Components before MST repair: %d (largest: %d nodes)", len(comps), len(comps[0]) if comps else 0)

    # ── Stage 4: MST connector insertion (Prim's on components) ──────────────

    connectors: List[SyntheticConnector] = []
    connector_count = 0

    # Median diameter of existing pipes — use for synthetic connectors
    all_diams = [e.diam_mm for e in edges]
    med_diam  = sorted(all_diams)[len(all_diams)//2] if all_diams else default_diam

    # Merge components one at a time: always attach the closest unmerged
    # component to the current main body (greedy MST).
    merged_set: set = set(comps[0])     # start with the largest component
    unmerged:   List[List[str]] = list(comps[1:])

    while unmerged:
        best_dist:    float = 1e18
        best_a_node:  Optional[str] = None
        best_b_node:  Optional[str] = None
        best_comp_idx: int = 0

        for ci, comp in enumerate(unmerged):
            for b_nid in comp:
                bx, by = nodes[b_nid].x_m, nodes[b_nid].y_m
                for a_nid in merged_set:
                    ax, ay = nodes[a_nid].x_m, nodes[a_nid].y_m
                    d = math.hypot(ax - bx, ay - by)
                    if d < best_dist:
                        best_dist      = d
                        best_a_node    = a_nid
                        best_b_node    = b_nid
                        best_comp_idx  = ci

        # Add a synthetic connector edge
        connector_count += 1
        cid      = f"CONN_{connector_count}"
        a_nd     = nodes[best_a_node]
        b_nd     = nodes[best_b_node]
        length_m = best_dist
        reason   = (
            f"MST connector {connector_count}: gap {length_m:.1f} m "
            f"between main network ({len(merged_set)} nodes) "
            f"and isolated segment ({len(unmerged[best_comp_idx])} nodes)"
        )
        logger.info(reason)
        if length_m > 500:
            warnings_out.append(
                f"Connector {cid} spans {length_m:.0f} m — "
                "consider field-verifying this connection before simulating."
            )

        conn = SyntheticConnector(
            connector_id  = cid,
            from_node     = best_a_node,
            to_node       = best_b_node,
            from_lonlat   = (a_nd.lon, a_nd.lat),
            to_lonlat     = (b_nd.lon, b_nd.lat),
            length_m      = round(length_m, 2),
            diam_mm       = med_diam,
            material      = default_mat,
            hw_c          = default_hw_c,
            reason        = reason,
        )
        connectors.append(conn)

        edges.append(NetworkEdge(
            edge_id      = cid,
            node_a       = best_a_node,
            node_b       = best_b_node,
            start_lonlat = (a_nd.lon, a_nd.lat),
            end_lonlat   = (b_nd.lon, b_nd.lat),
            length_m     = length_m,
            diam_mm      = med_diam,
            hw_c         = default_hw_c,
            material      = default_mat,
            is_synthetic = True,
        ))

        # Absorb the connected component into merged_set
        for nid in unmerged[best_comp_idx]:
            merged_set.add(nid)
        unmerged.pop(best_comp_idx)

    final_comps = _find_components()
    logger.info(
        "Topology repair complete: %d edges (%d synthetic), %d nodes, %d component(s)",
        len(edges), len(connectors), len(nodes), len(final_comps),
    )

    report = RepairReport(
        original_pipe_count       = len(pipe_coords),
        original_component_count  = original_comp_count,
        snap_tol_m                = snap_tol_m,
        segments_after_snap       = n_after_snap,
        segments_after_split      = n_after_split,
        final_component_count     = len(final_comps),
        connectors_added          = connectors,
        total_connector_length_m  = round(sum(c.length_m for c in connectors), 1),
        warnings                  = warnings_out,
    )

    return RepairedNetwork(nodes=nodes, edges=edges, report=report)
