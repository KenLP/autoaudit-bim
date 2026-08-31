import { useState } from "react";
import { CheckSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { strings } from "@/strings";
import { useApplyApprovals, useApprovals } from "@/api/hooks";
import { ProposalCard } from "./ProposalCard";
import { toast } from "sonner";

export function ApprovalsPage() {
  const { data, isLoading, isError, error } = useApprovals();
  const applyApprovals = useApplyApprovals();
  const [confirmApply, setConfirmApply] = useState(false);
  const [ignoredOpen, setIgnoredOpen] = useState(false);

  const proposals = data?.proposals ?? [];
  const active = proposals.filter((p) => p.status !== "ignored");
  const ignored = proposals.filter((p) => p.status === "ignored");

  function handleApply() {
    applyApprovals.mutate(undefined, {
      onSuccess: (res) => {
        toast(strings.approvals.resultBanner(res.applied, res.held), {
          description: res.held > 0 ? strings.approvals.heldExplanation : undefined,
        });
        setConfirmApply(false);
      },
      onError: (err) => {
        toast.error(String(err));
        setConfirmApply(false);
      },
    });
  }

  // FE-7 (2026-07 review): error ≠ empty — a dead service must not render
  // "No proposals yet".
  if (isError) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.approvals.title} description={strings.approvals.description} />
        <ApiErrorBanner error={error} />
      </div>
    );
  }

  if (!isLoading && proposals.length === 0) {
    return (
      <div className="flex flex-col gap-6 p-4">
        <PageHeader title={strings.approvals.title} description={strings.approvals.description} />
        <EmptyState
          icon={CheckSquare}
          title={strings.approvals.emptyTitle}
          body={strings.approvals.emptyBody}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <PageHeader
        title={strings.approvals.title}
        description={strings.approvals.description}
        actions={<Button onClick={() => setConfirmApply(true)}>{strings.approvals.applyNow}</Button>}
      />
      {data && (
        <span className="text-caption">
          {strings.approvals.pendingCount(data.counts.pending)} ·{" "}
          {strings.approvals.appliedCount(data.counts.applied)}
        </span>
      )}
      <p className="text-caption">{strings.approvals.caption}</p>

      <div className="flex flex-col gap-3">
        {active.map((p) => (
          <ProposalCard key={p.file} proposal={p} />
        ))}
      </div>

      {ignored.length > 0 && (
        <Collapsible open={ignoredOpen} onOpenChange={setIgnoredOpen}>
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm">
              {strings.approvals.ignoredSection} ({ignored.length})
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-3 pt-2">
            {ignored.map((p) => (
              <ProposalCard key={p.file} proposal={p} />
            ))}
          </CollapsibleContent>
        </Collapsible>
      )}

      <ConfirmDialog
        open={confirmApply}
        onOpenChange={setConfirmApply}
        title={strings.approvals.confirmApplyTitle}
        description={strings.approvals.confirmApplyBody}
        loading={applyApprovals.isPending}
        onConfirm={handleApply}
      />
    </div>
  );
}
