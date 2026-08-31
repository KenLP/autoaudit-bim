/**
 * Pure form <-> Rule/GeometryRule dict mapping (M2 Builder page).
 *
 * `RuleDict` / `GeometryRuleDict` (api/types.ts) are hand-written mirrors of
 * the REAL pydantic schema in
 * `bim-orchestrator/src/bim_orchestrator/policies/rules_schema.py` — field
 * names and the "handle" derivation logic below were read directly from
 * that file + the equivalent Rule Builder logic in
 * `bim-orchestrator/streamlit_app/app.py` (`_render_rule_form`,
 * `_render_geometry_rule_form`, `_save_rule_to_yaml`) so a rule authored
 * here loads/evaluates identically server-side. No business logic (unit
 * conversion, normalize preview, validation) lives here — B10: this module
 * only reshapes form state into the schema's plain-old-dict shape; the
 * server is the single source of truth for what a value MEANS.
 *
 * `formToRule` always emits a FULL canonical `RuleDict` (every optional
 * field explicitly present, defaulted to `null`/pydantic default rather
 * than omitted) so `formToRule(ruleToForm(rule).state)` round-trips
 * byte-for-byte against a `rule` that was already in that full shape (see
 * ruleForm.test.ts) — the same contract a `Rule.model_dump()` from the
 * server would produce.
 */

import type {
  AutofillStrategy,
  ClearanceDirection,
  CompareKind,
  ComparisonOperator,
  Fixability,
  GeometryCheckType,
  GeometryReferenceSource,
  GeometryRuleDict,
  NewValueStrategy,
  OfferedRequirement,
  RemediationAction,
  RequirementKind,
  RuleAutofillDict,
  RuleDict,
  RuleRemediationDict,
  SeverityLevel,
  WriteTarget,
} from "@/api/types";
import { OFFERED_REQUIREMENTS, PATTERN_FAMILY_REQUIREMENTS } from "@/api/types";

export type HandleOption =
  | "issue"
  | "normalize"
  | "compose_template"
  | "inherit_from_host"
  | "set_fixed";

export type WriteTargetOption = "auto" | "instance" | "type" | "family" | "rename_type";

export type NormalizeKindOption =
  | "auto"
  | "duration"
  | "length"
  | "area"
  | "fire_rating"
  | "family_name"
  | "template"
  | "map"
  | "reference";

export type RelationMode = "direct" | "lookup";

/** Output format defaults per quantity dimension (mirrors app.py
 *  `NORMALIZE_DEFAULT_FMT`) — used only when the author leaves the format
 *  field blank for a dimension kind. */
export const NORMALIZE_DEFAULT_FORMAT: Record<string, string> = {
  duration: "{h}-hour",
  fire_rating: "{h}-hour",
  length: "{mm} mm",
  area: "{m2} m²",
};

export interface RuleFormState {
  id: string;
  description: string;
  category: string;
  /** The catalog parameter name, OR free text when `boundParameter` is set
   *  via the "Other (shared parameter)" escape hatch. */
  parameter: string;
  /** Non-empty only when the author picked "Other (shared parameter)…" —
   *  maps to `Rule.bound_parameter`. */
  boundParameter: string;
  requirement: OfferedRequirement;
  pattern: string;
  patternNegate: boolean;
  patternSkipIfEmpty: boolean;
  operator: ComparisonOperator;
  /** Kept as a string for the input; parsed on submit ("" -> 0). */
  threshold: string;
  unit: string;
  relationMode: RelationMode;
  otherParam: string;
  compareKind: CompareKind;
  lookup: string;
  scopeFilterParam: string;
  scopeFilterPattern: string;
  severityLevel: SeverityLevel;
  severityTag: string;
  handle: HandleOption;
  writeTarget: WriteTargetOption;
  normalizeKind: NormalizeKindOption;
  normalizeFormat: string;
  normalizeMap: Record<string, string>;
  normalizeSource: string;
  normalizeReference: string;
  /** "Inherit from host when empty" checkbox under the normalize handle —
   *  upgrades `normalize` -> the compound `inherit_then_normalize` strategy
   *  and forces `requirement = canonical_format` (v1.4-K22). */
  inheritHostWhenEmpty: boolean;
  /** Host parameter to inherit — shared by the `inherit_from_host` handle
   *  and the normalize "inherit when empty" checkbox (only one is active
   *  at a time, so one field is enough). Blank = same-named inheritance. */
  hostParam: string;
  composeTemplate: string;
  fixedValue: string;
}

