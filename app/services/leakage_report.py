# app/services/leakage_report.py
"""
Leakage analysis service for a completed DMA simulation.

Inputs
------
SimulationOutput from simulation_service.run_simulation()

Outputs
-------
LeakageReport — a structured report containing:

  nrw             — Non-Revenue Water estimate
  pressure_zones  — nodes grouped into low / normal / high pressure
  pipe_risk       — per-pipe leakage risk score [0-1] based on:
                     • low min-pressure at adjacent nodes (indicator of burst)
                     • high velocity (erosion risk)
                     • flow reversal across timesteps
                     • pipe diameter (smaller = higher NRW risk per m)
  hotspots        — top-N highest-risk pipes as GeoJSON for the map
  timestep_flows  — aggregate inflow / outflow per hour (for the flow
                    balance / NRW trend chart)

The risk model is purely hydraulic — it flags *where* leaks are most
likely given observed pressure and velocity patterns, not where leaks
have been confirmed. Field teams use the hotspot map to prioritise
night-flow surveys and acoustic leak detection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.services.simulation_service import NodeResult, PipeResult, SimulationOutput


# ── constants (tuneable) ──────────────────────────────────────────────────────

LOW_PRESSURE_M        = 7.0     # m — below this = supply deficiency
HIGH_PRESSURE_M       = 45.0    # m — above this = elevated burst risk
HIGH_VELOCITY_MS      = 1.5     # m/s — above this = erosion risk
LEAKAGE_PRESSURE_EXP  = 0.5     # FAVAD leak exponent (N1)
MIN_RISK_PIPE_DIAM_MM = 50.0    # pipes < this get +20% risk weight (small mains, high NRW)
DEFAULT_PIPE_DIAM_MM  = 100.0   # used only when the real diameter is unknown
NEARBY_NODE_RADIUS_M  = 500.0   # radius used to find nodes "adjacent" to a pipe


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class NRWEstimate:
    system_input_m3h:   float   # total inflow from sources (sum of supply pipes)
    authorised_m3h:     float   # sum of junction demands
    nrw_m3h:            float
    nrw_pct:            float
    real_loss_m3h:      float   # real losses (leakage) ≈ NRW × 0.85
    apparent_loss_m3h:  float   # admin / billing errors ≈ NRW × 0.15
    ili:                float   # Infrastructure Leakage Index (>1 = excessive)
    source:             str = "estimated"  # "epanet_rpt" | "estimated"

@dataclass
class PressureZone:
    zone:       str             # "low" | "normal" | "high"
    node_ids:   List[str]
    count:      int
    avg_pct:    float           # % of total nodes

@dataclass
class PipeRisk:
    pipe_id:     str
    lat:         float
    lon:         float
    risk_score:  float          # [0, 1]
    risk_level:  str            # "low" | "medium" | "high" | "critical"
    drivers:     List[str]      # human-readable reasons
    avg_flow:    float          # m³/h
    min_pressure_adjacent: Optional[float]  # lowest pressure seen at either endpoint

@dataclass
class TimestepBalance:
    hour:          int
    inflow_m3h:    float        # sum of all source pipe flows at this timestep
    demand_m3h:    float        # sum of all junction demands
    nrw_m3h:       float        # inflow - demand (positive = unaccounted-for loss)

@dataclass
class LeakageReport:
    scenario_id:     int
    dma_name:        str
    nrw:             NRWEstimate
    pressure_zones:  List[PressureZone]
    pipe_risks:      List[PipeRisk]          # sorted by risk_score desc
    hotspots:        dict                    # GeoJSON FeatureCollection, top-50 pipes
    timestep_balance: List[TimestepBalance]
    warnings:        List[str] = field(default_factory=list)


# ── helpers ───────────────────────────────────────────────────────────────────

def _avg(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0

def _risk_level(score: float) -> str:
    if score >= 0.70: return "critical"
    if score >= 0.45: return "high"
    if score >= 0.20: return "medium"
    return "low"


# ── main analysis function ────────────────────────────────────────────────────

def analyse_leakage(
    output:      SimulationOutput,
    scenario_id: int,
    dma_name:    str,
    base_demand_m3h: float = 0.011,
    leakage_frac:    float = 0.20,
    pipe_diam_mm:    Optional[Dict[str, float]] = None,
    epanet_flow_balance: Optional[dict] = None,
) -> LeakageReport:
    """
    Derive the leakage report from a completed SimulationOutput.

    pipe_diam_mm
        Real per-pipe diameter (mm), keyed by EPANET link ID, as written
        into the [PIPES] section of the .inp the scenario was built from.
        When a pipe isn't in this map (e.g. a synthetic topology-repair
        connector), DEFAULT_PIPE_DIAM_MM is used instead of silently
        assuming every pipe is 50mm.
    epanet_flow_balance
        The parsed EPANET .rpt "Hydraulic Flow Balance" summary
        (see rpt_parser.rpt_nrw_summary), when available. When its
        source is "epanet_rpt" this is EPANET's own mass-balance figure
        and is used for system input / authorised consumption instead
        of the flow-based heuristic, which is only a fallback for
        scenarios where no .rpt could be parsed.
    """
    warnings_out: List[str] = []
    pipe_diam_mm = pipe_diam_mm or {}

    # ── index results ─────────────────────────────────────────────────────────
    # node_results: List[NodeResult(element_id, time_step, lat, lon, pressure, ...)]
    # pipe_results: List[PipeResult(element_id, time_step, lat, lon, flow_rate, ...)]

    node_ts: Dict[str, List[NodeResult]] = {}
    for nr in output.node_results:
        node_ts.setdefault(nr.element_id, []).append(nr)

    pipe_ts: Dict[str, List[PipeResult]] = {}
    for pr in output.pipe_results:
        pipe_ts.setdefault(pr.element_id, []).append(pr)

    n_steps   = output.summary.get("time_steps", 1)
    dur_hrs   = output.summary.get("duration_hrs", 24)
    total_nodes = len(node_ts)
    total_pipes = len(pipe_ts)

    # ── NRW estimate ──────────────────────────────────────────────────────────
    # Preferred source: EPANET's own Hydraulic Flow Balance from the .rpt file
    # (real mass-balance across reservoirs/tanks/demands, not an estimate).
    # Fallback: a flow-based heuristic using the top 10% of pipes by average
    # flow as a proxy for the supply mains, for scenarios where no .rpt could
    # be parsed (e.g. EPyT-Flow failed to write one, or the file was already
    # cleaned up).
    all_flows_m3h: List[float] = []
    for plist in pipe_ts.values():
        vals = [abs(pr.flow_rate * 3600) for pr in plist if pr.flow_rate is not None]
        if vals:
            all_flows_m3h.append(_avg(vals))
    all_flows_m3h.sort(reverse=True)
    # The top 10% of pipes by flow represent the main supply mains
    top_n = max(1, total_pipes // 10)

    nrw_source = "estimated"
    if epanet_flow_balance and epanet_flow_balance.get("source") == "epanet_rpt":
        system_input_m3h = float(epanet_flow_balance.get("total_inflow_m3h", 0.0))
        authorised_m3h   = float(epanet_flow_balance.get("consumer_demand_m3h", 0.0))
        nrw_source = "epanet_rpt"
    else:
        system_input_m3h = sum(all_flows_m3h[:top_n])
        # Authorised = number of demand nodes × base demand per node
        # (the builder embedded base_demand_m3h × (1+leakage_frac) at each junction)
        authorised_m3h = total_nodes * base_demand_m3h
        warnings_out.append(
            "NRW figures are estimated from pipe-flow heuristics (no EPANET "
            "flow-balance report available) — treat as indicative only."
        )

    n_demand_nodes = total_nodes
    nrw_m3h        = max(0.0, system_input_m3h - authorised_m3h)
    nrw_pct        = 100.0 * nrw_m3h / system_input_m3h if system_input_m3h > 0 else 0.0
    real_loss      = nrw_m3h * 0.85
    apparent_loss  = nrw_m3h * 0.15

    # ILI = Current Annual Real Losses / Unavoidable Annual Real Losses
    # CARL = real_loss × 8760 h/year (m³/year)
    # UARL ≈ (18 × mains_km + 0.8 × service_connections) × avg_pressure_m  (IWA formula)
    avg_p    = output.summary.get("pressure_avg_m", 20.0)
    mains_km = total_pipes * 0.2   # rough estimate: ~200 m per pipe segment
    uarl_m3y = (18 * mains_km + 0.8 * n_demand_nodes) * avg_p
    carl_m3y = real_loss * 8_760
    ili       = carl_m3y / uarl_m3y if uarl_m3y > 0 else 1.0

    nrw = NRWEstimate(
        system_input_m3h  = round(system_input_m3h, 3),
        authorised_m3h    = round(authorised_m3h, 3),
        nrw_m3h           = round(nrw_m3h, 3),
        nrw_pct           = round(nrw_pct, 1),
        real_loss_m3h     = round(real_loss, 3),
        apparent_loss_m3h = round(apparent_loss, 3),
        ili               = round(ili, 2),
        source            = nrw_source,
    )

    # ── pressure zones ────────────────────────────────────────────────────────
    low_nodes, normal_nodes, high_nodes = [], [], []
    for nid, results in node_ts.items():
        pressures = [r.pressure for r in results if r.pressure is not None]
        if not pressures:
            continue
        avg_p_node = _avg(pressures)
        if avg_p_node < LOW_PRESSURE_M:
            low_nodes.append(nid)
        elif avg_p_node > HIGH_PRESSURE_M:
            high_nodes.append(nid)
        else:
            normal_nodes.append(nid)

    def _zone(label, ids):
        return PressureZone(
            zone    = label,
            node_ids= ids,
            count   = len(ids),
            avg_pct = round(100.0 * len(ids) / total_nodes, 1) if total_nodes else 0.0,
        )
    pressure_zones = [
        _zone("low",    low_nodes),
        _zone("normal", normal_nodes),
        _zone("high",   high_nodes),
    ]

    # ── spatial index of node pressures, for O(1)-ish "nearby node" lookups ────
    # Bucket every node into a grid cell sized to NEARBY_NODE_RADIUS_M so each
    # pipe only has to scan its own cell + 8 neighbours, instead of either
    # scanning all nodes (slow on large networks) or an arbitrary first-50
    # sample (silently wrong — most pipes never matched any node at all).
    cell_deg = NEARBY_NODE_RADIUS_M / 111_320.0
    node_grid: Dict[Tuple[int, int], List[Tuple[float, float, float, float]]] = {}
    # each entry: (lat, lon, min_pressure, avg_pressure)
    for nid, nresults in node_ts.items():
        n0 = nresults[0]
        if n0.lat is None or n0.lon is None:
            continue
        p_vals = [r.pressure for r in nresults if r.pressure is not None]
        if not p_vals:
            continue
        cell = (int(n0.lat // cell_deg), int(n0.lon // cell_deg))
        node_grid.setdefault(cell, []).append((n0.lat, n0.lon, min(p_vals), _avg(p_vals)))

    def _nearby_node_pressures(lat: float, lon: float) -> List[Tuple[float, float]]:
        """Returns [(min_pressure, avg_pressure), ...] for nodes within radius."""
        cx, cy = int(lat // cell_deg), int(lon // cell_deg)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (nlat, nlon, nmin, navg) in node_grid.get((cx + dx, cy + dy), []):
                    d = math.hypot(
                        (nlat - lat) * 111_320,
                        (nlon - lon) * 111_320 * math.cos(math.radians(lat)),
                    )
                    if d <= NEARBY_NODE_RADIUS_M:
                        out.append((nmin, navg))
        return out

    # ── pipe risk scoring ─────────────────────────────────────────────────────
    pipe_risks: List[PipeRisk] = []

    for pid, results in pipe_ts.items():
        flows_m3h = [abs(pr.flow_rate * 3600) for pr in results if pr.flow_rate is not None]
        raw_flows  = [pr.flow_rate * 3600 for pr in results if pr.flow_rate is not None]
        lat = results[0].lat if results and results[0].lat is not None else None
        lon = results[0].lon if results and results[0].lon is not None else None

        avg_flow = _avg(flows_m3h)
        max_flow = max(flows_m3h) if flows_m3h else 0.0

        # Velocity: v = Q / A, using the pipe's real diameter from the .inp
        # this scenario was built from (falls back to DEFAULT_PIPE_DIAM_MM
        # only for synthetic topology-repair connectors / unknown pipes).
        diam_mm = pipe_diam_mm.get(pid, DEFAULT_PIPE_DIAM_MM)
        diam_m  = diam_mm / 1000.0
        area_m2 = math.pi * (diam_m / 2) ** 2
        velocity_ms = (avg_flow / 3600) / area_m2 if area_m2 > 0 else 0.0

        # Flow reversal: sign changes across timesteps
        reversals = sum(
            1 for i in range(1, len(raw_flows))
            if raw_flows[i] * raw_flows[i-1] < 0
        )

        # Local pressure context: nodes actually adjacent to this pipe, not a
        # network-wide average/fraction applied identically to every pipe.
        nearby = _nearby_node_pressures(lat, lon) if lat is not None and lon is not None else []
        min_adj_p = min((m for m, _ in nearby), default=None)
        avg_adj_p = _avg([a for _, a in nearby]) if nearby else None

        drivers: List[str] = []
        score = 0.0

        # 1. High local pressure → elevated leak rate (FAVAD model)
        # Pressure score ∝ (P/ref)^N1, ref = 20 m
        ref_p = 20.0
        if avg_adj_p is not None and avg_adj_p > ref_p:
            p_score = min(1.0, (avg_adj_p / ref_p) ** LEAKAGE_PRESSURE_EXP - 1.0)
            score += p_score * 0.35
            if p_score > 0.3:
                drivers.append(f"High local pressure ({avg_adj_p:.0f} m)")

        # 2. Low local pressure → probable burst or large leak nearby
        if min_adj_p is not None and min_adj_p < LOW_PRESSURE_M:
            deficit_frac = min(1.0, (LOW_PRESSURE_M - min_adj_p) / LOW_PRESSURE_M)
            score += deficit_frac * 0.30
            drivers.append(f"Low pressure nearby ({min_adj_p:.1f} m)")

        # 3. High velocity
        if velocity_ms > HIGH_VELOCITY_MS:
            v_score = min(1.0, (velocity_ms / HIGH_VELOCITY_MS) - 1.0)
            score += v_score * 0.20
            drivers.append(f"High velocity ({velocity_ms:.2f} m/s)")

        # 4. Flow reversals
        if reversals > 0:
            score += min(0.15, reversals * 0.05)
            drivers.append(f"Flow reversal ({reversals}×)")

        # 5. Small diameter bonus (small mains have disproportionate NRW)
        if diam_mm < MIN_RISK_PIPE_DIAM_MM:
            score += 0.10
            drivers.append(f"Small diameter ({diam_mm:.0f} mm)")

        score = min(1.0, max(0.0, score))
        if not drivers:
            drivers.append("No significant risk indicators")

        pipe_risks.append(PipeRisk(
            pipe_id     = pid,
            lat         = round(lat, 7) if lat is not None else None,
            lon         = round(lon, 7) if lon is not None else None,
            risk_score  = round(score, 4),
            risk_level  = _risk_level(score),
            drivers     = drivers,
            avg_flow    = round(avg_flow, 4),
            min_pressure_adjacent = round(min_adj_p, 2) if min_adj_p is not None else None,
        ))

    pipe_risks.sort(key=lambda x: -x.risk_score)

    # ── timestep balance ──────────────────────────────────────────────────────
    timestep_balance: List[TimestepBalance] = []
    if pipe_ts:
        for step in range(n_steps):
            step_flows = []
            for plist in pipe_ts.values():
                matching = [pr for pr in plist if pr.time_step == step]
                if matching and matching[0].flow_rate is not None:
                    step_flows.append(abs(matching[0].flow_rate * 3600))
            if step_flows:
                inflow = sum(sorted(step_flows, reverse=True)[:top_n])
                demand = n_demand_nodes * base_demand_m3h
                timestep_balance.append(TimestepBalance(
                    hour       = step,
                    inflow_m3h = round(inflow, 3),
                    demand_m3h = round(demand, 3),
                    nrw_m3h    = round(max(0, inflow - demand), 3),
                ))

    # ── hotspot GeoJSON (top 50 highest-risk pipes) ───────────────────────────
    top_pipes = pipe_risks[:50]
    hotspot_features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [p.lon, p.lat]},
            "properties": {
                "pipe_id":    p.pipe_id,
                "risk_score": p.risk_score,
                "risk_level": p.risk_level,
                "drivers":    "; ".join(p.drivers),
                "avg_flow_m3h": p.avg_flow,
                "min_adj_pressure_m": p.min_pressure_adjacent,
            },
        }
        for p in top_pipes
    ]
    hotspots = {"type": "FeatureCollection", "features": hotspot_features}

    if nrw.ili > 8:
        warnings_out.append(f"ILI={nrw.ili:.1f} — very high leakage. Immediate intervention recommended.")
    if len(low_nodes) > total_nodes * 0.20:
        warnings_out.append(f"{len(low_nodes)} nodes ({len(low_nodes)/total_nodes*100:.0f}%) below {LOW_PRESSURE_M}m — check source heads and tank levels.")

    return LeakageReport(
        scenario_id      = scenario_id,
        dma_name         = dma_name,
        nrw              = nrw,
        pressure_zones   = pressure_zones,
        pipe_risks       = pipe_risks,
        hotspots         = hotspots,
        timestep_balance = timestep_balance,
        warnings         = warnings_out,
    )