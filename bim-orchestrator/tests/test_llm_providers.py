"""Phase 2 GĐ1 — provider switch, structured outputs, schema sanitizer.

Offline: no Ollama server, no anthropic SDK call — the network method (`_chat`)
and the structured/legacy split points are monkeypatched, so these exercise the
adapter logic (retry, fallback, provider selection) deterministically.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.llm.anthropic_client import AnthropicLLMClient
from bim_orchestrator.llm.client import LLMError, sanitize_json_schema
from bim_orchestrator.llm.factory import build_llm_client, llm_provider
from bim_orchestrator.llm.fake import FakeLLMClient
from bim_orchestrator.llm.ollama_client import OllamaLLMClient


# ---- sanitize_json_schema --------------------------------------------------


def test_sanitize_strips_bounds_and_forces_required() -> None:
    schema = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["reason"],
    }
    out = sanitize_json_schema(schema)
    assert "minimum" not in out["properties"]["confidence"]
    assert "maximum" not in out["properties"]["confidence"]
    assert "minLength" not in out["properties"]["reason"]
    assert out["additionalProperties"] is False
    assert set(out["required"]) == {"reason", "confidence"}  # all props required


def test_sanitize_recurses_into_nested_objects() -> None:
    schema = {
        "type": "object",
        "properties": {
            "inner": {
                "type": "object",
                "properties": {"x": {"type": "integer", "maximum": 9}},
            },
        },
    }
    out = sanitize_json_schema(schema)
    inner = out["properties"]["inner"]
    assert inner["additionalProperties"] is False
    assert "maximum" not in inner["properties"]["x"]


# ---- provider switch -------------------------------------------------------


def test_provider_defaults_to_anthropic(monkeypatch) -> None:
    monkeypatch.delenv("BIM_LLM_PROVIDER", raising=False)
    assert llm_provider() == "anthropic"
    assert isinstance(build_llm_client(), AnthropicLLMClient)


def test_provider_ollama_builds_local_client(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("BIM_LLM_BASE_URL", "http://box:11434")
    monkeypatch.setenv("BIM_LLM_MODEL", "qwen3:14b")
    client = build_llm_client()
    assert isinstance(client, OllamaLLMClient)
    assert client._base_url == "http://box:11434"
    assert client._model == "qwen3:14b"


def test_provider_fake_is_offline_escape_hatch(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    assert llm_provider() == "fake"
    assert isinstance(build_llm_client(), FakeLLMClient)


@pytest.mark.asyncio
async def test_fake_provider_degrades_every_agent(monkeypatch) -> None:
    """The Wi-Fi-died switch: complete_json raises (agents skip / route to human),
    complete returns "" (no proposal survives validation) — no crash, no network."""
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    client = build_llm_client()
    assert await client.complete(system="s", prompt="p") == ""
    with pytest.raises(LLMError):
        await client.complete_json(system="s", prompt="p", schema={"type": "object"})


# ---- Anthropic determinism + bounded latency (rehearsal == live) -----------


def test_anthropic_defaults_temperature_zero_and_bounded_timeout() -> None:
    client = AnthropicLLMClient(api_key="x")
    assert client._temperature == 0.0  # rehearsal == live
    assert client._timeout < 600  # not the SDK's default 600s hang
    assert client._max_retries >= 1


@pytest.mark.asyncio
async def test_anthropic_passes_temperature_and_bounds_to_sdk(monkeypatch) -> None:
    client = AnthropicLLMClient(api_key="x", timeout=12.0, max_retries=3)
    seen: dict = {}

    class _Msgs:
        async def create(self, **kw):
            seen.update(kw)

            class _B:
                type = "text"
                text = "hi"

            class _R:
                content = [_B()]

            return _R()

    class _Cli:
        def __init__(self, **kw):
            seen["_ctor"] = kw
            self.messages = _Msgs()

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Cli)
    await client.complete(system="s", prompt="p")
    assert seen["temperature"] == 0.0
    assert seen["_ctor"]["timeout"] == 12.0
    assert seen["_ctor"]["max_retries"] == 3


# ---- Ollama complete_json: retry / parse / fail-closed ---------------------


@pytest.mark.asyncio
async def test_ollama_parses_valid_json(monkeypatch) -> None:
    client = OllamaLLMClient()

    async def fake_chat(**kw):
        return '{"reason": "ok", "action": "continue"}'

    monkeypatch.setattr(client, "_chat", fake_chat)
    out = await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert out["action"] == "continue"


@pytest.mark.asyncio
async def test_ollama_recovers_on_retry(monkeypatch) -> None:
    client = OllamaLLMClient(max_attempts=3)
    calls = {"n": 0}

    async def flaky_chat(**kw):
        calls["n"] += 1
        return "not json{" if calls["n"] == 1 else '{"ok": true}'

    monkeypatch.setattr(client, "_chat", flaky_chat)
    out = await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert out == {"ok": True}
    assert calls["n"] == 2  # retried once


@pytest.mark.asyncio
async def test_ollama_fails_closed_after_max_attempts(monkeypatch) -> None:
    client = OllamaLLMClient(max_attempts=2)
    calls = {"n": 0}

    async def bad_chat(**kw):
        calls["n"] += 1
        return "still not json"

    monkeypatch.setattr(client, "_chat", bad_chat)
    with pytest.raises(LLMError):
        await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert calls["n"] == 2  # bounded, no infinite retry


# ---- Anthropic structured outputs: fallback + error propagation -----------


@pytest.mark.asyncio
async def test_anthropic_falls_back_when_structured_unavailable(monkeypatch) -> None:
    # L2-17: this raised a bare RuntimeError as the stand-in for "SDK too old",
    # and the code degraded on ANY exception to match it. The real shape is a
    # TypeError — the installed SDK has no `output_config` keyword — and
    # accepting anything meant a dropped connection degraded too: the same
    # logical call billed twice, logged as an SDK problem.
    client = AnthropicLLMClient(api_key="x")

    async def structured_boom(**kw):
        raise TypeError(
            "messages.create() got an unexpected keyword argument 'output_config'"
        )

    async def legacy_ok(**kw):
        return {"from": "legacy"}

    monkeypatch.setattr(client, "_complete_json_structured", structured_boom)
    monkeypatch.setattr(client, "_complete_json_legacy", legacy_ok)
    out = await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert out == {"from": "legacy"}  # degraded gracefully


@pytest.mark.asyncio
async def test_anthropic_falls_back_when_the_api_rejects_output_config(monkeypatch) -> None:
    # The other genuine capability case: the SDK knows the kwarg, the API
    # rejects the request. A 4xx means "malformed for this endpoint" → degrade.
    client = AnthropicLLMClient(api_key="x")

    class _BadRequest(Exception):
        status_code = 400

    async def structured_boom(**kw):
        raise _BadRequest("output_config is not supported")

    async def legacy_ok(**kw):
        return {"from": "legacy"}

    monkeypatch.setattr(client, "_complete_json_structured", structured_boom)
    monkeypatch.setattr(client, "_complete_json_legacy", legacy_ok)
    assert await client.complete_json(
        system="s", prompt="p", schema={"type": "object"}
    ) == {"from": "legacy"}


@pytest.mark.asyncio
async def test_transport_failure_does_not_retry_on_the_legacy_path(monkeypatch) -> None:
    """L2-17: a dropped connection is not a capability problem. Retrying it on
    the legacy path bills the same logical call twice, and tells the operator
    their SDK is out of date while the real cause is the network."""
    client = AnthropicLLMClient(api_key="x")
    called = {"legacy": False}

    async def structured_boom(**kw):
        raise ConnectionError("Connection reset by peer")

    async def legacy(**kw):
        called["legacy"] = True
        return {}

    monkeypatch.setattr(client, "_complete_json_structured", structured_boom)
    monkeypatch.setattr(client, "_complete_json_legacy", legacy)
    with pytest.raises(LLMError):
        await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert called["legacy"] is False, "a network error was retried as a second billed call"


@pytest.mark.asyncio
async def test_rate_limit_is_not_treated_as_a_capability_problem(monkeypatch) -> None:
    # 429 is a 4xx but it means "try later", not "this endpoint cannot do it".
    client = AnthropicLLMClient(api_key="x")

    class _RateLimited(Exception):
        status_code = 429

    async def structured_boom(**kw):
        raise _RateLimited("slow down")

    monkeypatch.setattr(client, "_complete_json_structured", structured_boom)
    with pytest.raises(LLMError):
        await client.complete_json(system="s", prompt="p", schema={"type": "object"})


@pytest.mark.asyncio
async def test_anthropic_llmerror_propagates_not_fallback(monkeypatch) -> None:
    client = AnthropicLLMClient(api_key="x")

    async def structured_llmerr(**kw):
        raise LLMError("ANTHROPIC_API_KEY not set")

    called = {"legacy": False}

    async def legacy(**kw):
        called["legacy"] = True
        return {}

    monkeypatch.setattr(client, "_complete_json_structured", structured_llmerr)
    monkeypatch.setattr(client, "_complete_json_legacy", legacy)
    with pytest.raises(LLMError):
        await client.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert called["legacy"] is False  # auth error must not silently degrade


# ---- Anthropic malformed-JSON fixtures (Low P2) -----------------------------
#
# Both complete_json paths run the SAME malformed transport output through their
# OWN real json.loads (no monkeypatching _complete_json_structured/_legacy
# themselves — only the SDK message call), so a real fence-strip + parse failure
# is exercised end-to-end, not just the dispatch logic above.


class _FakeTextBlock:
    def __init__(self, text: str, type_: str = "text") -> None:
        self.text = text
        self.type = type_


@pytest.mark.asyncio
async def test_anthropic_structured_path_raises_llmerror_on_malformed_json(monkeypatch) -> None:
    client = AnthropicLLMClient(api_key="x")

    class _Msgs:
        async def create(self, **kw):
            class _R:
                content = [_FakeTextBlock("```json\nnot json{")]

            return _R()

    class _Cli:
        def __init__(self, **kw):
            self.messages = _Msgs()

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Cli)
    with pytest.raises(LLMError):
        await client._complete_json_structured(
            system="s", prompt="p", schema={"type": "object"}, max_tokens=512
        )


@pytest.mark.asyncio
async def test_anthropic_legacy_path_raises_llmerror_on_malformed_json(monkeypatch) -> None:
    client = AnthropicLLMClient(api_key="x")

    async def fake_complete(**kw):
        return "```json\nnot json{"

    monkeypatch.setattr(client, "complete", fake_complete)
    with pytest.raises(LLMError):
        await client._complete_json_legacy(system="s", prompt="p", max_tokens=512)


class TestAdapterContractAndProviderSwitch:
    """L2-02 / L2-04 (2026-07-26 Phase 2 review).

    Two fixes with one shared idea: **a failure must fail in the direction that
    is recoverable.** The adapter must fail as an `LLMError` (so the agents'
    documented degradation actually fires), and the provider switch must fail
    CLOSED (so a typo cannot silently pick the remote backend).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("boom", [
        ConnectionError("Connection reset by peer"),
        TimeoutError("Request timed out"),
        RuntimeError("something the SDK raised"),
    ])
    async def test_every_transport_failure_becomes_llmerror(self, boom):
        """The whole contract in one line: all three plugin agents catch only
        `LLMError`, so anything else escaping this adapter reaches LangGraph
        and kills an audit — including from the *advisory* agents, one of which
        runs AFTER Path B has already written to Revit."""
        client = AnthropicLLMClient(api_key="x")

        class _Boom:
            class messages:
                @staticmethod
                async def create(**kw):
                    raise boom

        client._client = lambda: _Boom()
        with pytest.raises(LLMError):
            await client.complete(system="s", prompt="p")

    @pytest.mark.asyncio
    async def test_an_empty_response_is_an_llmerror_not_an_indexerror(self):
        # A 200 with zero content blocks (max_tokens hit, or a refusal) used to
        # be `resp.content[0]` → IndexError, which reads to every caller as
        # "not an LLM problem".
        client = AnthropicLLMClient(api_key="x")

        class _Empty:
            class messages:
                @staticmethod
                async def create(**kw):
                    return type("R", (), {"content": []})()

        client._client = lambda: _Empty()
        with pytest.raises(LLMError):
            await client.complete(system="s", prompt="p")

    @pytest.mark.parametrize("typo", ["ollamma", "local", "fakee", "anthropik", "openai"])
    def test_a_typo_never_silently_selects_the_cloud(self, monkeypatch, typo):
        """L2-04. `ollama` is how an operator keeps element names, parameter
        values and NDA'd spec text on their own hardware. Falling through to
        Anthropic on a one-character typo sends that data to a third party, and
        the failure is unrecoverable once the request has left."""
        monkeypatch.setenv("BIM_LLM_PROVIDER", typo)
        with pytest.raises(LLMError) as exc:
            build_llm_client()
        assert typo in str(exc.value)

    @pytest.mark.parametrize("value,expected", [
        ("ollama", "OllamaLLMClient"),
        ("fake", "FakeLLMClient"),
        ("anthropic", "AnthropicLLMClient"),
        ("  OLLAMA  ", "OllamaLLMClient"),      # trimmed + lowercased, still fine
    ])
    def test_every_real_provider_still_builds(self, monkeypatch, value, expected):
        monkeypatch.setenv("BIM_LLM_PROVIDER", value)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        assert type(build_llm_client()).__name__ == expected

    def test_unset_still_defaults_to_anthropic(self, monkeypatch):
        # Guard: the documented default must not change. Only UNRECOGNISED
        # values raise — absence is a choice the docs already make.
        monkeypatch.delenv("BIM_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        assert type(build_llm_client()).__name__ == "AnthropicLLMClient"

    def test_empty_string_is_absence_not_a_typo(self, monkeypatch):
        monkeypatch.setenv("BIM_LLM_PROVIDER", "   ")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        assert type(build_llm_client()).__name__ == "AnthropicLLMClient"
