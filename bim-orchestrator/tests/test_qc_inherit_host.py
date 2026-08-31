"""Tests for the inherit_from_host autofill strategy (v1.4-K20).

A presence rule whose missing value is inherited DOWN from the host element:
a door with no Fire Rating takes the host wall's Fire Rating. The query layer
surfaces the host value under ``host.<name>`` (revit_query._apply_host_hop);
here we inject it directly to unit-test the QCAgent suggestion. An absent/blank
host value yields no suggestion → the finding routes to Path A.
"""

from __future__ import annotations

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy


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


def _rules_path(tmp_path, *, host_param: str | None = None):
    autofill: dict = {"strategy": "inherit_from_host"}
    if host_param is not None:
        autofill["host_param"] = host_param
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "door_fire_rating",
                "target_category": "Doors",
                "rules": [
                    {
                        "id": "doors.fire_rating.inherit",
                        "parameter": "Fire Rating",
                        "requirement": "present_and_nonempty",
                        "category": "Doors",
                        "severity_tag": "missing_required_param",
                        "description": "Door inherits host wall Fire Rating",
                        "fixability": "auto",
                        "autofill": autofill,
                        "remediation": {"action": "set_parameter", "target": "auto"},
                    }
                ],
            }
        )
    )
    return path


def _door(eid, *, fire_rating=None, host_value=None, host_key="host.Fire Rating"):
    params: dict = {}
    if fire_rating is not None:
        params["Fire Rating"] = fire_rating
    if host_value is not None:
        params[host_key] = host_value
    return {"id": eid, "category": "Doors", "name": f"Door {eid}", "params": params}


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


def _run(tmp_path, elements, *, host_param=None):
    agent = QCAgent(_rules_path(tmp_path, host_param=host_param), _autonomy(tmp_path))
    return agent.run(_state(elements))


class TestInheritFromHost:
    def test_missing_value_inherits_host(self, tmp_path):
        state = _run(tmp_path, [_door("200", fire_rating=None, host_value="2 HR")])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] == "2 HR"

    def test_no_host_value_yields_no_suggestion(self, tmp_path):
        # Host has no Fire Rating → can't inherit → suggested_value None → Path A.
        state = _run(tmp_path, [_door("200", fire_rating=None, host_value=None)])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] is None

    def test_blank_host_value_yields_no_suggestion(self, tmp_path):
        state = _run(tmp_path, [_door("200", fire_rating=None, host_value="   ")])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] is None

    def test_present_value_is_compliant(self, tmp_path):
        state = _run(tmp_path, [_door("200", fire_rating="1 HR", host_value="2 HR")])
        assert state["missing_data_items"] == []
        assert state["outcomes_summary"]["compliant"] == 1

    def test_explicit_host_param_override(self, tmp_path):
        # host_param="Wall Rating" → read host.Wall Rating, not host.Fire Rating.
        door = _door("200", fire_rating=None, host_value="3 HR",
                     host_key="host.Wall Rating")
        state = _run(tmp_path, [door], host_param="Wall Rating")
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] == "3 HR"
