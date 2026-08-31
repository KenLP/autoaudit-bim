import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { strings } from "@/strings";
import type { HealthStatus } from "@/api/types";

const COLOR: Record<HealthStatus | "checking", string> = {
  up: "var(--ok)",
  unconfigured: "var(--ink-muted)",
  error: "var(--fail)",
  checking: "var(--ink-muted)",
};

const LABEL: Record<HealthStatus | "checking", string> = {
  up: strings.health.up,
  unconfigured: strings.health.unconfigured,
  error: strings.health.error,
  checking: strings.health.checking,
};

/** The /health contract sends BOOLEANS per axis (service HealthAxes model);
 *  the string vocabulary below is UI-only. Every call site used to cast the
 *  boolean straight to HealthStatus — `COLOR[true]` is undefined, so the
 *  pills rendered colorless (the "Setup statuses have no color" report) and
 *  `revitStatus === "up"` was never true, so "Highlight in Revit" stayed
 *  disabled against a connected Revit. Convert HERE, once. */
export function axisStatus(v: boolean | undefined): HealthStatus | "checking" {
  if (v === undefined) return "checking";
  return v ? "up" : "error";
}

export interface StatusPillProps {
  name: string;
  status: HealthStatus | "checking";
}

export function StatusPill({ name, status }: StatusPillProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="inline-flex items-center gap-1.5 text-[12px] text-[var(--ink-muted)]"
          data-testid={`status-pill-${name.toLowerCase()}`}
          data-status={status}
        >
          <span
            aria-hidden
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: COLOR[status] }}
          />
          {name}
        </span>
      </TooltipTrigger>
      <TooltipContent>{LABEL[status]}</TooltipContent>
    </Tooltip>
  );
}
