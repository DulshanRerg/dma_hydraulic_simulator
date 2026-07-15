# app/routers/inp.py
"""
POST /inp/upload              — upload a raw EPANET .inp file
GET  /inp                     — list uploaded .inp files
GET  /inp/{filename}/layers   — GeoJSON preview of the network (pre-simulation)
DELETE /inp/{filename}        — remove an uploaded .inp file
POST /inp/{filename}/simulate — queue a simulation directly from it

Why this exists
----------------
The `/dma/*` pipeline reconstructs a network from GIS point/line layers,
which is necessarily lossy: pumps, real PRV/PSV/FCV valves, non-cylindrical
tanks, multi-category demand patterns and [CONTROLS]/[RULES] logic aren't
present in that GIS data, so the generated .inp can't represent them (see
dma_builder.py for exactly which EPANET features it does/doesn't cover).

If the user already has a real EPANET .inp — e.g. exported from EPANET,
epanet-js, or built by hand — this route runs it directly through the same
EPyT-Flow engine (`simulation_service.run_simulation`) with none of that
loss. Everything in the file (pumps, curves, valves, controls, patterns,
multiple demand categories) is preserved exactly as the engine sees it.

Persistence note
-----------------
Uploaded .inp files are written to `settings.inp_dir`, which has the same
non-persistent-disk caveat as `settings.gpkg_dir` on Render's free tier
(see render.yaml) — they will be lost on redeploy/restart unless a
persistent disk (or object storage) is attached.
"""

import logging
import os
import re
import shutil
import tempfile
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.config import get_settings
from app.core.database import get_db
from app.core.scenario_types import ALL_SCENARIO_TYPES, REPORTED_LEAK, validate_scenario_contract
from app.models.simulation import SimScenario
from app.services.inp_parser import parse_inp_coordinates, parse_inp_pipe_topology
from app.services.leak_report import LeakValidationError, ReportedLeak, resolve_and_validate_reports
from app.workers.simulation_worker import run_simulation_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inp", tags=["inp"])

_MAX_UPLOAD_MB = 50
_SAFE_NAME_RE  = re.compile(r"[^\w\-.]")

# A real .inp doesn't need every section, but without at least one node
# section and one link section there's nothing to simulate.
_REQUIRED_ANY_OF_NODE_SECTIONS = ("[JUNCTIONS]", "[RESERVOIRS]", "[TANKS]")
_REQUIRED_ANY_OF_LINK_SECTIONS = ("[PIPES]", "[PUMPS]", "[VALVES]")


class InpUploadResult(BaseModel):
    filename:        str
    size_kb:         float
    sections:        List[str]
    junction_count:  int
    pipe_count:      int
    pump_count:      int
    valve_count:     int
    tank_count:      int
    has_controls:    bool
    has_rules:       bool
    message:         str


