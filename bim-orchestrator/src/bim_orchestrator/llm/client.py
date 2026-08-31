"""The injection seam every Phase 2 agent talks to."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# Keywords no constrained-decoding grammar compiler reliably supports (Anthropic
# structured outputs AND llama.cpp/Ollama GBNF) — strip from the schema sent to
# the model and validate client-side instead. Leaving them in risks a 400 / a
# rejected grammar.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength", "minItems", "maxItems",
        "uniqueItems", "minProperties", "maxProperties",
    }
)


def sanitize_json_schema(node: Any) -> Any:
    """Return a copy of a JSON schema safe for constrained decoding: drop
    unsupported numeric/length constraints and, on every object, set
    ``additionalProperties: false`` + ``required`` = all declared properties.
    Shared by every concrete ``LLMClient`` so structured output behaves the same
    across backends; numeric/length bounds are re-validated in the agents."""
    if isinstance(node, list):
        return [sanitize_json_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        out[key] = sanitize_json_schema(value)
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


class LLMError(RuntimeError):
    """Raised when an LLM call fails or returns unusable output.

    Agents catch this and degrade gracefully (skip the proposal, fall back to a
    deterministic strategy, or route to a human) — an LLM failure must never
    crash the deterministic pipeline.
    """


class LLMClient(ABC):
    """Provider-agnostic LLM interface.

    Two methods, deliberately small:
      * ``complete``      — free-text out (remediation values, explanations).
      * ``complete_json`` — structured out (rule drafts, classifications).

    Concrete implementations: ``FakeLLMClient`` (tests), ``AnthropicLLMClient``
    (cloud). A future ``OllamaLLMClient`` with the same two methods drops in for
    local/air-gapped deployment with zero agent changes.
    """

    # P2-02: every client states which model it speaks to, so the metered
    # wrapper can record it on the run without knowing the provider. NOT
    # abstract on purpose — a client with no meaningful model id (the fake)
    # inherits None and nothing breaks.
    model: str | None = None

    @abstractmethod
    async def complete(
        self, *, system: str, prompt: str, max_tokens: int = 512
    ) -> str:
        """Return the model's free-text completion."""
        raise NotImplementedError

    @abstractmethod
    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        """Return a parsed JSON object.

        Implementations enforce/repair JSON (strip code fences, retry on
        forced tool-use, etc.) and raise ``LLMError`` if the model cannot
        produce valid JSON. ``schema`` is an optional JSON Schema the
        implementation may pass through to a structured-output / tool-use API.
        """
        raise NotImplementedError
