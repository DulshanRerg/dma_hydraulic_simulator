# app/services/simulation_service.py
"""
EPyT-Flow v0.17.x simulation runner.

Fixes applied vs previous version
-----------------------------------
1. `ScenarioSimulator.epanet_api` is an `epanet_plus.EPyT` instance, whose
   node/link accessors are snake_case (`get_node_idx`, `get_node_type`,
   `get_link_idx`, `get_link_type`, `get_all_nodes_id`, `get_all_links_id`,
   `getcoord`, `getlinknodes`, `get_node_id`) — not the camelCase
   Matlab-toolkit names (`getNodeIndex`, `getNodeType`, ...) this module
   previously called, which don't exist on that class and raised
   AttributeError before a single simulation could complete.

2. Sensor API: use the explicit setter methods:
       sim.set_pressure_sensors(sensor_locations=[...])
       sim.set_flow_sensors(sensor_locations=[...])
       sim.set_node_quality_sensors(sensor_locations=[...])

3. ScadaData result access (v0.17):
       scada.get_data_pressures()    → ndarray (T, N)
       scada.get_data_flows()        → ndarray (T, N)  — in CMH (m³/h)
       scada.get_data_node_quality() → ndarray (T, N)  — water age in seconds

4. Sensor ordering: sensor_locations list order = column order in arrays.
   We keep a local ordered list so indexing is correct.

5. AbruptLeakage: uses keyword arg `diameter` (not `leak_diameter`).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ── result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class NodeResult:
    element_id:      str
    time_step:       int             = 0
    lat:             Optional[float] = None
    lon:             Optional[float] = None
    pressure:        Optional[float] = None
    head:            Optional[float] = None
    demand:          Optional[float] = None
    water_age:       Optional[float] = None
    is_low_pressure: bool            = False


@dataclass
class PipeResult:
    element_id:       str
    time_step:        int             = 0
    lat:              Optional[float] = None
    lon:              Optional[float] = None
    flow_rate:        Optional[float] = None
    velocity:         Optional[float] = None
    headloss:         Optional[float] = None
    is_high_velocity: bool            = False


@dataclass
class SimulationOutput:
    node_results: List[NodeResult] = field(default_factory=list)
    pipe_results: List[PipeResult] = field(default_factory=list)
    summary:      dict             = field(default_factory=dict)


# ── coordinate extractor ───────────────────────────────────────────────────────

def _extract_node_coords(api, node_ids: List[str]) -> Dict[str, Tuple[float, float]]:
    """
    Extract (lon, lat) for each node using the bundled EPyT class
    (`epanet_plus.EPyT`). Its node/link accessors are snake_case
    (`get_node_idx`, `getcoord`, ...) — not the camelCase
    Matlab-toolkit names (`getNodeIndex`, `getNodeCoordinates`, ...).
    """
    coords: Dict[str, Tuple[float, float]] = {}
    for nid in node_ids:
        try:
            idx = api.get_node_idx(nid)
            xy  = api.getcoord(idx)   # [x, y]
            if xy and len(xy) >= 2:
                x, y = float(xy[0]), float(xy[1])
                if x != 0.0 or y != 0.0:
                    coords[nid] = (x, y)
        except Exception:
            pass
    return coords


def _extract_pipe_topology(
    api, pipe_ids: List[str]
) -> Dict[str, Tuple[str, str]]:
    """Return {pipe_id: (start_node_id, end_node_id)}."""
    topo: Dict[str, Tuple[str, str]] = {}
    for pid in pipe_ids:
        try:
            idx   = api.get_link_idx(pid)
            nodes = api.getlinknodes(idx)   # [start_idx, end_idx], 1-based
            s_id  = api.get_node_id(nodes[0])
            e_id  = api.get_node_id(nodes[1])
            topo[pid] = (s_id, e_id)
        except Exception:
            pass
    return topo


# ── main runner ────────────────────────────────────────────────────────────────

def run_simulation(
    inp_path:      str,
    duration_hrs:  int  = 24,
    time_step_min: int  = 60,
    leak_events:   Optional[List[dict]] = None,
) -> SimulationOutput:
    """
    Run EPyT-Flow simulation from an EPANET .inp file.

    Parameters
    ----------
    inp_path      : path to the .inp file produced by network_builder
    duration_hrs  : total EPS duration (hours)
    time_step_min : hydraulic timestep (minutes)
    leak_events   : list of dicts:
                    { node_id, diameter, start_time, end_time }
                    Each becomes an AbruptLeakage event in EPyT-Flow.
    """
    try:
        from epyt_flow.simulation import ScenarioSimulator
        from epyt_flow.simulation.events import AbruptLeakage
    except ImportError as exc:
        raise RuntimeError(
            "epyt-flow is not installed.  Run: pip install epyt-flow==0.17.1"
        ) from exc

    settings      = get_settings()
    duration_sec  = duration_hrs  * 3600
    timestep_sec  = time_step_min * 60

    logger.info(
        "EPyT-Flow: loading .inp '%s'  (duration=%dh, ts=%dmin, leaks=%d)",
        inp_path, duration_hrs, time_step_min, len(leak_events or []),
    )

    # ── pass 1: probe network to discover node/pipe IDs + coordinates ──────────
    with ScenarioSimulator(f_inp_in=inp_path) as probe:
        probe.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = timestep_sec,
            reporting_time_step = timestep_sec,
        )
        api = probe.epanet_api

        # all node / link names from EPANET
        all_node_ids: List[str] = api.get_all_nodes_id()
        all_link_ids: List[str] = api.get_all_links_id()

        # separate junction IDs from reservoir IDs
        junction_ids: List[str] = []
        reservoir_ids: List[str] = []
        for nid in all_node_ids:
            idx  = api.get_node_idx(nid)
            ntype = api.get_node_type(idx)   # returns int; 0=Junction,1=Reservoir,2=Tank
            if ntype == 1:
                reservoir_ids.append(nid)
            else:
                junction_ids.append(nid)

        # pipe IDs only (exclude pumps/valves)
        pipe_ids: List[str] = []
        for lid in all_link_ids:
            idx   = api.get_link_idx(lid)
            ltype = api.get_link_type(idx)   # 1=PIPE, 2=PUMP, etc.
            if ltype == 1:
                pipe_ids.append(lid)

        # extract coordinates and topology
        node_coords  = _extract_node_coords(api, all_node_ids)
        pipe_topology = _extract_pipe_topology(api, pipe_ids)

    logger.info(
        "Network: %d junctions, %d reservoirs, %d pipes | coords resolved: %d",
        len(junction_ids), len(reservoir_ids), len(pipe_ids), len(node_coords),
    )

    # ── pass 2: build AbruptLeakage events ─────────────────────────────────────
    system_events = []
    for ev in (leak_events or []):
        nid = ev.get("node_id")
        if nid not in junction_ids:
            logger.warning("Leak node '%s' not found in junctions — skipped", nid)
            continue
        system_events.append(
            AbruptLeakage(
                node_id    = nid,
                link_id    = None,
                diameter   = float(ev.get("diameter", 0.01)),
                start_time = int(ev.get("start_time", 0)),
                end_time   = int(ev.get("end_time", duration_sec)),
            )
        )
        logger.info(
            "  AbruptLeakage → node=%s  Ø=%.3f m  t=[%d, %d]s",
            nid, ev.get("diameter", 0.01),
            ev.get("start_time", 0), ev.get("end_time", duration_sec),
        )

    # ── pass 3: run with sensors + leakage events ──────────────────────────────
    with ScenarioSimulator(f_inp_in=inp_path) as sim:
        sim.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = timestep_sec,
            reporting_time_step = timestep_sec,
        )

        # register sensors — use junction_ids for pressure/quality (not reservoirs)
        sim.set_pressure_sensors(sensor_locations=junction_ids)
        sim.set_flow_sensors(sensor_locations=pipe_ids)
        if junction_ids:
            sim.set_node_quality_sensors(sensor_locations=junction_ids)

        # inject leakage events
        for leak in system_events:
            sim.add_leakage(leak)

        logger.info("Running EPyT-Flow simulation …")
        scada = sim.run_simulation()
        logger.info("EPyT-Flow simulation complete.")

        # result arrays — shape (T, N_sensors)
        pressure_arr = scada.get_data_pressures()     # m
        flow_arr     = scada.get_data_flows()         # m³/h (CMH)
        quality_arr  = scada.get_data_nodes_quality()  # seconds (water age)

        # sensor ordering matches the lists we passed in
        pressure_node_ids = junction_ids
        flow_pipe_ids     = pipe_ids
        quality_node_ids  = junction_ids

        n_steps = pressure_arr.shape[0] if pressure_arr is not None else 0
        logger.info("Result time steps: %d", n_steps)

    # ── parse results ──────────────────────────────────────────────────────────
    output = SimulationOutput()

    for t in range(n_steps):
        t_hr = t  # 1 step = 1 hour when time_step_min=60

        # nodes
        for j, nid in enumerate(pressure_node_ids):
            pressure = float(pressure_arr[t, j]) if pressure_arr is not None else None
            age_s    = (
                float(quality_arr[t, j])
                if quality_arr is not None and quality_arr.shape[1] > j
                else None
            )
            coords = node_coords.get(nid, (None, None))
            output.node_results.append(NodeResult(
                element_id      = nid,
                time_step       = t_hr,
                lon             = coords[0],
                lat             = coords[1],
                pressure        = round(pressure, 3) if pressure is not None else None,
                water_age       = round(age_s / 3600.0, 2) if age_s else None,
                is_low_pressure = (
                    pressure is not None and pressure < settings.min_pressure_m
                ),
            ))

        # pipes
        for p, pid in enumerate(flow_pipe_ids):
            flow_cmh = float(flow_arr[t, p]) if flow_arr is not None else None
            flow_m3s = flow_cmh / 3600.0 if flow_cmh is not None else None

            s_nid, e_nid = pipe_topology.get(pid, (None, None))
            s_xy = node_coords.get(s_nid, (None, None))
            e_xy = node_coords.get(e_nid, (None, None))
            mid_lon = (s_xy[0] + e_xy[0]) / 2 if s_xy[0] and e_xy[0] else None
            mid_lat = (s_xy[1] + e_xy[1]) / 2 if s_xy[1] and e_xy[1] else None

            output.pipe_results.append(PipeResult(
                element_id = pid,
                time_step  = t_hr,
                lon        = round(mid_lon, 7) if mid_lon else None,
                lat        = round(mid_lat, 7) if mid_lat else None,
                flow_rate  = round(flow_m3s, 6) if flow_m3s is not None else None,
            ))

    # ── summary ────────────────────────────────────────────────────────────────
    pressures = [n.pressure  for n in output.node_results if n.pressure  is not None]
    flows     = [abs(p.flow_rate) for p in output.pipe_results if p.flow_rate is not None]
    ages      = [n.water_age for n in output.node_results if n.water_age is not None]

    output.summary = {
        "pressure_min_m":       round(min(pressures),               2) if pressures else None,
        "pressure_max_m":       round(max(pressures),               2) if pressures else None,
        "pressure_avg_m":       round(sum(pressures)/len(pressures), 2) if pressures else None,
        "flow_max_m3s":         round(max(flows),                   6) if flows     else None,
        "water_age_max_hrs":    round(max(ages),                    2) if ages      else None,
        "low_pressure_nodes":   sum(1 for n in output.node_results if n.is_low_pressure),
        "high_velocity_pipes":  0,
        "leak_events_injected": len(system_events),
        "total_nodes":          len(junction_ids),
        "total_pipes":          len(pipe_ids),
        "duration_hrs":         duration_hrs,
        "time_steps":           n_steps,
        "engine":               "EPyT-Flow v0.17.1",
    }

    logger.info(
        "Parsed %d node records, %d pipe records",
        len(output.node_results), len(output.pipe_results),
    )
    return output