"""Value normalizers (v1.4-K5..K13) — deterministic format canonicalisation.

Used by the ``normalize`` autofill strategy: a parameter whose value is present
but in a non-canonical format (e.g. fire rating "180 MIN" / "2 HR") is converted
to the firm-standard form. This is a deterministic equivalence conversion — NOT
a guess — so it is safe to auto-apply (subject to the autonomy gate; a
safety-critical param like fire rating still routes to approve). Returns None
when the value can't be parsed → the finding falls back to a Path A ACC Issue
rather than writing garbage.

v1.4-K13 — generalised from two hard-wired kinds (fire_rating + family_name) to
a **unit registry**. A *quantity* value is a (magnitude, unit) pair in some
DIMENSION (duration, length, area, …); the canonical form is just that quantity
RENDERED in a declared output format/unit. So fire-rating is no longer special —
it is ``dimension=duration`` and the format picks the unit (minutes vs hours).
This covers "what if it's a different unit?" (add a dimension/alias, no code
branch) and "what if it's fixed text?" (``map`` kind = enumerated lookup).

Pure module — no I/O. ``agents/`` may import it; it imports nothing from agents.
"""

from __future__ import annotations

import re
from typing import Any

# ── Unit registry ──────────────────────────────────────────────────────────
# Each DIMENSION declares:
#   parse  : {unit-alias(lower) -> factor to the dimension's BASE unit}
#   render : {format-token       -> base-units PER token}  (value_in_token =
#            base / factor); a token is what you write inside {..} in the format.
#   default: the canonical format used when the rule omits ``normalize_format``.
# Adding a unit/dimension is DATA, never a new code branch. Base units:
# duration=minute, length=millimetre, area=m².
_DIMENSIONS: dict[str, dict[str, Any]] = {
    "duration": {
        "parse": {
            "min": 1, "mins": 1, "minute": 1, "minutes": 1,
            "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
        },
        "render": {"m": 1, "min": 1, "h": 60, "hr": 60},
        "default": "{h}-hour",
        # A-4b (review round 7; owner decision Ken 2026-08-17): an HOUR token
        # may only render LOSSLESSLY — the rendered magnitude must parse back
        # to the exact base minutes. "20 MIN" through "{h} HR" used to produce
        # "0.33 HR", which round-trips to 19.8 min — the value silently
        # drifted. Clean fractions are fine and stay: 90 min → "1.5 HR",
        # 45 min → "0.75 HR", 30 min → "0.5 HR" (Ken: those labels are
        # acceptable; only the lossy render is the defect). When a
        # `lossless_tokens` token would lose information, the whole format is
        # swapped for `fallback` (base-unit rendering): "20 MIN" stays
        # "20 MIN". Declared as DATA per the K13 rule (no kind branches);
        # dimensions without `lossless_tokens` (length/area) are untouched.
        "lossless_tokens": frozenset({"h", "hr"}),
        "fallback": "{m} MIN",
    },
    "length": {
        "parse": {
            "mm": 1, "millimeter": 1, "millimeters": 1,
            "millimetre": 1, "millimetres": 1,
            "cm": 10, "centimeter": 10, "centimeters": 10,
            "m": 1000, "meter": 1000, "meters": 1000,
            "metre": 1000, "metres": 1000,
        },
        "render": {"mm": 1, "cm": 10, "m": 1000},
        "default": "{mm} mm",
    },
    "area": {
        "parse": {
            "m2": 1, "m²": 1, "sqm": 1, "sm": 1,
            "ft2": 0.09290304, "sf": 0.09290304, "sqft": 0.09290304,
        },
        "render": {"m2": 1, "sqm": 1, "ft2": 0.09290304, "sf": 0.09290304},
        "default": "{m2} m²",
    },
}

# number  [optional - _ space sep]  unit.  Accepts "180 MIN", "1.5hr", "3-hour",
# "2400mm", "5 m", "12.5 m²". The separator-tolerant form lets a canonical value
# (e.g. "3-hour") round-trip through its OWN normalizer — the old regex couldn't.
_QUANTITY = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[-_ ]?\s*([A-Za-z0-9²]+)\s*$")
_TOKEN = re.compile(r"\{(\w+)\}")


