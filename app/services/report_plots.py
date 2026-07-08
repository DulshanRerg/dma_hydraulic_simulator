# app/services/report_plots.py
"""
Matplotlib chart generation for simulation results.

Renders the same style of chart the DUWASA team already produces by hand
from the /simulate/{id}/nodes and /simulate/{id}/pipes tables:

  • Node pressure vs time, one line per selected node   (plot_node_pressure)
  • Pipe/link flow rate vs time, one line per selected link (plot_pipe_flow)

Both return PNG image bytes so they can be served directly from a FastAPI
endpoint with `Response(content=png_bytes, media_type="image/png")` or
embedded in an HTML report as a base64 data URI.
"""

from __future__ import annotations

import io
from typing import Iterable, List, Optional, Sequence

import matplotlib

matplotlib.use("Agg")  # headless — no display backend needed on the server
import matplotlib.pyplot as plt

from app.models.simulation import SimResult

# A small fixed palette so repeated runs render consistent colours
# (matplotlib's default cycle is fine too, but this keeps it explicit).
_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
           "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]


def _time_step_label(time_step_min: Optional[int]) -> str:
    if time_step_min:
        return f"Time ({time_step_min}min steps)"
    return "Time step"


def plot_node_pressure(
    results: Sequence[SimResult],
    node_ids: Optional[Iterable[str]] = None,
    time_step_min: Optional[int] = None,
) -> bytes:
    """
    Build a "Pressure in meter" vs time-step line chart, one line per node,
    matching the style of the reference plot (markers + legend by node id).

    `results` should be the full list of node SimResult rows for a scenario
    (all time steps). If `node_ids` is given, only those element_ids are
    plotted; otherwise every distinct node in `results` is plotted.
    """
    by_node: dict[str, List[SimResult]] = {}
    for r in results:
        if r.element_type != "node":
            continue
        if node_ids and r.element_id not in node_ids:
            continue
        by_node.setdefault(r.element_id, []).append(r)

    if not by_node:
        raise ValueError("No matching node results to plot.")

    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=100)
    for i, (node_id, rows) in enumerate(sorted(by_node.items())):
        rows.sort(key=lambda r: r.time_step)
        xs = [r.time_step for r in rows]
        ys = [r.pressure if r.pressure is not None else 0.0 for r in rows]
        ax.plot(
            xs, ys,
            marker="o", markersize=3, linewidth=1.4,
            color=_COLORS[i % len(_COLORS)],
            label=f"Node {node_id}",
        )

    ax.set_xlabel(_time_step_label(time_step_min))
    ax.set_ylabel("Pressure in meter")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    return _fig_to_png(fig)


def plot_pipe_flow(
    results: Sequence[SimResult],
    link_ids: Optional[Iterable[str]] = None,
    time_step_min: Optional[int] = None,
) -> bytes:
    """
    Build a "Flow rate in cubicmeter/hr" vs time-step line chart, one line
    per pipe/link, matching the style of the reference plot.

    Stored flow_rate values are in m3/s (EPyT-Flow native units); this
    converts to m3/h to match the reference chart's y-axis label/scale.
    """
    by_link: dict[str, List[SimResult]] = {}
    for r in results:
        if r.element_type != "pipe":
            continue
        if link_ids and r.element_id not in link_ids:
            continue
        by_link.setdefault(r.element_id, []).append(r)

    if not by_link:
        raise ValueError("No matching pipe results to plot.")

    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=100)
    for i, (link_id, rows) in enumerate(sorted(by_link.items())):
        rows.sort(key=lambda r: r.time_step)
        xs = [r.time_step for r in rows]
        ys = [
            (r.flow_rate * 3600.0) if r.flow_rate is not None else 0.0
            for r in rows
        ]
        ax.plot(
            xs, ys,
            marker="o", markersize=3, linewidth=1.4,
            color=_COLORS[i % len(_COLORS)],
            label=f"Link {link_id}",
        )

    ax.set_xlabel(_time_step_label(time_step_min))
    ax.set_ylabel("Flow rate in cubicmeter/hr")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    return _fig_to_png(fig)


def _fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
