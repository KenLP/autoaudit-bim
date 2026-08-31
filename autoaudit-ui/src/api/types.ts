/**
 * Hand-written TS mirrors of the service's pydantic response shapes (B15 —
 * no OpenAPI codegen). Keep in sync with the M1 endpoint table in
 * docs/specs/SPEC_PHASE3B_AUTOAUDIT_UI.md. UI never re-derives these values
 * (B12) — it only counts/filters client-side.
 */

export type AxisName = "revit" | "forma" | "lod" | "spatial";

export type HealthStatus = "up" | "unconfigured" | "error";

export interface HealthResponse {
  ok: boolean;
  version: string;
  /** Booleans per the service's HealthAxes model — NOT HealthStatus
   *  strings (that vocabulary is UI-only; convert via axisStatus()). */
  axes: Record<AxisName, boolean>;
}

export interface RunMetadata {
  run_id: string;
  mode: string;
  started_at: string;
  finished_at: string | null;
  /** Free-form: run_recorder writes e.g. "converged", "max_iterations",
   *  plus sentinels "metadata_missing"/"metadata_corrupt" — display-only. */
  status: string;
  outcomes_summary: {
    compliant: number;
    non_compliant: number;
    manual_review: number;
    missing_data: number;
  } | null;
  /** MERGED finding count: QC findings + geometry findings (the orchestrator
   *  extends `findings` with the geometry bucket before persisting, so this
   *  is `len(findings)` — while `outcomes_summary` stays QC-only by design,
   *  K7). For runs without geometry rules the two are equal. Tiles must read
   *  THIS, or a geometry run shows "0 non-compliant" above a table of 25. */
  non_compliant_count?: number | null;
  duration_seconds: number | null;
  iterations: number | null;
  project?: string | null;
  model?: string | null;
  profile?: string | null;
  [extra: string]: unknown;
}

export interface RunArtifacts {
  report: boolean;
  verification_report: boolean;
  trace: boolean;
  axes: boolean;
  report_docx: boolean;
  report_pdf: boolean;
}

export interface RunDetailResponse {
  metadata: RunMetadata;
  artifacts: RunArtifacts;
}

/** GET /api/runs returns a BARE array (P3-2 contract), not an envelope. */
export type RunListResponse = RunMetadata[];

export type Bucket =
  | "compliant"
  | "non_compliant"
  | "manual_review"
  | "missing_data";

export type Severity = "high" | "medium" | "low";

export interface Finding {
  rule_id: string;
  element_id: number | string;
  parameter?: string | null;
  value?: string | number | null;
  suggested_value?: string | number | null;
  status: string;
  bucket: Bucket;
  severity?: Severity | string | null;
  message?: string | null;
  inherited_from?: string | null;
  diagnosis?: string | null;
  evidence?: string | null;
  acc_issue_url?: string | null;
  [extra: string]: unknown;
}

export interface OutcomesResponse {
  outcomes_summary: {
    compliant: number;
    non_compliant: number;
    manual_review: number;
    missing_data: number;
  };
  non_compliant: Finding[];
  manual_review_items: Finding[];
  missing_data_items: Finding[];
  proposed_fixes: Finding[];
  [extra: string]: unknown;
}

export interface TrendPoint {
  run_id: string;
  started_at: string;
  mode: string;
  compliant: number;
  non_compliant: number;
  manual_review: number;
  missing_data: number;
  compliance_pct: number;
}

export interface TrendResponse {
  points: TrendPoint[];
  diff_latest: { resolved: number; new: number; persistent: number } | null;
}

export interface ApprovalFix {
  element_id: number | string;
  parameter: string;
  old_value: string | number | null;
  new_value: string | number | null;
  inherited_from?: string | null;
  action?: string | null;
  target?: string | null;
}

export type ApprovalStatus =
  | "pending"
  | "applied"
  | "applied_issue_open"
  | "ignored";

export interface ApprovalRecord {
  file: string;
  display_id: string;
  issue_id?: string | null;
  project_id?: string | null;
  rule_ids: string[];
  status: ApprovalStatus;
  created_at: string;
  fixes: ApprovalFix[];
}

export interface ApprovalsResponse {
  proposals: ApprovalRecord[];
  counts: { pending: number; applied: number; ignored: number };
}

export interface ApplyOnceResponse {
  applied: number;
  held: number;
}

export interface ProfileSummary {
  path: string;
  name: string;
  rules: string[];
  axes: { lod: boolean; spatial: boolean };
  mode: string;
  error: string | null;
}