class InpSimulateRequest(BaseModel):
    name:          str            = Field("Uploaded .inp run", max_length=128)
    description:   Optional[str]  = None
    duration_hrs:  Optional[int]  = Field(
        None, ge=1, le=336,
        description="Override [TIMES] Duration in the .inp. Omit to use the file's own value.",
    )
    time_step_min: Optional[int]  = Field(
        None, ge=1, le=240,
        description="Override [TIMES] Hydraulic Timestep. Omit to use the file's own value.",
    )
    demand_model: str = Field(
        "DDA",
        pattern="^(DDA|PDA)$",
        description="'DDA' honours the file's own demand model unless PDA is explicitly requested.",
    )
    pda_pressure_min:      float = Field(0.0, ge=0)
    pda_pressure_required: float = Field(0.1, gt=0)
    pda_pressure_exponent: float = Field(0.5, gt=0)

    # scenario_type contract (app/core/scenario_types.py): this is a
    # simulation engine that consumes leak events supplied by the main
    # water-management system — it does not decide on its own where
    # leaks are. leakage_frac (random/synthetic per-node leak
    # generation) is only usable when scenario_type="research".
    scenario_type: str = Field(
        "baseline",
        description=f"One of: {', '.join(sorted(ALL_SCENARIO_TYPES))}.",
    )
    leakage_frac: float = Field(
        0.0, ge=0, le=1.0,
        description="Research-only: fraction of nodes to receive a random synthetic leak event. Requires scenario_type='research'.",
    )
    reported_leaks: Optional[List[ReportedLeak]] = Field(
        None,
        description="Required when scenario_type='reported_leak'. Leak report(s) from the main system, validated against the network before the scenario is queued.",
    )

    @model_validator(mode="after")
    def _check_scenario_contract(self):
        try:
            validate_scenario_contract(
                scenario_type       = self.scenario_type,
                leakage_frac        = self.leakage_frac,
                has_reported_leaks  = bool(self.reported_leaks),
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


def _parse_epanet_time(raw: str, default: float) -> float:
    """Parse an EPANET time value (hours, or HH:MM[:SS]) into hours."""
    raw = raw.strip()
    if not raw:
        return default
    if ":" in raw:
        parts = raw.split(":")
        try:
            h = float(parts[0])
            m = float(parts[1]) if len(parts) > 1 else 0.0
            s = float(parts[2]) if len(parts) > 2 else 0.0
            return h + m / 60 + s / 3600
        except ValueError:
            return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_times_section(text: str) -> tuple:
    """Return (duration_hrs, hydraulic_timestep_min) from [TIMES], or
    (24, 60) if the section/keys are missing or unparsable."""
    duration_hrs, timestep_min = 24.0, 60.0
    for line in _section_lines(text, "[TIMES]"):
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        key, val = parts[0].upper(), parts[1]
        if key == "DURATION":
            duration_hrs = _parse_epanet_time(val, duration_hrs)
        elif key == "HYDRAULIC" and val.upper().startswith("TIMESTEP"):
            # line looks like "Hydraulic Timestep 1" -> key="HYDRAULIC", val="Timestep 1"
            ts_val = val.split(None, 1)[1] if len(val.split(None, 1)) > 1 else ""
            timestep_min = _parse_epanet_time(ts_val, timestep_min / 60) * 60
    return duration_hrs, timestep_min


def _safe_filename(name: str) -> str:
    base = os.path.basename(name)
    safe = _SAFE_NAME_RE.sub("_", base)
    safe = re.sub(r"_+", "_", safe).strip("_.")
    if not safe.lower().endswith(".inp"):
        safe += ".inp"
    return safe


def _inp_path(filename: str) -> str:
    settings = get_settings()
    return os.path.join(settings.inp_dir, filename)


def _section_lines(text: str, section: str) -> List[str]:
    """Return the (non-comment, non-empty) data lines under `[SECTION]`."""
    lines, in_sec = [], False
    for raw in text.splitlines():
        s = raw.strip()
        if s.upper() == section:
            in_sec = True
            continue
        if s.startswith("[") and in_sec:
            break
        if not in_sec:
            continue
        if not s or s.startswith(";"):
            continue
        lines.append(s)
    return lines


def _inspect_inp(text: str) -> dict:
    upper = text.upper()
    sections = sorted(set(re.findall(r"^\[[A-Z]+\]", upper, flags=re.MULTILINE)))
    return {
        "sections":       sections,
        "junction_count": len(_section_lines(text, "[JUNCTIONS]")),
        "pipe_count":     len(_section_lines(text, "[PIPES]")),
        "pump_count":     len(_section_lines(text, "[PUMPS]")),
        "valve_count":    len(_section_lines(text, "[VALVES]")),
        "tank_count":     len(_section_lines(text, "[TANKS]")),
        "has_controls":   "[CONTROLS]" in sections and bool(_section_lines(text, "[CONTROLS]")),
        "has_rules":      "[RULES]" in sections and bool(_section_lines(text, "[RULES]")),
    }


# ── network preview (GeoJSON) ────────────────────────────────────────────────
# Pipe topology reuses parse_inp_pipe_topology from inp_parser.py (the same
# helper simulation_service.py relies on) — [PUMPS] and [VALVES] have their
# own two-node-plus-keywords layout so they get their own light parsers here.

def _parse_link_endpoints(text: str, section: str) -> Dict[str, Tuple[str, str]]:
    """[PUMPS] / [VALVES] both start with `Id  Node1  Node2  ...` — same
    two-node shape as [PIPES], just with different trailing fields."""
    out: Dict[str, Tuple[str, str]] = {}
    for line in _section_lines(text, section):
        parts = line.split()
        if len(parts) >= 3:
            out[parts[0]] = (parts[1], parts[2])
    return out


def _parse_pipe_attrs(text: str) -> Dict[str, dict]:
    """[PIPES] Id Node1 Node2 Length Diam Roughness [MinorLoss] [Status]"""
    out: Dict[str, dict] = {}
    for line in _section_lines(text, "[PIPES]"):
        parts = line.split()
        if len(parts) < 3:
            continue
        def f(i, default=None):
            try:
                return float(parts[i])
            except (IndexError, ValueError):
                return default
        out[parts[0]] = {
            "length_m":   f(3),
            "diam_mm":    f(4),
            "roughness":  f(5),
            "status":     parts[7] if len(parts) > 7 else (parts[6] if len(parts) > 6 and not
                          _is_number(parts[6]) else "Open"),
        }
    return out


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _parse_valve_attrs(text: str) -> Dict[str, dict]:
    """[VALVES] Id Node1 Node2 Diameter Type Setting [MinorLoss]"""
    out: Dict[str, dict] = {}
    for line in _section_lines(text, "[VALVES]"):
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            diam = float(parts[3])
        except ValueError:
            diam = None
        out[parts[0]] = {"diam_mm": diam, "valve_type": parts[4],
                          "setting": parts[5] if len(parts) > 5 else None}
    return out


def _node_ids_in_section(text: str, section: str) -> List[str]:
    return [line.split()[0] for line in _section_lines(text, section) if line.split()]


def _looks_geographic(coords: Dict[str, Tuple[float, float]]) -> bool:
    """Heuristic only: EPANET [COORDINATES] can be real lon/lat (as in this
    epanet-js export) or arbitrary local Cartesian units (feet/metres from
    an assumed origin) — the .inp format doesn't say which. If every value
    falls inside valid lon/lat ranges we assume geographic; local-unit
    networks are typically far outside +/-180 / +/-90 and get flagged so
    the caller can warn instead of silently mis-plotting them."""
    if not coords:
        return False
    xs = [c[0] for c in coords.values()]
    ys = [c[1] for c in coords.values()]
    return all(-180 <= x <= 180 for x in xs) and all(-90 <= y <= 90 for y in ys)


def _line_feature(coords, node1, node2, props: dict) -> Optional[dict]:
    c1, c2 = coords.get(node1), coords.get(node2)
    if not c1 or not c2:
        return None
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[c1[0], c1[1]], [c2[0], c2[1]]]},
        "properties": props,
    }


