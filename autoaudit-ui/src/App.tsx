import { lazy, Suspense } from "react";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
  useLocation,
  useParams,
} from "react-router-dom";
import { AppShell } from "@/components/AppShell";

/* S-07: every page used to be a static import, so one 1.03 MB chunk had to be
 * parsed before the first screen appeared — including the chart library only
 * History renders and the rule editor most sessions never open. The app is
 * served from localhost, so this was never about download time: it is parse and
 * evaluate on the presenter's laptop, on the one screen an audience is watching.
 *
 * Routes are the natural seam because the router already owns them. Each page
 * becomes its own chunk, and a dependency used by exactly one page follows it
 * out of the entry bundle without having to be named anywhere. */
const DashboardPage = lazy(() =>
  import("@/features/dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const SetupPage = lazy(() =>
  import("@/features/setup/SetupPage").then((m) => ({ default: m.SetupPage })),
);
const RunPage = lazy(() =>
  import("@/features/runs/RunPage").then((m) => ({ default: m.RunPage })),
);
const RunsPage = lazy(() =>
  import("@/features/runs/RunsPage").then((m) => ({ default: m.RunsPage })),
);
const RunDetailPage = lazy(() =>
  import("@/features/runs/RunDetailPage").then((m) => ({ default: m.RunDetailPage })),
);
const LatestResultPage = lazy(() =>
  import("@/features/runs/LatestResultPage").then((m) => ({ default: m.LatestResultPage })),
);
const ApprovalsPage = lazy(() =>
  import("@/features/approvals/ApprovalsPage").then((m) => ({ default: m.ApprovalsPage })),
);
const RulesPage = lazy(() =>
  import("@/features/rules/RulesPage").then((m) => ({ default: m.RulesPage })),
);
const BuilderPage = lazy(() =>
  import("@/features/builder/BuilderPage").then((m) => ({ default: m.BuilderPage })),
);

/** `/runs/:id` predates the Results restructure (2026-07-12) — keep it
 *  resolving (preserving any query string, e.g. `?tab=verification`) so
 *  existing bookmarks/links don't 404. */
function LegacyRunDetailRedirect() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  return <Navigate to={`/results/${id ?? ""}${location.search}`} replace />;
}

/** Shown while a route chunk loads. Deliberately blank rather than a spinner:
 *  off a local server these resolve in a few milliseconds, and a spinner that
 *  flashes for one frame reads as a glitch. */
function RouteFallback() {
  return <div className="min-h-[50vh]" aria-busy="true" />;
}

const router = createBrowserRouter(
  [
    {
      element: <AppShell />,
      children: [
        { path: "/", element: <DashboardPage /> },
        { path: "/setup", element: <SetupPage /> },
        { path: "/run", element: <RunPage /> },
        // Results = the latest run only; History = all runs + trend.
        { path: "/results", element: <LatestResultPage /> },
        { path: "/results/:id", element: <RunDetailPage /> },
        { path: "/history", element: <RunsPage /> },
        { path: "/approvals", element: <ApprovalsPage /> },
        // M2 (2026-07-12): /rules is the rule-file library, /rule-builder is
        // the single-rule editor (both replace the ComingInM2 placeholder).
        { path: "/rules", element: <RulesPage /> },
        { path: "/rule-builder", element: <BuilderPage /> },

        // Legacy routes — redirect rather than 404 for anything bookmarked
        // before the sidebar restructure.
        { path: "/runs", element: <Navigate to="/history" replace /> },
        { path: "/runs/:id", element: <LegacyRunDetailRedirect /> },
        { path: "/rules/builder", element: <Navigate to="/rule-builder" replace /> },
        { path: "/settings", element: <Navigate to="/setup" replace /> },
      ],
    },
  ],
  { basename: "/ui" },
);

function App() {
  // One boundary around the router rather than one per route: the shell (nav,
  // top bar) stays mounted while a page chunk arrives, so navigating never
  // blanks the frame the user is already looking at.
  return (
    <Suspense fallback={<RouteFallback />}>
      <RouterProvider router={router} />
    </Suspense>
  );
}

export default App;
