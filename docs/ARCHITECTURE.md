# bim-orchestrator — Architecture Overview

> **Test posture:** the live count is whatever CI reports — run
> `uv run pytest -q` in `bim-orchestrator/` (or read the badge in
> [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)). Hard-coded totals
> here drifted repeatedly (870 → 1048 → 1248 → …), so this header no longer
> carries one; treat any version-stamped count below as a historical snapshot,
> not current state.
>
> **Recent snapshots (historical — do not treat as current):**
> - Verification report module — QC `check_trace` (incl. the PASS set) →
>   requirement-type verify-recipe registry → `verification_report.md` with
>   native Revit/ACC re-check recipes; plus the opt-in auto-created schedules
>   (`--create-verification-views`) and docx/pdf export. See section 9.
> - 2026-07 review remediation (Gate 0/1) — write-side truth: graph-failed and
>   never-audited runs now exit non-zero (`_exit_code_for` + `query_coverage`),
>   Rule Builder PUT is the final validation authority, `bound_parameter` honoured
>   across every write/read site, audit provenance stamps the Git commit.
>
> **Packaging:** both MCP servers run Node-free at the host — Revit via
> `RevitHTTPClient` (HTTP-direct to the C# addin) and Forma via the SEA
> `forma-mcp.exe`. See [`bim-orchestrator/docs/PRODUCTION_PACKAGING.md`](../bim-orchestrator/docs/PRODUCTION_PACKAGING.md).
>
> **Python:** floor is **3.12** (`pyproject.toml` `requires-python = ">=3.12"`,
> mypy target `3.12`); the dev `.venv` on this machine runs **3.14** (`uv run
> python --version`). Code should stay 3.12-compatible; don't rely on 3.13+-only
> stdlib features without checking the floor.
>
> Extraction workflow: [`bim-orchestrator/extraction-skills/README.md`](../bim-orchestrator/extraction-skills/README.md)
> Release detail lives in [`bim-orchestrator/CHANGELOG.md`](../bim-orchestrator/CHANGELOG.md).

## 1. System topology — 3 tiers

> **Note:** the diagram below shows the original **stdio subprocess**
> topology (Node bridges for both MCP servers) — this is now the LEGACY mode. The
> production default is now **Node-free**: `RevitHTTPClient` talks to
> the C# addin directly over HTTP (`localhost:7891`/`7892`, no Node bridge), and
> `acc-forma-mcp-server` ships as the standalone SEA `forma-mcp.exe` (still spoken
> to over stdio, but as a single fetched exe, not a source checkout + `npm`). See
> [`bim-orchestrator/docs/PRODUCTION_PACKAGING.md`](../bim-orchestrator/docs/PRODUCTION_PACKAGING.md).
> The stdio-to-a-Node-source-checkout path (`RevitMCPServer` Node bridge shown
> below) still works and is exercised by `mcp_clients/revit.py`'s stdio path, but
> is no longer the default deploy.

```
┌────────────────────────────────────────────────────────────────────────┐
│                          EXTERNAL SYSTEMS                               │
├────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   ┌──────────────────┐    ┌──────────────────┐    ┌─────────────────┐ │
│   │  Autodesk Cloud  │    │   Revit 2026     │    │  PDF corpora    │ │
│   │  (ACC + APS API) │    │   + Snowdon .rvt │    │  BEP / IBC §7   │ │
│   │  hub b.5341...   │    │   54 rooms       │    │  (synthetic     │ │
│   │  project b.57de..│    │   1128 walls     │    │   fixtures)     │ │
│   │                  │    │   142 doors      │    │                 │ │
│   └────────┬─────────┘    └────────┬─────────┘    └────────┬────────┘ │
│            │ HTTPS                  │ HTTP(localhost:7891)  │ disk     │
└────────────┼────────────────────────┼───────────────────────┼──────────┘
             │                        │                       │
┌────────────▼────────────────────────▼───────────────────────▼──────────┐
│                    MCP SUBPROCESSES (stdio bridges)                     │
├────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   acc-forma-mcp-server         RevitMCPServer (Node)    chromadb       │
│   (Node + SSA JWT)             + RevitMCPAddin (C#)     (in-process)   │
│                                                                         │
│   30 tools                     61 tools                  RAG vector    │
│    • dm_list_hubs               • revit_list_elements    store         │
│    • dm_list_projects           • revit_get_element_info • ingest_text │
│    • issues_create (2-call)     • revit_set_parameter    • search      │
│    • issues_list_types          • revit_rename_element   • hit@k eval  │
│    • aecdm_query_elements       • revit_list_rooms                     │
│    • meta_verify_audit_chain    • revit_ping             stored in     │
│                                                          .chroma/      │
└────────────┬───────────────────────────┬───────────────────────────────┘
             │ MCP stdio (JSON-RPC)      │ MCP stdio
             │ (Python anyio)            │
┌────────────▼───────────────────────────▼───────────────────────────────┐
│                                                                         │
│              bim-orchestrator (Python 3.14, LangGraph)                  │
│                                                                         │
│   FormaMCPClient ←─── async stdio_client wrapper ─── RevitMCPClient     │
│   (mcp_clients/forma.py)                            (mcp_clients/revit) │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key boundary:** Agents *never* talk to ACC or Revit directly. Always through MCP
clients. This enables full mock-layer parity for tests.

---

## 2. Orchestrator role — split across 3 layers

There is **no single class named `Orchestrator`** in this project. The role is
deliberately split into 3 layers, each with one responsibility (SRP). Knowing
which layer owns what keeps refactors honest — e.g. don't push convergence logic
into a worker agent, don't push CLI parsing into the graph.

```
┌───────────────────────────────────────────────────────────────────┐
│  LAYER 1: SESSION DRIVER  (orchestrator.py)                        │
│                                                                    │
│  Responsibilities:                                                 │
│   • Parse CLI args (--run-revit, --rules, --limit, ...)            │
│   • Load configs (rules YAML, autonomy YAML, .env)                 │
│   • MCP lifecycle:                                                 │
│       async with FormaMCPClient(forma_config) as forma_client:    │
│         async with RevitMCPClient(revit_config) as revit_client:  │
│           ...                                                      │
│   • Construct agents w/ injected dependencies:                     │
│       query = RevitElementsQueryAgent(revit_client, specs)        │
│       qc = QCAgent(rules_path, autonomy)                          │
│       design = DesignAgent(forma_client, autonomy,                │
│                            project_id, revit_mcp=revit_client,    │
│                            rules=qc.rules)                        │
│   • app = build_graph(query, qc, design, grounding_agent=...)     │
│   • state = await app.ainvoke(initial_state)                       │
│   • Print summary, write findings.json, return exit code           │
│                                                                    │
│  Functions: main(), check(), apply(), run(), run_revit()           │
│  Entry point: bim_orchestrator.cli:main (shim) → orchestrator.main │
└──────────────────────────┬─────────────────────────────────────────┘
                           │
                           │  app = build_graph(...)
                           ▼
┌───────────────────────────────────────────────────────────────────┐
│  LAYER 2: GRAPH SUPERVISOR  (graph.py + LangGraph runtime)         │
│                                                                    │
│  Responsibilities:                                                 │
│   • Topology definition (StateGraph wiring)                        │
│       START → query → qc → grounding → route                       │
│       route → (designing: design → bump → query) | (end: END)      │
│   • Control-flow decisions (route_node) — deterministic, NO LLM    │
│       iteration==0        → "designing"                            │
│       findings==0         → "converged" (zero_findings)            │
│       fingerprint==prev   → "converged" (fingerprint_stable)       │
│       iter >= max         → "failed"    (iteration_cap)            │
│     Convergence compares the CONTENT fingerprint of the finding    │
│     set, not its COUNT — fixing A while revealing B keeps the      │
│     count equal but is not a fixpoint.                             │
│   • State merging between nodes (dict update semantics)            │
│   • Cycle safety (max_iterations)                                  │
│   • Checkpoint writing (bump_node → JSON snapshot)                 │
│                                                                    │
│  LangGraph calls this the "supervisor pattern" — but here it's     │
│  implemented as hand-coded conditional edges, not LLM-driven       │
│  routing. Deterministic, cheap, easy to test.                      │
└──────────────────────────┬─────────────────────────────────────────┘
                           │
                           │  Each node: async fn(state) → dict
                           ▼
┌───────────────────────────────────────────────────────────────────┐
│  LAYER 3: AGENTS (workers — NO orchestration logic)                │
│                                                                    │
│   QueryAgent / RevitQueryAgent / RevitElementsQueryAgent           │
│   QCAgent                                                          │
│   GroundingAgent                                                   │
│   DesignAgent                                                      │
│                                                                    │
│  Each agent is a pure async function: state in → partial state out │
│  Agents do NOT know about graph topology, do NOT know the next     │
│  node, do NOT invoke each other directly.                          │
└────────────────────────────────────────────────────────────────────┘
```

### Responsibility matrix

Multi-agent literature names three classic orchestrator duties. Here is where
each lives:

| Orchestrator duty | Lives in |
|---|---|
| **Lifecycle / resource mgmt** (MCP connections, configs, output) | `orchestrator.py` (Layer 1) |
| **Control flow** (next-agent-to-call decision) | `route_node` in `graph.py` (Layer 2) |
| **State management** (pass intermediate results between agents) | LangGraph runtime + `OrchestratorState` TypedDict (Layer 2) |

### Why the split — comparison with alternatives

**Pattern A: "Big God Orchestrator" (NOT used)**

```python
class BimOrchestrator:
    def run(self):
        elements = self.query_agent.run(...)
        findings = self.qc_agent.run(elements)
        findings = self.grounding_agent.run(findings)
        while not self.converged(findings):
            fixes = self.design_agent.run(findings)
            elements = self.query_agent.run(...)  # re-query
            findings = self.qc_agent.run(elements)
        return findings
```

Problem: orchestrator holds state + knows the sequence + knows convergence —
violates SRP, hard to test, hard to change topology.

**Pattern B: "LLM-driven Supervisor" (allowed, not used)**

```python
# A "supervisor agent" that asks an LLM which sub-agent to call next
supervisor = SupervisorAgent(llm=claude, sub_agents=[query, qc, design])
# At each step, LLM looks at state and picks next agent
```

Useful when task structure is not deterministic. This project has a clear
structure (query→qc→grounding→route), so the deterministic `route_node` is a
better fit (cheaper, predictable, easier to test).

**Pattern C: "Graph Supervisor" (THIS PROJECT)** ✅

LangGraph's StateGraph — fixed topology, `route_node` is plain code (no LLM),
agents are pure workers. This is the canonical LangGraph pattern.

### Practical refactor guidance

If you find yourself wanting to:

* Add a new convergence rule → **Layer 2** (`route_node`), not Layer 3.
* Add a new MCP client → **Layer 1** (orchestrator.py `*_config = ...` + `async with`).
* Add a new agent → **Layer 3**. Then wire it in via `build_graph(...)` (Layer 2)
  and instantiate it in the relevant run-mode function (Layer 1).
* Add a new run mode (e.g. `--validate-only`) → **Layer 1**, mirroring the
  existing `check()` / `apply()` / `run_revit()` functions.
* Add LLM-driven routing for some scenarios → **Layer 2**: keep `route_node`
  deterministic for the default flow, add a separate node that calls an LLM
  and emits a "next_phase" status the route_node honors.

---

## 2.5. End-to-end pipeline — from BEP PDF to executed fix

The orchestrator runtime (Sections 3–5) is the **back half** of the pipeline.
The front half is the **extraction workflow** that turns a compliance source
into the YAML the runtime consumes.

```
┌────────────────────────────────────────────────────────────────────────┐
│  EXTRACTION (offline, human-driven today; API-driven in-product)        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  BEP / IBC / spec PDF                                                   │
│         │                                                               │
│         │ attach + prompt                                                │
│         ▼                                                               │
│  ┌─────────────────────────────────────┐                                │
│  │  Claude Desktop                      │   ← Sonnet 4.7 recommended    │
│  │  + extraction_prompt.md (skill)      │   ← inline OST catalog,       │
│  │  + rule_schema.json   (tool-use JSON │     atomicity + unit + scope  │
│  │    schema)                           │     lessons, top-4 silent bugs│
│  └────────────┬─────────────────────────┘                                │
│               │ JSON envelope ({scenario, target_category, rules[]})    │
│               ▼                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  extraction-skills/scripts/json_to_yaml.py                       │    │
│  │   • Pydantic validation against policies/rules_schema.py         │    │
│  │   • _heuristic_warnings()      → unit-missing, scope-filter      │    │
│  │   • _fragmentation_warnings()  → (param, requirement, category)  │    │
│  │                                  duplicates across rules         │    │
│  │   • _split_by_status()         → executable | review | invalid   │    │
│  │   • OSTCatalog.find() normalises category casing                 │    │
│  │   • confidence < 0.75 → force fixability=manual + requires_human │    │
│  └────────────┬───────────────────────────────────┬────────────────┘    │
│               │ executable                         │ review + invalid    │
│               ▼                                    ▼                     │
│  config/rules.<scenario>.yaml         runs/extraction_review_<ts>.md    │
│  + extraction-skills/cleaned/<sc>.json                                  │
└──────────────────────┬──────────────────────────────────────────────────┘
                       │
                       │ same YAML loaded by QCAgent.__init__()
                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│  RUNTIME (orchestrator graph — Sections 3+)                             │
├────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  RuleSet (pydantic) ──► derive_specs(rules, backend, catalog)           │
│                          │                                              │
│                          ▼                                              │
│            QueryAgent / RevitQueryAgent  ──► MCP fetch                  │
│                          │                                              │
│                          ▼                                              │
│            QCAgent  ──► convert_to_rule_unit + evaluate                 │
│                          │                                              │
│                          ▼                                              │
│            DesignAgent  ──► Path A (Issue) | Path B (set_parameter,     │
│                                              rename_element)            │
└────────────────────────────────────────────────────────────────────────┘
```

**Human-in-the-loop alternative — Streamlit Extraction Review tab.** When
Claude's first pass has heuristic warnings or fragmented duplicates, the
BIM Manager opens the **🔎 Extraction Review** tab in the
Streamlit app, pastes/uploads the JSON, edits in place, re-validates
live, and clicks **Save & generate YAML** — which writes both the
cleaned JSON (for re-editing later) and the executable rules YAML
atomically. The tab imports `json_to_yaml.py` via `importlib` so the
CLI and UI share one validation implementation.

### Why an extraction stage at all?

Hand-authoring YAML for every BEP scenario doesn't scale — a single
project standard can produce 30+ atomic rules, and the canonical
parameter names, requirement enums, OST catalog labels, and unit
conventions are easy to get wrong. The skill pack puts the rules
schema in the LLM's context (literal JSON Schema as tool-use input)
and lets the mechanical validator catch the rest. This is the
"prompt + mechanical guardrails" pattern: prompt engineering has a
ceiling, so a deterministic post-processor mops up.

### Skill-pack assets (single source of truth)

| Asset | Generated from | Purpose |
|---|---|---|
| `extraction-skills/rule_schema.json` | `policies/rules_schema.py` (pydantic → JSON Schema) | Tool-use binding for Claude |
| `extraction-skills/ost_catalog_keys.txt` | `config/ost_catalog.yaml` | Flat list — Claude can't invent categories |
| `extraction-skills/extraction_prompt.md` | hand-written, iterated D0.0 → D0.7 | System prompt with inline OST catalog, top-4 silent bugs, atomicity / unit / scope lessons, 2 worked examples |
| `extraction-skills/examples/01..06_*.md` | hand-written | Full BEP-excerpt → JSON output pairs |
| `extraction-skills/scripts/json_to_yaml.py` | hand-written | Post-processor + heuristic validator |

Regenerate the first two with `scripts/_generate_schema.py` and
`scripts/_generate_catalog_keys.py` after editing the pydantic models
or the catalog.

---

## 3. Agent execution flow — LangGraph cyclic graph

```
                            START
                              │
                              ▼
                       ┌──────────────┐
                       │  query node  │  Reads elements from MCP →
                       │              │  state["elements"]
                       └──────┬───────┘
                              │
                2 implementations (chosen per CLI flag), both
                rules-driven via derive_specs(rules,
                backend, catalog):
                              │
   ┌──────────────────────────┼──────────────────────────────┐
   │                                                          │
   ▼                                                          ▼
┌─────────────────────────────────┐    ┌──────────────────────────────────────┐
│ QueryAgent  (Forma path)        │    │ RevitQueryAgent  (unified)           │
│                                 │    │                                      │
│ derive_specs → per category:    │    │ derive_specs → per category:         │
│   • backend_category =          │    │   • backend_category = OST_<name>    │
│     catalog.aecdm_label         │    │   • params = rule union              │
│   • params = rule union         │    │   • follow_host = any host.* ref     │
│                                 │    │                                      │
│ Forma MCP →                     │    │ Revit MCP →                          │
│ aecdm_query_elements per cat    │    │ list_elements per cat                │
│ stamp element["category"]       │    │   + get_element_info (instance)      │
│ flatten properties → params     │    │   + TYPE info cache per typeId       │
│                                 │    │   + host hop when follow_host=True   │
│ Used by --check / --apply /     │    │                                      │
│ --run on ACC models             │    │ Param precedence: Instance > Type.   │
│                                 │    │ ``type.<name>`` alias surfaces the   │
│                                 │    │ Type value explicitly for forced     │
│                                 │    │ reads.                               │
│                                 │    │                                      │
│                                 │    │ Used by --run-revit on local .rvt   │
│                                 │    │ via RevitMCPServer + Addin.          │
└─────────────────────────────────┘    └──────────────────────────────────────┘

   A third implementation handles geometry rules:

   ┌──────────────────────────────────────────────────────────────────────┐
   │ GeometricQueryAgent  (geometry path)                                  │
   │                                                                       │
   │ Dispatches clearance_min / clearance_max GeometryRules via            │
   │ revit_check_clearance. Optimised:                                     │
   │   • asyncio.gather — all batch groups + clearance_max run concurrently │
   │   • _ClearanceKey batching — rules sharing                            │
   │     (setA, setB, link, axis, direction, view) collapse to ONE call    │
   │     (Z: threshold = max over group, per-rule thresholds re-applied    │
   │      in Py. bbox reports no distance → its threshold is IN the key)   │
   │   • _dedup_by_element — findings on the same element_id merge into one │
   │     Finding (worst severity wins) → ≤1 ACC Issue per physical element │
   │   • view_id auto-resolution (constructor → active 3D → named 3D →     │
   │     first 3D); _max_elements/_max_clashes cap the loaded/returned set │
   │ Note: the Revit API is single-threaded — asyncio overlaps latency,    │
   │ not Revit-side compute. The real lever is fewer-fatter calls (P1).    │
   └──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                       ┌──────────────┐
                       │   qc node    │  QCAgent.run():
                       │              │   • RuleSet already loaded in __init__
                       │              │   • target_category filter (str | list)
                       │              │   • Pre-compute siblings_by_rule for
                       │              │     unique_in_set rules (honours
                       │              │     per-rule category filter)
                       │              │   • Iterate elements × rules:
                       │              │       raw = element.params[rule.parameter]
                       │              │       value = convert_to_rule_unit(
                       │              │           raw, rule.parameter, rule.unit
                       │              │       )           ◄─ unit conversion
                       │              │       passed = evaluate(
                       │              │           rule.requirement, value,
                       │              │           pattern, threshold,
                       │              │           condition_value, when_pattern,
                       │              │           siblings, other_value)
                       │              │   • 4-bucket classify per (elem, rule):
                       │              │       missing_data  (raw is None/blank)
                       │              │       manual_review (requires_human=T)
                       │              │       non_compliant (failed eval)
                       │              │       compliant     (passed)
                       │              │     severity from autonomy.severity_rules
                       │              │
                       │              │  state["findings"]             non_compliant
                       │              │  state["manual_review_items"]  borderline
                       │              │  state["missing_data_items"]   data-quality
                       │              │  state["outcomes_summary"]     4-count
                       └──────┬───────┘
                              │
                              ▼
                       ┌──────────────┐
                       │  grounding   │  GroundingAgent (optional):
                       │   node       │   • For each finding, query VectorStore
                       │              │   • Attach citation_refs per rule's
                       │              │     CitationPolicy
                       │              │   • Hard mode: missing → flag
                       │              │     citation_missing OR downgrade severity
                       │              │
                       │              │  state["findings"][i]["citation_refs"]
                       └──────┬───────┘
                              │
                              ▼
                       ┌──────────────┐
                       │  route node  │  Convergence check:
                       │              │   if iteration == 0:    → designing
                       │              │   if findings == 0:     → converged
                       │              │   if fingerprint(curr)  → converged
                       │              │      == fingerprint(prev) (stable set)
                       │              │   if iter >= max_iter:  → failed
                       │              │   else:                  → designing
                       └──────┬───────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
   converged/failed                       designing
              │                               │
              ▼                               ▼
            END                       ┌──────────────┐
                                      │  design node │  DesignAgent.run()
                                      └──────┬───────┘  ── see Section 4 ──
                                             │
                                             ▼
                                      ┌──────────────┐
                                      │   bump node  │  iteration += 1
                                      │              │  prev_finding_count = curr
                                      │              │  Write JSON checkpoint
                                      └──────┬───────┘
                                             │
                                             └─► back to query (loop)
```

**Subtle:** route returns `designing` always at iteration 0 (DesignAgent *always*
runs once). Convergence check only kicks in from iteration 1 onward. This is
gotcha #5 in Handoff §12.

---

## 4. Data flow through OrchestratorState

```
OrchestratorState (TypedDict) — flows through every node; each node merges fields:

┌─────────────────────────────────────────────────────────────────┐
│  project_id          str                                        │
│  iteration           int                                        │
│  max_iterations      int                                        │
│  prev_finding_count  int  (set by bump node)                    │
│  status              "init"|"querying"|"checking"|"designing"|  │
│                      "converged"|"failed"                       │
│  error               str | None                                 │
│                                                                  │
│  elements            list[Element]                              │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ {                                                       │    │
│  │   "id": "631418",                                       │    │
│  │   "name": "36\" x 84\" (180 MIN)",                      │    │
│  │   "category": "Doors",                                  │    │
│  │   "params": {                                           │    │
│  │     "Fire Rating": "180 MIN",       ◄─ from Type        │    │
│  │     "host.Fire Rating": "4 HR",     ◄─ host hop (W7)   │    │
│  │     "Mark": "S10",                                      │    │
│  │     "Host Id": 631165,                                  │    │
│  │     "_type_id": "2162722",                              │    │
│  │     ...                                                 │    │
│  │   }                                                     │    │
│  │ }                                                       │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                  │
│  findings            list[Finding]                              │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ {                                                       │    │
│  │   "rule_id": "door.fire_rating.matches_host",           │    │
│  │   "element_id": "631418",                               │    │
│  │   "parameter": "Fire Rating",                           │    │
│  │   "severity": "severity_high",                          │    │
│  │   "message": "...",                                     │    │
│  │   "suggested_value": None,    ◄─ for path B autofill   │    │
│  │   "citation_refs": [{source, section, score, snippet}], │    │
│  │   "citation_missing": False                             │    │
│  │ }                                                       │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                  │
│  proposed_fixes      list[ProposedFix]                          │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ {                                                       │    │
│  │   "finding_id": "<rule>::<elem>",                       │    │
│  │   "element_id": "631418",                               │    │
│  │   "parameter": "Fire Rating",                           │    │
│  │   "new_value": "240 MIN",                               │    │
│  │   "autonomy": "auto"|"approve"|"human-only",            │    │
│  │   "approval_token": "appr_...",  ◄─ Forma only          │    │
│  │   "preview": {                                          │    │
│  │     "executed_issue": {...}      ◄─ Path A success      │    │
│  │     "executed_changes": {        ◄─ Path B success      │    │
│  │       "before": "180 MIN",                              │    │
│  │       "after":  "240 MIN"                               │    │
│  │     },                                                  │    │
│  │     "executed_comments": {...}                          │    │
│  │   },                                                    │    │
│  │   "executed": True                                      │    │
│  │ }                                                       │    │
│  └────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. Fixability dispatch — Path A vs Path B in DesignAgent

```
              ┌─────────────────────────────────┐
              │   DesignAgent.run(state)         │
              └────────────┬────────────────────┘
                           ▼
              ┌─────────────────────────────────┐
              │ _apply_rule_filter (--rule)      │  no slicing here
              └────────────┬────────────────────┘
                           ▼
              ┌─────────────────────────────────┐
              │ _partition by fixability         │  auto→path_b, else→path_a
              └──────┬──────────────────┬────────┘
                     ▼                  ▼
         ┌──────────────────┐   ┌──────────────────────────────┐
         │ path_b (FIRST)   │   │ path_a (manual / no value)   │
         │ _dedup_by_write_ │   │                              │
         │ target (rule_id, │   │ group by (rule, status)      │
         │  write_eid,param)│   │   → _propose_rule_group:     │
         │ [+min conflict]  │   │   ONE ACC issue per problem, │
         └──────┬───────────┘   │   lists ALL elements         │
                ▼               │   (id | name | current)      │
   ┌──────────────────────────┐│ budget = max_issues − (#per- │
   │ _effective_remediation:  ││           rule proposals)    │
   │  resolve target="auto"   │└──────────────┬───────────────┘
   │ _prepare_revit_fix:      │               │
   │  • _compute_new_value    │               │
   │    (inferred=suggested / │               │
   │     fixed / next_avail)  │               │
   │  • dry-run preview       │               │
   │  • autonomy.resolve(     │               │
   │     parameters,set_value)│               │
   │  • stash action/target/  │               │
   │    old_value/inherited_  │               │
   │    from on preview       │               │
   └──────┬───────────────────┘               │
          ▼                                    │
   decision == "auto"  ─────► commit ALL in ONE revit_batch (one undo)
   decision == "approve" ──► gather per RULE → ONE proposal issue per rule
                             + park runs/approvals/<issue_id>.json (Loop 2)
          │                                    │
          └────────────────┬───────────────────┘
                           ▼
              state["proposed_fixes"] += fixes ; return state
```

The old `_select_findings` (slice-before-partition), `_propose_one`,
`_propose_grouped`, and `_build_grouped_issue_payload` are **removed** — slicing
now happens nowhere (the budget caps *issues* at the group level), and both paths
group by rule.

### Path A vs Path B contrast

| Aspect | Path A (Forma) | Path B (Revit) |
|---|---|---|
| **Trigger** | `fixability=manual` rules | `fixability=auto` rules |
| **Backend** | acc-forma-mcp-server | RevitMCPServer + Addin |
| **Tool** | `issues_create` | `revit_set_parameter` |
| **Token flow** | `approval_token` (2-call) | None (single dry-run preview) |
| **Autonomy table** | `documents.create_issue` | `parameters.set_value` |
| **Output** | ACC Issue #N (cloud record) | `.rvt` file mutated (artifact) |
| **Reversible** | Issue can be closed | Param can be re-written |
| **Demo example** | Wall type missing Fire Rating; Bedroom < 10m² | Department empty → infer from name |

### 5.1 QC 4-bucket → DesignAgent routing matrix

QC classifies every `(element, rule)` into one of four buckets; DesignAgent then
decides Path A vs Path B and the autonomy gate. Path is decided by **fixability
+ value-availability**, not by bucket alone — the original "missing_data never
becomes an issue" invariant has since been revised.

| QC bucket | Condition | DesignAgent route | Autonomy gate |
|---|---|---|---|
| `compliant` | passed eval | — (no action) | — |
| `compliant` (`exempt=True`) | `relation_compare` + `lookup:` resolves the host value to "not rated" (no requirement imposed) | — (no action; counts in the PASS set, distinct row in the verification report) | — |
| `manual_review` | `requires_human=true` (incl. a lookup miss — no row matched) | **local review queue** — `state["manual_review_items"]` → `review_queue.md` side report, deliberately kept OUT of the ACC Issues stream (see `state.py` + the flow diagram above). Exception: *geometry* manual_review rides `geometry_findings` → **Path A** | n/a (always human) |
| `non_compliant` | failed eval | `fixability=auto` → **Path B** (write); else **Path A** ACC Issue | `parameters.set_value` (auto / approve / human-only by severity) |
| `missing_data` | raw value None/blank | computable `suggested_value` (revit_mcp wired + `fixability=auto`) → **Path B**; else **Path A** ACC Issue ("fill in X") | see Opt B below |

**Path-B autonomy detail:**

* **Opt B — deterministic fills auto-apply.** A `compose_template` (or `normalize`)
  Path B write resolves to autonomy `auto` *regardless of severity* — the value is
  computed/canonicalised, not a human judgment. Heuristic strategies
  (`infer_from_room_name`, `infer_from_adjacent`) stay severity-gated.
* `_can_auto_fill_missing` is **value-based** (checks `finding.suggested_value`):
  an element that can't be computed (e.g. an unmapped duct) routes to Path A rather
  than a Path-B park — the agent never fabricates data.
* Approve-gated Path B fixes (computable value, safety-tier param) are **not**
  auto-applied — they are gathered into ONE ACC proposal issue and parked for
  Loop 2 (Section 5.2).

**Autofill strategies (QC computes `suggested_value`).** Set on `autofill.strategy`
(+ `normalize_kind` for `normalize`). The author declares INTENT; QC produces the
exact value or `None` (→ Path A, never a fabricated value).

| Strategy | What it produces | Determinism |
|---|---|---|
| `fixed` | the literal `new_value` | deterministic |
| `compose_template` | renders a token template `"{_containing_space}-{Reference Level}-{System Name}-{seq}"`; `{seq}` via a per-group pre-pass; unresolved token → `None` → Path A | deterministic → auto (Opt B) |
| `normalize` | canonicalises a present value via the **unit registry** (`normalize_kind` = duration/length/area/fire_rating) rendering `normalize_format`; or a name transform | deterministic; safety param stays `approve`-gated |
| `normalize_kind="map"` | enumerated/fixed text lookup (`{nr: "Not Rated"}`); case/space-insensitive; miss → `None` → Path A | deterministic |
| `normalize_kind="template"` | regex `normalize_source` captures → `normalize_format` renders the canonical NAME (restructure a name that contains the tokens) | deterministic |
| `normalize_kind="auto"` | engine SELF-PROPOSES — renders every canonical candidate, keeps the first matching the rule's `pattern`. Author declares only the pattern. Also the **self-heal** fallback when a declared format misses the pattern | deterministic |
| `normalize_kind="reference"` | snap to an authoritative list (`config/reference.<name>.yaml`): tier 1 exact / tier 2 alias+slug+case → canonical; tier 3 fuzzy → `None` → Path A | deterministic (tiers 1–2) |
| `inherit_from_host` | copy `host.<param>` down when the element's value is empty (+ optional `host_param`, default = the rule's param); blank host → `None` → Path A | deterministic |
| `inherit_then_normalize` | COMPOUND: value = (current if present else host) → normalize → canonical. ONE `canonical_format` rule covers "present (inherit if empty) AND in format" | deterministic |
| `infer_from_room_name` / `infer_from_adjacent` | heuristic inference | severity-gated |
| `next_available` | next free value in a sequence | deterministic |

