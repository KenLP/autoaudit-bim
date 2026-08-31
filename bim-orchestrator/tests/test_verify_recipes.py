"""Tests for verify_recipes — the requirement-type → native-verification registry.

Proves the registry is GENERAL (one entry per requirement type, mirroring the
rules_engine dispatch) and degrades honestly where a check can't map to a single
native Revit view-filter (regex / canonical / uniqueness / cross-element).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_args

from bim_orchestrator.policies.rules_schema import Requirement, Rule, RuleAutofill
from bim_orchestrator.verify_recipes import (
    REQUIREMENT_RENDERERS,
    VerifyRecipe,
    recipe_for,
)


def mk_rule(
    requirement: str,
    *,
    parameter: str = "Fire Rating",
    category: str | None = "Doors",
    operator: str | None = None,
    threshold: float | None = None,
    pattern: str | None = None,
    unit: str | None = None,
    lookup: str | None = None,
    other_param: str | None = None,
    allowed_values: list[str] | None = None,
) -> Rule:
    return Rule(
        id=f"test.{requirement}",
        parameter=parameter,
        requirement=requirement,  # type: ignore[arg-type]
        category=category,
        operator=operator,  # type: ignore[arg-type]
        threshold=threshold,
        pattern=pattern,
        unit=unit,
        lookup=lookup,
        other_param=other_param,
        allowed_values=allowed_values,
        severity_tag="rule_violation",
        description=f"{requirement} test rule",
        autofill=RuleAutofill(strategy="none"),
    )


# ── present_and_nonempty — native filter ─────────────────────────────────────


def test_present_and_nonempty_is_native_has_no_value_filter():
    recipe = recipe_for(mk_rule("present_and_nonempty", parameter="Department",
                                category="Rooms"), [])
    assert isinstance(recipe, VerifyRecipe)
    assert recipe.degraded is False
    assert recipe.view_filter is not None
    assert recipe.view_filter.rule_text == "has no value"
    assert recipe.view_filter.parameter == "Department"
    # Rooms get Number/Name identity columns, not Mark
    assert recipe.schedule.fields[:2] == ["Number", "Name"]
    assert "Department" in recipe.schedule.fields
    assert recipe.operand_columns == ["value"]


# ── numeric_compare — native filter, FAIL predicate is the negation ──────────


def test_numeric_compare_ge_uses_less_than_fail_filter():
    recipe = recipe_for(
        mk_rule("numeric_compare", parameter="Width", category="Doors",
                operator=">=", threshold=0.9, unit="m"),
        [],
    )
    assert recipe.degraded is False
    assert recipe.view_filter is not None
    # pass is ">= 0.9" so the filter highlighting FAILS must be "is less than 0.9"
    # v1.5-R6 (1.5): the unit rides along in the filter rule_text itself (not
    # just the narrative) — a filter rule quoted verbatim into Revit's dialog
    # must not silently assume the project's display unit matches the rule's.
    assert recipe.view_filter.rule_text == (
        "is less than 0.9 m (convert to your project's display units)"
    )
    assert recipe.operand_columns == ["value", "threshold"]
    # unit note surfaces when the rule declares a unit
    assert "m" in recipe.narrative


def test_numeric_compare_lt_negates_to_ge():
    recipe = recipe_for(
        mk_rule("numeric_compare", operator="<", threshold=5.0), []
    )
    assert recipe.view_filter is not None
    assert recipe.view_filter.rule_text == "is greater than or equal to 5.0"


def test_positive_number_maps_to_numeric_recipe():
    recipe = recipe_for(mk_rule("positive_number", parameter="Area"), [])
    assert recipe.degraded is False
    assert recipe.view_filter is not None
    # positive_number → "> 0", so FAIL filter is "is less than or equal to 0"
    assert recipe.view_filter.rule_text == "is less than or equal to 0"


def test_numeric_min_legacy_maps_to_numeric_recipe():
    recipe = recipe_for(mk_rule("numeric_min", threshold=2.6), [])
    assert recipe.degraded is False
    assert recipe.view_filter is not None


# ── degraded recipes (no single native filter) ──────────────────────────────


def test_matches_regex_degrades_no_view_filter():
    recipe = recipe_for(mk_rule("matches_regex", pattern=r"\d{3}"), [])
    assert recipe.degraded is True
    assert recipe.view_filter is None
    assert recipe.degraded_reason and "expression" in recipe.degraded_reason.lower()
    assert recipe.operand_columns == ["value", "pattern"]


def test_canonical_format_degrades_with_canonical_column():
    recipe = recipe_for(mk_rule("canonical_format"), [])
    assert recipe.degraded is True
    assert recipe.view_filter is None
    assert "suggested_value" in recipe.operand_columns


def test_unique_in_set_degrades_with_group_sort_hint():
    recipe = recipe_for(mk_rule("unique_in_set", parameter="Number",
                                category="Rooms"), [])
    assert recipe.degraded is True
    assert recipe.view_filter is None
    assert recipe.schedule.group_or_sort is not None
    # the parameter (Number) is also a Rooms identity column → must not appear twice
    assert recipe.schedule.fields == ["Number", "Name"]


def test_relation_compare_lookup_degrades_and_states_inputs_not_verdict():
    recipe = recipe_for(
        mk_rule("relation_compare", operator=">=", lookup="ibc716"), []
    )
    assert recipe.degraded is True
    assert recipe.view_filter is None
    assert recipe.operand_columns == ["value", "operand"]
    # The honesty guarantee: the operands schedule must not over-promise a verdict
    assert "shows the inputs, not the verdict" in recipe.narrative
    # cross-element recipe leads with Select by ID
    assert "Select by ID" in recipe.narrative


def test_fire_rating_ge_legacy_maps_to_relation_recipe():
    recipe = recipe_for(mk_rule("fire_rating_ge", other_param="host.Fire Rating"), [])
    assert recipe.degraded is True
    assert recipe.operand_columns == ["value", "operand"]


def test_value_in_subset_degrades_and_names_allowed_values():
    """Phase 2 GĐ2 requirement: membership in a per-element resolved set. No
    single native filter rule expresses multi-value membership, so it degrades
    honestly — the narrative names the allowed set from the records' operand."""
    rule = mk_rule(
        "value_in_subset", parameter="OmniClass Number", category="Doors",
        allowed_values=["23-13 11 11", "23-13 11 14"],
    )
    records = [
        {
            "rule_id": rule.id, "element_id": "1", "parameter": "OmniClass Number",
            "requirement": "value_in_subset", "raw_value": "23-13 11 11",
            "value": "23-13 11 11", "passed": True, "status": "compliant",
            "operand": ["23-13 11 11", "23-13 11 14"],
        },
    ]
    recipe = recipe_for(rule, records)
    assert recipe.degraded is True
    assert recipe.view_filter is None
    assert recipe.operand_columns == ["value", "operand"]
    assert "23-13 11 11" in recipe.narrative
    assert "Select by ID" in recipe.narrative


