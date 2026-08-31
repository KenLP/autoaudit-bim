"""Tests for policies/ids_converter.py — IDS 1.0 <-> rules.yaml.

All tests are offline/deterministic (no MCP, no model files).
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from bim_orchestrator.agents.qc import Rule, RuleSet
from bim_orchestrator.policies.ids_converter import (
    CATEGORY_TO_IFC,
    IFC_TO_CATEGORY,
    _IDS,
    _BIMO,
    _XS,
    ids_xml_to_ruleset,
    ruleset_to_ids_xml,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ids(tag: str) -> str:
    return f"{{{_IDS}}}{tag}"


def _xs(tag: str) -> str:
    return f"{{{_XS}}}{tag}"


def _bimo(tag: str) -> str:
    return f"{{{_BIMO}}}{tag}"


def _simple_rule(**kwargs) -> Rule:
    defaults: dict = {
        "id": "test.rule",
        "parameter": "Department",
        "requirement": "present_and_nonempty",
        "severity_tag": "missing_required_param",
        "description": "Test rule",
        "fixability": "manual",
        "autofill": {"strategy": "none"},
        "remediation": {"action": "create_acc_issue"},
    }
    defaults.update(kwargs)
    return Rule.model_validate(defaults)


def _ruleset(*rules: Rule, category: str = "Rooms") -> RuleSet:
    return RuleSet(scenario="test_scenario", target_category=category, rules=list(rules))


def _export(ruleset: RuleSet, **kw) -> tuple[ET.Element, list[str]]:
    xml, warnings = ruleset_to_ids_xml(ruleset, today="2026-06-11", **kw)
    root = ET.fromstring(xml)
    return root, warnings


def _first_spec(root: ET.Element) -> ET.Element:
    return root.find(_ids("specifications")).find(_ids("specification"))


def _first_req(root: ET.Element) -> ET.Element:
    spec = _first_spec(root)
    return spec.find(_ids("requirements"))


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------


class TestCategoryMapping:
    def test_rooms_maps_to_ifcspace(self):
        assert CATEGORY_TO_IFC["Rooms"] == "IFCSPACE"

    def test_walls_maps_to_ifcwall(self):
        assert CATEGORY_TO_IFC["Walls"] == "IFCWALL"

    def test_ifc_to_category_roundtrip(self):
        for cat, ifc in CATEGORY_TO_IFC.items():
            assert IFC_TO_CATEGORY[ifc] == cat


# ---------------------------------------------------------------------------
# Export: basic structure
# ---------------------------------------------------------------------------


class TestExportStructure:
    def test_xml_declaration(self):
        rs = _ruleset(_simple_rule())
        xml, _ = ruleset_to_ids_xml(rs, today="2026-06-11")
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    def test_info_title_from_scenario(self):
        rs = _ruleset(_simple_rule())
        root, _ = _export(rs)
        title = root.find(_ids("info")).find(_ids("title")).text
        assert title == "test_scenario"

    def test_info_title_override(self):
        rs = _ruleset(_simple_rule())
        root, _ = _export(rs, title="Custom Title")
        assert root.find(_ids("info")).find(_ids("title")).text == "Custom Title"

    def test_info_date(self):
        rs = _ruleset(_simple_rule())
        root, _ = _export(rs)
        assert root.find(_ids("info")).find(_ids("date")).text == "2026-06-11"

    def test_spec_name_is_rule_id(self):
        rs = _ruleset(_simple_rule(id="room.dept.required"))
        root, _ = _export(rs)
        spec = _first_spec(root)
        assert spec.get("name") == "room.dept.required"

    def test_spec_ifc_versions_default(self):
        rs = _ruleset(_simple_rule())
        root, _ = _export(rs)
        assert "IFC4" in _first_spec(root).get("ifcVersion", "")

    def test_applicability_entity_rooms(self):
        root, _ = _export(_ruleset(_simple_rule()))
        appl = _first_spec(root).find(_ids("applicability"))
        entity = appl.find(_ids("entity"))
        sv = entity.find(_ids("name")).find(_ids("simpleValue"))
        assert sv.text == "IFCSPACE"

    def test_no_warnings_for_supported_rule(self):
        rs = _ruleset(_simple_rule())
        _, warnings = ruleset_to_ids_xml(rs, today="2026-06-11")
        assert warnings == []


# ---------------------------------------------------------------------------
# Export: requirement types
# ---------------------------------------------------------------------------


class TestExportRequirementTypes:
    def test_present_and_nonempty_emits_min_length_1(self):
        rs = _ruleset(_simple_rule(requirement="present_and_nonempty", parameter="Department"))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        restr = prop.find(_ids("value")).find(_xs("restriction"))
        assert restr.find(_xs("minLength")).get("value") == "1"

    def test_numeric_min_emits_min_inclusive(self):
        rs = _ruleset(_simple_rule(
            requirement="numeric_min",
            parameter="areaMetric",
            threshold=9.0,
            severity_tag="geometric_violation",
        ))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        restr = prop.find(_ids("value")).find(_xs("restriction"))
        assert restr.find(_xs("minInclusive")).get("value") == "9.0"

    def test_positive_number_emits_min_exclusive_zero(self):
        rs = _ruleset(_simple_rule(
            requirement="positive_number",
            parameter="areaMetric",
            severity_tag="geometric_violation",
        ))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        restr = prop.find(_ids("value")).find(_xs("restriction"))
        assert restr.find(_xs("minExclusive")).get("value") == "0"

    def test_matches_regex_emits_xs_pattern(self):
        rs = _ruleset(_simple_rule(
            requirement="matches_regex",
            parameter="Number",
            pattern=r"^\d{3}[A-Z]?$",
        ))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        restr = prop.find(_ids("value")).find(_xs("restriction"))
        assert restr.find(_xs("pattern")).get("value") == r"^\d{3}[A-Z]?$"

    def test_name_parameter_uses_attribute_not_property(self):
        rs = _ruleset(_simple_rule(
            parameter="Name",
            requirement="matches_regex",
            pattern=r"^[A-Z]{2,4}-\d{3}$",
        ))
        root, _ = _export(rs)
        reqs = _first_req(root)
        assert reqs.find(_ids("attribute")) is not None
        assert reqs.find(_ids("property")) is None

    def test_name_attribute_pattern_set(self):
        rs = _ruleset(_simple_rule(
            parameter="Name",
            requirement="matches_regex",
            pattern=r"^[A-Z]{2,4}-\d{3}$",
        ))
        root, _ = _export(rs)
        attr = _first_req(root).find(_ids("attribute"))
        restr = attr.find(_ids("value")).find(_xs("restriction"))
        assert restr.find(_xs("pattern")).get("value") == r"^[A-Z]{2,4}-\d{3}$"

    def test_property_instructions_contains_revit_param(self):
        rs = _ruleset(_simple_rule(parameter="areaMetric"))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        assert "areaMetric" in prop.get("instructions", "")

    def test_known_param_maps_to_standard_pset(self):
        rs = _ruleset(_simple_rule(parameter="Department"))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        pset_sv = prop.find(_ids("propertySet")).find(_ids("simpleValue"))
        assert pset_sv.text == "Pset_SpaceCommon"

    def test_unknown_param_uses_revit_bimorchestrator_pset(self):
        rs = _ruleset(_simple_rule(parameter="MyCustomParam"))
        root, _ = _export(rs)
        prop = _first_req(root).find(_ids("property"))
        pset_sv = prop.find(_ids("propertySet")).find(_ids("simpleValue"))
        assert pset_sv.text == "Revit_BIMOrchestrator"


# ---------------------------------------------------------------------------
# Export: bimo extension for unsupported types
# ---------------------------------------------------------------------------


class TestExportBimoExtensions:
    def test_unique_in_set_emits_warning(self):
        rs = _ruleset(_simple_rule(
            requirement="unique_in_set",
            parameter="Number",
            severity_tag="duplicate_identifier",
        ))
        _, warnings = ruleset_to_ids_xml(rs, today="2026-06-11")
        assert any("unique_in_set" in w for w in warnings)

    def test_unique_in_set_emits_bimo_extension(self):
        rs = _ruleset(_simple_rule(
            requirement="unique_in_set",
            parameter="Number",
            severity_tag="duplicate_identifier",
        ))
        root, _ = _export(rs)
        bimo_ext = _first_req(root).find(_bimo("extension"))
        assert bimo_ext is not None
        assert bimo_ext.get("requirement") == "unique_in_set"

    def test_fire_rating_ge_emits_threshold_and_bimo_extension(self):
        rs = _ruleset(_simple_rule(
            requirement="fire_rating_ge",
            parameter="Fire Rating",
            threshold=60.0,
            other_param="Required Fire Rating",
            severity_tag="geometric_violation",
        ))
        root, warnings = _export(rs)
        # threshold exported as numeric_min
        prop = _first_req(root).find(_ids("property"))
        restr = prop.find(_ids("value")).find(_xs("restriction"))
        assert restr.find(_xs("minInclusive")).get("value") == "60.0"
        # bimo extension carries other_param
        bimo_ext = _first_req(root).find(_bimo("extension"))
        assert bimo_ext is not None
        assert bimo_ext.get("otherParam") == "Required Fire Rating"
        assert any("fire_rating_ge" in w for w in warnings)

    def test_not_matches_regex_emits_bimo_extension(self):
        rs = _ruleset(_simple_rule(
            requirement="not_matches_regex",
            parameter="Comments",
            pattern=r"PROHIBITED",
        ))
        root, warnings = _export(rs)
        bimo_ext = _first_req(root).find(_bimo("extension"))
        assert bimo_ext is not None
        assert bimo_ext.get("requirement") == "not_matches_regex"
        assert bimo_ext.get("pattern") == "PROHIBITED"
        assert any("not_matches_regex" in w for w in warnings)


# ---------------------------------------------------------------------------
# Export: conditional applicability (when_param / when_pattern)
# ---------------------------------------------------------------------------


class TestExportConditionalApplicability:
    def test_when_param_creates_applicability_property(self):
        rs = _ruleset(_simple_rule(
            requirement="numeric_min",
            parameter="areaMetric",
            threshold=10.0,
            when_param="Occupancy",
            when_pattern="^Residential",
            severity_tag="geometric_violation",
        ))
        root, _ = _export(rs)
        appl = _first_spec(root).find(_ids("applicability"))
        cond_props = appl.findall(_ids("property"))
        assert len(cond_props) == 1

    def test_when_pattern_set_as_xs_pattern_in_applicability(self):
        rs = _ruleset(_simple_rule(
            requirement="numeric_min",
            parameter="areaMetric",
            threshold=10.0,
            when_param="Occupancy",
            when_pattern="^Residential",
            severity_tag="geometric_violation",
        ))
        root, _ = _export(rs)
        appl = _first_spec(root).find(_ids("applicability"))
        cond_prop = appl.find(_ids("property"))
        val = cond_prop.find(_ids("value"))
        restr = val.find(_xs("restriction"))
        assert restr.find(_xs("pattern")).get("value") == "^Residential"


# ---------------------------------------------------------------------------
# Import: basic structure
# ---------------------------------------------------------------------------


class TestImportBasic:
    def _round_trip(self, rule: Rule) -> Rule:
        rs = _ruleset(rule)
        xml, _ = ruleset_to_ids_xml(rs, today="2026-06-11")
        imported, _ = ids_xml_to_ruleset(xml)
        return imported.rules[0]

    def test_scenario_from_title(self):
        rs = _ruleset(_simple_rule())
        xml, _ = ruleset_to_ids_xml(rs, title="My IDS", today="2026-06-11")
        imported, _ = ids_xml_to_ruleset(xml)
        assert imported.scenario == "My IDS"

    def test_target_category_roundtrip(self):
        rs = _ruleset(_simple_rule())
        xml, _ = ruleset_to_ids_xml(rs, today="2026-06-11")
        imported, _ = ids_xml_to_ruleset(xml)
        assert imported.target_category == "Rooms"

    def test_rule_id_roundtrip(self):
        rule = _simple_rule(id="room.dept.required")
        r = self._round_trip(rule)
        assert r.id == "room.dept.required"

    def test_present_and_nonempty_roundtrip(self):
        rule = _simple_rule(requirement="present_and_nonempty", parameter="Department")
        r = self._round_trip(rule)
        assert r.requirement == "present_and_nonempty"
        assert r.parameter == "Department"

    def test_numeric_min_roundtrip(self):
        rule = _simple_rule(
            requirement="numeric_min",
            parameter="areaMetric",
            threshold=9.0,
            unit="m²",
            severity_tag="geometric_violation",
        )
        r = self._round_trip(rule)
        assert r.requirement == "numeric_min"
        assert r.threshold == pytest.approx(9.0)

    def test_matches_regex_roundtrip(self):
        rule = _simple_rule(
            requirement="matches_regex",
            parameter="Number",
            pattern=r"^\d{3}$",
        )
        r = self._round_trip(rule)
        assert r.requirement == "matches_regex"
        assert r.pattern == r"^\d{3}$"

    def test_unique_in_set_roundtrip_via_bimo(self):
        rule = _simple_rule(
            requirement="unique_in_set",
            parameter="Number",
            severity_tag="duplicate_identifier",
        )
        r = self._round_trip(rule)
        assert r.requirement == "unique_in_set"
        assert r.parameter == "Number"

    def test_fire_rating_ge_roundtrip_via_bimo(self):
        rule = _simple_rule(
            requirement="fire_rating_ge",
            parameter="Fire Rating",
            threshold=60.0,
            other_param="Required Fire Rating",
            severity_tag="geometric_violation",
        )
        r = self._round_trip(rule)
        assert r.requirement == "fire_rating_ge"
        assert r.threshold == pytest.approx(60.0)
        assert r.other_param == "Required Fire Rating"

    def test_when_param_roundtrip(self):
        rule = _simple_rule(
            requirement="numeric_min",
            parameter="areaMetric",
            threshold=10.0,
            when_param="Occupancy",
            when_pattern="^Residential",
            severity_tag="geometric_violation",
        )
        r = self._round_trip(rule)
        assert r.when_param == "Occupancy"
        assert r.when_pattern == "^Residential"

    def test_name_attribute_roundtrip(self):
        rule = _simple_rule(
            parameter="Name",
            requirement="matches_regex",
            pattern=r"^[A-Z]{2,4}-\d{3}$",
        )
        r = self._round_trip(rule)
        assert r.parameter == "Name"
        assert r.requirement == "matches_regex"
        assert r.pattern == r"^[A-Z]{2,4}-\d{3}$"


# ---------------------------------------------------------------------------
# Import: third-party IDS (no bimo extensions)
# ---------------------------------------------------------------------------


class TestImportThirdParty:
    _MINIMAL_IDS = """\