export type RuleFormResult =
  | { legacy: false; state: RuleFormState }
  | { legacy: true; raw: RuleDict };

function foldRequirement(requirement: string): string {
  return (PATTERN_FAMILY_REQUIREMENTS as readonly string[]).includes(requirement)
    ? "matches_regex"
    : requirement;
}

function isOffered(requirement: string): requirement is OfferedRequirement {
  return (OFFERED_REQUIREMENTS as readonly string[]).includes(requirement);
}

function emptyAutofill(): RuleAutofillDict {
  return {
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
  };
}

function emptyRemediation(): RuleRemediationDict {
  return {
    action: "create_acc_issue",
    target_parameter: null,
    new_value: null,
    new_value_strategy: "inferred",
    comments_template: null,
    target: "instance",
    llm_safety_critical: false,
  };
}

function fullAutofill(
  strategy: AutofillStrategy,
  overrides: Partial<RuleAutofillDict> = {},
): RuleAutofillDict {
  return { ...emptyAutofill(), strategy, ...overrides };
}

function fullRemediation(
  action: RemediationAction,
  target: WriteTarget,
  overrides: Partial<RuleRemediationDict> = {},
): RuleRemediationDict {
  return { ...emptyRemediation(), action, target, ...overrides };
}

/** Default `RuleFormState` for a brand-new rule (Draft section starts here,
 *  overlaid with a server draft or a loaded rule). */
export function emptyRuleFormState(): RuleFormState {
  return {
    id: "",
    description: "",
    category: "",
    parameter: "",
    boundParameter: "",
    requirement: "present_and_nonempty",
    pattern: "",
    patternNegate: false,
    patternSkipIfEmpty: false,
    operator: ">=",
    threshold: "",
    unit: "",
    relationMode: "direct",
    otherParam: "",
    compareKind: "numeric",
    lookup: "",
    scopeFilterParam: "",
    scopeFilterPattern: "",
    severityLevel: "severity_medium",
    severityTag: "rule_violation",
    handle: "issue",
    writeTarget: "auto",
    normalizeKind: "auto",
    normalizeFormat: "",
    normalizeMap: {},
    normalizeSource: "",
    normalizeReference: "",
    inheritHostWhenEmpty: false,
    hostParam: "",
    composeTemplate: "",
    fixedValue: "",
  };
}

/**
 * A loaded rule whose (folded) requirement isn't one of the 6 OFFERED
 * requirements is "legacy" — the engine still evaluates it, but the Builder
 * shows it read-only rather than risk silently rewriting an old
 * numeric_min/fire_rating_ge/etc. rule into something subtly different
 * (spec 3b: "legacy — preserved, not editable here").
 */
