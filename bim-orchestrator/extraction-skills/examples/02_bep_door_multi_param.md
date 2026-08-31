# Example 2 — Multi-parameter clause → split into atomic rules

## Source

> **BEP §3.2 — Door Schedule Completeness**
>
> All Doors must declare Fire Rating, Acoustic Rating, and Manufacturer.
> Door Fire Rating shall be no less than the rating of the host wall.

## Expected JSON output

```json
{
  "scenario": "bep_door_schedule_completeness",
  "target_category": "Doors",
  "rules": [
    {
      "id": "bep.door.fire_rating.required",
      "rule_type": "parameter_completeness",
      "category": "Doors",
      "parameter": "Fire Rating",
      "requirement": "present_and_nonempty",
      "severity_tag": "fire_safety_change",
      "description": "All Doors must declare a Fire Rating value (BEP §3.2).",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.93,
        "source_text": "All Doors must declare Fire Rating, Acoustic Rating, and Manufacturer.",
        "source_location": "BEP §3.2",
        "execution_status": "executable"
      }
    },
    {
      "id": "bep.door.acoustic_rating.required",
      "rule_type": "parameter_completeness",
      "category": "Doors",
      "parameter": "Acoustic Rating",
      "requirement": "present_and_nonempty",
      "severity_tag": "missing_required_param",
      "description": "All Doors must declare an Acoustic Rating value (BEP §3.2).",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.91,
        "source_text": "All Doors must declare Fire Rating, Acoustic Rating, and Manufacturer.",
        "source_location": "BEP §3.2",
        "execution_status": "executable"
      }
    },
    {
      "id": "bep.door.manufacturer.required",
      "rule_type": "parameter_completeness",
      "category": "Doors",
      "parameter": "Manufacturer",
      "requirement": "present_and_nonempty",
      "severity_tag": "missing_required_param",
      "description": "All Doors must declare a Manufacturer value (BEP §3.2).",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.90,
        "source_text": "All Doors must declare Fire Rating, Acoustic Rating, and Manufacturer.",
        "source_location": "BEP §3.2",
        "execution_status": "executable"
      }
    },
    {
      "id": "bep.door.fire_rating.matches_host",
      "rule_type": "cross_element_relationship",
      "category": "Doors",
      "parameter": "Fire Rating",
      "requirement": "fire_rating_ge",
      "other_param": "host.Fire Rating",
      "severity_tag": "fire_safety_change",
      "description": "Door Fire Rating must be >= host wall Fire Rating (BEP §3.2).",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.95,
        "source_text": "Door Fire Rating shall be no less than the rating of the host wall.",
        "source_location": "BEP §3.2",
        "execution_status": "executable"
      }
    }
  ]
}
```

## Reasoning notes

- The first sentence enumerates THREE parameters → 3 atomic rules.
- "no less than" → `fire_rating_ge` (NOT `numeric_min`, because Fire
  Rating is encoded as "2 HR" / "180 MIN" strings).
- The host comparison is `cross_element_relationship` rule_type because
  `other_param` uses the `host.` prefix.
- Fire Rating completeness → `manual` (designer must pick the value,
  not autofillable).
