"""Tests for audit_report.render_audit_report — the verification report.

Proves GENERALITY: the same renderer produces a correct report across THREE
different requirement types (present_and_nonempty, numeric_compare,
relation_compare+lookup) with no rule-specific code, and honours the
non-negotiables — the PASS set is listed (false-negative defence), the recipe
degrades honestly for cross-element checks, the Design/Result outcome (Path A vs
Path B) is joined in, and the "what we did NOT touch" honesty section is present.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bim_orchestrator.audit_report import render_audit_report
from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill, RuleSet
from bim_orchestrator.report_trace import build_check_record

# ── builders ─────────────────────────────────────────────────────────────────


def mk_rule(requirement: str, **kw) -> Rule:
    defaults = dict(
        id=kw.pop("id", f"test.{requirement}"),
        parameter=kw.pop("parameter", "X"),
        requirement=requirement,
        severity_tag="rule_violation",
        description=kw.pop("description", f"{requirement} rule"),
        autofill=RuleAutofill(strategy="none"),
    )
    defaults.update(kw)
    return Rule(**defaults)  # type: ignore[arg-type]


def _el(eid, name, category, **params):
    return {"id": eid, "name": name, "category": category, "params": params}


def _fix(finding_id, *, executed=False, new_value=None, preview=None, autonomy="auto"):
    return {
        "finding_id": finding_id, "element_id": finding_id.split("::")[-1],
        "parameter": "X", "new_value": new_value, "autonomy": autonomy,
        "approval_token": None, "preview": preview or {}, "executed": executed,
    }


# Three rules, three requirement types.
R_PRESENT = mk_rule(
    "present_and_nonempty", id="rooms.department.required",
    parameter="Department", category="Rooms",
    description="Every room must carry a non-empty Department.",
)
R_NUMERIC = mk_rule(
    "numeric_compare", id="doors.width.min", parameter="Width", category="Doors",
    operator=">=", threshold=0.9, unit="m",
    description="Door clear width must be at least 0.9 m.",
)
R_RELATION = mk_rule(
    "relation_compare", id="doors.fire.ibc716", parameter="Fire Rating",
    category="Doors", operator=">=", lookup="ibc716", compare_kind="fire_rating",
    severity_level="severity_high",
    description=(
        "IBC 716: a fire door's rating must be at least the code-table value "
        "for its host wall."
    ),
)
RULESET = RuleSet(scenario="report_demo", target_category=["Rooms", "Doors"],
                  rules=[R_PRESENT, R_NUMERIC, R_RELATION])


def build_demo_state():
    """A realistic post-run state spanning all 3 requirement types + outcomes."""
    records = []
    # present_and_nonempty: 1 compliant, 1 missing_data (auto-filled via Path B)
    records.append(build_check_record(
        R_PRESENT, _el("1", "Lobby", "Rooms", Department="Public"),
        raw_value="Public", value="Public", passed=True, status="compliant"))
    records.append(build_check_record(
        R_PRESENT, _el("2", "Closet 11A", "Rooms", Department=""),
        raw_value="", value="", passed=False, status="missing_data",
        severity="severity_medium", suggested_value="Storage"))
    # numeric_compare: 1 compliant, 1 non_compliant (raised as Path A issue)
    records.append(build_check_record(
        R_NUMERIC, _el("100", "Door C", "Doors", Width=1.0),
        raw_value=1.0, value=1.0, passed=True, status="compliant"))
    records.append(build_check_record(
        R_NUMERIC, _el("101", "Door D", "Doors", Width=0.8),
        raw_value=0.8, value=0.8, passed=False, status="non_compliant",
        severity="severity_high"))
    # relation_compare+lookup: 1 compliant (operand captured), 1 manual_review
    records.append(build_check_record(
        R_RELATION, _el("102", "Door E", "Doors", **{"Fire Rating": "180 MIN"}),
        raw_value="180 MIN", value="180 MIN", passed=True, status="compliant",
        other_value="90 min", required="90 min"))
    records.append(build_check_record(
        R_RELATION, _el("103", "Door F", "Doors", **{"Fire Rating": "60 MIN"}),
        raw_value="60 MIN", value="60 MIN", passed=False, status="manual_review",
        severity="severity_high", other_value=None, required=None))

    summary = {"total": 6, "compliant": 3, "non_compliant": 1,
               "manual_review": 1, "missing_data": 1}

    fixes = [
        # present missing_data → Path B Revit write (Department inferred)
        _fix("rooms.department.required::2", executed=True, new_value="Storage",
             preview={"executed_via": "revit_batch", "old_value": "",
                      "data": {"changes": {"before": "", "after": "Storage"}},
                      "action": "set_parameter", "target": "instance",
                      "rule_id": "rooms.department.required"}),
        # numeric non_compliant → Path A grouped ACC issue
        _fix("rulegroup::doors.width.min::non_compliant", executed=True,
             preview={"executed_issue": {"id": "issue-mock-1", "displayId": 1001,
                      "title": "[AutoAudit] Doors -- doors.width.min", "status": "open"},
                      "grouped_rule_ids": ["doors.width.min"], "grouped_count": 1}),
        # relation manual_review → Path A grouped ACC issue
        _fix("rulegroup::doors.fire.ibc716::manual_review", executed=True,
             preview={"executed_issue": {"id": "issue-mock-2", "displayId": 1002,
                      "title": "[AutoAudit] Doors -- doors.fire.ibc716", "status": "open"},
                      "grouped_rule_ids": ["doors.fire.ibc716"], "grouped_count": 1}),
    ]
    return {
        "project_id": "b.demo", "iteration": 1, "max_iterations": 3,
        "elements": [], "findings": [], "proposed_fixes": fixes,
        "outcomes_summary": summary, "check_trace": records,
        "status": "converged", "error": None,
    }


def render_demo(**kw) -> str:
    state = build_demo_state()
    return render_audit_report(state, rules=RULESET, run_id="run-demo01",
                              mode="run-revit", duration_seconds=1.2, **kw)


# ── structure: all sections present ──────────────────────────────────────────


def test_report_has_all_sections():
    md = render_demo()
    assert "# Verification report — run-demo01" in md
    assert "## 1. Executive summary" in md
    assert "## 2. How to trust this report" in md
    assert "## 3. Per-rule verification" in md
    assert "## 4. Per-element appendix" in md
    assert "## 5. What we did NOT touch, and why" in md
    assert "## 6. Audit trail" in md


def test_executive_summary_scoreboard_lists_every_rule():
    md = render_demo()
    assert "`rooms.department.required`" in md
    assert "`doors.width.min`" in md
    assert "`doors.fire.ibc716`" in md
    # headline reflects 3 needing attention (1 nc + 1 mr + 1 md)
    assert "3 of 6 checks need attention" in md
    # v1.5-R6 (2.1a): "Elements checked" renamed to the Coverage block's
    # "evaluated" (paired with "fetched" for the fetched-vs-evaluated gap).
    assert "evaluated: **6**" in md


# ── generality: each requirement type gets the right recipe ──────────────────


def test_present_rule_has_native_has_no_value_filter():
    md = render_demo()
    assert "### Rule `rooms.department.required`" in md
    assert "has no value" in md  # native view filter for presence


def test_numeric_rule_has_native_less_than_filter():
    md = render_demo()
    # pass is ">= 0.9", so the FAIL filter is "is less than 0.9"
    assert "is less than 0.9" in md


def test_relation_rule_degrades_honestly():
    md = render_demo()
    assert "shows the inputs, not the verdict" in md
    # cross-element check must NOT pretend a single filter verifies it
    assert "No single native schedule or filter shows both sides" in md


# ── the heart: PASS set is listed (false-negative defence) ───────────────────


def test_pass_set_lists_compliant_elements():
    md = render_demo()
    assert "PASS set" in md
    # compliant elements appear so a Director can audit for silently-passed wrongs
    assert "Lobby" in md          # present compliant
    assert "Door C" in md         # numeric compliant
    assert "Door E" in md         # relation compliant
    # the relation PASS row shows the operand it passed against (required 90 min)
    assert "90 min" in md


def test_relation_compliant_does_not_show_exempt_when_required_met():
    md = render_demo()
    # Door E met an actual requirement (180 MIN >= 90 min), so it's a plain pass
    assert "180 MIN" in md


# ── Design/Result outcome join (Path A vs Path B) ────────────────────────────


def test_path_b_write_outcome_shown():
    md = render_demo()
    assert "Revit write" in md
    assert "Storage" in md  # before "" -> after "Storage"


def test_path_a_issue_outcome_shown():
    md = render_demo()
    assert "ACC Issue #1001" in md  # numeric non_compliant grouped issue
    assert "ACC Issue #1002" in md  # relation manual_review grouped issue


# ── honesty section ──────────────────────────────────────────────────────────


def test_honesty_section_reports_missing_and_manual_counts():
    md = render_demo()
    assert "Missing data (1)" in md
    assert "Needs human review (1)" in md
    assert "data_quality_report.md" in md
    assert "review_queue.md" in md


def test_select_by_id_lists_revit_element_ids():
    md = render_demo()
    assert "Select by ID" in md
    # numeric rule doors 100 + 101 are integer Revit ids → usable in Select-by-ID
    assert "100" in md and "101" in md


# ── robustness ───────────────────────────────────────────────────────────────


def test_renders_without_ruleset_using_record_fallback():
    """A later re-render may not have the live RuleSet; the report still renders
    from the self-contained trace (recipe derived from the record's anatomy)."""
    state = build_demo_state()
    md = render_audit_report(state, rules=None, run_id="run-norules")
    assert "## 3. Per-rule verification" in md
    assert "`doors.fire.ibc716`" in md
    # relation recipe still degrades correctly off the record's operand_source
    assert "shows the inputs, not the verdict" in md


def test_unknown_requirement_still_renders():
    # An unknown requirement can't be a pydantic Rule (Literal); use a duck-typed
    # rule to build the record, then render WITHOUT a RuleSet so the renderer
    # rebuilds a rule-view from the self-contained record and falls back cleanly.
    rule = SimpleNamespace(
        id="x.future", parameter="Foo", requirement="some_future_requirement",
        category="Walls",
    )
    rec = build_check_record(rule, _el("9", "Wall 9", "Walls", Foo="bar"),
                             raw_value="bar", value="bar", passed=False,
                             status="non_compliant", severity="severity_low")
    state = {
        "project_id": "p", "iteration": 0, "max_iterations": 1, "elements": [],
        "findings": [], "proposed_fixes": [],
        "outcomes_summary": {"total": 1, "compliant": 0, "non_compliant": 1,
                             "manual_review": 0, "missing_data": 0},
        "check_trace": [rec], "status": "converged", "error": None,
    }
    md = render_audit_report(state, rules=None, run_id="run-x")
    assert "`x.future`" in md
    assert "Select by ID" in md  # default recipe still anchors on ElementId


def test_empty_state_renders_without_crash():
    state = {
        "project_id": "p", "iteration": 0, "max_iterations": 1, "elements": [],
        "findings": [], "proposed_fixes": [], "outcomes_summary": {},
        "check_trace": [], "status": "converged", "error": None,
    }
    md = render_audit_report(state, rules=None, run_id="run-empty")
    assert "# Verification report — run-empty" in md
    assert "nothing checked" in md.lower() or "No parameter checks" in md


def test_finish_run_recording_writes_verification_artifacts(tmp_path, monkeypatch):
    """The orchestrator hook writes verification_report.md + report_trace.json
    into the run folder alongside the existing artifacts (additive)."""
    import json

    import bim_orchestrator.orchestrator as orch
    from bim_orchestrator.run_recorder import RunFolder, TraceCollector

    # Keep the cross-run trend write hermetic (it scans DEFAULT_RUNS_DIR).
    monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path)
    folder = RunFolder.create(tmp_path, mode="run-revit")
    collector = TraceCollector(run_id=folder.run_id)
    token = collector.activate()

    state = build_demo_state()
    orch._finish_run_recording(
        folder, collector, token, state, status="converged", rules=RULESET
    )

    vr = folder.root / "verification_report.md"
    rt = folder.root / "report_trace.json"
    assert vr.exists() and rt.exists()
    # legacy artifacts still written (additive, not a replacement)
    assert (folder.root / "report.md").exists()

    body = vr.read_text(encoding="utf-8")
    assert "Verification report" in body
    assert "PASS set" in body
    records = json.loads(rt.read_text(encoding="utf-8"))
    assert len(records) == 6  # the structured trace persisted for re-render


