"""Phase 2 — Diagnostic Agent (#2) socket: graph wiring, Phase-1 default.

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): the real ``DiagnosticAgent`` (prompt,
schema, enrichment, bounding, factory happy-path with the real class) moved to
the private ``bim-orchestrator-llm`` plugin — see its
``tests/test_diagnostic_agent.py``. What's pinned here needs NO concrete
agent, only something that satisfies ``DiagnosticAgentProtocol`` (see
``tests/_llm_stubs.py``) or, for the "no agent at all" case, nothing:

  * the graph only adds the diagnostic node when an agent is injected — Phase
    1 (no injection) is unaffected;
  * when injected, the node runs on loop exit and can enrich a finding
    (graph WIRING, not the model's judgment).
"""

from __future__ import annotations

import pytest

from bim_orchestrator.state import Finding, OrchestratorState
from tests._llm_stubs import StubDiagnosticAgent


def _finding(eid: str) -> Finding:
    return Finding(
        rule_id="qa.rule",
        element_id=eid,
        parameter="Department",
        severity_tag="data_quality",
        severity="severity_medium",  # type: ignore[arg-type]
        message=f"{eid} fails qa.rule",
        suggested_value=None,
        citation=None,
    )


def _graph_state() -> OrchestratorState:
    return {  # type: ignore[typeddict-item]
        "project_id": "t", "iteration": 0, "max_iterations": 2,
        "elements": [], "findings": [], "proposed_fixes": [],
        "status": "init", "error": None,
    }


class _FakeQuery:
    def __init__(self, elements): self._e = elements; self.calls = 0
    async def run(self, state):
        self.calls += 1
        return {**state, "elements": list(self._e), "status": "checking"}


class _FakeQC:
    def __init__(self, findings): self._f = findings
    def run(self, state):
        return {**state, "findings": self._f, "status": "designing"}


class _FakeDesign:
    async def run(self, state):
        return {**state, "status": "converged"}


@pytest.mark.asyncio
async def test_graph_runs_diagnostic_node() -> None:
    from bim_orchestrator.graph import build_graph

    findings = [_finding("e1")]
    stub = StubDiagnosticAgent()
    app = build_graph(
        _FakeQuery([{"id": "e1"}]), _FakeQC(findings), _FakeDesign(),
        diagnostic_agent=stub,
    )
    result = await app.ainvoke(_graph_state())
    assert result["status"] in ("converged", "designing")
    assert "diagnosis" in findings[0]  # the node ran on the final findings (exit)
    assert stub.run_count == 1  # once total, on exit — not per iteration


@pytest.mark.asyncio
async def test_graph_phase1_unchanged_without_diagnostic() -> None:
    from bim_orchestrator.graph import build_graph

    findings = [_finding("e1")]
    app = build_graph(_FakeQuery([{"id": "e1"}]), _FakeQC(findings), _FakeDesign())
    await app.ainvoke(_graph_state())
    assert "diagnosis" not in findings[0]  # no node added → no enrichment
