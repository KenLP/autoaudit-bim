import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { Download } from "lucide-react";
import { StatTile } from "@/components/StatTile";
import { axisStatus } from "@/components/StatusPill";
import { Button } from "@/components/ui/button";
import { MonoText } from "@/components/MonoText";
import { ApiErrorBanner } from "@/components/ApiErrorBanner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { strings } from "@/strings";
import { useHealth, useOutcomes, useRun } from "@/api/hooks";
import { useHighlight } from "@/api/hooks";
import { useIsCompact } from "@/lib/useIsCompact";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";
import { downloadCsv, toCsv } from "@/lib/csv";
import {
  DEFAULT_FINDINGS_FILTER,
  filterFindings,
  groupByRule,
  mergeOutcomeFindings,
  sortFindings,
} from "@/lib/findings";
import { FindingsTable } from "./FindingsTable";
import { findingKey } from "@/lib/findings";
import { RuleSidebar } from "./RuleSidebar";
import { FindingDetail } from "./FindingDetail";
import { ArtifactTabs } from "./ArtifactTabs";
import type { Bucket, Finding } from "@/api/types";
import { toast } from "sonner";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

/** Results detail for a single (finished) run. Live audits no longer render
 *  here — RunPage owns the live SSE view and navigates here once the job
 *  reports "done" (2026-07-12 restructure). */