def _trim_num(x: float) -> Any:
    """3.0 → 3, 1.5 → 1.5 (trim trailing .0 so "3-hour" not "3.0-hour")."""
    return int(x) if float(x) == int(x) else round(float(x), 2)


def _render(fmt: str, base: float, render_table: dict[str, float]) -> str | None:
    """Substitute every ``{token}`` with ``base / render_table[token]`` (trimmed).

    A token not in the dimension's render table → None (don't emit a half-
    substituted string). Literal text in ``fmt`` is preserved verbatim, so
    "{m} Min" / "{mm} mm" / "{m2} m²" all work.
    """
    miss: list[str] = []

    def repl(mo: "re.Match[str]") -> str:
        tok = mo.group(1)
        factor = render_table.get(tok)
        if factor is None:
            miss.append(tok)
            return ""
        return str(_trim_num(base / factor))

    out = _TOKEN.sub(repl, fmt)
    return None if miss else out


def normalize_quantity(value: Any, dimension: str, fmt: str | None = None) -> str | None:
    """Parse a (magnitude, unit) quantity, then RENDER it in ``fmt`` (v1.4-K13).

    PARSE recognises any unit alias registered for ``dimension`` (case-
    insensitive); RENDER substitutes the format tokens for that dimension. The
    output unit is chosen by the format, so the same value yields different
    canonical strings:

        duration  "180 MIN" + "{m} Min"  → "180 Min"   + "{h} HR" → "3 HR"
        length    "2.4 m"    + "{mm} mm"  → "2400 mm"
        area      "120 sf"   + "{m2} m²"  → "11.15 m²"

    Unparseable input / unknown unit / unknown token → None (escalate, don't
    write). Deterministic, no LLM.
    """
    dim = _DIMENSIONS.get(dimension)
    if dim is None or not isinstance(value, str):
        return None
    m = _QUANTITY.match(value)
    if not m:
        return None
    factor = dim["parse"].get(m.group(2).lower())
    if factor is None:
        return None
    base = float(m.group(1)) * factor
    chosen = fmt or dim["default"]
    # A-4b: a token in the dimension's `lossless_tokens` may only render when
    # the TRIMMED magnitude parses back to the exact base value ("0.75 HR"
    # → 45 min: keep; "0.33 HR" → 19.8 min ≠ 20: information lost) — a lossy
    # result swaps the WHOLE format for the dimension's `fallback` (see the
    # registry entry for the rationale + the owner decision).
    lossless: frozenset[str] = dim.get("lossless_tokens", frozenset())
    if lossless:
        for tok in _TOKEN.findall(chosen):
            f = dim["render"].get(tok)
            if tok in lossless and f is not None:
                if float(_trim_num(base / f)) * f != base:
                    chosen = dim["fallback"]
                    break
    return _render(chosen, base, dim["render"])


def normalize_fire_rating(value: Any, fmt: str = "{h}-hour") -> str | None:
    """Fire rating is just a DURATION (v1.4-K13 — delegates to the registry).

    Kept as a named entry point for back-compat. PARSE accepts min/hr spellings;
    RENDER tokens ``{h}`` (hours) / ``{m}`` (minutes): "{h}-hour"→"3-hour",
    "{h} HR"→"3 HR", "{m} MIN"→"180 MIN". Non-duration ("NR") → None.
    A-4b: an hour token renders only LOSSLESSLY — "90 MIN"→"1.5 HR" and
    "45 MIN"→"0.75 HR" round-trip exactly and stay, but "20 MIN" would become
    "0.33 HR" (19.8 min — value drift) so it falls back to "20 MIN".
    """
    return normalize_quantity(value, "duration", fmt)


# Runs of whitespace and/or hyphens (the common "wrong separator" violations)
# collapse to a single underscore. Repeated underscores also collapse.
_SEPARATORS = re.compile(r"[\s\-]+")
_MULTI_UNDERSCORE = re.compile(r"_+")


