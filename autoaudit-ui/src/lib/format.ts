export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
    return "—";
  }
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m === 0) return `${s}s`;
  return `${m}m${String(s).padStart(2, "0")}s`;
}

export function formatCompliancePct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return "—";
  return `${pct.toFixed(1)}%`;
}
