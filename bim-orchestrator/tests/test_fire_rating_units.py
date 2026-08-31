"""Tests for the Fire Rating value normalizer (Phase 2 Week 7 Day 1).

Snowdon Towers ships walls labeled "2 HR" / "4 HR" and doors labeled
"180 MIN" / "NR". The normalizer collapses both unit families to integer
minutes so cross-element rules (door must match host wall) can compare
apples to apples.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.policies.fire_rating_units import (
    format_minutes,
    is_not_rated,
    parse_to_minutes,
)


class TestParseToMinutes:
    # ── Hour-flavored inputs (wall convention) ──────────────────────────
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2 HR", 120),
            ("4 HR", 240),
            ("2hr", 120),
            ("2 hr", 120),
            ("2 hours", 120),
            ("2 Hour", 120),
            ("1H", 60),
            ("3-HR", 180),         # Revit sometimes uses hyphen
            ("1.5 hr", 90),
            ("0.5 hour", 30),
        ],
    )
    def test_hours(self, value, expected):
        assert parse_to_minutes(value) == expected

    # ── Minute-flavored inputs (door convention) ────────────────────────
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("180 MIN", 180),
            ("180MIN", 180),
            ("180 min", 180),
            ("90 minutes", 90),
            ("45 m", 45),
            ("20 MINS", 20),
        ],
    )
    def test_minutes(self, value, expected):
        assert parse_to_minutes(value) == expected

    # ── Explicit "no rating" sentinels ──────────────────────────────────
    @pytest.mark.parametrize(
        "value",
        ["NR", "nr", "N/A", "na", "None", "none", "no rating", "Not Rated", "-", "--"],
    )
    def test_explicit_zero(self, value):
        # Distinct from None — the param IS populated, just with a
        # "no rating" sentinel. Rule writers can decide if 0 is a
        # violation or just the lower bound.
        assert parse_to_minutes(value) == 0

    # ── Missing / unparseable inputs ────────────────────────────────────
    @pytest.mark.parametrize(
        "value",
        [None, "", "  ", "\t\n", "garbage", "TBD", "see schedule"],
    )
    def test_missing_or_unparseable(self, value):
        assert parse_to_minutes(value) is None

    # ── Bare numbers — ambiguous, never guessed (B-4, 2026-08-16) ───────
    @pytest.mark.parametrize(
        "value",
        ["90", "180", "2", "0.5", 90, 90.0, 90.7, 2, 0.5],
    )
    def test_bare_nonzero_numbers_are_ambiguous(self, value):
        """A unit-less number is a guess between hours (wall convention) and
        minutes (door convention) — a wall typed "2" means 2 HOURS, and the
        old bare=minutes rule read it as 2 minutes, which any door trivially
        satisfies. None routes downstream to manual_review instead."""
        assert parse_to_minutes(value) is None

    @pytest.mark.parametrize("value", ["0", 0, 0.0])
    def test_bare_zero_is_unit_independent(self, value):
        # 0 minutes == 0 hours — the one number a missing unit cannot distort.
        assert parse_to_minutes(value) == 0

    def test_half_hour_wall_is_not_exempt(self):
        """The compound hole B-4 closed: "0.5" parsed as 0 minutes (bare →
        minutes, then int() truncation) → is_not_rated said True → a real
        ½-hour wall became exempt and every door in it skipped §716."""
        assert is_not_rated("0.5") is False
        assert is_not_rated(0.5) is False
        # The genuine sentinels still register.
        assert is_not_rated("NR") is True
        assert is_not_rated("0 hr") is True

    # ── Edge cases / safety ─────────────────────────────────────────────
    def test_negative_rejected(self):
        assert parse_to_minutes("-30 MIN") is None
        assert parse_to_minutes(-90) is None

    def test_nan_rejected(self):
        assert parse_to_minutes(float("nan")) is None

    def test_bool_not_treated_as_int(self):
        # True is an int subclass; without explicit guard it would be
        # accepted as "1 minute" which is nonsense for fire ratings.
        assert parse_to_minutes(True) is None
        assert parse_to_minutes(False) is None

    def test_list_and_dict_rejected(self):
        assert parse_to_minutes([60]) is None
        assert parse_to_minutes({"min": 60}) is None

    def test_whitespace_stripped(self):
        assert parse_to_minutes("  180 MIN  ") == 180
        assert parse_to_minutes("\t2 HR\n") == 120


class TestFormatMinutes:
    def test_none_to_empty(self):
        assert format_minutes(None) == ""

    def test_zero_to_NR(self):
        assert format_minutes(0) == "NR"

    @pytest.mark.parametrize(
        "minutes,expected",
        [
            (60, "60 MIN"),
            (90, "90 MIN"),
            (180, "180 MIN"),
            (45, "45 MIN"),
        ],
    )
    def test_default_min_format(self, minutes, expected):
        assert format_minutes(minutes) == expected

    @pytest.mark.parametrize(
        "minutes,expected",
        [
            (60, "1 HR"),
            (120, "2 HR"),
            (180, "3 HR"),
            (240, "4 HR"),
        ],
    )
    def test_hr_format_clean_multiples(self, minutes, expected):
        assert format_minutes(minutes, prefer="hr") == expected

    def test_hr_format_falls_back_to_min_for_non_multiples(self):
        # 90 minutes ≠ clean HR multiple → fallback
        assert format_minutes(90, prefer="hr") == "90 MIN"
        assert format_minutes(45, prefer="hr") == "45 MIN"

    def test_roundtrip_preserves_value(self):
        # Roundtrip via Snowdon samples
        for snowdon_value in ["2 HR", "4 HR", "180 MIN", "NR"]:
            mins = parse_to_minutes(snowdon_value)
            assert mins is not None  # all Snowdon samples are valid
            formatted = format_minutes(mins, prefer="hr" if "HR" in snowdon_value else "min")
            assert parse_to_minutes(formatted) == mins
