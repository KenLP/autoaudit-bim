"""M-05 / M-06 / M-07 (2026-08-01 review) — the three ways Loop 2's record
and the delta report could lie by omission.

Each of these is a "the run said nothing" bug rather than a wrong answer, so
each test asserts that something is now SAID: a stamped record, a log line, a
note on the fix, or an artifact that is not written at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.approval_store import read_record, write_record
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
from tests.test_design_agent_path_b import (
    _autonomy,
    _finding,
    _room,
    _ruleset,
    _state,
)
from tests.test_auto_gate_value_source import _rule as _autofix_rule

_EID = "829712"


# ---------------------------------------------------------------------------
# M-06 — the record is written atomically and never skipped in silence
# ---------------------------------------------------------------------------


class TestRecordDurability:
    def test_write_is_atomic_and_leaves_no_temp_files(self, tmp_path: Path) -> None:
        path = tmp_path / "issue-1.json"
        write_record(path, {"issue_id": "issue-1", "fixes": [{"a": 1}]})
        assert json.loads(path.read_text(encoding="utf-8"))["issue_id"] == "issue-1"
        # The temp file lives in the same directory (so os.replace is atomic);
        # it must not survive the write.
        assert [p.name for p in tmp_path.iterdir()] == ["issue-1.json"]

    def test_the_target_is_replaced_whole_never_written_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guarantee is "a reader sees the old record or the new one, never
        half of one" — and the only failure that can produce a half is a crash
        or power loss while the bytes are in flight, which a test runner cannot
        stage. So this pins the MECHANISM that makes the half impossible: the
        record file is never opened for writing, it is swapped in by
        ``os.replace`` from a complete temp file in the same directory (same
        filesystem, or the replace is not atomic either).

        Pinning the mechanism is the honest option here. The obvious
        behavioural test — "a failed write leaves the old record intact" —
        passes just as happily against the truncate-then-write code this fix
        replaced, because ``json.dumps`` raises before the file is ever
        opened. It reads like proof and demonstrates nothing (verified by
        mutation: the pre-fix implementation passed it).
        """
        path = tmp_path / "issue-1.json"
        write_record(path, {"issue_id": "issue-1", "fixes": [{"applied": True}]})

        replaced: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy_replace(src, dst, *a, **kw):
            replaced.append((str(src), str(dst)))
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", spy_replace)
        write_record(path, {"issue_id": "issue-1", "fixes": [{"applied": False}]})

        assert len(replaced) == 1, "the record must be swapped in, not written in place"
        src, dst = replaced[0]
        assert dst == str(path)
        assert Path(src).parent == path.parent, (
            "temp file must share the record's directory — os.replace is only "
            "atomic within one filesystem"
        )
        assert json.loads(path.read_text(encoding="utf-8"))["fixes"] == [
            {"applied": False}
        ]
        assert [p.name for p in tmp_path.iterdir()] == ["issue-1.json"]

    def test_a_serialisation_failure_leaves_no_debris(self, tmp_path: Path) -> None:
        """Weaker claim than the one above, but still worth holding: a record
        that cannot be serialised must not leave a temp file behind for the
        next scan to trip over, and must not disturb what is already there."""
        path = tmp_path / "issue-1.json"
        write_record(path, {"issue_id": "issue-1", "fixes": [{"applied": True}]})

        # `default=str` catches almost everything, so the value has to fail
        # inside str() itself to reach the error path at all.
        class Unserialisable:
            def __str__(self) -> str:
                raise RuntimeError("cannot serialise this")

            __repr__ = __str__

        with pytest.raises(RuntimeError):
            write_record(path, {"boom": Unserialisable()})

        assert json.loads(path.read_text(encoding="utf-8"))["fixes"] == [
            {"applied": True}
        ]
        assert [p.name for p in tmp_path.iterdir()] == ["issue-1.json"]

    def test_an_unreadable_record_is_reported_not_skipped_silently(
        self, tmp_path: Path
    ) -> None:
        """A truncated record used to `continue` past — indistinguishable from
        a record that never existed, so the ACC issue stayed open forever with
        nothing anywhere explaining why."""
        path = tmp_path / "issue-2.json"
        path.write_text('{"issue_id": "issue-2", "fix', encoding="utf-8")

        with structlog.testing.capture_logs() as logs:
            assert read_record(path) is None

        events = [entry["event"] for entry in logs]
        assert "approval_store.record_unreadable" in events

    def test_the_watcher_loader_uses_it(self, tmp_path: Path) -> None:
        """Pin the wire, not just the helper (the L-01 lesson)."""
        from bim_orchestrator.approval_watcher import _load_records

        (tmp_path / "good.json").write_text(
            json.dumps({"issue_id": "i-1", "fixes": [{"element_id": "1"}]}),
            encoding="utf-8",
        )
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        with structlog.testing.capture_logs() as logs:
            records = _load_records(tmp_path)

        assert [rec["issue_id"] for _, rec in records] == ["i-1"]
        assert "approval_store.record_unreadable" in [e["event"] for e in logs]


