# bim-orchestrator

The CLI and engine behind [AutoAudit](../README.md): read a Revit or ACC model,
check it against your rules, and propose the fix — as an ACC issue for a human,
or as an approval-gated parameter write.

New here? Start with **[Try it in 5 minutes](#try-it-in-5-minutes-no-revit-no-acc-no-api-key)**
below — no Revit, no ACC, no API key.

The trust layer (audit chain, dry-run preview, approval tokens) comes from
[`acc-forma-mcp-server`](https://github.com/KenLP/acc-forma-mcp-server), either
self-hosted as a single executable or via the hosted service at
<https://mcp.bimlynx.com>.

## Status

Pilot preview. The deterministic engine is complete and its suite runs offline
with no credentials (`uv run pytest -q`). There is no installer and no code
signing yet, so treat a production rollout as a pilot.

Optional AI-assisted remediation lives in a separate private extension. Without
it — the default — the engine is fully deterministic and the suite passes with
the extension absent; the socket is `llm/interfaces.py` + `llm/factory.py`, and
every value an extension proposes is re-validated by the same rules engine that
raised the finding and is never applied without approval.

Release-by-release detail is in [`CHANGELOG.md`](CHANGELOG.md). Architecture is
in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); the reasoning behind it
is in [`../docs/WHY_THIS_SOLUTION.md`](../docs/WHY_THIS_SOLUTION.md).

## Try it in 5 minutes (no Revit, no ACC, no API key)

Want to see the whole loop — query → QC → auto-fix → approve-gated proposal →
verification report — before touching Revit or ACC credentials?

```bash
git clone https://github.com/KenLP/autoaudit-bim.git
cd autoaudit-bim/bim-orchestrator
uv sync --extra dev
uv run bim-orchestrator --demo --quiet
```

`--quiet` drops the structured log and leaves the summary; omit it to watch
every decision the engine makes. A committed copy of the report a run produces is at
[`docs/sample-output/verification_report.md`](../docs/sample-output/verification_report.md).

That's it — no `.env`, no Forma exe, no Revit session. `--demo` runs the
**real** rules engine, QC, DesignAgent, and trust pipeline against a bundled
mock model ("Demo Villa (simulated)"): auto-fixes execute and a re-check
shows them compliant, an approve-gated fix gets parked as an ACC proposal
(simulated), a manual violation becomes an ACC Issue (simulated), and the run
ends with a full `verification_report.md` — same renderer, same format as a
live run. **Reasoning is live, data is staged.** Open
`config/rules.demo.yaml`, tweak a threshold, and re-run to see the verdict
change.

Three tiers of "real":

1. **`--demo`** (above) — simulated model, zero setup. Learn the loop.
2. **`--run-revit --no-forma`** — a real Revit session (RevitMCPAddin), no
   ACC. See Path B writes land on an actual model.
3. **`--run-revit`** (full Quickstart above) — real Revit + real ACC Issues.
   The full trust pipeline end to end.

**Path A and Path B**, which the docs below use throughout: Path A raises an
**ACC issue** for a person to resolve; Path B writes the **corrected parameter
back into Revit**, and unless the fix is trivially safe it is parked behind an
approval first. Path A is for anything needing judgment or lacking a certain
answer; Path B is only for fixes with exactly one deterministic value.

The Revit side needs the **RevitMCP add-in**
([`KenLP/RevitMCPServer`](https://github.com/KenLP/RevitMCPServer), MIT, Revit
2025–2027 — use **v0.8+**, which exposes the batch route this client uses so a
set of writes is one undo step).

## Quickstart — connecting to real Revit / ACC

The demo above needs nothing. This section is for pointing the engine at an
actual model, which needs credentials. Requires Python 3.12+ and
[`uv`](https://docs.astral.sh/uv/).

```powershell
uv sync --extra dev
uv pip install -e .                 # installs the `bim-orchestrator` CLI entrypoint

# Fetch the Forma MCP server as a standalone exe (no Node.js needed):
& scripts\fetch-forma-mcp.ps1       # → vendor\forma-mcp\forma-mcp.exe (from GitHub Release)
copy vendor\forma-mcp\.env.example vendor\forma-mcp\.env   # then fill APS / SSA credentials

copy .env.example .env              # ACC demo IDs (Revit uses HTTP-direct — no path needed)
.venv\Scripts\bim-orchestrator.exe --hello
```

On a machine that has Node.js + a server checkout, you can skip the fetch and
point `FORMA_MCP_SERVER_CWD` at it instead — see
[`docs/PRODUCTION_PACKAGING.md`](docs/PRODUCTION_PACKAGING.md) for both modes.

Not self-hosting the trust layer at all is also an option:
[`acc-forma-mcp-server`](https://github.com/KenLP/acc-forma-mcp-server) runs as
a hosted service at <https://mcp.bimlynx.com> — same MCP surface, same
preview → approve → apply → audit-log guarantees, no exe to fetch.

The `--hello` flag runs a connection smoke test: spawns the Forma MCP server
(exe or Node), queries the `Rooms` category via `query_elements`, and exits.
**Requires `DEMO_ELEMENT_GROUP_ID` set in the environment/`.env`** — missing it
exits with code 2 before attempting the connection. For Revit-side smoke, use
`--list-revit-rooms` instead (needs Revit + RevitMCPAddin running; set
`REVIT_MCP_VERSION=2027` in `.env` when targeting Revit 2027 — see
the add-in's own docs for details).

## CLI subcommands

| Command | What it does |
|---|---|
| `--demo` | Full loop against a bundled mock model ("Demo Villa") — zero Revit, zero ACC, zero API key. See [Try it in 5 minutes](#try-it-in-5-minutes-no-revit-no-acc-no-api-key). |
| `--hello` | Connect to Forma MCP, query `Rooms` via `query_elements`, exit. Requires `DEMO_ELEMENT_GROUP_ID` (exits 2 if unset). |
| `--check` | Query → QC (read-only), dump `findings.json` + side reports |
| `--apply` | Query → QC → Design once (single pass). Creates ACC Issues. Add `--dry-run` to skip execute. |
| `--run` *(default)* | Full cyclic graph until convergence. Writes checkpoints to `checkpoints/<date>/iteration_NN_*.json`. |
| `--run-revit` | Same loop + Revit MCP path B (live parameter writes back to Revit). Set `REVIT_MCP_VERSION` for 2026/2027 dispatch. |
| `--list-revit-rooms` | Smoke test the Revit MCP bridge — prints document info + rooms table |
| `--verify-catalog` | v1.3: probe `config/ost_catalog.yaml` against the active Revit + AECDM session. Outputs `runs/catalog_verify_<ts>.md` with per-entry verdicts + fillable-label recommendations. Pair with `--no-revit` / `--no-forma` to skip a backend. |
| `--list-runs` | Print a table of every `runs/run-<id>/` folder, newest first |
| `--trend-report` | Regenerate `runs/trend.md` from existing runs (no fresh audit) |
| `--eval-rag` | Ingest the synthetic IBC §7 fixture and score RAG retrieval quality (Phase 2 W4 eval harness, not part of the compliance loop). Tune with `--use-real-embed` / `--eval-top-k`. |
| `--watch-approvals` | Loop the ApprovalWatcher — poll parked proposal issues, apply parked Path B writes when a human flips the issue to "In progress", close + comment. See [Approval-resume loop](#approval-resume-loop-v14-k5). |
| `--apply-approvals-once` | Single ApprovalWatcher pass (one scan, no loop). |
| `--create-verification-views RUN_ID` | v1 report Phase 2: build native Revit **verification schedules** from a finished run's `report_trace.json` — one ViewSchedule per rule (the artifact a reviewer builds by hand to re-check the findings). Writes `verification_views.json/.md` into the run folder. Revit must be open. `--dry-run` previews (rolled back). |
| `--export-report RUN_ID` | Export `runs/<RUN_ID>/verification_report.md` to docx/pdf (`--report-format`, default docx). Markdown stays canonical; uses pandoc when present, else prints how to convert via the document skills. |

Common flags: `--limit N`, `--rule <id>`, `--dry-run`, `--unpublished`, `--issue-subtype-id`, `--max-iterations`, `--checkpoint-dir`, `--rules <path...>` (one or more — multiple YAMLs merge into one run, rule ids dedup), `--findings-out <path>`, `--autonomy <path>` (autonomy YAML, default `config/autonomy.yaml`), `--no-revit`, `--no-forma`.

Logging: `--verbose` (DEBUG) and `--quiet` (WARNING) override `BIM_LOG_LEVEL`; `--log-format plain|json` overrides `BIM_LOG_FORMAT` (default: plain on a TTY, json otherwise).

RAG / grounding flags (Phase 2 — finding citations + retrieval eval):

| Flag | What it does |
|---|---|
| `--bep-pdf <path>` | Path to a BEP PDF. When set, the graph inserts a Grounding step between QC and Route that attaches citations to findings via RAG. |
| `--bep-fixture` | Ingest the synthetic BEP §1 fixture (7 chunks) instead of a real PDF — same Grounding/citation step, for demos when the real BEP isn't ready. |
| `--vector-store-dir <path>` | Persist the ChromaDB vector store at this path (re-ingesting the same PDF is idempotent). Default: ephemeral. |
| `--use-real-embed` | `--eval-rag` only: use sentence-transformers (real, ~90 MB) embeddings instead of the bag-of-words fallback. |
| `--eval-top-k N` | `--eval-rag` only: top-k for retrieval scoring (default **3**). |

Revit / geometry perf + approval flags:

| Flag | What it does |
|---|---|
| `--max-elements N` | P0 element cap (default **300**) shared by both paths — parameter path passes `limit` to `revit_list_elements`; geometry path sets `setA.limit` + `maxResults` on `revit_check_clearance`. `type=int` (argparse) — there is no "unbounded" sentinel value; pass a large number (e.g. `--max-elements 100000`) to effectively lift the cap. **AU-demo limitation:** 300 is the demo cap (the geometry path can't use a Revit section box to pre-narrow); raise it for production runs over larger models, mindful that the Revit API is single-threaded so larger sets run proportionally longer. |
| `--bulk-fields` | P1 N+1 killer (default **off**) — eligible specs fetch all instance-level params in one `revit_find_elements(fields=[...])` call instead of N `get_element_info` calls. Opt in for element-dense categories (Ducts). **Live-verified (R27, 300 ducts):** combined with in-flight type-fetch dedup it cut `get_element_info` from **1200 → 6** and per-query time from **~1.3 s → ~50 ms** (~25×). |
| `--fetch-concurrency N` | `--run-revit` only: number of parallel `get_element_info` calls the Revit query agent dispatches (default **4**). Bump to 8/16 on large models if the per-spec log shows long `elapsed_ms` + high cache-miss counts; set to 1 to debug. |
| `--approvals-dir <path>` | Directory holding parked proposal records (`runs/approvals/<issue_id>.json`); used by both the propose side and the watcher. |
| `--poll-interval <secs>` | ApprovalWatcher poll cadence when looping via `--watch-approvals`. |

#### Approval-resume loop

Approve-gated Path B fixes (a computable value, but a safety-tier parameter that
shouldn't auto-apply) are gathered into **one** ACC "proposal issue" and parked
to `runs/approvals/<issue_id>.json`. A human approves by setting that ACC issue
to **In progress**; the out-of-band `ApprovalWatcher` (`--watch-approvals` loop
or `--apply-approvals-once` single pass) then writes the parked fixes in one
`revit_batch` transaction (one undo, per-element fallback), comments, and closes
the issue. Auto / deterministic fixes are unaffected — they still auto-apply in
the main run. The `📥 Approvals` Streamlit tab surfaces the same flow with an
"Apply approved now" button.

**Approval-security:** before applying, the watcher (1) recomputes a
fingerprint over the parked write-set and compares it against the one stamped in
the ACC issue body — a mismatch (issue edited/tampered) holds the fix back as
`integrity_failed`; and (2) runs a dry-run re-preview of the live values — if a
human changed the target parameter between propose and approve (a third value,
neither the old nor the proposed one), the fix is held back as `stale` rather
than clobbering the drift. Both are intentional hold-backs, not bugs — the issue
stays open and no write happens until the condition clears.

Every `--check` / `--apply` / `--run` / `--run-revit` invocation writes
a self-contained run folder under `runs/run-<8-hex>/`:

```
runs/run-aabbccdd/
  metadata.json             run shape (mode, status, duration, 4-bucket counts)
  trace.md                  token-efficient reasoning trace
  findings.json             non_compliant findings (machine)
  outcomes.json             all 4 buckets + summary, structured
  report.md                 per-run audit report (human, concise)
  report_trace.json         structured (element, rule) trace incl. the PASS set
  verification_report.md    "is this trustworthy + how do I check it myself?" report
  review_queue.md           manual_review_items
  data_quality_report.md    missing_data items grouped by parameter
  verification_views.json/.md   (after --create-verification-views) schedule build manifest
  profile.json              (only for `--audit <profile>`) profile identity — name/mode/rules/propose_only
  delta.md / delta.json     (only for `--audit`, on a successful run — completed or converged) resolved/newly_introduced/persistent vs the last comparable run
runs/trend.md               cross-run aggregate (refreshed each run)
runs/issue_registry.json    (only for `--audit`) cross-run Path A issue dedup
```

#### Verification report (v1 report module)

Every run also writes **`verification_report.md`** — the human-readable answer to
a skeptical BIM Director's "is what the AI did accurate, and how do I check it
*myself*?". It RENDERS the run's recorded `report_trace.json` (it never re-runs a
check) and, per rule, gives a **native Revit/ACC recipe** to independently
reproduce the finding set — a schedule, a view filter + colour, the ElementId
"Select by ID" list, the ACC issue + audit chain. It lists the **PASS set** too
(so you can audit for false negatives) and a "What we did NOT touch, and why"
section (missing-data, manual-review, parked, budget-capped). `--create-verification-views`
then BUILDS the per-rule schedule in Revit for you (one click, still 100%
native); `--export-report` renders docx/pdf.

For running audits **unattended on a schedule** (Windows Task Scheduler →
`POST /audits`, propose-only so nothing writes the model without a human
approval, plus a per-run delta report of resolved/new/persistent problems),
see [`docs/SCHEDULED_AUDIT.md`](docs/SCHEDULED_AUDIT.md).

## BIM Manager UI (Streamlit)

For non-CLI users (BIM Managers), launch the v1 Streamlit app:

```bash
uv run streamlit run streamlit_app/app.py
```

Opens at <http://localhost:8501> with seven tabs.

> **The Rule Builder's natural-language drafting step calls the Anthropic API
> and needs your own `ANTHROPIC_API_KEY`** (set it in the environment or
> `.env`) — that one step is billed to your account. Everything else here,
> including `--demo`, the rules engine, QC, DesignAgent and the reports, is
> deterministic and needs no API key at all.

1. **📋 Rule Builder** — describe a check in natural language → Claude drafts a
   rule → editable form → save as `config/rules.<scenario>.yaml`. **Catalog-wired:**
   extraction is grounded with the live OST categories + the valid built-in params
   per category + intent→param aliases (`config/param_catalog.<ver>.yaml`), so a
   catalogued category shows the parameter as a **built-in dropdown** (read-only
   params refused as a write target; write target + unit pre-filled from the param's
   binding/dimension; legacy requirements auto-migrated on load). A "📂 Lịch sử tạo
   rule" expander replays the last 10 drafts from
   `runs/rule_builder_history.jsonl` with one-click Restore.
2. **⚙️ Setup** — Forma project IDs (pre-filled from `.env`), rules YAML
   multi-select (empty until you pick — shows a "N rule sẽ chạy" preview
   table; merges several files into one run), run options (mode / **max ACC
   issues** default 5 / rule filter / dry-run). Connection panel: **Forma MCP
   smoke test**, **Revit addin Test connection** (`GET /health`),
   and the **🔑 Forma / APS credential wizard** that writes APS/SSA keys into
   `vendor/forma-mcp/.env`. MCP Server Paths panel auto-detects the
   SEA exe vs Node layout.
3. **▶️ Run** — pre-flight check (green/red dot per required input), big
   "Run" button, live stdout tail, run_id captured into session state
   on completion.
4. **📊 Results** — defaults to the most-recent run (or whichever the
   Run History tab pinned), four metric cards (Compliant / Non-Compliant /
   Manual Review / Missing Data), filterable findings table (`element id` +
   `element name` = "<family> - <type>" for type params; host-inherit source
   shown as `(trống) ⤺ host: <value>`), links to every artefact. A **batch
   approval banner** lists pending Path B Revit writes (
   approval is consolidated into the 📥 Approvals tab below — see there for
   the governed ACC-status flow; the old inline "Approve & Execute" /
   `--apply-approved` has been removed).
5. **📥 Approvals** — parked proposal records grouped **one per
   rule** (Chờ duyệt on top / Lịch sử below; applied rows show the new value;
   each fix shows current → proposed with the host source for inherited
   values; Ignore button) + an "Apply approved now" button that shells
   `--apply-approvals-once` to run the ApprovalWatcher once.
6. **📈 Trend** — Plotly line chart of compliance % over runs, metric
   cards for the latest-vs-previous fingerprint diff (Resolved /
   Newly introduced / Persistent), expandable preview of `runs/trend.md`.
7. **📜 Run History** — table from `list_runs()`, click a row to pin
   that run as the Results-tab target.

The extraction front-half (JSON envelope → cleaned JSON + YAML) lives in the
[`extraction-skills/`](extraction-skills/README.md) skill pack run from Claude
Desktop; its in-app review surface is reached from the Setup tab's upload flow.

The UI is a thin presentation layer -- it spawns the existing
orchestrator CLI as a subprocess; the `bim_orchestrator` package
itself never imports `streamlit`.

## Architecture

```
                EXTRACTION (offline — human-driven)
   ┌──────────────────────────────────────────────────────────────┐
   │  BEP / IBC / spec PDF                                         │
   │    │                                                          │
   │    │ attach extraction_prompt.md + rule_schema.json           │
   │    ▼                                                          │
   │  Claude Desktop  →  JSON envelope                             │
   │                       │                                       │
   │                       ▼                                       │
   │  scripts/json_to_yaml.py (or Streamlit 🔎 Extraction Review)  │
   │   • Pydantic validate                                         │
   │   • heuristics: unit-missing, scope-filter, fragmentation     │
   │   • OSTCatalog normalise category                             │
   │   • confidence<0.75 → force fixability=manual                 │
   │                       │                                       │
   │                       ▼                                       │
   │  config/rules.<scenario>.yaml  +  runs/extraction_review_*.md │
   └──────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
              RUNTIME (LangGraph cyclic graph)
   ┌──────────────────────────────────────────────────────────────┐
   │  Orchestrator                                                 │
   │    │                                                          │
   │    ├── OSTCatalog (config/ost_catalog.yaml) ── dual-label     │
   │    │                                       (aecdm + ost)      │
   │    │   └ ParamCatalog (config/param_catalog.<ver>.yaml) ──    │
   │    │       built-in params per category (storage/binding/     │
   │    │       dimension); grounds Rule Builder + units view      │
   │    │                                                          │
   │    ├── QueryAgent (Forma)  ─► AECDM via acc-forma-mcp-server  │
   │    │     │ derive_specs(rules, "aecdm", catalog) → fetch       │
   │    │                                                          │
   │    ├── RevitQueryAgent     ─► Revit MCP (list_elements +      │
   │    │     │                    type/instance hydration +       │
   │    │     │                    host hop on follow_host=True)   │
   │    │                                                          │
   │    ├── QCAgent             → 4-bucket + canonical_format +    │
   │    │     │                    autofill _suggest (normalize/   │
   │    │     │                    template/map/auto/reference/    │
   │    │     │                    inherit*) + Rule.unit + RAG     │
   │    │                                                          │
   │    └── DesignAgent         → _effective_remediation (auto     │
   │              │               target) + dry-run + autonomy     │
   │              ├─ Path A: ONE ACC Issue per (rule, status)      │
   │              └─ Path B: write/rename; approve-gated → ONE     │
   │                          proposal issue PER RULE              │
   └──────────────────────────────────────────────────────────────┘
```

Run loop: `query → qc → design → query → qc → ✅ converged` (max 3 iterations).
For the full engine + agent-flow narrative (autofill strategies, auto write-target,
per-rule grouping, inherit-from-host, reference data) see
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) **§0**.

v1.3+ query agents are rules-driven — they call
`policies.query_specs.derive_specs(rules, backend, catalog)` to translate
the active `RuleSet` into per-category fetch specs. Add a new Revit
category by extending `config/ost_catalog.yaml`, not by editing Python.
The catalog ships with entries across architecture / structure / MEP and
every AECDM-non-null entry was live-verified against ACC on
via the `--verify-catalog` CLI.

The extraction front-half shares the same `RuleSet` schema, now also
produced from PDFs via the Claude Desktop skill pack. See
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for the full
end-to-end diagram and [`extraction-skills/README.md`](extraction-skills/README.md)
for the workflow.

## Configuration

| File | Purpose |
|---|---|
| `.env` | ACC credentials, MCP server paths (Forma + Revit), `REVIT_MCP_VERSION` (2026 default; set to 2027 to dispatch to port 7892 + the 2027 token path) |
| `config/autonomy.yaml` | Tiered autonomy policy (auto / approve / human-only) |
| `config/ost_catalog.yaml` | v1.3: dual-label registry of BIM categories. `aecdm_label` for Forma queries, `ost` for Revit `BuiltInCategory`, plus display name + discipline + aliases (incl. Vietnamese). Verify with `bim-orchestrator --verify-catalog`. |
| `config/param_catalog.<ver>.yaml` | The PARAMETER layer (sibling of `ost_catalog`): `(OST, parameter) → ParamSpec` (storage/binding/writable/dimension) per Revit version. Generate via `scripts/dump_param_catalog.py` (probes a live model) — don't hand-author. |
| `config/reference.<name>.yaml` | Authoritative value lists (`entries[].canonical` + `aliases`, `case_sensitive`) for `normalize_kind=reference` rules. One set, many rules. Ships a demo `reference.approved_materials.yaml`. |
| `config/lookup.<name>.yaml` | Code-table lookup for `relation_compare` + `lookup:` (e.g. `lookup.ibc716.yaml` — IBC §716 fire-rating-required-by-occupancy table). A row marked not-rated → `exempt` outcome; no matching row → `manual_review`. |
| `config/shared_param_conventions.yaml` | Maps a shared-param convention (e.g. Assembly Code, Classification Number) to its authoritative `reference.<set>.yaml`, so the Rule Builder can disambiguate "member of an approved set" from a code-table lookup. |
| `config/rules.parameter_completeness.yaml` | Phase 1 rules for Rooms parameter completeness |
| `config/rules.room_compliance.yaml` | BEP §1 room compliance (residential/other floor area minimums, clear height, unique numbers, Department required) |
| `config/rules.fire_rating_ibc7.yaml` | IBC Ch.7 fire-rating compliance (walls + doors + host hop) |
| `config/rules.doors_fire_rating_inherit_format.yaml` | Demo: compound `inherit_then_normalize` (Door Fire Rating present-or-inherit-from-host AND 'X HR') |

## License

MIT
