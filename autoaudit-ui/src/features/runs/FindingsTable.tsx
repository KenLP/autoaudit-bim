import { useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MonoText } from "@/components/MonoText";
import { BucketBadge } from "@/components/BucketBadge";
import { SeverityBadge } from "@/components/SeverityBadge";
import { strings } from "@/strings";
import { cn } from "@/lib/utils";
import {
  DEFAULT_FINDINGS_FILTER,
  filterFindings,
  sortFindings,
  findingKey,
  type FindingsFilter,
} from "@/lib/findings";
import type { Finding } from "@/api/types";

const ROW_HEIGHT = 34;
const VIRTUALIZE_THRESHOLD = 200;

export interface FindingsTableProps {
  findings: Finding[];
  showRuleColumn?: boolean;
  compact?: boolean;
  selectedKey?: string | null;
  onSelect: (finding: Finding) => void;
  checkedKeys: Set<string>;
  onToggleChecked: (key: string, checked: boolean) => void;
}

export function FindingsTable({
  findings,
  showRuleColumn = true,
  compact = false,
  selectedKey,
  onSelect,
  checkedKeys,
  onToggleChecked,
}: FindingsTableProps) {
  const [filter, setFilter] = useState<FindingsFilter>(DEFAULT_FINDINGS_FILTER);
  const parentRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(
    () => sortFindings(filterFindings(findings, filter)),
    [findings, filter],
  );

  const shouldVirtualize = rows.length > VIRTUALIZE_THRESHOLD;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  const virtualItems = virtualizer.getVirtualItems();
  const padTop = virtualItems.length ? virtualItems[0].start : 0;
  const padBottom = virtualItems.length
    ? virtualizer.getTotalSize() - virtualItems[virtualItems.length - 1].end
    : 0;

  // Every rendered cell, including the checkbox column the old count forgot —
  // it drives the spacer rows' colSpan, so an off-by-one silently narrows the
  // spacer and the scroll height stops matching the data.
  const columnCount = compact ? 4 : showRuleColumn ? 8 : 7;

  function renderRow(f: Finding, index: number) {
    const key = findingKey(f);
    const isSelected = selectedKey === key;
    const zebra = index % 2 === 1;
    return (
      <tr
        key={key}
        onClick={() => onSelect(f)}
        className={cn(
          "h-[34px] cursor-pointer border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]",
          (zebra || isSelected) && "bg-[var(--surface-2)]",
        )}
        data-testid="findings-row"
      >
        <td className="w-8 px-2" onClick={(e) => e.stopPropagation()}>
          <Checkbox
            checked={checkedKeys.has(key)}
            onCheckedChange={(v) => onToggleChecked(key, !!v)}
          />
        </td>
        <td className="px-2">
          <MonoText>{f.element_id}</MonoText>
        </td>
        {!compact && <td className="px-2">{f.parameter ?? "—"}</td>}
        {!compact && (
          <td className="px-2 font-mono-val">{f.value === "" || f.value == null ? "(empty)" : f.value}</td>
        )}
        {!compact && (
          <td className="px-2 font-mono-val">{f.suggested_value ?? "—"}</td>
        )}
        <td className="px-2">
          <BucketBadge bucket={f.bucket} />
        </td>
        <td className="px-2">
          <SeverityBadge severity={f.severity} />
        </td>
        {!compact && showRuleColumn && (
          <td className="px-2 font-mono-val">{f.rule_id}</td>
        )}
      </tr>
    );
  }

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={filter.bucket}
          onValueChange={(v) => setFilter((f) => ({ ...f, bucket: v as FindingsFilter["bucket"] }))}
        >
          <SelectTrigger className="w-40">
            <SelectValue placeholder={strings.runDetail.filterBucket} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{strings.runDetail.filterBucket}</SelectItem>
            <SelectItem value="non_compliant">{strings.bucket.non_compliant}</SelectItem>
            <SelectItem value="manual_review">{strings.bucket.manual_review}</SelectItem>
            <SelectItem value="missing_data">{strings.bucket.missing_data}</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={filter.severity}
          onValueChange={(v) => setFilter((f) => ({ ...f, severity: v as FindingsFilter["severity"] }))}
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder={strings.runDetail.filterSeverity} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{strings.runDetail.filterSeverity}</SelectItem>
            <SelectItem value="high">{strings.severity.high}</SelectItem>
            <SelectItem value="medium">{strings.severity.medium}</SelectItem>
            <SelectItem value="low">{strings.severity.low}</SelectItem>
          </SelectContent>
        </Select>
        <Input
          className="w-48"
          placeholder={strings.runDetail.filterSearch}
          value={filter.search}
          onChange={(e) => setFilter((f) => ({ ...f, search: e.target.value }))}
        />
      </div>

      <div ref={parentRef} className="flex-1 overflow-auto rounded-[var(--radius)] border border-[var(--border)]">
        {/* Virtualization uses SPACER ROWS, and the reason is a scar.
            FE-9 replaced a nested-<table>-per-row hack with absolutely
            positioned <tr>s inside a `display:block; position:relative`
            <tbody>. That swapped one >200-findings bug for a worse one: with
            every row taken out of flow, the block tbody had no in-flow
            content left and collapsed to the 32px checkbox column, so each
            row's `width:100%` resolved to 32px and `table-fixed` handed the
            remaining columns 0px — element id, bucket badge and severity all
            painted on top of each other at the same x. Measured live
            2026-07-31 (tbody 32px vs table 1152px, cells 0px) in a plain
            browser at 1400px, so this was never panel- or width-specific;
            it fired on every run with >200 findings and nothing else.
            Spacer rows keep ONE table doing all the column arithmetic: the
            header and the body are the same layout box, so they cannot drift
            no matter the width or engine. Rows stay ordinary <tr>s. */}
        <table
          className={cn("w-full text-left text-[13px]", shouldVirtualize && "table-fixed")}
        >
          <thead className="sticky top-0 z-10 bg-[var(--surface)]">
            <tr className="h-8 border-b border-[var(--border)] text-caption">
              <th className="w-8 px-2" />
              <th className="px-2">{strings.runDetail.columnElement}</th>
              {!compact && <th className="px-2">{strings.runDetail.columnParameter}</th>}
              {!compact && <th className="px-2">{strings.runDetail.columnValue}</th>}
              {!compact && <th className="px-2">{strings.runDetail.columnSuggested}</th>}
              <th className="px-2">{strings.runDetail.columnBucket}</th>
              <th className="px-2">{strings.runDetail.columnSeverity}</th>
              {!compact && showRuleColumn && <th className="px-2">{strings.runDetail.columnRule}</th>}
            </tr>
          </thead>
          {shouldVirtualize ? (
            <tbody>
              {padTop > 0 && (
                <tr aria-hidden="true" data-testid="findings-spacer-top">
                  <td colSpan={columnCount} style={{ height: padTop, padding: 0, border: 0 }} />
                </tr>
              )}
              {virtualItems.map((vi) => renderRow(rows[vi.index], vi.index))}
              {padBottom > 0 && (
                <tr aria-hidden="true" data-testid="findings-spacer-bottom">
                  <td colSpan={columnCount} style={{ height: padBottom, padding: 0, border: 0 }} />
                </tr>
              )}
            </tbody>
          ) : (
            <tbody>
              {rows.map((f, i) => renderRow(f, i))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={columnCount} className="p-4 text-center text-[var(--ink-muted)]">
                    —
                  </td>
                </tr>
              )}
            </tbody>
          )}
        </table>
      </div>
    </div>
  );
}
