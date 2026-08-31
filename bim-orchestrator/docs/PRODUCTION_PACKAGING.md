# Production Packaging — portable, no-Node deployment

> Goal: a teammate clones the repo,
> runs one fetch command, fills credentials, and the whole pipeline works —
> **no Node.js install, no path surgery, no manual builds.**
>
> **Addendum:** when the Forma MCP server gains tools (e.g. the issue
> API: `issues_list/get/update/add_comment`) the SEA exe must be rebuilt +
> re-fetched, and the Revit addin must ship the HTTP `batch` fix.

This document is the single source of truth for how `bim-orchestrator` and its
two MCP servers are packaged and distributed for production / demo use. For the
runtime architecture (agents, graph, rules) see
[`../../docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md).

---

## 1. The two transports — both Node-free at the host

Historically both MCP servers were Node.js stdio bridges the orchestrator spawned
as subprocesses. That required Node.js on every machine. The G-series removed
that dependency on both sides:

| Server | Old transport | New transport (G-series) | Node.js needed? |
|---|---|---|---|
| **RevitMCPServer** | Node `revit-mcp-server` stdio bridge → C# addin | `RevitHTTPClient` → C# addin REST API at `127.0.0.1:{port}` directly (httpx) | **No** (addin loads in Revit) |
| **acc-forma-mcp-server** | `node dist/index.js` stdio | `forma-mcp.exe` — SEA standalone (Node 20 runtime bundled) | **No** (bundled in exe) |

Both are auto-selected at runtime; the orchestrator code paths are unchanged.

### 1A — Revit: HTTP-direct (`mcp_clients/revit.py`)

`make_revit_client()` returns a `RevitHTTPClient` when `REVIT_MCP_USE_HTTP=true`
or when no Node bridge is configured (no `vendor/revit-mcp`, no
`REVIT_MCP_SERVER_CWD`). It calls the addin's `POST /mcp` and `GET /health`
directly. Port = `7891 + max(0, year - 2026)`; auth token auto-read from
`%APPDATA%\Autodesk\Revit\Addins\{version}\revit-mcp-token.txt`.

**Addin v0.7+ compat (G2):** the addin's `StatusForResult()` returns HTTP
4xx/5xx on errors (instead of `200 + ok:false`). `call_envelope()` therefore
**parses the JSON body first**, inspects the `ok` field, and only falls back to
`raise_for_status()` when the body is not valid JSON. This keeps the structured
`RevitEnvelopeError` (code + message) that all agents expect.

### 1B — Forma: SEA standalone executable

`acc-forma-mcp-server` is packaged into `forma-mcp.exe` (~43 MB) with
[`@yao-pkg/pkg`](https://github.com/yao-pkg/pkg). The exe bundles the Node 20
runtime + all JS deps; only `better-sqlite3` (native `.node` addon) is embedded
as a pkg asset and extracted to a temp path at runtime.

`FormaMCPConfig.from_env()` prefers `vendor/forma-mcp/forma-mcp.exe` over
`node dist/index.js` when no `FORMA_MCP_SERVER_CWD` is set. The exe's parent
directory becomes the subprocess `cwd` so dotenv finds `.env` there — the
orchestrator process never sees the APS/SSA secrets.

---

## 2. Build pipeline (in `acc-forma-mcp-server`)

```
src/index.ts
   │  tsup --config tsup.sea.config.ts   (CJS, noExternal: [/.*/] inlines all JS deps;
   │                                       better-sqlite3 stays external for pkg)
   ▼
dist-cjs/index.cjs  (~2.3 MB bundled)
   │  pkg --targets node20-win-x64 --output forma-mcp.exe
   │   (pkg `assets` glob embeds better-sqlite3/build/Release/*.node)
   ▼
forma-mcp.exe  (~43 MB, Node 20 runtime + bundle)
```

| File | Role |
|---|---|
| `tsup.sea.config.ts` | CJS build profile. `noExternal: [/.*/]` bundles node_modules; `external: ['better-sqlite3']` leaves the native addon for pkg. |
| `package.json` → `pkg.assets` | Globs `better-sqlite3/build/Release/*.node` so pkg embeds the native binding. |
| `scripts/sea-copy.mjs` | Copies the built exe straight into `../autoaudit-bim/bim-orchestrator/vendor/forma-mcp/` (local dev convenience). |
| `scripts/sea-publish.mjs` | Idempotent publish to GitHub Release (see §3). |

**npm scripts:**

```bash
npm run sea:build         # tsup CJS + pkg → forma-mcp.exe (Windows x64)
npm run sea:build:linux   # same → forma-mcp-linux (UNTESTED — Windows is the demo target)
npm run sea:copy          # sea:build + copy exe into bim-orchestrator/vendor/forma-mcp/
npm run sea:publish       # upload exe to the GitHub Release (needs `gh auth login`)
```

---

## 3. Distribution — GitHub Release, not git

The 43 MB exe is a **build artifact**, so it is never committed. It rides a
**rolling GitHub Release** (tag `forma-mcp-sea` on `KenLP/acc-forma-mcp-server`).
This keeps both repos' history clean and avoids LFS quota.

```
┌─ acc-forma-mcp-server ────────────┐        ┌─ bim-orchestrator ──────────────┐
│  npm run sea:build                │        │  pwsh scripts/fetch-forma-mcp.ps1│
│  npm run sea:publish              │  gh    │   (gh release download)          │
│   gh release upload --clobber ────┼──────► │   → vendor/forma-mcp/forma-mcp.exe│
│   tag: forma-mcp-sea              │ Release│                                  │
└───────────────────────────────────┘        └──────────────────────────────────┘
```

`sea:publish` uses the **GitHub CLI** to upload. Fetching does not: the release
is public, so `fetch-forma-mcp.ps1` downloads over plain HTTPS and verifies the
asset's published SHA-256 before installing it. It falls back to `gh` only for a
private fork, using the user's existing `gh auth login` — no manual
tokens.

**Publish (server maintainer):**
```bash
cd acc-forma-mcp-server
npm run sea:build
npm run sea:publish        # creates the rolling release on first run, then re-uploads --clobber
```

**Fetch (any consumer machine):**
```powershell
cd bim-orchestrator
& scripts\fetch-forma-mcp.ps1      # downloads vendor/forma-mcp/forma-mcp.exe
                                   # (public release; no gh, no auth)
```

### `.gitignore` layout

| Repo | Ignored | Tracked |
|---|---|---|
| acc-forma-mcp-server | `dist-cjs/`, `forma-mcp.exe`, `forma-mcp-linux` | `tsup.sea.config.ts`, `scripts/sea-*.mjs` |
| bim-orchestrator | `vendor/**/*.exe`, `vendor/forma-mcp/.env` | `vendor/forma-mcp/.env.example`, `scripts/fetch-forma-mcp.ps1` |

---

## 4. Credentials

The exe reads `vendor/forma-mcp/.env` via dotenv at startup. A blank-slate
clone has only the tracked `.env.example` template — running the exe without a
filled `.env` fails closed with `Invalid environment configuration:
APS_CLIENT_ID: Required` (correct, expected behaviour).

Two ways to fill it:

1. **Setup tab wizard** (recommended) — the Streamlit "🔑 Forma / APS
   credentials" expander writes `APS_*` / `SSA_*` into `vendor/forma-mcp/.env`.
2. **Manual** — `copy vendor\forma-mcp\.env.example vendor\forma-mcp\.env` and
   fill `APS_CLIENT_ID`, `APS_CLIENT_SECRET`, `SSA_ID`, `SSA_KEY_ID`,
   `SSA_KEY_PATH`.

Secrets live only in `vendor/forma-mcp/.env` (gitignored) — never in the
orchestrator's own `.env`, never passed through the orchestrator process env.

---

## 5. New-machine checklist

```powershell
git clone git@github.com:KenLP/bim-orchestrator.git
cd bim-orchestrator
uv sync --extra dev
& scripts\fetch-forma-mcp.ps1                 # pull forma-mcp.exe from the Release
copy .env.example .env                          # ACC demo IDs (Revit uses HTTP-direct, no path)
copy vendor\forma-mcp\.env.example vendor\forma-mcp\.env   # then fill APS/SSA creds
uv run pytest -q                                # expect 684 passing
uv run streamlit run streamlit_app\app.py       # Setup tab → Test connection
```

No Node.js. No server checkout. No `*_MCP_SERVER_CWD` surgery.

---

## 6. Gotchas

- **`pwsh` (PowerShell 7) may be absent.** Demo machines often have only
  Windows PowerShell 5.1 (`powershell.exe`). Run scripts via `& scripts\fetch-forma-mcp.ps1`
  in the current shell, or `powershell -File scripts\fetch-forma-mcp.ps1` — not `pwsh ...`.
- **`better-sqlite3` sqlite-mode path is UNTESTED on the exe.** Live smoke ran
  with the default `FORMA_PERSISTENCE_MODE=memory`, which never instantiates the
  native binding. pkg embeds the `.node` via `assets`, but `new Database()` is
  only reached when persistence=sqlite. Verify before enabling durable approval
  tokens in production.
- **tsup CJS emits `index.cjs`, not `index.js`.** The pkg step targets
  `dist-cjs/index.cjs` — don't "fix" it back to `.js`.
- **Without `noExternal`, the MCP SDK isn't bundled** and the exe dies with
  `Cannot find module '@modelcontextprotocol/sdk/server/stdio.js'`. Keep
  `noExternal: [/.*/]` in `tsup.sea.config.ts`.
- **Integrity check after fetch:** the downloaded exe must equal the Release
  asset byte-size (currently 45,389,387 bytes). A size mismatch = truncated
  download.
