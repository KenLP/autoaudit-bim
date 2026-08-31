"""Convert a rules JSON (from references/templates/rules-extraction-prompt.md)
into a config/rules.*.yaml file consumable by QCAgent.

⚠️  DEPRECATED as of v1.4-F4 (2026-06-09)
=========================================
This is the v1 (pre-D0) manual workflow converter, kept for back-compat
with the ``references/templates/sample-extracted-rules.json`` fixture
and existing CI exit-code assertions in ``tests/test_json_to_yaml.py``.

**For new work prefer the D0 skill-pack converter:**

    python extraction-skills/scripts/json_to_yaml.py path/to/rules.json \\
        --rules-out config/rules.<scenario>.yaml

The D0 converter adds:
  * Pydantic ``Rule`` validation (includes ``unit``, ``rule_type``,
    ``extraction_meta``)
  * Heuristic warnings (unit-missing, scope-filter, fragmentation)
  * ``execution_status``-based split (executable / needs_domain_mapping
    / not_model_checkable) with confidence-based fixability bump
  * OSTCatalog category normalisation
  * Review markdown output

The Streamlit Setup tab + Rule Builder tab already call the D0 converter.
This script will be removed once the legacy fixture migrates to the D0
shape.

Legacy usage:
    uv run python scripts/json_to_yaml.py path/to/rules.json \\
        --out config/rules.<scenario>.yaml

Exit codes:
    0   success
    1   unexpected error
    2   CLI arg error / file not found
    3   JSON schema validation failed (RuleSet mismatch)
    4   UNSUPPORTED_* requirement found (need to extend rules_engine.py)
    5   unknown severity_tag (need to add to config/autonomy.yaml)
    6   non-ASCII characters detected in description / comments_template
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

# scripts/ is sibling of src/ inside bim-orchestrator/ — `uv run` installs the
# package in editable mode so this import works without sys.path hacking.
from bim_orchestrator.agents.qc import RuleSet

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWN_SEVERITY_TAGS = {
    "missing_required_param",
    "missing_optional_param",
    "invalid_value_range",
    "structural_param_change",
    "fire_safety_change",
    "geometric_violation",
    "duplicate_identifier",
}


def _fail(code: int, msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return code


def _scan_ascii(rules: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Return list of (rule_id, field, offending_substring) for non-ASCII text.

    The Revit add-in mis-decodes UTF-8 multi-byte chars (em-dash, section sign,
    >=, etc.) so we keep the audit trail ASCII-only until that's patched.
    """
    bad: list[tuple[str, str, str]] = []
    fields_to_scan = ["description"]
    for rule in rules:
        rid = rule.get("id", "<no-id>")
        for field in fields_to_scan:
            val = rule.get(field)
            if isinstance(val, str) and not val.isascii():
                non_ascii = "".join(c for c in val if ord(c) > 127)
                bad.append((rid, field, non_ascii[:40]))
        # remediation.comments_template is nested
        remediation = rule.get("remediation") or {}
        tmpl = remediation.get("comments_template")
        if isinstance(tmpl, str) and not tmpl.isascii():
            non_ascii = "".join(c for c in tmpl if ord(c) > 127)
            bad.append((rid, "remediation.comments_template", non_ascii[:40]))
    return bad


