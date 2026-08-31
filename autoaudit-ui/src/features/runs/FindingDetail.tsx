import { ExternalLink, Crosshair } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { MonoText } from "@/components/MonoText";
import { BucketBadge } from "@/components/BucketBadge";
import { SeverityBadge } from "@/components/SeverityBadge";
import { strings } from "@/strings";
import type { Finding, HealthStatus } from "@/api/types";

export interface FindingDetailProps {
  finding: Finding | null;
  revitStatus: HealthStatus | "checking";
  onHighlight: (finding: Finding) => void;
}

const EXTRA_FIELDS: Array<[keyof Finding, string]> = [
  ["diagnosis", "Diagnosis"],
  ["evidence", "Evidence"],
  ["inherited_from", "Inherited from"],
];

export function FindingDetail({ finding, revitStatus, onHighlight }: FindingDetailProps) {
  if (!finding) {
    return (
      <div className="card flex h-full items-center justify-center p-4 text-[var(--ink-muted)]">
        {strings.runDetail.noSelection}
      </div>
    );
  }

  const revitReady = revitStatus === "up";

  return (
    <div className="card flex flex-col gap-3 overflow-y-auto p-3">
      <div className="text-section-title">
        <MonoText>{finding.element_id}</MonoText>
      </div>
      <div className="flex gap-2">
        <BucketBadge bucket={finding.bucket} />
        <SeverityBadge severity={finding.severity} />
      </div>

      <dl className="grid grid-cols-[auto,1fr] gap-x-2 gap-y-1 text-[13px]">
        <dt className="text-[var(--ink-muted)]">Rule</dt>
        <dd className="font-mono-val">{finding.rule_id}</dd>
        <dt className="text-[var(--ink-muted)]">Parameter</dt>
        <dd>{finding.parameter ?? "—"}</dd>
        <dt className="text-[var(--ink-muted)]">Value</dt>
        <dd className="font-mono-val">{finding.value ?? "(empty)"}</dd>
        {finding.suggested_value != null && (
          <>
            <dt className="text-[var(--ink-muted)]">Suggested</dt>
            <dd className="font-mono-val">{finding.suggested_value}</dd>
          </>
        )}
        {finding.message && (
          <>
            <dt className="text-[var(--ink-muted)]">Message</dt>
            <dd>{finding.message}</dd>
          </>
        )}
        {EXTRA_FIELDS.map(([key, label]) =>
          finding[key] ? (
            <>
              <dt key={`${String(key)}-label`} className="text-[var(--ink-muted)]">
                {label}
              </dt>
              <dd key={`${String(key)}-value`}>{String(finding[key])}</dd>
            </>
          ) : null,
        )}
      </dl>

      <div className="mt-2 flex flex-col gap-2">
        {revitReady ? (
          <Button variant="outline" onClick={() => onHighlight(finding)}>
            <Crosshair size={14} />
            {strings.runDetail.highlight}
          </Button>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <span>
                <Button variant="outline" disabled className="w-full">
                  <Crosshair size={14} />
                  {strings.runDetail.highlight}
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>{strings.runDetail.highlightDisabledTooltip}</TooltipContent>
          </Tooltip>
        )}
        {finding.acc_issue_url && (
          <a
            href={finding.acc_issue_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-[var(--primary)]"
          >
            <ExternalLink size={14} />
            {strings.runDetail.accIssue}
          </a>
        )}
      </div>
    </div>
  );
}