**Write target resolution.** `RuleRemediation.target: instance|type|family|auto`
(the Rule Builder default is `auto`). `DesignAgent._effective_remediation(rule,
element)` resolves `auto` per element → `Family Name`→rename family,
`Type Name`→rename type, a Type-carried param (mirrored `type.<param>`, e.g. Fire
Rating)→write the type, else instance. Writes dedup by **(rule_id, write_eid,
param)** — one write per type *within a rule*, never collapsing across rules. A
multi-host type conflict picks the **maximum** fire-rating candidate. Proposal
records carry the resolved write target + `flagged_instance` + `inherited_from`.

### 5.2 Two-loop model

Writes flow through two loops — a synchronous in-run loop for auto/deterministic
fixes, and an asynchronous approval-resume loop for approve-gated ones.

```
LOOP 1 — synchronous run pipeline (--run / --run-revit)
┌──────────────────────────────────────────────────────────────────────┐
│  Query (Forma | Revit | Geometric) → QC (4-bucket) → route → Design   │
│                                                          │            │
│   Design partitions findings (Path-B-first; dedup by rule+target):   │
│     • auto / deterministic Path B  → prepare preview + autonomy,      │
│       then commit all auto writes in ONE revit_batch (single undo)    │
│       [per-element fallback when addin lacks HTTP batch]              │
│     • Path A (manual / no value) → ONE ACC issue per (rule, status)  │
│     • approve-gated Path B  → ONE proposal issue PER RULE +           │
│       park runs/approvals/<issue_id>.json  ──────────────┐           │
│                                                          │           │
│  Re-query → re-QC verifies the auto writes (convergence) │           │
└──────────────────────────────────────────────────────────┼───────────┘
                                                            │ parked record
                                                            ▼
LOOP 2 — asynchronous ApprovalWatcher (--watch-approvals / --apply-approvals-once)
┌──────────────────────────────────────────────────────────────────────┐
│  scan_once(): for each runs/approvals/<issue_id>.json (not applied):   │
│    poll the ACC issue status (forma.get_issue)                         │
│        status == "in_progress"  ──► integrity gate (see below):       │
│            • FAIL  → integrity_failed, no write, issue left open       │
│            • pass  → dry-run batch re-preview (see below):            │
│                • stale  → hold back, issue left open, no write        │
│                • ok     → apply parked fixes:                          │
│                    • revit_batch (per-element fallback)                │
│                    • forma.update_issue → close  +  add_issue_comment  │
│                      (preview → approval_token → execute)              │
│                    • mark record applied (idempotent)                  │
│    watch(): loop scan_once() every --poll-interval seconds             │
└──────────────────────────────────────────────────────────────────────┘
```

