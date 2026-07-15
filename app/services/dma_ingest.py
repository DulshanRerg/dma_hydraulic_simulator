# app/services/dma_ingest.py
"""
DMA (District Metered Area) asset ingestion from a multi-layer GeoPackage.

Reads the DUWASA.gpkg (produced by merging the original shapefiles) and
clips every asset layer to the DMA boundary polygon.

Outputs per-asset typed dataclasses that the EPANET builder and the
leakage reporting system can consume directly.

Material → Hazen-Williams C lookup (field data has roughness=0 everywhere):
  PE / HDPE  → 150   (very smooth plastic)
  PVC / UPVC → 140
  DI         → 130   (ductile iron lined)
  STEEL      → 120
  GS (galv.) → 100
  CAST IRON  → 100
  default    → 130
"""

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.core.exceptions import GpkgNotFoundError, InvalidGpkgError

logger = logging.getLogger(__name__)

# ── Hazen-Williams C by pipe material ────────────────────────────────────────

HW_C: Dict[str, float] = {
    "PE":         150.0,
    "HDPE":       150.0,
    "PVC":        140.0,
    "UPVC":       140.0,
    "uPVC":       140.0,
    "PVS":        140.0,   # likely typo for PVC
    "DI":         130.0,
    "STEEL":      120.0,
    "ST":         120.0,
    "GS":         100.0,
    "CAST IRON":  100.0,
}
HW_C_DEFAULT = 130.0


def hw_c(material: Optional[str]) -> float:
    if not material:
        return HW_C_DEFAULT
    return HW_C.get(str(material).strip().upper(), HW_C_DEFAULT)


# ── dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class DMAPipe:
    fid:          int
    start:        Tuple[float, float]    # (lon, lat)
    end:          Tuple[float, float]
    coords:       List[Tuple[float, float]]
    length_m:     float
    diam_mm:      float
    hw_c:         float
    material:     Optional[str]
    purpose:      Optional[str]
    status:       Optional[str]


@dataclass
class DMASource:
    """Borehole / intake – modelled as EPANET Reservoir with pump-boosted head."""
    fid:          int
    name:         str
    lon:          float
    lat:          float
    elev_m:       float         # ground elevation at borehole
    yield_m3h:    Optional[float]  # rated yield, m³/h
    status:       str
    # derived
    total_head_m: float         # reservoir head used in .inp


@dataclass
class DMATank:
    """Storage facility – modelled as EPANET Tank."""
    fid:          int
    name:         str
    lon:          float
    lat:          float
    elev_m:       float         # base elevation (bottom of tank)
    cap_m3:       Optional[float]
    depth_m:      Optional[float]
    diameter_m:   float         # derived from cap and depth
    init_level_m: float
    min_level_m:  float
    max_level_m:  float
    status:       str


@dataclass
class DMAValve:
    """Sluice / gate / butterfly / non-return / washout / air valve.

    `kind` drives how dma_builder.py represents the valve in the .inp:
      ISOLATION  -> throttling pipe stub, Status Open  (sluice/gate/butterfly)
      CHECK      -> plain pipe stub, Status CV          (non-return)
      WASHOUT    -> throttling pipe stub, Status Closed (normally-shut drain)
      AIR        -> plain junction (no stub)            (air/vacuum release)
      UNKNOWN    -> plain junction, but logged for manual mapping
                    (unrecognised free-text or bare numeric legacy codes)
    """
    fid:          int
    valve_type:   str           # raw ValveType text/code from the GIS layer
    lon:          float
    lat:          float
    elev_m:       float
    diam_mm:      float
    status:       str
    kind:         str           # ISOLATION | CHECK | WASHOUT | AIR | UNKNOWN
    is_isolation: bool          # True = ISOLATION (kept for backward compat)



@dataclass
class DMABulkMeter:
    """Bulk meter at the DMA boundary (Inlet or Outlet)."""
    fid:          int
    name:         str           # "Inlet" or "Outlet"
    lon:          float
    lat:          float


