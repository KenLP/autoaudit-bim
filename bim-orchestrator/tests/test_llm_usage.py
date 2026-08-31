"""Phase 2 GĐ3-B1 — per-run LLM usage accounting + shared client budget.

Pins: the recorder tallies calls per agent; the metered wrapper records every
call and enforces the shared budget by raising LLMError (the same signal every
agent already degrades on); the factory builds ONE shared context only when an
LLM agent is enabled. Offline: FakeLLMClient — no network.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.llm.client import LLMError
from bim_orchestrator.llm.factory import build_llm_run_context
from bim_orchestrator.llm.fake import FakeLLMClient
from bim_orchestrator.llm.usage import (
    LLMRunContext,
    MeteredLLMClient,
    UsageRecorder,
)


# ---- UsageRecorder ---------------------------------------------------------


def test_recorder_tallies_per_agent() -> None:
    rec = UsageRecorder()
    rec.record("remediation", seconds=0.1, ok=True)
    rec.record("remediation", seconds=0.2, ok=True)
    rec.record("diagnostic", seconds=0.3, ok=False)
    assert rec.total_calls == 3
    s = rec.summary()
    assert s["by_agent"] == {"diagnostic": 1, "remediation": 2}
    assert s["blocked"] == 0


def test_recorder_format_line_none_when_idle() -> None:
    assert UsageRecorder().format_line() is None


def test_recorder_format_line_has_breakdown() -> None:
    rec = UsageRecorder()
    rec.record("remediation", seconds=1.0, ok=True)
    rec.record("supervisor", seconds=0.5, ok=True)
    line = rec.format_line()
    assert line is not None
    assert "2 calls" in line
    assert "remediation 1" in line and "supervisor 1" in line


def test_budget_check_raises_when_spent() -> None:
    rec = UsageRecorder(max_calls=2)
    rec.check("a")  # 0 < 2 → ok
    rec.record("a", seconds=0.1, ok=True)
    rec.check("a")  # 1 < 2 → ok
    rec.record("a", seconds=0.1, ok=True)
    with pytest.raises(LLMError):
        rec.check("a")  # 2 >= 2 → budget hit
    assert rec.summary()["blocked"] == 1


def test_non_positive_budget_means_no_budget() -> None:
    rec = UsageRecorder(max_calls=0)
    assert rec.max_calls is None
    for _ in range(5):
        rec.check("a")  # never raises
        rec.record("a", seconds=0.0, ok=True)


# ---- MeteredLLMClient ------------------------------------------------------


@pytest.mark.asyncio
async def test_metered_records_each_call() -> None:
    rec = UsageRecorder()
    inner = FakeLLMClient(default_text="ok", default_json={"v": 1})
    m = MeteredLLMClient(inner=inner, recorder=rec, agent="diagnostic")
    await m.complete(system="s", prompt="p")
    await m.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert rec.summary()["by_agent"] == {"diagnostic": 2}


@pytest.mark.asyncio
async def test_metered_records_even_on_inner_error() -> None:
    rec = UsageRecorder()
    inner = FakeLLMClient()  # complete_json with no canned → LLMError
    m = MeteredLLMClient(inner=inner, recorder=rec, agent="remediation")
    with pytest.raises(LLMError):
        await m.complete_json(system="s", prompt="p")
    assert rec.total_calls == 1  # the failed attempt still counts as a call


@pytest.mark.asyncio
async def test_budget_makes_the_call_degrade() -> None:
    """A spent budget raises LLMError BEFORE hitting the inner client — the
    circuit breaker. The call is 'blocked', not counted as a real call."""
    rec = UsageRecorder(max_calls=1)
    inner = FakeLLMClient(default_json={"v": 1})
    m = MeteredLLMClient(inner=inner, recorder=rec, agent="supervisor")
    await m.complete_json(system="s", prompt="p")  # 1st ok
    with pytest.raises(LLMError):
        await m.complete_json(system="s", prompt="p")  # 2nd → budget
    s = rec.summary()
    assert s["total_calls"] == 1 and s["blocked"] == 1


# ---- LLMRunContext + factory ----------------------------------------------


def test_context_shares_recorder_across_agents() -> None:
    ctx = LLMRunContext(client=FakeLLMClient(), recorder=UsageRecorder())
    a = ctx.for_agent("remediation")
    b = ctx.for_agent("diagnostic")
    assert a.recorder is b.recorder  # one shared budget/tally
    assert a.agent == "remediation" and b.agent == "diagnostic"


def test_factory_context_none_without_flags(monkeypatch) -> None:
    for f in ("BIM_LLM_REMEDIATION", "BIM_LLM_DIAGNOSTIC", "BIM_LLM_SUPERVISOR"):
        monkeypatch.delenv(f, raising=False)
    assert build_llm_run_context() is None  # pure Phase-1 → no accounting


def test_factory_context_built_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_DIAGNOSTIC", "1")
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")  # offline
    monkeypatch.delenv("BIM_LLM_MAX_CALLS", raising=False)
    ctx = build_llm_run_context()
    assert isinstance(ctx, LLMRunContext)
    # Low P2: a flag on + no budget set at all now defaults to 200, not
    # unlimited — an operator who forgets BIM_LLM_MAX_CALLS still gets a
    # circuit breaker.
    assert ctx.recorder.max_calls == 200


def test_factory_context_reads_budget_env(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_SUPERVISOR", "1")
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    monkeypatch.setenv("BIM_LLM_MAX_CALLS", "42")
    ctx = build_llm_run_context()
    assert ctx is not None and ctx.recorder.max_calls == 42


def test_factory_context_defaults_budget_to_200_when_unset(monkeypatch) -> None:
    """Low P2: any flag on + BIM_LLM_MAX_CALLS truly unset → default 200, a
    circuit breaker rather than unlimited spend."""
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    monkeypatch.delenv("BIM_LLM_MAX_CALLS", raising=False)
    ctx = build_llm_run_context()
    assert ctx is not None and ctx.recorder.max_calls == 200


def test_factory_context_explicit_invalid_budget_still_means_unlimited(monkeypatch) -> None:
    """An explicit-but-invalid value (e.g. "0") is a deliberate opt-out, distinct
    from leaving the var unset entirely — the default-200 must not override it."""
    monkeypatch.setenv("BIM_LLM_SUPERVISOR", "1")
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    monkeypatch.setenv("BIM_LLM_MAX_CALLS", "0")
    ctx = build_llm_run_context()
    assert ctx is not None and ctx.recorder.max_calls is None
