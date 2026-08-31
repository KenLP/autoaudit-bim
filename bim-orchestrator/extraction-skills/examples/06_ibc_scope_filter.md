# Example 6 — Applicability scoping + exception cross-reference (IBC §1003.2)

This is the **most common LLM mistake** — extracting a rule that fires
on every element when the source clearly targets a subset, and
generating rules for elements that the source actually exempts.

## Source

> **IBC §1003.2 — Ceiling height**
>
> The means of egress shall have a ceiling height of not less than
> 7 feet 6 inches (2286 mm) above the finished floor.
>
> **Exceptions:**
>
> 1. Sloped ceilings in accordance with Section 1208.2.
> 2. Ceilings of dwelling units and sleeping units within residential
>    occupancies in accordance with Section 1208.2.
> 3. Allowable projections in accordance with Section 1003.3.
> 4. Stair headroom in accordance with Section 1011.3.
> 5. Door height in accordance with Section 1010.1.1.
> 6. Ramp headroom in accordance with Section 1012.5.2.
> 7. The clear height of floor levels in vehicular and pedestrian
>    traffic areas of public and private parking garages in accordance
>    with Section 406.2.2.
> 8. Areas above and below mezzanine floors in accordance with
>    Section 505.2.

## What the WRONG output looks like (avoid)

```json
{
  "scenario": "ibc_1003_2_egress_ceiling_height",
  "target_category": ["Rooms", "Stairs", "Ramps", "Doors"],
  "rules": [
    {"id": "...rooms",  "category": "Rooms",  "parameter": "Unbounded Height", "requirement": "numeric_min", "threshold": 2286},
    {"id": "...stairs", "category": "Stairs", "parameter": "Unbounded Height", "requirement": "numeric_min", "threshold": 2286},
    {"id": "...ramps",  "category": "Ramps",  "parameter": "Unbounded Height", "requirement": "numeric_min", "threshold": 2286},
    {"id": "...doors",  "category": "Doors",  "parameter": "Height",           "requirement": "numeric_min", "threshold": 2286}
  ]
}
```

Two problems:
1. **No scope filter** — fires on every room, every stair, every ramp,
   even when they're not part of any means of egress.
2. **Exceptions inlined** — Stairs/Ramps/Doors are EXCEPTED from §1003.2
   by exceptions 4/5/6. Their headroom rules live in §1011.3 / §1012.5.2
   / §1010.1.1 with their own thresholds. Inlining them here produces
   semantically wrong rules.
3. **Unit mismatch** — Revit's `Unbounded Height` is in feet; threshold
   in mm makes every room fail.

## Correct output (slim metadata, ~25 lines)

```json
{
  "scenario": "ibc_1003_2_egress_ceiling_height",
  "target_category": "Rooms",
  "metadata": {
    "source": "IBC §1003.2",
    "custom_param": "Is Means of Egress (Yes/No on Rooms — add via Manage > Project Parameters; edit when_param if your project uses different name)",
    "follow_ups": ["§1208.2", "§1003.3", "§1011.3", "§1010.1.1", "§1012.5.2", "§406.2.2", "§505.2"]
  },
  "rules": [
    {
      "id": "ibc.1003.2.egress_ceiling_height",
      "rule_type": "value_constraint",
      "category": "Rooms",
      "parameter": "Unbounded Height",
      "requirement": "numeric_min_conditional",
      "threshold": 2.286,
      "unit": "m",
      "when_param": "Is Means of Egress",
      "when_pattern": "^(Yes|true|1)$",
      "severity_tag": "geometric_violation",
      "description": "Means of egress rooms must have ceiling height >= 2286 mm (IBC §1003.2). Stairs/ramps/doors handled by referenced exception sections.",
      "fixability": "manual",
      "autofill": {"strategy": "none"},
      "extraction_meta": {
        "confidence": 0.88,
        "source_text": "The means of egress shall have a ceiling height of not less than 7 feet 6 inches (2286 mm) above the finished floor.",
        "source_location": "IBC §1003.2",
        "execution_status": "executable"
      }
    }
  ]
}
```

## Reasoning notes

1. **`target_category: "Rooms"`** — single category. Stairs/Ramps/Doors
   are OUT (per Exceptions 4/5/6 — they have their own sections).
2. **`when_param: "Is Means of Egress"`** + `when_pattern: "^(Yes|true|1)$"`
   — the rule only fires on rooms flagged as egress. Non-egress rooms
   pass silently. Correct semantics.
3. **`parameter: "Unbounded Height (m)"`** + `threshold: 2.286`
   — uses the v1.3 metric mirror (RevitQueryAgent derives it from the
   imperial Revit value). Pairs with metric threshold. **Common bug
   the validator catches: using `"Unbounded Height"` (feet) with
   threshold `2286` (mm) means every room reads as ≤ 2286 ft tall and
   passes — silent false negative.**
4. **`numeric_min_conditional`** — the only requirement in the v1.3
   schema that supports `when_param`-style filtering. Use it whenever
   the source clause has an applicability subject.
5. **`metadata.custom_param`** — single-line note: tells the BIM team
   what shared parameter to add AND that they can edit `when_param`
   if their project uses a different name. Compact replacement for
   the verbose nested `custom_parameters_required` block.
6. **`metadata.follow_ups`** — flat list of section codes. Tells the
   user which sections to extract next as separate scenarios (each
   one of §1003.2's eight exceptions). Compact replacement for the
   verbose nested `cross_references` block.
7. **Confidence 0.88** — high because the mapping is mechanically
   sound: parameter unit matches threshold unit, scope filter encodes
   the subject explicitly, exceptions are routed elsewhere. Without
   the filter, confidence should be < 0.50 and the rule should be
   marked `not_model_checkable`.

## Downstream behaviour

`json_to_yaml.py` writes the executable rule + metadata into
`config/rules.ibc_1003_2_egress_ceiling_height.yaml`. At runtime,
`QCAgent`:

- Pulls every Room element from `RevitQueryAgent` (params include
  `Unbounded Height (m)` derived in-line).
- For each room, evaluates `numeric_min_conditional`:
  - If `Is Means of Egress` param is missing or doesn't match
    `^(Yes|true|1)$` → rule passes silently (out of scope).
  - If matches → check `Unbounded Height (m) >= 2.286`.
- The `metadata` block is **ignored** by QueryAgent / QCAgent — it's
  documentation for the BIM team about prep work + follow-up scenarios.
