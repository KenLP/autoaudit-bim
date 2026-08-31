"""Tests for DesignAgent path B dispatch (Phase 2 W6 D3).

Path B: when a RuleSet is provided and a finding's rule is tagged
``fixability=auto``, DesignAgent writes the parameter via Revit MCP
(dry-run preview → autonomy gate → commit → Comments tag) instead of
creating an ACC Issue.

These tests use MockFormaMCPClient + MockRevitMCPClient — no live MCP.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.qc import Rule, RuleSet
from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import Finding, OrchestratorState
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient


# ---- Fixtures --------------------------------------------------------------


def _autonomy(tmp_path, *, set_value: str = "auto") -> AutonomyPolicy:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "documents": {"create_issue": "auto"},
                    "parameters": {"set_value": set_value},
                },
                "severity_rules": {
                    "missing_required_param": "severity_medium",
                    "geometric_violation": "severity_high",
                    "duplicate_identifier": "severity_medium",
                },
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _state(findings: list[Finding], elements: list[dict[str, Any]]) -> OrchestratorState:
    return {
        "project_id": "b.test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": findings,
        "proposed_fixes": [],
        "status": "designing",
        "error": None,
    }


def _finding(
    *,
    rule_id: str,
    element_id: str,
    parameter: str,
    severity: str = "severity_medium",
    severity_tag: str = "missing_required_param",
    suggested: Any = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        element_id=element_id,
        parameter=parameter,
        severity_tag=severity_tag,
        severity=severity,  # type: ignore[arg-type]
        message=f"{rule_id} on {element_id}",
        suggested_value=suggested,
        citation=None,
    )


def _room(eid: str, name: str, **params: Any) -> dict[str, Any]:
    return {"id": eid, "name": name, "category": "Rooms", "params": params}


def _auto_rule_inferred() -> Rule:
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
                "comments_template": "Auto-filled Department='{new_value}' — BEP §1.7",
            },
            "autofill": {"strategy": "infer_from_room_name", "fallback": "General"},
        }
    )


def _manual_rule_area() -> Rule:
    return Rule.model_validate(
        {
            "id": "room.area.other_min",
            "parameter": "areaMetric",
            "requirement": "numeric_min",
            "threshold": 9.0,
            "severity_tag": "geometric_violation",
            "description": "Other rooms ≥ 9 m²",
            "fixability": "manual",
            "remediation": {
                "action": "create_acc_issue",
                "comments_template": "BEP §1.2 non-compliant: area {value} m² < 9 m²",
            },
            "autofill": {"strategy": "none"},
        }
    )


def _auto_rule_next_available() -> Rule:
    return Rule.model_validate(
        {
            "id": "room.number.unique",
            "parameter": "Number",
            "requirement": "unique_in_set",
            "severity_tag": "duplicate_identifier",
            "description": "Numbers unique",
            "fixability": "auto",
            "remediation": {
                "action": "set_parameter",
                "target_parameter": "Number",
                "new_value_strategy": "next_available",
            },
            "autofill": {"strategy": "none"},
        }
    )


def _ruleset(*rules: Rule) -> RuleSet:
    return RuleSet(scenario="test", target_category="Rooms", rules=list(rules))


def _type_rename_rule() -> Rule:
    return Rule.model_validate({
        "id": "fam.naming", "parameter": "Family Name", "requirement": "matches_regex",
        "pattern": "^ADSK_", "severity_tag": "naming_violation", "description": "naming",
        "fixability": "auto",
        "remediation": {"action": "rename_element", "target": "type"},
        "autofill": {"strategy": "normalize", "normalize_kind": "family_name"},
    })


def _family_rename_rule() -> Rule:
    return Rule.model_validate({
        "id": "fam.naming", "parameter": "Family Name", "requirement": "matches_regex",
        "pattern": "^ADSK_", "severity_tag": "naming_violation", "description": "naming",
        "fixability": "auto",
        "remediation": {"action": "rename_element", "target": "family"},
        "autofill": {"strategy": "normalize", "normalize_kind": "family_name"},
    })


@pytest.mark.asyncio
async def test_target_family_resolves_family_id(tmp_path):
    """v1.4-K17: target=family renames the FAMILY (Family Name) — resolve its id
    from list_families by name, NOT the type id."""
    from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
    revit = MockRevitMCPClient(families=[
        {"id": 179716, "name": "M_Chair-Breuer", "category": "Furniture"},
        {"id": 235381, "name": "Chair-Viper", "category": "Furniture"},
    ])
    rule = _family_rename_rule()
    agent = DesignAgent(
        mcp=MockFormaMCPClient(elements=[]), autonomy=_autonomy(tmp_path),
        project_id="p", max_issues=10, rule_filter=None,
        revit_mcp=revit, rules=_ruleset(rule),
    )
    await agent._ensure_family_map()
    assert agent._family_id_by_name["M_Chair-Breuer"] == 179716
    # write target = the FAMILY id (179716), resolved from the Family Name value
    el = {"id": "180296", "category": "Furniture",
          "params": {"Family Name": "M_Chair-Breuer", "_type_id": 179679}}
    f = _finding(rule_id="fam.naming", element_id="180296", parameter="Family Name")
    assert agent._resolve_write_eid(f, el, rule) == 179716   # family, NOT _type_id
    # unknown family name → None → routes to Path A
    el2 = {"id": "9", "params": {"Family Name": "Nonexistent"}}
    assert agent._resolve_write_eid(f, el2, rule) is None


def test_dedup_by_write_target_collapses_shared_type(tmp_path):
    """v1.4-K17: many instances of one family/type collapse to ONE write BEFORE
    the quota, so max_issues counts unique writes (not per-instance findings)."""
    from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
    agent = DesignAgent(
        mcp=MockFormaMCPClient(elements=[]), autonomy=_autonomy(tmp_path),
        project_id="p", max_issues=2, rule_filter=None,
        revit_mcp=MockRevitMCPClient(), rules=_ruleset(_type_rename_rule()),
    )
    els = [
        {"id": "1", "category": "Furniture", "params": {"Family Name": "M_Chair", "_type_id": 500}},
        {"id": "2", "category": "Furniture", "params": {"Family Name": "M_Chair", "_type_id": 500}},
        {"id": "3", "category": "Furniture", "params": {"Family Name": "M_Chair", "_type_id": 500}},
        {"id": "4", "category": "Furniture", "params": {"Family Name": "M_Desk", "_type_id": 600}},
    ]
    ebi = {e["id"]: e for e in els}
    fs = [_finding(rule_id="fam.naming", element_id=e["id"], parameter="Family Name")
          for e in els]
    out = agent._dedup_by_write_target(fs, ebi)
    # 4 instances → 2 unique type writes (500 ×3 collapsed, 600 ×1)
    assert len(out) == 2
    rule = agent._rules_by_id["fam.naming"]
    targets = {agent._resolve_write_eid(f, ebi[f["element_id"]], rule) for f in out}
    assert targets == {500, 600}


def _auto_target_rule(parameter: str, *, rid: str = "auto.rule") -> Rule:
    """v1.4-K19: a Path-B rule whose write target is left to the engine."""
    return Rule.model_validate({
        "id": rid, "parameter": parameter, "requirement": "matches_regex",
        "pattern": "^X", "severity_tag": "naming_violation", "description": "auto",
        "fixability": "auto",
        "remediation": {"action": "set_parameter", "target": "auto"},
        "autofill": {"strategy": "normalize", "normalize_kind": "family_name"},
    })


def _design_for(rule: Rule, tmp_path, *, revit=None) -> DesignAgent:
    return DesignAgent(
        mcp=MockFormaMCPClient(elements=[]), autonomy=_autonomy(tmp_path),
        project_id="p", max_issues=10, rule_filter=None,
        revit_mcp=revit or MockRevitMCPClient(), rules=_ruleset(rule),
    )


class TestEffectiveRemediation:
    """v1.4-K19: target=="auto" resolves (action, target) per element."""

    def test_family_name_resolves_to_family_rename(self, tmp_path) -> None:
        rule = _auto_target_rule("Family Name")
        agent = _design_for(rule, tmp_path)
        el = {"params": {"Family Name": "X_Chair"}}
        assert agent._effective_remediation(rule, el) == ("rename_element", "family")

    def test_type_name_resolves_to_type_rename(self, tmp_path) -> None:
        rule = _auto_target_rule("Type Name")
        agent = _design_for(rule, tmp_path)
        el = {"params": {"Type Name": "X-1"}}
        assert agent._effective_remediation(rule, el) == ("rename_element", "type")

    def test_type_carried_param_resolves_to_type(self, tmp_path) -> None:
        rule = _auto_target_rule("Fire Rating")
        agent = _design_for(rule, tmp_path)
        # the query mirrors a Type-side value under "type.<param>"
        el = {"params": {"Fire Rating": "2 HR", "type.Fire Rating": "2 HR"}}
        assert agent._effective_remediation(rule, el) == ("set_parameter", "type")

    def test_instance_only_param_resolves_to_instance(self, tmp_path) -> None:
        rule = _auto_target_rule("Mark")
        agent = _design_for(rule, tmp_path)
        el = {"params": {"Mark": "A1"}}  # no type.Mark mirror
        assert agent._effective_remediation(rule, el) == ("set_parameter", "instance")

    def test_explicit_target_is_honoured_verbatim(self, tmp_path) -> None:
        rule = _type_rename_rule()  # action=rename_element, target=type
        agent = _design_for(rule, tmp_path)
        el = {"params": {"Family Name": "X"}}
        assert agent._effective_remediation(rule, el) == ("rename_element", "type")


@pytest.mark.asyncio
async def test_auto_family_name_resolves_family_id(tmp_path):
    """v1.4-K19: an auto rule on Family Name fetches the family map and resolves
    the FAMILY element id — same outcome as an explicit target=family rule."""
    revit = MockRevitMCPClient(families=[
        {"id": 179716, "name": "M_Chair-Breuer", "category": "Furniture"},
    ])
    rule = _auto_target_rule("Family Name")
    agent = _design_for(rule, tmp_path, revit=revit)
    await agent._ensure_family_map()  # must fetch despite target=="auto"
    assert agent._family_id_by_name["M_Chair-Breuer"] == 179716
    el = {"id": "180296", "category": "Furniture",
          "params": {"Family Name": "M_Chair-Breuer", "_type_id": 179679}}
    f = _finding(rule_id="auto.rule", element_id="180296", parameter="Family Name")
    assert agent._resolve_write_eid(f, el, rule) == 179716   # family, NOT _type_id


def test_auto_type_param_resolves_type_id(tmp_path):
    """v1.4-K19: an auto rule on a Type-carried param writes the _type_id."""
    rule = _auto_target_rule("Fire Rating")
    agent = _design_for(rule, tmp_path)
    el = {"id": "42", "params": {"Fire Rating": "2 HR",
                                 "type.Fire Rating": "2 HR", "_type_id": 500}}
    f = _finding(rule_id="auto.rule", element_id="42", parameter="Fire Rating")
    assert agent._resolve_write_eid(f, el, rule) == 500


def test_inherit_conflict_collapses_to_max(tmp_path):
    """v1.4-K20: doors of one type hosted in walls of DIFFERENT ratings collapse
    to ONE type write whose value is the MAXIMUM, with an audit note.

    Owner decision 2026-07-25: the parameter carries the rating the CODE
    REQUIRES, not a certified product capability. A shared Type serving a
    60-minute and a 120-minute host has no single correct value — but the
    minimum leaves the 120-minute instances declaring LESS than their host
    demands, and a present/canonical rule would pass them forever. The maximum
    over-states for the lower host, which is visible and safe."""
    rule = _auto_target_rule("Fire Rating")
    agent = _design_for(rule, tmp_path)
    els = [
        {"id": "200", "params": {"type.Fire Rating": "X", "_type_id": 500}},
        {"id": "201", "params": {"type.Fire Rating": "X", "_type_id": 500}},
    ]
    ebi = {e["id"]: e for e in els}
    fs = [
        _finding(rule_id="auto.rule", element_id="200", parameter="Fire Rating",
                 suggested="2 HR"),    # 120 min
        _finding(rule_id="auto.rule", element_id="201", parameter="Fire Rating",
                 suggested="90 MIN"),  # 90 min
    ]
    out = agent._dedup_by_write_target(fs, ebi)
    assert len(out) == 1
    assert out[0]["suggested_value"] == "2 HR"              # max(120, 90)
    conflict = out[0]["host_conflict"]
    assert conflict["chosen"] == "2 HR"
    assert conflict["count"] == 2
    assert set(conflict["candidates"]) == {"2 HR", "90 MIN"}


def test_non_fire_rating_type_conflict_is_first_win(tmp_path):
    """v1.4-K20: magnitude-resolution is SCOPED to fire-rating values. A differing
    NON-fire-rating type-param conflict keeps the first finding (first-win)."""
    rule = _auto_target_rule("Some Type Param")
    agent = _design_for(rule, tmp_path)
    els = [
        {"id": "10", "params": {"type.Some Type Param": "X", "_type_id": 700}},
        {"id": "11", "params": {"type.Some Type Param": "X", "_type_id": 700}},
    ]
    ebi = {e["id"]: e for e in els}
    fs = [
        _finding(rule_id="auto.rule", element_id="10", parameter="Some Type Param",
                 suggested="Zebra"),
        _finding(rule_id="auto.rule", element_id="11", parameter="Some Type Param",
                 suggested="Apple"),
    ]
    out = agent._dedup_by_write_target(fs, ebi)
    assert len(out) == 1
    # first-win: the FIRST finding (element 10 → "Zebra"), NOT the lexical min
    assert out[0]["element_id"] == "10"
    assert out[0]["suggested_value"] == "Zebra"
    assert out[0].get("host_conflict") is None


@pytest.mark.asyncio
async def test_inherited_value_stamps_source_on_fix(tmp_path):
    """v1.4-K22: an inherited (empty→host) fix records the host value so the
    Approvals inbox + proposal body can show '⤺ host: <value>'."""
    rule = Rule.model_validate({
        "id": "doors.fr.ihn", "parameter": "Fire Rating",
        "requirement": "canonical_format", "severity_tag": "missing_required_param",
        "description": "inherit+format", "fixability": "auto",
        "autofill": {"strategy": "inherit_then_normalize",
                     "normalize_kind": "duration", "normalize_format": "{h} HR"},
        "remediation": {"action": "set_parameter", "target": "type"},
    })
    revit = MockRevitMCPClient(
        element_info={500: {"id": 500, "name": "DoorType",
                            "parameters": [{"name": "Fire Rating", "value": None}]}},
    )
    agent = _design_for(rule, tmp_path, revit=revit)
    # empty door, host wall carries "60 MINUTE"; QC already computed "1 HR"
    el = {"id": "1448343", "category": "Doors",
          "params": {"host.Fire Rating": "60 MINUTE", "_type_id": 500},
          "params_display": {}}
    f = _finding(rule_id="doors.fr.ihn", element_id="1448343",
                 parameter="Fire Rating", suggested="1 HR")
    fix, _spec = await agent._prepare_revit_fix(f, el, rule, write_eid=500)
    assert fix is not None
    assert fix["preview"]["inherited_from"] == "60 MINUTE"


def test_bare_number_type_conflict_is_first_win_not_magnitude_collapsed(tmp_path):
    """Low3: a differing BARE-NUMBER type-param conflict (e.g. "2000" vs
    "2100" — a Type Mark or count field, not a fire rating) must be
    first-win, NOT min-collapsed. Before the fix, ``parse_to_minutes``
    happily parsed a unit-less number as minutes (Revit's door-param
    convention), so ``_collapse_to_one`` wrongly treated ANY all-numeric
    type conflict as a fire-rating case and stamped a misleading
    host_conflict / max-resolution note on values that have nothing to do
    with fire rating."""
    rule = _auto_target_rule("Some Type Param")  # normalize_kind="family_name", NOT fire_rating
    agent = _design_for(rule, tmp_path)
    els = [
        {"id": "10", "params": {"type.Some Type Param": "X", "_type_id": 700}},
        {"id": "11", "params": {"type.Some Type Param": "X", "_type_id": 700}},
    ]
    ebi = {e["id"]: e for e in els}
    fs = [
        _finding(rule_id="auto.rule", element_id="10", parameter="Some Type Param",
                 suggested="2100"),
        _finding(rule_id="auto.rule", element_id="11", parameter="Some Type Param",
                 suggested="2000"),
    ]
    out = agent._dedup_by_write_target(fs, ebi)
    assert len(out) == 1
    # first-win: keeps element "10" / "2100" — NOT min("2100","2000")="2000".
    assert out[0]["element_id"] == "10"
    assert out[0]["suggested_value"] == "2100"
    assert out[0].get("host_conflict") is None


def test_declared_fire_rating_rule_bare_numbers_fall_to_first_win(tmp_path):
    """Low3/C-01 × B-4 (2026-08-16). The rule's fire-rating DECLARATION
    (compare_kind=="fire_rating") skips the per-candidate
    ``_looks_like_fire_rating`` screen — but the C-01 second belt still only
    admits candidates ``parse_to_minutes`` accepts, and B-4 made bare numbers
    unparseable (ambiguous between hours and minutes: a wall author's "2"
    means 2 HOURS). So a bare-number conflict under a declared rule now falls
    to FIRST-WIN with no host_conflict note: nothing parses → don't invent an
    ordering. (This test previously pinned the opposite — bare numbers
    magnitude-collapsing via the declaration — which was exactly the
    read-as-minutes guess B-4 removed.)"""
    rule = Rule.model_validate({
        "id": "auto.rule.declared_fr", "parameter": "Fire Rating",
        "requirement": "relation_compare", "compare_kind": "fire_rating",
        "operator": ">=", "severity_tag": "naming_violation", "description": "fr",
        "fixability": "auto",
        "remediation": {"action": "set_parameter", "target": "auto"},
        "autofill": {"strategy": "normalize", "normalize_kind": "family_name"},
    })
    agent = _design_for(rule, tmp_path)
    els = [
        {"id": "10", "params": {"type.Fire Rating": "X", "_type_id": 700}},
        {"id": "11", "params": {"type.Fire Rating": "X", "_type_id": 700}},
    ]
    ebi = {e["id"]: e for e in els}
    fs = [
        _finding(rule_id="auto.rule.declared_fr", element_id="10", parameter="Fire Rating",
                 suggested="90"),   # bare number, no unit token — unparseable since B-4
        _finding(rule_id="auto.rule.declared_fr", element_id="11", parameter="Fire Rating",
                 suggested="120"),
    ]
    out = agent._dedup_by_write_target(fs, ebi)
    assert len(out) == 1
    # First-win keeps "90" (element 10) — NOT max("90","120")="120". A max
    # here would mean we guessed both values' units.
    assert out[0]["suggested_value"] == "90"
    assert out[0].get("host_conflict") is None

    # The declaration still does its job for values that DO parse: unit-ful
    # candidates under the same declared rule MAX-collapse with the honest note.
    fs2 = [
        _finding(rule_id="auto.rule.declared_fr", element_id="10", parameter="Fire Rating",
                 suggested="90 MIN"),
        _finding(rule_id="auto.rule.declared_fr", element_id="11", parameter="Fire Rating",
                 suggested="2 HR"),
    ]
    out2 = agent._dedup_by_write_target(fs2, ebi)
    assert len(out2) == 1
    assert out2[0]["suggested_value"] == "2 HR"  # max(90, 120) minutes
    assert out2[0]["host_conflict"]["chosen"] == "2 HR"


def test_inherit_same_value_no_conflict_note(tmp_path):
    """v1.4-K20: identical inherited values collapse cleanly — no conflict note."""
    rule = _auto_target_rule("Fire Rating")
    agent = _design_for(rule, tmp_path)
    els = [
        {"id": "200", "params": {"type.Fire Rating": "X", "_type_id": 500}},
        {"id": "201", "params": {"type.Fire Rating": "X", "_type_id": 500}},
    ]
    ebi = {e["id"]: e for e in els}
    fs = [
        _finding(rule_id="auto.rule", element_id="200", parameter="Fire Rating",
                 suggested="2 HR"),
        _finding(rule_id="auto.rule", element_id="201", parameter="Fire Rating",
                 suggested="2 HR"),
    ]
    out = agent._dedup_by_write_target(fs, ebi)
    assert len(out) == 1
    assert out[0].get("host_conflict") is None


@pytest.mark.asyncio
async def test_auto_family_rule_stamps_rename_action(tmp_path):
    """v1.4-K19: an auto rule on Family Name stamps action=rename_element on the
    fix preview so the ApprovalWatcher renames (not set_parameter("Name"))."""
    revit = MockRevitMCPClient(
        families=[{"id": 179716, "name": "X chair", "category": "Furniture"}],
        element_info={179716: {"id": 179716, "name": "X chair",
                               "parameters": [{"name": "Name", "value": "X chair",
                                               "valueString": "X chair"}]}},
    )
    rule = _auto_target_rule("Family Name")
    agent = _design_for(rule, tmp_path, revit=revit)
    await agent._ensure_family_map()
    el = {"id": "1", "category": "Furniture",
          "params": {"Family Name": "X chair", "_type_id": 500},
          "params_display": {"Family Name": "X chair"}}
    f = _finding(rule_id="auto.rule", element_id="1", parameter="Family Name",
                 suggested="X_chair")
    write_eid = agent._resolve_write_eid(f, el, rule)
    fix, _spec = await agent._prepare_revit_fix(f, el, rule, write_eid=write_eid)
    assert fix is not None
    assert fix["preview"]["action"] == "rename_element"
    assert fix["preview"]["target"] == "family"
    assert fix["parameter"] == "Name"   # rename writes the Name property, not a param


@pytest.mark.asyncio
async def test_prepare_revit_fix_stashes_old_value_raw(tmp_path):
    """Low8: alongside the display-preferred ``old_value``, the preview must
    also carry ``old_value_raw`` (the raw, non-display value read straight
    from ``params``) — the ApprovalWatcher's stale re-preview compares
    against a live Revit read, which also returns raw values, not display
    strings that may differ (e.g. an ElementId resolved to a level name)."""
    revit = MockRevitMCPClient(
        element_info={
            42: {
                "id": 42, "name": "Room 101",
                "parameters": [{"name": "Department", "value": "", "valueString": ""}],
            }
        }
    )
    rule = _auto_rule_inferred()
    agent = _design_for(rule, tmp_path, revit=revit)
    el = {
        "id": "42", "category": "Rooms",
        # raw value differs from the display value (an ElementId-like raw
        # vs. a human label) so the test can tell the two fields apart.
        "params": {"Department": "RAW_LEVEL_ID_204"},
        "params_display": {"Department": "Level 2 (display)"},
    }
    f = _finding(rule_id="room.department.required", element_id="42",
                 parameter="Department", suggested="Residential")
    fix, _spec = await agent._prepare_revit_fix(f, el, rule, write_eid=42)
    assert fix is not None
    assert fix["preview"]["old_value"] == "Level 2 (display)"       # display-preferred
    assert fix["preview"]["old_value_raw"] == "RAW_LEVEL_ID_204"    # raw, for the watcher


@pytest.mark.asyncio
async def test_old_value_raw_survives_serialization_into_the_record(tmp_path):
    """The seam between the two Low8 halves.

    ``_prepare_revit_fix`` stashed ``old_value_raw`` and ``ApprovalWatcher``
    accepts either baseline — but ``_build_record_fixes`` (the ONLY producer
    of the record's ``fixes`` list) serialized only ``old_value``, so the
    watcher's ``f.get("old_value_raw")`` was always None: dead code end to
    end. For any param whose display form differs from storage, the live raw
    read then matched neither baseline, an UNCHANGED model looked stale, and
    the approved fix was held back forever. Both sides had passing tests; the
    serialization between them had none.
    """
    from bim_orchestrator.agents.design import _build_record_fixes

    revit = MockRevitMCPClient(
        element_info={
            42: {
                "id": 42, "name": "Room 101",
                "parameters": [{"name": "Department", "value": "", "valueString": ""}],
            }
        }
    )
    rule = _auto_rule_inferred()
    agent = _design_for(rule, tmp_path, revit=revit)
    el = {
        "id": "42", "category": "Rooms",
        "params": {"Department": "RAW_LEVEL_ID_204"},
        "params_display": {"Department": "Level 2 (display)"},
    }
    f = _finding(rule_id="room.department.required", element_id="42",
                 parameter="Department", suggested="Residential")
    fix, _spec = await agent._prepare_revit_fix(f, el, rule, write_eid=42)
    assert fix is not None

    record_fixes = _build_record_fixes([fix])
    assert len(record_fixes) == 1
    assert record_fixes[0]["old_value"] == "Level 2 (display)"
    assert record_fixes[0]["old_value_raw"] == "RAW_LEVEL_ID_204"


def test_dedup_keeps_findings_from_different_rules(tmp_path):
    """v1.4-K22.1: two rules writing the SAME (type, param) each KEEP their fix —
    dedup is within-rule, never cross-rule — so each rule gets its own proposal
    (was: the 2nd rule's fixes collapsed into the 1st and vanished)."""
    from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
    r1 = _auto_target_rule("Fire Rating", rid="r.format")
    r2 = _auto_target_rule("Fire Rating", rid="r.inherit")
    agent = DesignAgent(
        mcp=MockFormaMCPClient(elements=[]), autonomy=_autonomy(tmp_path),
        project_id="p", max_issues=10, rule_filter=None,
        revit_mcp=MockRevitMCPClient(), rules=_ruleset(r1, r2),
    )
    els = {"5": {"id": "5", "params": {"type.Fire Rating": "X", "_type_id": 500}}}
    fs = [
        _finding(rule_id="r.format", element_id="5", parameter="Fire Rating", suggested="1 HR"),
        _finding(rule_id="r.inherit", element_id="5", parameter="Fire Rating", suggested="1 HR"),
    ]
    out = agent._dedup_by_write_target(fs, els)
    assert len(out) == 2  # one per rule, NOT collapsed to 1
    assert {f["rule_id"] for f in out} == {"r.format", "r.inherit"}


def _state_with_missing(
    findings: list[Finding],
    missing: list[Finding],
    elements: list[dict[str, Any]],
) -> OrchestratorState:
    """State variant that also carries `missing_data_items` (v1 BB)."""
    return {  # type: ignore[typeddict-item]
        "project_id": "b.test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": findings,
        "missing_data_items": missing,
        "manual_review_items": [],
        "proposed_fixes": [],
        "status": "designing",
        "error": None,
    }


# ---- v1.1 missing_data → Path B promotion ----------------------------------


@pytest.mark.asyncio
class TestMissingDataPromotion:
    """v1.1 bridge: missing_data items with fixability=auto + autofill
    strategy are eligible for Path B Revit auto-fill, recovering the
    W6 D4 demo path that was hidden after v1 BB's 4-state classification
    routed blank Department to missing_data_items instead of findings."""

    async def test_blank_department_promoted_and_committed(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        rule = _auto_rule_inferred()  # fixability=auto + infer_from_room_name
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(rule),
        )
        # Blank Department on a known room: the post-merge code puts this
        # in missing_data_items, not findings. Without v1.1 promotion the
        # agent would never see it.
        missing = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",  # QC's autofill inference
            ),
        ]
        rooms = [_room("829712", "Studio Unit 203", Department="")]
        state = await agent.run(
            _state_with_missing(findings=[], missing=missing, elements=rooms)
        )
        # One ProposedFix should have executed (Path B) on the missing item.
        executed = [f for f in state["proposed_fixes"] if f["executed"]]
        assert len(executed) == 1, f"expected 1 commit, got {executed}"
        assert executed[0]["element_id"] == "829712"
        assert executed[0]["parameter"] == "Department"
        # Commit goes through ONE revit_batch transaction (single undo).
        batch_calls = revit.calls_to("revit_batch")
        assert len(batch_calls) == 1
        params = [s["params"] for s in batch_calls[0]["steps"]]
        assert any(
            p.get("parameterName") == "Department" and p.get("value") == "Residential"
            for p in params
        )

    async def test_batch_step_failure_not_marked_executed(self, tmp_path) -> None:
        # H2: when a batch STEP fails, its fix must NOT be marked executed=True.
        # The agent used to blanket-mark every fix executed on a truthy envelope,
        # so the proposal + ACC audit chain could claim a write that never landed.
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient(batch_fail_eids={829712})
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),
        )
        missing = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            ),
        ]
        rooms = [_room("829712", "Studio Unit 203", Department="")]
        state = await agent.run(
            _state_with_missing(findings=[], missing=missing, elements=rooms)
        )
        # The batch ran, but the failed step is NOT counted as executed …
        assert revit.calls_to("revit_batch")
        assert [f for f in state["proposed_fixes"] if f["executed"]] == []
        # … and the fix records the commit failure honestly.
        failed = [
            f for f in state["proposed_fixes"]
            if (f.get("preview") or {}).get("commit_failed")
        ]
        assert len(failed) == 1 and failed[0]["element_id"] == "829712"

    async def test_manual_missing_data_routes_path_a(self, tmp_path) -> None:
        """v1.4-K3 Layer 1: a manual-fixability missing_data item has no
        auto-fill source, so it routes to a Path A ACC Issue (not a Revit
        write) — and is no longer silently dropped."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        manual_rule = _manual_rule_area()  # fixability=manual
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(manual_rule),
        )
        missing = [
            _finding(
                rule_id="room.area.other_min",
                element_id="830966",
                parameter="areaMetric",
            ),
        ]
        rooms = [_room("830966", "Storage P04", areaMetric=None)]
        state = await agent.run(
            _state_with_missing(findings=[], missing=missing, elements=rooms)
        )
        # No Revit write (Path B) — a manual rule has no auto-fill source.
        assert revit.calls_to("revit_set_parameter") == []
        # Now routed to Path A: one ACC Issue raised for the blank field.
        assert len(state["proposed_fixes"]) == 1
        assert forma.calls_to("create_issue")  # routed to Path A (preview[+execute])

    async def test_no_autofill_strategy_routes_path_a(self, tmp_path) -> None:
        """Auto rules with autofill.strategy=none (e.g. next_available
        dedupes) have no value source, so they can't be a Path B auto-fill —
        v1.4-K3 Layer 1 routes them to a Path A ACC Issue instead."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        rule = _auto_rule_next_available()  # strategy=none
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(rule),
        )
        missing = [
            _finding(
                rule_id="room.number.unique",
                element_id="829712",
                parameter="Number",
            ),
        ]
        rooms = [_room("829712", "Studio Unit 203", Number=None)]
        state = await agent.run(
            _state_with_missing(findings=[], missing=missing, elements=rooms)
        )
        # No Revit write — strategy=none means no value source for Path B.
        assert revit.calls_to("revit_set_parameter") == []
        # v1.4-K3 Layer 1: routed to a Path A ACC Issue instead.
        assert len(state["proposed_fixes"]) == 1
        assert forma.calls_to("create_issue")  # routed to Path A (preview[+execute])

    async def test_no_revit_mcp_routes_missing_to_path_a(self, tmp_path) -> None:
        """With no Revit channel a missing_data item can't be auto-filled, so
        v1.4-K3 Layer 1 routes it to a Path A ACC Issue (Forma is wired)."""
        forma = MockFormaMCPClient(elements=[])
        rule = _auto_rule_inferred()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=None,  # ← no Revit channel
            rules=_ruleset(rule),
        )
        missing = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            ),
        ]
        rooms = [_room("829712", "Studio Unit 203", Department="")]
        state = await agent.run(
            _state_with_missing(findings=[], missing=missing, elements=rooms)
        )
        # v1.4-K3 Layer 1: no Revit channel → can't auto-fill → routed to a
        # Path A ACC Issue (one issue raised).
        assert len(state["proposed_fixes"]) == 1
        assert forma.calls_to("create_issue")  # routed to Path A (preview[+execute])