def test_report_trace_json_is_unindented(tmp_path, monkeypatch):
    """3.4 hygiene: report_trace.json is a machine-read artifact, not
    hand-edited — no indent keeps it smaller."""
    import bim_orchestrator.orchestrator as orch
    from bim_orchestrator.run_recorder import RunFolder, TraceCollector

    monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path)
    folder = RunFolder.create(tmp_path, mode="run-revit")
    collector = TraceCollector(run_id=folder.run_id)
    token = collector.activate()
    state = build_demo_state()
    orch._finish_run_recording(
        folder, collector, token, state, status="converged", rules=RULESET
    )
    raw = (folder.root / "report_trace.json").read_text(encoding="utf-8")
    assert "\n" not in raw.strip()


def test_finish_run_recording_writes_checksum_sidecar_matching_the_file(
    tmp_path, monkeypatch
):
    """2.6: verification_report.sha256 hashes the EXACT bytes written."""
    import hashlib

    import bim_orchestrator.orchestrator as orch
    from bim_orchestrator.run_recorder import RunFolder, TraceCollector

    monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path)
    folder = RunFolder.create(tmp_path, mode="run-revit")
    collector = TraceCollector(run_id=folder.run_id)
    token = collector.activate()

    state = build_demo_state()
    orch._finish_run_recording(
        folder, collector, token, state, status="converged", rules=RULESET
    )

    vr = folder.root / "verification_report.md"
    sidecar = folder.root / "verification_report.sha256"
    assert sidecar.exists()
    expected = hashlib.sha256(vr.read_bytes()).hexdigest()
    assert expected in sidecar.read_text(encoding="utf-8")
    assert "Integrity: SHA-256 in sidecar" in vr.read_text(encoding="utf-8")


