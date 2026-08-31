"""v1 report module — requirement-type → native-verification recipe registry.

This is the heart of the verification report. For ANY rule, it produces a recipe
a skeptical BIM Director can follow in ~2 minutes using tools they already trust —
a Revit schedule, a view filter + colour override, "Select by ID", an ACC issue —
to **independently** confirm the orchestrator's claim, WITHOUT trusting the tool's
own log (that would be circular).

It mirrors the engine's evaluator dispatch in ``policies/rules_engine.evaluate``:
``REQUIREMENT_RENDERERS`` is keyed by ``rule.requirement``, exactly the same keys
the QC engine dispatches on. Add a requirement type once (here + in rules_engine)
→ every rule of that type gets a verify recipe for free. NOTHING is hardcoded to
fire ratings or doors; the recipe is derived from the rule's anatomy
(category · parameter · operator/threshold/unit · operand/lookup · pattern).

**Graceful degradation (honesty).** Not every check maps to one native schedule
filter — a regex, a "is it canonical?" test, a duplicate scan, or a cross-element
comparison cannot be expressed as a single Revit view-filter rule. Those recipes
set ``degraded=True`` and lead with the two things that ALWAYS work: the
ElementId "Select by ID" list, and a schedule that lays the operands out
side-by-side so the human does the final comparison. We never over-promise that
"every check = one filter".

Pure module: dataclasses + functions, no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from bim_orchestrator.policies.revit_units import rule_value_to_storage_unit
from bim_orchestrator.report_trace import revit_parameter_of
from bim_orchestrator.state import CheckRecord

# ── Revit view-filter operator vocabulary ───────────────────────────────────
# Revit's "Filter Rules" dialog phrases numeric/string comparisons in words; the
# report quotes the exact phrasing so the user picks the right dropdown entry.
_REVIT_FILTER_OP: dict[str, str] = {
    ">": "is greater than",
    ">=": "is greater than or equal to",
    "<": "is less than",
    "<=": "is less than or equal to",
    "==": "equals",
    "!=": "does not equal",
}
# To highlight the FAILING set, the filter must express the NEGATION of "pass".
_FAIL_OP: dict[str, str] = {
    ">=": "<", ">": "<=", "<=": ">", "<": ">=", "==": "!=", "!=": "==",
}
# v1.5-R6 (3.2): the SAME ">/>=/<=/</==/!=" tokens, translated into the addin's
# ``configure_schedule`` filter-operator vocabulary (revit.py:456-472).
_ADDIN_FILTER_OP: dict[str, str] = {
    ">": "greater", ">=": "greater_equal", "<": "less", "<=": "less_equal",
    "==": "equals", "!=": "not_equals",
}


# ── recipe value objects ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScheduleRecipe:
    """A Revit schedule that reproduces the finding set natively.

    ``fields`` are the schedule columns (identity + the checked parameter +
    operands). ``filters`` (v1.5-R6, 3.2) are structured
    ``{field, operator, value}`` dicts — the exact shape
    ``mcp_clients.revit.configure_schedule`` accepts — that highlight the
    FAIL predicate (e.g. ``has_no_value`` for presence, the negated
    comparison for numeric_compare) when it's expressible; empty when it
    isn't (a regex / canonical / cross-element check, or a numeric rule
    whose parameter has no known Revit storage unit).

    This is a NARROWER promise than the manual recipe in ``narrative``: a
    human building their OWN schedule by hand should leave it UNFILTERED so
    the PASS set stays visible (the false-negative defence) — ``narrative``
    never tells them to add a filter. ``filters`` exists for the OPPOSITE
    consumer: ``verification_views.create_verification_schedules``, which
    auto-builds a companion schedule meant to jump straight to what's
    broken. A Revit schedule filter is non-destructive (removable in the
    UI in one click), so this doesn't compromise the false-negative
    defence — the unfiltered manual schedule (or the report's own PASS
    table) is still one Select-by-ID away.
    """

    category: str
    fields: list[str]
    filters: list[dict[str, Any]] = field(default_factory=list)
    group_or_sort: str | None = None


@dataclass(frozen=True)
class ViewFilterRecipe:
    """A Revit View Filter + colour override that makes the flagged set obvious.

    ``None`` (not this object) is returned when the predicate can't be expressed
    as a native filter rule (regex / canonical / duplicate / cross-element).
    """

    parameter: str
    rule_text: str
    color: str = "Red — solid fill"


@dataclass(frozen=True)
class VerifyRecipe:
    """Everything the renderer needs to write one rule's verify section."""

    schedule: ScheduleRecipe
    view_filter: ViewFilterRecipe | None
    narrative: str
    operand_columns: list[str]
    degraded: bool
    degraded_reason: str | None = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _category(rule: Any, records: Sequence[CheckRecord]) -> str:
    cat = getattr(rule, "category", None)
    if cat:
        return str(cat)
    for r in records:
        if r.get("category"):
            return str(r["category"])
    return "elements"