@dataclass
class DMAData:
    dma_name:     str
    dma_bbox:     Tuple[float, float, float, float]   # minlon,minlat,maxlon,maxlat
    dma_polygon:  List[Tuple[float, float]]           # exterior ring [(lon,lat),…]
    pipes:        List[DMAPipe]
    sources:      List[DMASource]
    tanks:        List[DMATank]
    valves:       List[DMAValve]
    bulk_meters:  List[DMABulkMeter]


# ── valve type classifier ──────────────────────────────────────────────────────
# Field data is hand-entered GIS text and is riddled with typos (SLIUCE VALVE,
# SLUIICE VALVE, GATE VAVLE, ...). A plain substring check on "SLUICE"/"GATE"
# misses several of these, silently dropping the valve to a plain junction.
# difflib gives us tolerance for that without hardcoding every typo we've seen.

def _classify_valve_type(vtype: str) -> str:
    """Map a raw (upper-cased, stripped) ValveType string to one of
    ISOLATION | CHECK | WASHOUT | AIR | UNKNOWN.

    UNKNOWN covers both unrecognised free text and the bare numeric legacy
    codes ("1", "2", "3", ...) that appear in this dataset with no legend —
    these are surfaced via a logger.warning in ingest_dma() rather than
    guessed at, so they can be mapped once their meaning is confirmed.
    """
    import difflib

    if not vtype or vtype.isdigit():
        return "UNKNOWN"

    # Short abbreviations seen in hand-entered data (exact token match only —
    # too short to fuzzy-match safely).
    abbrev = {"AV": "AIR", "GV": "ISOLATION", "SV": "ISOLATION", "NRV": "CHECK"}
    if vtype in abbrev:
        return abbrev[vtype]

    # Exact/substring checks first (cheap, covers the bulk of real rows).
    if "AIR" in vtype:
        return "AIR"
    if "NON RETURN" in vtype or "NON-RETURN" in vtype or "NON_RETURN" in vtype:
        return "CHECK"
    if "WASH" in vtype and "OUT" in vtype:
        return "WASHOUT"
    if "BUTTERFLY" in vtype:
        return "ISOLATION"
    if "SLUICE" in vtype or "GATE" in vtype:
        return "ISOLATION"

    # Fuzzy fallback for misspelled isolation-valve variants, e.g.
    # "SLIUCE VALVE", "SLUIICE VALVE", "SLICE VALVE", "SLUICE VAIVE".
    tokens = vtype.replace("-", " ").split()
    for token in tokens:
        if difflib.SequenceMatcher(None, token, "SLUICE").ratio() >= 0.75:
            return "ISOLATION"
        if difflib.SequenceMatcher(None, token, "GATE").ratio() >= 0.8:
            return "ISOLATION"

    return "UNKNOWN"


# ── WKB parser (pure stdlib, same approach as network_builder.py) ─────────────

def _parse_wkb_point(wkb: bytes) -> Optional[Tuple[float, float]]:
    import struct
    if not wkb or len(wkb) < 21:
        return None
    # skip 40-byte GPKG envelope header when present
    off = 0
    if wkb[:2] == b'GP':
        flags = wkb[3]
        envelope_type = (flags >> 1) & 0x07
        envelope_sizes = [0, 32, 48, 48, 64]
        env_size = envelope_sizes[envelope_type] if envelope_type < 5 else 0
        off = 8 + env_size
    try:
        bo = '<' if wkb[off] == 1 else '>'
        off += 1
        gtype = struct.unpack_from(bo + 'I', wkb, off)[0]
        off += 4
        if gtype == 1:  # Point
            x, y = struct.unpack_from(bo + 'dd', wkb, off)
            return (x, y)
        # Multi or collection: skip to first geometry
        if gtype in (4, 1001, 2001, 3001):
            n = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4
            if n == 0: return None
            off += 1  # byte order of inner
            inner_type = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4
            if inner_type == 1:
                x, y = struct.unpack_from(bo + 'dd', wkb, off)
                return (x, y)
    except Exception:
        pass
    return None


