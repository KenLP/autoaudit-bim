import { useEffect, useRef, useState } from "react";
import type { AuditEvent, AuditPhase } from "./types";

/**
 * Wraps EventSource with auto-reconnect (exponential backoff, capped).
 * Kept as a plain class (no React) so the reconnect algorithm is
 * unit-testable without mounting a component — inject a factory for
 * EventSource in tests.
 */
export type EventSourceFactory = (url: string) => EventSource;

const defaultFactory: EventSourceFactory = (url) => new EventSource(url);

export class ReconnectingEventSource {
  private url: string;
  private factory: EventSourceFactory;
  private source: EventSource | null = null;
  private closed = false;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly maxDelayMs = 10_000;
  /** Delay used for the most recently scheduled reconnect (test hook). */
  private lastScheduledDelay = 0;

  onMessage: ((data: string) => void) | null = null;
  onConnected: (() => void) | null = null;
  onDisconnected: (() => void) | null = null;

  constructor(url: string, factory: EventSourceFactory = defaultFactory) {
    this.url = url;
    this.factory = factory;
    this.connect();
  }

  private connect() {
    if (this.closed) return;
    const source = this.factory(this.url);
    this.source = source;
    source.onmessage = (evt: MessageEvent) => {
      this.attempt = 0;
      this.onMessage?.(evt.data);
    };
    source.onopen = () => {
      this.attempt = 0;
      this.onConnected?.();
    };
    source.onerror = () => {
      this.onDisconnected?.();
      source.close();
      if (this.closed) return;
      const delay = Math.min(1000 * 2 ** this.attempt, this.maxDelayMs);
      this.lastScheduledDelay = delay;
      this.attempt += 1;
      this.timer = setTimeout(() => this.connect(), delay);
    };
  }

  /** Delay used to schedule the most recent reconnect attempt (for test assertions). */
  nextDelayMs(): number {
    return this.lastScheduledDelay;
  }

  close() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.source?.close();
  }
}

export interface AuditEventsState {
  events: AuditEvent[];
  phase: AuditPhase | null;
  connected: boolean;
}

/**
 * The service replays a job's FULL event history on every new SSE connection
 * (`runner.py stream_events` starts at index 0) — without dedup, one dropped
 * connection doubles the whole live log and can re-fire "finished"
 * (2026-07 review, FE-8). Events are append-only and replayed in order, so
 * a per-connection index against a delivered-count is a sound dedup key.
 * Kept as a plain class so it is unit-testable without mounting the hook.
 */
export class ReplayDedup {
  private delivered = 0;
  private connSeen = 0;

  /** Call when a (re)connection opens — replay restarts from index 0. */
  onConnect(): void {
    this.connSeen = 0;
  }

  /** True if this event is NEW (not a replay of one already delivered). */
  accept(): boolean {
    const idx = this.connSeen;
    this.connSeen += 1;
    if (idx < this.delivered) return false;
    this.delivered = idx + 1;
    return true;
  }

  reset(): void {
    this.delivered = 0;
    this.connSeen = 0;
  }
}

const PHASE_ORDER: AuditPhase[] = [
  "service",
  "axes",
  "query",
  "qc",
  "design",
  "record",
  "run",
];

/** The runner's own terminal marks (`runner.py`): ("service", "finished") on
 *  success, ("service", "failed …") on any failure. After one of these the
 *  stream will never carry another event — the server replays the full history
 *  and closes on every future connection.
 *
 *  Found live re-accepting the AU demo arc (2026-07-29): EventSource treats
 *  that close as an error, so ReconnectingEventSource redialed a DEAD stream
 *  about once a second, each time pulling a full replay, for as long as the
 *  Run page stayed open. The window is usually seconds (auto-navigate leaves
 *  on "done") — except on a FAILED audit, where the page deliberately keeps
 *  the operator in place to read the error, i.e. the one moment on stage you
 *  least want a request loop in the devtools you may be projecting. */
export function isTerminalEvent(e: AuditEvent): boolean {
  return e.phase === "service" && (e.message === "finished" || e.message.startsWith("failed"));
}

/** Highest-reached phase across the events seen so far. */
export function latestPhase(events: AuditEvent[]): AuditPhase | null {
  let best = -1;
  for (const e of events) {
    const idx = PHASE_ORDER.indexOf(e.phase);
    if (idx > best) best = idx;
  }
  return best >= 0 ? PHASE_ORDER[best] : null;
}

export function useAuditEvents(auditId: string | null): AuditEventsState {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<ReconnectingEventSource | null>(null);

  useEffect(() => {
    setEvents([]);
    setConnected(false);
    if (!auditId) return;

    const dedup = new ReplayDedup();
    const rs = new ReconnectingEventSource(
      `/api/audits/${encodeURIComponent(auditId)}/events`,
    );
    sourceRef.current = rs;
    rs.onConnected = () => {
      dedup.onConnect();
      setConnected(true);
    };
    rs.onDisconnected = () => setConnected(false);
    rs.onMessage = (raw) => {
      if (!dedup.accept()) return; // replayed history after a reconnect
      try {
        const parsed = JSON.parse(raw) as AuditEvent;
        setEvents((prev) => [...prev, parsed]);
        if (isTerminalEvent(parsed)) {
          // The stream is complete — the server will only ever replay-and-close
          // from here on, and EventSource reads each close as an error, so
          // without this the reconnect loop redials a dead stream forever.
          // Dedup guarantees the terminal event is ACCEPTED exactly once, even
          // when it first arrives via a replay after a mid-stream drop.
          rs.close();
          setConnected(false);
        }
      } catch {
        // Ignore malformed frames — never crash the live view.
      }
    };

    return () => {
      rs.close();
      sourceRef.current = null;
    };
  }, [auditId]);

  return { events, phase: latestPhase(events), connected };
}
