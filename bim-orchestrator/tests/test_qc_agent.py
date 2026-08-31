"""Integration tests for the QCAgent: rule loading + finding emission."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import OrchestratorState

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "parameters": {"set_value": {"severity_medium": "approve"}},
                },
                "severity_rules": {
                    "missing_required_param": "severity_medium",
                    "missing_optional_param": "severity_low",
                    "invalid_value_range": "severity_medium",
                },
            }
        )
    )
    return AutonomyPolicy.load(cfg)


@pytest.fixture
def rules_path(tmp_path):
    """Minimal rules: 2 required params + 1 numeric range + 1 regex on Number."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test_completeness",
                "target_category": "Rooms",
                "rules": [
                    {
                        "id": "room.department.required",
                        "parameter": "Department",
                        "requirement": "present_and_nonempty",
                        "severity_tag": "missing_required_param",
                        "description": "Department required",
                        "autofill": {"strategy": "infer_from_adjacent", "fallback": "UNASSIGNED"},
                    },
                    {
                        "id": "room.occupancy.required",
                        "parameter": "OccupancyType",
                        "requirement": "present_and_nonempty",
                        "severity_tag": "missing_required_param",
                        "description": "OccupancyType required",
                        "autofill": {"strategy": "infer_from_room_name", "fallback": None},
                    },
                    {
                        "id": "room.area.positive",
                        "parameter": "Area",
                        "requirement": "positive_number",
                        "severity_tag": "invalid_value_range",
                        "description": "Area must be > 0",
                        "autofill": {"strategy": "none", "fallback": None},
                    },
                    {
                        "id": "room.number.format",
                        "parameter": "Number",
                        "requirement": "matches_regex",
                        "pattern": r"^[A-Z]?\d{3}[A-Z]?$",
                        "severity_tag": "missing_optional_param",
                        "description": "Room number format",
                        "autofill": {"strategy": "none", "fallback": None},
                    },
                ],
            }
        )
    )
    return path


def _state(elements):
    state: OrchestratorState = {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": [],
        "proposed_fixes": [],
        "status": "checking",
        "error": None,
    }
    return state


def test_loads_real_phase1_rules_file(autonomy):
    """The shipped config/rules.parameter_completeness.yaml must parse via pydantic."""
    real_rules = REPO_ROOT / "config" / "rules.parameter_completeness.yaml"
    agent = QCAgent(rules_path=real_rules, autonomy=autonomy)
    assert agent.rules.target_category == "Rooms"
    assert {r.id for r in agent.rules.rules} == {
        "room.department.required",
        "room.occupancy.required",
        "room.area.positive",
        "room.number.format",
    }


def test_loads_fire_rating_rules_file(autonomy):
    """Day 4: hard-citation rules YAML must parse with CitationPolicy block."""
    fire_rules = REPO_ROOT / "config" / "rules.fire_rating.yaml"
    agent = QCAgent(rules_path=fire_rules, autonomy=autonomy)
    assert agent.rules.scenario == "fire_rating_compliance"
    assert agent.rules.target_category == "Walls"

    rules_by_id = {r.id: r for r in agent.rules.rules}
    # wall.fire_rating.required → hard, warn, sources [BEP, IBC]
    r1 = rules_by_id["wall.fire_rating.required"]
    assert r1.citation.mode == "hard"
    assert r1.citation.on_missing == "warn"
    assert r1.citation.source_filter == ["BEP.pdf", "IBC.pdf"]
    # wall.fire_rating.format → hard, downgrade, BEP only
    r2 = rules_by_id["wall.fire_rating.format"]
    assert r2.citation.mode == "hard"
    assert r2.citation.on_missing == "downgrade"
    assert r2.citation.source_filter == ["BEP.pdf"]
    # wall.assembly_type.present → soft (no source_filter, default on_missing)
    r3 = rules_by_id["wall.assembly_type.present"]
    assert r3.citation.mode == "soft"
    assert r3.citation.source_filter is None


def test_loads_naming_rules_file(autonomy):
    """The Day 7 second scenario rules YAML must also parse cleanly."""
    naming_rules = REPO_ROOT / "config" / "rules.naming.yaml"
    agent = QCAgent(rules_path=naming_rules, autonomy=autonomy)
    assert agent.rules.scenario == "naming_convention"
    assert agent.rules.target_category == "Rooms"
    rule_ids = {r.id for r in agent.rules.rules}
    assert "room.name.no_digits_in_name" in rule_ids
    assert "room.name.no_placeholder_tokens" in rule_ids
    assert "room.number.strict_three_digit" in rule_ids
    # Verify not_matches_regex requirement parses
    assert any(r.requirement == "not_matches_regex" for r in agent.rules.rules)


