import { useMemo } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";

/**
 * Renders an already-generated markdown artifact (report.md /
 * verification_report.md) client-side. B12: this component NEVER
 * recomputes any figure in the file — it only converts markdown to
 * sanitized HTML for display.
 */
export function MarkdownView({ markdown }: { markdown: string }) {
  const html = useMemo(() => {
    const raw = marked.parse(markdown, { async: false }) as string;
    return DOMPurify.sanitize(raw);
  }, [markdown]);

  return (
    <div
      className="prose-md max-w-none text-[13px] leading-relaxed [&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-[var(--border)] [&_td]:border [&_td]:border-[var(--border)] [&_th]:p-1.5 [&_td]:p-1.5 [&_code]:font-mono-val [&_pre]:overflow-x-auto [&_pre]:bg-[var(--surface-2)] [&_pre]:p-2 [&_pre]:rounded-[var(--radius)] [&_h1]:text-page-title [&_h2]:text-section-title [&_h3]:font-semibold"
      // Sanitized via DOMPurify immediately above — safe to inject.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
