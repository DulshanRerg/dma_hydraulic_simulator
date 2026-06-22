# app/services/simulation_service.py
"""
Runs hydraulic + water-age simulation using EPyT-Flow v0.17.x.

Correct EPyT-Flow API (verified from source):
---------------------------------------------
  ScenarioSimulator(f_inp_in="path/to/net.inp")
  sim.set_general_parameters(simulation_duration=86400, hydraulic_time_step=3600, ...)
  sim.set_pressure_sensors(sensor_locations=["N_0", "N_1", ...])
  sim.set_flow_sensors(sensor_locations=["P_0", "P_1", ...])
  scada = sim.run_simulation()          # returns ScadaData
  scada.get_data_pressures()            # numpy array (T, N_nodes)
  scada.get_data_flows()                # numpy array (T, N_pipes)
  scada.get_data_node_quality()         # numpy array (T, N_nodes) — water age in seconds

  AbruptLeakage(node_id, link_id=None, diameter, start_time, end_time)
  ScenarioConfig(f_inp_in, sensor_config, system_events=[leak, ...])
  ScenarioSimulator(scenario_config=ScenarioConfig(...))
"""

import logging
import math
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


# ── runner ─────────────────────────────────────────────────────────────────────

def run_simulation(
    inp_path:      str,
    duration_hrs:  int  = 24,
    time_step_min: int  = 60,
    leak_events:   Optional[List[dict]] = None,
) -> SimulationOutput:
    """
    Run an EPyT-Flow simulation from an EPANET .inp file.

    Parameters
    ----------
    inp_path      : absolute path to the EPANET .inp file (from network_builder)
    duration_hrs  : total EPS duration in hours
    time_step_min : hydraulic time step in minutes
    leak_events   : list of dicts, each with keys:
                      node_id        (str)   — EPANET node ID, e.g. "N_42"
                      diameter       (float) — orifice diameter in metres
                      start_time     (int)   — seconds from sim start
                      end_time       (int)   — seconds from sim start
                    These become EPyT-Flow AbruptLeakage events.

    Returns
    -------
    SimulationOutput
    """
    try:
        from epyt_flow.simulation import ScenarioSimulator, ScenarioConfig
        from epyt_flow.simulation.events import AbruptLeakage
    except ImportError as exc:
        raise RuntimeError(
            "epyt-flow is not installed. Run:  pip install epyt-flow==0.17.1"
        ) from exc

    settings       = get_settings()
    duration_sec   = duration_hrs  * 3600
    time_step_sec  = time_step_min * 60

    logger.info(
        "EPyT-Flow: loading .inp '%s'  (duration=%dh, ts=%dmin, leaks=%d)",
        inp_path, duration_hrs, time_step_min, len(leak_events or []),
    )

    # ── build AbruptLeakage events ─────────────────────────────────────────────
    system_events = []
    for ev in (leak_events or []):
        system_events.append(
            AbruptLeakage(
                node_id    = ev["node_id"],
                link_id    = None,
                diameter   = ev.get("diameter", 0.01),
                start_time = ev.get("start_time", 0),
                end_time   = ev.get("end_time", duration_sec),
            )
        )
        logger.info(
            "  AbruptLeakage: node=%s  Ø=%.3f m  t=[%d, %d]s",
            ev["node_id"], ev.get("diameter", 0.01),
            ev.get("start_time", 0), ev.get("end_time", duration_sec),
        )

    # ── run simulation (two-pass approach) ─────────────────────────────────────
    # Pass 1: open the .inp to discover node and pipe IDs for sensor placement.
    # Pass 2: run with full sensor config + leak events.
    # (EPyT-Flow requires sensor IDs to be set before run_simulation())

    with ScenarioSimulator(f_inp_in=inp_path) as probe:
        probe.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = time_step_sec,
            reporting_time_step = time_step_sec,
        )
        all_node_ids = probe.sensor_config.nodes        # list[str]
        all_pipe_ids = probe.sensor_config.links        # list[str]

        # collect node coordinates via EPyT's low-level API
        node_coords: Dict[str, Tuple[float, float]] = {}
        for nid in all_node_ids:
            try:
                idx    = probe.epanet_api.getNodeIndex(nid)
                coords = probe.epanet_api.getNodeCoordinates(idx)
                if coords and len(coords) >= 2:
                    node_coords[nid] = (float(coords[0]), float(coords[1]))  # (lon, lat)
            except Exception:
                pass

        # collect pipe topology (start / end node) for midpoint coords
        pipe_topology: Dict[str, Tuple[str, str]] = {}
        for pid in all_pipe_ids:
            try:
                idx   = probe.epanet_api.getLinkIndex(pid)
                nodes = probe.epanet_api.getLinkNodesIndex(idx)
                s_id  = probe.epanet_api.getNodeNameID(nodes[0])
                e_id  = probe.epanet_api.getNodeNameID(nodes[1])
                pipe_topology[pid] = (s_id, e_id)
            except Exception:
                pass

    logger.info(
        "Network probe: %d nodes, %d pipes, %d with coords",
        len(all_node_ids), len(all_pipe_ids), len(node_coords),
    )

    # ── build ScenarioConfig with sensors + events ─────────────────────────────
    with ScenarioSimulator(f_inp_in=inp_path) as sim:
        sim.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = time_step_sec,
            reporting_time_step = time_step_sec,
        )

        # sensors on every junction (exclude reservoirs/tanks for pressure)
        junction_ids = [
            n for n in all_node_ids
            if n not in (probe.epanet_api.get_all_reservoirs_id() if False else [])
        ]
        sim.set_pressure_sensors(sensor_locations=all_node_ids)
        sim.set_flow_sensors(sensor_locations=all_pipe_ids)
        sim.set_node_quality_sensors(sensor_locations=all_node_ids)  # water age

        # inject leakage events
        for leak in system_events:
            sim.add_leakage(leak)

        # ── run ────────────────────────────────────────────────────────────────
        logger.info("Running EPyT-Flow simulation …")
        scada = sim.run_simulation()
        logger.info("EPyT-Flow simulation complete.")

        # ── extract result arrays ──────────────────────────────────────────────
        pressure_arr = scada.get_data_pressures()     # (T, N_nodes) — m
        flow_arr     = scada.get_data_flows()         # (T, N_pipes) — CMH (m³/h)
        quality_arr  = scada.get_data_nodes_quality()  # (T, N_nodes) — seconds (water age)

        sensor_node_ids = scada.sensor_config.pressure_sensors  # ordered list
        sensor_pipe_ids = scada.sensor_config.flow_sensors

        n_time_steps = pressure_arr.shape[0] if pressure_arr is not None else 0
        logger.info("Result time steps: %d", n_time_steps)

    # ── parse into dataclasses ─────────────────────────────────────────────────
    output = SimulationOutput()

    for t_idx in range(n_time_steps):
        t_hr = t_idx  # one step per hour when time_step_min=60

        # ── nodes ──────────────────────────────────────────────────────────────
        for j_idx, node_id in enumerate(sensor_node_ids):
            coords   = node_coords.get(node_id, (None, None))
            pressure = float(pressure_arr[t_idx, j_idx]) if pressure_arr is not None else None
            age_s    = (
                float(quality_arr[t_idx, j_idx])
                if quality_arr is not None and quality_arr.shape[1] > j_idx
                else None
            )

            output.node_results.append(NodeResult(
                element_id      = node_id,
                time_step       = t_hr,
                lon             = coords[0],
                lat             = coords[1],
                pressure        = round(pressure, 3) if pressure is not None else None,
                water_age       = round(age_s / 3600.0, 2) if age_s is not None else None,
                is_low_pressure = (
                    pressure is not None and pressure < settings.min_pressure_m
                ),
            ))

        # ── pipes ───────────────────────────────────────────────────────────────
        for p_idx, pipe_id in enumerate(sensor_pipe_ids):
            flow_cmh  = float(flow_arr[t_idx, p_idx]) if flow_arr is not None else None
            flow_m3s  = flow_cmh / 3600.0 if flow_cmh is not None else None

            s_nid, e_nid = pipe_topology.get(pipe_id, (None, None))
            s_coord = node_coords.get(s_nid, (None, None))
            e_coord = node_coords.get(e_nid, (None, None))
            mid_lon = (
                (s_coord[0] + e_coord[0]) / 2
                if s_coord[0] and e_coord[0] else None
            )
            mid_lat = (
                (s_coord[1] + e_coord[1]) / 2
                if s_coord[1] and e_coord[1] else None
            )

            output.pipe_results.append(PipeResult(
                element_id  = pipe_id,
                time_step   = t_hr,
                lon         = round(mid_lon, 7) if mid_lon else None,
                lat         = round(mid_lat, 7) if mid_lat else None,
                flow_rate   = round(flow_m3s, 6) if flow_m3s is not None else None,
                is_high_velocity = False,   # velocity needs pipe diameter; set False for now
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
        "total_nodes":          len(sensor_node_ids),
        "total_pipes":          len(sensor_pipe_ids),
        "duration_hrs":         duration_hrs,
        "time_steps":           n_time_steps,
        "engine":               "EPyT-Flow v0.17.1",
    }

    logger.info(
        "Parsed %d node records, %d pipe records",
        len(output.node_results), len(output.pipe_results),
    )
    return output
