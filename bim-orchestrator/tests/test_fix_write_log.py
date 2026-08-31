"""v1.5-R7 (R1-Stage 1): tests for the fix-write-log instrumentation.

Covers report_trace.detect_fix_interactions (pure) and DesignAgent's
accumulation of state["fix_write_log"] across two design passes, plus the
verification report's "Fix interactions observed" render.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.audit_report import render_audit_report
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.rules_schema import Rule, RuleSet
from bim_orchestrator.report_trace import detect_fix_interactions
from bim_orchestrator.state import Finding, OrchestratorState
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient


# ---- pure detect_fix_interactions ------------------------------------------


def test_two_rules_same_slot_two_iterations_is_one_interaction():
    log = [
        {"iteration": 0, "rule_id": "r.format", "write_eid": 500,
         "parameter": "Fire Rating", "old": "1 HR", "new": "60 MIN"},
        {"iteration": 1, "rule_id": "r.inherit", "write_eid": 500,
         "parameter": "Fire Rating", "old": "60 MIN", "new": "2 HR"},
    ]
    out = detect_fix_interactions(log)
    assert len(out) == 1
    interaction = out[0]
    assert interaction["write_eid"] == 500
    assert interaction["parameter"] == "Fire Rating"
    assert interaction["rules"] == ["r.format", "r.inherit"]
    assert interaction["iterations"] == [0, 1]
    assert interaction["values"] == [("1 HR", "60 MIN"), ("60 MIN", "2 HR")]


def test_single_rule_single_write_is_empty():
    log = [
        {"iteration": 0, "rule_id": "r.format", "write_eid": 500,
         "parameter": "Fire Rating", "old": None, "new": "1 HR"},
    ]
    assert detect_fix_interactions(log) == []


def test_empty_log_is_empty():
    assert detect_fix_interactions([]) == []
    assert detect_fix_interactions(None) == []


def test_different_slots_are_independent():
    """Two single writes to DIFFERENT (write_eid, parameter) slots must not
    be conflated into one interaction."""
    log = [
        {"iteration": 0, "rule_id": "r.a", "write_eid": 1, "parameter": "P1",
         "old": None, "new": "x"},
        {"iteration": 0, "rule_id": "r.b", "write_eid": 2, "parameter": "P2",
         "old": None, "new": "y"},
    ]
    assert detect_fix_interactions(log) == []


# ---- DesignAgent accumulation ----------------------------------------------


def _autonomy(tmp_path) -> AutonomyPolicy:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "documents": {"create_issue": "auto"},
                    "parameters": {"set_value": "auto"},
                },
                "severity_rules": {"missing_required_param": "severity_medium"},
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _rule() -> Rule:
    return Rule.model_validate(
        {
            "id": "room.department.required",
            "parameter": "Department",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "description": "Department required",
            "fixability": "auto",
            "remediation": {
                "action": "set_parameter",
                "target_parameter": "Department",
                "new_value_strategy": "inferred",
            },
            "autofill": {"strategy": "infer_from_room_name", "fallback": "General"},
        }
    )


def _ruleset(*rules: Rule) -> RuleSet:
    return RuleSet(scenario="test", target_category="Rooms", rules=list(rules))


def _finding(*, element_id: str, suggested: Any = "Residential") -> Finding:
    return Finding(
        rule_id="room.department.required",
        element_id=element_id,
        parameter="Department",
        severity_tag="missing_required_param",
        severity="severity_medium",  # type: ignore[arg-type]
        message="test",
        suggested_value=suggested,
        citation=None,
    )


def _room(eid: str, name: str, **params: Any) -> dict[str, Any]:
    return {"id": eid, "name": name, "category": "Rooms", "params": params}


def _state(findings: list[Finding], elements: list[dict[str, Any]],
           *, iteration: int = 0, fix_write_log: list[dict[str, Any]] | None = None
           ) -> OrchestratorState:
    st: OrchestratorState = {
        "project_id": "b.test",
        "iteration": iteration,
        "max_iterations": 3,
        "elements": elements,
        "findings": findings,
        "proposed_fixes": [],
        "status": "designing",
        "error": None,
    }
    if fix_write_log is not None:
        st["fix_write_log"] = fix_write_log  # type: ignore[typeddict-item]
    return st


@pytest.mark.asyncio
async def test_fix_write_log_accumulates_across_two_design_passes(tmp_path):
    forma = MockFormaMCPClient(elements=[])
    revit = MockRevitMCPClient()
    agent = DesignAgent(
        mcp=forma, autonomy=_autonomy(tmp_path), project_id="b.test",
        max_issues=10, rule_filter=None, revit_mcp=revit, rules=_ruleset(_rule()),
    )
    state0 = _state(
        [_finding(element_id="829712")], [_room("829712", "Studio 1", Department="")],
        iteration=0,
    )
    state1 = await agent.run(state0)
    log1 = state1["fix_write_log"]
    assert len(log1) == 1
    assert log1[0]["rule_id"] == "room.department.required"
    assert log1[0]["parameter"] == "Department"
    assert log1[0]["new"] == "Residential"

    # Second pass: a DIFFERENT element, fed the PRIOR log — must not be replaced.
    state2_in = _state(
        [_finding(element_id="830966")], [_room("830966", "Studio 2", Department="")],
        iteration=1, fix_write_log=log1,
    )
    state2 = await agent.run(state2_in)
    log2 = state2["fix_write_log"]
    assert len(log2) == 2
    assert log2[0] == log1[0]  # original entry preserved, not overwritten
    assert log2[1]["iteration"] == 1


@pytest.mark.asyncio
async def test_fix_write_log_records_two_rules_writing_same_element(tmp_path):
    """Two different rules writing the SAME (write_eid, parameter) across two
    passes is exactly the scenario detect_fix_interactions must catch."""
    rule_a = Rule.model_validate({
        "id": "r.a", "parameter": "Department", "requirement": "present_and_nonempty",
        "severity_tag": "missing_required_param", "description": "a",
        "fixability": "auto",
        "remediation": {"action": "set_parameter", "target_parameter": "Department",
                         "new_value_strategy": "inferred"},
        "autofill": {"strategy": "infer_from_room_name", "fallback": "General"},
    })
    rule_b = Rule.model_validate({
        "id": "r.b", "parameter": "Department", "requirement": "present_and_nonempty",
        "severity_tag": "missing_required_param", "description": "b",
        "fixability": "auto",
        "remediation": {"action": "set_parameter", "target_parameter": "Department",
                         "new_value_strategy": "inferred"},
        "autofill": {"strategy": "infer_from_room_name", "fallback": "General"},
    })
    forma = MockFormaMCPClient(elements=[])
    revit = MockRevitMCPClient()
    agent = DesignAgent(
        mcp=forma, autonomy=_autonomy(tmp_path), project_id="b.test",
        max_issues=10, rule_filter=None, revit_mcp=revit,
        rules=_ruleset(rule_a, rule_b),
    )
    room = _room("829712", "Studio 1", Department="")
    f_a = Finding(
        rule_id="r.a", element_id="829712", parameter="Department",
        severity_tag="missing_required_param", severity="severity_medium",  # type: ignore[arg-type]
        message="a", suggested_value="Residential", citation=None,
    )
    state0 = _state([f_a], [room], iteration=0)
    state1 = await agent.run(state0)

    f_b = Finding(
        rule_id="r.b", element_id="829712", parameter="Department",
        severity_tag="missing_required_param", severity="severity_medium",  # type: ignore[arg-type]
        message="b", suggested_value="Commercial", citation=None,
    )
    state2_in = {**state1, "findings": [f_b], "iteration": 1}
    state2 = await agent.run(state2_in)

    interactions = detect_fix_interactions(state2["fix_write_log"])
    assert len(interactions) == 1
    assert interactions[0]["rules"] == ["r.a", "r.b"]
    assert interactions[0]["parameter"] == "Department"


# ---- report rendering -------------------------------------------------------


def test_report_renders_none_observed_when_log_empty():
    state = {
        "project_id": "p", "iteration": 1, "status": "converged",
        "check_trace": [], "proposed_fixes": [], "fix_write_log": [],
    }
    md = render_audit_report(state, run_id="r1")
    assert "Fix interactions observed (0)" in md
    assert "_none observed_" in md


def test_report_renders_interaction_table():
    state = {
        "project_id": "p", "iteration": 2, "status": "converged",
        "check_trace": [], "proposed_fixes": [],
        "fix_write_log": [
            {"iteration": 0, "rule_id": "r.a", "write_eid": 500,
             "parameter": "Fire Rating", "old": "1 HR", "new": "60 MIN"},
            {"iteration": 1, "rule_id": "r.b", "write_eid": 500,
             "parameter": "Fire Rating", "old": "60 MIN", "new": "2 HR"},
        ],
    }
    md = render_audit_report(state, run_id="r2")
    assert "Fix interactions observed (1)" in md
    assert "`r.a`" in md and "`r.b`" in md
    assert "Fire Rating" in md
