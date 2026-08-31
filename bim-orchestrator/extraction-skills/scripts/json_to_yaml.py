"""Convert ExtractionAgent JSON output → executable RuleSet YAML.

D0 Skill Pack post-processor. Reads a JSON file (the response you
copy out of Claude Desktop after running the extraction skill against
a BEP), validates each rule against the pydantic ``Rule`` schema,
splits by ``execution_status``, and writes:

  * ``config/rules.<scenario>.yaml`` — only ``executable`` rules,
    immediately consumable by ``bim-orchestrator --check`` etc.
  * ``runs/extraction_review_<timestamp>.md`` — every rule that landed
    in ``needs_domain_mapping`` or ``not_model_checkable``, plus any
    ``executable`` rule with ``confidence < 0.75`` that got
    auto-bumped to ``fixability: manual``.

Usage::

    python extraction-skills/scripts/json_to_yaml.py extracted.json
    python extraction-skills/scripts/json_to_yaml.py extracted.json \\
        --rules-out config/rules.my_scenario.yaml \\
        --review-out runs/extraction_review_my_scenario.md

The JSON may be a single RuleSet or an envelope with multiple sets:
``{"rulesets": [...]}``. Either form is accepted.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

# Allow running as a loose script from anywhere in the repo
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bim_orchestrator.policies.ost_catalog import OSTCatalog  # noqa: E402
from bim_orchestrator.policies.rules_schema import (  # noqa: E402
    Rule,
    RuleAutofill,
    RuleSet,
)

# ---------------------------------------------------------------------------
# Defaults — derived from rule_type when fixability/remediation are omitted
# ---------------------------------------------------------------------------

RULE_TYPE_FIXABILITY_DEFAULTS: dict[str, str] = {
    "parameter_completeness": "auto",
    "value_constraint": "manual",
    "naming_convention": "auto",
    "uniqueness_constraint": "auto",
    "cross_element_relationship": "manual",
}


CONFIDENCE_BUMP_THRESHOLD = 0.75

# v1.4 D0.2 — keywords that signal a subset-applicability clause. When
# a rule's description mentions any of these but the rule has no
# ``when_param`` filter, the validator emits a warning suggesting the
# rule may be over-scoped (firing on every element). Not exhaustive —
# extend as more BEPs surface new scope vocabulary.
SCOPE_FILTER_KEYWORDS = (
    "means of egress", "egress", "exit access", "exit discharge",
    "occupied space", "habitable", "dwelling", "sleeping unit",
    "fire-rated", "fire rated", "smoke-rated", "smoke rated",
    "load-bearing", "load bearing", "exterior wall", "interior wall",
    "occupancy group", "group i-", "group r-", "group a-", "group b",
    "corridor", "stairwell", "exit corridor", "new construction",
    "renovation", "existing", "class a finish", "class b finish",
    "residential", "commercial", "institutional",
)

# v1.4-D0.5: with Rule.unit explicit, the old mm-as-feet / metres-as-
# feet detectors are obsolete — the engine converts at compare time.
# A single advisory check remains: if a numeric rule uses a parameter
# whose Revit raw unit we know (REVIT_STORAGE_UNITS keys) but doesn't
# declare a unit, warn the user. They might be relying on raw-feet
# comparison by accident.
NUMERIC_REQUIREMENTS = (
    "numeric_min", "numeric_min_conditional", "positive_number"
)


def _envelope_to_rulesets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept either a single RuleSet dict or ``{"rulesets": [...]}``."""
    if "rulesets" in payload and isinstance(payload["rulesets"], list):
        return list(payload["rulesets"])
    return [payload]


