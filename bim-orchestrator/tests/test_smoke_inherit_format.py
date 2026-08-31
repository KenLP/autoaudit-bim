"""Smoke test: the committed compound rule file end-to-end through QC (v1.4-K22).

Loads the SHIPPED config/rules.doors_fire_rating_inherit_format.yaml and runs the
real QCAgent over three doors that mirror the live #153 proposal (an empty door
inheriting from its host, plus two present-but-wrong-format doors). No live MCP —
the host value is injected the way the query layer would surface it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy

_RULE_FILE = (
    Path(__file__).resolve().parents[1]
    / "config" / "rules.doors_fire_rating_inherit_format.yaml"
)


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(yaml.safe_dump({
        "mutations": {"parameters": {"set_value": {"severity_medium": "approve"}}},
        "severity_rules": {"missing_required_param": "severity_medium"},
    }))
    return AutonomyPolicy.load(cfg)


def _door(eid, *, fire_rating=None, host=None):
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


def test_committed_rule_file_loads_and_runs(tmp_path):
    assert _RULE_FILE.exists(), f"missing shipped rule file: {_RULE_FILE}"
    agent = QCAgent(_RULE_FILE, _autonomy(tmp_path))
    state = agent.run(_state([
        _door("1448343", fire_rating=None, host="60 MINUTE"),  # empty → inherit
        _door("1457080", fire_rating="90 MIN"),                # wrong format
        _door("1457084", fire_rating="2 HR"),                  # already canonical
        # A-4b (owner decision 2026-08-17): "20 MIN" would render "0.33 HR" —
        # a LOSSY label (parses back to 19.8 min) — so the formatter falls
        # back and "20 MIN" is its own canonical form → compliant. Clean
        # fractions ("1.5 HR", above) still render and still heal.
        _door("1457090", fire_rating="20 MIN"),
    ]))

    # empty door → missing_data, value inherited from host then normalised
    md = {f["element_id"]: f for f in state["missing_data_items"]}
    assert md["1448343"]["suggested_value"] == "1 HR"
    # v1.4-K22: the finding carries the host source for the Results table
    assert md["1448343"].get("inherited_from") == "60 MINUTE"

    # present but wrong format → non_compliant, normalised
    nc = {f["element_id"]: f for f in state["findings"]}
    assert nc["1457080"]["suggested_value"] == "1.5 HR"

    # already canonical → compliant (no finding, no missing item)
    assert "1457084" not in nc and "1457084" not in md
    assert "1457090" not in nc and "1457090" not in md
    assert state["outcomes_summary"]["compliant"] == 2
