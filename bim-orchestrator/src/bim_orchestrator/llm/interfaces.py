"""Socket contract for the private LLM runtime-agent plugin (``bim_orchestrator_llm``).

v1.5-R2 wired 3 optional agents — Remediation / Diagnostic / Supervisor —
directly into ``agents/design.py`` / ``graph.py``. v1.5-R3
(SPEC_LLM_PLUGIN_SPLIT, 2026-07-07) extracted their prompt/schema/repair-loop
DESIGN into a separate private package so the core engine can ship public
without exposing it; core keeps only this socket (these Protocols) + the lazy
factory (``llm/factory.py``).

These Protocols pin the EXACT call surface ``design.py`` and ``graph.py``
depend on. A plugin build (or a test stub — see ``tests/_llm_stubs.py``) only
needs to satisfy these signatures.

There is still no ``isinstance`` check (Protocol methods can't be checked that
way at runtime), but since L2-13 these signatures are no longer merely
DOCUMENTED: ``llm/conformance.py`` inspects them and ``llm/factory.py`` runs
that check at wire-up, so a plugin that has drifted degrades to the
deterministic path with a log line instead of raising mid-run. The plugin's own
suite imports the same module, so both sides check one definition.

Unset flags → ``llm/factory.py`` returns ``None`` → these Protocols are never
consulted → the deterministic Phase 1 loop is byte-for-byte unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from ..state import OrchestratorState

Validator = Callable[[str], bool]
"""Deterministic verdict predicate injected by DesignAgent: does this proposed
value satisfy the rule that flagged the finding? (wraps
``policies.rules_engine.evaluate``). The symbolic guardrail half of the
neuro-symbolic split — the agent proposes, this predicate disposes."""


class RemediationAgentProtocol(Protocol):
    """What ``agents/design.py`` calls on the injected ``llm_agent`` (see
    ``DesignAgent._llm_propose`` / ``DesignAgent._prewarm_llm_batches``).

    ``propose`` returns an object exposing ``.proposed_value: str`` and
    ``.autonomy: str`` (``"approve"`` or ``"human-only"`` — NEVER ``"auto"``
    for an LLM-originated value), or ``None`` when nothing survives the
    closed-loop re-validation (``validate``). ``None`` is the established
    "route to Path A" sentinel — never a crash, never a fabricated value.
    """

    async def propose(
        self,
        finding: dict[str, Any],
        *,
        validate: Validator,
        context: dict[str, Any] | None = None,
        safety_critical: bool = False,
    ) -> Any: ...

    async def propose_batch(
        self,
        items: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]: ...
    """``{element_id: {"value": str, "for_current_value": str}}`` (contract v2,
    L2-09). ``for_current_value`` is the current value the MODEL says it was
    correcting — it must come from the model's answer, never be filled in from
    the request, or the check it exists for becomes a tautology. Core drops any
    entry whose echo doesn't match the value it asked about for that element and
    falls back to the per-element ``propose``: a rule is only batchable when its
    validator is element-INDEPENDENT, so validation alone cannot tell a permuted
    answer from a correct one."""


class DiagnosticAgentProtocol(Protocol):
    """What ``graph.py`` calls on the injected ``diagnostic_agent`` — ONCE, on
    loop exit (see ``graph.py``'s ``diagnostic_node``, GĐ3-A2). Advisory: may
    only set ``Finding["diagnosis"]`` on findings already in ``state``; must
    never touch ``status`` / ``severity`` / ``suggested_value``, and must
    never raise (an LLM/JSON failure degrades to "no diagnosis" per finding).
    """

    async def run(self, state: OrchestratorState) -> OrchestratorState: ...


class SupervisorAgentProtocol(Protocol):
    """What ``graph.py`` calls on the injected ``supervisor_agent`` — every
    iteration (see ``graph.py``'s ``supervisor_node``). Advisory: returns
    ``{"supervisor_directive": SupervisorDirective}``. ``route_node`` reads the
    directive ONLY in the branch where it would otherwise continue (monotonic
    stop, never extend — see ``state.SupervisorDirective`` docstring); a
    fail-safe agent emits ``{"action": "continue", ...}`` on any internal
    error rather than raising.
    """

    async def run(self, state: OrchestratorState) -> dict[str, Any]: ...
