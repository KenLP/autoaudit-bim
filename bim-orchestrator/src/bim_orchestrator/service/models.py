"""Pydantic request/response shapes for the AuditHub service (P3-2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

AuditJobStatus = Literal["running", "done", "failed"]


class HealthAxes(BaseModel):
    revit: bool
    forma: bool
    lod: bool
    spatial: bool


class HealthResponse(BaseModel):
    ok: bool = True
    version: str
    axes: HealthAxes


class AuditRequest(BaseModel):
    """Either a path to a profile YAML on disk, OR an inline profile body."""

    profile_path: str | None = None
    profile: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> AuditRequest:
        if bool(self.profile_path) == bool(self.profile):
            raise ValueError("provide exactly one of profile_path | profile")
        return self


class AuditCreated(BaseModel):
    audit_id: str
    # The run folder only exists once the run mode starts (the axes may take
    # minutes first) — null here; poll GET /audits/{id} or listen on the SSE
    # stream to learn it.
    run_id: str | None = None


class AuditStatus(BaseModel):
    audit_id: str
    status: AuditJobStatus
    phase: str
    run_id: str | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None


class AuditEvent(BaseModel):
    ts: str
    phase: str
    message: str


class ApplyOnceResponse(BaseModel):
    applied: int
    held: int


# ── M1 additions (Phase 3b) ─────────────────────────────────────────────────

ApprovalStatus = Literal["pending", "applied", "applied_issue_open", "ignored"]


class RunArtifacts(BaseModel):
    """Which per-run artifact files exist on disk (booleans only — the UI
    fetches the actual content via the dedicated endpoints/StaticFiles)."""

    report: bool
    verification_report: bool
    trace: bool
    axes: bool
    report_docx: bool
    report_pdf: bool
    delta: bool = False


class RunDetailResponse(BaseModel):
    metadata: dict[str, Any]
    artifacts: RunArtifacts


class TrendPoint(BaseModel):
    run_id: str
    started_at: str | None = None
    mode: str | None = None
    compliant: int = 0
    non_compliant: int = 0
    manual_review: int = 0
    missing_data: int = 0
    compliance_pct: float = 0.0


class DiffLatest(BaseModel):
    resolved: int
    new: int
    persistent: int


class TrendResponse(BaseModel):
    points: list[TrendPoint]
    diff_latest: DiffLatest | None = None


class ApprovalFix(BaseModel):
    """Mirrors ``agents.design._build_record_fixes`` verbatim — no field is
    invented here that the real ApprovalWatcher record doesn't carry (the
    record has no ``target`` key, unlike the SPEC_PHASE3B_AUTOAUDIT_UI.md
    table; see the implementer's handoff notes)."""

    element_id: str | None = None
    parameter: str | None = None
    old_value: Any = None
    new_value: Any = None
    inherited_from: str | None = None
    action: str | None = None
    # L2-05 (2026-07-26 Phase 2 review): the approval record has carried
    # `value_source` since GĐ3 ("provenance for the Approvals UI badge") and
    # this model dropped it — so the API behind the React Approvals UI, the
    # same router as `POST /approvals/apply-once`, could not tell a reviewer
    # whether a value was computed deterministically or proposed by a model.
    # Approve-gating exists PRECISELY because a model produced it; withholding
    # that fact from the approver removes the reason the gate is there.
    # `evidence` rides along for the same reason: for a classification value it
    # is the cited clause, i.e. the only thing that makes the value checkable.
    value_source: str | None = None
    evidence: Any = None


class ApprovalRecord(BaseModel):
    file: str
    display_id: int | str | None = None
    issue_id: str | None = None
    project_id: str | None = None
    rule_ids: list[str] = Field(default_factory=list)
    status: ApprovalStatus
    created_at: str | None = None
    fixes: list[ApprovalFix] = Field(default_factory=list)


class ApprovalCounts(BaseModel):
    pending: int
    applied: int
    ignored: int


class ApprovalsResponse(BaseModel):
    proposals: list[ApprovalRecord]
    counts: ApprovalCounts