def test_finish_run_recording_captures_provenance_and_rules_hash(tmp_path, monkeypatch):
    """2.2: rules_paths threaded through → the report's Provenance section
    names the rules file and its hash; renderer never computes it itself."""
    import bim_orchestrator.orchestrator as orch
    from bim_orchestrator.run_recorder import RunFolder, TraceCollector

    rules_file = tmp_path / "rules.demo.yaml"
    rules_file.write_text("scenario: demo\n", encoding="utf-8")

    monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    folder = RunFolder.create(tmp_path / "runs", mode="run-revit")
    collector = TraceCollector(run_id=folder.run_id)
    token = collector.activate()

    state = build_demo_state()
    orch._finish_run_recording(
        folder, collector, token, state, status="converged", rules=RULESET,
        rules_paths=rules_file, max_elements=300,
    )

    body = (folder.root / "verification_report.md").read_text(encoding="utf-8")
    assert "## 0. Provenance" in body
    assert "rules.demo.yaml" in body
    assert "SHA-256" in body
    assert "Elements fetched" in body
    assert "cap `--max-elements`=300" in body


def test_finish_run_recording_threads_demo_banner(tmp_path, monkeypatch):
    """2.4: a caller-supplied banner (e.g. --demo's notice) renders right
    under the title."""
    import bim_orchestrator.orchestrator as orch
    from bim_orchestrator.run_recorder import RunFolder, TraceCollector

    monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path)
    folder = RunFolder.create(tmp_path, mode="run-revit")
    collector = TraceCollector(run_id=folder.run_id)
    token = collector.activate()

    state = build_demo_state()
    orch._finish_run_recording(
        folder, collector, token, state, status="converged", rules=RULESET,
        banner="DEMO MODE — simulated model",
    )
    body = (folder.root / "verification_report.md").read_text(encoding="utf-8")
    lines = body.splitlines()
    title_idx = next(i for i, l in enumerate(lines) if l.startswith("# Verification report"))
    banner_idx = next(i for i, l in enumerate(lines) if "DEMO MODE" in l)
    assert banner_idx > title_idx
    assert banner_idx - title_idx <= 3  # right under the title, not buried


