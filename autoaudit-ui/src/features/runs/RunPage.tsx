import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { StatusPill, axisStatus } from "@/components/StatusPill";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { PageHeader } from "@/components/PageHeader";
import { MonoText } from "@/components/MonoText";
import { strings } from "@/strings";
import { basename } from "@/lib/path";
import { useHealth, useProfiles, useRulesFiles, useStartAudit, useAuditStatus } from "@/api/hooks";
import { ApiError } from "@/api/client";
import type { AxisName } from "@/api/types";
import { LiveRunView } from "./LiveRunView";

// Pre-flight only covers the two connections a check/run actually needs
// (LOD/Spatial axes stay hidden UI-wide — 2026-07-12 feedback).
const PREFLIGHT_AXES: { key: AxisName; label: string }[] = [
  { key: "revit", label: strings.health.revit },
  { key: "forma", label: strings.health.forma },
];

/** Full-page replacement for the old RunDrawer (2026-07-12 restructure):
 *  form → start → the SSE live view renders inline on this same page →
 *  once the job reaches "done" we navigate to the run's results page.
 *  A "failed" job stays here so the operator sees the error in place. */
export function RunPage() {
  const { data: health } = useHealth();
  const { data: profilesData } = useProfiles();
  const startAudit = useStartAudit();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const preselectProfilePath = searchParams.get("profile") ?? undefined;

  const [tab, setTab] = useState<"profile" | "quick">("profile");
  const [profilePath, setProfilePath] = useState<string | undefined>(preselectProfilePath);
  const [selectedRules, setSelectedRules] = useState<string[]>([]);
  const [mode, setMode] = useState<"run" | "run_revit" | "demo">("demo");
  const [dryRun, setDryRun] = useState(true);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [maxIssues, setMaxIssues] = useState("");
  const [maxElements, setMaxElements] = useState("");

  // Lifecycle: pendingAuditId (started, run_id not yet known) -> liveAuditId
  // + liveRunId (run_id known, LiveRunView renders inline) -> navigate away
  // once the job status leaves "running" with a successful finish.
  const [pendingAuditId, setPendingAuditId] = useState<string | null>(null);
  const [liveAuditId, setLiveAuditId] = useState<string | null>(null);
  const [liveRunId, setLiveRunId] = useState<string | null>(null);
  // FE-6 (2026-07 review): a job that fails BEFORE its run folder exists
  // (broken profile YAML, axes crash) never gets a run_id — surface the
  // error here instead of spinning on "Starting…" forever.
  const [earlyFailure, setEarlyFailure] = useState<string | null>(null);

  useEffect(() => {
    if (preselectProfilePath) {
      setProfilePath(preselectProfilePath);
      setTab("profile");
    }
  }, [preselectProfilePath]);

  // S-08: `?? []` minted a fresh array on every render while the query was
  // still loading, so the memo below re-ran each time. Memoise the fallback
  // and the identity is stable in both states.
  const profiles = useMemo(() => profilesData?.profiles ?? [], [profilesData]);
  // Quick Run lists the WHOLE rules library (/api/rules), not the union of
  // profile-referenced files it used until 2026-08-24. The union silently
  // hid any rule the Rule Builder had just saved — a fresh rule belongs to
  // no profile yet, so it could never appear here no matter how correctly
  // it saved (bit the cold-open rehearsal live). useRulesFiles shares the
  // query the Builder's Save invalidates, so a newly saved rule shows up
  // without a reload. Unparseable files (error != null) are excluded — a
  // run over a broken YAML fails later and worse.
  const { data: rulesFilesData } = useRulesFiles();
  const ruleOptions = useMemo(() => {
    const files = rulesFilesData?.files ?? [];
    return files
      .filter((f) => !f.error)
      .map((f) => f.path)
      .sort((a, b) => basename(a).localeCompare(basename(b)));
  }, [rulesFilesData]);

  // Poll for run_id once an audit has started but the run folder isn't
  // created yet (P3: run_id fills in once the folder exists).
  const { data: statusPoll } = useAuditStatus(pendingAuditId, !!pendingAuditId);
  useEffect(() => {
    if (!pendingAuditId || !statusPoll) return;
    if (statusPoll.run_id) {
      setLiveAuditId(pendingAuditId);
      setLiveRunId(statusPoll.run_id);
      setPendingAuditId(null);
      return;
    }
    if (statusPoll.status === "failed") {
      // Failed before the run folder existed — stop, show why, re-enable Start.
      setEarlyFailure(statusPoll.error || strings.run.failedEarlyBody);
      setPendingAuditId(null);
    }
  }, [pendingAuditId, statusPoll]);

  // Once live, keep polling the job status; the moment it reports "done"
  // navigate to the results page for that run.
  const { data: liveStatus } = useAuditStatus(liveAuditId, !!liveAuditId);
  useEffect(() => {
    if (liveAuditId && liveRunId && liveStatus?.status === "done") {
      navigate(`/results/${encodeURIComponent(liveRunId)}`);
    }
  }, [liveAuditId, liveRunId, liveStatus, navigate]);

  function numOrUndefined(v: string): number | undefined {
    if (v.trim() === "") return undefined;
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }

  function handleSubmit() {
    // Mirrors service AuditRequest: exactly one of profile_path | profile.
    // A profile file fully defines its own run options; overrides only
    // exist on the quick-run (inline profile) path.
    const body =
      tab === "profile"
        ? { profile_path: profilePath }
        : {
            profile: {
              name: "quick-run",
              rules: selectedRules,
              run: {
                mode,
                dry_run: dryRun,
                max_issues: numOrUndefined(maxIssues),
                max_elements: numOrUndefined(maxElements),
              },
            },
          };

    setEarlyFailure(null);
    startAudit.mutate(body, {
      onSuccess: (res) => {
        if (res.run_id) {
          setLiveAuditId(res.audit_id);
          setLiveRunId(res.run_id);
        } else {
          setPendingAuditId(res.audit_id);
        }
      },
    });
  }

  const isConflict =
    startAudit.isError &&
    startAudit.error instanceof ApiError &&
    startAudit.error.status === 409;
  const isValidationError =
    startAudit.isError &&
    startAudit.error instanceof ApiError &&
    startAudit.error.status === 422;

  const canSubmit = tab === "profile" ? !!profilePath : selectedRules.length > 0;
  const isRunning = !!pendingAuditId || !!liveAuditId;

  return (
    <div className="flex flex-col gap-6 p-4">
      <PageHeader title={strings.run.title} description={strings.run.description} />

      <div className="flex flex-wrap items-center gap-3">
        <span className="text-caption">{strings.run.preflight}</span>
        {PREFLIGHT_AXES.map(({ key, label }) => (
          <StatusPill
            key={key}
            name={label}
            status={axisStatus(health?.axes[key])}
          />
        ))}
      </div>

      {liveAuditId && liveRunId ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <MonoText className="text-section-title">{liveRunId}</MonoText>
            <span className="text-caption">{strings.liveRun.title}</span>
          </div>
          <LiveRunView auditId={liveAuditId} failed={liveStatus?.status === "failed"} />
          {liveStatus?.status === "failed" && liveStatus.error && (
            <div className="text-[13px] text-[var(--fail)]">{liveStatus.error}</div>
          )}
        </div>
      ) : (
        <div className="card flex max-w-2xl flex-col gap-3 p-4">
          <Tabs value={tab} onValueChange={(v) => setTab(v as "profile" | "quick")}>
            <TabsList>
              <TabsTrigger value="profile">{strings.run.tabProfile}</TabsTrigger>
              <TabsTrigger value="quick">{strings.run.tabQuickRun}</TabsTrigger>
            </TabsList>

            <TabsContent value="profile" className="flex flex-col gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-caption">{strings.run.profileSelect}</span>
                <Select value={profilePath} onValueChange={setProfilePath}>
                  <SelectTrigger>
                    <SelectValue placeholder={strings.run.profileSelect} />
                  </SelectTrigger>
                  <SelectContent>
                    {profiles.map((p) => (
                      <SelectItem key={p.path} value={p.path} disabled={!!p.error}>
                        {p.name} — {strings.run.profileRulesCount(p.rules.length)}
                        {p.error ? " (error)" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
            </TabsContent>

            <TabsContent value="quick" className="flex flex-col gap-3">
              <div className="flex flex-col gap-1">
                <span className="text-caption">{strings.run.rulesSelect}</span>
                <div className="card flex max-h-64 flex-col gap-1 overflow-y-auto p-2">
                  {ruleOptions.map((r) => (
                    <label key={r} className="flex items-center gap-2 text-[13px]" title={r}>
                      <Checkbox
                        checked={selectedRules.includes(r)}
                        onCheckedChange={(checked) =>
                          setSelectedRules((prev) =>
                            checked ? [...prev, r] : prev.filter((x) => x !== r),
                          )
                        }
                      />
                      <span className="font-mono-val truncate">{basename(r)}</span>
                    </label>
                  ))}
                  {ruleOptions.length === 0 && (
                    <span className="text-[var(--ink-muted)]">—</span>
                  )}
                </div>
              </div>
              <label className="flex flex-col gap-1">
                <span className="text-caption">{strings.run.modeSelect}</span>
                <Select
                  value={mode}
                  onValueChange={(v) => setMode(v as "run" | "run_revit" | "demo")}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="run">{strings.run.modeApply}</SelectItem>
                    <SelectItem value="run_revit">{strings.run.modeRunRevit}</SelectItem>
                    <SelectItem value="demo">{strings.run.modeDemo}</SelectItem>
                  </SelectContent>
                </Select>
              </label>

              <label className="flex items-center gap-2 text-[13px]">
                <Checkbox checked={dryRun} onCheckedChange={(v) => setDryRun(!!v)} />
                {strings.run.dryRun}
              </label>

              <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
                <CollapsibleTrigger asChild>
                  <button className="flex items-center gap-1 text-[13px] text-[var(--ink-muted)]">
                    <ChevronDown
                      size={14}
                      className={advancedOpen ? "rotate-180 transition-transform" : "transition-transform"}
                    />
                    {strings.run.advanced}
                  </button>
                </CollapsibleTrigger>
                <CollapsibleContent className="mt-2 grid grid-cols-2 gap-2">
                  <label className="flex flex-col gap-1">
                    <span className="text-caption">{strings.run.maxIssues}</span>
                    <Input value={maxIssues} onChange={(e) => setMaxIssues(e.target.value)} inputMode="numeric" />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-caption">{strings.run.maxElements}</span>
                    <Input value={maxElements} onChange={(e) => setMaxElements(e.target.value)} inputMode="numeric" />
                  </label>
                </CollapsibleContent>
              </Collapsible>
            </TabsContent>
          </Tabs>

          {isConflict && (
            <div className="flex flex-col gap-2">
              <ApiErrorBanner error={startAudit.error} />
              <Button variant="outline" onClick={() => navigate("/results")}>
                {strings.run.viewRunning}
              </Button>
            </div>
          )}
          {isValidationError && (
            <div>
              <div className="text-caption mb-1">{strings.run.validationTitle}</div>
              <ApiErrorBanner error={startAudit.error} />
            </div>
          )}
          {earlyFailure && (
            <div
              role="alert"
              className="card flex flex-col gap-1 border-[var(--fail)] px-3 py-2 text-[var(--fail)]"
            >
              <span className="font-medium">{strings.run.failedEarlyTitle}</span>
              <span className="text-[13px]">{earlyFailure}</span>
            </div>
          )}

          <Button
            className="mt-1 h-10 w-full text-[14px]"
            disabled={!canSubmit || startAudit.isPending || isRunning}
            onClick={handleSubmit}
          >
            {startAudit.isPending || isRunning ? strings.run.submitting : strings.run.submit}
          </Button>
        </div>
      )}
    </div>
  );
}
