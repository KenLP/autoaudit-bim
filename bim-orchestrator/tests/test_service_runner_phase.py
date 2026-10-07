"""The live-progress phase map knows the AI layer (2026-08-31).

Agent events were unmapped and rendered as phase "run" — whose UI label is
"Finished" — so the panel's live log announced the end of the audit while the
diagnostic agent was still writing. The three agents and the socket's own
events share one phase.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.service.runner import _phase_for


@pytest.mark.parametrize(
    "event",
    [
        "remediation_llm.proposed",
        "diagnostic_agent.start",
        "diagnostic_agent.done",
        "supervisor.directive",
        "supervisor.skipped_prefilter_continue",
        "llm.plugin_missing",
        "llm.budget_exceeded",
    ],
)
def test_agent_events_map_to_the_llm_phase(event: str) -> None:
    assert _phase_for(event) == "llm"


@pytest.mark.parametrize(
    ("event", "phase"),
    [
        ("qc_agent.done", "run"),          # unmapped prefix stays "run" (unchanged)
        ("design_agent.revit.autonomy", "run"),
        ("design.proposal_parked", "design"),
        ("route.converged", "design"),
        ("run_recorder.finished", "record"),
    ],
)
def test_existing_mapping_is_untouched(event: str, phase: str) -> None:
    assert _phase_for(event) == phase
