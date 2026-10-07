import { describe, expect, it } from "vitest";
import { formatCount, formatLlmUsage } from "./llmUsage";
import type { LlmUsage } from "@/api/types";

const measured: LlmUsage = {
  // run-be82bccd (2026-09-03), the first run through the panel with token
  // accounting on — the numbers the 3-agent clip reads aloud.
  total_calls: 22,
  total_seconds: 52.6,
  by_agent: { diagnostic: 19, remediation: 2, supervisor: 1 },
  failed_calls: 0,
  failed_by_agent: {},
  blocked: 0,
  max_calls: 200,
  models: ["claude-haiku-4-5-20251001"],
  total_tokens: 14716,
  input_tokens: 12281,
  output_tokens: 2435,
  tokens_by_agent: {
    diagnostic: { input: 10386, output: 2236 },
    remediation: { input: 1154, output: 112 },
    supervisor: { input: 741, output: 87 },
  },
};

describe("formatLlmUsage", () => {
  it("renders calls, tokens, per-agent breakdown and model on one line", () => {
    expect(formatLlmUsage(measured)).toBe(
      "22 calls · 14,716 tokens (in 12,281 / out 2,435) · " +
        "diagnostic 19 / remediation 2 / supervisor 1 · claude-haiku-4-5-20251001",
    );
  });

  it("is null for a run that made no call — a Phase-1 run must render nothing, not zeros", () => {
    expect(formatLlmUsage(null)).toBeNull();
    expect(formatLlmUsage(undefined)).toBeNull();
    expect(formatLlmUsage({ ...measured, total_calls: 0 })).toBeNull();
  });

  it("omits the token clause when the provider reported none (older runs, fake client)", () => {
    const noTokens: LlmUsage = { ...measured, total_tokens: 0, input_tokens: 0, output_tokens: 0 };
    const line = formatLlmUsage(noTokens)!;
    expect(line).not.toContain("tokens");
    expect(line).toContain("22 calls");
  });

  it("names failed calls — an empty-handed run must say the model was asked", () => {
    expect(formatLlmUsage({ ...measured, failed_calls: 3 })).toContain("⚠️ 3 failed");
  });
});

describe("formatCount", () => {
  it("groups thousands", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(999)).toBe("999");
    expect(formatCount(14320)).toBe("14,320");
    expect(formatCount(1234567)).toBe("1,234,567");
  });
});
