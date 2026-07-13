# app/services/inp_parser.py
"""
Shared plain-text .inp parsing helpers.

Used instead of querying the EPANET/EPyT-Flow API for node coordinates
and pipe topology. `epanet_api.getNodeCoordinates(idx)` does not do what
a per-node index call implies: per EPyT's own documentation, passing an
integer selects a *dimension* ("1" = all X coordinates, "2" = all Y
coordinates across the whole network), not a specific node's [x, y]
pair. Called once per node — as simulation_service originally did — this
silently fails for almost every node (confirmed in production: "0 with
coords" out of 369), and appears to also leave the underlying EPANET
instance unable to reliably serve the *next* loop's link/node lookups,
which were failing too as a result.

Parsing the .inp text directly sidesteps all of that: it's deterministic,
doesn't depend on guessing EPyT's per-call argument semantics, and this
app already writes an exact-precision [COORDINATES]/[PIPES] section
itself (see dma_builder.build_dma_inp) — nothing is lost by reading it
back instead of round-tripping through the API.
"""

from typing import Dict, Tuple


def parse_inp_coordinates(inp_path: str) -> Dict[str, Tuple[float, float]]:
    """Parse [COORDINATES] → {node_id: (lon, lat)}."""
    coords: Dict[str, Tuple[float, float]] = {}
    in_sec = False
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
                try:
                    coords[parts[0]] = (float(parts[1]), float(parts[2]))
                except ValueError:
                    continue
    return coords


def parse_inp_pipe_topology(inp_path: str) -> Dict[str, Tuple[str, str]]:
    """Parse [PIPES] → {pipe_id: (node1_id, node2_id)}."""
    topo: Dict[str, Tuple[str, str]] = {}
    in_sec = False
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
            if len(parts) >= 3:
                topo[parts[0]] = (parts[1], parts[2])
    return topo
