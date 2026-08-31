import { NavLink, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Settings,
  Play,
  History,
  CheckSquare,
  SquarePen,
  ClipboardCheck,
  FileStack,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { strings } from "@/strings";
import { useIsCompact } from "@/lib/useIsCompact";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const ITEMS: Array<{
  to: string;
  icon: typeof LayoutDashboard;
  label: string;
  end?: boolean;
}> = [
  // Workflow order (2026-07-13 UI feedback): define → run → review.
  // Setup stays at #2 (ACC/Forma project can change per model).
  { to: "/", icon: LayoutDashboard, label: strings.nav.overview, end: true },
  { to: "/setup", icon: Settings, label: strings.nav.setup },
  { to: "/rules", icon: FileStack, label: strings.nav.rules },
  { to: "/rule-builder", icon: SquarePen, label: strings.nav.ruleBuilder },
  { to: "/run", icon: Play, label: strings.nav.run },
  // Results = latest run only; History = all runs + trend.
  // No `end`: /results/:id (a run detail) is still a "result" → keep it lit.
  { to: "/results", icon: ClipboardCheck, label: strings.nav.results },
  { to: "/approvals", icon: CheckSquare, label: strings.nav.approvals },
  { to: "/history", icon: History, label: strings.nav.history },
];

/** Streamlit-familiar labeled sidebar (B — 2026-07-12 UI feedback): icon +
 *  text at >=720px, collapses to the old icon-only 48px rail (with tooltip)
 *  below that — reuses the same B13 compact breakpoint as everything else. */
export function Sidebar({ pendingApprovals = 0 }: { pendingApprovals?: number }) {
  // NOTE: className must be a plain STRING here. Radix TooltipTrigger
  // (asChild → Slot) merges its own className with the child's via string
  // concat — NavLink's function-form className gets .toString()'d into a
  // garbage class list (killing `relative`, so the badge anchored to the
  // viewport — caught live at 380px). Compute isActive ourselves instead.
  const { pathname } = useLocation();
  const compact = useIsCompact();

  return (
    <nav
      className={cn(
        "flex shrink-0 flex-col gap-0.5 border-r border-[var(--border)] bg-[var(--surface)] py-2",
        compact ? "w-12 items-center px-1" : "w-[184px] px-2",
      )}
    >
      {ITEMS.map(({ to, icon: Icon, label, end }) => {
        const isActive = end ? pathname === to : pathname.startsWith(to);
        const badge = to === "/approvals" && pendingApprovals > 0 && (
          <span
            className={cn(
              "flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--fail)] px-1 text-[10px] font-semibold text-white",
              compact ? "absolute -right-0.5 -top-0.5" : "ml-auto",
            )}
            data-testid="approvals-badge"
          >
            {pendingApprovals}
          </span>
        );
        const link = (
          <NavLink
            to={to}
            end={end}
            className={cn(
              "relative flex items-center gap-2 rounded-[var(--radius)] text-[var(--ink-muted)] hover:bg-[var(--surface-2)]",
              compact ? "h-9 w-9 justify-center" : "h-9 px-2.5",
              isActive && "bg-[var(--surface-2)] text-[var(--primary)]",
            )}
            aria-label={label}
          >
            <Icon size={18} className="shrink-0" />
            {!compact && <span className="truncate text-[13px] font-medium">{label}</span>}
            {badge}
          </NavLink>
        );
        if (!compact) {
          return <div key={to}>{link}</div>;
        }
        return (
          <Tooltip key={to}>
            <TooltipTrigger asChild>{link}</TooltipTrigger>
            <TooltipContent side="right">{label}</TooltipContent>
          </Tooltip>
        );
      })}
    </nav>
  );
}
