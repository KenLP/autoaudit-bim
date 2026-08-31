"""Design Agent — proposes and (after approval) applies fixes via MCP.

Phase 1 path (path A): for each Finding the agent builds an ACC Issue via
Forma MCP, runs the dry-run → approval token → execute trust pipeline,
and emits a ProposedFix with audit-chain entries.

Phase 2 Week 6 path B: when a ``rules`` (RuleSet) is provided and the
rule for the finding is tagged ``fixability: auto``, dispatch through
Revit MCP instead — preview the parameter write with ``dryRun=true``,
consult autonomy, then commit ``dryRun=false`` and optionally tag the
element's Comments parameter for audit.

Routing matrix:
  * No ``rules`` provided                → path A (Phase 1 backward-compat)
  * ``rules`` + ``revit_mcp`` + auto     → path B (Revit write-back)
  * ``rules`` + manual / no revit_mcp    → path A
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from bim_orchestrator.approval_store import read_record, write_record
from bim_orchestrator.issue_registry import RESOLVED_ISSUE_STATUSES, IssueRegistry
from bim_orchestrator.issue_registry import group_key as _issue_registry_group_key
from bim_orchestrator.llm.interfaces import RemediationAgentProtocol
from bim_orchestrator.mcp_clients.forma import FormaMCPClient
from bim_orchestrator.mcp_clients.revit import (
    AnyRevitClient,
    RevitEnvelopeError,
    StepOutcome,
    batch_commit_outcomes,
)
from bim_orchestrator.policies.approval_integrity import (
    fingerprint,
    fingerprint_line,
    parse_fingerprint,
)
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.demo_identity import DEMO_PROJECT_ID
from bim_orchestrator.policies.rules_engine import evaluate as _evaluate_requirement
from bim_orchestrator.policies.rules_schema import fetch_name as _fetch_name
from bim_orchestrator.state import Finding, OrchestratorState, ProposedFix

log = structlog.get_logger(__name__)

# Low3: a value only counts as a FIRE-RATING candidate (eligible for
# ``_collapse_to_one``'s magnitude-resolution) when it either carries an
# explicit duration unit token ("HR"/"hour"/"MIN"/...) or is a recognised
# not-rated sentinel ("NR", "-", "None", ...). Mirrors the unit-token half of
# ``fire_rating_units._VALUE_RE`` (kept local here since that module is out
# of this fix's file scope). Historically a BARE number like "2100" also
# parsed via ``parse_to_minutes``, which made plain numeric type conflicts
# (a Type Mark, a count field) magnitude-collapse as if they were ratings;
# B-4 (2026-08-16) made bare numbers unparseable, but this screen stays as
# its own belt (C-01 doctrine: two independent belts, either alone suffices)
# — only an EXPLICIT unit or sentinel triggers max-resolution + host_conflict.
_FIRE_RATING_UNIT_RE = re.compile(
    r"\d\s*[-_ ]?\s*(hours?|hrs?|hr|h|minutes?|mins?|min|m)\s*$", re.IGNORECASE
)
_FIRE_RATING_SENTINELS = frozenset(
    {"nr", "n/a", "na", "none", "no", "no rating", "not rated", "-", "--"}
)


def _looks_like_fire_rating(value: Any) -> bool:
    """True when ``value`` carries an explicit duration unit or is a
    not-rated sentinel — see ``_FIRE_RATING_UNIT_RE`` docstring above."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.casefold() in _FIRE_RATING_SENTINELS:
        return True
    return _FIRE_RATING_UNIT_RE.search(text) is not None


