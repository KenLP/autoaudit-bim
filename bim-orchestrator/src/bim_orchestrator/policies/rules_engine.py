"""Rule evaluation primitives + Phase 1 inference helpers.

Pure functions — no LLM, no I/O. Each evaluator takes a raw parameter value
and returns True when the value satisfies the rule. `infer_from_name` uses
a hardcoded lookup table for Phase 1; LLM-driven inference is a Phase 2
upgrade deferred to the Grounding Agent.
"""

from __future__ import annotations

import re
from typing import Any

# Substring keywords (lowercase) → OccupancyType classification.
# Order matters: longer/more-specific terms first so e.g. "parking" beats
# "park" and "machine" beats "mech".
#
# Extended in Phase 2 Week 6 Day 4 to cover Snowdon Towers vocabulary:
# café, studio, live/work, loft, commercial, retail, patio, park,
# green roof, elevator, parking, machine, bandstand — so the
# Department auto-fill produces meaningful values on the demo project.
_NAME_OCCUPANCY_LOOKUP: tuple[tuple[str, str], ...] = (
    ("powder", "Wet"),
    ("bathroom", "Wet"),
    ("bath", "Wet"),
    ("toilet", "Wet"),
    ("laundry", "Wet"),
    ("wc", "Wet"),
    # Storage / Services
    ("closet", "Storage"),
    ("storage", "Storage"),
    ("pantry", "Storage"),
    ("machine", "Services"),  # before "mech" so "Machine RM" → Services
    ("mech", "Services"),
    ("utility", "Services"),
    # Parking — must come before "park"/"garage"
    ("parking", "Parking"),
    ("garage", "Parking"),
    # Circulation
    ("entry", "Circulation"),
    ("foyer", "Circulation"),
    ("corridor", "Circulation"),
    ("hallway", "Circulation"),
    ("hall", "Circulation"),
    ("stair", "Circulation"),
    ("elevator", "Circulation"),
    ("lobby", "Circulation"),
    # Hospitality / Cooking
    ("café", "Hospitality"),
    ("cafe", "Hospitality"),       # ASCII fallback
    ("kitchen", "Cooking"),
    ("dining", "Hospitality"),
    # Living spaces
    ("living", "Living"),
    ("family", "Living"),
    ("nook", "Living"),
    ("great room", "Living"),
    # Sleeping / Residential units (Snowdon)
    ("bedroom", "Sleeping"),
    ("master", "Sleeping"),
    ("bed", "Sleeping"),
    ("live/work", "Residential"),
    ("loft", "Residential"),
    ("studio", "Residential"),
    # Work / Office
    ("office", "Office"),
    ("study", "Office"),
    ("den", "Office"),
    # Commercial
    ("commercial", "Commercial"),
    ("retail", "Commercial"),
    # Outdoor / Landscape
    ("green roof", "Outdoor"),
    ("patio", "Outdoor"),
    ("park", "Outdoor"),           # after "parking"
    ("outdoor", "Outdoor"),
    ("bandstand", "Recreation"),
)


