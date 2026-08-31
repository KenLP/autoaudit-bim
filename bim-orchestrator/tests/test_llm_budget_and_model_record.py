"""A-03 / P2-02 / P2-04 — findings from the 2026-07-25 orchestrated live review."""

from __future__ import annotations

import asyncio

import pytest

from bim_orchestrator.llm.client import LLMClient, LLMError
from bim_orchestrator.llm.usage import LLMRunContext, MeteredLLMClient, UsageRecorder


class _Client(LLMClient):
    """Inner client that blocks until released, so overlap is deterministic."""

    def __init__(self, model=None, gate=None):
        self.model = model
        self._gate = gate
        self.calls = 0

    async def complete(self, *, system, prompt, max_tokens=512):
        self.calls += 1
        if self._gate is not None:
            await self._gate.wait()
        return "ok"

    async def complete_json(self, *, system, prompt, schema=None, max_tokens=512):
        return {}


class TestBudgetIsAHardCeiling:
    """P2-04 — check()/record() used to be TOCTOU: two unlocked reads, so under
    C concurrent callers the real ceiling was ``max_calls + (C - 1)``.

    That was documented as accepted slop. The reasoning held only because
    nothing issues LLM calls concurrently TODAY — a property of the call sites,
    not of this class. The first parallel remediation would have overspent a
    paid API silently, on a product sold on auditability. check() now reserves
    under a lock and record() settles.
    """

    def test_concurrent_callers_cannot_exceed_the_budget(self):
        async def go():
            gate = asyncio.Event()
            inner = _Client(model="m", gate=gate)
            rec = UsageRecorder(max_calls=2)
            metered = MeteredLLMClient(inner=inner, recorder=rec, agent="a")

            async def one():
                try:
                    await metered.complete(system="s", prompt="p")
                    return "ok"
                except LLMError:
                    return "blocked"

            # 5 callers all in flight BEFORE any of them records — the exact
            # window the old code let through.
            tasks = [asyncio.create_task(one()) for _ in range(5)]
            await asyncio.sleep(0)          # let them all reach the inner call
            gate.set()
            results = await asyncio.gather(*tasks)
            return inner.calls, results

        calls, results = asyncio.run(go())
        assert calls == 2, f"budget of 2 allowed {calls} real API calls"
        assert results.count("blocked") == 3

    def test_a_failed_call_releases_its_reservation(self):
        # The reservation must not leak on the error path, or a run that hits
        # transient failures would strangle its own remaining budget.
        class _Boom(_Client):
            async def complete(self, *, system, prompt, max_tokens=512):
                raise LLMError("upstream 500")

        async def go():
            rec = UsageRecorder(max_calls=3)
            metered = MeteredLLMClient(inner=_Boom(), recorder=rec, agent="a")
            for _ in range(3):
                with pytest.raises(LLMError):
                    await metered.complete(system="s", prompt="p")
            return rec

        rec = asyncio.run(go())
        # 3 attempts, all settled (counted as completed calls), none stuck
        # in-flight — a 4th is refused by the budget, not by a leak.
        assert rec.total_calls == 3
        assert rec._inflight == 0

    def test_no_budget_means_no_ceiling(self):
        async def go():
            inner = _Client(model="m")
            rec = UsageRecorder()          # max_calls=None
            metered = MeteredLLMClient(inner=inner, recorder=rec, agent="a")
            await asyncio.gather(*[metered.complete(system="s", prompt="p")
                                   for _ in range(10)])
            return inner.calls
        assert asyncio.run(go()) == 10

    def test_unreserved_record_cannot_hand_out_a_free_slot(self):
        # The extraction seam records WITHOUT reserving (budget-exempt by
        # design). That must not drive the in-flight count negative, which
        # would silently widen the budget for everyone else.
        rec = UsageRecorder(max_calls=1)
        rec.record("extraction", seconds=0.1, ok=True)
        rec.record("extraction", seconds=0.1, ok=True)
        assert rec._inflight == 0
        with pytest.raises(LLMError):
            rec.check("remediation")


class TestModelIdIsRecorded:
    """P2-02 — model ids are largely floating aliases; a provider can re-point
    one at a new checkpoint and every later run behaves differently with
    nothing in the record to show it. Pinning every default to a dated
    snapshot is not possible (not all publish one), so the general mitigation
    is to RECORD what actually ran.
    """

    def test_metered_calls_record_the_inner_model(self):
        async def go():
            inner = _Client(model="claude-haiku-4-5-20251001")
            rec = UsageRecorder()
            await MeteredLLMClient(
                inner=inner, recorder=rec, agent="remediation"
            ).complete(system="s", prompt="p")
            return rec
        rec = asyncio.run(go())
        assert rec.models == ["claude-haiku-4-5-20251001"]
        assert rec.summary()["models"] == ["claude-haiku-4-5-20251001"]

    def test_model_appears_on_the_operator_facing_line(self):
        # "which model produced this?" is the first question when a run
        # behaves differently from yesterday's — it belongs on screen, not
        # only in an artifact nobody opens.
        async def go():
            rec = UsageRecorder()
            await MeteredLLMClient(
                inner=_Client(model="m-1"), recorder=rec, agent="a"
            ).complete(system="s", prompt="p")
            return rec.format_line()
        assert "model m-1" in asyncio.run(go())

    def test_several_models_in_one_run_are_all_kept(self):
        # Extraction deliberately uses a stronger model than the runtime
        # agents, so one run legitimately spans two — the record must show
        # both, not whichever landed last.
        rec = UsageRecorder()
        rec.record("remediation", seconds=0.1, ok=True, model="haiku-x")
        rec.record("extraction", seconds=0.1, ok=True, model="sonnet-y")
        assert rec.models == ["haiku-x", "sonnet-y"]

    def test_a_client_without_a_model_records_nothing_extra(self):
        async def go():
            rec = UsageRecorder()
            await MeteredLLMClient(
                inner=_Client(model=None), recorder=rec, agent="a"
            ).complete(system="s", prompt="p")
            return rec
        rec = asyncio.run(go())
        assert rec.models == []
        assert "model" not in (rec.format_line() or "")

    def test_cloud_default_is_a_dated_snapshot_not_a_floating_alias(self):
        from bim_orchestrator.llm.factory import _DEFAULT_ANTHROPIC_MODEL

        # The point of the finding: a bare family alias silently re-points.
        assert _DEFAULT_ANTHROPIC_MODEL != "claude-haiku-4-5"
        assert _DEFAULT_ANTHROPIC_MODEL[-8:].isdigit(), (
            "default model must carry a dated snapshot suffix"
        )

    def test_the_client_default_matches_the_factory_default(self):
        # Two places name a default; they drifting apart would mean the
        # pinned id depends on which construction path you took.
        from bim_orchestrator.llm.anthropic_client import AnthropicLLMClient
        from bim_orchestrator.llm.factory import _DEFAULT_ANTHROPIC_MODEL

        assert AnthropicLLMClient(api_key="x").model == _DEFAULT_ANTHROPIC_MODEL

    def test_run_context_threads_the_model_through_for_every_agent(self):
        async def go():
            ctx = LLMRunContext(client=_Client(model="m-9"), recorder=UsageRecorder())
            for agent in ("remediation", "diagnostic", "supervisor"):
                await ctx.for_agent(agent).complete(system="s", prompt="p")
            return ctx.recorder
        rec = asyncio.run(go())
        assert rec.models == ["m-9"]
        assert set(rec.summary()["by_agent"]) == {
            "remediation", "diagnostic", "supervisor"
        }
