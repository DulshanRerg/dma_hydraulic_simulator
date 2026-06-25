# app/services/network_builder.py
"""
Reads a .gpkg file and converts it to an EPANET .inp file for EPyT-Flow.

Key fixes
---------
- Only OPERATIONAL pipes with valid geometry AND positive length3dig are used.
- Topology is pruned: only nodes that appear in at least one valid pipe
  are written to the .inp (no floating isolated junctions).
- Pipe diameters are clamped to [25, 2000] mm to avoid EPANET solver failures.
- Pipe lengths are clamped to >= 1.0 m.
- A single reservoir is added at the node with the most pipe connections
  (the most-connected node is the best proxy for the supply point when
  elevation data is unavailable).
- Coordinates are written in the [COORDINATES] section so EPyT-Flow /
  the probe pass can read them back.
"""

import logging
import math
import os
import sqlite3
import struct
import tempfile
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.core.exceptions import GpkgNotFoundError, InvalidGpkgError

logger = logging.getLogger(__name__)
COORD_PRECISION = 6


# ── WKB parser ─────────────────────────────────────────────────────────────────

def _parse_gpkg_geometry(blob: bytes) -> List[List[Tuple[float, float]]]:
    if blob is None or len(blob) < 9:
        return []
    flags = blob[3]
    if (flags >> 4) & 0x01:
        return []
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    wkb_offset = 8 + envelope_bytes.get((flags >> 1) & 0x07, 0)
    wkb = blob[wkb_offset:]
    if len(wkb) < 5:
        return []
    endian    = "<" if wkb[0] == 1 else ">"
    geom_type = struct.unpack_from(endian + "I", wkb, 1)[0]

    def read_ring(data, offset):
        n = struct.unpack_from(endian + "I", data, offset)[0]
        offset += 4
        pts = []
        for _ in range(n):
            x, y = struct.unpack_from(endian + "dd", data, offset)
            pts.append((x, y))
            offset += 16
        return pts, offset

    rings, offset = [], 5
    if geom_type == 2:
        ring, _ = read_ring(wkb, offset)
        if ring:
            rings.append(ring)
    elif geom_type == 5:
        n_geoms = struct.unpack_from(endian + "I", wkb, offset)[0]
        offset += 4
        for _ in range(n_geoms):
            offset += 5
            ring, offset = read_ring(wkb, offset)
            if ring:
                rings.append(ring)
    return rings


def _node_key(lon: float, lat: float) -> str:
    return f"{round(lon, COORD_PRECISION)},{round(lat, COORD_PRECISION)}"