# ---- Path A backward compat ------------------------------------------------


@pytest.mark.asyncio
class TestBackwardCompat:
    async def test_no_rules_means_all_findings_route_path_a(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=None,  # ← no RuleSet → must stay path A
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        state = await agent.run(_state(findings, [_room("829712", "Studio 203")]))
        # path A → ACC Issue executes
        assert revit.calls == []
        assert len([f for f in state["proposed_fixes"] if f["executed"]]) == 1


# ---- Path B happy paths ----------------------------------------------------


@pytest.mark.asyncio
class TestPathBExecution:
    async def test_auto_rule_commits_param_and_tags_comments(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio Unit 203", Department="")])
        )
        # No ACC Issue creation for path B
        assert forma.calls_to("create_issue") == []
        # Only the dry-run preview is a direct set_parameter now; the commit
        # (Department + Comments) is ONE revit_batch transaction.
        set_calls = revit.calls_to("revit_set_parameter")
        assert len(set_calls) == 1
        assert set_calls[0]["dryRun"] is True
        assert set_calls[0]["parameterName"] == "Department"
        batch_calls = revit.calls_to("revit_batch")
        assert len(batch_calls) == 1
        steps = batch_calls[0]["steps"]
        assert steps[0]["params"]["parameterName"] == "Department"
        assert steps[0]["params"]["value"] == "Residential"
        assert steps[1]["params"]["parameterName"] == "Comments"
        assert "Residential" in steps[1]["params"]["value"]

        # And the fix is recorded as executed
        fixes = state["proposed_fixes"]
        assert len(fixes) == 1
        assert fixes[0]["executed"] is True
        assert fixes[0]["parameter"] == "Department"

    async def test_dry_run_only_skips_commit(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            dry_run_only=True,  # ← preview only
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Department="")])
        )
        set_calls = revit.calls_to("revit_set_parameter")
        # Only the dry-run preview, no commit
        assert len(set_calls) == 1
        assert set_calls[0]["dryRun"] is True
        assert state["proposed_fixes"][0]["executed"] is False

    async def test_autonomy_approve_parks_without_commit(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Department="")])
        )
        set_calls = revit.calls_to("revit_set_parameter")
        # Only the dry-run preview, no commit / no Comments
        assert len(set_calls) == 1
        fix = state["proposed_fixes"][0]
        assert fix["executed"] is False
        assert fix["autonomy"] == "approve"

    async def test_next_available_strategy_proposes_with_suffix(self, tmp_path) -> None:
        """Day 4 wires the strategy: DesignAgent re-queries list_rooms to
        gather existing Numbers, then appends 'A','B',... until unique.

        F-02 (catalog audit, 2026-08-01): the VALUE derivation is unchanged —
        what changed is where it lands. This test used to run with
        ``set_value="auto"`` and assert a COMMIT, which is exactly the write the
        capability catalog promised could never happen unattended ("always as an
        approve-gated proposal, never a silent write"). The renumber now demotes
        to ``approve`` regardless of policy, so the same "203A" is *proposed*,
        not written. Kept at ``set_value="auto"`` deliberately: with a policy
        that already says approve, this test could not tell the two behaviours
        apart.
        """
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_next_available()),
            # Loop 2 wired, so the demoted fix actually PARKS a proposal issue
            # and its rendered body can be asserted below.
            approvals_dir=tmp_path / "approvals",
        )
        findings = [
            _finding(
                rule_id="room.number.unique",
                element_id="829712",
                parameter="Number",
                severity_tag="duplicate_identifier",
                suggested="203",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Number="203")])
        )
        set_calls = revit.calls_to("revit_set_parameter")
        # list_rooms fetched once for siblings cache
        assert len(revit.calls_to("revit_list_rooms")) == 1
        # First suffix not already in the fixture (which has "203" twice)
        # → expect "203A", derived exactly as before.
        number_previews = [c for c in set_calls if c["parameterName"] == "Number"]
        assert len(number_previews) == 1
        assert number_previews[0]["dryRun"] is True
        assert number_previews[0]["value"] == "203A"
        # ...and it stays a preview: nothing commits, whatever autonomy.yaml says.
        assert [c for c in revit.calls_to("revit_batch") if not c.get("dryRun")] == []
        fix = state["proposed_fixes"][0]
        assert fix["autonomy"] == "approve"
        assert fix["executed"] is False
        # Assert the RENDERED proposal, not just the decision dict (K20's
        # lesson: the min→max reducer flip shipped a body still saying
        # "minimum"). A human approves what this text says.
        body = forma.calls_to("create_issue")[0]["description"]
        assert "203A" in body
        assert fix["new_value"] == "203A"

    async def test_next_available_skips_when_suffix_already_used(
        self, tmp_path
    ) -> None:
        """If 203A is already taken, advance to 203B."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        # Add a third sibling that occupies "203A"
        revit.rooms.append(
            {"id": 999003, "name": "Other", "number": "203A",
             "levelName": "L9", "areaMetric": 50.0}
        )
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_next_available()),
        )
        findings = [
            _finding(
                rule_id="room.number.unique",
                element_id="829712",
                parameter="Number",
                severity_tag="duplicate_identifier",
            )
        ]
        await agent.run(
            _state(findings, [_room("829712", "Studio 203", Number="203")])
        )
        primary = next(
            c for c in revit.calls_to("revit_set_parameter")
            if c["parameterName"] == "Number"
        )
        assert primary["value"] == "203B"

    async def test_next_available_reserves_candidate_across_two_duplicates(
        self, tmp_path
    ) -> None:
        """M4: TWO elements sharing the same duplicate base Number ("101")
        must get DIFFERENT suggested values in the same run. Before the fix,
        ``_next_available`` never reserved its chosen candidate into the
        shared sibling cache, so both elements independently computed
        "existing" from the same starting set and both proposed "101A" —
        still colliding after the fix was applied."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_next_available()),
        )
        findings = [
            _finding(
                rule_id="room.number.unique", element_id="1001",
                parameter="Number", severity_tag="duplicate_identifier",
            ),
            _finding(
                rule_id="room.number.unique", element_id="1002",
                parameter="Number", severity_tag="duplicate_identifier",
            ),
        ]
        elements = [
            _room("1001", "Room 101 A", Number="101"),
            _room("1002", "Room 101 B", Number="101"),
        ]
        await agent.run(_state(findings, elements))
        number_previews = [
            c for c in revit.calls_to("revit_set_parameter")
            if c["parameterName"] == "Number"
        ]
        proposed_values = {c["value"] for c in number_previews}
        # Both must have gotten a suggestion, and they must be DISTINCT —
        # the bug produced {"101A"} (both collapsing onto the same value).
        assert len(number_previews) == 2
        assert proposed_values == {"101A", "101B"}

    async def test_next_available_parks_when_no_revit_mcp(
        self, tmp_path
    ) -> None:
        """Without a Revit client, next_available can't query siblings →
        the strategy returns None and the fix parks."""
        forma = MockFormaMCPClient(elements=[])
        # rules provided but no revit_mcp → _partition routes everything path A
        # so the strategy never runs. That's the contract.
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=None,
            rules=_ruleset(_auto_rule_next_available()),
        )
        findings = [
            _finding(
                rule_id="room.number.unique",
                element_id="829712",
                parameter="Number",
                severity_tag="duplicate_identifier",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Number="203")])
        )
        # Routed to path A → an ACC Issue was created
        assert len(forma.calls_to("create_issue")) >= 1
        assert state["proposed_fixes"][0]["executed"] is True

    async def test_non_integer_element_id_routes_to_path_a(self, tmp_path) -> None:
        # C2: a non-integer / unresolvable element id must NEVER be written to
        # Revit, and must NOT become a parked, un-previewed approve-gated
        # proposal (the old behaviour). The real violation routes to Path A
        # (an ACC issue) instead.
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="urn:not-a-revit-id",  # invalid for Revit path
                parameter="Department",
                suggested="Residential",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("urn:not-a-revit-id", "X")])
        )
        # Never wrote to Revit with a bad id …
        assert revit.calls_to("revit_set_parameter") == []
        # … and it routed to Path A (an ACC issue) instead of a parked Path-B write.
        assert forma.calls_to("create_issue")


