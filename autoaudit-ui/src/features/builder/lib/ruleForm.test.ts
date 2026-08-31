import { describe, expect, it } from "vitest";
import { formToRule, ruleToForm } from "./ruleForm";
import type { RuleDict } from "@/api/types";

/**
 * Each fixture below is already in the FULL canonical shape `formToRule`
 * emits (every optional field explicit, matching a `Rule.model_dump()`
 * from the server — see rules_schema.py) so the round-trip
 * `formToRule(ruleToForm(rule).state)` can be compared with plain
 * `toEqual` against the original fixture.
 */

const RULE_PRESENT_AND_NONEMPTY: RuleDict = {
  id: "doors.mark.present",
  parameter: "Mark",
  requirement: "present_and_nonempty",
  pattern: null,
  threshold: null,
  unit: null,
  when_param: null,
  when_pattern: null,
  other_param: null,
  operator: null,
  compare_kind: null,
  allowed_values: null,
  lookup: null,
  scope_filter: null,
  category: "Doors",
  severity_tag: "rule_violation",
  severity_level: "severity_medium",
  description: "Doors must have a Mark value",
  autofill: {
    strategy: "none",
    fallback: null,
    template: null,
    sequence_scope: null,
    normalize_kind: null,
    normalize_format: null,
    normalize_map: null,
    normalize_source: null,
    normalize_reference: null,
    host_param: null,
  },
  citation: { mode: "soft", source_filter: null, on_missing: "warn" },
  fixability: "manual",
  remediation: {
    action: "create_acc_issue",
    target_parameter: null,
    new_value: null,
    new_value_strategy: "inferred",
    comments_template: null,
    target: "instance",
    llm_safety_critical: false,
  },
  requires_human: false,
  rule_type: null,
  extraction_meta: null,
  bound_parameter: null,
};

const RULE_CANONICAL_REFERENCE: RuleDict = {
  id: "doors.material.reference",
  parameter: "Material",
  requirement: "canonical_format",
  pattern: null,
  threshold: null,
  unit: null,
  when_param: null,
  when_pattern: null,
  other_param: null,
  operator: null,
  compare_kind: null,
  allowed_values: null,
  lookup: null,
  scope_filter: null,
  category: "Doors",
  severity_tag: "rule_violation",
  severity_level: "severity_low",
  description: "Door material must be an approved value",
  autofill: {
    strategy: "normalize",
    fallback: null,
    template: null,
    sequence_scope: null,
    normalize_kind: "reference",
    normalize_format: null,
    normalize_map: null,
    normalize_source: null,
    normalize_reference: "approved_materials",
    host_param: null,
  },
  citation: { mode: "soft", source_filter: null, on_missing: "warn" },
  fixability: "auto",
  remediation: {
    action: "set_parameter",
    target_parameter: null,
    new_value: null,
    new_value_strategy: "inferred",
    comments_template: null,
    target: "instance",
    llm_safety_critical: false,
  },
  requires_human: false,
  rule_type: null,
  extraction_meta: null,
  bound_parameter: null,
};

const RULE_NUMERIC_COMPARE_UNIT: RuleDict = {
  id: "doors.width.min",
  parameter: "Width",
  requirement: "numeric_compare",
  pattern: null,
  threshold: 900,
  unit: "mm",
  when_param: null,
  when_pattern: null,
  other_param: null,
  operator: ">=",
  compare_kind: null,
  allowed_values: null,
  lookup: null,
  scope_filter: null,
  category: "Doors",
  severity_tag: "rule_violation",
  severity_level: "severity_high",
  description: "Egress door width must be at least 900mm",
  autofill: {
    strategy: "none",
    fallback: null,
    template: null,
    sequence_scope: null,
    normalize_kind: null,
    normalize_format: null,
    normalize_map: null,
    normalize_source: null,
    normalize_reference: null,
    host_param: null,
  },
  citation: { mode: "soft", source_filter: null, on_missing: "warn" },
  fixability: "manual",
  remediation: {
    action: "create_acc_issue",
    target_parameter: null,
    new_value: null,
    new_value_strategy: "inferred",
    comments_template: null,
    target: "instance",
    llm_safety_critical: false,
  },
  requires_human: false,
  rule_type: null,
  extraction_meta: null,
  bound_parameter: null,
};

const RULE_RELATION_COMPARE_LOOKUP: RuleDict = {
  id: "doors.fire_rating.ibc716",
  parameter: "Fire Rating",
  requirement: "relation_compare",
  pattern: null,
  threshold: null,
  unit: null,
  when_param: null,
  when_pattern: null,
  other_param: null,
  operator: ">=",
  compare_kind: "fire_rating",
  allowed_values: null,
  lookup: "ibc716",
  scope_filter: null,
  category: "Doors",
  severity_tag: "rule_violation",
  severity_level: "severity_high",
  description: "Door fire rating must satisfy IBC Table 716.1 for the host wall",
  autofill: {
    strategy: "none",
    fallback: null,
    template: null,
    sequence_scope: null,
    normalize_kind: null,
    normalize_format: null,
    normalize_map: null,
    normalize_source: null,
    normalize_reference: null,
    host_param: null,
  },
  citation: { mode: "soft", source_filter: null, on_missing: "warn" },
  fixability: "manual",
  remediation: {
    action: "create_acc_issue",
    target_parameter: null,
    new_value: null,
    new_value_strategy: "inferred",
    comments_template: null,
    target: "instance",
    llm_safety_critical: false,
  },
  requires_human: false,
  rule_type: null,
  extraction_meta: null,
  bound_parameter: null,
};

