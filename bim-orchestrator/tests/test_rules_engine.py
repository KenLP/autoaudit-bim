"""Unit tests for the deterministic rule evaluators."""

from __future__ import annotations

import pytest

from bim_orchestrator.policies.rules_engine import (
    evaluate,
    infer_from_name,
    matches_regex,
    not_matches_regex,
    numeric_min,
    numeric_min_conditional,
    positive_number,
    present_and_nonempty,
    unique_in_set,
)


class TestPresentAndNonempty:
    @pytest.mark.parametrize("value", ["Storage", " x ", 0, 1.5, ["a"], True])
    def test_truthy(self, value):
        assert present_and_nonempty(value) is True

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_falsy(self, value):
        assert present_and_nonempty(value) is False


class TestPositiveNumber:
    @pytest.mark.parametrize("value", [1, 1.5, "12.3", "0.0001"])
    def test_positive(self, value):
        assert positive_number(value) is True

    @pytest.mark.parametrize("value", [0, -1, "-2.5", "not a number", None, "", True, False])
    def test_non_positive(self, value):
        assert positive_number(value) is False


class TestMatchesRegex:
    PATTERN = r"^[A-Z]?\d{3}[A-Z]?$"

    @pytest.mark.parametrize("value", ["101", "A203", "203B", "A101B"])
    def test_match(self, value):
        assert matches_regex(value, self.PATTERN) is True

    @pytest.mark.parametrize("value", ["10", "1234", "abc", "AA101", "", None, 101])
    def test_no_match(self, value):
        assert matches_regex(value, self.PATTERN) is False


class TestInferFromName:
    @pytest.mark.parametrize(
        "name,expected",
        [
            # Phase 1 originals
            ("Closet 11A", "Storage"),
            ("Bathroom 1 07", "Wet"),
            ("Master Bathroom", "Wet"),
            ("Powder Room", "Wet"),
            ("Entry", "Circulation"),
            ("Stair 01", "Circulation"),
            ("Nook", "Living"),
            ("Living Room", "Living"),
            ("Bedroom 2", "Sleeping"),
            ("Kitchen", "Cooking"),
            ("Home Office", "Office"),
            # Phase 2 W6 D4 — Snowdon vocabulary
            ("Café 101", "Hospitality"),
            ("Cafe 102", "Hospitality"),
            ("Café Kitchen 102", "Hospitality"),  # "café" appears before "kitchen" in lookup → wins
            ("Studio Unit 203", "Residential"),
            ("Live/Work Unit 202", "Residential"),
            ("Live/Work Loft Unit 405", "Residential"),
            ("Two Story Studio Unit 204", "Residential"),
            ("Office Unit 301", "Office"),
            ("Commercial/Retail 105", "Commercial"),
            ("Residential Lobby 106", "Circulation"),
            ("Utility 107", "Services"),
            ("Machine RM P02", "Services"),
            ("Storage P04", "Storage"),
            ("Parking Garage P01", "Parking"),
            ("Garage", "Parking"),
            ("Pocket Park 104", "Outdoor"),
            ("Green Roof R100", "Outdoor"),
            ("Private Patio 507A", "Outdoor"),
            ("Outdoor Covered Dining 103", "Hospitality"),  # "dining" precedes "outdoor" in lookup
            ("Bandstand R101", "Recreation"),
            ("Elevator E1", "Circulation"),
        ],
    )
    def test_known_name(self, name, expected):
        assert infer_from_name(name) == expected

    @pytest.mark.parametrize("name", [None, "", "ZZZ-XYZ-9", "Room 101"])
    def test_unknown_name(self, name):
        assert infer_from_name(name) is None

    def test_garden_does_not_match_den_substring(self):
        """Low5: "den" is a keyword (→ "Office"), but it must only match as a
        WHOLE WORD, not as a substring of "Garden" — a plain ``in`` check
        used to wrongly infer "Office" for any room named "Garden" (Outdoor,
        via the "park"/"green roof"/"patio" family)."""
        assert infer_from_name("Garden") is None

    def test_garden_room_with_real_keyword_still_matches(self):
        # Sanity: word-boundary matching still finds a REAL keyword
        # elsewhere in the same name (doesn't just start rejecting matches).
        assert infer_from_name("Garden Storage") == "Storage"

    def test_parking_still_wins_over_park_substring(self):
        # Regression guard: "parking" must still resolve to "Parking" (its
        # own whole-word entry), not be affected by the word-boundary change.
        assert infer_from_name("Parking Garage P01") == "Parking"


class TestNotMatchesRegex:
    """Inverse of matches_regex: passes when pattern is NOT found in value."""

    @pytest.mark.parametrize("value", ["Closet", "Bathroom", "Entry"])
    def test_no_digit_passes(self, value):
        assert not_matches_regex(value, r"\d") is True

    @pytest.mark.parametrize("value", ["Bathroom 1", "Room 101", "TBD Office"])
    def test_with_digit_fails(self, value):
        # Pattern \d found → rule violated → returns False
        if any(c.isdigit() for c in value):
            assert not_matches_regex(value, r"\d") is False

    @pytest.mark.parametrize("value", ["TBD Room", "Office TODO", "??? Space"])
    def test_placeholder_fails(self, value):
        assert not_matches_regex(value, r"TBD|TODO|XXX|\?\?\?") is False

    @pytest.mark.parametrize("value", ["Closet 11A", "Office 101", "Living Room"])
    def test_clean_passes_placeholder_check(self, value):
        assert not_matches_regex(value, r"TBD|TODO|XXX|\?\?\?") is True

    @pytest.mark.parametrize("value", [None, "", 42, True])
    def test_null_and_non_string_pass(self, value):
        # Missing/non-string values can't violate a "must not contain" rule
        assert not_matches_regex(value, r".+") is True