def _slug(s: str) -> str:
    """Collapse runs of whitespace/hyphens (and repeats) to a single underscore.

    "Dining Round w Chairs" → "Dining_Round_w_Chairs"; "A--B" → "A_B". The shared
    separator-canonicaliser for family names and template tokens.
    """
    return _MULTI_UNDERSCORE.sub("_", _SEPARATORS.sub("_", s.strip())).strip("_")


def normalize_family_name(value: Any) -> str | None:
    """Canonicalise a family/type name's SEPARATORS to underscores (v1.4-K9).

    Fixes the deterministic *format* class of naming violations — wrong
    separators / stray whitespace — without inventing semantics:
      "ADSK Fur Chair Viper" → "ADSK_Fur_Chair_Viper"
      "ADSK-Fur-Chair-Viper" → "ADSK_Fur_Chair_Viper"
      "ADSK_Fur__Chair_Viper" → "ADSK_Fur_Chair_Viper"

    Returns None when there is nothing to deterministically fix (the cleaned
    name equals the input — e.g. a *semantic* violation like "Door1" that we
    can't safely rename) so the finding falls back to a Path A ACC Issue for a
    human to name. Never guesses a token the input didn't already contain.
    """
    if not isinstance(value, str):
        return None
    cleaned = _slug(value)
    if not cleaned or cleaned == value:
        return None
    return cleaned


_NUMBERED_FIELD = re.compile(r"\{(\d+)\}")


def normalize_template(value: Any, source: str | None, fmt: str | None) -> str | None:
    """Capture→render: parse ``value`` with a regex, rebuild it from a template.

    The general deterministic naming-convention transform (v1.4-K15). ``source``
    is a regex with **named** (preferred) or numbered capture groups; ``fmt`` is a
    template that references them. It restructures a name that already CONTAINS
    the needed tokens — reorder, re-separator, add a fixed prefix:

        value  "ADSK Fur Chair Viper"
        source r"(?i)^adsk[ _-]*fur[ _-]*(?P<fn>[a-z]+)[ _-]*(?P<d1>[a-z0-9]+)"
        fmt    "ADSK_Fur_{fn}_{d1}"          → "ADSK_Fur_Chair_Viper"

    Each captured token is **slugged** (v1.4-K15) — inner whitespace/hyphens
    collapse to single underscores — so a multi-word description becomes one
    clean token: "Dining Round w Chairs" → "Dining_Round_w_Chairs" (so
    "M_Table-Dining Round w Chairs" → "ADSK_Fur_Table_Dining_Round_w_Chairs",
    no stray spaces to fail the pattern). Casing is preserved (deterministic —
    we never guess Title-case).

    Named groups are exposed as ``{name}``; numbered groups also as ``{g1}``,
    ``{g2}``, …. If the value doesn't match, or the template references a group
    that didn't capture, returns None → the finding escalates to a Path A ACC
    Issue (a human, since the tokens to build the name aren't present). This is
    the deterministic CEILING: it cannot invent a token the input lacks.
    """
    if not isinstance(value, str) or not source or not fmt:
        return None
    try:
        m = re.search(source, value)
    except re.error:
        return None
    if not m:
        return None
    groups: dict[str, str] = {k: _slug(v) for k, v in m.groupdict().items() if v is not None}
    for i, g in enumerate(m.groups(), 1):
        if g is not None:
            groups.setdefault(f"g{i}", _slug(g))
    # An author -- or the Rule Builder's model -- naturally writes "{1}" for the
    # first numbered group; "{g1}" is OUR spelling and nobody would guess it.
    # str.format_map parses "{1}" as a POSITIONAL field and raises ValueError,
    # so the whole template silently yielded no fix: a rule whose YAML looked
    # perfect (fixability: auto, normalize_kind: template, remediation
    # rename_element) proposed NOTHING, with no error anywhere. Measured
    # 2026-08-25: the Rule Builder emitted "{1}" on one of three drafts of the
    # same sentence, so this is a coin-flip failure, not a corner case.
    # Accept the natural spelling by translating it to ours.
    fmt = _NUMBERED_FIELD.sub(r"{g\1}", fmt)
    try:
        out = fmt.format_map(groups)
    except (KeyError, IndexError, ValueError):
        return None
    return out or None