# ---------------------------------------------------------------------------
# M-05 — a proposal closed on ACC is still suppressed, but no longer in silence
# ---------------------------------------------------------------------------


async def _propose_once(
    tmp_path: Path, forma: MockFormaMCPClient, approvals: Path
) -> dict[str, Any]:
    """Run one approve-gated Path B finding through DesignAgent."""
    rule = _autofix_rule(value_strategy="inferred", autofill_strategy=None)
    agent = DesignAgent(
        mcp=forma,
        autonomy=_autonomy(tmp_path, set_value="approve"),
        project_id="b.test",
        max_issues=10,
        rule_filter=None,
        revit_mcp=MockRevitMCPClient(),
        rules=_ruleset(rule),
        approvals_dir=approvals,
    )
    state = await agent.run(
        _state(
            [_finding(rule_id=rule.id, element_id=_EID, parameter="Mark",
                      suggested="Mark-1")],
            [_room(_EID, "R1")],
        )
    )
    return state


@pytest.mark.asyncio
class TestClosedProposalIsAnnounced:
    async def test_a_closed_proposal_is_not_reproposed_but_is_stamped(
        self, tmp_path: Path
    ) -> None:
        """Owner decision 2026-08-02: closing a proposal on ACC is a decision,
        so it is NOT re-proposed — but the run must say that is what happened,
        because the record never reaches `applied` and the suppression is
        otherwise permanent and invisible."""
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])

        await _propose_once(tmp_path, forma, approvals)
        records = list(approvals.glob("*.json"))
        assert len(records) == 1, "first run must park exactly one proposal"
        issue_id = read_record(records[0])["issue_id"]
        created_first = len(forma.calls_to("create_issue"))

        # A human closes it on ACC without approving.
        for issue in forma.issues:
            if issue.get("id") == issue_id:
                issue["status"] = "closed"

        state = await _propose_once(tmp_path, forma, approvals)

        # 1. Not re-proposed.
        assert len(forma.calls_to("create_issue")) == created_first
        assert len(list(approvals.glob("*.json"))) == 1

        # 2. Said out loud. Asserted on the DURABLE surfaces (the record and
        # the fix preview the report joins), not on a captured log line:
        # `configure_logging()` — which several tests trigger via `main()` —
        # caches structlog's bound loggers, so `capture_logs` silently returns
        # nothing once the full suite has run it. A log-only assertion would
        # have passed alone and failed in CI (it did, before this rewrite).
        rec = read_record(records[0])
        assert rec["status"] == "declined"
        assert rec["issue_status"] == "closed"
        assert "declined_at" in rec and rec["declined_note"]

        previews = [f.get("preview") or {} for f in state["proposed_fixes"]]
        declined = [p for p in previews if p.get("proposal_declined")]
        assert declined, "the fix must carry the declined flag for the report"
        assert "closed on ACC" in declined[0]["proposal_note"]

    async def test_an_open_proposal_is_still_a_quiet_skip(
        self, tmp_path: Path
    ) -> None:
        """The other end: a proposal still awaiting a human is unchanged —
        no scary warning, no `declined` stamp."""
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        await _propose_once(tmp_path, forma, approvals)
        records = list(approvals.glob("*.json"))

        state = await _propose_once(tmp_path, forma, approvals)

        assert read_record(records[0])["status"] == "pending_approval"
        previews = [f.get("preview") or {} for f in state["proposed_fixes"]]
        assert not any(p.get("proposal_declined") for p in previews)

    async def test_a_status_lookup_failure_is_not_reported_as_a_human_decision(
        self, tmp_path: Path
    ) -> None:
        """`get_issue` failing means we do not KNOW — and "we could not check"
        must never render as "a human declined this" (the same honesty rule
        the geometry path learned as P1-GEO-01)."""
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        await _propose_once(tmp_path, forma, approvals)
        records = list(approvals.glob("*.json"))

        forma.fail_on.add("issues_get")
        state = await _propose_once(tmp_path, forma, approvals)

        # Unknown is not "declined": the record keeps its pending status and
        # the fix carries no declined flag, so nothing tells the reviewer a
        # human rejected something a human never saw.
        assert read_record(records[0])["status"] == "pending_approval"
        previews = [f.get("preview") or {} for f in state["proposed_fixes"]]
        assert not any(p.get("proposal_declined") for p in previews)


