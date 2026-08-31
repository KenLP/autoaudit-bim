"""Deterministic in-memory LLM for tests — no network, no model download.

Mirrors the ``tests/_mocks.py`` philosophy (MCP mocks with 1:1 protocol
parity): canned responses keyed by a substring match on the prompt, plus call
recording so tests can assert what an agent asked the model.
"""

from __future__ import annotations

from typing import Any

from .client import LLMClient, LLMError


class FakeLLMClient(LLMClient):
    def __init__(
        self,
        text_responses: dict[str, str] | None = None,
        json_responses: dict[str, dict[str, Any]] | None = None,
        *,
        default_text: str = "",
        default_json: dict[str, Any] | None = None,
    ) -> None:
        self._text = text_responses or {}
        self._json = json_responses or {}
        self._default_text = default_text
        self._default_json = default_json
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _match(mapping: dict[str, Any], prompt: str) -> Any | None:
        for needle, value in mapping.items():
            if needle in prompt:
                return value
        return None

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int = 512
    ) -> str:
        self.calls.append({"kind": "complete", "system": system, "prompt": prompt})
        hit = self._match(self._text, prompt)
        return hit if hit is not None else self._default_text

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        self.calls.append(
            {"kind": "complete_json", "system": system, "prompt": prompt, "schema": schema}
        )
        hit = self._match(self._json, prompt)
        if hit is not None:
            return hit
        if self._default_json is not None:
            return self._default_json
        raise LLMError("FakeLLMClient: no canned JSON response matched the prompt")

    def calls_to(self, kind: str) -> list[dict[str, Any]]:
        """Audit helper — every call of a given kind, in order."""
        return [c for c in self.calls if c["kind"] == kind]