def _parse_wkb_linestrings(wkb: bytes) -> List[List[Tuple[float, float]]]:
    """Return list of coordinate rings from a WKB LineString / MultiLineString."""
    import struct
    if not wkb:
        return []
    off = 0
    if wkb[:2] == b'GP':
        flags = wkb[3]
        envelope_type = (flags >> 1) & 0x07
        env_size = [0, 32, 48, 48, 64][envelope_type] if envelope_type < 5 else 0
        off = 8 + env_size

    def _read_linestring(data, pos, bo):
        n_pts = struct.unpack_from(bo + 'I', data, pos)[0]; pos += 4
        pts = []
        for _ in range(n_pts):
            x, y = struct.unpack_from(bo + 'dd', data, pos); pos += 16
            pts.append((x, y))
        return pts, pos

    try:
        bo = '<' if wkb[off] == 1 else '>'
        off += 1
        gtype = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4

        if gtype == 2:  # LineString
            pts, _ = _read_linestring(wkb, off, bo)
            return [pts]

        if gtype == 5:  # MultiLineString
            n_geoms = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4
            rings = []
            for _ in range(n_geoms):
                bo2 = '<' if wkb[off] == 1 else '>'; off += 1
                _ = struct.unpack_from(bo2 + 'I', wkb, off)[0]; off += 4  # inner type
                pts, off = _read_linestring(wkb, off, bo2)
                rings.append(pts)
            return rings
    except Exception:
        pass
    return []


def _point_in_polygon(lon: float, lat: float, ring: List[Tuple[float, float]]) -> bool:
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            if lon < xi + (lat - yi) * (xj - xi) / ((yj - yi) or 1e-15):
                inside = not inside
        j = i
    return inside


def _any_point_in_polygon(pts: List[Tuple[float, float]], ring: List[Tuple[float, float]]) -> bool:
    return any(_point_in_polygon(x, y, ring) for x, y in pts)


def _haversine(lon1, lat1, lon2, lat2) -> float:
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.asin(math.sqrt(a))


# ── DMA polygon from GPKG ─────────────────────────────────────────────────────

def _read_dma_polygon(
    cur: sqlite3.Cursor,
    zone_name: str = None,
) -> Tuple[str, List[Tuple[float, float]]]:
    """
    Return (dma_name, exterior_ring) from the 'dma' layer.
    If zone_name is given (case-insensitive substring), select that row;
    otherwise the first row (smallest fid) is used.
    """
    import struct
    rows = cur.execute('SELECT fid, geom, "Name" FROM dma ORDER BY fid').fetchall()
    if not rows:
        raise InvalidGpkgError("No rows in 'dma' layer — upload a GeoPackage with a dma layer.")

    chosen = rows[0]
    if zone_name:
        zone_lower = zone_name.lower()
        for row in rows:
            if zone_lower in str(row[2] or "").lower():
                chosen = row
                break

    name = str(chosen[2] or "DMA")
    wkb  = bytes(chosen[1])

    off = 0
    if wkb[:2] == b'GP':
        flags         = wkb[3]
        envelope_type = (flags >> 1) & 0x07
        env_size      = [0, 32, 48, 48, 64][envelope_type] if envelope_type < 5 else 0
        off           = 8 + env_size
    bo    = '<' if wkb[off] == 1 else '>'; off += 1
    gtype = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4

    if gtype in (3, 6, 1003, 2003, 3003):
        if gtype in (6, 1006, 2006, 3006):
            n_polys = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4
            if n_polys == 0:
                raise InvalidGpkgError("Empty MultiPolygon in dma layer.")
            off += 5
        n_rings = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4
        n_pts   = struct.unpack_from(bo + 'I', wkb, off)[0]; off += 4
        ring = []
        for _ in range(n_pts):
            x, y = struct.unpack_from(bo + 'dd', wkb, off); off += 16
            ring.append((x, y))
        return name, ring
    raise InvalidGpkgError(f"Unsupported DMA geometry type: {gtype}")