def test_naming_rules_fire_on_demo_data_shape(tmp_path, autonomy):
    """Synthetic Bathroom 1 / Closet rooms — naming rules should flag digit-in-Name."""
    naming_rules = REPO_ROOT / "config" / "rules.naming.yaml"
    agent = QCAgent(rules_path=naming_rules, autonomy=autonomy)

    elements = [
        {  # Should flag: digit in Name, non-3-digit Number
            "id": "e1", "name": "Bathroom 1 07", "category": "Rooms",
            "params": {"Name": "Bathroom 1", "Number": "07"},
        },
        {  # Should flag: non-3-digit Number ("11A"), clean Name
            "id": "e2", "name": "Closet 11A", "category": "Rooms",
            "params": {"Name": "Closet", "Number": "11A"},
        },
        {  # All clean
            "id": "e3", "name": "Office 101", "category": "Rooms",
            "params": {"Name": "Office", "Number": "101"},
        },
    ]
    result = agent.run(_state(elements))
    findings = result["findings"]

    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1

    # "Bathroom 1" has a digit → 1 finding
    assert by_rule.get("room.name.no_digits_in_name") == 1
    # "07" and "11A" don't match \d{3} → 2 findings
    assert by_rule.get("room.number.strict_three_digit") == 2
    # No placeholder tokens in any name → 0 findings
    assert by_rule.get("room.name.no_placeholder_tokens", 0) == 0
    # All names start with capital → 0 findings
    assert by_rule.get("room.name.capitalized", 0) == 0


def test_clean_room_produces_no_findings(autonomy, rules_path):
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    clean = {
        "id": "e1",
        "name": "Office 101",
        "category": "Rooms",
        "params": {
            "Department": "Admin",
            "OccupancyType": "Work",
            "Area": 200.0,
            "Number": "101",
        },
    }
    result = agent.run(_state([clean]))
    assert result["findings"] == []
    assert result["status"] == "designing"


def test_30_empty_rooms_produces_60_missing_data_items(autonomy, rules_path):
    """Mirrors the demo: 30 rooms with empty Department + OccupancyType, valid rest.

    Updated for v1 task BB: rooms missing required parameters land in the
    `missing_data_items` bucket (data quality issue) rather than `findings`
    (which now means "non_compliant" -- design that needs to change).
    """
    elements = [
        {
            "id": f"e{i}",
            "name": "Bedroom 2",
            "category": "Rooms",
            "params": {
                "Department": "",
                "OccupancyType": None,
                "Area": 150.0,
                "Number": f"{100 + i}",
            },
        }
        for i in range(30)
    ]
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state(elements))
    md_items = result["missing_data_items"]
    by_rule = {}
    for f in md_items:
        by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1
    assert by_rule.get("room.department.required") == 30
    assert by_rule.get("room.occupancy.required") == 30
    assert by_rule.get("room.area.positive", 0) == 0
    assert by_rule.get("room.number.format", 0) == 0
    assert len(md_items) == 60
    # No design-side violations
    assert result["findings"] == []
    assert result["outcomes_summary"]["missing_data"] == 60
    assert result["outcomes_summary"]["non_compliant"] == 0


def test_infer_from_name_populates_suggested_value(autonomy, rules_path):
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    element = {
        "id": "e1",
        "name": "Closet 11A",
        "category": "Rooms",
        "params": {
            "Department": "Admin",
            "OccupancyType": "",
            "Area": 10.0,
            "Number": "101",
        },
    }
    result = agent.run(_state([element]))
    # Missing OccupancyType -> missing_data bucket (v1 BB); suggested_value
    # still derived via the autofill strategy so DesignAgent can later
    # auto-fill the parameter when the rule is wired for it.
    occ_findings = [
        f for f in result["missing_data_items"] if f["rule_id"] == "room.occupancy.required"
    ]
    assert len(occ_findings) == 1
    assert occ_findings[0]["suggested_value"] == "Storage"


def test_department_fallback_used_when_no_inference(autonomy, rules_path):
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    element = {
        "id": "e1",
        "name": "Bedroom 2",
        "category": "Rooms",
        "params": {
            "Department": None,
            "OccupancyType": "Sleeping",
            "Area": 150.0,
            "Number": "201",
        },
    }
    result = agent.run(_state([element]))
    # Department=None -> missing_data bucket (v1 BB)
    dept = [
        f for f in result["missing_data_items"] if f["rule_id"] == "room.department.required"
    ]
    assert len(dept) == 1
    assert dept[0]["suggested_value"] == "UNASSIGNED"


def test_negative_area_flagged(autonomy, rules_path):
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    element = {
        "id": "e1",
        "name": "Office 101",
        "category": "Rooms",
        "params": {
            "Department": "Admin",
            "OccupancyType": "Work",
            "Area": -5,
            "Number": "101",
        },
    }
    result = agent.run(_state([element]))
    area = [f for f in result["findings"] if f["rule_id"] == "room.area.positive"]
    assert len(area) == 1
    assert area[0]["severity"] == "severity_medium"


