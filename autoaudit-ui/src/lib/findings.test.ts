import { describe, expect, it } from "vitest";
import {
  DEFAULT_FINDINGS_FILTER,
  filterFindings,
  groupByRule,
  mergeOutcomeFindings,
  sortFindings,
} from "./findings";
import type { Finding, OutcomesResponse } from "@/api/types";

function finding(overrides: Partial<Finding>): Finding {
  return {
    rule_id: "fire_rating_format",
    element_id: 1,
    status: "non_compliant",
    bucket: "non_compliant",
    severity: "medium",
    ...overrides,
  };
}

describe("mergeOutcomeFindings", () => {
  it("flattens the three finding buckets and dedupes by rule+element+bucket", () => {
    const outcomes: OutcomesResponse = {
      outcomes_summary: { compliant: 0, non_compliant: 1, manual_review: 1, missing_data: 1 },
      non_compliant: [finding({ element_id: 1, bucket: "non_compliant" })],
      manual_review_items: [finding({ element_id: 2, bucket: "manual_review" })],
      missing_data_items: [finding({ element_id: 3, bucket: "missing_data" })],
      proposed_fixes: [],
    };
    const merged = mergeOutcomeFindings(outcomes);
    expect(merged).toHaveLength(3);
    expect(merged.map((f) => f.element_id)).toEqual([1, 2, 3]);
  });

  it("drops an exact duplicate (same rule, element, bucket) across lists", () => {
    const dup = finding({ element_id: 1, bucket: "non_compliant" });
    const outcomes: OutcomesResponse = {
      outcomes_summary: { compliant: 0, non_compliant: 1, manual_review: 0, missing_data: 0 },
      non_compliant: [dup, dup],
      manual_review_items: [],
      missing_data_items: [],
      proposed_fixes: [],
    };
    expect(mergeOutcomeFindings(outcomes)).toHaveLength(1);
  });

  it("normalizes the REAL engine shape: severity_x tag, no bucket key, current_value", () => {
    // Mirrors runs/run-f3cade96/outcomes.json verbatim (2026-07 review, B1):
    // the engine writes severity as a "severity_high" tag, never writes a
    // `bucket` key (list membership IS the bucket), and calls the observed
    // value `current_value`. Before this normalization the Results page
    // filtered to 0 rows and every Severity cell rendered "—".
    const engineFinding = {
      rule_id: "demo.doors.fire_rating",
      element_id: "703",
      parameter: "Fire Rating",
      severity_tag: "fire_safety_change",
      severity: "severity_high",
      message: "…must be present…",
      current_value: "120 min",
      suggested_value: "2 HR",
      citation: null,
      status: "non_compliant",
      element_name: "Door-Single-Flush - 36x84 (120 MIN)",
    } as unknown as Finding;
    const outcomes: OutcomesResponse = {
      outcomes_summary: { compliant: 0, non_compliant: 1, manual_review: 1, missing_data: 0 },
      non_compliant: [engineFinding],
      manual_review_items: [{ ...engineFinding, element_id: "704", severity: "severity_low" }],
      missing_data_items: [],
      proposed_fixes: [],
    };
    const merged = mergeOutcomeFindings(outcomes);
    expect(merged).toHaveLength(2);
    expect(merged[0].bucket).toBe("non_compliant");
    expect(merged[0].severity).toBe("high");
    expect(merged[0].value).toBe("120 min");
    expect(merged[1].bucket).toBe("manual_review");
    expect(merged[1].severity).toBe("low");
    // already-normalized input passes through unchanged
    const clean = mergeOutcomeFindings({
      ...outcomes,
      non_compliant: [finding({ severity: "high", value: "X" })],
      manual_review_items: [],
    });
    expect(clean[0].severity).toBe("high");
    expect(clean[0].value).toBe("X");
  });
});

describe("filterFindings", () => {
  const findings = [
    finding({ element_id: 1, bucket: "non_compliant", severity: "high", parameter: "Fire Rating" }),
    finding({ element_id: 2, bucket: "manual_review", severity: "low", rule_id: "door_width" }),
    finding({ element_id: 3, bucket: "missing_data", severity: "medium", value: "120 Min" }),
  ];

  it("filters by bucket", () => {
    const result = filterFindings(findings, { ...DEFAULT_FINDINGS_FILTER, bucket: "manual_review" });
    expect(result.map((f) => f.element_id)).toEqual([2]);
  });

  it("filters by severity", () => {
    const result = filterFindings(findings, { ...DEFAULT_FINDINGS_FILTER, severity: "high" });
    expect(result.map((f) => f.element_id)).toEqual([1]);
  });

  it("filters by rule id", () => {
    const result = filterFindings(findings, { ...DEFAULT_FINDINGS_FILTER, ruleId: "door_width" });
    expect(result.map((f) => f.element_id)).toEqual([2]);
  });

  it("searches across element id, value, parameter and rule id (case-insensitive)", () => {
    expect(
      filterFindings(findings, { ...DEFAULT_FINDINGS_FILTER, search: "fire rating" }).map(
        (f) => f.element_id,
      ),
    ).toEqual([1]);
    expect(
      filterFindings(findings, { ...DEFAULT_FINDINGS_FILTER, search: "120 min" }).map(
        (f) => f.element_id,
      ),
    ).toEqual([3]);
  });

  it("combines filters with AND semantics", () => {
    const result = filterFindings(findings, {
      bucket: "non_compliant",
      severity: "low",
      ruleId: "all",
      search: "",
    });
    expect(result).toHaveLength(0);
  });
});

describe("sortFindings", () => {
  it("sorts severity desc, then rule id ascending", () => {
    const findings = [
      finding({ element_id: 1, rule_id: "z_rule", severity: "low" }),
      finding({ element_id: 2, rule_id: "a_rule", severity: "high" }),
      finding({ element_id: 3, rule_id: "b_rule", severity: "high" }),
      finding({ element_id: 4, rule_id: "c_rule", severity: "medium" }),
    ];
    const sorted = sortFindings(findings);
    expect(sorted.map((f) => f.element_id)).toEqual([2, 3, 4, 1]);
  });

  it("does not mutate the input array", () => {
    const findings = [finding({ element_id: 1, severity: "low" }), finding({ element_id: 2, severity: "high" })];
    const original = [...findings];
    sortFindings(findings);
    expect(findings).toEqual(original);
  });
});

describe("groupByRule", () => {
  it("counts findings per rule and tracks the worst bucket", () => {
    const findings = [
      finding({ rule_id: "a", bucket: "manual_review" }),
      finding({ rule_id: "a", bucket: "non_compliant" }),
      finding({ rule_id: "b", bucket: "missing_data" }),
    ];
    const groups = groupByRule(findings);
    expect(groups).toEqual([
      { ruleId: "a", count: 2, worstBucket: "non_compliant" },
      { ruleId: "b", count: 1, worstBucket: "missing_data" },
    ]);
  });
});
