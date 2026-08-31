import { describe, expect, it } from "vitest";
// Vite's `?raw` rather than node:fs — this tsconfig types only `vite/client`,
// so a node import type-checks under vitest and then fails `tsc -b` in CI.
import SOURCE from "./App.tsx?raw";

/**
 * S-07 — pins the code-splitting itself, not its effect.
 *
 * Route splitting is one of those fixes that works today and un-works the first
 * time someone adds `import { NewPage } from "@/features/..."` at the top of
 * App.tsx to get a reference for a route. The bundle silently returns to one
 * chunk, nothing fails, and the next person to notice is whoever is presenting.
 *
 * There is no runtime assertion available for it — the split lives in the
 * bundler's output, and the test suite doesn't build. So this reads the source,
 * the same way the Python side pins its own wiring with `inspect.getsource`.
 * It is a blunt instrument and it is the honest one: it fails on exactly the
 * edit that would undo the fix.
 */

const ROUTE_PAGES = [
  "DashboardPage",
  "SetupPage",
  "RunPage",
  "RunsPage",
  "RunDetailPage",
  "LatestResultPage",
  "ApprovalsPage",
  "RulesPage",
  "BuilderPage",
];

describe("App route code-splitting", () => {
  it.each(ROUTE_PAGES)("%s is loaded lazily", (page) => {
    expect(SOURCE).toMatch(new RegExp(`const ${page} = lazy\\(`));
  });

  it("no page is also imported statically", () => {
    // A static import next to the lazy() one pulls the page back into the entry
    // chunk and makes the lazy() decorative.
    const staticImports = SOURCE.match(/^import\s+\{[^}]*\}\s+from\s+"@\/features\/.*$/gm);
    expect(staticImports).toBeNull();
  });

  it("renders behind a Suspense boundary", () => {
    // lazy() without one throws on first navigation rather than degrading.
    expect(SOURCE).toContain("<Suspense");
  });
});
