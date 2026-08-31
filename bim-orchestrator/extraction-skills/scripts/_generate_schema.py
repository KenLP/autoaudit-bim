"""Regenerate extraction-skills/rule_schema.json from pydantic models.

Run after editing policies/rules_schema.py to keep the JSON Schema in sync.
The JSON Schema is what Claude Desktop attachments / API tool-use bindings
consume to constrain the LLM output.
"""

from __future__ import annotations

import json
from pathlib import Path

from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import (
    ExecutionStatus,
    Requirement,
    RuleSet,
    RuleType,
)

SKILL_PACK_ROOT = Path(__file__).resolve().parents[1]


def _extract_literal_values(literal_type) -> list[str]:
    """Pull the string options from a Literal[...] alias."""
    return list(literal_type.__args__)


def main() -> None:
    # Generate the base schema from the pydantic model
    schema = RuleSet.model_json_schema()

    # Augment with explicit enums for the runtime-derived constraints
    # that pydantic alone doesn't surface in the JSON Schema. These are
    # what we want Claude to pick from when extracting.
    catalog = OSTCatalog.load()
    schema["x-bim-orchestrator-enums"] = {
        "category_keys": sorted(e.key for e in catalog.entries),
        "category_display_labels": sorted(e.display for e in catalog.entries),
        "requirement": _extract_literal_values(Requirement),
        "rule_type": _extract_literal_values(RuleType),
        "execution_status": _extract_literal_values(ExecutionStatus),
        "severity_tag": [
            "fire_safety_change",
            "geometric_violation",
            "missing_required_param",
            "quality_change",
            "duplicate_identifier",
            "naming_violation",
        ],
    }

    out_path = SKILL_PACK_ROOT / "rule_schema.json"
    out_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"  - {len(schema['x-bim-orchestrator-enums']['category_keys'])} OST category keys")
    print(f"  - {len(schema['x-bim-orchestrator-enums']['requirement'])} requirement values")
    print(f"  - {len(schema['x-bim-orchestrator-enums']['rule_type'])} rule_type values")


if __name__ == "__main__":
    main()