export function ruleToForm(rule: RuleDict): RuleFormResult {
  const foldedRequirement = foldRequirement(rule.requirement);
  if (!isOffered(foldedRequirement)) {
    return { legacy: true, raw: rule };
  }

  const af = rule.autofill ?? emptyAutofill();
  const rem = rule.remediation ?? emptyRemediation();

  // Mirrors app.py's `cur_handle` derivation (_render_rule_form).
  let handle: HandleOption = "issue";
  if (af.strategy === "compose_template") {
    handle = "compose_template";
  } else if (af.strategy === "inherit_from_host") {
    handle = "inherit_from_host";
  } else if (af.strategy === "inherit_then_normalize") {
    handle = "normalize";
  } else if (rem.new_value_strategy === "fixed") {
    handle = "set_fixed";
  } else if (
    af.strategy === "normalize" ||
    rem.action === "set_parameter" ||
    rem.action === "rename_element"
  ) {
    handle = "normalize";
  }

  let writeTarget: WriteTargetOption = "auto";
  if (rem.action === "rename_element") {
    writeTarget = rem.target === "type" ? "rename_type" : "family";
  } else if (rem.target) {
    writeTarget = rem.target;
  }

  const relationMode: RelationMode = rule.lookup ? "lookup" : "direct";

  const state: RuleFormState = {
    id: rule.id ?? "",
    description: rule.description ?? "",
    category: rule.category ?? "",
    parameter: rule.parameter ?? "",
    boundParameter: rule.bound_parameter ?? "",
    requirement: foldedRequirement as OfferedRequirement,
    pattern: rule.pattern ?? "",
    patternNegate: rule.requirement === "not_matches_regex",
    patternSkipIfEmpty: rule.requirement === "matches_regex_if_present",
    operator: rule.operator ?? ">=",
    threshold: rule.threshold != null ? String(rule.threshold) : "",
    unit: rule.unit ?? "",
    relationMode,
    otherParam: rule.other_param ?? "",
    compareKind: rule.compare_kind ?? "numeric",
    lookup: rule.lookup ?? "",
    scopeFilterParam: rule.scope_filter?.param ?? "",
    scopeFilterPattern: rule.scope_filter?.pattern ?? "",
    severityLevel: rule.severity_level ?? "severity_medium",
    severityTag: rule.severity_tag || "rule_violation",
    handle,
    writeTarget,
    normalizeKind: (af.normalize_kind as NormalizeKindOption) ?? "auto",
    normalizeFormat: af.normalize_format ?? "",
    normalizeMap: af.normalize_map ?? {},
    normalizeSource: af.normalize_source ?? "",
    normalizeReference: af.normalize_reference ?? "",
    inheritHostWhenEmpty: af.strategy === "inherit_then_normalize",
    hostParam: af.host_param ?? "",
    composeTemplate: af.template ?? "",
    fixedValue: rem.new_value != null ? String(rem.new_value) : "",
  };
  return { legacy: false, state };
}

