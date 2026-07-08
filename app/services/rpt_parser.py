# app/services/rpt_parser.py
"""
EPANET .rpt (report) file parser.

Extracts the information EPyT-Flow writes to the report file after
a simulation run:

  • Hydraulic Flow Balance  — Total Inflow, Consumer Demand, Total Outflow,
                              Storage Flow
  • Hydraulic Status events — pipe open/closed transitions, tank level events
  • Node pressure table     — per-node pressure at each reporting timestep
  • Pipe flow table         — per-pipe flow at each reporting timestep

These are used by the DMA leakage endpoint to report EPANET's own NRW
figure (derived from its flow balance) rather than a re-computed estimate.

The .rpt path is derived from the .inp path by appending ".rpt".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class FlowBalance:
    """EPANET Hydraulic Flow Balance (m³/h)."""
    total_inflow_m3h:    float = 0.0
    consumer_demand_m3h: float = 0.0
    demand_deficit_m3h:  float = 0.0
    leakage_flow_m3h:    float = 0.0
    total_outflow_m3h:   float = 0.0
    storage_flow_m3h:    float = 0.0   # negative = tanks draining INTO network
    flow_ratio:          float = 1.0

    @property
    def nrw_m3h(self) -> float:
        """
        Hydraulic NRW = Inflow − Consumer Demand.
        Positive means unaccounted-for losses; includes storage changes.
        """
        return max(0.0, self.total_inflow_m3h - self.consumer_demand_m3h)

    @property
    def nrw_pct(self) -> float:
        if self.total_inflow_m3h <= 0:
            return 0.0
        return round(100.0 * self.nrw_m3h / self.total_inflow_m3h, 1)


@dataclass
class StatusEvent:
    time_hms:  str    # e.g. "0:03:01"
    message:   str    # full text of the status line


@dataclass
class RptData:
    flow_balance:   Optional[FlowBalance]       = None
    status_events:  List[StatusEvent]           = field(default_factory=list)
    # brief quality mass balance
    mass_ratio:     Optional[float]             = None
    balanced:       bool                        = True
    warnings:       List[str]                   = field(default_factory=list)


# ── parser ────────────────────────────────────────────────────────────────────

_FB_PATTERNS: List[Tuple[str, str]] = [
    (r"Total Inflow\s*:\s*([\d.]+)",    "total_inflow_m3h"),
    (r"Consumer Demand\s*:\s*([\d.]+)", "consumer_demand_m3h"),
    (r"Demand Deficit\s*:\s*([\d.]+)",  "demand_deficit_m3h"),
    (r"Leakage Flow\s*:\s*([\d.]+)",    "leakage_flow_m3h"),
    (r"Total Outflow\s*:\s*([\d.]+)",   "total_outflow_m3h"),
    (r"Storage Flow\s*:\s*(-?[\d.]+)",  "storage_flow_m3h"),
    (r"Flow Ratio\s*:\s*([\d.]+)",      "flow_ratio"),
]

_STATUS_RE = re.compile(r"^\s+(\d+:\d+:\d+):\s+(.+)$")


def persisted_report_path(scenario_id: int) -> str:
    """
    Deterministic path where the worker copies a scenario's .rpt file so it
    survives temp-dir cleanup. Shared by the worker (writer) and the
    /simulate/{id}/report* endpoints (readers) so there is a single
    source of truth for the naming convention.
    """
    from app.core.config import get_settings  # local import avoids a cycle
    settings = get_settings()
    return os.path.join(settings.reports_dir, f"scenario_{scenario_id}.rpt")


def parse_rpt(inp_path: str) -> Optional[RptData]:
    """
    Parse the EPANET .rpt file associated with `inp_path`
    (i.e. `inp_path + ".rpt"`).

    Returns None if the .rpt file does not exist or cannot be read.
    """
    return parse_rpt_file(inp_path + ".rpt")


def parse_rpt_file(rpt_path: str) -> Optional[RptData]:
    """
    Parse an EPANET .rpt file given its exact path (as opposed to
    `parse_rpt`, which derives the path from an .inp path). Used to read
    back a persisted report copy, e.g. one saved by the simulation worker
    into `settings.reports_dir`.

    Returns None if the .rpt file does not exist or cannot be read.
    """
    if not os.path.isfile(rpt_path):
        return None

    try:
        text = open(rpt_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None

    result = RptData()

    # ── flow balance ──────────────────────────────────────────────────────────
    if "Hydraulic Flow Balance" in text:
        fb = FlowBalance()
        for pattern, attr in _FB_PATTERNS:
            m = re.search(pattern, text)
            if m:
                setattr(fb, attr, float(m.group(1)))
        result.flow_balance = fb

    # ── status events ─────────────────────────────────────────────────────────
    for line in text.splitlines():
        m = _STATUS_RE.match(line)
        if m:
            result.status_events.append(StatusEvent(
                time_hms = m.group(1).strip(),
                message  = m.group(2).strip(),
            ))

    # ── quality mass ratio ────────────────────────────────────────────────────
    mr = re.search(r"Mass Ratio\s*:\s*([\d.eE+\-]+)", text)
    if mr:
        result.mass_ratio = float(mr.group(1))

    # ── convergence check ─────────────────────────────────────────────────────
    if "unbalanced" in text.lower():
        result.balanced = False
        result.warnings.append("EPANET reported unbalanced hydraulics — check reservoir heads and pipe connectivity.")

    # ── warning lines ─────────────────────────────────────────────────────────
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("warning"):
            result.warnings.append(line.strip())

    return result


def rpt_nrw_summary(rpt: RptData) -> dict:
    """
    Return a NRW summary dict from a parsed RptData.
    Falls back to zeros if the flow balance section is absent.
    """
    if not rpt or not rpt.flow_balance:
        return {
            "source": "estimated",
            "total_inflow_m3h":    0.0,
            "consumer_demand_m3h": 0.0,
            "storage_flow_m3h":    0.0,
            "nrw_m3h":             0.0,
            "nrw_pct":             0.0,
        }
    fb = rpt.flow_balance
    return {
        "source":              "epanet_rpt",
        "total_inflow_m3h":    round(fb.total_inflow_m3h,    3),
        "consumer_demand_m3h": round(fb.consumer_demand_m3h, 3),
        "storage_flow_m3h":    round(fb.storage_flow_m3h,    3),
        "total_outflow_m3h":   round(fb.total_outflow_m3h,   3),
        "flow_ratio":          round(fb.flow_ratio,           4),
        "nrw_m3h":             round(fb.nrw_m3h,             3),
        "nrw_pct":             round(fb.nrw_pct,             1),
        "balanced":            rpt.balanced,
        "status_events":       len(rpt.status_events),
    }