# ── main ingest function ──────────────────────────────────────────────────────

# Operational status synonyms in the field data
_OPERATIONAL_STATUSES = {
    "OPERATIONAL", "OPERATING", "EXISTING", "OPERATIONA", "OPERATION",
    "OPERATIOAL", "OPERATIOMAL", "OERATIONAL", "GOOD",
}


def _is_operational(status: Optional[str]) -> bool:
    if not status:
        return True
    return status.strip().upper() in _OPERATIONAL_STATUSES


def _tank_diameter(cap_m3: Optional[float], depth_m: Optional[float]) -> float:
    """Estimate equivalent circular tank diameter from volume and depth."""
    if cap_m3 and cap_m3 > 0 and depth_m and depth_m > 0:
        return max(1.0, math.sqrt(4 * cap_m3 / (math.pi * depth_m)))
    if cap_m3 and cap_m3 > 0:
        return max(1.0, math.sqrt(cap_m3 / math.pi))   # assume depth ≈ √cap
    return 5.0  # fallback


def list_dma_zones(filename: str) -> list:
    """
    Return a list of all DMA zones (rows in the dma layer).
    Used by the frontend to let users pick one DMA when the file
    contains multiple DMA polygons.
    """
    settings = get_settings()
    import os
    path = os.path.join(settings.gpkg_dir, filename)
    if not os.path.isfile(path):
        raise GpkgNotFoundError(filename)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    layers_present = {r[0] for r in cur.execute("SELECT table_name FROM gpkg_contents").fetchall()}
    if "dma" not in layers_present:
        conn.close()
        return []
    rows = cur.execute('SELECT fid, "Name", geom FROM dma ORDER BY fid').fetchall()
    conn.close()
    result = []
    for row in rows:
        name = str(row["Name"] or f"DMA_{row['fid']}")
        try:
            _, ring = _read_dma_polygon(cur_stub := None, zone_name=None)
        except Exception:
            ring = []
        # Use a lightweight BBox via WKB envelope header (bytes 8-40) if present
        wkb = bytes(row["geom"])
        bbox = (0.0, 0.0, 0.0, 0.0)
        try:
            import struct
            if wkb[:2] == b'GP' and len(wkb) >= 40:
                flags = wkb[3]
                et = (flags >> 1) & 0x07
                if et == 1:   # has xmin,xmax,ymin,ymax
                    bo = '<' if (wkb[3] & 0x01) else '>'
                    xmin, xmax, ymin, ymax = struct.unpack_from(bo + 'dddd', wkb, 8)
                    bbox = (xmin, ymin, xmax, ymax)
        except Exception:
            pass
        result.append({"fid": int(row["fid"]), "name": name, "bbox": bbox})
    return result


