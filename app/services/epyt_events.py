# app/services/epyt_events.py
"""
EPyT-Flow event + uncertainty builders.

Verified against EPyT-Flow 0.17.1 source:

  Sensor type constants (from epyt_flow.simulation.sensor_config):
    SENSOR_TYPE_NODE_PRESSURE = 1
    SENSOR_TYPE_NODE_QUALITY  = 2
    SENSOR_TYPE_NODE_DEMAND   = 3
    SENSOR_TYPE_LINK_FLOW     = 4
    SENSOR_TYPE_LINK_QUALITY  = 5
    SENSOR_TYPE_VALVE_STATE   = 6
    SENSOR_TYPE_PUMP_STATE    = 7
    SENSOR_TYPE_TANK_VOLUME   = 8
    Valid range: 1–10 (raises ValueError otherwise)

  ActuatorEvent.__init__(self, time: int, **kwds)
    → a single-instant event; internally sets start_time=time, end_time=time+1

  PercentageDeviationUncertainty(deviation_percentage: float)
    → must be in (0, 1)  —  i.e. 0.15 means ±15%
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ── EPyT-Flow sensor type constants ───────────────────────────────────────────
SENSOR_TYPE_NODE_PRESSURE = 1
SENSOR_TYPE_NODE_QUALITY  = 2
SENSOR_TYPE_NODE_DEMAND   = 3
SENSOR_TYPE_LINK_FLOW     = 4
SENSOR_TYPE_LINK_QUALITY  = 5
SENSOR_TYPE_VALVE_STATE   = 6
SENSOR_TYPE_PUMP_STATE    = 7
SENSOR_TYPE_TANK_VOLUME   = 8

# Human-friendly aliases used by the API (0=pressure, 1=flow)
_API_TYPE_MAP = {
    0: SENSOR_TYPE_NODE_PRESSURE,   # API "0" → EPyT pressure
    1: SENSOR_TYPE_LINK_FLOW,       # API "1" → EPyT flow
    2: SENSOR_TYPE_NODE_QUALITY,
    3: SENSOR_TYPE_NODE_DEMAND,
    # pass through EPyT native values unchanged
    SENSOR_TYPE_NODE_PRESSURE: SENSOR_TYPE_NODE_PRESSURE,
    SENSOR_TYPE_LINK_FLOW:     SENSOR_TYPE_LINK_FLOW,
}


def _resolve_sensor_type(raw) -> int:
    """Convert API sensor_type (0=pressure, 1=flow) to EPyT-Flow constant."""
    v = int(raw)
    return _API_TYPE_MAP.get(v, v)   # if not in map, pass through as-is


def _check(d: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in d or d[k] is None]
    if missing:
        raise ValueError(f"Event dict missing required keys: {missing!r}")


# ── leakage builders ──────────────────────────────────────────────────────────

def build_abrupt_leakage(ev: dict):
    """
    ev: link_id (str), diameter (float, m), start_time (int, s), end_time (int, s)
    """
    from epyt_flow.simulation.events import AbruptLeakage
    _check(ev, "link_id", "start_time", "end_time")
    return AbruptLeakage(
        link_id    = str(ev["link_id"]),
        diameter   = float(ev.get("diameter", 0.015)),
        start_time = int(ev["start_time"]),
        end_time   = int(ev["end_time"]),
    )


def build_incipient_leakage(ev: dict):
    """
    ev: link_id, peak_time, diameter, start_time, end_time
    """
    from epyt_flow.simulation.events import IncipientLeakage
    _check(ev, "link_id", "peak_time", "start_time", "end_time")
    return IncipientLeakage(
        link_id    = str(ev["link_id"]),
        peak_time  = int(ev["peak_time"]),
        diameter   = float(ev.get("diameter", 0.015)),
        start_time = int(ev["start_time"]),
        end_time   = int(ev["end_time"]),
    )


# ── sensor fault builders ─────────────────────────────────────────────────────

def build_sensor_fault(ev: dict):
    """
    ev: fault_type, sensor_id, sensor_type (API: 0=pressure,1=flow),
        start_time, end_time
        + fault-specific params
    """
    from epyt_flow.simulation.events import (
        SensorFaultConstant, SensorFaultDrift, SensorFaultGaussian,
        SensorFaultPercentage, SensorFaultStuckZero,
    )
    _check(ev, "fault_type", "sensor_id", "sensor_type", "start_time", "end_time")
    ft      = ev["fault_type"].lower()
    sid     = str(ev["sensor_id"])
    stype   = _resolve_sensor_type(ev["sensor_type"])   # must be 1–10
    t0, t1  = int(ev["start_time"]), int(ev["end_time"])
    common  = dict(sensor_id=sid, sensor_type=stype, start_time=t0, end_time=t1)

    if ft == "constant":
        return SensorFaultConstant(constant_shift=float(ev.get("constant_shift", 5.0)), **common)
    elif ft == "drift":
        return SensorFaultDrift(coef=float(ev.get("coef", 0.01)), **common)
    elif ft == "gaussian":
        return SensorFaultGaussian(std=float(ev.get("std", 1.0)), **common)
    elif ft == "percentage":
        return SensorFaultPercentage(coef=float(ev.get("coef", 0.05)), **common)
    elif ft == "stuck_zero":
        return SensorFaultStuckZero(sensor_id=sid, sensor_type=stype, start_time=t0, end_time=t1)
    else:
        raise ValueError(f"Unknown fault_type: {ft!r}")


# ── actuator event builders ───────────────────────────────────────────────────
# ActuatorEvent.__init__(self, time: int) → single-instant event.
# We expose start_time from the API for UX clarity but map it to `time`.

def build_valve_event(ev: dict):
    """
    ev: valve_id, valve_state (open|closed), start_time (mapped → time)
    """
    from epyt_flow.simulation.events import ValveStateEvent, ActuatorConstants
    _check(ev, "valve_id", "valve_state", "start_time")
    state = (ActuatorConstants.EN_CLOSED
             if str(ev["valve_state"]).lower() == "closed"
             else ActuatorConstants.EN_OPEN)
    return ValveStateEvent(
        valve_id    = str(ev["valve_id"]),
        valve_state = state,
        time        = int(ev["start_time"]),
    )


def build_pump_state_event(ev: dict):
    """
    ev: pump_id, pump_state (on|off), start_time
    """
    from epyt_flow.simulation.events import PumpStateEvent, ActuatorConstants
    _check(ev, "pump_id", "pump_state", "start_time")
    state = (ActuatorConstants.EN_CLOSED
             if str(ev["pump_state"]).lower() in ("off", "closed")
             else ActuatorConstants.EN_OPEN)
    return PumpStateEvent(
        pump_id    = str(ev["pump_id"]),
        pump_state = state,
        time       = int(ev["start_time"]),
    )


def build_pump_speed_event(ev: dict):
    """
    ev: pump_id, pump_speed (float 0-2), start_time
    """
    from epyt_flow.simulation.events import PumpSpeedEvent
    _check(ev, "pump_id", "pump_speed", "start_time")
    return PumpSpeedEvent(
        pump_id    = str(ev["pump_id"]),
        pump_speed = float(ev["pump_speed"]),
        time       = int(ev["start_time"]),
    )


# ── uncertainty builders ──────────────────────────────────────────────────────

def build_model_uncertainty(cfg: dict):
    """
    cfg: demand_pct (0-1), roughness_pct (0-1), seed (int)
    PercentageDeviationUncertainty requires fraction in (0, 1).
    """
    from epyt_flow.uncertainty import ModelUncertainty
    from epyt_flow.uncertainty.uncertainties import PercentageDeviationUncertainty

    demand_pct    = float(cfg.get("demand_pct",    0.0))
    roughness_pct = float(cfg.get("roughness_pct", 0.0))
    if demand_pct == 0.0 and roughness_pct == 0.0:
        return None

    kwargs: Dict[str, Any] = {"seed": int(cfg.get("seed", 42))}
    if 0 < demand_pct < 1:
        kwargs["global_base_demand_uncertainty"] = PercentageDeviationUncertainty(
            deviation_percentage=float(demand_pct)
        )
    if 0 < roughness_pct < 1:
        kwargs["global_pipe_roughness_uncertainty"] = PercentageDeviationUncertainty(
            deviation_percentage=float(roughness_pct)
        )
    return ModelUncertainty(**kwargs) if len(kwargs) > 1 else None


def build_sensor_noise(cfg: dict):
    """
    cfg: pressure_noise_std (m), flow_noise_std (m³/h), seed (int)

    EPyT-Flow SensorNoise.global_uncertainty applies additive noise to ALL
    sensor readings.  We take the larger of the two stds as the global std
    (fine for DMA scenarios where pressure readings dominate the analysis).
    local_uncertainties requires concrete sensor_id strings — not suitable
    for a blanket global noise setting.
    """
    from epyt_flow.uncertainty import SensorNoise
    from epyt_flow.uncertainty.uncertainties import AbsoluteGaussianUncertainty

    pn = float(cfg.get("pressure_noise_std", 0.0))
    fn = float(cfg.get("flow_noise_std",     0.0))
    std = max(pn, fn)
    if std == 0.0:
        return None
    global_unc = AbsoluteGaussianUncertainty(mean=0.0, scale=std)
    return SensorNoise(global_uncertainty=global_unc, seed=int(cfg.get("seed", 42)))


# ── dispatcher ────────────────────────────────────────────────────────────────

def build_events(events_cfg: list) -> dict:
    """
    Convert list of event dicts → EPyT-Flow objects grouped by type.
    Returns {"leakages": [...], "sensor_faults": [...], "actuator_events": [...]}
    """
    out: Dict[str, list] = {"leakages": [], "sensor_faults": [], "actuator_events": []}

    for ev in (events_cfg or []):
        ev_type = str(ev.get("type", "")).lower()
        try:
            if ev_type == "abrupt_leakage":
                out["leakages"].append(build_abrupt_leakage(ev))

            elif ev_type == "incipient_leakage":
                out["leakages"].append(build_incipient_leakage(ev))

            elif "sensor_fault" in ev_type:
                # infer fault_type from type string if not explicit
                if "fault_type" not in ev:
                    inferred = ev_type.replace("sensor_fault_", "") or "gaussian"
                    ev = {**ev, "fault_type": inferred}
                out["sensor_faults"].append(build_sensor_fault(ev))

            elif ev_type == "valve_state":
                out["actuator_events"].append(build_valve_event(ev))

            elif ev_type == "pump_state":
                out["actuator_events"].append(build_pump_state_event(ev))

            elif ev_type == "pump_speed":
                out["actuator_events"].append(build_pump_speed_event(ev))

            else:
                logger.warning("Unknown event type %r — skipped", ev_type)

        except Exception as exc:
            logger.warning("Failed to build event %r: %s", ev_type, exc)

    return out