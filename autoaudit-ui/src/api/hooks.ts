import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  ApplyOnceResponse,
  ApprovalsResponse,
  AuditStatusResponse,
  CategoriesResponse,
  DoctorResponse,
  DraftRuleRequest,
  DraftRuleResponse,
  ExportReportResponse,
  ExtractPdfResponse,
  HealthResponse,
  HighlightResponse,
  IdsImportResponse,
  LookupsResponse,
  OutcomesResponse,
  ParamsResponse,
  PreviewNormalizeRequest,
  PreviewNormalizeResponse,
  ProfilesResponse,
  ReferencesResponse,
  RevitDocumentResponse,
  RuleFileDetailResponse,
  RuleFilesResponse,
  RuleSetDict,
  RunDetailResponse,
  RunListResponse,
  SaveEnvRequest,
  SaveEnvResponse,
  SaveLookupRequest,
  SaveReferenceRequest,
  SaveRulesetResponse,
  SettingsResponse,
  StartAuditRequest,
  StartAuditResponse,
  TestConnectionResponse,
  TrendResponse,
  ValidateRuleRequest,
  ValidationResult,
  VerificationViewsResponse,
} from "./types";

export const queryKeys = {
  health: ["health"] as const,
  runs: ["runs"] as const,
  run: (id: string) => ["run", id] as const,
  outcomes: (id: string) => ["outcomes", id] as const,
  approvals: ["approvals"] as const,
  trend: ["trend"] as const,
  profiles: ["profiles"] as const,
  audit: (id: string) => ["audit", id] as const,
  rulesFiles: ["rules-files"] as const,
  ruleset: (name: string) => ["ruleset", name] as const,
  categories: ["categories"] as const,
  params: (category: string) => ["params", category] as const,
  lookups: ["lookups"] as const,
  references: ["references"] as const,
  revitDocument: ["revit-document"] as const,
  settings: ["settings"] as const,
};

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: () => api.get<HealthResponse>("/health"),
    refetchInterval: 30_000,
  });
}

/** The live open Revit model — polled so the panel reflects the document
 *  you currently have open (the thing you'd audit next). */
export function useRevitDocument() {
  return useQuery({
    queryKey: queryKeys.revitDocument,
    queryFn: () => api.get<RevitDocumentResponse>("/revit/document"),
    refetchInterval: 15_000,
  });
}

export function useRuns() {
  return useQuery({
    queryKey: queryKeys.runs,
    queryFn: () => api.get<RunListResponse>("/runs"),
  });
}

export function useRun(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.run(id ?? ""),
    queryFn: () => api.get<RunDetailResponse>(`/runs/${encodeURIComponent(id!)}`),
    enabled: !!id,
  });
}

export function useOutcomes(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.outcomes(id ?? ""),
    queryFn: () =>
      api.get<OutcomesResponse>(`/runs/${encodeURIComponent(id!)}/outcomes`),
    enabled: !!id,
  });
}

export function useApprovals() {
  return useQuery({
    queryKey: queryKeys.approvals,
    queryFn: () => api.get<ApprovalsResponse>("/approvals"),
  });
}

export function useTrend() {
  return useQuery({
    queryKey: queryKeys.trend,
    queryFn: () => api.get<TrendResponse>("/trend"),
  });
}

export function useProfiles() {
  return useQuery({
    queryKey: queryKeys.profiles,
    queryFn: () => api.get<ProfilesResponse>("/profiles"),
  });
}

export function useAuditStatus(auditId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.audit(auditId ?? ""),
    queryFn: () =>
      api.get<AuditStatusResponse>(`/audits/${encodeURIComponent(auditId!)}`),
    enabled: !!auditId && enabled,
    // Poll until the job leaves "running". Terminal status ALWAYS stops the
    // poll — a job that fails before its run folder exists has run_id null
    // forever, and `|| !d.run_id` kept this polling (and the Run page stuck
    // on "Starting…") for eternity (2026-07 review, FE-6).
    refetchInterval: (query) => {
      const d = query.state.data;
      if (!d) return 1500;
      if (d.status !== "running") return false;
      return 1500;
    },
  });
}

