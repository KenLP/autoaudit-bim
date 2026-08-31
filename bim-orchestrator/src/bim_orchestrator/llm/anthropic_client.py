"""Cloud Anthropic adapter.

Kept thin and lazy-importing so the package imports cleanly where the
``anthropic`` SDK or API key is absent (Phase 1 runtime stays LLM-free). To go
local later, write an ``OllamaLLMClient(LLMClient)`` with the same two methods
and inject that instead — no agent code changes.

``complete_json`` uses Anthropic **structured outputs** (constrained decoding,
GA for claude-haiku-4-5) when a schema is given — the model is forced to emit
JSON matching the schema. If the installed SDK predates ``output_config`` (or the
call errors for any non-auth reason) it degrades to the legacy strip-fence +
``json.loads`` path, so this is safe across SDK versions. NOTE: the Anthropic API
does not expose token logprobs, so logprob-based confidence/abstention is not
available on this backend (use self-consistency or a verifier instead).
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

from .client import LLMClient, LLMError, sanitize_json_schema

log = structlog.get_logger(__name__)


def _is_capability_error(exc: BaseException) -> bool:
    """True when the structured-outputs attempt failed because this SDK/API
    cannot DO structured outputs — the only case where retrying on the legacy
    path is the right move (L2-17).

    Two shapes, both meaning "the request was malformed for this endpoint":
      * ``TypeError`` — the installed SDK has no ``output_config`` kwarg.
      * an HTTP 4xx — the API rejected the request itself.

    A transport failure, a 429/5xx, or a timeout is NOT this: retrying it on
    the legacy path bills a second time for the same logical call and produces
    a log line blaming the SDK for a network problem.
    """
    if isinstance(exc, TypeError):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


class AnthropicLLMClient(LLMClient):
    def __init__(
        self,
        *,
        # P2-02: dated snapshot, matching factory._DEFAULT_ANTHROPIC_MODEL —
        # see there for why a floating alias is not an acceptable default.
        model: str = "claude-haiku-4-5-20251001",
        api_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.0,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        # Deterministic + bounded: temperature=0 so rehearsal == live; a short
        # per-call timeout + a couple of SDK-level retries so one slow/hung call
        # never freezes the LangGraph loop for the SDK's 600s default mid-demo.
        self._timeout = timeout
        self._max_retries = max_retries
        self._temperature = temperature

    def _client(self):
        if not self._api_key:
            raise LLMError("ANTHROPIC_API_KEY not set")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise LLMError(
                "anthropic SDK not installed (uv pip install anthropic)"
            ) from exc
        return anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )

    @property
    def model(self) -> str:
        """P2-02: the model id this client actually calls, for the run record."""
        return self._model

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int = 512
    ) -> str:
        # L2-02 (2026-07-26 Phase 2 review): every failure out of this adapter
        # MUST be an LLMError. `client.py` states the contract — "an LLM failure
        # must never crash the deterministic pipeline" — and all three plugin
        # agents implement it as `except LLMError`. This method used to let the
        # SDK's own exception types through (APIConnectionError, a 429/529
        # APIStatusError, httpx.ReadTimeout), so every one of those catches
        # missed and an *advisory* agent could take the whole audit down.
        # `ollama_client` already does this; the Anthropic path was the outlier.
        #
        # `resp.content[0]` is included on purpose: a 200 with zero content
        # blocks (max_tokens hit, or a refusal) is an IndexError, which is
        # exactly the shape that reaches a caller as "not an LLM problem".
        try:
            resp = await self._client().messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=self._temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"anthropic call failed: {exc}") from exc

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        if schema is not None:
            try:
                return await self._complete_json_structured(
                    system=system, prompt=prompt, schema=schema, max_tokens=max_tokens
                )
            except Exception as exc:
                # L2-17: this used to degrade on ANY exception, so a dropped
                # connection retried the whole call on the legacy path — one
                # logical call billed twice — and logged it as "your SDK is too
                # old", sending an operator debugging an outage to entirely the
                # wrong place. Degrade only for a genuine CAPABILITY problem;
                # everything else is an LLM failure and says so.
                if not _is_capability_error(exc):
                    raise LLMError(f"anthropic structured call failed: {exc}") from exc
                log.info("anthropic.structured_outputs_unavailable", error=str(exc))
        return await self._complete_json_legacy(
            system=system, prompt=prompt, max_tokens=max_tokens
        )

    async def _complete_json_structured(
        self, *, system: str, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        resp = await self._client().messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=self._temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {"type": "json_schema", "schema": sanitize_json_schema(schema)}
            },
        )
        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"structured output not valid JSON: {exc}") from exc

    async def _complete_json_legacy(
        self, *, system: str, prompt: str, max_tokens: int
    ) -> dict[str, Any]:
        text = await self.complete(system=system, prompt=prompt, max_tokens=max_tokens)
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]).rsplit("```", 1)[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {exc}") from exc
