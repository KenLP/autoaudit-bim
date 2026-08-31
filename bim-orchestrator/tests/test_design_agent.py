"""Tests for the DesignAgent — uses MockFormaMCPClient, no real ACC calls."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from bim_orchestrator.agents.design import DesignAgent, _build_issue_payload
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import Finding, OrchestratorState
from tests._mocks import MockFormaMCPClient


# Default mock for these tests: 1 active Quality subtype, no elements needed
def _default_mock(**kwargs: Any) -> MockFormaMCPClient:
    return MockFormaMCPClient(
        elements=[],
        subtypes=kwargs.get("subtypes", [
            {"id": "subtype-AAA", "title": "Quality", "type_id": "type-1",
             "type_title": "Quality", "is_active": True}
        ]),
    )


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "documents": {
                        "create_issue": "auto",
                    },
                    "parameters": {
                        "set_value": {"severity_high": "approve"},
                    },
                },
                "severity_rules": {
                    "missing_required_param": "severity_medium",
                },
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _make_state(findings: list[Finding], elements: list[dict[str, Any]]) -> OrchestratorState:
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
    rule_id: str = "room.department.required",
    element_id: str = "elem-1",
    parameter: str = "Department",
    severity: str = "severity_medium",
    suggested: Any = "UNASSIGNED",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        element_id=element_id,
        parameter=parameter,
        severity_tag="missing_required_param",
        severity=severity,  # type: ignore[arg-type]
        message=f"{rule_id} failed on {element_id}",
        suggested_value=suggested,
        citation=None,
    )


def _room(element_id: str, name: str) -> dict[str, Any]:
    return {"id": element_id, "name": name, "category": "Rooms", "params": {}}


# --- Tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_findings_short_circuits(autonomy):
    mcp = _default_mock()
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test")
    state = _make_state([], [])
    result = await agent.run(state)
    assert result["status"] == "converged"
    assert result["proposed_fixes"] == []
    # Should NOT discover subtype if there's nothing to do
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_filters_by_rule_id_groups_into_one_issue(autonomy):
    mcp = _default_mock()
    agent = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
        rule_filter="room.department.required",
    )
    findings = [
        _finding(element_id="e1"),
        _finding(element_id="e2"),
        _finding(element_id="e3"),
        _finding(rule_id="room.occupancy.required", element_id="e4"),
    ]
    elements = [_room(f"e{i}", f"Room {i}") for i in range(1, 5)]
    result = await agent.run(_make_state(findings, elements))

    # v1.4-K18: rule filter keeps the 3 Department findings → ONE grouped issue
    # (e4's rule filtered out). Limit=2 ≥ 1 group → not truncated.
    assert len(result["proposed_fixes"]) == 1
    fix = result["proposed_fixes"][0]
    assert fix["finding_id"] == "rulegroup::room.department.required::non_compliant"
    body = next(
        a["description"] for n, a in mcp.calls
        if n == "create_issue" and a.get("dry_run") is False
    )
    assert "`e1`" in body and "`e2`" in body and "`e3`" in body
    assert "`e4`" not in body


@pytest.mark.asyncio
async def test_auto_executes_two_call_flow(autonomy):
    mcp = _default_mock()
    agent = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1,
    )
    findings = [_finding(element_id="e1", suggested="UNASSIGNED")]
    elements = [_room("e1", "Closet 11A")]
    result = await agent.run(_make_state(findings, elements))

    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    assert len(create_calls) == 2  # dry_run then execute
    assert create_calls[0][1]["dry_run"] is True
    assert create_calls[0][1]["approval_token"] is None
    assert create_calls[1][1]["dry_run"] is False
    assert create_calls[1][1]["approval_token"] is not None
    assert create_calls[1][1]["approval_token"].startswith("appr_mock_")
    # v1 task B: title now uses [AutoAudit] prefix + category + rule_id (ASCII)
    assert create_calls[1][1]["title"].startswith("[AutoAudit] ")
    assert "room.department.required" in create_calls[1][1]["title"]

    fix = result["proposed_fixes"][0]
    assert fix["executed"] is True
    assert fix["autonomy"] == "auto"
    assert fix["preview"] is not None
    assert "executed_issue" in fix["preview"]
    assert fix["preview"]["executed_issue"]["id"].startswith("issue-mock-")


@pytest.mark.asyncio
async def test_dry_run_only_skips_execute(autonomy):
    mcp = _default_mock()
    agent = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1,
        dry_run_only=True,
    )
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    result = await agent.run(_make_state(findings, elements))

    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    assert len(create_calls) == 1  # only dry-run
    assert create_calls[0][1]["dry_run"] is True

    fix = result["proposed_fixes"][0]
    assert fix["executed"] is False
    assert fix["approval_token"] is not None
    assert fix["approval_token"].startswith("appr_mock_")
    assert fix["autonomy"] == "auto"  # autonomy still resolved


@pytest.mark.asyncio
async def test_approve_decision_parks_fix(tmp_path):
    """When autonomy returns 'approve', the fix is parked (not executed)."""
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {"documents": {"create_issue": "approve"}},
                "severity_rules": {"missing_required_param": "severity_medium"},
            }
        )
    )
    autonomy = AutonomyPolicy.load(cfg)
    mcp = _default_mock()
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1)

    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    result = await agent.run(_make_state(findings, elements))

    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    assert len(create_calls) == 1  # preview only
    fix = result["proposed_fixes"][0]
    assert fix["executed"] is False
    assert fix["autonomy"] == "approve"


@pytest.mark.asyncio
async def test_no_subtypes_raises(autonomy):
    mcp = MockFormaMCPClient(elements=[], subtypes=[])
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1)
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    with pytest.raises(RuntimeError, match="No issue subtypes"):
        await agent.run(_make_state(findings, elements))


@pytest.mark.asyncio
async def test_all_subtypes_inactive_raises(autonomy):
    """When MCP returns subtypes but ALL are inactive, raise a clear error."""
    mcp = MockFormaMCPClient(elements=[], subtypes=[
        {"id": "s1", "title": "Design", "type_id": "t1", "type_title": "Design", "is_active": False},
        {"id": "s2", "title": "Quality", "type_id": "t2", "type_title": "Quality", "is_active": False},
    ])
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1)
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    with pytest.raises(RuntimeError, match="all .* inactive|inactive"):
        await agent.run(_make_state(findings, elements))


@pytest.mark.asyncio
async def test_skips_inactive_subtype_picks_active(autonomy):
    """First subtype is inactive (mirrors real ACC project state). Auto-discovery must skip it."""
    mcp = MockFormaMCPClient(elements=[], subtypes=[
        {"id": "inactive-1", "title": "Design", "type_id": "t1", "type_title": "Design", "is_active": False},
        {"id": "inactive-2", "title": "Requirement Change", "type_id": "t1", "type_title": "Design", "is_active": False},
        {"id": "active-quality", "title": "Quality", "type_id": "t2", "type_title": "Quality", "is_active": True},
        {"id": "active-general", "title": "General", "type_id": "t3", "type_title": "General", "is_active": True},
    ])
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1)
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    await agent.run(_make_state(findings, elements))

    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    # Must have used Quality (preferred), not inactive Design
    assert all(c[1]["issue_subtype_id"] == "active-quality" for c in create_calls)


@pytest.mark.asyncio
async def test_prefers_quality_over_general(autonomy):
    """When both Quality and General are active, Quality wins per preference order."""
    mcp = MockFormaMCPClient(elements=[], subtypes=[
        {"id": "general-1", "title": "General", "type_id": "t1", "type_title": "General", "is_active": True},
        {"id": "quality-1", "title": "Quality", "type_id": "t2", "type_title": "Quality", "is_active": True},
    ])
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1)
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    await agent.run(_make_state(findings, elements))

    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    assert create_calls[0][1]["issue_subtype_id"] == "quality-1"


@pytest.mark.asyncio
async def test_fallback_to_first_active_when_no_preferred(autonomy):
    """No subtype matches preference list → fall back to first active subtype."""
    mcp = MockFormaMCPClient(elements=[], subtypes=[
        {"id": "inactive-x", "title": "X", "type_id": "tx", "type_title": "Custom", "is_active": False},
        {"id": "active-y", "title": "Y", "type_id": "ty", "type_title": "Custom", "is_active": True},
        {"id": "active-z", "title": "Z", "type_id": "tz", "type_title": "Another", "is_active": True},
    ])
    agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1)
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    await agent.run(_make_state(findings, elements))

    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    # First active subtype (active-y) since no preference matches "Custom" or "Another"
    assert create_calls[0][1]["issue_subtype_id"] == "active-y"


@pytest.mark.asyncio
async def test_override_subtype_skips_discovery(autonomy):
    """When issue_subtype_id is passed explicitly, MCP list_issue_subtypes is NOT called."""
    mcp = _default_mock()
    agent = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1,
        issue_subtype_id="manual-override-id",
    )
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    await agent.run(_make_state(findings, elements))

    # list_issue_subtypes should NOT have been called
    assert not any(c[0] == "list_issue_subtypes" for c in mcp.calls)
    # Override used directly
    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    assert create_calls[0][1]["issue_subtype_id"] == "manual-override-id"


@pytest.mark.asyncio
async def test_published_flag_passed_through(autonomy):
    mcp = _default_mock()
    agent = DesignAgent(
        mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=1,
        published=True,
    )
    findings = [_finding(element_id="e1")]
    elements = [_room("e1", "Closet 11A")]
    await agent.run(_make_state(findings, elements))
    create_calls = [c for c in mcp.calls if c[0] == "create_issue"]
    for c in create_calls:
        assert c[1]["published"] is True


# --- Pure payload builder tests ---------------------------------------------


class TestBuildIssuePayload:
    """v1 task B: title is `[AutoAudit] <Category> -- <rule_id>` (ASCII).
    Body has Finding + Reference sections; ASCII only per RevitMCP gotcha."""

    def test_title_includes_autoaudit_prefix_category_and_rule(self):
        f = _finding(element_id="e1", parameter="Department", suggested="UNASSIGNED")
        el = _room("e1", "Closet 11A")
        title, _desc, _ld = _build_issue_payload(f, el)
        assert title.startswith("[AutoAudit] ")
        assert "Rooms" in title  # category from _room fixture
        assert "room.department.required" in title

    def test_description_has_element_and_actual_value(self):
        f = _finding(element_id="e1", suggested="Storage")
        el = _room("e1", "Closet 11A")
        _t, desc, _ld = _build_issue_payload(f, el)
        assert "Closet 11A" in desc
        assert "e1" in desc
        assert "**Rule:**" in desc
        assert "**Severity:**" in desc

    def test_description_includes_suggested_value_when_present(self):
        f = _finding(element_id="e1", suggested="Storage")
        el = _room("e1", "Closet 11A")
        _t, desc, _ld = _build_issue_payload(f, el)
        assert "Storage" in desc

    def test_description_omits_suggested_value_when_none(self):
        f = _finding(element_id="e1", suggested=None)
        el = _room("e1", "Bedroom 2")
        _t, desc, _ld = _build_issue_payload(f, el)
        # No suggested_value line in this case -- body still readable
        assert "Suggested value" not in desc

    def test_unnamed_element_falls_back_gracefully(self):
        f = _finding(element_id="e1")
        el = {"id": "e1", "name": None, "category": "Rooms", "params": {}}
        title, desc, _ld = _build_issue_payload(f, el)
        assert "(unnamed)" in desc
        # Title still well-formed
        assert title.startswith("[AutoAudit] Rooms")

    def test_linked_documents_round_trips(self):
        """When caller passes linked_documents, payload returns them verbatim."""
        f = _finding(element_id="e1")
        el = _room("e1", "Closet 11A")
        ld = [{
            "type": "ThreeDVectorPushpin",
            "urn": "urn:adsk.wipprod:dm.lineage:abc",
            "details": {"objectId": 12345},
        }]
        _t, _desc, returned = _build_issue_payload(f, el, linked_documents=ld)
        assert returned == ld

    def test_payload_is_ascii_only(self):
        """RevitMCPAddin UTF-8 mojibake guard -- title + description must be ASCII."""
        f = _finding(element_id="e1", suggested="UNASSIGNED")
        el = _room("e1", "Closet 11A")
        title, desc, _ld = _build_issue_payload(f, el)
        title.encode("ascii")  # raises if any non-ASCII char
        desc.encode("ascii")

    def test_ascii_fold_handles_em_dash_section_and_superscript(self):
        """Realistic QC messages contain em-dash + sect-sign + m². Body must stay ASCII."""
        from bim_orchestrator.state import Finding
        f: Finding = {  # type: ignore[typeddict-item]
            "rule_id": "room.area.residential_min",
            "element_id": "urn:adsk:elem-1",
            "parameter": "areaMetric",
            "severity_tag": "geometric_violation",
            "severity": "severity_high",
            "message": "Bedroom 201 — Area 8.5 m² ≥ 10 m² per BEP §1.1.",
            "suggested_value": None,
            "citation": "“BEP §1.1” – Doc Control",
            "status": "non_compliant",
        }
        el = {
            "id": "urn:adsk:elem-1",
            "name": "Bedroom “201”",
            "category": "Rooms",
            "params": {"areaMetric": 8.5},
        }
        title, desc, _ld = _build_issue_payload(f, el)
        title.encode("ascii")  # must not raise
        desc.encode("ascii")
        # Specific folds we depend on
        assert "--" in desc        # em-dash -> --
        assert "Sec." in desc      # section -> Sec.
        assert "m^2" in desc       # superscript-2 -> ^2
        # No raw unicode chars survived
        assert "—" not in desc
        assert "§" not in desc
        assert "²" not in desc

    def test_ascii_fold_helper_handles_none_gracefully(self):
        from bim_orchestrator.agents.design import _ascii_safe
        assert _ascii_safe(None) == ""
        assert _ascii_safe("") == ""

    def test_body_surfaces_revit_unique_id_when_external_id_present(self):
        """v1 B-3 (locked from live ACC probe on 2026-06-01): AECDM exposes
        Revit UniqueId as the 'External ID' property. Body must show it
        so a BIM Manager can navigate to the element in Revit manually
        when linked_documents isn't populated yet."""
        f = _finding(element_id="urn:adsk:elem-1", suggested=None)
        el = {
            "id": "urn:adsk:elem-1",
            "name": "Closet 11A",
            "category": "Rooms",
            "params": {
                "External ID": "00000000-0000-0000-0000-000000000001-00000001",
                "Revit Element ID": "1000001",
            },
        }
        _t, desc, _ld = _build_issue_payload(f, el)
        assert "Revit UniqueId" in desc
        assert "00000000-0000-0000-0000-000000000001-00000001" in desc
        assert "Revit ElementId" in desc
        assert "1000001" in desc

    def test_body_omits_revit_ids_when_params_lack_them(self):
        """Defensive default: rooms in non-Revit-sourced models (or mocks)
        may not expose External ID. Issue body must still render cleanly."""
        f = _finding(element_id="urn:adsk:elem-1", suggested=None)
        el = {
            "id": "urn:adsk:elem-1",
            "name": "Mock Room",
            "category": "Rooms",
            "params": {"Department": "Sales"},
        }
        _t, desc, _ld = _build_issue_payload(f, el)
        assert "Revit UniqueId" not in desc
        assert "Revit ElementId" not in desc
        # The rest of the body still renders
        assert "## Reference" in desc

    def test_expected_clause_renders_per_requirement(self):
        """Expected line varies by rule.requirement -- spot-check 3 shapes."""
        from bim_orchestrator.agents.design import _describe_expected
        from bim_orchestrator.agents.qc import Rule, RuleAutofill

        f = _finding(element_id="e1")
        numeric = Rule(
            id="r", parameter="A", requirement="numeric_min", threshold=10.0,
            severity_tag="geo", description="d",
            autofill=RuleAutofill(strategy="none"),
        )
        assert _describe_expected(numeric, f) == "Value >= 10.0"

        present = Rule(
            id="r", parameter="A", requirement="present_and_nonempty",
            severity_tag="missing_required_param", description="d",
            autofill=RuleAutofill(strategy="none"),
        )
        assert _describe_expected(present, f) == "Value present and non-empty"

        regex = Rule(
            id="r", parameter="A", requirement="matches_regex",
            pattern=r"^\d{3}$",
            severity_tag="missing_optional_param", description="d",
            autofill=RuleAutofill(strategy="none"),
        )
        assert "matches" in _describe_expected(regex, f)

        # Fallback when no rule
        out = _describe_expected(None, f)
        assert "room.department.required" in out  # rule_id fallback


