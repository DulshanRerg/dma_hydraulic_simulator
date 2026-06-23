# app/services/dma_builder.py
"""
EPANET .inp builder for a DMA (District Metered Area).

Translates DMAData (from dma_ingest.py) into a properly structured
EPANET input file with:

  RESERVOIRS  — each OPERATIONAL borehole/intake becomes a reservoir
                node whose Total Head = ground elevation + pump head.
  TANKS       — OPERATING storage facilities with capacity-derived geometry.
  JUNCTIONS   — every unique pipe endpoint, with elevation interpolated
                from the nearest water source elevation and
                demand = total DMA demand / number of junctions.
  PIPES       — using material-derived Hazen-Williams C values.
  VALVES      — sluice/gate valves as TCV (throttle control) elements;
                air valves as plain junctions.
  BULK METERS — snapped to nearest pipe node; used as demand-free
                observation junctions (zero-demand).  Their pipe-segment
                flows are tagged in [REPORT] for NRW computation.

NRW / leakage estimation
-------------------------
After simulation, the caller can compute:
    NRW  = Σ(flow at DMA inlet bulk-meter pipes) − Σ(junction demands)
    Real leakage ≈ NRW − billed-unmetered − admin losses

The simulation result endpoint already returns per-pipe flows, so the
frontend can show this directly on the map.

EPANET units used: CMH (m³/h) throughout.
"""

import logging
import math
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from app.services.dma_ingest import DMAData, DMAPipe, DMASource, DMATank, DMAValve, DMABulkMeter
from app.services.network_subset import snap_and_build_graph, connected_components
from app.services.dma_ingest import _haversine

logger = logging.getLogger(__name__)

# EPANET global options
_DURATION_HRS_DEFAULT  = 24
_TIMESTEP_MIN_DEFAULT  = 60
_DEMAND_MULTIPLIER     = 1.0
_VISCOSITY             = 1.0     # kinematic viscosity (× water at 20 °C)
_SPEC_GRAVITY          = 1.0

# Typical per-connection demand estimate (Tanzania urban):
# ~80 L/person/day, ~6 persons/connection, ~2 connections/junction node
# ≈ 960 L/junction/day ≈ 0.0111 m³/h per junction
_BASE_DEMAND_M3H = 0.011   # m³/h per junction node

# Minimum pipe length EPANET will accept
_MIN_PIPE_LEN_M = 1.0

# snap tolerance for endpoint merging (same as network_subset default)
_SNAP_TOL_M = 2.0


# ── tiny graph / snap structures (reuse from network_subset) ─────────────────

def _raw_pipe_for_subset(p: DMAPipe):
    """Convert DMAPipe to the duck-typed object that snap_and_build_graph expects."""
    class _FakePipe:
        def __init__(self):
            self.pipe_uid    = f"{p.fid}:0"
            self.fid         = p.fid
            self.start       = p.start
            self.end         = p.end
            self.full_coords = p.coords
            self.length_m    = p.length_m
            self.diam_mm     = p.diam_mm
            self.roughness   = p.hw_c
            self.status      = p.status or ""
            self.material    = p.material
    return _FakePipe()


def _nearest_node(lon: float, lat: float, node_coords: Dict[str, Tuple[float, float]]) -> str:
    return min(node_coords, key=lambda k: _haversine(lon, lat, node_coords[k][0], node_coords[k][1]))


def _elevation_at_node(
    lon: float, lat: float,
    sources: List[DMASource],
    tanks: List[DMATank],
    default_elev: float = 1100.0,
) -> float:
    """
    Interpolate ground elevation for a junction using IDW from the nearest
    boreholes and tanks (whose elevations come from the GIS data).
    Falls back to the average of all source elevations.
    """
    all_pts = (
        [(s.lon, s.lat, s.elev_m) for s in sources]
        + [(t.lon, t.lat, t.elev_m + (t.depth_m or 3.0)) for t in tanks]
    )
    if not all_pts:
        return default_elev

    # IDW with p=2 distance exponent
    total_w, total_wz = 0.0, 0.0
    for px, py, ez in all_pts:
        d = max(1.0, _haversine(lon, lat, px, py))
        w = 1.0 / (d * d)
        total_w  += w
        total_wz += w * ez
    return total_wz / total_w if total_w > 0 else default_elev


