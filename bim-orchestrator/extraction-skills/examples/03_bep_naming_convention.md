# Example 3 — Naming convention (auto-fixable)

## Source

> **BEP §2.4 — Room Number Format**
>
> Room numbers shall follow the format: 3-digit numeric, optionally
> suffixed by a single uppercase letter (e.g. 101, 203B). Duplicate
> numbers are not permitted within the project.

## Expected JSON output

```json
{
  "scenario": "bep_room_numbering",
  "target_category": "Rooms",
  "rules": [
    {
      "id": "bep.room.number.format",
      "rule_type": "naming_convention",
      "category": "Rooms",
      "parameter": "Number",
      "requirement": "matches_regex",
      "pattern": "^\\d{3}[A-Z]?$",
      "severity_tag": "naming_violation",
      "description": "Room Number must match 3-digit + optional uppercase suffix (BEP §2.4).",
      "fixability": "auto",
      "autofill": {"strategy": "none"},
      "remediation": {
        "action": "rename_element",
        "new_value_strategy": "inferred"
      },
      "extraction_meta": {
        "confidence": 0.96,
        "source_text": "Room numbers shall follow the format: 3-digit numeric, optionally suffixed by a single uppercase letter (e.g. 101, 203B).",
        "source_location": "BEP §2.4",
        "execution_status": "executable"
      }
    },
    {
      "id": "bep.room.number.unique",
      "rule_type": "uniqueness_constraint",
      "category": "Rooms",
      "parameter": "Number",
      "requirement": "unique_in_set",
      "severity_tag": "duplicate_identifier",
      "description": "Room Number must be unique within the project (BEP §2.4).",
      "fixability": "auto",
      "autofill": {"strategy": "none"},
      "remediation": {
        "action": "set_parameter",
        "target_parameter": "Number",
        "new_value_strategy": "next_available"
      },
      "extraction_meta": {
        "confidence": 0.97,
        "source_text": "Duplicate numbers are not permitted within the project.",
        "source_location": "BEP §2.4",
        "execution_status": "executable"
      }
    }
  ]
}
```

## Reasoning notes

- Two distinct conditions → 2 atomic rules (format + uniqueness).
- Format → `naming_convention` → `auto` + `rename_element`.
- Uniqueness → `uniqueness_constraint` → `auto` + `set_parameter` with
  `next_available` strategy (the engine finds the next free number).
- The regex is escaped properly for JSON (backslash doubled).