# ---------------------------------------------------------------------------
# L-05 — an ACC issue with no local record is worse than a duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOrphanedProposalRecovery:
    """The window between `create_issue` returning and the record being
    written is milliseconds, but what it leaves behind is nasty: an issue that
    LOOKS like a normal proposal, that a human can approve, and that does
    nothing when they do — the watcher only ever reads local records. The next
    run then can't see it either, so it raises a second issue for the same
    write-set.

    The anchor (`<fp>.creating`, written before the ACC call) is what makes
    recovery possible, and it keeps the cost at zero for healthy runs: no
    anchor on disk, no ACC lookup.
    """

    @staticmethod
    def _crash_after_create(forma: MockFormaMCPClient):
        """Let the issue be created on ACC, then die — exactly the window."""
        real = forma.create_issue

        async def create_then_die(*a, **kw):
            result = await real(*a, **kw)
            if not kw.get("dry_run", True):
                raise RuntimeError("process died before the record was written")
            return result

        forma.create_issue = create_then_die  # type: ignore[method-assign]

    async def test_an_interrupted_run_leaves_an_anchor(self, tmp_path: Path) -> None:
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        self._crash_after_create(forma)

        await _propose_once(tmp_path, forma, approvals)

        # The issue exists on ACC...
        assert len(forma.issues) == 1
        # ...no real record was written...
        assert list(approvals.glob("*.json")) == []
        # ...but the anchor is there, carrying the payload needed to recover.
        anchors = list(approvals.glob("*.creating"))
        assert len(anchors) == 1
        anchor = read_record(anchors[0])
        assert anchor["status"] == "creating"
        assert anchor["fixes"], "the anchor must carry the write-set"

    async def test_the_next_run_adopts_the_orphan_instead_of_duplicating(
        self, tmp_path: Path
    ) -> None:
        """The whole point: no second issue, AND the first one starts working."""
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        self._crash_after_create(forma)
        await _propose_once(tmp_path, forma, approvals)
        orphan_id = forma.issues[0]["id"]

        # Next run, healthy this time.
        forma.create_issue = MockFormaMCPClient.create_issue.__get__(forma)
        created_before = len(forma.issues)
        state = await _propose_once(tmp_path, forma, approvals)

        assert len(forma.issues) == created_before, "raised a duplicate issue"
        # The orphan now has a record — approving it will actually apply.
        rec = read_record(approvals / f"{orphan_id}.json")
        assert rec is not None
        assert rec["issue_id"] == orphan_id
        assert rec["adopted_from_orphan"] is True
        assert rec["fixes"], "the adopted record must carry the write-set"
        assert list(approvals.glob("*.creating")) == [], "anchor not cleared"
        previews = [f.get("preview") or {} for f in state["proposed_fixes"]]
        assert any("orphan" in (p.get("proposal_note") or "") for p in previews)

    async def test_a_healthy_run_never_asks_acc_about_orphans(
        self, tmp_path: Path
    ) -> None:
        """Cost control: the recovery path is gated on an anchor existing, so
        the normal case pays nothing — this is what makes 'reconcile' cheap
        enough to always be on."""
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        await _propose_once(tmp_path, forma, approvals)

        assert forma.calls_to("issues_list") == []
        assert list(approvals.glob("*.creating")) == [], "anchor left behind"

    async def test_a_failed_create_keeps_the_anchor_then_self_cleans(
        self, tmp_path: Path
    ) -> None:
        """A raised `create_issue` does NOT mean no issue was created — the
        response can be lost on the way back, which is the very crash this
        path exists for. So the anchor survives (unknown is not "no"), and the
        NEXT run settles it: it asks ACC, finds nothing carrying the
        fingerprint, and drops the anchor itself.

        The first draft of this test asserted the opposite — that a failed
        create clears the anchor — and it caught a real design mistake in the
        code rather than the other way round.
        """
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])

        async def always_fails(*a, **kw):
            raise RuntimeError("ACC unreachable")

        forma.create_issue = always_fails  # type: ignore[method-assign]
        await _propose_once(tmp_path, forma, approvals)

        assert list(approvals.glob("*.creating")), "evidence was discarded"
        assert list(approvals.glob("*.json")) == []

        # Next run: ACC answers, no issue carries the fingerprint → the
        # anchor is cleared and a proposal is raised normally.
        forma.create_issue = MockFormaMCPClient.create_issue.__get__(forma)
        await _propose_once(tmp_path, forma, approvals)

        assert list(approvals.glob("*.creating")) == [], "anchor never settled"
        assert len(list(approvals.glob("*.json"))) == 1
        assert len(forma.issues) == 1

    async def test_an_acc_lookup_failure_fails_open(self, tmp_path: Path) -> None:
        """Unknown is not "no orphan" — but it must not become a hole either.

        When ACC cannot be asked, raise the proposal anyway (a duplicate issue
        is noise; a missing proposal is a compliance gap) and let it complete
        normally. Documented limit: the earlier orphan stays orphaned on ACC.
        Keeping its anchor would not help — once this run writes a real record
        the cross-run dedup short-circuits before reconciliation is ever
        reached again, so the anchor could never be settled and would sit
        there forever. A stale issue someone can close by hand beats an
        unremovable file that makes every future run look interrupted.
        """
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        self._crash_after_create(forma)
        await _propose_once(tmp_path, forma, approvals)

        forma.create_issue = MockFormaMCPClient.create_issue.__get__(forma)
        forma.fail_on.add("issues_list")
        await _propose_once(tmp_path, forma, approvals)

        assert len(forma.issues) == 2, "should fail open and create"
        # The new proposal is fully usable — record written, anchor settled.
        records = list(approvals.glob("*.json"))
        assert len(records) == 1
        assert read_record(records[0])["fixes"]
        assert list(approvals.glob("*.creating")) == []