# ── EPANET .inp writer ────────────────────────────────────────────────────────

def build_dma_inp(
    dma:             DMAData,
    inp_dir:         Optional[str] = None,
    duration_hrs:    int   = _DURATION_HRS_DEFAULT,
    time_step_min:   int   = _TIMESTEP_MIN_DEFAULT,
    base_demand_m3h: float = _BASE_DEMAND_M3H,
    leakage_frac:    float = 0.0,   # extra demand fraction modelling background leakage
) -> str:
    """
    Build an EPANET .inp for the DMA and return the file path.

    Parameters
    ----------
    dma             : DMAData returned by dma_ingest.ingest_dma()
    inp_dir         : directory to write the .inp (defaults to tempdir)
    duration_hrs    : simulation duration
    time_step_min   : hydraulic time step
    base_demand_m3h : demand per junction node (m³/h)
    leakage_frac    : additional demand fraction to model background leakage
                      (0.15 = +15% extra demand at every node)
    """
    if not dma.pipes:
        raise ValueError("DMA has no operational pipes — cannot build .inp.")

    # ── snap pipe endpoints → node graph ──────────────────────────────────────
    fake_pipes = [_raw_pipe_for_subset(p) for p in dma.pipes]
    graph = snap_and_build_graph(fake_pipes, tolerance_m=_SNAP_TOL_M)

    # Take the largest connected component (the main distribution network)
    comps = connected_components(graph)
    if not comps:
        raise ValueError("No connected pipe graph in the DMA — check the pipe data.")
    comp = comps[0]
    if len(comps) > 1:
        logger.warning(
            "DMA graph has %d components; using the largest (%d pipes). "
            "Consider increasing snap tolerance.",
            len(comps), len(comp.pipe_fids),
        )

    node_set    = set(comp.node_keys)
    node_coords: Dict[str, Tuple[float, float]] = {k: graph.node_coords[k] for k in node_set}

    # Collect edges for the main component
    uid_set     = set(comp.pipe_uids)
    comp_edges  = [e for e in graph.edges if e["pipe_uid"] in uid_set]

    # ── snap sources, tanks, bulk meters to nearest node ─────────────────────
    def _snap(lon: float, lat: float) -> str:
        return _nearest_node(lon, lat, node_coords)

    source_nodes: Dict[str, DMASource] = {_snap(s.lon, s.lat): s for s in dma.sources}
    tank_nodes:   Dict[str, DMATank]   = {_snap(t.lon, t.lat): t for t in dma.tanks}
    bm_nodes:     Dict[str, DMABulkMeter] = {_snap(b.lon, b.lat): b for b in dma.bulk_meters}

    # Air valves → treated as junctions (open); sluice/gate → TCV inline
    valve_nodes: Dict[str, DMAValve] = {_snap(v.lon, v.lat): v for v in dma.valves}

    # Node categories (a node can only play one primary role)
    reservoir_keys = set(source_nodes.keys())
    tank_keys      = set(tank_nodes.keys()) - reservoir_keys
    junction_keys  = node_set - reservoir_keys - tank_keys

    # ── elevation for each junction ────────────────────────────────────────────
    avg_src_elev = (
        sum(s.elev_m for s in dma.sources) / len(dma.sources)
        if dma.sources else 1100.0
    )

    def _junc_elev(key: str) -> float:
        lon, lat = node_coords[key]
        return _elevation_at_node(lon, lat, dma.sources, dma.tanks, default_elev=avg_src_elev)

    demand_m3h = base_demand_m3h * (1.0 + leakage_frac)

    # ── TCV (valve) pipe segments ─────────────────────────────────────────────
    # Sluice/gate valves split a pipe into two stubs connected through the TCV.
    # We add them as separate short-pipe + TCV elements.
    tcv_counter = 0
    tcv_elements: List[dict] = []   # {from_node, to_node, diam_mm, valve_id}

    # ── write .inp ─────────────────────────────────────────────────────────────
    if inp_dir is None:
        inp_dir = tempfile.gettempdir()
    os.makedirs(inp_dir, exist_ok=True)
    out_path = os.path.join(inp_dir, "dma_network.inp")

    lines: List[str] = []

    def sec(title: str):
        lines.append(f"\n[{title}]")

    def row(*cols):
        lines.append("  " + "  ".join(str(c) for c in cols))

    lines.append("; EPANET input file — DUWASA DMA hydraulic model")
    lines.append(f"; DMA: {dma.dma_name}")
    lines.append(f"; Generated by dma_builder.py")
    lines.append(f"; Pipes: {len(comp_edges)}  Nodes: {len(node_set)}")
    lines.append(f"; Sources: {len(dma.sources)}  Tanks: {len(dma.tanks)}  Bulk meters: {len(dma.bulk_meters)}")

    # ── [TITLE] ───────────────────────────────────────────────────────────────
    sec("TITLE")
    lines.append(f"DUWASA {dma.dma_name} — DMA Hydraulic Simulation")

    # ── [JUNCTIONS] ───────────────────────────────────────────────────────────
    sec("JUNCTIONS")
    row(";ID", "Elev(m)", "Demand(m3/h)", "Pattern")
    for key in sorted(junction_keys):
        is_bm   = key in bm_nodes     # bulk meter → zero demand
        demand  = 0.0 if is_bm else demand_m3h
        elev    = round(_junc_elev(key), 1)
        row(key, elev, round(demand, 6), "")

    # ── [RESERVOIRS] ─────────────────────────────────────────────────────────
    sec("RESERVOIRS")
    row(";ID", "Head(m)", "Pattern")
    for key, src in source_nodes.items():
        row(key, round(src.total_head_m, 1), "")

    # ── [TANKS] ───────────────────────────────────────────────────────────────
    sec("TANKS")
    row(";ID", "Elev(m)", "InitLvl(m)", "MinLvl(m)", "MaxLvl(m)", "Diam(m)", "MinVol(m3)", "VolCurve")
    for key, tank in tank_nodes.items():
        row(
            key,
            round(tank.elev_m, 1),
            round(tank.init_level_m, 2),
            round(tank.min_level_m,  2),
            round(tank.max_level_m,  2),
            round(tank.diameter_m,   2),
            0,
            "",
        )

    # ── [PIPES] ───────────────────────────────────────────────────────────────
    sec("PIPES")
    row(";ID", "Node1", "Node2", "Length(m)", "Diam(mm)", "C(H-W)", "Minor", "Status")
    for idx, e in enumerate(comp_edges):
        length = max(_MIN_PIPE_LEN_M, e["length_m"])
        row(
            f"P_{idx}",
            e["node_a"],
            e["node_b"],
            round(length, 2),
            round(e["diam_mm"], 1),
            round(e["roughness"], 1),   # roughness field holds H-W C in our graph
            0,
            "Open",
        )

    # ── [VALVES] ─────────────────────────────────────────────────────────────
    # TCV on a short stub pipe between the valve node and a helper node
    # (only isolation valves; air valves are junctions already)
    valve_lines = []
    for key, v in valve_nodes.items():
        if not v.is_isolation:
            continue
        helper = f"V_HELP_{v.fid}"
        node_coords[helper] = node_coords[key]   # same location as the valve junction
        tcv_counter += 1
        tcv_id = f"TCV_{v.fid}"
        diam   = max(25.0, v.diam_mm)
        valve_lines.append(f"  {tcv_id}  {key}  {helper}  {diam:.1f}  TCV  2.0  Open")

    sec("VALVES")
    row(";ID", "Node1", "Node2", "Diam(mm)", "Type", "Setting", "MCoeff")
    lines.extend(valve_lines)

    # ── [DEMANDS] (blank — embedded in JUNCTIONS already) ────────────────────
    sec("DEMANDS")
    row(";Junction", "Demand", "Pattern", "Category")

    # ── [PATTERNS] ────────────────────────────────────────────────────────────
    sec("PATTERNS")
    row(";ID", "Multipliers")
    # 24-hour diurnal demand pattern (typical urban Tanzania)
    diurnal = [
        0.4, 0.3, 0.3, 0.3, 0.5, 0.8,
        1.2, 1.5, 1.4, 1.2, 1.0, 0.9,
        1.0, 1.1, 1.0, 0.9, 1.0, 1.3,
        1.4, 1.2, 1.0, 0.8, 0.6, 0.4,
    ]
    row(";DIURNAL", " ".join(str(v) for v in diurnal))

    # ── [ENERGY] ──────────────────────────────────────────────────────────────
    sec("ENERGY")
    lines.append("  Global Efficiency  75")
    lines.append("  Global Price       0")

    # ── [REACTIONS] ───────────────────────────────────────────────────────────
    sec("REACTIONS")
    lines.append("  Order Bulk  1")
    lines.append("  Order Tank  1")
    lines.append("  Order Wall  1")
    lines.append("  Global Bulk  0")
    lines.append("  Global Wall  0")

    # ── [TIMES] ───────────────────────────────────────────────────────────────
    sec("TIMES")
    lines.append(f"  Duration           {duration_hrs}:00")
    lines.append(f"  Hydraulic Timestep 0:{time_step_min:02d}:00")
    lines.append( "  Quality Timestep   0:05:00")
    lines.append( "  Pattern Timestep   1:00:00")
    lines.append( "  Pattern Start      0:00:00")
    lines.append( "  Report Timestep    1:00:00")
    lines.append( "  Report Start       0:00:00")
    lines.append( "  Start ClockTime    12 am")
    lines.append( "  Statistic          NONE")

    # ── [REPORT] ──────────────────────────────────────────────────────────────
    sec("REPORT")
    lines.append("  Status      Yes")
    lines.append("  Summary     Yes")
    lines.append("  Page        55")

    # ── [OPTIONS] ─────────────────────────────────────────────────────────────
    sec("OPTIONS")
    lines.append("  Units           CMH")
    lines.append("  Headloss        H-W")        # Hazen-Williams
    lines.append("  Specific Gravity 1.0")
    lines.append("  Viscosity       1.0")
    lines.append("  Trials          200")
    lines.append("  Accuracy        0.001")
    lines.append("  CHECKFREQ       2")
    lines.append("  MAXCHECK        10")
    lines.append("  DAMPLIMIT       0")
    lines.append("  Unbalanced      Continue 10")
    lines.append("  Pattern         ")
    lines.append("  Demand Multiplier 1.0")
    lines.append("  Emitter Exponent  0.5")
    lines.append("  Quality         Age")
    lines.append("  Diffusivity     1")
    lines.append("  Tolerance       0.01")

    # ── [COORDINATES] ─────────────────────────────────────────────────────────
    sec("COORDINATES")
    row(";Node", "X-Coord", "Y-Coord")
    for key, (lon, lat) in node_coords.items():
        row(key, round(lon, 7), round(lat, 7))

    # ── [END] ─────────────────────────────────────────────────────────────────
    sec("END")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(
        "DMA .inp written: %s | %d junctions, %d reservoirs, %d tanks, %d pipes, %d TCVs",
        out_path,
        len(junction_keys),
        len(reservoir_keys),
        len(tank_keys),
        len(comp_edges),
        tcv_counter,
    )
    return out_path