# ---- Mixed routing ---------------------------------------------------------


@pytest.mark.asyncio
class TestMixedRouting:
    async def test_manual_and_auto_findings_route_separately(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        rules = _ruleset(_manual_rule_area(), _auto_rule_inferred())
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=rules,
        )
        findings = [
            # Path B target (auto)
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            ),
            # Path A target (manual / geometric)
            _finding(
                rule_id="room.area.other_min",
                element_id="830966",
                parameter="areaMetric",
                severity="severity_high",
                severity_tag="geometric_violation",
            ),
        ]
        state = await agent.run(
            _state(
                findings,
                [
                    _room("829712", "Studio 203", Department=""),
                    _room("830966", "Storage P04", areaMetric=5.32),
                ],
            )
        )
        # Auto finding → preview (set_parameter dry-run) + commit in revit_batch.
        revit_calls = revit.calls_to("revit_set_parameter")
        assert any(c["parameterName"] == "Department" for c in revit_calls)
        batch_steps = revit.calls_to("revit_batch")[0]["steps"]
        assert any(s["params"].get("parameterName") == "Department" for s in batch_steps)

        # Manual finding → ACC Issue created (dry-run + execute)
        issues = forma.calls_to("create_issue")
        # At least preview + execute on the manual finding
        assert any(args.get("dry_run") is True for args in issues)
        assert any(args.get("dry_run") is False for args in issues)

        # Both fixes recorded
        assert len(state["proposed_fixes"]) == 2

    async def test_unknown_rule_falls_back_to_path_a(self, tmp_path) -> None:
        """If a finding's rule_id isn't in the loaded RuleSet, default to
        Phase 1 path A (create ACC Issue) — never silently drop."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),  # only knows one rule
        )
        findings = [
            _finding(
                rule_id="some.unknown.rule",
                element_id="999",
                parameter="X",
                suggested="anything",
            )
        ]
        state = await agent.run(_state(findings, [_room("999", "X")]))
        # Unknown rule → path A (ACC issue), no Revit writes
        assert revit.calls == []
        assert state["proposed_fixes"][0]["executed"] is True


# ---- v1.4-F2: partition-before-quota regression ----------------------------


def _fake_finding(rule_id: str, element_id: str) -> Finding:
    """Tiny Finding stub for quota / partition unit tests."""
    return Finding(
        rule_id=rule_id,
        element_id=element_id,
        parameter="x",
        severity_tag="missing_required_param",
        severity="severity_medium",
        message="",
        suggested_value=None,
        citation=None,
        status="non_compliant",
    )


class TestApplyQuotaPolicy:
    """v1.4-K18: ``_apply_quota`` is a pass-through — no per-finding slicing.

    The budget now caps ACC ISSUES at the GROUP level in ``run()`` (Path B → one
    uncapped proposal; Path A → one issue per (rule, status), bounded by the
    remaining budget). Issue-level capping is covered by
    ``test_coordination.TestIssueBudget``. Here we only pin that nothing is
    silently dropped at the partition seam.
    """

    def test_passthrough_keeps_everything(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path), project_id="b.test",
            max_issues=2, revit_mcp=revit,
        )
        path_a = [_fake_finding("a", f"A{i}") for i in range(3)]
        path_b = [_fake_finding("b", f"B{i}") for i in range(3)]
        a_out, b_out = agent._apply_quota(path_a, path_b)
        assert [f["element_id"] for f in a_out] == ["A0", "A1", "A2"]
        assert [f["element_id"] for f in b_out] == ["B0", "B1", "B2"]


@pytest.mark.asyncio
class TestPathBPriorityIntegration:
    """End-to-end: Path B is never starved — its writes commit regardless of how
    Path A findings are ordered (v1.4-F2 intent, preserved under K18)."""

    async def test_path_b_commits_and_path_a_gets_its_issue(
        self, tmp_path
    ) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=1,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_manual_rule_area(), _auto_rule_inferred()),
        )
        findings = [
            _finding(
                rule_id="room.area.other_min", element_id="830966",
                parameter="areaMetric", severity="severity_high",
                severity_tag="geometric_violation",
            ),
            _finding(
                rule_id="room.department.required", element_id="829712",
                parameter="Department", suggested="Residential",
            ),
        ]
        elements = [
            _room("830966", "Storage P04", areaMetric=5.32),
            _room("829712", "Studio 203", Department=""),
        ]
        state = await agent.run(_state(findings, elements))
        # Path B Department committed via ONE revit_batch (auto-write, uncapped)
        batch_calls = revit.calls_to("revit_batch")
        assert len(batch_calls) == 1
        committed_dept = [
            s for s in batch_calls[0]["steps"]
            if s["params"].get("parameterName") == "Department"
        ]
        assert len(committed_dept) == 1
        # v1.4-K18: the auto write isn't a proposal issue, so Path A's manual
        # area finding gets its OWN issue within budget=1 (not starved).
        executed_issues = [
            a for n, a in forma.calls
            if n == "create_issue" and a.get("dry_run") is False
        ]
        assert len(executed_issues) == 1
        assert len(state["proposed_fixes"]) == 2


# ---- Comments template rendering -------------------------------------------


@pytest.mark.asyncio
class TestCommentsTemplate:
    async def test_template_substitutes_new_value(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        await agent.run(
            _state(findings, [_room("829712", "Studio 203", Department="")])
        )
        batch_steps = revit.calls_to("revit_batch")[0]["steps"]
        comments_step = next(
            s["params"] for s in batch_steps
            if s["params"].get("parameterName") == "Comments"
        )
        # Template: "Auto-filled Department='{new_value}' — BEP §1.7"
        assert comments_step["value"] == (
            "Auto-filled Department='Residential' — BEP §1.7"
        )


# ---- rename_element dispatch ------------------------------------------------


def _auto_rule_rename() -> Rule:
    return Rule.model_validate(
        {
            "id": "room.name.sra",
            "parameter": "Name",
            "requirement": "matches_regex",
            # NB: the field is `pattern`. This fixture said `regex` for months;
            # the lenient schema dropped it silently, so this rule ran with
            # pattern=None. Caught the moment `extra="forbid"` landed.
            "pattern": r"^[A-Z]{2,4}-\d{3}$",
            "severity_tag": "missing_required_param",
            "description": "Room name must match SRA format",
            "fixability": "auto",
            "remediation": {
                "action": "rename_element",
                "new_value_strategy": "inferred",
            },
            "autofill": {"strategy": "none"},
        }
    )


@pytest.mark.asyncio
class TestRenameElementDispatch:
    """rename_element action routes through rename_element MCP call, not set_parameter."""

    async def test_rename_calls_rename_element_not_set_parameter(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_rename()),
        )
        findings = [
            _finding(
                rule_id="room.name.sra",
                element_id="829712",
                parameter="Name",
                suggested="LV-203",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Name="Studio 203")])
        )
        # Preview is a direct rename_element (dry-run); commit is in revit_batch.
        rename_calls = revit.calls_to("revit_rename_element")
        assert len(rename_calls) == 1
        assert rename_calls[0]["dryRun"] is True
        batch_steps = revit.calls_to("revit_batch")[0]["steps"]
        rename_step = next(s for s in batch_steps if s["command"] == "rename_element")
        assert rename_step["params"]["name"] == "LV-203"
        # set_parameter must NOT have been used for the main write
        set_calls = [
            c for c in revit.calls_to("revit_set_parameter")
            if c["parameterName"] != "Comments"
        ]
        assert set_calls == []
        fix = state["proposed_fixes"][0]
        assert fix["executed"] is True
        assert fix["parameter"] == "Name"
        assert fix["new_value"] == "LV-203"

    async def test_rename_fix_parameter_field_is_name(self, tmp_path) -> None:
        """ProposedFix.parameter == 'Name' regardless of rule.parameter value."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_rename()),
        )
        findings = [
            _finding(
                rule_id="room.name.sra",
                element_id="829712",
                parameter="Name",
                suggested="LV-203",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Name="Studio 203")])
        )
        assert state["proposed_fixes"][0]["parameter"] == "Name"


