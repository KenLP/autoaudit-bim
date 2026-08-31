"""v1 report module — structured (element, rule) trace + Design/Result join.

Two pure halves, both consumed by ``audit_report.render_audit_report``:

* :func:`build_check_record` is called by ``QCAgent.run`` inside its evaluation
  loop to capture EVERY ``(element, rule)`` verdict — including the ``compliant``
  and lookup-``exempt`` outcomes that ``findings`` discards. That PASS set is the
  whole point of the verification report: a BIM Director's real fear is the
  silently-passed wrong element (a false negative), so the report must show what
  passed and *why*, not only the failures. Capturing it during the run (rather
  than re-deriving it afterwards) is deliberate — a second evaluation pass could
  disagree with the run's actual verdict, giving two sources of truth.

* :func:`join_outcomes` attaches the Design/Result stage to each record: Path A
  (ACC Issue) vs Path B (Revit write), the issue id + status, and the
  ``before -> after`` value. It reads ``state["proposed_fixes"]`` only — the
  DesignAgent is never modified. Fixes are keyed back to records by
  ``(rule_id, element_id)`` for per-element writes/proposals and by
  ``(rule_id, status)`` for the grouped Path A issues (one per rule+bucket).

Nothing here touches a network or the filesystem.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bim_orchestrator.policies.revit_units import format_in_unit
from bim_orchestrator.state import CheckRecord, OutcomeStatus, ProposedFix


def _coerce_revit_element_id(element_id: Any) -> int | None:
    """Return ``element_id`` as an int for the native "Select by ID" recipe.

    The ``--run-revit`` path uses integer Revit ElementIds; the Forma/AECDM path
    uses base64-ish URNs. Return None for the latter so the renderer falls back
    to the URN string + the ACC viewer path instead of a bogus Select-by-ID.
    """
    try:
        return int(str(element_id))
    except (TypeError, ValueError):
        return None


def revit_parameter_of(rule: Any) -> str:
    """The actual Revit parameter name for ``rule`` — mirrors
    ``policies.rules_schema.fetch_name`` (bound_parameter wins over the
    canonical ``parameter`` intent label) but tolerates the duck-typed
    ``SimpleNamespace`` :func:`rule_view_from_record` builds when the live
    ``RuleSet`` isn't in hand (no hard attribute access, so a fallback rule
    without ``bound_parameter`` doesn't raise).

    v1.5-R6: a `bound_parameter` rule's ``params``/``params_display`` dict is
    keyed by THIS name, not the canonical one — every report-side detection /
    schedule-field/view-filter reference must resolve through here (not
    ``rule.parameter`` directly) or a bound rule silently mismatches, exactly
    the false-negative class the bound_parameter chain review caught
    elsewhere (design.py / query_specs.py).
    """
    bound = getattr(rule, "bound_parameter", None)
    if bound:
        return str(bound)
    return str(getattr(rule, "parameter", "") or "")


def _element_display_name(rule: Any, element: dict[str, Any]) -> str | None:
    """Mirror ``QCAgent._build_finding``'s name choice so the report and the ACC
    issues agree: a TYPE-level parameter shows ``"<family> - <type>"`` (the value
    lives on the family type), an instance param shows the element's own name."""
    params = element.get("params") or {}
    parameter = revit_parameter_of(rule)
    if f"type.{parameter}" in params:
        fam = params.get("_family_name")
        typ = params.get("_type_name")
        joined = " - ".join(x for x in (fam, typ) if x)
        if joined:
            return joined
    name = element.get("name")
    return str(name) if name else None


