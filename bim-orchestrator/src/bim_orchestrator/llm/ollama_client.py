"""Local / on-prem adapter — Ollama (and any OpenAI-incompatible llama.cpp-style
server that speaks the Ollama ``/api/chat`` protocol).

Same two-method ``LLMClient`` contract as ``AnthropicLLMClient`` → switching a
Phase 2 agent (or the Rule Builder) from cloud to local is a constructor change,
not an agent change. Built for air-gapped / data-residency deployments: nothing
leaves the host.

``complete_json`` uses Ollama's ``format`` = JSON-schema constrained decoding
(GBNF under the hood), then validates the result with ``json.loads`` — Ollama can
stop mid-object (e.g. truncated ``}``) and still return 200, so a parse is
mandatory. On a parse/transport failure it retries a BOUNDED number of times
within a total wall-clock deadline; if it still can't get valid JSON it raises
``LLMError`` so the caller routes to a human rather than fabricating. httpx is
lazy-imported so the package still imports where it's absent (Phase 1 stays
dependency-light).
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from .client import LLMClient, LLMError, sanitize_json_schema

log = structlog.get_logger(__name__)

_DEFAULT_MODEL = "qwen3:14b"
_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaLLMClient(LLMClient):
    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 120.0,
        max_attempts: int = 3,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)

    def _httpx(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise LLMError("httpx not installed (needed for OllamaLLMClient)") from exc
        return httpx


    @property
    def model(self) -> str:
        """P2-02: the model id this client actually calls, for the run record."""
        return self._model

    async def _chat(
        self, *, system: str, prompt: str, max_tokens: int, fmt: Any | None
    ) -> str:
        httpx = self._httpx()
        body: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if fmt is not None:
            body["format"] = fmt
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # connection refused, timeout, HTTP error, bad JSON env
            raise LLMError(f"Ollama request failed: {exc}") from exc
        return (data.get("message") or {}).get("content", "") or ""

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int = 512
    ) -> str:
        return (await self._chat(
            system=system, prompt=prompt, max_tokens=max_tokens, fmt=None
        )).strip()

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        fmt = sanitize_json_schema(schema) if schema is not None else "json"
        last_err: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                text = await self._chat(
                    system=system, prompt=prompt, max_tokens=max_tokens, fmt=fmt
                )
                return json.loads(text)
            except LLMError as exc:
                last_err = exc  # transport error — retry within the attempt budget
            except json.JSONDecodeError as exc:
                last_err = exc  # truncated / invalid JSON — retry
                log.info("ollama.invalid_json_retry", attempt=attempt + 1)
        raise LLMError(
            f"Ollama did not return valid JSON after {self._max_attempts} attempts: {last_err}"
        )
