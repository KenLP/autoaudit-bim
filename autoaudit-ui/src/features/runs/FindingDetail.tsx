import { ExternalLink, Crosshair, Stethoscope } from "lucide-react";
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
import type { Diagnosis, Finding, HealthStatus } from "@/api/types";

export interface FindingDetailProps {
  finding: Finding | null;
  revitStatus: HealthStatus | "checking";
  onHighlight: (finding: Finding) => void;
}

/** Everything the panel shows as a plain label/value row. `diagnosis` is NOT
 *  here: it arrives as an object from the Diagnostic agent and gets its own
 *  block below — squeezing it into a one-line `<dd>` is what printed
 *  "[object Object]". */
const EXTRA_FIELDS: Array<[keyof Finding, string]> = [
  ["evidence", "Evidence"],
  ["inherited_from", "Inherited from"],
];

/** Normalise the two shapes the wire actually carries (see `Finding.diagnosis`):
 *  the agent's object, or a bare string from an older/deterministic run. */
function asDiagnosis(value: unknown): Diagnosis | null {
  if (value == null) return null;
  if (typeof value === "string") return value.trim() ? { summary: value } : null;
  if (typeof value !== "object") return null;
  const d = value as Diagnosis;
  return d.summary || d.suggested_action ? d : null;
}

function DiagnosisBlock({ diagnosis }: { diagnosis: Diagnosis }) {
  const source = diagnosis.source?.trim();
  return (
    <section
      data-testid="finding-diagnosis"
      className="rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface-2)] p-3"
    >
      <h3 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-[var(--ink-muted)]">
        <Stethoscope size={13} />
        {strings.runDetail.diagnosis}
      </h3>

      {diagnosis.summary && (
        <p className="text-[14px] leading-relaxed text-[var(--ink)]">
          {diagnosis.summary}
        </p>
      )}

      {diagnosis.suggested_action && (
        <>
          <h4 className="mb-1 mt-3 text-[11px] font-semibold uppercase tracking-wide text-[var(--ink-muted)]">
            {strings.runDetail.diagnosisAction}
          </h4>
          <p className="text-[14px] leading-relaxed text-[var(--ink)]">
            {diagnosis.suggested_action}
          </p>
        </>
      )}

      {source && (
        <p className="mt-2 text-[11px] text-[var(--ink-muted)]">
          {strings.runDetail.diagnosisBy(source, diagnosis.confidence ?? undefined)}
        </p>
      )}
    </section>
  );
}

export function FindingDetail({ finding, revitStatus, onHighlight }: FindingDetailProps) {
  if (!finding) {
    return (
      <div className="card flex h-full items-center justify-center p-4 text-[var(--ink-muted)]">
        {strings.runDetail.noSelection}
      </div>
    );
  }

  const revitReady = revitStatus === "up";
  const diagnosis = asDiagnosis(finding.diagnosis);

  return (
    <div className="card flex flex-col gap-3 overflow-y-auto p-3">
      <div className="text-section-title">
        <MonoText>{finding.element_id}</MonoText>
      </div>
      <div className="flex gap-2">
        <BucketBadge bucket={finding.bucket} />
        <SeverityBadge severity={finding.severity} />
      </div>

      {/* The agent's words come FIRST. They are why a reviewer opens a
          finding; underneath the (often long) Message they sat below the
          fold of a 300px panel and had to be scrolled to on camera. */}
      {diagnosis && <DiagnosisBlock diagnosis={diagnosis} />}

      {/* Tailwind arbitrary values are underscore-separated, not
          comma-separated: `grid-cols-[auto,1fr]` compiles to the invalid
          declaration `grid-template-columns: auto,1fr`, which the CSS parser
          drops — so this list silently rendered as ONE column. */}
      <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[13px]">
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
