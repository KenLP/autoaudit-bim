"""Tests for ``bim-orchestrator --demo`` (SPEC_DEMO_MODE.md).

Covers: dataset shape, a full offline E2E run through the SAME ``run_revit``
code path ``--demo`` uses (Query -> QC -> Design -> loop -> converge ->
verification report), a real convergence check (an auto-fixed element is
non-compliant at iteration 0 and compliant by iteration 1 because the mock
client mutated its own state), and the ``--demo``/``--run-revit`` CLI guard.

Fully offline / deterministic — ``build_demo_clients()`` never touches the
network, and every run here writes into a ``tmp_path``-scoped runs dir (the
module-level ``DEFAULT_RUNS_DIR`` is monkeypatched) so nothing leaks into
the real ``runs/`` folder.
"""

from __future__ import annotations

import json
import sys

import pytest

from bim_orchestrator import orchestrator
from bim_orchestrator.demo import DEMO_PROJECT_ID, build_demo_clients
from bim_orchestrator.demo.dataset import (
    _DOOR_TYPE_BAD_FMT,
    _DOOR_TYPE_BLANK_FR,
    _DOOR_TYPE_NARROW,
    _DOOR_TYPE_OK,
    _WALL_TYPE_ID,
)


def _param(element_info: dict, element_id: int, name: str):
    info = element_info[element_id]
    return next(p for p in info["parameters"] if p["name"] == name)


class TestDatasetShape:
    """§4 bullet 1 — dataset builds with the exact element/violation counts
    the spec describes."""

    def test_element_counts(self):
        revit_client, _forma_client = build_demo_clients()
        doors = revit_client.elements_by_category["OST_Doors"]
        rooms = revit_client.elements_by_category["OST_Rooms"]
        assert len(doors) == 12
        assert len(rooms) == 8

    def test_fire_rating_violations_cast_intentionally(self):
        revit_client, _forma_client = build_demo_clients()
        info = revit_client.element_info
        # Blank Fire Rating type — inherits from the host wall.
        assert _param(info, _DOOR_TYPE_BLANK_FR, "Fire Rating")["value"] == ""
        # Wrong-format type — normalizes to "2 HR".
        assert _param(info, _DOOR_TYPE_BAD_FMT, "Fire Rating")["value"] == "120 min"
        # Compliant type — already canonical (PASS set).
        assert _param(info, _DOOR_TYPE_OK, "Fire Rating")["value"] == "2 HR"
        # Host wall carries the "2 HR" value doors 701/702 inherit.
        assert _param(info, _WALL_TYPE_ID, "Fire Rating")["value"] == "2 HR"

    def test_door_width_violation(self):
        revit_client, _forma_client = build_demo_clients()
        width_param = _param(revit_client.element_info, _DOOR_TYPE_NARROW, "Width")
        # 2.0 ft = 609.6mm, well under the rule's 900mm minimum.
        assert width_param["value"] == pytest.approx(2.0)

    def test_mark_naming_violation_isolated_to_one_door(self):
        revit_client, _forma_client = build_demo_clients()
        door_ids = range(701, 713)  # 701..712
        marks = {
            eid: _param(revit_client.element_info, eid, "Mark")["value"]
            for eid in door_ids
        }
        assert marks[705] == "D 105"  # the one deliberately malformed Mark
        bad = [eid for eid, v in marks.items() if eid != 705 and " " in str(v)]
        assert bad == []

    def test_room_department_and_duplicate_number(self):
        revit_client, _forma_client = build_demo_clients()
        info = revit_client.element_info
        assert _param(info, 401, "Department")["value"] is None
        assert _param(info, 402, "Number")["value"] == "101"
        assert _param(info, 403, "Number")["value"] == "101"
        # Every other room has a unique Number + a present Department.
        numbers = [_param(info, eid, "Number")["value"] for eid in (404, 405, 406, 407, 408)]
        assert len(numbers) == len(set(numbers))
        for eid in (404, 405, 406, 407, 408):
            assert _param(info, eid, "Department")["value"]

    def test_clients_are_async_context_managers(self):
        # Sanity: run_revit's `async with revit_client_factory as ...` /
        # `async with forma_client_factory as ...` injection points require
        # this protocol — pin it so a future mock-class change fails loud.
        revit_client, forma_client = build_demo_clients()
        assert hasattr(revit_client, "__aenter__") and hasattr(revit_client, "__aexit__")
        assert hasattr(forma_client, "__aenter__") and hasattr(forma_client, "__aexit__")