const RULE_MATCHES_REGEX_SCOPE_NEGATE: RuleDict = {
  id: "doors.name.no_copy_of",
  parameter: "Name",
  requirement: "not_matches_regex",
  pattern: "(?i)copy of",
  threshold: null,
  unit: null,
  when_param: null,
  when_pattern: null,
  other_param: null,
  operator: null,
  compare_kind: null,
  allowed_values: null,
  lookup: null,
  scope_filter: { param: "IsExternal", pattern: "(?i)^(true|yes)$" },
  category: "Doors",
  severity_tag: "rule_violation",
  severity_level: "severity_low",
  description: "External door names must not contain 'Copy of'",
  autofill: {
    strategy: "none",
    fallback: null,
    template: null,
    sequence_scope: null,
    normalize_kind: null,
    normalize_format: null,
    normalize_map: null,
    normalize_source: null,
    normalize_reference: null,
    host_param: null,
  },
  citation: { mode: "soft", source_filter: null, on_missing: "warn" },
  fixability: "manual",
  remediation: {
    action: "create_acc_issue",
    target_parameter: null,
    new_value: null,
    new_value_strategy: "inferred",
    comments_template: null,
    target: "instance",
    llm_safety_critical: false,
  },
  requires_human: false,
  rule_type: null,
  extraction_meta: null,
  bound_parameter: null,
};

const LEGACY_RULE: RuleDict = {
  ...RULE_NUMERIC_COMPARE_UNIT,
  id: "doors.width.min.legacy",
  requirement: "numeric_min",
};

describe("ruleForm round-trip", () => {
  const cases: Array<[string, RuleDict]> = [
    ["present_and_nonempty", RULE_PRESENT_AND_NONEMPTY],
    ["canonical_format + reference", RULE_CANONICAL_REFERENCE],
    ["numeric_compare + unit", RULE_NUMERIC_COMPARE_UNIT],
    ["relation_compare + lookup", RULE_RELATION_COMPARE_LOOKUP],
    ["matches_regex + scope_filter + negate", RULE_MATCHES_REGEX_SCOPE_NEGATE],
  ];

  it.each(cases)("%s round-trips through ruleToForm -> formToRule", (_label, rule) => {
    const result = ruleToForm(rule);
    expect(result.legacy).toBe(false);
    if (result.legacy) return; // narrows for TS
    expect(formToRule(result.state)).toEqual(rule);
  });

  it("flags a true legacy requirement (outside the offered + pattern-family set) instead of editing it", () => {
    const result = ruleToForm(LEGACY_RULE);
    expect(result).toEqual({ legacy: true, raw: LEGACY_RULE });
  });

  it("derives relationMode from the presence of a lookup table", () => {
    const result = ruleToForm(RULE_RELATION_COMPARE_LOOKUP);
    expect(result.legacy).toBe(false);
    if (result.legacy) return;
    expect(result.state.relationMode).toBe("lookup");
    expect(result.state.lookup).toBe("ibc716");
  });

  it("keeps a blank threshold as null instead of synthesizing 0", () => {
    // An omitted value must never be conflated with an authored 0: the
    // backend blocks `>= null` (no limit declared) but ACCEPTS `> 0` (the
    // positive_number check). Coercing "" to 0 here erased that distinction,
    // so an unfinished "width must be at least 900" silently saved as
    // "width >= 0" — a rule that reports 100% compliance forever.
    const result = ruleToForm(RULE_NUMERIC_COMPARE_UNIT);
    expect(result.legacy).toBe(false);
    if (result.legacy) return;
    const blanked = formToRule({ ...result.state, threshold: "" });
    expect(blanked.threshold).toBeNull();
  });

  it("still carries an explicitly authored 0 threshold", () => {
    // The other half of the same distinction: `> 0` is a real requirement
    // (config/rules.va_bim.yaml — "room area must be positive").
    const result = ruleToForm(RULE_NUMERIC_COMPARE_UNIT);
    expect(result.legacy).toBe(false);
    if (result.legacy) return;
    const zero = formToRule({ ...result.state, threshold: "0", operator: ">" });
    expect(zero.threshold).toBe(0);
  });

  it("folds not_matches_regex into the matches_regex dropdown + negate checkbox", () => {
    const result = ruleToForm(RULE_MATCHES_REGEX_SCOPE_NEGATE);
    expect(result.legacy).toBe(false);
    if (result.legacy) return;
    expect(result.state.requirement).toBe("matches_regex");
    expect(result.state.patternNegate).toBe(true);
    expect(result.state.patternSkipIfEmpty).toBe(false);
  });
});