def _point_feature(coords, node_id, props: dict) -> Optional[dict]:
    c = coords.get(node_id)
    if not c:
        return None
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [c[0], c[1]]},
        "properties": props,
    }


def _fc(features: List[Optional[dict]]) -> dict:
    return {"type": "FeatureCollection", "features": [f for f in features if f]}


@router.get("/{filename}/layers")
async def get_inp_layers(filename: str, _: str = Depends(require_api_key)):
    """
    GeoJSON preview of the network exactly as written in the uploaded
    `.inp` — pipes/pumps/valves as lines, reservoirs/tanks as points —
    built straight from `[COORDINATES]`, with no reconstruction involved
    (contrast with `/dma/{file}/layers`, which rebuilds a network from GIS
    data). Meant for a "load onto map" preview before running a simulation.

    `has_geographic_coords` is a heuristic (see `_looks_geographic`) — some
    `.inp` files use local Cartesian coordinates rather than real lon/lat,
    in which case plotting them on a geographic map is meaningless; the
    frontend should check this flag and warn instead of rendering blindly.
    """
    path = _inp_path(_safe_filename(filename))
    if not os.path.isfile(path):
        raise HTTPException(404, f"'{filename}' not found. Upload it first via POST /inp/upload.")

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    coords     = parse_inp_coordinates(path)
    pipe_topo  = parse_inp_pipe_topology(path)
    pipe_attrs = _parse_pipe_attrs(text)
    pump_topo  = _parse_link_endpoints(text, "[PUMPS]")
    valve_topo = _parse_link_endpoints(text, "[VALVES]")
    valve_attrs = _parse_valve_attrs(text)

    junction_ids  = _node_ids_in_section(text, "[JUNCTIONS]")
    reservoir_ids = _node_ids_in_section(text, "[RESERVOIRS]")
    tank_ids      = _node_ids_in_section(text, "[TANKS]")

    geographic = _looks_geographic(coords)

    pipes_fc = _fc([
        _line_feature(coords, n1, n2, {
            "id": pid, "type": "pipe",
            **{k: v for k, v in pipe_attrs.get(pid, {}).items()},
        })
        for pid, (n1, n2) in pipe_topo.items()
    ])
    pumps_fc = _fc([
        _line_feature(coords, n1, n2, {"id": pid, "type": "pump"})
        for pid, (n1, n2) in pump_topo.items()
    ])
    valves_fc = _fc([
        _line_feature(coords, n1, n2, {
            "id": vid, "type": "valve",
            **{k: v for k, v in valve_attrs.get(vid, {}).items()},
        })
        for vid, (n1, n2) in valve_topo.items()
    ])
    reservoirs_fc = _fc([
        _point_feature(coords, rid, {"id": rid, "type": "reservoir"})
        for rid in reservoir_ids
    ])
    tanks_fc = _fc([
        _point_feature(coords, tid, {"id": tid, "type": "tank"})
        for tid in tank_ids
    ])

    total_pipe_length_m = sum(
        a["length_m"] for a in pipe_attrs.values() if a.get("length_m") is not None
    )
    total_nodes = len(junction_ids) + len(reservoir_ids) + len(tank_ids)
    nodes_with_coords = sum(
        1 for nid in (junction_ids + reservoir_ids + tank_ids) if nid in coords
    )

    return {
        "filename": _safe_filename(filename),
        "has_geographic_coords": geographic,
        "pipes": pipes_fc,
        "pumps": pumps_fc,
        "valves": valves_fc,
        "reservoirs": reservoirs_fc,
        "tanks": tanks_fc,
        "stats": {
            "junction_count":        len(junction_ids),
            "reservoir_count":       len(reservoir_ids),
            "tank_count":            len(tank_ids),
            "pipe_count":            len(pipe_topo),
            "pump_count":            len(pump_topo),
            "valve_count":           len(valve_topo),
            "total_pipe_length_m":   round(total_pipe_length_m, 1),
            "total_nodes":           total_nodes,
            "nodes_with_coords":     nodes_with_coords,
            "coords_coverage_pct":   round(100 * nodes_with_coords / total_nodes, 1) if total_nodes else 0.0,
        },
    }


