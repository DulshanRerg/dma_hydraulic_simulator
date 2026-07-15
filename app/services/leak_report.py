# app/services/leak_report.py
"""
Reported-leak handling: the structured input the main water-management
system sends in, validated against the actual network, resolved into an
EPyT-Flow leak event, and — after the simulation runs — turned into a
service-impact summary and isolation recommendations.

This intentionally does NOT do true valve-segment isolation tracing
(computing the minimal set of valves whose closure disconnects exactly the
leaking segment). That requires knowing real valve positions along pipes,
which most .inp files don't encode explicitly and which isn't something
this module can determine on its own. What it does instead — and says
plainly that it's doing — is report the pipes topologically adjacent to
the leak location: a reasonable starting point for a field crew, not a
substitute for a real isolation trace against as-built valve records.
"""

import math
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from app.services.inp_parser import parse_inp_coordinates, parse_inp_pipe_topology

SEVERITIES = ("minor", "moderate", "major", "critical")


class ReportedLeak(BaseModel):
    """
    A specific leak report from the main system. At least one of
    `pipe_id`, `node_id`, or `(lat, lon)` must be given to locate it, and
    at least one of `leak_diameter_m` / `leak_area_m2` to size it.
    """
    pipe_id:  Optional[str] = Field(None, description="EPANET pipe ID the leak is on")
    node_id:  Optional[str] = Field(None, description="EPANET node ID nearest the leak")
    lat:      Optional[float] = Field(None, description="Leak latitude, if pipe_id/node_id unknown")
    lon:      Optional[float] = Field(None, description="Leak longitude, if pipe_id/node_id unknown")

    leak_diameter_m: Optional[float] = Field(None, gt=0, description="Orifice diameter (m)")
    leak_area_m2:    Optional[float] = Field(None, gt=0, description="Orifice area (m²) — alternative to diameter")

    severity:    Optional[str] = Field(
        None, description=f"Informational only, one of: {', '.join(SEVERITIES)}",
    )
    reported_at: Optional[datetime] = Field(None, description="When the leak was reported")

    start_time_s: int = Field(0,     ge=0, description="Seconds into the simulation the leak becomes active")
    end_time_s:   Optional[int] = Field(
        None, ge=0,
        description="Seconds into the simulation the leak ends. Omit to run for the full duration.",
    )

    @model_validator(mode="after")
    def _check_location_and_size(self):
        if not (self.pipe_id or self.node_id or (self.lat is not None and self.lon is not None)):
            raise ValueError("Provide at least one of: pipe_id, node_id, or (lat and lon).")
        if not (self.leak_diameter_m or self.leak_area_m2):
            raise ValueError("Provide at least one of: leak_diameter_m or leak_area_m2.")
        if self.severity and self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got '{self.severity}'.")
        return self


class LeakValidationError(ValueError):
    """Raised when a ReportedLeak can't be resolved against the network."""


def _area_to_diameter_m(area_m2: float) -> float:
    return math.sqrt(4.0 * area_m2 / math.pi)


def _nearest_node(lat: float, lon: float, coords: Dict[str, Tuple[float, float]]) -> Optional[str]:
    best, best_d = None, float("inf")
    for nid, (nx, ny) in coords.items():
        d = math.hypot(nx - lon, ny - lat)
        if d < best_d:
            best_d, best = d, nid
    return best


def resolve_leak_report(leak: ReportedLeak, inp_path: str, duration_sec: int) -> dict:
    """
    Validate a ReportedLeak against the network in `inp_path` and resolve
    it to an EPyT-Flow leak event dict: {node_id, diameter, start_time,
    end_time}, plus the target pipe_id (if any) for isolation
    recommendations later.

    Raises LeakValidationError if the report can't be located in this
    network — e.g. an unknown pipe_id/node_id, or lat/lon supplied but the
    network has no [COORDINATES] to snap against.
    """
    coords    = parse_inp_coordinates(inp_path)
    pipe_topo = parse_inp_pipe_topology(inp_path)

    target_pipe_id: Optional[str] = None
    node_id: Optional[str]

    if leak.node_id:
        if leak.node_id not in coords:
            raise LeakValidationError(
                f"node_id '{leak.node_id}' was not found in this network's [COORDINATES]/nodes."
            )
        node_id = leak.node_id
        for pid, (n1, n2) in pipe_topo.items():
            if node_id in (n1, n2):
                target_pipe_id = pid
                break

    elif leak.pipe_id:
        if leak.pipe_id not in pipe_topo:
            raise LeakValidationError(f"pipe_id '{leak.pipe_id}' was not found in this network's [PIPES].")
        target_pipe_id = leak.pipe_id
        n1, n2 = pipe_topo[leak.pipe_id]
        # EPANET leaks are node-based, not link-based — inject at the pipe's
        # start node. Documented simplification: for a long pipe the true
        # leak position along its length isn't distinguishable at this
        # node-based resolution.
        node_id = n1

    else:
        if not coords:
            raise LeakValidationError(
                "This network has no [COORDINATES] to snap a lat/lon leak report against — "
                "provide pipe_id or node_id instead."
            )
        node_id = _nearest_node(leak.lat, leak.lon, coords)
        if node_id is None:
            raise LeakValidationError("Could not snap the given lat/lon to any network node.")
        for pid, (n1, n2) in pipe_topo.items():
            if node_id in (n1, n2):
                target_pipe_id = pid
                break

    diameter = leak.leak_diameter_m or _area_to_diameter_m(leak.leak_area_m2)

    return {
        "node_id":        node_id,
        "diameter":       diameter,
        "start_time":     leak.start_time_s,
        "end_time":       leak.end_time_s if leak.end_time_s is not None else duration_sec,
        "target_pipe_id": target_pipe_id,
        "severity":       leak.severity,
        "reported_at":    leak.reported_at.isoformat() if leak.reported_at else None,
    }


