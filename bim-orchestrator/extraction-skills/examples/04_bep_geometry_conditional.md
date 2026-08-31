# Example 4 — Geometry rule with conditional (when_param)

## Source

> **BEP §1.1 — Floor Area Minimums**
>
> Residential rooms must have a floor area of at least 10 m².
> All other occupied rooms must have at least 9 m².
> Mechanical and circulation spaces are exempt at the human-review stage.

## Expected JSON output

```json
{
  "scenario": "bep_floor_area_minimums",
  "target_category": "Rooms",
  "rules": [
    {
      "id": "bep.room.area.residential_min",
      "rule_type": "value_constraint",
      "category": "Rooms",
      "parameter": "areaMetric",
      "requirement": "numeric_min_conditional",
      "threshold": 10.0,
      "when_param": "Occupancy",
      "when_pattern": "^Residential",
      "severity_tag": "geometric_violation",
      "description": "Residential rooms must have area >= 10 m² (BEP §1.1).",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.93,
        "source_text": "Residential rooms must have a floor area of at least 10 m².",
        "source_location": "BEP §1.1",
        "execution_status": "executable"
      }
    },
    {
      "id": "bep.room.area.other_min",
      "rule_type": "value_constraint",
      "category": "Rooms",
      "parameter": "areaMetric",
      "requirement": "numeric_min",
      "threshold": 9.0,
      "severity_tag": "geometric_violation",
      "description": "All occupied rooms must have area >= 9 m² (BEP §1.1).",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.85,
        "source_text": "All other occupied rooms must have at least 9 m². Mechanical and circulation spaces are exempt at the human-review stage.",
        "source_location": "BEP §1.1",
        "execution_status": "executable",
        "status_reason": "Mechanical/circulation exemption is not encoded — human-review note for unusual cases"
      }
    }
  ]
}
```

## Reasoning notes

- Residential-only constraint → `numeric_min_conditional` with
  `when_param=Occupancy` + `when_pattern=^Residential` (a regex that
  matches "Residential", "Residential One Story", etc.)
- General constraint → plain `numeric_min` (no condition).
- Area parameter is `areaMetric` (derived by RevitQueryAgent from the
  Revit `Area` ft² value — see v1.3 Room metric mirrors).
- Geometry → `fixability: manual` (can't auto-resize a room).
- Confidence on the second rule is 0.85, not 0.95, because the
  exemption ("Mechanical and circulation are exempt") is left as a
  human-review note — engine can't encode it without an exemption list.