* The human approval signal is **flipping the ACC issue to "In progress"**
  (`permittedStatuses` includes `in_progress`, verified against live ACC).
* No `approvals_dir` (or dry-run-only mode) → the propose side is a no-op; auto /
  deterministic fixes are unaffected and still apply in Loop 1.
* The `📥 Approvals` Streamlit tab is a thin front-end over Loop 2 — it lists the
  parked records and shells `--apply-approvals-once`.
* **Fingerprint integrity gate (approval-security).** `policies/
  approval_integrity.py:fingerprint(fixes)` is a stable SHA-256 over the canonical
  write-set (`element_id, action, parameter, new_value`, sorted, order-independent).
  `DesignAgent._create_proposal_issue` stamps it into BOTH the proposal issue body
  (marker line `AutoAudit-Fingerprint: <hex>`) and the local record. Before
  applying, the watcher recomputes the fingerprint from the local record and
  compares it against the one carried in the fetched ACC issue — a mismatch (issue
  body edited/tampered) → `integrity_failed`, no Revit write, issue stays open.
  Records that predate the fingerprint (no anchor) still apply — back-compat.
* **Stale-value re-preview (approval-security).** Before committing,
  `ApprovalWatcher._commit_writes` runs a **dry-run batch** (`_reprove_stale`) and
  reads each write target's live `changes.before`. `live == old_value` → apply as
  planned; `live == new_value` → already at target, mark satisfied (no write,
  still closes); any THIRD value → a human changed it between propose and approve
  → hold the fix back (`stale`), issue stays open, never clobbered. A fix without
  a captured `old_value`, or an addin lacking batch, fails **open** (the
  fingerprint gate stays the primary defence in that case).

