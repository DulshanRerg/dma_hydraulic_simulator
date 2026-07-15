# app/core/scenario_types.py
"""
Scenario classification for simulation runs.

This service is a hydraulic simulation engine — it should not be deciding
on its own where leaks are. Random/synthetic leak injection is a research
and testing convenience, not something a production run driven by a real
water-management system should ever get by default. `scenario_type` makes
that distinction an explicit, enforced part of the API contract rather
than an implicit side effect of a `leakage_frac > 0` default.

    baseline         Normal operating conditions. No leaks unless the
                      network's own .inp models background loss (e.g. via
                      demand inflation baked in at build time) — no
                      random/discrete leak events are ever injected.
    reported_leak     A specific leak reported by the main system: pipe ID,
                      node ID, or coordinates + diameter/area + severity +
                      timestamp. Validated against the network, injected
                      at the resolved location, then the response includes
                      service impact and isolation recommendations.
    planned_shutdown  A maintenance/isolation scenario — valves/pumps taken
                      out of service via actuator_events. No leak events.
    fire_flow         A fire-flow / high-demand test at a specific node.
                      No leak events; typically paired with actuator_events
                      or a demand override at the target node.
    research          The only mode allowed to generate synthetic/random
                      leak events (the pre-existing `leakage_frac` random
                      per-node injection). Intended for testing, training
                      data generation, and what-if exploration — never for
                      production runs a real system would act on.
"""

from typing import Final

BASELINE:         Final[str] = "baseline"
REPORTED_LEAK:     Final[str] = "reported_leak"
PLANNED_SHUTDOWN:  Final[str] = "planned_shutdown"
FIRE_FLOW:         Final[str] = "fire_flow"
RESEARCH:          Final[str] = "research"

ALL_SCENARIO_TYPES: Final[frozenset] = frozenset({
    BASELINE, REPORTED_LEAK, PLANNED_SHUTDOWN, FIRE_FLOW, RESEARCH,
})

# Only this scenario_type may use the legacy random-leak `leakage_frac`
# parameter — for every other type it must be omitted/zero.
RANDOM_LEAKS_ALLOWED_FOR: Final[frozenset] = frozenset({RESEARCH})


def validate_scenario_contract(
    scenario_type: str,
    leakage_frac: float = 0.0,
    has_reported_leaks: bool = False,
    has_explicit_leak_events: bool = False,
) -> None:
    """
    Enforce the scenario_type contract described above. Raises ValueError
    (→ the caller turns this into a 422) on any violation. Intended to be
    called from every request schema's model_validator so a bad request
    is rejected before a scenario is ever queued — not discovered later
    as a worker-time failure.

    leakage_frac / has_explicit_leak_events
        Cover both routes to synthetic leak injection in this codebase:
        the random-per-node `leakage_frac` knob, and an explicit list of
        EPyT-Flow leakage_events (link_id + diameter). Both are
        synthetic/what-if tools and so are restricted the same way —
        only scenario_type='research' may use either.
    has_reported_leaks
        Whether the request included one or more entries in
        `reported_leaks` (the validated pipe_id/node_id/coords +
        diameter/severity/timestamp leak reports from the main system).
    """
    if scenario_type not in ALL_SCENARIO_TYPES:
        raise ValueError(
            f"scenario_type must be one of {sorted(ALL_SCENARIO_TYPES)}, got '{scenario_type}'."
        )

    if (leakage_frac or has_explicit_leak_events) and scenario_type not in RANDOM_LEAKS_ALLOWED_FOR:
        raise ValueError(
            "leakage_frac / leakage_events (synthetic or random leak generation) "
            f"are only allowed when scenario_type='{RESEARCH}' "
            f"(got scenario_type='{scenario_type}')."
        )

    if scenario_type == REPORTED_LEAK and not has_reported_leaks:
        raise ValueError(
            f"scenario_type='{REPORTED_LEAK}' requires at least one entry in reported_leaks."
        )

    if scenario_type != REPORTED_LEAK and has_reported_leaks:
        raise ValueError(
            f"reported_leaks was provided but scenario_type is '{scenario_type}' — "
            f"set scenario_type='{REPORTED_LEAK}' to inject them."
        )