@router.post("/upload", response_model=InpUploadResult, status_code=201)
async def upload_inp(
    file: UploadFile = File(..., description="A real EPANET .inp file"),
    _:    str        = Depends(require_api_key),
):
    """
    Upload a real EPANET .inp file and register it for direct simulation.

    Unlike `/files/upload` (GIS `.gpkg`/`.zip`), nothing is reconstructed —
    the file you upload is exactly what gets simulated via
    `POST /inp/{filename}/simulate`, including any pumps, real PRV/PSV/FCV
    valves, non-cylindrical tanks, multi-category demand patterns, and
    [CONTROLS]/[RULES] logic it already contains.
    """
    settings = get_settings()
    os.makedirs(settings.inp_dir, exist_ok=True)

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > _MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Maximum is {_MAX_UPLOAD_MB} MB.",
        )

    original_name = file.filename or "upload.inp"
    if os.path.splitext(original_name)[1].lower() != ".inp":
        raise HTTPException(status_code=422, detail="Only .inp files are accepted here.")

    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        # EPANET .inp files are occasionally saved as latin-1 by older tools.
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(422, "File is not readable text — is this really a .inp file?")

    info = _inspect_inp(text)

    has_node_section = any(sec in info["sections"] for sec in _REQUIRED_ANY_OF_NODE_SECTIONS)
    has_link_section = any(sec in info["sections"] for sec in _REQUIRED_ANY_OF_LINK_SECTIONS)
    if not (has_node_section and has_link_section):
        raise HTTPException(
            422,
            "This doesn't look like a valid EPANET .inp file — no recognisable "
            "node section ([JUNCTIONS]/[RESERVOIRS]/[TANKS]) and link section "
            "([PIPES]/[PUMPS]/[VALVES]) were found.",
        )
    if info["junction_count"] == 0:
        raise HTTPException(422, "The .inp file has no [JUNCTIONS] entries — nothing to simulate.")

    out_name = _safe_filename(original_name)
    dest = _inp_path(out_name)
    if os.path.exists(dest):
        shutil.move(dest, dest + ".bak")
        logger.info("Backed up existing %s → %s.bak", out_name, dest)

    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(text)
    size_kb = round(os.path.getsize(dest) / 1024, 1)

    logger.info(
        "Uploaded .inp OK: %s  junctions=%d pipes=%d pumps=%d valves=%d tanks=%d "
        "controls=%s rules=%s",
        out_name, info["junction_count"], info["pipe_count"], info["pump_count"],
        info["valve_count"], info["tank_count"], info["has_controls"], info["has_rules"],
    )

    return InpUploadResult(
        filename       = out_name,
        size_kb        = size_kb,
        sections       = info["sections"],
        junction_count = info["junction_count"],
        pipe_count     = info["pipe_count"],
        pump_count     = info["pump_count"],
        valve_count    = info["valve_count"],
        tank_count     = info["tank_count"],
        has_controls   = info["has_controls"],
        has_rules      = info["has_rules"],
        message        = f"Uploaded and registered as '{out_name}'. "
                          f"Simulate it with POST /inp/{out_name}/simulate.",
    )