def ingest_dma(filename: str, clip_to_dma: bool = True, zone_name: str = None) -> "DMAData":
    """
    Load all DMA assets from `filename`.

    Parameters
    ----------
    filename     : base filename of the .gpkg in GPKG_DIR
    clip_to_dma  : clip all assets to the DMA polygon boundary
    zone_name    : when the file has multiple DMA polygons, pick the one
                   whose Name matches this string (case-insensitive substring
                   match). If None, the first row is used.
    """
    settings = get_settings()
    import os
    path = os.path.join(settings.gpkg_dir, filename)
    if not os.path.isfile(path):
        raise GpkgNotFoundError(filename)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # check required layers exist
    layers_present = {r[0] for r in cur.execute("SELECT table_name FROM gpkg_contents").fetchall()}
    required = {"dma", "waterpipes"}
    missing = required - layers_present
    if missing:
        raise InvalidGpkgError(f"Missing required layers in GeoPackage: {missing}")

    # ── DMA polygon ────────────────────────────────────────────────────────────
    dma_name, dma_ring = _read_dma_polygon(cur, zone_name=zone_name)
    lons = [p[0] for p in dma_ring]; lats = [p[1] for p in dma_ring]
    dma_bbox = (min(lons), min(lats), max(lons), max(lats))
    logger.info("DMA '%s' bbox=%s", dma_name, dma_bbox)

    def _in_dma(lon: float, lat: float) -> bool:
        if not clip_to_dma:
            return True
        return _point_in_polygon(lon, lat, dma_ring)

    def _linestring_in_dma(pts: List[Tuple[float, float]]) -> bool:
        if not clip_to_dma:
            return True
        return _any_point_in_polygon(pts, dma_ring)

    # ── pipes ──────────────────────────────────────────────────────────────────
    pipes_out: List[DMAPipe] = []
    if "waterpipes" in layers_present:
        for row in cur.execute(
            'SELECT fid, geom, intdiammm, material, length3dig, status, pipepurpos '
            'FROM waterpipes WHERE geom IS NOT NULL'
        ).fetchall():
            rings = _parse_wkb_linestrings(bytes(row["geom"]))
            for ri, ring in enumerate(rings):
                if len(ring) < 2:
                    continue
                if not _linestring_in_dma(ring):
                    continue
                status = str(row["status"] or "")
                if not _is_operational(status):
                    continue
                diam = max(10.0, min(float(row["intdiammm"] or 50), 2000.0))
                length = max(1.0, float(row["length3dig"] or 10))
                mat = row["material"]
                pipes_out.append(DMAPipe(
                    fid      = int(row["fid"]),
                    start    = ring[0],
                    end      = ring[-1],
                    coords   = ring,
                    length_m = length,
                    diam_mm  = diam,
                    hw_c     = hw_c(mat),
                    material = mat,
                    purpose  = row["pipepurpos"],
                    status   = status,
                ))

    logger.info("Pipes in DMA (operational): %d", len(pipes_out))

    # ── water sources (boreholes / intakes) ────────────────────────────────────
    sources_out: List[DMASource] = []
    if "watersources" in layers_present:
        for row in cur.execute(
            'SELECT fid, geom, "Name", "ElevationM", "YieldCapM3", "HeadPatter", "Status" '
            'FROM watersources WHERE geom IS NOT NULL'
        ).fetchall():
            pt = _parse_wkb_point(bytes(row["geom"]))
            if not pt:
                continue
            lon, lat = pt
            if not _in_dma(lon, lat):
                continue
            status = str(row["Status"] or "OPERATIONAL")
            if not _is_operational(status):
                continue
            elev = float(row["ElevationM"] or 0) or 1080.0
            yield_val = row["YieldCapM3"]
            yield_m3h = float(yield_val) if yield_val is not None else None
            # Head pattern field holds pump total head for some sources
            try:
                head_pattern = float(row["HeadPatter"])
                total_head = elev + head_pattern  # elevation + pump head offset
            except (TypeError, ValueError):
                # Assume submersible pump lifts to ~30m above ground surface
                total_head = elev + 30.0
            sources_out.append(DMASource(
                fid          = int(row["fid"]),
                name         = str(row["Name"] or f"SRC_{row['fid']}"),
                lon          = lon,
                lat          = lat,
                elev_m       = elev,
                yield_m3h    = yield_m3h,
                status       = status,
                total_head_m = total_head,
            ))

    logger.info("Water sources in DMA (operational): %d", len(sources_out))

    # ── storage facilities (tanks) ─────────────────────────────────────────────
    tanks_out: List[DMATank] = []
    if "storagefacility" in layers_present:
        for row in cur.execute(
            'SELECT fid, geom, "Name", "ElevationM", "TankCapaci", "TankDepthM", '
            '"TankBaseEl", "Status" '
            'FROM storagefacility WHERE geom IS NOT NULL'
        ).fetchall():
            pt = _parse_wkb_point(bytes(row["geom"]))
            if not pt:
                continue
            lon, lat = pt
            if not _in_dma(lon, lat):
                continue
            status = str(row["Status"] or "OPERATING")
            if not _is_operational(status):
                continue
            elev = float(row["ElevationM"] or 0) or 1100.0
            cap  = row["TankCapaci"];  cap  = float(cap)  if cap  else None
            dep  = row["TankDepthM"];  dep  = float(dep)  if dep  else None
            base = row["TankBaseEl"];
            # Base elevation: TankBaseEl if available, else elev - depth (approx)
            if base:
                try:
                    base_elev = float(base)
                except (TypeError, ValueError):
                    base_elev = elev - (dep or 3.0)
            else:
                base_elev = elev - (dep or 3.0)
            max_lvl  = dep if dep else 3.0
            init_lvl = max_lvl * 0.6
            diam     = _tank_diameter(cap, dep)
            tanks_out.append(DMATank(
                fid          = int(row["fid"]),
                name         = str(row["Name"] or f"TANK_{row['fid']}"),
                lon          = lon,
                lat          = lat,
                elev_m       = base_elev,
                cap_m3       = cap,
                depth_m      = dep,
                diameter_m   = diam,
                init_level_m = init_lvl,
                min_level_m  = 0.3,
                max_level_m  = max_lvl,
                status       = status,
            ))

    logger.info("Tanks in DMA (operating): %d", len(tanks_out))

    # ── valves ─────────────────────────────────────────────────────────────────
    valves_out: List[DMAValve] = []
    if "valves" in layers_present:
        for row in cur.execute(
            'SELECT fid, geom, "ValveType", "ElevationM", "NomDiamMm", "Status" '
            'FROM valves WHERE geom IS NOT NULL'
        ).fetchall():
            pt = _parse_wkb_point(bytes(row["geom"]))
            if not pt:
                continue
            lon, lat = pt
            if not _in_dma(lon, lat):
                continue
            vtype  = str(row["ValveType"] or "").strip().upper()
            status = str(row["Status"] or "OPERATIONAL")
            elev   = row["ElevationM"]; elev = float(elev) if elev else 1100.0
            diam   = row["NomDiamMm"];  diam = float(diam) if diam else 100.0
            kind   = _classify_valve_type(vtype)
            valves_out.append(DMAValve(
                fid          = int(row["fid"]),
                valve_type   = str(row["ValveType"] or "UNKNOWN"),
                lon          = lon,
                lat          = lat,
                elev_m       = elev,
                diam_mm      = diam,
                status       = status,
                kind         = kind,
                is_isolation = (kind == "ISOLATION"),
            ))

    unknown_types = sorted({v.valve_type for v in valves_out if v.kind == "UNKNOWN"})
    if unknown_types:
        logger.warning(
            "Valves in DMA: %d unclassified ValveType value(s) fell back to "
            "plain junctions (no isolation/check/washout behaviour modelled): %s",
            len(unknown_types), unknown_types,
        )
    logger.info("Valves in DMA: %d", len(valves_out))

    # ── bulk meters ────────────────────────────────────────────────────────────
    bm_out: List[DMABulkMeter] = []
    if "bulk_meters" in layers_present:
        for row in cur.execute('SELECT fid, geom, "Name" FROM bulk_meters WHERE geom IS NOT NULL').fetchall():
            pt = _parse_wkb_point(bytes(row["geom"]))
            if not pt:
                continue
            lon, lat = pt
            # include all bulk meters (they define the DMA boundary, so always relevant)
            bm_out.append(DMABulkMeter(
                fid  = int(row["fid"]),
                name = str(row["Name"] or f"BM_{row['fid']}"),
                lon  = lon,
                lat  = lat,
            ))

    conn.close()
    logger.info(
        "DMA ingest complete: %d pipes, %d sources, %d tanks, %d valves, %d bulk_meters",
        len(pipes_out), len(sources_out), len(tanks_out), len(valves_out), len(bm_out),
    )
    return DMAData(
        dma_name    = dma_name,
        dma_bbox    = dma_bbox,
        dma_polygon = dma_ring,
        pipes       = pipes_out,
        sources     = sources_out,
        tanks       = tanks_out,
        valves      = valves_out,
        bulk_meters = bm_out,
    )