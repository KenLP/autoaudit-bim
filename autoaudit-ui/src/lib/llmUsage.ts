import type { LlmUsage } from "@/api/types";

/** Thousands separator without locale surprises in tests ("14,320"). */
export function formatCount(n: number): string {
  return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * One line for the run header — the same shape the CLI prints after a run
 * (`orchestrator._print_llm_usage`), so what the panel shows and what the
 * terminal shows can be read against each other:
 *
 *   22 calls · 14,320 tokens (in 11,900 / out 2,420) · diagnostic 19 / remediation 2 / supervisor 1 · claude-haiku-4-5-20251001
 *
 * Returns null when the run made no LLM call at all (a Phase-1 run has no
 * `llm_usage` in its metadata), so callers can render nothing rather than a
 * row of zeros that would read as "the AI was asked and answered nothing".
 */
export function formatLlmUsage(u: LlmUsage | null | undefined): string | null {
  if (!u || (u.total_calls ?? 0) === 0) return null;
  const parts: string[] = [`${u.total_calls} call${u.total_calls === 1 ? "" : "s"}`];
  const tokens = u.total_tokens ?? 0;
  if (tokens > 0) {
    const inTok = formatCount(u.input_tokens ?? 0);
    const outTok = formatCount(u.output_tokens ?? 0);
    parts.push(`${formatCount(tokens)} tokens (in ${inTok} / out ${outTok})`);
  }
  const byAgent = Object.entries(u.by_agent ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([agent, n]) => `${agent} ${n}`)
    .join(" / ");
  if (byAgent) parts.push(byAgent);
  if (u.models && u.models.length > 0) parts.push(u.models.join(", "));
  if ((u.failed_calls ?? 0) > 0) parts.push(`⚠️ ${u.failed_calls} failed`);
  return parts.join(" · ");
}
