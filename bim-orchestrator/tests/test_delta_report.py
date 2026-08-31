"""SPEC_SCHEDULED_AUDIT_DELTA.md W4 — per-run delta report (render-from-disk,
reuses run_recorder.diff_outcomes, baseline selection by run identity)."""

from __future__ import annotations

import json
from pathlib import Path

from bim_orchestrator.delta_report import find_baseline, render_delta_report, write_delta_report


def _make_run(
    runs_root: Path,
    run_id: str,
    *,
    started_at: str,
    status: str = "completed",
    mode: str = "run",
    profile_name: str | None = None,
    non_compliant: list[dict] | None = None,
    document: dict | None = None,
) -> Path:
    d = runs_root / run_id
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(
        json.dumps({
            "run_id": run_id, "mode": mode, "status": status,
            "started_at": started_at, "document": document,
        }),
        encoding="utf-8",
    )
    (d / "outcomes.json").write_text(
        json.dumps({
            "non_compliant": non_compliant or [],
            "manual_review_items": [],
            "missing_data_items": [],
            "proposed_fixes": [],
        }),
        encoding="utf-8",
    )
    if profile_name is not None:
        (d / "profile.json").write_text(
            json.dumps({
                "profile_name": profile_name, "mode": mode,
                "rules": ["rules.t.yaml"], "propose_only": True,
            }),
            encoding="utf-8",
        )
    return d


