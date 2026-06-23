# app/routers/dma.py
"""
DMA (District Metered Area) simulation endpoints.

Workflow
--------
GET  /dma/{filename}/layers          → GeoJSON FeatureCollections for every
                                       asset layer in the DMA (pipes, sources,
                                       tanks, valves, bulk_meters, boundary).

POST /dma/{filename}/simulate        → Build a full DMA EPANET model
                                       (multi-source, tanks, Hazen-Williams,
                                       bulk-meter monitoring) and run it.
                                       Returns a scenario_id for polling.

GET  /dma/{filename}/simulate/{id}/nrw
                                     → NRW (Non-Revenue Water) estimate for
                                       a completed simulation: compares
                                       inlet bulk meter flow with sum of
                                       junction demands.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_api_key
from app.core.database import get_session
from app.core.exceptions import GpkgNotFoundError, InvalidGpkgError
from app.models.simulation import SimResult, SimScenario
from app.services.dma_builder import build_dma_inp, estimate_nrw
from app.services.dma_ingest import ingest_dma
from app.workers.simulation_worker import run_simulation_task
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dma", tags=["dma"])


# ── GET layers ────────────────────────────────────────────────────────────────

@router.get("/{filename}/layers")
def get_dma_layers(
    filename: str,
    _: str = Depends(require_api_key),
):
    """
    Return all DMA asset layers as a single GeoJSON-per-layer dict.
    The frontend uses this to render the base map with icons for each
    asset type (boreholes, tanks, valves, bulk meters, DMA boundary).
    """
    try:
        dma = ingest_dma(filename, clip_to_dma=True)
    except GpkgNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidGpkgError as e:
        raise HTTPException(status_code=422, detail=str(e))

    def point_feature(lon, lat, props):
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        }

    def line_features(pipes):
        return [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[x, y] for x, y in p.coords]},
                "properties": {
                    "fid": p.fid, "diam_mm": p.diam_mm, "hw_c": p.hw_c,
                    "material": p.material, "purpose": p.purpose,
                    "length_m": round(p.length_m, 1),
                },
            }
            for p in pipes
        ]

    return {
        "dma_name":    dma.dma_name,
        "dma_bbox":    dma.dma_bbox,
        "boundary": {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in dma.dma_polygon]]},
                "properties": {"name": dma.dma_name},
            }],
        },
        "pipes": {
            "type": "FeatureCollection",
            "features": line_features(dma.pipes),
        },
        "sources": {
            "type": "FeatureCollection",
            "features": [
                point_feature(s.lon, s.lat, {
                    "fid": s.fid, "name": s.name, "elev_m": s.elev_m,
                    "yield_m3h": s.yield_m3h, "total_head_m": round(s.total_head_m, 1),
                    "status": s.status, "type": "borehole",
                })
                for s in dma.sources
            ],
        },
        "tanks": {
            "type": "FeatureCollection",
            "features": [
                point_feature(t.lon, t.lat, {
                    "fid": t.fid, "name": t.name, "elev_m": t.elev_m,
                    "cap_m3": t.cap_m3, "max_level_m": t.max_level_m,
                    "diameter_m": round(t.diameter_m, 2), "status": t.status, "type": "tank",
                })
                for t in dma.tanks
            ],
        },
        "valves": {
            "type": "FeatureCollection",
            "features": [
                point_feature(v.lon, v.lat, {
                    "fid": v.fid, "valve_type": v.valve_type,
                    "diam_mm": v.diam_mm, "is_isolation": v.is_isolation, "type": "valve",
                })
                for v in dma.valves
            ],
        },
        "bulk_meters": {
            "type": "FeatureCollection",
            "features": [
                point_feature(b.lon, b.lat, {"fid": b.fid, "name": b.name, "type": "bulk_meter"})
                for b in dma.bulk_meters
            ],
        },
        "stats": {
            "pipe_count":     len(dma.pipes),
            "source_count":   len(dma.sources),
            "tank_count":     len(dma.tanks),
            "valve_count":    len(dma.valves),
            "bulk_meter_count": len(dma.bulk_meters),
            "total_pipe_length_m": round(sum(p.length_m for p in dma.pipes), 1),
        },
    }


# ── POST simulate ──────────────────────────────────────────────────────────────

class DMASimRequest(BaseModel):
    name:            str   = Field("DMA hydraulic run", max_length=200)
    duration_hrs:    int   = Field(24, ge=1, le=168)
    time_step_min:   int   = Field(60, ge=5,  le=360)
    base_demand_m3h: float = Field(0.011, gt=0, description="Demand per junction (m³/h)")
    leakage_frac:    float = Field(0.20,  ge=0, le=1.0, description="Extra demand fraction modelling background leakage (0.20 = +20%)")


@router.post("/{filename}/simulate", status_code=202)
async def simulate_dma(
    filename:   str,
    body:       DMASimRequest,
    background: BackgroundTasks,
    db:         AsyncSession = Depends(get_session),
    _:          str          = Depends(require_api_key),
):
    """
    Build a full DMA EPANET model and run it in the background.

    The model includes:
    - All OPERATIONAL boreholes as Reservoir nodes (pump-boosted head)
    - All OPERATING storage tanks with real capacity/elevation geometry
    - Junction demands calibrated to `base_demand_m3h` per node
    - `leakage_frac` extra demand at every node (background leakage signal)
    - Hazen-Williams C from pipe material
    - Bulk meter nodes at the DMA inlet/outlet (zero demand, used for NRW)

    Returns a scenario_id to poll via GET /simulate/{id} and GET /dma/{file}/simulate/{id}/nrw
    """
    try:
        dma = ingest_dma(filename, clip_to_dma=True)
    except GpkgNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    except InvalidGpkgError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not dma.pipes:
        raise HTTPException(status_code=422, detail="No operational pipes found in the DMA.")
    if not dma.sources and not dma.tanks:
        raise HTTPException(status_code=422, detail="No water sources or tanks found in the DMA.")

    import tempfile
    inp_dir  = tempfile.mkdtemp(prefix="dma_epyt_")
    try:
        inp_path, repair_report = build_dma_inp(
            dma             = dma,
            inp_dir         = inp_dir,
            duration_hrs    = body.duration_hrs,
            time_step_min   = body.time_step_min,
            base_demand_m3h = body.base_demand_m3h,
            leakage_frac    = body.leakage_frac,
        )
    except Exception as e:
        logger.exception("DMA .inp build failed")
        raise HTTPException(status_code=422, detail=f"Failed to build EPANET model: {e}")

    # Serialise connectors for the response
    connectors_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(c.from_lonlat), list(c.to_lonlat)],
                },
                "properties": {
                    "id":        c.connector_id,
                    "length_m":  c.length_m,
                    "diam_mm":   c.diam_mm,
                    "material":  c.material,
                    "reason":    c.reason,
                },
            }
            for c in repair_report.connectors_added
        ],
    }

    # Store the pre-built inp_path and metadata in the scenario so the worker
    # can pick it up without rebuilding.  We reuse the existing SimScenario
    # model with pipe_ids=None (so the worker knows it's a pre-built .inp).
    scenario = SimScenario(
        gpkg_filename  = filename,
        name           = body.name,
        description    = f"DMA simulation — {dma.dma_name}",
        base_demand    = body.base_demand_m3h,
        duration_hrs   = body.duration_hrs,
        time_step_min  = body.time_step_min,
        reservoir_head = 0.0,
        extra_demands  = {
            "_dma_inp_path":         inp_path,
            "_dma_name":             dma.dma_name,
            "_leakage_frac":         body.leakage_frac,
            "_total_demand_m3h":     round(body.base_demand_m3h * (1 + body.leakage_frac), 6),
            "_connectors_added":     len(repair_report.connectors_added),
            "_connector_length_m":   repair_report.total_connector_length_m,
            "_original_components":  repair_report.original_component_count,
            "_repair_warnings":      repair_report.warnings,
        },
        status = "PENDING",
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)

    background.add_task(run_simulation_task, scenario.id)
    logger.info("Queued DMA scenario %d for '%s'", scenario.id, filename)
    return {
        "id":                   scenario.id,
        "status":               "PENDING",
        "dma_name":             dma.dma_name,
        "topology_repair": {
            "original_components":    repair_report.original_component_count,
            "connectors_added":       len(repair_report.connectors_added),
            "total_connector_length_m": repair_report.total_connector_length_m,
            "warnings":               repair_report.warnings,
            "connectors_geojson":     connectors_geojson,
        },
    }


# ── GET NRW ───────────────────────────────────────────────────────────────────

@router.get("/{filename}/simulate/{scenario_id}/nrw")
async def get_nrw(
    filename:    str,
    scenario_id: int,
    db:          AsyncSession = Depends(get_session),
    _:           str          = Depends(require_api_key),
):
    """
    Compute Non-Revenue Water (NRW) for a completed DMA simulation.

    NRW = system input − authorised consumption
    Where:
      system_input   = sum of flows at inlet bulk-meter pipe segments
      authorised     = sum of all junction demands in the simulation
    """
    scenario = await db.get(SimScenario, scenario_id)
    if not scenario or scenario.gpkg_filename != filename:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    if scenario.status != "DONE":
        raise HTTPException(status_code=409, detail=f"Simulation is {scenario.status}, not DONE.")
    if not scenario.extra_demands or "_dma_inp_path" not in scenario.extra_demands:
        raise HTTPException(status_code=422, detail="This scenario is not a DMA simulation.")

    summary = scenario.summary or {}
    total_demand = scenario.extra_demands.get("_total_demand_m3h", 0.011)

    # Use the simulation's summary flow data if available
    inlet_flow   = summary.get("inlet_flow_m3h",  0.0)
    outlet_flow  = summary.get("outlet_flow_m3h", 0.0)
    sim_demand   = summary.get("total_demand_m3h", total_demand)

    if inlet_flow == 0.0:
        # Derive system input from total node demand (no real meter reading yet)
        inlet_flow  = sim_demand * 1.0  # actual supply equals demand in steady state
        outlet_flow = 0.0

    nrw = estimate_nrw(
        inlet_flow_m3h   = inlet_flow,
        total_demand_m3h = sim_demand,
        outlet_flow_m3h  = outlet_flow,
    )

    return {
        "scenario_id":        scenario_id,
        "dma_name":           scenario.extra_demands.get("_dma_name"),
        "simulation_summary": summary,
        "nrw":                nrw,
        "note": (
            "inlet_flow_m3h derived from simulation demand because real bulk-meter "
            "readings are not yet connected.  Connect live meter data to improve accuracy."
            if inlet_flow == sim_demand else
            "inlet_flow_m3h from bulk-meter simulation nodes."
        ),
    }