---

## 6. Rule YAML schema — full surface

```yaml
scenario: <name>
target_category: <Walls>            # string OR list [Walls, Doors]
metadata:                           # scenario-level provenance (optional)
  source:        <BEP_v3.pdf>       # original document + revision
  extracted_by:  <claude-sonnet-4.7>
  extracted_at:  <2026-06-09T10:00Z>
  custom_parameters_required:       # shared params BIM team must add
    - {name: <Occupancy Type>, type: <text>, applies_to: <Rooms>, note: ...}
  cross_references:                 # follow-up scenarios (e.g. IBC §1011.3)
    - <section_ref>
rules:
  - id:               <rule_id>     # globally unique
    category:         <Walls>       # optional per-rule narrow (multi-target sets)
    parameter:        <Fire Rating> # canonical Revit name — NO unit decoration
    requirement:      <evaluator>   # dispatches to rules_engine.evaluate()
    unit:             m|mm|m²|ft|…  # declared unit of `threshold`
    pattern:          <regex>       # matches_regex / not_matches_regex
    threshold:        <float>       # numeric_min / numeric_min_conditional
    when_param:       <param>       # applicability filter — gate by another param
    when_pattern:     <regex>       #   (e.g. only "Means of Egress" rooms)
    other_param:      <param>       # cross-element ref (relation_compare)
    operator:         >=|>|<=|<|==   # numeric_compare / relation_compare
    compare_kind:     numeric|fire_rating|string   # relation_compare
    scope_filter:                   # UNIVERSAL applicability gate
      param:          <param>       #   apply rule only when this other param…
      pattern:        <regex>       #   …matches (else element is out of scope)
    lookup:           <name>        # relation_compare only: map other_param
                                     #   through config/lookup.<name>.yaml to a
                                     #   REQUIRED value before comparing. A row
                                     #   with no match → manual_review; a row
                                     #   marked not-rated → outcome `exempt`
                                     #   (counts as PASS, not a finding)
    severity_tag:     <key>         # → severity via autonomy.yaml severity_rules
    severity_level:   severity_low|severity_medium|severity_high  # explicit, wins over tag
    description:      <prose>
    fixability:       manual|auto   # routes path A / path B
    requires_human:   true|false    # routes failed eval to manual_review bucket
    remediation:                    # honored when fixability=auto
      action:         set_parameter|rename_element|create_acc_issue
      target_parameter: <param>
      new_value:      <literal>
      new_value_strategy: fixed|inferred|next_available
      target:         auto|instance|type|family   # DEFAULT auto (engine resolves)
      comments_template: <str with {value}/{old_value}/{new_value}/{rule_id}>
    citation:
      mode:           hard|soft
      source_filter:  [BEP.txt, IBC.txt]
      on_missing:     warn|downgrade
    autofill:
      strategy:       normalize|compose_template|inherit_from_host|
                      inherit_then_normalize|infer_from_room_name|
                      infer_from_adjacent|none
      normalize_kind: auto|duration|length|area|fire_rating|
                      family_name|template|map|reference   # for strategy=normalize
      normalize_format: "{h} HR"    # output template (token picks the unit)
      normalize_map:    {nr: Not Rated}        # for normalize_kind=map
      normalize_source: "<regex>"   # for normalize_kind=template
      normalize_reference: <set>    # for normalize_kind=reference → config/reference.<set>.yaml
      host_param:       <param>     # for inherit_* — host param to copy (default = rule param)
      template:         "{Space}-{seq}"  # for compose_template
      sequence_scope:   [<param>]   # {seq} grouping for compose_template
      fallback:       <literal>     # used when inference/normalize returns None
    # conceptual grouping for ExtractionAgent defaults + report grouping
    rule_type:        parameter_completeness | value_constraint |
                      naming_convention | uniqueness_constraint |
                      cross_element_relationship
    # extraction-agent provenance (only set when ExtractionAgent produced
    # the rule; hand-authored YAML omits)
    extraction_meta:
      confidence:        0.0..1.0   # < 0.75 force-bumps to manual + requires_human
      source_text:       "Every room must have Department."
      source_location:   "BEP §1.7 page 12"
      extracted_by:      claude-sonnet-4.7
      extracted_at:      2026-06-09T10:00Z
      execution_status:  executable | needs_domain_mapping | not_model_checkable
      status_reason:     <prose>    # why if not executable
      notes:                        # interpretation guidance not in `description`
        - "Headroom measured from finished floor to underside of obstruction"
```

