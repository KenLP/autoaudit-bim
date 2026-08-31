"""P1-TRUST-01 — "executed" must mean Revit said so.

Three live-write paths each decided for themselves whether a batch had
committed, and all three treated *the envelope didn't say otherwise* as
success. Missing `results` was read as **trust the commit**, and `committed`
was never consulted at all — so an addin returning `{ok: true}` and nothing
else got every write marked applied, the approval record closed, and the ACC
issue closed with it, over a model that could still hold the old values.

Owner decision 2026-07-26, option (a): unconfirmed is NOT applied — the record
stays pending and the issue stays open.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.mcp_clients.revit import batch_commit_outcomes


def _ok_results(n):
    return {"ok": True, "committed": True, "data": {"results": [{"ok": True}] * n}}


class TestExplicitNegativeBlocks:
    def test_committed_false_blocks_every_step(self):
        """The headline case: the addin says nothing landed, and the old code
        marked all of it applied because `ok` was true."""
        env = {"ok": True, "committed": False, "data": {"results": [{"ok": True}] * 2}}
        outcomes = batch_commit_outcomes(env, expected=2)
        assert [o.ok for o in outcomes] == [False, False]
        assert all(o.reason == "not_committed" for o in outcomes)

    def test_committed_false_wins_over_healthy_per_step_results(self):
        # Per-step results claiming success cannot override an explicit
        # transaction-level "nothing was committed".
        env = {"ok": True, "committed": False,
               "data": {"results": [{"ok": True, "changes": {"after": "X"}}]}}
        assert batch_commit_outcomes(env, expected=1)[0].ok is False


class TestMissingConfirmationIsNotSuccess:
    def test_no_results_at_all_is_unconfirmed(self):
        # Option (a). This is the shape that closed ACC issues over unwritten
        # models: `ok: true` and nothing else.
        outcomes = batch_commit_outcomes({"ok": True}, expected=3)
        assert [o.ok for o in outcomes] == [False, False, False]
        assert all(o.reason == "no_step_results" for o in outcomes)

    def test_short_results_only_confirms_what_it_covers(self):
        # A truncated result list confirms its own entries and nothing more —
        # blanket-applying the tail is exactly the old bug in miniature.
        env = {"ok": True, "committed": True, "data": {"results": [{"ok": True}]}}
        outcomes = batch_commit_outcomes(env, expected=3)
        assert [o.ok for o in outcomes] == [True, False, False]
        assert outcomes[1].reason == "missing_step_result"

    def test_a_failed_step_is_reported_with_its_own_error(self):
        env = {"ok": True, "committed": True, "data": {"results": [
            {"ok": True}, {"ok": False, "error": "read_only_parameter"},
        ]}}
        outcomes = batch_commit_outcomes(env, expected=2)
        assert outcomes[0].ok is True
        assert outcomes[1].ok is False
        assert "read_only_parameter" in outcomes[1].reason

    def test_step_carrying_an_error_key_is_not_ok_even_without_ok_false(self):
        env = {"ok": True, "committed": True,
               "data": {"results": [{"error": "element not found"}]}}
        assert batch_commit_outcomes(env, expected=1)[0].ok is False

    @pytest.mark.parametrize("env", [None, "text", [], 42])
    def test_a_malformed_envelope_confirms_nothing(self, env):
        outcomes = batch_commit_outcomes(env, expected=2)
        assert [o.ok for o in outcomes] == [False, False]
        assert all(o.reason == "malformed_envelope" for o in outcomes)

    def test_malformed_step_entry_is_not_ok(self):
        env = {"ok": True, "committed": True, "data": {"results": ["fine?"]}}
        assert batch_commit_outcomes(env, expected=1)[0].reason == "malformed_step_result"


class TestAbsentCommittedIsNotANegative:
    """`committed` absent is NOT `committed=False`, and the distinction is
    load-bearing.

    The stdio transport and older addins never sent the field; treating its
    silence as failure would strand every write on those paths — a different
    lie, in the safe-looking direction. Silence falls through to the per-step
    results, which are the stronger evidence anyway. The "confirms nothing"
    envelope is still caught, by the results rule above.
    """

    def test_results_alone_are_sufficient_confirmation(self):
        env = {"ok": True, "data": {"results": [{"ok": True}, {"ok": True}]}}
        assert [o.ok for o in batch_commit_outcomes(env, expected=2)] == [True, True]

    def test_top_level_results_are_read_too(self):
        # RevitHTTPClient.batch mirrors top-level `results` into `data`, but a
        # future/stdio envelope may only carry the top-level key.
        env = {"ok": True, "committed": True, "results": [{"ok": True}]}
        assert batch_commit_outcomes(env, expected=1)[0].ok is True

    def test_absent_committed_with_no_results_is_still_unconfirmed(self):
        assert batch_commit_outcomes({"ok": True}, expected=1)[0].ok is False


class TestHealthyCommitStillWorks:
    """Guard: the point is to stop lying, not to stop working. A normal
    successful batch must still mark every write applied."""

    def test_all_confirmed(self):
        outcomes = batch_commit_outcomes(_ok_results(4), expected=4)
        assert all(o.ok for o in outcomes)
        assert all(o.reason is None for o in outcomes)

    def test_expected_zero_returns_nothing(self):
        assert batch_commit_outcomes(_ok_results(0), expected=0) == []


class TestWatcherHonoursTheValidator:
    """The wire, not just the mechanism: the watcher must not close an ACC
    issue on an unconfirmed commit."""

    @pytest.mark.asyncio
    async def test_uncommitted_batch_applies_nothing(self):
        from bim_orchestrator.approval_watcher import _commit_writes

        class _Revit:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                return {"ok": True, "committed": False, "data": {}}

        fixes = [{"element_id": "1", "parameter": "Mark", "new_value": "A",
                  "action": "set_parameter"}]
        applied = await _commit_writes(_Revit(), fixes)
        assert applied == 0
        assert not fixes[0].get("applied")

    @pytest.mark.asyncio
    async def test_confirmed_batch_still_applies(self):
        from bim_orchestrator.approval_watcher import _commit_writes

        class _Revit:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                return {"ok": True, "committed": True,
                        "data": {"results": [{"ok": True}] * len(steps)}}

        fixes = [{"element_id": "1", "parameter": "Mark", "new_value": "A",
                  "action": "set_parameter"}]
        applied = await _commit_writes(_Revit(), fixes)
        assert applied == 1
        assert fixes[0]["applied"] is True