def build_check_record(
    rule: Any,
    element: dict[str, Any],
    *,
    raw_value: Any,
    value: Any,
    passed: bool,
    status: OutcomeStatus,
    other_value: Any = None,
    required: Any = None,
    exempt: bool = False,
    severity: str | None = None,
    suggested_value: Any = None,
    inherited_from: str | None = None,
    subset: list[Any] | None = None,
) -> CheckRecord:
    """Build one :class:`CheckRecord` from the QC evaluation context.

    Called once per ``(element, rule)`` pair, at every outcome point in the QC
    loop (compliant, lookup-exempt, missing_data, manual_review, non_compliant).
    Denormalizes the rule's comparison anatomy (operand / threshold / operator /
    pattern / unit) so the artifact stands alone for the renderer.
    """
    element_id = str(element.get("id", ""))
    params_display = element.get("params_display") or {}
    parameter = getattr(rule, "parameter", "")
    requirement = getattr(rule, "requirement", "")
    # v1.5-R6: the ACTUAL Revit field (bound_parameter wins) — `params` /
    # `params_display` are keyed by this, not the canonical label. Stamped on
    # the record (NotRequired, back-compat) so every downstream consumer
    # (verify recipe, schedule field, view filter) can resolve the real Revit
    # name even without the live RuleSet in hand.
    revit_param = revit_parameter_of(rule)

    record: CheckRecord = {
        "rule_id": getattr(rule, "id", ""),
        "element_id": element_id,
        "parameter": parameter,
        "requirement": requirement,
        "raw_value": raw_value,
        "value": value,
        "passed": passed,
        "status": status,
    }
    if revit_param:
        record["revit_parameter"] = revit_param

    name = _element_display_name(rule, element)
    if name:
        record["element_name"] = name
    # F-02: a design-option element is an ALTERNATIVE — at most one option per
    # set ever gets built. Its findings must not read like findings on the real
    # design, so the option rides on the record and the renderer names it.
    _option = (element.get("params") or {}).get("_design_option")
    if _option:
        record["design_option"] = str(_option)
    record["revit_element_id"] = _coerce_revit_element_id(element_id)
    if element.get("category"):
        record["category"] = str(element["category"])

    # Low4: for a TYPE-level parameter, stash the element's resolved type id
    # (same detection `_element_display_name` uses) so the outcome join can
    # find the fix that a Path-B TYPE write collapsed N instances into, even
    # when THIS record's own element_id isn't the one DesignAgent's dedup kept
    # as the representative finding. Not a re-derivation — this is the same
    # ``_type_id`` breadcrumb the query layer already attached to `element`;
    # the render-side join in `outcome_for` reads it, it doesn't compute it.
    params = element.get("params") or {}
    if f"type.{revit_param}" in params and params.get("_type_id") is not None:
        record["_type_id"] = params["_type_id"]  # type: ignore[typeddict-unknown-key]

    # L-02: for a rule that DECLARES a unit, the human-facing cell must state
    # the converted value + unit ("914.4 mm"), not Revit's raw storage number
    # ("2.8333333333333335"). The verdict was always computed on `value`; only
    # the DISPLAY fell back to `raw_value`, so a reviewer was asked to sign off
    # on evidence in a unit the rule never mentioned. `format_in_unit` returns
    # the SAME object when there is nothing to label, so `is not` separates
    # "produced a unit string" from "left it alone" without re-testing types.
    unit_display = format_in_unit(value, getattr(rule, "unit", None))
    display = params_display.get(revit_param)
    if unit_display is not value:
        record["value_display"] = unit_display
    elif display is not None:
        record["value_display"] = display

    # ── comparison operands (per requirement) ──────────────────────────────
    if requirement == "relation_compare":
        lookup = getattr(rule, "lookup", None)
        if lookup:
            # The required value is a code-table function of the related element;
            # `required` is what the lookup resolved to (None when unresolved).
            record["operand"] = required
            record["operand_source"] = f"lookup table '{lookup}'"
        else:
            record["operand"] = other_value
            other_param = getattr(rule, "other_param", None)
            if other_param:
                record["operand_source"] = other_param
        op = getattr(rule, "operator", None)
        if op:
            record["operator"] = op
    elif requirement == "fire_rating_ge":
        record["operand"] = other_value
        other_param = getattr(rule, "other_param", None)
        if other_param:
            record["operand_source"] = other_param
        record["operator"] = ">="
    elif requirement in ("numeric_compare", "numeric_min", "numeric_min_conditional"):
        if getattr(rule, "threshold", None) is not None:
            record["threshold"] = rule.threshold
        record["operator"] = getattr(rule, "operator", None) or ">="
    elif requirement == "positive_number":
        record["threshold"] = 0
        record["operator"] = ">"
    elif requirement in (
        "matches_regex", "matches_regex_if_present", "not_matches_regex"
    ):
        if getattr(rule, "pattern", None):
            record["pattern"] = rule.pattern
    elif requirement == "value_in_subset":
        # Phase 2 GĐ2: the per-element resolved allowed set (explicit
        # rule.allowed_values or the classification-table lookup) IS the
        # operand for this requirement — the renderer needs it to name the
        # native filter's allowed-values list.
        if subset is not None:
            record["operand"] = list(subset)
            record["operand_source"] = (
                "rule.allowed_values" if getattr(rule, "allowed_values", None)
                else "classification table"
            )

    if getattr(rule, "unit", None):
        record["unit"] = rule.unit
    if severity:
        record["severity"] = severity  # type: ignore[typeddict-item]
    if suggested_value is not None:
        record["suggested_value"] = suggested_value
    if inherited_from is not None:
        record["inherited_from"] = inherited_from
    if exempt:
        record["exempt"] = True
    return record