def _haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((math.radians(lat2 - lat1)) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin((math.radians(lon2 - lon1)) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(max(0.0, a)))


def _nearest_node_key(lat, lon, node_coords):
    best, best_d = None, float("inf")
    for key, (nlon, nlat) in node_coords.items():
        d = _haversine_m(lon, lat, nlon, nlat)
        if d < best_d:
            best_d, best = d, key
    return best


def _sanitise_id(raw: str) -> str:
    return raw.replace(" ", "_").replace(";", "_").replace(",", "_")


# ── .inp writer ────────────────────────────────────────────────────────────────

def _write_inp(
    node_coords:    Dict[str, Tuple[float, float]],
    node_demands:   Dict[str, float],
    reservoir_key:  str,
    reservoir_head: float,
    pipe_defs:      List[dict],
    duration_hrs:   int,
    time_step_min:  int,
    out_path:       str,
) -> None:

    # sequential safe IDs
    node_id: Dict[str, str] = {
        k: _sanitise_id(f"N_{i}") for i, k in enumerate(node_coords)
    }
    res_id = "RES_0"

    lines = []

    # [TITLE]
    lines += ["[TITLE]", "DUWAS Water Network", ""]

    # [JUNCTIONS]
    lines += ["[JUNCTIONS]", ";ID              Elev    Demand   Pattern"]
    for key, nid in node_id.items():
        if key == reservoir_key:
            continue
        demand = node_demands.get(key, 0.0)
        lines.append(f" {nid:<16} 0.00     {demand:.6f}")
    lines.append("")

    # [RESERVOIRS]
    lines += ["[RESERVOIRS]", ";ID              Head"]
    lines.append(f" {res_id:<16} {reservoir_head:.2f}")
    lines.append("")

    # [TANKS]
    lines += ["[TANKS]", ""]

    # [PIPES]
    lines += ["[PIPES]",
              ";ID              Node1           Node2           "
              "Length    Diameter  Roughness  MinorLoss  Status"]
    for i, p in enumerate(pipe_defs):
        pid = f"P_{i}"
        n1  = res_id if p["start_key"] == reservoir_key else node_id.get(p["start_key"])
        n2  = res_id if p["end_key"]   == reservoir_key else node_id.get(p["end_key"])
        if not n1 or not n2:
            continue
        lines.append(
            f" {pid:<16} {n1:<16} {n2:<16} "
            f"{p['length_m']:.2f}      {p['diam_mm']:.2f}      "
            f"{p['roughness']:.2f}       0          Open"
        )
    lines.append("")

    # [PUMPS] [VALVES]
    lines += ["[PUMPS]", "", "[VALVES]", ""]

    # [DEMANDS] [STATUS] [PATTERNS] [CURVES] [CONTROLS] [RULES]
    lines += ["[DEMANDS]", "", "[STATUS]", "",
              "[PATTERNS]", "", "[CURVES]", "",
              "[CONTROLS]", "", "[RULES]", ""]

    # [ENERGY]
    lines += ["[ENERGY]",
              " Global Efficiency  75",
              " Global Price       0",
              " Demand Charge      0", ""]

    # [EMITTERS] [QUALITY] [SOURCES]
    lines += ["[EMITTERS]", "", "[QUALITY]", "", "[SOURCES]", ""]

    # [REACTIONS]
    lines += ["[REACTIONS]",
              " Order Bulk   1", " Order Tank   1", " Order Wall   1",
              " Global Bulk  0", " Global Wall  0",
              " Limiting Potential    0",
              " Roughness Correlation 0", ""]

    # [MIXING]
    lines += ["[MIXING]", ""]

    # [TIMES]
    th, tm = divmod(duration_hrs * 60, 60)
    sh, sm = divmod(time_step_min, 60)
    lines += [
        "[TIMES]",
        f" Duration            {th}:{tm:02d}",
        f" Hydraulic Timestep  {sh}:{sm:02d}",
        f" Quality Timestep    {sh}:{sm:02d}",
        f" Pattern Timestep    {sh}:{sm:02d}",
        f" Pattern Start       0:00",
        f" Report Timestep     {sh}:{sm:02d}",
        f" Report Start        0:00",
        " Start ClockTime     12 am",
        " Statistic           NONE", "",
    ]

    # [REPORT]
    lines += ["[REPORT]",
              " Status    Yes", " Summary   No", " Page      0", ""]

    # [OPTIONS]
    lines += [
        "[OPTIONS]",
        " Units               CMH",
        " Headloss            H-W",
        " Specific Gravity    1",
        " Viscosity           1",
        " Trials              200",
        " Accuracy            0.01",
        " CHECKFREQ           2",
        " MAXCHECK            10",
        " DAMPLIMIT           0",
        " Unbalanced          Continue 10",
        " Pattern             1",
        " Demand Multiplier   1.0",
        " Emitter Exponent    0.5",
        " Quality             Age   hours",
        " Diffusivity         1",
        " Tolerance           0.01", "",
    ]

    # [COORDINATES]
    lines += ["[COORDINATES]", ";Node            X-Coord           Y-Coord"]
    for key, nid in node_id.items():
        if key == reservoir_key:
            continue
        lon, lat = node_coords[key]
        lines.append(f" {nid:<16} {lon:.8f}   {lat:.8f}")
    r_lon, r_lat = node_coords[reservoir_key]
    lines.append(f" {res_id:<16} {r_lon:.8f}   {r_lat:.8f}")
    lines.append("")

    # [VERTICES] [LABELS] [BACKDROP] [TAGS]
    lines += ["[VERTICES]", "", "[LABELS]", "",
              "[BACKDROP]", "", "[TAGS]", ""]

    lines.append("[END]")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    logger.info("Wrote EPANET .inp to %s  (%d lines)", out_path, len(lines))


# ── public helpers ─────────────────────────────────────────────────────────────

def list_gpkg_files() -> List[str]:
    settings = get_settings()
    d = settings.gpkg_dir
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith(".gpkg"))


def gpkg_full_path(filename: str) -> str:
    settings = get_settings()
    safe = os.path.basename(filename)
    path = os.path.join(settings.gpkg_dir, safe)
    if not os.path.isfile(path):
        raise GpkgNotFoundError(filename)
    return path


# ── main builder ───────────────────────────────────────────────────────────────

