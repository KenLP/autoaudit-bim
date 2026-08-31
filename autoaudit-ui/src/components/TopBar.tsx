import { Plus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/ThemeToggle";
import { StatusPill, axisStatus } from "@/components/StatusPill";
import { strings } from "@/strings";
import { useHealth } from "@/api/hooks";
import type { AxisName } from "@/api/types";

// LOD/Spatial axes exist (types unchanged) but stay hidden from the topbar
// until those checks are demo-ready (2026-07-12 UI feedback) — see also
// RunPage's pre-flight pills and SetupPage.
const AXES: { key: AxisName; label: string }[] = [
  { key: "revit", label: strings.health.revit },
  { key: "forma", label: strings.health.forma },
];

export function TopBar() {
  const { data: health } = useHealth();
  const navigate = useNavigate();

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-[var(--border)] bg-[var(--surface)] px-4">
      <span className="text-page-title">{strings.app.name}</span>
      <Badge variant="muted">{strings.app.pilotBadge}</Badge>

      <div className="ml-4 hidden items-center gap-3 sm:flex">
        {AXES.map(({ key, label }) => (
          <StatusPill
            key={key}
            name={label}
            status={axisStatus(health?.axes[key])}
          />
        ))}
      </div>

      <div className="ml-auto flex items-center gap-2">
        <Button onClick={() => navigate("/run")}>
          <Plus size={14} />
          {strings.topbar.run}
        </Button>
        <ThemeToggle />
      </div>
    </header>
  );
}
