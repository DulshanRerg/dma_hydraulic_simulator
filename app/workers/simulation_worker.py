# app/workers/simulation_worker.py
"""
Background task — EPyT-Flow v0.17.x edition.

This service is a hydraulic simulation *engine*: it does not decide on
its own where leaks are. Every scenario carries a `scenario_type`
(see app/core/scenario_types.py) that determines where its leak events,
if any, come from:

    baseline / planned_shutdown / fire_flow   No leak events, ever.
    reported_leak   Leak events come only from `reported_leaks`,
                     resolved and validated against the network by the
                     router *before* the scenario was queued (see
                     routers/dma.py, routers/inp.py) and handed to this
                     worker pre-resolved via extra_demands["_leak_events"].
    research         The only scenario_type allowed to fall back to the
                     legacy random-per-node `leakage_frac` generator.

Pipeline
--------
1. Mark scenario RUNNING.
2. Look up the .inp already built for this scenario by the DMA ingest
   pipeline (dma_builder.build_dma_inp, invoked from routers/dma.py or
   routers/inp.py), via extra_demands["_dma_inp_path"]. There is no
   longer any direct .gpkg → .inp / subset-selection builder here —
   that used to live in network_builder.py / network_subset.py, which
   have been removed. A scenario without a pre-built .inp path fails
   fast with a clear error.
3. Resolve leak events per the scenario_type contract above.
4. Run EPyT-Flow simulation.
5. Persist results in batches. For scenario_type="reported_leak", also
   compute a service-impact summary and topological isolation
   recommendations per leak.
6. Mark DONE or FAILED; clean up temp .inp file.
"""

import logging
import math
import os
import shutil
from datetime import datetime

from app.core.config import get_settings
from app.core.database import get_session
from app.core.scenario_types import ALL_SCENARIO_TYPES, RANDOM_LEAKS_ALLOWED_FOR, REPORTED_LEAK
from app.models.simulation import SimResult, SimScenario
from app.services.leak_report import compute_service_impact, recommend_isolation
from app.services.rpt_parser import parse_rpt, rpt_nrw_summary
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


def _parse_inp_pipe_diameters(inp_path: str) -> dict:
    """
    Parse [PIPES] from .inp → {pipe_id: diam_mm}.

    Used so the leakage-risk report can compute real per-pipe velocity
    instead of assuming a fixed diameter for every pipe. Must be captured
    here, at build/run time, because the .inp is deleted once the scenario
    finishes (see the `finally` block below) and the leakage report is
    generated later, on demand, from persisted DB rows alone.
    """
    diam_mm, in_sec = {}, False
    with open(inp_path) as f:
        for line in f:
            s = line.strip()
            if s.upper().startswith("[PIPES]"):
                in_sec = True
                continue
            if s.startswith("[") and in_sec:
                break
            if not in_sec or s.startswith(";") or not s:
                continue
            parts = s.split()
            # ;ID  Node1  Node2  Length(m)  Diam(mm)  C(H-W)  Minor  Status
            if len(parts) >= 5:
                try:
                    diam_mm[parts[0]] = float(parts[4])
                except ValueError:
                    continue
    return diam_mm


def _nearest_node(lat: float, lon: float, coords: dict) -> str:
    best, best_d = None, float("inf")
    for nid, (nx, ny) in coords.items():
        d = math.hypot(nx - lon, ny - lat)
        if d < best_d:
            best_d, best = d, nid
    return best


