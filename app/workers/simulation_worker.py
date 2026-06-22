# app/workers/simulation_worker.py
"""
Background task — EPyT-Flow v0.17.x edition.

Pipeline
--------
1. Mark scenario RUNNING.
2. Convert .gpkg → EPANET .inp  via network_builder.
3. Resolve lat/lon from extra_demands → nearest EPANET node ID
   (parsed from [COORDINATES] in the generated .inp).
4. Build leak_event dicts for simulation_service (keys: node_id,
   diameter, start_time, end_time).
5. Run EPyT-Flow simulation.
6. Persist results in batches.
7. Mark DONE or FAILED; clean up temp .inp file.
"""

import logging
import math
import os
import tempfile
from datetime import datetime

from app.core.database import get_session
from app.models.simulation import SimResult, SimScenario
from app.services.network_builder import build_inp_from_gpkg
from app.services.network_subset import build_inp_from_subset
from app.services.simulation_service import run_simulation

logger = logging.getLogger(__name__)
BATCH_SIZE = 500


def _parse_inp_coordinates(inp_path: str) -> dict:
    """Parse [COORDINATES] from .inp → {node_id: (lon, lat)}."""
    coords, in_sec = {}, False
    with open(inp_path) as f:
        for line in f:
            s = line.strip()
            if s.upper().startswith("[COORDINATES]"):
                in_sec = True
                continue
            if s.startswith("[") and in_sec:
                break
            if not in_sec or s.startswith(";") or not s:
                continue
            parts = s.split()
            if len(parts) >= 3:
                coords[parts[0]] = (float(parts[1]), float(parts[2]))
    return coords


def _nearest_node(lat: float, lon: float, coords: dict) -> str:
    best, best_d = None, float("inf")
    for nid, (nx, ny) in coords.items():
        d = math.hypot(nx - lon, ny - lat)
        if d < best_d:
            best_d, best = d, nid
    return best


def _build_leak_events(
    extra_demands: list, inp_path: str, duration_sec: int
) -> list:
    """
    Map lat/lon extra_demands → EPyT-Flow leak event dicts.
    Keys returned: node_id, diameter, start_time, end_time.
    """
    if not extra_demands:
        return []

    node_coords = _parse_inp_coordinates(inp_path)
    if not node_coords:
        logger.warning("No coordinates parsed from .inp — leak events skipped")
        return []

    events = []
    for ed in extra_demands:
        lat = ed.get("lat", 0.0)
        lon = ed.get("lon", 0.0)
        # skip dummy 0,0 coordinates (happens when leak_events are not geo-tagged)
        if lat == 0.0 and lon == 0.0:
            logger.warning("Skipping leak event with (0,0) coordinates")
            continue
        nid = _nearest_node(lat, lon, node_coords)
        if nid:
            events.append({
                "node_id":    nid,
                "diameter":   ed.get("leak_diameter_m", 0.01),
                "start_time": ed.get("start_time_s",   0),
                "end_time":   ed.get("end_time_s",     duration_sec),
            })
            logger.info(
                "Leak event: (%.5f, %.5f) → node %s  Ø=%.3f m",
                lat, lon, nid, ed.get("leak_diameter_m", 0.01),
            )
    return events


async def run_simulation_task(scenario_id: int) -> None:
    inp_path = None

    async with get_session() as session:
        scenario = await session.get(SimScenario, scenario_id)
        if not scenario:
            logger.error("Scenario %d not found.", scenario_id)
            return
        scenario.status     = "RUNNING"
        scenario.started_at = datetime.utcnow()
        await session.commit()
    logger.info("[%d] Status → RUNNING", scenario_id)

    try:
        duration_sec = scenario.duration_hrs * 3600

        # step 1: .gpkg → .inp
        inp_dir  = tempfile.mkdtemp(prefix="epytflow_")
        if scenario.pipe_ids:
            logger.info(
                "[%d] Building .inp from a %d-pipe subset (reservoir=%.6f,%.6f)",
                scenario_id, len(scenario.pipe_ids), scenario.reservoir_lat, scenario.reservoir_lon,
            )
            inp_path = build_inp_from_subset(
                filename         = scenario.gpkg_filename,
                pipe_fids        = scenario.pipe_ids,
                reservoir_lat    = scenario.reservoir_lat,
                reservoir_lon    = scenario.reservoir_lon,
                snap_tolerance_m = scenario.snap_tolerance_m or 2.0,
                base_demand      = scenario.base_demand,
                reservoir_head   = scenario.reservoir_head,
                duration_hrs     = scenario.duration_hrs,
                time_step_min    = scenario.time_step_min,
                extra_demands    = scenario.extra_demands,
                inp_dir          = inp_dir,
            )
        else:
            inp_path = build_inp_from_gpkg(
                filename       = scenario.gpkg_filename,
                pipe_status    = scenario.pipe_status,
                base_demand    = scenario.base_demand,
                reservoir_head = scenario.reservoir_head,
                duration_hrs   = scenario.duration_hrs,
                time_step_min  = scenario.time_step_min,
                extra_demands  = scenario.extra_demands,
                inp_dir        = inp_dir,
            )
        logger.info("[%d] .inp → %s", scenario_id, inp_path)

        # step 2: resolve leak events
        leak_events = _build_leak_events(
            scenario.extra_demands or [], inp_path, duration_sec
        )

        # step 3: EPyT-Flow
        output = run_simulation(
            inp_path      = inp_path,
            duration_hrs  = scenario.duration_hrs,
            time_step_min = scenario.time_step_min,
            leak_events   = leak_events,
        )

        # step 4: persist
        total = len(output.node_results) + len(output.pipe_results)
        logger.info("[%d] Persisting %d records …", scenario_id, total)

        records = []
        for n in output.node_results:
            records.append(SimResult(
                scenario_id     = scenario_id,
                time_step       = n.time_step,
                element_type    = "node",
                element_id      = n.element_id,
                lat             = n.lat,
                lon             = n.lon,
                pressure        = n.pressure,
                head            = n.head,
                demand          = n.demand,
                water_age       = n.water_age,
                is_low_pressure = n.is_low_pressure,
            ))
        for p in output.pipe_results:
            records.append(SimResult(
                scenario_id      = scenario_id,
                time_step        = p.time_step,
                element_type     = "pipe",
                element_id       = p.element_id,
                lat              = p.lat,
                lon              = p.lon,
                flow_rate        = p.flow_rate,
                velocity         = p.velocity,
                headloss         = p.headloss,
                is_high_velocity = p.is_high_velocity,
            ))

        async with get_session() as session:
            for i in range(0, len(records), BATCH_SIZE):
                session.add_all(records[i : i + BATCH_SIZE])
                await session.flush()
            scenario = await session.get(SimScenario, scenario_id)
            scenario.status      = "DONE"
            scenario.finished_at = datetime.utcnow()
            scenario.summary     = output.summary
            await session.commit()

        logger.info("[%d] Status → DONE  (%d records)", scenario_id, total)

    except Exception as exc:
        logger.exception("[%d] FAILED: %s", scenario_id, exc)
        async with get_session() as session:
            scenario = await session.get(SimScenario, scenario_id)
            if scenario:
                scenario.status        = "FAILED"
                scenario.error_message = str(exc)
                scenario.finished_at   = datetime.utcnow()
                await session.commit()
    finally:
        if inp_path and os.path.isfile(inp_path):
            try:
                os.remove(inp_path)
                os.rmdir(os.path.dirname(inp_path))
            except OSError:
                pass