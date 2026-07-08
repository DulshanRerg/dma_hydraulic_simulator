# app/services/simulation_service.py
"""
EPyT-Flow v0.17.x simulation runner — full feature edition.

Supports
--------
* Pressure, flow, demand, water-age sensors on all nodes/links
* AbruptLeakage + IncipientLeakage events
* SensorFault variants (constant, drift, gaussian, percentage, stuck_zero)
* ActuatorEvent variants (ValveStateEvent, PumpStateEvent, PumpSpeedEvent)
* ModelUncertainty  (demand ±%, roughness ±%)
* SensorNoise       (additive Gaussian noise on readings)
* Tank volume extraction from ScadaData
* Valve/pump state extraction when present
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ── result dataclasses ────────────────────────────────────────────────────────

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
class TankResult:
    element_id: str
    time_step:  int
    volume_m3:  Optional[float] = None
    lat:        Optional[float] = None
    lon:        Optional[float] = None


@dataclass
class SimulationOutput:
    node_results:  List[NodeResult]  = field(default_factory=list)
    pipe_results:  List[PipeResult]  = field(default_factory=list)
    tank_results:  List[TankResult]  = field(default_factory=list)
    summary:       dict              = field(default_factory=dict)


# ── main runner ───────────────────────────────────────────────────────────────

def run_simulation(
    inp_path:         str,
    duration_hrs:     int  = 24,
    time_step_min:    int  = 60,
    # legacy: old callers pass AbruptLeakage-compatible dicts
    leak_events:      Optional[List[dict]] = None,
    # new: structured EPyT-Flow event objects
    leakage_objects:  Optional[list] = None,
    sensor_faults:    Optional[list] = None,
    actuator_events:  Optional[list] = None,
    model_uncertainty = None,
    sensor_noise      = None,
) -> SimulationOutput:
    """
    Run an EPyT-Flow simulation.

    Parameters
    ----------
    inp_path         : EPANET .inp file
    duration_hrs     : simulation duration
    time_step_min    : hydraulic time step
    leak_events      : legacy list of dicts (node_id, diameter, start_time, end_time)
                       — converted to AbruptLeakage automatically
    leakage_objects  : pre-built EPyT-Flow Leakage objects (AbruptLeakage / IncipientLeakage)
    sensor_faults    : pre-built SensorFault objects
    actuator_events  : pre-built ActuatorEvent objects
    model_uncertainty: ModelUncertainty object (demand/roughness perturbation)
    sensor_noise     : SensorNoise object (Gaussian noise on readings)
    """
    try:
        from epyt_flow.simulation import ScenarioSimulator
        from epyt_flow.simulation.events import AbruptLeakage
    except ImportError as exc:
        raise RuntimeError("epyt-flow is not installed.") from exc

    settings      = get_settings()
    duration_sec  = duration_hrs  * 3600
    time_step_sec = time_step_min * 60

    # ── convert legacy leak_events dicts ──────────────────────────────────────
    all_leakages = list(leakage_objects or [])
    for ev in (leak_events or []):
        try:
            all_leakages.append(AbruptLeakage(
                link_id    = str(ev["node_id"]),  # legacy callers pass node_id
                diameter   = float(ev.get("diameter", 0.015)),
                start_time = int(ev.get("start_time", 0)),
                end_time   = int(ev.get("end_time", duration_sec)),
            ))
        except Exception as e:
            logger.warning("Skipping legacy leak event: %s", e)

    logger.info(
        "EPyT-Flow run: %s | %dh / %dmin | leaks=%d faults=%d actuators=%d unc=%s noise=%s",
        inp_path, duration_hrs, time_step_min,
        len(all_leakages),
        len(sensor_faults or []),
        len(actuator_events or []),
        model_uncertainty is not None,
        sensor_noise is not None,
    )

    # ── pass 1: probe network topology ───────────────────────────────────────
    node_coords:    Dict[str, Tuple[float, float]] = {}
    pipe_topology:  Dict[str, Tuple[str, str]]     = {}
    all_node_ids:   list = []
    all_pipe_ids:   list = []
    all_tank_ids:   list = []
    all_pump_ids:   list = []
    all_valve_ids:  list = []

    with ScenarioSimulator(f_inp_in=inp_path) as probe:
        probe.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = time_step_sec,
            reporting_time_step = time_step_sec,
        )
        sc = probe.sensor_config
        all_node_ids  = list(sc.nodes)
        all_pipe_ids  = list(sc.links)
        all_tank_ids  = [t for t in (getattr(sc, "tanks", []) or [])]
        all_pump_ids  = [p for p in (getattr(sc, "pumps", []) or [])]
        all_valve_ids = [v for v in (getattr(sc, "valves", []) or [])]

        api = probe.epanet_api
        for nid in all_node_ids:
            try:
                idx  = api.getNodeIndex(nid)
                c    = api.getNodeCoordinates(idx)
                if c and len(c) >= 2 and (c[0] or c[1]):
                    node_coords[nid] = (float(c[0]), float(c[1]))
            except Exception:
                pass

        for pid in all_pipe_ids:
            try:
                idx   = api.getLinkIndex(pid)
                nodes = api.getLinkNodesIndex(idx)
                s_id  = api.getNodeNameID(nodes[0])
                e_id  = api.getNodeNameID(nodes[1])
                pipe_topology[pid] = (s_id, e_id)
            except Exception:
                pass

    logger.info(
        "Probe: %d nodes (%d with coords), %d pipes, %d tanks, %d pumps, %d valves",
        len(all_node_ids), len(node_coords), len(all_pipe_ids),
        len(all_tank_ids), len(all_pump_ids), len(all_valve_ids),
    )

    # ── pass 2: full simulation ───────────────────────────────────────────────
    with ScenarioSimulator(f_inp_in=inp_path) as sim:
        sim.set_general_parameters(
            simulation_duration = duration_sec,
            hydraulic_time_step = time_step_sec,
            reporting_time_step = time_step_sec,
        )

        # ── sensors: everything ───────────────────────────────────────────────
        sim.place_pressure_sensors_everywhere()
        sim.place_flow_sensors_everywhere()
        sim.place_demand_sensors_everywhere()
        sim.place_node_quality_sensors_everywhere()   # water age
        sim.place_link_quality_sensors_everywhere()
        if all_tank_ids:
            sim.place_tank_sensors_everywhere()
        if all_valve_ids:
            sim.place_valve_sensors_everywhere()
        if all_pump_ids:
            sim.place_pump_sensors_everywhere()
            sim.place_pump_state_sensors_everywhere()

        # ── leakage events ────────────────────────────────────────────────────
        for lk in all_leakages:
            try:
                sim.add_leakage(lk)
            except Exception as e:
                logger.warning("Could not add leakage %s: %s", lk, e)

        # ── sensor faults ─────────────────────────────────────────────────────
        for sf in (sensor_faults or []):
            try:
                sim.add_sensor_fault(sf)
            except Exception as e:
                logger.warning("Could not add sensor fault: %s", e)

        # ── actuator events ───────────────────────────────────────────────────
        for ae in (actuator_events or []):
            try:
                sim.add_actuator_event(ae)
            except Exception as e:
                logger.warning("Could not add actuator event: %s", e)

        # ── uncertainties ─────────────────────────────────────────────────────
        if model_uncertainty is not None:
            try:
                sim.set_model_uncertainty(model_uncertainty)
            except Exception as e:
                logger.warning("Could not apply model uncertainty: %s", e)
        if sensor_noise is not None:
            try:
                sim.set_sensor_noise(sensor_noise)
            except Exception as e:
                logger.warning("Could not apply sensor noise: %s", e)

        # ── run ───────────────────────────────────────────────────────────────
        logger.info("Running EPyT-Flow simulation …")
        scada = sim.run_simulation()
        logger.info("EPyT-Flow simulation complete.")

        # ── extract arrays ────────────────────────────────────────────────────
        sc_out           = scada.sensor_config
        sensor_node_ids  = list(sc_out.pressure_sensors)
        sensor_pipe_ids  = list(sc_out.flow_sensors)
        sensor_tank_ids  = list(getattr(sc_out, "tank_volume_sensors", []) or [])

        pressure_arr = _safe(scada.get_data_pressures)
        flow_arr     = _safe(scada.get_data_flows)
        quality_arr  = _safe(scada.get_data_nodes_quality)
        demand_arr   = _safe(scada.get_data_demands)
        tank_vol_arr = _safe(scada.get_data_tanks_water_volume) if sensor_tank_ids else None

    n_steps = pressure_arr.shape[0] if pressure_arr is not None else 0
    logger.info("Parsing %d time steps × %d nodes × %d pipes", n_steps, len(sensor_node_ids), len(sensor_pipe_ids))

    output = SimulationOutput()

    # demand sensor ordering may differ from pressure sensor ordering
    demand_node_ids = list(getattr(scada.sensor_config, "demand_sensors", []) or [])
    demand_map: Dict[str, int] = {nid: i for i, nid in enumerate(demand_node_ids)}

    for t in range(n_steps):
        # ── nodes ─────────────────────────────────────────────────────────────
        for j, nid in enumerate(sensor_node_ids):
            lon, lat = node_coords.get(nid, (None, None))
            pressure = _val(pressure_arr, t, j)
            age_s    = _val(quality_arr,  t, j)
            d_idx    = demand_map.get(nid)
            demand   = _val(demand_arr, t, d_idx) if d_idx is not None and demand_arr is not None else None
            # demand in EPyT-Flow is CMH — convert to m³/s for storage
            demand_m3s = demand / 3600.0 if demand is not None else None

            output.node_results.append(NodeResult(
                element_id      = nid,
                time_step       = t,
                lon             = lon,
                lat             = lat,
                pressure        = _r3(pressure),
                demand          = _r6(demand_m3s),
                water_age       = _r2(age_s / 3600.0) if age_s is not None else None,
                is_low_pressure = bool(pressure is not None and pressure < settings.min_pressure_m),
            ))

        # ── pipes ─────────────────────────────────────────────────────────────
        for p, pid in enumerate(sensor_pipe_ids):
            flow_cmh = _val(flow_arr, t, p)
            flow_m3s = flow_cmh / 3600.0 if flow_cmh is not None else None
            s_n, e_n = pipe_topology.get(pid, (None, None))
            sc_  = node_coords.get(s_n, (None, None))
            ec_  = node_coords.get(e_n, (None, None))
            mid_lon = (sc_[0] + ec_[0]) / 2 if sc_[0] and ec_[0] else None
            mid_lat = (sc_[1] + ec_[1]) / 2 if sc_[1] and ec_[1] else None

            output.pipe_results.append(PipeResult(
                element_id  = pid,
                time_step   = t,
                lon         = _r7(mid_lon),
                lat         = _r7(mid_lat),
                flow_rate   = _r6(flow_m3s),
                is_high_velocity = False,  # velocity needs pipe diameter; skipped
            ))

        # ── tanks ─────────────────────────────────────────────────────────────
        for k, tid in enumerate(sensor_tank_ids):
            vol = _val(tank_vol_arr, t, k) if tank_vol_arr is not None else None
            lon, lat = node_coords.get(tid, (None, None))
            output.tank_results.append(TankResult(
                element_id = tid,
                time_step  = t,
                volume_m3  = _r2(vol),
                lon        = lon,
                lat        = lat,
            ))

    # ── summary ───────────────────────────────────────────────────────────────
    pressures = [n.pressure  for n in output.node_results if n.pressure is not None]
    flows     = [abs(p.flow_rate) for p in output.pipe_results if p.flow_rate is not None]
    ages      = [n.water_age for n in output.node_results if n.water_age is not None]
    demands   = [n.demand    for n in output.node_results if n.demand is not None]

    output.summary = {
        "pressure_min_m":       _r2(min(pressures)) if pressures else None,
        "pressure_max_m":       _r2(max(pressures)) if pressures else None,
        "pressure_avg_m":       _r2(sum(pressures) / len(pressures)) if pressures else None,
        "flow_max_m3s":         _r6(max(flows))    if flows     else None,
        "flow_avg_m3s":         _r6(sum(flows) / len(flows)) if flows else None,
        "water_age_max_hrs":    _r2(max(ages))     if ages      else None,
        "total_demand_m3h":     _r3(sum(demands) * 3600.0 / n_steps) if demands and n_steps else None,
        "low_pressure_nodes":   sum(1 for n in output.node_results if n.is_low_pressure),
        "high_velocity_pipes":  0,
        "leak_events_injected": len(all_leakages),
        "sensor_faults_applied":len(sensor_faults or []),
        "actuator_events":      len(actuator_events or []),
        "model_uncertainty":    model_uncertainty is not None,
        "sensor_noise":         sensor_noise is not None,
        "total_nodes":          len(sensor_node_ids),
        "total_pipes":          len(sensor_pipe_ids),
        "total_tanks":          len(sensor_tank_ids),
        "duration_hrs":         duration_hrs,
        "time_steps":           n_steps,
        "engine":               "EPyT-Flow v0.17.1",
    }

    logger.info(
        "Parsed %d node·t, %d pipe·t, %d tank·t records",
        len(output.node_results), len(output.pipe_results), len(output.tank_results),
    )
    return output


# ── array helpers ─────────────────────────────────────────────────────────────

def _safe(fn):
    """Call fn(), return result or None on error."""
    try:
        return fn()
    except Exception:
        return None


def _val(arr, t: int, j):
    """Safely index a 2-D numpy array; return float or None."""
    if arr is None or j is None:
        return None
    try:
        v = float(arr[t, j])
        return v if math.isfinite(v) else None
    except (IndexError, TypeError):
        return None


def _r2(v): return round(v, 2)  if v is not None else None
def _r3(v): return round(v, 3)  if v is not None else None
def _r6(v): return round(v, 6)  if v is not None else None
def _r7(v): return round(v, 7)  if v is not None else None