class OkResponse(BaseModel):
    ok: bool = True


class ExportReportRequest(BaseModel):
    format: Literal["docx", "pdf"]


class ExportReportResponse(BaseModel):
    ok: bool
    artifact: str


class VerificationViewsRequest(BaseModel):
    dry_run: bool = False


class ScheduleOutcome(BaseModel):
    rule_id: str
    status: str
    category: str | None = None
    schedule_id: int | None = None
    schedule_name: str | None = None
    detail: str | None = None


class VerificationViewsResponse(BaseModel):
    ok: bool
    created: list[ScheduleOutcome]
    skipped: list[ScheduleOutcome]


class RGB(BaseModel):
    """A Revit graphic-override colour (0-255 per channel)."""

    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)


class HighlightRequest(BaseModel):
    element_ids: list[int] = Field(min_length=1, max_length=500)
    # 2026-08-17: walk one view per level instead of letting Revit's
    # ShowElements pick a single view for a multi-level set. Defaults ON
    # because that IS the fix; `false` restores the pre-change one-shot path.
    per_level: bool = True
    # OPT-IN and off by default: a per-element graphic override WRITES to the
    # model (the override lives in the view) and is presentation, not evidence
    # — see `bim_orchestrator/highlight.py`. Navigation alone writes nothing.
    color: RGB | None = None
    # Clear the overrides for these elements instead of showing them. Activates
    # no view and changes no selection.
    reset: bool = False


class HighlightViewOutcome(BaseModel):
    """One level's outcome (or the whole set's, when the walk degraded)."""

    element_ids: list[int]
    status: str  # shown | cleared | no_view | no_level | degraded | error
    level_id: int | None = None
    level_name: str | None = None
    view_id: int | None = None
    view_name: str | None = None
    colored: bool = False
    detail: str | None = None


class HighlightResponse(BaseModel):
    ok: bool
    selected: int
    # Empty for callers/tests written before the per-level walk existed.
    views: list[HighlightViewOutcome] = Field(default_factory=list)


class RevitDocumentResponse(BaseModel):
    """The live open Revit document (GET /api/revit/document). All fields
    optional — a Home screen / addin-down state returns connected=False."""

    connected: bool = False
    title: str | None = None
    path: str | None = None
    active_view: str | None = None


class ProfileAxes(BaseModel):
    lod: bool = False
    spatial: bool = False


class ProfileEntry(BaseModel):
    path: str
    name: str | None = None
    rules: list[str] = Field(default_factory=list)
    axes: ProfileAxes = Field(default_factory=ProfileAxes)
    mode: str | None = None
    error: str | None = None


class ProfilesResponse(BaseModel):
    profiles: list[ProfileEntry]


# ── M2 additions (Phase 3b, SPEC_3B_M2_RULE_BUILDER_NOW.md) ────────────────


class RuleFileEntry(BaseModel):
    """One config/rules.<scenario>.yaml, summarised for the rules library page."""

    name: str
    path: str
    scenario: str | None = None
    rule_count: int = 0
    categories: list[str] = Field(default_factory=list)
    mtime: str | None = None  # ISO date string (see routes_rules._summarize_rule_file)
    error: str | None = None


class RulesListResponse(BaseModel):
    files: list[RuleFileEntry]


class RuleDetailResponse(BaseModel):
    ruleset: dict[str, Any]
    legacy_rule_ids: list[str] = Field(default_factory=list)


class PutRuleSetRequest(BaseModel):
    ruleset: dict[str, Any]
    overwrite: bool = False


class ValidationErrorItem(BaseModel):
    field: str
    message: str


class PutRuleSetResponse(BaseModel):
    ok: bool = True
    path: str


class CategoryEntry(BaseModel):
    key: str
    label: str
    note: str | None = None


class CategoriesResponse(BaseModel):
    categories: list[CategoryEntry]


class ParamEntry(BaseModel):
    name: str
    storage: str
    binding: str
    writable: bool
    dimension: str | None = None