def test_geometry_findings_get_their_own_section():
    state = build_demo_state()
    state["geometry_findings"] = [{
        "rule_id": "geo.clearance", "element_id": "500", "element_name": "Duct 5",
        "severity": "severity_high", "message": "Clearance 40mm < 100mm required",
    }]
    md = render_audit_report(state, rules=RULESET, run_id="run-geo")
    assert "Geometry / clearance findings" in md
    assert "geo.clearance" in md
    assert "Interference Check" in md


# ── v1.5-R6 wave 1: join-hardening + clarity ─────────────────────────────────


def test_geometry_finding_outcome_joins_by_rule_status_bucket():
    """1.1e: a geometry finding's Path A group issue (folded in by DesignAgent
    at (rule_id, status)) shows up as an Outcome column, not silence."""
    state = build_demo_state()
    state["geometry_findings"] = [{
        "rule_id": "geo.clearance", "element_id": "500", "element_name": "Duct 5",
        "severity": "severity_high", "message": "Clearance 40mm < 100mm required",
        "status": "non_compliant",
    }]
    state["proposed_fixes"] = list(state["proposed_fixes"]) + [
        _fix("rulegroup::geo.clearance::non_compliant", executed=True, preview={
            "executed_issue": {"id": "issue-mock-9", "displayId": 2001, "status": "open"},
            "grouped_rule_ids": ["geo.clearance"], "grouped_count": 1,
        })
    ]
    md = render_audit_report(state, rules=RULESET, run_id="run-geo-outcome")
    assert "| Rule | Element | ElementId | Severity | Detail | Outcome |" in md
    assert "ACC Issue #2001" in md


def test_geometry_finding_with_no_fix_says_no_action_not_bare_dash():
    """1.1f: a geometry finding nobody proposed a fix for must render an
    explanatory phrase, never a bare em-dash."""
    state = build_demo_state()
    state["geometry_findings"] = [{
        "rule_id": "geo.orphan", "element_id": "600", "element_name": "Beam 6",
        "severity": "severity_low", "message": "Unresolved clearance",
        "status": "non_compliant",
    }]
    md = render_audit_report(state, rules=RULESET, run_id="run-geo-noaction")
    assert "no action recorded" in md
    # the geometry row's Outcome cell is never a lone dash
    for line in md.splitlines():
        if "geo.orphan" in line:
            assert "| — |" not in line
            assert not line.rstrip().endswith("| — |")


def test_auto_fixed_rule_shows_bucket_and_table_not_nothing_flagged():
    """1.2: a rule whose failing element got auto-fixed (now compliant in the
    final check_trace) must say so explicitly, not 'Nothing flagged'."""
    rule = mk_rule(
        "matches_regex", id="doors.mark.naming", parameter="Mark",
        category="Doors", pattern=r"^D_\d+$",
    )
    ruleset = RuleSet(scenario="autofix_demo", target_category="Doors", rules=[rule])
    rec = build_check_record(
        rule, _el("707", "D_105", "Doors", Mark="D_105"),
        raw_value="D_105", value="D_105", passed=True, status="compliant",
    )
    fixes = [_fix("doors.mark.naming::707", executed=True, new_value="D_105", preview={
        "executed_via": "revit_batch", "old_value": "D 105",
        "data": {"changes": {"before": "D 105", "after": "D_105"}},
        "action": "set_parameter", "target": "instance",
        "rule_id": "doors.mark.naming",
    })]
    state = {
        "project_id": "p", "iteration": 1, "max_iterations": 3, "elements": [],
        "findings": [], "proposed_fixes": fixes,
        "outcomes_summary": {"total": 1, "compliant": 1, "non_compliant": 0,
                             "manual_review": 0, "missing_data": 0},
        "check_trace": [rec], "status": "converged", "error": None,
    }
    md = render_audit_report(state, rules=ruleset, run_id="run-autofix")
    assert "Nothing flagged" not in md
    assert "0 outstanding — 1 auto-fixed this run" in md
    assert "D 105 → D_105" in md
    assert "**Auto-fixed (1):**" in md
    assert "1 auto-fixed" in md  # per-rule Outcome line bucket


