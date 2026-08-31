"""Tests for Path A grouping — ONE ACC Issue per (rule, status) (v1.4-K18).

Manual-review findings are grouped by PROBLEM, like the auto-fix proposal: every
element that fails the same rule with the same status lands in ONE ACC Issue
listing all of them (id | name | current). "100 elements violate rule X" → 1
issue, not 100. Two different rules → two issues (one per problem), even on the
same element.
"""

from __future__ import annotations

from typing import Any

import pytest

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.qc import Rule, RuleSet
from bim_orchestrator.state import Finding, OrchestratorState
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient  # noqa: F401
from tests.test_design_agent_path_b import _autonomy


def _manual_rule(rule_id: str, parameter: str, *, severity_tag: str) -> Rule:
    return Rule.model_validate({
        "id": rule_id,
        "parameter": parameter,
        "requirement": "present_and_nonempty",
        "severity_tag": severity_tag,
        "description": f"{rule_id} desc",
        "fixability": "manual",
        "remediation": {"action": "create_acc_issue"},
        "autofill": {"strategy": "none"},
    })


def _finding(rule_id: str, element_id: str, parameter: str, *,
             severity: str = "severity_medium",
             severity_tag: str = "missing_required_param",
             status: str = "non_compliant") -> Finding:
    return Finding(
        rule_id=rule_id, element_id=element_id, parameter=parameter,
        severity_tag=severity_tag, severity=severity,  # type: ignore[arg-type]
        message=f"{rule_id} on {element_id}", suggested_value=None, citation=None,
        status=status,  # type: ignore[typeddict-item]
    )


def _el(eid: str, name: str, category: str = "Doors", **params: Any) -> dict[str, Any]:
    return {"id": eid, "name": name, "category": category, "params": params}


def _state(findings, elements, *, geometry=None, iteration=0) -> OrchestratorState:
    s: OrchestratorState = {
        "project_id": "b.test", "iteration": iteration, "max_iterations": 1,
        "elements": elements, "findings": findings, "proposed_fixes": [],
        "status": "designing", "error": None,
    }
    if geometry is not None:
        s["geometry_findings"] = geometry  # type: ignore[typeddict-item]
    return s


def _agent(tmp_path, forma, *, rules, revit=None, max_issues=10) -> DesignAgent:
    return DesignAgent(
        mcp=forma, autonomy=_autonomy(tmp_path), project_id="b.test",
        max_issues=max_issues, rule_filter=None, revit_mcp=revit, rules=rules,
    )


def _executes(forma) -> list[dict[str, Any]]:
    return [a for n, a in forma.calls if n == "create_issue" and a.get("dry_run") is False]


