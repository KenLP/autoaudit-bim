"""QC finding display name: family - type for type params (v1.4-K22.1).

For a TYPE-level parameter the reviewer needs to know WHICH family/type changes
(a door instance's own name is just its type). QC renders "<family> - <type>"
from the breadcrumbs the query stashes; instance params keep the element name.
"""

from __future__ import annotations

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(yaml.safe_dump({
        "mutations": {"parameters": {"set_value": {"severity_medium": "approve"}}},
        "severity_rules": {"value_out_of_range": "severity_medium",
                           "missing_required_param": "severity_medium"},
    }))
    return AutonomyPolicy.load(cfg)


def _rules(tmp_path, *, scenario, target, rule):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(
        {"scenario": scenario, "target_category": target, "rules": [rule]}))
    return path


def _state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def test_type_level_param_shows_family_and_type(tmp_path):
    rule = {
        "id": "doors.fr.fmt", "parameter": "Fire Rating",
        "requirement": "canonical_format", "category": "Doors",
        "severity_tag": "value_out_of_range", "description": "X HR",
        "fixability": "auto",
        "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating",
                     "normalize_format": "{h} HR"},
        "remediation": {"action": "set_parameter", "target": "type"},
    }
    agent = QCAgent(_rules(tmp_path, scenario="d", target="Doors", rule=rule),
                    _autonomy(tmp_path))
    el = {"id": "1675489", "category": "Doors", "name": "900 x 2000mm",
          "params": {"Fire Rating": "60 MIN", "type.Fire Rating": "60 MIN",
                     "_family_name": "Door-Single-Flush", "_type_name": "900 x 2000mm",
                     "_type_id": 500}}
    state = agent.run(_state([el]))
    assert state["findings"][0]["element_name"] == "Door-Single-Flush - 900 x 2000mm"


def test_instance_param_keeps_element_name(tmp_path):
    rule = {
        "id": "room.dept", "parameter": "Department",
        "requirement": "present_and_nonempty", "category": "Rooms",
        "severity_tag": "missing_required_param", "description": "dept",
        "fixability": "manual", "autofill": {"strategy": "none"},
    }
    agent = QCAgent(_rules(tmp_path, scenario="r", target="Rooms", rule=rule),
                    _autonomy(tmp_path))
    # instance param → no "type.Department" mirror → keep the room name
    el = {"id": "829712", "category": "Rooms", "name": "Studio 203",
          "params": {"Department": ""}}
    state = agent.run(_state([el]))
    assert state["missing_data_items"][0]["element_name"] == "Studio 203"
