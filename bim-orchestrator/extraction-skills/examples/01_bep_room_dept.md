# Example 1 — Room Department required (single atomic rule)

## Source

> **BEP §1.7 — Room Data Requirements**
>
> Every Room element shall carry a non-empty Department value.
> Empty Department blocks downstream FM integration and asset
> register population at handover.

## Expected JSON output

```json
{
  "scenario": "bep_room_data_requirements",
  "target_category": "Rooms",
  "rules": [
    {
      "id": "bep.room.dept.required",
      "rule_type": "parameter_completeness",
      "category": "Rooms",
      "parameter": "Department",
      "requirement": "present_and_nonempty",
      "severity_tag": "missing_required_param",
      "description": "Every Room must declare a non-empty Department value (BEP §1.7).",
      "fixability": "auto",
      "autofill": {"strategy": "infer_from_room_name"},
      "extraction_meta": {
        "confidence": 0.94,
        "source_text": "Every Room element shall carry a non-empty Department value. Empty Department blocks downstream FM integration and asset register population at handover.",
        "source_location": "BEP §1.7",
        "execution_status": "executable"
      }
    }
  ]
}
```

## Reasoning notes (for your own learning, not part of the output)

- "Every Room … shall carry" → `present_and_nonempty` requirement
- The source mentions "downstream FM integration" — supports `auto`
  fixability with `infer_from_room_name` autofill (residential rooms
  → "Residential", storage → "Services" etc.)
- Single atomic rule — Department is the only parameter mentioned
- Confidence 0.94 because the mapping is 1-to-1 and unambiguous
