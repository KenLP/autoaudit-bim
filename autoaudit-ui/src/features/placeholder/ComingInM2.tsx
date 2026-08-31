import { strings } from "@/strings";

/** M1 nav items for M2 screens (Rules/Builder/Settings) point here. */
export function ComingInM2() {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="card flex max-w-md flex-col items-center gap-2 px-6 py-10 text-center">
        <div className="text-section-title">{strings.comingInM2.title}</div>
        <p className="text-[var(--ink-muted)]">{strings.comingInM2.body}</p>
        <a
          href="http://127.0.0.1:8501"
          target="_blank"
          rel="noreferrer"
          className="text-[13px] text-[var(--primary)] underline"
        >
          {strings.comingInM2.link}
        </a>
      </div>
    </div>
  );
}
