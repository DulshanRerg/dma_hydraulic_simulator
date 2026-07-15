# app/routers/simulation.py
"""
Simulation endpoints — EPyT-Flow edition.

Key addition over the wntr version:
  extra_demands now supports full AbruptLeakage parameters so the main
  system can pass leak reports directly and EPyT-Flow will inject them
  as physical pipe-burst events in the simulation.

Endpoints
---------
POST   /simulate                    Queue a new simulation
GET    /simulate                    List scenarios
GET    /simulate/{id}               Status + summary
GET    /simulate/{id}/nodes         Node results
GET    /simulate/{id}/pipes         Pipe results
GET    /simulate/{id}/alerts        Low-pressure nodes + high-velocity pipes
GET    /simulate/{id}/geojson/nodes Node GeoJSON
GET    /simulate/{id}/geojson/pipes Pipe GeoJSON
DELETE /simulate/{id}               Delete scenario
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.database import get_db
from app.core.exceptions import SimulationNotFoundError, SimulationStillRunningError
from app.core.scenario_types import ALL_SCENARIO_TYPES, REPORTED_LEAK, validate_scenario_contract
from app.models.simulation import SimResult, SimScenario
from app.services.report_plots import plot_node_pressure, plot_pipe_flow
from app.services.rpt_parser import (
    parse_rpt_file,
    persisted_report_path,
    rpt_nrw_summary,
)
from app.workers.simulation_worker import run_simulation_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/simulate", tags=["simulation"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class LeakEvent(BaseModel):
    """
    Represents one active leak report to be injected as an EPyT-Flow
    AbruptLeakage event in the simulation.
    """
    lat:             float = Field(..., description="Latitude of the leak location")
    lon:             float = Field(..., description="Longitude of the leak location")
    demand_m3s:      float = Field(0.005, description="Extra demand added at the nearest node (m³/s)")
    leak_diameter_m: float = Field(0.01,  description="Orifice diameter for AbruptLeakage (metres)")
    start_time_s:    int   = Field(0,     description="Leak start time in seconds from simulation start")
    end_time_s:      int   = Field(86400, description="Leak end time in seconds from simulation start")


class SimulateRequest(BaseModel):
    gpkg_filename:  str         = Field(..., description="Filename of the .gpkg in the shared volume")
    name:           str         = Field("Hydraulic run", max_length=128)
    description:    Optional[str] = None
    pipe_status:    str         = Field("OPERATIONAL")
    base_demand:    float       = Field(0.001, gt=0, description="m³/s base demand per junction")
    duration_hrs:   int         = Field(24,  ge=1, le=168)
    time_step_min:  int         = Field(60,  ge=5, le=240)
    reservoir_head: float       = Field(50.0, gt=0, description="Reservoir head in metres")

    demand_model: str = Field(
        "DDA",
        pattern="^(DDA|PDA)$",
        description=(
            "Hydraulic demand model: 'DDA' (demand-driven analysis, default) or "
            "'PDA' (pressure-driven analysis)."
        ),
    )
    pda_pressure_min: float = Field(
        0.0, ge=0,
        description="PDA only: pressure (m) below which delivered demand is zero.",
    )
    pda_pressure_required: float = Field(
        0.1, gt=0,
        description="PDA only: pressure (m) at/above which full demand is delivered.",
    )
    pda_pressure_exponent: float = Field(
        0.5, gt=0,
        description="PDA only: exponent in the pressure-demand relationship.",
    )

    leak_events:    Optional[List[LeakEvent]] = Field(
        None,
        description=(
            "Active leak reports to simulate as EPyT-Flow AbruptLeakage events. "
            "Each entry is snapped to the nearest network node. Requires "
            "scenario_type='reported_leak'."
        ),
    )

    # scenario_type contract (app/core/scenario_types.py). NOTE: this
    # endpoint currently has no working .gpkg → .inp builder (see
    # simulation_worker.py's docstring) — any scenario queued here fails
    # at run time regardless of scenario_type. These fields exist for API
    # consistency with /dma/*/simulate and /inp/*/simulate, which do have
    # a working .inp pipeline and fully enforce this contract.
    scenario_type: str = Field(
        "baseline",
        description=f"One of: {', '.join(sorted(ALL_SCENARIO_TYPES))}.",
    )
    leakage_frac: float = Field(
        0.0, ge=0, le=1.0,
        description="Research-only: fraction of nodes to receive a random synthetic leak event. Requires scenario_type='research'.",
    )

    # ── sub-network simulation ──────────────────────────────────────────────
    # Pass the pipe_ids + a node from one component returned by
    # POST /network/{filename}/select to simulate only that extracted
    # sub-network instead of the whole .gpkg.
    pipe_ids:         Optional[List[int]] = Field(
        None,
        description=(
            "Restrict the simulation to exactly these pipe fids (one connected "
            "component from /network/{filename}/select). Omit to simulate the "
            "whole network, as before."
        ),
    )
    reservoir_lat:    Optional[float] = Field(
        None, description="Latitude of the chosen source node. Required when pipe_ids is set."
    )
    reservoir_lon:    Optional[float] = Field(
        None, description="Longitude of the chosen source node. Required when pipe_ids is set."
    )
    snap_tolerance_m: float = Field(
        2.0, ge=0, le=50,
        description="Endpoint-merging tolerance — must match the value used in /network/{filename}/select.",
    )

    @model_validator(mode="after")
    def _check_reservoir_with_pipe_ids(self):
        if self.pipe_ids and (self.reservoir_lat is None or self.reservoir_lon is None):
            raise ValueError("reservoir_lat and reservoir_lon are required when pipe_ids is set")
        return self

    @model_validator(mode="after")
    def _check_pda_pressures(self):
        if self.demand_model == "PDA" and self.pda_pressure_required <= self.pda_pressure_min:
            raise ValueError("pda_pressure_required must be greater than pda_pressure_min")
        return self

    @model_validator(mode="after")
    def _check_scenario_contract(self):
        try:
            validate_scenario_contract(
                scenario_type       = self.scenario_type,
                leakage_frac        = self.leakage_frac,
                has_reported_leaks  = bool(self.leak_events),
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ScenarioResponse(BaseModel):
    id:             int
    gpkg_filename:  str
    name:           str
    status:         str
    pipe_status:    str
    base_demand:    float
    duration_hrs:   int
    time_step_min:  int
    reservoir_head: float
    demand_model:   str
    pda_pressure_min:      float
    pda_pressure_required: float
    pda_pressure_exponent: float
    pipe_ids:       Optional[List[int]]
    reservoir_lat:  Optional[float]
    reservoir_lon:  Optional[float]
    snap_tolerance_m: float
    scenario_type:  str
    leakage_frac:   float
    created_at:     datetime
    started_at:     Optional[datetime]
    finished_at:    Optional[datetime]
    error_message:  Optional[str]
    summary:        Optional[Dict[str, Any]]

    class Config:
        from_attributes = True


class NodeResultResponse(BaseModel):
    element_id:      str
    time_step:       int
    lat:             Optional[float]
    lon:             Optional[float]
    pressure:        Optional[float]
    head:            Optional[float]
    demand:          Optional[float]
    water_age:       Optional[float]
    is_low_pressure: bool

    class Config:
        from_attributes = True


class PipeResultResponse(BaseModel):
    element_id:       str
    time_step:        int
    lat:              Optional[float]
    lon:              Optional[float]
    flow_rate:        Optional[float]
    velocity:         Optional[float]
    headloss:         Optional[float]
    is_high_velocity: bool

    class Config:
        from_attributes = True


class AlertsResponse(BaseModel):
    scenario_id:         int
    time_step:           Optional[int]
    low_pressure_nodes:  List[NodeResultResponse]
    high_velocity_pipes: List[PipeResultResponse]


# ── helpers ────────────────────────────────────────────────────────────────────

async def _get_or_404(scenario_id: int, db: AsyncSession) -> SimScenario:
    s = await db.get(SimScenario, scenario_id)
    if not s:
        raise SimulationNotFoundError(scenario_id)
    return s


def _to_geojson_feature(r: SimResult) -> dict:
    geometry = (
        {"type": "Point", "coordinates": [r.lon, r.lat]}
        if r.lat and r.lon else None
    )
    props: Dict[str, Any] = {
        "element_id":   r.element_id,
        "element_type": r.element_type,
        "time_step":    r.time_step,
    }
    if r.element_type == "node":
        props.update({
            "pressure":        r.pressure,
            "head":            r.head,
            "demand":          r.demand,
            "water_age":       r.water_age,
            "is_low_pressure": r.is_low_pressure,
        })
    else:
        props.update({
            "flow_rate":        r.flow_rate,
            "velocity":         r.velocity,
            "headloss":         r.headloss,
            "is_high_velocity": r.is_high_velocity,
        })
    return {"type": "Feature", "geometry": geometry, "properties": props}


# ── endpoints ──────────────────────────────────────────────────────────────────

@router.post("", status_code=202, response_model=ScenarioResponse)
async def create_simulation(
    body:       SimulateRequest,
    background: BackgroundTasks,
    db:         AsyncSession = Depends(get_db),
    _:          str          = Depends(require_api_key),
):
    """
    Queue a new EPyT-Flow hydraulic simulation.

    Returns **202 Accepted** immediately.
    Poll `GET /simulate/{id}` until `status=DONE`.

    **Leak events**: pass `leak_events` to inject AbruptLeakage scenarios
    from your leak report database — each is snapped to the nearest network
    node and simulated as a physical pipe burst using EPyT-Flow's leakage model.
    """
    # serialise leak_events → extra_demands JSON for storage
    extra_demands = (
        [ev.model_dump() for ev in body.leak_events]
        if body.leak_events else None
    )

    scenario = SimScenario(
        gpkg_filename  = body.gpkg_filename,
        name           = body.name,
        description    = body.description,
        pipe_status    = body.pipe_status,
        base_demand    = body.base_demand,
        duration_hrs   = body.duration_hrs,
        time_step_min  = body.time_step_min,
        reservoir_head = body.reservoir_head,
        extra_demands  = extra_demands,
        demand_model          = body.demand_model,
        pda_pressure_min      = body.pda_pressure_min,
        pda_pressure_required = body.pda_pressure_required,
        pda_pressure_exponent = body.pda_pressure_exponent,
        pipe_ids          = body.pipe_ids,
        reservoir_lat     = body.reservoir_lat,
        reservoir_lon     = body.reservoir_lon,
        snap_tolerance_m  = body.snap_tolerance_m,
        scenario_type     = body.scenario_type,
        leakage_frac      = body.leakage_frac,
        status         = "PENDING",
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)

    background.add_task(run_simulation_task, scenario.id)
    logger.info(
        "Queued scenario %d for '%s'  (%d leak events%s)",
        scenario.id, body.gpkg_filename, len(body.leak_events or []),
        f", subset of {len(body.pipe_ids)} pipes" if body.pipe_ids else "",
    )
    return scenario


@router.get("", response_model=List[ScenarioResponse])
async def list_simulations(
    limit:  int           = Query(20, ge=1, le=100),
    offset: int           = Query(0,  ge=0),
    status: Optional[str] = Query(None),
    db:     AsyncSession  = Depends(get_db),
    _:      str           = Depends(require_api_key),
):
    q = select(SimScenario).order_by(SimScenario.created_at.desc()).offset(offset).limit(limit)
    if status:
        q = q.where(SimScenario.status == status.upper())
    return (await db.execute(q)).scalars().all()


@router.get("/{scenario_id}", response_model=ScenarioResponse)
async def get_simulation(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """Poll this endpoint until `status` is `DONE` or `FAILED`."""
    return await _get_or_404(scenario_id, db)


@router.get("/{scenario_id}/nodes", response_model=List[NodeResultResponse])
async def get_node_results(
    scenario_id: int,
    time_step:   Optional[int] = Query(None, description="Hour index (0-based)"),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "node",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    return (await db.execute(q)).scalars().all()


@router.get("/{scenario_id}/pipes", response_model=List[PipeResultResponse])
async def get_pipe_results(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "pipe",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    return (await db.execute(q)).scalars().all()


@router.get("/{scenario_id}/alerts", response_model=AlertsResponse)
async def get_alerts(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """
    Return only anomalous elements:
    - Nodes with pressure below MIN_PRESSURE_M threshold
    - Pipes with velocity above MAX_VELOCITY_MS threshold
    """
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)

    def ts_filter(q, ts):
        return q.where(SimResult.time_step == ts) if ts is not None else q

    low_p_q = ts_filter(select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "node",
        SimResult.is_low_pressure == True,   # noqa: E712
    ), time_step)
    high_v_q = ts_filter(select(SimResult).where(
        SimResult.scenario_id   == scenario_id,
        SimResult.element_type  == "pipe",
        SimResult.is_high_velocity == True,  # noqa: E712
    ), time_step)

    low_p  = (await db.execute(low_p_q)).scalars().all()
    high_v = (await db.execute(high_v_q)).scalars().all()

    return AlertsResponse(
        scenario_id         = scenario_id,
        time_step           = time_step,
        low_pressure_nodes  = low_p,
        high_velocity_pipes = high_v,
    )


@router.get("/{scenario_id}/geojson/nodes")
async def get_nodes_geojson(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """Node results as GeoJSON FeatureCollection — load directly into Leaflet/MapLibre."""
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "node",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    rows = (await db.execute(q)).scalars().all()
    return {"type": "FeatureCollection", "features": [_to_geojson_feature(r) for r in rows]}


@router.get("/{scenario_id}/geojson/pipes")
async def get_pipes_geojson(
    scenario_id: int,
    time_step:   Optional[int] = Query(None),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """Pipe results as GeoJSON FeatureCollection (midpoint geometry)."""
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "pipe",
    )
    if time_step is not None:
        q = q.where(SimResult.time_step == time_step)
    rows = (await db.execute(q)).scalars().all()
    return {"type": "FeatureCollection", "features": [_to_geojson_feature(r) for r in rows]}


@router.get("/{scenario_id}/report", response_class=PlainTextResponse)
async def get_report_text(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """
    Return the raw EPANET .rpt file contents for this scenario, so it can be
    viewed directly (e.g. in a <pre> block in the frontend).
    """
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    path = persisted_report_path(scenario_id)
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=f"No .rpt report was persisted for scenario {scenario_id}.",
        )
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


@router.get("/{scenario_id}/report/download")
async def download_report(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """Download the raw .rpt file as an attachment."""
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    path = persisted_report_path(scenario_id)
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=f"No .rpt report was persisted for scenario {scenario_id}.",
        )
    return FileResponse(
        path,
        media_type="text/plain",
        filename=f"scenario_{scenario_id}.rpt",
    )


@router.get("/{scenario_id}/report/summary")
async def get_report_summary(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    """
    Structured summary parsed from the persisted .rpt file: EPANET's own
    hydraulic flow balance / NRW figure, convergence status, and any
    warning or status-change lines EPANET logged during the run.
    """
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)
    path = persisted_report_path(scenario_id)
    rpt = parse_rpt_file(path)
    if not rpt:
        raise HTTPException(
            status_code=404,
            detail=f"No .rpt report was persisted for scenario {scenario_id}.",
        )
    return {
        "scenario_id":    scenario_id,
        "flow_balance":   rpt_nrw_summary(rpt),
        "balanced":       rpt.balanced,
        "status_events":  [{"time": e.time_hms, "message": e.message} for e in rpt.status_events],
        "warnings":       rpt.warnings,
    }


@router.get("/{scenario_id}/plots/pressure")
async def get_pressure_plot(
    scenario_id: int,
    nodes:       Optional[str] = Query(
        None,
        description="Comma-separated node element_ids to plot, e.g. '13,16,22,30'. Omit to plot all nodes.",
    ),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """
    Render a pressure-vs-time-step chart (PNG) for the given nodes across
    every recorded time step — one line per node, matching the reference
    "Pressure in meter" chart.
    """
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)

    node_ids = [n.strip() for n in nodes.split(",")] if nodes else None
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "node",
    )
    if node_ids:
        q = q.where(SimResult.element_id.in_(node_ids))
    rows = (await db.execute(q)).scalars().all()

    try:
        png = plot_node_pressure(rows, node_ids=node_ids, time_step_min=s.time_step_min)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(content=png, media_type="image/png")


@router.get("/{scenario_id}/plots/flow")
async def get_flow_plot(
    scenario_id: int,
    links:       Optional[str] = Query(
        None,
        description="Comma-separated pipe/link element_ids to plot, e.g. '1,2'. Omit to plot all links.",
    ),
    db:          AsyncSession  = Depends(get_db),
    _:           str           = Depends(require_api_key),
):
    """
    Render a flow-rate-vs-time-step chart (PNG) for the given links across
    every recorded time step — one line per link, matching the reference
    "Flow rate in cubicmeter/hr" chart.
    """
    s = await _get_or_404(scenario_id, db)
    if s.status != "DONE":
        raise SimulationStillRunningError(scenario_id)

    link_ids = [n.strip() for n in links.split(",")] if links else None
    q = select(SimResult).where(
        SimResult.scenario_id  == scenario_id,
        SimResult.element_type == "pipe",
    )
    if link_ids:
        q = q.where(SimResult.element_id.in_(link_ids))
    rows = (await db.execute(q)).scalars().all()

    try:
        png = plot_pipe_flow(rows, link_ids=link_ids, time_step_min=s.time_step_min)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(content=png, media_type="image/png")


@router.delete("/{scenario_id}", status_code=204)
async def delete_simulation(
    scenario_id: int,
    db:          AsyncSession = Depends(get_db),
    _:           str          = Depends(require_api_key),
):
    s = await _get_or_404(scenario_id, db)
    if s.status == "RUNNING":
        raise SimulationStillRunningError(scenario_id)
    await db.delete(s)
    await db.commit()