export function formToRule(state: RuleFormState): RuleDict {
  // ── Requirement + pattern family (matches_regex / not_matches_regex /
  // matches_regex_if_present fold back to the exact engine key on save). ──
  let requirement: RequirementKind = state.requirement;
  let pattern: string | null = null;
  if (state.requirement === "matches_regex") {
    pattern = state.pattern;
    if (state.patternNegate) {
      requirement = "not_matches_regex";
    } else if (state.patternSkipIfEmpty) {
      requirement = "matches_regex_if_present";
    } else {
      requirement = "matches_regex";
    }
  }

  // ── numeric_compare ──
  let threshold: number | null = null;
  let unit: string | null = null;
  let operator: ComparisonOperator | null = null;
  if (requirement === "numeric_compare") {
    operator = state.operator;
    // Keep an untouched field as null — NEVER synthesize 0. An omitted value
    // and an authored 0 mean different things ("I haven't filled this in" vs
    // "the limit really is zero"), and only the backend can judge the second:
    // `> 0` is the legitimate positive_number check, `>= 0` is a no-op rule.
    // Coercing to 0 here erased that distinction before validation ever saw
    // it, so an unfinished "width >= 900" saved as "width >= 0" and reported
    // full compliance. Same idiom as geometryFormToRule below.
    const rawThreshold = String(state.threshold).trim();
    threshold = rawThreshold === "" ? null : Number(rawThreshold);
    if (threshold !== null && Number.isNaN(threshold)) threshold = null;
    unit = state.unit || null;
  }

  // ── relation_compare (direct vs lookup-table source) ──
  let otherParam: string | null = null;
  let compareKind: CompareKind | null = null;
  let lookup: string | null = null;
  if (requirement === "relation_compare") {
    compareKind = state.compareKind;
    operator = state.operator;
    if (state.relationMode === "lookup") {
      lookup = state.lookup || null;
    } else {
      otherParam = state.otherParam || null;
    }
  }

  // ── universal scope filter ──
  const scopeFilter =
    state.scopeFilterParam.trim() && state.scopeFilterPattern.trim()
      ? { param: state.scopeFilterParam.trim(), pattern: state.scopeFilterPattern.trim() }
      : null;

  // ── canonical_format is forced when the normalize kind is "reference" or
  // the "inherit when empty" checkbox is on (v1.4-K21/K22: the fix IS the
  // check — there is no separate Action selector for canonical_format). ──
  const normalizeKind = state.normalizeKind;
  const inheritThenNormalize =
    state.handle === "normalize" &&
    state.inheritHostWhenEmpty &&
    normalizeKind !== "auto" &&
    normalizeKind !== "reference";
  if (state.handle === "normalize" && normalizeKind === "reference") {
    requirement = "canonical_format";
  }
  if (inheritThenNormalize) {
    requirement = "canonical_format";
  }
  // canonical_format's fix is inherent — the handle is always "normalize"
  // regardless of what the (hidden, per the Action section) selector holds.
  const effectiveHandle: HandleOption =
    requirement === "canonical_format" ? "normalize" : state.handle;

  let autofill: RuleAutofillDict;
  let remediation: RuleRemediationDict;
  let fixability: Fixability;

  if (effectiveHandle === "issue") {
    autofill = fullAutofill("none");
    remediation = fullRemediation("create_acc_issue", "instance");
    fixability = "manual";
  } else {
    fixability = "auto";
    const isRename = state.writeTarget === "family" || state.writeTarget === "rename_type";
    const target: WriteTarget = isRename
      ? "instance" // placeholder; overwritten below via fullRemediation `target`
      : (state.writeTarget as WriteTarget);
    const remediationTarget: WriteTarget = state.writeTarget === "rename_type" ? "type" : "family";

    if (isRename) {
      remediation = fullRemediation("rename_element", remediationTarget);
    } else {
      remediation = fullRemediation("set_parameter", target);
    }

    if (effectiveHandle === "normalize") {
      const overrides: Partial<RuleAutofillDict> = { normalize_kind: normalizeKind };
      if (normalizeKind === "map") {
        overrides.normalize_map = state.normalizeMap;
      } else if (normalizeKind === "reference") {
        overrides.normalize_reference = state.normalizeReference || "";
      } else if (normalizeKind === "template") {
        overrides.normalize_source = state.normalizeSource;
        overrides.normalize_format = state.normalizeFormat;
      } else if (normalizeKind !== "auto" && normalizeKind !== "family_name") {
        overrides.normalize_format =
          state.normalizeFormat || NORMALIZE_DEFAULT_FORMAT[normalizeKind] || "";
      }
      let strategy: AutofillStrategy = "normalize";
      if (inheritThenNormalize) {
        strategy = "inherit_then_normalize";
        if (state.hostParam.trim()) overrides.host_param = state.hostParam.trim();
      }
      autofill = fullAutofill(strategy, overrides);
    } else if (effectiveHandle === "compose_template") {
      autofill = fullAutofill("compose_template", { template: state.composeTemplate });
    } else if (effectiveHandle === "inherit_from_host") {
      const overrides: Partial<RuleAutofillDict> = {};
      if (state.hostParam.trim()) overrides.host_param = state.hostParam.trim();
      autofill = fullAutofill("inherit_from_host", overrides);
    } else {
      // set_fixed
      autofill = fullAutofill("none");
      const strategy: NewValueStrategy = "fixed";
      remediation = { ...remediation, new_value_strategy: strategy, new_value: state.fixedValue };
    }
  }

  const rule: RuleDict = {
    id: state.id.trim(),
    parameter: state.parameter.trim(),
    requirement,
    pattern,
    threshold,
    unit,
    when_param: null,
    when_pattern: null,
    other_param: otherParam,
    operator,
    compare_kind: compareKind,
    allowed_values: null,
    lookup,
    scope_filter: scopeFilter,
    category: state.category.trim() || null,
    severity_tag: state.severityTag.trim() || "rule_violation",
    severity_level: state.severityLevel,
    description: state.description,
    autofill,
    citation: { mode: "soft", source_filter: null, on_missing: "warn" },
    fixability,
    remediation,
    requires_human: false,
    rule_type: null,
    extraction_meta: null,
    bound_parameter: state.boundParameter.trim() || null,
  };
  return rule;
}