def present_and_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def positive_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def matches_regex(value: Any, pattern: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(pattern, value) is not None


def not_matches_regex(value: Any, pattern: str) -> bool:
    """Inverse of matches_regex. Uses re.search (substring), not fullmatch.

    Semantics: rule PASSES when the pattern is NOT found in the value.
    Null/missing values pass (use present_and_nonempty for required fields).

    Useful for "must not contain placeholder", "must not contain digits", etc.
    """
    if not isinstance(value, str):
        return True  # missing value can't violate a "must not contain X" rule
    return re.search(pattern, value) is None


def numeric_min(value: Any, threshold: float) -> bool:
    """Rule PASSES when ``value`` is a finite number ≥ ``threshold``.

    Null / non-numeric / bool / NaN values fail — use this only on
    parameters that ought to carry a numeric reading (e.g. Area, height).
    """
    if value is None or isinstance(value, bool):
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if f != f:  # NaN
        return False
    return f >= float(threshold)


def numeric_min_conditional(
    value: Any,
    threshold: float,
    *,
    condition_value: Any,
    when_pattern: str,
) -> bool:
    """Gated numeric_min: apply only when ``condition_value`` matches ``when_pattern``.

    Rule PASSES when:
      * the condition value is not a string, or
      * the pattern is NOT found in the condition value (rule out of scope), or
      * the value is a finite number ≥ ``threshold`` (rule satisfied).

    Used for "if Occupancy ~= 'Residential.*', then Area ≥ 10 m²" style rules.
    Out-of-scope elements quietly pass — they are tested by other rules.
    """
    if not isinstance(condition_value, str):
        return True
    if re.search(when_pattern, condition_value) is None:
        return True
    return numeric_min(value, threshold)


def fire_rating_ge(value: Any, other_value: Any) -> bool:
    """Rule PASSES when ``value`` fire-rating is at least ``other_value``'s.

    Both inputs are normalized via ``parse_to_minutes`` so wall "2 HR" and
    door "120 MIN" compare correctly. Out-of-scope rules:

      * ``other_value`` is missing (None) → reference unrated → rule passes
        (no fire rating to match — caller should pair with a separate
        "host must be rated" rule if that's a concern).
      * ``other_value`` parses to 0 ("NR") → same as above — host unrated.
      * ``value`` is missing while ``other_value`` is set → FAIL
        (caller has an unrated element in a fire-rated host).

    Both must satisfy ``value_minutes >= other_minutes`` for non-trivial
    other_values to pass.
    """
    # Local import keeps this module dependency-light and avoids a cycle
    # if rules_engine is ever imported from fire_rating_units (it isn't
    # today, but defensive).
    from bim_orchestrator.policies.fire_rating_units import parse_to_minutes

    other_min = parse_to_minutes(other_value)
    if other_min is None or other_min == 0:
        # No reference rating to enforce against.
        return True
    value_min = parse_to_minutes(value)
    if value_min is None:
        # Reference is rated, but our value is missing → violation.
        return False
    return value_min >= other_min


def unique_in_set(value: Any, siblings: list[Any]) -> bool:
    """Rule PASSES when ``value`` appears at most once across ``siblings``.

    Empty / null values are treated as non-applicable (pass) so that
    "missing" doesn't get mis-flagged as "duplicate" — pair with
    ``present_and_nonempty`` if you need both checks.

    ``siblings`` should include the element's own value (the QC agent
    pre-builds the full list from all in-scope elements, so the value
    being checked counts once toward its own duplicate detection).
    """
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    occurrences = sum(1 for s in siblings if s == value)
    return occurrences <= 1


def value_in_subset(value: Any, subset: list[Any]) -> bool:
    """Rule PASSES when ``value`` is EXACTLY one of ``subset`` (trim-tolerant
    string compare). A blank/None value is non-applicable (pass) — pair with
    ``present_and_nonempty`` if the field is also required. An empty subset never
    passes a present value (nothing is allowed).

    Phase 2 GĐ2: the caller resolves ``subset`` per element (e.g. the valid
    classification codes for the element's category) — turning a slice of
    "meaning" into a deterministic membership check.
    """
    if value is None:
        return True
    sval = str(value).strip()
    if not sval:
        return True
    allowed = {str(s).strip() for s in (subset or [])}
    return sval in allowed


def infer_from_name(room_name: str | None) -> str | None:
    """Map a Revit Room name to an OccupancyType keyword via word-boundary lookup.

    Phase 1: hardcoded table. Returns None when no keyword matches — caller
    decides whether to use the rule's `fallback` or escalate.

    Low5: matched by ``\\b<keyword>\\b`` (case-insensitive), not plain
    substring — a bare ``in`` check let "den" match inside "Garden"
    (→ wrongly inferred "Office"), "park" inside "Parking" before its own
    entry ran, etc. Word-boundary keeps multi-word keywords ("great room",
    "live/work") working unchanged since non-word characters (space, "/")
    are themselves boundaries.
    """
    if not room_name:
        return None
    lowered = room_name.lower()
    for keyword, occupancy in _NAME_OCCUPANCY_LOOKUP:
        if re.search(rf"\b{re.escape(keyword)}\b", lowered):
            return occupancy
    return None


# v1.4-K10: comparison operators for numeric_compare / relation_compare.
_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def numeric_compare(value: Any, threshold: float, operator: str = ">=") -> bool:
    """Generic numeric comparison: ``value <operator> threshold`` (v1.4-K10).

    Subsumes ``positive_number`` (operator='>', threshold=0) and ``numeric_min``
    (operator='>='). Null / non-numeric / bool / NaN fail (no number to compare).
    """
    fn = _OPS.get(operator or ">=")
    if fn is None:
        raise ValueError(f"Unknown operator: {operator}")
    if value is None or isinstance(value, bool):
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if f != f:  # NaN
        return False
    return bool(fn(f, float(threshold)))


def relation_compare(
    value: Any,
    other_value: Any,
    *,
    operator: str = ">=",
    compare_kind: str = "numeric",
) -> bool:
    """Compare this element's value to a RELATED element's value (v1.4-K10).

    Generalises ``fire_rating_ge`` (which is ``compare_kind='fire_rating'`` +
    ``operator='>='``). ``other_value`` is read by QCAgent from ``other_param``
    (e.g. ``host.Fire Rating``). When the reference value is missing the rule is
    out of scope → passes (no reference to enforce), matching fire_rating_ge.
    """
    fn = _OPS.get(operator or ">=")
    if fn is None:
        raise ValueError(f"Unknown operator: {operator}")
    kind = compare_kind or "numeric"
    if kind == "fire_rating":
        from bim_orchestrator.policies.fire_rating_units import parse_to_minutes
        other_min = parse_to_minutes(other_value)
        if other_min is None or other_min == 0:
            return True  # no reference rating to enforce against
        value_min = parse_to_minutes(value)
        if value_min is None:
            return False  # unrated element in a rated context
        return bool(fn(value_min, other_min))
    if kind == "string":
        a = "" if value is None else str(value).strip()
        b = "" if other_value is None else str(other_value).strip()
        return bool(fn(a, b))
    # numeric
    if other_value is None:
        return True  # no reference value to enforce
    try:
        return bool(fn(float(value), float(other_value)))
    except (TypeError, ValueError):
        return False


def evaluate(
    requirement: str,
    value: Any,
    *,
    pattern: str | None = None,
    threshold: float | None = None,
    condition_value: Any = None,
    when_pattern: str | None = None,
    siblings: list[Any] | None = None,
    other_value: Any = None,
    operator: str | None = None,
    compare_kind: str | None = None,
    subset: list[Any] | None = None,
) -> bool:
    """Dispatch table for rule.requirement → evaluator.

    ``pattern`` — used by matches_regex / not_matches_regex.
    ``threshold`` — used by numeric_min / numeric_min_conditional.
    ``condition_value`` + ``when_pattern`` — used by numeric_min_conditional.
    ``siblings`` — used by unique_in_set; the QCAgent pre-builds the list of
    all in-scope values for the rule's parameter.
    ``other_value`` — used by fire_rating_ge for cross-element comparisons
    (e.g. door rating vs host wall rating).
    """
    if requirement == "present_and_nonempty":
        return present_and_nonempty(value)
    if requirement == "positive_number":
        return positive_number(value)
    if requirement == "matches_regex":
        if pattern is None:
            raise ValueError("matches_regex requires a pattern")
        return matches_regex(value, pattern)
    if requirement == "matches_regex_if_present":
        # Format check that only applies to PRESENT values — a missing/blank
        # value is compliant (absence is a separate present_and_nonempty
        # concern, not a format violation). Used by normalize-style rules so
        # blank params don't flood Path A.
        if pattern is None:
            raise ValueError("matches_regex_if_present requires a pattern")
        if value is None or (isinstance(value, str) and not value.strip()):
            return True
        return matches_regex(value, pattern)
    if requirement == "not_matches_regex":
        if pattern is None:
            raise ValueError("not_matches_regex requires a pattern")
        return not_matches_regex(value, pattern)
    if requirement == "numeric_min":
        if threshold is None:
            raise ValueError("numeric_min requires a threshold")
        return numeric_min(value, threshold)
    if requirement == "numeric_min_conditional":
        if threshold is None or when_pattern is None:
            raise ValueError(
                "numeric_min_conditional requires threshold and when_pattern"
            )
        return numeric_min_conditional(
            value,
            threshold,
            condition_value=condition_value,
            when_pattern=when_pattern,
        )
    if requirement == "unique_in_set":
        if siblings is None:
            raise ValueError("unique_in_set requires siblings")
        return unique_in_set(value, siblings)
    if requirement == "value_in_subset":
        if subset is None:
            raise ValueError("value_in_subset requires subset")
        return value_in_subset(value, subset)
    if requirement == "fire_rating_ge":
        return fire_rating_ge(value, other_value)
    if requirement == "numeric_compare":
        if threshold is None:
            raise ValueError("numeric_compare requires a threshold")
        return numeric_compare(value, threshold, operator or ">=")
    if requirement == "relation_compare":
        return relation_compare(
            value, other_value,
            operator=operator or ">=",
            compare_kind=compare_kind or "numeric",
        )
    if requirement == "canonical_format":
        # v1.4-K12: QCAgent normally handles this (it has the normalizer + the
        # element). Here ``other_value`` carries the pre-computed canonical form:
        # compliant iff the value already equals it. Unparseable (None) → fail.
        return other_value is not None and str(value).strip() == str(other_value).strip()
    raise ValueError(f"Unknown requirement: {requirement}")