class TestPathAGroupDedup:
    """v1.5-R5 (Path A half): DesignAgent must not create a second ACC issue
    for a manual-finding group (rule_id, bucket, element set) that's still
    unresolved across iterations of the SAME run — the Path A twin of the
    Path B fingerprint memo (``TestProposalIssueDedup`` in
    ``test_design_agent_path_b.py``). In-run only, by design: cross-run
    duplicates are left alone (a Path A issue may already be closed by hand
    on ACC)."""

    @pytest.mark.asyncio
    async def test_same_rule_same_findings_across_two_passes_creates_one_issue(
        self, autonomy
    ) -> None:
        mcp = _default_mock()
        agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2)
        findings = [_finding(element_id="e1"), _finding(element_id="e2")]
        elements = [_room("e1", "Room 1"), _room("e2", "Room 2")]

        # graph.py builds ONE DesignAgent per run() and reuses it across every
        # LangGraph iteration. Simulate iteration 0, then iteration 1 re-running
        # on the SAME still-unresolved findings (route_node looped because some
        # OTHER rule's auto-fix changed the findings fingerprint).
        result1 = await agent.run(_make_state(findings, elements))
        result2 = await agent.run(_make_state(findings, elements))

        create_calls = [
            a for name, a in mcp.calls if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 1  # was 2 before the fix

        fix1 = result1["proposed_fixes"][0]
        assert fix1["executed"] is True
        fix2 = result2["proposed_fixes"][0]
        assert fix2["executed"] is False
        assert fix2["preview"]["skipped_duplicate"] is True

    @pytest.mark.asyncio
    async def test_different_element_set_creates_new_issue(self, autonomy) -> None:
        """Elements added/removed between iterations → a DIFFERENT group key
        → a new issue is correctly created (never suppressed)."""
        mcp = _default_mock()
        agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2)
        findings1 = [_finding(element_id="e1")]
        elements1 = [_room("e1", "Room 1")]
        findings2 = [_finding(element_id="e1"), _finding(element_id="e2")]
        elements2 = [_room("e1", "Room 1"), _room("e2", "Room 2")]

        await agent.run(_make_state(findings1, elements1))
        await agent.run(_make_state(findings2, elements2))

        create_calls = [
            a for name, a in mcp.calls if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 2  # different element set → new issue


class TestCrossRunIssueRegistry:
    """SPEC_SCHEDULED_AUDIT_DELTA.md W2b/2c — a fresh DesignAgent instance per
    "run" (mirrors two separate CLI/scheduled invocations), sharing the SAME
    mock ACC store + registry file. The in-run memo (``TestPathAGroupDedup``
    above) never engages here since each test constructs a NEW agent."""

    @pytest.mark.asyncio
    async def test_empty_registry_creates_issue_and_records_it(
        self, autonomy, tmp_path
    ) -> None:
        mcp = _default_mock()
        reg_path = tmp_path / "issue_registry.json"
        agent = DesignAgent(
            mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
            issue_registry=reg_path,
        )
        findings = [_finding(element_id="e1")]
        elements = [_room("e1", "Room 1")]
        result = await agent.run(_make_state(findings, elements))

        create_calls = [
            a for name, a in mcp.calls if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 1
        issue_id = result["proposed_fixes"][0]["preview"]["executed_issue"]["id"]

        from bim_orchestrator.issue_registry import IssueRegistry, group_key

        key = group_key("b.test", "room.department.required", "non_compliant", ["e1"])
        entry = IssueRegistry(reg_path).lookup(key)
        assert entry is not None
        assert entry["issue_id"] == issue_id

    @pytest.mark.asyncio
    async def test_registry_hit_with_open_status_skips_second_issue(
        self, autonomy, tmp_path
    ) -> None:
        mcp = _default_mock()
        reg_path = tmp_path / "issue_registry.json"
        findings = [_finding(element_id="e1")]
        elements = [_room("e1", "Room 1")]

        agent1 = DesignAgent(
            mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
            issue_registry=reg_path,
        )
        await agent1.run(_make_state(findings, elements))

        # A SEPARATE agent instance (simulating a later scheduled run) sees
        # the same still-open issue via the shared mock ACC store.
        agent2 = DesignAgent(
            mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
            issue_registry=reg_path,
        )
        result2 = await agent2.run(_make_state(findings, elements))

        create_calls = [
            a for name, a in mcp.calls if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 1  # still just the first run's issue
        fix2 = result2["proposed_fixes"][0]
        assert fix2["executed"] is False
        assert fix2["preview"]["skipped_cross_run"] is True
        assert fix2["preview"]["skipped_duplicate"] is True

    @pytest.mark.asyncio
    async def test_registry_hit_with_closed_status_creates_new_issue(
        self, autonomy, tmp_path
    ) -> None:
        mcp = _default_mock()
        reg_path = tmp_path / "issue_registry.json"
        findings = [_finding(element_id="e1")]
        elements = [_room("e1", "Room 1")]

        agent1 = DesignAgent(
            mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
            issue_registry=reg_path,
        )
        result1 = await agent1.run(_make_state(findings, elements))
        first_issue_id = result1["proposed_fixes"][0]["preview"]["executed_issue"]["id"]

        # Close the first issue on the shared mock ACC store — the problem is
        # considered resolved, so a reappearance should raise a FRESH issue.
        for issue in mcp.issues:
            if issue["id"] == first_issue_id:
                issue["status"] = "closed"

        agent2 = DesignAgent(
            mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
            issue_registry=reg_path,
        )
        result2 = await agent2.run(_make_state(findings, elements))

        create_calls = [
            a for name, a in mcp.calls if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 2
        fix2 = result2["proposed_fixes"][0]
        assert fix2["executed"] is True
        second_issue_id = fix2["preview"]["executed_issue"]["id"]
        assert second_issue_id != first_issue_id

        from bim_orchestrator.issue_registry import IssueRegistry, group_key

        key = group_key("b.test", "room.department.required", "non_compliant", ["e1"])
        entry = IssueRegistry(reg_path).lookup(key)
        assert entry["issue_id"] == second_issue_id

    @pytest.mark.asyncio
    async def test_get_issue_failure_is_fail_open_creates_issue_anyway(
        self, autonomy, tmp_path
    ) -> None:
        """A registry entry pointing at an issue the live ACC lookup can't
        resolve (network blip, issue deleted) must NOT swallow the warning —
        create the issue anyway (fail-open, same posture as design.py:169's
        rejection of blind dedup)."""
        from bim_orchestrator.issue_registry import IssueRegistry, group_key

        reg_path = tmp_path / "issue_registry.json"
        key = group_key("b.test", "room.department.required", "non_compliant", ["e1"])
        IssueRegistry(reg_path).record(key, {"issue_id": "issue-does-not-exist"})

        mcp = _default_mock()  # empty issues store → get_issue raises
        agent = DesignAgent(
            mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2,
            issue_registry=reg_path,
        )
        findings = [_finding(element_id="e1")]
        elements = [_room("e1", "Room 1")]
        result = await agent.run(_make_state(findings, elements))

        create_calls = [
            a for name, a in mcp.calls if name == "create_issue" and not a["dry_run"]
        ]
        assert len(create_calls) == 1
        assert result["proposed_fixes"][0]["executed"] is True

    @pytest.mark.asyncio
    async def test_issue_registry_none_never_touches_disk(
        self, autonomy, tmp_path
    ) -> None:
        """Legacy CLI path: no ``issue_registry`` kwarg → no file read/write,
        even though ``tmp_path`` is otherwise empty and writable."""
        mcp = _default_mock()
        agent = DesignAgent(mcp=mcp, autonomy=autonomy, project_id="b.test", max_issues=2)
        assert agent._issue_registry is None
        findings = [_finding(element_id="e1")]
        elements = [_room("e1", "Room 1")]
        await agent.run(_make_state(findings, elements))
        await agent.run(_make_state(findings, elements))  # would-be 2nd cross-run

        assert list(tmp_path.glob("*.json")) == []
