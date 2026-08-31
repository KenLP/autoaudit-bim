/**
 * Pure client-side counting/filtering helpers over an already-fetched
 * outcomes.json (B10/B12/B15) — no re-derivation of audit results, no
 * business logic beyond what a spreadsheet filter would do. Kept in a
 * standalone module so it is unit-testable without mounting the table.
 */
import type { Bucket, Finding, OutcomesResponse, Severity } from "@/api/types";

const SEVERITY_ORDER: Record<string, number> = { high: 3, medium: 2, low: 1 };

/** Stable identity for one finding: the (rule, element) pair the engine
 *  produced it from. Lives here rather than beside the table component so that
 *  file exports only components (S-08 — a mixed module silently turns off Fast
 *  Refresh for it in dev, which is a papercut you feel and never diagnose). */
export function findingKey(f: Finding): string {
  return `${f.rule_id}::${f.element_id}`;
}

/** Engine severities arrive as tags ("severity_high", reports.py) — strip the
 *  prefix down to the UI's high/medium/low. Unknown values pass through
 *  lowercased so a future tier degrades to "—" instead of crashing. */
function normalizeSeverity(raw: unknown): string | null {
  const s = String(raw ?? "")
    .toLowerCase()
    .replace(/^severity_/, "");
  return s || null;
}

/** Flatten the outcome buckets into one list. This is the ONE normalization
 *  boundary between raw outcomes.json and the UI (2026-07 review, B1):
 *  - `bucket` comes from WHICH list the finding sits in — the engine
 *    partitioned them (QC 4-bucket) and never writes a `bucket` key, so list
 *    membership is the source of truth, not a per-finding field.
 *  - `severity` is normalized from the engine's "severity_x" tag form.
 *  - `value` falls back to the engine's `current_value` key. */
export function mergeOutcomeFindings(outcomes: OutcomesResponse): Finding[] {
  const seen = new Set<string>();
  const merged: Finding[] = [];
  const lists: Array<[Finding["bucket"], Finding[]]> = [
    ["non_compliant", outcomes.non_compliant ?? []],
    ["manual_review", outcomes.manual_review_items ?? []],
    ["missing_data", outcomes.missing_data_items ?? []],
  ];
  for (const [bucket, list] of lists) {
    for (const raw of list) {
      const f: Finding = {
        ...raw,
        bucket,
        severity: normalizeSeverity(raw.severity),
        value: raw.value ?? (raw.current_value as Finding["value"]) ?? null,
      };
      const key = `${f.rule_id}::${f.element_id}::${f.bucket}`;
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(f);
    }
  }
  return merged;
}

export interface FindingsFilter {
  bucket: Bucket | "all";
  severity: Severity | "all";
  ruleId: string | "all";
  search: string;
}

export const DEFAULT_FINDINGS_FILTER: FindingsFilter = {
  bucket: "all",
  severity: "all",
  ruleId: "all",
  search: "",
};

function severityOf(f: Finding): string {
  return String(f.severity ?? "").toLowerCase();
}

export function filterFindings(
  findings: Finding[],
  filter: FindingsFilter,
): Finding[] {
  const search = filter.search.trim().toLowerCase();
  return findings.filter((f) => {
    if (filter.bucket !== "all" && f.bucket !== filter.bucket) return false;
    if (filter.severity !== "all" && severityOf(f) !== filter.severity) {
      return false;
    }
    if (filter.ruleId !== "all" && f.rule_id !== filter.ruleId) return false;
    if (search) {
      const haystack = [
        f.element_id,
        f.value,
        f.suggested_value,
        f.parameter,
        f.rule_id,
      ]
        .map((v) => String(v ?? "").toLowerCase())
        .join(" ");
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

/** Default sort: severity desc, then rule id. */
export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort((a, b) => {
    const sevDiff = (SEVERITY_ORDER[severityOf(b)] ?? 0) - (SEVERITY_ORDER[severityOf(a)] ?? 0);
    if (sevDiff !== 0) return sevDiff;
    return String(a.rule_id).localeCompare(String(b.rule_id));
  });
}

export interface RuleGroup {
  ruleId: string;
  count: number;
  worstBucket: Bucket;
}

const BUCKET_SEVERITY_RANK: Record<Bucket, number> = {
  non_compliant: 3,
  manual_review: 2,
  missing_data: 1,
  compliant: 0,
};

export function groupByRule(findings: Finding[]): RuleGroup[] {
  const map = new Map<string, RuleGroup>();
  for (const f of findings) {
    const existing = map.get(f.rule_id);
    if (!existing) {
      map.set(f.rule_id, { ruleId: f.rule_id, count: 1, worstBucket: f.bucket });
    } else {
      existing.count += 1;
      if (BUCKET_SEVERITY_RANK[f.bucket] > BUCKET_SEVERITY_RANK[existing.worstBucket]) {
        existing.worstBucket = f.bucket;
      }
    }
  }
  return [...map.values()].sort((a, b) => a.ruleId.localeCompare(b.ruleId));
}