/* ── Geometry rule mapping (item 4 — toggle at top of the Builder form) ── */

export interface GeometryFormState {
  id: string;
  category: string;
  checkType: GeometryCheckType;
  description: string;
  thresholdMm: string;
  direction: ClearanceDirection;
  referenceCategory: string;
  referenceSource: GeometryReferenceSource;
  referenceLinkHint: string;
  spatialFilterCategory: string;
  spatialFilterNameContains: string;
}

export function emptyGeometryFormState(): GeometryFormState {
  return {
    id: "",
    category: "",
    checkType: "clearance_min",
    description: "",
    thresholdMm: "2400",
    direction: "below",
    referenceCategory: "",
    referenceSource: "same_model",
    referenceLinkHint: "",
    spatialFilterCategory: "",
    spatialFilterNameContains: "",
  };
}

const NEEDS_THRESHOLD: readonly GeometryCheckType[] = [
  "clearance_min",
  "clearance_max",
  "min_spacing",
];
const NEEDS_DIRECTION: readonly GeometryCheckType[] = ["clearance_min", "clearance_max"];
const NEEDS_REFERENCE: readonly GeometryCheckType[] = [
  "clearance_min",
  "clearance_max",
  "min_spacing",
];

export function geometryFormToRule(state: GeometryFormState): GeometryRuleDict {
  const needsThreshold = NEEDS_THRESHOLD.includes(state.checkType);
  const needsDirection = NEEDS_DIRECTION.includes(state.checkType);
  const needsReference = NEEDS_REFERENCE.includes(state.checkType);
  const hasSpatialFilter =
    !!state.spatialFilterCategory.trim() || !!state.spatialFilterNameContains.trim();

  return {
    id: state.id.trim(),
    category: state.category,
    check_type: state.checkType,
    description: state.description,
    threshold_mm: needsThreshold && state.thresholdMm !== "" ? Number(state.thresholdMm) : null,
    clearance_direction: needsDirection ? state.direction : null,
    reference_category: needsReference ? state.referenceCategory || null : null,
    reference_source: needsReference ? state.referenceSource : "same_model",
    reference_link_hint:
      needsReference && state.referenceSource !== "same_model" && state.referenceLinkHint.trim()
        ? state.referenceLinkHint.trim()
        : null,
    spatial_filter: hasSpatialFilter
      ? {
          category: state.spatialFilterCategory || null,
          name_contains: state.spatialFilterNameContains.trim() || null,
          name_exact: null,
        }
      : null,
    severity_tag: "geometric_violation",
    execution_status: "not_model_checkable",
    view_id: null,
    notes: null,
  };
}

export function geometryRuleToForm(rule: GeometryRuleDict): GeometryFormState {
  return {
    id: rule.id,
    category: rule.category,
    checkType: rule.check_type,
    description: rule.description,
    thresholdMm: rule.threshold_mm != null ? String(rule.threshold_mm) : "",
    direction: rule.clearance_direction ?? "below",
    referenceCategory: rule.reference_category ?? "",
    referenceSource: rule.reference_source ?? "same_model",
    referenceLinkHint: rule.reference_link_hint ?? "",
    spatialFilterCategory: rule.spatial_filter?.category ?? "",
    spatialFilterNameContains: rule.spatial_filter?.name_contains ?? "",
  };
}