def rule_view_from_record(rec: CheckRecord) -> SimpleNamespace:
    """Reconstruct a minimal rule-like object from a CheckRecord.

    The verification report + the view auto-creator both need a rule's anatomy to
    pick a verify recipe. When the live ``RuleSet`` is in hand they use it; when
    it isn't (a later re-render / view build straight from the persisted
    ``report_trace.json``) this rebuilds just the attributes the recipe registry +
    describers read. ``description`` is unavailable in this fallback.
    """
    op_src = rec.get("operand_source") or ""
    lookup = None
    if op_src.startswith("lookup table "):
        lookup = op_src.removeprefix("lookup table ").strip("'\" ")
    return SimpleNamespace(
        id=rec["rule_id"],
        parameter=rec["parameter"],
        requirement=rec["requirement"],
        category=rec.get("category"),
        threshold=rec.get("threshold"),
        operator=rec.get("operator"),
        pattern=rec.get("pattern"),
        unit=rec.get("unit"),
        lookup=lookup,
        other_param=None if lookup else op_src or None,
        description=None,
        severity_level=rec.get("severity"),
        severity_tag=None,
        # v1.5-R6: back-compat trace (pre-revit_parameter records) leaves this
        # None → revit_parameter_of falls back to `parameter`, unchanged.
        bound_parameter=rec.get("revit_parameter"),
    )


# ── Design/Result outcome join ──────────────────────────────────────────────


def _split_finding_id(finding_id: str) -> tuple[str, str, str | None]:
    """Parse a ProposedFix.finding_id into ``(rule_id, key, bucket)``.

    Two shapes are produced by DesignAgent:
      * ``"<rule_id>::<element_id>"``         → per-element write / proposal / park
      * ``"rulegroup::<rule_id>::<bucket>"``  → ONE grouped Path A issue per
                                                (rule, status bucket)
    For the grouped shape ``key`` is the element_id stamped on the fix (the first
    member) and ``bucket`` is the status; for the per-element shape ``bucket`` is
    None.

    v1.5-R6 (3.4 hygiene): the per-element shape splits on the LAST ``::``
    (``rpartition``), not the first (``partition``) — a rule id is free to
    contain ``::`` itself (e.g. an intentionally hierarchical id); a
    Revit ElementId / AECDM URN never does, so splitting from the right still
    isolates ``element_id`` correctly while no longer truncating a ``::``-
    bearing rule_id. Mirrors the rulegroup branch above, which already joins
    ``parts[1:-1]`` and is unaffected by this class of bug.
    """
    if finding_id.startswith("rulegroup::"):
        parts = finding_id.split("::")
        # rulegroup :: <rule_id...> :: <bucket>
        rule_id = "::".join(parts[1:-1]) if len(parts) >= 3 else ""
        bucket = parts[-1] if len(parts) >= 3 else None
        return rule_id, "", bucket
    rule_id, sep, element_id = finding_id.rpartition("::")
    if not sep:
        # No separator at all — preserve the old partition() semantics
        # (whole string as rule_id, empty element_id) rather than rpartition's
        # own no-match shape (empty rule_id, whole string as element_id).
        rule_id, element_id = finding_id, ""
    return rule_id, element_id, None


