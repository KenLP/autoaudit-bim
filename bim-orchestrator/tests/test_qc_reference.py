"""QC integration for reference-data rules (v1.4-K21).

A ``canonical_format`` rule whose canonicaliser is a reference set: an exact
palette value is compliant; a recognised variant (alias/case/separator) is
non-compliant with the canonical as the suggested auto-fix; an off-list value is
non-compliant and UNFIXABLE (suggested None → Path A).
"""

from __future__ import annotations

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.reference import clear_cache


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {"parameters": {"set_value": {"severity_medium": "auto"}}},
                "severity_rules": {"value_constraint": "severity_medium"},
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _rules_path(tmp_path):
    # reference set lives in the SAME dir as the rules file (QC resolves it there)
    (tmp_path / "reference.materials.yaml").write_text(
        yaml.safe_dump({
            "name": "materials",
            "case_sensitive": False,
            "entries": [
                {"canonical": "Oak", "aliases": ["white oak"]},
                {"canonical": "Steel-Brushed", "aliases": ["brushed steel"]},
            ],
        }),
        encoding="utf-8",
    )
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump({
            "scenario": "material_palette",
            "target_category": "Furniture",
            "rules": [{
                "id": "furniture.material.palette",
                "parameter": "Material",
                "requirement": "canonical_format",
                "category": "Furniture",
                "severity_tag": "value_constraint",
                "description": "Material must be in the approved palette",
                "fixability": "auto",
                "autofill": {
                    "strategy": "normalize",
                    "normalize_kind": "reference",
                    "normalize_reference": "materials",
                },
                "remediation": {"action": "set_parameter", "target": "auto"},
            }],
        }),
        encoding="utf-8",
    )
    return path


def _furn(eid, material):
    return {"id": eid, "category": "Furniture", "name": f"F{eid}",
            "params": {"Material": material}}


def _state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def _run(tmp_path, elements):
    clear_cache()
    agent = QCAgent(_rules_path(tmp_path), _autonomy(tmp_path))
    return agent.run(_state(elements))


class TestReferenceQC:
    def test_exact_palette_value_is_compliant(self, tmp_path):
        state = _run(tmp_path, [_furn("1", "Oak")])
        assert state["outcomes_summary"]["compliant"] == 1
        assert state["findings"] == []

    def test_recognised_variant_is_autofixable(self, tmp_path):
        # "brushed steel" (alias) → non-compliant, suggested canonical "Steel-Brushed"
        state = _run(tmp_path, [_furn("1", "brushed steel")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] == "Steel-Brushed"

    def test_case_variant_is_autofixable(self, tmp_path):
        state = _run(tmp_path, [_furn("1", "oak")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] == "Oak"

    def test_off_list_is_unfixable_path_a(self, tmp_path):
        # "Pine" is not a deterministic member → suggested None → Path A
        state = _run(tmp_path, [_furn("1", "Pine")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] is None