def resolve_and_validate_reports(
    leaks: List[ReportedLeak], inp_path: str, duration_sec: int
) -> List[dict]:
    """
    Resolve+validate a batch of ReportedLeak entries against the network
    in `inp_path`. Used by the scenario-creation endpoints (dma.py,
    inp.py) so a bad report is rejected with a 422 *before* a scenario is
    queued, rather than surfacing as a worker-time FAILED scenario.

    Raises LeakValidationError on the first invalid entry, prefixed with
    its index in the list so the caller can point back at the offending
    item in the request body.
    """
    resolved: List[dict] = []
    for i, leak in enumerate(leaks):
        try:
            resolved.append(resolve_leak_report(leak, inp_path, duration_sec))
        except LeakValidationError as exc:
            raise LeakValidationError(f"reported_leaks[{i}]: {exc}") from exc
    return resolved


def recommend_isolation(resolved_leak: dict, inp_path: str) -> dict:
    """
    Topological isolation candidates: pipes directly incident to the leak
    pipe's two endpoints (or, for a node-located leak with no pipe_id, all
    pipes incident to that node).

    This is NOT a true valve-segment trace — it doesn't know where real
    isolation valves actually sit. It's the minimal set of adjacent pipes
    a field crew would start from; closing them isolates the immediate
    vicinity of the leak assuming valves exist near each pipe's ends,
    which should be verified in the field / against valve GIS records
    before acting on it.
    """
    pipe_topo = parse_inp_pipe_topology(inp_path)
    incident: Dict[str, List[str]] = defaultdict(list)
    for pid, (n1, n2) in pipe_topo.items():
        incident[n1].append(pid)
        incident[n2].append(pid)

    target_pipe_id = resolved_leak.get("target_pipe_id")
    node_id        = resolved_leak.get("node_id")

    if target_pipe_id and target_pipe_id in pipe_topo:
        n1, n2 = pipe_topo[target_pipe_id]
        candidates = set(incident.get(n1, [])) | set(incident.get(n2, []))
        candidates.discard(target_pipe_id)
        anchor_nodes = [n1, n2]
    else:
        candidates = set(incident.get(node_id, []))
        anchor_nodes = [node_id] if node_id else []

    return {
        "leak_pipe_id":            target_pipe_id,
        "leak_node_id":            node_id,
        "candidate_isolation_pipes": sorted(candidates),
        "anchor_nodes":            anchor_nodes,
        "method":                  "topological_adjacency",
        "note": (
            "Pipes topologically adjacent to the leak, not a true valve-segment "
            "isolation trace (real valve positions aren't available). Verify "
            "against field/GIS valve records before closing anything."
        ),
    }


def compute_service_impact(node_results: list, min_pressure_m: Optional[float] = None) -> dict:
    """
    Summarise how many nodes/what fraction of the network fell below the
    low-pressure threshold at any point during the run. `node_results` is
    the list of NodeResult-like objects from simulation_service's output
    (needs `.element_id`, `.pressure`, `.is_low_pressure`).
    """
    if not node_results:
        return {
            "total_nodes": 0, "affected_nodes": 0, "pct_nodes_affected": 0.0,
            "affected_node_ids": [], "min_pressure_m": None,
        }

    by_node: Dict[str, list] = defaultdict(list)
    for n in node_results:
        by_node[n.element_id].append(n)

    total_nodes = len(by_node)
    affected_ids = sorted({
        nid for nid, rows in by_node.items() if any(r.is_low_pressure for r in rows)
    })
    pressures = [n.pressure for n in node_results if n.pressure is not None]

    return {
        "total_nodes":         total_nodes,
        "affected_nodes":      len(affected_ids),
        "pct_nodes_affected":  round(100 * len(affected_ids) / total_nodes, 1) if total_nodes else 0.0,
        "affected_node_ids":   affected_ids[:100],  # cap payload size
        "min_pressure_m":      round(min(pressures), 2) if pressures else None,
    }