def _is_rich(fix: ProposedFix) -> bool:
    """True when ``fix``'s preview carries a real outcome (an ACC issue was
    created, a proposal was actually parked, or a Revit write happened) — as
    opposed to an in-run dedup STUB (``preview.skipped_duplicate`` for a Path
    A group re-hit, see ``DesignAgent._propose_rule_group``) that carries none
    of these. Used by :func:`index_fixes` so a later, thinner iteration's stub
    can never displace an earlier iteration's real fix for the same key."""
    preview = fix.get("preview") or {}
    return bool(
        preview.get("executed_issue")
        or preview.get("proposal_issue_id")
        or preview.get("executed_via")
    )


def index_fixes(
    proposed_fixes: list[ProposedFix] | None,
) -> tuple[dict[tuple[str, str], ProposedFix], dict[tuple[str, str], ProposedFix]]:
    """Index fixes for the record join.

    Returns ``(by_element, by_rule_bucket)``:
      * ``by_element[(rule_id, element_id)]`` — per-element Path B writes,
        approve-gated proposals, and parked fixes. ALSO aliased under
        ``(rule_id, str(write_eid))`` (Low4) when the fix's preview carries a
        ``write_eid`` different from the flagged element_id (a TYPE-level
        write) — this is how ``outcome_for`` finds the representative fix for
        a sibling instance that ``_dedup_by_write_target`` collapsed away.
        The alias is only added when that key isn't already taken, so it
        never shadows a real per-element entry.
      * ``by_rule_bucket[(rule_id, bucket)]`` — the grouped Path A ACC issues
        (and the synthetic ``"human_only"`` bucket — see ``outcome_for``).
    Last-write-wins on duplicate keys (a later iteration supersedes an
    earlier), EXCEPT (v1.5-R6, join-hardening R5):
      * a fix whose preview is ``skipped_duplicate`` (an in-run Path A dedup
        stub carrying no issue/write info of its own) is never indexed at
        all — the earlier REAL fix for that key stays put instead of being
        overwritten by a stub that would otherwise render as "—".
      * a "rich" fix (:func:`_is_rich` — has an issue id or a committed
        write) is never displaced by a thinner one at the same key, so a
        later un-stamped duplicate can't shadow an earlier real outcome.

    Kept a 2-tuple return (not 3) — ``audit_report.py`` (out of this fix's
    file scope) unpacks exactly ``by_element, by_rule_bucket`` at several call
    sites, so the write_eid index rides inside ``by_element`` instead of a
    new dict the caller would need to thread through.
    """
    by_element: dict[tuple[str, str], ProposedFix] = {}
    by_rule_bucket: dict[tuple[str, str], ProposedFix] = {}
    write_eid_aliases: list[tuple[tuple[str, str], ProposedFix]] = []

    def _place(index: dict[tuple[str, str], ProposedFix], key: tuple[str, str], fix: ProposedFix) -> None:
        existing = index.get(key)
        if existing is not None and _is_rich(existing) and not _is_rich(fix):
            return
        index[key] = fix

    for fix in proposed_fixes or []:
        if (fix.get("preview") or {}).get("skipped_duplicate"):
            continue
        rule_id, element_id, bucket = _split_finding_id(fix.get("finding_id", ""))
        if bucket is not None:
            _place(by_rule_bucket, (rule_id, bucket), fix)
        else:
            _place(by_element, (rule_id, element_id), fix)
            write_eid = (fix.get("preview") or {}).get("write_eid")
            if write_eid is not None and str(write_eid) != element_id:
                write_eid_aliases.append(((rule_id, str(write_eid)), fix))
    for key, fix in write_eid_aliases:
        by_element.setdefault(key, fix)
    return by_element, by_rule_bucket


