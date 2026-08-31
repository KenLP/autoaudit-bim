import { cn } from "@/lib/utils";
import { strings } from "@/strings";
import type { RuleGroup } from "@/lib/findings";
import type { Bucket } from "@/api/types";

const BUCKET_COLOR: Record<Bucket, string> = {
  compliant: "var(--bucket-compliant)",
  non_compliant: "var(--bucket-non-compliant)",
  manual_review: "var(--bucket-manual-review)",
  missing_data: "var(--bucket-missing-data)",
};

export interface RuleSidebarProps {
  groups: RuleGroup[];
  selected: string | "all";
  onSelect: (ruleId: string | "all") => void;
}

export function RuleSidebar({ groups, selected, onSelect }: RuleSidebarProps) {
  return (
    <div className="card flex flex-col overflow-y-auto p-1">
      <button
        onClick={() => onSelect("all")}
        className={cn(
          "flex items-center justify-between rounded-[var(--radius)] px-2 py-1.5 text-left text-[13px] hover:bg-[var(--surface-2)]",
          selected === "all" && "bg-[var(--surface-2)]",
        )}
      >
        <span>{strings.runDetail.allRules}</span>
      </button>
      {groups.map((g) => (
        <button
          key={g.ruleId}
          onClick={() => onSelect(g.ruleId)}
          className={cn(
            "flex items-center gap-2 rounded-[var(--radius)] px-2 py-1.5 text-left text-[13px] hover:bg-[var(--surface-2)]",
            selected === g.ruleId && "bg-[var(--surface-2)]",
          )}
        >
          <span
            aria-hidden
            className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: BUCKET_COLOR[g.worstBucket] }}
          />
          <span className="flex-1 truncate font-mono-val">{g.ruleId}</span>
          <span className="text-caption">{g.count}</span>
        </button>
      ))}
    </div>
  );
}