def test_value_in_subset_with_no_operand_records_falls_back_to_generic_note():
    """No record carries a resolved operand yet (e.g. an empty run) — the
    narrative must not crash and should point at the per-element table."""
    rule = mk_rule("value_in_subset", parameter="OmniClass Number")
    recipe = recipe_for(rule, [])
    assert recipe.degraded is True
    assert "per-element table" in recipe.narrative


# ── generality / fallback ────────────────────────────────────────────────────


def test_unknown_requirement_falls_back_to_default_degraded():
    # A future/unknown requirement can't be a pydantic Rule (Literal), so use a
    # duck-typed rule — recipe_for reads attributes via getattr.
    rule = SimpleNamespace(
        id="x.future", parameter="Foo", requirement="some_future_requirement",
        category="Walls",
    )
    recipe = recipe_for(rule, [])
    assert recipe.degraded is True
    assert recipe.view_filter is None
    assert "Select by ID" in recipe.narrative


def test_registry_covers_every_offered_requirement():
    """Each requirement the engine offers must have an explicit renderer (not the
    fallback), so the report stays in lock-step with rules_engine."""
    offered = [
        "present_and_nonempty", "canonical_format", "numeric_compare",
        "matches_regex", "unique_in_set", "relation_compare",
    ]
    for req in offered:
        assert req in REQUIREMENT_RENDERERS, f"missing renderer for {req}"


def test_registry_parity_with_requirement_literal():
    """v1.5-R6 (3.4): a stricter parity guard than the sample-list check above —
    EVERY member of the ``Requirement`` Literal (the engine's own schema type)
    must have a renderer, and the registry must not carry stray keys the schema
    doesn't know about. Catches drift in both directions, not just missing
    entries for a hand-picked sample."""
    assert set(REQUIREMENT_RENDERERS) == set(get_args(Requirement))


def test_category_falls_back_to_record_when_rule_has_none():
    """When the rule carries no category, the recipe derives it from a record —
    so a multi-category ruleset still names the right schedule category."""
    rule = mk_rule("present_and_nonempty", category=None)
    records = [{"category": "Windows", "element_id": "1"}]  # type: ignore[list-item]
    recipe = recipe_for(rule, records)
    assert recipe.schedule.category == "Windows"