export function useStartAudit() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: StartAuditRequest) =>
      api.post<StartAuditResponse>("/audits", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
    },
  });
}

export function useIgnoreApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: string) =>
      api.post<{ ok: boolean }>(`/approvals/${encodeURIComponent(file)}/ignore`),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.approvals }),
  });
}

export function useRestoreApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: string) =>
      api.post<{ ok: boolean }>(`/approvals/${encodeURIComponent(file)}/restore`),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.approvals }),
  });
}

export function useApplyApprovals() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<ApplyOnceResponse>("/approvals/apply-once"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.approvals });
      qc.invalidateQueries({ queryKey: queryKeys.runs });
    },
  });
}

export function useExportReport(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (format: "docx" | "pdf") =>
      api.post<ExportReportResponse>(
        `/runs/${encodeURIComponent(runId)}/export-report`,
        { format },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.run(runId) }),
  });
}

export function useCreateVerificationViews(runId: string) {
  return useMutation({
    mutationFn: (dryRun: boolean) =>
      api.post<VerificationViewsResponse>(
        `/runs/${encodeURIComponent(runId)}/verification-views`,
        { dry_run: dryRun },
      ),
  });
}

export function useHighlight() {
  return useMutation({
    /** perLevel omitted -> service default (per-level plan walk, the beat-5b
     *  behaviour). false -> one-shot select+zoom, which STAYS in the active
     *  view when it already shows the elements (measured live 2026-08-26 on
     *  a 3D view: no view switch). */
    mutationFn: ({
      elementIds,
      perLevel,
    }: {
      elementIds: Array<number | string>;
      perLevel?: boolean;
    }) =>
      api.post<HighlightResponse>("/revit/highlight", {
        element_ids: elementIds,
        ...(perLevel === undefined ? {} : { per_level: perLevel }),
      }),
  });
}

/* ── M2: Rules library / Rule Builder ──────────────────────────────────── */

export function useRulesFiles() {
  return useQuery({
    queryKey: queryKeys.rulesFiles,
    queryFn: () => api.get<RuleFilesResponse>("/rules"),
  });
}

export function useRuleset(name: string | undefined) {
  return useQuery({
    queryKey: queryKeys.ruleset(name ?? ""),
    queryFn: () =>
      api.get<RuleFileDetailResponse>(`/rules/${encodeURIComponent(name!)}`),
    enabled: !!name,
  });
}

/** The backend CRUD surface is pinned to `rules.<scenario>.yaml` (SVC-3) —
 *  build the filename HERE, in one place. Callers (SaveSection,
 *  ExtractPdfDialog) pass the bare scenario; passing it straight through as
 *  the URL name used to write a prefixless `config/<scenario>` file the
 *  library page could never list (2026-07 review, FE-11). */
export function rulesFileName(scenario: string): string {
  return `rules.${scenario.trim()}.yaml`;
}

export function useSaveRuleset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      scenario,
      ruleset,
      overwrite,
    }: {
      scenario: string;
      ruleset: RuleSetDict;
      overwrite: boolean;
    }) =>
      api.put<SaveRulesetResponse>(
        `/rules/${encodeURIComponent(rulesFileName(scenario))}`,
        {
          ruleset,
          overwrite,
        },
      ),
    onSuccess: (_res, vars) => {
      qc.invalidateQueries({ queryKey: queryKeys.rulesFiles });
      qc.invalidateQueries({ queryKey: queryKeys.ruleset(rulesFileName(vars.scenario)) });
    },
  });
}