<?xml version="1.0" encoding="UTF-8"?>
<ids xmlns="http://standards.buildingsmart.org/IDS"
     xmlns:xs="http://www.w3.org/2001/XMLSchema"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="http://standards.buildingsmart.org/IDS \
http://standards.buildingsmart.org/IDS/1.0/ids.xsd">
  <info><title>External IDS</title><version>1.0</version><date>2026-01-01</date></info>
  <specifications>
    <specification name="space.dept" ifcVersion="IFC4" minOccurs="0" maxOccurs="unbounded"
                   instructions="Department must be filled">
      <applicability>
        <entity><name><simpleValue>IFCSPACE</simpleValue></name></entity>
      </applicability>
      <requirements>
        <property dataType="IFCLABEL" minOccurs="1" maxOccurs="1"
                  instructions="Department">
          <propertySet><simpleValue>Pset_SpaceCommon</simpleValue></propertySet>
          <baseName><simpleValue>Department</simpleValue></baseName>
          <value>
            <xs:restriction base="xs:string">
              <xs:minLength value="1"/>
            </xs:restriction>
          </value>
        </property>
      </requirements>
    </specification>
    <specification name="space.area.min" ifcVersion="IFC4" minOccurs="0" maxOccurs="unbounded"
                   instructions="Min area 9 sqm">
      <applicability>
        <entity><name><simpleValue>IFCSPACE</simpleValue></name></entity>
      </applicability>
      <requirements>
        <property dataType="IFCAREAMEASURE" minOccurs="1" maxOccurs="1"
                  instructions="Area">
          <propertySet><simpleValue>Qto_SpaceBaseQuantities</simpleValue></propertySet>
          <baseName><simpleValue>NetFloorArea</simpleValue></baseName>
          <value>
            <xs:restriction base="xs:double">
              <xs:minInclusive value="9.0"/>
            </xs:restriction>
          </value>
        </property>
      </requirements>
    </specification>
  </specifications>
