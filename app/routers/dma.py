# app/routers/dma.py
"""
DMA (District Metered Area) simulation endpoints.

Endpoints
---------
GET  /dma/{filename}/layers
POST /dma/{filename}/simulate               — baseline hydraulic run
POST /dma/{filename}/simulate/advanced      — EPyT-Flow full scenario
                                              (leakages, sensor faults, actuators,
                                               uncertainties, sensor noise)
GET  /dma/{filename}/simulate/{id}/nrw
GET  /dma/{filename}/simulate/{id}/leakage
GET  /dma/{filename}/simulate/{id}/tanks    — tank volume time-series
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.database import get_db                       # ← correct FastAPI dependency
from app.core.exceptions import GpkgNotFoundError, InvalidGpkgError
from app.models.simulation import SimResult, SimScenario
from app.services.dma_builder import build_dma_inp, estimate_nrw
from app.services.dma_ingest import ingest_dma
from app.services.leakage_report import analyse_leakage
from app.workers.simulation_worker import run_simulation_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dma", tags=["dma"])


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /dma/{filename}/layers
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{filename}/layers")
def get_dma_layers(
    filename: str,
    _: str = Depends(require_api_key),
):
    """Return all DMA asset layers as GeoJSON FeatureCollections."""
    try:
        dma = ingest_dma(filename, clip_to_dma=True)
    except GpkgNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidGpkgError as e:
        raise HTTPException(status_code=422, detail=str(e))

    def point_feature(lon, lat, props):
        return {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props}

    def line_features(pipes):
        return [
            {"type": "Feature",
             "geometry": {"type": "LineString",
                          "coordinates": [[x, y] for x, y in p.coords]},
             "properties": {"fid": p.fid, "diam_mm": p.diam_mm, "hw_c": p.hw_c,
                            "material": p.material, "purpose": p.purpose,
                            "length_m": round(p.length_m, 1)}}
            for p in pipes
        ]

    return {
        "dma_name": dma.dma_name, "dma_bbox": dma.dma_bbox,
        "boundary": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature",
                          "geometry": {"type": "Polygon",
                                       "coordinates": [[[lon, lat] for lon, lat in dma.dma_polygon]]},
                          "properties": {"name": dma.dma_name}}]},
        "pipes": {"type": "FeatureCollection", "features": line_features(dma.pipes)},
        "sources": {"type": "FeatureCollection",
                    "features": [point_feature(s.lon, s.lat,
                        {"fid": s.fid, "name": s.name, "elev_m": s.elev_m,
                         "yield_m3h": s.yield_m3h, "total_head_m": round(s.total_head_m, 1),
                         "status": s.status, "type": "borehole"}) for s in dma.sources]},
        "tanks": {"type": "FeatureCollection",
                  "features": [point_feature(t.lon, t.lat,
                      {"fid": t.fid, "name": t.name, "elev_m": t.elev_m,
                       "cap_m3": t.cap_m3, "max_level_m": t.max_level_m,
                       "diameter_m": round(t.diameter_m, 2), "status": t.status,
                       "type": "tank"}) for t in dma.tanks]},
        "valves": {"type": "FeatureCollection",
                   "features": [point_feature(v.lon, v.lat,
                       {"fid": v.fid, "valve_type": v.valve_type,
                        "diam_mm": v.diam_mm, "is_isolation": v.is_isolation,
                        "type": "valve"}) for v in dma.valves]},
        "bulk_meters": {"type": "FeatureCollection",
                        "features": [point_feature(b.lon, b.lat,
                            {"fid": b.fid, "name": b.name, "type": "bulk_meter"})
                                     for b in dma.bulk_meters]},
        "stats": {
            "pipe_count": len(dma.pipes), "source_count": len(dma.sources),
            "tank_count": len(dma.tanks), "valve_count": len(dma.valves),
            "bulk_meter_count": len(dma.bulk_meters),
            "total_pipe_length_m": round(sum(p.length_m for p in dma.pipes), 1),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /dma/{filename}/simulate  — baseline
# ═══════════════════════════════════════════════════════════════════════════════

class DMASimRequest(BaseModel):
    name:            str   = Field("DMA hydraulic run", max_length=200)
    duration_hrs:    int   = Field(24, ge=1, le=168)
    time_step_min:   int   = Field(60, ge=5, le=360)
    base_demand_m3h: float = Field(0.011, gt=0)
    leakage_frac:    float = Field(0.20,  ge=0, le=1.0)

    demand_model: str = Field(
        "DDA",
        pattern="^(DDA|PDA)$",
        description="Hydraulic demand model: 'DDA' (default) or 'PDA'.",
    )
    pda_pressure_min:      float = Field(0.0, ge=0)
    pda_pressure_required: float = Field(0.1, gt=0)
    pda_pressure_exponent: float = Field(0.5, gt=0)


@router.post("/{filename}/simulate", status_code=202)
async def simulate_dma(
    filename:   str,
    body:       DMASimRequest,
    background: BackgroundTasks,
    db:         AsyncSession = Depends(get_db),
    _:          str          = Depends(require_api_key),
):
    """Build a DMA EPANET model and queue a baseline hydraulic simulation."""
    try:
        dma = ingest_dma(filename, clip_to_dma=True)
    except GpkgNotFoundError:
        raise HTTPException(404, f"File not found: {filename}")
    except InvalidGpkgError as e:
        raise HTTPException(422, str(e))

    if not dma.pipes:
        raise HTTPException(422, "No operational pipes found in the DMA.")
    if not dma.sources and not dma.tanks:
        raise HTTPException(422, "No water sources or tanks found.")

    import tempfile
    inp_dir = tempfile.mkdtemp(prefix="dma_epyt_")
    try:
        inp_path, repair_report = build_dma_inp(
            dma             = dma,
            inp_dir         = inp_dir,
            duration_hrs    = body.duration_hrs,
            time_step_min   = body.time_step_min,
            base_demand_m3h = body.base_demand_m3h,
            leakage_frac    = body.leakage_frac,
            demand_model          = body.demand_model,
            pda_pressure_min      = body.pda_pressure_min,
            pda_pressure_required = body.pda_pressure_required,
            pda_pressure_exponent = body.pda_pressure_exponent,
        )
    except Exception as e:
        logger.exception("DMA .inp build failed")
        raise HTTPException(422, f"Failed to build EPANET model: {e}")

    connectors_geojson = _connectors_to_geojson(repair_report)

    scenario = SimScenario(
        gpkg_filename  = filename,
        name           = body.name,
        description    = f"DMA baseline — {dma.dma_name}",
        base_demand    = body.base_demand_m3h,
        duration_hrs   = body.duration_hrs,
        time_step_min  = body.time_step_min,
        reservoir_head = 0.0,
        demand_model          = body.demand_model,
        pda_pressure_min      = body.pda_pressure_min,
        pda_pressure_required = body.pda_pressure_required,
        pda_pressure_exponent = body.pda_pressure_exponent,
        extra_demands  = {
            "_dma_inp_path":        inp_path,
            "_dma_name":            dma.dma_name,
            "_leakage_frac":        body.leakage_frac,
            "_total_demand_m3h":    round(body.base_demand_m3h * (1 + body.leakage_frac), 6),
            "_connectors_added":    len(repair_report.connectors_added),
            "_connector_length_m":  repair_report.total_connector_length_m,
            "_original_components": repair_report.original_component_count,
            "_repair_warnings":     repair_report.warnings,
            "_scenario_type":       "baseline",
        },
        status = "PENDING",
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)

    background.add_task(run_simulation_task, scenario.id)
    logger.info("Queued DMA baseline scenario %d for '%s'", scenario.id, filename)

    return {
        "id": scenario.id, "status": "PENDING",
        "dma_name": dma.dma_name,
        "topology_repair": {
            "original_components":      repair_report.original_component_count,
            "connectors_added":         len(repair_report.connectors_added),
            "total_connector_length_m": repair_report.total_connector_length_m,
            "warnings":                 repair_report.warnings,
            "connectors_geojson":       connectors_geojson,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /dma/{filename}/simulate/advanced  — full EPyT-Flow scenario
# ═══════════════════════════════════════════════════════════════════════════════

class LeakageEventModel(BaseModel):
    """One leakage event to inject."""
    type:       str   = Field("abrupt_leakage", description="abrupt_leakage | incipient_leakage")
    link_id:    str   = Field(..., description="EPANET pipe ID, e.g. E_42")
    diameter:   float = Field(0.015, gt=0, description="Orifice diameter (m)")
    start_time: int   = Field(0,     ge=0, description="Seconds from sim start")
    end_time:   int   = Field(86400, ge=0)
    peak_time:  Optional[int] = Field(None, description="Required for incipient_leakage")


class SensorFaultModel(BaseModel):
    """One sensor fault to apply."""
    type:            str   = Field("sensor_fault")
    fault_type:      str   = Field("gaussian", description="constant|drift|gaussian|percentage|stuck_zero")
    sensor_id:       str   = Field(..., description="EPANET node/link ID")
    sensor_type:     int   = Field(0, description="0=pressure, 1=flow")
    start_time:      int   = Field(0)
    end_time:        int   = Field(86400)
    constant_shift:  Optional[float] = Field(None, description="For constant fault (m)")
    coef:            Optional[float] = Field(None, description="For drift / percentage fault")
    std:             Optional[float] = Field(None, description="For gaussian fault (m)")


class ActuatorEventModel(BaseModel):
    """One actuator event."""
    type:        str   = Field(..., description="valve_state | pump_state | pump_speed")
    valve_id:    Optional[str]   = None
    pump_id:     Optional[str]   = None
    valve_state: Optional[str]   = Field(None, description="open | closed")
    pump_state:  Optional[str]   = Field(None, description="on | off")
    pump_speed:  Optional[float] = Field(None, description="Speed ratio 0-2 (1=nominal)")
    start_time:  int = Field(0)
    end_time:    int = Field(86400)


class ModelUncertaintyModel(BaseModel):
    demand_pct:    float = Field(0.0, ge=0, le=1.0, description="± fraction on base demands")
    roughness_pct: float = Field(0.0, ge=0, le=1.0, description="± fraction on H-W C")
    seed:          int   = Field(42)


class SensorNoiseModel(BaseModel):
    pressure_noise_std: float = Field(0.0, ge=0, description="Gaussian std on pressure (m)")
    flow_noise_std:     float = Field(0.0, ge=0, description="Gaussian std on flow (m³/h)")
    seed:               int   = Field(42)


class DMAAdvancedSimRequest(BaseModel):
    name:             str   = Field("DMA advanced scenario", max_length=200)
    duration_hrs:     int   = Field(24, ge=1, le=168)
    time_step_min:    int   = Field(60, ge=5, le=360)
    base_demand_m3h:  float = Field(0.011, gt=0)
    leakage_frac:     float = Field(0.20, ge=0, le=1.0)

    demand_model: str = Field(
        "DDA",
        pattern="^(DDA|PDA)$",
        description="Hydraulic demand model: 'DDA' (default) or 'PDA'.",
    )
    pda_pressure_min:      float = Field(0.0, ge=0)
    pda_pressure_required: float = Field(0.1, gt=0)
    pda_pressure_exponent: float = Field(0.5, gt=0)

    # EPyT-Flow events
    leakage_events:   List[LeakageEventModel]  = Field(default_factory=list)
    sensor_faults:    List[SensorFaultModel]   = Field(default_factory=list)
    actuator_events:  List[ActuatorEventModel] = Field(default_factory=list)

    # EPyT-Flow uncertainties
    model_uncertainty: Optional[ModelUncertaintyModel] = None
    sensor_noise:      Optional[SensorNoiseModel]      = None


@router.post("/{filename}/simulate/advanced", status_code=202)
async def simulate_dma_advanced(
    filename:   str,
    body:       DMAAdvancedSimRequest,
    background: BackgroundTasks,
    db:         AsyncSession = Depends(get_db),
    _:          str          = Depends(require_api_key),
):
    """
    Full EPyT-Flow scenario for a DMA.

    Supports all EPyT-Flow unique features:
    - Abrupt / incipient leakage events
    - Sensor faults (constant shift, drift, Gaussian noise, percentage error, stuck-at-zero)
    - Actuator events (valve open/close, pump on/off, pump speed change)
    - Model uncertainties (demand ±%, pipe roughness ±%)
    - Sensor noise (additive Gaussian on SCADA readings)

    Poll `GET /simulate/{id}` until DONE, then use the same result
    endpoints as a baseline run.
    """
    try:
        dma = ingest_dma(filename, clip_to_dma=True)
    except GpkgNotFoundError:
        raise HTTPException(404, f"File not found: {filename}")
    except InvalidGpkgError as e:
        raise HTTPException(422, str(e))

    if not dma.pipes:
        raise HTTPException(422, "No operational pipes found in the DMA.")

    import tempfile
    inp_dir = tempfile.mkdtemp(prefix="dma_adv_")
    try:
        inp_path, repair_report = build_dma_inp(
            dma             = dma,
            inp_dir         = inp_dir,
            duration_hrs    = body.duration_hrs,
            time_step_min   = body.time_step_min,
            base_demand_m3h = body.base_demand_m3h,
            leakage_frac    = body.leakage_frac,
            demand_model          = body.demand_model,
            pda_pressure_min      = body.pda_pressure_min,
            pda_pressure_required = body.pda_pressure_required,
            pda_pressure_exponent = body.pda_pressure_exponent,
        )
    except Exception as e:
        logger.exception("DMA .inp build failed (advanced)")
        raise HTTPException(422, f"Failed to build EPANET model: {e}")

    # Serialise all event dicts for storage in extra_demands
    events_cfg = (
        [e.model_dump() for e in body.leakage_events]
        + [e.model_dump() for e in body.sensor_faults]
        + [e.model_dump() for e in body.actuator_events]
    )

    scenario = SimScenario(
        gpkg_filename  = filename,
        name           = body.name,
        description    = f"DMA advanced EPyT-Flow scenario — {dma.dma_name}",
        base_demand    = body.base_demand_m3h,
        duration_hrs   = body.duration_hrs,
        time_step_min  = body.time_step_min,
        reservoir_head = 0.0,
        demand_model          = body.demand_model,
        pda_pressure_min      = body.pda_pressure_min,
        pda_pressure_required = body.pda_pressure_required,
        pda_pressure_exponent = body.pda_pressure_exponent,
        extra_demands  = {
            "_dma_inp_path":          inp_path,
            "_dma_name":              dma.dma_name,
            "_leakage_frac":          body.leakage_frac,
            "_total_demand_m3h":      round(body.base_demand_m3h * (1 + body.leakage_frac), 6),
            "_connectors_added":      len(repair_report.connectors_added),
            "_connector_length_m":    repair_report.total_connector_length_m,
            "_original_components":   repair_report.original_component_count,
            "_repair_warnings":       repair_report.warnings,
            "_scenario_type":         "advanced",
            "_events":                events_cfg,
            "_model_uncertainty":     body.model_uncertainty.model_dump() if body.model_uncertainty else None,
            "_sensor_noise":          body.sensor_noise.model_dump() if body.sensor_noise else None,
        },
        status = "PENDING",
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)

    background.add_task(run_simulation_task, scenario.id)
    logger.info(
        "Queued DMA advanced scenario %d | leaks=%d faults=%d actuators=%d",
        scenario.id, len(body.leakage_events), len(body.sensor_faults), len(body.actuator_events),
    )

    return {
        "id": scenario.id, "status": "PENDING",
        "dma_name": dma.dma_name,
        "events_queued": {
            "leakage_events":   len(body.leakage_events),
            "sensor_faults":    len(body.sensor_faults),
            "actuator_events":  len(body.actuator_events),
            "model_uncertainty": body.model_uncertainty is not None,
            "sensor_noise":      body.sensor_noise is not None,
        },
        "topology_repair": {
            "original_components":      repair_report.original_component_count,
            "connectors_added":         len(repair_report.connectors_added),
            "total_connector_length_m": repair_report.total_connector_length_m,
            "warnings":                 repair_report.warnings,
            "connectors_geojson":       _connectors_to_geojson(repair_report),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /dma/{filename}/simulate/{id}/nrw
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{filename}/simulate/{scenario_id}/nrw")
async def get_nrw(
    filename:    str,
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    scenario = await db.get(SimScenario, scenario_id)
    if not scenario or scenario.gpkg_filename != filename:
        raise HTTPException(404, "Scenario not found.")
    if scenario.status != "DONE":
        raise HTTPException(409, f"Simulation is {scenario.status}, not DONE.")

    summary      = scenario.summary or {}
    extra        = scenario.extra_demands or {}
    total_demand = extra.get("_total_demand_m3h", 0.011)
    inlet_flow   = summary.get("inlet_flow_m3h", 0.0)
    outlet_flow  = summary.get("outlet_flow_m3h", 0.0)
    sim_demand   = summary.get("total_demand_m3h", total_demand)

    if inlet_flow == 0.0:
        inlet_flow = sim_demand

    nrw = estimate_nrw(
        inlet_flow_m3h   = inlet_flow,
        total_demand_m3h = sim_demand,
        outlet_flow_m3h  = outlet_flow,
    )
    return {
        "scenario_id": scenario_id,
        "dma_name":    extra.get("_dma_name"),
        "simulation_summary": summary,
        "nrw": nrw,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /dma/{filename}/simulate/{id}/leakage
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{filename}/simulate/{scenario_id}/leakage")
async def get_leakage_report(
    filename:    str,
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    scenario = await db.get(SimScenario, scenario_id)
    if not scenario or scenario.gpkg_filename != filename:
        raise HTTPException(404, "Scenario not found.")
    if scenario.status != "DONE":
        raise HTTPException(409, f"Simulation is {scenario.status}, not DONE.")
    if not (scenario.extra_demands or {}).get("_dma_inp_path"):
        raise HTTPException(422, "Not a DMA simulation.")

    from sqlalchemy import select as sa_select
    from app.services.simulation_service import NodeResult, PipeResult, SimulationOutput

    rows = (await db.execute(
        sa_select(SimResult).where(SimResult.scenario_id == scenario_id)
    )).scalars().all()

    node_results, pipe_results = [], []
    for sr in rows:
        d = sr.data if hasattr(sr, "data") else {}
        if sr.element_type == "node":
            node_results.append(NodeResult(
                element_id = sr.element_id, time_step = sr.time_step,
                lat = sr.lat, lon = sr.lon,
                pressure = sr.pressure, head = sr.head,
                demand   = sr.demand, water_age = sr.water_age,
                is_low_pressure = sr.is_low_pressure,
            ))
        elif sr.element_type == "pipe":
            pipe_results.append(PipeResult(
                element_id = sr.element_id, time_step = sr.time_step,
                lat = sr.lat, lon = sr.lon,
                flow_rate  = sr.flow_rate, velocity = sr.velocity,
                headloss   = sr.headloss, is_high_velocity = sr.is_high_velocity,
            ))

    # scenario.summary carries the real values captured at run time
    # (pressure_avg_m, time_steps, duration_hrs, the EPANET .rpt flow
    # balance, and real per-pipe diameters) — the .inp file itself is long
    # gone by the time this report is requested, so this is the only place
    # those figures can still come from. Without it, analyse_leakage()
    # would silently fall back to hardcoded defaults (20m pressure, 1
    # timestep, 24hr duration, 50mm pipes) regardless of the actual run.
    scenario_summary = scenario.summary or {}
    sim_output = SimulationOutput(
        node_results = node_results,
        pipe_results = pipe_results,
        summary      = scenario_summary,
    )
    extra  = scenario.extra_demands or {}
    report = analyse_leakage(
        output              = sim_output,
        scenario_id         = scenario_id,
        dma_name            = extra.get("_dma_name", "DMA"),
        base_demand_m3h     = scenario.base_demand or 0.011,
        leakage_frac        = extra.get("_leakage_frac", 0.20),
        pipe_diam_mm        = scenario_summary.get("pipe_diam_mm", {}),
        epanet_flow_balance = scenario_summary.get("epanet_flow_balance"),
    )

    return {
        "scenario_id": scenario_id,
        "dma_name":    report.dma_name,
        "warnings":    report.warnings,
        "nrw": {
            "system_input_m3h":  report.nrw.system_input_m3h,
            "authorised_m3h":    report.nrw.authorised_m3h,
            "nrw_m3h":           report.nrw.nrw_m3h,
            "nrw_pct":           report.nrw.nrw_pct,
            "real_loss_m3h":     report.nrw.real_loss_m3h,
            "apparent_loss_m3h": report.nrw.apparent_loss_m3h,
            "ili":               report.nrw.ili,
            "source":            report.nrw.source,
        },
        "pressure_zones": [
            {"zone": z.zone, "count": z.count, "pct": z.avg_pct,
             "node_ids": z.node_ids[:20]} for z in report.pressure_zones
        ],
        "pipe_risks_top20": [
            {"pipe_id": r.pipe_id, "lat": r.lat, "lon": r.lon,
             "risk_score": r.risk_score, "risk_level": r.risk_level,
             "drivers": r.drivers, "avg_flow": r.avg_flow,
             "min_pressure_adjacent": r.min_pressure_adjacent}
            for r in report.pipe_risks[:20]
        ],
        "hotspots_geojson":  report.hotspots,
        "timestep_balance": [
            {"hour": b.hour, "inflow_m3h": b.inflow_m3h,
             "demand_m3h": b.demand_m3h, "nrw_m3h": b.nrw_m3h}
            for b in report.timestep_balance
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /dma/{filename}/simulate/{id}/tanks  — tank volume time-series
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{filename}/simulate/{scenario_id}/tanks")
async def get_tank_volumes(
    filename:    str,
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """
    Return tank water-volume time-series for a completed DMA simulation.
    """
    scenario = await db.get(SimScenario, scenario_id)
    if not scenario or scenario.gpkg_filename != filename:
        raise HTTPException(404, "Scenario not found.")
    if scenario.status != "DONE":
        raise HTTPException(409, f"Simulation is {scenario.status}, not DONE.")

    rows = (await db.execute(
        select(SimResult).where(
            SimResult.scenario_id == scenario_id,
            SimResult.element_type == "tank",
        ).order_by(SimResult.element_id, SimResult.time_step)
    )).scalars().all()

    by_tank: Dict[str, list] = {}
    for r in rows:
        by_tank.setdefault(r.element_id, []).append({
            "hour": r.time_step,
            "volume_m3": r.volume_m3 if hasattr(r, "volume_m3") else None,
        })

    return {
        "scenario_id": scenario_id,
        "dma_name":    (scenario.extra_demands or {}).get("_dma_name"),
        "tanks":       [{"tank_id": tid, "series": series}
                        for tid, series in sorted(by_tank.items())],
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _connectors_to_geojson(repair_report) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "geometry": {"type": "LineString",
                          "coordinates": [list(c.from_lonlat), list(c.to_lonlat)]},
             "properties": {"id": c.connector_id, "length_m": c.length_m,
                            "diam_mm": c.diam_mm, "material": c.material,
                            "reason": c.reason}}
            for c in repair_report.connectors_added
        ],
    }