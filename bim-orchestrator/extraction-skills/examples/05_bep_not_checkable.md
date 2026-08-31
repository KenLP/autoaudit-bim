# Example 5 — Non-checkable clauses (LOD, process, ambiguous)

## Source

> **BEP §5.1 — Level of Development**
>
> The architectural model shall be developed to LOD 300 at the end of
> Schematic Design and LOD 350 at the end of Design Development.
> The BIM team shall coordinate weekly with the structural engineer.
> Custom millwork shall be modelled to LOD 400 where contract documents
> require fabrication.

## Expected JSON output

```json
{
  "scenario": "bep_lod_and_process",
  "target_category": "",
  "rules": [
    {
      "id": "bep.lod.arch_sd_300",
      "rule_type": "parameter_completeness",
      "category": "",
      "parameter": "",
      "requirement": "present_and_nonempty",
      "severity_tag": "quality_change",
      "description": "Architectural model shall reach LOD 300 by end of Schematic Design.",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.30,
        "source_text": "The architectural model shall be developed to LOD 300 at the end of Schematic Design and LOD 350 at the end of Design Development.",
        "source_location": "BEP §5.1",
        "execution_status": "not_model_checkable",
        "status_reason": "LOD requires discipline-specific measurable criteria (parameter mapping, geometric complexity, sheet completeness) — no single Revit parameter encodes 'is LOD 300'."
      }
    },
    {
      "id": "bep.process.weekly_coordination",
      "rule_type": "parameter_completeness",
      "category": "",
      "parameter": "",
      "requirement": "present_and_nonempty",
      "severity_tag": "quality_change",
      "description": "BIM team shall coordinate weekly with structural engineer.",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.20,
        "source_text": "The BIM team shall coordinate weekly with the structural engineer.",
        "source_location": "BEP §5.1",
        "execution_status": "not_model_checkable",
        "status_reason": "Process/coordination requirement — not encoded in model data."
      }
    }
  ]
}
```

## Reasoning notes

- Both clauses are emitted with `execution_status: "not_model_checkable"`
  and a clear `status_reason` so the JSON-to-YAML converter parks them
  in the review queue instead of the executable rules file.
- The `category`, `parameter`, and rule body fields are set to neutral
  defaults — they won't be used (the rule never runs in the engine),
  but the schema still validates.
- Confidence is intentionally low — `< 0.50` for both — to signal that
  the LLM knows these are weak/non-mechanical extractions.
- The third sentence ("Custom millwork shall be modelled to LOD 400…")
  is **not** emitted as a separate rule because it's a sub-case of the
  same LOD clause. One extracted rule per distinct mechanical
  requirement; sub-cases without measurable criteria are noted in the
  parent rule's `status_reason` or simply skipped.

## What the downstream pipeline does with these

`scripts/json_to_yaml.py` will:
1. Split the response by `execution_status`.
2. Write `executable` rules into `config/rules.<scenario>.yaml`.
3. Write `not_model_checkable` + `needs_domain_mapping` rules into
   `runs/extraction_review_<timestamp>.md` for human review.
4. Reject any rule with `confidence < 0.50` AND `execution_status: "executable"`
   (those should have been marked non-checkable — schema enforces it).