def test_bad_room_number_flagged_low_severity(autonomy, rules_path):
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    element = {
        "id": "e1",
        "name": "Office X",
        "category": "Rooms",
        "params": {
            "Department": "Admin",
            "OccupancyType": "Work",
            "Area": 150.0,
            "Number": "ROOM-XYZ",
        },
    }
    result = agent.run(_state([element]))
    num = [f for f in result["findings"] if f["rule_id"] == "room.number.format"]
    assert len(num) == 1
    assert num[0]["severity"] == "severity_low"


def test_other_category_ignored(autonomy, rules_path):
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    element = {
        "id": "w1",
        "name": "Wall A",
        "category": "Walls",
        "params": {},
    }
    result = agent.run(_state([element]))
    assert result["findings"] == []


def test_bound_parameter_used_for_fetch(autonomy, tmp_path):
    """bound_parameter overrides parameter for Revit fetch; parameter stays
    as the canonical label in findings."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test_binding",
                "target_category": "Stair Runs",
                "rules": [
                    {
                        "id": "stair.clear_width.min_1000mm",
                        "parameter": "stair_clear_width",       # canonical / intent
                        "bound_parameter": "Actual Run Width",  # real Revit param
                        "requirement": "numeric_min",
                        "threshold": 1000.0,
                        "unit": "mm",
                        "severity_tag": "geometric_violation",
                        "description": "Clear width >= 1000 mm",
                        "autofill": {"strategy": "none"},
                        "category": "Stair Runs",
                    }
                ],
            }
        )
    )
    agent = QCAgent(rules_path=path, autonomy=autonomy)

    # Element where value lives under the BOUND name, not the canonical name.
    # 3.6667 ft = ~1118 mm → passes 1000 mm threshold.
    passing = {
        "id": "run-wide",
        "name": "Wide Run",
        "category": "Stair Runs",
        "params": {"Actual Run Width": 3.6667},   # bound key present
    }
    # 3.0 ft = ~914 mm → fails.
    failing = {
        "id": "run-narrow",
        "name": "Narrow Run",
        "category": "Stair Runs",
        "params": {"Actual Run Width": 3.0},
    }
    # canonical key absent → missing_data (bound_parameter is what's fetched)
    missing = {
        "id": "run-missing",
        "name": "No Width Run",
        "category": "Stair Runs",
        "params": {"stair_clear_width": 4.0},   # canonical key only — not fetched
    }

    result = agent.run(_state([passing, failing, missing]))
    assert result["outcomes_summary"]["compliant"] == 1
    assert result["outcomes_summary"]["non_compliant"] == 1
    assert result["outcomes_summary"]["missing_data"] == 1
    # finding carries canonical parameter name
    assert result["findings"][0]["parameter"] == "stair_clear_width"


def test_bound_parameter_unique_in_set_fires_on_bound_name(autonomy, tmp_path):
    """M2: unique_in_set siblings must be built from bound_parameter, not the
    canonical parameter — before the fix, siblings read ``rule.parameter``
    ("Ma cua") while the main fetch used ``bound_parameter`` ("Mark"), so two
    elements sharing the same Mark never counted as duplicates (false
    negative, the bound rule silently never fired)."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test_binding_unique",
                "target_category": "Doors",
                "rules": [
                    {
                        "id": "door.mark.unique",
                        "parameter": "Ma cua",             # canonical intent label
                        "bound_parameter": "Mark",          # real Revit param
                        "requirement": "unique_in_set",
                        "severity_tag": "duplicate_identifier",
                        "description": "Mark must be unique",
                        "autofill": {"strategy": "none"},
                        "category": "Doors",
                    }
                ],
            }
        )
    )
    agent = QCAgent(rules_path=path, autonomy=autonomy)
    dup_a = {"id": "d1", "name": "Door 1", "category": "Doors", "params": {"Mark": "D-01"}}
    dup_b = {"id": "d2", "name": "Door 2", "category": "Doors", "params": {"Mark": "D-01"}}
    unique_c = {"id": "d3", "name": "Door 3", "category": "Doors", "params": {"Mark": "D-02"}}

    result = agent.run(_state([dup_a, dup_b, unique_c]))
    assert result["outcomes_summary"]["non_compliant"] == 2
    flagged_ids = {f["element_id"] for f in result["findings"]}
    assert flagged_ids == {"d1", "d2"}


