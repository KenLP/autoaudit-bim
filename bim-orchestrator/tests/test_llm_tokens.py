"""Token accounting on the LLM usage recorder (2026-08-31).

Calls were counted from GĐ3-B1; tokens were not — the provider's usage block
was dropped in every adapter, so "how much did those three agents cost?" had
no answer in the artifact. Pins: the recorder sums per agent, absent usage is
NOT counted as zero, the summary/format line carry the totals, and the metered
wrapper reads the inner client's ``last_usage`` in its ``finally``.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.llm.client import LLMClient, LLMError
from bim_orchestrator.llm.fake import FakeLLMClient
from bim_orchestrator.llm.usage import MeteredLLMClient, UsageRecorder


def test_tokens_sum_per_agent_and_in_total() -> None:
    rec = UsageRecorder()
    rec.record("remediation", seconds=0.1, ok=True, tokens=(100, 20))
    rec.record("remediation", seconds=0.1, ok=True, tokens=(50, 5))
    rec.record("diagnostic", seconds=0.1, ok=True, tokens=(10, 1))
    assert rec.input_tokens == 160
    assert rec.output_tokens == 26
    assert rec.total_tokens == 186
    s = rec.summary()
    assert s["total_tokens"] == 186
    assert s["input_tokens"] == 160 and s["output_tokens"] == 26
    assert s["tokens_by_agent"] == {
        "diagnostic": {"input": 10, "output": 1},
        "remediation": {"input": 150, "output": 25},
    }


def test_absent_usage_is_not_zero() -> None:
    """A call the provider didn't account for must not read as 'cost 0'."""
    rec = UsageRecorder()
    rec.record("supervisor", seconds=0.1, ok=True, tokens=None)
    assert rec.total_calls == 1
    assert rec.total_tokens == 0
    assert rec.summary()["tokens_by_agent"] == {}
    line = rec.format_line()
    assert line is not None and "tokens" not in line


def test_format_line_names_tokens_when_reported() -> None:
    rec = UsageRecorder()
    rec.record("diagnostic", seconds=1.0, ok=True, tokens=(11900, 2420))
    line = rec.format_line()
    assert line is not None
    assert "14,320 tokens (in 11,900 / out 2,420)" in line
    assert "1 calls" in line


def test_negative_or_garbage_counts_are_clamped() -> None:
    rec = UsageRecorder()
    rec.record("a", seconds=0.0, ok=True, tokens=(-5, 3))
    assert rec.input_tokens == 0 and rec.output_tokens == 3


class _CountingClient(LLMClient):
    """Inner client that reports usage the way a real adapter does: reset
    before the call, filled after — and left None when the call raises."""

    model = "stub-model"

    def __init__(self, *, fail_json: bool = False) -> None:
        self._fail_json = fail_json

    async def complete(self, *, system: str, prompt: str, max_tokens: int = 512) -> str:
        self.last_usage = None
        self.last_usage = (len(prompt), 7)
        return "ok"

    async def complete_json(self, *, system: str, prompt: str, schema=None, max_tokens: int = 512):
        self.last_usage = None
        if self._fail_json:
            raise LLMError("boom")
        self.last_usage = (3, 4)
        return {"v": 1}


@pytest.mark.asyncio
async def test_metered_client_forwards_inner_usage_to_recorder() -> None:
    rec = UsageRecorder()
    m = MeteredLLMClient(inner=_CountingClient(), recorder=rec, agent="remediation")
    await m.complete(system="s", prompt="hello")
    await m.complete_json(system="s", prompt="p")
    assert rec.summary()["tokens_by_agent"] == {"remediation": {"input": 8, "output": 11}}


@pytest.mark.asyncio
async def test_failed_call_bills_no_tokens() -> None:
    """The adapter clears ``last_usage`` before each request, so a call that
    raises cannot be settled with the previous call's numbers."""
    rec = UsageRecorder()
    inner = _CountingClient(fail_json=True)
    m = MeteredLLMClient(inner=inner, recorder=rec, agent="remediation")
    await m.complete(system="s", prompt="ab")  # (2, 7)
    with pytest.raises(LLMError):
        await m.complete_json(system="s", prompt="p")
    assert rec.total_calls == 2 and rec.failed_calls == 1
    assert rec.total_tokens == 9  # only the first call's usage


@pytest.mark.asyncio
async def test_fake_client_reports_nothing_and_that_is_fine() -> None:
    rec = UsageRecorder()
    m = MeteredLLMClient(inner=FakeLLMClient(default_text="x"), recorder=rec, agent="d")
    await m.complete(system="s", prompt="p")
    assert rec.total_calls == 1 and rec.total_tokens == 0