@pytest.mark.asyncio
class TestRuleGrouping:
    async def test_same_rule_many_elements_one_issue(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.fire", "Fire Rating", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        findings = [
            _finding("r.fire", "777", "Fire Rating"),
            _finding("r.fire", "888", "Fire Rating"),
            _finding("r.fire", "999", "Fire Rating"),
        ]
        state = await agent.run(_state(
            findings, [_el("777", "A"), _el("888", "B"), _el("999", "C")]))
        # 3 elements, ONE rule → ONE issue listing all three
        assert len(_executes(forma)) == 1
        fixes = state["proposed_fixes"]
        assert len(fixes) == 1
        assert fixes[0]["finding_id"] == "rulegroup::r.fire::non_compliant"
        body = _executes(forma)[0]["description"]
        assert "`777`" in body and "`888`" in body and "`999`" in body
        assert "3 non-compliant" in _executes(forma)[0]["title"]

    async def test_two_rules_same_element_two_issues(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.fire", "Fire Rating", severity_tag="missing_required_param"),
            _manual_rule("r.mark", "Mark", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        findings = [
            _finding("r.fire", "777", "Fire Rating"),
            _finding("r.mark", "777", "Mark"),
        ]
        state = await agent.run(_state(findings, [_el("777", "Door A")]))
        # different PROBLEMS → one issue each (grouped by rule, not element)
        assert len(_executes(forma)) == 2
        ids = {f["finding_id"] for f in state["proposed_fixes"]}
        assert ids == {"rulegroup::r.fire::non_compliant",
                       "rulegroup::r.mark::non_compliant"}

    async def test_missing_data_and_non_compliant_split(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.fire", "Fire Rating", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        findings = [
            _finding("r.fire", "777", "Fire Rating", status="non_compliant"),
            _finding("r.fire", "888", "Fire Rating", status="missing_data"),
        ]
        state = await agent.run(_state(findings, [_el("777", "A"), _el("888", "B")]))
        # same rule, different STATUS → two issues (grouped by status too)
        assert len(_executes(forma)) == 2


@pytest.mark.asyncio
class TestGeometryCoordination:
    async def test_param_and_geometry_grouped_by_rule(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.fire", "Fire Rating", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        param = [_finding("r.fire", "777", "Fire Rating")]
        geo = [_finding("geo.clearance", "777", "clearance",
                        severity="severity_high", severity_tag="geometric_violation")]
        await agent.run(_state(param, [_el("777", "Door A")], geometry=geo))
        # two distinct problems on one element → two issues (one per rule)
        assert len(_executes(forma)) == 2
        titles = " ".join(e["title"] for e in _executes(forma))
        assert "r.fire" in titles and "geo.clearance" in titles

    async def test_geometry_only_folded_on_iteration_0(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.fire", "Fire Rating", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        param = [_finding("r.fire", "777", "Fire Rating")]
        geo = [_finding("geo.clearance", "777", "clearance",
                        severity_tag="geometric_violation")]
        # iteration=1 → geometry bucket NOT folded; only the param rule issue
        state = await agent.run(
            _state(param, [_el("777", "Door A")], geometry=geo, iteration=1)
        )
        assert len(_executes(forma)) == 1
        assert state["proposed_fixes"][0]["finding_id"] == "rulegroup::r.fire::non_compliant"

    async def test_geometry_only_ruleset_creates_issue(self, tmp_path):
        # Geometry-ONLY run: no param findings at all (an empty `findings` list,
        # as a clearance-only ruleset produces). Regression for the early-return
        # guard that bailed before the geometry fold → 0 ACC issues despite real
        # clashes. The fold must still fire on iteration 0.
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Floors", rules=[
            _manual_rule("geo.clearance", "clearance",
                         severity_tag="geometric_violation"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        geo = [
            _finding("geo.clearance", "501", "clearance",
                     severity_tag="geometric_violation"),
            _finding("geo.clearance", "502", "clearance",
                     severity_tag="geometric_violation"),
        ]
        state = await agent.run(
            _state([], [_el("501", "Floor A"), _el("502", "Floor B")], geometry=geo)
        )
        # Without the fix the early-return guard fires before the geometry fold
        # → 0 issues. With it, the 2 clearance findings fold into one ACC issue.
        assert len(_executes(forma)) == 1
        body = _executes(forma)[0]["description"]
        assert "`501`" in body and "`502`" in body

    async def test_geometry_only_iteration_1_still_converges(self, tmp_path):
        # The fold is iteration-0 only; a geometry-only state on a later iteration
        # has nothing to fold and must still early-return converged (don't widen
        # the guard past iteration 0).
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Floors", rules=[
            _manual_rule("geo.clearance", "clearance",
                         severity_tag="geometric_violation"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        geo = [_finding("geo.clearance", "501", "clearance",
                        severity_tag="geometric_violation")]
        state = await agent.run(
            _state([], [_el("501", "Floor A")], geometry=geo, iteration=1)
        )
        assert len(_executes(forma)) == 0
        assert state["status"] == "converged"

    async def test_geometry_alone_gets_own_issue(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.fire", "Fire Rating", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules)
        param = [_finding("r.fire", "777", "Fire Rating")]
        geo = [_finding("geo.clearance", "999", "clearance",
                        severity_tag="geometric_violation")]
        await agent.run(_state(param, [_el("777", "A"), _el("999", "G")], geometry=geo))
        # r.fire group (777) + geo.clearance group (999) → two issues
        assert len(_executes(forma)) == 2


@pytest.mark.asyncio
class TestIssueBudget:
    async def test_max_issues_caps_path_a_groups(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        rules = RuleSet(scenario="t", target_category="Doors", rules=[
            _manual_rule("r.a", "Fire Rating", severity_tag="missing_required_param"),
            _manual_rule("r.b", "Mark", severity_tag="missing_required_param"),
            _manual_rule("r.c", "Comments", severity_tag="missing_required_param"),
        ])
        agent = _agent(tmp_path, forma, rules=rules, max_issues=2)
        findings = [
            _finding("r.a", "1", "Fire Rating"),
            _finding("r.b", "1", "Mark"),
            _finding("r.c", "1", "Comments"),
        ]
        state = await agent.run(_state(findings, [_el("1", "A")]))
        # 3 rule-groups, budget=2 (no Path B → reserved 0) → 2 issues, 1 parked
        assert len(_executes(forma)) == 2
        parked = [f for f in state["proposed_fixes"] if not f["executed"]]
        assert len(parked) == 1
