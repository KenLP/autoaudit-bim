import { Badge } from "@/components/ui/badge";
import { strings } from "@/strings";

const COLOR: Record<string, string> = {
  high: "var(--sev-high)",
  medium: "var(--sev-medium)",
  low: "var(--sev-low)",
};

export function SeverityBadge({ severity }: { severity?: string | null }) {
  const key = (severity ?? "").toLowerCase();
  const label = strings.severity[key as keyof typeof strings.severity];
  if (!label) return <span className="text-[var(--ink-muted)]">—</span>;
  return (
    <Badge variant="outline" color={COLOR[key]}>
      {label}
    </Badge>
  );
}
