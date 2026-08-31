"""Phase 2 — Supervisor Agent (#3) socket: route_node governance via the real graph.

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): the real ``SupervisorAgent`` (prompt,
short-circuit prefilter, directive emission, fail-safe behaviour, factory
happy-path with the real class) moved to the private ``bim-orchestrator-llm``
plugin — see its ``tests/test_supervisor_agent.py``. What's pinned here is the
GOVERNANCE ``route_node`` enforces on WHATEVER satisfies
``SupervisorAgentProtocol`` (a canned-directive stub is enough — see
``tests/_llm_stubs.py``):

  * a ``stop_early`` directive converges the loop when the deterministic route
    would otherwise continue;
  * a ``continue`` directive can never override a hard deterministic stop
    (fingerprint-unchanged) or extend past ``max_iterations``;
  * ``bump_node``'s bounded iteration-history trail (no agent involved at all).
"""

from __future__ import annotations

import pytest

from bim_orchestrator.state import Finding, OrchestratorState
from tests._llm_stubs import StubSupervisorAgent


def _finding(eid: str) -> Finding:
    return Finding(
        rule_id="qa.rule", element_id=eid, parameter="P",
        severity_tag="data_quality", severity="severity_medium",  # type: ignore[arg-type]
        message="x", suggested_value=None, citation=None,
    )


def _graph_state(max_iter: int = 5) -> OrchestratorState:
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": max_iter,
        "elements": [], "findings": [], "proposed_fixes": [],
        "status": "init", "error": None,
    }


class _FakeQuery:
    def __init__(self, elements): self._e = elements; self.calls = 0
    async def run(self, state):
        self.calls += 1
        return {**state, "elements": list(self._e), "status": "checking"}


class _SeqQC:
    """Returns a different finding-set per call → fingerprint CHANGES, so the
    deterministic route would CONTINUE (reaching the supervisor branch)."""
    def __init__(self, seq): self._seq = seq; self._i = 0
    def run(self, state):
        f = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return {**state, "findings": f, "status": "designing"}


class _CountingDesign:
    def __init__(self): self.calls = 0
    async def run(self, state):
        self.calls += 1
        return {**state, "status": "designing"}


@pytest.mark.asyncio
async def test_stop_early_converges_when_route_would_continue() -> None:
    from bim_orchestrator.graph import build_graph

    # Changing findings each iteration → deterministic route would keep looping;
    # the supervisor's stop_early is what ends it.
    qc = _SeqQC([[_finding("a")], [_finding("b")], [_finding("c")]])
    design = _CountingDesign()
    app = build_graph(
        _FakeQuery([{"id": "a"}]), qc, design,
        supervisor_agent=StubSupervisorAgent({"action": "stop_early", "reason": "stalled"}),
    )
    result = await app.ainvoke(_graph_state(max_iter=5))
    assert result["status"] == "converged"
    assert design.calls == 1  # iter 0 only; iter 1 stopped early before design


@pytest.mark.asyncio
async def test_supervisor_continue_cannot_extend_past_convergence() -> None:
    """Same findings each iteration → fingerprint unchanged → hard converge at
    iter 1 REGARDLESS of a 'continue' directive (the supervisor can't loosen)."""
    from bim_orchestrator.graph import build_graph

    same = [_finding("a")]
    qc = _SeqQC([same, same, same])
    design = _CountingDesign()
    app = build_graph(
        _FakeQuery([{"id": "a"}]), qc, design,
        supervisor_agent=StubSupervisorAgent({"action": "continue", "reason": "keep going"}),
    )
    result = await app.ainvoke(_graph_state(max_iter=9))
    assert result["status"] == "converged"
    assert design.calls == 1  # fingerprint stop wins; supervisor cannot extend


@pytest.mark.asyncio
async def test_max_iterations_bounds_loop_despite_continue() -> None:
    """Changing findings forever + supervisor 'continue' → still bounded by the
    deterministic max_iterations gate (failed), never infinite."""
    from bim_orchestrator.graph import build_graph

    qc = _SeqQC([[_finding(f"e{i}")] for i in range(20)])
    design = _CountingDesign()
    app = build_graph(
        _FakeQuery([{"id": "a"}]), qc, design,
        supervisor_agent=StubSupervisorAgent({"action": "continue", "reason": "keep going"}),
    )
    result = await app.ainvoke(_graph_state(max_iter=3))
    assert result["status"] == "failed"
    assert "max_iterations" in (result.get("error") or "")


@pytest.mark.asyncio
async def test_bump_populates_bounded_iteration_history() -> None:
    """GĐ3-A3: bump_node records a deterministic per-iteration count trail (no
    supervisor needed), capped at 8 entries, for the Supervisor + the report."""
    from bim_orchestrator.graph import build_graph

    qc = _SeqQC([[_finding(f"e{i}")] for i in range(20)])
    app = build_graph(_FakeQuery([{"id": "a"}]), qc, _CountingDesign())
    result = await app.ainvoke(_graph_state(max_iter=3))
    hist = result.get("iteration_history")
    assert hist and len(hist) <= 8
    assert all("non_compliant" in h and "iteration" in h for h in hist)
