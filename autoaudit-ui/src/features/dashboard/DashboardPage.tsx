import { useNavigate } from "react-router-dom";
import { AlertTriangle, Rocket } from "lucide-react";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { StatTile } from "@/components/StatTile";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { MonoText } from "@/components/MonoText";
import { PageHeader } from "@/components/PageHeader";
import { TrendPanel } from "@/features/runs/TrendPanel";
import { strings } from "@/strings";
import { useApprovals, useRevitDocument, useRuns, useTrend } from "@/api/hooks";
import { formatDateTime, formatDuration } from "@/lib/format";

const AU_DEMO_PROFILE_PATH = "config/audit.au_demo.yaml";

export function DashboardPage() {
  const { data: runsData, isLoading, isError, error } = useRuns();
  const { data: approvals } = useApprovals();
  const { data: trend } = useTrend();
  const { data: revitDoc } = useRevitDocument();
  const navigate = useNavigate();

  const latest = runsData?.[0];
  const pending = approvals?.counts.pending ?? 0;

  // The live open Revit model — the thing you'd audit next. Shown even
  // before any run, so the plugin tells you what's loaded.
  const liveModel = revitDoc?.connected ? revitDoc.title : null;
  const liveModelCard = (
    <div className="card flex flex-wrap items-center gap-2 p-3">
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: liveModel ? "var(--ok)" : "var(--ink-muted)" }}
      />
      <span className="text-caption">{strings.dashboard.liveModel}:</span>
      {liveModel ? (
        <MonoText className="text-[13px]">{liveModel}</MonoText>
      ) : (
        <span className="text-caption">{strings.dashboard.liveModelNone}</span>
      )}
      {liveModel && (
        <Button
          size="sm"
          variant="outline"
          className="ml-auto"
          onClick={() => navigate("/run")}
        >
          {strings.dashboard.runOnThis}
        </Button>
      )}
    </div>
  );

  // FE-7 (2026-07 review): a dead service must read as an ERROR, not as
  // "Run your first audit" — the empty state below is only for real emptiness.
  if (isError) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.dashboard.title} description={strings.dashboard.description} />
        <ApiErrorBanner error={error} />
      </div>
    );
  }

  if (!isLoading && !latest) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.dashboard.title} description={strings.dashboard.description} />
        {liveModelCard}
        <EmptyState
          icon={Rocket}
          title={strings.dashboard.emptyTitle}
          body={strings.dashboard.emptyBody}
          actionLabel={strings.dashboard.emptyAction}
          onAction={() =>
            navigate(`/run?profile=${encodeURIComponent(AU_DEMO_PROFILE_PATH)}`)
          }
        />
      </div>
    );
  }

  const summary = latest?.outcomes_summary;

  return (
    <div className="flex flex-col gap-6 p-4">
      <PageHeader title={strings.dashboard.title} description={strings.dashboard.description} />

      {liveModelCard}

      {latest && (
        <div className="card flex flex-col gap-3 p-4">
          <div className="text-caption">
            {strings.dashboard.project}: {latest.project ?? "—"} · {strings.dashboard.model}:{" "}
            {latest.model ?? liveModel ?? "—"} · {strings.dashboard.mode}: {latest.mode}
            {latest.profile ? ` · ${strings.dashboard.profile}: ${latest.profile}` : ""}
          </div>
          <div className="flex flex-wrap gap-2">
            <StatTile label={strings.bucket.compliant} value={summary?.compliant ?? "—"} color="var(--bucket-compliant)" />
            {/* Merged count (QC + geometry) — same rationale as RunDetailPage. */}
            <StatTile label={strings.bucket.non_compliant} value={latest?.non_compliant_count ?? summary?.non_compliant ?? "—"} color="var(--bucket-non-compliant)" />
            <StatTile label={strings.bucket.manual_review} value={summary?.manual_review ?? "—"} color="var(--bucket-manual-review)" />
            <StatTile label={strings.bucket.missing_data} value={summary?.missing_data ?? "—"} color="var(--bucket-missing-data)" />
          </div>
          <div className="text-caption">
            {strings.dashboard.lastRun}: <MonoText>{latest.run_id}</MonoText> · {latest.mode} ·{" "}
            {formatDateTime(latest.started_at)} · {formatDuration(latest.duration_seconds)}
          </div>
          <div className="flex gap-2">
            <Button onClick={() => navigate(`/results/${encodeURIComponent(latest.run_id)}`)}>
              {strings.dashboard.openResults}
            </Button>
            <Button
              variant="outline"
              onClick={() => navigate(`/results/${encodeURIComponent(latest.run_id)}?tab=verification`)}
            >
              {strings.dashboard.verificationReport}
            </Button>
          </div>
        </div>
      )}

      {pending > 0 && (
        <div className="card flex items-center gap-3 border-[var(--warn)] px-4 py-3">
          <AlertTriangle size={18} className="shrink-0 text-[var(--warn)]" />
          <span className="flex-1 text-[13px]">{strings.dashboard.approvalsPendingBanner(pending)}</span>
          <Button onClick={() => navigate("/approvals")}>{strings.dashboard.review}</Button>
        </div>
      )}

      <div className="card flex flex-col gap-3 p-4">
        <div className="flex items-center justify-between">
          <span className="text-section-title">{strings.dashboard.complianceTrend}</span>
          <Button variant="link" onClick={() => navigate("/history")}>
            {strings.dashboard.runsHistory}
          </Button>
        </div>
        <TrendPanel trend={trend} />
      </div>
    </div>
  );
}
