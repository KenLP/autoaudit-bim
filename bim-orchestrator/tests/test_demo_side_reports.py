"""A demo run never touches the tracked side reports (2026-09-03).

`--demo` and `mode: demo` through the service used to write findings.json,
review_queue.md and data_quality_report.md at PROJECT_ROOT and refresh
runs/trend.md — all tracked. Three times in one week a demo overwrote another
session's real Snowdon results, and the deletion hook cannot see a
modification. Now a demo writes under runs/demo/ (gitignored), stamps its
metadata `demo: true`, skips the trend refresh, and is skipped by the trend
renderer and by delta baselines even when a later real run refreshes them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bim_orchestrator import orchestrator
from bim_orchestrator.delta_report import find_baseline
from bim_orchestrator.demo import DEMO_PROJECT_ID, build_demo_clients
from bim_orchestrator.reports import render_trend_report

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _run_demo_loop(tmp_path: Path, *, demo: bool) -> Path:
    revit_client, forma_client = build_demo_clients()
    rc = await orchestrator.run_revit(
        [orchestrator.DEFAULT_DEMO_RULES_PATH],
        orchestrator.DEFAULT_AUTONOMY_PATH,
        tmp_path / "out" / "findings.json",
        limit=10,
        rule_filter=None,
        dry_run_only=False,
        published=False,
        issue_subtype_id=None,
        max_iterations=4,
        checkpoint_dir=tmp_path / "checkpoints",
        bep_pdf=None,
        use_bep_fixture=False,
        vector_store_dir=None,
        approvals_dir=tmp_path / "approvals",
        revit_client_factory=revit_client,
        forma_client_factory=forma_client,
        project_id=DEMO_PROJECT_ID,
        demo=demo,
    )
    assert rc == 0
    run_dirs = [p for p in (tmp_path / "runs").iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    return run_dirs[0]


# ---- run_revit(demo=True) ---------------------------------------------------


@pytest.mark.asyncio
async def test_demo_run_stamps_metadata_and_skips_the_trend(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    run_dir = await _run_demo_loop(tmp_path, demo=True)
    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["demo"] is True
    assert not (tmp_path / "runs" / "trend.md").exists()
    # The side reports land next to findings_out, and the parent is created.
    assert (tmp_path / "out" / "findings.json").exists()
    assert (tmp_path / "out" / "review_queue.md").exists()
    assert (tmp_path / "out" / "data_quality_report.md").exists()


@pytest.mark.asyncio
async def test_real_run_is_not_marked_demo_and_refreshes_the_trend(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    run_dir = await _run_demo_loop(tmp_path, demo=False)
    meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["demo"] is False
    assert (tmp_path / "runs" / "trend.md").exists()


# ---- demo() defaults ----------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_command_writes_under_the_demo_dir_not_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    demo_out = tmp_path / "runs" / "demo" / "findings.json"
    monkeypatch.setattr(orchestrator, "DEFAULT_DEMO_FINDINGS_OUT", demo_out)
    root_findings = orchestrator.DEFAULT_FINDINGS_OUT
    before = root_findings.stat().st_mtime_ns if root_findings.exists() else None

    rc = await orchestrator.demo(
        [orchestrator.DEFAULT_DEMO_RULES_PATH],
        checkpoint_dir=tmp_path / "checkpoints",
        approvals_dir=tmp_path / "approvals",
    )
    assert rc == 0
    assert demo_out.exists()
    assert (demo_out.parent / "review_queue.md").exists()
    assert (demo_out.parent / "data_quality_report.md").exists()
    # The tracked root copy was not touched.
    after = root_findings.stat().st_mtime_ns if root_findings.exists() else None
    assert after == before


# ---- CLI: --demo forwards findings_out only when explicit ------------------------


class TestDemoCli:
    def _capture(self, monkeypatch, argv: list[str]) -> dict:
        calls: list[dict] = []

        async def _fake_demo(rules_path, **kwargs):
            calls.append(kwargs)
            return 0

        monkeypatch.setattr(orchestrator, "demo", _fake_demo)
        monkeypatch.setattr(orchestrator, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(sys, "argv", ["bim-orchestrator", *argv])
        assert orchestrator.main() == 0
        assert len(calls) == 1
        return calls[0]

    def test_default_findings_out_is_not_forwarded(self, monkeypatch):
        kwargs = self._capture(monkeypatch, ["--demo"])
        assert kwargs["findings_out"] is None  # demo() resolves to runs/demo/

    def test_explicit_findings_out_is_forwarded(self, monkeypatch, tmp_path):
        target = tmp_path / "mine.json"
        kwargs = self._capture(monkeypatch, ["--demo", "--findings-out", str(target)])
        assert kwargs["findings_out"] == target


# ---- audit(): mode demo through the service ----------------------------------


@pytest.mark.asyncio
async def test_audit_demo_branch_uses_the_demo_dir(monkeypatch, tmp_path):
    from bim_orchestrator import audit_axes

    rules = tmp_path / "rules.x.yaml"
    rules.write_text("scenario: x\nrules: []\n", encoding="utf-8")
    profile = tmp_path / "audit.x.yaml"
    profile.write_text(
        f"name: x\nrules:\n  - {rules.as_posix()}\nrun:\n  mode: demo\n",
        encoding="utf-8",
    )

    async def fake_axes(*_a, **_k):
        return SimpleNamespace(findings=[], skipped=[])

    seen: dict = {}

    async def fake_run_revit(*args, **kwargs):
        seen["findings_out"] = args[2]
        seen["demo"] = kwargs.get("demo")
        return 0

    monkeypatch.setattr(audit_axes, "run_audit_axes", fake_axes)
    monkeypatch.setattr(orchestrator, "run_revit", fake_run_revit)

    rc = await orchestrator.audit(
        profile,
        orchestrator.DEFAULT_AUTONOMY_PATH,
        REPO_ROOT / "findings.json",   # the service passes the tracked root path
        max_iterations=1,
        checkpoint_dir=tmp_path / "ckpt",
    )
    assert rc == 0
    assert seen["findings_out"] == orchestrator.DEFAULT_DEMO_FINDINGS_OUT
    assert seen["demo"] is True


# ---- trend + baseline skip demo folders -----------------------------------------


def _fake_run(root: Path, run_id: str, *, started: str, demo: bool) -> Path:
    d = root / run_id
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": "run-revit",
                "started_at": started,
                "finished_at": started,
                "status": "converged",
                "demo": demo,
                "outcomes_summary": {
                    "total": 10, "compliant": 8, "non_compliant": 2,
                    "manual_review": 0, "missing_data": 0,
                },
                "non_compliant_count": 2,
                "manual_review_count": 0,
                "missing_data_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (d / "outcomes.json").write_text("{}", encoding="utf-8")
    return d


def test_trend_report_leaves_demo_runs_out(tmp_path):
    _fake_run(tmp_path, "run-aaaa0001", started="2026-09-01T10:00:00", demo=False)
    _fake_run(tmp_path, "run-bbbb0002", started="2026-09-02T10:00:00", demo=True)
    text = render_trend_report(tmp_path)
    assert "run-aaaa0001" in text
    assert "run-bbbb0002" not in text


def test_delta_baseline_never_picks_a_demo_run(tmp_path):
    _fake_run(tmp_path, "run-aaaa0001", started="2026-09-01T10:00:00", demo=True)
    real_old = _fake_run(tmp_path, "run-bbbb0002", started="2026-09-02T10:00:00", demo=False)
    demo_new = _fake_run(tmp_path, "run-cccc0003", started="2026-09-03T10:00:00", demo=True)
    current = _fake_run(tmp_path, "run-dddd0004", started="2026-09-04T10:00:00", demo=False)
    # Newest earlier run is a demo — must be skipped in favour of the real one.
    assert find_baseline(tmp_path, current) == real_old
    assert find_baseline(tmp_path, demo_new) == real_old