# ---- v1.4-K4: compose_template auto + revit_batch grouping -----------------


def _auto_rule_compose() -> Rule:
    return Rule.model_validate(
        {
            "id": "ducts.mark.required",
            "parameter": "Mark",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",  # → severity_medium → approve
            "description": "Mark naming convention",
            "fixability": "auto",
            "remediation": {
                "action": "set_parameter",
                "target_parameter": "Mark",
                "new_value_strategy": "inferred",
            },
            "autofill": {
                "strategy": "compose_template",
                "template": "{X}",
                "sequence_scope": [],
            },
        }
    )


@pytest.mark.asyncio
class TestComposeTemplateAutoBatch:
    async def test_deterministic_auto_overrides_approve_and_batches(self, tmp_path):
        """compose_template fill is auto despite severity_medium=approve, and
        all writes commit in ONE revit_batch transaction (single undo)."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            # severity_medium=approve would gate a NON-deterministic fill.
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_compose()),
        )
        eids = ["829712", "830966", "829648"]  # present in SAMPLE_REVIT_ELEMENT_INFO
        findings = [
            _finding(
                rule_id="ducts.mark.required",
                element_id=eid,
                parameter="Mark",
                suggested=f"Mark-{eid}",
            )
            for eid in eids
        ]
        state = await agent.run(
            _state(findings, [_room(eid, f"R{eid}") for eid in eids])
        )
        # Opt B: deterministic → auto despite autonomy=approve, all executed.
        assert all(f["autonomy"] == "auto" for f in state["proposed_fixes"])
        assert all(f["executed"] for f in state["proposed_fixes"])
        # ONE revit_batch transaction carrying all 3 writes.
        batch_calls = revit.calls_to("revit_batch")
        assert len(batch_calls) == 1
        steps = batch_calls[0]["steps"]
        assert len(steps) == 3
        assert {s["params"]["value"] for s in steps} == {f"Mark-{e}" for e in eids}
        # No per-element committed set_parameter (only dry-run previews).
        commits = [
            c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is False
        ]
        assert commits == []

    async def test_falls_back_to_per_element_when_batch_unsupported(self, tmp_path):
        """When the addin lacks HTTP batch (unknown_command), commits fall back
        to per-element set_parameter — writes still land (just N undo entries)."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient(unsupported_commands={"revit_batch"})
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_compose()),
        )
        eids = ["829712", "830966"]
        findings = [
            _finding(rule_id="ducts.mark.required", element_id=e,
                     parameter="Mark", suggested=f"Mark-{e}")
            for e in eids
        ]
        state = await agent.run(_state(findings, [_room(e, f"R{e}") for e in eids]))
        # batch was attempted then fell back → per-element commits executed.
        assert len(revit.calls_to("revit_batch")) == 1
        committed = [
            c for c in revit.calls_to("revit_set_parameter")
            if c["dryRun"] is False and c["parameterName"] == "Mark"
        ]
        assert {c["value"] for c in committed} == {f"Mark-{e}" for e in eids}
        assert all(f["executed"] for f in state["proposed_fixes"])

    async def test_non_deterministic_still_gated_by_approve(self, tmp_path):
        """A non-compose_template auto rule still respects severity=approve →
        parked, no batch."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred()),  # infer_from_room_name
        )
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        state = await agent.run(
            _state(findings, [_room("829712", "Studio 203", Department="")])
        )
        assert state["proposed_fixes"][0]["autonomy"] == "approve"
        assert state["proposed_fixes"][0]["executed"] is False
        assert revit.calls_to("revit_batch") == []


class TestProposeOnly:
    """SPEC_SCHEDULED_AUDIT_DELTA.md W1 — ``propose_only`` demotes every
    would-be-auto Path B decision (including the K4 Opt B deterministic
    bypass) to approve-gated, so a scheduled/unattended run never writes
    the model."""

    @pytest.mark.asyncio
    async def test_deterministic_auto_demoted_to_approve_no_write(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_compose()),
            approvals_dir=tmp_path / "approvals",
            propose_only=True,
        )
        eids = ["829712", "830966", "829648"]
        findings = [
            _finding(
                rule_id="ducts.mark.required",
                element_id=eid,
                parameter="Mark",
                suggested=f"Mark-{eid}",
            )
            for eid in eids
        ]
        state = await agent.run(
            _state(findings, [_room(eid, f"R{eid}") for eid in eids])
        )
        # Would be "auto" (compose_template, Opt B) without propose_only —
        # demoted to "approve" here, and NOT executed.
        assert all(f["autonomy"] == "approve" for f in state["proposed_fixes"])
        assert all(not f["executed"] for f in state["proposed_fixes"])
        # No committed write of any kind reached Revit.
        assert revit.calls_to("revit_batch") == []
        commits = [
            c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is False
        ]
        assert commits == []
        # The fix still becomes an approve-gated proposal issue (Loop 2) —
        # a record is parked in approvals_dir for the ApprovalWatcher.
        records = list((tmp_path / "approvals").glob("*.json"))
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_propose_only_false_keeps_legacy_auto_behaviour(self, tmp_path):
        """Sanity: propose_only defaults False and doesn't touch the
        pre-existing compose_template auto-apply behaviour."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_compose()),
        )
        eids = ["829712", "830966"]
        findings = [
            _finding(rule_id="ducts.mark.required", element_id=e,
                     parameter="Mark", suggested=f"Mark-{e}")
            for e in eids
        ]
        state = await agent.run(_state(findings, [_room(e, f"R{e}") for e in eids]))
        assert all(f["autonomy"] == "auto" for f in state["proposed_fixes"])
        assert all(f["executed"] for f in state["proposed_fixes"])
        assert len(revit.calls_to("revit_batch")) == 1