</ids>"""

    def test_third_party_import_scenario(self):
        rs, _ = ids_xml_to_ruleset(self._MINIMAL_IDS)
        assert rs.scenario == "External IDS"

    def test_third_party_category_from_ifc_entity(self):
        rs, _ = ids_xml_to_ruleset(self._MINIMAL_IDS)
        assert rs.target_category == "Rooms"

    def test_third_party_present_and_nonempty(self):
        rs, _ = ids_xml_to_ruleset(self._MINIMAL_IDS)
        dept_rule = next(r for r in rs.rules if r.id == "space.dept")
        assert dept_rule.requirement == "present_and_nonempty"
        assert dept_rule.parameter == "Department"

    def test_third_party_numeric_min(self):
        rs, _ = ids_xml_to_ruleset(self._MINIMAL_IDS)
        area_rule = next(r for r in rs.rules if r.id == "space.area.min")
        assert area_rule.requirement == "numeric_min"
        assert area_rule.threshold == pytest.approx(9.0)

    def test_third_party_area_unit_inferred(self):
        rs, _ = ids_xml_to_ruleset(self._MINIMAL_IDS)
        area_rule = next(r for r in rs.rules if r.id == "space.area.min")
        assert area_rule.unit == "m²"

    def test_import_no_specifications_returns_empty_ruleset(self):
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<ids xmlns="http://standards.buildingsmart.org/IDS"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="http://standards.buildingsmart.org/IDS \
http://standards.buildingsmart.org/IDS/1.0/ids.xsd">
  <info><title>Empty</title><version>1.0</version><date>2026-01-01</date></info>
</ids>"""
        rs, _ = ids_xml_to_ruleset(xml)
        assert rs.rules == []

    def test_import_spec_without_requirements_warns_and_skips(self):
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<ids xmlns="http://standards.buildingsmart.org/IDS"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="http://standards.buildingsmart.org/IDS \
http://standards.buildingsmart.org/IDS/1.0/ids.xsd">
  <info><title>T</title><version>1.0</version><date>2026-01-01</date></info>
  <specifications>
    <specification name="bad.rule" ifcVersion="IFC4" minOccurs="0" maxOccurs="1"
                   instructions="no requirements">
      <applicability>
        <entity><name><simpleValue>IFCSPACE</simpleValue></name></entity>
      </applicability>
    </specification>
  </specifications>
