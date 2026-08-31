"""QC tests for the compound inherit_then_normalize autofill (v1.4-K22).

ONE rule covers "Fire Rating must be present (inherit from host if empty) AND in
canonical 'X HR' format". The suggested value is a deterministic pipeline:
fill-from-host-if-empty → normalise. No LLM — QC computes it like any autofill.
"""

from __future__ import annotations

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "mutations": {"parameters": {"set_value": {"severity_medium": "auto"}}},
            "severity_rules": {"missing_required_param": "severity_medium"},
        })
    )
    return AutonomyPolicy.load(cfg)


def _rules_path(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({
        "scenario": "door_fire_rating",
        "target_category": "Doors",
        "rules": [{
            "id": "doors.fire_rating.inherit_and_format",
            "parameter": "Fire Rating",
            "requirement": "canonical_format",
            "category": "Doors",
            "severity_tag": "missing_required_param",
            "description": "Fire Rating present (inherit host) AND 'X HR'",
            "fixability": "auto",
            "autofill": {
                "strategy": "inherit_then_normalize",
                "normalize_kind": "duration",
                "normalize_format": "{h} HR",
            },
            "remediation": {"action": "set_parameter", "target": "auto"},
        }],
    }))
    return path


def _door(eid, *, fire_rating=None, host="120 MIN"):
    params = {}
    if fire_rating is not None:
        params["Fire Rating"] = fire_rating
    if host is not None:
        params["host.Fire Rating"] = host
    return {"id": eid, "category": "Doors", "name": f"D{eid}", "params": params}


def _state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def _run(tmp_path, elements):
    return QCAgent(_rules_path(tmp_path), _autonomy(tmp_path)).run(_state(elements))


class TestInheritThenNormalize:
    def test_empty_inherits_host_then_normalizes(self, tmp_path):
        # empty door → inherit host "120 MIN" → normalise → "2 HR"
        state = _run(tmp_path, [_door("1", fire_rating=None, host="120 MIN")])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] == "2 HR"

    def test_present_wrong_format_normalizes(self, tmp_path):
        # present but "120 MIN" → non-compliant, normalised to "2 HR" (host ignored)
        state = _run(tmp_path, [_door("1", fire_rating="120 MIN", host="999 MIN")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] == "2 HR"

    def test_canonical_value_is_compliant(self, tmp_path):
        state = _run(tmp_path, [_door("1", fire_rating="2 HR", host="120 MIN")])
        assert state["outcomes_summary"]["compliant"] == 1
        assert state["findings"] == []

    def test_empty_and_no_host_routes_path_a(self, tmp_path):
        # nothing to inherit + nothing to normalise → suggested None → Path A
        state = _run(tmp_path, [_door("1", fire_rating=None, host=None)])
        md = state["missing_data_items"]
        assert len(md) == 1
        assert md[0]["suggested_value"] is None