class TestUnfixablePathBRoutesToA:
    """v1.4-K11 — a non_compliant Path-B finding with NO computable value
    (normalize/inferred produced None) must become a Path A ACC Issue, not a
    dead parked write with new_value=None (which 400s set_parameter)."""

    @pytest.mark.asyncio
    async def test_no_value_routes_to_path_a_issue(self, tmp_path):
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        rule = Rule.model_validate({
            "id": "d.fr.fmt", "parameter": "Fire Rating", "category": "Doors",
            "requirement": "matches_regex_if_present", "pattern": "^X$",
            "severity_tag": "naming_violation", "description": "fmt",
            "fixability": "auto",
            "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating"},
            "remediation": {"action": "set_parameter", "target": "type"},
        })
        finding = _finding(
            rule_id="d.fr.fmt", element_id="631418", parameter="Fire Rating",
            severity="severity_high", severity_tag="naming_violation", suggested=None,
        )
        el = {"id": "631418", "name": "Door", "category": "Doors",
              "params": {"Fire Rating": "180 MIN", "_type_id": "2162722"}}
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test", max_issues=10, rule_filter=None,
            revit_mcp=revit, rules=_ruleset(rule),
        )
        state = await agent.run(_state([finding], [el]))

        # No set_parameter attempted (no value to write)
        assert revit.calls_to("revit_set_parameter") == []
        assert revit.calls_to("revit_batch") == []
        # A Path A ACC issue WAS created instead
        assert any(
            name == "create_issue" and a.get("dry_run") is False
            for name, a in forma.calls
        )
        # No dangling parked fix carrying a None value as approve-pending
        for f in state["proposed_fixes"]:
            if f.get("autonomy") == "approve" and not f.get("executed"):
                assert f.get("new_value") is not None