</ids>"""
        rs, warnings = ids_xml_to_ruleset(xml)
        assert rs.rules == []
        assert any("bad.rule" in w for w in warnings)


# ---------------------------------------------------------------------------
# Phase 2 GĐ2 step 5 — value_in_subset <-> xs:enumeration
# ---------------------------------------------------------------------------


class TestValueInSubsetEnumeration:
    def _door_cls_rule(self, **kw) -> Rule:
        return _simple_rule(
            id="door.classification",
            parameter="Classification",
            requirement="value_in_subset",
            description="Door classification must be a valid door code",
            **kw,
        )

    def test_export_emits_xs_enumeration(self):
        rs = _ruleset(
            self._door_cls_rule(allowed_values=["Pr_30_59_24", "Pr_30_59_24_14"]),
            category="Doors",
        )
        root, warnings = _export(rs)
        restr = (
            _first_req(root)
            .find(_ids("property"))
            .find(_ids("value"))
            .find(_xs("restriction"))
        )
        vals = [e.get("value") for e in restr.findall(_xs("enumeration"))]
        assert vals == ["Pr_30_59_24", "Pr_30_59_24_14"]
        assert warnings == []  # native IDS mapping → no extension needed

    def test_roundtrip_preserves_allowed_values(self):
        rs = _ruleset(
            self._door_cls_rule(allowed_values=["Pr_30_59_24", "Pr_30_59_24_14"]),
            category="Doors",
        )
        xml, _ = ruleset_to_ids_xml(rs, today="2026-06-11")
        back, _ = ids_xml_to_ruleset(xml)
        r = back.rules[0]
        assert r.requirement == "value_in_subset"
        assert r.allowed_values == ["Pr_30_59_24", "Pr_30_59_24_14"]
        assert back.target_category == "Doors"

    def test_table_driven_subset_falls_back_to_bimo(self):
        """No allowed_values (resolved per-element by category) → no enumeration
        to emit; bimo:extension preserves the requirement type for round-trip."""
        rs = _ruleset(self._door_cls_rule(), category="Doors")  # no allowed_values
        root, warnings = _export(rs)
        assert any("value_in_subset" in w for w in warnings)
        assert _first_req(root).find(_bimo("extension")) is not None
        xml, _ = ruleset_to_ids_xml(rs, today="2026-06-11")
        back, _ = ids_xml_to_ruleset(xml)
        assert back.rules[0].requirement == "value_in_subset"
        assert back.rules[0].allowed_values is None