def test_select_by_id_defaults_to_flagged_set_with_full_scope_line():
    """1.4: the paste-ready Select-by-ID list is the FLAGGED (non-compliant)
    set, not the full PASS+FAIL scope — the full scope is offered separately."""
    md = render_demo()
    # doors.width.min: door 100 compliant, door 101 non_compliant.
    lines = md.splitlines()
    select_lines = [l for l in lines if l.strip().startswith("- **Select by ID:**")]
    numeric_select = next(l for l in select_lines if "101" in l)
    assert "100" not in numeric_select  # compliant door not in the default paste
    full_scope = [l for l in lines if "Full scope (audit false negatives)" in l]
    assert full_scope  # offered for the rules that have a PASS set too
    assert any("100" in l and "101" in l for l in full_scope)


def test_no_bare_dash_outcome_cells_anywhere():
    """1.1f: every '—'-bearing table cell must carry an explanation, never a
    lone dash — grep-level honesty check across the whole rendered report."""
    md = render_demo()
    for line in md.splitlines():
        if line.strip() in ("| — |", "—"):
            raise AssertionError(f"bare dash cell: {line!r}")


# ── Nghiệm thu #5: back-compat with a PRE-v1.5-R6 report_trace.json ─────────


def test_renders_pre_v15r6_check_trace_without_the_new_fields():
    """A report_trace.json written by an OLDER version of this module lacks
    every field this spec added (revit_parameter on records,
    skipped_out_of_scope on the summary, etc.) — render_audit_report must
    still produce a report, not KeyError/AttributeError. Simulates loading
    such a trace straight off disk (plain dicts, no CheckRecord construction
    helper — the shape a bare `json.load()` would actually produce)."""
    old_style_trace = [
        {
            "rule_id": "legacy.rule",
            "element_id": "1",
            "parameter": "Department",
            "requirement": "present_and_nonempty",
            "raw_value": "Public",
            "value": "Public",
            "passed": True,
            "status": "compliant",
            # NOTE: no revit_parameter, no element_name, no category — exactly
            # what a pre-R6 build_check_record produced.
        },
        {
            "rule_id": "legacy.rule",
            "element_id": "2",
            "parameter": "Department",
            "requirement": "present_and_nonempty",
            "raw_value": None,
            "value": None,
            "passed": False,
            "status": "missing_data",
        },
    ]
    state = {
        "project_id": "p", "iteration": 1, "max_iterations": 3, "elements": [],
        "findings": [], "proposed_fixes": [],
        # NOTE: no skipped_out_of_scope key — a pre-R6 outcomes_summary.
        "outcomes_summary": {"total": 2, "compliant": 1, "non_compliant": 0,
                             "manual_review": 0, "missing_data": 1},
        "check_trace": old_style_trace, "status": "converged", "error": None,
    }
    # rules=None forces the record-fallback path (rule_view_from_record),
    # exactly like re-rendering from a saved trace with no RuleSet in hand.
    md = render_audit_report(state, rules=None, run_id="run-legacy-trace")
    assert "`legacy.rule`" in md
    assert "Verification report" in md
    assert "1. Executive summary" in md
    # Coverage line still renders (0 skipped, not a crash) even though the
    # summary dict never had the key.
    assert "Skipped as out-of-scope" not in md  # 0 → line simply omitted
    assert "evaluated: **2**" in md


# ── v1.5-R6 polish round 2 (Director re-review): N1-N6 ──────────────────────


def test_n1_awaiting_approval_dedups_reproposed_write_set():
    """N1: an iteration-1 memo-hit re-propose stamps the SAME
    proposal_issue_id onto a FRESH set of ProposedFix objects that share the
    exact same write (element/action/parameter/new_value) as iteration 0's —
    both copies land in proposed_fixes. §5's 'Awaiting approval' count must
    dedup by write-set identity, not len(list)."""
    rule = mk_rule(
        "canonical_format", id="doors.fire.rating", parameter="Fire Rating",
        category="Doors",
    )
    ruleset = RuleSet(scenario="n1_demo", target_category="Doors", rules=[rule])
    records = [
        build_check_record(
            rule, _el(str(700 + i), f"Door {i}", "Doors", **{"Fire Rating": ""}),
            raw_value="", value="", passed=False, status="missing_data",
            severity="severity_high", suggested_value="2 HR",
        )
        for i in range(4)
    ]

    def _proposal_fix(eid: str) -> dict:
        return _fix(
            f"doors.fire.rating::{eid}", executed=False, new_value="2 HR",
            preview={
                "proposal_issue_id": "issue-mock-0001", "action": "set_parameter",
                "target": "instance", "rule_id": "doors.fire.rating",
                "old_value": "",
            },
        )

    iter0_fixes = [_proposal_fix(str(700 + i)) for i in range(4)]
    # iteration 1 re-proposes the SAME 4 writes (memo-hit) — fresh dict
    # objects, same write-set, same stamped proposal_issue_id.
    iter1_fixes = [_proposal_fix(str(700 + i)) for i in range(4)]

    state = {
        "project_id": "p", "iteration": 2, "max_iterations": 3, "elements": [],
        "findings": [], "proposed_fixes": iter0_fixes + iter1_fixes,
        "outcomes_summary": {"total": 4, "compliant": 0, "non_compliant": 0,
                             "manual_review": 0, "missing_data": 4},
        "check_trace": records, "status": "converged", "error": None,
    }
    md = render_audit_report(state, rules=ruleset, run_id="run-n1")
    assert "**Awaiting approval (4):**" in md
    assert "**Awaiting approval (8):**" not in md


