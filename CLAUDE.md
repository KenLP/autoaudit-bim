# CLAUDE.md — onboarding pointer for Claude Code sessions

You're working on **bim-orchestrator**, a multi-agent BIM quality-assurance
system targeting Autodesk University 2026. Read this file first; it points
you to the right deeper docs for whatever task the user gives you.

## What this project does

A LangGraph-driven loop that reads a Revit/ACC model, checks it against
YAML compliance rules, and proposes fixes via MCP — either as ACC Issues
(Path A) or live Revit parameter writes (Path B). The trust pipeline
(dry-run preview → approval token → execute → audit chain) is provided
by sibling repo [`acc-forma-mcp-server`](https://github.com/KenLP/acc-forma-mcp-server).

End-to-end flow:

```
BEP / IBC / spec PDF
   → Claude Desktop + extraction-skills/extraction_prompt.md  (offline)
   → JSON envelope → json_to_yaml.py → config/rules.<scenario>.yaml
   → bim-orchestrator --check / --apply / --run / --run-revit
   → QueryAgent (rules-driven via OSTCatalog) → QCAgent (4-bucket outcomes)
   → DesignAgent → Path A (ACC Issue) or Path B (Revit set_parameter, one revit_batch = one undo)
   → approve-gated Path B → ACC "proposal issue"; ApprovalWatcher applies on status "In progress" (v1.4-K5)
```

## Where to look first

| When you need… | Read |
|---|---|
| End-to-end architecture diagram + agent flow | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Latest changes + iteration trail | [bim-orchestrator/CHANGELOG.md](bim-orchestrator/CHANGELOG.md) |
| CLI surface + tabs + quickstart | [bim-orchestrator/README.md](bim-orchestrator/README.md) |
| **Verification report** (v1.5-R1: trust-but-verify audit + native Revit/ACC re-check recipes + auto-created schedules + docx/pdf) | [docs/ARCHITECTURE.md §9](docs/ARCHITECTURE.md) · per-run `runs/<id>/verification_report.md` |
| **Production packaging** (no-Node deploy, SEA `forma-mcp.exe`, Revit HTTP-direct, fetch/publish, distribution) | [bim-orchestrator/docs/PRODUCTION_PACKAGING.md](bim-orchestrator/docs/PRODUCTION_PACKAGING.md) |
| **Scheduled/continuous audit** (v1.7-3bP1: unattended `--audit` via Task Scheduler → `POST /audits`, `propose_only`, cross-run Path A issue dedup, per-run `delta.md`) | [bim-orchestrator/docs/SCHEDULED_AUDIT.md](bim-orchestrator/docs/SCHEDULED_AUDIT.md) |
| Extraction workflow (PDF → YAML) | [bim-orchestrator/extraction-skills/README.md](bim-orchestrator/extraction-skills/README.md) |
| **Rule Capability Catalog** (public-shareable AU doc: every NL use case the Rule Builder compiles to YAML, with examples; grounded in the 63/63 NL suite) | [docs/RULE_CAPABILITY_CATALOG.md](docs/RULE_CAPABILITY_CATALOG.md) |

## Layout (the active code lives in `bim-orchestrator/`)

```
autoaudit-bim/                           ← repo root (git root)
├── CLAUDE.md                            ← this file
├── LICENSE                              ← MIT
├── docs/                                ← workspace-level docs
│   ├── ARCHITECTURE.md                  ← canonical architecture
│   ├── WHY_THIS_SOLUTION.md             ← design rationale (EN/VI)
│   └── RULE_CAPABILITY_CATALOG.md       ← what the Rule Builder compiles
├── references/                          ← BEP / IBC fixtures + sample JSON
├── bim-orchestrator/                    ← the actual product
│   ├── README.md                        ← CLI quickstart
│   ├── CHANGELOG.md                     ← v1.0 → v1.5-R1 trail
│   ├── src/bim_orchestrator/
│   │   ├── orchestrator.py              ← CLI + run modes (+ --watch-approvals + report cmds + --audit)
│   │   ├── audit_axes.py                ← v1.6-P3: one-shot LOD/Spatial satellite run → K7 bucket
│   │   ├── issue_registry.py            ← v1.7-3bP1: cross-run Path A issue dedup (fail-open on ACC lookup)
│   │   ├── delta_report.py              ← v1.7-3bP1: per-run delta.md/.json (resolved/new/persistent vs baseline)
│   │   ├── service/                     ← v1.6-P3: AuditHub FastAPI :8601 (app/models/runner, SSE, lock)
│   │   ├── cli.py                       ← entry-point shim (pyproject → orchestrator.main)
│   │   ├── logging_setup.py             ← plain/json log toggle
│   │   ├── graph.py                     ← LangGraph build_graph + route_node
│   │   ├── state.py                     ← OrchestratorState + Finding + ProposedFix + CheckRecord
│   │   ├── approval_watcher.py          ← v1.4-K5 Loop 2: ACC status-watcher → Path B
│   │   │                                   (+ approval-security fingerprint gate/stale re-preview)
│   │   ├── report_trace.py              ← v1.5-R1: CheckRecord builder + Design/Result join
│   │   ├── verify_recipes.py            ← v1.5-R1: requirement→native-verify-recipe registry
│   │   ├── audit_report.py              ← v1.5-R1: render verification_report.md
│   │   ├── verification_views.py        ← v1.5-R1 P2: auto-create Revit schedules
│   │   ├── report_export.py             ← v1.5-R1 P2: docx/pdf export (pandoc/skills)
│   │   ├── reports.py / run_recorder.py ← per-run report.md/trend.md + RunFolder/trace
│   │   ├── agents/                      ← Query / RevitQuery / Geometry / QC / Grounding / Design
│   │   ├── mcp_clients/                 ← Forma (exe/stdio) + Revit (HTTP-direct/stdio)
│   │   ├── demo/                        ← v1.5-R4: `--demo` mock Revit+Forma clients + Demo Villa dataset
│   │   ├── rag/                         ← ChromaDB store + chunker + pdf_extractor + eval
│   │   └── policies/                    ← schema + rules_engine + autonomy + OST + revit_units +
│   │                                       normalize + lookup_table + approval_integrity +
│   │                                       ids_converter + shared_params + audit_profile (v1.6-P3)
│   ├── config/                          ← ost_catalog.yaml + rules.*.yaml + autonomy.yaml +
│   │                                       param_catalog.<ver>.yaml + reference.*.yaml +
│   │                                       lookup.*.yaml + shared_param_conventions.yaml +
│   │                                       audit.<name>.yaml + audit_services.yaml(.example)
│   ├── docs/PRODUCTION_PACKAGING.md     ← v1.4-G: no-Node deploy, SEA exe, distribution
│   ├── extraction-skills/               ← v1.4-D0 skill pack for Claude Desktop
│   ├── streamlit_app/app.py             ← 7-tab BIM Manager UI (Rule Builder first, Approvals tab K5)
│   ├── vendor/forma-mcp/                ← forma-mcp.exe (gitignored, fetched) + .env(.example)
│   ├── scripts/                         ← fetch-forma-mcp.ps1 + smoke runbooks + dump_param_catalog.py
│   └── tests/                           ← deterministic; run `uv run pytest --collect-only -q`
│                                           for the current count (see "Test posture" below)
```

## Conventions (do not learn these the hard way)

- **`policies/` is the lower layer** — pure schema + lookups, no I/O.
  `agents/` may import from `policies/`, never the other way round.
- **Rule declaration is Scope→Check→Severity→Action (v1.4-K10)** — `numeric_compare`
  (operator+threshold) subsumes positive_number/numeric_min; `relation_compare`
  (compare_kind numeric/fire_rating/string) generalises fire_rating_ge; `scope_filter`
  is a UNIVERSAL gate (any requirement, not just the legacy numeric_min_conditional);
  `severity_level` (Low/Med/High) is decoupled from the requirement kind (QC uses it
  over the severity_tag→level map). Legacy requirements still evaluate — don't delete
  them. Rule Builder now authors Path-B remediation (autofill+remediation) directly.
  Rationale: docs/ARCHITECTURE.md § Rule Builder.
- **MCP boundary is absolute** — agents never talk to ACC or Revit
  directly, always through `mcp_clients/`. Tests use `_mocks.py` with
  1:1 protocol parity.
- **Verification report renders, never re-derives (v1.5-R1)** —
  `audit_report.render_audit_report` reads `state.check_trace` + `proposed_fixes`
  ONLY; it must NOT re-run a check (a 2nd evaluation = a 2nd source of truth that
  could disagree). The PASS set (compliant + lookup-exempt — the false-negative
  defence) is captured by QC into `check_trace` DURING the run; DesignAgent is
  untouched (the renderer JOINS outcomes by `(rule_id, element_id)` /
  `(rule_id, bucket)` via `report_trace.outcome_for`). Per-rule verify recipes come
  from `verify_recipes.REQUIREMENT_RENDERERS`, which MIRRORS `rules_engine.evaluate`'s
  dispatch — add a requirement to BOTH, never hardcode to a rule/category.
  `verification_views.py` auto-builds the per-rule Revit schedule via
  `mcp_clients/revit.py` (boundary holds); `apply_view_filter` is equality-only
  (the add-in has no operator support there) so the coloured view is
  manual. No LLM in the report path.
- **Document identity is captured ONCE, renderers only read (v1.7-3bP3)** —
  `orchestrator._fetch_document_identity` stamps the open Revit document's
  title/path/version/is_modified into `state.document_info` a single time,
  right after `_run_with_forma` enters the live `revit_client` (best-effort:
  transport error or no document open → `None`, never fails the run).
  `metadata.json["document"]` / `report.md` / `verification_report.md` /
  `delta.md` only READ it (renders-never-re-derives applies here too) — never
  re-fetch. Wire camelCase (`pathName`, `isWorkshared`, …) is normalized to
  snake_case in the helper; demo overrides it via
  `MockRevitMCPClient.document_info`. Forma-only `--run`/`--check` never set
  the key → `document: null`.
- **Query agents are rules-driven (v1.3+)** — derive everything from
  the active `RuleSet` via `policies/query_specs.derive_specs`. Add
  a category by editing `config/ost_catalog.yaml`, NOT by editing
  Python.
- **Parameter catalog = the PARAMETER layer (`policies/param_catalog.py`)** —
  sibling of `ost_catalog` (category layer). Maps `(OST, parameter) → ParamSpec`
  (storage/binding/writable/dimension), per Revit version
  (`config/param_catalog.<ver>.yaml`; generate via `scripts/dump_param_catalog.py`,
  don't hand-author). It GROUNDS the Rule Builder (param dropdown, read-only refused
  as write target, write-target/unit prefill, extraction grounding + intent→param
  aliases incl. the Stairs-vs-Stair-Runs disambiguation — see
  `app.py:_rb_grounding_block` / `_INTENT_ALIASES` / `_CATEGORY_NOTES`) and
  `revit_units` (below). SYSTEM families fully; loadable (Doors/Windows/Structural)
  carry common built-ins only — the rest uses the `✏️ Khác` / `bound_parameter`
  escape. Add a category by probing it live, not by editing Python.
- **Unit handling lives in data (v1.4-D0.5)** — set `Rule.unit` and
  `policies/revit_units.convert_to_rule_unit` does the math.
  `REVIT_STORAGE_UNITS` is now a VIEW of `param_catalog` (any length/area/volume
  built-in converts; the explicit dict is just overrides). Do NOT reintroduce
  metric-mirror parameter names like `"Unbounded Height (m)"`.
- **`normalize` is a unit registry, not baked-in kinds (v1.4-K13)** —
  `policies/normalize._DIMENSIONS` declares duration/length/area as
  `(unit alias → factor-to-base)` tables; `normalize_quantity` parses a
  `(magnitude, unit)` and renders the `normalize_format` token in the chosen
  output unit (`{m}`/`{h}`, `{mm}`/`{cm}`/`{m}`, `{m2}`). `fire_rating` is an
  ALIAS of `duration` (keep it). Add a unit/dimension by editing the table —
  NEVER a new `if kind ==` branch. Fixed/enumerated text uses
  `normalize_kind="map"` + `normalize_map`. Keep the parser separator-tolerant
  so a canonical value round-trips through its OWN normalizer (else
  `canonical_format` with a hyphen format silently fails). The Rule Builder
  offers kinds as a DROPDOWN with a per-kind default format — don't restore the
  hard-wired `{h}-hour` default. v1.4-K15 adds `normalize_kind="template"`
  (`normalize_source` regex captures → `normalize_format` renders) — the general
  deterministic NAMING transform; it restructures a name that contains the
  tokens, but can't invent a missing token or change casing → Path A. The Rule
  Builder's Action is ONE selector (`📋 Issue | 🔧 normalize/compose/fixed`); 🔧 is
  always approve-gated Path B; rename is a write-target, not a strategy. v1.4-K16
  adds `normalize_kind="auto"` (the DEFAULT): for a `matches_regex` rule QC calls
  `auto_candidates(value)` and keeps the first output matching the pattern — the
  author declares only the pattern (no unit/format). `auto` covers units +
  separators; `template`/`map` can't be auto (the pattern can't supply a parse
  regex / lookup table) — keep them manual.
