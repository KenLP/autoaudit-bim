"""P0 — a failed or never-audited run must not exit 0.

``graph.after_query`` routes a query failure straight to END, and
``route_node`` returns ``status="failed"`` on the iteration cap. The former's
docstring already promised that "the orchestrator ... maps a final
status=='failed' to a non-zero exit" — but ``run()``/``run_revit()`` returned
0 unconditionally, so the run record said *failed* while every MACHINE
consumer (shell ``$?``, CI, the service runner's ``rc == 0 -> job.status =
'done'``) read a successful audit. A compliance tool reporting "all clear"
for a model it could not read is the worst failure mode available to it.

These tests pin the mapping and its quieter twin: a ruleset whose target
categories all failed to resolve fetches nothing, finds nothing and
converges — indistinguishable, without the coverage record, from a
genuinely clean model.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from bim_orchestrator import orchestrator
from bim_orchestrator.demo import DEMO_PROJECT_ID, build_demo_clients
from bim_orchestrator.orchestrator import _exit_code_for


def _coverage(
    resolved: list[str],
    dropped: tuple[tuple[str, str], ...] = (),
    *,
    requested: tuple[str, ...] = ("Walls",),
    rule_count: int = 1,
) -> dict[str, Any]:
    return {
        "targets_requested": list(requested),
        "categories_resolved": list(resolved),
        "categories_dropped": [{"category": c, "reason": r} for c, r in dropped],
        "rule_count": rule_count,
    }


class TestDeclaredFailureExitsNonZero:
    def test_query_failure_exits_non_zero(self):
        # after_query preserved status="failed" + error all the way to END.
        state: Any = {"status": "failed", "error": "revit addin unreachable"}
        assert _exit_code_for(state) == 1

    def test_iteration_cap_exits_non_zero(self):
        # route_node's cap branch — the loop never converged.
        state: Any = {
            "status": "failed",
            "error": "max_iterations_reached (3/3)",
            "stop_reason": "iteration_cap",
        }
        assert _exit_code_for(state) == 1

    def test_converged_run_exits_zero(self):
        state: Any = {
            "status": "converged",
            "stop_reason": "zero_findings",
            "query_coverage": _coverage(["Walls"]),
        }
        assert _exit_code_for(state) == 0

    def test_failure_wins_over_coverage(self):
        # A failed status short-circuits: no need to consult coverage.
        state: Any = {
            "status": "failed",
            "query_coverage": _coverage(["Walls"]),
        }
        assert _exit_code_for(state) == 1


class TestQueryCoverageGate:
    def test_every_category_dropped_exits_non_zero(self, capsys):
        state: Any = {
            "status": "converged",
            "stop_reason": "zero_findings",
            "query_coverage": _coverage([], (("Bananas", "unresolved_on_revit"),)),
        }
        assert _exit_code_for(state) == 1
        out = capsys.readouterr().out
        assert "No audit was performed" in out
        assert "Bananas" in out            # the operator learns WHICH category

    def test_partial_coverage_warns_but_succeeds(self, capsys):
        # Some categories resolved: an incomplete but real audit — the run
        # stands, the gap is stated out loud.
        state: Any = {
            "status": "converged",
            "query_coverage": _coverage(
                ["Walls"],
                (("Bananas", "unresolved_on_revit"),),
                requested=("Walls", "Bananas"),
                rule_count=2,
            ),
        }
        assert _exit_code_for(state) == 0
        out = capsys.readouterr().out
        assert "Partial coverage" in out
        assert "Bananas" in out

    def test_ruleset_with_no_target_category_is_also_no_audit(self):
        """L4 (audit): the second 'never audited' route.

        Gating on `targets_requested` being non-empty missed a ruleset that HAS
        rules but an empty `target_category` (the schema permits "" / []): it
        requests nothing, resolves nothing, fetches nothing — and used to exit 0.
        What matters is that rules existed and nothing resolved.
        """
        state: Any = {
            "status": "converged",
            "query_coverage": _coverage([], requested=(), rule_count=3),
        }
        assert _exit_code_for(state) == 1

    def test_partial_coverage_fails_when_the_caller_opts_in(self, capsys):
        """Owner decision 2026-07-25: strictness is opt-IN, not the default.

        A partial run IS a real audit, so flipping the default would turn every
        existing scheduled run red on day one and just teach everyone to pass
        the override. An unattended production audit wants the opposite — a
        silently narrowed scope must page someone — so it asks for it.
        """
        state: Any = {
            "status": "converged",
            "query_coverage": _coverage(
                ["Walls"],
                (("Bananas", "unresolved_on_revit"),),
                requested=("Walls", "Bananas"),
                rule_count=2,
            ),
        }
        assert _exit_code_for(state) == 0                      # default unchanged
        assert _exit_code_for(state, fail_on_partial_coverage=True) == 1
        assert "Bananas" in capsys.readouterr().out

    def test_opt_in_strictness_does_not_affect_a_full_coverage_run(self):
        state: Any = {"status": "converged", "query_coverage": _coverage(["Walls"])}
        assert _exit_code_for(state, fail_on_partial_coverage=True) == 0

    def test_no_audit_still_fails_regardless_of_the_flag(self):
        # The flag only governs PARTIAL. "Nothing was audited" is never a pass.
        state: Any = {
            "status": "converged",
            "query_coverage": _coverage([], (("X", "unresolved_on_revit"),)),
        }
        assert _exit_code_for(state, fail_on_partial_coverage=False) == 1

    def test_empty_ruleset_is_not_a_coverage_failure(self):
        # The legacy categories= path and rulesets with no parameter rules
        # legitimately derive zero specs — that is a no-op, not a lie.
        state: Any = {
            "status": "converged",
            "query_coverage": _coverage([], requested=(), rule_count=0),
        }
        assert _exit_code_for(state) == 0

    def test_missing_coverage_record_is_tolerated(self):
        # Pre-R7 runs and older checkpoints carry no coverage record.
        state: Any = {"status": "converged"}
        assert _exit_code_for(state) == 0


class _RaisingRevitClient:
    """Async-context-manager Revit client whose every call raises — models the
    addin being unreachable, so RevitQueryAgent returns status="failed"."""

    async def __aenter__(self) -> "_RaisingRevitClient":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    def __getattr__(self, _name: str):
        async def _boom(*_a: Any, **_k: Any):
            raise RuntimeError("revit addin unreachable (test)")
        return _boom


async def _drive_run_revit(rules_path, tmp_path, *, revit_client):
    """Drive the REAL run_revit() with mock clients — the same injection points
    --demo uses — so the test exercises orchestrator → graph → query → coverage
    → _exit_code_for → recorded metadata at the actual bug site."""
    _, forma_client = build_demo_clients()
    orchestrator.DEFAULT_RUNS_DIR  # touch to ensure module import ok
    return await orchestrator.run_revit(
        [rules_path],
        orchestrator.DEFAULT_AUTONOMY_PATH,
        tmp_path / "findings.json",
        limit=10, rule_filter=None, dry_run_only=True, published=False,
        issue_subtype_id=None, max_iterations=2,
        checkpoint_dir=tmp_path / "checkpoints",
        bep_pdf=None, use_bep_fixture=False, vector_store_dir=None,
        skip_forma=False, approvals_dir=tmp_path / "approvals",
        revit_client_factory=revit_client, forma_client_factory=forma_client,
        project_id=DEMO_PROJECT_ID,
    )


def _read_run_metadata(runs_dir):
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("run-")]
    assert len(run_dirs) == 1, run_dirs
    return json.loads((run_dirs[0] / "metadata.json").read_text(encoding="utf-8"))


class TestExitCodeWiringSpanning:
    """M4 (2026-07 audit): the disjoint unit tests above all pass even if
    run_revit()'s `return _exit_code_for(final_state)` is reverted to `return 0`
    — the wiring at the bug site was unprotected. These drive the REAL run_revit
    end to end and also assert the recorded metadata, so a silent regression of
    either the exit code OR the recorded status fails a test.
    """

    @pytest.mark.asyncio
    async def test_query_failure_spans_to_nonzero_exit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        rules_path = tmp_path / "rules.doors.yaml"
        rules_path.write_text(yaml.safe_dump({
            "scenario": "doors", "target_category": "Doors",
            "rules": [{
                "id": "d.mark", "parameter": "Mark",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param", "description": "mark",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }), encoding="utf-8")
        rc = await _drive_run_revit(
            rules_path, tmp_path, revit_client=_RaisingRevitClient()
        )
        assert rc != 0
        meta = _read_run_metadata(tmp_path / "runs")
        assert meta["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_audit_coverage_spans_to_nonzero_exit_and_metadata(
        self, tmp_path, monkeypatch
    ):
        # A non-empty ruleset whose only category is unknown → derive_specs
        # drops it → zero elements → zero findings → graph converges. Without
        # the coverage gate this is a clean exit-0 "converged" run for a model
        # that was NEVER audited.
        monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        revit_client, _ = build_demo_clients()
        rules_path = tmp_path / "rules.bogus.yaml"
        rules_path.write_text(yaml.safe_dump({
            "scenario": "bogus", "target_category": "Bananas",
            "rules": [{
                "id": "b.x", "parameter": "X",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param", "description": "x",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }), encoding="utf-8")
        rc = await _drive_run_revit(rules_path, tmp_path, revit_client=revit_client)
        assert rc != 0                          # never-audited must not exit 0
        meta = _read_run_metadata(tmp_path / "runs")
        # M2: the durable artifact must not show a clean "converged" run.
        assert meta["status"] == "no_audit"
        assert meta["coverage_status"] == "no_audit"
        assert meta["query_coverage"]["categories_resolved"] == []

    @pytest.mark.asyncio
    async def test_report_and_metadata_agree_on_a_no_audit_run(
        self, tmp_path, monkeypatch
    ):
        """P1-07: the two artifacts of one run must not contradict each other.

        metadata.json got the "no_audit" downgrade in the previous round, but
        verification_report.md rendered the graph's internal loop state and no
        coverage at all — so the document a person actually signs still read as
        a completed, full audit.
        """
        monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        revit_client, _ = build_demo_clients()
        rules_path = tmp_path / "rules.bogus.yaml"
        rules_path.write_text(yaml.safe_dump({
            "scenario": "bogus", "target_category": "Bananas",
            "rules": [{
                "id": "b.x", "parameter": "X",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param", "description": "x",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }), encoding="utf-8")
        await _drive_run_revit(rules_path, tmp_path, revit_client=revit_client)

        run_dir = next(
            p for p in (tmp_path / "runs").iterdir()
            if p.is_dir() and p.name.startswith("run-")
        )
        meta = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        report = (run_dir / "verification_report.md").read_text(encoding="utf-8")

        assert meta["status"] == "no_audit"
        assert f"**Final status:** {meta['status']}" in report   # same string
        assert "NO AUDIT WAS PERFORMED" in report
        assert "Bananas" in report

    @pytest.mark.asyncio
    async def test_coverage_diagnostics_run_before_the_trace_is_written(
        self, tmp_path, monkeypatch
    ):
        """L5 (audit): diagnostics must be resolved while the trace tap is live.

        `_finish_run_recording` writes trace.md and THEN drops the structlog
        tap, so a coverage diagnostic logged after it reached the console only —
        never the run's own captured trace, which is the artifact a reviewer
        actually reads. Asserted as call ORDER rather than trace content because
        the trace processor is installed by the CLI's `configure_logging()`,
        which caches loggers process-wide and cannot be undone inside a test.
        """
        monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        order: list[str] = []
        real_exit = orchestrator._exit_code_for
        real_finish = orchestrator._finish_run_recording

        def _spy_exit(state, **kw):
            order.append("diagnostics")
            return real_exit(state, **kw)

        def _spy_finish(*a, **k):
            order.append("trace_written")
            return real_finish(*a, **k)

        monkeypatch.setattr(orchestrator, "_exit_code_for", _spy_exit)
        monkeypatch.setattr(orchestrator, "_finish_run_recording", _spy_finish)

        revit_client, _ = build_demo_clients()
        rules_path = tmp_path / "rules.bogus.yaml"
        rules_path.write_text(yaml.safe_dump({
            "scenario": "bogus", "target_category": "Bananas",
            "rules": [{
                "id": "b.x", "parameter": "X",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param", "description": "x",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }), encoding="utf-8")
        await _drive_run_revit(rules_path, tmp_path, revit_client=revit_client)

        assert order == ["diagnostics", "trace_written"]
