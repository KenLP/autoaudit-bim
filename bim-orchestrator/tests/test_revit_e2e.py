"""End-to-end integration: Revit + Forma path B (Phase 2 W6 D4).

Wires the real graph (build_graph) with MockRevitMCPClient as the source
and MockFormaMCPClient as the Phase 1 fallback. Loads the production
rules.room_compliance.yaml so any regression in the YAML schema fails
here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.graph import build_graph
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import OrchestratorState
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOM_RULES = REPO_ROOT / "config" / "rules.room_compliance.yaml"
AUTONOMY = REPO_ROOT / "config" / "autonomy.yaml"


def _initial_state(max_iterations: int = 2) -> OrchestratorState:
    return {
        "project_id": "b.test-project",
        "iteration": 0,
        "max_iterations": max_iterations,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }


@pytest.mark.asyncio
async def test_room_compliance_full_graph_with_mocks() -> None:
    """Production rules YAML drives a mock-MCP run end-to-end.

    SAMPLE_REVIT_ROOMS contains:
      * Studio Unit 203 (residential, 55 m², Number=203)
      * Storage P04 (5.32 m² — violates other_min)
      * Corridor 201 (55 m², passes)
      * Bedroom 999 (8.5 m² residential — violates BOTH residential_min and other_min)
      * Duplicate Studio 203 (Number=203 — collides with id 829712)

    Expected (10 findings total):
      * 1× residential_min (Bedroom 999, 8.5 m² < 10)
      * 2× other_min (Storage P04 5.32, Bedroom 999 8.5)
      * 2× number_unique (both Number=203 rooms)
      * 1× height_min (Bedroom 999, 8.5 ft = 2.59 m < 2.6)
      * 4× department_required (Bedroom 999 has Department="Residential";
                                 the other 4 are blank)
    """
    revit = MockRevitMCPClient()
    forma = MockFormaMCPClient(
        elements=[],
        subtypes=[
            {"id": "subtype-quality", "title": "Quality", "type_id": "type-quality",
             "type_title": "Quality", "is_active": True},
        ],
    )
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=ROOM_RULES, autonomy=autonomy)
    query = RevitQueryAgent(mcp=revit, rules=qc.rules)
    design = DesignAgent(
        mcp=forma,
        autonomy=autonomy,
        project_id="b.test-project",
        max_issues=20,
        rule_filter=None,  # all rules
        revit_mcp=revit,
        rules=qc.rules,
    )
    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=2))

    # v1 task BB: department.required is present_and_nonempty → missing_data bucket.
    # Combine all 3 buckets to keep the by_rule coverage assertion intact.
    all_findings = (
        list(result.get("findings", []))
        + list(result.get("manual_review_items", []))
        + list(result.get("missing_data_items", []))
    )
    by_rule: dict[str, int] = {}
    for f in all_findings:
        by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1

    # Locked expectations on the SAMPLE_REVIT_ROOMS fixture
    assert by_rule.get("room.area.residential_min", 0) == 1
    assert by_rule.get("room.area.other_min", 0) == 2
    assert by_rule.get("room.number.unique", 0) == 2
    assert by_rule.get("room.height.clear_min", 0) == 1
    assert by_rule.get("room.department.required", 0) == 4
    assert sum(by_rule.values()) == 10
    # BB split verification
    assert result["outcomes_summary"]["non_compliant"] == 6
    assert result["outcomes_summary"]["missing_data"] == 4


@pytest.mark.asyncio
async def test_dry_run_only_blocks_all_commits() -> None:
    revit = MockRevitMCPClient()
    forma = MockFormaMCPClient(
        elements=[],
        subtypes=[
            {"id": "subtype-quality", "title": "Quality", "type_id": "type-quality",
             "type_title": "Quality", "is_active": True},
        ],
    )
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=ROOM_RULES, autonomy=autonomy)
    query = RevitQueryAgent(mcp=revit, rules=qc.rules)
    design = DesignAgent(
        mcp=forma,
        autonomy=autonomy,
        project_id="b.test-project",
        max_issues=20,
        rule_filter=None,
        dry_run_only=True,  # ← preview only
        revit_mcp=revit,
        rules=qc.rules,
    )
    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state())

    # No Revit commits
    revit_writes = [c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is False]
    assert revit_writes == []
    # No Forma issue commits
    forma_executes = [
        args for name, args in forma.calls
        if name == "create_issue" and args.get("dry_run") is False
    ]
    assert forma_executes == []
    # All proposed fixes are non-executed
    assert all(not f["executed"] for f in result["proposed_fixes"])


@pytest.mark.asyncio
async def test_path_b_routes_separately_from_path_a() -> None:
    """Verify path B previews run for auto rules and path A creates ACC
    Issues for manual rules. With production autonomy.yaml, path B writes
    are parked (severity_medium → approve) — that's the safe default.
    Path A documents.create_issue is `auto` so issues commit."""
    revit = MockRevitMCPClient()
    forma = MockFormaMCPClient(
        elements=[],
        subtypes=[
            {"id": "subtype-quality", "title": "Quality", "type_id": "type-quality",
             "type_title": "Quality", "is_active": True},
        ],
    )
    autonomy = AutonomyPolicy.load(AUTONOMY)
    qc = QCAgent(rules_path=ROOM_RULES, autonomy=autonomy)
    query = RevitQueryAgent(mcp=revit, rules=qc.rules)
    design = DesignAgent(
        mcp=forma,
        autonomy=autonomy,
        project_id="b.test-project",
        max_issues=20,
        rule_filter=None,
        revit_mcp=revit,
        rules=qc.rules,
    )
    app = build_graph(query, qc, design)
    await app.ainvoke(_initial_state())

    # Path B exercised: at least one revit dry-run preview happened
    revit_previews = [
        c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is True
    ]
    assert len(revit_previews) >= 1

    # Path B commits parked: no revit dry_run=False writes from auto rules
    # (autonomy.yaml's parameters.set_value.severity_medium = approve)
    revit_commits = [
        c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is False
    ]
    assert revit_commits == []

    # Path A commits: ACC Issues created for manual findings
    forma_executes = [
        args for name, args in forma.calls
        if name == "create_issue" and args.get("dry_run") is False
    ]
    assert len(forma_executes) >= 1