class TestFindBaseline:
    def test_picks_completed_run_with_same_profile_name(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
                  profile_name="nightly")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
                             profile_name="nightly")
        baseline = find_baseline(runs_root, current)
        assert baseline is not None
        assert baseline.name == "run-11111111"

    def test_picks_converged_run_with_same_profile_name(self, tmp_path: Path) -> None:
        """The graph modes (--run/--run-revit, hence every --audit) record
        status "converged", not "completed" — a converged run MUST qualify as
        baseline (post-ship fix: gating on "completed" alone made the delta
        feature dead in the scheduled-audit main path)."""
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
                  profile_name="nightly", status="converged")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
                             profile_name="nightly", status="converged")
        baseline = find_baseline(runs_root, current)
        assert baseline is not None
        assert baseline.name == "run-11111111"

    def test_ignores_failed_run(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
                  profile_name="nightly", status="failed")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
                             profile_name="nightly")
        assert find_baseline(runs_root, current) is None

    def test_ignores_different_profile_name(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
                  profile_name="weekly")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
                             profile_name="nightly")
        assert find_baseline(runs_root, current) is None

    def test_ignores_ad_hoc_run_with_no_profile_when_current_has_one(
        self, tmp_path: Path
    ) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
                             profile_name="nightly")
        assert find_baseline(runs_root, current) is None

    def test_bare_cli_matches_by_mode_only_among_no_profile_candidates(
        self, tmp_path: Path
    ) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00", mode="run")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00", mode="run")
        baseline = find_baseline(runs_root, current)
        assert baseline is not None and baseline.name == "run-11111111"

    def test_bare_cli_ignores_candidate_with_profile_json(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
                  mode="run", profile_name="nightly")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00", mode="run")
        assert find_baseline(runs_root, current) is None

    def test_bare_cli_ignores_candidate_with_different_mode(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-14T02:00:00", mode="check")
        current = _make_run(runs_root, "run-22222222", started_at="2026-07-15T02:00:00", mode="run")
        assert find_baseline(runs_root, current) is None

    def test_no_candidates_returns_none(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        current = _make_run(runs_root, "run-11111111", started_at="2026-07-15T02:00:00")
        assert find_baseline(runs_root, current) is None

    def test_picks_newest_when_multiple_candidates(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(runs_root, "run-11111111", started_at="2026-07-13T02:00:00",
                  profile_name="nightly")
        _make_run(runs_root, "run-22222222", started_at="2026-07-14T02:00:00",
                  profile_name="nightly")
        current = _make_run(runs_root, "run-33333333", started_at="2026-07-15T02:00:00",
                             profile_name="nightly")
        baseline = find_baseline(runs_root, current)
        assert baseline is not None and baseline.name == "run-22222222"


class TestRenderDeltaReport:
    def test_no_baseline_renders_first_comparable_run(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        current = _make_run(
            runs_root, "run-11111111", started_at="2026-07-15T02:00:00",
            profile_name="nightly",
            non_compliant=[{"rule_id": "r1", "element_id": "1", "parameter": "P",
                             "status": "non_compliant", "severity": "severity_high"}],
        )
        md = render_delta_report(current, None)
        assert "Baseline: (none — first comparable run)" in md
        assert "## Newly introduced (1)" in md
        assert "### r1 (1 element(s))" in md
        assert "`1` | param `P` | non_compliant | severity_high" in md
        assert "## Resolved since baseline (0)" in md
        assert "_(none)_" in md

    def test_three_sections_reflect_diff_outcomes(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        baseline = _make_run(
            runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
            profile_name="nightly",
            non_compliant=[
                {"rule_id": "r1", "element_id": "1", "parameter": "P"},  # resolved
                {"rule_id": "r2", "element_id": "2", "parameter": "P"},  # persistent
            ],
        )
        current = _make_run(
            runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
            profile_name="nightly",
            non_compliant=[
                {"rule_id": "r2", "element_id": "2", "parameter": "P"},  # persistent
                {"rule_id": "r3", "element_id": "3", "parameter": "P"},  # new
            ],
        )
        md = render_delta_report(current, baseline)
        assert "| 1 | 1 | 1 |" in md  # resolved / new / persistent counts
        assert "## Newly introduced (1)" in md
        assert "### r3 (1 element(s))" in md
        assert "## Resolved since baseline (1)" in md
        assert "### r1 (1 element(s))" in md
        assert "## Persistent (1)" in md
        assert "### r2 (1 element(s))" in md

    def test_missing_fields_render_as_question_mark(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        current = _make_run(
            runs_root, "run-11111111", started_at="2026-07-15T02:00:00",
            profile_name="nightly",
            non_compliant=[{"rule_id": "r1", "element_id": "1"}],  # no parameter/status/severity
        )
        md = render_delta_report(current, None)
        assert "`1` | param `?` | ? | ?" in md

    def test_different_model_titles_warn_mismatch(self, tmp_path: Path) -> None:
        """SPEC_DOCUMENT_IDENTITY_STAMP D8: current and baseline both carry a
        document.title, and they differ -> mismatch warning + both titles
        shown in the Current run / Baseline lines."""
        runs_root = tmp_path / "runs"
        baseline = _make_run(
            runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
            profile_name="nightly", document={"title": "OldModel.rvt"},
        )
        current = _make_run(
            runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
            profile_name="nightly", document={"title": "NewModel.rvt"},
        )
        md = render_delta_report(current, baseline)
        assert "model `NewModel.rvt`" in md
        assert "model `OldModel.rvt`" in md
        assert "Model differs from baseline" in md
        assert "current `NewModel.rvt`" in md
        assert "baseline `OldModel.rvt`" in md

    def test_same_model_title_no_mismatch_warning(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        baseline = _make_run(
            runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
            profile_name="nightly", document={"title": "SameModel.rvt"},
        )
        current = _make_run(
            runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
            profile_name="nightly", document={"title": "SameModel.rvt"},
        )
        md = render_delta_report(current, baseline)
        assert "Model differs from baseline" not in md

    def test_pre_spec_run_missing_document_key_no_crash_no_warning(
        self, tmp_path: Path
    ) -> None:
        """A run recorded before this spec has no "document" key in
        metadata.json at all — must not crash and must not warn (D8: absence
        on either side is silence, not a mismatch)."""
        runs_root = tmp_path / "runs"
        baseline_dir = runs_root / "run-11111111"
        baseline_dir.mkdir(parents=True)
        (baseline_dir / "metadata.json").write_text(
            json.dumps({
                "run_id": "run-11111111", "mode": "run", "status": "completed",
                "started_at": "2026-07-14T02:00:00",
                # no "document" key at all -- pre-spec run
            }),
            encoding="utf-8",
        )
        (baseline_dir / "outcomes.json").write_text(
            json.dumps({"non_compliant": [], "manual_review_items": [],
                        "missing_data_items": [], "proposed_fixes": []}),
            encoding="utf-8",
        )
        (baseline_dir / "profile.json").write_text(
            json.dumps({"profile_name": "nightly", "mode": "run",
                        "rules": ["rules.t.yaml"], "propose_only": True}),
            encoding="utf-8",
        )
        current = _make_run(
            runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
            profile_name="nightly", document={"title": "NewModel.rvt"},
        )
        md = render_delta_report(current, baseline_dir)
        assert "Model differs from baseline" not in md


class TestWriteDeltaReport:
    def test_writes_markdown_and_json_shape(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        baseline = _make_run(
            runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
            profile_name="nightly",
            non_compliant=[{"rule_id": "r1", "element_id": "1", "parameter": "P"}],
        )
        current = _make_run(
            runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
            profile_name="nightly",
            non_compliant=[{"rule_id": "r2", "element_id": "2", "parameter": "P"}],
        )
        out_path = write_delta_report(current)
        assert out_path == current / "delta.md"
        assert out_path.exists()
        payload = json.loads((current / "delta.json").read_text(encoding="utf-8"))
        assert payload["run_id"] == "run-22222222"
        assert payload["baseline_run_id"] == "run-11111111"
        assert payload["counts"] == {"resolved": 1, "newly_introduced": 1, "persistent": 0}
        assert len(payload["resolved"]) == 1
        assert len(payload["newly_introduced"]) == 1
        assert payload["persistent"] == []

    def test_no_baseline_writes_json_with_none_baseline_id(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        current = _make_run(runs_root, "run-11111111", started_at="2026-07-15T02:00:00")
        write_delta_report(current)
        payload = json.loads((current / "delta.json").read_text(encoding="utf-8"))
        assert payload["baseline_run_id"] is None

    def test_delta_json_carries_document_and_baseline_document(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _make_run(
            runs_root, "run-11111111", started_at="2026-07-14T02:00:00",
            profile_name="nightly",
            non_compliant=[{"rule_id": "r1", "element_id": "1", "parameter": "P"}],
            document={"title": "OldModel.rvt"},
        )
        current = _make_run(
            runs_root, "run-22222222", started_at="2026-07-15T02:00:00",
            profile_name="nightly",
            non_compliant=[{"rule_id": "r2", "element_id": "2", "parameter": "P"}],
            document={"title": "NewModel.rvt"},
        )
        write_delta_report(current)
        payload = json.loads((current / "delta.json").read_text(encoding="utf-8"))
        assert payload["document"] == {"title": "NewModel.rvt"}
        assert payload["baseline_document"] == {"title": "OldModel.rvt"}


class TestFinishRunRecordingHook:
    """Wire-up in orchestrator._finish_run_recording (only on a successful
    status — "completed" from --check/--apply, "converged" from the graph
    modes; never fails the run on a render error)."""

    def _run_finish(self, tmp_path: Path, monkeypatch, *, status: str):
        import bim_orchestrator.orchestrator as orch
        from bim_orchestrator.run_recorder import RunFolder, TraceCollector

        monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path)
        folder = RunFolder.create(tmp_path, mode="run")
        collector = TraceCollector(run_id=folder.run_id)
        token = collector.activate()
        state = {
            "project_id": "b.test", "iteration": 1, "max_iterations": 3,
            "elements": [], "findings": [], "proposed_fixes": [],
            "status": status, "error": None,
        }
        orch._finish_run_recording(folder, collector, token, state, status=status)
        return folder

    def test_completed_status_produces_delta_md(self, tmp_path, monkeypatch) -> None:
        folder = self._run_finish(tmp_path, monkeypatch, status="completed")
        assert (folder.root / "delta.md").exists()

    def test_converged_status_produces_delta_md(self, tmp_path, monkeypatch) -> None:
        """--run/--run-revit (and --audit) finish with status "converged" —
        the delta hook must fire for them too."""
        folder = self._run_finish(tmp_path, monkeypatch, status="converged")
        assert (folder.root / "delta.md").exists()

    def test_failed_status_does_not_produce_delta_md(self, tmp_path, monkeypatch) -> None:
        folder = self._run_finish(tmp_path, monkeypatch, status="failed")
        assert not (folder.root / "delta.md").exists()

    def test_render_error_does_not_fail_the_run(self, tmp_path, monkeypatch) -> None:
        import bim_orchestrator.delta_report as delta_report

        def _boom(current_dir):
            raise RuntimeError("boom")

        monkeypatch.setattr(delta_report, "write_delta_report", _boom)
        # _finish_run_recording must complete without raising even though the
        # delta hook's import target now explodes.
        folder = self._run_finish(tmp_path, monkeypatch, status="completed")
        assert (folder.root / "metadata.json").exists()  # run finished normally
        assert not (folder.root / "delta.md").exists()

    def test_a_no_audit_run_produces_no_delta_md(self, tmp_path, monkeypatch) -> None:
        """M-07: gate on `recorded_status`, not the raw graph status.

        A run where every category failed to resolve still CONVERGES — zero
        elements in, zero findings out — and `recorded_status` downgrades it
        to "no_audit" for exactly that reason. Gating on the raw status wrote
        a delta anyway, and an empty outcome set diffed against a real
        baseline renders as "every finding resolved since": a clean bill of
        health sitting next to a metadata.json that says no audit happened.
        """
        import bim_orchestrator.orchestrator as orch
        from bim_orchestrator.run_recorder import RunFolder, TraceCollector

        monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path)
        folder = RunFolder.create(tmp_path, mode="run")
        collector = TraceCollector(run_id=folder.run_id)
        token = collector.activate()
        state = {
            "project_id": "b.test", "iteration": 1, "max_iterations": 3,
            "elements": [], "findings": [], "proposed_fixes": [],
            "status": "converged", "error": None,
            # rules existed, and NOTHING resolved -> coverage says no_audit
            "query_coverage": {
                "rule_count": 3,
                "targets_requested": ["Doors"],
                "categories_resolved": [],
                "categories_dropped": [{"category": "Doors", "reason": "mcp_error"}],
            },
        }
        orch._finish_run_recording(folder, collector, token, state, status="converged")

        from bim_orchestrator.state import recorded_status
        assert recorded_status("converged", state) == "no_audit", (
            "fixture no longer reproduces the downgrade this test is about"
        )
        assert not (folder.root / "delta.md").exists()
        assert not (folder.root / "delta.json").exists()
