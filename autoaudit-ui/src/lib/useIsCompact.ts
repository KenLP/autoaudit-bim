import { useEffect, useState } from "react";

const QUERY = "(max-width: 720px)";

/** B13 compact-mode breakpoint: <720px. */
export function useIsCompact(): boolean {
  const [compact, setCompact] = useState(
    () => window.matchMedia?.(QUERY).matches ?? false,
  );

  useEffect(() => {
    const mql = window.matchMedia(QUERY);
    const handler = (e: MediaQueryListEvent) => setCompact(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);

  return compact;
}
