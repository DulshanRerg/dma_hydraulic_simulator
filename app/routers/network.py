# app/routers/network.py
"""
Network exploration endpoints.

Workflow this supports
-----------------------
1. GET  /network/{filename}/pipes   → full (or status-filtered) pipe
                                       network as GeoJSON, for the initial
                                       Leaflet display.
2. POST /network/{filename}/select  → resolve a map selection (clicked
                                       pipe ids / point+radius / drawn
                                       polygon) into connected sub-networks.
                                       Nearby endpoints are auto-snapped
                                       within `snap_tolerance_m` to fix
                                       small digitising gaps; every
                                       resulting connected component is
                                       returned (with its own node list)
                                       so the frontend can let the user
                                       pick one and then click a node to
                                       use as the reservoir.
3. POST /simulate                   → existing endpoint (see
                                       routers/simulation.py), extended to
                                       accept `pipe_ids` + `reservoir_lat`/
                                       `reservoir_lon` so it simulates only
                                       the chosen component.
"""

import logging
from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator

from app.core.auth import require_api_key
from app.core.exceptions import InvalidGpkgError
from app.services import network_subset as ns

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/network", tags=["network"])


# ── GET pipes ────────────────────────────────────────────────────────────────

@router.get("/{filename}/pipes")
def get_pipes_geojson(
    filename:    str,
    pipe_status: Optional[str] = Query(None, description="Filter e.g. OPERATIONAL. Omit for all statuses."),
    limit:       int           = Query(20_000, ge=1, le=50_000),
    _:           str           = Depends(require_api_key),
):
    """
    The full pipe network as a GeoJSON FeatureCollection of LineStrings —
    load this straight into Leaflet for the base map.
    """
    raw_pipes = ns.fetch_raw_pipes(filename, pipe_status=pipe_status)
    truncated = len(raw_pipes) > limit
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in p.full_coords]},
            "properties": {
                "fid":      p.fid,
                "status":   p.status,
                "material": p.material,
                "diam_mm":  p.diam_mm,
                "length_m": p.length_m,
            },
        }
        for p in raw_pipes[:limit]
    ]
    return {"type": "FeatureCollection", "features": features, "total_pipes": len(raw_pipes), "truncated": truncated}


# ── POST select ──────────────────────────────────────────────────────────────

class SelectionRequest(BaseModel):
    selection_type:   Literal["pipes", "point", "polygon"] = Field(
        ..., description="'pipes' = explicit fids clicked on the map, "
                          "'point' = point + radius_m buffer, "
                          "'polygon' = a drawn ring"
    )
    pipe_ids:         Optional[List[int]]                  = None
    lat:              Optional[float]                      = None
    lon:              Optional[float]                      = None
    radius_m:         float                                = Field(50.0, gt=0, le=5000)
    polygon:          Optional[List[Tuple[float, float]]]  = Field(
        None, description="[[lon, lat], ...] — at least 3 points, not required to be closed"
    )
    pipe_status:      Optional[str] = Field(None, description="Optional status filter applied before selection")
    snap_tolerance_m: float         = Field(
        2.0, ge=0, le=50,
        description="Pipe endpoints within this distance (metres) are merged into one node"
    )

    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.selection_type == "pipes" and not self.pipe_ids:
            raise ValueError("pipe_ids is required when selection_type='pipes'")
        if self.selection_type == "point" and (self.lat is None or self.lon is None):
            raise ValueError("lat and lon are required when selection_type='point'")
        if self.selection_type == "polygon" and (not self.polygon or len(self.polygon) < 3):
            raise ValueError("polygon needs at least 3 [lon, lat] points when selection_type='polygon'")
        return self


class ComponentResponse(BaseModel):
    component_id:   int
    pipe_count:      int
    node_count:      int
    total_length_m:  float
    bbox:            Tuple[float, float, float, float]
    pipe_ids:        List[int]
    nodes:           List[dict]
    geojson:         dict


class SelectionResponse(BaseModel):
    selection_type:          str
    matched_pipe_count:       int
    dropped_self_loop_pipes:  int
    snap_tolerance_m:         float
    components:               List[ComponentResponse]


@router.post("/{filename}/select", response_model=SelectionResponse)
def select_network(
    filename: str,
    body:     SelectionRequest,
    _:        str = Depends(require_api_key),
):
    """
    Turn a map selection into one or more connected sub-networks.

    Endpoints within `snap_tolerance_m` of each other are merged into a
    single node before connectivity is computed — this repairs the small
    digitising gaps that are common in hand-maintained GIS layers. If the
    selection still splits into several disconnected pieces, **every**
    piece is returned (largest first) so the frontend can show them all
    and let the user choose which one to simulate.
    """
    all_pipes = ns.fetch_raw_pipes(filename, pipe_status=body.pipe_status)

    if body.selection_type == "pipes":
        matched = ns.filter_by_ids(all_pipes, body.pipe_ids)
    elif body.selection_type == "point":
        matched = ns.filter_by_point(all_pipes, body.lat, body.lon, body.radius_m)
    else:
        matched = ns.filter_by_polygon(all_pipes, body.polygon)

    if not matched:
        raise InvalidGpkgError("No pipes matched this selection.")
    if len(matched) > ns.MAX_SELECTION_PIPES:
        raise InvalidGpkgError(
            f"Selection matched {len(matched)} pipes — narrow it down "
            f"to {ns.MAX_SELECTION_PIPES} or fewer."
        )

    graph = ns.snap_and_build_graph(matched, tolerance_m=body.snap_tolerance_m)
    components = ns.connected_components(graph)
    if not components:
        raise InvalidGpkgError("Selection produced no connected pipes (all self-loops?).")

    raw_by_uid = {p.pipe_uid: p for p in matched}
    comp_responses = [
        ComponentResponse(
            component_id   = c.component_id,
            pipe_count      = len(c.pipe_fids),
            node_count      = len(c.node_keys),
            total_length_m  = c.total_length_m,
            bbox            = c.bbox,
            pipe_ids        = c.pipe_fids,
            nodes           = ns.component_nodes(c, graph),
            geojson         = ns.component_geojson(c, graph, raw_by_uid),
        )
        for c in components
    ]

    logger.info(
        "select(%s) on %s: %d pipes matched -> %d component(s), %d self-loop(s) dropped",
        body.selection_type, filename, len(matched), len(components), graph.dropped_self_loops,
    )

    return SelectionResponse(
        selection_type          = body.selection_type,
        matched_pipe_count       = len(matched),
        dropped_self_loop_pipes  = graph.dropped_self_loops,
        snap_tolerance_m         = body.snap_tolerance_m,
        components               = comp_responses,
    )