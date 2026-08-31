"""GĐ3-C1 — bridge the standalone ``rules_extractor`` LLM seam through the phase2
provider switch + usage recorder, so PDF→rule extraction lives in the product.

``rules_extractor`` (repo ExtractionAgents) is a mature governed regulation→RuleSet
extractor. It is decoupled at a JSON envelope and its LLM seam is
``emit_ruleset(system, user_content, tool, model)`` — forced **tool_use**, NOT the
phase2 ``complete_json``. We do NOT route through ``complete_json`` on purpose: its
``sanitize_json_schema`` forces ``required = all properties``, which would break the
rich rule schema (pattern / threshold / unit / when_param are OPTIONAL). So this
bridge keeps tool_use for the actual call and only adds the two things the phase2
seam gives us: provider selection (``BIM_LLM_PROVIDER``) and per-run usage/cost
accounting (the B1 ``UsageRecorder``, tagged ``"extraction"``).

Provider parity: extraction is **anthropic** (cloud tool_use), **ollama** (local,
JSON-schema constrained decoding — for air-gapped / data-residency runs), or
**fake** (tests). Extraction wants a STRONGER model than the haiku runtime default,
so the model is resolved separately + provider-aware (``BIM_EXTRACTION_MODEL`` →
``BIM_LLM_MODEL`` → sonnet for cloud, qwen3:14b for ollama). Cloud stays the
recommended path: local models are lower-recall on regulation prose (they skip more
rules); the win of the ollama path is that no document leaves the host.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import structlog

from .factory import llm_provider
from .usage import UsageRecorder

log = structlog.get_logger(__name__)

# Extraction reasons over regulation prose → default to the stronger model, not the
# haiku the runtime agents use. Override precedence: BIM_EXTRACTION_MODEL → BIM_LLM_MODEL.
_DEFAULT_EXTRACTION_MODEL = "claude-sonnet-4-6"
# Local (Ollama) extraction default — a capable-enough open model for constrained
# JSON decoding; the operator overrides with BIM_EXTRACTION_MODEL / BIM_LLM_MODEL.
_DEFAULT_OLLAMA_EXTRACTION_MODEL = "qwen3:14b"


class ExtractionUnavailable(RuntimeError):
    """rules-extractor is not installed, or the active provider can't extract."""


class ExtractionCancelled(RuntimeError):
    """The caller gave up (timeout) — stop spending.

    S-04 (2026-07-25 live review): the service ran extraction under
    ``asyncio.wait_for(asyncio.to_thread(...), 300)``. On timeout the AWAIT is
    abandoned, but a Python thread cannot be killed — ``rules_extractor`` kept
    fanning sections out and every one kept billing the API for a result
    nobody would ever read. The thread still can't be killed; what CAN be
    stopped is the money, at the one chokepoint every billed call passes
    through (``ExtractionSeamClient.emit_ruleset``).
    """


def extraction_model() -> str:
    """The model id flowed into ``extract_sections(model=...)`` — provider-aware
    so the Ollama path never inherits the ``claude-sonnet-4-6`` default (which
    would be posted verbatim as an Ollama model name and 404)."""
    explicit = (
        os.environ.get("BIM_EXTRACTION_MODEL", "").strip()
        or os.environ.get("BIM_LLM_MODEL", "").strip()
    )
    if explicit:
        return explicit
    if llm_provider() == "ollama":
        return _DEFAULT_OLLAMA_EXTRACTION_MODEL
    return _DEFAULT_EXTRACTION_MODEL


class ExtractionSeamClient:
    """Implements the ``rules_extractor`` LLMClient protocol (``emit_ruleset``) by
    delegating to an inner rules_extractor client and recording each call on a shared
    ``UsageRecorder`` (tag ``"extraction"``).

    The recorder is RECORDED-ONLY here (no budget ``check``): extraction runs its
    sections concurrently, and tripping a mid-extraction budget would leave confusing
    partial coverage. The budget (``BIM_LLM_MAX_CALLS``) governs the runtime loop.
    ``rules_extractor`` fans ``emit_ruleset`` out across threads, so recorder writes
    are guarded by a lock (the async ``UsageRecorder`` isn't otherwise thread-safe).
    """

    def __init__(self, inner: Any, *, recorder: UsageRecorder | None = None) -> None:
        self._inner = inner
        self._recorder = recorder
        self._lock = threading.Lock()
        # S-04: threading.Event, not a bool — rules_extractor calls this from
        # its own worker threads while the CANCEL comes from the event loop.
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Refuse all FURTHER LLM calls (S-04). Idempotent, thread-safe.

        Granularity is the next call boundary: a request already in flight
        finishes and is still billed — that one is unavoidable without killing
        the thread. Everything queued behind it is not.
        """
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def emit_ruleset(
        self, *, system: str, user_content: str, tool: dict[str, Any], model: str
    ) -> dict[str, Any]:
        if self._cancelled.is_set():
            # Raise BEFORE the inner call and before recording usage: an
            # abandoned extraction must cost nothing further, and a refused
            # call is not a call that happened.
            raise ExtractionCancelled(
                "extraction was cancelled (caller timed out) — no further LLM calls"
            )
        t0 = time.perf_counter()
        ok = False
        try:
            out = self._inner.emit_ruleset(
                system=system, user_content=user_content, tool=tool, model=model
            )
            ok = True
            return out
        finally:
            if self._recorder is not None:
                with self._lock:
                    self._recorder.record(
                        "extraction", seconds=time.perf_counter() - t0, ok=ok,
                        model=model,   # P2-02: record WHICH model extracted
                    )


def rules_extractor_available() -> bool:
    try:
        import rules_extractor  # noqa: F401
    except ImportError:
        return False
    return True


def make_extraction_client(
    *, recorder: UsageRecorder | None = None, inner: Any = None
) -> ExtractionSeamClient:
    """Build an extraction client for the current provider, metered by ``recorder``.

    ``inner`` injects a rules_extractor client verbatim (tests / a pre-built
    AnthropicClient). Otherwise the provider is resolved from ``BIM_LLM_PROVIDER``:
    ``anthropic`` → cloud tool_use client; ``ollama`` → local schema-constrained
    client (``BIM_LLM_BASE_URL`` for the host); ``fake`` (and any other) has no
    envelope to emit outside tests → ``ExtractionUnavailable``.
    """
    try:
        from rules_extractor.llm import AnthropicClient, OllamaClient
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise ExtractionUnavailable(
            "rules-extractor not installed (uv pip install -e <ExtractionAgents>)"
        ) from exc

    if inner is not None:
        return ExtractionSeamClient(inner, recorder=recorder)

    provider = llm_provider()
    if provider == "anthropic":
        log.info("extraction.provider", provider="anthropic", model=extraction_model())
        return ExtractionSeamClient(AnthropicClient(), recorder=recorder)
    if provider == "ollama":
        base_url = os.environ.get("BIM_LLM_BASE_URL", "").strip() or "http://localhost:11434"
        # model flows in via extract_sections(model=extraction_model()); the
        # constructor value is only a fallback for a direct emit_ruleset call.
        log.info("extraction.provider", provider="ollama", model=extraction_model(), base_url=base_url)
        return ExtractionSeamClient(
            OllamaClient(base_url=base_url, model=extraction_model()), recorder=recorder
        )
    raise ExtractionUnavailable(
        f"provider {provider!r} cannot extract (needs anthropic or ollama); "
        "set BIM_LLM_PROVIDER=anthropic (cloud) or =ollama (local)"
    )
