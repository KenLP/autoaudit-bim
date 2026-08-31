"""IDS (Information Delivery Specification) <-> rules.yaml converter.

Implements buildingSMART IDS 1.0.
Spec: https://github.com/buildingSMART/IDS

Mapping overview
----------------
RuleSet            <->  ids document (info + specifications)
Rule               <->  ids:specification
target_category    <->  applicability/entity (via CATEGORY_TO_IFC table)
when_param/pattern <->  applicability/property (conditional applicability)
parameter          <->  requirements/attribute (for Name) or requirements/property
requirement type   <->  value restrictions (xs:pattern / xs:minInclusive / etc.)

Requirements with no native IDS equivalent are exported as bimo:extension
annotations so round-trips are lossless:
  unique_in_set      - cross-element uniqueness; no IDS concept
  not_matches_regex  - IDS xs:restriction has no negated pattern
  fire_rating_ge     - cross-param comparison (exports threshold as numeric_min
                       + bimo:extension carrying otherParam)

Extension namespace: https://github.com/KenLP/bim-orchestrator/ids-ext/1.0
"""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any
from xml.etree import ElementTree as ET

from bim_orchestrator.policies.rules_schema import (
    Rule,
    RuleAutofill,
    RuleRemediation,
    RuleSet,
)

# ---------------------------------------------------------------------------
# Namespace constants + helpers
# ---------------------------------------------------------------------------

_IDS = "http://standards.buildingsmart.org/IDS"
_XS = "http://www.w3.org/2001/XMLSchema"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_BIMO = "https://github.com/KenLP/bim-orchestrator/ids-ext/1.0"
_SCHEMA_LOC = f"{_IDS} {_IDS}/1.0/ids.xsd"

ET.register_namespace("", _IDS)
ET.register_namespace("xs", _XS)
ET.register_namespace("xsi", _XSI)
ET.register_namespace("bimo", _BIMO)


def _ids(tag: str) -> str:
    return f"{{{_IDS}}}{tag}"


def _xs_tag(tag: str) -> str:
    return f"{{{_XS}}}{tag}"


def _bimo_tag(tag: str) -> str:
    return f"{{{_BIMO}}}{tag}"


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# Revit category name -> IFC entity type
CATEGORY_TO_IFC: dict[str, str] = {
    "Rooms":                "IFCSPACE",
    "Walls":                "IFCWALL",
    "Doors":                "IFCDOOR",
    "Windows":              "IFCWINDOW",
    "Floors":               "IFCSLAB",
    "Ceilings":             "IFCCOVERING",
    "Columns":              "IFCCOLUMN",
    "Beams":                "IFCBEAM",
    "Stairs":               "IFCSTAIR",
    "Roofs":                "IFCROOF",
    "Furniture":            "IFCFURNISHINGELEMENT",
    "Mechanical Equipment": "IFCFLOWMOVINGDEVICE",
    "Electrical Equipment": "IFCDISTRIBUTIONELEMENT",
    "Generic Models":       "IFCBUILDINGELEMENTPROXY",
}
IFC_TO_CATEGORY: dict[str, str] = {v: k for k, v in CATEGORY_TO_IFC.items()}

# Revit parameter name -> (IFC pset, IFC baseName, IFC dataType)
_PARAM_PSET: dict[str, tuple[str, str, str]] = {
    # Rooms / Spaces
    "Department":        ("Pset_SpaceCommon",               "Department",    "IFCLABEL"),
    "OccupancyType":     ("Pset_SpaceCommon",               "OccupancyType", "IFCLABEL"),
    "Occupancy":         ("Pset_SpaceOccupancyRequirements", "OccupancyType","IFCLABEL"),
    "areaMetric":        ("Qto_SpaceBaseQuantities",         "NetFloorArea", "IFCAREAMEASURE"),
    "Unbounded Height":  ("Qto_SpaceBaseQuantities",         "Height",       "IFCLENGTHMEASURE"),
    "Number":            ("Pset_SpaceCommon",                "Reference",    "IFCLABEL"),
    "Comments":          ("Pset_ManufacturerTypeInformation","ProductInformation","IFCLABEL"),
    # Walls
    "Fire Rating":       ("Pset_WallCommon",                 "FireRating",   "IFCLABEL"),
    "Function":          ("Pset_WallCommon",                 "IsExternal",   "IFCBOOLEAN"),
    # Doors / Windows
    "Width":             ("Qto_DoorBaseQuantities",          "Width",        "IFCLENGTHMEASURE"),
    "Height":            ("Qto_DoorBaseQuantities",          "Height",       "IFCLENGTHMEASURE"),
    # Naming
    "Assembly Code":     ("Pset_BuildingElementCommon",      "Tag",          "IFCLABEL"),
    "Assembly Description": ("Pset_BuildingElementCommon",   "Description",  "IFCLABEL"),
}
# Reverse: (pset, baseName) -> Revit param
_PSET_PARAM: dict[tuple[str, str], str] = {
    (pset, base): param for param, (pset, base, _) in _PARAM_PSET.items()
}