def outcome_for(
    record: CheckRecord,
    by_element: dict[tuple[str, str], ProposedFix],
    by_rule_bucket: dict[tuple[str, str], ProposedFix],
) -> dict[str, Any] | None:
    """Resolve the Design/Result outcome for one CheckRecord, or None.

    None means "no fix was attached" — expected for a ``compliant`` record, and a
    signal worth surfacing for a failing one (e.g. dropped by the issue budget).

    Low4: when the direct ``(rule_id, element_id)`` lookup misses, try the
    record's own resolved TYPE id (stamped by ``build_check_record`` as
    ``record["_type_id"]`` for a type-level parameter) against the SAME
    ``by_element`` index — ``index_fixes`` aliases each Path-B fix under its
    ``write_eid`` there too. This is what lets a sibling instance that
    ``_dedup_by_write_target`` collapsed into one representative write still
    render that fix's outcome instead of a misleading "—" (which used to read
    as "dropped by the issue budget" rather than "this fix covers you too").

    v1.5-R6 (human-only): a ``human-only`` (LLM safety-critical) fix ALWAYS
    also carries a per-element dangling preview (``_prepare_revit_fix``
    stamps ``write_eid`` even though the write is never committed — see
    ``DesignAgent._prepare_revit_fix``), which the direct lookup above would
    find FIRST and misreport as "Revit write (pending)". The real outcome
    lives in the grouped Path A issue at ``(rule_id, "human_only")`` — prefer
    that whenever the resolved fix is human-only-gated. Also tried as a
    fallback for a record with no direct match at all, alongside the normal
    ``(rule_id, record["status"])`` bucket.

    The human-only DETERMINATION is made from the direct per-element fix (or
    the synthetic-bucket fallback) BEFORE any swap to the grouped issue — the
    grouped ``ProposedFix`` itself carries a doc-issue autonomy (from
    ``_propose_rule_group``), not ``"human-only"``, so checking the final
    resolved fix's own ``autonomy`` field (as ``normalize_outcome`` does,
    defensively) would miss this case. ``outcome_for`` stamps the override
    onto the result explicitly so it survives the swap.
    """
    rule_id = record["rule_id"]
    fix = by_element.get((rule_id, record["element_id"]))
    if fix is None:
        type_id = record.get("_type_id")  # type: ignore[typeddict-item]
        if type_id is not None:
            fix = by_element.get((rule_id, str(type_id)))
    is_human_only = fix is not None and fix.get("autonomy") == "human-only"
    if is_human_only:
        group_fix = by_rule_bucket.get((rule_id, "human_only"))
        if group_fix is not None:
            fix = group_fix
    if fix is None and record["status"] != "compliant":
        fix = by_rule_bucket.get((rule_id, record["status"]))
        if fix is None:
            fix = by_rule_bucket.get((rule_id, "human_only"))
            is_human_only = fix is not None
    if fix is None:
        return None
    out = normalize_outcome(fix, record)
    if is_human_only:
        out["human_only"] = True
        out["path"] = "human_only"
    return out