class TestDemoE2E:
    """§4 bullet 2+3 — the full loop, run through ``run_revit`` exactly like
    ``--demo`` does, with real state mutation + convergence."""

    @pytest.mark.asyncio
    async def test_full_loop_converges_with_real_fixes_and_parked_proposals(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        revit_client, forma_client = build_demo_clients()

        rc = await orchestrator.run_revit(
            [orchestrator.DEFAULT_DEMO_RULES_PATH],
            orchestrator.DEFAULT_AUTONOMY_PATH,
            tmp_path / "findings.json",
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
            skip_forma=False,
            approvals_dir=tmp_path / "approvals",
            revit_client_factory=revit_client,
            forma_client_factory=forma_client,
            project_id=DEMO_PROJECT_ID,
        )
        assert rc == 0

        # write_trend_report also drops a runs/trend.md sibling — filter to
        # the run-<id>/ directories only.
        run_dirs = [p for p in (tmp_path / "runs").iterdir() if p.is_dir()]
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]

        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["status"] == "converged"
        assert metadata["project_id"] == DEMO_PROJECT_ID
        assert metadata["executed_fixes_count"] >= 1  # >=1 fix auto executed
        # SPEC_DOCUMENT_IDENTITY_STAMP (D6): the demo dataset's Revit mock
        # carries a "Demo Villa" document identity end-to-end into metadata.
        assert metadata["document"]["title"] == "Demo Villa"

        outcomes = json.loads((run_dir / "outcomes.json").read_text(encoding="utf-8"))
        fixes = outcomes["proposed_fixes"]
        assert len(fixes) > 0  # findings > 0 (non-compliant elements existed)
        parked = [f for f in fixes if not f["executed"]]
        assert any(
            (f.get("preview") or {}).get("proposal_issue_id") for f in parked
        )  # >=1 proposal parked (approve-gated)

        # v1.5-R5: exactly ONE ACC proposal issue per approve-gated rule across
        # the whole run — was 2 before the design.py dedup fix (the demo dataset
        # deliberately mixes an auto-fix with a still-parked approve-gated fix,
        # so route_node loops to iteration 1 and DesignAgent used to re-propose
        # the SAME unchanged approve-gated findings a second time).
        proposal_issue_ids_by_rule: dict[str, set[str]] = {}
        for f in parked:
            preview = f.get("preview") or {}
            issue_id = preview.get("proposal_issue_id")
            rule_id = preview.get("rule_id")
            if issue_id and rule_id:
                proposal_issue_ids_by_rule.setdefault(rule_id, set()).add(issue_id)
        assert proposal_issue_ids_by_rule  # at least one approve-gated rule was parked
        for rule_id, issue_ids in proposal_issue_ids_by_rule.items():
            assert len(issue_ids) == 1, (
                f"rule {rule_id} got {len(issue_ids)} distinct proposal issues "
                f"(expected exactly 1): {issue_ids}"
            )

        # v1.5-R5 (Path A half): same one-issue-per-rule invariant for the
        # MANUAL group (demo.doors.width_min, fixability: manual — straight to
        # an ACC Issue, no approval gate). Was 2 create_issue(dry_run=False)
        # calls before the ``_propose_rule_group`` dedup fix (route_node loops
        # to iteration 1 over the demo dataset's mixed auto/approve-gated
        # rules, re-proposing the SAME still-non-compliant width findings).
        # outcomes.json only reflects the LAST iteration's state (proposed_fixes
        # isn't an accumulating reducer key), so the duplicate is only visible
        # via the shared mock's call log, not the JSON — mirrors how the Path B
        # dedup test asserts on ``forma.calls``, not on state.
        manual_create_calls = [
            a for name, a in forma_client.calls
            if name == "create_issue" and not a["dry_run"]
            and "demo.doors.width_min" in (a.get("title") or a.get("description") or "")
        ]
        assert len(manual_create_calls) == 1, (
            f"expected exactly 1 manual ACC issue for demo.doors.width_min, "
            f"got {len(manual_create_calls)}"
        )

        report = (run_dir / "verification_report.md").read_text(encoding="utf-8")
        assert (run_dir / "verification_report.md").exists()
        assert "demo.doors.fire_rating" in report
        assert "PASS set" in report
        assert "Demo Villa" in report  # §0 Provenance's Model line

        # Real mutation, not just a preview: the auto-fixed elements actually
        # changed in the mock's in-memory state.
        assert _param(revit_client.element_info, 401, "Department")["value"] == "General"
        assert _param(revit_client.element_info, 705, "Mark")["value"] == "D_105"
        # The approve-gated fire-rating fix was NEVER executed (still parked).
        assert _param(revit_client.element_info, _DOOR_TYPE_BLANK_FR, "Fire Rating")["value"] == ""

    @pytest.mark.asyncio
    async def test_iteration_0_noncompliant_iteration_1_compliant(self, tmp_path, monkeypatch):
        """The mock genuinely mutates state — re-querying after the auto-fix
        sees a DIFFERENT (compliant) value, proving convergence is real and
        not just a status label."""
        from bim_orchestrator.agents.design import DesignAgent
        from bim_orchestrator.agents.qc import QCAgent
        from bim_orchestrator.agents.revit_query import RevitQueryAgent
        from bim_orchestrator.policies.autonomy import AutonomyPolicy
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        from bim_orchestrator.state import OrchestratorState

        revit_client, forma_client = build_demo_clients()
        autonomy = AutonomyPolicy.load(orchestrator.DEFAULT_AUTONOMY_PATH)
        qc = QCAgent(rules_path=orchestrator.DEFAULT_DEMO_RULES_PATH, autonomy=autonomy)
        catalog = OSTCatalog.load()
        query = RevitQueryAgent(mcp=revit_client, rules=qc.rules, catalog=catalog)
        design = DesignAgent(
            mcp=forma_client, autonomy=autonomy, project_id=DEMO_PROJECT_ID,
            max_issues=10, rule_filter=None, dry_run_only=False, published=False,
            issue_subtype_id=None, revit_mcp=revit_client, rules=qc.rules,
            approvals_dir=tmp_path / "approvals",
        )

        state: OrchestratorState = {
            "project_id": DEMO_PROJECT_ID, "iteration": 0, "max_iterations": 4,
            "elements": [], "findings": [], "proposed_fixes": [],
            "status": "init", "error": None,
        }

        # Iteration 0: query -> QC -> the Mark rule finding must exist.
        state = await query.run(state)
        state = qc.run(state)
        mark_findings_iter0 = [
            f for f in state["findings"] if f["rule_id"] == "demo.doors.mark_naming"
        ]
        assert len(mark_findings_iter0) == 1
        assert mark_findings_iter0[0]["element_id"] == "705"

        # Design executes the auto-fix (severity_low -> autonomy=auto).
        state = await design.run(state)
        mark_fix = next(
            f for f in state["proposed_fixes"]
            if (f.get("preview") or {}).get("rule_id") == "demo.doors.mark_naming"
        )
        assert mark_fix["executed"] is True

        # Iteration 1: re-query the SAME mock client -> the mutation persists.
        state = {**state, "iteration": 1, "findings": [], "elements": []}
        state = await query.run(state)
        state = qc.run(state)
        mark_findings_iter1 = [
            f for f in state["findings"] if f["rule_id"] == "demo.doors.mark_naming"
        ]
        assert mark_findings_iter1 == []  # compliant now — real mutation, real re-check