`GeometryRule` is a separate model (own list, `geometry_rules:` in the YAML) for
clearance/collision checks dispatched via `revit_check_clearance`, not the
`rules_engine.evaluate` path:

```yaml
geometry_rules:
  - id:                 <rule_id>
    check_type:         clearance_min | clearance_max | …
    description:        <prose>
    threshold_mm:       <float>
    clearance_direction: <horizontal|vertical|…>
    reference_category: <Doors>          # what to check clearance against
    reference_source:   same_model | linked_mep | linked_arch | …
    reference_link_hint: <substring>     # disambiguates which LINKED file to
                                          #   query when several links share a
                                          #   discipline (e.g. "HVAC" vs "Plumbing"
                                          #   vs "Electrical" all under linked_mep).
                                          #   Ignored for same_model.
    spatial_filter:      { … }
    severity_tag:        geometric_violation
    view_id:             <int>           # required for axis=Z raycasts
```

### Evaluators registry (`policies/rules_engine.py`)

The Rule Builder OFFERS the consolidated set (top 6); the legacy requirements are
still **evaluated** for old YAML but are no longer offered (subsumed). A universal
`scope_filter` (any requirement) gates applicability ("apply only when param X
matches Y") — replacing the old `numeric_min_conditional`-specific `when_param`.

| Requirement (offered) | Args | Used by |
|---|---|---|
| `present_and_nonempty` | value | Parameter-completeness (most common) |
| `canonical_format` | value, `_suggest` | Auto-fixable format/membership — compliant iff value already canonical; the fix is the canonical. No pattern. Backs normalize/template/map/reference |
| `numeric_compare` | value, operator, threshold(+unit) | Any threshold — subsumes positive_number / numeric_min |
| `matches_regex` | value, pattern (+negate / skip-if-empty flags) | Naming conventions, number formats; folds not_matches_regex + matches_regex_if_present |
| `unique_in_set` | value, siblings | Room/Sheet/Mark number deduplication |
| `relation_compare` | value, other_value, operator, compare_kind | Cross-element — subsumes fire_rating_ge (compare_kind=fire_rating) |
| _legacy (still eval, not offered)_ | | positive_number · numeric_min · numeric_min_conditional · fire_rating_ge |

### Unit conversion contract

Revit stores lengths in **feet**, areas in **ft²**, volumes in **ft³**.
Threshold-typed rules (`numeric_min`, `numeric_min_conditional`, `positive_number`)
declare `unit:` so QC can convert before comparing:

```
raw_value = element.params[rule.parameter]                  # e.g. 7.87 (ft)
value     = convert_to_rule_unit(raw_value,
                                 rule.parameter,            # "Unbounded Height"
                                 rule.unit)                 # "m"  → 2.40
passed    = evaluate("numeric_min", value, threshold=2.4)   # True
```

`policies/revit_units.REVIT_STORAGE_UNITS` maps canonical parameter names
to storage units. Unknown parameters (custom shared params) skip conversion;
the value is assumed already in the rule's unit. Missing-data detection
uses the **raw** value so a converted `0.0` doesn't falsely look absent.

---

## 7. Mock layer parity — testing architecture

```
                  TESTS                          PRODUCTION
                  ─────                          ──────────
┌──────────────────────────┐         ┌──────────────────────────┐
│  MockFormaMCPClient      │         │  FormaMCPClient           │
│  (tests/_mocks.py)       │  ◄─ ►   │  (stdio to acc-forma-mcp) │
│  • In-memory issues_list │         │                           │
│  • approval_token gen    │         │                           │
│  • calls_to(tool) audit  │         │                           │
└──────────────────────────┘         └──────────────────────────┘

┌──────────────────────────┐         ┌──────────────────────────┐
│  MockRevitMCPClient      │         │  RevitMCPClient           │
│  • SAMPLE_REVIT_ROOMS    │  ◄─ ►   │  (stdio to RevitMCPServer)│
│  • SAMPLE_REVIT_WALLS    │         │                           │
│  • SAMPLE_REVIT_DOORS    │         │                           │
│  • elements_by_category  │         │                           │
│  • deepcopy isolation    │         │                           │
└──────────────────────────┘         └──────────────────────────┘
              ▲                                    ▲
              │      Both implement same protocol  │
              │      (ping, list_rooms,            │
              │       list_elements, get_info,     │
              │       set_parameter, ...)          │
              └────────────────────────────────────┘

→ Same agents (QueryAgent, DesignAgent, etc.) work with either backend.
→ The whole suite is deterministic, with no live Revit dependency (run
  `uv run pytest -q` for the current count — see the header note on why no
  number is written down here).
→ Live smoke runs manually only when demo needed (--run-revit on Snowdon).
```

---

## 8. Architecture observations & forward roadmap

### Strengths

1. **Strict separation of concerns** — each agent is a pure async function over
   state, no side-channels.
2. **Absolute MCP boundary** — agents never touch ACC/Revit directly.
3. **1:1 mock parity** — test = production code path, only backend swapped.
4. **Meaningful cyclic graph** — re-queries verify writes.
5. **Citations as first-class** — GroundingAgent slots between QC and Route,
   doesn't pollute rule engine.
6. **Tiered autonomy** — same code path, gate logic resolved via autonomy.yaml.
7. **Rules-driven query agents** — categories, param allowlist, host-hop
   flags all derived from the active `RuleSet`. Add a Revit category by
   editing `config/ost_catalog.yaml`, not Python.
8. **Extraction guardrails** — Pydantic schema + JSON-Schema tool-use
   contract + 3 mechanical heuristics catch ~95% of LLM mistakes before
   YAML lands in `--check`.
9. **Unit handling is data, not naming** — `Rule.unit` field
   plus `revit_units.convert_to_rule_unit` converts at compare time. No
   more `"Unbounded Height (m)"` metric-mirror parameter names.

### Weak points / tech debt

| Issue | Impact | Direction |
|---|---|---|
| ~~`_select_findings` slices **before** `_partition` → starves path A~~ | ~~Demo bug~~ | **RESOLVED** — no slicing; partition first, budget caps *issues* at the group level |
| ~~Consumer-side rename dispatch hard-coded `set_parameter` for every fix~~ | ~~renames silently no-op'd in Revit~~ | **RESOLVED** — `_commit_writes`/`_prepare_revit_fix` now dispatch `rename_element` (not `set_parameter`) when the remediation `action` is a rename; fails *loudly* instead of a silent no-op. **Addin-side gap still open** (next row) — `revit_rename_element` itself must still gain Family/FamilySymbol support upstream. |
| DesignAgent has path A + path B inline → ~1271 lines | Hard to test branches in isolation | Split into `FormaPathStrategy` / `RevitPathStrategy` classes |
| ~~`approve` autonomy currently only parks (no re-entry)~~ | ~~severity_medium fixes lost in limbo~~ | **RESOLVED** — `ApprovalWatcher` polls the parked proposal issue; status "In progress" → applies parked writes + closes (Section 5.2) |
| ~~Route convergence check based on count, not content~~ | ~~False convergence when count happens to match~~ | **RESOLVED** — `graph.route_node` hashes the findings set (`_fingerprint`); convergence now checks "same set vs different set", not just count |
| ~~Addin HTTP-direct endpoint does not expose `batch`~~ | ~~Path B is N undo entries on the no-Node deploy~~ | **RESOLVED** — addin exposes `POST /mcp/batch`; `RevitHTTPClient.batch()` posts there (one undo). Per-element fallback kept as a net. Room containment also landed (`get_element_rooms`) |
| Rules YAML has no cross-file inheritance | Duplicate citation policy boilerplate | Defer |
| `Family.Name` / `FamilySymbol.Name` not reachable via `revit_set_parameter` (addin-side) | Naming-convention auto-fix stops at "we'd rename it to X" until the addin lands a property-setter path | RevitMCPServer extension — consumer-side dispatch resolved, add-in side unverified |
| ~~ExtractionAgent is human-driven (Claude Desktop)~~ | ~~Bottleneck for AU demo if many BEPs need processing~~ | **RESOLVED** — shipped in a different shape than the planned CLI flag: `llm/extraction_bridge.py` (API-driven, same prompt + schema) reached from `service/routes_extraction.py` and the Streamlit Setup tab. There is no `--extract-rules` flag and none is planned. |

### Forward roadmap (no calendar)

```
Offline extraction skill pack (LANDED)
   ├─ Skill pack: extraction_prompt.md + rule_schema.json + 6 examples
   ├─ Post-processor: json_to_yaml.py with 3 heuristic validators
   ├─ Rule.unit + revit_units.convert_to_rule_unit
   ├─ extraction_meta.notes + confidence-based fixability bump
   ├─ Fragmentation detector (param + requirement + category duplicates)
   ├─ Streamlit Extraction Review tab (JSON editor + live validation)
   └─ Revit rename landscape report forwarded to RevitMCPServer team

In-product extraction (LANDED — shape changed)
   ├─ llm/extraction_bridge.py — API-driven version of the same skill pack
   ├─ Surfaces: service/routes_extraction.py + Streamlit Setup tab
   │  (NO `--extract-rules` CLI flag — it was never added and is not planned)
   ├─ Reuses extraction_prompt.md as system prompt
   ├─ Reuses rule_schema.json as tool-use input_schema
   └─ Reuses json_to_yaml.py logic (imported, not shelled out)

Add-in rename support (LATER)
   ├─ RevitMCPServer: rename_element extended OR rename_definition added
   │  (covers Family.Name + FamilySymbol.Name gap)
   ├─ DesignAgent: rename_element remediation wired through
   ├─ Live demo: family-naming-convention auto-fix end-to-end on a .rvt
   └─ +tests on rename_element dispatch + audit chain

Geometry performance + two-loop writes (LANDED)
   ├─ GeometricQueryAgent — parallel + _ClearanceKey batch + dedup-by-element
   ├─ --max-elements element cap + --bulk-fields N+1 killer
   ├─ missing_data routing by value-availability — Path B if computable else Path A
   ├─ compose_template autofill + _containing_space spatial enrichment
   ├─ revit_batch one-undo grouping + per-element fallback
   ├─ ApprovalWatcher — --watch-approvals / --apply-approvals-once
   ├─ normalize autofill + matches_regex_if_present + type-targeted writes
   ├─ Forma issue wrappers (list/get/update/add_comment)
   └─ Streamlit 📥 Approvals tab (UI now 7 tabs)

Rule-language generalisation (LANDED)
   ├─ multi-scenario merge; in-flight type-fetch dedup
   ├─ normalize unit-registry + canonical_format + template/map/auto
   ├─ family/type rename dispatch + ApprovalWatcher rename
   ├─ max_issues = ACC issues; Path A grouped by (rule, status)
   ├─ auto write-target resolution; inherit_from_host + host-conflict collapse
   ├─ reference-data tiers 1–2; requirement consolidation +
   │   compound inherit_then_normalize
   ├─ per-rule proposals + host-source annotation + normalize self-heal
   └─ per-rule write-target dedup + Results columns + family-type display

BACKLOG
   ├─ DesignAgent path A/B Strategy refactor
   ├─ reference-data tier 3 (fuzzy/semantic, LLM-assisted) — deferred
   ├─ per-instance (not per-type) host-inherit writes — deferred
   └─ Cross-file rule inheritance (citation-policy reuse)
       (Content-hashed convergence left this list — shipped.)
```

---

## 9. Verification report module

Answers the BIM Director's *"is what the AI did accurate, and how do I check it
MYSELF?"* — **without trusting the tool's own log** (that would be circular). It
RENDERS the run's recorded artifacts and, per rule, gives a NATIVE Revit/ACC
recipe to independently reproduce each claim.

```
 QCAgent (during the run, never re-derived)
   └─ check_trace: list[CheckRecord]   one per (element, rule), INCL. compliant
        │            + lookup-exempt — the PASS set `findings` discards (the
        │            false-negative defence). Denormalizes the comparison anatomy
        │            (operand/threshold/operator/pattern/unit) → self-contained.
        ▼
 _finish_run_recording  →  report_trace.json  (persisted)
        │
        ▼
 audit_report.render_audit_report(state, rules)         report_trace.index_fixes /
   │  joins each record with proposed_fixes  ◄──────────  outcome_for  (Path A / B /
   │  (DesignAgent NOT modified)                          proposal / parked)
   │  per-rule verify recipe  ◄── verify_recipes.recipe_for(rule, records)
   │                              (registry keyed by requirement, MIRRORS
   │                               rules_engine.evaluate; degrades honestly)
   ▼
 verification_report.md   §1 Exec summary · §2 Trust ladder · §3 Per-rule recipe +
                          PASS/FAIL tables · §4 Per-element appendix · §5 "What we
                          did NOT touch, and why" · §6 Audit trail
        │
        ▼  (opt-in)
 verification_views.create_verification_schedules(revit, check_trace)
   │  one Revit ViewSchedule per rule via revit_create_schedule +
   │  revit_configure_schedule (rich operators reproduce the predicate; PASS set
   │  stays visible). CLI: --create-verification-views RUN_ID. Self-contained from
   │  report_trace.json; degrades on unknown_command.
   ▼
 report_export.export_report(md, docx|pdf)   pandoc-if-present else doc-skill guidance
                                             (Markdown stays canonical). CLI: --export-report
```

**Design invariants** (don't break these):

* **Render, never re-derive.** The report reads `check_trace` + `proposed_fixes`;
  it does NOT re-run a check. A second evaluation could disagree → two sources of
  truth. *Verification* is the USER reproducing the claim natively.
* **General, not rule-specific.** Per-rule recipes come from the requirement-type
  registry; nothing is hardcoded to fire ratings/doors. Add a requirement to
  `rules_engine` AND `verify_recipes.REQUIREMENT_RENDERERS` together.
* **Honest.** All four QC buckets are reported (incl. `missing_data` +
  `manual_review`), the PASS set is listed, and degraded recipes say so
  (Select-by-ID + operands schedule, "shows the inputs, not the verdict").
* **MCP boundary holds.** View creation goes through `mcp_clients/revit.py`; the
  addin's `apply_view_filter` is equality-only so the coloured-view step is manual
  (the add-in's `apply_view_filter` is equality-only).

---

## 10. AuditHub layer — scheduled/unattended audit as a product surface

> This section is the tier ABOVE the compliance loop of §§1–5: the loop is
> the engine; AuditHub is the vehicle that runs it on a schedule, with
> nobody at the keyboard.

```
Task Scheduler (22:30) ──► scripts\nightly_wrapper.ps1
                              │  (self-heals: rebuilds a dead service — 9 real activations)
                              ▼
                    AuditHub service  service/  (FastAPI, 127.0.0.1:8601)
                    POST /audits ──► orchestrator.audit(profile)
                              │        │
                              │        ├─ audit_axes.py  — one-shot satellites BEFORE the graph:
                              │        │    lod-validator · spatial-qc, each spawned over stdio MCP
                              │        │    in its OWN Python 3.10 venv (never a library import);
                              │        │    unconfigured → axis "skipped", never fatal
                              │        ├─ the §3 LangGraph loop (run / run_revit), propose_only
                              │        ├─ issue_registry.py — cross-run Path A dedup (fails OPEN)
                              │        └─ delta_report.py   — delta.md vs newest earlier successful
                              │                               run of the SAME identity (renders from
                              │                               disk, never re-runs a check)
                              ▼
                    runs/<id>/  (+ axes/ envelopes, delta.md, verification_report.md)
                    autoaudit-ui/ (React at :8601/ui) + Revit WebView2 panel read it
```

Key design decisions (each is a convention in CLAUDE.md — this is the map):

* **The service holds ZERO business logic.** `service/` is
  orchestrate-only: `POST /audits` → `orchestrator.audit`; approvals apply ONLY
  via `ApprovalWatcher.scan_once`. One audit at a time per instance — in-process
  lock + `runs/.service_lock` (`pid start_time`, so a recycled PID is
  detectable). CLI commands deliberately do NOT take that lock — they warn
  (`_warn_if_service_busy`) instead of blocking, because a dead CLI holding a
  stale lock would jam the nightly with nobody watching.
* **Scheduled audit is propose-only by construction.**
  `AuditRunOptions.propose_only` demotes every would-be-`auto` Path B decision
  to `approve` AFTER all decision branches — including the Opt B
  deterministic bypass. Path A (raising the ACC issue) IS the propose act.
  Fourteen straight nights (02→15 Aug 2026) of empty `fix_write_log.json` are
  the operating proof.
* **Audit axes ride the shared geometry-findings bucket, one-shot before the
  graph.** Axis findings use the GeometricQueryAgent `Finding` shape;
  artifacts land in `runs/<id>/axes/` and the reports render FROM those
  saved envelopes (renders-never-re-derives applies here too).
* **Unattended mode wraps dispatch, never owns Revit's lifecycle.**
  `unattended.py` launches Revit → spawns the RevitControl watchdog (dismiss
  recognised dialogs only; unknown dialog → HALT) → waits for the addin. `__aexit__` terminates the watchdog but NEVER kills Revit. The
  cloud-model constraint stands: the CLI cannot launch a cloud model, so the
  nightly runs against a Revit session left open (open_cloud_model is a pending
  handoff to RevitMCPServer).
* **Delta compares like with like.** Baseline = newest earlier successful run
  with the same profile identity; "successful" =
  `delta_report.SUCCESSFUL_STATUSES = {"completed", "converged"}` (the graph
  modes record "converged" — don't re-narrow it).
* **Demo mode is the same loop with mock clients.** `demo/` ships
  `MockRevitMCPClient`/mock Forma + the Demo Villa dataset so `--demo` runs the
  REAL engine with staged data; the `--demo` transcript is a pinned surface
  (`tests/test_demo_transcript.py`).

Docs: [SCHEDULED_AUDIT.md](../bim-orchestrator/docs/SCHEDULED_AUDIT.md).

---

## Appendix: Key file index

### Source (committed)

```
bim-orchestrator/
├── src/bim_orchestrator/
│   ├── orchestrator.py             — CLI entry + run modes
│   │                                  check / apply / run / run_revit
│   │                                  + --verify-catalog
│   │                                  + --list-revit-rooms / --list-runs / --trend-report
│   │                                  + --watch-approvals / --apply-approvals-once
│   │                                  + --max-elements / --bulk-fields
│   │                                  + --create-verification-views / --export-report
│   ├── approval_watcher.py         — ApprovalWatcher: poll parked
│   │                                  proposal issues → apply on "In progress"
│   ├── cli.py                      — entry-point shim
│   ├── graph.py                    — LangGraph build_graph + route_node
│   ├── state.py                    — OrchestratorState TypedDict +
│   │                                  Finding (4-bucket) + ProposedFix +
│   │                                  CheckRecord (per-(elem,rule) trace)
│   ├── report_trace.py             — build_check_record (called by QC) +
│   │                                  index_fixes/outcome_for (Design/Result join)
│   ├── verify_recipes.py           — requirement→native-verify-recipe
│   │                                  registry (mirrors rules_engine.evaluate)
│   ├── audit_report.py             — render_audit_report → verification_report.md
│   ├── verification_views.py       — auto-create Revit schedules (opt-in)
│   ├── report_export.py            — docx/pdf export, opt-in (pandoc/skills)
│   ├── reports.py                  — side reports + per-run report.md + trend.md
│   ├── run_recorder.py             — RunFolder + TraceCollector (event trace)
│   ├── agents/
│   │   ├── query.py                — QueryAgent (Forma path, rules-driven)
│   │   ├── revit_query.py          — RevitQueryAgent (unified;
│   │   │                              + _enrich_containing_space spatial join)
│   │   ├── geometry_query.py       — GeometricQueryAgent (clearance checks,
│   │   │                              parallel + _ClearanceKey batch + dedup)
│   │   ├── qc.py                   — QCAgent (4-bucket + Rule.unit conversion)
│   │   ├── grounding.py            — GroundingAgent (RAG citation attach)
│   │   └── design.py               — DesignAgent + path A/B dispatch
│   ├── mcp_clients/
│   │   ├── forma.py                — FormaMCPClient (stdio + REVIT_MCP_VERSION)
│   │   └── revit.py                — RevitMCPClient (stdio + port/token dispatch)
│   ├── policies/                   — pure schema + lookups, no I/O
│   │   ├── rules_schema.py         — Rule, RuleSet, ExtractionMeta, RuleType,
│   │   │                              ExecutionStatus
│   │   ├── rules_engine.py         — evaluators registry
│   │   ├── ost_catalog.py          — dual-label registry loader
│   │   ├── ost_catalog_verify.py   — --verify-catalog logic
│   │   ├── param_catalog.py        — PARAMETER-layer sibling of ost_catalog:
│   │   │                              (OST, param) → ParamSpec (storage/binding/
│   │   │                              writable/dimension); grounds Rule Builder
│   │   ├── query_specs.py          — derive_specs(rules, backend, catalog)
│   │   ├── revit_units.py          — REVIT_STORAGE_UNITS (now a VIEW of param_catalog)
│   │   │                              + convert_to_rule_unit
│   │   ├── reference.py            — reference-data loader + 2-tier matcher
│   │   ├── normalize.py            — normalize unit-registry + template/map/auto:
│   │   │                              mis-formatted value → canonical form
│   │   ├── fire_rating_units.py    — HR/MIN/NR normalizer
│   │   ├── lookup_table.py         — load_lookup(name) → (required, exempt)
│   │   │                              resolver for relation_compare + lookup:
│   │   ├── approval_integrity.py   — approval-security: fingerprint(fixes) SHA-256
│   │   │                              over the canonical write-set (Loop 2 gate)
│   │   ├── ids_converter.py        — IDS (buildingSMART) ⇄ rules.*.yaml conversion
│   │   ├── shared_params.py        — shared-param convention lookups (reference
│   │   │                              set disambiguation)
│   │   └── autonomy.py             — AutonomyPolicy (tiered approval)
│   └── rag/
│       ├── store.py                — chromadb wrapper
│       ├── chunker.py              — paragraph/sentence splitter
│       ├── pdf_extractor.py        — pypdf wrapper (PDF → text)
│       ├── eval.py                 — hit@k / MRR retrieval-quality eval harness
│       └── fixtures/               — BEP §1, IBC §7 synthetic chunks
├── config/
│   ├── ost_catalog.yaml            — dual-label category registry
│   ├── param_catalog.2027.yaml     — 17 categories / 286 built-in params, per
│   │                                  Revit version (probed live; tier mcp-probe)
│   ├── reference.approved_materials.yaml  — demo reference set
│   ├── lookup.ibc716.yaml          — IBC §716 fire-rating-required lookup table
│   ├── shared_param_conventions.yaml — shared-param → reference-set convention map
│   ├── rules.*.yaml                — 13 curated rulesets (test fixtures +
│   │                                  fire-rating / duct / naming / door demos —
│   │                                  count via `ls config/rules.*.yaml`, don't
│   │                                  hardcode; it drifts)
│   └── autonomy.yaml               — tier table + severity_rules
├── extraction-skills/              — skill pack for Claude Desktop
│   ├── extraction_prompt.md        — system prompt (top-4 silent bugs,
│   │                                  unit + scope + atomicity, 2 inline examples)
│   ├── rule_schema.json            — JSON Schema generated from rules_schema.py
│   ├── ost_catalog_keys.txt        — 63 catalog keys + display + discipline
│   ├── examples/01..06_*.md        — worked BEP-excerpt → JSON pairs
│   ├── examples/_combined_smoke.json
│   ├── examples/_corrected_smoke.json
│   └── scripts/
│       ├── json_to_yaml.py         — validator + heuristics + lowering
│       ├── _generate_schema.py     — regenerate rule_schema.json
│       └── _generate_catalog_keys.py
├── streamlit_app/
│   └── app.py                      — 7-tab BIM Manager UI
│                                     (Rule Builder / Setup / Run / Results /
│                                      📥 Approvals / Trend / Run History)
├── scripts/
│   ├── dump_param_catalog.py       — probes live Revit → generates
│   │                                  config/param_catalog.<ver>.yaml (don't
│   │                                  hand-author the catalog)
│   └── _test_extraction_review_tab.py — headless smoke for the Extraction Review tab
└── tests/                          — deterministic; see CHANGELOG.md for the
                                       current test-count trail (run
                                       `uv run pytest --collect-only -q` for
                                       the live number — don't hardcode it here)
```

### Source-of-truth docs (committed)

```
README.md                               — what this is, and the 5-minute demo
docs/
├── ARCHITECTURE.md                     — this file
├── WHY_THIS_SOLUTION.md                — the reasoning behind the design
└── RULE_CAPABILITY_CATALOG.md          — what the Rule Builder can express

bim-orchestrator/
├── README.md                           — CLI surface + quickstart
├── CHANGELOG.md                        — release history
├── extraction-skills/README.md         — extraction workflow + skill pack docs
└── docs/
    ├── PILOT_INSTALL.md                — fresh-machine install, end to end
    ├── PRODUCTION_PACKAGING.md         — no-Node deployment
    └── SCHEDULED_AUDIT.md              — unattended nightly audits + delta
```

### Local-only / gitignored

```
.env, .env.local                    — secrets (ACC SSA key, ANTHROPIC_API_KEY)
.claude/                            — Claude Code per-project state
checkpoints/, audit-logs/           — runtime artifacts
findings.json                       — CLI output dump
```