# ── NRW helper ────────────────────────────────────────────────────────────────

def estimate_nrw(
    inlet_flow_m3h:     float,
    total_demand_m3h:   float,
    outlet_flow_m3h:    float = 0.0,
) -> dict:
    """
    Simple NRW (Non-Revenue Water) estimate from bulk meter flows.

    Parameters
    ----------
    inlet_flow_m3h   : Total flow into DMA measured at inlet bulk meter (m³/h)
    total_demand_m3h : Sum of simulated junction demands (m³/h)
    outlet_flow_m3h  : Flow out of DMA at outlet bulk meter (m³/h), if applicable

    Returns a dict with nrw_m3h, nrw_fraction, leakage_m3h.
    """
    system_input  = inlet_flow_m3h - outlet_flow_m3h
    nrw_m3h       = max(0.0, system_input - total_demand_m3h)
    nrw_fraction  = (nrw_m3h / system_input) if system_input > 0 else 0.0
    return {
        "system_input_m3h":  round(system_input,  3),
        "authorised_m3h":    round(total_demand_m3h, 3),
        "nrw_m3h":           round(nrw_m3h,        3),
        "nrw_fraction":      round(nrw_fraction,    4),
        "nrw_pct":           round(nrw_fraction * 100, 1),
        "leakage_m3h":       round(nrw_m3h * 0.85, 3),   # ~85% of NRW is real loss
    }