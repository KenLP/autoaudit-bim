import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { MonoText } from "@/components/MonoText";
import { strings } from "@/strings";
import { formatDateTime } from "@/lib/format";
import { useIgnoreApproval, useRestoreApproval } from "@/api/hooks";
import type { ApprovalRecord } from "@/api/types";

const STATUS_LABEL: Record<ApprovalRecord["status"], string> = {
  pending: strings.approvals.statusPending,
  applied: strings.approvals.statusApplied,
  applied_issue_open: strings.approvals.statusAppliedIssueOpen,
  ignored: strings.approvals.statusIgnored,
};

const STATUS_COLOR: Record<ApprovalRecord["status"], string> = {
  pending: "var(--warn)",
  applied: "var(--ok)",
  applied_issue_open: "var(--ok)",
  ignored: "var(--ink-muted)",
};

export function ProposalCard({ proposal }: { proposal: ApprovalRecord }) {
  const ignore = useIgnoreApproval();
  const restore = useRestoreApproval();
  const [confirmIgnore, setConfirmIgnore] = useState(false);
  const [confirmRestore, setConfirmRestore] = useState(false);

  return (
    <div className="card flex flex-col gap-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-section-title">{proposal.display_id}</span>
        <span className="text-caption font-mono-val">
          {proposal.rule_ids.join(", ")}
        </span>
        <span className="text-caption">
          {proposal.fixes.length} fix{proposal.fixes.length === 1 ? "" : "es"}
        </span>
        <Badge variant="outline" color={STATUS_COLOR[proposal.status]}>
          {STATUS_LABEL[proposal.status]}
        </Badge>
        <span className="text-caption ml-auto">
          {formatDateTime(proposal.created_at)}
        </span>
      </div>

      {proposal.issue_id && (
        <a
          href={`#issue-${proposal.issue_id}`}
          className="inline-flex w-fit items-center gap-1 text-[13px] text-[var(--primary)]"
        >
          <ExternalLink size={13} />
          {strings.approvals.accIssueLink} #{proposal.issue_id}
        </a>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-left text-[13px]">
          <thead>
            <tr className="text-caption">
              <th className="pr-3">{strings.runDetail.columnElement}</th>
              <th className="pr-3">{strings.runDetail.columnParameter}</th>
              <th className="pr-3">Current</th>
              <th className="pr-3">Proposed</th>
            </tr>
          </thead>
          <tbody>
            {proposal.fixes.map((fx, i) => (
              <tr key={i} className="h-[30px] border-t border-[var(--border)]">
                <td className="pr-3">
                  <MonoText>{fx.element_id}</MonoText>
                </td>
                <td className="pr-3">{fx.parameter}</td>
                <td className="pr-3 font-mono-val">
                  {fx.old_value === "" || fx.old_value == null ? "(empty)" : fx.old_value}
                  {fx.inherited_from && (
                    <span className="text-caption ml-1">
                      ⤺ {strings.approvals.inheritedFromHost}: {fx.inherited_from}
                    </span>
                  )}
                </td>
                <td className="pr-3 font-mono-val">{fx.new_value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex justify-end">
        {proposal.status === "pending" && (
          <Button variant="outline" size="sm" onClick={() => setConfirmIgnore(true)}>
            {strings.approvals.ignore}
          </Button>
        )}
        {proposal.status === "ignored" && (
          <Button variant="outline" size="sm" onClick={() => setConfirmRestore(true)}>
            {strings.approvals.restore}
          </Button>
        )}
      </div>

      <ConfirmDialog
        open={confirmIgnore}
        onOpenChange={setConfirmIgnore}
        title={strings.approvals.confirmIgnoreTitle}
        description={strings.approvals.confirmIgnoreBody}
        loading={ignore.isPending}
        onConfirm={() => ignore.mutate(proposal.file, { onSuccess: () => setConfirmIgnore(false) })}
      />
      <ConfirmDialog
        open={confirmRestore}
        onOpenChange={setConfirmRestore}
        title={strings.approvals.confirmRestoreTitle}
        description={strings.approvals.confirmRestoreBody}
        loading={restore.isPending}
        onConfirm={() => restore.mutate(proposal.file, { onSuccess: () => setConfirmRestore(false) })}
      />
    </div>
  );
}