def _semantic_issues(rule: dict[str, Any]) -> list[str]:
    """Cross-field / compilable checks pydantic cannot express (P1-AUTHOR-01).

    Delegates to ``rule_builder_core.validate_rule`` — the SAME validator the
    Rule Builder UI, ``PUT /rules/{name}`` and the IDS import already run — so
    "valid rule" means one thing across every authoring surface. Re-typing the
    checks here is exactly how the surfaces drifted apart in the first place.

    Import is local and failure is soft: this script is also runnable
    standalone from the skill pack, where the package may not be importable.
    Losing the check degrades to the old behaviour rather than breaking the
    converter outright.
    """
    try:
        from bim_orchestrator.rule_builder_core import validate_rule
    except Exception:  # pragma: no cover - standalone skill-pack use
        return []
    try:
        result = validate_rule(rule, is_geometry=False)
    except Exception as exc:  # pragma: no cover - validator itself misbehaving
        return [f"semantic validation failed: {exc}"]
    return [f"{issue.field}: {issue.message}" for issue in result.errors]


def _normalise_rule(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill in v1.3-required fields the LLM might omit.

    Mainly:
      * ``autofill`` defaults to ``{"strategy": "none"}``.
      * ``fixability`` derives from ``rule_type`` if omitted.
      * Confidence below threshold force-bumps fixability to manual
        + sets ``requires_human=True``.
      * Skips rules with ``execution_status != "executable"`` — those
        go to the review queue instead.
    """
    if "autofill" not in raw:
        raw["autofill"] = {"strategy": "none"}

    meta = raw.get("extraction_meta") or {}
    confidence = float(meta.get("confidence", 0.0))
    status = meta.get("execution_status", "executable")

    # rule_type → fixability default when LLM didn't pick one
    rule_type = raw.get("rule_type")
    if not raw.get("fixability") and rule_type in RULE_TYPE_FIXABILITY_DEFAULTS:
        raw["fixability"] = RULE_TYPE_FIXABILITY_DEFAULTS[rule_type]

    # Low-confidence override — safer than trusting LLM's own fixability call
    if status == "executable" and confidence < CONFIDENCE_BUMP_THRESHOLD:
        raw["fixability"] = "manual"
        raw["requires_human"] = True

    return raw


def _validate_rule(raw: dict[str, Any]) -> tuple[Rule | None, str | None]:
    """Return (Rule, None) on success or (None, error_message) on failure."""
    try:
        return Rule.model_validate(raw), None
    except ValidationError as exc:
        return None, str(exc)


def _fragmentation_warnings(rules: list[Rule]) -> list[str]:
    """Detect over-fragmentation across a ruleset.

    Two or more executable rules sharing the exact ``(parameter,
    requirement, category)`` triple is a strong signal Claude split
    one logical convention into multiple mechanical checks. Almost
    always the right move is to fold them into a single composite
    regex (see SRA worked example in extraction_prompt.md).

    This is per-ruleset, not per-rule, so it runs once after all rules
    have been validated. Skips ruleset with < 2 rules.
    """
    if len(rules) < 2:
        return []
    by_key: dict[tuple[str, str, str | None], list[str]] = {}
    for r in rules:
        key = (r.parameter, r.requirement, r.category)
        by_key.setdefault(key, []).append(r.id)
    warnings: list[str] = []
    for (param, req, cat), ids in by_key.items():
        if len(ids) < 2:
            continue
        cat_part = f", category={cat!r}" if cat else ""
        warnings.append(
            f"Rules {ids} share parameter={param!r}, requirement={req!r}"
            f"{cat_part}. Likely over-fragmentation — one element will produce "
            f"{len(ids)} duplicate findings. Consider folding into ONE composite "
            f"check (see extraction_prompt.md 'Atomicity' section)."
        )
    return warnings


def _heuristic_warnings(rule: Rule) -> list[str]:
    """Surface likely extraction mistakes that pass schema but smell wrong.

    Returns a list of warning strings; empty when the rule looks clean.
    Advisory — they print to stderr and into the review markdown but
    don't block the YAML write by default. Pass ``--strict`` (if
    introduced) or hand-edit the JSON to address them.
    """
    # Import here to avoid a hard dep on the runtime side just for
    # heuristic lookups (the validator is a dev-tool script).
    from bim_orchestrator.policies.revit_units import REVIT_STORAGE_UNITS

    warnings: list[str] = []

    # 1. Missing-unit advisory — a numeric rule on a parameter with a
    # known Revit storage unit but no explicit `unit` field. The rule
    # will run, comparing raw values directly to the threshold; if the
    # source quoted metric, that comparison is silently wrong.
    if (
        rule.requirement in NUMERIC_REQUIREMENTS
        and rule.parameter in REVIT_STORAGE_UNITS
        and rule.unit is None
        and rule.threshold is not None
    ):
        revit_raw = REVIT_STORAGE_UNITS[rule.parameter]
        warnings.append(
            f"{rule.id}: rule uses parameter '{rule.parameter}' (Revit raw: "
            f"{revit_raw}) with threshold {rule.threshold} but no `unit` "
            f"declared. If source quotes metric, add e.g. `\"unit\": \"m\"` "
            f"so the engine converts at compare-time. Leaving `unit` unset "
            f"means raw {revit_raw} comparison."
        )

    # 2. Scope-filter heuristic — if the description mentions a subset
    # keyword but the rule has no when_param, the rule probably fires
    # on every element in the category (false positives across the board).
    desc_lower = (rule.description or "").lower()
    if (
        any(k in desc_lower for k in SCOPE_FILTER_KEYWORDS)
        and not rule.when_param
        and rule.requirement != "fire_rating_ge"  # cross-element uses other_param
    ):
        warnings.append(
            f"{rule.id}: description mentions a subset-applicability clause "
            f"(egress / occupancy / fire-rated / ...), but the rule has no "
            f"when_param filter — likely fires on every element in category. "
            f"Add when_param + when_pattern, or mark execution_status as "
            f"not_model_checkable."
        )
    return warnings


def _split_by_status(
    ruleset_raw: dict[str, Any],
) -> tuple[list[Rule], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (executable_rules, review_items, invalid_items, heuristic_warnings).

    ``review_items`` carries the original raw dict + reason so the
    Markdown report can show the source quote etc. without losing data.
    ``heuristic_warnings`` are advisory strings about likely extraction
    mistakes that still passed pydantic — printed to stderr by main().
    """
    executable: list[Rule] = []
    review: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    warnings: list[str] = []

    for raw in ruleset_raw.get("rules", []) or []:
        meta = raw.get("extraction_meta") or {}
        status = meta.get("execution_status", "executable")
        if status != "executable":
            review.append({"raw": raw, "reason": status, "details": meta})
            continue
        normalised = _normalise_rule(dict(raw))
        rule, err = _validate_rule(normalised)
        if rule is None:
            invalid.append({"raw": raw, "error": err})
            continue
        # P1-AUTHOR-01 (2026-07-26 independent review): pydantic above checks
        # SHAPE only — it never compiles a regex or runs a cross-field rule. An
        # extracted rule with `pattern: "["` therefore validated, was written to
        # config/ under a green "saved", and only fell over at audit time, where
        # every affected element lands in manual_review. The tool was wrong
        # about what it had just saved, and the operator reads that as a
        # model-data problem rather than a broken rule.
        #
        # A-01 fixed this for the IDS import tab; the fix belongs at the SHARED
        # gate, not once per surface. Every authoring path that persists
        # extracted rules passes through here (Rule Builder save, Extraction
        # Review, PDF extraction), so one check covers all three — and a
        # semantic failure lands in the same `invalid` bucket those callers
        # already refuse to write, so none of them needs a new contract.
        semantic = _semantic_issues(normalised)
        if semantic:
            invalid.append({"raw": raw, "error": "; ".join(semantic)})
            continue
        executable.append(rule)
        warnings.extend(_heuristic_warnings(rule))
    # v1.4-D0.7: ruleset-level fragmentation check (after all rules validated).
    warnings.extend(_fragmentation_warnings(executable))
    return executable, review, invalid, warnings