def test_bound_parameter_canonical_format_suggests_from_bound_value(autonomy, tmp_path):
    """M2: the ``normalize``/canonical_format autofill must read the CURRENT
    value from ``bound_parameter``, not the canonical ``parameter`` — else
    ``_suggest`` always sees None (the canonical key doesn't exist on a bound
    element) and never proposes a fix."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test_binding_canonical",
                "target_category": "Doors",
                "rules": [
                    {
                        "id": "door.fire_rating.canonical",
                        "parameter": "Muc chiu lua",         # canonical intent label
                        "bound_parameter": "Fire Rating",      # real Revit param
                        "requirement": "canonical_format",
                        "severity_tag": "value_out_of_range",
                        "description": "Fire Rating canonical",
                        "autofill": {
                            "strategy": "normalize",
                            "normalize_kind": "fire_rating",
                            "normalize_format": "{h} HR",
                        },
                        "category": "Doors",
                    }
                ],
            }
        )
    )
    agent = QCAgent(rules_path=path, autonomy=autonomy)
    door = {"id": "d1", "name": "Door 1", "category": "Doors", "params": {"Fire Rating": "90 MIN"}}
    result = agent.run(_state([door]))
    assert result["outcomes_summary"]["non_compliant"] == 1
    assert result["findings"][0]["suggested_value"] == "1.5 HR"


def test_unique_in_set_siblings_honour_scope_filter(autonomy, tmp_path):
    """Low2: the unique_in_set siblings pre-pass must apply the SAME
    scope_filter predicate as the main evaluation loop. Before the fix, an
    out-of-scope element's value (e.g. an INTERNAL door, not "external")
    still counted toward duplicate detection against in-scope siblings —
    two in-scope elements matching only because an out-of-scope third
    element happened to share the value would never be caught this way, but
    the converse (a real in-scope duplicate wrongly CLEARED because its only
    match was out-of-scope) is exactly what this test pins."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test_scope_filter_unique",
                "target_category": "Doors",
                "rules": [
                    {
                        "id": "door.mark.unique.external_only",
                        "parameter": "Mark",
                        "requirement": "unique_in_set",
                        "severity_tag": "duplicate_identifier",
                        "description": "Mark must be unique among EXTERNAL doors",
                        "autofill": {"strategy": "none"},
                        "category": "Doors",
                        "scope_filter": {"param": "Exterior", "pattern": "^True$"},
                    }
                ],
            }
        )
    )
    agent = QCAgent(rules_path=path, autonomy=autonomy)
    # Two EXTERNAL doors share Mark "D-01" — a real in-scope duplicate.
    ext_a = {"id": "e1", "name": "Ext Door 1", "category": "Doors",
             "params": {"Mark": "D-01", "Exterior": "True"}}
    ext_b = {"id": "e2", "name": "Ext Door 2", "category": "Doors",
             "params": {"Mark": "D-01", "Exterior": "True"}}
    # An INTERNAL door (out of scope) also happens to have a unique Mark —
    # must not affect anything, and must not itself be flagged.
    interior = {"id": "i1", "name": "Int Door", "category": "Doors",
                "params": {"Mark": "D-02", "Exterior": "False"}}

    result = agent.run(_state([ext_a, ext_b, interior]))
    assert result["outcomes_summary"]["non_compliant"] == 2
    flagged_ids = {f["element_id"] for f in result["findings"]}
    assert flagged_ids == {"e1", "e2"}


def test_unique_in_set_out_of_scope_duplicate_not_flagged(autonomy, tmp_path):
    """Low2 (converse case): an EXTERNAL door whose only "duplicate" is an
    out-of-scope INTERNAL door must NOT be flagged — the out-of-scope
    element's value must not enter the sibling pool at all. Before the fix
    this already passed by coincidence (the exterior door is unique among
    exterior doors), but pins the scope_filter is actually being applied,
    not merely harmless."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test_scope_filter_unique_converse",
                "target_category": "Doors",
                "rules": [
                    {
                        "id": "door.mark.unique.external_only",
                        "parameter": "Mark",
                        "requirement": "unique_in_set",
                        "severity_tag": "duplicate_identifier",
                        "description": "Mark must be unique among EXTERNAL doors",
                        "autofill": {"strategy": "none"},
                        "category": "Doors",
                        "scope_filter": {"param": "Exterior", "pattern": "^True$"},
                    }
                ],
            }
        )
    )
    agent = QCAgent(rules_path=path, autonomy=autonomy)
    ext = {"id": "e1", "name": "Ext Door", "category": "Doors",
           "params": {"Mark": "D-01", "Exterior": "True"}}
    interior_same_mark = {"id": "i1", "name": "Int Door", "category": "Doors",
                          "params": {"Mark": "D-01", "Exterior": "False"}}

    result = agent.run(_state([ext, interior_same_mark]))
    # The exterior door is unique among the (scope-filtered) exterior set —
    # the interior door sharing "D-01" must not count as a sibling.
    assert result["outcomes_summary"]["non_compliant"] == 0
    assert result["findings"] == []
