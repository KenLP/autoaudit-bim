# Pilot install — AutoAudit on a fresh company machine

Step-by-step install for a machine that is **not** a dev box: Windows,
Revit 2026 or 2027 installed, no toolchain assumed beyond what this doc
installs (Python via `uv` in step 3, and Node in step 10 if you want the
in-Revit panel). If you're setting up a development checkout instead,
use the shorter [`README.md` Quickstart](../README.md#quickstart).

For a scripted version of most of this, see
[`scripts/install-autoaudit.ps1`](../scripts/install-autoaudit.ps1) — this
document is the manual walkthrough (and the reference the script follows).

## 1. Prerequisites

- **Windows 10/11.**
- **Autodesk Revit 2026 or 2027** installed, with the **RevitMCP add-in**
  loaded. It is a separate open-source project — this repo ships only the
  client that talks to it. Get it from
  [`KenLP/RevitMCPServer`](https://github.com/KenLP/RevitMCPServer)
  ([latest release](https://github.com/KenLP/RevitMCPServer/releases/latest),
  MIT, Revit 2025–2027) and follow its README to install the `.addin`.
  **Use v0.8 or newer:** this client posts batched parameter writes to
  `POST /mcp/batch` so a set of fixes lands as one undoable transaction,
  and older add-ins fall back to writing them one at a time. The same
  release ships the **AutoAudit ribbon tab and dockable panel** used in
  step 13 — nothing extra to install for it.
- **PowerShell** (built into Windows — `powershell.exe` or `pwsh` both work
  for the commands below).
- Network access to GitHub (for `uv`, the forma-mcp exe release, and cloning
  this repo) — or a pre-downloaded copy of everything if the machine is
  offline. No Anthropic API key is required for `--check` / `--apply` /
  `--run` / `--run-revit` / `--audit` — the deterministic engine runs
  LLM-free. An API key is only needed for the optional Rule Builder
  natural-language authoring tab in Streamlit.

## 2. Get the code

Clone (or copy) this repository onto the machine, then open a PowerShell
prompt **inside `bim-orchestrator/`** (the subfolder, not the repo root) for
every command below.

## 3. Install `uv`

```powershell
winget install --id=astral-sh.uv -e
```

If `winget` isn't available, follow the fallback installer at
<https://docs.astral.sh/uv/getting-started/installation/> (a `pip install
uv` or the PowerShell one-liner both work). Confirm with:

```powershell
uv --version
```

## 4. Install Python dependencies

```powershell
uv sync --extra dev --extra service --inexact
```

**Use `--inexact`, not a bare `uv sync`.** Without it, `uv sync` performs an
*exact* sync of the environment and will **remove** two optional editable
installs if they happen to already be present on this machine (an AI
remediation extension and the PDF-rule-extraction sibling package) — neither
is needed for a pilot install, but `--inexact` costs nothing and avoids the
surprise if someone later adds them. `--extra service` pulls in the AuditHub
FastAPI service dependencies (needed for step 8 below); `--extra dev`
includes it too, so the two together are redundant but harmless — keep both
for parity with the dev setup this doc is derived from.

Sanity check:

```powershell
.venv\Scripts\bim-orchestrator.exe --hello
```

## 5. Fetch the Forma MCP server (ACC connectivity)

```powershell
& scripts\fetch-forma-mcp.ps1
copy vendor\forma-mcp\.env.example vendor\forma-mcp\.env
```

Edit `vendor\forma-mcp\.env` and fill in the APS/SSA credentials for your ACC
account (ask whoever administers your Autodesk Construction Cloud
integration for these — they are not something this repo can generate).

This is a plain HTTPS download from a public release — no GitHub account,
no CLI, nothing to authenticate. (If you point `-Repo` at a private fork,
the script falls back to the GitHub CLI and uses your existing `gh` login.)

**The download is verified before it is installed.** `forma-mcp.exe` is
unsigned, so the script downloads to a temporary file, checks its SHA-256
against the digest published with the release, and only then renames it into
place — a mismatch discards the file and leaves any previous copy untouched.
The verified hash is written next to the exe as `forma-mcp.exe.sha256`, and
`bim-orchestrator --doctor` re-checks the binary against it, so a file that
changes after install is caught too.

Be clear about what that does and does not prove: verifying against the
release's own digest catches a corrupted, truncated or proxy-substituted
download. It cannot prove the release itself was not tampered with, because
the hash and the file come from the same place. On a locked-down machine,
read the hash from the release page yourself and pin it:

```powershell
& scripts\fetch-forma-mcp.ps1 -ExpectedSha256 <hex>
```

Code signing would settle this properly and is not in this release.

## 6. Fill in `.env`

```powershell
copy .env.example .env
```

Open `.env` and fill in your ACC project/hub IDs (`DEMO_PROJECT_ID` etc. —
rename these appropriately for your real project, they're demo-named but
used for any project) and Revit connection settings if you're not using the
HTTP-direct default. Comments inside the file explain every key.

## 7. Configure the audit satellites (LOD + spatial axes)

The two audit axes — LOD validation and spatial code-compliance — run as
**separate satellite processes**, each in its own Python 3.10 virtual
environment (they pin dependencies that conflict with this project's
Python 3.12+ environment; never try to merge the envs).

```powershell
copy config\audit_services.yaml.example config\audit_services.yaml
```

Edit `config\audit_services.yaml` to point at the two satellite repos'
Python 3.10 venvs on this machine (`lod_validator`, `spatial_qc`, and
`revitcontrol` if you're also setting up unattended mode — see
[`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)
for that). **A missing entry or file is not fatal** — the audit axis reports
"skipped: unconfigured" instead of crashing, so you can run without the
satellites at first and add them later.

## 8. Export IFC (manual, until `export_ifc` lands)

Both audit axes consume an **IFC export** of the model, not the live Revit
model directly. There is currently no automated export command — see
the add-in gap for the
capability request tracking this gap. Until it lands, export by hand:

1. Open the model in Revit.
2. **File → Export → IFC**.
3. Choose **IFC4** as the schema version.
4. Save the `.ifc` file somewhere stable (e.g. next to the `.rvt`).
5. Point your audit profile's `axes.lod.ifc_path` /
   `axes.spatial.ifc_path` (in `config/audit.<name>.yaml`) at that path.

Re-export whenever the model changes meaningfully before re-running an
audit — the axes only see whatever IFC snapshot you last exported.

## 9. Known limitations

- **LOD checks are model-wide, not per-element.** The LOD axis reports a
  single required-LOD verdict class for the whole export
  (`axes.lod.required_lod` in the profile), not a per-element LOD score.
- **Spatial checks are width-only through the profile.** The only threshold
  the audit profile can currently override is minimum corridor width
  (`axes.spatial.required_width_m`) — headroom, turning-circle, and door
  clearance use the satellite's bundled defaults. Exposing full
  per-space-type config is a request open against the satellite.
- **No automated IFC export** (step 8 above) — the Revit add-in has no
  `export_ifc` command yet, so that step stays manual.

## 10. Build the AutoAudit UI

The console is a small web app the service serves at `/ui`. It is **not**
committed as a build — `autoaudit-ui/dist/` is generated — so build it once:

```powershell
& scripts\build-ui.ps1
```

Needs [Node.js 18+](https://nodejs.org/) on PATH. Skip this and the service
still runs and the CLI still works, but `/ui` (and the in-Revit panel in step
12) answers with a page saying the UI was never built.

## 11. Start the service

The AuditHub service is a small local API (no cloud, no auth) that
orchestrates audit runs and streams progress. Start it with:

```powershell
# On a Revit 2027 machine set the version so the service talks to the right
# addin port (default is 2026 -> 7891; 2027 -> 7892). Without this,
# "Highlight in Revit" and "Create verification views" can't reach Revit
# even though the addin is running. Put REVIT_MCP_VERSION in .env, or:
$env:REVIT_MCP_VERSION = "2027"
uv run autoaudit-service
```

It listens on **`http://127.0.0.1:8601`**, bound to localhost only — it does
**not** authenticate requests, so **do not** expose this port to the LAN or
internet (no reverse proxy, no port-forward, no `0.0.0.0` bind override).
Anyone who can reach the port can trigger an audit run.

Smoke-test it in another terminal:

```powershell
curl http://127.0.0.1:8601/health
```

A healthy response returns `200 OK` with a small JSON status body.

## 12. Run an audit

The bundled demo profile ships with both IFC axes **disabled** so it loads
cleanly on any machine (see `config/audit.demo.yaml`). To run a real audit:

1. Copy `config/audit.demo.yaml` to a new file, e.g.
   `config/audit.mymodel.yaml` (or edit the demo file directly for a first
   trial run).
2. Set `axes.lod.enabled: true` / `axes.spatial.enabled: true` and update
   both `ifc_path` values to your exported IFC from step 8.
3. Run:

```powershell
bim-orchestrator --audit config/audit.mymodel.yaml
```

This runs the LOI (rule-based) checks plus whichever IFC axes you enabled,
into one `runs/<id>/` folder with one `verification_report.md`. See
[`docs/ARCHITECTURE.md` §9](../../docs/ARCHITECTURE.md) for what the report contains
and how to re-check a finding natively in Revit/ACC.

You can also drive the same run through the service started in step 11
(`POST http://127.0.0.1:8601/audits`) or through the Streamlit console
(`uv run streamlit run streamlit_app/app.py`, then the Rule Builder / audit
tabs) — both are thin front ends over the same `--audit` machinery.

## 13. Open the panel inside Revit

The add-in from step 1 ships a dockable AutoAudit panel, so the console does not
have to live in a separate browser tab.

1. Start the service (step 11) and leave it running.
2. In Revit, open the **AutoAudit** ribbon tab and click **Panel**.
3. The panel loads `http://127.0.0.1:8601/ui/` — the same UI as the browser.

If the panel is blank or shows a "not built" page, step 10 was skipped.

**Pointing the panel somewhere else.** It reads an optional config file and
falls back to the default URL when the file is missing or unreadable:

```
%APPDATA%\Autodesk\Revit\Addins\<version>\revit-mcp-panel.json
```

```json
{ "url": "http://127.0.0.1:9000/ui/" }
```

Use it when the service runs on a non-default port, or on another machine on the
same trusted network — remembering that the service does not authenticate
requests (step 11), so anything that can reach the port can start an audit.
