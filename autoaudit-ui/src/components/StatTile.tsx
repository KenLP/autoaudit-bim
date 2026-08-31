import { cn } from "@/lib/utils";

export interface StatTileProps {
  label: string;
  value: number | string;
  color?: string;
  active?: boolean;
  onClick?: () => void;
}

export function StatTile({ label, value, color, active, onClick }: StatTileProps) {
  const Comp = onClick ? "button" : "div";
  return (
    <Comp
      onClick={onClick}
      // Left accent stripe carries the bucket color (2026-07-12 polish pass);
      // set via inline style, not a border-l-* utility, so it always wins
      // over .card's own `border: 1px solid var(--border)` shorthand
      // regardless of CSS source order.
      style={{ borderLeftWidth: 3, borderLeftColor: color ?? "var(--border)" }}
      className={cn(
        "card flex min-w-[110px] flex-col gap-1 py-2 pl-3 pr-4 text-left",
        onClick && "cursor-pointer hover:bg-[var(--surface-2)]",
        active && "ring-2 ring-[var(--focus-ring)]",
      )}
    >
      <span className="text-caption uppercase tracking-wide">{label}</span>
      <span
        className="font-mono-val text-[24px] font-semibold leading-none"
        style={{ color: color ?? "var(--ink)" }}
      >
        {value}
      </span>
    </Comp>
  );
}