export function RunDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const isCompact = useIsCompact();

  const { data: run, error: runError } = useRun(id);
  const { data: outcomes, error: outcomesError } = useOutcomes(id);
  const { data: health } = useHealth();
  const highlight = useHighlight();

  const [ruleFilter, setRuleFilter] = useState<string | "all">("all");
  const [statBucketFilter, setStatBucketFilter] = useState<Bucket | "all">("all");
  const [selected, setSelected] = useState<Finding | null>(null);
  const [checkedKeys, setCheckedKeys] = useState<Set<string>>(new Set());
  const [confirmHighlightOpen, setConfirmHighlightOpen] = useState(false);

  const allFindings = useMemo(
    () => (outcomes ? mergeOutcomeFindings(outcomes) : []),
    [outcomes],
  );
  const ruleGroups = useMemo(() => groupByRule(allFindings), [allFindings]);

  const scoped = useMemo(() => {
    let list = allFindings;
    if (ruleFilter !== "all") list = list.filter((f) => f.rule_id === ruleFilter);
    if (statBucketFilter !== "all") {
      list = filterFindings(list, { ...DEFAULT_FINDINGS_FILTER, bucket: statBucketFilter });
    }
    return list;
  }, [allFindings, ruleFilter, statBucketFilter]);

  const revitStatus = axisStatus(health?.axes.revit);

  function handleExportCsv() {
    const rows = sortFindings(scoped).map((f) => ({
      element_id: f.element_id,
      parameter: f.parameter ?? "",
      value: f.value ?? "",
      suggested_value: f.suggested_value ?? "",
      bucket: f.bucket,
      severity: f.severity ?? "",
      rule_id: f.rule_id,
    }));
    const csv = toCsv(rows, [
      "element_id",
      "parameter",
      "value",
      "suggested_value",
      "bucket",
      "severity",
      "rule_id",
    ]);
    downloadCsv(`${id}-findings.csv`, csv);
  }

  function handleConfirmHighlight() {
    const ids = [...checkedKeys]
      .map((k) => allFindings.find((f) => findingKey(f) === k)?.element_id)
      .filter((v): v is number | string => v !== undefined);
    // One element: frame it WHERE THE USER IS (their 3D view) — jumping to a
    // plan for a single finding was the "why doesn't it use my open 3D view"
    // complaint. A multi-element set keeps the per-level plan walk: one view
    // per level is the point of that feature.
    highlight.mutate(
      { elementIds: ids, perLevel: ids.length > 1 ? undefined : false },
      {
        onSuccess: (res) => toast.success(`Selected ${res.selected} element(s) in Revit`),
        onError: (err) => toast.error(String(err)),
      },
    );
    setConfirmHighlightOpen(false);
  }

  if (runError || outcomesError) {
    return (
      <div className="p-4">
        <ApiErrorBanner error={runError ?? outcomesError} />
      </div>
    );
  }
  if (!run || !outcomes || !id) {
    return <div className="p-4 text-[var(--ink-muted)]">{strings.common.loading}</div>;
  }

  const summary = run.metadata.outcomes_summary;

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <MonoText className="text-section-title">{run.metadata.run_id}</MonoText>
        <span className="text-caption">
          {run.metadata.mode} · {run.metadata.status} · {formatDateTime(run.metadata.finished_at)} ·{" "}
          {strings.runDetail.iterations(run.metadata.iterations ?? 0)}
        </span>
      </div>

      <div className="flex flex-wrap gap-2">
        <StatTile
          label={strings.bucket.compliant}
          value={summary?.compliant ?? "—"}
          color="var(--bucket-compliant)"
          active={statBucketFilter === "compliant"}
          onClick={() => setStatBucketFilter((v) => (v === "compliant" ? "all" : "compliant"))}
        />
        <StatTile
          label={strings.bucket.non_compliant}
          // Merged count (QC + geometry findings), NOT outcomes_summary: the
          // geometry bucket is deliberately outside QC's summary (K7), so a
          // geometry run showed "0" here above a table listing 25 findings.
          value={run.metadata.non_compliant_count ?? summary?.non_compliant ?? "—"}
          color="var(--bucket-non-compliant)"
          active={statBucketFilter === "non_compliant"}
          onClick={() => setStatBucketFilter((v) => (v === "non_compliant" ? "all" : "non_compliant"))}
        />
        <StatTile
          label={strings.bucket.manual_review}
          value={summary?.manual_review ?? "—"}
          color="var(--bucket-manual-review)"
          active={statBucketFilter === "manual_review"}
          onClick={() => setStatBucketFilter((v) => (v === "manual_review" ? "all" : "manual_review"))}
        />
        <StatTile
          label={strings.bucket.missing_data}
          value={summary?.missing_data ?? "—"}
          color="var(--bucket-missing-data)"
          active={statBucketFilter === "missing_data"}
          onClick={() => setStatBucketFilter((v) => (v === "missing_data" ? "all" : "missing_data"))}
        />
      </div>

      <div
        className={
          isCompact
            ? "flex flex-col gap-3"
            : "grid flex-1 grid-cols-[200px,1fr,320px] gap-3 overflow-hidden"
        }
      >
        {!isCompact && (
          <RuleSidebar groups={ruleGroups} selected={ruleFilter} onSelect={setRuleFilter} />
        )}
        {isCompact && (
          <select
            className="h-8 rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface)] px-2 text-[13px]"
            value={ruleFilter}
            onChange={(e) => setRuleFilter(e.target.value)}
          >
            <option value="all">{strings.runDetail.allRules}</option>
            {ruleGroups.map((g) => (
              <option key={g.ruleId} value={g.ruleId}>
                {g.ruleId} ({g.count})
              </option>
            ))}
          </select>
        )}

        {/* The compact branch has no height-bounded ancestor (desktop gets one
            from `grid flex-1 … overflow-hidden`), so this box used to grow to
            the full height of the findings list. FindingsTable's own
            `overflow-auto` div then never scrolled — and since the virtualizer
            watches THAT div, it never saw a scroll event and kept rendering
            the same first ~19 rows above ~11,000px of reserved blank space.
            Measured live 2026-07-31 in the Revit panel width (client 11150 =
            scroll 11150). Bounding the height here restores the invariant the
            virtualizer relies on: the table's own div is the scroller. */}
        <div
          className={cn(
            "flex min-h-[300px] flex-col gap-2 overflow-hidden",
            isCompact && "max-h-[70vh]",
          )}
        >
          <div className="flex justify-end gap-2">
            {checkedKeys.size > 0 && (
              <Button variant="outline" size="sm" onClick={() => setConfirmHighlightOpen(true)}>
                {strings.runDetail.highlight} ({checkedKeys.size})
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={handleExportCsv}>
              <Download size={14} />
              {strings.runDetail.exportCsv}
            </Button>
          </div>
          <FindingsTable
            findings={scoped}
            showRuleColumn={ruleFilter === "all"}
            compact={isCompact}
            selectedKey={selected ? findingKey(selected) : null}
            onSelect={setSelected}
            checkedKeys={checkedKeys}
            onToggleChecked={(key, checked) =>
              setCheckedKeys((prev) => {
                const next = new Set(prev);
                if (checked) next.add(key);
                else next.delete(key);
                return next;
              })
            }
          />
        </div>

        {!isCompact && (
          <FindingDetail
            finding={selected}
            revitStatus={revitStatus}
            onHighlight={(f) => {
              setCheckedKeys(new Set([findingKey(f)]));
              setConfirmHighlightOpen(true);
            }}
          />
        )}
      </div>

      <ArtifactTabs
        runId={id}
        artifacts={run.artifacts}
        revitStatus={revitStatus}
        initialTab={searchParams.get("tab") ?? undefined}
      />

      {isCompact && (
        <Sheet open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
          <SheetContent>
            <SheetTitle>{strings.runDetail.detailTitle}</SheetTitle>
            <FindingDetail
              finding={selected}
              revitStatus={revitStatus}
              onHighlight={(f) => {
                setCheckedKeys(new Set([findingKey(f)]));
                setConfirmHighlightOpen(true);
              }}
            />
          </SheetContent>
        </Sheet>
      )}

      <ConfirmDialog
        open={confirmHighlightOpen}
        onOpenChange={setConfirmHighlightOpen}
        title={strings.runDetail.confirmHighlightTitle}
        description={strings.runDetail.confirmHighlightBody(checkedKeys.size)}
        loading={highlight.isPending}
        onConfirm={handleConfirmHighlight}
      />
    </div>
  );
}
