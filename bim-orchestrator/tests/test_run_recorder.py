"""Tests for v1 task L + M: TraceCollector, render_trace_markdown, RunFolder, list_runs."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bim_orchestrator.run_recorder import (
    RunFolder,
    TraceCollector,
    _classify,
    diff_outcomes,
    fingerprint,
    format_runs_table,
    list_runs,
    render_trace_markdown,
    trace_processor,
)


# ── TraceCollector ──────────────────────────────────────────────────────────


def test_trace_collector_buffers_events():
    c = TraceCollector(run_id="run-xyz")
    c.record({"event": "qc_agent.start", "rule_count": 5, "iteration": 0})
    c.record({"event": "qc_agent.done", "findings": 3, "iteration": 0})
    assert len(c.events) == 2
    assert c.events[0]["event"] == "qc_agent.start"
    assert c.events[1]["findings"] == 3


def test_trace_collector_drops_prompt_response_keys():
    """Prompt/response/messages keys are never captured to keep the trace
    token-efficient and protect against accidental prompt leakage."""
    c = TraceCollector(run_id="run-xyz")
    c.record({
        "event": "llm.call",
        "prompt": "x" * 10000,
        "response": "y" * 10000,
        "messages": [{"role": "user"}],
        "context": {"big": "blob"},
        "model": "claude-haiku-4.5",
        "input_tokens": 42,
    })
    rec = c.events[0]
    for forbidden in ("prompt", "response", "messages", "context"):
        assert forbidden not in rec
    # Useful metadata is preserved
    assert rec["model"] == "claude-haiku-4.5"
    assert rec["input_tokens"] == 42


def test_trace_collector_truncates_long_strings():
    c = TraceCollector(run_id="run-xyz")
    long_str = "z" * 1000
    c.record({"event": "agent.note", "detail": long_str})
    detail = c.events[0]["detail"]
    assert len(detail) <= 500
    assert detail.endswith("...")


def test_trace_collector_activate_deactivate_via_contextvar():
    """trace_processor should pick up the current collector and ignore others."""
    c1 = TraceCollector(run_id="run-1")
    c2 = TraceCollector(run_id="run-2")

    tok = c1.activate()
    trace_processor(None, "info", {"event": "to-c1", "iteration": 0})
    c1.deactivate(tok)

    # No active collector now -> events go nowhere (no error)
    trace_processor(None, "info", {"event": "orphan"})

    tok = c2.activate()
    trace_processor(None, "info", {"event": "to-c2", "iteration": 0})
    c2.deactivate(tok)

    assert [e["event"] for e in c1.events] == ["to-c1"]
    assert [e["event"] for e in c2.events] == ["to-c2"]


def test_trace_processor_returns_event_dict_unchanged():
    """structlog processors MUST return the event dict so the chain continues."""
    ev = {"event": "x", "iteration": 0}
    result = trace_processor(None, "info", ev)
    assert result is ev


# ── render_trace_markdown ───────────────────────────────────────────────────


def test_render_trace_empty_collector_produces_valid_markdown():
    c = TraceCollector(run_id="run-empty")
    md = render_trace_markdown(c, run_id="run-empty")
    assert md.startswith("# Run trace: run-empty")
    assert "No events captured" in md


def test_render_trace_groups_by_iteration_then_phase():
    c = TraceCollector(run_id="run-multi")
    # Iteration 0: Query then QC then Design
    c.record({"event": "query_agent.start", "iteration": 0})
    c.record({"event": "query_agent.done", "iteration": 0, "count": 3})
    c.record({"event": "qc_agent.start", "iteration": 0, "rule_count": 5})
    c.record({"event": "qc_agent.done", "iteration": 0, "findings": 2})
    c.record({"event": "design_agent.start", "iteration": 0})
    # Iteration 1: Query again
    c.record({"event": "query_agent.start", "iteration": 1})
    md = render_trace_markdown(c, run_id="run-multi")

    assert "## Iteration 0" in md
    assert "## Iteration 1" in md
    assert "### Phase: Query" in md
    assert "### Phase: QC" in md
    assert "### Phase: Design" in md
    # Iteration 0 must appear before iteration 1
    assert md.find("## Iteration 0") < md.find("## Iteration 1")
    # Within iteration 0, Query phase must appear before QC
    iter0_start = md.find("## Iteration 0")
    iter1_start = md.find("## Iteration 1")
    iter0_block = md[iter0_start:iter1_start]
    assert iter0_block.find("### Phase: Query") < iter0_block.find("### Phase: QC")


def test_render_trace_classifies_unknown_prefix_verbatim():
    c = TraceCollector(run_id="run-x")
    c.record({"event": "custom_thing.happened", "iteration": 0})
    md = render_trace_markdown(c, run_id="run-x")
    assert "### Phase: custom_thing" in md


def test_render_trace_global_section_for_events_without_iteration():
    c = TraceCollector(run_id="run-pre")
    c.record({"event": "checkpoint.write"})
    md = render_trace_markdown(c, run_id="run-pre")
    assert "## Pre-loop / global" in md


def test_classify_helper_known_and_unknown():
    assert _classify("qc_agent.done") == "QC"
    assert _classify("design_agent.preview") == "Design"
    assert _classify("foo.bar") == "foo"
    assert _classify("bareword") == "bareword"


# ── RunFolder ───────────────────────────────────────────────────────────────


def test_run_folder_create_makes_unique_directory(tmp_path: Path):
    runs_root = tmp_path / "runs"
    rf1 = RunFolder.create(runs_root, mode="check")
    rf2 = RunFolder.create(runs_root, mode="check")
    assert rf1.root.exists()
    assert rf2.root.exists()
    assert rf1.run_id != rf2.run_id
    assert rf1.run_id.startswith("run-")
    assert len(rf1.run_id) == len("run-") + 8  # 4 hex bytes = 8 chars


def test_run_folder_paths(tmp_path: Path):
    rf = RunFolder.create(tmp_path, mode="check")
    assert rf.findings_path == rf.root / "findings.json"
    assert rf.metadata_path == rf.root / "metadata.json"
    assert rf.trace_path == rf.root / "trace.md"
    assert rf.outcomes_path == rf.root / "outcomes.json"


def test_run_folder_write_metadata(tmp_path: Path):
    rf = RunFolder.create(tmp_path, mode="run")
    state = {
        "project_id": "b.demo",
        "iteration": 2,
        "max_iterations": 5,
        "findings": [{"id": 1}, {"id": 2}],
        "manual_review_items": [{"id": 3}],
        "missing_data_items": [],
        "proposed_fixes": [{"executed": True}, {"executed": False}],
        "outcomes_summary": {
            "total": 100, "compliant": 90, "non_compliant": 2,
            "manual_review": 1, "missing_data": 0,
        },
    }
    rf.write_metadata(status="converged", state=state)
    meta = json.loads(rf.metadata_path.read_text())
    assert meta["run_id"] == rf.run_id
    assert meta["mode"] == "run"
    assert meta["status"] == "converged"
    assert meta["iterations"] == 2
    assert meta["max_iterations"] == 5
    assert meta["project_id"] == "b.demo"
    assert meta["non_compliant_count"] == 2
    assert meta["manual_review_count"] == 1
    assert meta["missing_data_count"] == 0
    assert meta["proposed_fixes_count"] == 2
    assert meta["executed_fixes_count"] == 1
    assert meta["outcomes_summary"]["total"] == 100
    assert "started_at" in meta
    assert "finished_at" in meta
    assert "duration_seconds" in meta


def test_run_folder_write_metadata_document_populated(tmp_path: Path):
    """SPEC_DOCUMENT_IDENTITY_STAMP: state["document_info"] populated -> metadata.json
    carries the exact dict under the "document" key."""
    rf = RunFolder.create(tmp_path, mode="run-revit")
    doc = {"title": "MyModel", "path": "C:\\models\\MyModel.rvt", "revit_version_name": "Autodesk Revit 2026"}
    state = {
        "project_id": "b.demo",
        "iteration": 1,
        "max_iterations": 5,
        "findings": [],
        "manual_review_items": [],
        "missing_data_items": [],
        "proposed_fixes": [],
        "document_info": doc,
    }
    rf.write_metadata(status="converged", state=state)
    meta = json.loads(rf.metadata_path.read_text())
    assert meta["document"] == doc


def test_run_folder_write_metadata_document_absent_is_null(tmp_path: Path):
    """No document_info key on state -> metadata.json still carries the
    "document" key, but with value null (never omitted)."""
    rf = RunFolder.create(tmp_path, mode="check")
    state = {
        "project_id": "b.demo",
        "iteration": 0,
        "max_iterations": 5,
        "findings": [],
        "manual_review_items": [],
        "missing_data_items": [],
        "proposed_fixes": [],
    }
    rf.write_metadata(status="converged", state=state)
    meta = json.loads(rf.metadata_path.read_text())
    assert "document" in meta
    assert meta["document"] is None


def test_run_folder_write_outcomes(tmp_path: Path):
    rf = RunFolder.create(tmp_path, mode="check")
    state = {
        "findings": [{"rule_id": "r1", "status": "non_compliant"}],
        "manual_review_items": [{"rule_id": "r2", "status": "manual_review"}],
        "missing_data_items": [
            {"rule_id": "r3", "status": "missing_data"},
            {"rule_id": "r3", "status": "missing_data"},
        ],
        "outcomes_summary": {"total": 4, "compliant": 0, "non_compliant": 1,
                             "manual_review": 1, "missing_data": 2},
    }
    rf.write_outcomes(state)
    outcomes = json.loads(rf.outcomes_path.read_text())
    assert outcomes["outcomes_summary"]["total"] == 4
    assert len(outcomes["non_compliant"]) == 1
    assert len(outcomes["manual_review_items"]) == 1
    assert len(outcomes["missing_data_items"]) == 2


def test_run_folder_write_trace_emits_markdown(tmp_path: Path):
    rf = RunFolder.create(tmp_path, mode="check")
    c = TraceCollector(run_id=rf.run_id)
    c.record({"event": "qc_agent.start", "iteration": 0})
    c.record({"event": "qc_agent.done", "iteration": 0, "findings": 1})
    rf.write_trace(c, project_id="b.demo")
    md = rf.trace_path.read_text()
    assert f"# Run trace: {rf.run_id}" in md
    assert "Project: `b.demo`" in md
    assert "Mode: `check`" in md
    assert "### Phase: QC" in md


# ── list_runs ───────────────────────────────────────────────────────────────


def test_list_runs_returns_empty_when_dir_missing(tmp_path: Path):
    rows = list_runs(tmp_path / "nonexistent")
    assert rows == []


def test_list_runs_returns_metadata_newest_first(tmp_path: Path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    # Create 3 runs with explicit started_at to control ordering
    for run_id, ts in [
        ("run-aaaa1111", "2026-05-30T10:00:00"),
        ("run-bbbb2222", "2026-05-31T15:00:00"),
        ("run-cccc3333", "2026-05-29T08:00:00"),
    ]:
        folder = runs_root / run_id
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({
            "run_id": run_id, "mode": "check", "status": "converged",
            "started_at": ts,
        }))

    rows = list_runs(runs_root)
    assert [r["run_id"] for r in rows] == [
        "run-bbbb2222",  # newest
        "run-aaaa1111",
        "run-cccc3333",  # oldest
    ]


def test_list_runs_handles_missing_metadata(tmp_path: Path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    bad = runs_root / "run-deadbeef"
    bad.mkdir()
    # no metadata.json
    rows = list_runs(runs_root)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-deadbeef"
    assert rows[0]["status"] == "metadata_missing"


def test_list_runs_handles_corrupt_metadata(tmp_path: Path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    bad = runs_root / "run-cafe1234"
    bad.mkdir()
    (bad / "metadata.json").write_text("{not json")
    rows = list_runs(runs_root)
    assert rows[0]["run_id"] == "run-cafe1234"
    assert rows[0]["status"] == "metadata_corrupt"


def test_list_runs_ignores_non_run_folders(tmp_path: Path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    (runs_root / "scratch").mkdir()
    (runs_root / "tmp-XYZ").mkdir()
    (runs_root / "run-aabbccdd").mkdir()
    (runs_root / "run-aabbccdd" / "metadata.json").write_text(json.dumps({
        "run_id": "run-aabbccdd", "started_at": "2026-05-31T10:00:00",
    }))
    rows = list_runs(runs_root)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-aabbccdd"


# ── format_runs_table ───────────────────────────────────────────────────────


def test_format_runs_table_empty():
    assert "no runs found" in format_runs_table([])


def test_format_runs_table_renders_columns():
    rows = [
        {
            "run_id": "run-aabbccdd", "mode": "check", "status": "converged",
            "started_at": "2026-05-31T10:00:00", "duration_seconds": 3.5,
            "non_compliant_count": 2, "manual_review_count": 1, "missing_data_count": 4,
        },
    ]
    table = format_runs_table(rows)
    assert "Run ID" in table
    assert "run-aabbccdd" in table
    assert "check" in table
    assert "converged" in table
    assert "2/1/4" in table
    assert "3.5s" in table


# ── Integration with structlog logging chain ────────────────────────────────


def test_structlog_events_flow_into_active_collector():
    """When configure_logging() has installed trace_processor and a collector
    is active, structlog log calls populate collector.events."""
    import structlog

    from bim_orchestrator.logging_setup import configure_logging

    configure_logging()
    c = TraceCollector(run_id="run-int")
    tok = c.activate()
    try:
        log = structlog.get_logger("test")
        log.info("qc_agent.start", iteration=0, rule_count=3)
        log.info("qc_agent.done", iteration=0, findings=2)
    finally:
        c.deactivate(tok)

    events = [e.get("event") for e in c.events]
    assert "qc_agent.start" in events
    assert "qc_agent.done" in events


# ── fingerprint + diff_outcomes (v1 task V-2) ───────────────────────────────


def _fnd(rule_id="r", element_id="e", parameter="p", **extra):
    base = {"rule_id": rule_id, "element_id": element_id, "parameter": parameter,
            "severity": "severity_low", "message": "x", "suggested_value": None,
            "citation": None, "status": "non_compliant"}
    base.update(extra)
    return base


def test_fingerprint_is_deterministic_for_same_triple():
    a = _fnd(rule_id="r1", element_id="e1", parameter="p1")
    b = _fnd(rule_id="r1", element_id="e1", parameter="p1")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_on_any_triple_change():
    a = _fnd(rule_id="r1", element_id="e1", parameter="p1")
    assert fingerprint(a) != fingerprint(_fnd(rule_id="r2", element_id="e1", parameter="p1"))
    assert fingerprint(a) != fingerprint(_fnd(rule_id="r1", element_id="e2", parameter="p1"))
    assert fingerprint(a) != fingerprint(_fnd(rule_id="r1", element_id="e1", parameter="p2"))


def test_fingerprint_ignores_severity_message_citation():
    """Severity / message / citation can drift between runs without the
    underlying problem being a different finding."""
    a = _fnd(severity="severity_high", message="old", citation="A")
    b = _fnd(severity="severity_low", message="new", citation="B")
    assert fingerprint(a) == fingerprint(b)


def test_diff_outcomes_first_run_treats_everything_as_new():
    curr = {"non_compliant": [_fnd(rule_id="r1", element_id="e1")]}
    d = diff_outcomes(None, curr)
    assert len(d["newly_introduced"]) == 1
    assert d["resolved"] == []
    assert d["persistent"] == []


def test_diff_outcomes_partitions_resolved_new_persistent():
    prev = {
        "non_compliant": [
            _fnd(rule_id="r1", element_id="e1"),  # persistent
            _fnd(rule_id="r2", element_id="e2"),  # resolved
        ],
        "missing_data_items": [_fnd(rule_id="rm", element_id="em")],  # resolved
    }
    curr = {
        "non_compliant": [
            _fnd(rule_id="r1", element_id="e1"),  # persistent
            _fnd(rule_id="r3", element_id="e3"),  # newly_introduced
        ],
    }
    d = diff_outcomes(prev, curr)
    resolved_fps = {fingerprint(f) for f in d["resolved"]}
    new_fps = {fingerprint(f) for f in d["newly_introduced"]}
    persistent_fps = {fingerprint(f) for f in d["persistent"]}
    assert resolved_fps == {"r2::e2::p", "rm::em::p"}
    assert new_fps == {"r3::e3::p"}
    assert persistent_fps == {"r1::e1::p"}


def test_diff_outcomes_considers_all_three_buckets():
    """A finding in manual_review_items prev + missing_data_items curr is
    persistent (same triple, different bucket -- still the same root issue)."""
    prev = {"manual_review_items": [_fnd(rule_id="r", element_id="e")]}
    curr = {"missing_data_items": [_fnd(rule_id="r", element_id="e")]}
    d = diff_outcomes(prev, curr)
    assert len(d["persistent"]) == 1
    assert d["resolved"] == []
    assert d["newly_introduced"] == []


def test_diff_outcomes_handles_empty_curr():
    """All findings resolved -- happy path for a converging model."""
    prev = {"non_compliant": [_fnd(rule_id="r1", element_id="e1")]}
    d = diff_outcomes(prev, {})
    assert len(d["resolved"]) == 1
    assert d["newly_introduced"] == []
    assert d["persistent"] == []


def test_structlog_events_do_not_leak_across_collectors():
    """After deactivating a collector, subsequent log calls must NOT land in it."""
    import structlog

    from bim_orchestrator.logging_setup import configure_logging

    configure_logging()
    c = TraceCollector(run_id="run-iso")
    tok = c.activate()
    structlog.get_logger("test").info("captured.in.scope")
    c.deactivate(tok)
    # This must NOT be captured
    structlog.get_logger("test").info("orphan.out.of.scope")

    captured_events = [e.get("event") for e in c.events]
    assert "captured.in.scope" in captured_events
    assert "orphan.out.of.scope" not in captured_events