def build_inp_from_gpkg(
    filename:       str,
    pipe_status:    str   = "OPERATIONAL",
    base_demand:    float = 0.001,
    reservoir_head: float = 50.0,
    duration_hrs:   int   = 24,
    time_step_min:  int   = 60,
    extra_demands:  Optional[List[dict]] = None,
    inp_dir:        Optional[str]        = None,
) -> str:
    """
    Convert a .gpkg to an EPANET .inp and return its path.

    Demand units: m³/s in the API → converted to m³/h (CMH) in the .inp
    because [OPTIONS] Units = CMH.
    """
    path = gpkg_full_path(filename)
    logger.info("Loading .gpkg: %s  (status=%s)", filename, pipe_status)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    layer_row = cur.execute(
        "SELECT table_name FROM gpkg_contents LIMIT 1"
    ).fetchone()
    if not layer_row:
        conn.close()
        raise InvalidGpkgError("No layers found.")
    layer = layer_row[0]

    # Only fetch pipes with a real measured length (length3dig > 0)
    cur.execute(f"""
        SELECT fid, geom, intdiammm, roughness, length3dig
        FROM   {layer}
        WHERE  status = ?
          AND  geom IS NOT NULL
          AND  length3dig > 0
    """, (pipe_status,))
    rows = cur.fetchall()
    conn.close()
    logger.info("Fetched %d pipe rows (status=%s, length3dig>0)", len(rows), pipe_status)

    if not rows:
        raise InvalidGpkgError(
            f"No pipes with status='{pipe_status}' and positive length3dig found."
        )

    # ── collect pipe definitions ───────────────────────────────────────────────
    raw_pipes: List[dict] = []
    node_coords: Dict[str, Tuple[float, float]] = {}

    for row in rows:
        rings = _parse_gpkg_geometry(bytes(row["geom"]))
        if not rings:
            continue

        length_m  = float(row["length3dig"])
        diam_mm   = float(row["intdiammm"] or 100.0)
        roughness = float(row["roughness"]  or 130.0)

        # clamp values to physically meaningful ranges
        length_m  = max(1.0, min(length_m, 50_000.0))
        diam_mm   = max(25.0, min(diam_mm, 2000.0))     # 25 mm … 2 m
        roughness = max(60.0, min(roughness, 160.0))    # H-W range

        for ring in rings:
            if len(ring) < 2:
                continue
            start_lon, start_lat = ring[0]
            end_lon,   end_lat   = ring[-1]

            # skip degenerate pipes (start == end)
            if abs(start_lon - end_lon) < 1e-9 and abs(start_lat - end_lat) < 1e-9:
                continue

            for lon, lat in [(start_lon, start_lat), (end_lon, end_lat)]:
                key = _node_key(lon, lat)
                if key not in node_coords:
                    node_coords[key] = (lon, lat)

            raw_pipes.append({
                "fid":       row["fid"],
                "start_key": _node_key(start_lon, start_lat),
                "end_key":   _node_key(end_lon,   end_lat),
                "length_m":  length_m,
                "diam_mm":   diam_mm,
                "roughness": roughness,
            })

    # ── deduplicate pipes ──────────────────────────────────────────────────────
    seen: set = set()
    pipe_defs: List[dict] = []
    for p in raw_pipes:
        key = (p["start_key"], p["end_key"])
        rkey = (p["end_key"], p["start_key"])
        if key not in seen and rkey not in seen:
            seen.add(key)
            pipe_defs.append(p)

    logger.info("Unique nodes: %d  |  unique pipes: %d", len(node_coords), len(pipe_defs))

    # ── prune isolated nodes (not connected to any pipe) ──────────────────────
    connected_keys: set = set()
    for p in pipe_defs:
        connected_keys.add(p["start_key"])
        connected_keys.add(p["end_key"])
    node_coords = {k: v for k, v in node_coords.items() if k in connected_keys}
    logger.info("Connected nodes after pruning: %d", len(node_coords))

    if not node_coords or not pipe_defs:
        raise InvalidGpkgError("No connected pipe network found after cleaning.")

    # ── choose reservoir: most-connected node ──────────────────────────────────
    degree: Dict[str, int] = {k: 0 for k in node_coords}
    for p in pipe_defs:
        degree[p["start_key"]] = degree.get(p["start_key"], 0) + 1
        degree[p["end_key"]]   = degree.get(p["end_key"],   0) + 1
    reservoir_key = max(degree, key=lambda k: degree[k])
    logger.info(
        "Reservoir at node key: %s  (degree=%d)",
        reservoir_key, degree[reservoir_key],
    )

    # ── build demand map  (m³/s → m³/h for CMH units) ─────────────────────────
    base_cmh = base_demand * 3600.0
    node_demands: Dict[str, float] = {
        k: 0.0 if k == reservoir_key else base_cmh
        for k in node_coords
    }
    if extra_demands:
        for ed in extra_demands:
            key = _nearest_node_key(ed.get("lat", 0.0), ed.get("lon", 0.0), node_coords)
            if key and key != reservoir_key:
                node_demands[key] = node_demands.get(key, 0.0) + ed.get("demand_m3s", 0.005) * 3600.0
        logger.info("Applied %d extra demand entries", len(extra_demands))

    # ── write .inp ─────────────────────────────────────────────────────────────
    if inp_dir is None:
        inp_dir = tempfile.gettempdir()
    os.makedirs(inp_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(filename))[0]
    inp_path  = os.path.join(inp_dir, f"{base_name}.inp")

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
    return inp_path