def _check_unsupported(rules: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return list of (rule_id, requirement) where requirement starts with UNSUPPORTED_."""
    return [
        (r.get("id", "<no-id>"), r["requirement"])
        for r in rules
        if isinstance(r.get("requirement"), str)
        and r["requirement"].startswith("UNSUPPORTED_")
    ]


def _check_severity_tags(rules: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return list of (rule_id, severity_tag) where severity_tag is not in known set."""
    return [
        (r.get("id", "<no-id>"), r["severity_tag"])
        for r in rules
        if isinstance(r.get("severity_tag"), str)
        and r["severity_tag"] not in KNOWN_SEVERITY_TAGS
    ]


# v1.1 (post-Stage 1 S1.5-B): per-source canonical property lists. Used to WARN
# (not block) when an LLM-extracted rule references a property name that no
# real query response exposes. Lists were captured from live probes in Stage
# 1 (Pacific Continental project) and W7 D1 (Snowdon Towers walls/doors).
# A property absent from BOTH lists doesn't necessarily mean the rule is
# broken (custom Revit params, shared params, etc.) -- but it's exactly the
# kind of LLM hallucination that S1.5 caught ("Family and Type" doesn't exist
# in AECDM responses), so we surface it loudly.

_AECDM_PROPS_BY_CATEGORY: dict[str, frozenset[str]] = {
    "Rooms": frozenset({
        "Area", "Number", "Name", "Department", "Occupancy",
        "Unbounded Height", "Ceiling Height", "Perimeter", "Volume",
        "Family Name", "Element Name", "Element Context", "External ID",
        "Revit Element ID", "Revit Category Type Id", "Comments",
        "Base Finish", "Ceiling Finish", "Floor Finish", "Wall Finish",
        "Room Height", "Room Length", "Room Width", "Base Offset",
        "Limit Offset", "Computation Height", "Design Option",
        "IFC Predefined Type", "IfcGUID",
    }),
    "Doors": frozenset({
        "Family Name", "Type Mark", "Width", "Rough Width",
        "External ID", "Revit Element ID", "Revit Category Type Id",
        "Element Name", "Element Context", "Design Option",
        "Comments", "Mark", "Frame Material", "Frame Type",
        "Material", "Finish", "Sill Height", "Head Height",
    }),
    "Walls": frozenset({
        "Family Name", "External ID", "Revit Element ID",
        "Revit Category Type Id", "Element Name", "Element Context",
        "Comments", "Function", "Length", "Volume", "Area",
    }),
}

# Revit MCP path (revit_get_element_info) exposes a much richer surface --
# Type-level params (Fire Rating, Type Mark, Function, ...) are queried via
# typeId. Listing the most commonly used ones for the standard categories.
_REVIT_MCP_PROPS_BY_CATEGORY: dict[str, frozenset[str]] = {
    "Rooms": frozenset({
        "Area", "Number", "Name", "Department", "Occupancy",
        "Comments", "Base Offset", "Limit Offset", "Computation Height",
        "Unbounded Height", "Perimeter", "Volume", "Level",
        "Upper Limit", "Phase", "Phase Id", "IfcGUID",
        "areaMetric", "Unbounded Height (m)",  # metric mirrors added by RevitQueryAgent
    }),
    "Doors": frozenset({
        "Family Name", "Type Name", "Type Mark", "Mark",
        "Fire Rating", "Width", "Height", "Thickness", "Function",
        "Host Id", "Frame Material", "Finish", "Material",
        "Rough Width", "Rough Height", "Head Height", "Sill Height",
        "Comments", "Phase Created", "Phase Demolished",
        # W7 D1 RevitElementsQueryAgent host-hop namespace
        "host.Fire Rating", "host.Function", "host.Type Mark",
        "host.Width", "host.Family Name", "host.Type Name", "host.id",
    }),
    "Walls": frozenset({
        "Family Name", "Type Name", "Type Mark", "Mark",
        "Fire Rating", "Function", "Width", "Height", "Length",
        "Unconnected Height", "Base Offset", "Top Offset",
        "Base Constraint", "Top Constraint", "Location Line",
        "Structural", "Structural Usage", "Room Bounding",
        "Comments", "Phase Created", "Phase Demolished", "Area", "Volume",
    }),
}

# Common LLM-hallucinated property names with the canonical fix. Caught at
# install time so the operator sees this BEFORE running --check / --apply
# against a real project.
_LIKELY_HALLUCINATIONS: dict[str, str] = {
    "Family and Type": "Family Name (and/or Type Mark) — combined field doesn't exist",
    "Type Title": "Type Mark or Type Name",
    "Family Title": "Family Name",
    "Schedule Mark": "Type Mark or Mark",
    "Door Type": "Family Name or Type Name",
    "Wall Type": "Family Name or Type Name",
    "Room Type": "Family Name (typically null on Rooms; use Name + Occupancy)",
}


def _check_property_names(
    target_category: str | list[str],
    rules: list[dict[str, Any]],
) -> list[tuple[str, str, str, str]]:
    """Warn when a rule's `parameter` / `when_param` / `other_param` aren't in
    either canonical source list for the rule's category.

    Returns ``[(rule_id, field, property_name, hint), ...]``. Empty list when
    everything checks out.

    The check is purely advisory -- custom Revit shared params and project
    parameters are legitimate and won't appear in the canonical lists. But
    plausible-looking names that don't exist in either source are exactly the
    S1.5-B hallucination pattern, so we surface them.
    """
    targets = (
        [target_category] if isinstance(target_category, str) else list(target_category)
    )
    aecdm_union: set[str] = set()
    revit_union: set[str] = set()
    for cat in targets:
        aecdm_union |= _AECDM_PROPS_BY_CATEGORY.get(cat, frozenset())
        revit_union |= _REVIT_MCP_PROPS_BY_CATEGORY.get(cat, frozenset())
    known_union = aecdm_union | revit_union

    warnings: list[tuple[str, str, str, str]] = []
    for rule in rules:
        rid = rule.get("id", "<no-id>")
        # Per-rule category filter (W7 D1) narrows further
        rule_cat = rule.get("category")
        if isinstance(rule_cat, str):
            scoped_aecdm = _AECDM_PROPS_BY_CATEGORY.get(rule_cat, frozenset())
            scoped_revit = _REVIT_MCP_PROPS_BY_CATEGORY.get(rule_cat, frozenset())
            scoped_known = scoped_aecdm | scoped_revit
        else:
            scoped_known = known_union
        for field in ("parameter", "when_param", "other_param"):
            value = rule.get(field)
            if not isinstance(value, str) or not value:
                continue
            if value in scoped_known:
                continue
            hint = _LIKELY_HALLUCINATIONS.get(value)
            if hint is None:
                hint = "not in canonical AECDM or Revit MCP property list"
            warnings.append((rid, field, value, hint))
    return warnings


def _build_yaml_header(scenario: str, target_category: str, source: dict[str, Any] | None) -> str:
    """Build a YAML header comment block with provenance metadata."""
    lines = [
        f"# Rules generated by scripts/json_to_yaml.py for scenario '{scenario}'",
        f"# Target Revit category: {target_category}",
    ]
    if source:
        if doc := source.get("document"):
            lines.append(f"# Source document: {doc}")
        if ref := source.get("reference"):
            lines.append(f"# Source reference: {ref}")
        if dt := source.get("extracted_date"):
            lines.append(f"# Extracted: {dt}")
    lines.append("#")
    lines.append("# Review before committing -- the upstream extraction is LLM-assisted.")
    lines.append("")
    return "\n".join(lines)


def _strip_empty_optional_fields(rule_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove keys whose value is None so YAML output stays clean.

    Pydantic serializes optional Nones explicitly; we want
    `parameter: Area` not `parameter: Area\\npattern: null` for rules that
    don't use that field.
    """
    return {k: v for k, v in rule_dict.items() if v is not None}


def convert(input_path: Path, output_path: Path) -> int:
    if not input_path.exists():
        return _fail(2, f"Input file not found: {input_path}")

    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _fail(3, f"Invalid JSON: {e}")

    if not isinstance(raw, dict):
        return _fail(3, f"JSON root must be an object, got {type(raw).__name__}")

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        return _fail(3, "JSON must contain a 'rules' array")

    # 1. UNSUPPORTED_* check (before pydantic so we can give a clearer error)
    unsupported = _check_unsupported(rules_raw)
    if unsupported:
        print("ERROR: Rules with UNSUPPORTED_* requirement found.", file=sys.stderr)
        print(
            "       Extend bim_orchestrator/policies/rules_engine.py to add the evaluator,",
            file=sys.stderr,
        )
        print("       then re-run this script.", file=sys.stderr)
        for rid, req in unsupported:
            print(f"         - {rid}: {req}", file=sys.stderr)
        return 4

    # 2. Severity tag check
    bad_tags = _check_severity_tags(rules_raw)
    if bad_tags:
        print("ERROR: Unknown severity_tag(s) found.", file=sys.stderr)
        print(
            "       Add them to config/autonomy.yaml under 'severity_rules:'",
            file=sys.stderr,
        )
        print("       then re-run this script.", file=sys.stderr)
        for rid, tag in bad_tags:
            print(f"         - {rid}: {tag}", file=sys.stderr)
        return 5

    # 2.5. Property-name advisory (v1.1 post-S1.5-B). Pre-validation so
    # we can give better context messages -- pydantic just sees a string and
    # has no opinion on whether the property name actually exists.
    target_for_check = raw.get("target_category") or ""
    prop_warnings = _check_property_names(target_for_check, rules_raw)
    if prop_warnings:
        print(
            "WARNING: rule(s) reference property names that are not in the "
            "canonical AECDM or Revit MCP lists for the target category. "
            "This is often a benign custom shared param, but it's exactly "
            "the pattern S1.5 caught (e.g. 'Family and Type' is not a real "
            "field). Verify before running --check / --apply / --run-revit:",
            file=sys.stderr,
        )
        for rid, field, value, hint in prop_warnings:
            print(f"  - {rid} ({field}={value!r}): {hint}", file=sys.stderr)
        print(
            "  Conversion is proceeding -- WARNINGs don't block. To suppress, "
            "either rename the property to a canonical value or document the "
            "custom-shared-param assumption in `parameter_note`.",
            file=sys.stderr,
        )

    # 3. ASCII check (RevitMCPAddin mojibake guard)
    bad_ascii = _scan_ascii(rules_raw)
    if bad_ascii:
        print(
            "ERROR: Non-ASCII characters detected. Replace them per the prompt template:",
            file=sys.stderr,
        )
        print(
            "         >=  not  unicode-ge,    Sec.  not  unicode-section,    --  not  em-dash",
            file=sys.stderr,
        )
        for rid, field, snippet in bad_ascii:
            print(f"         - {rid} ({field}): {snippet!r}", file=sys.stderr)
        return 6

    # 4. Schema validation via pydantic (the authoritative shape)
    payload_for_validation = {
        "scenario": raw.get("scenario"),
        "target_category": raw.get("target_category"),
        "rules": rules_raw,
    }
    try:
        ruleset = RuleSet.model_validate(payload_for_validation)
    except ValidationError as e:
        print("ERROR: JSON does not match the RuleSet schema:", file=sys.stderr)
        print(e, file=sys.stderr)
        return 3

    # 5. Emit YAML
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rules_clean = [
        _strip_empty_optional_fields(rule.model_dump(exclude_unset=False))
        for rule in ruleset.rules
    ]
    yaml_body: dict[str, Any] = {
        "scenario": ruleset.scenario,
        "target_category": ruleset.target_category,
        "rules": rules_clean,
    }

    header = _build_yaml_header(
        ruleset.scenario, ruleset.target_category, raw.get("source")
    )
    yaml_dump = yaml.safe_dump(
        yaml_body,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,  # we already enforced ASCII upstream
        width=88,
    )
    output_path.write_text(header + yaml_dump, encoding="utf-8")

    # 6. Round-trip sanity check: load the YAML we just wrote and re-validate
    with output_path.open() as f:
        reload = RuleSet.model_validate(yaml.safe_load(f))
    if len(reload.rules) != len(ruleset.rules):
        return _fail(
            1,
            f"Round-trip mismatch: wrote {len(ruleset.rules)} rules, loaded {len(reload.rules)}",
        )

    # 7. Report unmachineable rules (informational only)
    unmach = raw.get("unmachineable") or []
    if unmach:
        print(
            f"NOTE: {len(unmach)} unmachineable rule(s) were skipped by the extractor:",
            file=sys.stderr,
        )
        for item in unmach:
            section = item.get("section", "?")
            text = item.get("text", "")[:60]
            print(f"         - {section}: {text}", file=sys.stderr)

    print(f"OK: wrote {len(ruleset.rules)} rules to {output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # v1.4-F4: deprecation banner on CLI invocation. Library calls (e.g.
    # tests/test_json_to_yaml.py importing _check_property_names) bypass
    # main() so they stay quiet — only end-users running the script
    # directly see the nudge to switch.
    print(
        "WARNING: scripts/json_to_yaml.py is deprecated as of v1.4-F4.\n"
        "         Prefer: extraction-skills/scripts/json_to_yaml.py "
        "(D0 skill-pack converter with heuristic warnings + execution_status split).\n"
        "         CLI: positional json_path + --rules-out (replaces --out).",
        file=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        prog="json_to_yaml",
        description="[DEPRECATED] Convert a rules JSON to a rules YAML file. "
        "Prefer extraction-skills/scripts/json_to_yaml.py for new work.",
    )
    parser.add_argument("input", type=Path, help="Path to the input JSON file")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output YAML path (e.g. config/rules.<scenario>.yaml)",
    )
    args = parser.parse_args(argv)
    return convert(args.input.resolve(), args.out.resolve())


if __name__ == "__main__":
    sys.exit(main())