# Identity columns by category, so the schedule names the element a reviewer can
# find. Most categories use Mark; Rooms/Sheets carry their own identifiers.
_IDENTITY_FIELDS: dict[str, list[str]] = {
    "Rooms": ["Number", "Name"],
    "Spaces": ["Number", "Name"],
    "Sheets": ["Sheet Number", "Sheet Name"],
}
_DEFAULT_IDENTITY = ["Mark", "Family and Type"]


def _identity_fields(category: str) -> list[str]:
    return _IDENTITY_FIELDS.get(category, _DEFAULT_IDENTITY)


def _cols(fields: Sequence[str]) -> str:
    """Render a schedule's column list as inline-code, comma-joined — reads as
    Markdown (`` `Number`, `Name` ``) instead of a raw Python list repr."""
    return ", ".join(f"`{f}`" for f in fields)


def _schedule_fields(category: str, *extra: str) -> list[str]:
    """Identity columns + the checked parameter(s), order-preserving + deduped —
    so a rule on an identity column (e.g. a unique Room ``Number``) doesn't list
    that column twice."""
    out: list[str] = []
    for f in [*_identity_fields(category), *extra]:
        if f not in out:
            out.append(f)
    return out


def _filter_value_text(value: float) -> str:
    """Render a schedule-filter value as a string for the wire.

    Since addin v0.8.23 (2026-08-05) the bridge accepts string OR number and
    builds a typed ``ScheduleFilter`` from either — measured byte-identical
    results (RevitMCPServer's acceptance run). Sending the STRING form is a
    DELIBERATE choice, not a leftover workaround, and the reason is the
    older addins still deployed (R2025 sits on 0.8.20): there a NUMBER kills
    the whole ``configure_schedule`` command — sort fields included, error
    envelope, nothing applied — while a STRING degrades to a per-filter
    warning that the build manifest surfaces honestly. Same result on new
    addins, strictly safer failure on old ones. Don't switch to a float
    return without checking the fleet.

    (History: on v0.8.21 the schema was ``z.string()`` only, and a float
    failed MCP input validation as an opaque ``bad_envelope`` — LIVE
    2026-08-01, the reason this helper exists at all.)

    Full ``str()`` precision on purpose: rounding here would silently move
    the pass/fail boundary of the filter the reviewer is meant to trust.
    Python's ``str()`` always renders with ``.`` (invariant-culture-safe on
    the C# parse side, ``NumberStyles.Float``).
    """
    return str(value)


def _unit_note(rule: Any) -> str:
    unit = getattr(rule, "unit", None)
    if not unit:
        return ""
    return (
        f" The rule's threshold is in **{unit}**; Revit shows the parameter in "
        f"the project's display units — convert before comparing if they differ."
    )


def _param_ref(rule: Any) -> tuple[str, str]:
    """``(field, label)`` for one rule's parameter, bound-parameter-aware.

    ``field`` is what a Revit schedule column / view-filter rule must
    literally be named — always the EFFECTIVE Revit parameter
    (``revit_parameter_of``: ``bound_parameter`` wins), since that's the only
    name that actually exists on the element. ``label`` is the narrative
    phrase: the canonical ``rule.parameter`` alone when the two agree, or
    with a "(Revit param: X)" note appended when they differ — so prose stays
    readable in the rule's own vocabulary while never hiding that the field
    reference underneath differs (v1.5-R6: bound_parameter transparency).
    """
    canonical = getattr(rule, "parameter", "") or ""
    field = revit_parameter_of(rule)
    label = (
        f"`{canonical}`" if field == canonical
        else f"`{canonical}` (Revit param: `{field}`)"
    )
    return field, label


# ── per-requirement renderers ───────────────────────────────────────────────