export interface ProfilesResponse {
  profiles: ProfileSummary[];
}

/** Mirrors service AuditRunOptions (policies/audit_profile.py). */
export interface AuditRunOptions {
  mode: "check" | "run" | "run_revit" | "demo";
  max_elements?: number;
  max_issues?: number;
  dry_run?: boolean;
}

/** Inline AuditProfile body for quick-run (subset the UI authors). */
export interface InlineAuditProfile {
  name: string;
  rules: string[];
  run: AuditRunOptions;
}

/** Mirrors service AuditRequest: exactly one of profile_path | profile. */
export interface StartAuditRequest {
  profile_path?: string;
  profile?: InlineAuditProfile;
}

export interface StartAuditResponse {
  audit_id: string;
  run_id: string | null;
}

export interface AuditStatusResponse {
  audit_id: string;
  run_id: string | null;
  /** Service AuditJob statuses — "done", not "finished" (verified live). */
  status: "running" | "done" | "failed";
  error?: string | null;
  phase?: string;
  summary?: Record<string, number> | null;
}

export type AuditPhase =
  | "service"
  | "axes"
  | "query"
  | "qc"
  | "design"
  | "record"
  | "run";

export interface AuditEvent {
  ts: string;
  phase: AuditPhase;
  message: string;
}

export interface ExportReportResponse {
  ok: boolean;
  artifact: string;
}

export interface VerificationViewsResponse {
  ok: boolean;
  created: string[];
  skipped: string[];
}

/** One level's outcome of the highlight walk (2026-08-17). */
export interface HighlightViewOutcome {
  element_ids: number[];
  /** shown | cleared | no_view | no_level | degraded | error */
  status: string;
  level_id?: number | null;
  level_name?: string | null;
  view_id?: number | null;
  view_name?: string | null;
  colored: boolean;
  detail?: string | null;
}

export interface HighlightResponse {
  ok: boolean;
  selected: number;
  /** One entry per level walked. Empty when the addin answered nothing. */
  views: HighlightViewOutcome[];
}

/** GET /api/revit/document — the live open Revit model (or connected:false). */
export interface RevitDocumentResponse {
  connected: boolean;
  title?: string | null;
  path?: string | null;
  active_view?: string | null;
}

/* ────────────────────────────────────────────────────────────────────────
 * M2 — Rule Builder / Rules library
 *
 * The RuleDict / GeometryRuleDict shapes below are hand-written mirrors of
 * the REAL pydantic schema in
 * `bim-orchestrator/src/bim_orchestrator/policies/rules_schema.py` (read
 * directly, 2026-07-12 — field names/defaults below match that file, not
 * the endpoint-table prose in SPEC_PHASE3B_AUTOAUDIT_UI.md, which has a
 * copy/paste bug: the "GET /api/catalogs/lookups" row describes the
 * REFERENCE shape (`entries`/`case_sensitive`) instead of the real
 * LookupTable shape (`keys`/`rows`) — see `lookup_table.py`. This file
 * follows the real Python models; the deviation is called out in the
 * implementer report.
 * ──────────────────────────────────────────────────────────────────────── */

export type RequirementKind =
  | "present_and_nonempty"
  | "positive_number"
  | "matches_regex"
  | "matches_regex_if_present"
  | "not_matches_regex"
  | "numeric_min"
  | "numeric_min_conditional"
  | "unique_in_set"
  | "fire_rating_ge"
  | "numeric_compare"
  | "relation_compare"
  | "value_in_subset"
  | "canonical_format";

/** The 6 requirements the Rule Builder OFFERS (v1.4-K22). Everything else
 *  still loads/evaluates server-side but is presented read-only ("legacy"). */
export const OFFERED_REQUIREMENTS = [
  "present_and_nonempty",
  "canonical_format",
  "numeric_compare",
  "matches_regex",
  "unique_in_set",
  "relation_compare",
] as const;
export type OfferedRequirement = (typeof OFFERED_REQUIREMENTS)[number];

/** The pattern family: 3 engine requirements folded into ONE "Match
 *  pattern" dropdown entry + 2 checkboxes (negate / skip-if-empty). */
export const PATTERN_FAMILY_REQUIREMENTS = [
  "matches_regex",
  "not_matches_regex",
  "matches_regex_if_present",
] as const;

