"""Minimal stand-ins for the 3 private-plugin LLM agents (``bim_orchestrator_llm``).

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): design.py / graph.py call these agents
through duck typing (see ``bim_orchestrator.llm.interfaces`` for the exact
Protocol each one satisfies) — no isinstance check anywhere. These stubs
implement just enough of that call surface so core tests can pin the WIRING
invariants (never-auto governance, human-only routing to Path A, graph node
insertion, route_node's monotonic-stop consumption of a directive) WITHOUT
installing the plugin. They deliberately do NOT reproduce the real agents'
prompt construction / repair loop / batching — that design lives in the
plugin's own tests (``bim-orchestrator-llm/tests``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StubRemediationProposal:
    element_id: str
    parameter: str
    proposed_value: str
    rule_id: str
    autonomy: str  # "approve" | "human-only" — mirrors RemediationProposal
    rationale: str
    validated: bool = True
    source: str = "llm"


class StubRemediationAgent:
    """Canned-value remediation stub satisfying ``RemediationAgentProtocol``.

    ``value=None`` mirrors "the model failed / had nothing usable" (→ None,
    same as a real agent's LLMError or a value that fails re-validation).
    A non-None ``value`` still runs through the injected ``validate`` — same
    closed-loop contract as the real agent: a value that fails validation is
    rejected (returns None), never returned as-is.
    """

    def __init__(
        self,
        value: str | None = "STUB_VALUE",
        *,
        batch_values: dict[str, Any] | None = None,
        autonomy: str = "approve",
    ) -> None:
        self.value = value
        self.batch_values = dict(batch_values or {})
        self.autonomy = autonomy
        self.calls: list[dict[str, Any]] = []

    async def propose(
        self,
        finding: dict[str, Any],
        *,
        validate,
        context: dict[str, Any] | None = None,
        safety_critical: bool = False,
    ) -> StubRemediationProposal | None:
        self.calls.append({"finding": finding, "context": context})
        if self.value is None or not validate(self.value):
            return None
        return StubRemediationProposal(
            element_id=str(finding.get("element_id", "")),
            parameter=str(finding.get("parameter", "")),
            proposed_value=self.value,
            rule_id=str(finding.get("rule_id", "")),
            autonomy="human-only" if safety_critical else self.autonomy,
            rationale="stub",
            validated=True,
        )

    async def propose_batch(
        self, items: list[dict[str, Any]], *, context: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Contract v2 (L2-09): each entry echoes the current value it answers.

        ``batch_values`` may be given as ``{eid: value}`` (the value is bound to
        that element's own ``current_value`` from ``items`` — the well-behaved
        model) or as ``{eid: (value, echo)}`` to simulate a permuted / unbound
        answer, which is what the binding check exists to catch.
        """
        asked = {str(i.get("element_id")): i.get("current_value") for i in items}
        out: dict[str, dict[str, Any]] = {}
        for eid, v in self.batch_values.items():
            if isinstance(v, tuple):
                value, echo = v
            else:
                value, echo = v, asked.get(str(eid))
            out[str(eid)] = {"value": value, "for_current_value": echo}
        return out


class StubDiagnosticAgent:
    """Canned-enrichment stub satisfying ``DiagnosticAgentProtocol``."""

    def __init__(self, *, summary: str = "stub summary", action: str = "stub action") -> None:
        self.summary = summary
        self.action = action
        self.run_count = 0

    async def run(self, state):
        self.run_count += 1
        for bucket in ("findings", "manual_review_items", "missing_data_items"):
            for f in state.get(bucket, []) or []:
                f["diagnosis"] = {
                    "summary": self.summary,
                    "suggested_action": self.action,
                    "source": "llm",
                }
        return state


class StubSupervisorAgent:
    """Fixed-directive stub satisfying ``SupervisorAgentProtocol``."""

    def __init__(self, directive: dict[str, Any] | None = None) -> None:
        self.directive = dict(directive or {"action": "continue", "reason": "stub"})
        self.run_count = 0

    async def run(self, state) -> dict[str, Any]:
        self.run_count += 1
        return {"supervisor_directive": dict(self.directive)}


class UnvalidatedRemediationAgent:
    """P2-01: a plugin that IGNORES the injected ``validate`` callback.

    Not a hypothetical. The core's guarantee — *every proposal is re-checked by
    the rule that flagged it* — used to hold on the single-element path ONLY
    because the real plugin calls the validator we hand it. That plugin is a
    separate package with its own version and its own repo, so the invariant
    was enforced there and merely trusted here. This stub is that trust being
    misplaced: it returns its canned value verbatim, exactly as a refactored
    (or simply older) plugin might.
    """

    def __init__(self, value: str = "definitely not compliant") -> None:
        self.value = value
        self.validate_called = False

    async def propose(
        self,
        finding: dict[str, Any],
        *,
        validate,
        context: dict[str, Any] | None = None,
        safety_critical: bool = False,
    ) -> StubRemediationProposal:
        # Deliberately does NOT call validate(...).
        return StubRemediationProposal(
            element_id=str(finding.get("element_id", "")),
            parameter=str(finding.get("parameter", "")),
            proposed_value=self.value,
            rule_id=str(finding.get("rule_id", "")),
            autonomy="approve",
            rationale="stub that skipped its own validation",
            validated=True,          # it even CLAIMS it validated
        )

    async def propose_batch(
        self, items: list[dict[str, Any]], *, context: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        return {
            str(i.get("element_id")): {
                "value": self.value, "for_current_value": i.get("current_value"),
            }
            for i in items
        }
