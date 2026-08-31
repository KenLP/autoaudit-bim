# Extraction Skills — D0 (Claude Desktop workflow)

Skill pack for turning **BEPs, design standards, and technical specs**
into BIM Orchestrator–executable rule YAML, using **Claude Desktop**
(or any LLM that accepts file attachments) as the extraction engine.

This is the **D0 phase** of v1.4 ExtractionAgent: the human-driven Claude
Desktop workflow. The automated path shipped separately and in a different
shape than the CLI flag once planned here — `llm/extraction_bridge.py`,
reached from the AuditHub service (`service/routes_extraction.py`) and the
Streamlit Setup tab. There is no `bim-orchestrator --extract-rules` command
and none is planned.

---

## Files in this folder

| File | What it is |
|---|---|
| `extraction_prompt.md` | System prompt + schema instructions you paste / attach into Claude Desktop |
| `rule_schema.json` | Formal JSON Schema of the LLM output. Generated from `policies/rules_schema.py` — regenerate with `scripts/_generate_schema.py` if the pydantic models change. |
| `ost_catalog_keys.txt` | Flat list of every catalog `key` + display + discipline (regenerate with `scripts/_generate_catalog_keys.py`). Attach this so Claude doesn't invent categories. |
| `examples/01..05_*.md` | Five worked BEP-excerpt → JSON output pairs. Read first two before extracting anything new. |
| `examples/_combined_smoke.json` | Synthetic LLM-style output used for the json_to_yaml smoke test. |
| `scripts/json_to_yaml.py` | Post-processor: validates JSON, applies defaults, splits executable from review, writes YAML + review markdown. |
| `scripts/_generate_schema.py` | Regenerate `rule_schema.json` from pydantic models. |
| `scripts/_generate_catalog_keys.py` | Regenerate `ost_catalog_keys.txt` from `config/ost_catalog.yaml`. |

---

## End-to-end workflow — minimal (2 attachments)

The `extraction_prompt.md` now contains the OST catalog (all display
labels grouped by discipline) and two inline worked examples
(parameter_completeness + multi-param atomicity). For most BEPs you
only need 2 attachments:

```text
1. Open Claude Desktop (Sonnet 4.7 recommended)

2. Start a new chat. Attach:
   - extraction_prompt.md     (prompt + inline catalog + 2 mini examples)
   - rule_schema.json         (formal JSON Schema)
   - (your BEP PDF or .txt file)

3. Prompt Claude:
   "Extract compliance rules from the attached BEP. Reply with ONLY
    the JSON object described in extraction_prompt.md."

4. Save Claude's JSON response to a file (e.g. extracted-bep.json).

5. Run the post-processor:
       python extraction-skills/scripts/json_to_yaml.py extracted-bep.json
   Outputs:
       - config/rules.<scenario>.yaml         (executable rules)
       - runs/extraction_review_<...>.md      (non-checkable + invalid items)

6. Review runs/extraction_review_<...>.md by hand:
   - If it really IS extractable → edit the JSON, re-run step 5
   - If correctly non-checkable → leave it in the review queue
   - Schema-invalid entries → fix the LLM JSON, re-run

7. Run the orchestrator with the new rules YAML:
       bim-orchestrator --check --rules config/rules.<scenario>.yaml
```

### Full-attach mode (optional, for harder BEPs)

When the BEP covers categories beyond the inline list, mixes naming
conventions with geometry, or you want Claude to study additional
patterns, attach the extras:

- `ost_catalog_keys.txt` — full catalog including key + discipline +
  aliases (useful when BEP uses non-English category names)
- `examples/03_bep_naming_convention.md` — auto-fix via `rename_element`
- `examples/04_bep_geometry_conditional.md` — `numeric_min_conditional`
- `examples/05_bep_not_checkable.md` — LOD / process / ambiguous handling

---

## What the post-processor does automatically

`json_to_yaml.py` is not a pure converter — it applies safety
defaults to compensate for LLM imperfection:

| Situation | Action |
|---|---|
| `extraction_meta.execution_status != "executable"` | Skip rule, append to review queue with reason |
| `extraction_meta.confidence < 0.75` | Bump `fixability` to `manual` + `requires_human: true` |
| `fixability` omitted, `rule_type` set | Derive default from `RULE_TYPE_FIXABILITY_DEFAULTS` |
| `autofill` omitted | Default to `{"strategy": "none"}` |
| `category` doesn't case-match catalog (e.g. `"rooms"`) | Normalise via `OSTCatalog.find()` to canonical display |
| Rule fails pydantic validation | Skip + log to review markdown's invalid section |

The bump + default tables are at the top of `scripts/json_to_yaml.py`
and intentionally easy to tune.

---

## Regenerating skill-pack assets

After editing `policies/rules_schema.py` or `config/ost_catalog.yaml`:

```bash
python extraction-skills/scripts/_generate_schema.py
python extraction-skills/scripts/_generate_catalog_keys.py
```

These keep `rule_schema.json` and `ost_catalog_keys.txt` in sync with
the pydantic + catalog source of truth.

---

## Migration to D1 (API-driven ExtractionAgent)

When D1 lands, the same `extraction_prompt.md` becomes the system
prompt of the Claude API call, and `rule_schema.json` becomes the
tool-use `input_schema`. `json_to_yaml.py` either stays as a CLI
helper or its body inlines into `agents/extraction.py`. Either way,
the contract is identical — the only difference is who pushes the
button.

That's why this folder is worth doing first: every test you run here
calibrates the prompt + schema you'll productize later.