export type ComparisonOperator = ">" | ">=" | "<" | "<=" | "==" | "!=";
export type CompareKind = "numeric" | "fire_rating" | "string";
export type SeverityLevel = "severity_low" | "severity_medium" | "severity_high";
export type AutofillStrategy =
  | "infer_from_adjacent"
  | "infer_from_room_name"
  | "compose_template"
  | "normalize"
  | "inherit_from_host"
  | "inherit_then_normalize"
  | "none";
export type CitationMode = "hard" | "soft";
export type OnMissingCitation = "warn" | "downgrade";
export type Fixability = "manual" | "auto";
export type RemediationAction = "create_acc_issue" | "set_parameter" | "rename_element";
export type NewValueStrategy = "fixed" | "inferred" | "next_available" | "llm_propose";
export type WriteTarget = "instance" | "type" | "family" | "auto";

export interface RuleScopeFilterDict {
  param: string;
  pattern: string;
}

export interface RuleAutofillDict {
  strategy: AutofillStrategy;
  fallback?: unknown;
  template?: string | null;
  sequence_scope?: string[] | null;
  normalize_kind?: string | null;
  normalize_format?: string | null;
  normalize_map?: Record<string, string> | null;
  normalize_source?: string | null;
  normalize_reference?: string | null;
  host_param?: string | null;
}

export interface RuleRemediationDict {
  action: RemediationAction;
  target_parameter?: string | null;
  new_value?: unknown;
  new_value_strategy: NewValueStrategy;
  comments_template?: string | null;
  target: WriteTarget;
  llm_safety_critical: boolean;
}

export interface CitationPolicyDict {
  mode: CitationMode;
  source_filter?: string[] | null;
  on_missing: OnMissingCitation;
}

/** Mirrors `rules_schema.Rule`. */
export interface RuleDict {
  id: string;
  parameter: string;
  requirement: RequirementKind;
  pattern?: string | null;
  threshold?: number | null;
  unit?: string | null;
  when_param?: string | null;
  when_pattern?: string | null;
  other_param?: string | null;
  operator?: ComparisonOperator | null;
  compare_kind?: CompareKind | null;
  allowed_values?: string[] | null;
  lookup?: string | null;
  scope_filter?: RuleScopeFilterDict | null;
  category?: string | null;
  severity_tag: string;
  severity_level?: SeverityLevel | null;
  description: string;
  autofill: RuleAutofillDict;
  citation: CitationPolicyDict;
  fixability: Fixability;
  remediation: RuleRemediationDict;
  requires_human: boolean;
  rule_type?: string | null;
  extraction_meta?: Record<string, unknown> | null;
  bound_parameter?: string | null;
}

export type GeometryCheckType =
  | "clearance_min"
  | "clearance_max"
  | "spatial_containment"
  | "min_spacing";
export type ClearanceDirection = "below" | "above" | "horizontal";
export type GeometryReferenceSource =
  | "same_model"
  | "linked_arch"
  | "linked_struct"
  | "linked_mep";

export interface GeometryRuleSpatialFilterDict {
  category?: string | null;
  name_contains?: string | null;
  name_exact?: string | null;
}

/** Mirrors `rules_schema.GeometryRule`. */
export interface GeometryRuleDict {
  id: string;
  category: string;
  check_type: GeometryCheckType;
  description: string;
  threshold_mm?: number | null;
  clearance_direction?: ClearanceDirection | null;
  reference_category?: string | null;
  reference_source: GeometryReferenceSource;
  reference_link_hint?: string | null;
  spatial_filter?: GeometryRuleSpatialFilterDict | null;
  severity_tag: string;
  execution_status: string;
  view_id?: number | null;
  notes?: string[] | null;
}

/** Mirrors `rules_schema.RuleSet`. */
export interface RuleSetDict {
  scenario: string;
  target_category: string | string[];
  rules: RuleDict[];
  metadata?: Record<string, unknown> | null;
  geometry_rules: GeometryRuleDict[];
}

export interface RuleFileSummary {
  name: string;
  path: string;
  scenario: string;
  rule_count: number;
  categories: string[];
  mtime: string;
  error: string | null;
}

export interface RuleFilesResponse {
  files: RuleFileSummary[];
}

export interface RuleFileDetailResponse {
  ruleset: RuleSetDict;
  legacy_rule_ids: string[];
}

export interface SaveRulesetRequest {
  ruleset: RuleSetDict;
  overwrite: boolean;
}

export interface SaveRulesetResponse {
  ok: boolean;
  path?: string;
}

