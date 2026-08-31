"""Unit tests for GeometryRule schema and RuleSet.geometry_rules field."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from bim_orchestrator.policies.rules_schema import (
    GeometryRule,
    GeometryRuleSpatialFilter,
    RuleSet,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_geo(**overrides) -> dict:
    base = {
        "id": "ducts.parking.floor_clearance",
        "category": "Ducts",
        "check_type": "clearance_min",
        "description": "Duct clearance to floor slab >= 2400mm",
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# GeometryRule basics
# ---------------------------------------------------------------------------


class TestGeometryRuleDefaults:
    def test_minimal_valid(self):
        r = GeometryRule.model_validate(_minimal_geo())
        assert r.id == "ducts.parking.floor_clearance"
        assert r.execution_status == "not_model_checkable"
        assert r.severity_tag == "geometric_violation"
        assert r.reference_source == "same_model"
        assert r.threshold_mm is None
        assert r.clearance_direction is None
        assert r.spatial_filter is None
        assert r.view_id is None

    def test_view_id_accepted(self):
        r = GeometryRule.model_validate(_minimal_geo(view_id=1551372))
        assert r.view_id == 1551372

    def test_id_required(self):
        bad = _minimal_geo()
        del bad["id"]
        with pytest.raises(ValidationError):
            GeometryRule.model_validate(bad)

    def test_category_required(self):
        bad = _minimal_geo()
        del bad["category"]
        with pytest.raises(ValidationError):
            GeometryRule.model_validate(bad)

    def test_description_required(self):
        bad = _minimal_geo()
        del bad["description"]
        with pytest.raises(ValidationError):
            GeometryRule.model_validate(bad)


class TestGeometryCheckTypes:
    @pytest.mark.parametrize("check_type", [
        "clearance_min",
        "clearance_max",
        "spatial_containment",
        "min_spacing",
    ])
    def test_all_valid_check_types(self, check_type):
        r = GeometryRule.model_validate(_minimal_geo(check_type=check_type))
        assert r.check_type == check_type

    def test_invalid_check_type(self):
        with pytest.raises(ValidationError):
            GeometryRule.model_validate(_minimal_geo(check_type="gravity_violation"))


class TestClearanceDirection:
    @pytest.mark.parametrize("direction", ["below", "above", "horizontal"])
    def test_valid_directions(self, direction):
        r = GeometryRule.model_validate(_minimal_geo(clearance_direction=direction))
        assert r.clearance_direction == direction

    def test_invalid_direction(self):
        with pytest.raises(ValidationError):
            GeometryRule.model_validate(_minimal_geo(clearance_direction="diagonal"))

    def test_direction_optional(self):
        r = GeometryRule.model_validate(_minimal_geo(check_type="spatial_containment"))
        assert r.clearance_direction is None


class TestReferenceSource:
    @pytest.mark.parametrize("source", [
        "same_model", "linked_arch", "linked_struct", "linked_mep",
    ])
    def test_all_valid_sources(self, source):
        r = GeometryRule.model_validate(_minimal_geo(reference_source=source))
        assert r.reference_source == source

    def test_invalid_source(self):
        with pytest.raises(ValidationError):
            GeometryRule.model_validate(_minimal_geo(reference_source="ifc_file"))

    def test_default_source_is_same_model(self):
        r = GeometryRule.model_validate(_minimal_geo())
        assert r.reference_source == "same_model"


# ---------------------------------------------------------------------------
# Full clearance rule
# ---------------------------------------------------------------------------


class TestFullClearanceRule:
    def test_full_duct_floor_clearance(self):
        r = GeometryRule.model_validate(_minimal_geo(
            threshold_mm=2400.0,
            clearance_direction="below",
            reference_category="Floors",
            reference_source="linked_arch",
            spatial_filter={"category": "Spaces", "name_contains": "Parking"},
        ))
        assert r.threshold_mm == 2400.0
        assert r.clearance_direction == "below"
        assert r.reference_category == "Floors"
        assert r.reference_source == "linked_arch"
        assert r.spatial_filter is not None
        assert r.spatial_filter.category == "Spaces"
        assert r.spatial_filter.name_contains == "Parking"

    def test_min_spacing_rule(self):
        r = GeometryRule.model_validate(_minimal_geo(
            check_type="min_spacing",
            category="Structural Columns",
            threshold_mm=1000.0,
            reference_category="Structural Columns",
            description="Columns must be at least 1000mm apart",
        ))
        assert r.check_type == "min_spacing"
        assert r.threshold_mm == 1000.0

    def test_notes_field(self):
        r = GeometryRule.model_validate(_minimal_geo(
            notes=["Measured from duct bottom face", "Excludes MEP hangers"],
        ))
        assert r.notes is not None
        assert len(r.notes) == 2


# ---------------------------------------------------------------------------
# GeometryRuleSpatialFilter
# ---------------------------------------------------------------------------


class TestGeometryRuleSpatialFilter:
    def test_name_contains_only(self):
        sf = GeometryRuleSpatialFilter.model_validate({"name_contains": "Parking"})
        assert sf.name_contains == "Parking"
        assert sf.category is None

    def test_category_and_name(self):
        sf = GeometryRuleSpatialFilter.model_validate(
            {"category": "Spaces", "name_contains": "Parking"}
        )
        assert sf.category == "Spaces"
        assert sf.name_contains == "Parking"

    def test_name_exact(self):
        sf = GeometryRuleSpatialFilter.model_validate(
            {"name_exact": "Parking Garage Level 1"}
        )
        assert sf.name_exact == "Parking Garage Level 1"

    def test_all_none_is_valid(self):
        sf = GeometryRuleSpatialFilter.model_validate({})
        assert sf.category is None
        assert sf.name_contains is None
        assert sf.name_exact is None


# ---------------------------------------------------------------------------
# RuleSet.geometry_rules integration
# ---------------------------------------------------------------------------


class TestRuleSetGeometryRules:
    def test_geometry_rules_field_present(self):
        rs = RuleSet.model_validate({
            "scenario": "duct_clearance",
            "target_category": "Ducts",
            "rules": [],
            "geometry_rules": [_minimal_geo(threshold_mm=2400.0, clearance_direction="below")],
        })
        assert len(rs.geometry_rules) == 1
        assert rs.geometry_rules[0].threshold_mm == 2400.0

    def test_geometry_rules_default_empty(self):
        rs = RuleSet.model_validate({
            "scenario": "test",
            "target_category": "Rooms",
            "rules": [],
        })
        assert rs.geometry_rules == []

    def test_existing_yaml_loads_without_geometry_rules(self):
        """Back-compat: YAML files without geometry_rules still load."""
        rs = RuleSet.model_validate({
            "scenario": "legacy",
            "target_category": "Rooms",
            "rules": [
                {
                    "id": "rooms.number.present",
                    "parameter": "Number",
                    "requirement": "present_and_nonempty",
                    "severity_tag": "missing_required_param",
                    "description": "Room number must be set",
                    "autofill": {"strategy": "none"},
                }
            ],
        })
        assert len(rs.rules) == 1
        assert rs.geometry_rules == []

    def test_mixed_parameter_and_geometry_rules(self):
        rs = RuleSet.model_validate({
            "scenario": "mep_compliance",
            "target_category": "Ducts",
            "rules": [
                {
                    "id": "ducts.mark.present",
                    "parameter": "Mark",
                    "requirement": "present_and_nonempty",
                    "severity_tag": "missing_required_param",
                    "description": "Mark must be set",
                    "autofill": {"strategy": "none"},
                }
            ],
            "geometry_rules": [
                _minimal_geo(threshold_mm=2400.0, clearance_direction="below"),
            ],
        })
        assert len(rs.rules) == 1
        assert len(rs.geometry_rules) == 1

    def test_round_trip_yaml_serialization(self):
        rs = RuleSet.model_validate({
            "scenario": "duct_clearance",
            "target_category": "Ducts",
            "rules": [],
            "geometry_rules": [
                _minimal_geo(
                    threshold_mm=2400.0,
                    clearance_direction="below",
                    reference_category="Floors",
                    reference_source="linked_arch",
                    spatial_filter={"category": "Spaces", "name_contains": "Parking"},
                )
            ],
        })
        dumped = rs.model_dump(exclude_none=True)
        yaml_text = yaml.dump(dumped, allow_unicode=True, sort_keys=False)
        loaded = yaml.safe_load(yaml_text)
        rs2 = RuleSet.model_validate(loaded)
        geo = rs2.geometry_rules[0]
        assert geo.threshold_mm == 2400.0
        assert geo.check_type == "clearance_min"
        assert geo.reference_source == "linked_arch"
        assert geo.spatial_filter is not None
        assert geo.spatial_filter.name_contains == "Parking"

    def test_model_dump_exclude_none_omits_optional_fields(self):
        rs = RuleSet.model_validate({
            "scenario": "test",
            "target_category": "Ducts",
            "rules": [],
            "geometry_rules": [_minimal_geo()],
        })
        dumped = rs.model_dump(exclude_none=True)
        geo_dumped = dumped["geometry_rules"][0]
        assert "threshold_mm" not in geo_dumped
        assert "clearance_direction" not in geo_dumped
        assert "spatial_filter" not in geo_dumped
        assert "reference_category" not in geo_dumped