def _build_leak_events(
    extra_demands, inp_path: str, duration_sec: int
) -> list:
    """
    Map lat/lon extra_demands → EPyT-Flow leak event dicts.
    Keys returned: node_id, diameter, start_time, end_time.

    DMA/inp scenarios store a metadata dict in extra_demands (keys start
    with "_") rather than a bare list of [{lat, lon, ...}] leak events.
    One exception: a pre-resolved `_leak_events` list nested inside that
    dict — set by routers/dma.py or routers/inp.py for scenario_type=
    "reported_leak", after validating the report(s) against the network
    via leak_report.resolve_and_validate_reports(). Those are already in
    the {node_id, diameter, start_time, end_time, ...} shape the worker
    needs, so they're returned as-is rather than re-resolved from lat/lon.
    """
    if not extra_demands:
        return []

    if isinstance(extra_demands, dict):
        pre_resolved = extra_demands.get("_leak_events")
        if isinstance(pre_resolved, list) and pre_resolved:
            return [
                {
                    "node_id":    e["node_id"],
                    "diameter":   e.get("diameter", 0.01),
                    "start_time": e.get("start_time", 0),
                    "end_time":   e.get("end_time", duration_sec),
                }
                for e in pre_resolved if "node_id" in e
            ]
        return []
    if not isinstance(extra_demands, list):
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

        # step 1: locate the .inp for this scenario
        #
        # The only supported source is a .inp pre-built by the DMA ingest
        # pipeline (dma_builder.build_dma_inp), whose path is stashed in
        # extra_demands["_dma_inp_path"] by routers/dma.py or routers/inp.py.
        # Building an .inp directly from a raw .gpkg (whole-network or a
        # pipe_ids subset) is no longer supported — network_builder.py and
        # network_subset.py, which used to do that, have been removed.
        extra = scenario.extra_demands or {}
        dma_inp_path = extra.get("_dma_inp_path") if isinstance(extra, dict) else None

        if not (dma_inp_path and os.path.isfile(dma_inp_path)):
            raise RuntimeError(
                "No pre-built .inp found for this scenario "
                "(extra_demands['_dma_inp_path'] missing or file not found). "
                "Direct .gpkg-to-.inp building (including pipe_ids subsets) is "
                "no longer supported — build the scenario via the DMA ingest "
                "pipeline first."
            )

        inp_path = dma_inp_path
        logger.info("[%d] Using pre-built .inp: %s", scenario_id, inp_path)

        logger.info(
            "[%d] scenario_type=%s leakage_frac=%s demand_model=%s",
            scenario_id,
            scenario.scenario_type,
            scenario.leakage_frac,
            scenario.demand_model,
        )

        if scenario.scenario_type not in ALL_SCENARIO_TYPES:
            raise RuntimeError(
                f"Unknown scenario_type '{scenario.scenario_type}' — must be one of "
                f"{sorted(ALL_SCENARIO_TYPES)}."
            )

        # step 2: resolve leak events
        leak_events = _build_leak_events(
            scenario.extra_demands or [], inp_path, duration_sec
        )

        if scenario.scenario_type == REPORTED_LEAK:
            # The router (routers/dma.py / routers/inp.py) must already have
            # resolved and validated the report(s) against this network and
            # stashed them in extra_demands["_leak_events"] before queuing.
            # If none made it through, this scenario was queued incorrectly
            # — fail loudly rather than silently running a "reported_leak"
            # scenario with no leak in it.
            if not leak_events:
                raise RuntimeError(
                    "scenario_type='reported_leak' but no resolved leak events "
                    "were found in extra_demands['_leak_events']. The report(s) "
                    "must be validated against the network at request time."
                )
        elif not leak_events and scenario.leakage_frac > 0:
            # Random/synthetic per-node leak generation — a research and
            # testing convenience only. Never a silent default for a
            # production run driven by the main water-management system.
            if scenario.scenario_type not in RANDOM_LEAKS_ALLOWED_FOR:
                raise RuntimeError(
                    f"leakage_frac={scenario.leakage_frac} is set but scenario_type="
                    f"'{scenario.scenario_type}' — random/synthetic leak generation "
                    "is only permitted for scenario_type='research'. This should "
                    "have been rejected when the scenario was created; failing the "
                    "run instead of silently injecting leaks."
                )

            coords = _parse_inp_coordinates(inp_path)

            all_nodes = list(coords.keys())

            leak_count = max(
                1,
                int(len(all_nodes) * scenario.leakage_frac)
            )

            import random

            selected_nodes = random.sample(
                all_nodes,
                min(leak_count, len(all_nodes))
            )

            leak_events = [
                {
                    "node_id": node_id,
                    "diameter": 0.01,
                    "start_time": 0,
                    "end_time": duration_sec,
                }
                for node_id in selected_nodes
            ]

            logger.info(
                "[%d] Generated %d research-mode synthetic leak events (fraction=%.2f)",
                scenario_id,
                len(leak_events),
                scenario.leakage_frac,
            )

        # step 3: EPyT-Flow
        output = run_simulation(
            inp_path      = inp_path,
            duration_hrs  = scenario.duration_hrs,
            time_step_min = scenario.time_step_min,
            leak_events   = leak_events,
            demand_model          = scenario.demand_model,
            pda_pressure_min      = scenario.pda_pressure_min,
            pda_pressure_required = scenario.pda_pressure_required,
            pda_pressure_exponent = scenario.pda_pressure_exponent,
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

        # tank results — stored as "tank" element_type (no schema change needed)
        for t in output.tank_results:
            records.append(SimResult(
                scenario_id  = scenario_id,
                time_step    = t.time_step,
                element_type = "tank",
                element_id   = t.element_id,
                lat          = t.lat,
                lon          = t.lon,
                # reuse flow_rate column for volume_m3 (same float, labelled by element_type)
                flow_rate    = t.volume_m3,
            ))

        total = len(records)

        async with get_session() as session:
            for i in range(0, len(records), BATCH_SIZE):
                session.add_all(records[i : i + BATCH_SIZE])
                await session.flush()
            scenario = await session.get(SimScenario, scenario_id)
            scenario.status      = "DONE"
            scenario.finished_at = datetime.utcnow()

            # Merge EPANET's own .rpt flow balance into the summary
            # so the NRW endpoint gets real figures, not estimates.
            merged_summary = dict(output.summary)

            # Real per-pipe diameters (mm) — captured now because the .inp
            # (and with it the [PIPES] section) is deleted in `finally` below,
            # but the leakage report is generated later, on demand.
            try:
                merged_summary["pipe_diam_mm"] = _parse_inp_pipe_diameters(inp_path)
            except OSError as diam_exc:
                logger.warning("[%d] Could not parse pipe diameters: %s", scenario_id, diam_exc)
                merged_summary["pipe_diam_mm"] = {}

            rpt = parse_rpt(inp_path)
            if rpt:
                merged_summary["epanet_flow_balance"] = rpt_nrw_summary(rpt)
                merged_summary["epanet_status_events"] = len(rpt.status_events)
                if rpt.flow_balance:
                    merged_summary["inlet_flow_m3h"]    = rpt.flow_balance.total_inflow_m3h
                    merged_summary["total_demand_m3h"]  = rpt.flow_balance.consumer_demand_m3h
                if not rpt.balanced:
                    merged_summary.setdefault("warnings", []).extend(rpt.warnings)

            # Reported-leak analysis: service impact (pressure-drop footprint)
            # and topological isolation candidates per leak. Computed now,
            # while inp_path (needed for pipe topology) still exists — it's
            # deleted in the `finally` block below.
            if scenario.scenario_type == REPORTED_LEAK:
                resolved_leaks = (scenario.extra_demands or {}).get("_leak_events") or []
                try:
                    isolation = [
                        recommend_isolation(rl, inp_path) for rl in resolved_leaks
                    ]
                except OSError as iso_exc:
                    logger.warning(
                        "[%d] Could not compute isolation recommendations: %s",
                        scenario_id, iso_exc,
                    )
                    isolation = []

                merged_summary["reported_leak_analysis"] = {
                    "service_impact": compute_service_impact(output.node_results),
                    "isolation_recommendations": isolation,
                }

            # Persist the raw .rpt file itself (not just the parsed summary)
            # so the user can view/download the full EPANET report after the
            # temp .inp/.rpt working directory is cleaned up below.
            rpt_src = inp_path + ".rpt"
            report_available = False
            if os.path.isfile(rpt_src):
                settings = get_settings()
                try:
                    os.makedirs(settings.reports_dir, exist_ok=True)
                    from app.services.rpt_parser import persisted_report_path
                    shutil.copy2(rpt_src, persisted_report_path(scenario_id))
                    report_available = True
                except OSError as copy_exc:
                    logger.warning(
                        "[%d] Could not persist .rpt file: %s", scenario_id, copy_exc
                    )
            merged_summary["report_available"] = report_available

            scenario.summary = merged_summary
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