class DesignAgent:
    def __init__(
        self,
        mcp: FormaMCPClient | None,
        autonomy: AutonomyPolicy,
        project_id: str,
        *,
        max_issues: int = 2,
        # v1.1 (2026-06-02): default reverted from "room.department.required"
        # (a Phase 1 holdover) to None. The old default silently filtered
        # any --run-revit invocation that didn't pass --rule, so partitioning
        # under room_compliance.yaml never saw area/height findings. Pass
        # "none" / "all" / "" explicitly when you want no filter from the CLI.
        rule_filter: str | None = None,
        dry_run_only: bool = False,
        published: bool = True,
        issue_subtype_id: str | None = None,
        # Phase 2 Week 6 — path B dispatch
        revit_mcp: AnyRevitClient | None = None,
        rules: Any = None,  # RuleSet | None (avoid circular import)
        # v1.4-K5: when set, approve-gated Path B fixes are gathered into ONE
        # ACC "proposal issue" and a record is written here for the
        # ApprovalWatcher to pick up. None → legacy silent-park behaviour.
        approvals_dir: Path | None = None,
        # Phase 2: optional runtime remediation intelligence. When injected,
        # rules with `new_value_strategy: llm_propose` get their fix value from
        # this agent (closed-loop re-validated). None (the Phase 1 default) →
        # llm_propose yields no value → the finding routes to a Path A issue.
        # SPEC_LLM_PLUGIN_SPLIT: the concrete class (RemediationLLMAgent) lives
        # in the private bim_orchestrator_llm plugin now — this only needs the
        # socket Protocol (duck-typed; no isinstance check).
        llm_agent: RemediationAgentProtocol | None = None,
        # Phase 2 GĐ2: category → valid classification codes, for the
        # value_in_subset closed loop. None → lazy-loaded from the default
        # config on first use; inject one in tests to avoid disk/OST deps.
        classification: Any = None,
        # Scheduled/continuous audit (Mức 1): nothing writes the model
        # unattended — demotes every would-be-auto Path B decision to
        # approve-gated. See ``_prepare_revit_fix``.
        propose_only: bool = False,
        # Cross-run Path A issue dedup (Mức 1, Q3). None (default) → legacy
        # behaviour, no registry read/write. Only ``audit()`` passes a path.
        issue_registry: Path | None = None,
        # L-01: the run's per-category element cap (``--max-elements``), so a
        # `next_available` proposal can DISCLOSE that the "already used" set it
        # chose from may be partial. None → no cap in play, nothing to say.
        max_elements: int | None = None,
    ) -> None:
        self._mcp = mcp
        self._approvals_dir = approvals_dir
        self._llm_agent = llm_agent
        self._propose_only = propose_only
        self._issue_registry = (
            IssueRegistry(issue_registry) if issue_registry is not None else None
        )
        self._classification = classification
        self._classification_loaded = classification is not None
        # Phase 2: the element population of the current run() — stashed so
        # _llm_propose can mine COMPLIANT sibling values as few-shot examples
        # for the model. Set per-run; empty until then.
        self._all_elements: list[dict[str, Any]] = []
        # GĐ3-A4: in-run memo for LLM proposals. Many findings share the SAME
        # question — e.g. 30 doors of one Type all with the same bad name → one
        # (rule, current value, param, category) key. Cache the answer (INCLUDING
        # None, so a rejected question isn't re-asked). unique_in_set is never
        # cached (each element needs a DIFFERENT value). Not persisted across runs
        # (a stale value must never be written to a live model).
        self._llm_propose_memo: dict[tuple[Any, ...], Any] = {}
        # L2-01: keys proven NOT shareable — a cached answer failed a later
        # element's validator, so the two questions only looked alike (same
        # current value, different host / condition). Never cached again this
        # run; without this the entry would ping-pong between elements.
        self._llm_propose_memo_unshareable: set[tuple[Any, ...]] = set()
        # GĐ3-B2: batch the LLM proposal for a format/naming rule-group with MORE
        # than this many distinct uncached questions into ONE call (the rest keep
        # the per-element path). Each batched value is still validated per element.
        self._llm_batch_min = 3
        self._autonomy = autonomy
        self._project_id = project_id
        self._max_issues = max_issues
        self._rule_filter = rule_filter
        self._dry_run_only = dry_run_only
        self._published = published
        self._issue_subtype_id = issue_subtype_id
        self._revit_mcp = revit_mcp
        self._rules_by_id: dict[str, Any] = {
            r.id: r for r in (rules.rules if rules is not None else [])
        }
        # Lazy cache: parameter name → set of values seen on Revit rooms.
        # Populated on first ``next_available`` request, reused for the run.
        # Keyed by (category, parameter): keying on the parameter alone let a
        # Doors rule and a Ducts rule share one "Mark" set (L-01).
        self._revit_siblings_cache: dict[tuple[Any, str], set[Any]] = {}
        self._max_elements = max_elements
        # v1.4-K17: {family name → family element id}, fetched once per run when a
        # remediation.target=="family" rule is active (to rename the Family Name).
        self._family_id_by_name: dict[str, int] = {}
        # v1.5-R5: in-run memo for proposal-issue dedup. DesignAgent is constructed
        # ONCE per run() (graph.py builds it before the LangGraph loop) and lives
        # across iterations, so a set keyed by (rule_id, fingerprint) survives
        # iteration 0 → iteration 1. Without this, an auto-fix elsewhere changes
        # the findings fingerprint that drives route_node's convergence check →
        # the loop runs again → the SAME still-approve-gated findings for an
        # untouched rule get re-proposed as a SECOND ACC issue. Cross-run
        # duplicates (two separate `--run-revit` invocations before anyone
        # approves) are caught separately by scanning the approvals dir — see
        # ``_find_parked_duplicate``.
        # v1.5-R6: value is the ACC issue id from the FIRST proposal for this
        # (rule_id, fingerprint) — needed so a memo-hit (below) can stamp the
        # SAME issue id onto the new iteration's fixes instead of leaving them
        # unstamped (see the memo-hit branch in ``_create_proposal_issue``).
        self._proposed_fingerprints: dict[tuple[str, str], str] = {}
        # v1.5-R5 (Path A half): same in-run-only dedup for manual-finding
        # groups (``_propose_rule_group``). Keyed by (rule_id, bucket, element
        # set) rather than a write-set fingerprint (Path A findings carry no
        # suggested value to fingerprint) — an unchanged group of elements
        # failing the same rule+status must not get a second ACC issue when
        # route_node re-loops because some OTHER rule's auto-fix changed the
        # findings fingerprint. Deliberately NOT cleared in run() — this must
        # survive across iterations, same as ``_proposed_fingerprints`` above.
        # Cross-run duplicates (two separate CLI invocations) are left alone —
        # unlike Path B's parked write-set, a Path A issue may already have
        # been closed by hand on ACC, so scanning for a cross-run duplicate
        # here would risk silently suppressing a genuinely new issue.
        self._proposed_group_keys: set[tuple[str, str, frozenset[Any]]] = set()

    async def run(self, state: OrchestratorState) -> OrchestratorState:
        # v1.4-K3 (Layer 1) — missing_data routing by value-availability:
        #   • missing_data WITH an auto-fill source (revit_mcp wired +
        #     fixability=auto + a concrete autofill strategy) → Path B: we can
        #     compute the correct value, so write it to Revit.
        #   • missing_data WITHOUT a source → Path A ACC Issue ("fill in X"),
        #     so a blank required field is escalated to a human instead of
        #     being silently dropped (the pre-K3 behaviour).
        # This deliberately revises the old v1-BB invariant ("missing_data
        # never becomes an ACC Issue"): we now raise an issue PRECISELY when we
        # cannot compute the value, and only auto-write when we can — never
        # fabricating data. non_compliant findings are unchanged: still
        # partitioned by rule.fixability via _partition (scope A).
        # Phase 2: make the run's element population available to _llm_propose
        # for few-shot compliant-example mining (additive; no Phase 1 effect).
        self._all_elements = list(state.get("elements", []) or [])
        # GĐ3-A4: scope the LLM-proposal memo to ONE design pass. Within a pass the
        # bulk win is real (N identical elements → 1 call); clearing between passes
        # avoids reusing a value after the underlying element may have changed.
        self._llm_propose_memo.clear()
        self._llm_propose_memo_unshareable.clear()   # L2-01: same pass scope
        # v1.5-R5: same reasoning, for `_next_available`'s sibling-reservation
        # cache. Within ONE design pass, reserving a chosen candidate ("101A")
        # into the cache is correct (stops a second duplicate-"101" room from
        # also proposing "101A"). But a `next_available` write is never
        # committed during the run — the autonomy gate in `_prepare_revit_fix`
        # demotes it to `approve` by construction (F-02), so it always parks as
        # a proposal for Loop 2 — and if the cache survives into the
        # NEXT iteration (route_node looped because some OTHER rule's auto-fix
        # changed the findings fingerprint), the still-unresolved duplicate finds
        # "101A"/"101B" already reserved from last time and advances PAST them to
        # "101C"/"101D" — a DIFFERENT write-set every iteration, which defeats the
        # (rule_id, fingerprint) proposal dedup below (a moving target never
        # matches). Clearing here makes each design pass re-derive candidates from
        # the live (unchanged) Revit state, so a still-parked proposal computes
        # the SAME values every iteration.
        self._revit_siblings_cache.clear()
        missing_items = self._apply_rule_filter(
            state.get("missing_data_items", []) or []
        )
        missing_to_b: list[Finding] = []
        missing_to_a: list[Finding] = []
        for f in missing_items:
            target = missing_to_b if self._can_auto_fill_missing(f) else missing_to_a
            target.append(f)

        element_by_id = {el.get("id"): el for el in state["elements"]}
        await self._ensure_family_map()

        # v1.4-F2: partition BEFORE quota (Path-B-first) to avoid starvation.
        findings = self._apply_rule_filter(list(state["findings"]))
        path_a_findings, path_b_findings = self._partition(findings)
        path_a_full = path_a_findings + missing_to_a
        path_b_full = path_b_findings + missing_to_b
        # v1.4-K17: dedup Path B by WRITE TARGET *before* the quota. A type/family
        # write (Fire Rating type, family rename) collapses N instances to ONE
        # write — but the in-loop dedup ran AFTER _apply_quota, so 61 instances of
        # one family ate 61 budget slots and starved the other families. Dedup
        # first → max_issues counts the ~6 unique writes, not the 107 findings.
        path_b_full = self._dedup_by_write_target(path_b_full, element_by_id)
        path_a_targets, path_b_targets = self._apply_quota(
            path_a_full, path_b_full
        )

        log.info(
            "design_agent.start",
            total_findings=len(state["findings"]),
            missing_data_items=len(missing_items),
            missing_to_path_b=len(missing_to_b),
            missing_to_path_a=len(missing_to_a),
            candidates_after_filter=len(path_a_full) + len(path_b_full),
            path_a_full=len(path_a_full),
            path_b_full=len(path_b_full),
            path_a_selected=len(path_a_targets),
            path_b_selected=len(path_b_targets),
            rule_filter=self._rule_filter,
            dry_run_only=self._dry_run_only,
            path_b_available=self._revit_mcp is not None,
        )
        # v1.4-K7/K18: geometry findings fold into Path A on iteration 0 (see the
        # geo_findings block below). They live in their own state bucket and never
        # produce param path_a/path_b targets, so a geometry-only ruleset would
        # otherwise early-return here before the fold ever runs — leaving real
        # clearance clashes with ZERO ACC issues. Keep them in the guard.
        geo_present = state.get("iteration", 0) == 0 and bool(
            state.get("geometry_findings")
        )
        if not (path_a_targets or path_b_targets or geo_present):
            return {**state, "status": "converged"}

        log.info(
            "design_agent.partition",
            path_a=len(path_a_targets),
            path_b=len(path_b_targets),
        )

        fixes: list[ProposedFix] = list(state.get("proposed_fixes", []))

        # Path B (Revit auto-fix). v1.4-K4: prepare each fix (dry-run preview +
        # autonomy gate), then commit ALL auto writes in ONE revit_batch
        # transaction so N parameter writes are a single user-undo entry.
        b_fix_by_id: dict[str, ProposedFix] = {}
        commit_specs: list[dict[str, Any]] = []
        proposal_fixes: list[ProposedFix] = []
        # v1.4-K11: Path-B findings that turned out to have NO computable fix
        # value → re-routed to Path A (ACC issue) instead of a dead parked write.
        path_b_unfixable: list[Finding] = []
        # M12: fixes gated "human-only" (llm_safety_critical) — a valid
        # suggested value exists but must NEVER be auto-applied or offered as
        # an approve-and-apply proposal. Before this fix they fell through
        # BOTH the commit_specs branch (decision != auto) AND the proposal_fixes
        # branch (that elif only checks autonomy == "approve") — landing in
        # `fixes` as an orphan preview, invisible to ACC and permanently
        # executed=False. Collected here → folded into a Path A group below so
        # a human sees the suggested value and a note that it needs a manual
        # Revit write (never routed through the ApprovalWatcher).
        human_only_findings: list[Finding] = []
        seen_targets: set[tuple[str, int, str]] = set()
        # GĐ3-B2: pre-warm the LLM-proposal memo for large format/naming rule-groups
        # with ONE batched call each (each value still validated per element below),
        # so the per-finding loop hits the memo instead of firing N serial calls.
        await self._prewarm_llm_batches(path_b_targets, element_by_id)
        for finding in path_b_targets:
            element = element_by_id.get(finding["element_id"], {})
            rule = self._rules_by_id[finding["rule_id"]]
            write_eid = self._resolve_write_eid(finding, element, rule)
            target_param = rule.remediation.target_parameter or _fetch_name(rule)
            # v1.4-K5: dedup type-level writes — many instances share one type,
            # so a type-targeted fix is proposed/written ONCE per (type, param).
            # v1.4-K22.1: key by RULE too — two different rules writing the same
            # (type, param) must each keep their fix (→ one proposal issue each),
            # not collapse cross-rule into whichever rule ran first.
            if write_eid is not None:
                key = (finding["rule_id"], write_eid, target_param)
                if key in seen_targets:
                    continue
                seen_targets.add(key)
            fix, spec = await self._prepare_revit_fix(
                finding, element, rule, write_eid=write_eid
            )
            if fix is None:
                # No computable value → not an auto-fix; raise it as an issue.
                path_b_unfixable.append(finding)
                continue
            fixes.append(fix)
            b_fix_by_id[fix["finding_id"]] = fix
            if spec is not None:
                commit_specs.append(spec)
            elif (
                fix["autonomy"] == "approve"
                and fix.get("new_value") is not None
                and not fix["executed"]
            ):
                # Computable value but gated → propose for human approval.
                proposal_fixes.append(fix)
            elif (
                fix["autonomy"] == "human-only"
                and fix.get("new_value") is not None
            ):
                # M12: a life-safety value the LLM proposed — never auto, never
                # approve-gated. Stamp the suggested value onto the finding so
                # the Path A group below can show it, then route there instead
                # of silently vanishing (the pre-fix behaviour).
                finding = {**finding, "suggested_value": fix["new_value"]}
                human_only_findings.append(finding)
        if commit_specs:
            await self._commit_revit_batch(commit_specs, b_fix_by_id)
        # v1.5-R7 (R1-Stage 1): record every successfully EXECUTED Path B write
        # for cross-iteration interaction detection
        # (report_trace.detect_fix_interactions). ACCUMULATES across
        # iterations — unlike `check_trace`, never replaced; read the prior
        # log from state and append (state.py:fix_write_log).
        fix_write_log: list[dict[str, Any]] = list(state.get("fix_write_log") or [])
        for spec in commit_specs:
            committed_fix = b_fix_by_id.get(spec["finding_id"])
            if committed_fix is None or not committed_fix.get("executed"):
                continue
            committed_preview = committed_fix.get("preview") or {}
            fix_write_log.append({
                "iteration": state.get("iteration", 0),
                "rule_id": committed_preview.get("rule_id"),
                "write_eid": committed_preview.get("write_eid"),
                "parameter": committed_fix.get("parameter"),
                "old": committed_preview.get("old_value_raw", committed_preview.get("old_value")),
                "new": committed_fix.get("new_value"),
            })
        # v1.4-K22: ONE approve-gated proposal issue PER RULE — each lists ALL of
        # that rule's elements (never sliced). Was K5/K18's single combined issue
        # for every rule, which mixed unrelated problems. Mirrors the Path A
        # per-rule grouping below; the issue budget counts proposal issues +
        # Path A groups together, with auto-fix proposals reserving budget first.
        budget = self._max_issues  # 0 = unlimited
        proposal_groups: dict[str, list[ProposedFix]] = {}
        for fix in proposal_fixes:
            rid = (fix.get("preview") or {}).get("rule_id") or "(unknown rule)"
            proposal_groups.setdefault(rid, []).append(fix)
        prop_items = list(proposal_groups.items())
        if budget:
            prop_kept, prop_dropped = prop_items[:budget], prop_items[budget:]
        else:
            prop_kept, prop_dropped = prop_items, []
        for _rid, _group in prop_kept:
            await self._create_proposal_issue(_group)
        if prop_dropped:
            log.warning(
                "design_agent.proposal_budget_dropped",
                dropped_rules=[rid for rid, _ in prop_dropped],
                note="max_issues reached before every proposal rule got an issue",
            )
        reserved = len(prop_kept)

        # Path A (Forma create_issue) — needs subtype resolved once. When
        # Forma is unavailable (e.g. Snowdon-only demo with no ACC project)
        # we park path-A findings without crashing the rest of the run.
        #
        # v1.4-K7/K18: geometry findings live in a shared state bucket; fold them
        # into Path A on iteration 0. Path A is then grouped by (rule, status) —
        # see below — so geometry findings join the ACC issue for their rule.
        geo_findings = (
            (state.get("geometry_findings") or [])
            if state.get("iteration", 0) == 0
            else []
        )
        # v1.4-K11: Path-B findings with no computable value fall through to
        # Path A (an ACC issue) so a real violation isn't silently dropped.
        # M12: human-only fixes are appended too, tagged so the grouping below
        # keys them by a DISTINCT synthetic bucket ("human_only") — never
        # merged into a rule's ordinary non_compliant/manual_review group,
        # which would blur "flag this" with "here's the value, write it by hand".
        path_a_all = (
            list(path_a_targets) + list(path_b_unfixable) + list(geo_findings)
            + list(human_only_findings)
        )
        human_only_ids = {id(f) for f in human_only_findings}
        if path_a_all:
            if self._mcp is None:
                log.warning(
                    "design_agent.path_a_unavailable",
                    parked=len(path_a_all),
                    note="No Forma MCP client — manual findings parked",
                )
                for finding in path_a_all:
                    fixes.append(_park(finding, reason="no Forma MCP client"))
            else:
                subtype_id = await self._resolve_subtype_id()
                # v1.4-K18: group manual findings by (rule, status) → ONE ACC
                # issue per problem (like the auto-fix proposal), listing all
                # affected elements — NOT one issue per element. "100 elements
                # violate rule X" becomes 1 issue, not 100.
                groups: dict[tuple[str, str], list[Finding]] = {}
                for finding in path_a_all:
                    bucket = (
                        "human_only"
                        if id(finding) in human_only_ids
                        else (finding.get("status") or "non_compliant")
                    )
                    key = (finding["rule_id"], bucket)
                    groups.setdefault(key, []).append(finding)
                # Issue budget: per-rule Path B proposals already reserved their
                # share above (v1.4-K22); Path A groups take the remainder.
                items = list(groups.items())
                if budget:
                    keep_n = max(0, budget - reserved)
                    kept, dropped = items[:keep_n], items[keep_n:]
                else:                                  # 0 = unlimited
                    kept, dropped = items, []
                log.info(
                    "design_agent.path_a_grouped",
                    groups=len(items), kept=len(kept), dropped=len(dropped),
                    geometry_findings=len(geo_findings),
                )
                for (rule_id, bucket), group in kept:
                    fix = await self._propose_rule_group(
                        rule_id, bucket, group, subtype_id, element_by_id
                    )
                    fixes.append(fix)
                for (_rule_id, _bucket), group in dropped:
                    for finding in group:
                        fixes.append(_park(finding, reason="issue budget reached"))

        executed = sum(1 for f in fixes if f["executed"])
        log.info("design_agent.done", proposed=len(fixes), executed=executed)
        return {
            **state,
            "proposed_fixes": fixes,
            "status": "converged",
            "fix_write_log": fix_write_log,
        }

    def _can_auto_fill_missing(self, f: Finding) -> bool:
        """True when a missing_data finding can be auto-filled → routes Path B.

        All must hold:
          * revit_mcp is wired (else we have no write channel)
          * the rule is known and rule.fixability == "auto"
          * the finding carries a concrete ``suggested_value`` (the value
            source). This is what actually distinguishes "we can compute the
            correct value" from "we can't" — e.g. a compose_template Mark
            resolves for a duct mapped to a space, but yields None for an
            unmapped duct, which then correctly routes to a Path A ACC Issue.

        When this is False the finding routes to Path A as an ACC Issue
        (v1.4-K3 Layer 1) rather than being dropped or written with garbage.
        """
        if self._revit_mcp is None or not self._rules_by_id:
            return False
        rule = self._rules_by_id.get(f["rule_id"])
        if rule is None or rule.fixability != "auto":
            return False
        return f.get("suggested_value") is not None

    def _eligible_missing_data(
        self, missing_items: list[Finding]
    ) -> list[Finding]:
        """Missing_data items the agent CAN auto-fill (→ Path B). Thin wrapper
        over ``_can_auto_fill_missing`` kept for callers/tests."""
        return [f for f in missing_items if self._can_auto_fill_missing(f)]

    def _partition(
        self, findings: list[Finding]
    ) -> tuple[list[Finding], list[Finding]]:
        """Split findings into (path_a, path_b) by rule.fixability.

        Path B requires both a known rule and an injected revit_mcp client.
        Unknown rules fall back to path A (Phase 1 default).
        """
        if self._revit_mcp is None or not self._rules_by_id:
            return list(findings), []
        a: list[Finding] = []
        b: list[Finding] = []
        for f in findings:
            rule = self._rules_by_id.get(f["rule_id"])
            if rule is not None and rule.fixability == "auto":
                b.append(f)
            else:
                a.append(f)
        return a, b

    def _apply_rule_filter(self, findings: list[Finding]) -> list[Finding]:
        """Apply ``--rule <id>`` filter without slicing.

        v1.4-F2 split from the old ``_select_findings`` so we can partition
        BEFORE applying ``max_issues`` and avoid Path B starvation. Slicing
        is now ``_apply_quota``'s job.

        ``--rule none`` / ``"all"`` / ``""`` disable filtering — useful for
        scenarios with multiple rule families like room_compliance.
        """
        if self._rule_filter and self._rule_filter.lower() not in ("none", "all"):
            findings = [f for f in findings if f["rule_id"] == self._rule_filter]
        return findings

    @staticmethod
    def _conflict_sort_key(value: Any) -> tuple[int, float, str]:
        """Sortable key used to pick the MAXIMUM among conflicting writes.

        Fire-rating / duration values sort by their magnitude in minutes
        ("90 MIN" < "2 HR"). An unparseable value sorts **BELOW every parseable
        one** (group ``-1``) so it can never win the reducer.

        C-01: this key used to put unparseable values in group ``1`` — ABOVE
        every parseable one. That was safe under the old ``min()`` reducer
        (``0 < 1``, so a real value always won) and became a hazard the instant
        the reducer flipped to ``max()``: garbage (a corrupted normalize output,
        a stale host value, a typo) would beat every valid candidate and be
        written to the Type. Ordering the sentinel BELOW preserves
        "unparseable never wins" under EITHER reducer, so a future flip cannot
        resurrect the bug. ``_collapse_to_one`` additionally refuses to
        magnitude-collapse a group in which nothing parses.
        """
        from bim_orchestrator.policies.fire_rating_units import parse_to_minutes

        mins = parse_to_minutes(value)
        if mins is not None:
            return (0, float(mins), "")
        return (-1, 0.0, "" if value is None else str(value))

    def _collapse_to_one(self, group: list[Finding], rule: Any = None) -> Finding:
        """Pick ONE finding to represent a write-target conflict group.

        Groups only ever form for **type-level / family writes** (instance writes
        have a unique ``write_eid`` per element, so they never collapse — instance
        params are unaffected).

        v1.4-K20 — conflict policy, SCOPED:
          * Identical candidate values → clean collapse (no note).
          * **Fire-rating type param** (every candidate parses to minutes) with
            DIFFERING values — the inherit-from-host case: write the **maximum**
            and stamp ``host_conflict`` so the proposal is honest. This is the
            ONLY case that gets magnitude-resolution.

            Why max, not min (owner decision 2026-07-25): this parameter carries
            the rating the CODE REQUIRES, not a certified product capability. One
            shared Type serving a 60-minute wall and a 120-minute wall has no
            single correct value — but writing the minimum leaves the 120-minute
            instances declaring LESS than their host demands, and a
            ``present_and_nonempty`` / ``canonical_format`` rule would then pass
            them forever. The maximum over-states the requirement for the
            60-minute instances, which is visible and safe; the minimum
            under-states it for the 120-minute ones, which is silent and not.
          * ANY OTHER type-param conflict → **first-win** (keep the first finding,
            matching the pre-K20 behaviour) — we don't second-guess non-fire-rating
            values.
        A true per-instance fix (writing each door's own host value) needs
        instance-level params and is deferred to Phase 2.

        Low3: "every candidate parses to minutes" used to mean literally
        ``parse_to_minutes(...) is not None`` — and back then a BARE number
        (e.g. a Type Mark of "2100") parsed as minutes, so a non-fire-rating
        numeric type conflict was wrongly magnitude-collapsed and stamped
        ``host_conflict`` as if it were a fire-rating case. A candidate only
        counts as fire-rating when it carries an explicit unit token /
        not-rated sentinel (``_looks_like_fire_rating``) OR the rule/autofill
        is explicitly declared fire-rating (``compare_kind`` /
        ``normalize_kind`` == ``"fire_rating"``) — a bare numeric conflict
        falls through to first-win (pre-K20 behaviour). B-4 (2026-08-16) also
        made bare numbers unparseable at the source; both belts are kept
        deliberately (C-01: a future parser change must not re-open this).
        """
        if len(group) == 1:
            return group[0]
        distinct = sorted({str(f.get("suggested_value")) for f in group})
        if len(distinct) <= 1:
            return group[0]  # identical values → clean collapse, no conflict

        declared_fire_rating = bool(rule is not None and (
            getattr(rule, "compare_kind", None) == "fire_rating"
            or getattr(getattr(rule, "autofill", None), "normalize_kind", None) == "fire_rating"
        ))
        all_fire_rating = declared_fire_rating or all(
            _looks_like_fire_rating(f.get("suggested_value")) for f in group
        )
        if not all_fire_rating:
            return group[0]  # non-fire-rating type conflict → first-win (pre-K20)

        # C-01, second belt: only candidates that actually PARSE as a duration
        # may enter the magnitude reducer. A value carrying no rating cannot be
        # "the largest required rating", so excluding it is semantics, not a
        # patch — and it closes the `declared_fire_rating` bypass, where the
        # per-candidate `_looks_like_fire_rating` screen above is skipped
        # entirely and any string reaches this line. The full candidate list
        # (garbage included) is still reported in `host_conflict`, so the human
        # reviewing the proposal sees exactly what was in play.
        from bim_orchestrator.policies.fire_rating_units import parse_to_minutes

        rateable = [f for f in group if parse_to_minutes(f.get("suggested_value")) is not None]
        if not rateable:
            # Nothing to compare magnitudes on → don't invent an ordering.
            return group[0]
        chosen = max(rateable, key=lambda f: self._conflict_sort_key(f.get("suggested_value")))
        return {  # type: ignore[return-value]
            **chosen,
            "host_conflict": {
                "count": len(group),
                "chosen": chosen.get("suggested_value"),
                "candidates": distinct,
            },
        }

    def _dedup_by_write_target(
        self, path_b: list[Finding], element_by_id: dict[Any, dict[str, Any]]
    ) -> list[Finding]:
        """Collapse Path B findings that resolve to the SAME write (v1.4-K17).

        Key = (rule_id, write_eid, target_param). A type-level param or a family
        rename has many instances mapping to one write; collapsing to ONE per key
        means the ``max_issues`` quota counts unique writes, not per-instance
        findings. ``_resolve_write_eid`` reads the already-fetched ``_type_id`` (no
        MCP call), so this is cheap. The in-loop dedup downstream stays as a net.

        v1.4-K20: instead of "first wins", each group is collapsed via
        ``_collapse_to_one`` (the MAXIMUM rateable candidate), so a host-rating conflict
        resolves deterministically rather than by element order.

        v1.4-K22.1: the key includes ``rule_id`` — dedup is WITHIN a rule (300
        instances of 3 types → 3 writes), never ACROSS rules. Two rules writing the
        same (type, param) each keep their fix so each gets its own proposal issue
        (was: cross-rule collapse made the 2nd rule's fixes vanish into the 1st).
        """
        groups: dict[tuple[str, int, str], list[Finding]] = {}
        out: list[Finding] = []
        slot: dict[tuple[str, int, str], int] = {}
        for finding in path_b:
            rule = self._rules_by_id.get(finding["rule_id"])
            write_eid = (
                self._resolve_write_eid(finding, element_by_id.get(finding["element_id"], {}), rule)
                if rule is not None
                else None
            )
            if rule is None or write_eid is None:
                out.append(finding)  # ungrouped — passes through in place
                continue
            param = rule.remediation.target_parameter or _fetch_name(rule)
            key = (finding["rule_id"], write_eid, param)
            if key not in groups:
                groups[key] = []
                slot[key] = len(out)
                out.append(finding)  # reserve this slot; resolved below
            groups[key].append(finding)
        for key, members in groups.items():
            group_rule = self._rules_by_id.get(key[0])
            out[slot[key]] = self._collapse_to_one(members, group_rule)
        return out

    def _apply_quota(
        self,
        path_a: list[Finding],
        path_b: list[Finding],
    ) -> tuple[list[Finding], list[Finding]]:
        """Pass-through seam (v1.4-K18) — no per-finding slicing.

        ``max_issues`` now caps the number of ACC ISSUES, applied at the GROUP
        level in ``run()``: Path B → ONE uncapped proposal issue; Path A findings
        are grouped by (rule, status) into one issue each (like the proposal) and
        the NUMBER of those issue-groups is bounded by the remaining budget.
        Slicing findings here (the old behaviour) truncated grouped issues — e.g.
        a proposal listing 4 of 6 families. The v1.4-F2 partition-before-anything
        rule still holds (``_partition`` ran upstream); Path B is already deduped
        by ``_dedup_by_write_target``.
        """
        return path_a, path_b

    # Preference order for auto-picking when no override is provided.
    # Matches against (type_title, subtype_title) — first hit wins.
    _SUBTYPE_PREFERENCE: tuple[tuple[str, str | None], ...] = (
        ("Quality", "Quality"),
        ("Quality", None),
        ("General", None),
        ("Observation", None),
        ("Work List", None),
    )

    async def _resolve_subtype_id(self) -> str:
        if self._issue_subtype_id:
            return self._issue_subtype_id
        subtypes = await self._mcp.list_issue_subtypes(self._project_id)
        if not subtypes:
            raise RuntimeError(
                f"No issue subtypes configured for project {self._project_id}. "
                "Configure at least one in ACC Project Admin → Issues."
            )

        active = [s for s in subtypes if s.get("is_active", True)]
        if not active:
            inactive_count = len(subtypes)
            raise RuntimeError(
                f"All {inactive_count} issue subtype(s) in project {self._project_id} "
                "are inactive. Activate one in ACC Project Admin → Issues, or pass "
                "--issue-subtype-id with a known-active ID."
            )

        chosen = self._pick_preferred(active)
        log.info(
            "design_agent.subtype_discovered",
            subtype_id=chosen["id"],
            subtype_title=chosen["title"],
            type_title=chosen["type_title"],
            active_count=len(active),
            total_count=len(subtypes),
        )
        self._issue_subtype_id = chosen["id"]
        return chosen["id"]

    @classmethod
    def _pick_preferred(cls, active: list[dict[str, Any]]) -> dict[str, Any]:
        """Pick the most appropriate subtype from active candidates."""
        for pref_type, pref_subtype in cls._SUBTYPE_PREFERENCE:
            for s in active:
                if s.get("type_title") != pref_type:
                    continue
                if pref_subtype is not None and s.get("title") != pref_subtype:
                    continue
                return s
        return active[0]  # fall back to first active

    async def _propose_rule_group(
        self,
        rule_id: str,
        bucket: str,
        findings: list[Finding],
        subtype_id: str,
        element_by_id: dict[Any, dict[str, Any]],
    ) -> ProposedFix:
        """v1.4-K18: ONE ACC Issue for ALL elements failing <rule_id> with the
        same status (bucket) — like the auto-fix proposal but for manual review.

        Body states the rule + requirement ONCE, then lists each element
        (id | name | current value). No suggested value (these aren't
        auto-fixable). Same dry-run → token → execute trust pipeline; strictest
        severity across the group drives the autonomy gate.

        v1.5-R5: dedups by ``(rule_id, bucket, frozenset(element_ids))`` via
        ``self._proposed_group_keys`` (in-run only, see ``__init__``) so a
        route_node re-loop doesn't re-create the SAME ACC issue for a manual
        group that's still unresolved (the exact bug the Path B fingerprint
        memo fixed for approve-gated proposals — this is the Path A twin).
        A group whose element set changed (finding added/removed between
        iterations) gets a DIFFERENT key → a new issue is correctly created.
        """
        rule = self._rules_by_id.get(rule_id)
        element_ids = frozenset(f["element_id"] for f in findings)
        group_key = (rule_id, bucket, element_ids)
        top_sev = _strictest_severity(f["severity"] for f in findings)
        if group_key in self._proposed_group_keys:
            log.info(
                "design.issue_skipped_duplicate",
                rule_id=rule_id, bucket=bucket, elements=len(findings),
                note="same rule+bucket+element-set already proposed this run",
            )
            return ProposedFix(
                finding_id=f"rulegroup::{rule_id}::{bucket}",
                element_id=findings[0]["element_id"],
                parameter=", ".join(sorted({f["parameter"] for f in findings})),
                new_value=None,
                autonomy=self._autonomy.resolve("documents", "create_issue", top_sev),
                approval_token=None,
                preview={"grouped_rule_ids": [rule_id], "grouped_count": len(findings),
                         "skipped_duplicate": True},
                executed=False,
            )

        # Q3 (Mức 1 continuous audit): cross-run dedup — if a PREVIOUS run
        # already raised this exact group and the issue is still open on ACC,
        # skip re-raising it. get_issue failure → fail-open (create anyway;
        # rather a duplicate issue than a swallowed warning — same posture as
        # the in-run dedup's comment above).
        reg_key: str | None = None
        if self._issue_registry is not None:
            reg_key = _issue_registry_group_key(
                self._project_id, rule_id, bucket, element_ids
            )
            prev = self._issue_registry.lookup(reg_key)
            if prev is not None and prev.get("issue_id"):
                live_status: str | None = None
                try:
                    got = await self._mcp.get_issue(self._project_id, prev["issue_id"])
                    live_status = str((got.get("issue") or got).get("status") or "").lower()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "design.issue_registry_check_failed",
                        issue_id=prev["issue_id"], error=str(exc),
                    )
                if live_status is not None and live_status not in RESOLVED_ISSUE_STATUSES:
                    log.info(
                        "design.issue_skipped_cross_run",
                        rule_id=rule_id, bucket=bucket,
                        existing_issue=prev["issue_id"], status=live_status,
                    )
                    self._proposed_group_keys.add(group_key)
                    return ProposedFix(
                        finding_id=f"rulegroup::{rule_id}::{bucket}",
                        element_id=findings[0]["element_id"],
                        parameter=", ".join(sorted({f["parameter"] for f in findings})),
                        new_value=None,
                        autonomy=self._autonomy.resolve("documents", "create_issue", top_sev),
                        approval_token=None,
                        preview={
                            "grouped_rule_ids": [rule_id],
                            "grouped_count": len(findings),
                            # report join ignores stubs carrying this flag.
                            "skipped_duplicate": True,
                            "skipped_cross_run": True,
                            "existing_issue_id": prev["issue_id"],
                            "existing_display_id": prev.get("display_id"),
                        },
                        executed=False,
                    )

        title, description = _build_rule_group_payload(
            rule_id, bucket, findings, rule, element_by_id
        )
        description = _fit_acc_description(description)
        preview = await self._mcp.create_issue(
            project_id=self._project_id, title=title, description=description,
            issue_subtype_id=subtype_id, published=self._published, dry_run=True,
        )
        approval_token = preview.get("approval_token")
        decision = self._autonomy.resolve("documents", "create_issue", top_sev)
        log.info(
            "design_agent.rule_group_preview",
            rule=rule_id, bucket=bucket, elements=len(findings),
            severity=top_sev, decision=decision,
        )
        fix = ProposedFix(
            finding_id=f"rulegroup::{rule_id}::{bucket}",
            element_id=findings[0]["element_id"],
            parameter=", ".join(sorted({f["parameter"] for f in findings})),
            new_value=None,
            autonomy=decision,
            approval_token=approval_token,
            preview={**preview, "grouped_rule_ids": [rule_id],
                     "grouped_count": len(findings)},
            executed=False,
        )
        if self._dry_run_only or decision != "auto" or not approval_token:
            return fix
        result = await self._mcp.create_issue(
            project_id=self._project_id, title=title, description=description,
            issue_subtype_id=subtype_id, published=self._published,
            dry_run=False, approval_token=approval_token,
        )
        issue = result.get("issue") or {}
        log.info(
            "design_agent.rule_group_executed",
            issue_id=issue.get("id"), display_id=issue.get("displayId"),
            rule=rule_id, elements=len(findings),
        )
        fix["executed"] = True
        fix["preview"] = {**(fix["preview"] or {}), "executed_issue": issue}
        self._proposed_group_keys.add(group_key)
        if self._issue_registry is not None and reg_key is not None:
            self._issue_registry.record(reg_key, {
                "rule_id": rule_id, "bucket": bucket, "project_id": self._project_id,
                "issue_id": issue.get("id"), "display_id": issue.get("displayId"),
                "element_count": len(findings),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        return fix

    # ─── Path B: Revit parameter write-back ───────────────────────────────

    async def _ensure_family_map(self) -> None:
        """Fetch {family name → id} ONCE when any active rule renames a family.

        Family Name is a read-only `Element.Name`, so it can't be set via
        set_parameter — we rename the Family element, and need its id. The map is
        built from ``list_families`` (one call) and reused for the run.
        """
        if self._family_id_by_name or self._revit_mcp is None:
            return
        # v1.4-K19: also fetch when an "auto" rule may resolve to a family
        # rename (parameter == "Family Name") — the resolution is per element but
        # the family-name signal is knowable at the rule level.
        def _may_rename_family(r: Any) -> bool:
            rem = getattr(r, "remediation", None)
            tgt = getattr(rem, "target", None)
            if tgt == "family":
                return True
            return tgt == "auto" and self._targets_name_sentinel(r, "Family Name")

        needs = any(_may_rename_family(r) for r in self._rules_by_id.values())
        if not needs:
            return
        try:
            fams = await self._revit_mcp.list_families()
        except Exception as exc:                    # noqa: BLE001
            log.warning("design_agent.list_families_failed", error=str(exc))
            return
        for f in fams:
            name, fid = f.get("name"), f.get("id")
            if name is not None and fid is not None:
                self._family_id_by_name.setdefault(str(name), int(fid))
        log.info("design_agent.family_map", families=len(self._family_id_by_name))

    @staticmethod
    def _targets_name_sentinel(rule: Any, sentinel: str) -> bool:
        """Does ``rule`` check the rename pseudo-parameter ``sentinel``?

        Matched under EITHER the canonical intent label or the bound Revit
        name. The two differ whenever ``bound_parameter`` is set, and testing
        only one of them silently changes the resolved write target — which is
        why `_ensure_family_map` and `_effective_remediation` share this helper
        instead of each spelling the comparison out.
        """
        return sentinel in (getattr(rule, "parameter", None), _fetch_name(rule))

    def _effective_remediation(
        self, rule: Any, element: dict[str, Any]
    ) -> tuple[str, str]:
        """Resolve ``(action, target)`` for a Path-B fix, expanding ``"auto"``.

        v1.4-K19: ``remediation.target == "auto"`` lets the author skip the
        write-target decision. We derive it from the parameter under check:

          * ``"Family Name"`` → rename the FAMILY element.
          * ``"Type Name"``   → rename the family Type.
          * a param the TYPE carries (Fire Rating, Type Mark, …) → write the
            Type. The query layer mirrors a Type-side value under
            ``type.<param>`` (``revit_query._merge_params``); its presence is the
            signal that the value lives on the Type.
          * otherwise → write the instance.

        A non-``auto`` target is returned together with the authored action
        verbatim — an explicit override always wins.
        """
        rem = getattr(rule, "remediation", None)
        action = getattr(rem, "action", "set_parameter")
        target = getattr(rem, "target", "instance")
        if target != "auto":
            return action, target
        if self._targets_name_sentinel(rule, "Family Name"):
            return "rename_element", "family"
        if self._targets_name_sentinel(rule, "Type Name"):
            return "rename_element", "type"
        params = element.get("params") or {}
        # `params` is keyed by the name the QUERY fetched — i.e. the BOUND name
        # (query_specs/revit_query both resolve through `fetch_name`). Probing
        # `type.<canonical>` therefore always missed for a bound rule, so a
        # type-carried param resolved to an INSTANCE write, failed dry-run with
        # not_found, and the fix degraded to a Path A issue.
        if f"type.{_fetch_name(rule)}" in params:
            return "set_parameter", "type"
        return "set_parameter", "instance"

    def _resolve_write_eid(
        self, finding: Finding, element: dict[str, Any], rule: Any
    ) -> int | None:
        """Resolve the element id to WRITE. ``remediation.target == "type"``
        redirects to the element's family type (via the ``_type_id``
        breadcrumb) — needed for type-level params like Fire Rating. ``"auto"``
        is expanded first via ``_effective_remediation`` (v1.4-K19)."""
        eid_str = finding["element_id"]
        _, target = self._effective_remediation(rule, element)
        if target == "type":
            tid = (element.get("params") or {}).get("_type_id")
            if tid is None:
                # C2: a type-level write with no resolved type id must NOT silently
                # fall back to the flagged INSTANCE id — that writes the value to the
                # wrong element. Return None → the finding routes to Path A instead.
                return None
            eid_str = str(tid)
        elif target == "family":
            # v1.4-K17: rename the FAMILY (Family Name) — resolve its element id
            # from the current family-name value via the fetched families map.
            fam_name = (element.get("params") or {}).get(_fetch_name(rule))
            fid = self._family_id_by_name.get(str(fam_name)) if fam_name is not None else None
            if fid is None:
                return None   # unknown family → can't rename → routes to Path A
            eid_str = str(fid)
        try:
            return int(eid_str)
        except (TypeError, ValueError):
            return None

    async def _prepare_revit_fix(
        self,
        finding: Finding,
        element: dict[str, Any],
        rule: Any,
        *,
        write_eid: int | None = None,
    ) -> tuple[ProposedFix, dict[str, Any] | None]:
        """Dry-run preview → autonomy gate → build the fix (executed=False) and,
        when the decision is auto, a commit spec for batched execution.

        Returns ``(fix, spec)``: ``(fix, spec)`` when the fix previewed OK
        (spec set only for auto commits); ``(fix, None)`` for a previewed but
        gated/dry-run-only fix (→ approve-gated proposal). Returns the
        ``(None, None)`` sentinel — "route to Path A" — when there is no
        computable value, an unresolved write target, a bad id, or a preview
        failure (C2: these must NOT become fake approve-gated proposals).

        For ``new_value_strategy = inferred`` we trust the QCAgent's
        ``suggested_value`` (driven by ``autofill``). For ``fixed`` we use
        ``rule.remediation.new_value``.
        """
        assert self._revit_mcp is not None  # gated by _partition
        eid_str = finding["element_id"]
        eid = write_eid
        if eid is None:
            # C2: only an INSTANCE target legitimately writes the flagged element
            # id itself. A type/family target that reached here with no resolved
            # write_eid could not be resolved → route to Path A, never fall back
            # to writing the instance (an un-previewed write to the wrong target).
            _, _resolved_target = self._effective_remediation(rule, element)
            if _resolved_target != "instance":
                log.warning(
                    "design_agent.revit.unresolved_write_target",
                    rule=rule.id, element_id=eid_str, target=_resolved_target,
                )
                return None, None
            try:
                eid = int(eid_str)
            except (TypeError, ValueError):
                log.warning("design_agent.revit.bad_element_id", element_id=eid_str)
                return None, None

        target_param = rule.remediation.target_parameter or _fetch_name(rule)
        # v1.4-K19: resolve action (set_parameter vs rename_element) AND target
        # (instance/type/family) — honours a target=="auto" rule that turns out
        # to be a Family/Type rename or a Type-level parameter write.
        action, resolved_target = self._effective_remediation(rule, element)
        effective_param = "Name" if action == "rename_element" else target_param
        new_value = await self._compute_new_value(finding, element, rule)
        if new_value is None:
            # v1.4-K11: no computable fix value (e.g. normalize produced nothing
            # that satisfies the rule's pattern). This is a real violation we
            # can't auto-fix → signal the caller to re-route it to Path A (an
            # ACC issue for a human), NOT a dead parked Path-B write. Returning
            # (None, None) is the "route to Path A" sentinel.
            log.info(
                "design_agent.revit.no_value_route_path_a",
                rule=rule.id,
                element_id=eid_str,
                strategy=rule.remediation.new_value_strategy,
            )
            return None, None

        # Step 1: dry-run preview
        try:
            if action == "rename_element":
                preview = await self._revit_mcp.rename_element(eid, new_value, dry_run=True)
            else:
                preview = await self._revit_mcp.set_parameter(
                    eid, target_param, new_value, dry_run=True
                )
        except RevitEnvelopeError as exc:
            log.warning(
                "design_agent.revit.preview_failed",
                rule=rule.id, element_id=eid_str, code=exc.code,
            )
            # C2: a failed preview must NOT become an approve-gated proposal (that
            # would claim "previewed + ready to write" when it wasn't, with no
            # write_eid). Route the real violation to Path A (an ACC issue) instead.
            return None, None

        log.info(
            "design_agent.revit.preview",
            rule=rule.id,
            element_id=eid_str,
            parameter=effective_param,
            new_value=new_value,
            changes=(preview.get("data") or {}).get("changes"),
        )

        # Step 2: autonomy gate. v1.4-K4: a deterministic compose_template fill
        # is auto-applied regardless of severity — the value is computed, not a
        # human judgment. Heuristic/other strategies stay severity-gated.
        # Phase 2 governance invariant #2: a value an LLM ORIGINATED is NEVER
        # auto — human-only for life-safety params (llm_safety_critical), else
        # approve-gated. Only determinism earns auto. This check precedes the
        # compose_template branch so an llm_propose rule can't slip through.
        #
        # L2-14 (2026-07-26 Phase 2 review): this grant used to read
        # `autofill.strategy` alone — a field that does not decide where the
        # written value comes from. `_compute_new_value` dispatches on
        # `remediation.new_value_strategy`, and only "inferred" actually routes
        # through the autofill pipeline. So a rule with
        # `new_value_strategy: fixed` and an unrelated `autofill.strategy:
        # compose_template` had its LITERAL `remediation.new_value` written
        # unattended, overriding the operator's autonomy.yaml, on the strength
        # of a template that never ran. Rules are increasingly drafted by the
        # extraction agent from a PDF, so "a literal in the YAML" is not
        # automatically "a value a human chose".
        #
        # K4 Opt B's argument was that the VALUE is computed rather than
        # judged. That argument only holds when the value did in fact come from
        # a deterministic autofill — so the gate now requires both halves to
        # agree. Everything else falls to autonomy.yaml, which is where an
        # operator expects the decision to be made.
        strategy = getattr(getattr(rule, "autofill", None), "strategy", None)
        value_strategy = rule.remediation.new_value_strategy
        deterministic_fill = (
            value_strategy == "inferred" and strategy in _AUTO_AUTOFILL_STRATEGIES
        )
        if value_strategy == "llm_propose":
            decision = (
                "human-only"
                if getattr(rule.remediation, "llm_safety_critical", False)
                else "approve"
            )
        elif deterministic_fill:
            decision = "auto"
        else:
            decision = self._autonomy.resolve(
                "parameters", "set_value", finding["severity"]
            )

        # F-02 (catalog audit, 2026-08-01): a `next_available` renumber is NEVER
        # an unattended write. Three places already REASONED from that guarantee
        # without anything enforcing it: the public capability catalog ("always
        # as an approve-gated proposal, never a silent write"), the sibling-cache
        # note in `run()` above (which argues the cache may be cleared *because*
        # such a write is never committed), and `rule_builder_core.
        # enforce_unique_autofix`'s docstring ("still approve-gated ... safe to
        # enable"). None of it held: `next_available` is not in
        # `_AUTO_AUTOFILL_STRATEGIES`, so it fell through to `autonomy.yaml`,
        # where the shipped `severity_low: auto` wrote it straight to the model.
        # A renumber is deterministic but not harmless — it rewrites an
        # identifier that drawings, schedules and other documents refer to, and
        # which value it picks depends on the siblings that happened to exist at
        # that moment. Demote-only (never a promotion), so an operator policy of
        # `human-only` still wins.
        if value_strategy == "next_available" and decision == "auto":
            decision = "approve"

        # Scheduled/continuous audit (propose_only): nothing writes the model
        # unattended — a computed "auto" fix becomes an approve-gated proposal
        # (Loop 2 / ApprovalWatcher applies it after human review). Placed AFTER
        # every decision branch so the K4 Opt B deterministic bypass is covered.
        if self._propose_only and decision == "auto":
            decision = "approve"

        log.info(
            "design_agent.revit.autonomy",
            decision=decision,
            severity=finding["severity"],
            deterministic=deterministic_fill,
            autofill_strategy=strategy,
            value_strategy=value_strategy,
            llm_proposed=(value_strategy == "llm_propose"),
            propose_only=self._propose_only,
        )

        fix: ProposedFix = ProposedFix(
            finding_id=f"{rule.id}::{eid_str}",
            element_id=eid_str,
            parameter=effective_param,
            new_value=new_value,
            autonomy=decision,
            approval_token=None,  # Revit dry-run has no token concept
            preview=preview,
            executed=False,
        )
        # Stash the resolved WRITE target (may be a type id, ≠ the flagged
        # instance) so the proposal record + watcher write the right element.
        # Also stash the original value + rule_id so a proposal issue can show
        # "old → new" per element and state the rule once (v1.4-K5.1).
        params = element.get("params") or {}
        params_display = element.get("params_display") or {}
        _fname = _fetch_name(rule)
        old_value = params_display.get(_fname, params.get(_fname))
        # v1.4-K22: if this fix's value was INHERITED from the host (the element's
        # own value was empty under an inherit strategy), record the host's value
        # so the proposal can say "← kế thừa host: <value>" — a reviewer needs to
        # see WHERE a value came from before approving a write.
        inherited_from = None
        _af = getattr(rule, "autofill", None)
        if getattr(_af, "strategy", None) in ("inherit_from_host", "inherit_then_normalize"):
            if old_value is None or (isinstance(old_value, str) and not old_value.strip()):
                # host params are hydrated under `host.<fetched name>`
                # (query_specs._collect_params resolves through fetch_name).
                _hp = getattr(_af, "host_param", None) or _fname
                inherited_from = params.get(f"host.{_hp}")
        fix["preview"] = {
            **(fix.get("preview") or {}),
            "write_eid": eid,
            "old_value": old_value,
            # Low8: the RAW (non-display) value, alongside the display-preferred
            # ``old_value`` above — a sibling agent (ApprovalWatcher) needs the
            # raw value for its stale re-preview comparison against a live
            # Revit read, which also returns raw values, not display strings.
            "old_value_raw": params.get(_fname),
            "inherited_from": inherited_from,
            "rule_id": rule.id,
            # v1.4-K17: carry the remediation action so the ApprovalWatcher knows
            # to RENAME (Family/FamilySymbol Name property) vs set_parameter. The
            # approve-gated path used to always set_parameter("Name") → fails on a
            # rename (Name isn't a writable Parameter) → "Revit didn't change".
            "action": action,
            # v1.4-K19: the RESOLVED write target (instance/type/family), so the
            # proposal body shows a concrete target even when the rule said "auto".
            "target": resolved_target,
            # v1.4-K20: when several elements collapsed to this one write with
            # DIFFERING inherited values, carry the chosen-min audit so the
            # proposal issue is honest about the conflict.
            "host_conflict": finding.get("host_conflict"),
        }
        # Phase 2: mark the value's origin so the Approvals UI can badge an
        # LLM-proposed fix (vs a deterministic one). Absent for Phase-1 fixes.
        if rule.remediation.new_value_strategy == "llm_propose":
            fix["preview"]["value_source"] = "llm"
            # GĐ2 step 4: cite-the-clause evidence — the chosen classification
            # code's definition, so the human reviewer sees WHY it's right.
            if rule.requirement == "value_in_subset" and new_value is not None:
                cat = self._classification_catalog()
                definition = (
                    cat.define(element.get("category"), new_value) if cat else None
                )
                if definition:
                    system = getattr(cat, "system", "") or "classification"
                    fix["preview"]["evidence"] = f"{new_value} = {definition} ({system})"

        if self._dry_run_only or decision != "auto":
            reason = "dry_run_only" if self._dry_run_only else f"autonomy={decision}"
            log.info("design_agent.revit.skipped_execute", reason=reason)
            return fix, None

        comment: str | None = None
        template = rule.remediation.comments_template
        if template:
            comment = _render_comment_template(
                template, (preview.get("data") or {}).get("changes", {}), new_value, rule
            )
        spec = {
            "finding_id": fix["finding_id"],
            "eid": eid,
            "action": action,
            "target_param": target_param,
            "new_value": new_value,
            "comment": comment,
        }
        return fix, spec

    async def _commit_revit_batch(
        self, specs: list[dict[str, Any]], fix_by_id: dict[str, ProposedFix]
    ) -> None:
        """Commit all prepared auto Path B writes in ONE revit_batch transaction
        (a single undo entry). On failure the fixes stay executed=False."""
        steps: list[dict[str, Any]] = []
        primary_idx: dict[str, int] = {}   # finding_id → index of its PRIMARY step
        for s in specs:
            primary_idx[s["finding_id"]] = len(steps)  # recorded before the primary step
            if s["action"] == "rename_element":
                steps.append(
                    {"command": "rename_element", "params": {"id": s["eid"], "name": s["new_value"]}}
                )
            else:
                steps.append(
                    {
                        "command": "set_parameter",
                        "params": {
                            "id": s["eid"],
                            "parameterName": s["target_param"],
                            "value": s["new_value"],
                        },
                    }
                )
            if s.get("comment"):
                steps.append(
                    {
                        "command": "set_parameter",
                        "params": {"id": s["eid"], "parameterName": "Comments", "value": s["comment"]},
                    }
                )
        try:
            env = await self._revit_mcp.batch(steps, dry_run=False)
        except RevitEnvelopeError as exc:
            if exc.code == "unknown_command":
                # The addin's HTTP-direct endpoint doesn't expose batch (only
                # the stdio/MCP transport does). Fall back to per-element
                # commits — writes still land, just as N undo entries instead
                # of one. Safe: batch with stopOnError never partially applied.
                log.warning(
                    "design_agent.revit.batch_unsupported",
                    note="addin lacks HTTP batch; committing per-element (N undo entries)",
                )
                await self._commit_per_element(specs, fix_by_id)
                return
            log.error("design_agent.revit.batch_failed", code=exc.code, steps=len(steps))
            return
        # H2: read the per-step results and mark each fix executed ONLY when its
        # own step actually succeeded — do NOT blanket-mark on a truthy envelope.
        # A partial batch (stopOnError, one bad target) previously reported
        # "applied N writes" for writes that never landed, so the proposal record
        # and the ACC audit chain could claim a change that isn't in the model.
        # P1-TRUST-01: one shared validator across every live-write path, so
        # the watcher and this branch cannot drift on what "committed" means.
        # The old `results is None → ok=True` was the trust hole: an addin that
        # confirmed nothing got every write marked executed, merely labelled
        # "(unconfirmed)" in a field no report surfaces. Unconfirmed is now
        # NOT executed — the value is in the model or it is not.
        outcomes = batch_commit_outcomes(env, expected=len(steps))
        committed = 0
        for s in specs:
            fix = fix_by_id.get(s["finding_id"])
            if fix is None:
                continue
            idx = primary_idx.get(s["finding_id"])
            outcome = (
                outcomes[idx] if (idx is not None and idx < len(outcomes))
                else StepOutcome(False, "missing_step_result")
            )
            fix["executed"] = outcome.ok
            if outcome.ok:
                committed += 1
                fix["preview"] = {
                    **(fix.get("preview") or {}), "executed_via": "revit_batch",
                }
            else:
                fix["preview"] = {
                    **(fix.get("preview") or {}),
                    "executed_via": None,
                    "commit_failed": outcome.reason or "step not confirmed",
                }
                log.error(
                    "design_agent.revit.commit_step_failed",
                    finding_id=s["finding_id"], element_id=s["eid"],
                    reason=outcome.reason,
                )
        if committed < len(specs):
            log.warning(
                "design_agent.revit.batch_unconfirmed_writes",
                confirmed=committed, requested=len(specs),
                note="writes without a confirmed per-step result are NOT "
                     "recorded as executed",
            )
        log.info(
            "design_agent.revit.batch_executed",
            writes=len(specs), steps=len(steps), committed=committed,
        )

    async def _commit_per_element(
        self, specs: list[dict[str, Any]], fix_by_id: dict[str, ProposedFix]
    ) -> None:
        """Fallback commit: one set_parameter/rename per spec (N undo entries).
        Used when the addin transport doesn't support batch."""
        for s in specs:
            # L-06: the PRIMARY write and the Comments write are separate
            # transactions here, so they need separate error handling. Sharing
            # one `try` meant a failed Comments write (read-only on that type,
            # an older addin, anything) sent the loop to `continue` and skipped
            # `executed = True` — for a fix whose value was ALREADY in the
            # model. The record then under-claimed: the run reported a write it
            # had really made. That is not the harmless direction. Because
            # `_find_parked_duplicate` skips records with `applied` truthy, an
            # under-claimed fix also loses its dedup anchor and gets proposed
            # again. The batch path already keys the outcome to the primary
            # step alone (`primary_idx`); this brings the fallback in line.
            try:
                if s["action"] == "rename_element":
                    await self._revit_mcp.rename_element(s["eid"], s["new_value"], dry_run=False)
                else:
                    await self._revit_mcp.set_parameter(
                        s["eid"], s["target_param"], s["new_value"], dry_run=False
                    )
            except RevitEnvelopeError as exc:
                log.error(
                    "design_agent.revit.commit_failed", element_id=s["eid"], code=exc.code
                )
                continue

            fix = fix_by_id.get(s["finding_id"])
            if fix is not None:
                fix["executed"] = True
                fix["preview"] = {**(fix["preview"] or {}), "executed_via": "per_element"}

            if s.get("comment"):
                try:
                    await self._revit_mcp.set_parameter(
                        s["eid"], "Comments", s["comment"], dry_run=False
                    )
                except RevitEnvelopeError as exc:
                    # The audit note is provenance, not the fix. Say it went
                    # missing rather than disown a write that landed.
                    log.warning(
                        "design_agent.revit.comment_write_failed",
                        element_id=s["eid"], code=exc.code,
                        note="parameter write succeeded; the audit comment did not",
                    )
                    if fix is not None:
                        fix["preview"] = {
                            **(fix["preview"] or {}),
                            "comment_failed": exc.code,
                        }
        log.info("design_agent.revit.per_element_executed", writes=len(specs))

    async def _create_proposal_issue(self, proposal_fixes: list[ProposedFix]) -> None:
        """Gather approve-gated Path B fixes into ONE ACC proposal issue and
        write an ApprovalWatcher record. No-op without Forma/approvals_dir, or
        in dry-run-only mode (a real issue is a side effect).

        v1.5-R5: dedups by ``(rule_id, fingerprint(write-set))`` at two levels
        before ever calling ACC, so a rule whose approve-gated findings are
        unchanged across iterations/runs doesn't get a second proposal issue:
          1. In-run memo (``self._proposed_fingerprints``) — catches the same
             DesignAgent instance proposing the same rule twice across
             iterations (route_node re-looped because SOME OTHER rule's
             auto-fix changed the findings fingerprint).
          2. Cross-run scan of the approvals dir (``_find_parked_duplicate``)
             — catches two separate CLI invocations (e.g. ``--run-revit`` run
             twice before anyone approved) proposing the same write-set.
        Caller passes fixes for exactly ONE rule (``run()`` groups by
        ``rule_id`` before calling this), so all of ``proposal_fixes`` share
        one ``preview.rule_id``.
        """
        if self._mcp is None or self._approvals_dir is None:
            return
        if self._dry_run_only:
            log.info(
                "design_agent.proposal_skipped",
                reason="dry_run_only", count=len(proposal_fixes),
            )
            return
        rule_id = (
            (proposal_fixes[0].get("preview") or {}).get("rule_id")
            if proposal_fixes
            else None
        ) or "(unknown rule)"
        # Canonical write-set is built up-front (before any MCP call) so the
        # dedup checks below can run cheaply — the same list + fingerprint is
        # reused for the issue body / record once we know this isn't a repeat.
        record_fixes = _build_record_fixes(proposal_fixes)
        fp = fingerprint(record_fixes)
        memo_key = (rule_id, fp)
        if memo_key in self._proposed_fingerprints:
            issue_id = self._proposed_fingerprints[memo_key]
            # v1.5-R6 (join-hardening R5): stamp the SAME issue id on THIS
            # iteration's fixes too. Without this, these fresh ProposedFix
            # objects sit in `state["proposed_fixes"]` with no
            # proposal_issue_id — and since the join in report_trace.index_fixes
            # is last-write-wins, a later per-element lookup would find this
            # unstamped stub instead of iteration 0's real proposal, rendering
            # "—" instead of the actual awaiting-approval outcome.
            for f in proposal_fixes:
                f["preview"] = {
                    **(f.get("preview") or {}),
                    "proposal_issue_id": issue_id,
                    "proposal_origin": "reused_this_run",
                    "proposal_note": "already proposed this run (duplicate fingerprint)",
                }
            log.info(
                "design.proposal_skipped_duplicate",
                rule_id=rule_id, fingerprint=fp[:8], issue_id=issue_id,
                note="same rule+write-set already proposed this run",
            )
            return
        parked = self._find_parked_duplicate(fp)
        if parked is not None:
            parked_issue_id, parked_path = parked
            # M-05: same suppression either way — but say WHICH it is. A
            # proposal still open is waiting for someone; one closed on ACC
            # was decided against, and that decision is why this write-set
            # will never be proposed again.
            closed_status = await self._parked_issue_disposition(
                parked_issue_id, parked_path
            )
            declined = closed_status is not None
            note = (
                f"proposal issue was closed on ACC ({closed_status}) without "
                "being approved — fixes not applied, not re-proposed"
                if declined
                else "already parked (previous run) — awaiting approval"
            )
            for f in proposal_fixes:
                f["preview"] = {
                    **(f.get("preview") or {}),
                    "proposal_issue_id": parked_issue_id,
                    "proposal_origin": "declined" if declined else "reused_parked",
                    "proposal_note": note,
                    **({"proposal_declined": True} if declined else {}),
                }
            if declined:
                log.warning(
                    "design.proposal_declined_on_acc",
                    rule_id=rule_id, fingerprint=fp[:8],
                    issue_id=parked_issue_id, issue_status=closed_status,
                    fixes=len(proposal_fixes),
                    note="a human closed this proposal without approving it; "
                    "the findings remain and will keep being detected, but no "
                    "new proposal is raised for the same write-set",
                )
            else:
                log.info(
                    "design.proposal_already_parked",
                    rule_id=rule_id, fingerprint=fp[:8], issue_id=parked_issue_id,
                )
            self._proposed_fingerprints[memo_key] = parked_issue_id
            return

        # L-05: an issue can exist on ACC with no local record — the process
        # can die in the milliseconds between `create_issue` returning and the
        # record being written. That orphan is worse than a duplicate: a human
        # flips it to "In progress" and NOTHING happens, because the watcher
        # only ever reads local records. Before creating anything, look for
        # evidence of a previous attempt that was cut off, and adopt its issue
        # if ACC has one.
        adopted = await self._adopt_orphan_proposal(fp, record_fixes, rule_id)
        if adopted is not None:
            for f in proposal_fixes:
                f["preview"] = {
                    **(f.get("preview") or {}),
                    "proposal_issue_id": adopted,
                    "proposal_origin": "adopted_orphan",
                    "proposal_note": "adopted an issue left orphaned by an "
                    "interrupted run (no new issue raised)",
                }
            self._proposed_fingerprints[memo_key] = adopted
            return

        try:
            subtype_id = await self._resolve_subtype_id()
        except Exception as exc:
            log.warning("design_agent.proposal_subtype_failed", error=str(exc))
            return

        title = (
            f"[AutoAudit:approval] {len(proposal_fixes)} proposed Revit fixes "
            f"- set status 'In progress' to apply"
        )
        description = self._build_proposal_description(proposal_fixes)
        fp_block = f"\n\n---\n{fingerprint_line(fp)}\n"
        # Reserve room for the fingerprint block so it survives any trim — the
        # ApprovalWatcher's integrity gate reads that marker from the issue body.
        description = _fit_acc_description(description, reserve=len(fp_block))
        description = f"{description}{fp_block}"
        # The anchor. Written BEFORE the ACC call so that a crash during it
        # leaves proof the attempt happened, carrying everything the next run
        # needs to finish the job. Deliberately NOT `*.json`: the watcher
        # filters on `issue_id`, but the Approvals inbox and the service route
        # both glob `*.json` unfiltered and would show it as a broken row.
        pending_path = self._write_pending_anchor(fp, record_fixes, rule_id)
        try:
            preview = await self._mcp.create_issue(
                self._project_id, title=title, issue_subtype_id=subtype_id,
                description=description, published=self._published, dry_run=True,
            )
            created = await self._mcp.create_issue(
                self._project_id, title=title, issue_subtype_id=subtype_id,
                description=description, published=self._published,
                dry_run=False, approval_token=preview.get("approval_token"),
            )
        except Exception as exc:
            # KEEP the anchor. A raised create tells us the call did not
            # COMPLETE — not that it had no effect: the issue may well exist on
            # ACC with the response lost on the way back (that is precisely the
            # crash this whole path is about). Deleting the anchor here would
            # throw away the only evidence, so the next run reconciles instead:
            # it finds the issue and adopts it, or finds nothing and drops the
            # anchor itself. Unknown is not "no".
            log.error("design_agent.proposal_create_failed", error=str(exc))
            return
        issue = created.get("issue") or {}
        issue_id = issue.get("id")
        if not issue_id:
            # Same reasoning: a response we cannot read is not proof of no
            # issue. Leave the anchor for the next run to settle.
            log.error("design_agent.proposal_no_issue_id", resp=str(created)[:200])
            return
        self._proposed_fingerprints[memo_key] = issue_id

        for f in proposal_fixes:
            f["preview"] = {
                **(f.get("preview") or {}),
                "proposal_issue_id": issue_id,
                "proposal_origin": "created",
            }
        record = {
            "issue_id": issue_id,
            "display_id": issue.get("displayId"),
            "project_id": self._project_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "applied": False,
            # v1.4-K14 — explicit lifecycle for the Approvals inbox UI:
            #   pending_approval → (human sets ACC status In progress) → applied.
            # issue_status mirrors the ACC issue (open → closed on apply).
            "status": "pending_approval",
            "issue_status": "open",
            # Approval-security: fingerprint of the exact write-set, also embedded
            # in the issue body. The watcher recomputes it from `fixes` and
            # verifies against the issue before applying.
            "fingerprint": fp,
            "fixes": record_fixes,
        }
        # M-06: atomic — this record is the ONLY proof of what the watcher is
        # allowed to write, and a half-written one is indistinguishable from a
        # proposal that never happened.
        write_record(self._approvals_dir / f"{issue_id}.json", record)
        # The real record now exists, so the anchor has done its job. Ordering
        # matters: clearing it BEFORE the record is written would re-open the
        # very window this exists to cover.
        self._clear_pending_anchor(pending_path)
        log.info(
            "design_agent.proposal_issue_created",
            issue_id=issue_id, display_id=issue.get("displayId"),
            fixes=len(proposal_fixes),
        )

    # ---- orphaned-proposal recovery (L-05) ------------------------------

    def _write_pending_anchor(
        self, fp: str, record_fixes: list[dict[str, Any]], rule_id: str
    ) -> Path | None:
        """Record that we are ABOUT to create an issue for this write-set.

        Carries the full payload, so the next run can finish the job rather
        than merely notice something went wrong. Best-effort: if this cannot
        be written we still create the issue — the anchor is a recovery aid,
        not a gate, and refusing to propose because a scratch file failed
        would be the tail wagging the dog.
        """
        if self._approvals_dir is None:
            return None
        path = self._approvals_dir / f"{fp[:16]}.creating"
        try:
            write_record(path, {
                "status": "creating",
                "fingerprint": fp,
                "rule_id": rule_id,
                "project_id": self._project_id,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "fixes": record_fixes,
            })
            return path
        except OSError as exc:
            log.warning("design.proposal_anchor_write_failed", error=str(exc))
            return None

    def _clear_pending_anchor(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("design.proposal_anchor_clear_failed", error=str(exc))

    async def _adopt_orphan_proposal(
        self, fp: str, record_fixes: list[dict[str, Any]], rule_id: str
    ) -> str | None:
        """Recover an issue a previous run created but never recorded.

        Runs ONLY when an anchor from an interrupted attempt is on disk, so a
        healthy run pays nothing for it — no extra ACC call, no scan. When one
        IS found, ask ACC for the open issues and look for the fingerprint
        marker that every proposal body carries (it is reserved out of the
        1000-character trim precisely so it always survives). A match means
        the issue exists but has no record: write the record from the anchor's
        own payload and hand back the issue id, which both prevents a
        duplicate AND makes the orphaned issue work — a human flipping it to
        "In progress" now applies, where before nothing happened.

        Anything unclear fails OPEN (create a new issue, as today): ACC
        unreachable, marker not found in the window scanned. The anchor is
        left in place on an ACC error so a later run can still recover; a
        duplicate issue is noise, but a silently unusable one is a lie.
        """
        if self._approvals_dir is None or self._mcp is None:
            return None
        path = self._approvals_dir / f"{fp[:16]}.creating"
        if not path.exists():
            return None                      # the common case: nothing to do

        log.warning(
            "design.orphan_proposal_anchor_found",
            rule_id=rule_id, fingerprint=fp[:8],
            note="a previous run was interrupted between creating the ACC "
            "issue and recording it; checking whether that issue exists",
        )
        try:
            listing = await self._mcp.list_issues(self._project_id, limit=50)
        except Exception as exc:  # fail open, keep the anchor
            log.warning(
                "design.orphan_proposal_lookup_failed", error=str(exc),
                note="cannot tell whether the issue exists; creating a new "
                "one and keeping the anchor for a later attempt",
            )
            return None

        for issue in (listing or {}).get("issues") or []:
            if parse_fingerprint(issue.get("description")) != fp:
                continue
            issue_id = issue.get("id")
            if not issue_id:
                continue
            anchor = read_record(path) or {}
            write_record(self._approvals_dir / f"{issue_id}.json", {
                "issue_id": issue_id,
                "display_id": issue.get("displayId"),
                "project_id": self._project_id,
                "rule_id": anchor.get("rule_id", rule_id),
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "status": "pending_approval",
                "issue_status": issue.get("status", "open"),
                "fingerprint": fp,
                "fixes": anchor.get("fixes") or record_fixes,
                "adopted_from_orphan": True,
            })
            self._clear_pending_anchor(path)
            log.warning(
                "design.orphan_proposal_adopted",
                issue_id=issue_id, rule_id=rule_id, fingerprint=fp[:8],
                note="issue existed on ACC without a record; the record has "
                "been rebuilt, so approving it now applies as intended",
            )
            return str(issue_id)

        # No match in the window: the create never reached ACC. Drop the
        # anchor and let the caller create the issue normally.
        self._clear_pending_anchor(path)
        log.info(
            "design.orphan_proposal_not_found",
            rule_id=rule_id, fingerprint=fp[:8],
            note="no ACC issue carries this fingerprint — the interrupted "
            "attempt never created one; proposing fresh",
        )
        return None

    def _find_parked_duplicate(self, fp: str) -> tuple[str, Path] | None:
        """Cross-run proposal dedup (v1.5-R5): scan the approvals dir for an
        existing PENDING record whose write-set fingerprint matches ``fp``.

        Returns ``(issue_id, record_path)`` — the caller needs the path to
        stamp the record when ACC says that issue is already closed (M-05).

        Catches two separate CLI invocations (e.g. ``--run-revit`` run twice
        before anyone approved the first proposal) from parking a second,
        identical ACC issue for the same rule.

        Rules, by construction:
          * A record with ``applied`` truthy is NEVER a duplicate — findings
            reappearing after a real apply means the model drifted since, and
            a fresh proposal is the correct response (never suppress it).
          * A record with no ``fingerprint`` field (pre-approval-security,
            110a27f) is skipped — back-compat, no dedup against it.
          * Any I/O/parse error scanning the dir fails OPEN (returns None, so
            the caller creates the issue as normal) — a scan glitch must not
            silently drop a legitimate proposal.
        """
        if self._approvals_dir is None:
            return None
        try:
            if not self._approvals_dir.exists():
                return None
            for p in sorted(self._approvals_dir.glob("*.json")):
                rec = read_record(p)
                if rec is None:
                    continue
                if rec.get("applied"):
                    continue
                rec_fp = rec.get("fingerprint")
                if rec_fp and rec_fp == fp:
                    issue_id = rec.get("issue_id")
                    if issue_id:
                        return (str(issue_id), p)
        except OSError as exc:
            log.warning("design_agent.proposal_dedup_scan_failed", error=str(exc))
            return None
        return None

    async def _parked_issue_disposition(
        self, issue_id: str, path: Path
    ) -> str | None:
        """Is a parked proposal issue still awaiting a human, or already closed?

        M-05 (owner decision 2026-08-02): a proposal a human CLOSED on ACC
        without approving is a decision — do not re-propose it. But the
        suppression used to be total silence: the record never reaches
        ``applied``, so the fingerprint blocked that write-set for good while
        the run said nothing, and the surface a reviewer would look at (the
        issue) was shut. Policy is unchanged; what changes is that a closed
        proposal is now SAID — stamped on the record, logged, and carried on
        the fix preview so the run's own artifacts show it.

        Returns the live ACC status when it is a resolved one, else ``None``
        (still open, or the lookup failed — a lookup glitch must not be
        reported to the user as a human decision).
        """
        if self._mcp is None:
            return None
        # A simulated (--demo) record outlives the process that wrote it, but
        # the mock Forma client does not: run --demo twice and the second run
        # asks a brand-new mock about issue-mock-0001, gets `not found`, and
        # warns on the exact path the README tells people to walk. There is
        # nothing to learn from the lookup either way — a simulated proposal
        # is never closed by a human on ACC. Same reasoning, and the same
        # constant, as ApprovalWatcher.scan_once's demo skip (C-2).
        if self._project_id == DEMO_PROJECT_ID:
            log.debug("design.demo_parked_issue_check_skipped", issue_id=issue_id)
            return None
        try:
            got = await self._mcp.get_issue(self._project_id, issue_id)
            live = str((got.get("issue") or got).get("status") or "").lower()
        except Exception as exc:
            log.warning(
                "design.parked_issue_status_check_failed",
                issue_id=issue_id, error=str(exc),
                note="cannot tell whether this proposal was closed; "
                "leaving the record as-is and still not re-proposing",
            )
            return None
        if live not in RESOLVED_ISSUE_STATUSES:
            return None
        rec = read_record(path)
        if rec is not None and rec.get("status") != "declined":
            rec["status"] = "declined"
            rec["issue_status"] = live
            rec["declined_at"] = datetime.now(UTC).isoformat(
                timespec="seconds"
            )
            rec["declined_note"] = (
                "proposal issue was closed on ACC without being approved; "
                "these fixes were never applied and are not re-proposed"
            )
            try:
                write_record(path, rec)
            except OSError as exc:
                log.warning(
                    "design.parked_issue_stamp_failed",
                    issue_id=issue_id, error=str(exc),
                )
        return live

    def _build_proposal_description(self, proposal_fixes: list[ProposedFix]) -> str:
        """Render the approve-gated proposal issue body (v1.4-K5.1).

        Grouped BY RULE so the requirement + expected format are stated once
        (not repeated per element), then one ``old → new`` line per element /
        type. Makes the issue self-explanatory: a reviewer sees what the rule
        demands, which parameter changes, and the original vs proposed value
        before flipping the status to apply.
        """
        by_rule: dict[str, list[ProposedFix]] = {}
        for f in proposal_fixes:
            rid = (f.get("preview") or {}).get("rule_id") or "(unknown rule)"
            by_rule.setdefault(rid, []).append(f)

        sections: list[str] = []
        for rid, group in by_rule.items():
            rule = self._rules_by_id.get(rid)
            param = group[0]["parameter"]
            lines = [f"### Rule `{rid}` — parameter **{param}**"]
            if rule is not None:
                if rule.description:
                    lines.append(rule.description)
                lines.append(f"- **Requirement:** {_describe_requirement(rule)}")
                fmt = _describe_format(rule)
                if fmt:
                    lines.append(f"- **Expected format:** {fmt}")
                # v1.4-K19: prefer the per-fix RESOLVED target (an "auto" rule
                # has no concrete target at the rule level); fall back to the
                # authored one for older records.
                target = (group[0].get("preview") or {}).get("target") or getattr(
                    rule.remediation, "target", "instance"
                )
                action = (group[0].get("preview") or {}).get("action")
                descriptor = (
                    f"rename ({target})"
                    if action == "rename_element"
                    else f"{target}-level parameter"
                )
                lines.append(f"- **Write target:** {descriptor}")
                # L-01: a renumber picks a value by looking at what is already
                # taken. When that view is partial, the reviewer — who is the
                # only backstop, since these never auto-apply — has to be told.
                if rule.remediation.new_value_strategy == "next_available":
                    scope_note = self._sibling_scope_note(rule)
                    if scope_note:
                        lines.append(f"- **⚠ Scope:** {scope_note}")
            change_lines = [f"\n**Proposed changes ({len(group)}):**"]
            for f in group:
                old = (f.get("preview") or {}).get("old_value")
                inh = (f.get("preview") or {}).get("inherited_from")
                if inh not in (None, ""):
                    # v1.4-K22: empty value inherited from the host — say so + show
                    # the host's value, so the reviewer knows the source.
                    old_s = f"_(empty)_ ⤺ host `{inh}`"
                else:
                    old_s = "_(empty)_" if old in (None, "") else f"`{old}`"
                change_lines.append(
                    f"- {f['element_id']} | {old_s} → `{f['new_value']}`"
                )
                # v1.4-K20: be honest when a write collapsed conflicting inherited
                # values — state what was chosen (the maximum) and the full set.
                conflict = (f.get("preview") or {}).get("host_conflict")
                if conflict:
                    cands = ", ".join(f"`{c}`" for c in conflict.get("candidates", []))
                    change_lines.append(
                        f"  - ⚠️ {conflict.get('count')} hosts had differing values "
                        f"({cands}) — wrote the **maximum** `{conflict.get('chosen')}` "
                        "so no host is under-declared (temporary; per-instance "
                        "fix deferred)."
                    )
            # L2-05: say when a value in THIS group came from a language model.
            # The record has carried `value_source` since GĐ3, and every other
            # surface reads it — the report tags "(LLM-proposed)", Streamlit
            # shows "🤖 AI đề xuất" — but this body, the one an ACC reviewer
            # acts on to authorise a live Revit write, did not. Approve-gating
            # exists PRECISELY because a model produced the value; withholding
            # that from the approver removes the reason the gate is there.
            if any(
                (f.get("preview") or {}).get("value_source") == "llm"
                for f in group
            ):
                change_lines.append(
                    "  - 🤖 **Proposed by a language model**, then re-validated "
                    "against this rule. The rule is satisfied — but a model "
                    "inferred the INTENT, so check each value reads correctly "
                    "for its element before approving."
                )
            sections.append("\n".join(lines + change_lines))

        # L2-05: "computed" is an active overclaim once any value in the issue
        # came from a model — a claim of determinism on the exact document that
        # authorises the write. Say which kind of work produced these values.
        any_llm = any(
            (f.get("preview") or {}).get("value_source") == "llm"
            for f in proposal_fixes
        )
        verb = (
            "computed the corrected values (some proposed by a language model, "
            "marked 🤖 below)" if any_llm else "computed the corrected values"
        )
        return (
            f"AutoAudit detected the issues below and {verb}. Review them, then "
            "set this issue's status to **In progress** for the "
            "ApprovalWatcher to write them to Revit — or leave / close to skip."
            "\n\n" + "\n\n---\n\n".join(sections)
        )

    async def _compute_new_value(
        self, finding: Finding, element: dict[str, Any], rule: Any
    ) -> Any:
        strategy = rule.remediation.new_value_strategy
        if strategy == "fixed":
            return rule.remediation.new_value
        if strategy == "inferred":
            return finding["suggested_value"]
        if strategy == "llm_propose":
            return await self._llm_propose(finding, element, rule)
        if strategy == "next_available":
            target_param = rule.remediation.target_parameter or _fetch_name(rule)
            current = (element.get("params") or {}).get(target_param)
            if current is None:
                # No current value to suffix → fall back to the suggested value
                current = finding.get("suggested_value")
            if current is None:
                return None
            return await self._next_available(str(current), target_param, rule=rule)
        return None

    @staticmethod
    def _describe_constraint(
        rule: Any, *, other_value: Any = None, subset: list[Any] | None = None
    ) -> str | None:
        """Map the rule's requirement to a requirement-AWARE instruction.

        The closed-loop validator is the same across requirements, but the
        model needs to be told what "compliant" MEANS for THIS rule — and the
        sense is not uniform: ``matches_regex`` wants a value that matches the
        pattern, ``not_matches_regex`` wants one that does NOT (the pattern is
        the forbidden thing the current value wrongly contains), ``unique_in_set``
        wants a value unlike every sibling, ``relation_compare`` wants a value in
        a given relation to a REFERENCE value (``other_value``, e.g. the host
        wall's rating). Getting this wrong (e.g. telling the model to MATCH a
        forbidden pattern) is the main way a general remediation prompt goes off
        the rails — so it lives here, next to the schema, not baked into the
        agent. Returns None when no machine-readable constraint applies (the
        description carries it then).
        """
        req = getattr(rule, "requirement", None)
        pattern = getattr(rule, "pattern", None)
        threshold = getattr(rule, "threshold", None)
        operator = getattr(rule, "operator", None)
        if req in ("matches_regex", "matches_regex_if_present") and pattern:
            return (
                "The value MUST fully match this regular expression: "
                f"{pattern}"
            )
        if req == "not_matches_regex" and pattern:
            return (
                "The value MUST NOT match this regular expression (the current "
                f"value wrongly does, and that is the violation): {pattern}"
            )
        if req == "present_and_nonempty":
            return "The value must be present and non-empty (no blanks/placeholders)."
        if req == "value_in_subset":
            if subset:
                shown = ", ".join(repr(str(s)) for s in subset[:30])
                return (
                    "The value must be EXACTLY one of these allowed codes for this "
                    f"element's category (no others): {shown}"
                )
            return (
                "The value must be exactly one of the allowed classification codes "
                "for this element's category."
            )
        if req == "unique_in_set":
            return (
                f"The value must be UNIQUE for '{rule.parameter}' — it must not "
                "equal any value already used by another element (see the list "
                "of taken values, if given)."
            )
        if req == "fire_rating_ge":
            ref = f" {other_value!r}" if other_value is not None else " the related reference"
            return (
                f"The value must be a fire rating GREATER THAN OR EQUAL TO{ref} "
                "(durations compared in minutes; e.g. '2 HR' ≥ '90 MIN')."
            )
        if req == "relation_compare":
            kind = getattr(rule, "compare_kind", None) or "numeric"
            ref = f" {other_value!r}" if other_value is not None else " the related reference value"
            return (
                f"The value must be {operator or '>='}{ref} "
                f"(compared as {kind})."
            )
        if req == "numeric_min" and threshold is not None:
            return f"The value must be a number >= {threshold}."
        if req == "numeric_compare" and threshold is not None:
            return f"The value must be a number {operator or '>='} {threshold}."
        return None

    def _gather_compliant_examples(
        self, rule: Any, *, exclude_eid: str, limit: int = 5
    ) -> list[str]:
        """Mine the run's element population for values of this rule's parameter
        that ALREADY satisfy it — few-shot anchors so the model imitates the
        project's real convention instead of inventing one. Uses the same
        validator as the closed loop, so an "example" is compliant by the exact
        rule that flagged the finding. Distinct, capped, order-preserving."""
        validate = self._make_validator(rule)
        seen: set[str] = set()
        out: list[str] = []
        for el in self._all_elements:
            if str(el.get("id")) == str(exclude_eid):
                continue
            params = el.get("params") or {}
            params_display = el.get("params_display") or {}
            _fname = _fetch_name(rule)
            val = params_display.get(_fname, params.get(_fname))
            if val is None:
                continue
            sval = str(val)
            if sval in seen:
                continue
            try:
                if validate(sval):
                    seen.add(sval)
                    out.append(sval)
                    if len(out) >= limit:
                        break
            except Exception:
                continue
        return out

    def _classification_catalog(self) -> Any:
        """Lazy-load the ClassificationCatalog once (None if unavailable)."""
        if not self._classification_loaded:
            self._classification_loaded = True
            try:
                from bim_orchestrator.policies.classification import ClassificationCatalog
                self._classification = ClassificationCatalog.load()
            except Exception as exc:
                log.info("design_agent.classification_unavailable", error=str(exc))
                self._classification = None
        return self._classification

    def _classification_subset(self, element: dict[str, Any]) -> list[str]:
        """Valid classification codes for this element's category (empty if no
        catalog / unknown category)."""
        cat = self._classification_catalog()
        return cat.subset_for(element.get("category")) if cat else []

    def _classification_definitions(self, element: dict[str, Any]) -> dict[str, str]:
        """``{code: definition}`` for this element's category (for grounding +
        evidence). Empty when no catalog / category / definitions."""
        cat = self._classification_catalog()
        return cat.definitions_for(element.get("category")) if cat else {}

    def _gather_sibling_values(self, rule: Any, *, exclude_eid: str) -> list[Any]:
        """The values of this rule's parameter on every OTHER in-scope element —
        the duplicate-detection context for ``unique_in_set``. Mirrors QC's
        sibling build (same per-rule category filter) but EXCLUDES the element
        being remediated, since that one is about to change to the proposal."""
        out: list[Any] = []
        cat = getattr(rule, "category", None)
        for el in self._all_elements:
            if str(el.get("id")) == str(exclude_eid):
                continue
            if cat is not None and el.get("category") != cat:
                continue
            params = el.get("params") or {}
            # Bound rules read their siblings under the bound name too — else
            # every sibling comes back None and the unique_in_set validator
            # sees no duplicates at all, happily proposing a value already in
            # use elsewhere in the model.
            out.append(params.get(_fetch_name(rule)))
        return out

    def _make_validator(
        self,
        rule: Any,
        *,
        sibling_values: list[Any] | None = None,
        other_value: Any = None,
        condition_value: Any = None,
        subset: list[Any] | None = None,
    ) -> Callable[[str], bool]:
        """Build the closed-loop guardrail: a predicate that re-runs the SAME
        requirement that flagged the finding against a single proposed value.

        This is the symbolic half of the neuro-symbolic split — the LLM may
        propose any string, but only one that satisfies the detect-rule survives
        (`detect-rule == accept-rule`). Re-uses ``rules_engine.evaluate`` (the
        one source of truth), so the validator can never drift from QC's check.

        Relational requirements are honoured when the caller supplies their
        context (``_llm_propose`` does): ``unique_in_set`` rejects a proposal
        that collides with a sibling; ``relation_compare`` / ``fire_rating_ge``
        check against ``other_value`` (e.g. the host wall's rating);
        ``numeric_min_conditional`` against ``condition_value``. With no context
        passed (e.g. the example-mining call), evaluators that REQUIRE it raise
        and we fail closed (reject) — never a false accept.
        """

        def validate(proposed: str) -> bool:
            # L2-15: shape check BEFORE the requirement runs. The closed loop
            # is only as strong as what the requirement actually constrains,
            # and `present_and_nonempty` — the most natural rule a BIM manager
            # writes — constrains almost nothing: a dict, a list, an int or a
            # one-megabyte string are all "present and non-empty", so all of
            # them passed and flowed into `set_parameter`. The Validator's own
            # type says `Callable[[str], bool]`; nothing enforced it, and core
            # was relying on the plugin to send a string.
            #
            # This lives inside the validator rather than at the call sites so
            # it covers all three of them at once — the single proposal, the
            # batch, AND the plugin's own repair loop, which calls the
            # validator we inject. A guard the plugin can skip is not a guard.
            if not _proposal_shape_ok(proposed, rule):
                return False
            try:
                siblings: list[Any] | None = None
                if rule.requirement == "unique_in_set":
                    # Mirror QC semantics: the checked value counts once toward
                    # its own duplicate detection (`occurrences <= 1`). The
                    # proposal isn't in the set yet, so add it to the siblings.
                    siblings = [proposed, *(sibling_values or [])]
                return bool(
                    _evaluate_requirement(
                        rule.requirement,
                        proposed,
                        pattern=getattr(rule, "pattern", None),
                        threshold=getattr(rule, "threshold", None),
                        when_pattern=getattr(rule, "when_pattern", None),
                        condition_value=condition_value,
                        siblings=siblings,
                        other_value=other_value,
                        operator=getattr(rule, "operator", None),
                        compare_kind=getattr(rule, "compare_kind", None),
                        subset=subset,
                    )
                )
            except Exception as exc:  # evaluator can't judge this proposal → reject
                log.info(
                    "design_agent.llm_propose.validator_error",
                    rule=rule.id, error=str(exc),
                )
                return False

        return validate

    # Requirements whose validator is ELEMENT-INDEPENDENT (the rule's pattern /
    # allowed-set is the same for every element), so a batched proposal is safe:
    # each returned value is still re-validated per element. Relational kinds
    # (unique_in_set, fire_rating_ge, relation_compare) are NOT batchable — their
    # validity depends on per-element context (siblings / host value).
    _BATCHABLE_REQUIREMENTS = frozenset(
        {
            "matches_regex",
            "not_matches_regex",
            "matches_regex_if_present",
            "canonical_format",
        }
    )

    def _is_batchable(self, rule: Any) -> bool:
        req = getattr(rule, "requirement", None)
        if req in self._BATCHABLE_REQUIREMENTS:
            return True
        # value_in_subset is batchable only with an EXPLICIT rule-level allowed set
        # (uniform across elements); the per-category subset variant is not.
        if req == "value_in_subset" and getattr(rule, "allowed_values", None):
            return True
        return False

    async def _prewarm_llm_batches(
        self, path_b_targets: list[Finding], element_by_id: dict[Any, dict[str, Any]]
    ) -> None:
        """GĐ3-B2: for each large batchable rule-group, make ONE call proposing all
        values, validate each per element, and cache the survivors in the memo. The
        per-finding loop then hits the memo; anything the batch missed (or that
        failed validation) falls through to the per-element ``_llm_propose`` (with
        its repair loop). No correctness change — only fewer calls.
        """
        if self._llm_agent is None:
            return
        by_rule: dict[str, list[Finding]] = {}
        for finding in path_b_targets:
            rule = self._rules_by_id.get(finding["rule_id"])
            if (
                rule is None
                or rule.remediation.new_value_strategy != "llm_propose"
                or not self._is_batchable(rule)
            ):
                continue
            by_rule.setdefault(rule.id, []).append(finding)

        for rid, findings in by_rule.items():
            rule = self._rules_by_id[rid]
            subset = (
                list(rule.allowed_values)
                if getattr(rule, "allowed_values", None)
                else None
            )
            _fname = _fetch_name(rule)
            target_param = rule.remediation.target_parameter or _fname
            # Gather DISTINCT, still-uncached questions (memo already collapses
            # identical current values — A4 — so the batch only pays for the rest).
            items: list[tuple[Finding, tuple[Any, ...]]] = []
            seen_keys: set[tuple[Any, ...]] = set()
            payload: list[dict[str, Any]] = []
            for finding in findings:
                element = element_by_id.get(finding["element_id"], {})
                params = element.get("params") or {}
                params_display = element.get("params_display") or {}
                current = params_display.get(_fname, params.get(_fname))
                key = (rule.id, str(current), target_param, element.get("category"))
                if key in self._llm_propose_memo or key in seen_keys:
                    continue
                seen_keys.add(key)
                items.append((finding, key))
                payload.append(
                    {"element_id": finding["element_id"], "current_value": current}
                )

            if len(items) <= self._llm_batch_min:
                continue  # too small to be worth a batch — per-element handles it

            context: dict[str, Any] = {
                "rule_description": getattr(rule, "description", None),
                "parameter": target_param,
                "constraint": self._describe_constraint(rule, subset=subset),
            }
            if subset:
                context["allowed"] = subset
            proposals = await self._llm_agent.propose_batch(payload, context=context)

            validator = self._make_validator(rule, subset=subset)
            cached = 0
            unbound = 0
            for finding, key in items:
                entry = proposals.get(str(finding["element_id"]))
                # L2-09 (2026-07-26 Phase 2 review): `validator(val)` alone
                # cannot catch a PERMUTED batch. A rule only reaches this path
                # when its validator is element-INDEPENDENT (that is what makes
                # it batchable), so if the model answers element A with the
                # value it worked out for element B, both values are valid and
                # both are accepted — every element ends up compliant and
                # wrong. No attacker needed; swapping rows is ordinary model
                # error, and the human backstop is thin because the ACC body
                # trims long lists. So the answer must say which QUESTION it
                # answers: the plugin echoes the current value it was
                # correcting, and an echo that doesn't match THIS element's
                # current value is dropped rather than cached. Dropped means
                # per-element `_llm_propose` handles it — one question, one
                # answer, unpermutable — so the failure mode is cost, not a
                # wrong write.
                val, echo = _unpack_batch_entry(entry)
                if not val:
                    continue
                if not _echo_matches(echo, key[1]):
                    unbound += 1
                    continue
                if validator(val):  # per-element re-validation, same as _llm_propose
                    self._llm_propose_memo[key] = val
                    cached += 1
                # else: leave uncached → per-element _llm_propose repairs it
            log.info(
                "design_agent.llm_batch",
                rule=rid, requested=len(items), cached=cached, unbound=unbound,
            )
            if unbound:
                log.warning(
                    "design_agent.llm_batch.unbound",
                    rule=rid, dropped=unbound,
                    note="batch answers not bound to the element they were asked "
                    "about (missing or mismatched current-value echo) — falling "
                    "back to per-element proposals for those",
                )

    async def _llm_propose(
        self, finding: Finding, element: dict[str, Any], rule: Any
    ) -> Any:
        """Ask the injected remediation agent for a fix value (closed-loop).

        Returns the proposed value (already re-validated by the detect-rule) or
        ``None``. ``None`` is the established "route to Path A" sentinel
        (`_prepare_revit_fix` turns it into an ACC issue), so when no LLM agent
        is wired — the Phase 1 default — this strategy degrades to a human issue
        instead of fabricating a value. The autonomy gate separately guarantees
        an accepted value is never auto-applied.
        """
        if self._llm_agent is None:
            log.info(
                "design_agent.llm_propose.disabled",
                rule=rule.id, element_id=finding["element_id"],
            )
            return None

        _fname = _fetch_name(rule)
        target_param = rule.remediation.target_parameter or _fname
        params = element.get("params") or {}
        params_display = element.get("params_display") or {}
        current = params_display.get(_fname, params.get(_fname))

        # GĐ3-A4: memo hit — same (rule, current value, param, category) → same
        # question already answered this run. unique_in_set is excluded (every
        # element needs a DISTINCT value, so its answer can't be shared).
        # L2-01 (2026-07-26 Phase 2 review): the memo RETURN used to happen
        # right here — before the relational context below exists — so a cached
        # value went back to the caller without ever meeting THIS element's
        # validator. The key carries no `other_value` / `condition_value`, so
        # two doors with the same current rating but hosts of 1 HR and 4 HR
        # shared one answer and the 4 HR door received the 1 HR value. That is a
        # second entrance around the P2-01 guarantee ("every proposal is
        # re-validated by the rule that flagged it"): we closed the plugin
        # branch and left this one open. The lookup now happens after the
        # validator exists — see the re-validated hit below.
        cacheable = rule.requirement != "unique_in_set"
        memo_key = (rule.id, str(current), target_param, element.get("category"))

        # Relational context for the closed loop — the SAME inputs QC fed the
        # detector, so the validator verifies the SAME thing it flagged.
        eid = finding["element_id"]
        other_value = (
            params.get(rule.other_param) if getattr(rule, "other_param", None) else None
        )
        condition_value = (
            params.get(rule.when_param) if getattr(rule, "when_param", None) else None
        )
        sibling_values = (
            self._gather_sibling_values(rule, exclude_eid=eid)
            if rule.requirement == "unique_in_set"
            else None
        )
        subset = (
            (list(rule.allowed_values) if getattr(rule, "allowed_values", None)
             else self._classification_subset(element))
            if rule.requirement == "value_in_subset"
            else None
        )

        context: dict[str, Any] = {
            "parameter": target_param,
            "current_value": current,
            "rule_description": getattr(rule, "description", None),
            "constraint": self._describe_constraint(
                rule, other_value=other_value, subset=subset
            ),
        }
        if rule.requirement == "unique_in_set":
            # Tell the model which values are already taken so it can AVOID them
            # (uniqueness is achievable, not just rejected after the fact).
            context["avoid"] = sorted(
                {str(v) for v in (sibling_values or []) if v not in (None, "")}
            )
        elif rule.requirement == "value_in_subset":
            # Hand the model the exact allowed set so it picks FROM it (and the
            # deterministic validator rejects anything outside it). With
            # definitions, it can pick the semantically-right code (grounding).
            context["allowed"] = list(subset or [])
            defs = self._classification_definitions(element)
            if defs:
                context["allowed_defs"] = {c: defs[c] for c in subset if c in defs}
        else:
            context["examples"] = self._gather_compliant_examples(rule, exclude_eid=eid)

        payload = {
            "element_id": eid,
            "parameter": target_param,
            "current_value": current,
            "rule_id": rule.id,
            "message": finding.get("message", ""),
        }
        validator = self._make_validator(
            rule,
            sibling_values=sibling_values,
            other_value=other_value,
            condition_value=condition_value,
            subset=subset,
        )

        # L2-01: the memo hit, now re-validated against THIS element. The
        # validator is a pure predicate over `_evaluate_requirement`, so running
        # it on a cached value costs one symbolic evaluation and nothing else —
        # the same argument that justified the double-validation in P2-01.
        if cacheable and memo_key in self._llm_propose_memo:
            cached = self._llm_propose_memo[memo_key]
            if cached is None or validator(cached):
                log.info(
                    "design_agent.llm_propose.memo_hit",
                    rule=rule.id, element_id=finding["element_id"], value=cached,
                )
                return cached
            # The cached answer does not hold for this element — the two
            # questions only LOOKED alike. Poison the key so we neither reuse
            # nor re-cache it (otherwise every element after this one would
            # ping-pong the entry), and ask properly for this element.
            self._llm_propose_memo_unshareable.add(memo_key)
            self._llm_propose_memo.pop(memo_key, None)
            log.warning(
                "design_agent.llm_propose.memo_rejected",
                rule=rule.id, element_id=finding["element_id"], cached=cached,
                detail="cached value fails this element's validator — the memo "
                       "key does not capture the context that makes them differ",
            )
        cacheable = cacheable and memo_key not in self._llm_propose_memo_unshareable

        proposal = await self._llm_agent.propose(
            payload,
            validate=validator,
            context=context,
            safety_critical=bool(
                getattr(rule.remediation, "llm_safety_critical", False)
            ),
        )
        if proposal is None:
            log.info(
                "design_agent.llm_propose.no_value",
                rule=rule.id, element_id=finding["element_id"],
            )
            result = None
        elif not validator(proposal.proposed_value):
            # P2-01 (2026-07-25 live review): this branch used to accept
            # `proposed_value` verbatim. The guarantee "every proposal is
            # re-validated by the rule that flagged it" then held only because
            # the PLUGIN calls the validator we hand it — code in a separate,
            # separately-versioned private repo. That turns an invariant
            # ENFORCED by this file into one merely TRUSTED of another, and a
            # plugin refactor could regress it with nothing here to notice.
            # The batch branch above already re-checks core-side; this makes
            # the two paths agree. The validator is a pure predicate over
            # `_evaluate_requirement`, so running it twice has no side effects
            # — the cost is one extra symbolic evaluation, and what it buys is
            # that the guarantee no longer rests on a promise.
            log.warning(
                "design_agent.llm_propose.rejected_by_core",
                rule=rule.id, element_id=finding["element_id"],
                proposed=proposal.proposed_value,
                detail="plugin returned a value the rule's own validator rejects",
            )
            result = None
        else:
            log.info(
                "design_agent.llm_propose.accepted",
                rule=rule.id,
                element_id=finding["element_id"],
                proposed=proposal.proposed_value,
                autonomy=proposal.autonomy,
            )
            result = proposal.proposed_value
        # GĐ3-A4: memoise the answer (incl. None) so an identical question later
        # this run is a no-cost hit. Never cache unique_in_set (see above).
        if cacheable:
            self._llm_propose_memo[memo_key] = result
        return result

    async def _next_available(
        self, base: str, parameter: str, *, rule: Any = None
    ) -> str | None:
        """Generate a candidate value not in the live Revit sibling set.

        Strategy: append ``A``, ``B``, …, ``Z`` to ``base`` until we find
        an unused value. Returns None if the suffix space is exhausted
        (very unlikely for room numbers) or when no Revit client is wired.

        M4: the chosen candidate is reserved into the SHARED sibling cache
        (``_get_revit_siblings`` memoises by parameter for the DesignAgent's
        lifetime) before returning. Without this, two elements with the same
        duplicate base value (e.g. two rooms both "101") both see the same
        "existing" set and both propose "101A" — reserving here makes the
        second call see "101A" already taken and advance to "101B".
        """
        if self._revit_mcp is None:
            return None
        existing = await self._get_revit_siblings(parameter, rule=rule)
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            candidate = f"{base}{letter}"
            if candidate not in existing:
                existing.add(candidate)
                return candidate
        log.warning(
            "design_agent.next_available_exhausted",
            base=base, parameter=parameter, tried=26,
        )
        return None

    def _sibling_scope_note(self, rule: Any) -> str | None:
        """The honesty half of L-01: when the "already used" set this proposal
        was derived from is INCOMPLETE, say so on the proposal itself.

        Non-Rooms categories read their siblings from the run's own element
        population, which ``--max-elements`` caps (300 per category by
        default). A model with 800 doors gives a run that sees 300 of them, so
        a renumber can pick a value one of the unseen 500 already holds. The
        value is never written unattended (the autonomy gate demotes every
        ``next_available`` fix to approve), so the reviewer is the backstop —
        which only works if the reviewer is TOLD. Returns None when the set is
        complete, so a normal proposal carries no noise.
        """
        cap = self._max_elements
        if not cap or rule is None:
            return None
        cat = getattr(rule, "category", None)
        seen = [
            el for el in self._all_elements
            if cat is None or el.get("category") == cat
        ]
        if len(seen) < cap:
            return None            # under the cap → the run saw everything
        return (
            f"Candidate chosen from the {len(seen)} elements this run loaded "
            f"(--max-elements cap of {cap} reached, so the model may hold "
            "more). Confirm the value is unused across the rest of the model "
            "before approving."
        )

    async def _get_revit_siblings(
        self, parameter: str, *, rule: Any = None
    ) -> set[Any]:
        """The set of values ``parameter`` already holds on sibling elements.

        L-01: this used to walk ``list_rooms`` for EVERY rule, whatever the
        rule's category. A Mark rule on Doors therefore asked "which values do
        the ROOMS carry?" — near-always nothing — so the renumber concluded
        the first suffix was free and could hand back a value another door
        already had: a duplicate proposed as the cure for a duplicate.

        Source of truth now follows the rule's category:
          * Rooms (or no rule context) → ``list_rooms``, which is a
            whole-model query and stays the most complete answer available;
          * anything else → the run's own element population, already
            category-correct, read through ``fetch_name`` so a
            ``bound_parameter`` rule looks under the name the model actually
            uses (v1.5-R2 — read and write must not diverge).

        The population is capped by ``--max-elements``; that gap is disclosed
        on the proposal by ``_sibling_scope_note`` rather than silently
        accepted. Cached per (category, parameter) — keying on the parameter
        alone would let a Doors rule and a Ducts rule share one "Mark" set.
        """
        cat = getattr(rule, "category", None) if rule is not None else None
        cache_key = (cat, parameter)
        if cache_key in self._revit_siblings_cache:
            return self._revit_siblings_cache[cache_key]

        values: set[Any] = set()
        use_rooms = rule is None or cat is None or cat == "Rooms"
        if use_rooms:
            assert self._revit_mcp is not None  # gated by caller
            rooms = await self._revit_mcp.list_rooms()
            if parameter == "Number":
                for r in rooms:
                    num = r.get("number")
                    if num:
                        values.add(num)
            else:
                for r in rooms:
                    try:
                        info = await self._revit_mcp.get_element_info(int(r["id"]))
                    except (RevitEnvelopeError, ValueError):
                        continue
                    for p in info.get("parameters", []):
                        if p.get("name") == parameter and p.get("value"):
                            values.add(p["value"])
                            break
        else:
            name = _fetch_name(rule)
            for el in self._all_elements:
                if el.get("category") != cat:
                    continue
                val = (el.get("params") or {}).get(name)
                if val not in (None, ""):
                    values.add(val)

        self._revit_siblings_cache[cache_key] = values
        log.info(
            "design_agent.siblings_cached",
            parameter=parameter, category=cat, count=len(values),
            source="rooms" if use_rooms else "run_population",
        )
        return values


# L2-15: an upper bound on a proposed parameter value. Revit's own text
# parameters are far shorter than this, so the cap is not a modelling
# constraint — it is a refusal to carry an unbounded payload from a model into
# a write, an ACC issue body, and an approval record.
MAX_PROPOSED_VALUE_CHARS = 512


def _proposal_shape_ok(proposed: Any, rule: Any) -> bool:
    """Is this even the KIND of thing a parameter value can be?

    Deliberately blunt and requirement-independent: a proposal must be a string,
    non-empty once stripped, bounded in length, and free of control characters
    (a newline or NUL in a Revit parameter is a corrupt value, not a short one).
    Everything semantic stays where it belongs — in the rule's own requirement.
    """
    if not isinstance(proposed, str):
        log.warning(
            "design_agent.proposal_rejected",
            rule=getattr(rule, "id", None), reason="not_a_string",
            got=type(proposed).__name__,
        )
        return False
    if not proposed.strip():
        return False
    if len(proposed) > MAX_PROPOSED_VALUE_CHARS:
        log.warning(
            "design_agent.proposal_rejected",
            rule=getattr(rule, "id", None), reason="too_long",
            length=len(proposed), max=MAX_PROPOSED_VALUE_CHARS,
        )
        return False
    if any(ch in proposed for ch in ("\x00", "\n", "\r")):
        log.warning(
            "design_agent.proposal_rejected",
            rule=getattr(rule, "id", None), reason="control_characters",
        )
        return False
    return True


# v1.4-K4 Opt B: the autofill strategies whose output is EXACT — a computed
# value, not a judgment — and so may be written without a human. Named here
# rather than inlined because it is a governance list: adding a member widens
# what this product writes unattended, and that should be a visible edit.
# `normalize` is deliberately NOT a member: it is deterministic, but it has
# never been granted auto by this gate and widening the set is not part of
# fixing L2-14 (which is about the gate reading the WRONG FIELD).
_AUTO_AUTOFILL_STRATEGIES = frozenset({"compose_template"})


def _unpack_batch_entry(entry: Any) -> tuple[str | None, Any]:
    """Split a ``propose_batch`` entry into ``(value, echo)`` (L2-09).

    Contract v2 (``llm/conformance.py``) says an entry is a mapping carrying the
    proposed ``value`` plus ``for_current_value`` — the current value the model
    says it was correcting. A bare string is contract v1: still readable, but it
    arrives with NO evidence of which element it answers, so it is returned with
    ``echo=None`` and the caller drops it. That costs one per-element call and
    keeps a value that cannot be bound out of the write path.
    """
    if isinstance(entry, str):
        return (entry.strip() or None, None)
    if isinstance(entry, Mapping):
        raw = entry.get("value")
        val = str(raw).strip() if raw is not None else ""
        return (val or None, entry.get("for_current_value"))
    return (None, None)


def _echo_matches(echo: Any, asked: str) -> bool:
    """Does the model's echoed current value identify the element core asked about?

    Compared after stripping whitespace and one layer of quotes, because the
    prompt renders the value with ``repr`` and a model may echo it as it saw it.
    Deliberately NOT fuzzy beyond that: the point is to detect an answer written
    for a DIFFERENT element, and every loosening makes two different elements
    look like one.
    """
    if echo is None:
        return False
    return str(echo).strip().strip("'\"") == str(asked).strip().strip("'\"")


# ACC Issues service rejects a `description` over this many characters with a
# 400 "request validation failed" (errorCode ISSUES_SERVICE_BAD_REQUEST). The
# body only needs to be human-readable — the FULL element set is applied from
# the write-set/record, never from the body text — so we trim the text to fit
# and say so, rather than dropping any fix.
ACC_DESCRIPTION_MAX = 1000
_TRIM_FOOTER = (
    "\n\n_(showing {kept} of {total} lines — trimmed to fit ACC's {max}-character "
    "limit. The FULL element set is applied and recorded; see the verification "
    "report or the Approvals view to review every value before approving.)_"
)


def _fit_acc_description(body: str, *, reserve: int = 0) -> str:
    """Return ``body`` unchanged if it fits ACC's description cap, else trim it
    at a line boundary and append an honest footer.

    ``reserve`` holds back room for text appended AFTER this call (e.g. the
    approval-security fingerprint block), so the caller can concatenate it and
    still land at or under ``ACC_DESCRIPTION_MAX``.

    L2-09 (2026-07-26): the footer now states HOW MUCH is missing. It always
    said the list was trimmed, which is honest but not actionable — an approver
    reading 14 lines of a 120-line proposal cannot tell from "trimmed" whether
    they have seen most of it or a tenth of it, and this is the document on
    which they authorise the write.
    """
    budget = ACC_DESCRIPTION_MAX - reserve
    if len(body) <= budget:
        return body
    total = body.count("\n") + 1
    footer = _TRIM_FOOTER.format(kept=total, total=total, max=ACC_DESCRIPTION_MAX)
    keep = budget - len(footer)
    if keep <= 0:
        # Degenerate: reserve alone eats the budget. Return the footer trimmed
        # so the total still respects the cap (fingerprint stays primary).
        return footer[:budget]
    cut = body[:keep]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + _TRIM_FOOTER.format(
        kept=cut.count("\n") + 1, total=total, max=ACC_DESCRIPTION_MAX
    )


def _build_record_fixes(proposal_fixes: list[ProposedFix]) -> list[dict[str, Any]]:
    """Serialise proposal fixes to the ApprovalWatcher record shape.

    Factored out of ``_create_proposal_issue`` so the canonical write-set is
    available BEFORE the issue is created — its fingerprint is stamped into the
    issue body, and the identical list is persisted to the record so the
    watcher's re-derived fingerprint matches (approval-security).
    """
    return [
        {
            "finding_id": f["finding_id"],
            # write target (type id for type-level params, else instance)
            "element_id": str(
                (f.get("preview") or {}).get("write_eid") or f["element_id"]
            ),
            "flagged_instance": f["element_id"],
            "parameter": f["parameter"],
            # Current value at the checked param (display-preferred), so the
            # Approvals inbox can show "current → proposed" before a human
            # approves — AND so the watcher can re-preview against live drift.
            # Stashed on the preview by _prepare_revit_fix.
            "old_value": (f.get("preview") or {}).get("old_value"),
            # ...and the RAW storage baseline beside it. The watcher's stale
            # re-preview accepts EITHER baseline, but dropping this here made
            # `old_value_raw` dead code end to end: for any param whose display
            # form differs from storage (units!), the live raw read matched
            # neither baseline, so an UNCHANGED model looked stale and the
            # approved fix was held back forever. Fails closed — but it defeats
            # exactly the case the raw baseline was added for.
            "old_value_raw": (f.get("preview") or {}).get("old_value_raw"),
            "new_value": f["new_value"],
            # Phase 2: provenance for the Approvals UI badge (None = deterministic).
            "value_source": (f.get("preview") or {}).get("value_source"),
            # GĐ2 step 4: cite-the-clause evidence (None when not applicable).
            "evidence": (f.get("preview") or {}).get("evidence"),
            # v1.4-K22: the host value this was inherited from (None unless the
            # element was empty + an inherit strategy filled it) — shown in the
            # Approvals inbox so the reviewer sees the source.
            "inherited_from": (f.get("preview") or {}).get("inherited_from"),
            # v1.4-K17: rename_element vs set_parameter (default) so the watcher
            # dispatches the right Revit command.
            "action": (f.get("preview") or {}).get("action") or "set_parameter",
        }
        for f in proposal_fixes
    ]


def _describe_requirement(rule: Any) -> str:
    """Human-readable statement of what a rule demands (for proposal issues)."""
    req = rule.requirement
    if req in ("matches_regex", "matches_regex_if_present"):
        return f"value must match pattern `{rule.pattern}`"
    if req == "not_matches_regex":
        return f"value must NOT match pattern `{rule.pattern}`"
    if req == "present_and_nonempty":
        return "parameter must be present and non-empty"
    if req == "positive_number":
        return "value must be a positive number"
    if req in ("numeric_min", "numeric_min_conditional"):
        unit = f" {rule.unit}" if getattr(rule, "unit", None) else ""
        return f"value must be ≥ {rule.threshold}{unit}"
    if req == "unique_in_set":
        return "value must be unique across sibling elements"
    if req == "fire_rating_ge":
        # L-02: this compares against ANOTHER element's value, never a
        # threshold — `fire_rating_ge(value, other_value)` in rules_engine —
        # so `rule.threshold` is legitimately None on every such rule that
        # ships. It rendered "fire rating must be ≥ None" in the proposal body
        # a human reads before approving a write. Mirrors
        # `audit_report._requirement_sentence`, which already got this right.
        lookup = getattr(rule, "lookup", None)
        if lookup:
            return (
                "fire rating must be ≥ the value the code table "
                f"`{lookup}` requires for the related element"
            )
        other = getattr(rule, "other_param", None)
        target = f"`{other}`" if other else "the related element's fire rating"
        return f"fire rating must be ≥ {target}"
    return str(req)


def _describe_format(rule: Any) -> str | None:
    """Describe the expected output format from the rule's autofill strategy."""
    autofill = getattr(rule, "autofill", None)
    if autofill is None:
        return None
    if autofill.strategy == "compose_template" and autofill.template:
        return f"`{autofill.template}`"
    if autofill.strategy == "normalize" and autofill.normalize_kind:
        return f"canonical {autofill.normalize_kind.replace('_', ' ')} form"
    return None


def _park(finding: Finding, *, reason: str) -> ProposedFix:
    """Build a non-executed ProposedFix for a finding we couldn't action."""
    return ProposedFix(
        finding_id=f"{finding['rule_id']}::{finding['element_id']}",
        element_id=finding["element_id"],
        parameter=finding["parameter"],
        new_value=finding.get("suggested_value"),
        autonomy="approve",
        approval_token=None,
        preview={"reason": reason},
        executed=False,
    )


def _render_comment_template(
    template: str, primary_changes: dict[str, Any], new_value: Any, rule: Any
) -> str:
    return template.format(
        value=primary_changes.get("before"),
        old_value=primary_changes.get("before"),
        new_value=new_value if new_value is not None else primary_changes.get("after"),
        rule_id=rule.id,
    )


# v1 task B: ASCII-only payload format. Per HANDOFF Sec.12 gotcha, the
# RevitMCPAddin C# request reader treats incoming bodies as Windows-1252,
# so em-dashes / section-signs / unicode bullets get mojibaked on the way
# to a Revit Comments parameter write. We keep titles + descriptions
# strictly ASCII; this also makes structlog console output cleaner.


_ASCII_FOLD: dict[int, str] = {
    ord("—"): "--",   # em-dash
    ord("–"): "-",    # en-dash
    ord("§"): "Sec.", # section sign
    ord("≥"): ">=",   # >=
    ord("≤"): "<=",   # <=
    ord("≠"): "!=",   # !=
    ord("²"): "^2",   # superscript 2 (m²)
    ord("³"): "^3",   # superscript 3 (m³)
    ord("‘"): "'",    # left single quote
    ord("’"): "'",    # right single quote
    ord("“"): '"',    # left double quote
    ord("”"): '"',    # right double quote
    ord("…"): "...",  # ellipsis
    ord("•"): "*",    # bullet
    ord(" "): " ",    # non-breaking space
}


def _ascii_safe(s: str | None) -> str:
    """Fold the common typographic glyphs that creep in from PDFs / qc messages
    into ASCII equivalents, then drop anything still non-ASCII. Keeps the
    issue body readable without tripping the RevitMCPAddin Latin-1 reader.
    """
    if not s:
        return ""
    folded = s.translate(_ASCII_FOLD)
    return folded.encode("ascii", "ignore").decode("ascii")


def _humanize_severity(s: str) -> str:
    """severity_high -> HIGH (cleaner badge in the issue card)."""
    return s.replace("severity_", "").upper() if s.startswith("severity_") else s.upper()


def _describe_expected(rule: Any | None, finding: Finding) -> str:
    """Render a one-line 'expected' clause from the rule definition.

    Defensive about the rule arg shape -- this is called with an optional
    Rule pydantic object. When the rule isn't available (legacy paths),
    we fall back to the rule_id + parameter so the issue still reads cleanly.
    """
    if rule is None:
        return f"Rule `{finding['rule_id']}` satisfied on parameter `{finding['parameter']}`"
    req = getattr(rule, "requirement", "")
    threshold = getattr(rule, "threshold", None)
    pattern = getattr(rule, "pattern", None)
    when_param = getattr(rule, "when_param", None)
    when_pattern = getattr(rule, "when_pattern", None)
    if req == "numeric_min":
        return f"Value >= {threshold}"
    if req == "numeric_min_conditional":
        return (
            f"Value >= {threshold} when `{when_param}` matches /{when_pattern}/"
        )
    if req == "present_and_nonempty":
        return "Value present and non-empty"
    if req == "positive_number":
        return "Value > 0"
    if req == "matches_regex":
        return f"Value matches /{pattern}/"
    if req == "not_matches_regex":
        return f"Value does NOT match /{pattern}/"
    if req == "unique_in_set":
        return f"Value unique across all elements' `{finding['parameter']}`"
    # Unknown requirement -- fall back to a descriptive label
    return f"Rule `{finding['rule_id']}` requirement `{req}`"


def _build_issue_payload(
    finding: Finding,
    element: dict[str, Any],
    *,
    rule: Any | None = None,
    run_id: str | None = None,
    linked_documents: list[dict[str, Any]] | None = None,
) -> tuple[str, str, list[dict[str, Any]] | None]:
    """v1 task B: render an ACC Issue title + description from a Finding.

    Title format (HART convention): ``[AutoAudit] <Category> -- <rule_id>``
    Body sections: Finding / Reference (citation + Run ID) / Footer.

    Returns ``(title, description, linked_documents)``. linked_documents is
    forwarded verbatim to forma.create_issue when populated -- caller is
    responsible for constructing the schema (see acc-forma-mcp-server R1).

    All string output is ASCII per the RevitMCPAddin UTF-8 mojibake gotcha
    (HANDOFF Sec.12). Trust pipeline contract (dry-run -> approval_token ->
    execute -> audit chain) is unchanged; only the rendered content of the
    issue body differs from pre-B.
    """
    category = _ascii_safe(element.get("category")) or "Element"
    element_name = _ascii_safe(element.get("name")) or "(unnamed)"
    # L2 (audit): `finding["parameter"]` is the canonical report label; the
    # params dict is keyed by the FETCHED (bound) name, so a bound rule rendered
    # "Actual: (missing)" on a Path A issue even when the model carried a value.
    _pname = _fetch_name(rule) if rule is not None else finding["parameter"]
    actual_value = element.get("params", {}).get(_pname)
    actual_repr = repr(actual_value) if actual_value not in (None, "") else "(missing)"
    severity_h = _humanize_severity(finding["severity"])
    expected = _ascii_safe(_describe_expected(rule, finding))
    citation = _ascii_safe(finding.get("citation"))
    status = finding.get("status", "non_compliant")
    detail = _ascii_safe(finding.get("message", ""))
    suggested = finding.get("suggested_value")

    # Title -- short enough for the ACC Issues list row, ASCII-safe.
    title = _ascii_safe(f"[AutoAudit] {category} -- {finding['rule_id']}")

    # Body. Use `--` not em-dash, `Sec.` not section-sign, `>=` not unicode ge.
    parts: list[str] = []
    parts.append("## Finding")
    parts.append("")
    parts.append(f"**Rule:** `{finding['rule_id']}`")
    parts.append(f"**Element:** {element_name} (id: `{finding['element_id']}`)")
    parts.append(f"**Parameter:** `{finding['parameter']}`")
    parts.append(f"**Expected:** {expected}")
    parts.append(f"**Actual:** {_ascii_safe(actual_repr)}")
    parts.append(f"**Severity:** {severity_h}")
    parts.append(f"**Status:** {status}")
    if suggested not in (None, ""):
        parts.append(f"**Suggested value:** `{_ascii_safe(str(suggested))}`")
    parts.append("")

    parts.append("## Reference")
    parts.append("")
    if citation:
        parts.append(f"- Source: {citation}")
    if finding.get("citation_missing"):
        parts.append("- Source: (hard-mode rule, no citation found)")
    if run_id:
        parts.append(f"- Audit run: `{run_id}`")
    # v1 task B-3: surface Revit identifiers so a reviewer can navigate the
    # element in Revit / Forma Viewer manually even when linked_documents
    # isn't populated (current state -- ACC's native "View in Model" button
    # needs a full lineage URN + viewable GUID + dbId triple).
    from bim_orchestrator.mcp_clients.forma import (
        extract_revit_element_id,
        extract_revit_unique_id,
    )
    revit_unique = extract_revit_unique_id(element)
    revit_elem = extract_revit_element_id(element)
    if revit_unique:
        parts.append(f"- Revit UniqueId: `{_ascii_safe(revit_unique)}`")
    if revit_elem is not None:
        parts.append(f"- Revit ElementId: `{revit_elem}`")
    if detail:
        parts.append(f"- Detail: {detail}")
    parts.append("")

    parts.append("---")
    parts.append("_Auto-generated by bim-orchestrator. "
                 "Trust pipeline: dry-run preview -> approval token -> execute -> audit chain._")

    return title, "\n".join(parts), linked_documents


_SEVERITY_ORDER = {"severity_low": 0, "severity_medium": 1, "severity_high": 2}


def _strictest_severity(severities: "Any") -> str:
    """Return the highest-ranked severity in an iterable (v1.4-K7 grouping).

    A grouped issue inherits the strictest finding's severity so the autonomy
    gate never auto-creates an issue that contains a high-severity concern.
    Defaults to ``severity_medium`` for empty / unknown input.
    """
    best = -1
    best_sev = "severity_medium"
    for sev in severities:
        rank = _SEVERITY_ORDER.get(sev, 1)
        if rank > best:
            best, best_sev = rank, sev
    return best_sev


def _build_rule_group_payload(
    rule_id: str,
    bucket: str,
    findings: list[Finding],
    rule: Any,
    element_by_id: dict[Any, dict[str, Any]],
    *,
    max_list: int = 25,
) -> tuple[str, str]:
    """v1.4-K18: render ONE ACC Issue covering EVERY element that fails one rule
    with the same status (like the auto-fix proposal, but for manual review).

    Title: ``[AutoAudit] <Category> -- <rule_id> (N <bucket>)``. Body states the
    rule + requirement + expected ONCE, then a table ``element id | name |
    current`` (first ``max_list``, then "... and M more"). No suggested value —
    these aren't auto-fixable. ASCII-only (RevitMCPAddin mojibake gotcha).

    M12: ``bucket == "human_only"`` is a SYNTHETIC bucket (not an
    ``OutcomeStatus`` — these findings passed QC as real violations, e.g.
    non_compliant, but their fix was gated ``human-only`` because an LLM
    originated the value for a life-safety param). For this bucket the table
    gains a **suggested** column (from ``finding["suggested_value"]``, stamped
    by the caller) and the footer states plainly that the value must be
    written to Revit BY HAND — this issue is informational only and is never
    picked up by the ApprovalWatcher (no fingerprint, no write-set record)."""
    first = findings[0]
    el0 = element_by_id.get(first["element_id"], {})
    category = _ascii_safe(el0.get("category")) or "Element"
    param = first["parameter"]
    expected = _ascii_safe(_describe_expected(rule, first))
    top_sev = _humanize_severity(_strictest_severity(f["severity"] for f in findings))
    n = len(findings)
    bucket_label = {
        "missing_data": "missing", "non_compliant": "non-compliant",
        "human_only": "human-only fix",
    }.get(bucket, bucket)
    title = _ascii_safe(f"[AutoAudit] {category} -- {rule_id} ({n} {bucket_label})")
    is_human_only = bucket == "human_only"

    parts: list[str] = []
    parts.append(f"## `{rule_id}`")
    parts.append("")
    parts.append(f"**Parameter:** `{param}`")
    parts.append(f"**Requirement:** {expected}")
    parts.append(f"**Status:** {bucket}")
    parts.append(f"**Severity:** {top_sev}")
    parts.append(f"**Affected elements:** {n}")
    parts.append("")
    if is_human_only:
        parts.append("| element id | name | current | suggested |")
        parts.append("|---|---|---|---|")
    else:
        parts.append("| element id | name | current |")
        parts.append("|---|---|---|")
    for f in findings[:max_list]:
        el = element_by_id.get(f["element_id"], {})
        name = _ascii_safe(el.get("name")) or "(unnamed)"
        cur = (el.get("params", {}) or {}).get(param)
        cur_repr = _ascii_safe(str(cur)) if cur not in (None, "") else "(missing)"
        if is_human_only:
            sugg = f.get("suggested_value")
            sugg_repr = _ascii_safe(str(sugg)) if sugg not in (None, "") else "(none)"
            parts.append(f"| `{f['element_id']}` | {name} | {cur_repr} | {sugg_repr} |")
        else:
            parts.append(f"| `{f['element_id']}` | {name} | {cur_repr} |")
    if n > max_list:
        parts.append("")
        parts.append(f"_... and {n - max_list} more element(s)._")
    parts.append("")
    parts.append("---")
    if is_human_only:
        parts.append(
            "_(suggested value -- requires MANUAL write; not auto-appliable. "
            "This value was proposed by an LLM for a life-safety parameter and "
            "is gated human-only: it will never be auto-applied or offered as "
            "an approve-and-apply proposal.)_"
        )
    parts.append("_Auto-generated by bim-orchestrator. Manual review "
                 "(not auto-fixable). Trust pipeline: dry-run -> token -> execute._")
    return title, "\n".join(parts)
