# AutoAudit UI

React SPA for AutoAudit (Phase 3b M1). Served by the AuditHub service
(`bim-orchestrator`, FastAPI, :8601) at `/ui`; consumed inside a Revit
dockable WebView2 panel or a plain browser window. See
`docs/ARCHITECTURE.md` for the full design.

## Develop

Run the AuditHub service separately (`uv run bim-orchestrator ...` /
`autoaudit-service`, listening on :8601), then:

```bash
npm install
npm run dev
```

`vite.config.ts` proxies `/api/*` to `http://127.0.0.1:8601`, so the app
works against a live service without a build step. Base path is `/ui/`
(matches the service mount) — visit `http://localhost:5173/ui/`.

## Build

```bash
npm run build
```

Or from `bim-orchestrator/`: `powershell scripts/build-ui.ps1`. Output is
`dist/` (gitignored — regenerated on demand, never committed). The
service serves it directly; if `dist/` doesn't exist, `/ui` returns a 503
with build instructions instead of a stack trace.

## Test

```bash
npm test        # vitest run
npx tsc --noEmit
```

## Structure

```
src/
  strings.ts        # EVERY user-facing string (EN-only, B6) — no hard-coded text in components
  theme/tokens.css   # design tokens (colors, spacing, type scale) — copied verbatim from the spec
  api/               # client.ts (fetch wrapper + ApiError), types.ts (hand-written, mirrors
                      # the service's pydantic shapes), hooks.ts (TanStack Query), sse.ts (live run events)
  lib/               # pure helpers only — csv export, date/duration formatting, findings
                      # filter/sort/group (client-side only, B10/B12/B15: never re-derives audit results)
  components/        # shared shell + primitives (ui/ = hand-authored Radix-based components)
  features/          # one folder per screen area (dashboard, runs, approvals; rules/builder/
                      # settings are M2 placeholders in M1)
```

## Conventions (do not relearn these)

- **No business logic in TypeScript.** Every number the UI shows is either
  read verbatim from a service response or counted/filtered client-side
  over data the service already computed (`lib/findings.ts`). If you find
  yourself recomputing a compliance percentage or re-evaluating a rule,
  that logic belongs in `bim-orchestrator/src/bim_orchestrator/`, not here.
- **All strings go through `strings.ts`.** No literal user-facing text in
  JSX — the module is the single place i18n or copy edits will ever touch.
- **Confirm dialogs on every write action**; disabled buttons + tooltip
  when the relevant health pill (Revit/Forma/LOD/Spatial) isn't green;
  empty states always carry an action, never a bare table.
- **Node is a build-time dependency only.** Nothing at runtime fetches
  from a CDN — fonts are bundled via `@fontsource*` packages, icons via
  `lucide-react`. The deployed artifact is static files under `dist/`.
