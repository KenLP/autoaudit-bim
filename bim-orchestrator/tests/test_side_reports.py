"""Tests for v1 task CC: side-report renderers + ACC issue gate.

Covers two things:

1. Pure-function renderers in `bim_orchestrator.reports` produce well-formed
   Markdown for the manual_review and missing_data buckets, including the
   empty-bucket case (still a valid file with a "no items" notice).

2. The implicit gate -- DesignAgent must only operate on
   `state["findings"]` (= non_compliant after BB). Populating
   `manual_review_items` and `missing_data_items` must NOT cause extra
   ACC Issues. This locks in the BB+CC contract before more agents start
   reading those buckets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.reports import (
    _compliance_pct,
    _top_findings,
    render_data_quality_report,
    render_per_run_report,
    render_review_queue,
    render_trend_report,
    write_side_reports,
    write_trend_report,
)
from bim_orchestrator.state import Finding, OrchestratorState
from tests._mocks import MockFormaMCPClient


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTONOMY_PATH = REPO_ROOT / "config" / "autonomy.yaml"


def _finding(
    *,
    rule_id: str,
    element_id: str,
    parameter: str = "Department",
    severity: str = "severity_medium",
    severity_tag: str = "missing_required_param",
    status: str = "non_compliant",
    message: str | None = None,
    citation: str | None = None,
    citation_missing: bool | None = None,
) -> Finding:
    f: Finding = {  # type: ignore[typeddict-item]
        "rule_id": rule_id,
        "element_id": element_id,
        "parameter": parameter,
        "severity_tag": severity_tag,
        "severity": severity,
        "message": message or f"{rule_id} on {element_id}",
        "suggested_value": None,
        "citation": citation,
        "status": status,
    }
    if citation_missing is not None:
        f["citation_missing"] = citation_missing
    return f


# ── render_review_queue ─────────────────────────────────────────────────────


def test_review_queue_empty_produces_valid_markdown():
    md = render_review_queue([], iteration=0, project_id="p1")
    assert md.startswith("# Manual Review Queue")
    assert "No manual-review items" in md
    # Should not have a table when empty
    assert "| # |" not in md


def test_review_queue_renders_header_metadata():
    md = render_review_queue([], iteration=3, project_id="b.test-project")
    assert "Project: `b.test-project`" in md
    assert "Run iteration: 3" in md
    assert "Generated:" in md


def test_review_queue_sorts_by_severity_high_first():
    items = [
        _finding(rule_id="rule.a", element_id="e1", severity="severity_low"),
        _finding(rule_id="rule.b", element_id="e2", severity="severity_high"),
        _finding(rule_id="rule.c", element_id="e3", severity="severity_medium"),
    ]
    md = render_review_queue(items)
    # high should appear before medium before low in the table
    high_pos = md.find("rule.b")
    medium_pos = md.find("rule.c")
    low_pos = md.find("rule.a")
    assert high_pos < medium_pos < low_pos


def test_review_queue_summary_counts_by_severity():
    items = [
        _finding(rule_id="r1", element_id="e1", severity="severity_high"),
        _finding(rule_id="r2", element_id="e2", severity="severity_high"),
        _finding(rule_id="r3", element_id="e3", severity="severity_medium"),
    ]
    md = render_review_queue(items)
    assert "Total items: 3" in md
    assert "severity_high: 2" in md
    assert "severity_medium: 1" in md


def test_review_queue_renders_citation_or_missing_marker():
    items = [
        _finding(
            rule_id="r1", element_id="e1",
            citation="BEP Sec.1.1 — Document Control",
        ),
        _finding(
            rule_id="r2", element_id="e2",
            citation=None, citation_missing=True,
        ),
        _finding(rule_id="r3", element_id="e3"),  # no citation, no missing flag
    ]
    md = render_review_queue(items)
    assert "BEP Sec.1.1" in md
    assert "missing (hard-mode rule)" in md


def test_review_queue_escapes_pipe_in_message():
    items = [
        _finding(
            rule_id="r1", element_id="e1",
            message="value|with|pipes should not break the table",
        ),
    ]
    md = render_review_queue(items)
    # No literal unescaped pipes inside cell text
    assert "value\\|with\\|pipes" in md


# ── render_data_quality_report ──────────────────────────────────────────────


def test_data_quality_empty_produces_valid_markdown():
    md = render_data_quality_report([], iteration=0)
    assert md.startswith("# Data Quality Report")
    assert "No missing-data items" in md


def test_data_quality_groups_by_parameter():
    items = [
        _finding(rule_id="r1", element_id="e1", parameter="Department"),
        _finding(rule_id="r1", element_id="e2", parameter="Department"),
        _finding(rule_id="r1", element_id="e3", parameter="Department"),
        _finding(rule_id="r2", element_id="e4", parameter="Occupancy"),
    ]
    md = render_data_quality_report(items)
    # Each parameter gets its own section
    assert "### Parameter: `Department`" in md
    assert "### Parameter: `Occupancy`" in md
    # Department section appears before Occupancy (most-common first)
    assert md.find("Department") < md.find("Occupancy")


def test_data_quality_summary_lists_most_common_first():
    items = [
        _finding(rule_id="r1", element_id="e1", parameter="Occupancy"),
        _finding(rule_id="r1", element_id="e2", parameter="Department"),
        _finding(rule_id="r1", element_id="e3", parameter="Department"),
        _finding(rule_id="r1", element_id="e4", parameter="Department"),
    ]
    md = render_data_quality_report(items)
    # Summary should rank Department(3) above Occupancy(1)
    dept_idx = md.find("`Department`: 3 element")
    occ_idx = md.find("`Occupancy`: 1 element")
    assert 0 < dept_idx < occ_idx


def test_data_quality_total_count_reported():
    items = [
        _finding(rule_id="r1", element_id=f"e{i}", parameter="P")
        for i in range(7)
    ]
    md = render_data_quality_report(items)
    assert "Total items: 7" in md


# ── write_side_reports ──────────────────────────────────────────────────────


def test_write_side_reports_creates_two_files(tmp_path):
    findings_out = tmp_path / "findings.json"
    findings_out.write_text("[]")  # findings.json itself is written by caller
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "p",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "manual_review_items": [_finding(rule_id="r1", element_id="e1")],
        "missing_data_items": [_finding(rule_id="r2", element_id="e2")],
        "status": "designing",
        "error": None,
    }
    review_path, dataq_path = write_side_reports(state, findings_out)
    assert review_path == findings_out.with_name("review_queue.md")
    assert dataq_path == findings_out.with_name("data_quality_report.md")
    assert review_path.exists()
    assert dataq_path.exists()
    assert "Manual Review Queue" in review_path.read_text(encoding="utf-8")
    assert "Data Quality Report" in dataq_path.read_text(encoding="utf-8")


def test_write_side_reports_handles_missing_buckets(tmp_path):
    """If state has no manual_review_items / missing_data_items keys, still write
    empty-but-valid reports (BB makes them NotRequired)."""
    findings_out = tmp_path / "findings.json"
    minimal_state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "p",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "designing",
        "error": None,
    }
    review_path, dataq_path = write_side_reports(minimal_state, findings_out)
    assert review_path.exists()
    assert dataq_path.exists()
    assert "No manual-review items" in review_path.read_text(encoding="utf-8")
    assert "No missing-data items" in dataq_path.read_text(encoding="utf-8")


# ── DesignAgent gate: manual_review never reaches ACC; missing_data routes ──
# ── to Path A when it has no auto-fill source (v1.4-K3 Layer 1) ─────────────


@pytest.mark.asyncio
async def test_design_agent_routes_missing_data_but_not_manual_review():
    """v1.4-K3 Layer 1 revises the old v1-CC contract: manual_review items are
    STILL never touched, but missing_data items WITHOUT an auto-fill source now
    route to Path A ACC Issues instead of being dropped.
    """
    autonomy = AutonomyPolicy.load(AUTONOMY_PATH)
    mcp = MockFormaMCPClient()
    design = DesignAgent(
        mcp=mcp,
        autonomy=autonomy,
        project_id="b.test-project",
        max_issues=10,
        rule_filter=None,  # don't filter; let everything through to design
    )

    # State has 1 non_compliant + 2 manual_review + 3 missing_data.
    # Only the non_compliant should become a proposed_fix.
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.test-project",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [
            {"id": "e1", "category": "Rooms", "name": "Room 1", "params": {}},
            {"id": "e2", "category": "Rooms", "name": "Room 2", "params": {}},
            {"id": "e3", "category": "Rooms", "name": "Room 3", "params": {}},
            {"id": "e4", "category": "Rooms", "name": "Room 4", "params": {}},
            {"id": "e5", "category": "Rooms", "name": "Room 5", "params": {}},
            {"id": "e6", "category": "Rooms", "name": "Room 6", "params": {}},
        ],
        "findings": [
            _finding(
                rule_id="room.number.format",
                element_id="e1",
                severity_tag="missing_optional_param",
                severity="severity_low",
                status="non_compliant",
            ),
        ],
        "manual_review_items": [
            _finding(
                rule_id="room.area.borderline",
                element_id="e2",
                status="manual_review",
            ),
            _finding(
                rule_id="room.area.borderline",
                element_id="e3",
                status="manual_review",
            ),
        ],
        "missing_data_items": [
            _finding(
                rule_id="room.dept.required", element_id="e4", status="missing_data"
            ),
            _finding(
                rule_id="room.dept.required", element_id="e5", status="missing_data"
            ),
            _finding(
                rule_id="room.occ.required", element_id="e6", status="missing_data"
            ),
        ],
        "proposed_fixes": [],
        "status": "designing",
        "error": None,
    }

    result = await design.run(state)

    # No Revit channel → missing_data can't be auto-filled, so each routes to
    # Path A. v1.4-K18: grouped by (rule, status) → 3 issues: number.format
    # (e1) · dept.required/missing_data (e4 + e5) · occ.required (e6).
    # manual_review (e2, e3) stays untouched.
    assert len(result["proposed_fixes"]) == 3
    fix_element_ids = {f["element_id"] for f in result["proposed_fixes"]}
    assert fix_element_ids == {"e1", "e4", "e6"}  # e5 folded into the dept group

    # manual_review items must STILL never become fixes.
    for excluded_id in {"e2", "e3"}:
        assert excluded_id not in fix_element_ids

    # The side buckets remain in state untouched
    assert len(result["manual_review_items"]) == 2
    assert len(result["missing_data_items"]) == 3


# ── render_per_run_report (v1 task V-1) ─────────────────────────────────────


def test_compliance_pct_handles_empty_summary():
    assert _compliance_pct(None) == "(n/a)"
    assert _compliance_pct({"total": 0, "compliant": 0}) == "(n/a)"


def test_compliance_pct_formats_one_decimal():
    s = {"total": 100, "compliant": 87}
    assert _compliance_pct(s) == "87.0%"


def test_top_findings_sorts_high_severity_first():
    items = [
        _finding(rule_id="r.a", element_id="e1", severity="severity_low"),
        _finding(rule_id="r.b", element_id="e2", severity="severity_high"),
        _finding(rule_id="r.c", element_id="e3", severity="severity_medium"),
    ]
    top = _top_findings(items, limit=10)
    assert [f["rule_id"] for f in top] == ["r.b", "r.c", "r.a"]


def test_top_findings_respects_limit():
    items = [
        _finding(rule_id=f"r{i}", element_id=f"e{i}", severity="severity_high")
        for i in range(20)
    ]
    assert len(_top_findings(items, limit=5)) == 5


def test_per_run_report_empty_state_still_renders():
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "converged",
        "error": None,
    }
    md = render_per_run_report(state, run_id="run-empty", mode="check")
    assert "Compliance audit report" in md
    assert "run-empty" in md
    assert "No non-compliant findings" in md


def test_per_run_report_header_metadata_renders():
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.demo",
        "iteration": 2,
        "max_iterations": 5,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "outcomes_summary": {
            "total": 50, "compliant": 40, "non_compliant": 5,
            "manual_review": 2, "missing_data": 3,
        },
        "status": "converged",
        "error": None,
    }
    md = render_per_run_report(
        state, run_id="run-abc",
        mode="run", started_at="2026-06-01T10:00:00",
        finished_at="2026-06-01T10:01:30", duration_seconds=90.5,
        rules_path="config/rules.room_compliance.yaml",
    )
    assert "Mode:** `run`" in md
    assert "Project:** `b.demo`" in md
    assert "Started:** 2026-06-01T10:00:00" in md
    assert "Duration:** 90.50s" in md
    assert "Iterations:** 2" in md
    assert "Rules:** `config/rules.room_compliance.yaml`" in md
    # Summary numbers
    assert "Total checks: 50" in md
    assert "Compliant:      40  (80.0%)" in md
    assert "Non-Compliant:  5" in md
    assert "Manual Review:  2" in md
    assert "Missing Data:   3" in md


def test_per_run_report_document_renders_model_line():
    """SPEC_DOCUMENT_IDENTITY_STAMP: `document` kwarg -> **Model:** line."""
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.demo",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "converged",
        "error": None,
    }
    md = render_per_run_report(
        state, run_id="run-doc", mode="check",
        document={
            "title": "MyModel.rvt",
            "path": "C:\\models\\MyModel.rvt",
            "revit_version_name": "Autodesk Revit 2026",
            "is_modified": True,
        },
    )
    assert "**Model:** `MyModel.rvt` — Autodesk Revit 2026" in md
    assert "**Model path:** `C:\\models\\MyModel.rvt`" in md
    assert "unsaved changes" in md.lower()


def test_per_run_report_without_document_is_byte_identical():
    """Additive contract: omitting `document` reproduces the pre-spec report
    exactly — no Model line, no other change."""
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.demo",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "converged",
        "error": None,
    }
    md_omitted = render_per_run_report(state, run_id="run-x2", mode="check")
    md_explicit_none = render_per_run_report(
        state, run_id="run-x2", mode="check", document=None,
    )
    assert md_omitted == md_explicit_none
    assert "Model:**" not in md_omitted


def test_per_run_report_prefers_element_name_in_top_table():
    """QW-1: when element_name present, Top findings table shows the name
    instead of the truncated element_id URN."""
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.demo",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [
            {**_finding(rule_id="r.x", element_id="urn:adsk:long-base64-blah-blah-blah"),
             "element_name": "Closet 11A"},
        ],
        "proposed_fixes": [],
        "status": "converged",
        "error": None,
    }
    md = render_per_run_report(state, run_id="run-x", mode="check")
    assert "Closet 11A" in md
    # URN should NOT be rendered when name is available
    assert "urn:adsk:long-base64" not in md


def test_review_queue_prefers_element_name_when_present():
    items = [
        {**_finding(rule_id="r1", element_id="urn:adsk:elem-aaaa"),
         "element_name": "Bath 07"},
    ]
    md = render_review_queue(items)
    assert "Bath 07" in md


def test_data_quality_report_prefers_element_name_when_present():
    items = [
        {**_finding(rule_id="r1", element_id="urn:adsk:elem-aaaa", parameter="Dept"),
         "element_name": "Entry 01"},
    ]
    md = render_data_quality_report(items)
    assert "Entry 01" in md


def test_per_run_report_table_lists_top_findings_with_relative_links():
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.demo",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [
            _finding(rule_id="r.high", element_id="e1", severity="severity_high"),
            _finding(rule_id="r.med", element_id="e2", severity="severity_medium"),
        ],
        "manual_review_items": [_finding(rule_id="r.mr", element_id="e3")],
        "missing_data_items": [
            _finding(rule_id="r.md", element_id="e4"),
            _finding(rule_id="r.md", element_id="e5"),
        ],
        "proposed_fixes": [],
        "status": "converged",
        "error": None,
    }
    md = render_per_run_report(state, run_id="run-x", mode="check")
    assert "## Top non-compliant findings" in md
    assert "| 1 | HIGH | `r.high`" in md
    assert "| 2 | MEDIUM | `r.med`" in md
    # Relative links to sibling files
    assert "[review_queue.md](review_queue.md)" in md
    assert "[data_quality_report.md](data_quality_report.md)" in md
    assert "[findings.json](findings.json)" in md
    assert "[trace.md](trace.md)" in md
    # Bucket counts
    assert "Manual review items: 1" in md
    assert "Missing data items: 2" in md


def _seed_run(
    runs_root: Path,
    run_id: str,
    *,
    started_at: str,
    non_compliant: list[Finding] | None = None,
    missing_data: list[Finding] | None = None,
    summary: dict | None = None,
) -> Path:
    """Helper: create a synthetic run folder with metadata.json + outcomes.json."""
    import json
    folder = runs_root / run_id
    folder.mkdir(parents=True, exist_ok=True)
    nc = non_compliant or []
    md = missing_data or []
    s = summary or {
        "total": 50, "compliant": 50 - len(nc) - len(md),
        "non_compliant": len(nc), "manual_review": 0, "missing_data": len(md),
    }
    (folder / "metadata.json").write_text(json.dumps({
        "run_id": run_id, "mode": "check", "status": "converged",
        "started_at": started_at, "duration_seconds": 1.0,
        "non_compliant_count": len(nc), "manual_review_count": 0,
        "missing_data_count": len(md),
        "outcomes_summary": s,
    }))
    (folder / "outcomes.json").write_text(json.dumps({
        "outcomes_summary": s,
        "non_compliant": nc,
        "manual_review_items": [],
        "missing_data_items": md,
    }))
    return folder


# ── render_trend_report (v1 task V-3) ───────────────────────────────────────


def test_trend_report_no_runs_folder_returns_graceful_message(tmp_path):
    md = render_trend_report(tmp_path / "no-such")
    assert "No runs/ folder yet" in md


def test_trend_report_empty_runs_folder_handled(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    md = render_trend_report(runs)
    assert "No run folders found" in md


def test_trend_report_single_run_shows_table_and_no_diff(tmp_path):
    runs = tmp_path / "runs"
    _seed_run(
        runs, "run-001", started_at="2026-06-01T10:00:00",
        non_compliant=[_finding(rule_id="r1", element_id="e1")],
    )
    md = render_trend_report(runs)
    assert "Total runs scanned: **1**" in md
    assert "run-001" in md
    assert "Previous: (none -- this is the first run)" in md


def test_trend_report_two_runs_diff_resolved_and_new(tmp_path):
    runs = tmp_path / "runs"
    _seed_run(
        runs, "run-001", started_at="2026-06-01T10:00:00",
        non_compliant=[
            _finding(rule_id="r.a", element_id="e1"),  # resolved
            _finding(rule_id="r.b", element_id="e2"),  # persistent
        ],
    )
    _seed_run(
        runs, "run-002", started_at="2026-06-01T11:00:00",
        non_compliant=[
            _finding(rule_id="r.b", element_id="e2"),  # persistent
            _finding(rule_id="r.c", element_id="e3"),  # newly_introduced
        ],
    )
    md = render_trend_report(runs)
    assert "Resolved since previous: **1**" in md
    assert "Newly introduced: **1**" in md
    assert "Persistent (both runs): **1**" in md
    assert "`r.c`" in md  # newly_introduced section
    assert "`r.a`" in md  # resolved section
    # Oldest at top of table -- run-001 row appears before run-002 row in the table
    table_section = md.split("## Latest run vs previous")[0]
    assert table_section.index("run-001") < table_section.index("run-002")
    # In the diff section, "Latest" line points to run-002
    diff_section = md.split("## Latest run vs previous")[1]
    assert diff_section.index("run-002") < diff_section.index("run-001")


def test_trend_report_persistent_across_all_shown_when_3plus_runs(tmp_path):
    runs = tmp_path / "runs"
    persistent = _finding(rule_id="r.stuck", element_id="e.persistent")
    for i, ts in enumerate([
        "2026-06-01T10:00:00",
        "2026-06-01T11:00:00",
        "2026-06-01T12:00:00",
    ]):
        _seed_run(
            runs, f"run-{i:03d}", started_at=ts,
            non_compliant=[persistent],
        )
    md = render_trend_report(runs)
    assert "Persistent across ALL 3 runs" in md
    assert "r.stuck::e.persistent::Department" in md  # fingerprint shown


def test_trend_report_tolerates_missing_outcomes_json(tmp_path):
    """A half-written run folder (crash mid-finish) should not crash the trend."""
    import json
    runs = tmp_path / "runs"
    folder = runs / "run-partial"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps({
        "run_id": "run-partial", "mode": "check", "status": "failed",
        "started_at": "2026-06-01T09:00:00",
    }))
    # No outcomes.json
    md = render_trend_report(runs)
    assert "run-partial" in md  # still appears in table


def test_trend_report_skips_folders_without_metadata(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run-good").mkdir()
    (runs / "run-good" / "metadata.json").write_text(
        '{"run_id": "run-good", "started_at": "2026-06-01T10:00:00"}'
    )
    (runs / "run-broken").mkdir()
    # no metadata.json
    md = render_trend_report(runs)
    assert "run-good" in md
    assert "run-broken" not in md


def test_trend_report_respects_limit(tmp_path):
    runs = tmp_path / "runs"
    for i in range(5):
        _seed_run(
            runs, f"run-{i:03d}",
            started_at=f"2026-06-01T1{i}:00:00",
        )
    md = render_trend_report(runs, limit=3)
    assert "Total runs scanned: **3**" in md


def test_write_trend_report_creates_file(tmp_path):
    runs = tmp_path / "runs"
    _seed_run(runs, "run-x", started_at="2026-06-01T10:00:00")
    path = write_trend_report(runs)
    assert path == runs / "trend.md"
    assert path.exists()
    assert "Compliance trend" in path.read_text(encoding="utf-8")


def test_per_run_report_caps_visible_rows_and_notes_extra():
    findings = [
        _finding(rule_id=f"r{i}", element_id=f"e{i}", severity="severity_low")
        for i in range(15)
    ]
    state: OrchestratorState = {  # type: ignore[typeddict-item]
        "project_id": "b.demo",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": findings,
        "proposed_fixes": [],
        "status": "converged",
        "error": None,
    }
    md = render_per_run_report(state, run_id="run-many", mode="check", top_n=10)
    assert "(+5 more in `findings.json`)" in md