def _resolve_category(label: str, catalog: OSTCatalog) -> str:
    """Normalise a category label via the catalog (handle aliases / casing)."""
    if not label:
        return label
    entry = catalog.find(label)
    return entry.display if entry else label


def _build_ruleset(
    raw: dict[str, Any], rules: list[Rule], catalog: OSTCatalog
) -> RuleSet:
    target_raw = raw.get("target_category", "")
    if isinstance(target_raw, str):
        target = _resolve_category(target_raw, catalog) if target_raw else ""
    elif isinstance(target_raw, list):
        target = [_resolve_category(t, catalog) for t in target_raw if t]
    else:
        target = ""
    # Normalise per-rule category too — Claude sometimes emits "Walls" vs "walls"
    for r in rules:
        if r.category:
            r_copy = r.model_copy(update={"category": _resolve_category(r.category, catalog)})
            # pydantic frozen=False by default — but Rule has no Config.frozen,
            # so direct mutation is fine; using model_copy keeps it pure.
            rules[rules.index(r)] = r_copy
    return RuleSet(
        scenario=raw.get("scenario") or "extracted_unnamed",
        target_category=target or "",
        rules=rules,
        metadata=raw.get("metadata"),
    )


def _ruleset_to_yaml(ruleset: RuleSet) -> str:
    """Dump RuleSet → YAML string. Strips None-valued fields for cleanliness."""
    data = ruleset.model_dump(exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _render_review_md(
    review_items: list[dict[str, Any]],
    invalid_items: list[dict[str, Any]],
    *,
    scenario: str,
    warnings: list[str] | None = None,
    yaml_blocked: bool = False,
) -> str:
    warnings = warnings or []
    header_status = (
        "YAML WRITE BLOCKED (heuristic warnings + --strict — fix JSON or drop --strict)"
        if yaml_blocked
        else "YAML written successfully"
    )
    lines: list[str] = [
        f"# Extraction review — scenario `{scenario}`",
        "",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"- **Status**: {header_status}",
        f"- **Not-checkable / needs-mapping**: {len(review_items)}",
        f"- **Schema-invalid (rejected)**: {len(invalid_items)}",
        f"- **Heuristic warnings**: {len(warnings)}",
        "",
    ]
    if warnings:
        section_title = (
            "## Heuristic warnings (BLOCKING — --strict)"
            if yaml_blocked
            else "## Heuristic warnings (advisory)"
        )
        lines.append(section_title + "\n")
        if yaml_blocked:
            lines.append(
                "_These rules passed pydantic validation but tripped one of "
                "the heuristics; the executable YAML was NOT written "
                "(`--strict` was passed). Fix the JSON or drop `--strict`._\n"
            )
        else:
            lines.append(
                "_These rules passed schema validation but tripped one of "
                "the heuristics — review before runtime use. Pass `--strict` "
                "if you want them to block future YAML writes._\n"
            )
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
    if review_items:
        lines.append("## Items requiring human review\n")
        for i, item in enumerate(review_items, 1):
            raw = item["raw"]
            meta = item.get("details") or {}
            lines.append(f"### {i}. `{raw.get('id', 'no-id')}` — {item['reason']}\n")
            lines.append(f"- **Description**: {raw.get('description', '')}")
            lines.append(f"- **Source quote**: > {meta.get('source_text', '')}")
            lines.append(f"- **Location**: {meta.get('source_location', '')}")
            lines.append(f"- **Confidence**: {meta.get('confidence', '?')}")
            if meta.get("status_reason"):
                lines.append(f"- **Why not checkable**: {meta['status_reason']}")
            lines.append("")
    if invalid_items:
        lines.append("## Schema-invalid rules (need fix-up before re-running)\n")
        for i, item in enumerate(invalid_items, 1):
            raw = item["raw"]
            lines.append(f"### {i}. `{raw.get('id', 'no-id')}`\n")
            lines.append("```\n" + item["error"] + "\n```")
            lines.append("")
    if not review_items and not invalid_items:
        lines.append("_All extracted rules were directly executable — nothing to review._\n")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert ExtractionAgent JSON → executable rules YAML + review report"
    )
    parser.add_argument("json_path", type=Path, help="LLM-produced JSON file")
    parser.add_argument(
        "--rules-out", type=Path, default=None,
        help="Output YAML path. Default: config/rules.<scenario>.yaml",
    )
    parser.add_argument(
        "--review-out", type=Path, default=None,
        help="Output review markdown. Default: runs/extraction_review_<scenario>_<ts>.md",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=_REPO_ROOT,
        help=f"Repo root for relative paths (default: {_REPO_ROOT})",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Block YAML write when any heuristic warning fires. By default "
        "the validator emits advisory warnings (stderr + review markdown) "
        "but still writes the YAML — pass --strict for hard-block behaviour "
        "when you want CI to fail on lint-level issues.",
    )
    args = parser.parse_args(argv)

    payload = json.loads(args.json_path.read_text(encoding="utf-8"))
    rulesets_raw = _envelope_to_rulesets(payload)
    catalog = OSTCatalog.load()

    total_exec = 0
    total_review = 0
    total_invalid = 0

    total_warnings = 0
    blocked_scenarios: list[str] = []

    for rs_raw in rulesets_raw:
        scenario = rs_raw.get("scenario") or "extracted_unnamed"
        executable, review, invalid, warnings = _split_by_status(rs_raw)

        # v1.4-D0.5: advisory by default, block only under --strict.
        # Earlier (D0.4a) tried hard-block by default but the false-
        # positive rate was too high; with Rule.unit explicit, the
        # validator's role is now mostly lint-style. Review markdown
        # still surfaces every warning regardless.
        block_yaml = bool(warnings) and args.strict

        ruleset = _build_ruleset(rs_raw, executable, catalog)
        rules_out = args.rules_out or (
            args.repo_root / "config" / f"rules.{scenario}.yaml"
        )
        if not block_yaml:
            rules_out.parent.mkdir(parents=True, exist_ok=True)
            rules_out.write_text(_ruleset_to_yaml(ruleset), encoding="utf-8")
        else:
            blocked_scenarios.append(scenario)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        review_out = args.review_out or (
            args.repo_root / "runs" / f"extraction_review_{scenario}_{ts}.md"
        )
        review_out.parent.mkdir(parents=True, exist_ok=True)
        review_out.write_text(
            _render_review_md(
                review, invalid,
                scenario=scenario, warnings=warnings,
                yaml_blocked=block_yaml,
            ),
            encoding="utf-8",
        )

        total_exec += 0 if block_yaml else len(executable)
        total_review += len(review)
        total_invalid += len(invalid)
        total_warnings += len(warnings)

        print(f"[{scenario}]")
        if block_yaml:
            print(f"  executable rules : {len(executable):>3} -- BLOCKED (heuristic warnings + --strict; drop --strict to ship)")
        else:
            print(f"  executable rules : {len(executable):>3} -> {rules_out}")
        print(f"  review items     : {len(review):>3}")
        print(f"  schema invalid   : {len(invalid):>3}")
        print(f"  heuristic warns  : {len(warnings):>3}")
        print(f"  review report    : {review_out}")
        if warnings:
            header = (
                "  -- Heuristic warnings (BLOCKING because --strict passed) --"
                if block_yaml
                else "  -- Heuristic warnings (advisory; --strict to block) --"
            )
            print(header, file=sys.stderr)
            for w in warnings:
                print(f"     {w}", file=sys.stderr)

    print(
        f"\nTotal: {total_exec} executable / {total_review} review / "
        f"{total_invalid} invalid / {total_warnings} warnings across "
        f"{len(rulesets_raw)} ruleset(s)."
    )
    if blocked_scenarios:
        print(
            f"\nBLOCKED scenarios (YAML NOT written): {', '.join(blocked_scenarios)}.\n"
            f"Fix the JSON to address the heuristic warnings, or drop "
            f"--strict to ship anyway.",
            file=sys.stderr,
        )
    if total_invalid > 0:
        return 2
    if blocked_scenarios:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
