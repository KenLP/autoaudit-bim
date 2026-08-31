import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

/** Every screen opens with a title + one-sentence description, so a bare
 *  page never reads as empty/undirected (2026-07-12 UI feedback). */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="flex flex-col gap-1">
        <h1 className="text-page-title">{title}</h1>
        {description && <p className="text-caption max-w-xl">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