# ---- M2: bound_parameter honoured on the WRITE side too --------------------


class TestBoundParameterWriteSide:
    """M2: DesignAgent must resolve the actual Revit param via
    ``bound_parameter`` (not the canonical ``parameter``) for the write
    target, the old_value capture, and the dedup key — else a bound rule's
    fix targets a parameter name that doesn't exist on the element (silent
    not_found / wrong binding) and old_value always reads as empty."""

    @pytest.mark.asyncio
    async def test_write_targets_bound_parameter_name(self, tmp_path) -> None:
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        rule = Rule.model_validate({
            "id": "room.department.bound",
            "parameter": "Phong_ban",          # canonical intent label (VN)
            "bound_parameter": "Department",    # real Revit param
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "description": "Department required",
            "fixability": "auto",
            "remediation": {
                "action": "set_parameter",
                # target_parameter deliberately omitted → must fall back to
                # bound_parameter, NOT the canonical "Phong_ban" (which isn't
                # a real Revit parameter and would 400 on write).
                "new_value_strategy": "inferred",
            },
            "autofill": {"strategy": "infer_from_room_name", "fallback": "General"},
        })
        finding = _finding(
            rule_id="room.department.bound", element_id="829712",
            parameter="Phong_ban", suggested="Residential",
        )
        el = _room("829712", "Studio Unit 203", Department="")
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test", max_issues=10, rule_filter=None,
            revit_mcp=revit, rules=_ruleset(rule),
        )
        state = await agent.run(_state([finding], [el]))

        set_calls = revit.calls_to("revit_set_parameter")
        assert len(set_calls) == 1
        # The dry-run preview must target the BOUND name, not "Phong_ban".
        assert set_calls[0]["parameterName"] == "Department"
        batch_steps = revit.calls_to("revit_batch")[0]["steps"]
        assert batch_steps[0]["params"]["parameterName"] == "Department"
        fix = state["proposed_fixes"][0]
        assert fix["executed"] is True
        assert fix["parameter"] == "Department"

    @pytest.mark.asyncio
    async def test_old_value_read_from_bound_parameter(self, tmp_path) -> None:
        """old_value must be read via bound_parameter — else a bound rule
        always shows "(empty)" even though the element carries a real value
        under the bound Revit name."""
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient(
            element_info={
                501: {
                    "id": 501, "name": "Door 501", "category": "Doors",
                    "parameters": [
                        {"name": "Mark", "value": "05", "valueString": "05"},
                    ],
                }
            }
        )
        rule = Rule.model_validate({
            "id": "door.mark.bound",
            "parameter": "Ma_cua",
            "bound_parameter": "Mark",
            "requirement": "matches_regex", "pattern": r"^D-\d{2}$",
            "severity_tag": "naming_violation", "description": "Mark format",
            "fixability": "auto",
            "autofill": {
                "strategy": "normalize", "normalize_kind": "template",
                "normalize_source": r"^(\d+)$", "normalize_format": "D-{1}",
            },
            "remediation": {"action": "set_parameter", "new_value_strategy": "inferred"},
        })
        finding = _finding(
            rule_id="door.mark.bound", element_id="501", parameter="Ma_cua",
            severity_tag="naming_violation", suggested="D-05",
        )
        el = {"id": "501", "name": "Door 501", "category": "Doors", "params": {"Mark": "05"}}
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test", max_issues=10, rule_filter=None,
            revit_mcp=revit, rules=_ruleset(rule),
        )
        state = await agent.run(_state([finding], [el]))
        fix = state["proposed_fixes"][0]
        # old_value resolved from the BOUND name's live value ("05"), not None.
        assert fix["preview"]["old_value"] == "05"
        assert fix["preview"]["old_value_raw"] == "05"

    def test_auto_target_resolves_type_via_bound_name(self, tmp_path) -> None:
        """``target="auto"`` probes ``type.<param>`` to decide instance-vs-type.

        The query layer mirrors a Type-side value under the name it FETCHED —
        i.e. the bound one — so probing ``type.<canonical>`` always missed for
        a bound rule. It then resolved to an INSTANCE write, which fails
        dry-run with not_found for a type-carried param, and the whole fix
        degraded to a Path A issue: a working Path B silently lost.
        """
        rule = Rule.model_validate({
            "id": "door.rating.bound",
            "parameter": "Cap_chong_chay",       # canonical intent label (VN)
            "bound_parameter": "Fire Rating",     # real, TYPE-carried Revit param
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param", "description": "rating",
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "auto",
                            "new_value_strategy": "inferred"},
            "autofill": {"strategy": "none"},
        })
        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test", max_issues=10, rule_filter=None,
            revit_mcp=MockRevitMCPClient(), rules=_ruleset(rule),
        )
        el = {
            "id": "501", "category": "Doors",
            # mirrored under the BOUND name, exactly as revit_query writes it
            "params": {"Fire Rating": "", "type.Fire Rating": "60", "_type_id": 900},
        }
        assert agent._effective_remediation(rule, el) == ("set_parameter", "type")

    def test_auto_target_family_sentinel_matches_bound_name(self, tmp_path) -> None:
        # The rename sentinels are matched BY NAME; a rule that binds to
        # "Family Name" must still resolve to a family rename.
        rule = Rule.model_validate({
            "id": "fam.name.bound",
            "parameter": "Ten_ho",
            "bound_parameter": "Family Name",
            "requirement": "matches_regex", "pattern": r"^[A-Z]",
            "severity_tag": "naming_violation", "description": "family name",
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "auto"},
            "autofill": {"strategy": "none"},
        })
        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test", max_issues=10, rule_filter=None,
            revit_mcp=MockRevitMCPClient(), rules=_ruleset(rule),
        )
        el = {"id": "1", "category": "Furniture", "params": {"Family Name": "x chair"}}
        assert agent._effective_remediation(rule, el) == ("rename_element", "family")

    def test_sibling_values_read_under_bound_name(self, tmp_path) -> None:
        """unique_in_set compares against siblings — read under the bound name.

        Reading canonical made every sibling None, so the validator saw no
        duplicates at all and would happily propose a value already in use.
        """
        rule = Rule.model_validate({
            "id": "room.number.bound",
            "parameter": "So_phong",
            "bound_parameter": "Number",
            "requirement": "unique_in_set",
            "severity_tag": "duplicate_value", "description": "unique number",
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "instance",
                            "new_value_strategy": "next_available"},
            "autofill": {"strategy": "none"},
            "category": "Rooms",
        })
        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test", max_issues=10, rule_filter=None,
            revit_mcp=MockRevitMCPClient(), rules=_ruleset(rule),
        )
        agent._all_elements = [
            {"id": "1", "category": "Rooms", "params": {"Number": "101"}},
            {"id": "2", "category": "Rooms", "params": {"Number": "102"}},
            {"id": "3", "category": "Rooms", "params": {"Number": "103"}},
        ]
        siblings = agent._gather_sibling_values(rule, exclude_eid="3")
        assert siblings == ["101", "102"]


# ---- ACC 1000-char description cap (ISSUES_SERVICE_BAD_REQUEST guard) -------


class TestFitAccDescription:
    """`_fit_acc_description` keeps a create_issue body under ACC's hard 1000-
    char limit while never dropping fixes (the write-set carries all elements;
    only the human-readable text is trimmed, and it says so)."""

    def test_short_body_unchanged(self):
        from bim_orchestrator.agents.design import _fit_acc_description

        body = "### Rule\n- 1 | a -> b\n- 2 | c -> d"
        assert _fit_acc_description(body) == body

    def test_long_body_capped_and_footer_added(self):
        from bim_orchestrator.agents.design import (
            ACC_DESCRIPTION_MAX,
            _fit_acc_description,
        )

        body = "### Rule `r`\n" + "\n".join(
            f"- element-{i} | `(empty)` -> `Value{i}`" for i in range(200)
        )
        assert len(body) > ACC_DESCRIPTION_MAX
        out = _fit_acc_description(body)
        assert len(out) <= ACC_DESCRIPTION_MAX
        assert "trimmed to fit" in out
        # Trimmed at a line boundary — no dangling half-line before the footer.
        assert " -> `Value" in out  # at least one full change line survived

    def test_reserve_leaves_room_for_appended_block(self):
        from bim_orchestrator.agents.design import (
            ACC_DESCRIPTION_MAX,
            _fit_acc_description,
        )

        body = "\n".join(f"- element-{i} | x -> y" for i in range(200))
        tail = "\n\n---\nAutoAudit-Fingerprint: " + "a" * 64 + "\n"
        capped = _fit_acc_description(body, reserve=len(tail))
        # The caller concatenates `tail` after — the SUM must fit the ACC cap.
        assert len(capped) + len(tail) <= ACC_DESCRIPTION_MAX

    def test_proposal_body_keeps_fingerprint_line_under_cap(self, tmp_path):
        """End-to-end: a proposal with many fixes still emits a create_issue
        body that is <= 1000 chars AND carries the intact fingerprint marker."""
        import asyncio

        from bim_orchestrator.agents.design import (
            ACC_DESCRIPTION_MAX,
            DesignAgent,
        )
        from bim_orchestrator.policies.approval_integrity import FINGERPRINT_LABEL

        forma = MockFormaMCPClient()
        agent = DesignAgent(
            mcp=forma,
            autonomy=_autonomy(tmp_path),
            project_id="p",
            approvals_dir=tmp_path / "approvals",
            issue_subtype_id="00000000-0000-0000-0000-000000000005",
        )
        agent._rules_by_id = {}
        proposal_fixes: list[dict[str, Any]] = [
            {
                "finding_id": f"r::{i}",
                "element_id": f"door-{i:04d}",
                "parameter": "Fire Rating",
                "new_value": "90 MIN",
                "autonomy": "approve",
                "approval_token": None,
                "preview": {"rule_id": "doors.fire.ibc716", "old_value": "60 MIN"},
                "executed": False,
            }
            for i in range(80)
        ]
        asyncio.run(agent._create_proposal_issue(proposal_fixes))

        create_calls = [a for name, a in forma.calls if name == "create_issue"]
        assert create_calls, "expected a create_issue call"
        for a in create_calls:
            desc = a.get("description") or ""
            assert len(desc) <= ACC_DESCRIPTION_MAX
            assert FINGERPRINT_LABEL in desc  # marker survived the trim


