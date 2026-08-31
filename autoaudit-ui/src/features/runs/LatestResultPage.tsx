import { Navigate, useNavigate } from "react-router-dom";
import { Rocket } from "lucide-react";
import { useRuns } from "@/api/hooks";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { strings } from "@/strings";

/** "Results" = the SINGLE most recent run (or the one currently running).
 *  It resolves the latest run id and hands off to the run detail view; the
 *  full cross-run list + trend lives under History (`/history`). */
export function LatestResultPage() {
  const { data: runs, isLoading, isError, error } = useRuns();
  const navigate = useNavigate();

  if (isLoading) {
    return <div className="p-4 text-[var(--ink-muted)]">{strings.common.loading}</div>;
  }

  // FE-7 (2026-07 review): error ≠ empty — a dead service must not render
  // the "no runs yet" empty state.
  if (isError) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.results.title} description={strings.results.description} />
        <ApiErrorBanner error={error} />
      </div>
    );
  }

  const latest = runs?.[0];
  if (latest) {
    return <Navigate to={`/results/${encodeURIComponent(latest.run_id)}`} replace />;
  }

  return (
    <div className="flex flex-col gap-6 p-4">
      <PageHeader title={strings.results.title} description={strings.results.description} />
      <EmptyState
        icon={Rocket}
        title={strings.results.emptyTitle}
        body={strings.results.emptyBody}
        actionLabel={strings.results.emptyAction}
        onAction={() => navigate("/run")}
      />
    </div>
  );
}