class TestDemoCLI:
    """§4 bullet 4 — --demo is mutually exclusive with every other mode."""

    def test_demo_and_run_revit_exits_2(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["bim-orchestrator", "--demo", "--run-revit"])
        with pytest.raises(SystemExit) as exc_info:
            orchestrator.main()
        assert exc_info.value.code == 2

    def test_demo_flag_alone_dispatches_with_demo_rules_default(self, monkeypatch):
        """``--demo`` alone (no --rules) must resolve to rules.demo.yaml, not
        the production rules.parameter_completeness.yaml default."""
        calls: list[tuple] = []

        async def _fake_demo(rules_path, **kwargs):
            calls.append((rules_path, kwargs))
            return 0

        monkeypatch.setattr(orchestrator, "demo", _fake_demo)
        monkeypatch.setattr(sys, "argv", ["bim-orchestrator", "--demo"])
        # `main()` calls configure_logging(), which globally reconfigures
        # structlog with `cache_logger_on_first_use=True` — poisoning ANY
        # module-level logger that hasn't logged yet for the rest of the test
        # session (breaks structlog.testing.capture_logs() in unrelated test
        # files). Stub it out; CLI dispatch logic is what this test verifies,
        # not logging setup.
        monkeypatch.setattr(orchestrator, "configure_logging", lambda **kwargs: None)
        rc = orchestrator.main()
        assert rc == 0
        assert len(calls) == 1
        rules_path, _kwargs = calls[0]
        assert rules_path == [orchestrator.DEFAULT_DEMO_RULES_PATH]

    @pytest.mark.asyncio
    async def test_demo_command_prints_banner_and_next_steps(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(orchestrator, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        rc = await orchestrator.demo(
            [orchestrator.DEFAULT_DEMO_RULES_PATH],
            autonomy_path=orchestrator.DEFAULT_AUTONOMY_PATH,
            findings_out=tmp_path / "findings.json",
            max_iterations=4,
            checkpoint_dir=tmp_path / "checkpoints",
            approvals_dir=tmp_path / "approvals",
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "DEMO MODE" in out
        assert "Demo Villa" in out
        assert "Next steps" in out
        assert "verification_report.md" in out


class TestDemoRerunIsQuiet:
    """A second `--demo` run must not warn about the first run's mock issues.

    The demo writes real approval records to `runs/approvals_demo/`, naming
    mock issue ids. Those records outlive the process; the mock Forma client
    does not. Before the fix, run two asked a brand-new mock about
    `issue-mock-0001`, got `not found`, and logged a warning — on the exact
    "tweak a threshold and run again" path the README tells people to walk.
    """

    def test_demo_project_id_short_circuits_the_lookup(self):
        from bim_orchestrator.policies.demo_identity import DEMO_PROJECT_ID
        import bim_orchestrator.approval_watcher as watcher

        # One constant, not two copies that can drift apart.
        assert watcher._DEMO_PROJECT_ID == DEMO_PROJECT_ID

    async def test_parked_issue_lookup_is_skipped_for_demo(self, monkeypatch):
        import asyncio  # noqa: F401  (marker for the async runner)
        from pathlib import Path

        from bim_orchestrator.agents.design import DesignAgent
        from bim_orchestrator.policies.demo_identity import DEMO_PROJECT_ID

        called: list[str] = []

        class _Mcp:
            async def get_issue(self, project_id, issue_id):
                called.append(issue_id)
                raise RuntimeError(f"issue {issue_id} not found")

        agent = DesignAgent.__new__(DesignAgent)
        agent._mcp = _Mcp()
        agent._project_id = DEMO_PROJECT_ID

        got = await agent._parked_issue_disposition("issue-mock-0001", Path("x.json"))

        assert got is None
        assert called == [], "demo run must not query the mock client at all"

    async def test_real_project_still_queries(self):
        from pathlib import Path

        from bim_orchestrator.agents.design import DesignAgent

        called: list[str] = []

        class _Mcp:
            async def get_issue(self, project_id, issue_id):
                called.append(issue_id)
                return {"issue": {"status": "open"}}

        agent = DesignAgent.__new__(DesignAgent)
        agent._mcp = _Mcp()
        agent._project_id = "b.real-project"

        await agent._parked_issue_disposition("issue-42", Path("x.json"))

        assert called == ["issue-42"], "the guard must not silence real projects"


class TestIssueCountTellsTheTruth:
    """A rerun reused two proposals and reported "ACC Issues created: 3".

    The give-away was the manual issue's id dropping from #1003 to #1001 — it
    was the FIRST issue actually created in the second process. A tool whose
    claim is that it reports what it did cannot round "reused" up to "created".
    """

    @staticmethod
    def _summary(capsys, parked, path_a=()):
        """Render the Elements → ACC Issues block for a given fix set."""
        from bim_orchestrator import orchestrator

        state = {
            "status": "converged",
            "iteration": 1,
            "outcomes_summary": {"non_compliant": 5, "missing_data": 2},
            "findings": [],
            "proposed_fixes": [*parked, *path_a],
            "fix_write_log": [],
        }
        orchestrator._print_run_revit_summary(state, dry_run_only=False)
        return capsys.readouterr().out

    def test_reused_proposals_are_not_counted_as_created(self, capsys):
        parked = [
            {"preview": {"proposal_issue_id": "i-1", "proposal_origin": "reused_parked"}},
            {"preview": {"proposal_issue_id": "i-2", "proposal_origin": "reused_parked"}},
        ]
        out = self._summary(capsys, parked)
        assert "created this run:  0" in out
        assert "reused (already open from an earlier run): 2" in out
        assert "ACC Issues created:  2" not in out, "must not claim it created them"

    def test_all_new_keeps_the_simple_line(self, capsys):
        parked = [
            {"preview": {"proposal_issue_id": "i-1", "proposal_origin": "created"}},
            {"preview": {"proposal_issue_id": "i-2", "proposal_origin": "created"}},
        ]
        out = self._summary(capsys, parked)
        assert "ACC Issues created:  2" in out
        assert "reused" not in out, "no reuse line when nothing was reused"

    def test_mixed_run_splits_them(self, capsys):
        parked = [
            {"preview": {"proposal_issue_id": "i-1", "proposal_origin": "reused_parked"}},
            {"preview": {"proposal_issue_id": "i-2", "proposal_origin": "created"}},
        ]
        out = self._summary(capsys, parked)
        assert "created this run:  1" in out
        assert "reused (already open from an earlier run): 1" in out

    def test_origin_absent_is_treated_as_created(self, capsys):
        """Back-compat: a fix from before the flag existed still counts."""
        parked = [{"preview": {"proposal_issue_id": "i-1"}}]
        out = self._summary(capsys, parked)
        assert "ACC Issues created:  1" in out