def test_n2_grouped_path_a_issue_names_the_real_rule_not_rulegroup():
    """N2: a Path A grouped proposal's finding_id is
    "rulegroup::<rule_id>::<bucket>" — the Executive Summary's 'Issues by
    rule' line must name the real rule (with a working anchor), never the
    literal word "rulegroup"."""
    md = render_demo()
    assert "[`rulegroup`]" not in md
    assert "[`doors.width.min`](#rule-doors-width-min) (#1001)" in md


def test_n3_numeric_filter_shows_rule_unit_and_rounded_storage_value():
    """N3: a numeric_compare schedule filter's value is in the parameter's
    Revit STORAGE unit — show the rule's own threshold+unit alongside the
    storage value (rounded to 4 decimals), not the bare internal float."""
    rule = mk_rule(
        "numeric_compare", id="doors.width.min2", parameter="Width",
        category="Doors", operator=">=", threshold=900.0, unit="mm",
    )
    ruleset = RuleSet(scenario="n3_demo", target_category="Doors", rules=[rule])
    rec = build_check_record(
        rule, _el("707", "Door Narrow", "Doors", Width=610),
        raw_value=610, value=610, passed=False, status="non_compliant",
        severity="severity_high",
    )
    state = {
        "project_id": "p", "iteration": 1, "max_iterations": 3, "elements": [],
        "findings": [], "proposed_fixes": [],
        "outcomes_summary": {"total": 1, "compliant": 0, "non_compliant": 1,
                             "manual_review": 0, "missing_data": 0},
        "check_trace": [rec], "status": "converged", "error": None,
    }
    md = render_audit_report(state, rules=ruleset, run_id="run-n3")
    assert "900.0 mm (= 2.9528 internal/storage units — auto-schedule uses raw values)" in md


def test_n4_provenance_timestamps_are_utc_and_project_line_not_duplicated():
    """N4: Started/Finished must carry the same UTC tag as Captured at (no
    unlabelled local-vs-UTC mismatch), and Project id / Project must not
    both appear with the identical value."""
    md = render_demo(
        provenance={
            "tool_version": "0.1.0", "user": "lep", "machine": "HOST1",
            "captured_at": "2026-07-07T14:22:13+00:00 (UTC)",
            "project_id": "b.demo", "run_id": "run-demo01",
            "elements_fetched": 6, "rules_files": [],
        },
        started_at="2026-07-07T21:22:12+00:00 (UTC)",
        finished_at="2026-07-07T21:22:13+00:00 (UTC)",
    )
    assert "Captured at:** 2026-07-07T14:22:13+00:00 (UTC)" in md
    assert "Started:** 2026-07-07T21:22:12+00:00 (UTC)" in md
    assert "Finished:** 2026-07-07T21:22:13+00:00 (UTC)" in md
    # exactly one line carries the project id value (no duplicate Project /
    # Project id pair).
    project_lines = [
        l for l in md.splitlines()
        if l.strip().startswith("- **Project") and "b.demo" in l
    ]
    assert len(project_lines) == 1
    assert "Project id:** b.demo" in project_lines[0]


def test_n5_hash_lines_carry_a_verify_hint():
    """N5: both the rules-file SHA-256 line and the bottom integrity note
    must be followed by a copy-pasteable verification command."""
    md = render_demo(
        provenance={
            "tool_version": "0.1.0", "user": "lep", "machine": "HOST1",
            "captured_at": "2026-07-07T14:22:13+00:00 (UTC)",
            "project_id": "b.demo", "run_id": "run-demo01",
            "elements_fetched": 6,
            "rules_files": [{"path": "config/rules.demo.yaml", "sha256": "abc123"}],
        },
    )
    assert md.count("Get-FileHash") == 2
    assert "Verify: PowerShell `Get-FileHash <file> -Algorithm SHA256`" in md


def test_provenance_document_renders_model_and_revit_lines():
    """SPEC_DOCUMENT_IDENTITY_STAMP: a provenance with a `document` block
    renders §0's Model + Revit lines."""
    md = render_demo(
        provenance={
            "tool_version": "0.1.0", "user": "lep", "machine": "HOST1",
            "captured_at": "2026-07-07T14:22:13+00:00 (UTC)",
            "project_id": "b.demo", "run_id": "run-demo01",
            "elements_fetched": 6, "rules_files": [],
            "document": {
                "title": "SampleTower.rvt",
                "path": "C:\\models\\SampleTower.rvt",
                "revit_version_name": "Autodesk Revit 2026",
                "is_workshared": True,
                "is_modified": False,
            },
        },
    )
    assert "**Model:** `SampleTower.rvt` — `C:\\models\\SampleTower.rvt`" in md
    assert "**Revit:** Autodesk Revit 2026" in md
    assert "**Workshared:** yes" in md


def test_provenance_document_modified_warns_unsaved_changes():
    md = render_demo(
        provenance={
            "tool_version": "0.1.0", "user": "lep", "machine": "HOST1",
            "captured_at": "2026-07-07T14:22:13+00:00 (UTC)",
            "project_id": "b.demo", "run_id": "run-demo01",
            "elements_fetched": 6, "rules_files": [],
            "document": {"title": "Model.rvt", "is_modified": True},
        },
    )
    assert "Unsaved changes" in md


