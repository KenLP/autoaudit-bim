import { AlertTriangle } from "lucide-react";
import { ApiError } from "@/api/client";
import { strings } from "@/strings";

export function ApiErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const detail = error instanceof ApiError ? error.detail : strings.common.error;
  const isConflict = error instanceof ApiError && error.status === 409;
  return (
    <div
      role="alert"
      className="card flex items-start gap-2 border-[var(--fail)] px-3 py-2 text-[var(--fail)]"
      style={{ background: "color-mix(in srgb, var(--fail) 8%, transparent)" }}
    >
      <AlertTriangle size={16} className="mt-0.5 shrink-0" />
      <span>{isConflict ? strings.run.conflictBanner : detail}</span>
    </div>
  );
}