# Rule.unit -> IFC measure dataType for numeric rules
_UNIT_IFC_TYPE: dict[str, str] = {
    "m²": "IFCAREAMEASURE", "ft²": "IFCAREAMEASURE",
    "m":  "IFCLENGTHMEASURE", "mm": "IFCLENGTHMEASURE", "ft": "IFCLENGTHMEASURE",
    "°":  "IFCPLANEANGLEMEASURE",
}
# IFC dataType -> unit (for import inference)
_IFC_TYPE_UNIT: dict[str, str] = {
    "IFCAREAMEASURE":   "m²",
    "IFCLENGTHMEASURE": "m",
}

_DEFAULT_IFC_VERSIONS = "IFC2X3 IFC4 IFC4X3_ADD2"

# Requirement types that have no native IDS equivalent
_BIMO_ONLY = frozenset({"unique_in_set", "not_matches_regex"})


# ---------------------------------------------------------------------------
# Export: RuleSet -> IDS XML
# ---------------------------------------------------------------------------


def ruleset_to_ids_xml(
    ruleset: RuleSet,
    *,
    title: str | None = None,
    description: str | None = None,
    version: str = "1.0",
    today: str | None = None,
    ifc_versions: str = _DEFAULT_IFC_VERSIONS,
) -> tuple[str, list[str]]:
    """Convert a RuleSet to IDS 1.0 XML string.

    Returns (xml_string, warnings).  ``warnings`` lists rules that required
    bimo: extension annotations because their requirement type has no direct
    IDS equivalent.
    """
    warnings: list[str] = []
    root = ET.Element(
        _ids("ids"),
        {f"{{{_XSI}}}schemaLocation": _SCHEMA_LOC},
    )
    _build_info(root, ruleset, title=title, description=description, version=version, today=today)
    specs = ET.SubElement(root, _ids("specifications"))
    for rule in ruleset.rules:
        w = _rule_to_spec(specs, rule, ruleset, ifc_versions=ifc_versions)
        warnings.extend(w)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}', warnings


def _build_info(
    parent: ET.Element,
    ruleset: RuleSet,
    *,
    title: str | None,
    description: str | None,
    version: str,
    today: str | None,
) -> None:
    info = ET.SubElement(parent, _ids("info"))
    ET.SubElement(info, _ids("title")).text = title or ruleset.scenario
    ET.SubElement(info, _ids("version")).text = version
    ET.SubElement(info, _ids("date")).text = today or str(_date.today())
    meta = ruleset.metadata or {}
    desc = description or (meta.get("source", "") if isinstance(meta, dict) else "")
    if desc:
        ET.SubElement(info, _ids("description")).text = str(desc)


def _rule_to_spec(
    parent: ET.Element,
    rule: Rule,
    ruleset: RuleSet,
    *,
    ifc_versions: str,
) -> list[str]:
    warnings: list[str] = []
    spec = ET.SubElement(
        parent,
        _ids("specification"),
        {
            "name": rule.id,
            "ifcVersion": ifc_versions,
            "minOccurs": "0",
            "maxOccurs": "unbounded",
            "instructions": rule.description.strip().replace("\n", " "),
        },
    )
    _build_applicability(spec, rule, ruleset)
    warnings.extend(_build_requirements(spec, rule))
    return warnings