def normalize_map(value: Any, mapping: dict[str, str] | None) -> str | None:
    """Enumerated canonicalisation (v1.4-K13) — fixed text, not a quantity.

    ``mapping`` is {accepted-variant → canonical}; the lookup is case- and
    whitespace-insensitive. Handles "what if the canonical is a fixed string?":
      {"nr": "Not Rated", "n/r": "Not Rated", "0": "Not Rated"}  "N/R" → "Not Rated"
    A value already equal to a canonical target maps to itself (so the
    canonical_format check sees it as compliant). Miss → None → Path A.
    """
    if value is None or not mapping:
        return None
    key = str(value).strip().lower()
    # Medium: lower-case the MAP KEYS too, so the lookup is truly case-insensitive
    # regardless of how the author wrote the variant (e.g. {"NR": "Not Rated"}
    # previously missed "nr" because only the input value was lower-cased).
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    # direct variant hit, or already-canonical (value matches a target verbatim)
    if key in lowered:
        return lowered[key]
    for canonical in mapping.values():
        if key == str(canonical).strip().lower():
            return canonical
    return None


# Text-only normalizers (no magnitude) keyed by name.
_TEXT_NORMALIZERS = {
    "family_name": normalize_family_name,
}


def normalize_value(
    value: Any,
    kind: str,
    fmt: str | None = None,
    mapping: dict[str, str] | None = None,
    source: str | None = None,
) -> str | None:
    """Dispatch to the right normalizer by ``kind``. Unknown kind → None.

    - quantity kinds (``duration``/``length``/``area`` + the ``fire_rating``
      alias) parse a magnitude+unit and render ``fmt``;
    - ``template`` parses ``value`` with the ``source`` regex and renders ``fmt``
      from its capture groups (general naming transform);
    - ``map`` looks ``value`` up in ``mapping`` (fixed/enumerated text);
    - text kinds (``family_name``) ignore the other args.
    """
    if kind == "map":
        return normalize_map(value, mapping)
    if kind == "template":
        return normalize_template(value, source, fmt)
    if kind == "fire_rating":
        return normalize_quantity(value, "duration", fmt)
    if kind in _DIMENSIONS:
        return normalize_quantity(value, kind, fmt)
    fn = _TEXT_NORMALIZERS.get(kind)
    return fn(value) if fn is not None else None


# Common output formats to TRY per dimension in "auto" mode (v1.4-K16). The
# engine renders every candidate and the caller keeps whichever satisfies the
# rule's own pattern — so a matches_regex rule needs NO kind/format declaration.
_AUTO_FORMATS: dict[str, tuple[str, ...]] = {
    "duration": ("{h} HR", "{h}-hour", "{h} hr", "{h} hour",
                 "{m} Min", "{m} MIN", "{m} min"),
    "length":   ("{mm} mm", "{m} m", "{cm} cm"),
    "area":     ("{m2} m²", "{m2} m2", "{m2} sqm"),
}


def auto_candidates(value: Any) -> list[str]:
    """All deterministic canonical forms of ``value`` (kind-agnostic) — for auto.

    Tries every quantity dimension in several common output formats, plus the
    family-name separator fix. The caller (QC) picks whichever output satisfies
    the rule's check (pattern), so the rule author declares NOTHING but the
    pattern. Covers units + separators; ``template``/``map`` are NOT here (they
    need a parse-regex / lookup-table the pattern can't supply). Deduped, ordered.
    """
    out: list[str] = []
    for dim, fmts in _AUTO_FORMATS.items():
        for f in fmts:
            v = normalize_quantity(value, dim, f)
            if v is not None:
                out.append(v)
    fam = normalize_family_name(value)
    if fam is not None:
        out.append(fam)
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


# Dimensions + the non-quantity kinds the Rule Builder offers, for UI listing.
QUANTITY_DIMENSIONS = tuple(_DIMENSIONS.keys())
NORMALIZE_KINDS = (
    "auto", "duration", "length", "area", "fire_rating",
    "family_name", "template", "map", "reference",
)
