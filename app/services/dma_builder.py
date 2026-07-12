# app/services/dma_builder.py
"""
EPANET .inp builder for a DMA — topology-repair edition.

Pipeline
--------
1. topology_repair.repair_topology()
   • Shapely-snap all pipe geometries at 20 m in UTM 37S
   • Split at T-intersections (linemerge)
   • MST connector insertion to make the network fully connected

2. build_dma_inp()
   • Map boreholes → [RESERVOIRS]
   • Map storage tanks → [TANKS]
   • Map all other nodes → [JUNCTIONS] with IDW-interpolated elevation
   • Map repaired edges → [PIPES] (Hazen-Williams, material-derived C)
   • Snap valves to nearest node → [VALVES] (TCV for sluice/gate)
   • Bulk meter nodes → zero-demand junctions for NRW monitoring
   • Return inp_path + repair report (synthetic connectors)

EPANET units: CMH (m³/h), Hazen-Williams headloss.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from app.services.dma_ingest import (
    DMAData, DMABulkMeter, DMAPipe, DMASource, DMATank, DMAValve,
    _haversine,
)
from app.services.topology_repair import (
    NetworkEdge, NetworkNode, RepairReport, RepairedNetwork,
    repair_topology,
)

logger = logging.getLogger(__name__)

_DURATION_HRS_DEFAULT  = 24
_TIMESTEP_MIN_DEFAULT  = 60
_BASE_DEMAND_M3H       = 0.011    # m³/h per junction (80 L/p/d × 6p/conn × 2 conn/node)
_SNAP_TOL_M            = 20.0
_NODE_GRID_M           = 5.0
_MIN_PIPE_LEN          = 0.5      # m — EPANET minimum


# ── nearest node helper ───────────────────────────────────────────────────────

def _nearest_node_id(
    lon: float, lat: float,
    nodes: Dict[str, NetworkNode],
) -> str:
    return min(nodes, key=lambda k: _haversine(lon, lat, nodes[k].lon, nodes[k].lat))


# ── IDW elevation interpolation ────────────────────────────────────────────────

def _idw_elev(
    lon: float, lat: float,
    sources: List[DMASource],
    tanks:   List[DMATank],
    default: float = 1100.0,
) -> float:
    pts = (
        [(s.lon, s.lat, s.elev_m) for s in sources]
        + [(t.lon, t.lat, t.elev_m + (t.depth_m or 3.0)) for t in tanks]
    )
    if not pts:
        return default
    tw = wz = 0.0
    for px, py, ez in pts:
        d = max(1.0, _haversine(lon, lat, px, py))
        w = 1.0 / (d * d)
        tw += w; wz += w * ez
    return wz / tw if tw else default


# ── .inp section writer ───────────────────────────────────────────────────────

class _INP:
    def __init__(self):
        self._lines: List[str] = []

    def comment(self, text: str):
        self._lines.append(f"; {text}")

    def section(self, name: str):
        self._lines.append(f"\n[{name}]")

    def row(self, *cols):
        self._lines.append("  " + "  ".join(str(c) for c in cols))

    def raw(self, text: str):
        self._lines.append(text)

    def write(self, path: str):
        text = "\n".join(self._lines) + "\n"
        # EPANET parser is strict ASCII — replace any stray non-ASCII chars
        safe = text.encode("ascii", errors="replace").decode("ascii")
        with open(path, "w", encoding="ascii") as f:
            f.write(safe)


# ── main build function ───────────────────────────────────────────────────────

def build_dma_inp(
    dma:             DMAData,
    inp_dir:         Optional[str] = None,
    duration_hrs:    int   = _DURATION_HRS_DEFAULT,
    time_step_min:   int   = _TIMESTEP_MIN_DEFAULT,
    base_demand_m3h: float = _BASE_DEMAND_M3H,
    leakage_frac:    float = 0.20,
    snap_tol_m:      float = _SNAP_TOL_M,
) -> Tuple[str, RepairReport]:
    """
    Build a fully-connected EPANET DMA model.

    Returns
    -------
    (inp_path, report)
        inp_path — absolute path to the written .inp file
        report   — RepairReport listing every synthetic connector added
    """
    if not dma.pipes:
        raise ValueError("DMA has no operational pipes — cannot build .inp.")

    # ── 1. topology repair ────────────────────────────────────────────────────
    pipe_coords = [p.coords for p in dma.pipes]
    pipe_meta   = [
        {"diam_mm": p.diam_mm, "hw_c": p.hw_c, "material": p.material or "PE",
         "length_m": p.length_m}
        for p in dma.pipes
    ]
    network = repair_topology(
        pipe_coords  = pipe_coords,
        pipe_meta    = pipe_meta,
        snap_tol_m   = snap_tol_m,
        node_grid_m  = _NODE_GRID_M,
    )
    report  = network.report
    nodes   = network.nodes
    edges   = network.edges

    logger.info(
        "Repaired DMA graph: %d nodes, %d edges (%d synthetic connectors), "
        "1 component (was %d)",
        len(nodes), len(edges),
        len(report.connectors_added),
        report.original_component_count,
    )

    # ── 2. snap assets to nearest graph node ──────────────────────────────────
    def _snap(lon: float, lat: float) -> str:
        return _nearest_node_id(lon, lat, nodes)

    source_nodes: Dict[str, DMASource]    = {_snap(s.lon, s.lat): s for s in dma.sources}
    tank_nodes:   Dict[str, DMATank]      = {_snap(t.lon, t.lat): t for t in dma.tanks}
    bm_nodes:     Dict[str, DMABulkMeter] = {_snap(b.lon, b.lat): b for b in dma.bulk_meters}
    valve_nodes:  Dict[str, DMAValve]     = {_snap(v.lon, v.lat): v for v in dma.valves}

    reservoir_keys = set(source_nodes.keys())
    tank_keys      = set(tank_nodes.keys()) - reservoir_keys
    junction_keys  = set(nodes.keys()) - reservoir_keys - tank_keys

    avg_src_elev = (
        sum(s.elev_m for s in dma.sources) / len(dma.sources)
        if dma.sources else 1100.0
    )

    demand_m3h = base_demand_m3h * (1.0 + leakage_frac)

    # ── 3a. Pre-compute valve helpers BEFORE writing any section ─────────────
    # EPANET TCV requires both nodes to already be pipe-connected, which is
    # impossible for our snapped-to-junction valve nodes.  Instead, model each
    # isolation valve (sluice / gate) as a short 0.5 m stub pipe with:
    #   - high minor-loss coefficient (10.0) to simulate throttling resistance
    #   - status = Open  (field engineer can mark it Closed per scenario)
    # Air valves → plain junctions (already handled by snapping to nearest node).
    valve_pipe_rows: List[str] = []
    for key, v in valve_nodes.items():
        if not v.is_isolation:
            continue
        helper_id = f"VH_{v.fid}"
        nd = nodes[key]
        nodes[helper_id] = NetworkNode(
            node_id=helper_id, lon=nd.lon, lat=nd.lat, x_m=nd.x_m, y_m=nd.y_m,
        )
        junction_keys.add(helper_id)   # VH_ is a zero-demand junction
        diam = max(25.0, v.diam_mm)
        valve_pipe_rows.append(
            f"  VPIPE_{v.fid}  {key}  {helper_id}  0.5  {diam:.1f}  130.0  10.0  Open"
            f"  ; {v.valve_type}"
        )

    # ── 3. write .inp ─────────────────────────────────────────────────────────
    if inp_dir is None:
        inp_dir = tempfile.gettempdir()
    os.makedirs(inp_dir, exist_ok=True)
    out_path = os.path.join(inp_dir, "dma_network.inp")

    inp = _INP()
    inp.comment(f"DUWASA DMA hydraulic model - {dma.dma_name}")
    inp.comment(f"Nodes: {len(nodes)}  Edges: {len(edges)}  Connectors: {len(report.connectors_added)}")
    inp.comment(f"Sources: {len(dma.sources)}  Tanks: {len(dma.tanks)}")
    inp.comment("Generated by dma_builder.py")
    if report.connectors_added:
        inp.comment(f"SYNTHETIC CONNECTORS ({len(report.connectors_added)}, total {report.total_connector_length_m:.0f}m):")
        for c in report.connectors_added:
            inp.comment(f"  {c.connector_id}: {c.length_m:.1f}m  {c.reason}")

    inp.section("TITLE")
    inp.raw(f"DUWASA {dma.dma_name} DMA Hydraulic Simulation")

    # JUNCTIONS
    inp.section("JUNCTIONS")
    inp.row(";ID", "Elev(m)", "Demand(m3/h)", "Pattern")
    for key in sorted(junction_keys):
        lon, lat = nodes[key].lon, nodes[key].lat
        is_bm    = key in bm_nodes
        demand   = 0.0 if is_bm else demand_m3h
        elev     = round(_idw_elev(lon, lat, dma.sources, dma.tanks, avg_src_elev), 1)
        inp.row(key, elev, round(demand, 6), "DIURNAL")

    # RESERVOIRS (boreholes)
    inp.section("RESERVOIRS")
    inp.row(";ID", "Head(m)", "Pattern")
    for key, src in source_nodes.items():
        inp.row(key, round(src.total_head_m, 1), "")

    # TANKS
    inp.section("TANKS")
    inp.row(";ID", "Elev(m)", "InitLvl(m)", "MinLvl(m)", "MaxLvl(m)", "Diam(m)", "MinVol(m3)", "VolCurve")
    for key, tank in tank_nodes.items():
        inp.row(
            key,
            round(tank.elev_m, 1),
            round(tank.init_level_m, 2),
            round(tank.min_level_m, 2),
            round(tank.max_level_m, 2),
            round(tank.diameter_m, 2),
            0, "",
        )

    # PIPES (real + synthetic connectors + valve stubs)
    inp.section("PIPES")
    inp.row(";ID", "Node1", "Node2", "Length(m)", "Diam(mm)", "C(H-W)", "Minor", "Status")
    for e in edges:
        length = max(_MIN_PIPE_LEN, e.length_m)
        suffix = "  ; SYNTHETIC CONNECTOR" if e.is_synthetic else ""
        inp.raw(
            f"  {e.edge_id}  {e.node_a}  {e.node_b}  "
            f"{round(length,2)}  {round(e.diam_mm,1)}  {round(e.hw_c,1)}  0  Open" + suffix
        )
    # Valve stubs (isolation valves as short high-minor-loss pipes)
    for vrow in valve_pipe_rows:
        inp.raw(vrow)

    # VALVES section — intentionally empty (valves modelled as pipe stubs above)
    inp.section("VALVES")
    inp.row(";ID", "Node1", "Node2", "Diam(mm)", "Type", "Setting", "MCoeff")

    inp.section("DEMANDS")
    inp.row(";Junction", "Demand", "Pattern", "Category")

    inp.section("PATTERNS")
    # 24-h diurnal demand pattern (urban Tanzania)
    diurnal = [
        0.4, 0.3, 0.3, 0.3, 0.5, 0.8,
        1.2, 1.5, 1.4, 1.2, 1.0, 0.9,
        1.0, 1.1, 1.0, 0.9, 1.0, 1.3,
        1.4, 1.2, 1.0, 0.8, 0.6, 0.4,
    ]
    inp.row("DIURNAL", *[f"{v:.2f}" for v in diurnal])

    inp.section("ENERGY")
    inp.raw("  Global Efficiency  75")
    inp.raw("  Global Price       0")

    inp.section("REACTIONS")
    inp.raw("  Order Bulk  1")
    inp.raw("  Order Tank  1")
    inp.raw("  Order Wall  1")
    inp.raw("  Global Bulk  0")
    inp.raw("  Global Wall  0")

    inp.section("TIMES")
    inp.raw(f"  Duration           {duration_hrs}:00")
    # EPANET time format: HH:MM or HH:MM:SS — 60 min must be "1:00" not "0:60"
    ts_h, ts_m = divmod(time_step_min, 60)
    inp.raw(f"  Hydraulic Timestep {ts_h}:{ts_m:02d}:00")
    inp.raw( "  Quality Timestep   0:05:00")
    inp.raw( "  Pattern Timestep   1:00:00")
    inp.raw( "  Report Timestep    1:00:00")
    inp.raw( "  Report Start       0:00:00")
    inp.raw( "  Start ClockTime    12 am")
    inp.raw( "  Statistic          NONE")

    inp.section("REPORT")
    inp.raw("  Status   Yes")
    inp.raw("  Summary  Yes")
    inp.raw("  Page     55")

    inp.section("OPTIONS")
    inp.raw("  Units             CMH")
    inp.raw("  Headloss          H-W")
    inp.raw("  Specific Gravity  1.0")
    inp.raw("  Viscosity         1.0")
    inp.raw("  Trials            200")
    inp.raw("  Accuracy          0.001")
    inp.raw("  CHECKFREQ         2")
    inp.raw("  MAXCHECK          10")
    inp.raw("  DAMPLIMIT         0")
    inp.raw("  Unbalanced        Continue 10")
    inp.raw("  Demand Multiplier 1.0")
    inp.raw("  Emitter Exponent  0.5")
    inp.raw("  Quality           Age")
    inp.raw("  Diffusivity       1")
    inp.raw("  Tolerance         0.01")

    # COORDINATES — all nodes including TCV helper stubs
    inp.section("COORDINATES")
    inp.row(";Node", "X-Coord(lon)", "Y-Coord(lat)")
    for nid, nd in sorted(nodes.items()):
        inp.row(nid, round(nd.lon, 7), round(nd.lat, 7))

    inp.section("END")
    inp.write(out_path)

    logger.info(
        "DMA .inp written -> %s | %d junctions, %d reservoirs, %d tanks, "
        "%d pipes+valvestubs, %d synthetic connectors",
        out_path, len(junction_keys), len(reservoir_keys), len(tank_keys),
        len(edges) + len(valve_pipe_rows), len(report.connectors_added),
    )
    return out_path, report


# ── NRW helper ────────────────────────────────────────────────────────────────

def estimate_nrw(
    inlet_flow_m3h:   float,
    total_demand_m3h: float,
    outlet_flow_m3h:  float = 0.0,
) -> dict:
    system_input  = max(0.0, inlet_flow_m3h - outlet_flow_m3h)
    nrw_m3h       = max(0.0, system_input - total_demand_m3h)
    nrw_fraction  = (nrw_m3h / system_input) if system_input > 0 else 0.0
    return {
        "system_input_m3h":  round(system_input,  3),
        "authorised_m3h":    round(total_demand_m3h, 3),
        "nrw_m3h":           round(nrw_m3h,        3),
        "nrw_fraction":      round(nrw_fraction,    4),
        "nrw_pct":           round(nrw_fraction * 100, 1),
        "leakage_m3h":       round(nrw_m3h * 0.85, 3),
    }