@router.get("", response_model=List[InpUploadResult])
async def list_inp_files(_: str = Depends(require_api_key)):
    """List previously uploaded .inp files with a quick content summary."""
    settings = get_settings()
    os.makedirs(settings.inp_dir, exist_ok=True)
    out: List[InpUploadResult] = []
    for fname in sorted(os.listdir(settings.inp_dir)):
        if not fname.lower().endswith(".inp"):
            continue
        path = os.path.join(settings.inp_dir, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        info = _inspect_inp(text)
        out.append(InpUploadResult(
            filename       = fname,
            size_kb        = round(os.path.getsize(path) / 1024, 1),
            sections       = info["sections"],
            junction_count = info["junction_count"],
            pipe_count     = info["pipe_count"],
            pump_count     = info["pump_count"],
            valve_count    = info["valve_count"],
            tank_count     = info["tank_count"],
            has_controls   = info["has_controls"],
            has_rules      = info["has_rules"],
            message        = "",
        ))
    return out


@router.delete("/{filename}", status_code=204)
async def delete_inp(filename: str, _: str = Depends(require_api_key)):
    path = _inp_path(_safe_filename(filename))
    if not os.path.isfile(path):
        raise HTTPException(404, f"'{filename}' not found.")
    os.remove(path)


@router.post("/{filename}/simulate", status_code=202)
async def simulate_inp(
    filename:   str,
    body:       InpSimulateRequest,
    background: BackgroundTasks,
    db:         AsyncSession = Depends(get_db),
    _:          str          = Depends(require_api_key),
):
    """
    Queue a simulation that runs the uploaded .inp file exactly as-is
    (pumps, real valves, controls, patterns and all) through EPyT-Flow.

    `duration_hrs`/`time_step_min` are optional overrides — omit them to
    use whatever the file's own `[TIMES]` section specifies.
    """
    safe_name = _safe_filename(filename)
    src = _inp_path(safe_name)
    if not os.path.isfile(src):
        raise HTTPException(404, f"'{filename}' not found. Upload it first via POST /inp/upload.")

    # The worker deletes whatever .inp it's handed once the run finishes
    # (see simulation_worker.py's `finally` block) — so we must hand it a
    # throwaway copy, never the persisted original, or the upload would
    # vanish after its first simulation.
    work_dir = tempfile.mkdtemp(prefix="inp_upload_")
    work_path = os.path.join(work_dir, safe_name)
    shutil.copy2(src, work_path)

    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        file_text = fh.read()
    info = _inspect_inp(file_text)
    file_duration_hrs, file_timestep_min = _parse_times_section(file_text)

    resolved_duration_hrs  = body.duration_hrs or round(file_duration_hrs)
    resolved_timestep_min  = body.time_step_min or round(file_timestep_min)

    # Resolve + validate reported leak(s) against the network before
    # queuing — a bad report fails the request with a 422, not a FAILED
    # scenario discovered later.
    resolved_leaks: list = []
    if body.scenario_type == REPORTED_LEAK:
        try:
            resolved_leaks = resolve_and_validate_reports(
                body.reported_leaks, work_path, resolved_duration_hrs * 3600
            )
        except LeakValidationError as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise HTTPException(422, str(e))

    scenario = SimScenario(
        gpkg_filename  = safe_name,   # reused as a display label — not a .gpkg here
        name           = body.name,
        description    = body.description or f"Direct .inp run — {safe_name}",
        duration_hrs   = resolved_duration_hrs,
        time_step_min  = resolved_timestep_min,
        reservoir_head = 0.0,
        demand_model          = body.demand_model,
        pda_pressure_min      = body.pda_pressure_min,
        pda_pressure_required = body.pda_pressure_required,
        pda_pressure_exponent = body.pda_pressure_exponent,
        scenario_type  = body.scenario_type,
        leakage_frac   = body.leakage_frac,
        extra_demands  = {
            "_dma_inp_path": work_path,
            "_ingest_source": "raw_inp_upload",
            "_source_filename": safe_name,
            "_pump_count": info["pump_count"],
            "_valve_count": info["valve_count"],
            "_has_controls": info["has_controls"],
            "_has_rules": info["has_rules"],
            "_leak_events": resolved_leaks,
        },
        status = "PENDING",
    )
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)

    background.add_task(run_simulation_task, scenario.id)
    logger.info(
        "Queued raw-.inp scenario %d for '%s' (scenario_type=%s%s)",
        scenario.id, safe_name, body.scenario_type,
        f", {len(resolved_leaks)} reported leak(s)" if resolved_leaks else "",
    )

    return {
        "id": scenario.id,
        "status": "PENDING",
        "source_filename": safe_name,
        "pumps_in_file": info["pump_count"],
        "valves_in_file": info["valve_count"],
        "controls_in_file": info["has_controls"],
        "rules_in_file": info["has_rules"],
        "message": f"Poll GET /simulate/{scenario.id} for status.",
    }