def test_provenance_without_document_is_byte_identical():
    """Additive contract: omitting `document` from provenance must not
    change the rendered report at all (pre-spec provenance still parses)."""
    kw = dict(
        provenance={
            "tool_version": "0.1.0", "user": "lep", "machine": "HOST1",
            "captured_at": "2026-07-07T14:22:13+00:00 (UTC)",
            "project_id": "b.demo", "run_id": "run-demo01",
            "elements_fetched": 6, "rules_files": [],
        },
    )
    md_no_document_key = render_demo(**kw)
    kw["provenance"]["document"] = None
    md_explicit_none = render_demo(**kw)
    assert md_no_document_key == md_explicit_none
    assert "Model:**" not in md_no_document_key


def test_n6_pass_set_row_and_appendix_annotate_auto_fixed_element():
    """N6: an element that re-queries as compliant only because THIS run
    auto-fixed it must say so in both the PASS-set row and the per-element
    appendix's QC verdict — never a bare, unqualified 'Compliant'/'✓ pass'."""
    rule = mk_rule(
        "present_and_nonempty", id="rooms.department.required2",
        parameter="Department", category="Rooms",
    )
    ruleset = RuleSet(scenario="n6_demo", target_category="Rooms", rules=[rule])
    rec = build_check_record(
        rule, _el("401", "Guest Bedroom", "Rooms", Department="General"),
        raw_value="General", value="General", passed=True, status="compliant",
    )
    fixes = [_fix(
        "rooms.department.required2::401", executed=True, new_value="General",
        preview={
            "executed_via": "revit_batch", "old_value": "",
            "data": {"changes": {"before": "", "after": "General"}},
            "action": "set_parameter", "target": "instance",
            "rule_id": "rooms.department.required2",
        },
    )]
    state = {
        "project_id": "p", "iteration": 1, "max_iterations": 3, "elements": [],
        "findings": [], "proposed_fixes": fixes,
        "outcomes_summary": {"total": 1, "compliant": 1, "non_compliant": 0,
                             "manual_review": 0, "missing_data": 0},
        "check_trace": [rec], "status": "converged", "error": None,
    }
    md = render_audit_report(state, rules=ruleset, run_id="run-n6")
    assert "✓ pass (auto-fixed this run: (empty) → General)" in md
    assert "Compliant (after auto-fix)" in md
    # a bare unqualified "| Compliant |" for this element must not appear
    for line in md.splitlines():
        if "Guest Bedroom" in line and "401" in line and "|" in line:
            assert "| Compliant |" not in line


# ── v1.5-R7: cap-hit honesty ─────────────────────────────────────────────────


def test_iteration_cap_stop_reason_renders_warning_under_status_line():
    state = build_demo_state()
    state["status"] = "failed"
    state["stop_reason"] = "iteration_cap"
    state["max_iterations"] = 1
    md = render_audit_report(state, rules=RULESET, run_id="run-cap")
    assert "Stopped at the iteration cap" in md
    assert "max_iterations=1" in md
    assert "Fix interactions" in md
    # right under the status line, inside §1 Executive summary — not buried
    idx_headline = md.index("Headline:")
    idx_cap = md.index("Stopped at the iteration cap")
    idx_coverage = md.index("**Coverage:**")
    assert idx_headline < idx_cap < idx_coverage


def test_normal_convergence_has_no_cap_warning():
    state = build_demo_state()
    state["stop_reason"] = "fingerprint_stable"
    md = render_demo()
    assert "Stopped at the iteration cap" not in md
    assert render_audit_report(state, rules=RULESET, run_id="run-normal").count(
        "Stopped at the iteration cap"
    ) == 0


# ── P1-07: terminal status + query coverage must reach the REPORT ────────────


def test_final_status_uses_the_recorded_status_not_the_loop_state():
    """The report and metadata.json must describe one run the same way.

    `state["status"]` is the graph's internal loop state — after `check()` QC
    leaves it on "designing" while the run recorder writes "completed", so an
    auditor comparing the two artifacts saw a contradiction.
    """
    md = render_demo(run_status="completed")
    assert "**Final status:** completed" in md


def test_final_status_falls_back_to_state_for_direct_callers():
    md = render_demo()
    assert "**Final status:**" in md


def test_no_audit_run_says_so_in_the_report():
    """A run whose categories all failed to resolve read as a full audit —
    the coverage record reached metadata.json and the exit code, never the PDF
    the person signing actually reads."""
    state = build_demo_state()
    state["query_coverage"] = {
        "targets_requested": ["Bananas"], "categories_resolved": [],
        "categories_dropped": [{"category": "Bananas", "reason": "unresolved_on_revit"}],
        "rule_count": 1,
    }
    md = render_audit_report(state, rules=RULESET, run_id="r", run_status="no_audit")
    assert "NO AUDIT WAS PERFORMED" in md
    assert "must not" in md          # explicit "don't sign this" wording
    assert "Bananas" in md
    assert "unresolved_on_revit" in md


