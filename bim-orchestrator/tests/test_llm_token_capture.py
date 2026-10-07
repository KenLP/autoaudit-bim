"""The two real adapters capture the provider's token counts (2026-08-31).

Same SDK-stub technique as test_llm_providers.py: ``anthropic.AsyncAnthropic``
is monkeypatched with a class whose ``messages.create`` returns a canned
response, so nothing leaves the machine. Ollama's parser is pure, so it is
tested on dicts shaped like ``/api/chat`` replies.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.llm.anthropic_client import AnthropicLLMClient, _usage_of
from bim_orchestrator.llm.ollama_client import _ollama_usage


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens = i
        self.output_tokens = o


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str, usage: _Usage | None) -> None:
        self.content = [_Block(text)]
        if usage is not None:
            self.usage = usage


def _install_sdk_stub(monkeypatch, resp: _Resp, seen: dict) -> None:
    class _Msgs:
        async def create(self, **kw):
            seen.update(kw)
            return resp

    class _Cli:
        def __init__(self, **kw):
            self.messages = _Msgs()

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Cli)


# ---- Anthropic -------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_complete_records_usage(monkeypatch) -> None:
    client = AnthropicLLMClient(api_key="x")
    _install_sdk_stub(monkeypatch, _Resp("hi", _Usage(120, 9)), {})
    assert client.last_usage is None
    await client.complete(system="s", prompt="p")
    assert client.last_usage == (120, 9)


@pytest.mark.asyncio
async def test_anthropic_structured_records_usage(monkeypatch) -> None:
    client = AnthropicLLMClient(api_key="x")
    _install_sdk_stub(monkeypatch, _Resp('{"a": 1}', _Usage(300, 12)), {})
    out = await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert out == {"a": 1}
    assert client.last_usage == (300, 12)


@pytest.mark.asyncio
async def test_anthropic_response_without_usage_leaves_none(monkeypatch) -> None:
    """An SDK/stub that reports nothing must read as 'unknown', never as 0."""
    client = AnthropicLLMClient(api_key="x")
    _install_sdk_stub(monkeypatch, _Resp("hi", None), {})
    await client.complete(system="s", prompt="p")
    assert client.last_usage is None


@pytest.mark.asyncio
async def test_anthropic_resets_usage_before_each_call(monkeypatch) -> None:
    """A second call that fails must not leave the first call's numbers behind."""
    client = AnthropicLLMClient(api_key="x")
    _install_sdk_stub(monkeypatch, _Resp("hi", _Usage(5, 5)), {})
    await client.complete(system="s", prompt="p")
    assert client.last_usage == (5, 5)

    class _Boom:
        async def create(self, **kw):
            raise RuntimeError("network")

    class _Cli:
        def __init__(self, **kw):
            self.messages = _Boom()

    import anthropic

    from bim_orchestrator.llm.client import LLMError

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Cli)
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p")
    assert client.last_usage is None


def test_usage_of_is_defensive() -> None:
    assert _usage_of(object()) is None
    assert _usage_of(_Resp("x", _Usage(1, 2))) == (1, 2)

    class _Weird:
        usage = type("U", (), {"input_tokens": "many", "output_tokens": 1})()

    assert _usage_of(_Weird()) is None


# ---- Ollama ----------------------------------------------------------------


def test_ollama_usage_reads_eval_counts() -> None:
    assert _ollama_usage({"prompt_eval_count": 40, "eval_count": 12}) == (40, 12)


def test_ollama_usage_missing_prompt_count_is_zero_not_unknown() -> None:
    """Ollama omits prompt_eval_count on a cache hit; the reply still carried
    accounting, so the answer is (0, n), not None."""
    assert _ollama_usage({"eval_count": 12}) == (0, 12)


def test_ollama_usage_absent_entirely_is_none() -> None:
    assert _ollama_usage({"message": {"content": "hi"}}) is None
    assert _ollama_usage("not a dict") is None
