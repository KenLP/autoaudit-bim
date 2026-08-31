# Rules Extraction Prompt Template

**Purpose:** Convert a building-code / BEP / design-standard PDF into a JSON file that `scripts/json_to_yaml.py` can convert into a `config/rules.*.yaml` file compatible with `bim-orchestrator`'s QCAgent.

**Workflow (v1 — manual, no agent):**
1. Open [Claude desktop](https://claude.ai/) or claude.ai web.
2. Attach the regulations PDF (or paste content).
3. Paste the prompt below into the chat, edit the **`<<<EDIT_ME>>>`** placeholders for your scenario.
4. Claude returns a JSON file. Save it locally.
5. Run `uv run python scripts/json_to_yaml.py path/to/rules.json --out bim-orchestrator/config/rules.<scenario>.yaml`.
6. Optionally review the YAML and adjust before committing.

> v2 will replace this manual flow with an automated Rules Extraction Agent (see `bim-orchestrator/extraction-skills/`).

---

## The prompt (copy everything between the lines)

---

You are a senior BIM standards engineer. I will give you a regulations document (PDF attached). Your job is to extract machine-checkable compliance rules into a structured JSON file that my Python QC engine can consume.

### Scenario context
- **Scenario name:** `<<<EDIT_ME: e.g. singapore_residential_accessibility>>>`
- **Target Revit category:** `<<<EDIT_ME: one of Rooms | Doors | Walls | Floors | StructuralColumns | ... (match the Revit category exactly)>>>`
- **Source document reference:** `<<<EDIT_ME: e.g. Singapore BCA Accessibility Code 2023>>>`

### Output JSON schema (must match exactly)

```json
{
  "scenario": "<scenario_name_snake_case>",
  "target_category": "<Revit category, PascalCase>",
  "source": {
    "document": "<original document filename or title>",
    "reference": "<official document reference / version / date>",
    "extracted_date": "<YYYY-MM-DD today>"
  },
  "rules": [
    {
      "id": "<dotted.lowercase.id>",
      "parameter": "<Revit parameter name as it appears in the model>",
      "requirement": "<one of the 7 evaluators below>",
      "threshold": <number, only when requirement is numeric_min or numeric_min_conditional>,
      "pattern": "<regex string, only when requirement is matches_regex or not_matches_regex>",
      "when_param": "<conditional parameter name, only for numeric_min_conditional>",
      "when_pattern": "<regex for when_param value, only for numeric_min_conditional>",
      "severity_tag": "<one of the severity tags below>",
      "description": "<plain English explanation, can span multiple sentences>",
      "fixability": "<manual | auto>",
      "remediation": {
        "action": "<create_acc_issue | set_parameter | rename_element>",
        "target_parameter": "<optional, defaults to rule.parameter>",
        "new_value_strategy": "<inferred | fixed | next_available>",
        "comments_template": "<ASCII-only audit message, use placeholders {value} {old_value} {new_value} {rule_id}>"
      },
      "citation": {
        "mode": "<hard | soft>",
        "source_filter": ["<filename of source PDF, e.g. BEP.txt>"],
        "on_missing": "<warn | downgrade>"
      },
      "autofill": {
        "strategy": "<infer_from_room_name | infer_from_adjacent | none>",
        "fallback": <any value or null>
      }
    }
  ]
}
```

### Valid `requirement` evaluators (pick exactly one per rule)

| Evaluator | When to use | Required extra fields |
|---|---|---|
| `present_and_nonempty` | Parameter must exist and be non-empty | none |
| `positive_number` | Parameter must be a number > 0 | none |
| `matches_regex` | Parameter value must match regex | `pattern` |
| `not_matches_regex` | Parameter value must NOT match regex | `pattern` |
| `numeric_min` | Parameter value >= threshold | `threshold` (number) |
| `numeric_min_conditional` | Parameter value >= threshold ONLY IF `when_param` matches `when_pattern` | `threshold`, `when_param`, `when_pattern` |
| `unique_in_set` | Parameter value must be unique across all elements in scope | none |

If the regulation needs an evaluator NOT in this list (e.g. `numeric_max`, `range`, `geometry_intersect`), STOP and emit `"requirement": "UNSUPPORTED_<your_name>"` in that rule with a `"note"` field explaining what is needed. The converter will fail loudly so a human can extend the engine.

### Valid `severity_tag` values (extend if needed but flag novel ones)

| Tag | Meaning | Default routing |
|---|---|---|
| `missing_required_param` | Parameter is required but absent | severity_medium → approve |
| `missing_optional_param` | Parameter is nice-to-have | severity_low → auto |
| `invalid_value_range` | Value is out of bounds | severity_medium → approve |
| `structural_param_change` | Touches load-bearing data | severity_high → approve |
| `fire_safety_change` | Touches fire rating / egress | severity_high → approve |
| `geometric_violation` | Area / width / height violation, needs human | severity_high → approve |
| `duplicate_identifier` | Room numbers / element IDs collide | severity_medium → approve |

If your rule needs a new severity tag, use a new snake_case tag and include `"severity_note"` so the human reviewer knows to add it to `config/autonomy.yaml`.

### Hard constraints (READ CAREFULLY)

1. **ASCII-only text** in `description`, `comments_template`, `source` fields. NEVER use:
   - `≥` `≤` `±` `²` `³` (use `>=` `<=` `+/-` `m2` `m3`)
   - `—` em-dash (use `--`)
   - `§` section sign (use `Sec.`)
   - Any non-ASCII Unicode character
   This is because the Revit add-in reads request bodies as Latin-1 and mojibakes UTF-8 multi-byte chars.

2. **`id` naming:** `<domain>.<aspect>.<qualifier>` all lowercase dots, no spaces. Examples: `room.area.residential_min`, `door.clearance.width`, `wall.fire_rating.required`.

3. **`parameter` and `when_param` must match a real property name** as exposed by the data source the orchestrator queries. There are TWO data sources, and the property availability differs between them:

   **(a) AECDM — Forma `aecdm_query_elements`** (used by `--check` / `--apply` / Stage 1 Path A workflow). Properties verified live on the Ken-MCP-Testing project:
   - Rooms: `Area`, `Number`, `Name`, `Department`, `Occupancy`, `Unbounded Height`, `Ceiling Height`, `Perimeter`, `Volume`, `Family Name`, `Element Name`, `Element Context`, `External ID`, `Revit Element ID`, `Revit Category Type Id`, `Comments`, `Base Finish`, `Ceiling Finish`, `Floor Finish`, `Wall Finish`, `Room Height`, `Room Length`, `Room Width`
   - Doors: `Family Name`, `Type Mark`, `Width`*, `Rough Width`, `External ID`, `Revit Element ID`. *(`Width` is null for door types whose width is encoded in the type name, e.g. `36" x 96"` — see S1.5-D in `docs/dogfood/STAGE_1_log.md`.)*
   - Walls: limited; `--run-revit` path is far more reliable for wall rules.

   **(b) Revit MCP — `revit_get_element_info`** (used by `--run-revit` / Stage 2 Path B / fire-rating scenario). Type-level Fire Rating, full geometric params, etc. live HERE, not in AECDM. Doors expose `Width` reliably here via the Type. Walls/doors expose `Fire Rating`, `Function`, `Type Mark`, etc. — all of which the W7 D1 fire-rating rules use.

   **❌ Properties that DO NOT exist as a single field in EITHER source** (despite looking plausible in casual prose):
   - `Family and Type` — split across `Family Name` + `Type Mark` in AECDM, or `Family and Type` IS valid only on Revit instance params (not AECDM). When in doubt, prefer `Family Name`.
   - `Type Title`, `Family Title`, `Schedule Mark` — invented terminology; use `Type Mark` or `Mark`.

   **Cross-check rule:** if your rule targets the AECDM path (no `--run-revit`), include a `"parameter_source": "aecdm"` field in the JSON; otherwise `"parameter_source": "revit_mcp"`. The `scripts/json_to_yaml.py` converter (v1.1+) will WARN if the property name doesn't appear in the canonical per-source list.

   If unsure, pick the most plausible parameter name AND add a `"parameter_note"` field explaining the reasoning so a human reviewer can spot-check at install time. Better: run `bim-orchestrator/scripts/diag_*.py` style probe before finalizing the rule (see `scripts/diag_s15_doors.py` for the prototype).

4. **`fixability`:**
   - `manual` — needs human to physically rework geometry / replace family. Pair with `remediation.action: create_acc_issue`.
   - `auto` — agent can write parameter value or rename element. Pair with `remediation.action: set_parameter` or `rename_element`.

5. **Citation block is REQUIRED** when the rule references a specific section/clause of the source PDF. Use `mode: hard` if the citation is mandatory (audit-grade), `mode: soft` if it is just a hint.

6. **`autofill` block is REQUIRED for every rule** even when the strategy is `none`. The QC engine reads this field unconditionally.

7. **Skip rules that cannot be machine-checked** — e.g. "design must be aesthetically pleasing", "shall comply with the spirit of this code". Emit them in a separate `"unmachineable"` top-level array with `{section, text}` so the human reviewer knows what was left out.

### Few-shot examples

#### Example 1 — Geometric numeric_min rule (manual fix)

```json
{
  "id": "room.area.residential_min",
  "parameter": "areaMetric",
  "requirement": "numeric_min_conditional",
  "threshold": 10.0,
  "when_param": "Occupancy",
  "when_pattern": "^Residential",
  "severity_tag": "geometric_violation",
  "description": "Residential rooms must have a floor area of at least 10 m2. Geometric violation -- humans must rework walls; agent flags only.",
  "fixability": "manual",
  "remediation": {
    "action": "create_acc_issue",
    "comments_template": "BEP Sec.1.1 non-compliant: area {value} m2 < 10 m2 minimum"
  },
  "citation": {
    "mode": "hard",
    "source_filter": ["BEP.txt"],
    "on_missing": "warn"
  },
  "autofill": {
    "strategy": "none",
    "fallback": null
  }
}
```

#### Example 2 — Unique identifier rule (auto fix via renumber)

```json
{
  "id": "room.number.unique",
  "parameter": "Number",
  "requirement": "unique_in_set",
  "severity_tag": "duplicate_identifier",
  "description": "Room numbers must be unique across the project. Duplicates are programmatically renumberable -- agent proposes the next-available value and tags Comments with the old value for audit.",
  "fixability": "auto",
  "remediation": {
    "action": "set_parameter",
    "target_parameter": "Number",
    "new_value_strategy": "next_available",
    "comments_template": "Renamed Number from '{old_value}' -- BEP Sec.1.5 duplicate flag"
  },
  "citation": {
    "mode": "hard",
    "source_filter": ["BEP.txt"],
    "on_missing": "warn"
  },
  "autofill": {
    "strategy": "none",
    "fallback": null
  }
}
```

#### Example 3 — Regex naming convention rule (no fix, just flag)

```json
{
  "id": "room.number.format",
  "parameter": "Number",
  "requirement": "matches_regex",
  "pattern": "^[A-Z]?\\d{3}[A-Z]?$",
  "severity_tag": "missing_optional_param",
  "description": "Room number should follow firm convention (e.g., 101, A203B)",
  "fixability": "manual",
  "remediation": {
    "action": "create_acc_issue",
    "comments_template": "Room number '{value}' does not match expected pattern"
  },
  "citation": {
    "mode": "soft",
    "source_filter": [],
    "on_missing": "warn"
  },
  "autofill": {
    "strategy": "none",
    "fallback": null
  }
}
```

### Your task

Extract every machine-checkable rule from the attached PDF that applies to the target Revit category (`<<<EDIT_ME>>>`). For each rule:
1. Cite the exact section/clause of the source document in `description` (use `Sec.X.Y` not `§X.Y`).
2. Pick the right `requirement` evaluator (or emit `UNSUPPORTED_*` if none fits).
3. Pick the right `severity_tag` (or propose a new one with `severity_note`).
4. Default to `fixability: manual` unless the violation is mechanically auto-resolvable.
5. Default citation `mode: hard` with `source_filter: [<filename of attached PDF>]`.

Emit the final JSON as a single fenced ```json``` block. Do not include the prompt back, do not summarize, do not add prose around the JSON. Just the JSON.

If you skip rules that are not machine-checkable, add them to a top-level `"unmachineable"` array so the human reviewer can see what you considered:

```json
{
  "scenario": "...",
  "target_category": "...",
  "source": {...},
  "rules": [...],
  "unmachineable": [
    {"section": "Sec.3.1", "text": "Design shall be visually harmonious with surroundings", "reason": "subjective"}
  ]
}
```

---

## After Claude responds

1. Save the JSON output to a file, e.g. `rules.singapore_accessibility.json`.
2. Run the converter:
   ```bash
   cd bim-orchestrator
   uv run python scripts/json_to_yaml.py ../rules.singapore_accessibility.json \
     --out config/rules.singapore_accessibility.yaml
   ```
3. The converter will fail loudly if:
   - JSON does not match the pydantic schema
   - `requirement` is `UNSUPPORTED_*` (need to extend evaluators in `policies/rules_engine.py`)
   - `severity_tag` is unknown (need to add to `config/autonomy.yaml`)
   - Non-ASCII chars are detected in `description` / `comments_template`
4. Review the YAML diff before committing. Spot-check 2-3 rules against the original PDF.
5. Commit both the JSON (in `references/extracted/` for audit trail) and the YAML.
