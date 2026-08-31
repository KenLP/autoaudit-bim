"""Shared orchestrator state — the TypedDict LangGraph passes between agent nodes."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

Severity = Literal["severity_low", "severity_medium", "severity_high"]
Status = Literal["init", "querying", "checking", "designing", "converged", "failed"]

# Roadmap v1 task BB: 4-state compliance outcome per (element, rule) check.
#   compliant     -- value present + evaluator passes
#   non_compliant -- value present + evaluator fails (the original "violation")
#   manual_review -- evaluator fails but rule.requires_human=True; needs human
#   missing_data  -- parameter is absent / empty on the element; cannot evaluate
OutcomeStatus = Literal["compliant", "non_compliant", "manual_review", "missing_data"]


class OutcomeSummary(TypedDict):
    """Aggregate counts of check outcomes for a single QC run.

    Emitted as `outcomes_summary` on OrchestratorState after the QC node.
    `total` equals the number of (element, rule) pairs evaluated; the four
    buckets sum to `total`. Used by --check output and the v1 Streamlit UI's
    Results tab to render the 4-state summary card.
    """

    total: int
    compliant: int
    non_compliant: int
    manual_review: int
    missing_data: int
    # v1.5-R6 (Coverage block, §2.1a): elements/pairs skipped BEFORE the
    # `total` increment because a rule's per-category filter or scope_filter
    # ruled them out-of-scope (qc.py, both `continue`s ahead of `total += 1`).
    # NotRequired so pre-v1.5-R6 readers of an old outcomes_summary still
    # parse; absence renders as 0, not a crash.
    skipped_out_of_scope: NotRequired[int]


class CitationRef(TypedDict):
    """Phase 2 Day 5: structured citation for UI consumption.

    Mirrors `agents.grounding.Citation` but as a TypedDict so it can live in
    OrchestratorState without circular imports and can be JSON-serialized for
    checkpointing.
    """
    source: str
    section: str | None
    page: int | None
    snippet: str
    score: float


class DiagnosticNote(TypedDict):
    """Phase 2 (agent #2): an ADVISORY LLM diagnosis attached to a Finding.

    The Diagnostic Agent explains WHY a value likely fails and HOW a human might
    address it — pure judgment that enriches the finding. It is NOT a verdict and
    NOT a fix value: the deterministic engine still owns pass/fail (`status`,
    `severity`) and the Remediation Agent still owns the proposed value
    (`suggested_value`). Routing never reads this field.
    """
    summary: str            # root-cause / why it fails (advisory)
    suggested_action: str   # how a human might fix it (advisory, not a value)
    confidence: NotRequired[float]
    source: str             # provenance, e.g. "llm"


class SupervisorDirective(TypedDict):
    """Phase 2 (agent #3): an ADVISORY loop-control hint from the Supervisor.

    The Supervisor proposes whether the cyclic graph should keep iterating. It
    is bounded by the golden rule: ``route_node`` (deterministic) remains the
    sole decision authority and consults this ONLY in the branch where it would
    otherwise continue — so a directive can convert continue→stop, NEVER the
    reverse. Hard stops (zero findings / fingerprint-unchanged / max_iterations)
    are computed first and are non-overridable. ``stop_early`` = converge now;
    ``continue`` = no-op (let the deterministic route decide).
    """
    action: Literal["continue", "stop_early"]
    reason: str
    confidence: NotRequired[float]


class Finding(TypedDict):
    rule_id: str
    element_id: str
    parameter: str
    severity_tag: str
    severity: Severity
    # v1 dogfood QW-1: human-readable element name (e.g. "Closet 11A"),
    # surfaced from the AECDM element's `name` attribute. NotRequired so
    # pre-QW-1 callers continue to work; readers fall back to a truncated
    # element_id when this is absent. Surfaces in report.md tables and
    # the Streamlit Results tab.
    element_name: NotRequired[str]
    message: str
    # v1.4-K9: the element's CURRENT value at the checked parameter (display-
    # preferred), so reports + the Results/Approvals UI can show "current →
    # proposed" and a reviewer sees WHY the element fails the rule. NotRequired
    # for backward compat — older findings/readers fall back to the message text.
    current_value: NotRequired[Any]
    suggested_value: Any | None
    citation: str | None  # Phase 2: formatted citation string (Day 2)
    # Phase 2 Day 4: True when a hard-citation rule found no matching chunk.
    # Absent entirely on soft-mode rules or when citation was attached.
    citation_missing: NotRequired[bool]
    # Phase 2 Day 5: structured citations for UI rendering. Parallel to
    # `citation` (the formatted string). Absent when no citations attached.
    citation_refs: NotRequired[list[CitationRef]]
    # v1 task BB: which 4-state bucket this finding belongs to. NotRequired
    # for backward compat — pre-BB code paths produce only non_compliant
    # findings and may omit the field; readers should treat absence as
    # "non_compliant" (the original semantics).
    status: NotRequired[OutcomeStatus]
    # Phase 2 (agent #2): advisory LLM diagnosis. Absent unless the Diagnostic
    # Agent ran (feature-flagged). Advisory ONLY — never affects verdict/routing.
    diagnosis: NotRequired[DiagnosticNote]


class ProposedFix(TypedDict):
    finding_id: str
    element_id: str
    parameter: str
    new_value: Any
    autonomy: Literal["auto", "approve", "human-only"]
    approval_token: str | None
    preview: dict[str, Any] | None
    executed: bool


class CheckRecord(TypedDict):
    """v1 report module: one structured record per (element, rule) QC evaluated.

    Unlike ``Finding`` (which holds only the three FAILED buckets and is consumed
    by DesignAgent), a CheckRecord is emitted for EVERY evaluated pair INCLUDING
    ``compliant`` and lookup-``exempt`` ones — the PASS set that ``findings``
    throws away. The verification report lists that PASS set so a skeptical BIM
    Director can check for false negatives (silently-passed wrong elements).

    The record is captured DURING the QC pass (never re-derived afterwards — a
    second evaluation could disagree with the run's actual verdict), and it
    denormalizes the rule's comparison anatomy (``requirement`` / operand /
    ``threshold`` / ``operator`` / ``pattern`` / ``unit``) so the artifact is
    self-contained: the renderer can build a verify-recipe and operand table from
    the record alone, even when the original ``RuleSet`` isn't in hand (e.g. a
    later re-render). DesignAgent is NOT touched — the renderer joins the
    Design/Result outcome (Path A vs B, ACC issue id, before -> after) from
    ``proposed_fixes`` by ``(rule_id, element_id)``.

    Capture point: ``QCAgent.run`` (see ``report_trace.build_check_record``).
    """

    # ── identity ──
    rule_id: str
    element_id: str
    parameter: str
    requirement: str
    # ── QC "Query" evidence: what was read from the model ──
    raw_value: Any                  # value as pulled (pre unit-conversion)
    value: Any                      # value the evaluator actually saw (post-convert)
    # ── verdict (always present) ──
    passed: bool
    status: OutcomeStatus           # compliant | non_compliant | manual_review | missing_data

    # ── optional display + operands (NotRequired for back-compat / lean records) ──
    element_name: NotRequired[str]
    # Revit ElementId for the native "Select by ID" recipe — int when element_id
    # parses as one (the --run-revit path), else None (an ACC/AECDM URN element).
    revit_element_id: NotRequired[int | None]
    category: NotRequired[str]
    # v1.5-R6: the actual Revit parameter name (bound_parameter or the
    # canonical `parameter`) — see report_trace.revit_parameter_of. Absent on
    # pre-v1.5-R6 traces; readers fall back to `parameter`.
    revit_parameter: NotRequired[str]
    value_display: NotRequired[Any]            # params_display preference (e.g. "L2")
    # F-02: set ONLY when the element lives in a design option (an alternative
    # that may never be built) — absent means main model, so old traces read
    # correctly without migration.
    design_option: NotRequired[str]
    operand: NotRequired[Any]                  # the other side of a comparison
    operand_source: NotRequired[str]           # where `operand` came from (host param / lookup)
    threshold: NotRequired[Any]
    operator: NotRequired[str]
    unit: NotRequired[str]
    pattern: NotRequired[str]
    severity: NotRequired[Severity]
    suggested_value: NotRequired[Any]
    inherited_from: NotRequired[str]
    # lookup-exempt (relation_compare + lookup where the host imposes no
    # requirement, e.g. a door in a non-rated wall) — compliant-by-exemption.
    exempt: NotRequired[bool]


class OrchestratorState(TypedDict):
    project_id: str
    iteration: int
    max_iterations: int

    elements: list[dict[str, Any]]
    findings: list[Finding]
    proposed_fixes: list[ProposedFix]

    prev_finding_count: NotRequired[int]  # convergence tracking across iterations
    # v1.4-F3: content-based convergence. Pre-F3 the loop converged when
    # ``findings_count >= prev_count`` — but a run where iteration N fixes
    # finding A and surfaces finding B (count stays the same) would
    # false-converge even though the FINDING SET changed. The fingerprint
    # is a frozenset of stable (rule_id, element_id, parameter, status,
    # suggested_value) tuples; identical fingerprint across iterations is
    # the new "no progress" signal. Absent on the very first iteration.
    prev_findings_fingerprint: NotRequired[frozenset[tuple[Any, ...]]]
    # v1 task BB: 4-state outcome aggregate. NotRequired so pre-BB callers
    # of the graph (or older serialized checkpoints) keep parsing.
    outcomes_summary: NotRequired[OutcomeSummary]
    # v1 task BB: bucketed Findings parallel to `findings`. `findings` itself
    # continues to hold only non_compliant items (backward compat for DesignAgent
    # which routes them to ACC issues). manual_review + missing_data are kept
    # separate so task CC can write them to dedicated side reports (review_queue.md,
    # data_quality_report.md) without polluting the ACC Issues stream.
    manual_review_items: NotRequired[list[Finding]]
    missing_data_items: NotRequired[list[Finding]]
    # v1.4-K7 (Tầng 3 coordination): shared-state bucket for geometry findings,
    # computed once by GeometricQueryAgent (outside the LangGraph loop). Kept
    # SEPARATE from `findings` so the param rules_engine / route convergence
    # never see them, but DesignAgent reads this bucket on iteration 0 and folds
    # geometry findings into the per-element Path A grouping — so an element
    # flagged by BOTH a parameter rule and a geometry rule gets ONE unified ACC
    # Issue, not two. Absent on param-only or Forma-only runs.
    geometry_findings: NotRequired[list[Finding]]
    # P1-GEO-01: execution evidence for the geometry half — which rules ran,
    # which could not, and why. Absent on runs with no geometry rules.
    geometry_coverage: NotRequired[dict[str, Any]]
    # Phase 2 (agent #3): advisory loop-control hint written by the Supervisor
    # node and consumed by route_node. Absent unless the Supervisor ran
    # (feature-flagged). Advisory ONLY — route honours it only to stop EARLY,
    # never to extend past the deterministic convergence / max_iterations gates.
    supervisor_directive: NotRequired[SupervisorDirective]
    # GĐ3-A3: bounded per-iteration count trail, appended by ``bump_node`` (last
    # ~8 entries). Deterministic + cheap; lets the Supervisor see the TREND
    # (stalled 2 rounds vs still dropping) beyond a single-step delta, and feeds
    # the verification report. Counts only — no raw findings.
    iteration_history: NotRequired[list[dict[str, Any]]]
    # v1 report module: structured per-(element, rule) trace, INCLUDING compliant
    # + exempt outcomes (which `findings` discards). Populated by QCAgent on every
    # pass (overwritten each pass → reflects the FINAL QC verdict at convergence).
    # The verification report renders this; it is never re-derived. Absent on
    # pre-report runs and older serialized checkpoints — readers must tolerate it.
    check_trace: NotRequired[list[CheckRecord]]
    # v1.5-R7 (R1-Stage 1): per-iteration record of every successfully EXECUTED
    # Path B write — {"iteration", "rule_id", "write_eid", "parameter", "old",
    # "new"}. UNLIKE `check_trace` (overwritten every QC pass), this ACCUMULATES
    # across iterations — DesignAgent.run reads the prior list and returns it
    # NOTED-on, never replaced, so the full write history survives to the end of
    # the run. Feeds `report_trace.detect_fix_interactions` (a parameter written
    # by ≥2 rules, or in ≥2 iterations, is a concrete critical pair — see
    # docs/260711_Autofix Loop.md). Absent on pre-R7 runs/checkpoints.
    fix_write_log: NotRequired[list[dict[str, Any]]]
    # v1.5-R7 (cap-hit honesty): which route_node branch actually stopped the
    # loop — "fingerprint_stable" | "zero_findings" | "iteration_cap" |
    # "supervisor" | "failed". Set by route_node at the exact stop branch; a
    # label only, never changes route logic. Absent on the still-looping
    # ("designing") status and on pre-R7 runs/checkpoints.
    stop_reason: NotRequired[str]
    # L2-12: what Phase 2 was ASKED for versus what was actually wired —
    # {"requested", "wired" (each {remediation, diagnostic, supervisor}),
    # "flag_problems" (env values not understood), "rules_requesting_llm",
    # "llm_rules_degraded_to_path_a"}. A mistyped flag, a missing plugin, and a
    # ruleset whose `llm_propose` rules had no agent to serve them all produced
    # a run indistinguishable from a plain Phase-1 one — so "the AI proposed
    # nothing today" and "the AI was never asked" read identically. Recorded,
    # never enforced: an unwired agent still yields a complete, valid audit.
    # Absent when there is nothing to say (no flags, no LLM rules, no bad
    # values) and on pre-R8 runs/checkpoints.
    llm_status: NotRequired[dict[str, Any]]
    # Query-plan coverage, stamped by whichever query agent ran —
    # {"targets_requested", "categories_resolved", "categories_dropped"
    # (each {category, reason}), "rule_count"}. Answers "was this model
    # actually audited?", which zero-findings alone cannot: every target
    # category dropped (unknown label / unsupported on the backend) also
    # yields zero findings and a converged run. `_exit_code_for` fails the
    # run when a non-empty ruleset resolved to NO category. Absent on
    # pre-R7 runs/checkpoints — readers must tolerate it.
    query_coverage: NotRequired[dict[str, Any]]
    # Document-identity stamp (spec SPEC_DOCUMENT_IDENTITY_STAMP): captured ONCE
    # at run start from the live Revit client, attached AFTER the graph returns
    # (never a LangGraph channel). Renderers READ this; they never re-fetch.
    # Absent on Forma-only / check / pre-spec runs — readers must tolerate it.
    document_info: NotRequired[dict[str, Any] | None]

    status: Status
    error: str | None


def coverage_verdict(
    state: OrchestratorState | dict[str, Any],
) -> Literal["ok", "partial", "no_audit"] | None:
    """Was this model actually audited? (M2, 2026-07 audit — single source.)

    Reads ``state["query_coverage"]`` (stamped by the query agents):
      * ``no_audit`` — a non-empty parameter ruleset resolved to ZERO query
        specs (every target category unknown/unsupported): nothing fetched,
        nothing checked, yet the graph converged "clean".
      * ``partial`` — some categories resolved, others were dropped.
      * ``ok`` — every requested category resolved.
      * ``None`` — no coverage recorded (Forma-only / legacy run) → not
        applicable.

    Used by BOTH ``orchestrator._exit_code_for`` (fails the run on ``no_audit``)
    and ``run_recorder.write_metadata`` (records the coverage state in the
    durable artifact + downgrades the recorded status), so the exit code, the
    recorded status, and metadata.json can never disagree.
    """
    cov = state.get("query_coverage")
    if not cov:
        return None
    return _coverage_verdict_from(cov)


def _coverage_verdict_from(cov: dict[str, Any]) -> Literal["ok", "partial", "no_audit"]:
    if cov.get("rule_count") and not cov.get("categories_resolved"):
        # L4 (audit): keying this on `targets_requested` being non-empty left a
        # second "never audited" route open — a ruleset WITH rules but an empty
        # `target_category` (the schema permits "" / []) requests nothing,
        # resolves nothing, fetches nothing, and used to exit 0. What matters is
        # that rules existed and NOTHING resolved, however that came about.
        # `rule_count == 0` (the legacy categories= path, geometry-only rulesets)
        # is still a legitimate no-op, not a failure.
        return "no_audit"
    if cov.get("categories_dropped"):
        return "partial"
    return "ok"


def geometry_verdict(
    state: OrchestratorState | dict[str, Any],
) -> Literal["ok", "partial", "no_audit"] | None:
    """Did the GEOMETRY half of this run actually execute? (P1-GEO-01.)

    The twin of :func:`coverage_verdict`, reading ``state["geometry_coverage"]``
    as stamped by ``GeometricQueryAgent``:

      * ``no_audit`` — geometry rules were requested and NONE ran (MCP error,
        or a reference link that never resolved). This is the state that was
        indistinguishable from "checked, found no clashes": both produced an
        empty findings list, a converged run and exit 0.
      * ``partial``  — some geometry rules ran, others could not.
      * ``ok``       — every dispatched geometry rule got a real answer.
      * ``None``     — no geometry rules (not applicable).

    Read by ``orchestrator._exit_code_for`` and by :func:`recorded_status`, so
    the exit code, metadata.json and the report cannot disagree about whether
    the geometry audit happened — the same single-source rule P1-07 set for
    parameter coverage.
    """
    cov = state.get("geometry_coverage")
    if not isinstance(cov, dict) or not cov:
        return None
    verdict = cov.get("verdict")
    if verdict in ("ok", "partial", "no_audit"):
        return verdict  # type: ignore[return-value]
    return None


def recorded_status(status: str, state: OrchestratorState | dict[str, Any]) -> str:
    """The ONE terminal status every durable artifact must show (P1-07).

    `state["status"]` is the graph's internal loop state, and the run modes
    pass their own label to the recorder — after `check()` QC leaves the state
    on "designing" while the recorder writes "completed", so metadata.json and
    verification_report.md described the same run differently. Both now derive
    the string from here, including the "no_audit" downgrade, so they cannot
    drift apart.

    P1-GEO-01: a geometry ``no_audit`` downgrades the same way. A run whose
    only checks were geometry checks, none of which executed, must never be
    recorded as a completed audit.
    """
    if coverage_verdict(state) == "no_audit" or geometry_verdict(state) == "no_audit":
        return "no_audit"
    return status
