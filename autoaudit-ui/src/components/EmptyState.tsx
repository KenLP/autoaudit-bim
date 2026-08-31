import type { LucideIcon } from "lucide-react";
import { Inbox } from "lucide-react";
import { Button } from "@/components/ui/button";

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  body?: string;
  actionLabel?: string;
  onAction?: () => void;
}

/** Every empty state carries an action — never a bare "no data" table (visual language #6). */
export function EmptyState({
  icon: Icon = Inbox,
  title,
  body,
  actionLabel,
  onAction,
}: EmptyStateProps) {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-10 text-center">
      <Icon size={28} className="text-[var(--ink-muted)]" />
      <div className="text-section-title">{title}</div>
      {body && <p className="max-w-sm text-[var(--ink-muted)]">{body}</p>}
      {actionLabel && onAction && (
        <Button onClick={onAction}>{actionLabel}</Button>
      )}
    </div>
  );
}