def normalize_outcome(fix: ProposedFix, record: CheckRecord | None = None) -> dict[str, Any]:
    """Flatten a ProposedFix (+ its preview) into a renderer-friendly outcome.

    Classifies the path from the preview's shape (the same keys DesignAgent and
    the CLI summary read): an ``executed_issue`` is Path A; a ``proposal_issue_id``
    is an approve-gated Path B proposal; an ``executed_via`` is a committed Path B
    write; a bare ``write_eid`` is a prepared-but-gated Path B write; a ``reason``
    is a park.
    """
    preview = fix.get("preview") or {}
    changes = (preview.get("data") or {}).get("changes") or {}
    issue = preview.get("executed_issue") or {}

    out: dict[str, Any] = {
        "executed": bool(fix.get("executed")),
        "autonomy": fix.get("autonomy"),
        "new_value": fix.get("new_value"),
        "before": preview.get("old_value", changes.get("before")),
        "after": (
            fix.get("new_value")
            if fix.get("new_value") is not None
            else changes.get("after")
        ),
        "action": preview.get("action"),
        "write_target": preview.get("target"),
        "executed_via": preview.get("executed_via"),
        "reason": preview.get("reason"),
        "inherited_from": preview.get("inherited_from"),
        "host_conflict": preview.get("host_conflict"),
        "issue_id": issue.get("id"),
        "issue_display_id": issue.get("displayId"),
        "issue_title": issue.get("title"),
        "issue_status": issue.get("status"),
        "proposal_issue_id": preview.get("proposal_issue_id"),
        # v1.5-R6: LLM provenance rides straight from the preview (stamped by
        # DesignAgent._prepare_revit_fix for a `new_value_strategy: llm_propose`
        # rule) so the renderer can tag "(LLM-proposed)" without re-deriving it.
        "value_source": preview.get("value_source"),
        "evidence": preview.get("evidence"),
    }

    if "executed_issue" in preview:
        out["path"] = "A"
    elif "proposal_issue_id" in preview:
        out["path"] = "B-proposal"
    elif "executed_via" in preview or "write_eid" in preview:
        out["path"] = "B"
    elif "reason" in preview:
        out["path"] = "parked"
    else:
        out["path"] = None

    # v1.5-R6: a human-only (LLM safety-critical) fix must NEVER read as
    # "Revit write (pending)" — that's the per-element dangling preview
    # DesignAgent leaves behind even though the write is gated off. The real
    # routing is a manual ACC issue (see ``outcome_for``'s human-only
    # override, which resolves `fix` to the grouped issue when one exists).
    # Override the path AFTER the classification above so it wins regardless
    # of which preview shape this particular `fix` happens to carry.
    if fix.get("autonomy") == "human-only":
        out["human_only"] = True
        out["path"] = "human_only"
    return out


# ── R1-Stage 1: fix-interaction detection ───────────────────────────────────


def detect_fix_interactions(fix_write_log: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Turn the accumulated ``fix_write_log`` into an enumerated list of
    concrete critical pairs (docs/260711_Autofix Loop.md, "Practical
    corollary").

    Groups log entries by ``(write_eid, parameter)``. A key with >= 2 entries
    where the entries differ by ``iteration`` OR by ``rule_id`` is an
    interaction — the same (element, parameter) slot was written more than
    once, either by different rules (a write-write conflict) or across
    iterations (a write-read/re-trigger cycle). A single rule writing the same
    slot once is not an interaction (the common, harmless case).

    Pure function — no I/O, no re-evaluation of any rule.
    """
    by_key: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for entry in fix_write_log or []:
        key = (entry.get("write_eid"), entry.get("parameter"))
        by_key.setdefault(key, []).append(entry)

    interactions: list[dict[str, Any]] = []
    for (write_eid, parameter), entries in by_key.items():
        if len(entries) < 2:
            continue
        rule_ids = {e.get("rule_id") for e in entries}
        iterations = {e.get("iteration") for e in entries}
        if len(rule_ids) < 2 and len(iterations) < 2:
            continue
        values = [(e.get("old"), e.get("new")) for e in entries]
        interactions.append({
            "write_eid": write_eid,
            "parameter": parameter,
            "rules": sorted(r for r in rule_ids if r is not None),
            "iterations": sorted(i for i in iterations if i is not None),
            "values": values,
        })
    return interactions


__all__ = [
    "build_check_record",
    "detect_fix_interactions",
    "index_fixes",
    "normalize_outcome",
    "outcome_for",
    "revit_parameter_of",
    "rule_view_from_record",
]