class TestProposalIssueDedup:
    """v1.5-R5: DesignAgent must not create a second ACC proposal issue for a
    rule whose approve-gated write-set hasn't changed — neither across
    iterations of ONE run (in-run memo, ``self._proposed_fingerprints``) nor
    across separate CLI invocations before anyone approved the first one
    (cross-run scan of the approvals dir, ``_find_parked_duplicate``).
    """

    @staticmethod
    def _findings_and_elements() -> tuple[list[Finding], list[dict[str, Any]]]:
        findings = [
            _finding(
                rule_id="room.department.required",
                element_id="829712",
                parameter="Department",
                suggested="Residential",
            )
        ]
        elements = [_room("829712", "Studio Unit 203", Department="")]
        return findings, elements

    def _agent(self, tmp_path, approvals_dir, *, mcp=None) -> DesignAgent:
        return DesignAgent(
            mcp=mcp if mcp is not None else MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=MockRevitMCPClient(),
            rules=_ruleset(_auto_rule_inferred()),
            approvals_dir=approvals_dir,
        )

    async def test_same_rule_same_findings_across_iterations_creates_one_issue(
        self, tmp_path
    ) -> None:
        approvals_dir = tmp_path / "approvals"
        agent = self._agent(tmp_path, approvals_dir)
        forma = agent._mcp
        findings, elements = self._findings_and_elements()

        # graph.py constructs DesignAgent ONCE per run() and reuses it across
        # every LangGraph iteration. Simulate iteration 0 then iteration 1
        # re-running on the SAME still-approve-gated findings (route_node
        # looped because some OTHER rule's auto-fix changed the fingerprint).
        await agent.run(_state(findings, elements))
        await agent.run(_state(findings, elements))

        create_calls = [
            a for name, a in forma.calls
            if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 1  # was 2 before the fix
        assert len(list(approvals_dir.glob("*.json"))) == 1

    async def test_in_run_memo_hit_stamps_issue_id_on_iteration_1_fixes(
        self, tmp_path
    ) -> None:
        """v1.5-R6 (join-hardening 1.1a): the SECOND iteration's fixes (the
        memo-hit branch) must carry the SAME proposal_issue_id as iteration
        0's — before this fix they were left un-stamped, and since
        `report_trace.index_fixes` is last-write-wins, the later un-stamped
        stub would shadow the real proposal in the verification report."""
        approvals_dir = tmp_path / "approvals"
        agent = self._agent(tmp_path, approvals_dir)
        findings, elements = self._findings_and_elements()

        state1 = await agent.run(_state(findings, elements))
        issue_id_1 = next(
            (f.get("preview") or {}).get("proposal_issue_id")
            for f in state1["proposed_fixes"]
            if (f.get("preview") or {}).get("proposal_issue_id")
        )
        assert issue_id_1 is not None

        state2 = await agent.run(_state(findings, elements))
        # Every iteration-1 fix for this rule must carry the SAME issue id —
        # not be left unstamped (None) the way it was before the fix.
        iter1_fixes = [
            f for f in state2["proposed_fixes"]
            if f.get("finding_id", "").startswith("room.department.required::")
        ]
        assert iter1_fixes
        for f in iter1_fixes:
            preview = f.get("preview") or {}
            assert preview.get("proposal_issue_id") == issue_id_1
            assert "already proposed this run" in (preview.get("proposal_note") or "")

    async def test_cross_run_pending_duplicate_skips_and_stamps_old_issue_id(
        self, tmp_path
    ) -> None:
        findings, elements = self._findings_and_elements()
        approvals_dir = tmp_path / "approvals"
        # Shared MCP client across both "runs" — ACC issue ids are globally
        # unique in production regardless of process; a fresh mock instance
        # per agent would coincidentally reuse "issue-mock-0001" and mask the
        # dedup behaviour under test.
        forma = MockFormaMCPClient(elements=[])

        # "Run 1": a fresh DesignAgent (a separate CLI invocation) proposes.
        agent1 = self._agent(tmp_path, approvals_dir, mcp=forma)
        await agent1.run(_state(findings, elements))
        record_files = list(approvals_dir.glob("*.json"))
        assert len(record_files) == 1
        original_issue_id = json.loads(
            record_files[0].read_text(encoding="utf-8")
        )["issue_id"]

        # "Run 2": a brand-new DesignAgent instance (no in-run memo — this is
        # the cross-run case), same approvals_dir, nobody approved run 1 yet.
        calls_before = len(forma.calls)
        agent2 = self._agent(tmp_path, approvals_dir, mcp=forma)
        state2 = await agent2.run(_state(findings, elements))

        create_calls2 = [
            a for name, a in forma.calls[calls_before:]
            if name == "create_issue" and not a["dry_run"]
        ]
        assert create_calls2 == []  # no NEW ACC issue
        assert list(approvals_dir.glob("*.json")) == record_files  # no new record

        preview = state2["proposed_fixes"][0].get("preview") or {}
        assert preview.get("proposal_issue_id") == original_issue_id
        assert "already parked" in (preview.get("proposal_note") or "")

    async def test_cross_run_applied_record_still_creates_new_issue(
        self, tmp_path
    ) -> None:
        """Findings reappearing for a rule whose PARKED proposal was already
        applied means the model drifted since — a fresh proposal is correct,
        never suppressed."""
        findings, elements = self._findings_and_elements()
        approvals_dir = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])  # shared — see note above

        agent1 = self._agent(tmp_path, approvals_dir, mcp=forma)
        await agent1.run(_state(findings, elements))
        record_path = next(approvals_dir.glob("*.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["applied"] = True  # simulate the ApprovalWatcher having applied it
        record_path.write_text(json.dumps(record), encoding="utf-8")

        calls_before = len(forma.calls)
        agent2 = self._agent(tmp_path, approvals_dir, mcp=forma)
        await agent2.run(_state(findings, elements))

        create_calls2 = [
            a for name, a in forma.calls[calls_before:]
            if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls2) == 1  # genuinely new proposal, not suppressed
        assert len(list(approvals_dir.glob("*.json"))) == 2

    async def test_record_without_fingerprint_field_is_ignored(
        self, tmp_path
    ) -> None:
        """Back-compat: a pre-approval-security record (no ``fingerprint``
        key, predates 110a27f) must never be dedup'd against."""
        findings, elements = self._findings_and_elements()
        approvals_dir = tmp_path / "approvals"
        approvals_dir.mkdir(parents=True)
        (approvals_dir / "issue-legacy-0001.json").write_text(
            json.dumps({
                "issue_id": "issue-legacy-0001",
                "project_id": "b.test",
                "applied": False,
                "status": "pending_approval",
                "fixes": [],
                # deliberately no "fingerprint" key
            }),
            encoding="utf-8",
        )

        agent = self._agent(tmp_path, approvals_dir)
        forma = agent._mcp
        await agent.run(_state(findings, elements))

        create_calls = [
            a for name, a in forma.calls
            if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 1  # not suppressed by the fingerprint-less record


# ---- C-01: garbage must never win the magnitude reducer --------------------


class TestConflictReducerRejectsGarbage:
    """C-01 regression. `_conflict_sort_key` used to sort unparseable values
    ABOVE every parseable one — harmless under the old `min()` reducer, a
    hazard the instant it flipped to `max()`: a corrupted normalize output or a
    typo'd host value would beat every real rating and be written to the Type.
    """

    def test_sort_key_puts_unparseable_below_every_rating(self):
        key = DesignAgent._conflict_sort_key
        assert key("CORRUPTED") < key("20 MIN")      # garbage loses to the smallest
        assert key(None) < key("20 MIN")
        assert key("90 MIN") < key("2 HR")           # real ordering intact

    def test_garbage_candidate_never_becomes_the_write(self, tmp_path):
        """The exact input that shipped broken: a declared-fire-rating rule
        bypasses the per-candidate screen, so any string reaches the reducer."""
        rule = Rule.model_validate({
            "id": "fr.declared", "parameter": "Fire Rating",
            "requirement": "relation_compare", "compare_kind": "fire_rating",
            "operator": ">=", "severity_tag": "naming_violation", "description": "fr",
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "auto"},
            "autofill": {"strategy": "none"},
        })
        agent = _design_for(rule, tmp_path)
        els = [
            {"id": "10", "params": {"type.Fire Rating": "X", "_type_id": 700}},
            {"id": "11", "params": {"type.Fire Rating": "X", "_type_id": 700}},
        ]
        ebi = {e["id"]: e for e in els}
        fs = [
            _finding(rule_id="fr.declared", element_id="10",
                     parameter="Fire Rating", suggested="90 MIN"),
            _finding(rule_id="fr.declared", element_id="11",
                     parameter="Fire Rating", suggested="CORRUPTED"),
        ]
        out = agent._dedup_by_write_target(fs, ebi)
        assert len(out) == 1
        assert out[0]["suggested_value"] == "90 MIN"          # NOT "CORRUPTED"
        conflict = out[0]["host_conflict"]
        assert conflict["chosen"] == "90 MIN"
        # the garbage is still DISCLOSED to the human, just not written
        assert set(conflict["candidates"]) == {"90 MIN", "CORRUPTED"}

    def test_all_unparseable_group_does_not_invent_an_ordering(self, tmp_path):
        rule = Rule.model_validate({
            "id": "fr.declared2", "parameter": "Fire Rating",
            "requirement": "relation_compare", "compare_kind": "fire_rating",
            "operator": ">=", "severity_tag": "naming_violation", "description": "fr",
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "auto"},
            "autofill": {"strategy": "none"},
        })
        agent = _design_for(rule, tmp_path)
        els = [{"id": str(i), "params": {"type.Fire Rating": "X", "_type_id": 800}}
               for i in (20, 21)]
        ebi = {e["id"]: e for e in els}
        fs = [
            _finding(rule_id="fr.declared2", element_id="20",
                     parameter="Fire Rating", suggested="JUNK-A"),
            _finding(rule_id="fr.declared2", element_id="21",
                     parameter="Fire Rating", suggested="JUNK-B"),
        ]
        out = agent._dedup_by_write_target(fs, ebi)
        assert len(out) == 1
        assert out[0]["suggested_value"] == "JUNK-A"          # first-win, no invented order


class TestProposalBodyTellsTheTruth:
    """C-01 second half: the ACC issue body — the text a human reads before
    approving — still said "wrote the **minimum**" after the reducer flipped to
    max. No test touched the rendered markdown, only the host_conflict dict,
    which is exactly why it shipped.
    """

    def test_conflict_note_says_maximum_not_minimum(self, tmp_path):
        rule = _auto_target_rule("Fire Rating")
        agent = _design_for(rule, tmp_path)
        fix: dict = {
            "finding_id": "f1", "element_id": "700", "parameter": "Fire Rating",
            "new_value": "2 HR", "rule_id": rule.id,
            "preview": {
                "old_value": "90 MIN", "write_eid": 700,
                "host_conflict": {
                    "count": 2, "chosen": "2 HR",
                    "candidates": ["2 HR", "90 MIN"],
                },
            },
        }
        body = agent._build_proposal_description([fix])  # type: ignore[list-item]
        assert "**maximum**" in body
        assert "minimum" not in body.lower()
        assert "2 HR" in body and "90 MIN" in body       # full candidate set disclosed


# ---------------------------------------------------------------------------
# L-06 / L-02 (2026-08-01 review, Low sweep)
# ---------------------------------------------------------------------------


class _CommentHostileRevit(MockRevitMCPClient):
    """A Revit whose Comments parameter refuses writes.

    `fail_on` keys on the TOOL name, and the audit comment shares
    `revit_set_parameter` with the primary write — so failing just the comment
    needs a filter on the parameter itself (the same shape the dry-run vs
    commit distinction needed in PR #38).
    """

    async def set_parameter(self, element_id, parameter_name, value, *, dry_run=True):
        if parameter_name == "Comments" and not dry_run:
            raise RevitEnvelopeError(
                tool="revit_set_parameter", code="read_only",
                message="Comments is read-only on this type",
            )
        return await super().set_parameter(
            element_id, parameter_name, value, dry_run=dry_run
        )


def _auto_rule_compose_with_comment() -> Rule:
    body = _auto_rule_compose().model_dump()
    body["remediation"]["comments_template"] = "AutoAudit set {new_value}"
    return Rule.model_validate(body)


@pytest.mark.asyncio
class TestPerElementCommentFailure:
    async def test_a_failed_comment_does_not_disown_the_write(self, tmp_path):
        """L-06: the primary write and the Comments write were in one `try`, so
        a read-only Comments parameter sent the loop to `continue` and skipped
        `executed = True` — for a value that was ALREADY in the model. The run
        then under-claimed: it denied a write it had really made, and (because
        `_find_parked_duplicate` ignores records with `applied` truthy) lost
        the dedup anchor for it too.
        """
        revit = _CommentHostileRevit(unsupported_commands={"revit_batch"})
        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(_auto_rule_compose_with_comment()),
        )
        eid = "829712"
        state = await agent.run(
            _state(
                [_finding(rule_id="ducts.mark.required", element_id=eid,
                          parameter="Mark", suggested="Mark-1")],
                [_room(eid, "R1")],
            )
        )
        fix = state["proposed_fixes"][0]
        # The Mark write really did commit...
        mark_commits = [
            c for c in revit.calls_to("revit_set_parameter")
            if c["parameterName"] == "Mark" and not c.get("dryRun")
        ]
        assert mark_commits, "the primary write never happened — wrong scenario"
        # ...so the record must say so, and say the note went missing.
        assert fix["executed"] is True
        assert fix["preview"]["comment_failed"] == "read_only"

    async def test_a_failed_primary_write_is_still_not_executed(self, tmp_path):
        """The other end: splitting the try must not make a FAILED primary
        write look successful."""

        class _WriteHostileRevit(MockRevitMCPClient):
            async def set_parameter(
                self, element_id, parameter_name, value, *, dry_run=True
            ):
                if not dry_run:
                    raise RevitEnvelopeError(
                        tool="revit_set_parameter", code="not_found",
                        message="element vanished",
                    )
                return await super().set_parameter(
                    element_id, parameter_name, value, dry_run=dry_run
                )

        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=_WriteHostileRevit(unsupported_commands={"revit_batch"}),
            rules=_ruleset(_auto_rule_compose_with_comment()),
        )
        eid = "829712"
        state = await agent.run(
            _state(
                [_finding(rule_id="ducts.mark.required", element_id=eid,
                          parameter="Mark", suggested="Mark-1")],
                [_room(eid, "R1")],
            )
        )
        assert state["proposed_fixes"][0]["executed"] is False