def _build_applicability(
    spec: ET.Element, rule: Rule, ruleset: RuleSet
) -> None:
    appl = ET.SubElement(spec, _ids("applicability"))

    cat = rule.category or (
        ruleset.target_category
        if isinstance(ruleset.target_category, str)
        else ruleset.target_category[0]
    )
    ifc_entity = CATEGORY_TO_IFC.get(cat, f"IFC{cat.upper().replace(' ', '')}")
    entity = ET.SubElement(appl, _ids("entity"))
    _simple_value(ET.SubElement(entity, _ids("name")), ifc_entity)

    # Conditional applicability: when_param + when_pattern -> applicability/property
    if rule.when_param and rule.when_pattern:
        prop = ET.SubElement(appl, _ids("property"))
        pset, base, _ = _resolve_pset(rule.when_param)
        _simple_value(ET.SubElement(prop, _ids("propertySet")), pset)
        _simple_value(ET.SubElement(prop, _ids("baseName")), base)
        val = ET.SubElement(prop, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:string"})
        ET.SubElement(restr, _xs_tag("pattern"), {"value": rule.when_pattern})


def _build_requirements(spec: ET.Element, rule: Rule) -> list[str]:
    warnings: list[str] = []
    reqs = ET.SubElement(spec, _ids("requirements"))
    req_type = rule.requirement
    pset, base, dtype = _resolve_pset(rule.parameter, rule.unit)

    # Name attribute -> ids:attribute (IFC standard attribute, not a property set)
    if rule.parameter == "Name":
        _build_attribute_req(reqs, rule)
        if req_type in _BIMO_ONLY:
            warnings.append(
                f"[{rule.id}] '{req_type}' has no IDS native equivalent; bimo:extension added"
            )
            _append_bimo_ext(reqs, rule)
        return warnings

    prop = ET.SubElement(
        reqs,
        _ids("property"),
        {
            "dataType": dtype,
            "minOccurs": "1",
            "maxOccurs": "1",
            "instructions": f"Revit parameter: {rule.parameter}",
        },
    )
    _simple_value(ET.SubElement(prop, _ids("propertySet")), pset)
    _simple_value(ET.SubElement(prop, _ids("baseName")), base)

    if req_type == "present_and_nonempty":
        val = ET.SubElement(prop, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:string"})
        ET.SubElement(restr, _xs_tag("minLength"), {"value": "1"})

    elif req_type == "positive_number":
        val = ET.SubElement(prop, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:double"})
        ET.SubElement(restr, _xs_tag("minExclusive"), {"value": "0"})

    elif req_type in ("numeric_min", "numeric_min_conditional", "fire_rating_ge"):
        val = ET.SubElement(prop, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:double"})
        ET.SubElement(restr, _xs_tag("minInclusive"), {"value": str(rule.threshold or 0.0)})
        if req_type == "fire_rating_ge":
            warnings.append(
                f"[{rule.id}] fire_rating_ge cross-param compare "
                f"(other_param={rule.other_param!r}) exported as numeric_min threshold; "
                f"bimo:extension preserves other_param for round-trip"
            )
            _append_bimo_ext(reqs, rule)

    elif req_type == "matches_regex":
        val = ET.SubElement(prop, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:string"})
        ET.SubElement(restr, _xs_tag("pattern"), {"value": rule.pattern or ""})

    elif req_type == "value_in_subset":
        # Native IDS mapping: allowed set → xs:enumeration. Round-trips exactly
        # when the rule carries allowed_values; a table-driven rule (values
        # resolved per-element by category) has nothing inline to enumerate, so
        # we fall back to a bimo:extension that preserves the requirement type.
        vals = rule.allowed_values or []
        if vals:
            val = ET.SubElement(prop, _ids("value"))
            restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:string"})
            for code in vals:
                ET.SubElement(restr, _xs_tag("enumeration"), {"value": str(code)})
        else:
            warnings.append(
                f"[{rule.id}] value_in_subset is table-driven (no allowed_values); "
                "IDS enumeration omitted, bimo:extension preserves the requirement"
            )
            _append_bimo_ext(reqs, rule)

    elif req_type in _BIMO_ONLY:
        warnings.append(
            f"[{rule.id}] '{req_type}' has no IDS native equivalent; bimo:extension added"
        )
        _append_bimo_ext(reqs, rule)

    return warnings


def _build_attribute_req(reqs: ET.Element, rule: Rule) -> None:
    attr = ET.SubElement(
        reqs,
        _ids("attribute"),
        {"minOccurs": "1", "maxOccurs": "1",
         "instructions": f"Revit parameter: {rule.parameter}"},
    )
    _simple_value(ET.SubElement(attr, _ids("name")), "Name")
    if rule.requirement == "matches_regex" and rule.pattern:
        val = ET.SubElement(attr, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:string"})
        ET.SubElement(restr, _xs_tag("pattern"), {"value": rule.pattern})
    elif rule.requirement == "present_and_nonempty":
        val = ET.SubElement(attr, _ids("value"))
        restr = ET.SubElement(val, _xs_tag("restriction"), {"base": "xs:string"})
        ET.SubElement(restr, _xs_tag("minLength"), {"value": "1"})


def _append_bimo_ext(parent: ET.Element, rule: Rule) -> None:
    attrs: dict[str, str] = {"requirement": rule.requirement}
    if rule.threshold is not None:
        attrs["threshold"] = str(rule.threshold)
    if rule.pattern:
        attrs["pattern"] = rule.pattern
    if rule.other_param:
        attrs["otherParam"] = rule.other_param
    if rule.unit:
        attrs["unit"] = rule.unit
    if rule.allowed_values:
        attrs["allowedValues"] = "||".join(str(v) for v in rule.allowed_values)
    ET.SubElement(parent, _bimo_tag("extension"), attrs)


def _resolve_pset(param: str, unit: str | None = None) -> tuple[str, str, str]:
    if param in _PARAM_PSET:
        return _PARAM_PSET[param]
    dtype = _UNIT_IFC_TYPE.get(unit or "", "IFCLABEL")
    return ("Revit_BIMOrchestrator", param, dtype)


def _simple_value(parent: ET.Element, text: str) -> ET.Element:
    sv = ET.SubElement(parent, _ids("simpleValue"))
    sv.text = text
    return sv


# ---------------------------------------------------------------------------
# Import: IDS XML -> RuleSet
# ---------------------------------------------------------------------------


def ids_xml_to_ruleset(xml_text: str) -> tuple[RuleSet, list[str]]:
    """Parse IDS 1.0 XML and produce a (RuleSet, warnings) tuple.

    The returned RuleSet has severity_tag="missing_required_param" and
    fixability="manual" on all imported rules — caller should review and
    adjust these for the target project.
    """
    warnings: list[str] = []
    root = ET.fromstring(xml_text)

    # Extract scenario name from info/title
    scenario = "imported"
    info_el = root.find(_ids("info"))
    if info_el is not None:
        title_el = info_el.find(_ids("title"))
        if title_el is not None and title_el.text:
            scenario = title_el.text.strip()

    specs_el = root.find(_ids("specifications"))
    if specs_el is None:
        return RuleSet(scenario=scenario, target_category="Unknown", rules=[]), warnings

    rules: list[Rule] = []
    all_categories: list[str] = []

    for spec_el in specs_el.findall(_ids("specification")):
        rule, spec_warns, cat = _spec_to_rule(spec_el)
        warnings.extend(spec_warns)
        if rule is not None:
            rules.append(rule)
        if cat and cat not in all_categories:
            all_categories.append(cat)

    target_cat: str | list[str] = (
        all_categories[0] if len(all_categories) == 1
        else (all_categories if all_categories else "Unknown")
    )
    return RuleSet(scenario=scenario, target_category=target_cat, rules=rules), warnings


def _spec_to_rule(
    spec_el: ET.Element,
) -> tuple[Rule | None, list[str], str]:
    warns: list[str] = []
    rule_id = spec_el.get("name", "unknown.rule")
    description = spec_el.get("instructions", "")

    category, when_param, when_pattern = _parse_applicability(spec_el)

    reqs_el = spec_el.find(_ids("requirements"))
    if reqs_el is None:
        warns.append(f"[{rule_id}] no <requirements> element — skipped")
        return None, warns, category

    bimo_ext = reqs_el.find(_bimo_tag("extension"))

    # ids:attribute (for IFC entity attributes like Name)
    attr_el = reqs_el.find(_ids("attribute"))
    if attr_el is not None:
        param, req, pattern, threshold, unit, other_param, allowed = _parse_attribute_el(
            attr_el, bimo_ext
        )
    else:
        prop_el = reqs_el.find(_ids("property"))
        if prop_el is None:
            warns.append(f"[{rule_id}] no <property> or <attribute> requirement — skipped")
            return None, warns, category
        param, req, pattern, threshold, unit, other_param, allowed = _parse_property_el(
            prop_el, bimo_ext
        )

    rule = Rule(
        id=rule_id,
        parameter=param,
        requirement=req,
        pattern=pattern,
        threshold=threshold,
        unit=unit,
        other_param=other_param,
        allowed_values=allowed,
        when_param=when_param,
        when_pattern=when_pattern,
        severity_tag=_default_severity(req),
        description=description,
        fixability="manual",
        autofill=RuleAutofill(strategy="none"),
        remediation=RuleRemediation(action="create_acc_issue"),
    )
    return rule, warns, category


def _parse_applicability(
    spec_el: ET.Element,
) -> tuple[str, str | None, str | None]:
    category = "Unknown"
    when_param: str | None = None
    when_pattern: str | None = None

    appl = spec_el.find(_ids("applicability"))
    if appl is None:
        return category, when_param, when_pattern

    entity_el = appl.find(_ids("entity"))
    if entity_el is not None:
        name_el = entity_el.find(_ids("name"))
        if name_el is not None:
            sv = name_el.find(_ids("simpleValue"))
            if sv is not None and sv.text:
                category = IFC_TO_CATEGORY.get(sv.text.strip(), sv.text.strip())

    for prop_el in appl.findall(_ids("property")):
        pset_sv = _find_sv(prop_el, _ids("propertySet"))
        base_sv = _find_sv(prop_el, _ids("baseName"))
        pset = pset_sv.text.strip() if pset_sv is not None and pset_sv.text else ""
        base = base_sv.text.strip() if base_sv is not None and base_sv.text else ""
        when_param = _PSET_PARAM.get((pset, base), base)
        val_el = prop_el.find(_ids("value"))
        if val_el is not None:
            restr = val_el.find(_xs_tag("restriction"))
            if restr is not None:
                pat = restr.find(_xs_tag("pattern"))
                if pat is not None:
                    when_pattern = pat.get("value")

    return category, when_param, when_pattern


def _parse_attribute_el(
    attr_el: ET.Element, bimo_ext: ET.Element | None
) -> tuple[str, str, str | None, float | None, str | None, str | None, list[str] | None]:
    name_sv = _find_sv(attr_el, _ids("name"))
    param = name_sv.text.strip() if name_sv is not None and name_sv.text else "Name"
    if bimo_ext is not None:
        return _unpack_bimo(param, bimo_ext)
    req, pattern, threshold, unit, allowed = _infer_req_from_value(attr_el.find(_ids("value")))
    return param, req, pattern, threshold, unit, None, allowed


def _parse_property_el(
    prop_el: ET.Element, bimo_ext: ET.Element | None
) -> tuple[str, str, str | None, float | None, str | None, str | None, list[str] | None]:
    pset_sv = _find_sv(prop_el, _ids("propertySet"))
    base_sv = _find_sv(prop_el, _ids("baseName"))
    pset = pset_sv.text.strip() if pset_sv is not None and pset_sv.text else ""
    base = base_sv.text.strip() if base_sv is not None and base_sv.text else ""

    # Prefer original Revit param name from instructions annotation
    instructions = prop_el.get("instructions", "")
    m = re.search(r"Revit parameter:\s*(.+)", instructions)
    param = m.group(1).strip() if m else _PSET_PARAM.get((pset, base), base)

    if bimo_ext is not None:
        return _unpack_bimo(param, bimo_ext)

    req, pattern, threshold, unit, allowed = _infer_req_from_value(prop_el.find(_ids("value")))

    # Infer unit from dataType when not reconstructed from bimo
    if unit is None:
        unit = _IFC_TYPE_UNIT.get(prop_el.get("dataType", ""), None)

    return param, req, pattern, threshold, unit, None, allowed


def _unpack_bimo(
    param: str, ext: ET.Element
) -> tuple[str, str, str | None, float | None, str | None, str | None, list[str] | None]:
    req = ext.get("requirement", "present_and_nonempty")
    pattern = ext.get("pattern")
    t = ext.get("threshold")
    threshold = float(t) if t else None
    unit = ext.get("unit")
    other_param = ext.get("otherParam")
    av = ext.get("allowedValues")
    allowed = av.split("||") if av else None
    return param, req, pattern, threshold, unit, other_param, allowed


def _infer_req_from_value(
    val_el: ET.Element | None,
) -> tuple[str, str | None, float | None, str | None, list[str] | None]:
    if val_el is None:
        return "present_and_nonempty", None, None, None, None
    restr = val_el.find(_xs_tag("restriction"))
    if restr is None:
        return "present_and_nonempty", None, None, None, None

    # xs:enumeration → value_in_subset (the native IDS allowed-set mapping)
    enums = restr.findall(_xs_tag("enumeration"))
    if enums:
        vals = [e.get("value") for e in enums if e.get("value") is not None]
        return "value_in_subset", None, None, None, vals

    if restr.find(_xs_tag("minLength")) is not None:
        return "present_and_nonempty", None, None, None, None

    el = restr.find(_xs_tag("minExclusive"))
    if el is not None and el.get("value") == "0":
        return "positive_number", None, None, None, None

    el = restr.find(_xs_tag("minInclusive"))
    if el is not None:
        return "numeric_min", None, float(el.get("value", "0")), None, None

    el = restr.find(_xs_tag("pattern"))
    if el is not None:
        return "matches_regex", el.get("value"), None, None, None

    return "present_and_nonempty", None, None, None, None


def _find_sv(parent: ET.Element, child_tag: str) -> ET.Element | None:
    child = parent.find(child_tag)
    if child is None:
        return None
    return child.find(_ids("simpleValue"))


def _default_severity(req: str) -> str:
    if req in ("numeric_min", "numeric_min_conditional", "positive_number", "fire_rating_ge"):
        return "geometric_violation"
    return "missing_required_param"