class ParamsResponse(BaseModel):
    params: list[ParamEntry]
    aliases: dict[str, str] = Field(default_factory=dict)


class LookupKeyOut(BaseModel):
    param: str
    dimension: str = "string"


class LookupRowOut(BaseModel):
    when: list[str]
    require: str


class LookupEntry(BaseModel):
    """Mirrors ``policies.lookup_table.LookupTable`` verbatim (keys/rows —
    NOT the entries/exempt_values/case_sensitive shape the SPEC_PHASE3B_
    AUTOAUDIT_UI.md M2-endpoints table names, which doesn't match the real
    lookup-table schema; that shape belongs to ReferenceEntryOut below. See
    the implementer's M2-A handoff notes for the deviation."""

    name: str
    description: str | None = None
    keys: list[LookupKeyOut] = Field(default_factory=list)
    rows: list[LookupRowOut] = Field(default_factory=list)


class LookupsResponse(BaseModel):
    lookups: list[LookupEntry]


class PutLookupRequest(BaseModel):
    keys: list[LookupKeyOut] = Field(default_factory=list)
    rows: list[LookupRowOut] = Field(default_factory=list)
    description: str | None = None
    overwrite: bool = False


class ReferenceEntryOut(BaseModel):
    name: str
    entries: list[dict[str, Any]] = Field(default_factory=list)
    case_sensitive: bool = False


class ReferencesResponse(BaseModel):
    references: list[ReferenceEntryOut]


class PutReferenceRequest(BaseModel):
    entries: list[dict[str, Any]] = Field(default_factory=list)
    case_sensitive: bool = False
    overwrite: bool = False


class BuilderDraftRequest(BaseModel):
    text: str


class BuilderDraftResponse(BaseModel):
    rule: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class BuilderValidateRequest(BaseModel):
    rule: dict[str, Any]
    is_geometry: bool = False
    # A-03: the enclosing ruleset's target_category. A rule that leaves
    # `category` unset INHERITS these, and several checks — notably the
    # read-only write-target guard — resolve nothing without them. Omitting
    # the field made this endpoint structurally UNABLE to reach the same
    # verdict as PUT /rules/{name}, which has always passed it: the client
    # could not supply the context even if it wanted to, so the UI could say
    # OK and the save gate then answer 422. Optional, so every existing
    # caller keeps working exactly as before — the answer is simply less
    # complete without it.
    ruleset_categories: str | list[str] | None = None


class BuilderValidateResponse(BaseModel):
    ok: bool
    errors: list[ValidationErrorItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BuilderPreviewRequest(BaseModel):
    normalize_kind: str | None = None
    normalize_format: str | None = None
    normalize_map: dict[str, str] | None = None
    normalize_source: str | None = None
    reference: str | None = None
    sample: str | None = None
    pattern: str | None = None


class BuilderPreviewResponse(BaseModel):
    output: str | None = None
    matches: bool | None = None
    error: str | None = None


class IdsImportResponse(BaseModel):
    ruleset: dict[str, Any]
    rule_count: int


class IdsExportRequest(BaseModel):
    ruleset: dict[str, Any]


# ── M2-C: settings + PDF extraction ─────────────────────────────────────────


class ExtractionResponse(BaseModel):
    ruleset: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class EnvItem(BaseModel):
    key: str
    set: bool
    masked: str | None = None


class ServicesStatus(BaseModel):
    lod: bool
    spatial: bool
    revitcontrol: bool


class LlmStatus(BaseModel):
    provider: str | None = None


class SettingsResponse(BaseModel):
    env: list[EnvItem]
    services: ServicesStatus
    llm: LlmStatus


class PutEnvRequest(BaseModel):
    key: str
    value: str


class PutEnvResponse(BaseModel):
    ok: bool = True


class TestConnectionResponse(BaseModel):
    ok: bool
    message: str


class TestRevitResponse(BaseModel):
    ok: bool
    message: str
    version: str | None = None


class DoctorCheckItem(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str = ""


class DoctorResponse(BaseModel):
    checks: list[DoctorCheckItem]
