"""Tests for the compose_template autofill strategy + sequence pre-pass (v1.4-K3).

The QCAgent composes a suggested_value from a token template referencing element
params plus a per-group ``{seq}`` counter. Missing template inputs (e.g. a duct
not mapped to a space) yield no suggestion → the finding routes to Path A.
"""

from __future__ import annotations

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy

_TEMPLATE = "{_containing_space}-{Reference Level}-{System Name}-{seq}"


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {"parameters": {"set_value": {"severity_medium": "auto"}}},
                "severity_rules": {"missing_required_param": "severity_medium"},
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _rules_path(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "duct_mark",
                "target_category": "Ducts",
                "rules": [
                    {
                        "id": "ducts.mark.required",
                        "parameter": "Mark",
                        "requirement": "present_and_nonempty",
                        "category": "Ducts",
                        "severity_tag": "missing_required_param",
                        "description": "Mark required",
                        "fixability": "auto",
                        "autofill": {
                            "strategy": "compose_template",
                            "template": _TEMPLATE,
                            "sequence_scope": [
                                "_containing_space",
                                "Reference Level",
                                "System Name",
                            ],
                        },
                    }
                ],
            }
        )
    )
    return path


def _duct(
    eid,
    *,
    mark=None,
    space="Live/Work Unit 202",
    level="L2",
    system="Mechanical Return Air 1",
):
    params = {"Reference Level": level, "System Name": system}
    if space is not None:
        params["_containing_space"] = space
    if mark is not None:
        params["Mark"] = mark
    return {"id": eid, "category": "Ducts", "name": f"Duct {eid}", "params": params}


def _state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": [],
        "proposed_fixes": [],
        "status": "checking",
        "error": None,
    }


def _run(tmp_path, elements):
    agent = QCAgent(_rules_path(tmp_path), _autonomy(tmp_path))
    return agent.run(_state(elements))


class TestComposeTemplate:
    def test_missing_mark_gets_composed_suggested_value(self, tmp_path):
        state = _run(tmp_path, [_duct("100", mark=None)])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert (
            md[0]["suggested_value"]
            == "Live/Work Unit 202-L2-Mechanical Return Air 1-01"
        )

    def test_sequence_increments_within_group(self, tmp_path):
        ducts = [_duct("100"), _duct("101"), _duct("102")]  # same space/level/system
        state = _run(tmp_path, ducts)
        sv = {f["element_id"]: f["suggested_value"] for f in state["missing_data_items"]}
        assert sv["100"].endswith("-01")
        assert sv["101"].endswith("-02")
        assert sv["102"].endswith("-03")

    def test_separate_groups_number_independently(self, tmp_path):
        ducts = [
            _duct("100", system="Mechanical Return Air 1"),
            _duct("101", system="Mechanical Supply Air 1"),
        ]
        state = _run(tmp_path, ducts)
        sv = {f["element_id"]: f["suggested_value"] for f in state["missing_data_items"]}
        assert sv["100"].endswith("-01")
        assert sv["101"].endswith("-01")  # different system → its own group

    def test_unmapped_duct_yields_no_suggestion(self, tmp_path):
        # No _containing_space → template can't resolve → suggested_value None
        # → (Layer 1) routes to a Path A ACC Issue rather than a bad write.
        state = _run(tmp_path, [_duct("100", space=None)])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] is None

    def test_present_mark_is_compliant(self, tmp_path):
        state = _run(tmp_path, [_duct("100", mark="Existing-Mark")])
        assert state["missing_data_items"] == []
        assert state["outcomes_summary"]["compliant"] == 1


class TestSchemaFields:
    def test_compose_template_fields_accepted(self):
        from bim_orchestrator.policies.rules_schema import RuleAutofill

        af = RuleAutofill.model_validate(
            {
                "strategy": "compose_template",
                "template": _TEMPLATE,
                "sequence_scope": ["_containing_space", "System Name"],
            }
        )
        assert af.strategy == "compose_template"
        assert af.template == _TEMPLATE
        assert af.sequence_scope == ["_containing_space", "System Name"]

    def test_defaults_none_for_non_template_strategies(self):
        from bim_orchestrator.policies.rules_schema import RuleAutofill

        af = RuleAutofill.model_validate({"strategy": "none"})
        assert af.template is None
        assert af.sequence_scope is None