def test_partial_coverage_names_the_dropped_categories():
    state = build_demo_state()
    state["query_coverage"] = {
        "targets_requested": ["Doors", "Pipes"], "categories_resolved": ["Doors"],
        "categories_dropped": [{"category": "Pipes", "reason": "unresolved_on_revit"}],
        "rule_count": 2,
    }
    md = render_audit_report(state, rules=RULESET, run_id="r")
    assert "PARTIAL COVERAGE" in md
    assert "Pipes" in md


def test_full_coverage_adds_no_warning_banner():
    state = build_demo_state()
    state["query_coverage"] = {
        "targets_requested": ["Doors"], "categories_resolved": ["Doors"],
        "categories_dropped": [], "rule_count": 1,
    }
    md = render_audit_report(state, rules=RULESET, run_id="r")
    assert "Query coverage" in md
    assert "PARTIAL COVERAGE" not in md
    assert "NO AUDIT WAS PERFORMED" not in md


def test_legacy_run_without_coverage_renders_unchanged():
    # Forma-only / pre-R7 runs carry no query_coverage — must not crash or
    # invent a coverage line.
    md = render_demo()
    assert "Query coverage" not in md


class TestMalformedCoverageDegradesLoudly:
    """A-02 (2026-07-25 live review) — the coverage block was written that
    same day and had only ever met well-formed input.

    ``categories_dropped`` holding strings instead of dicts raised
    AttributeError; a non-list ``targets_requested`` raised TypeError. Either
    one killed the WHOLE report. Coverage is the section that says whether the
    audit covered the model at all, so the failure mode was: malformed scope
    data destroys the very document that would have disclosed it.

    Two properties, and the second is the one that matters: never crash, and
    never fall SILENT. Quietly skipping the block would leave a report that
    reads like a full-scope audit — the exact false assurance the block exists
    to prevent.
    """

    @staticmethod
    def _render(coverage):
        return render_audit_report({
            "check_trace": [], "findings": [], "proposed_fixes": [],
            "outcomes_summary": {"total": 0, "compliant": 0, "non_compliant": 0,
                                 "manual_review": 0, "missing_data": 0},
            "query_coverage": coverage,
        })

    @pytest.mark.parametrize("coverage", [
        {"targets_requested": 3, "categories_resolved": [], "categories_dropped": []},
        {"targets_requested": ["Doors"], "categories_resolved": [],
         "categories_dropped": ["Topography"]},
        {"targets_requested": "Doors", "categories_resolved": [],
         "categories_dropped": []},
        {"targets_requested": ["Doors"], "categories_resolved": None,
         "categories_dropped": [{"category": "X", "reason": "y"}, "bare-string"]},
        "not-a-mapping",
        ["also", "not", "a", "mapping"],
    ])
    def test_never_crashes(self, coverage):
        md = self._render(coverage)
        assert isinstance(md, str) and md

    @pytest.mark.parametrize("coverage", [
        {"targets_requested": 3, "categories_resolved": [], "categories_dropped": []},
        {"targets_requested": "Doors", "categories_resolved": [],
         "categories_dropped": []},
        "not-a-mapping",
    ])
    def test_malformed_shape_is_disclosed_not_swallowed(self, coverage):
        md = self._render(coverage)
        assert "COVERAGE DATA IS MALFORMED" in md
        assert "CANNOT be certified" in md

    def test_the_defect_message_names_the_field_and_type(self):
        # It goes straight into a document a human reads — "malformed" alone
        # gives whoever has to fix it nothing to go on.
        md = self._render({"targets_requested": 3, "categories_resolved": [],
                           "categories_dropped": []})
        assert "targets_requested" in md
        assert "int" in md

    def test_a_bare_string_is_not_iterated_character_by_character(self):
        # The nastiest shape: a str IS a Sequence with a len(), so a naive
        # guard would happily render one "category" per character.
        md = self._render({"targets_requested": "Doors", "categories_resolved": [],
                           "categories_dropped": []})
        assert "COVERAGE DATA IS MALFORMED" in md
        assert "requested: **5**" not in md

    def test_dropped_entry_as_a_bare_string_still_reports_the_category(self):
        # Tolerated rather than fatal: losing the REASON is survivable,
        # losing the whole report is not. The category must still surface,
        # because that is what tells the reader what went unaudited.
        md = self._render({
            "targets_requested": ["Doors", "Topography"],
            "categories_resolved": ["Doors"],
            "categories_dropped": ["Topography"],
        })
        assert "PARTIAL COVERAGE" in md
        assert "Topography" in md
        assert "COVERAGE DATA IS MALFORMED" not in md

    def test_well_formed_coverage_is_completely_unchanged(self):
        md = self._render({
            "targets_requested": ["Doors", "Topography"],
            "categories_resolved": ["Doors"],
            "categories_dropped": [
                {"category": "Topography", "reason": "unresolved_on_revit"}
            ],
        })
        assert "requested: **2**" in md
        assert "resolved: **1**" in md
        assert "PARTIAL COVERAGE" in md
        assert "unresolved_on_revit" in md
        assert "COVERAGE DATA IS MALFORMED" not in md

    def test_no_audit_banner_survives_the_hardening(self):
        # The strongest statement the block makes — nothing resolved at all.
        md = self._render({
            "targets_requested": ["Doors"],
            "categories_resolved": [],
            "categories_dropped": [{"category": "Doors", "reason": "unknown_label"}],
        })
        assert "NO AUDIT WAS PERFORMED" in md
