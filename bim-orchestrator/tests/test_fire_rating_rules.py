"""Tests for the fire-rating rules YAML + fire_rating_ge evaluator
(Phase 2 Week 7 Day 1).

Two layers:

1. Unit tests on the new ``fire_rating_ge`` evaluator + the dispatch
   plumbing in ``evaluate()``.

2. Integration: run the production ``rules.fire_rating_ibc7.yaml``
   through QCAgent against the wall/door fixtures (via
   RevitElementsQueryAgent + MockRevitMCPClient) and assert the
   expected pattern of violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.rules_engine import evaluate, fire_rating_ge

from tests._mocks import MockRevitMCPClient


# ---- Paths ------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = PROJECT_ROOT / "config" / "rules.fire_rating_ibc7.yaml"
AUTONOMY_PATH = PROJECT_ROOT / "config" / "autonomy.yaml"


# ---- 1. Direct evaluator tests ---------------------------------------------


class TestFireRatingGe:
    """Direct evaluator behavior — host comparison semantics."""

    def test_door_rated_higher_than_host_passes(self):
        # 4 HR door in 2 HR wall → fine
        assert fire_rating_ge("4 HR", "2 HR") is True

    def test_door_equal_to_host_passes(self):
        assert fire_rating_ge("2 HR", "2 HR") is True
        # Unit-crossing equivalence: 120 MIN door = 2 HR wall
        assert fire_rating_ge("120 MIN", "2 HR") is True

    def test_door_below_host_fails(self):
        # 90 MIN door in 2 HR (120 MIN) wall — the demo violation pattern
        assert fire_rating_ge("90 MIN", "2 HR") is False
        # 3 HR door in 4 HR wall — Snowdon's door 200 vs host 100 pattern
        assert fire_rating_ge("180 MIN", "4 HR") is False

    def test_nr_door_in_rated_wall_fails(self):
        assert fire_rating_ge("NR", "2 HR") is False
        assert fire_rating_ge("NR", "4 HR") is False

    def test_missing_door_value_with_rated_host_fails(self):
        # Door rating not set but host is rated → violation
        assert fire_rating_ge(None, "2 HR") is False
        assert fire_rating_ge("", "2 HR") is False

    def test_unrated_host_passes_regardless_of_door(self):
        # Host has no rating → rule out of scope
        assert fire_rating_ge("NR", "NR") is True
        assert fire_rating_ge(None, "") is True
        assert fire_rating_ge("180 MIN", None) is True
        assert fire_rating_ge("180 MIN", "") is True
        assert fire_rating_ge("180 MIN", "NR") is True


class TestEvaluateDispatch:
    def test_dispatch_uses_other_value(self):
        # Door 180 MIN vs host 4 HR → fail
        assert evaluate(
            "fire_rating_ge",
            "180 MIN",
            other_value="4 HR",
        ) is False
        # Door 4 HR vs host 2 HR → pass
        assert evaluate(
            "fire_rating_ge",
            "4 HR",
            other_value="2 HR",
        ) is True


# ---- 2. End-to-end integration: Query → QC on fixtures ---------------------


@pytest.mark.asyncio
async def test_fire_rating_e2e_violations_match_fixtures():
    """The production rules YAML produces the expected violation set
    when fed by the wall+door fixture.

    Expected violation pattern:
        * wall.type.fire_rating.required → 1
            (wall 102, type 1002 "Interior - Partition" has Fire Rating "")
        * door.fire_rating.matches_host → 2
            - door 200: type 2000 "180 MIN" hosted in type 1000 "4 HR" → 180<240, fail
            - door 201: type 2001 "NR" hosted in type 1000 "4 HR" → fail
            (door 202 is "180 MIN" in 2 HR wall → 180>=120, pass)
            (door 203 is "NR" in unrated wall → out of scope, pass)
        * door.fire_rating.required → 0
            (both door types have Fire Rating populated — one as "NR")
    """
    client = MockRevitMCPClient()
    state = {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }
    autonomy = AutonomyPolicy.load(AUTONOMY_PATH)
    qc = QCAgent(rules_path=RULES_PATH, autonomy=autonomy)
    # v1.3: unified RevitQueryAgent — categories + host hop derived from rules.
    query_agent = RevitQueryAgent(mcp=client, rules=qc.rules)

    state = await query_agent.run(state)
    assert state["status"] == "checking"
    state = qc.run(state)

    # Phase 2 W7 D1 + v1 BB merge: QC classifies every (element, rule) into
    # one of 4 buckets: compliant / non_compliant / manual_review / missing_data.
    # - non_compliant violations → state["findings"]
    # - missing values (None / "" / whitespace) → state["missing_data_items"]
    # - requires_human=True rules → state["manual_review_items"]
    # For fire-rating, blank Fire Rating is STRUCTURALLY missing data, not
    # a non-compliance call — the agent can't say a wall is non-compliant
    # without first knowing what rating was intended. Concrete values that
    # fail the evaluator (e.g. "180 MIN" door in 4HR wall) remain in findings.
    all_items: dict[str, list] = {}
    for f in state["findings"]:
        all_items.setdefault(f["rule_id"], []).append(("non_compliant", f))
    for f in state.get("missing_data_items", []):
        all_items.setdefault(f["rule_id"], []).append(("missing_data", f))
    for f in state.get("manual_review_items", []):
        all_items.setdefault(f["rule_id"], []).append(("manual_review", f))

    # wall.type.fire_rating.required — wall 102 has Type Fire Rating "" → missing_data
    wall_required = all_items.get("wall.type.fire_rating.required", [])
    assert len(wall_required) == 1, (
        f"Expected 1 wall.type.fire_rating.required, got {len(wall_required)}: "
        f"{[(bucket, f['element_id']) for bucket, f in wall_required]}"
    )
    bucket, finding = wall_required[0]
    assert bucket == "missing_data", (
        f"Expected wall 102 (blank Fire Rating) in missing_data bucket, got {bucket}"
    )
    assert finding["element_id"] == "102"

    # door.fire_rating.matches_host — doors 200 ("180 MIN") and 201 ("NR")
    # both have concrete values that FAIL fire_rating_ge vs host "4 HR";
    # values are non-missing strings, so they land in findings (non_compliant).
    matches_host = all_items.get("door.fire_rating.matches_host", [])
    assert {b for b, _ in matches_host} == {"non_compliant"}, (
        f"Expected all matches_host violations in findings (non_compliant), "
        f"got buckets {[b for b, _ in matches_host]}"
    )
    matches_host_ids = sorted(f["element_id"] for _, f in matches_host)
    assert matches_host_ids == ["200", "201"], (
        f"Expected doors 200 and 201 to violate matches_host, got {matches_host_ids}"
    )

    # door.fire_rating.required — both door types have Fire Rating populated
    # ("180 MIN", "NR" — neither is missing) so this rule doesn't fire.
    door_required = all_items.get("door.fire_rating.required", [])
    assert len(door_required) == 0, (
        f"Expected 0 door.fire_rating.required violations, got {len(door_required)}: "
        f"{[(b, f['element_id']) for b, f in door_required]}"
    )

    # 4-state summary sanity: 12 total checks (4 walls × 1 rule
    # + 4 doors × 2 rules), 9 compliant, 2 non_compliant (matches_host),
    # 1 missing_data (wall.type.fire_rating.required on wall 102).
    summary = state["outcomes_summary"]
    assert summary["total"] == 12, f"total checks: {summary}"
    assert summary["non_compliant"] == 2, f"non_compliant: {summary}"
    assert summary["missing_data"] == 1, f"missing_data: {summary}"
    assert summary["compliant"] == 9, f"compliant: {summary}"


@pytest.mark.asyncio
async def test_door_with_blank_fire_rating_triggers_required():
    """Inject a door type with blank Fire Rating — check door.fire_rating.required fires."""
    client = MockRevitMCPClient()
    # Mutate door type 2000's Fire Rating to empty string
    info = dict(client.element_info[2000])
    info["parameters"] = [
        ({**p, "value": ""} if p["name"] == "Fire Rating" else p)
        for p in info["parameters"]
    ]
    client.element_info[2000] = info

    state = {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }
    autonomy = AutonomyPolicy.load(AUTONOMY_PATH)
    qc = QCAgent(rules_path=RULES_PATH, autonomy=autonomy)
    query_agent = RevitQueryAgent(mcp=client, rules=qc.rules)
    state = await query_agent.run(state)
    state = qc.run(state)

    # With type 2000's Fire Rating now blank, doors 200 + 202 hit BOTH rules:
    #   - door.fire_rating.required: present_and_nonempty fails on "" → missing_data
    #   - door.fire_rating.matches_host: fire_rating_ge fails (None < 4HR) → missing_data
    # Both rules route to missing_data_items because the value is empty.
    door_required_ids = sorted(
        f["element_id"] for f in state["missing_data_items"]
        if f["rule_id"] == "door.fire_rating.required"
    )
    assert door_required_ids == ["200", "202"], (
        f"Expected doors 200, 202 in missing_data_items for door.fire_rating.required, "
        f"got {door_required_ids}"
    )


class TestMultiCategoryQC:
    """QCAgent honors per-rule category filter so wall-only rules don't
    fire on doors and vice versa."""

    def test_wall_only_rule_ignores_doors(self):
        """Synthesize an element list with a door that has Fire Rating="";
        the wall-only rule must NOT fire on it."""
        elements = [
            {"id": "100", "name": "Concrete Wall", "category": "Walls",
             "params": {"Fire Rating": "4 HR"}},
            {"id": "999", "name": "Blank Door", "category": "Doors",
             "params": {"Fire Rating": ""}},  # blank, but it's a door
        ]
        state = {
            "project_id": "test", "iteration": 0, "max_iterations": 1,
            "elements": elements, "findings": [], "proposed_fixes": [],
            "status": "init", "error": None,
        }
        autonomy = AutonomyPolicy.load(AUTONOMY_PATH)
        qc = QCAgent(rules_path=RULES_PATH, autonomy=autonomy)
        result = qc.run(state)

        # Pool all 4-state buckets so a wall-only rule misfiring on a door
        # would surface regardless of which bucket the door violation lands in.
        all_rule_ids_by_element: dict[str, set[str]] = {}
        for bucket_key in ("findings", "missing_data_items", "manual_review_items"):
            for f in result.get(bucket_key, []):
                all_rule_ids_by_element.setdefault(
                    f["element_id"], set()
                ).add(f["rule_id"])

        # wall-only rule must NOT appear on the door element (999)
        assert "wall.type.fire_rating.required" not in all_rule_ids_by_element.get("999", set()), (
            f"wall-only rule fired on door 999: "
            f"{all_rule_ids_by_element.get('999')}"
        )
        # door.fire_rating.required must fire on door 999 (blank Fire Rating →
        # missing_data per 4-state semantics)
        assert "door.fire_rating.required" in all_rule_ids_by_element.get("999", set()), (
            f"door rule didn't fire on door 999: "
            f"{all_rule_ids_by_element.get('999')}"
        )
