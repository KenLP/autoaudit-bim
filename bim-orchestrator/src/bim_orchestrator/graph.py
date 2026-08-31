"""LangGraph cyclic graph: Query → QC → [Grounding?] → Route → Design → Bump → …

The loop runs until one of these convergence conditions:
    1. Findings count == 0 → fully resolved (status = "converged")
    2. Findings fingerprint unchanged across iterations → no progress
       (status = "converged"). v1.4-F3 — pre-F3 we used a count-based
       heuristic that false-converged when fix-A-reveals-B kept the count
       stable; the fingerprint compares the actual SET of findings.
    3. Iteration >= max_iterations → safety stop (status = "failed")

Phase 1: AECDM read-only → findings don't decrease → converges after 1 re-check.
Phase 2: with `grounding_agent` passed in, a Grounding node sits between QC and
Route to attach BEP / IBC citations to each finding via RAG. Backward compatible —
omit the agent and the graph is identical to Phase 1.

GĐ3-A2: the Diagnostic Agent runs ONCE on loop EXIT (route → diagnostic → END),
not once per iteration. Explaining findings that DesignAgent is about to fix in
the same iteration is wasted cost + latency; the only findings worth explaining
are the ones that SURVIVE to the final state (the set the UI / report shows). So
`max_diagnoses` now bounds the whole run, and the loop spends zero LLM calls on
explanation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import structlog
from langgraph.graph import END, START, StateGraph

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.grounding import GroundingAgent
from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.query import QueryAgent
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.llm.interfaces import DiagnosticAgentProtocol, SupervisorAgentProtocol
from bim_orchestrator.state import Finding, OrchestratorState


def _fingerprint(findings: list[Finding]) -> frozenset[tuple[Any, ...]]:
    """Content fingerprint of the current findings set.

    v1.4-F3: lets the route node tell apart "no progress" (set unchanged)
    from "different findings but same count" (set changed → keep looping
    so DesignAgent has a chance at the new set). Stable, JSON-friendly
    fields only — ``status`` collapses missing to ``"non_compliant"``
    matching pre-v1-task-BB readers.
    """
    return frozenset(
        (
            f["rule_id"],
            f["element_id"],
            f.get("parameter", ""),
            f.get("status", "non_compliant"),
            # ``suggested_value`` is included so a re-query that produces
            # the same violation but with a different proposed fix is
            # treated as new state. Convert unhashable values defensively.
            _hashable(f.get("suggested_value")),
        )
        for f in findings
    )


def _hashable(value: Any) -> Any:
    """Make a possibly-unhashable value safe for a frozenset element."""
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)

log = structlog.get_logger(__name__)


def build_graph(
    query_agent: QueryAgent | RevitQueryAgent,
    qc_agent: QCAgent,
    design_agent: DesignAgent,
    *,
    grounding_agent: GroundingAgent | None = None,
    diagnostic_agent: DiagnosticAgentProtocol | None = None,
    supervisor_agent: SupervisorAgentProtocol | None = None,
    checkpoint_dir: Path | None = None,
) -> Any:
    """Build and compile the orchestrator's cyclic StateGraph.

    Pass `grounding_agent` to insert a RAG-backed citation step between QC and
    Route (Phase 2). Pass `supervisor_agent` for an advisory loop-control step
    (runs every iteration — it advises per-iteration). Pass `diagnostic_agent`
    for an advisory LLM explanation step that runs ONCE on loop exit (GĐ3-A2 —
    not per iteration). All optional and independent:
        qc → [grounding] → [supervisor] → route → design (loop)
                                          route → [diagnostic] → END (on exit)
    Leave all None for Phase 1 behavior (identical graph). The supervisor can
    only advise stopping EARLY — route_node remains the sole authority and never
    extends the loop past its deterministic gates (see route_node).
    """

    # ---- node functions (closures capturing agents) -------------------------

    async def query_node(state: OrchestratorState) -> dict[str, Any]:
        return await query_agent.run(state)

    def qc_node(state: OrchestratorState) -> dict[str, Any]:
        return qc_agent.run(state)

    def grounding_node(state: OrchestratorState) -> dict[str, Any]:
        assert grounding_agent is not None
        return grounding_agent.run(state)

    async def diagnostic_node(state: OrchestratorState) -> dict[str, Any]:
        """Advisory enrichment — L2-06 / L2-03.

        Two invariants this node now ENFORCES instead of documenting:

        1. **It cannot change the audit.** ``interfaces.py`` says the Diagnostic
           may only set ``Finding["diagnosis"]``, but this node returned the
           plugin's whole state dict straight into the LangGraph merge — so a
           drifted plugin could drop findings or rewrite a verdict and the run
           would still report ``converged``. We now return ``{}``; the plugin's
           in-place ``diagnosis`` writes on the finding dicts still land, which
           is the one effect it is permitted to have.
        2. **It cannot kill the run.** This node executes on loop EXIT — after
           Path B has already written to Revit. An exception here skipped
           ``findings.json``, the report AND the run record: the model changed
           and nothing recorded it. An advisory step must never be able to do
           that, whatever the plugin does.
        """
        assert diagnostic_agent is not None
        try:
            await diagnostic_agent.run(state)
        except Exception as exc:  # advisory: degrade, never abort
            log.warning(
                "graph.diagnostic_failed", error=str(exc),
                error_type=type(exc).__name__,
                detail="advisory enrichment skipped; the audit is unaffected",
            )
        return {}

    async def supervisor_node(state: OrchestratorState) -> dict[str, Any]:
        """Advisory loop-control — L2-06 / L2-03.

        Same two invariants. The merge is allow-listed to the ONE key this agent
        may write: returning the plugin's dict wholesale let a drifted
        supervisor wipe ``findings``, forge ``query_coverage`` (the very
        artifact added to answer "was this model audited?"), or rewind
        ``iteration`` so ``route_node``'s hard stops — which read that state —
        never fire. The directive's CONTENT is still validated by ``route_node``.
        """
        assert supervisor_agent is not None
        try:
            out = await supervisor_agent.run(state)
        except Exception as exc:  # advisory: degrade to the deterministic route
            log.warning(
                "graph.supervisor_failed", error=str(exc),
                error_type=type(exc).__name__,
                detail="loop control falls back to the deterministic route",
            )
            return {}
        if not isinstance(out, dict):
            return {}
        extra = set(out) - {"supervisor_directive"}
        if extra:
            log.warning(
                "graph.supervisor_extra_keys_dropped", keys=sorted(extra),
                detail="an advisory agent may only write supervisor_directive",
            )
        directive = out.get("supervisor_directive")
        return {"supervisor_directive": directive} if directive is not None else {}

    def route_node(state: OrchestratorState) -> dict[str, Any]:
        """Convergence check — decides whether to proceed to Design or stop.

        v1.4-F3 ordering:
          1. iteration == 0 → always run Design once
          2. findings_count == 0 → converged (zero work left)
          3. fingerprint unchanged vs previous iteration → converged
             (no progress — the set of violations is identical)
          4. iteration >= max_iterations → failed (safety stop)
          5. otherwise → keep looping (set changed, so Design gets
             another shot at the new findings)

        The fingerprint test replaces the pre-F3 ``findings_count >=
        prev_count`` heuristic, which false-converged when fix-A-reveals-B
        kept the count stable across iterations.
        """
        findings = state.get("findings", [])
        findings_count = len(findings)
        prev_count = state.get("prev_finding_count", -1)
        prev_fp = state.get("prev_findings_fingerprint")
        iteration = state["iteration"]

        if iteration == 0:
            log.info(
                "route.first_iteration",
                findings=findings_count,
            )
            return {"status": "designing"}

        if findings_count == 0:
            log.info("route.converged", reason="zero_findings")
            return {"status": "converged", "stop_reason": "zero_findings"}

        curr_fp = _fingerprint(findings)
        if prev_fp is not None and curr_fp == prev_fp:
            log.info(
                "route.converged",
                reason="fingerprint_unchanged",
                current=findings_count,
                previous=prev_count,
            )
            return {"status": "converged", "stop_reason": "fingerprint_stable"}

        if iteration >= state["max_iterations"]:
            log.warning(
                "route.max_iterations",
                iteration=iteration,
                max=state["max_iterations"],
            )
            return {
                "status": "failed",
                "error": f"max_iterations_reached ({iteration}/{state['max_iterations']})",
                "stop_reason": "iteration_cap",
            }

        # (5) SOFT — Phase 2 Supervisor (#3). Reached ONLY when every hard stop
        # above declined to stop, i.e. the deterministic logic would CONTINUE.
        # Here — and only here — an advisory directive may stop the loop EARLY.
        # This ordering is the governance guarantee: the supervisor can convert
        # continue→stop, never stop→continue, and never extends past (4).
        directive = state.get("supervisor_directive")
        if directive and directive.get("action") == "stop_early":
            log.info(
                "route.converged",
                reason="supervisor_stop_early",
                note=directive.get("reason"),
                findings=findings_count,
            )
            return {"status": "converged", "stop_reason": "supervisor"}

        log.info(
            "route.continue",
            findings=findings_count,
            previous=prev_count,
            delta=prev_count - findings_count,
            fingerprint_changed=(prev_fp is not None and curr_fp != prev_fp),
        )
        return {"status": "designing"}

    async def design_node(state: OrchestratorState) -> dict[str, Any]:
        return await design_agent.run(state)

    def bump_node(state: OrchestratorState) -> dict[str, Any]:
        """Increment iteration, snapshot prev_finding_count + fingerprint, checkpoint."""
        new_iter = state["iteration"] + 1
        findings = state.get("findings", [])
        fc = len(findings)
        fp = _fingerprint(findings)
        log.info(
            "bump.next_iteration",
            iteration=new_iter,
            prev_finding_count=fc,
            prev_fingerprint_size=len(fp),
        )

        if checkpoint_dir is not None:
            _write_checkpoint(state, checkpoint_dir, new_iter - 1)

        # GĐ3-A3: append a bounded count trail (last 8) for the Supervisor's trend
        # view + the report. Deterministic, counts only.
        prev_fp = state.get("prev_findings_fingerprint")
        history = list(state.get("iteration_history", []))
        history.append(
            {
                "iteration": state["iteration"],
                "non_compliant": fc,
                "manual_review": len(state.get("manual_review_items", []) or []),
                "missing_data": len(state.get("missing_data_items", []) or []),
                "fingerprint_changed": prev_fp is not None and fp != prev_fp,
            }
        )

        return {
            "iteration": new_iter,
            "prev_finding_count": fc,
            "prev_findings_fingerprint": fp,
            "iteration_history": history[-8:],
            "status": "querying",
        }

    # ---- conditional edge ---------------------------------------------------

    def after_route(state: OrchestratorState) -> Literal["design", "diagnose", "__end__"]:
        if state["status"] == "designing":
            return "design"
        # Loop is exiting (converged / failed / supervisor stop). Run the advisory
        # Diagnostic pass ONCE here on the FINAL findings, if wired (GĐ3-A2).
        return "diagnose" if diagnostic_agent is not None else "__end__"

    def after_query(state: OrchestratorState) -> Literal["qc", "__end__"]:
        """H1: when the query agent could not read the model (e.g. the Revit addin
        is unreachable) it returns status="failed" + error. Route straight to END so
        that failure is PRESERVED — never let QC + route recompute it into
        "converged". A compliance tool that reports "all clear" when it could not
        read the model is the worst failure mode possible; the orchestrator already
        maps a final status=="failed" to a non-zero exit + a failed run record."""
        if state.get("status") == "failed":
            log.error("query.failed", error=state.get("error"))
            return "__end__"
        return "qc"

    # ---- wire the graph -----------------------------------------------------

    graph = StateGraph(OrchestratorState)

    graph.add_node("query", query_node)
    graph.add_node("qc", qc_node)
    graph.add_node("route", route_node)
    graph.add_node("design", design_node)
    graph.add_node("bump", bump_node)

    graph.add_edge(START, "query")
    # H1: a failed query short-circuits to END (see after_query) instead of the
    # old unconditional query→qc edge that let a read failure look "converged".
    graph.add_conditional_edges("query", after_query, {"qc": "qc", "__end__": END})

    # Build the QC → … → Route spine through whichever per-iteration Phase 2 nodes
    # are present, preserving order: grounding (citations) then supervisor
    # (loop-control advice). Each link is added only for the agents injected, so
    # Phase 1 (all None) keeps the exact qc→route edge. Diagnostic is NOT in this
    # spine — it runs once on loop exit (GĐ3-A2), wired below.
    chain: list[str] = ["qc"]
    if grounding_agent is not None:
        graph.add_node("grounding", grounding_node)
        chain.append("grounding")
    if supervisor_agent is not None:
        graph.add_node("supervisor", supervisor_node)
        chain.append("supervisor")
    chain.append("route")
    for src, dst in zip(chain, chain[1:]):
        graph.add_edge(src, dst)

    # Route's exit branch: run the advisory Diagnostic pass ONCE on the final
    # findings (if wired) before ending, else end directly.
    route_targets: dict[str, Any] = {"design": "design", "__end__": END}
    if diagnostic_agent is not None:
        graph.add_node("diagnostic", diagnostic_node)
        graph.add_edge("diagnostic", END)
        route_targets["diagnose"] = "diagnostic"
    log.info(
        "graph.phase2_chain",
        chain=" → ".join(chain),
        diagnostic_on_exit=diagnostic_agent is not None,
    )

    graph.add_conditional_edges("route", after_route, route_targets)
    graph.add_edge("design", "bump")
    graph.add_edge("bump", "query")

    return graph.compile()


# ---- checkpointing (simple JSON) -------------------------------------------


# v1.5-R6 (3.4 hygiene): these two keys dwarf everything else in `state` —
# `check_trace` is one record per (element, rule) and `elements` is the full
# fetched element list — and neither is needed to inspect or resume the
# convergence loop from a checkpoint (`check_trace` is rebuilt fresh by QC
# every iteration; `elements` likewise by the query agent). Dropped from the
# CHECKPOINT PAYLOAD only — `state` itself is untouched, so the running graph
# still has both.
_CHECKPOINT_EXCLUDED_KEYS = frozenset({"check_trace", "elements"})


def _write_checkpoint(state: OrchestratorState, base_dir: Path, iteration: int) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base_dir / ts.split("T")[0]
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"iteration_{iteration:02d}_{ts}.json"
    payload = {k: v for k, v in state.items() if k not in _CHECKPOINT_EXCLUDED_KEYS}
    serializable = _make_serializable(payload)
    # No indent (3.4 hygiene) — this file is a debug/resume artifact, not
    # meant for hand-editing; unindented JSON is smaller and just as greppable.
    path.write_text(json.dumps(serializable, default=str))
    log.info("checkpoint.written", path=str(path), keys=list(serializable.keys()))


def _make_serializable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_serializable(v) for v in obj]
    # v1.4-F3: ``prev_findings_fingerprint`` is a ``frozenset`` of tuples —
    # encode as a sorted list of lists so the JSON checkpoint stays
    # deterministic and re-loadable. Plain ``set`` handled too for safety.
    if isinstance(obj, (frozenset, set)):
        return sorted([_make_serializable(v) for v in obj], key=repr)
    if isinstance(obj, tuple):
        return [_make_serializable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)
