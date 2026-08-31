"""Fire-rating value normalization.

Revit stores Fire Rating as a free-form string. Walls typically use hours
("2 HR", "4 HR") while doors typically use minutes ("180 MIN", "90 MIN").
Snowdon Towers also uses sentinel "NR" (no rating) on un-rated doors.

This module normalizes those heterogeneous strings to a single canonical
unit — integer minutes — so downstream rule evaluators can compare
walls against doors without re-parsing.

Convention:
    * ``None`` means *no value present* (param missing / blank) OR a value we
        cannot read without guessing. Distinct from 0.
    * ``0``    means *explicitly rated as unrated* ("NR", "None", "0 hr").
        A door labeled "NR" hosted in a 2-HR wall is a violation;
        a door with blank Fire Rating is a different violation
        (missing data, not non-compliant).
    * A bare number WITHOUT a unit ("2", 90, "0.5") is **ambiguous** and
        parses to ``None`` — B-4 (review round 7, 2026-08-16). Walls are
        conventionally authored in hours and doors in minutes, so a wall
        typed "2" almost certainly means 2 HOURS; the old rule ("bare =
        minutes", justified by a "Revit storage convention" that does not
        exist) read it as 2 minutes, which any door trivially satisfies —
        a false negative on the §716 path. Zero is the one unit-independent
        number, so "0"/0 still parse to 0.

Examples
--------
>>> parse_to_minutes("2 HR")
120
>>> parse_to_minutes("180 MIN")
180
>>> parse_to_minutes("NR")
0
>>> parse_to_minutes("")
>>> parse_to_minutes(None)
>>> parse_to_minutes("1.5 hr")
90
"""

from __future__ import annotations

import re
from typing import Final

# Tokens that mean "explicitly rated as unrated / no fire resistance".
# Matched case-insensitively against the trimmed input.
_EXPLICIT_ZERO: Final[frozenset[str]] = frozenset({
    "nr", "n/a", "na", "none", "no", "no rating", "not rated", "-", "--",
})

# Pattern captures: <number> optional whitespace then optional unit token.
# Number can be int or decimal. Unit is hours-flavored or minutes-flavored.
_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^\s*
    (?P<num>\d+(?:\.\d+)?)            # 2, 180, 0.5, 1.5
    \s*[-_ ]?\s*                      # optional separator (hyphen, underscore, space)
    (?P<unit>hours?|hrs?|hr|h|minutes?|mins?|min|m)?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_HOURS_UNITS: Final[frozenset[str]] = frozenset({"hour", "hours", "hr", "hrs", "h"})
_MINUTES_UNITS: Final[frozenset[str]] = frozenset(
    {"minute", "minutes", "min", "mins", "m"}
)


def parse_to_minutes(value: object) -> int | None:
    """Normalize a Fire Rating value to integer minutes.

    Returns:
        ``None`` if value is missing / blank / unparseable.
        ``0`` if value is an explicit no-rating sentinel.
        Otherwise an ``int`` count of minutes.

    Rounding: decimal hours convert by multiplying then truncating to int
    via ``int()`` (toward zero), so ``"0.5 hr"`` → ``30`` and
    ``"1.5 hr"`` → ``90``. Decimal minutes round the same way:
    ``"90.7 MIN"`` → ``90``.

    Bare nonzero numbers (no unit — "2", 90, "0.5") are **ambiguous** and
    return ``None`` — see the module docstring (B-4). Zero is
    unit-independent, so "0"/0/0.0 still return 0. This also closes the
    ``int()``-truncation hole where "0.5" (a real ½-hour wall) collapsed to
    0 minutes and ``is_not_rated`` declared it exempt.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass; reject it explicitly so True doesn't
        # silently mean "1 minute".
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN guard
            return None
        if value == 0:
            return 0  # zero minutes == zero hours — the one unambiguous number
        return None  # B-4: unit-less numeric — hours or minutes? Don't guess.
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.lower() in _EXPLICIT_ZERO:
        return 0

    m = _VALUE_RE.match(text)
    if not m:
        return None

    try:
        num = float(m.group("num"))
    except ValueError:
        return None
    if num < 0:
        return None

    unit = (m.group("unit") or "").lower()
    if unit in _HOURS_UNITS:
        return int(num * 60)
    if unit in _MINUTES_UNITS:
        return int(num)
    # B-4: no unit token. Zero is unit-independent; anything else is a guess
    # between hours (wall convention) and minutes (door convention) → None,
    # so downstream routes to manual_review instead of silently passing.
    return 0 if num == 0 else None


def is_not_rated(value: object) -> bool:
    """True only for an EXPLICIT not-rated sentinel ("NR", "None", "-", 0 hr).

    L-03 (2026-07-25 live review): "not rated" existed twice in the engine
    with no shared definition — ``lookup_table`` knew the sentinel (a
    not-rated host imposes no requirement → exempt) while the ``normalize``
    path did not, so a ``canonical_format`` rule on Fire Rating flagged all 51
    ``NR`` doors in Snowdon as format violations. ``NR`` is a valid BIM
    statement ("this door carries no fire-resistance requirement"), not a
    malformed duration. This is the single definition both sides now use.

    Deliberately three-way, mirroring ``lookup_table``'s M5 reasoning — the
    two NON-sentinel shapes must NOT come back True:
      * blank / None            → False (absent is *missing data*, a different
                                  verdict from "declared as unrated")
      * unparseable non-blank   → False (junk like "2 HR (UL U419)" DOES carry
                                  a rating we simply can't read; calling it
                                  exempt would silently pass a possibly
                                  under-spec element)

    B-4 (2026-08-16) closed a third leak INTO this predicate: "0.5" used to
    parse as 0 minutes (bare-number-as-minutes + ``int()`` truncation), so a
    real ½-hour wall came back True — exempt — and every door in it skipped
    the check. Bare nonzero numbers now parse to None → False here.

    Not for ``relation_compare``: there, an ``NR`` door in a 2-HR wall is a
    real violation (see this module's header). This predicate answers "is the
    VALUE a declaration of no-rating", not "does the element satisfy its host".
    """
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return parse_to_minutes(value) == 0


def format_minutes(minutes: int | None, *, prefer: str = "min") -> str:
    """Inverse of ``parse_to_minutes`` — for write-back / audit strings.

    ``prefer="min"`` always returns ``"<n> MIN"`` (door convention).
    ``prefer="hr"`` returns ``"<n> HR"`` when minutes is a clean multiple
    of 60, else falls back to MIN.

    ``None``  → ``""``  (caller writes empty string to clear the param).
    ``0``     → ``"NR"`` (door sentinel for un-rated).
    """
    if minutes is None:
        return ""
    if minutes == 0:
        return "NR"
    if prefer == "hr" and minutes % 60 == 0:
        return f"{minutes // 60} HR"
    return f"{minutes} MIN"
