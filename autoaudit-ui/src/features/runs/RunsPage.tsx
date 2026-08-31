import { useNavigate } from "react-router-dom";
import { History } from "lucide-react";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { EmptyState } from "@/components/EmptyState";
import { MonoText } from "@/components/MonoText";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { strings } from "@/strings";
import { useRuns, useTrend } from "@/api/hooks";
import { formatCompliancePct, formatDateTime, formatDuration } from "@/lib/format";
import { TrendPanel } from "./TrendPanel";

export function RunsPage() {
  const { data, isLoading, isError, error } = useRuns();
  const { data: trend } = useTrend();
  const navigate = useNavigate();

  const runs = data ?? [];

  // FE-7 (2026-07 review): error ≠ empty — a dead service must not render
  // the "no runs yet" empty state.
  if (isError) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.runsPage.title} description={strings.runsPage.description} />
        <ApiErrorBanner error={error} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 p-4">
      <PageHeader title={strings.runsPage.title} description={strings.runsPage.description} />

      <div className="card p-4">
        <div className="text-section-title mb-3">{strings.trend.title}</div>
        <TrendPanel trend={trend} />
      </div>

      {!isLoading && runs.length === 0 ? (
        <EmptyState
          icon={History}
          title={strings.runsPage.emptyTitle}
          body={strings.runsPage.emptyBody}
          actionLabel={strings.runsPage.emptyAction}
          onAction={() => navigate("/run")}
        />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-left text-[13px]">
            <thead className="sticky top-0 z-10 bg-[var(--surface)]">
              <tr className="h-8 border-b border-[var(--border)] text-caption">
                <th className="px-3">{strings.runsPage.columnRunId}</th>
                <th className="px-3">{strings.runsPage.columnMode}</th>
                <th className="px-3">{strings.runsPage.columnStarted}</th>
                <th className="px-3">{strings.runsPage.columnDuration}</th>
                <th className="px-3">{strings.runsPage.columnStatus}</th>
                <th className="px-3">{strings.runsPage.columnCompliance}</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, i) => {
                const corrupt =
                  run.status === "metadata_missing" || run.status === "metadata_corrupt";
                const total =
                  (run.outcomes_summary?.compliant ?? 0) +
                  (run.outcomes_summary?.non_compliant ?? 0) +
                  (run.outcomes_summary?.manual_review ?? 0) +
                  (run.outcomes_summary?.missing_data ?? 0);
                const pct =
                  run.outcomes_summary && total > 0
                    ? (run.outcomes_summary.compliant / total) * 100
                    : null;
                const zebra = i % 2 === 1;
                const row = (
                  <tr
                    key={run.run_id}
                    className={`h-[34px] cursor-pointer border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)] ${
                      zebra ? "bg-[var(--surface-2)]" : ""
                    } ${corrupt ? "opacity-50" : ""}`}
                    onClick={() =>
                      !corrupt && navigate(`/results/${encodeURIComponent(run.run_id)}`)
                    }
                  >
                    <td className="px-3">
                      <MonoText>{run.run_id}</MonoText>
                    </td>
                    <td className="px-3">{run.mode}</td>
                    <td className="px-3">{formatDateTime(run.started_at)}</td>
                    <td className="px-3">{formatDuration(run.duration_seconds)}</td>
                    <td className="px-3">
                      <Badge variant="outline">{run.status}</Badge>
                    </td>
                    <td className="px-3 font-mono-val">{formatCompliancePct(pct)}</td>
                  </tr>
                );
                if (!corrupt) return row;
                return (
                  <Tooltip key={run.run_id}>
                    <TooltipTrigger asChild>{row}</TooltipTrigger>
                    <TooltipContent>{strings.runsPage.corruptTooltip}</TooltipContent>
                  </Tooltip>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