def _present_and_nonempty(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    fields = _schedule_fields(cat, param)
    # v1.5-R6 (3.2): "has no value" needs no unit/type resolution — always
    # expressible as a real schedule filter.
    filters = [{"field": param, "operator": "has_no_value"}]
    return VerifyRecipe(
        schedule=ScheduleRecipe(category=cat, fields=fields, filters=filters),
        view_filter=ViewFilterRecipe(parameter=param, rule_text="has no value"),
        narrative=(
            f"Create a schedule of **{cat}** with columns {_cols(fields)}. Any **blank "
            f"{label}** cell is a FAIL; a filled cell is a PASS. To see them in "
            f"a view, add a View Filter {label} **has no value** and override "
            f"its colour — every flagged element lights up. Both are 100% native; "
            f"nothing here trusts the tool's log."
        ),
        operand_columns=["value"],
        degraded=False,
    )


def _numeric_compare(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    op = getattr(rule, "operator", None) or ">="
    if rule.requirement == "positive_number":
        op, threshold = ">", 0
    else:
        threshold = getattr(rule, "threshold", None)
    fields = _schedule_fields(cat, param)
    fail_op = _FAIL_OP.get(op, "<")
    fail_text = _REVIT_FILTER_OP.get(fail_op, "is less than")
    pass_text = _REVIT_FILTER_OP.get(op, "is greater than or equal to")
    # v1.5-R6 (1.5): carry the unit into the filter rule text itself — the
    # narrative already had a unit note, but the filter line (quoted verbatim
    # into Revit's dialog) silently assumed the project's display unit matched
    # the rule's unit.
    unit = getattr(rule, "unit", None)
    unit_suffix = f" {unit} (convert to your project's display units)" if unit else ""

    # v1.5-R6 (3.2): a REAL configure_schedule filter, expressed in the
    # parameter's Revit STORAGE unit (not the rule's declared unit — see
    # revit_units.rule_value_to_storage_unit). Skipped (never guessed) when
    # the threshold or the storage unit isn't determinable.
    filters: list[dict[str, Any]] = []
    filter_note = ""
    if threshold is not None:
        try:
            threshold_f = float(threshold)
        except (TypeError, ValueError):
            threshold_f = None
        if threshold_f is not None:
            if unit is None:
                filters = [{
                    "field": param, "operator": _ADDIN_FILTER_OP.get(fail_op, "less"),
                    "value": _filter_value_text(threshold_f),
                }]
            else:
                converted = rule_value_to_storage_unit(param, threshold_f, unit)
                if converted is not None:
                    filter_value, _storage_unit = converted
                    filters = [{
                        "field": param, "operator": _ADDIN_FILTER_OP.get(fail_op, "less"),
                        "value": _filter_value_text(filter_value),
                    }]
                else:
                    filter_note = (
                        f" (auto-created schedule has no filter — `{param}`'s Revit "
                        f"storage unit is unknown, so a **{unit}** threshold can't be "
                        f"converted safely; the manual View Filter above still works.)"
                    )

    return VerifyRecipe(
        schedule=ScheduleRecipe(category=cat, fields=fields, filters=filters),
        view_filter=ViewFilterRecipe(
            parameter=param, rule_text=f"{fail_text} {threshold}{unit_suffix}"
        ),
        narrative=(
            f"Schedule **{cat}** with columns {_cols(fields)}. The rule passes when "
            f"{label} {pass_text} **{threshold}**, so a View Filter {label} "
            f"**{fail_text} {threshold}** + a red override highlights exactly the "
            f"FAIL set. Sort the schedule by {label} to eyeball the boundary."
            + _unit_note(rule) + filter_note
        ),
        operand_columns=["value", "threshold"],
        degraded=False,
    )


def _matches_regex(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    pattern = getattr(rule, "pattern", None)
    negate = rule.requirement == "not_matches_regex"
    fields = _schedule_fields(cat, param)
    verb = "must NOT match" if negate else "must match"
    return VerifyRecipe(
        schedule=ScheduleRecipe(category=cat, fields=fields, group_or_sort=f"Sort by {param}"),
        view_filter=None,
        narrative=(
            f"Schedule **{cat}** with columns {_cols(fields)}, sorted by {label}. The "
            f"value {verb} the pattern `{pattern}`. Revit view filters can't "
            f"evaluate a regex, so verify by eye against the sorted column, then "
            f"confirm the exact flagged set with **Select by ID** (list below). "
            f"The per-element table also shows each value next to the pattern."
        ),
        operand_columns=["value", "pattern"],
        degraded=True,
        degraded_reason="Revit view filters cannot evaluate a regular expression.",
    )


def _canonical_format(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    fields = _schedule_fields(cat, param)
    return VerifyRecipe(
        schedule=ScheduleRecipe(category=cat, fields=fields, group_or_sort=f"Group by {param}"),
        view_filter=None,
        narrative=(
            f"Schedule **{cat}** with columns {_cols(fields)}, grouped by {label} so "
            f"every distinct spelling sits together. The rule passes only when "
            f"{label} is ALREADY in canonical form; the per-element table shows "
            f"each value next to the canonical it should be. \"Is it canonical?\" "
            f"can't be a view-filter rule, so confirm the flagged set with "
            f"**Select by ID** (list below)."
        ),
        operand_columns=["value", "suggested_value"],
        degraded=True,
        degraded_reason="\"Already canonical\" is not expressible as a native filter rule.",
    )


def _unique_in_set(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    fields = _schedule_fields(cat, param)
    return VerifyRecipe(
        schedule=ScheduleRecipe(
            category=cat, fields=fields,
            group_or_sort=f"Sort/Group by {param}, enable Itemize every instance",
        ),
        view_filter=None,
        narrative=(
            f"Schedule **{cat}** with columns {_cols(fields)}; sort or group by {label} "
            f"with *Itemize every instance* on, so duplicate {label} values land "
            f"on adjacent rows — that's the native way to spot collisions. A view "
            f"filter can't express \"appears more than once\", so confirm each "
            f"duplicate group with **Select by ID** (list below)."
        ),
        operand_columns=["value"],
        degraded=True,
        degraded_reason="Uniqueness (\"appears more than once\") is not a per-element filter rule.",
    )


def _relation_compare(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    op = getattr(rule, "operator", None) or ">="
    lookup = getattr(rule, "lookup", None)
    other_param = getattr(rule, "other_param", None)
    fields = _schedule_fields(cat, param)
    if lookup:
        operand_desc = (
            f"the **required** value derived from a code table (`{lookup}`) keyed "
            f"on the related element"
        )
    else:
        operand_desc = f"the related element's `{other_param or 'reference value'}`"
    return VerifyRecipe(
        schedule=ScheduleRecipe(category=cat, fields=fields),
        view_filter=None,
        narrative=(
            f"This is a CROSS-ELEMENT check: {label} {op} {operand_desc}. No "
            f"single native schedule or filter shows both sides at once "
            f"(Revit schedules are one category at a time), so the recipe DEGRADES "
            f"honestly:\n"
            f"  1. **Select by ID** the flagged **{cat}** (list below) — the "
            f"interpretation-free anchor.\n"
            f"  2. With one selected, press **Tab** to highlight its host/related "
            f"element and read that element's value in Properties.\n"
            f"  3. The per-element table below lays the two operands side by side "
            f"(this element's value vs the required value) — **this table shows "
            f"the inputs, not the verdict**; you make the final comparison."
        ),
        operand_columns=["value", "operand"],
        degraded=True,
        degraded_reason=(
            "Cross-element comparison — no single-category schedule/filter shows "
            "both operands."
        ),
    )


_MAX_SUBSET_FILTER_CHAIN = 8


def _value_in_subset(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, label = _param_ref(rule)
    fields = _schedule_fields(cat, param)
    # The allowed set is per-element (explicit `rule.allowed_values` or the
    # classification table keyed by category) — records carry the RESOLVED
    # list as `operand`. Use the first record's operand as a representative
    # sample for the narrative; the per-element table shows each element's
    # own resolved set (it can differ by category when unset on the rule).
    sample = next((r.get("operand") for r in records if r.get("operand")), None)
    allowed_desc = (
        _cols(sample) if isinstance(sample, list) and sample
        else "the element's resolved allowed set (see the per-element table)"
    )
    # v1.5-R6 (3.2): "not one of the allowed set" IS expressible as a native
    # filter — not as ONE rule, but as a CHAIN of `!= <allowed value>` rules
    # ANDed together (configure_schedule's `filters` list is AND-combined):
    # value != A AND value != B AND ... is exactly "not a member of {A, B,
    # ...}", the FAIL predicate. Only when the set is small — an 8-rule AND
    # chain is already pushing it for a human to read back in the Revit UI,
    # and it doesn't help the still-honest degraded narrative below (which
    # covers the general case + explains WHY a single-rule filter can't).
    filters: list[dict[str, Any]] = []
    degraded_reason = (
        "Multi-value membership is not a single native filter rule "
        "(would need one OR'd rule per allowed value)."
    )
    if isinstance(sample, list) and sample:
        if len(sample) <= _MAX_SUBSET_FILTER_CHAIN:
            filters = [
                {"field": param, "operator": "not_equals", "value": v} for v in sample
            ]
            degraded_reason = (
                "Multi-value membership has no single-rule filter, but the "
                f"auto-created schedule's filter chain (`{param} != ` each of the "
                f"{len(sample)} allowed values, ANDed) reproduces the FAIL predicate "
                "natively — see the created schedule."
            )
        else:
            degraded_reason = (
                "Multi-value membership is not a single native filter rule, and the "
                f"allowed set ({len(sample)} values) is too large for a readable "
                f"`!=` filter chain (cap {_MAX_SUBSET_FILTER_CHAIN}) — no filter applied."
            )
    return VerifyRecipe(
        schedule=ScheduleRecipe(
            category=cat, fields=fields, filters=filters, group_or_sort=f"Sort by {param}",
        ),
        view_filter=None,
        narrative=(
            f"Schedule **{cat}** with columns {_cols(fields)}, sorted by {label}. The "
            f"rule passes when {label} is EXACTLY one of the allowed values: "
            f"{allowed_desc}. A native View Filter can express a SINGLE {label} "
            f"**equals** rule but not a multi-value membership test in one rule "
            f"(you'd need one OR'd filter rule per allowed value), so the recipe "
            f"degrades honestly: confirm the flagged set with **Select by ID** "
            f"(list below) — the per-element table shows each value next to its "
            f"resolved allowed set."
        ),
        operand_columns=["value", "operand"],
        degraded=True,
        degraded_reason=degraded_reason,
    )


def _default(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    cat = _category(rule, records)
    param, _label = _param_ref(rule)
    fields = _schedule_fields(cat, param)
    return VerifyRecipe(
        schedule=ScheduleRecipe(category=cat, fields=fields),
        view_filter=None,
        narrative=(
            f"Schedule **{cat}** with columns {_cols(fields)} and confirm the flagged "
            f"set with **Select by ID** (list below). This requirement "
            f"(`{rule.requirement}`) has no specialised native recipe yet, so the "
            f"ElementId list is the source of truth and the per-element table "
            f"shows the values used."
        ),
        operand_columns=["value"],
        degraded=True,
        degraded_reason=f"No specialised recipe for requirement '{rule.requirement}'.",
    )


# ── registry (mirrors rules_engine.evaluate dispatch keys) ───────────────────

REQUIREMENT_RENDERERS: dict[str, Callable[[Any, Sequence[CheckRecord]], VerifyRecipe]] = {
    "present_and_nonempty": _present_and_nonempty,
    # numeric family — all share the threshold/operator recipe
    "numeric_compare": _numeric_compare,
    "numeric_min": _numeric_compare,
    "numeric_min_conditional": _numeric_compare,
    "positive_number": _numeric_compare,
    # pattern family
    "matches_regex": _matches_regex,
    "matches_regex_if_present": _matches_regex,
    "not_matches_regex": _matches_regex,
    # canonical / membership
    "canonical_format": _canonical_format,
    # uniqueness
    "unique_in_set": _unique_in_set,
    # cross-element
    "relation_compare": _relation_compare,
    "fire_rating_ge": _relation_compare,
    # membership (Phase 2 GĐ2)
    "value_in_subset": _value_in_subset,
}


def recipe_for(rule: Any, records: Sequence[CheckRecord]) -> VerifyRecipe:
    """Dispatch ``rule.requirement`` → its verify recipe, with a safe fallback.

    Mirrors ``rules_engine.evaluate``'s dispatch table so the report stays in
    lock-step with the engine: any requirement the engine can evaluate has a
    recipe here, and an unknown one degrades to the ElementId-anchored default
    rather than raising.
    """
    renderer = REQUIREMENT_RENDERERS.get(getattr(rule, "requirement", ""), _default)
    return renderer(rule, records)


__all__ = [
    "REQUIREMENT_RENDERERS",
    "ScheduleRecipe",
    "VerifyRecipe",
    "ViewFilterRecipe",
    "recipe_for",
]
