"""GĐ3-B1 — per-run LLM usage accounting + a shared client with a call budget.

Phase 2 wired three independent LLM agents (Remediation / Diagnostic / Supervisor),
each building its OWN client — so a run had no shared view of "how many LLM calls
did this cost?" and no ceiling. This module adds both, without touching agent code:

  * ``UsageRecorder`` — counts calls + wall-time per agent for ONE run.
  * ``MeteredLLMClient`` — a thin ``LLMClient`` decorator that records every call
    and enforces the recorder's optional hard budget. When the budget is hit the
    NEXT call raises ``LLMError`` — which every agent already degrades on
    (Remediation→Path A, Diagnostic→skip, Supervisor→continue) — so the budget
    doubles as a circuit breaker with zero new agent logic.
  * ``LLMRunContext`` — one shared inner client + one recorder, wrapped per agent
    with an attribution tag so the run summary can break the total down.

The recorder/wrapper add no network of their own; the inner client is whatever
``build_llm_client()`` returns (cloud / local / fake).
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import structlog

from .client import LLMClient, LLMError

log = structlog.get_logger(__name__)


class UsageRecorder:
    """Tallies LLM calls (and their summed duration) per agent for one run, and
    enforces an optional hard call budget shared across all agents.

    P2-04 (2026-07-25 live review) — ``check()`` then ``record()`` used to be
    TOCTOU: two separate reads with no lock, so under C concurrent callers the
    effective ceiling was ``max_calls + (C - 1)``. That was written down as an
    accepted slop, on the argument that the overshoot is bounded and every agent
    degrades rather than mutating anything. The argument holds — but it made the
    cap correct only BECAUSE nothing currently issues LLM calls concurrently,
    which is a property of today's call sites, not of this class. The first
    parallel remediation would have silently overspent a paid API, on a product
    whose pitch is auditability.

    So the budget is now a RESERVATION under a lock: ``check()`` counts the call
    before it happens and ``record()`` settles it. ``MeteredLLMClient`` is the
    only reserver and always settles in a ``finally``, so a reservation cannot
    leak. ``record()`` on its own (the extraction seam, deliberately
    budget-exempt) still just tallies. The lock also makes the recorder safe for
    that extraction path, which genuinely does call ``record()`` from several
    threads.
    """

    def __init__(self, *, max_calls: int | None = None) -> None:
        # A non-positive budget means "no budget" (defensive against a stray 0).
        self.max_calls = max_calls if (max_calls is not None and max_calls > 0) else None
        self._calls: Counter[str] = Counter()   # agent → completed call count
        self._failed: Counter[str] = Counter()  # agent → calls that raised (L2-16)
        self._seconds: dict[str, float] = {}     # agent → summed call duration
        self._blocked = 0                          # calls refused by the budget
        self._inflight = 0                         # reserved, not yet settled
        self._models: set[str] = set()             # P2-02: model ids actually used
        self._lock = threading.Lock()

    def check(self, agent: str) -> None:
        """Reserve one budget slot, or raise ``LLMError`` when none is left.

        The agent catches the error and degrades — this is the circuit breaker.
        The reservation is what closes P2-04: a concurrent caller sees this call
        counted immediately rather than after the LLM round-trip. Every
        reservation MUST be settled by a matching ``record()``;
        ``MeteredLLMClient`` does that in a ``finally``, including on failure.
        """
        with self._lock:
            if self.max_calls is not None:
                committed = sum(self._calls.values())
                if committed + self._inflight >= self.max_calls:
                    self._blocked += 1
                    log.warning(
                        "llm.budget_exceeded",
                        agent=agent, max_calls=self.max_calls,
                        total=committed, inflight=self._inflight,
                    )
                    raise LLMError(
                        f"LLM call budget reached ({self.max_calls} calls); "
                        "degrading to the deterministic path"
                    )
            self._inflight += 1

    def record(
        self, agent: str, *, seconds: float, ok: bool, model: str | None = None
    ) -> None:
        """Settle a call: move it from reserved to completed.

        Callers that never reserved (the extraction seam, metered but
        budget-exempt by design) simply tally — the ``max(0, ...)`` stops an
        unreserved settle from driving the in-flight count negative, which
        would hand out a free slot.

        ``model`` (P2-02) is the id the call was ACTUALLY issued against, so
        the run artifact says which model produced its LLM-assisted content.
        Model ids are largely floating aliases: the provider can re-point one
        at a new checkpoint and every future run behaves differently with
        nothing in the record to show it. Pinning every default to a dated
        snapshot is not possible (not all have one published), but RECORDING
        what ran turns a silent change into a visible one — two runs can be
        compared, which is the property "audit-grade" actually needs.
        """
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._calls[agent] += 1
            self._seconds[agent] = self._seconds.get(agent, 0.0) + max(0.0, seconds)
            if not ok:
                # L2-16: `ok` was accepted and then never read, so a run where
                # EVERY call errored produced the same usage line and the same
                # artifact as a clean one. The agents degrade correctly on a
                # failure, which is why this stayed invisible — but "the model
                # was asked 40 times and answered 0" is the single most useful
                # thing to know when a run comes back with nothing proposed.
                self._failed[agent] += 1
            if model:
                self._models.add(str(model))

    @property
    def total_calls(self) -> int:
        return sum(self._calls.values())

    @property
    def total_seconds(self) -> float:
        return sum(self._seconds.values())

    @property
    def blocked(self) -> int:
        return self._blocked

    @property
    def models(self) -> list[str]:
        """P2-02: model ids this run actually issued calls against."""
        return sorted(self._models)

    @property
    def failed_calls(self) -> int:
        """L2-16: calls that were made and did NOT come back with an answer."""
        return sum(self._failed.values())

    def summary(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "total_seconds": round(self.total_seconds, 1),
            "by_agent": {a: n for a, n in sorted(self._calls.items())},
            "failed_calls": self.failed_calls,
            "failed_by_agent": {a: n for a, n in sorted(self._failed.items())},
            "blocked": self._blocked,
            "max_calls": self.max_calls,
            "models": self.models,
        }

    def format_line(self) -> str | None:
        """One-line human summary for the CLI / report, or None if nothing ran."""
        if self.total_calls == 0 and self._blocked == 0:
            return None
        breakdown = " / ".join(f"{a} {n}" for a, n in sorted(self._calls.items()))
        line = f"LLM: {self.total_calls} calls · {self.total_seconds:.1f}s total across calls"
        if breakdown:
            line += f" ({breakdown})"
        if self._models:
            # P2-02: name the model on the operator's own screen, not only in
            # the artifact — "which model produced this?" is the first question
            # after a run behaves differently from yesterday's.
            line += f" · model {', '.join(self.models)}"
        if self.failed_calls:
            # L2-16: on the operator's screen, not only in the artifact — this
            # is the first thing that explains an empty-handed run.
            line += f" · ⚠️ {self.failed_calls} FAILED (no answer)"
        if self.max_calls is not None:
            line += f" · budget {self.max_calls}"
        if self._blocked:
            line += f" · {self._blocked} degraded by budget"
        return line


@dataclass
class MeteredLLMClient(LLMClient):
    """``LLMClient`` decorator that records each call on a shared recorder and
    enforces its budget. Attributes calls to ``agent`` for the per-agent breakdown."""

    inner: LLMClient
    recorder: UsageRecorder
    agent: str

    async def complete(self, *, system: str, prompt: str, max_tokens: int = 512) -> str:
        self.recorder.check(self.agent)  # may raise LLMError (budget) → agent degrades
        t0 = time.perf_counter()
        ok = False
        try:
            out = await self.inner.complete(
                system=system, prompt=prompt, max_tokens=max_tokens
            )
            ok = True
            return out
        finally:
            self.recorder.record(
                self.agent, seconds=time.perf_counter() - t0, ok=ok,
                model=getattr(self.inner, "model", None),   # P2-02
            )

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        self.recorder.check(self.agent)
        t0 = time.perf_counter()
        ok = False
        try:
            out = await self.inner.complete_json(
                system=system, prompt=prompt, schema=schema, max_tokens=max_tokens
            )
            ok = True
            return out
        finally:
            self.recorder.record(
                self.agent, seconds=time.perf_counter() - t0, ok=ok,
                model=getattr(self.inner, "model", None),   # P2-02
            )


@dataclass
class LLMRunContext:
    """One shared inner client + one recorder for a whole run. ``for_agent`` hands
    each agent a metered view tagged with its name — so all three share the budget
    and roll up into a single per-run usage summary."""

    client: LLMClient
    recorder: UsageRecorder

    def for_agent(self, agent: str) -> MeteredLLMClient:
        return MeteredLLMClient(inner=self.client, recorder=self.recorder, agent=agent)
