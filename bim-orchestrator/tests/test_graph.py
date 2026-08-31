"""Tests for the LangGraph cyclic orchestrator graph.

Uses fake agents (no MCP, no LLM) to verify convergence and loop logic.
"""

from __future__ import annotations

from typing import Any

import pytest

from bim_orchestrator.state import Finding, OrchestratorState


# ---- Fake agents -----------------------------------------------------------


class FakeQueryAgent:
    """Returns a fixed element list. Counts how many times run() was called."""

    def __init__(self, elements: list[dict[str, Any]]) -> None:
        self._elements = elements
        self.call_count = 0

    async def run(self, state: OrchestratorState) -> OrchestratorState:
        self.call_count += 1
        return {**state, "elements": list(self._elements), "status": "checking"}


class FakeQCAgent:
    """Returns a fixed number of findings. Can decrease on each call to
    simulate progress (write-back scenario)."""

    def __init__(self, findings_sequence: list[list[Finding]]) -> None:
        self._seq = findings_sequence
        self._idx = 0

    def run(self, state: OrchestratorState) -> OrchestratorState:
        findings = self._seq[min(self._idx, len(self._seq) - 1)]
        self._idx += 1
        return {**state, "findings": findings, "status": "designing"}


class FakeDesignAgent:
    """No-op design that just counts calls."""

    def __init__(self) -> None:
        self.call_count = 0

    async def run(self, state: OrchestratorState) -> OrchestratorState:
        self.call_count += 1
        return {**state, "status": "converged"}


# ---- Helpers ---------------------------------------------------------------


def _finding(rule_id: str = "test.rule", element_id: str = "e1") -> Finding:
    return Finding(
        rule_id=rule_id,
        element_id=element_id,
        parameter="Param",
        severity_tag="missing_required_param",
        severity="severity_medium",
        message="test",
        suggested_value=None,
        citation=None,
    )


def _initial_state(max_iterations: int = 3) -> OrchestratorState:
    return {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": max_iterations,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }


# ---- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_converges_when_findings_unchanged():
    """Phase 1 scenario: AECDM read-only, findings never decrease.
    Expected: iteration 0 runs design, iteration 1 re-checks, sees same count, stops."""
    from bim_orchestrator.graph import build_graph

    ten_findings = [_finding(element_id=f"e{i}") for i in range(10)]
    query = FakeQueryAgent(elements=[{"id": f"e{i}", "name": f"Room {i}"} for i in range(10)])
    qc = FakeQCAgent(findings_sequence=[ten_findings, ten_findings])
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=5))

    assert result["status"] == "converged"
    assert result["iteration"] == 1  # bumped once after iteration 0
    assert design.call_count == 1  # design ran only on iteration 0
    assert query.call_count == 2  # query ran on iter 0 and iter 1
    assert len(result["findings"]) == 10
    # v1.5-R7: cap-hit honesty — this is a normal fingerprint-stable stop,
    # not a cap-hit; no cap warning should ever render for it.
    assert result["stop_reason"] == "fingerprint_stable"


@pytest.mark.asyncio
async def test_converges_when_zero_findings():
    """All findings resolved on first pass."""
    from bim_orchestrator.graph import build_graph

    query = FakeQueryAgent(elements=[])
    qc = FakeQCAgent(findings_sequence=[[]])
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state())

    # First iteration: 0 findings → route still sends to design (iteration==0)
    # Then bump → iteration 1 → query → qc → 0 findings → converged
    assert result["status"] == "converged"
    assert design.call_count == 1
    assert result["stop_reason"] == "zero_findings"


@pytest.mark.asyncio
async def test_max_iterations_guard():
    """Findings decrease each iteration but never reach 0. Max iterations stops the loop."""
    from bim_orchestrator.graph import build_graph

    seq = [
        [_finding(element_id=f"e{i}") for i in range(10)],  # iter 0: 10
        [_finding(element_id=f"e{i}") for i in range(8)],   # iter 1: 8 (progress)
        [_finding(element_id=f"e{i}") for i in range(6)],   # iter 2: 6 (progress)
        [_finding(element_id=f"e{i}") for i in range(4)],   # iter 3: 4 (would continue but max=3)
    ]
    query = FakeQueryAgent(elements=[{"id": "e0", "name": "R"}])
    qc = FakeQCAgent(findings_sequence=seq)
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=3))

    assert result["status"] == "failed"
    assert "max_iterations_reached" in (result.get("error") or "")
    assert result["iteration"] == 3
    assert design.call_count == 3  # ran on iter 0, 1, 2
    # v1.5-R7: cap-hit honesty — a hard max_iterations stop, not convergence.
    assert result["stop_reason"] == "iteration_cap"


@pytest.mark.asyncio
async def test_query_failure_ends_failed_not_converged():
    """H1: a query agent that cannot read the model returns status='failed' +
    error. The graph must END as 'failed' (never recompute to 'converged') and
    must NOT run QC/Design — reporting 'all clear' when the model was unreadable
    is the worst failure mode for a compliance tool."""
    from bim_orchestrator.graph import build_graph

    class FailingQueryAgent:
        def __init__(self) -> None:
            self.call_count = 0

        async def run(self, state: OrchestratorState) -> OrchestratorState:
            self.call_count += 1
            return {**state, "elements": [], "status": "failed",
                    "error": "revit addin unreachable"}

    query = FailingQueryAgent()
    qc = FakeQCAgent(findings_sequence=[[]])
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state())

    assert result["status"] == "failed"
    assert result["error"] == "revit addin unreachable"
    assert design.call_count == 0   # short-circuited to END; never designed
    assert query.call_count == 1    # queried once, then stopped


