"""Unit tests for policies.revit_units (v1.4-D0.5).

Coverage:
  * convert() — same unit, known pair, unknown pair (raises)
  * convert_to_rule_unit() — happy path, unit not declared, unknown
    storage unit (passthrough), legacy metric-mirror passthrough,
    non-numeric values, error logging on failure.
  * QCAgent integration smoke — a rule with ``unit: "m"`` against a
    raw feet value evaluates correctly.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.policies.revit_units import (
    REVIT_STORAGE_UNITS,
    convert,
    convert_to_rule_unit,
)


class TestCatalogView:
    """QA F-aux: REVIT_STORAGE_UNITS is now backed by the param_catalog view, so a
    dimensional built-in (Diameter, Ceiling Height) converts even if not hand-listed."""

    def test_diameter_now_converts(self):
        # Diameter was length in param_catalog but absent here → used to skip.
        assert convert_to_rule_unit(1.0, "Diameter", "mm") == pytest.approx(304.8, rel=1e-4)

    def test_catalog_only_length_param_converts(self):
        # "Ceiling Height" is a Rooms length param, NOT in the explicit dict →
        # resolved via the catalog view.
        assert "Ceiling Height" not in REVIT_STORAGE_UNITS
        assert convert_to_rule_unit(1.0, "Ceiling Height", "mm") == pytest.approx(304.8, rel=1e-4)

    def test_non_dimensional_param_passes_through(self):
        # Slope is a "number" in the catalog → no unit, value unchanged.
        assert convert_to_rule_unit(5.0, "Slope", "mm") == 5.0


class TestConvert:
    def test_same_unit_is_noop(self):
        assert convert(2.4, "m", "m") == 2.4

    def test_feet_to_metres(self):
        assert convert(10.0, "ft", "m") == pytest.approx(3.048, rel=1e-3)

    def test_metres_to_feet(self):
        assert convert(3.048, "m", "ft") == pytest.approx(10.0, rel=1e-3)

    def test_mm_to_metres(self):
        assert convert(2286.0, "mm", "m") == pytest.approx(2.286, rel=1e-6)

    def test_sqft_to_sqm(self):
        # 1 ft² ≈ 0.0929 m²
        assert convert(100.0, "ft²", "m²") == pytest.approx(9.29, rel=1e-2)

    def test_unknown_pair_raises(self):
        with pytest.raises(ValueError, match="No conversion registered"):
            convert(1.0, "ft", "kg")


class TestConvertToRuleUnit:
    def test_no_unit_declared_is_passthrough(self):
        # rule.unit = None → return value unchanged regardless of param
        assert convert_to_rule_unit(10.25, "Unbounded Height", None) == 10.25

    def test_unknown_param_assumes_already_in_target(self):
        # Custom param not in REVIT_STORAGE_UNITS → no conversion
        assert convert_to_rule_unit(2.4, "Custom Height", "m") == 2.4

    def test_known_param_feet_to_metres(self):
        # Unbounded Height stored in ft, rule wants m
        result = convert_to_rule_unit(10.0, "Unbounded Height", "m")
        assert result == pytest.approx(3.048, rel=1e-3)

    def test_known_param_same_unit_noop(self):
        # Area is stored in ft², rule declares ft² → no conversion
        assert convert_to_rule_unit(100.0, "Area", "ft²") == 100.0

    def test_legacy_metric_mirror_passthrough(self):
        # "Unbounded Height (m)" isn't in REVIT_STORAGE_UNITS — value is
        # already metric. Back-compat for legacy YAMLs.
        assert convert_to_rule_unit(3.124, "Unbounded Height (m)", "m") == 3.124

    def test_none_value_passthrough(self):
        assert convert_to_rule_unit(None, "Unbounded Height", "m") is None

    def test_non_numeric_value_passthrough(self):
        # String that isn't a number → return unchanged (evaluator rejects)
        assert convert_to_rule_unit("n/a", "Unbounded Height", "m") == "n/a"

    def test_unknown_target_unit_raises_unit_conversion_error(self):
        # M-b: storage unit known (ft), target unknown/mismatched (kg) → RAISE
        # instead of silently returning the raw value (which the evaluator would
        # then compare wrong-unit). QC catches this and routes to manual_review.
        from bim_orchestrator.policies.revit_units import UnitConversionError
        with pytest.raises(UnitConversionError, match="cannot convert"):
            convert_to_rule_unit(10.0, "Unbounded Height", "kg")


class TestRevitStorageUnitsTable:
    """Spot checks on the lookup table — quick guard against typos."""

    @pytest.mark.parametrize(
        "param,unit",
        [
            ("Unbounded Height", "ft"),
            ("Area", "ft²"),
            ("Width", "ft"),
            ("Volume", "ft³"),
            ("Actual Run Width", "ft"),
            ("Minimum Run Width", "ft"),
        ],
    )
    def test_known_revit_params(self, param, unit):
        assert REVIT_STORAGE_UNITS[param] == unit

    def test_actual_run_width_converts_feet_to_mm(self):
        # 1.2 m stair run ≈ 3.9370 ft; rule threshold 1000 mm.
        # Without the REVIT_STORAGE_UNITS entry this would return 3.937
        # (feet) and compare < 1000 → every run falsely flagged.
        result = convert_to_rule_unit(3.937008, "Actual Run Width", "mm")
        assert result == pytest.approx(1200.0, rel=1e-3)


class TestQCAgentIntegration:
    """End-to-end: rule with unit + raw feet value → correct evaluation."""

    def test_rule_with_unit_converts_value_at_evaluation(self):
        from bim_orchestrator.agents.qc import QCAgent
        from bim_orchestrator.policies.autonomy import AutonomyPolicy
        from bim_orchestrator.policies.rules_schema import (
            Rule,
            RuleAutofill,
            RuleSet,
        )
        from pathlib import Path

        autonomy = AutonomyPolicy.load(
            Path(__file__).resolve().parents[1] / "config" / "autonomy.yaml"
        )
        ruleset = RuleSet(
            scenario="unit_test",
            target_category="Rooms",
            rules=[
                Rule(
                    id="test.height.min_2_4m",
                    parameter="Unbounded Height",
                    requirement="numeric_min",
                    threshold=2.4,
                    unit="m",  # ← THE NEW BIT
                    category="Rooms",
                    severity_tag="geometric_violation",
                    description="Ceiling height >= 2.4 m",
                    autofill=RuleAutofill(strategy="none"),
                ),
            ],
        )

        # Two elements: one tall enough (10 ft ≈ 3.05 m, passes), one
        # too short (7 ft ≈ 2.13 m, fails).
        elements = [
            {
                "id": "room-tall",
                "name": "Tall room",
                "category": "Rooms",
                "params": {"Unbounded Height": 10.0},  # feet
            },
            {
                "id": "room-short",
                "name": "Short room",
                "category": "Rooms",
                "params": {"Unbounded Height": 7.0},  # feet
            },
        ]

        # Inject ruleset directly bypassing the YAML loader
        qc = QCAgent.__new__(QCAgent)
        qc._rules = ruleset  # noqa: SLF001
        qc._autonomy = autonomy  # noqa: SLF001

        state = {
            "iteration": 0,
            "elements": elements,
            "findings": [],
            "proposed_fixes": [],
            "status": "init",
        }
        result = qc.run(state)

        # Tall room compliant, short room non-compliant
        assert result["outcomes_summary"]["compliant"] == 1
        assert result["outcomes_summary"]["non_compliant"] == 1
        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding["element_id"] == "room-short"
        # L-02 (2026-07-25 live review) — this used to assert the RAW 7.0 feet
        # on the grounds that the user should see "the actual Revit-side
        # number". Live, that produced *"Ceiling height >= 2.4 m. Got 2.16"* on
        # the document a reviewer signs: a verdict computed in metres, evidence
        # presented in unlabelled feet, unverifiable by the person accountable
        # for it. When the rule DECLARES a unit, the evidence must be in that
        # unit and must say so.
        assert "2.1336 m" in finding["message"]
        assert finding["current_value"] == "2.1336 m"
        assert "7.0" not in finding["message"]