class TestEvaluateDispatch:
    def test_present_and_nonempty(self):
        assert evaluate("present_and_nonempty", "x") is True
        assert evaluate("present_and_nonempty", "") is False

    def test_positive_number(self):
        assert evaluate("positive_number", 5) is True
        assert evaluate("positive_number", -1) is False

    def test_matches_regex(self):
        assert evaluate("matches_regex", "101", pattern=r"^\d{3}$") is True
        assert evaluate("matches_regex", "10", pattern=r"^\d{3}$") is False

    def test_matches_regex_requires_pattern(self):
        with pytest.raises(ValueError):
            evaluate("matches_regex", "101")

    def test_not_matches_regex(self):
        assert evaluate("not_matches_regex", "Closet", pattern=r"\d") is True
        assert evaluate("not_matches_regex", "Bathroom 1", pattern=r"\d") is False

    def test_not_matches_regex_requires_pattern(self):
        with pytest.raises(ValueError):
            evaluate("not_matches_regex", "x")

    def test_unknown_requirement(self):
        with pytest.raises(ValueError):
            evaluate("something_else", "x")

    def test_numeric_min(self):
        assert evaluate("numeric_min", 9.0, threshold=9.0) is True
        assert evaluate("numeric_min", 8.99, threshold=9.0) is False

    def test_numeric_min_requires_threshold(self):
        with pytest.raises(ValueError):
            evaluate("numeric_min", 5)

    def test_numeric_min_conditional_in_scope(self):
        # In scope (condition matches) → must meet threshold
        assert (
            evaluate(
                "numeric_min_conditional",
                10.5,
                threshold=10.0,
                condition_value="Residential One Story",
                when_pattern=r"^Residential",
            )
            is True
        )
        assert (
            evaluate(
                "numeric_min_conditional",
                8.5,
                threshold=10.0,
                condition_value="Residential One Story",
                when_pattern=r"^Residential",
            )
            is False
        )

    def test_numeric_min_conditional_out_of_scope_passes(self):
        # Condition doesn't match → rule passes regardless of value
        assert (
            evaluate(
                "numeric_min_conditional",
                1.0,
                threshold=10.0,
                condition_value="Commercial",
                when_pattern=r"^Residential",
            )
            is True
        )

    def test_numeric_min_conditional_requires_threshold_and_pattern(self):
        with pytest.raises(ValueError):
            evaluate("numeric_min_conditional", 5)
        with pytest.raises(ValueError):
            evaluate("numeric_min_conditional", 5, threshold=10.0)

    def test_unique_in_set(self):
        assert evaluate("unique_in_set", "203", siblings=["201", "202", "203"]) is True
        assert (
            evaluate("unique_in_set", "203", siblings=["201", "203", "203"]) is False
        )

    def test_unique_in_set_requires_siblings(self):
        with pytest.raises(ValueError):
            evaluate("unique_in_set", "203")


# ---- Direct evaluator unit tests for the 3 new functions -------------------


class TestNumericMin:
    @pytest.mark.parametrize("value", [9.0, 9.1, 100, "12.5", 10])
    def test_pass(self, value):
        assert numeric_min(value, 9.0) is True

    @pytest.mark.parametrize("value", [8.99, 0, -1, "8", None, True, False, "not-a-number"])
    def test_fail(self, value):
        assert numeric_min(value, 9.0) is False

    def test_nan_fails(self):
        assert numeric_min(float("nan"), 9.0) is False

    def test_exact_threshold_passes(self):
        # 9.0 == 9.0 — inclusive lower bound
        assert numeric_min(9.0, 9.0) is True


class TestNumericMinConditional:
    THRESHOLD = 10.0
    PATTERN = r"^Residential"

    def test_residential_room_meets_threshold(self):
        assert numeric_min_conditional(
            55.08, self.THRESHOLD,
            condition_value="Residential One Story",
            when_pattern=self.PATTERN,
        ) is True

    def test_residential_room_below_threshold_fails(self):
        assert numeric_min_conditional(
            8.5, self.THRESHOLD,
            condition_value="Residential One Story",
            when_pattern=self.PATTERN,
        ) is False

    def test_non_residential_room_passes_even_when_small(self):
        # Storage P04 area 5.32 m²; condition value "" → rule out of scope
        assert numeric_min_conditional(
            5.32, self.THRESHOLD,
            condition_value="",
            when_pattern=self.PATTERN,
        ) is True

    def test_none_condition_passes(self):
        # Corridor Occupancy is sometimes null on Snowdon
        assert numeric_min_conditional(
            3.5, self.THRESHOLD,
            condition_value=None,
            when_pattern=self.PATTERN,
        ) is True


class TestUniqueInSet:
    def test_unique_value_passes(self):
        assert unique_in_set("203", ["201", "202", "203", "204"]) is True

    def test_duplicate_value_fails(self):
        assert unique_in_set("203", ["201", "203", "203", "204"]) is False

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_value_passes(self, value):
        # missing values are out of scope — uniqueness only applies to
        # actually-present identifiers
        assert unique_in_set(value, ["201", "202", value, value]) is True

    def test_self_count_only_pass(self):
        # When siblings contains the value exactly once (typically the
        # element's own value), uniqueness holds.
        assert unique_in_set("P04", ["E1", "E2", "P04", "P05"]) is True