export function useDeleteRuleset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      api.del<{ ok: boolean }>(`/rules/${encodeURIComponent(name)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.rulesFiles }),
  });
}

export function useCategories() {
  return useQuery({
    queryKey: queryKeys.categories,
    queryFn: () => api.get<CategoriesResponse>("/catalogs/categories"),
    staleTime: 5 * 60_000,
  });
}

export function useParams(category: string | undefined) {
  return useQuery({
    queryKey: queryKeys.params(category ?? ""),
    queryFn: () =>
      api.get<ParamsResponse>(
        `/catalogs/params?category=${encodeURIComponent(category!)}`,
      ),
    enabled: !!category,
    staleTime: 5 * 60_000,
  });
}

export function useLookups() {
  return useQuery({
    queryKey: queryKeys.lookups,
    queryFn: () => api.get<LookupsResponse>("/catalogs/lookups"),
  });
}

export function useSaveLookup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: SaveLookupRequest }) =>
      api.put<{ ok: boolean }>(
        `/catalogs/lookups/${encodeURIComponent(name)}`,
        body,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.lookups }),
  });
}

export function useReferences() {
  return useQuery({
    queryKey: queryKeys.references,
    queryFn: () => api.get<ReferencesResponse>("/catalogs/references"),
  });
}

export function useSaveReference() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: SaveReferenceRequest }) =>
      api.put<{ ok: boolean }>(
        `/catalogs/references/${encodeURIComponent(name)}`,
        body,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.references }),
  });
}

/** Not polled/auto-run — called explicitly by the Draft section's button. */
export function useDraftRule() {
  return useMutation({
    mutationFn: (body: DraftRuleRequest) =>
      api.post<DraftRuleResponse>("/builder/draft", body),
  });
}

/** Called on-change, debounced by the caller (400ms — B "Preview live"). */
export function usePreviewNormalize() {
  return useMutation({
    mutationFn: (body: PreviewNormalizeRequest) =>
      api.post<PreviewNormalizeResponse>("/builder/preview", body),
  });
}

/** Called on-change, debounced by the caller (500ms — sticky validation footer). */
export function useValidateRule() {
  return useMutation({
    mutationFn: (body: ValidateRuleRequest) =>
      api.post<ValidationResult>("/builder/validate", body),
  });
}

export function useIdsImport() {
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.post<IdsImportResponse>("/builder/ids-import", form);
    },
  });
}

/** Returns the raw IDS XML text (client.ts falls back to `.text()` for a
 *  non-JSON response) — caller wraps it in a Blob to trigger a download. */
export function useIdsExport() {
  return useMutation({
    mutationFn: (ruleset: RuleSetDict) =>
      api.post<string>("/builder/ids-export", { ruleset }),
  });
}

/* ── M2: Settings + PDF extraction ─────────────────────────────────────── */

export function useSettings() {
  return useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => api.get<SettingsResponse>("/settings"),
  });
}

export function useSaveEnv() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SaveEnvRequest) =>
      api.put<SaveEnvResponse>("/settings/env", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.settings }),
  });
}

export function useTestForma() {
  return useMutation({
    mutationFn: () => api.post<TestConnectionResponse>("/settings/test/forma"),
  });
}

export function useTestRevit() {
  return useMutation({
    mutationFn: () => api.post<TestConnectionResponse>("/settings/test/revit"),
  });
}

/** Triggered explicitly by a "Run diagnostics" button, not auto-fetched —
 *  modeled as a mutation even though the endpoint is a GET (same pattern as
 *  every other on-demand server round-trip in this file). */
export function useDoctor() {
  return useMutation({
    mutationFn: () => api.get<DoctorResponse>("/settings/doctor"),
  });
}

export function useExtractPdf() {
  return useMutation({
    mutationFn: ({ file, maxSections }: { file: File; maxSections?: number }) => {
      const form = new FormData();
      form.append("file", file);
      // Optional demo cap: only the first N sections (a big spec runs ALL of
      // them by default — minutes). Omitted entirely = full run (the golden).
      if (maxSections != null) form.append("max_sections", String(maxSections));
      return api.post<ExtractPdfResponse>("/extraction/pdf", form);
    },
  });
}