export interface CategoryEntry {
  key: string;
  label: string;
  note: string | null;
}

export interface CategoriesResponse {
  categories: CategoryEntry[];
}

/** Mirrors `param_catalog.ParamSpec` (subset the UI needs). */
export interface ParamEntry {
  name: string;
  storage: string;
  binding: "instance" | "type";
  writable: boolean;
  dimension: string;
  rename_only?: boolean;
}

export interface ParamsResponse {
  params: ParamEntry[];
  aliases: Record<string, string>;
}

/** Mirrors `lookup_table.LookupKey`. */
export interface LookupKeyEntry {
  param: string;
  dimension: "fire_rating" | "string";
}

/** Mirrors `lookup_table.LookupRow`. */
export interface LookupRowEntry {
  when: string[];
  require: string;
}

/** Mirrors `lookup_table.LookupTable` — NOT the (wrong) shape printed in the
 *  M2 endpoint table, see file-header note above. */
export interface LookupSet {
  name: string;
  description?: string | null;
  keys: LookupKeyEntry[];
  rows: LookupRowEntry[];
}

export interface LookupsResponse {
  lookups: LookupSet[];
}

export interface SaveLookupRequest {
  keys: LookupKeyEntry[];
  rows: LookupRowEntry[];
  description?: string | null;
  overwrite: boolean;
}

/** Mirrors `reference.ReferenceEntry`. */
export interface ReferenceEntryDict {
  canonical: string;
  aliases: string[];
}

/** Mirrors `reference.ReferenceSet`. */
export interface ReferenceSetDict {
  name: string;
  description?: string | null;
  case_sensitive: boolean;
  entries: ReferenceEntryDict[];
}

export interface ReferencesResponse {
  references: ReferenceSetDict[];
}

export interface SaveReferenceRequest {
  entries: ReferenceEntryDict[];
  case_sensitive?: boolean;
  description?: string | null;
  overwrite: boolean;
}

export interface DraftRuleRequest {
  text: string;
}

export interface DraftRuleResponse {
  rule: RuleDict;
  warnings: string[];
}

export interface ValidationError {
  field: string;
  message: string;
}

export interface ValidationResult {
  ok: boolean;
  errors: ValidationError[];
  warnings: string[];
}

export interface ValidateRuleRequest {
  rule: RuleDict | GeometryRuleDict;
  is_geometry: boolean;
}

export interface PreviewNormalizeRequest {
  normalize_kind?: string | null;
  normalize_format?: string | null;
  normalize_map?: Record<string, string> | null;
  normalize_source?: string | null;
  reference?: string | null;
  sample: string;
  pattern?: string | null;
}

export interface PreviewNormalizeResponse {
  output: string | null;
  matches: boolean | null;
  error: string | null;
}

export interface IdsImportResponse {
  ruleset: RuleSetDict;
  rule_count: number;
}

export interface IdsExportRequest {
  ruleset: RuleSetDict;
}

/* ────────────────────────────────────────────────────────────────────────
 * M2 — Settings (env, diagnostics, connection smoke tests) + PDF extraction
 * ──────────────────────────────────────────────────────────────────────── */

/** One allowlisted env key. `masked` is `null` when `set` is false — the
 *  server never returns the full secret (only the last 4 chars, per B). */
export interface EnvEntry {
  key: string;
  set: boolean;
  masked: string | null;
}

export interface ServicesStatus {
  lod: boolean;
  spatial: boolean;
  revitcontrol: boolean;
}

export interface SettingsResponse {
  env: EnvEntry[];
  services: ServicesStatus;
  llm: { provider: string | null };
}

export interface SaveEnvRequest {
  key: string;
  value: string;
}

export interface SaveEnvResponse {
  ok: boolean;
}

export interface TestConnectionResponse {
  ok: boolean;
  message: string;
  version?: string | null;
}

export type DoctorStatus = "pass" | "warn" | "fail";

export interface DoctorCheck {
  name: string;
  status: DoctorStatus;
  detail: string;
}

export interface DoctorResponse {
  checks: DoctorCheck[];
}

/** POST /api/extraction/pdf — the `ruleset` is a partial RuleSetDict (the
 *  extractor doesn't know the target scenario name yet; the dialog fills it
 *  in before calling the existing `useSaveRuleset`, same as the builder). */
export interface ExtractPdfResponse {
  ruleset: RuleSetDict;
  warnings: string[];
}