- **Findings convergence is by fingerprint, not count (v1.4-F3)** —
  if you touch `graph.py:route_node`, preserve the fingerprint check.
- **`max_issues` caps ACC ISSUES, not fixes; everything grouped by rule (v1.4-K18/K22.1)** —
  Path B auto-fixes gather into ONE approve-gated proposal issue **PER RULE**
  (v1.4-K22.1 — was one COMBINED issue for all rules; that mixed unrelated
  problems). Each proposal lists ALL of its rule's elements, **never sliced**
  (slicing truncated it → "proposal lists 4 of 6 families"). Path A manual findings
  likewise group by `(rule, status)` into ONE issue per problem
  (`_propose_rule_group`), listing all affected elements — NOT one issue per
  element. `_apply_quota` is now a pass-through; the budget caps the NUMBER of ACC
  issues at the GROUP level in `run()` — per-rule proposals reserve their share
  first, Path A groups take the rest. Still partition BEFORE anything (v1.4-F2);
  `_dedup_by_write_target` collapses Path B by **(rule_id, write_eid, param)**
  (v1.4-K22.2 — WITHIN a rule only; two rules writing the same (type,param) each
  keep their fix → each its own issue. Keying by (write_eid,param) alone collapsed
  cross-rule and made the 2nd rule's fixes vanish — don't revert). The old
  per-element `_propose_one` / `_propose_grouped` / `_build_grouped_issue_payload`
  are DEAD — don't revive them. **ACC caps issue `description` at 1000 chars**
  (over → APS 400 `ISSUES_SERVICE_BAD_REQUEST` "description exceeds max limit of
  1000 characters"). `_fit_acc_description` (design.py) trims the BODY TEXT at a
  line boundary + appends an honest footer; the write-set/record still carries
  ALL elements ("never sliced" is about the fixes, NOT the display text). Path B
  proposals pass `reserve=len(fp_block)` so the `AutoAudit-Fingerprint` marker
  survives the trim (the watcher's integrity gate reads it from the body). Title
  has its own smaller ACC limit — keep it short.
- **Revit Instance > Type fallback respects blank strings (v1.4-F1)** —
  use `_has_value` helper in `revit_query._merge_params`, not
  `is not None`.
- **Forma exe is fetched, never committed (v1.4-G2)** — `vendor/forma-mcp/forma-mcp.exe`
  is gitignored; it rides a GitHub Release (tag `forma-mcp-sea`). Rebuild it
  in `acc-forma-mcp-server` (`npm run sea:build`), not here. On demo machines
  `pwsh` may be absent — run PS scripts via `& scripts\fetch-forma-mcp.ps1`.
  See [docs/PRODUCTION_PACKAGING.md](bim-orchestrator/docs/PRODUCTION_PACKAGING.md).
- **Write target can be `auto` (v1.4-K19)** — `remediation.target="auto"` (the
  Rule Builder default) lets `DesignAgent._effective_remediation(rule, element)`
  resolve `(action, target)` per element: `Family Name`→rename family, `Type
  Name`→rename type, a Type-carried param (query mirrors it as `type.<param>`,
  e.g. Fire Rating)→write the Type, else instance. The resolved action+target are
  stamped on the fix preview (`preview["action"]`/`["target"]`) so the proposal
  body + ApprovalWatcher hit the right element. Explicit instance/type/family is
  honoured verbatim (override for params bound to BOTH). Schema default is still
  `instance` — don't flip it (would change existing YAML); only the Rule Builder
  emits `auto`. `_ensure_family_map` must also fetch when an `auto` rule's param
  is `Family Name`.
- **Requirement set is consolidated; legacy still evaluates (v1.4-K22)** — the Rule
  Builder OFFERS 6 requirements (present_and_nonempty, canonical_format, numeric_compare,
  matches_regex, unique_in_set, relation_compare). The 4 legacy (positive_number,
  numeric_min, numeric_min_conditional, fire_rating_ge) are NOT offered but the ENGINE
  still evaluates them (old YAMLs load + are preserved in the editor) — don't delete from
  `rules_engine`. The pattern family (matches_regex / not_matches_regex /
  matches_regex_if_present) is ONE "Khớp pattern" UI entry + two checkboxes (negate /
  skip-if-empty) mapped to the 3 engine keys on save — keep the 3 keys.
- **`inherit_then_normalize` = compound present-AND-format fix (v1.4-K22)** — a
  deterministic pipeline (empty → inherit `host.<param>` → `normalize_value`), so ONE
  `canonical_format` rule covers "must be present (inherit if empty) AND canonical". QC
  `_suggest` runs the pipeline; `query_specs._collect_params` flips `follow_host` for it
  too (alongside `inherit_from_host`). NO LLM — it's a value pipeline. Rule Builder shows
  it as a "➕ Kế thừa từ host khi trống" checkbox under normalize (not a separate handle).
- **Reference-data check = membership in an authoritative list (v1.4-K21, tiers 1–2)** —
  `normalize_kind="reference"` + `autofill.normalize_reference="<set>"` cites
  `config/reference.<set>.yaml` (`entries[].canonical`+`aliases`, `case_sensitive`).
  `policies/reference.py`: `load_reference` (cached by path) + `ReferenceSet.match` —
  Tier 1 exact→compliant, Tier 2 alias/`_slug`/case→deterministic auto-fix, Tier 3
  fuzzy→None→Path A (**Phase 2**, never guess). Reuses K12 `canonical_format` so check
  + fix can't drift; QC `_suggest` owns the I/O (`self._config_dir` = the rules file's
  dir). DesignAgent/approval/K18 unchanged. Keep `map` (inline) AND `reference` (shared
  file) — don't fold one into the other. Design notes for this live in the private working repo.
- **`inherit_from_host` copies a host param down (v1.4-K20)** — the WRITE-side
  partner to the existing host read (`revit_query._apply_host_hop`→`host.<param>`)
  and `relation_compare`. `autofill.strategy="inherit_from_host"` (+ optional
  `host_param`, default = the rule's own parameter) → QC `_suggest` returns
  `element.params["host.<param>"]`; blank/absent → None → Path A (NEVER invent).
  `query_specs._collect_params` flips `follow_host` from this autofill alone — no
  need to also set a `host.*` `other_param`. This is the *deterministic* inherit;
  fuzzy/semantic host mapping stays Phase 2. Don't fold it into compose_template
  (dotted+spaced `host.Fire Rating` tokens don't parse cleanly). **Type-write
  conflict = MAX, SCOPED:** N doors of one Type in walls of differing ratings
  collapse to one Type write — `design._collapse_to_one` picks the **maximum**
  candidate (`parse_to_minutes`) **only when every candidate is a fire-rating
  value**, stamping `host_conflict` so the proposal is honest. ANY OTHER
  type-param conflict → **first-win** (pre-K20 behaviour); instance params never
  collapse (unique write target). **Why max (owner decision):** the
  param carries the rating the CODE REQUIRES, not a certified capability, so of
  the two wrong answers the min is the dangerous one — it leaves the
  higher-requirement instances declaring LESS than their host demands and a
  present/canonical rule passes them forever; the max over-states for the lower
  host, which is visible and safe. True per-instance write is deferred to Phase
  2; don't broaden magnitude-collapse beyond fire-rating or drop the note.
  **Two belts keep garbage out, keep BOTH (C-01):** `_conflict_sort_key` puts
  unparseable values in group **`-1`** (BELOW every rating) — deliberately, so
  "garbage never wins" holds under `min` AND `max` and a future reducer flip
  can't re-open the hole; and `_collapse_to_one` only admits candidates
  `parse_to_minutes()` accepts, because the `_looks_like_fire_rating` screen is
  SKIPPED for rules declaring `compare_kind`/`normalize_kind: fire_rating`.
  Nothing parseable → first-win, don't invent an ordering. The reducer's own
  flip (min→max) shipped a proposal body still saying "minimum" — when you
  change a write policy, **assert the RENDERED markdown**, not just the dict.
- **A CLI flag is only real once `_dispatch` forwards it (L-01)** —
  `--fail-on-partial-coverage` was declared by argparse and honoured by
  `_exit_code_for`, both ends tested, yet inert on `--run`/`--run-revit` because
  the dispatch never passed it. `tests/test_orchestrator_cli.py` now pins the
  wire for every run mode plus a table-coverage test that FAILS when a new mode
  declares `fail_on_partial_coverage` without being wired — if that test breaks,
  wire the mode, don't edit the table.
- **missing_data routes by value-availability (v1.4-K4)** — a missing_data
  finding with a computable `suggested_value` → Path B write; without → Path A
  ACC Issue. Don't restore the old "missing_data never becomes an issue" rule.
- **Deterministic autofills auto-apply (v1.4-K4 Opt B)** — `compose_template` /
  `normalize` produce exact values → DesignAgent treats them as autonomy `auto`
  regardless of severity; heuristic (`infer_*`) stays severity-gated.
- **Path B writes batch into ONE revit_batch = one undo (v1.4-K4)** — the addin
  now exposes batch on a **separate route `POST /mcp/batch`** (v1.4-K22.3);
  `RevitHTTPClient.batch()` posts there (body `{steps, dryRun}` → `{ok, committed,
  results}`). KEEP the per-element fallback (`unknown_command`/404 → write
  individually) as a defensive net for older addins. Native room containment is
  available too (`RevitHTTPClient.get_element_rooms` → `room`/`fromRoom`/`toRoom`),
  a future swap for the geometry-bbox `_enrich_containing_space`.
- **Type-level params write the TYPE (v1.4-K5)** — `remediation.target: type`
  resolves `_type_id` and dedups writes per (type, param). Fire Rating etc. are
  type params; instance writes fail `not_found`.
- **Geometry findings ride a shared bucket (v1.4-K7)** — `OrchestratorState.
  geometry_findings` (NOT `findings`, so the rules_engine / route never see them).
  DesignAgent folds them into Path A on iteration 0. (Grouping is now by
  `(rule, status)` — v1.4-K18, see above — NOT per element.) Don't reintroduce the
  separate geometry design pass.
- **Audit axes ride the SAME K7 bucket, one-shot BEFORE the graph (v1.6-P3, D5)** —
  `--audit <profile>` runs lod-validator + spatial-qc (each in its OWN Python 3.10
  venv via stdio MCP — `mcp_clients/lod_validator.py`/`spatial_qc.py`, NEVER a
  library import) and seeds `geometry_findings`; `run_revit` EXTENDS the seed so
  IFC axes + model-checked geometry rules coexist. Profile schema =
  `policies/audit_profile.py` (relative paths resolve profile-dir-first); satellite
  paths = `config/audit_services.yaml` (gitignored; missing → axis "skipped:
  unconfigured", NEVER fatal — the LOI axis must run on a machine with no
  satellites). Artifacts land in `runs/<id>/axes/` via the `on_folder` hook; the
  reports' "Audit axes" section renders FROM those saved envelopes
  (renders-never-re-derives applies here too). Axis findings use the
  GeometricQueryAgent Finding shape: text in `message` (there is no `details`
  key), severity values `severity_*`, IFC guid in extra key `ifc_guid`.
- **AuditHub service is orchestrate-only + single-run (v1.6-P3, D6/D7)** —
  `service/` (FastAPI 127.0.0.1:8601, entry `autoaudit-service`, extras group
  `service`) holds ZERO business logic: POST /audits → `orchestrator.audit`;
  approvals apply ONLY via `ApprovalWatcher.scan_once` (v1.5-R2 invariant). One
  audit at a time **per service instance** — in-process lock +
  `runs/.service_lock` file holding `pid start_time` (the start time is what
  makes a recycled PID detectable; a legacy bare-PID file still parses).
  SCOPE, say it plainly (L-07): only paths THROUGH the service take this lock
  — POST /audits, approvals apply-once, verification views. A CLI command
  (`--run-revit`, `--audit`, `--watch-approvals`) touches the same Revit
  document but does NOT take it; it calls `_warn_if_service_busy` and prints a
  warning instead. Deliberate: a CLI process dying without releasing a lock
  would jam the nightly audit behind a stale file with nobody watching, which
  is worse than the inconsistent read it would prevent. Do not "fix" this by
  making the CLI block. SSE progress = structlog tap (`logging_setup.
  set_service_tap`, same contextvar pattern as `trace_processor`) — do NOT add
  progress callbacks into agents/graph. `POST /audits` returns `run_id: null`
  (fills once the run folder exists — never block on the axes). Test gotcha:
  `fastapi.testclient.TestClient` MUST be used as a context manager, or the
  background job task never progresses (suite-hang class).
- **Scheduled audit is propose-only by construction (v1.7-3bP1)** —
  `AuditRunOptions.propose_only` (profile-only field, no CLI flag) demotes
  every would-be-`auto` Path B decision to `approve` in
  `DesignAgent._prepare_revit_fix`, placed AFTER every decision branch so it
  also catches the K4 Opt B deterministic `compose_template` bypass (which
  skips `autonomy.yaml` entirely). Path A (raising an ACC issue) is
  unaffected — creating the issue IS the propose act. Only `audit()` sets it;
  bare `--run`/`--run-revit` default `False` (legacy unchanged). Apply still
  goes ONLY through `ApprovalWatcher` — this doesn't add a second apply path.
  Cross-run Path A dedup (`issue_registry.py`, keyed by
  `sha256(project|rule|bucket|sorted(element_ids))`) fails OPEN when the ACC
  liveness check (`forma.get_issue`) errors — same posture as the existing
  in-run dedup's refusal to suppress blindly. `delta_report.py` is
  render-from-disk ONLY (reuses `run_recorder.diff_outcomes`, never re-runs a
  check — same renders-never-re-derives rule as the verification report);
  baseline = newest earlier SUCCESSFUL run with the same identity
  (`profile.json.profile_name`, or `metadata.mode` when neither run has a
  profile) — don't compare across different profiles/modes. "Successful" =
  `delta_report.SUCCESSFUL_STATUSES = {"completed", "converged"}` (v1.7-3bP5
  post-ship fix: the graph modes — hence every `--audit` — record
  "converged", never "completed"; gating on "completed" alone made delta.md
  unreachable in the feature's own main path. Don't re-narrow it).
- **Rule Builder NL-intent belt: teach + guarantee (v1.7-3bP6/3bP7)** — the
  extraction prompt (`rule_builder_core.RB_EXTRACT_SYSTEM`) TEACHES intent
  (Layer 1), and `apply_nl_intents(nl, rule)` — pure/idempotent, called from
  `draft_rule`, NOT the `enforce_*` save chain (those lack the NL) —
  GUARANTEES two of them deterministically (Layer 2): quoted-literal duration
  format ('X HR' → "{h} HR") and empty+inherit → `inherit_then_normalize`.
  Same doctrine as `enforce_reference_membership` (K24): the LLM never holds
  the pen. Both corrections are verdict-safe by construction — don't add a
  correction here that could flip an existing verdict, and don't move the
  call into the enforce chain. A private black-box QA harness calls the SAME
  function post-parse (Mirror contract) — keep it import-safe.
  Prompt lesson (learned live, 4 iterations): haiku copies patterns from the
  prompt's own examples — compact CONTRASTING examples inline beat long
  conceptual guidance.
- **Unattended mode wraps dispatch, never owns Revit's lifecycle (v1.6-P3, P3-3;
  reordered v1.7-3bP2)** — `unattended.py:UnattendedSession` wraps `audit()`'s mode
  dispatch ONLY when `unattended.enabled AND services.revitcontrol.exists()`;
  enabled-but-unconfigured degrades to attended with a warning (same honesty rule as
  "axis skipped"), never a crash. The watchdog runs under RevitControl's OWN Python
  via `subprocess.Popen` (boundary held — no library import); `unknown_dialog_action:
  "halt"` stays (no blind-clicking). `__aexit__` terminates the watchdog but NEVER
  kills Revit (the user owns that — a test asserts no Revit process is killed).
  Port/token for `wait_addin_ready` resolve on THIS side
  (`revit._resolve_port`/`_load_auth_token`) and pass to `scripts/unattended_launch.py`
  via argv, so that helper (running under RevitControl's interpreter) never imports
  `bim_orchestrator`. `--doctor` is a FLAG in the mutually-exclusive mode group (the
  argparse has no subparsers). **Ordering is launch → watchdog → wait, not
  watchdog → launch+wait** (the "launch storm" fix): `scripts/unattended_launch.py`
  gained `--phase {launch,wait,all}` (default `all`, back-compat) so
  `UnattendedSession.__aenter__` can call `--phase launch` (is-running guard + launch,
  no wait) BEFORE spawning the watchdog — the watchdog's own no-revit.exe-yet
  relaunch tick must never race the initial launch decision — then spawn the
  watchdog (needed to dismiss the Unsigned Add-In modal), then block on `--phase
  wait`. A nonzero wait phase is fail-fast: terminates the just-spawned watchdog and
  raises `UnattendedLaunchError`, aborting the `--audit` dispatch rather than limping
  into `run`/`run_revit` and failing later at a stuck modal with no human present. A
  watchdog that exits immediately after spawn (exit code 3 = another instance already
  supervising) is not an error — `_watchdog_proc` is set to `None` so `__aexit__`
  skips terminating a process it didn't spawn. `__aexit__` also surfaces a
  watchdog-written `PAUSED` crash-loop flag via `log.error` — it needs no new artifact
  format since `PAUSED` already rides the existing `persist_unattended_dir` copytree.
- **Multi-scenario merge is a pure thin layer (v1.4-K6)** — `policies/rules_schema.
  merge_rulesets` combines N RuleSets (dedup by id, union target_category); a SINGLE
  input is identity (don't break this — the one-file path must stay unchanged).
  `QCAgent` accepts one path or a list; CLI `--rules` is `nargs="+"`. Don't push
  merge logic into the engine — it already handles a big RuleSet.
- **Proposal-issue body is self-explanatory, grouped by rule (v1.4-K5.1)** —
  `DesignAgent._build_proposal_description` states the rule + requirement +
  expected format ONCE per rule, then `<eid> | <original> → <proposed>` per
  element. Don't revert to the bare repeated `eid | param -> value` list; keep
  `_prepare_revit_fix` stashing `old_value` + `rule_id` on the fix preview.
- **Revit API is single-threaded** — the addin serialises every command on
  Revit's main thread. Client-side `asyncio` overlaps latency, not compute;
  fewer-fatter calls win (hence `--bulk-fields` = one `find_elements` vs N
  `get_element_info`). `--max-elements` (default 300) caps both paths.
- **Type fetches dedup in-flight (v1.4-K8)** — `revit_query._get_type` holds one
  `asyncio.Future` per `type_id` (`self._type_inflight`) so the concurrent
  hydration fan-out doesn't fire N `get_element_info` for the same type (300
  ducts / 3 types → 3 fetches, not 300). Don't drop the in-flight map or the
  `finally: fut.set_result(...)` — awaiters hang without it. Live-verified on
  R27: `--bulk-fields` + this = 1200 → 6 `get_element_info` for 300 ducts.
- **Lookup tables resolve a required value, not just membership (v1.4-K24)** —
  `Rule.lookup: <name>` on a `relation_compare` rule maps the related value
  (`other_param`, e.g. `host.Fire Rating`) through `config/lookup.<name>.yaml`
  (`policies/lookup_table.py:load_lookup` → `(required, exempt)`) BEFORE
  comparing. A row explicitly marked not-rated → `exempt=True` → outcome
  `exempt` (compliant-by-exemption; part of the report's PASS set, not a
  finding). No matching row → `manual_review` (never guessed). Keep `lookup`
  (code-table, keyed by a related element) distinct from `normalize_reference`
  (K21, membership in an approved set) — they solve different NL phrasings of
  "must be valid"; see `app._enforce_reference_membership` for the save-time
  disambiguation.
- **Approval-security: fingerprint gate + stale-value re-preview (110a27f,
 )** — hardens Loop 2 against a human editing the parked write-set,
  or the model drifting between propose and approve. `policies/
  approval_integrity.py:fingerprint(fixes)` is a stable SHA-256 over the
  canonical write-set (`element_id, action, parameter, new_value`, sorted,
  order-independent); `DesignAgent._create_proposal_issue` stamps it into BOTH
  the proposal issue body (marker `AutoAudit-Fingerprint: <hex>`) and the local
  record. Before applying, `ApprovalWatcher` recomputes the fingerprint from the
  record and compares it to the one in the fetched ACC issue — mismatch →
  `integrity_failed`, no write, issue left open. It then runs a **dry-run batch
  re-preview** (`_reprove_stale`): live value == `old_value` → apply; ==
  `new_value` → already satisfied (close, no write); a THIRD value → genuine
  drift → hold back as `stale`, issue stays open. Records predating the
  fingerprint (no anchor) still apply — back-compat; a fix without a captured
  `old_value` or an addin lacking batch fails **open** (fingerprint gate stays
  primary).
- **`GeometryRule.reference_link_hint` disambiguates same-discipline links
  (v1.4-K26)** — `reference_source: linked_mep` (etc.) now falls back through
  discipline keywords (MEP/HVAC/Mechanical/Plumbing/Electrical/…), not a
  literal `"MEP"` substring, but when several links of the SAME discipline are
  loaded (HVAC vs Plumbing vs Electrical all under `linked_mep`), set
  `reference_link_hint: <substring>` to name the exact link file. The link
  cache keys by the effective hint, so two `linked_mep` rules with different
  hints resolve and batch independently instead of collapsing.

- **LLM seam is OFF by default; the LLM never holds the pen — runtime agents
  now ship as a separate private extension (v1.5-R3)** — the 3
  agent CLASSES (Remediation / Diagnostic / Supervisor — prompts, JSON
  schemas, repair loop, batching, memoisation) were extracted out of this repo
  into an optional extension package so this core engine can be public
  without exposing that design. This repo keeps only the SOCKET:
  `llm/interfaces.py` (3 `Protocol`s pinning the exact call surface
  `design.py`/`graph.py` use) + `llm/factory.py` (env flags + shared
  client/budget plumbing + a LAZY import of the extension). Posture:
  * Flags unset (and no `client` injected) → `make_*_agent` returns `None`
    with **no import attempted at all** → graph identical to the
    deterministic loop (the full test suite runs offline, no extension
    required — get the live count with `uv run pytest --collect-only -q`).
  * A flag on (or a `client` injected, e.g. by a test) but the extension
    ISN'T installed → `llm.plugin_missing` warning + `None` → same
    deterministic degrade, never a crash.
  * A flag on AND the extension IS installed (editable, into the SAME venv —
    same posture as the `rules_extractor` sibling: `uv sync` is exact-sync and
    will remove the editable install again, so after the first
    `uv pip install -e <extension-package-path>` use
    `uv sync --extra dev --inexact`) → the socket delegates construction to
    the extension with the same args callers already pass.
  Invariants preserved regardless of where the classes live: every
  LLM-proposed value re-validates through the SAME `rules_engine` requirement
  that flagged it (validator built in `design._make_validator`); LLM fixes are
  NEVER autonomy `auto` (approve → proposal issue; `llm_safety_critical` →
  human-only → Path A issue with the suggested value, never applied);
  Supervisor may only convert continue→stop AFTER the fingerprint/
  max-iteration checks; call budget defaults to 200 when any flag is on
  (`BIM_LLM_MAX_CALLS` overrides; budget = LOGICAL calls). Don't wire an LLM
  into route_node, the rules engine, or the verification report. Test stubs
  for the socket (no extension needed) live in `tests/_llm_stubs.py`.
- **`bound_parameter` is honoured END-TO-END via `rules_schema.fetch_name` (v1.5-R2)** —
  siblings, `_suggest` (normalize/inherit), design write-target/old_value/dedup,
  and `query_specs` host-param collection ALL resolve the effective Revit name
  with `fetch_name(rule)` (bound over canonical). Never read `rule.parameter`
  directly for a value fetch/write — the read and write sides silently diverge
  for bound rules (the exact false-negative class the 2026-07 review caught).
- **`--apply-approved` is gone (v1.5-R2)** — it bypassed fingerprint/stale
  re-preview/lock. The ONLY approved-write path is the ApprovalWatcher
  (`--watch-approvals` / `--apply-approvals-once`). Don't re-add a direct
  apply command.
- **rules-lint (v1.5-R7)** — `--lint-rules` performs a static read/write
  footprint analysis (AWH-style) over a RuleSet; footprint extraction MUST
  use `fetch_name(rule)` (bound over canonical), same as every other
  read/write site. A new autofill/remediation construct that
  `policies/rules_lint.py:extract_footprint` doesn't recognise is treated as
  `analyzable=False` (fail-closed) — add it to `extract_footprint` when you
  add the construct, or it silently becomes an "unanalyzable" lint blind
  spot instead of a real check.

- **A behavior-changing PR carries its own record — in the SAME PR (adopted
  from the DeepSeek Harness review, their "Agent Notes MUST be in the
  same PR" rule)** — any PR that changes what the product does, promises, or
  prints also updates CHANGELOG.md in that PR, not in a later sweep. This was a
  habit here, and the habit slipped twice: the round-6 review (12 PRs) had no
  session-note entry until a catch-up found the gap, and the nightly reschedule
  left
  SCHEDULED_AUDIT.md + nightly_wrapper.ps1 saying 01:00 through TWO schedule
  changes. Mechanical/local edits (typo, comment, test-only refactor) are
  exempt.

- **An empty `except` NAMES what it swallows (same source)** — a
  bare `pass` under an except states, in a comment: exactly which failure is
  being swallowed, why swallowing is the correct behavior (not just
  convenient), and why nothing else can reach that handler. The three existing
  sites comply (`revit_query` areaMetric derive · `approval_watcher`
  _release_lock · `service/runner` emit-outside-loop) — copy their shape. This
  is the #37 lesson generalized: a swallowed error that can't be named is
  usually an error that shouldn't be swallowed.

- **The `--demo` transcript is a pinned surface (adopted from the
  DeepSeek Harness review)** — `tests/test_demo_transcript.py` runs the real
  CLI as a subprocess and diffs the normalized transcript against
  `tests/snapshots/demo_transcript.txt`. It exists for the wiring-bug class
  unit tests miss (L-01's inert flag, #37's summary crash, v1.7-R9's
  removed call — all had green tests on both ends). If it goes red after an
  INTENTIONAL surface change: `BIM_UPDATE_SNAPSHOT=1 uv run pytest
  tests/test_demo_transcript.py`, then review the snapshot diff in the PR
  like code — that review is the point. Keep the normalizer MINIMAL: only
  patterns proven nondeterministic by diffing two live runs (run id,
  timestamps, checkpoint date-path, durations, elapsed_ms/elements_per_sec).
  A new normalizer pattern without that evidence is a place for a regression
  to hide.

- **`.gitignore` does NOT tell you what is tracked — `git status` before any
  `rm -rf` inside the repo.** `runs/`, `checkpoints/` and `_runs_archive_*` are
  all listed in `.gitignore`, which makes them read as scratch you can delete
  freely. They have been **force-added** more than once (a session commits a
  batch of run artifacts as evidence). The moment that happens, the ignore rules
  say nothing about those files, and a routine artifact cleanup deletes another
  session's committed data. This is not hypothetical: `rm -rf runs checkpoints`
  removed **1048 tracked files** here mid-session, while another session had
  them committed. Caught before staging, so nothing was lost.
  * The net is `.githooks/pre-commit`, which blocks a commit staging bulk
    deletions (>10 under the artifact dirs, >40 anywhere). Install it once per
    clone with **`sh .githooks/install.sh`**. Deliberate purge:
    `git commit --no-verify`.
  * **Do NOT use `git config core.hooksPath .githooks`.** That path resolves
    against each worktree's OWN root, so a linked worktree checked out at a
    commit from before `.githooks/` existed finds nothing and runs no hook —
    silently, with no sign the guard is off. Three of this repo's four
    worktrees were in that state, including both `.claude/worktrees/*` that
    other sessions work from. `install.sh` copies into `$GIT_DIR/hooks`, which
    resolves to the COMMON git dir for every worktree; verified by committing a
    bulk deletion from a throwaway worktree pinned to an old commit. The cost
    is that `.git/hooks` is not versioned, so re-run `install.sh` after editing
    a hook — `.githooks/` stays the source of truth.
  * The hook fires at COMMIT time, so it cannot stop the `rm -rf` itself — it
    only stops the damage becoming permanent. `git status` first is the actual
    defence.
  * It has no `set -e`, deliberately: under `set -e` a `[ x -gt y ] && …` line
    that evaluates false ends the script non-zero, which a hook reads as
    "block". The first version did that and rejected a 5-file deletion. A guard
    that fires when it should not is worse than no guard — people learn to pass
    `--no-verify` by reflex.
  * It reports on **stderr** and starts with `trap '' PIPE`. A hook writing to
    stdout dies of SIGPIPE as soon as anything closes its output early — even
    `git commit | head -20` — and never reaches its `exit 1`; git reads the
    signal death as success and the commit lands. That is how a test commit
    untracking 196 checkpoint files walked straight through this guard on the
    day it was added. **When verifying a hook, assert the commit count, not the
    message**: the block message printed perfectly while the commit succeeded.

- **Run `--demo` in the export dir, not in this checkout.** A `--demo` run
  rewrites `runs/trend.md`, `findings.json`, `review_queue.md` and
  `data_quality_report.md` at the package root. Those are tracked, so a few demo
  runs silently replace another session's real run history with demo rows — the
  same incident lost a day of real Snowdon trend rows that way, and it is
  invisible to the deletion hook because it is a MODIFICATION, not a deletion.
  Do the demo acceptance in `$PUB` (the throwaway snapshot), and if you must run
  it here, `git checkout -- bim-orchestrator/runs bim-orchestrator/findings.json
  bim-orchestrator/review_queue.md bim-orchestrator/data_quality_report.md`
  afterwards.

## Two json_to_yaml scripts — pick the right one

- `extraction-skills/scripts/json_to_yaml.py` — **canonical** (D0
  skill-pack converter; heuristic warnings; execution_status split).
  Both Streamlit Rule Builder + Setup tab call this.
- `scripts/json_to_yaml.py` — **deprecated** (kept for the legacy
  fixture + existing `tests/test_json_to_yaml.py` exit-code contract).
  Prints a stderr banner on CLI invocation.

## Tooling + commands

```powershell
# from bim-orchestrator/ — Windows venv layout
uv sync --extra dev
.venv\Scripts\bim-orchestrator.exe --hello              # smoke
uv run streamlit run streamlit_app\app.py               # launches UI on :8501
uv run pytest                                            # ~20s wall time
```

PowerShell encoding gotcha: set `$env:PYTHONIOENCODING = "utf-8"` if
your script prints emoji / Vietnamese (else cp1252 crash).

PDF→rule extraction (`rules_extractor`, sibling repo): `uv pip install -e
<path-to>/ExtractionAgents`. GOTCHA: `uv sync` is exact-sync and WILL
remove it again — once installed, use `uv sync --extra dev --inexact`.

## Test posture + acceptance bar

Don't hardcode the test count here — it drifts (this framework's own lesson:
CLAUDE.md once said "870 tests" when the real count was 1223). Get the live
number with `uv run pytest --collect-only -q` (last line: "N tests
collected") from `bim-orchestrator/`. Any change that lowers this count
without explicit justification is a regression; see `CHANGELOG.md` for the
count at each release. Use `uv run pytest --tb=short` for failures.

## Deferred work — don't forget these

- **Approval resume loop** — ✅ RESOLVED (v1.4-K5): ACC status-watcher
  (`ApprovalWatcher` + `--watch-approvals`); approve-gated Path B → proposal
  issue → human sets "In progress" → watcher applies + closes. Live-verified
  (Issue #88, 7 door-type Fire Rating normalizations). Future: webhook instead
  of poll; the `normalize` strategy can grow more `normalize_kind`s.
- **LLMQueryAgent (`--ask`)** — ✅ RESOLVED (F5 removed; not in compliance flow).

## When in doubt

- Look at the latest 2-3 commits — the F-series (F1..F4) carries lots
  of structural lessons.
- `docs/ARCHITECTURE.md` has the canonical diagrams + responsibility
  matrix.