@pytest.mark.asyncio
async def test_loop_continues_when_findings_set_changes_even_if_count_grows():
    """v1.4-F3: pre-F3 the loop converged whenever the count didn't shrink,
    treating count-grew-but-set-changed as "no progress". Post-F3 the loop
    inspects the actual finding fingerprint and keeps looping while the set
    is genuinely different — stopping only when the same set repeats or the
    max-iteration safety guard fires.

    Sequence here:
      iter 0: 5 findings (e0..e4)            → design
      iter 1: 8 different findings (e10..e17) → set changed → continue
      iter 2: same 8 findings as iter 1       → fingerprint match → converged
    """
    from bim_orchestrator.graph import build_graph

    seq = [
        [_finding(element_id=f"e{i}") for i in range(5)],
        [_finding(element_id=f"e{i}") for i in range(10, 18)],
        [_finding(element_id=f"e{i}") for i in range(10, 18)],
    ]
    query = FakeQueryAgent(elements=[])
    qc = FakeQCAgent(findings_sequence=seq)
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=10))

    assert result["status"] == "converged"
    assert result["iteration"] == 2
    # Design ran on iter 0 AND iter 1 — pre-F3 would have stopped at iter 1
    assert design.call_count == 2


@pytest.mark.asyncio
async def test_loop_continues_when_set_changes_at_equal_count():
    """v1.4-F3 regression: the classic fix-A-reveals-B scenario where the
    count stays identical but the violations are different. Pre-F3 the
    count-based check (`findings_count >= prev_count`) false-converged
    after iteration 1 — masking real progress and hiding the new B.
    """
    from bim_orchestrator.graph import build_graph

    seq = [
        # iter 0: {A, B} — DesignAgent fixes A
        [_finding(rule_id="r.a", element_id="A"),
         _finding(rule_id="r.b", element_id="B")],
        # iter 1: A gone (fixed) but B + a newly-surfaced C → still 2 findings
        [_finding(rule_id="r.b", element_id="B"),
         _finding(rule_id="r.c", element_id="C")],
        # iter 2: same set as iter 1 → fingerprint stable → converged
        [_finding(rule_id="r.b", element_id="B"),
         _finding(rule_id="r.c", element_id="C")],
    ]
    query = FakeQueryAgent(elements=[])
    qc = FakeQCAgent(findings_sequence=seq)
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=10))

    assert result["status"] == "converged"
    # Design called at least twice: iter 0 (initial) + iter 1 (set changed).
    # Pre-F3 would only call it once because count stayed at 2 → false-converge.
    assert design.call_count == 2


@pytest.mark.asyncio
async def test_full_convergence_with_decreasing_findings():
    """Findings decrease to 0 across iterations → fully converged."""
    from bim_orchestrator.graph import build_graph

    seq = [
        [_finding(element_id=f"e{i}") for i in range(4)],  # iter 0: 4
        [_finding(element_id=f"e{i}") for i in range(2)],  # iter 1: 2
        [],                                                  # iter 2: 0 → converged!
    ]
    query = FakeQueryAgent(elements=[])
    qc = FakeQCAgent(findings_sequence=seq)
    design = FakeDesignAgent()

    app = build_graph(query, qc, design)
    result = await app.ainvoke(_initial_state(max_iterations=10))

    assert result["status"] == "converged"
    assert len(result["findings"]) == 0
    assert design.call_count == 2  # ran on iter 0 and iter 1 (progress)


@pytest.mark.asyncio
async def test_checkpoint_files_written(tmp_path):
    """Checkpoints are written per iteration."""
    from bim_orchestrator.graph import build_graph

    findings = [_finding(element_id=f"e{i}") for i in range(3)]
    query = FakeQueryAgent(elements=[])
    qc = FakeQCAgent(findings_sequence=[findings, findings])
    design = FakeDesignAgent()

    app = build_graph(query, qc, design, checkpoint_dir=tmp_path)
    await app.ainvoke(_initial_state())

    # Should have at least 1 checkpoint (from bump after iteration 0)
    json_files = list(tmp_path.rglob("*.json"))
    assert len(json_files) >= 1

    import json

    raw = json_files[0].read_text()
    data = json.loads(raw)
    assert "findings" in data
    assert "iteration" in data
    # v1.5-R6 (3.4 hygiene): elements/check_trace excluded from the payload,
    # and the JSON is unindented (a debug artifact, not hand-edited).
    assert "elements" not in data
    assert "check_trace" not in data
    assert "\n" not in raw.strip()


@pytest.mark.asyncio
async def test_checkpoint_excludes_check_trace_when_present(tmp_path):
    """check_trace (QC's per-(element,rule) trace) is the other bulky key —
    dropped from the checkpoint the same way `elements` is."""
    from bim_orchestrator.graph import build_graph

    findings = [_finding(element_id=f"e{i}") for i in range(3)]
    query = FakeQueryAgent(elements=[{"id": "e0"}])
    qc = FakeQCAgent(findings_sequence=[findings, findings])
    design = FakeDesignAgent()

    app = build_graph(query, qc, design, checkpoint_dir=tmp_path)
    # FakeQCAgent doesn't emit check_trace, so stamp one directly onto the
    # initial state to prove the exclusion (not just "it happened to be absent").
    state = _initial_state()
    state["check_trace"] = [{"rule_id": "r", "element_id": "e0"}]  # type: ignore[typeddict-item]
    await app.ainvoke(state)

    import json
    json_files = list(tmp_path.rglob("*.json"))
    assert json_files
    data = json.loads(json_files[0].read_text())
    assert "check_trace" not in data
