import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isTerminalEvent, latestPhase, ReconnectingEventSource, ReplayDedup } from "./sse";
import type { AuditEvent } from "./types";

class FakeEventSource {
  onmessage: ((evt: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  static instances: FakeEventSource[] = [];
  static reset() {
    FakeEventSource.instances = [];
  }
}

describe("latestPhase", () => {
  it("returns null when there are no events", () => {
    expect(latestPhase([])).toBeNull();
  });

  it("returns the furthest-reached phase, not just the last event", () => {
    const events: AuditEvent[] = [
      { ts: "1", phase: "query", message: "" },
      { ts: "2", phase: "axes", message: "" },
    ];
    expect(latestPhase(events)).toBe("query");
  });
});

describe("ReplayDedup", () => {
  it("accepts every event on the first connection", () => {
    const d = new ReplayDedup();
    d.onConnect();
    expect([d.accept(), d.accept(), d.accept()]).toEqual([true, true, true]);
  });

  it("skips replayed history after a reconnect, then accepts new events", () => {
    // Server replays the FULL history from index 0 on every new connection
    // (runner.py stream_events) — FE-8: only genuinely new events pass.
    const d = new ReplayDedup();
    d.onConnect();
    d.accept(); // ev0
    d.accept(); // ev1
    d.onConnect(); // reconnect → server replays ev0, ev1, then ev2 (new)
    expect(d.accept()).toBe(false); // ev0 replay
    expect(d.accept()).toBe(false); // ev1 replay
    expect(d.accept()).toBe(true); // ev2 — new
    expect(d.accept()).toBe(true); // ev3 — new
  });

  it("a second reconnect replays the larger history and still dedups", () => {
    const d = new ReplayDedup();
    d.onConnect();
    d.accept();
    d.onConnect();
    d.accept(); // replay of ev0 → false, but count the call anyway
    expect(d.accept()).toBe(true); // ev1 new
    d.onConnect();
    expect([d.accept(), d.accept()]).toEqual([false, false]); // ev0+ev1 replay
    expect(d.accept()).toBe(true); // ev2 new
  });

  it("reset() clears delivered state (new audit id)", () => {
    const d = new ReplayDedup();
    d.onConnect();
    d.accept();
    d.reset();
    d.onConnect();
    expect(d.accept()).toBe(true);
  });
});

describe("ReconnectingEventSource", () => {
  beforeEach(() => {
    FakeEventSource.reset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens a connection immediately via the injected factory", () => {
    const rs = new ReconnectingEventSource(
      "/api/audits/abc/events",
      (url) => new FakeEventSource(url) as unknown as EventSource,
    );
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/audits/abc/events");
    rs.close();
  });

  it("forwards message payloads via onMessage", () => {
    const rs = new ReconnectingEventSource(
      "/api/audits/abc/events",
      (url) => new FakeEventSource(url) as unknown as EventSource,
    );
    const received: string[] = [];
    rs.onMessage = (data) => received.push(data);
    const source = FakeEventSource.instances[0];
    source.onmessage?.({ data: '{"phase":"query"}' } as MessageEvent);
    expect(received).toEqual(['{"phase":"query"}']);
    rs.close();
  });

  it("reconnects with exponential backoff after an error, capped at 10s", () => {
    const rs = new ReconnectingEventSource(
      "/api/audits/abc/events",
      (url) => new FakeEventSource(url) as unknown as EventSource,
    );
    const first = FakeEventSource.instances[0];
    first.onerror?.();
    expect(first.closed).toBe(true);
    expect(rs.nextDelayMs()).toBe(1000);

    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances).toHaveLength(2);

    const second = FakeEventSource.instances[1];
    second.onerror?.();
    expect(rs.nextDelayMs()).toBe(2000);

    rs.close();
  });

  it("stops reconnecting once closed", () => {
    const rs = new ReconnectingEventSource(
      "/api/audits/abc/events",
      (url) => new FakeEventSource(url) as unknown as EventSource,
    );
    rs.close();
    const first = FakeEventSource.instances[0];
    first.onerror?.();
    vi.advanceTimersByTime(20_000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("resets the backoff attempt counter after a successful reconnect", () => {
    const rs = new ReconnectingEventSource(
      "/api/audits/abc/events",
      (url) => new FakeEventSource(url) as unknown as EventSource,
    );
    FakeEventSource.instances[0].onerror?.();
    vi.advanceTimersByTime(1000);
    FakeEventSource.instances[1].onopen?.();
    expect(rs.nextDelayMs()).toBe(1000);
    rs.close();
  });
});

describe("isTerminalEvent", () => {
  const ev = (phase: string, message: string) =>
    ({ ts: "t", phase, message }) as unknown as AuditEvent;

  it("recognises the runner's success mark", () => {
    expect(isTerminalEvent(ev("service", "finished"))).toBe(true);
  });

  it("recognises every failure mark shape the runner emits", () => {
    // runner.py emits "failed exit_code=2" and "failed <exception text>"
    expect(isTerminalEvent(ev("service", "failed exit_code=2"))).toBe(true);
    expect(isTerminalEvent(ev("service", "failed boom"))).toBe(true);
  });

  it("ignores ordinary service-phase chatter and other phases", () => {
    expect(isTerminalEvent(ev("service", "started"))).toBe(false);
    // "finished" from another phase is a coincidence of wording, not the mark
    expect(isTerminalEvent(ev("record", "finished"))).toBe(false);
    expect(isTerminalEvent(ev("qc", "qc_agent.done"))).toBe(false);
  });
});

describe("useAuditEvents — terminal close (2026-07-29 re-acceptance finding)", () => {
  /** The server replays the full history and CLOSES on every connection to a
   *  finished audit; EventSource reads that close as an error. Pre-fix, the
   *  live view redialed that dead stream ~1/s for as long as the Run page
   *  stayed open — and a FAILED audit deliberately keeps the operator on the
   *  page. These mount the real hook against the fake source. */
  let realES: unknown;

  beforeEach(() => {
    FakeEventSource.reset();
    vi.useFakeTimers();
    realES = (globalThis as Record<string, unknown>).EventSource;
    (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  });

  afterEach(() => {
    (globalThis as Record<string, unknown>).EventSource = realES;
    vi.useRealTimers();
  });

  const frame = (phase: string, message: string) =>
    ({ data: JSON.stringify({ ts: "t", phase, message }) }) as MessageEvent;

  async function mountHook() {
    const { renderHook, act } = await import("@testing-library/react");
    const { useAuditEvents } = await import("./sse");
    return { renderHook, act, useAuditEvents };
  }

  it("stops reconnecting once the stream says finished", async () => {
    const { renderHook, act, useAuditEvents } = await mountHook();
    const { unmount } = renderHook(() => useAuditEvents("aud-1"));
    const source = FakeEventSource.instances[0];
    act(() => {
      source.onopen?.();
      source.onmessage?.(frame("query", "querying"));
      source.onmessage?.(frame("service", "finished"));
    });
    expect(source.closed).toBe(true);
    // The close must be OURS, not the error path: a subsequent server close
    // (onerror) must not schedule a redial.
    act(() => {
      source.onerror?.();
      vi.advanceTimersByTime(30_000);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    unmount();
  });

  it("stops reconnecting on a FAILED audit — the page that keeps the operator around", async () => {
    const { renderHook, act, useAuditEvents } = await mountHook();
    const { unmount } = renderHook(() => useAuditEvents("aud-2"));
    const source = FakeEventSource.instances[0];
    act(() => {
      source.onopen?.();
      source.onmessage?.(frame("service", "failed exit_code=2"));
      source.onerror?.();
      vi.advanceTimersByTime(30_000);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    unmount();
  });

  it("closes when the terminal event first arrives via a replay after a drop", async () => {
    const { renderHook, act, useAuditEvents } = await mountHook();
    const { unmount } = renderHook(() => useAuditEvents("aud-3"));
    const first = FakeEventSource.instances[0];
    act(() => {
      first.onopen?.();
      first.onmessage?.(frame("query", "querying"));
      first.onerror?.(); // dropped before the terminal event arrived
      vi.advanceTimersByTime(1000);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    const second = FakeEventSource.instances[1];
    act(() => {
      second.onopen?.();
      second.onmessage?.(frame("query", "querying")); // replay — deduped
      second.onmessage?.(frame("service", "finished")); // NEW — accepted
    });
    expect(second.closed).toBe(true);
    act(() => {
      second.onerror?.();
      vi.advanceTimersByTime(30_000);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    unmount();
  });

  it("a still-running audit keeps reconnecting exactly as before", async () => {
    const { renderHook, act, useAuditEvents } = await mountHook();
    const { unmount } = renderHook(() => useAuditEvents("aud-4"));
    const first = FakeEventSource.instances[0];
    act(() => {
      first.onopen?.();
      first.onmessage?.(frame("query", "querying")); // no terminal yet
      first.onerror?.(); // transient drop mid-run
      vi.advanceTimersByTime(1000);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    unmount();
  });
});
