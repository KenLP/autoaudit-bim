"""QC Agent — compares fetched elements against YAML rules, produces Findings.

Phase 1: pure rule evaluation (no LLM). Phase 2 will add Grounding Agent
output (BEP/code citations) into each Finding.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml

from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.fire_rating_units import is_not_rated
from bim_orchestrator.policies.revit_units import (
    UnitConversionError,
    convert_to_rule_unit,
    format_in_unit,
)
from bim_orchestrator.policies.rules_engine import evaluate, infer_from_name, matches_regex
# v1.3: schema lives in policies/rules_schema.py so derive_specs (also in
# policies/) can import it without inverting the layering. Re-exported here
# for backward compat — `from bim_orchestrator.agents.qc import Rule, RuleSet,
# CitationPolicy, ...` still works exactly as before.
from bim_orchestrator.policies.rules_schema import (
    AutofillStrategy,
    CitationMode,
    CitationPolicy,
    ExecutionStatus,
    ExtractionMeta,
    Fixability,
    NewValueStrategy,
    OnMissingCitation,
    RemediationAction,
    Requirement,
    Rule,
    RuleAutofill,
    RuleRemediation,
    RuleSet,
    RuleType,
    duplicate_rule_ids,
    fetch_name as _fetch_name,
    merge_rulesets,
)
from bim_orchestrator.report_trace import build_check_record
from bim_orchestrator.state import (
    CheckRecord,
    Finding,
    OrchestratorState,
    OutcomeStatus,
    OutcomeSummary,
    Severity,
)

log = structlog.get_logger(__name__)

__all__ = [
    # Schema re-exports (back-compat for pre-v1.3 imports)
    "AutofillStrategy",
    "CitationMode",
    "CitationPolicy",
    "ExecutionStatus",
    "ExtractionMeta",
    "Fixability",
    "NewValueStrategy",
    "OnMissingCitation",
    "RemediationAction",
    "Requirement",
    "Rule",
    "RuleAutofill",
    "RuleRemediation",
    "RuleSet",
    "RuleType",
    # Agent + helpers actually defined in this module
    "QCAgent",
]


def _is_missing(value: Any) -> bool:
    """v1 task BB: detect missing parameter values for the missing_data bucket.

    Treats only None and whitespace-only / empty strings as missing. Numeric
    zero, False, and empty collections are real values — not missing — and
    surface as non_compliant when an evaluator says the value fails.
    """
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _is_duration_rule(rule: Rule) -> bool:
    """Does this rule canonicalise a DURATION (fire rating)? — L-03.

    ``fire_rating`` is an alias of ``duration`` in the normalize registry
    (see ``policies/normalize._DIMENSIONS``), so both spellings count. Used to
    scope the not-rated sentinel: "NR" means no-fire-resistance-required, a
    claim that only makes sense for a duration. On any other dimension the
    same string is simply an unparseable value and must keep failing.
    """
    kind = getattr(getattr(rule, "autofill", None), "normalize_kind", None)
    return kind in ("duration", "fire_rating")


def _scope_result(
    rule: Rule, element: dict[str, Any]
) -> Literal["match", "no_match", "unknown"]:
    """Tri-state applicability of ``rule.scope_filter`` to ``element`` (M1,
    2026-07 audit).

    Collapsing "definitely not applicable" and "applicability cannot be
    determined" into one ``False`` let an invalid scope regex or a missing
    gating value SILENTLY skip an element; if every element skipped, the run
    reached ``zero_findings`` and converged "clean". The three BIM-distinct
    states are now separate:

      * ``match``    — the gating value is present and matches the pattern →
        evaluate the rule.
      * ``no_match`` — the gating value is present and does NOT match → the rule
        legitimately does not apply → skip (unchanged behaviour).
      * ``unknown``  — the pattern won't compile, or the gating value is absent /
        non-textual → applicability cannot be determined → the caller routes it
        to ``manual_review`` with a ``CheckRecord``, never a silent skip.

    No scope_filter → always ``match``. The gating param IS fetched by the query
    (``query_specs._collect_params`` adds ``scope_filter.param``), so a
    ``None``/non-string value here means the model genuinely left it unset —
    which is exactly the "cannot determine" case, not "does not match".
    """
    sf = rule.scope_filter
    if sf is None:
        return "match"
    sf_val = element.get("params", {}).get(sf.param)
    if not isinstance(sf_val, str) or not sf_val.strip():
        # Blank/whitespace counts as UNDETERMINED, not as a legitimate
        # non-match. In Revit an empty string is how an unset parameter reads,
        # and this module's own `_is_missing` already treats it that way — so
        # letting a blank gating value fall through to the regex (where it
        # quietly becomes `no_match` and the element vanishes from the audit)
        # contradicted our own missing-semantics one function away.
        return "unknown"
    try:
        return "match" if re.search(sf.pattern, sf_val) is not None else "no_match"
    except re.error as exc:
        log.warning("qc_agent.bad_scope_filter_regex",
                    rule=rule.id, pattern=sf.pattern, error=str(exc))
        return "unknown"


_TEMPLATE_TOKEN = re.compile(r"\{([^{}]+)\}")


def _fill_template(template: str, values: dict[str, Any]) -> str | None:
    """Substitute ``{token}`` references in ``template`` from ``values``.

    Tokens are literal param names (may contain spaces / underscores, e.g.
    ``{Reference Level}``, ``{_containing_space}``). Returns None if ANY
    referenced token is missing/blank — so an element lacking the spatial
    context (e.g. an unmapped duct) yields no suggested value and routes to a
    Path A ACC Issue rather than producing a malformed Mark like ``-L2-...``.
    """
    missing = False

    def _repl(m: re.Match[str]) -> str:
        nonlocal missing
        key = m.group(1)
        v = values.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing = True
            return ""
        return str(v)

    result = _TEMPLATE_TOKEN.sub(_repl, template)
    return None if missing else result


class QCAgent:
    def __init__(
        self,
        rules_path: str | Path | Sequence[str | Path],
        autonomy: AutonomyPolicy,
        *,
        classification: Any = None,
    ) -> None:
        # v1.4-K6: accept ONE path (unchanged behaviour) or several, which are
        # loaded and merged into a single RuleSet so a run can check parameter
        # + geometry + naming scenarios at once. A lone path skips the merge
        # entirely (identity), so the common one-file case is untouched.
        paths: list[str | Path] = (
            [rules_path]
            if isinstance(rules_path, (str, Path))
            else list(rules_path)
        )
        if not paths:
            raise ValueError("QCAgent needs at least one rules path")
        loaded: list[RuleSet] = []
        for p in paths:
            # encoding="utf-8" is mandatory: on Windows open() defaults to the
            # cp1252 locale, which mojibakes non-ASCII rule content (e.g.
            # unit: "m²", Vietnamese descriptions/patterns) at load time. The
            # Streamlit writer already pins utf-8, so the read must match.
            with open(p, encoding="utf-8") as f:
                loaded.append(RuleSet.model_validate(yaml.safe_load(f)))
        if len(loaded) > 1:
            collisions = duplicate_rule_ids(loaded)
            if collisions:
                # An IDENTICAL duplicate (same rule shipped in two packs) still
                # merges to one entry; only a differing definition raises below.
                log.warning(
                    "qc_agent.merge_rule_id_collision",
                    ids=collisions,
                    note="identical duplicates collapse; differing ones abort the merge",
                )
            log.info(
                "qc_agent.merged_rulesets",
                files=len(loaded),
                scenarios=[rs.scenario for rs in loaded],
            )
        self._rules = merge_rulesets(loaded)
        self._autonomy = autonomy
        # Phase 2 GĐ2: category → valid classification codes for the
        # value_in_subset detector. None → lazy-loaded from the default config on
        # first use; inject in tests to avoid disk / OST deps.
        self._classification = classification
        self._classification_loaded = classification is not None
        # v1.4-K21: reference sets (config/reference.<name>.yaml) are resolved
        # relative to the rules file's directory, so a scenario ships its rules
        # + reference tables together. Falls back to the package config/ default.
        try:
            self._config_dir: Path | None = Path(paths[0]).resolve().parent
        except (TypeError, ValueError):
            self._config_dir = None

    @property
    def rules(self) -> RuleSet:
        return self._rules

    def _classification_subset(self, element: dict[str, Any]) -> list[str]:
        """Valid classification codes for this element's category (empty if no
        catalog / unknown category). Lazy-loads the catalog once. Mirrors
        DesignAgent so QC's DETECT and Remediation's ACCEPT use the same table."""
        if not self._classification_loaded:
            self._classification_loaded = True
            try:
                from bim_orchestrator.policies.classification import ClassificationCatalog
                self._classification = ClassificationCatalog.load()
            except Exception as exc:
                log.info("qc_agent.classification_unavailable", error=str(exc))
                self._classification = None
        if self._classification is None:
            return []
        return self._classification.subset_for(element.get("category"))

    def run(self, state: OrchestratorState) -> OrchestratorState:
        log.info(
            "qc_agent.start",
            iteration=state["iteration"],
            element_count=len(state["elements"]),
            rule_count=len(self._rules.rules),
        )
        target = self._rules.target_category
        target_set: set[str] | None
        if isinstance(target, str):
            target_set = {target}
        elif isinstance(target, list):
            target_set = set(target)
        else:
            target_set = None
        in_scope = [
            el for el in state["elements"]
            if not el.get("category")
            or target_set is None
            or el["category"] in target_set
        ]
        # Pre-compute sibling lists once per set-scope rule so duplicate
        # detection is O(N) per rule instead of O(N²) per element.
        # Honours per-rule category filter so e.g. unique door Mark and
        # unique wall Mark don't pool into the same set.
        siblings_by_rule: dict[str, list[Any]] = {}
        for rule in self._rules.rules:
            if rule.requirement == "unique_in_set":
                siblings_by_rule[rule.id] = [
                    el.get("params", {}).get(_fetch_name(rule))
                    for el in in_scope
                    if (rule.category is None or el.get("category") == rule.category)
                    and _scope_result(rule, el) == "match"
                ]

        # v1.4-K3 — compose_template sequence pre-pass. ``{seq}`` is a per-group
        # counter (01, 02, …) that can't be computed per-element, so build the
        # element_id → seq map per rule here, over the full in-scope set.
        self._seq_maps = self._build_sequence_maps(in_scope)

        # v1 task BB: classify every (element, rule) into one of 4 buckets
        # instead of just emitting violations. `findings` keeps backward-compat
        # semantics (non_compliant only); manual_review and missing_data go to
        # sibling lists so DesignAgent's existing ACC-issue path is untouched
        # (task CC will route those to dedicated side reports).
        findings: list[Finding] = []
        manual_review_items: list[Finding] = []
        missing_data_items: list[Finding] = []
        # v1 report module: one CheckRecord per evaluated (element, rule) pair,
        # INCLUDING compliant + exempt outcomes — the PASS set the verification
        # report renders so a reviewer can audit for false negatives. Built here
        # (during the run), never re-derived. Parallel to `findings`; ignored by
        # DesignAgent + the route.
        check_trace: list[CheckRecord] = []
        summary: OutcomeSummary = {
            "total": 0,
            "compliant": 0,
            "non_compliant": 0,
            "manual_review": 0,
            "missing_data": 0,
            "skipped_out_of_scope": 0,
        }
        for element in in_scope:
            for rule in self._rules.rules:
                # Phase 2 W7 D1 — Per-rule category filter. Runs BEFORE the
                # summary increment so rules out-of-scope for this element
                # (e.g. a Walls-only rule against a Door element) don't pollute
                # the outcomes_summary totals with "compliant by default" counts.
                if rule.category is not None and element.get("category") != rule.category:
                    # v1.5-R6 (Coverage block): capture-time count of the
                    # (element, rule) pairs the report's Coverage section needs
                    # to explain the fetched-vs-evaluated gap honestly.
                    summary["skipped_out_of_scope"] += 1
                    continue
                # v1.4-K10 — universal scope filter (tri-state, M1). A legitimate
                # non-match is skipped BEFORE the summary increment (not counted
                # as compliant nor as a finding). But an UNDETERMINED scope (bad
                # regex or a missing gating value) must NOT be a silent skip — it
                # is a COUNTED pair whose outcome is manual_review + a trace
                # record, so an all-undetermined run can never converge "clean".
                scope = _scope_result(rule, element)
                if scope == "no_match":
                    summary["skipped_out_of_scope"] += 1
                    continue
                if scope == "unknown":
                    summary["total"] += 1
                    summary["manual_review"] += 1
                    finding = self._build_finding(
                        rule, element, None, status="manual_review"
                    )
                    manual_review_items.append(finding)
                    check_trace.append(build_check_record(
                        rule, element, raw_value=None, value=None,
                        passed=False, status="manual_review",
                        severity=finding["severity"],
                        suggested_value=finding.get("suggested_value"),
                        inherited_from=finding.get("inherited_from"),
                    ))
                    continue
                summary["total"] += 1
                # Binding layer: bound_parameter is the actual Revit param
                # name; parameter is the canonical intent label. Use the
                # bound name for the fetch + unit conversion; parameter is
                # preserved in findings for reports.
                fetch_param = _fetch_name(rule)
                raw_value = element.get("params", {}).get(fetch_param)
                # v1.4-D0.5: if rule declares an explicit unit, convert the
                # raw Revit storage value to the rule's unit BEFORE the
                # evaluator runs. No-op when rule.unit is None (back-compat
                # for hand-authored YAMLs and legacy metric mirrors).
                try:
                    value = convert_to_rule_unit(raw_value, fetch_param, rule.unit)
                except UnitConversionError as exc:
                    # M-b: known-but-unconvertible unit → route to a human instead
                    # of a silent wrong-unit compare.
                    log.warning("qc_agent.unit_mismatch", rule=rule.id,
                                param=fetch_param, error=str(exc))
                    summary["manual_review"] += 1
                    finding = self._build_finding(
                        rule, element, raw_value, status="manual_review"
                    )
                    manual_review_items.append(finding)
                    # Same contract as M-a below: a pair counted in `total` MUST
                    # leave a trace record. The verification report RENDERS the
                    # trace and never re-derives, so without this the run summary
                    # counts a manual_review the report cannot show per element.
                    # `value` is unset here (the conversion is what raised), so
                    # the raw value stands in for both.
                    check_trace.append(build_check_record(
                        rule, element, raw_value=raw_value, value=raw_value,
                        passed=False, status="manual_review",
                        severity=finding["severity"],
                        suggested_value=finding.get("suggested_value"),
                        inherited_from=finding.get("inherited_from"),
                    ))
                    continue
                condition_value = (
                    element.get("params", {}).get(rule.when_param)
                    if rule.when_param else None
                )
                # Phase 2 W7 D1 — cross-element reference value for the
                # fire_rating_ge evaluator (e.g. door rating vs host wall rating).
                other_value = (
                    element.get("params", {}).get(rule.other_param)
                    if rule.other_param else None
                )
                # Phase 2 GĐ2: per-element allowed set for value_in_subset —
                # resolved by the element's category, the SAME table Remediation
                # uses to accept (detect-rule == accept-rule).
                subset = (
                    (list(rule.allowed_values) if rule.allowed_values
                     else self._classification_subset(element))
                    if rule.requirement == "value_in_subset" else None
                )
                # Table-driven relational check (IBC §716 POC): map the related
                # value (host wall rating) through a lookup table → the REQUIRED
                # value, then relation_compare against that. Out-of-scope (door not
                # in a rated wall) → compliant; a rated wall whose rating isn't a
                # table key → manual review (never guessed).
                lookup_unresolved = False
                not_rated_exempt = False  # L-03, set in the canonical_format branch
                if rule.lookup and rule.requirement == "relation_compare":
                    from bim_orchestrator.policies.lookup_table import load_lookup
                    try:
                        required, exempt = load_lookup(
                            rule.lookup, getattr(self, "_config_dir", None)
                        ).match(element.get("params", {}))
                    except (FileNotFoundError, OSError, ValueError) as exc:
                        log.warning("qc_agent.lookup_load_failed", rule=rule.id,
                                    name=rule.lookup, error=str(exc))
                        required, exempt = None, False
                    if exempt:
                        # host wall not rated → §716 imposes no requirement → compliant
                        summary["compliant"] += 1
                        check_trace.append(build_check_record(
                            rule, element, raw_value=raw_value, value=value,
                            passed=True, status="compliant", exempt=True,
                        ))
                        continue
                    lookup_unresolved = required is None
                    other_value = required
                try:
                    if lookup_unresolved:
                        passed = False
                    elif rule.requirement == "canonical_format":
                        # v1.4-K12: check + fix derive from ONE declaration (the
                        # normalize autofill). Compliant iff the value is ALREADY in
                        # canonical form — i.e. equals what the normalizer would
                        # produce. The suggested fix (via _suggest) is that SAME
                        # canonical form, so the check and the fix can never drift
                        # (no separate pattern to keep in sync). Unparseable values
                        # (canonical=None) are non-compliant + unfixable → Path A.
                        canonical = self._suggest(rule, element)
                        passed = (
                            canonical is not None
                            and str(value).strip() == str(canonical).strip()
                        )
                        # L-03: on a DURATION rule, an explicit not-rated
                        # sentinel is a valid BIM statement ("this element
                        # carries no fire-resistance requirement"), not a
                        # malformed duration — the normalizer just has nothing
                        # to canonicalise, so it returned None and the element
                        # fell out as non_compliant + unfixable. Live on
                        # Snowdon that flagged all 51 `NR` doors, i.e. the
                        # first run a BIM manager sees is 149/149 red.
                        # `is_not_rated` is the SAME predicate lookup_table
                        # uses (that path already treated a not-rated host as
                        # exempt); the two now share one definition instead of
                        # disagreeing about the same word. Deliberately narrow:
                        # blank stays missing_data and an unparseable non-blank
                        # ("2 HR (UL U419)") stays non-compliant — that element
                        # DOES carry a rating we can't read, and exempting it
                        # would silently pass a possibly under-spec door.
                        if not passed and _is_duration_rule(rule) and is_not_rated(value):
                            not_rated_exempt = True
                    else:
                        passed = evaluate(
                            rule.requirement,
                            value,
                            pattern=rule.pattern,
                            threshold=rule.threshold,
                            condition_value=condition_value,
                            when_pattern=rule.when_pattern,
                            siblings=siblings_by_rule.get(rule.id),
                            other_value=other_value,
                            # v1.4-K10: operator + compare_kind for numeric_compare /
                            # relation_compare (ignored by the other requirements).
                            operator=rule.operator,
                            compare_kind=rule.compare_kind,
                            subset=subset,
                        )
                except (re.error, ValueError, TypeError) as exc:
                    # M-a: a mis-authored rule (bad user regex, wrong-typed
                    # threshold, etc.) must route to manual_review — never crash the
                    # whole run so every OTHER rule still reports.
                    log.warning("qc_agent.rule_eval_error", rule=rule.id, error=str(exc))
                    summary["manual_review"] += 1
                    finding = self._build_finding(
                        rule, element, raw_value, status="manual_review",
                        converted_value=value,  # L-02: the conversion succeeded
                    )
                    manual_review_items.append(finding)
                    # v1 report module: record the mis-authored → manual_review
                    # outcome in the trace too, so the verification report accounts
                    # for it (then skip the normal bucketing below).
                    check_trace.append(build_check_record(
                        rule, element, raw_value=raw_value, value=value,
                        passed=False, status="manual_review", other_value=other_value,
                        severity=finding["severity"],
                        suggested_value=finding.get("suggested_value"),
                        inherited_from=finding.get("inherited_from"),
                    ))
                    continue
                # v1 report module: `required` carries the lookup-resolved value
                # for a relation_compare+lookup rule (== other_value after the
                # block above); None otherwise. Used only to populate the record.
                required_value = (
                    other_value
                    if (rule.lookup and rule.requirement == "relation_compare")
                    else None
                )
                if not_rated_exempt:
                    # L-03: the check does not APPLY to this element (it
                    # declares no rating), which is not the same as passing
                    # it. `exempt=True` is the flag the lookup path already
                    # uses for exactly this, so the report keeps showing the
                    # two apart instead of burying it in the pass count.
                    summary["compliant"] += 1
                    check_trace.append(build_check_record(
                        rule, element, raw_value=raw_value, value=value,
                        passed=True, status="compliant", exempt=True,
                        other_value=other_value, required=required_value,
                        subset=subset,
                    ))
                    continue
                if passed:
                    summary["compliant"] += 1
                    check_trace.append(build_check_record(
                        rule, element, raw_value=raw_value, value=value,
                        passed=True, status="compliant",
                        other_value=other_value, required=required_value,
                        subset=subset,
                    ))
                    continue

                # Failed evaluation — pick the correct bucket. Missing-data
                # wins over requires_human: if the parameter is absent we
                # can't say whether the value would have been compliant.
                # Use ``raw_value`` for the missing check + finding display
                # so the user sees the actual Revit-side value (and so
                # "0.0" feet doesn't accidentally count as missing because
                # conversion produced 0.0).
                if _is_missing(raw_value):
                    bucket_status: OutcomeStatus = "missing_data"
                elif rule.requires_human or lookup_unresolved:
                    # lookup_unresolved: a rated wall whose rating isn't in the
                    # table — a human confirms the required door rating (§716 POC).
                    bucket_status = "manual_review"
                else:
                    bucket_status = "non_compliant"
                summary[bucket_status] += 1
                finding = self._build_finding(
                    rule, element, raw_value, status=bucket_status,
                    converted_value=value,  # L-02: unit-declared display
                )
                if bucket_status == "missing_data":
                    missing_data_items.append(finding)
                elif bucket_status == "manual_review":
                    manual_review_items.append(finding)
                else:
                    findings.append(finding)
                check_trace.append(build_check_record(
                    rule, element, raw_value=raw_value, value=value,
                    passed=False, status=bucket_status,
                    other_value=other_value, required=required_value,
                    severity=finding["severity"],
                    suggested_value=finding.get("suggested_value"),
                    inherited_from=finding.get("inherited_from"),
                    subset=subset,
                ))

        # Trace-completeness invariant: every (element, rule) pair counted in
        # `total` must leave exactly one CheckRecord — the verification report
        # RENDERS the trace and never re-derives, so a counted-but-untraced
        # outcome is a per-element evidence hole (this is exactly how the
        # UnitConversionError branch silently dropped its record). Soft on
        # purpose: a real run must never crash on a bookkeeping mismatch, but
        # the warning turns a future regression from invisible into greppable.
        if len(check_trace) != summary["total"]:
            log.warning(
                "qc_agent.trace_incomplete",
                total=summary["total"],
                trace_records=len(check_trace),
                note="a counted (element, rule) pair produced no CheckRecord",
            )
        # M1 run-level coverage guard: a non-empty ruleset over a non-empty
        # element set that evaluated ZERO pairs is not an ordinary clean pass —
        # every pair was ruled out-of-scope. Legitimate (a rule that genuinely
        # applies to nothing) OR a mis-scoped ruleset; either way it must be
        # visible, not read as "all checked, all compliant". Soft (never crashes
        # a run); undetermined scopes already surface as manual_review above.
        if self._rules.rules and in_scope and summary["total"] == 0:
            log.warning(
                "qc_agent.zero_evaluated_pairs",
                rules=len(self._rules.rules),
                elements=len(in_scope),
                skipped_out_of_scope=summary["skipped_out_of_scope"],
                note="nothing was evaluated — every (element, rule) pair was out of scope",
            )
        log.info(
            "qc_agent.done",
            findings=len(findings),
            manual_review=len(manual_review_items),
            missing_data=len(missing_data_items),
            compliant=summary["compliant"],
            total=summary["total"],
            trace_records=len(check_trace),
            high=sum(1 for f in findings if f["severity"] == "severity_high"),
            medium=sum(1 for f in findings if f["severity"] == "severity_medium"),
            low=sum(1 for f in findings if f["severity"] == "severity_low"),
        )
        return {
            **state,
            "findings": findings,
            "manual_review_items": manual_review_items,
            "missing_data_items": missing_data_items,
            "outcomes_summary": summary,
            "check_trace": check_trace,
            "status": "designing",
        }

    def _build_finding(
        self,
        rule: Rule,
        element: dict[str, Any],
        actual_value: Any,
        *,
        status: OutcomeStatus = "non_compliant",
        converted_value: Any = None,
    ) -> Finding:
        # v1.4-K10: an explicit severity_level (set by the Rule Builder's
        # Low/Med/High picker) wins over the severity_tag→level mapping —
        # severity is the user's importance judgment, decoupled from the
        # requirement kind. Falls back to the tag mapping for legacy rules.
        severity: Severity = (
            rule.severity_level  # type: ignore[assignment]
            or self._autonomy.resolve_severity(rule.severity_tag)  # type: ignore[assignment]
        )
        suggested = self._suggest(rule, element)
        room_label = element.get("name") or element.get("id", "<unknown>")
        # L-02: when the rule declares a unit, the evidence a human reads must
        # be in THAT unit. The comparison always used the converted number;
        # only this message (and current_value below) showed Revit's raw
        # storage value, so "must be at least 900 mm ... Got 2.16" asked a
        # reviewer to sign off on a number the rule never mentions. Falls back
        # to the raw value whenever there is no unit or nothing numeric to
        # label, so non-dimensional rules are untouched.
        shown_value = format_in_unit(converted_value, rule.unit)
        if shown_value is converted_value:
            shown_value = actual_value
        message = (
            f"{rule.id}: {room_label} — {rule.description}. "
            f"Got {shown_value!r} for parameter {rule.parameter!r}."
        )
        # Display-preferred current value (e.g. "L2" over a levelId), falling
        # back to the raw evaluated value — surfaced so the UI can show
        # "current → proposed" (v1.4-K9).
        # L2 (audit): params_display is keyed by the FETCHED (bound) name — the
        # canonical label is only the human intent alias, so a bound rule missed
        # here and silently fell back to the raw value (losing e.g. "L2" for a
        # levelId). The finding's `parameter` FIELD below deliberately stays
        # canonical: that one is the report label, not a dict key.
        # L-02: same precedence as the report trace's `value_display` — a
        # declared unit outranks the Revit display string, which for a
        # dimensional param is just the raw number again.
        current_value = (element.get("params_display") or {}).get(
            _fetch_name(rule), actual_value
        )
        if shown_value is not actual_value:
            current_value = shown_value
        finding: Finding = Finding(
            rule_id=rule.id,
            element_id=element.get("id", ""),
            parameter=rule.parameter,
            severity_tag=rule.severity_tag,
            severity=severity,
            message=message,
            current_value=current_value,
            suggested_value=suggested,
            citation=None,
            status=status,
        )
        # v1 dogfood QW-1: surface human-readable element name when available
        # so downstream reports + UI don't show base64 URNs to BIM Managers.
        # v1.4-K22.1: for a TYPE-level parameter (the value lives on the family
        # type — the query mirrors it as ``type.<param>``), show "<family> - <type>"
        # so the reviewer sees WHICH family/type changes — a door instance's own
        # name is just its type. Instance params keep the plain element name.
        _params = element.get("params") or {}
        # L2 (audit): type mirrors are keyed `type.<fetched name>` by the query
        # agent, so a bound rule never matched and lost its "family - type" name.
        _is_type_level = f"type.{_fetch_name(rule)}" in _params
        _fam = _params.get("_family_name")
        _typ = _params.get("_type_name")
        if _is_type_level and (_fam or _typ):
            finding["element_name"] = " - ".join(x for x in (_fam, _typ) if x)
        elif element.get("name"):
            finding["element_name"] = str(element.get("name"))
        # v1.4-K22: for an inherit rule whose value is EMPTY, record the host's
        # value so the Results table (not just Approvals) can show
        # "(trống) ⤺ host: <value>" — the reviewer sees the source up front.
        _af = rule.autofill
        if getattr(_af, "strategy", None) in ("inherit_from_host", "inherit_then_normalize"):
            if actual_value is None or (isinstance(actual_value, str) and not actual_value.strip()):
                # L2 (audit): host params are hydrated under `host.<fetched name>`
                # (query_specs._collect_params resolves through fetch_name), so
                # this must too — it was the last site diverging from its already
                # fixed twin in design.py's `_prepare_revit_fix`.
                _hp = getattr(_af, "host_param", None) or _fetch_name(rule)
                _hv = (element.get("params") or {}).get(f"host.{_hp}")
                if _hv not in (None, ""):
                    finding["inherited_from"] = str(_hv)
        return finding

    def _build_sequence_maps(
        self, in_scope: list[dict[str, Any]]
    ) -> dict[str, dict[Any, int]]:
        """Map element_id → 1-based sequence per compose_template rule.

        Elements are grouped by the rule's ``sequence_scope`` param values and
        numbered within each group, ordered by element id for determinism.
        """
        maps: dict[str, dict[Any, int]] = {}
        for rule in self._rules.rules:
            af = rule.autofill
            if af.strategy != "compose_template" or not af.sequence_scope:
                continue
            groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
            for el in in_scope:
                if rule.category is not None and el.get("category") != rule.category:
                    continue
                params = el.get("params", {})
                key = tuple(str(params.get(s, "")) for s in af.sequence_scope)
                groups[key].append(el)
            seq_by_el: dict[Any, int] = {}
            for members in groups.values():
                members.sort(key=lambda e: str(e.get("id")))
                for i, el in enumerate(members, start=1):
                    seq_by_el[el.get("id")] = i
            maps[rule.id] = seq_by_el
        return maps

    def _suggest(self, rule: Rule, element: dict[str, Any]) -> Any | None:
        strategy = rule.autofill.strategy
        fallback = rule.autofill.fallback
        if strategy == "infer_from_room_name":
            inferred = infer_from_name(element.get("name"))
            return inferred if inferred is not None else fallback
        if strategy == "infer_from_adjacent":
            # Phase 1: adjacency analysis is deferred; use the fallback only.
            return fallback
        if strategy == "normalize":
            from bim_orchestrator.policies.normalize import (
                auto_candidates,
                normalize_value,
            )
            kind = rule.autofill.normalize_kind or "fire_rating"
            current = element.get("params", {}).get(_fetch_name(rule))
            if kind == "reference":
                # v1.4-K21: snap to an authoritative reference set (tiers 1–2).
                # QC owns the file I/O (loader is cached); a miss → None → Path A.
                from bim_orchestrator.policies.reference import (
                    load_reference,
                    normalize_reference,
                )
                ref_name = rule.autofill.normalize_reference
                if not ref_name:
                    return fallback
                try:
                    ref = load_reference(ref_name, getattr(self, "_config_dir", None))
                except (FileNotFoundError, OSError, ValueError) as exc:
                    log.warning("qc_agent.reference_load_failed",
                                rule=rule.id, name=ref_name, error=str(exc))
                    return fallback
                snapped = normalize_reference(current, ref)
                return snapped if snapped is not None else fallback
            _is_regex = rule.requirement in ("matches_regex", "matches_regex_if_present")
            if kind == "auto":
                # v1.4-K16: the engine SELF-PROPOSES — try every deterministic
                # canonicaliser (units in several formats + separator fix) and keep
                # the first whose output satisfies the rule's pattern. The author
                # declares NOTHING but the pattern. No pattern → can't validate an
                # auto pick → fall back (→ Path A).
                if not (_is_regex and rule.pattern):
                    return fallback
                for cand in auto_candidates(current):
                    # M3: guard by FULLMATCH (rules_engine.matches_regex), not
                    # re.search. An un-anchored pattern (e.g. "HR$") can match
                    # a substring of a candidate that still fails the actual
                    # K9 check (re.fullmatch) — search would wrongly propose
                    # it, violating "never propose a value that still
                    # violates the rule" (K9 invariant).
                    if matches_regex(str(cand), rule.pattern):
                        return cand
                return fallback
            normalized = normalize_value(
                current, kind, rule.autofill.normalize_format,
                rule.autofill.normalize_map, rule.autofill.normalize_source,
            )
            # Safety guard (v1.4-K9): never PROPOSE a value that still violates the
            # rule. For a matches_regex / matches_regex_if_present rule the
            # normalised value must satisfy the pattern by FULLMATCH (M3: was
            # re.search, which lets an un-anchored pattern like "HR$" wrongly
            # pass on a candidate that merely CONTAINS a match but fails the
            # actual K9 check in rules_engine — re.fullmatch — silently
            # re-proposing a still-violating value). v1.4-K22: when the
            # DECLARED format doesn't (e.g. format "{h}-hour" → "1-hour" but
            # pattern wants "...HR$"), SELF-HEAL via the auto candidates (K16)
            # before giving up — pick the first canonical form that matches
            # the pattern, so a format/pattern mismatch still produces a fix
            # instead of a silent empty proposal. Only if NONE match → fallback.
            if (
                rule.requirement in ("matches_regex", "matches_regex_if_present")
                and rule.pattern
                and (normalized is None or not matches_regex(str(normalized), rule.pattern))
            ):
                for cand in auto_candidates(current):
                    if matches_regex(str(cand), rule.pattern):
                        return cand
                return fallback
            return normalized if normalized is not None else fallback
        if strategy == "compose_template":
            template = rule.autofill.template
            if not template:
                return fallback
            values = dict(element.get("params", {}))
            # Prefer display strings (e.g. "L2") over raw values (levelId) so
            # ElementId-valued params render meaningfully in the template.
            for k, v in (element.get("params_display") or {}).items():
                if v is not None and str(v).strip():
                    values[k] = v
            seq = getattr(self, "_seq_maps", {}).get(rule.id, {}).get(element.get("id"))
            if seq is not None:
                values["seq"] = f"{seq:02d}"
            composed = _fill_template(template, values)
            return composed if composed is not None else fallback
        if strategy == "inherit_from_host":
            # v1.4-K20: copy a parameter DOWN from the host element. The query
            # layer surfaces the host's value under "host.<name>" (revit_query.
            # _apply_host_hop) once the rule turns on the host hop. Default to the
            # rule's OWN parameter (a door's Fire Rating inherits the host wall's
            # Fire Rating). Absent/blank host value → None → Path A; we never
            # invent a value.
            host_param = rule.autofill.host_param or _fetch_name(rule)
            val = element.get("params", {}).get(f"host.{host_param}")
            if val is None or (isinstance(val, str) and not val.strip()):
                return fallback
            return val
        if strategy == "inherit_then_normalize":
            # v1.4-K22: COMPOUND deterministic fix — fill from host when empty,
            # THEN canonicalise. One rule covers "must be present (inherit if not)
            # AND in canonical format": empty door → inherit host Fire Rating →
            # normalise to "2 HR"; "120 MIN" → normalise to "2 HR"; "2 HR" → itself
            # (compliant). Pair with requirement="canonical_format". No LLM — pure
            # value pipeline, so QC + DesignAgent execute it like any autofill.
            from bim_orchestrator.policies.normalize import normalize_value
            params = element.get("params", {})
            val = params.get(_fetch_name(rule))
            if val is None or (isinstance(val, str) and not val.strip()):
                host_param = rule.autofill.host_param or _fetch_name(rule)
                val = params.get(f"host.{host_param}")
            if val is None or (isinstance(val, str) and not val.strip()):
                return fallback   # nothing to inherit → Path A
            kind = rule.autofill.normalize_kind or "fire_rating"
            out = normalize_value(
                val, kind, rule.autofill.normalize_format,
                rule.autofill.normalize_map, rule.autofill.normalize_source,
            )
            return out if out is not None else fallback
        return None
