import { useEffect, useRef, useState } from "react";
import { Loader2, CheckCircle2, Circle, XCircle, Clock } from "lucide-react";
import { useAuditEvents } from "@/api/sse";
import { strings } from "@/strings";
import { cn } from "@/lib/utils";
import { formatDuration } from "@/lib/format";
import type { AuditPhase } from "@/api/types";

const PHASES: AuditPhase[] = [
  "service",
  "axes",
  "query",
  "qc",
  "design",
  "run",
];

const PHASE_ORDER: Record<AuditPhase, number> = {
  service: 0,
  axes: 1,
  query: 2,
  qc: 3,
  design: 4,
  record: 5,
  run: 6,
};

export interface LiveRunViewProps {
  auditId: string;
  failed?: boolean;
  onFinished?: () => void;
}

export function LiveRunView({ auditId, failed, onFinished }: LiveRunViewProps) {
  const { events, phase, connected } = useAuditEvents(auditId);
  const logRef = useRef<HTMLDivElement>(null);
  const currentRank = phase ? PHASE_ORDER[phase] : -1;

  // Simple elapsed clock (2026-07-12 polish pass) — counts from when this
  // view mounted, not from the audit's actual start time (close enough:
  // it mounts within one poll tick of the audit starting).
  const [elapsedSec, setElapsedSec] = useState(0);
  useEffect(() => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsedSec(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [events]);

  // Finish detection lives in RunPage (polls GET /audits/{id} until status
  // leaves "running") — the runner's terminal SSE mark is
  // ("service", "finished"), which is indistinguishable from other
  // service-phase events, so phase alone can't signal completion.
  useEffect(() => {
    const last = events[events.length - 1];
    if (last?.message === "finished") onFinished?.();
  }, [events, onFinished]);

  return (
    <div className="card flex flex-col gap-4 p-4" data-testid="live-run-view">
      <div className="flex items-center gap-1.5 text-caption" data-testid="live-run-elapsed">
        <Clock size={12} />
        {formatDuration(elapsedSec)}
      </div>
      {failed && (
        <div className="text-[var(--fail)] flex items-center gap-2">
          <XCircle size={16} />
          {strings.liveRun.failed}
        </div>
      )}
      <ol className="flex flex-col gap-2">
        {PHASES.map((p) => {
          const rank = PHASE_ORDER[p];
          const active = rank === currentRank && !failed;
          const done = rank < currentRank || (rank === currentRank && p === "run");
          return (
            <li key={p} className="flex items-center gap-2 text-[13px]">
              {active ? (
                <Loader2 size={14} className="animate-spin text-[var(--primary)]" />
              ) : done ? (
                <CheckCircle2 size={14} className="text-[var(--ok)]" />
              ) : (
                <Circle size={14} className="text-[var(--border)]" />
              )}
              <span className={cn(done && "text-[var(--ink)]", !done && "text-[var(--ink-muted)]")}>
                {strings.liveRun.phases[p]}
              </span>
            </li>
          );
        })}
      </ol>

      {!connected && events.length > 0 && (
        <div className="text-caption">{strings.liveRun.reconnecting}</div>
      )}

      <div
        ref={logRef}
        className="max-h-[40vh] overflow-y-auto rounded-[var(--radius)] bg-[var(--surface-2)] p-2 font-mono-val text-[12px]"
        data-testid="live-run-log"
      >
        {events.map((e, i) => (
          <div key={i}>
            <span className="text-[var(--ink-muted)]">[{e.phase}]</span> {e.message}
          </div>
        ))}
      </div>
    </div>
  );
}