def test_fire_rating_ge_requirement_names_the_related_element_not_none():
    """L-02: `fire_rating_ge` compares against another element's value, so
    `rule.threshold` is None on every such rule that ships — and the proposal
    body a human approves rendered "fire rating must be ≥ None".

    Asserted on the RENDERED sentence, not the dict (K20's lesson: the
    min→max flip shipped a body still saying "minimum").
    """
    from bim_orchestrator.agents.design import _describe_requirement

    rule = Rule.model_validate({
        "id": "door.fire_rating.matches_host",
        "parameter": "Fire Rating",
        "requirement": "fire_rating_ge",
        "other_param": "host.Fire Rating",
        "severity_tag": "missing_required_param",
        "description": "Door rating must meet its host wall",
        "fixability": "manual",
        "autofill": {"strategy": "none"},
    })
    sentence = _describe_requirement(rule)
    assert "None" not in sentence
    assert "host.Fire Rating" in sentence


# ---------------------------------------------------------------------------
# L-01 — a renumber must look at ITS OWN category's siblings
# ---------------------------------------------------------------------------


def _door_mark_rule() -> Rule:
    return Rule.model_validate({
        "id": "door.mark.unique",
        "category": "Doors",
        "parameter": "Mark",
        "requirement": "unique_in_set",
        "severity_tag": "duplicate_identifier",
        "description": "Door Marks unique",
        "fixability": "auto",
        "remediation": {
            "action": "set_parameter",
            "target_parameter": "Mark",
            "new_value_strategy": "next_available",
        },
        "autofill": {"strategy": "none"},
    })


def _door(eid: str, mark: str) -> dict[str, Any]:
    return {"id": eid, "name": f"Door {eid}", "category": "Doors",
            "params": {"Mark": mark}}


@pytest.mark.asyncio
class TestNextAvailableUsesTheRulesOwnCategory:
    async def _propose(self, tmp_path, elements, *, max_elements=None):
        rule = _door_mark_rule()
        self.forma = MockFormaMCPClient(elements=[])
        agent = DesignAgent(
            mcp=self.forma,
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=MockRevitMCPClient(),
            rules=RuleSet(scenario="t", target_category="Doors", rules=[rule]),
            approvals_dir=tmp_path / "approvals",
            max_elements=max_elements,
        )
        findings = [_finding(rule_id=rule.id, element_id=elements[0]["id"],
                             parameter="Mark", severity_tag="duplicate_identifier",
                             suggested="D-01")]
        state = await agent.run(_state(findings, elements))
        return agent, state

    async def test_a_door_renumber_does_not_collide_with_another_door(self, tmp_path):
        """L-01: siblings came from `list_rooms` whatever the rule's category,
        so a Doors rule asked what the ROOMS carry — near-always nothing. The
        renumber concluded "D-01A is free" and proposed a value a second door
        already held: a duplicate offered as the cure for a duplicate.
        """
        elements = [
            _door("100", "D-01"),   # the one being renumbered
            _door("101", "D-01"),   # its duplicate
            _door("102", "D-01A"),  # ...and the value the old code would pick
        ]
        _, state = await self._propose(tmp_path, elements)
        proposed = state["proposed_fixes"][0]["new_value"]
        assert proposed != "D-01A", (
            "proposed a Mark another door already holds — the sibling set is "
            "still being read from the wrong category"
        )
        assert proposed == "D-01B"

    async def test_rooms_still_use_the_whole_model_query(self, tmp_path):
        """The other end: for Rooms, `list_rooms` is a whole-model query and
        remains the most complete answer — it must NOT be traded for the
        capped run population."""
        rule = _auto_rule_next_available()
        revit = MockRevitMCPClient()
        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=revit,
            rules=_ruleset(rule),
            approvals_dir=tmp_path / "approvals",
        )
        await agent.run(
            _state(
                [_finding(rule_id=rule.id, element_id="829712", parameter="Number",
                          severity_tag="duplicate_identifier", suggested="203")],
                [_room("829712", "Studio 203", Number="203")],
            )
        )
        assert len(revit.calls_to("revit_list_rooms")) == 1

    async def test_two_categories_do_not_share_one_sibling_set(self, tmp_path):
        """The cache used to key on the parameter alone, so a Doors "Mark"
        rule and a Ducts "Mark" rule pooled their values."""
        agent, _ = await self._propose(tmp_path, [_door("100", "D-01")])
        doors = await agent._get_revit_siblings(  # noqa: SLF001
            "Mark", rule=_door_mark_rule()
        )
        duct_rule = Rule.model_validate({
            **_door_mark_rule().model_dump(), "id": "duct.mark", "category": "Ducts",
        })
        ducts = await agent._get_revit_siblings("Mark", rule=duct_rule)  # noqa: SLF001
        assert "D-01" in doors
        assert ducts == set(), "a Ducts rule inherited the Doors sibling set"

    async def test_a_capped_population_says_so_on_the_proposal(self, tmp_path):
        """L-01 (owner decision 2026-08-02): still propose, but DISCLOSE that
        the 'already used' set was partial. The value never auto-applies, so
        the reviewer is the backstop — which only works if they are told.

        Asserted on the RENDERED issue body, because that is the document the
        reviewer actually reads (K20's lesson: the min→max flip shipped a body
        still saying "minimum"). An earlier draft of this test only checked
        that the helper existed — which proves nothing about what reaches ACC.
        """
        elements = [_door(str(100 + i), f"D-{i:02d}") for i in range(5)]
        elements[0]["params"]["Mark"] = "D-01"
        await self._propose(tmp_path, elements, max_elements=5)

        body = self.forma.calls_to("create_issue")[0]["description"]
        assert "Scope:" in body, "the capped-population warning never reached ACC"
        assert "--max-elements" in body
        assert "5" in body                      # the cap the reviewer must judge
        assert "Confirm the value is unused" in body

    async def test_an_uncapped_population_stays_quiet(self, tmp_path):
        """No cap reached → no warning. A note on every proposal would be
        noise, and noise is how real warnings stop being read."""
        agent, _ = await self._propose(
            tmp_path, [_door("100", "D-01"), _door("101", "D-01")], max_elements=300
        )
        assert agent._sibling_scope_note(_door_mark_rule()) is None  # noqa: SLF001
