"""QCAgent end-to-end for the IBC §716 table-driven relational check (2-key).

A door's fire rating must be ≥ TABLE(host wall USE × rating). QC reads the wall's
key params → looks up the required door rating (``lookup: ibc716``) → relation_compare
(fire_rating). Buckets: rated wall + under-spec door → non_compliant; meets/exceeds →
compliant; wall EXPLICITLY declared not-rated ("NR") → exempt (compliant);
blank / unknown / unreadable wall rating → manual review (B-4: not recorded
is NOT "no requirement"). The lookup table is written beside the rules file so the QCAgent's
``_config_dir`` (= rules-file parent) finds it.
"""

from __future__ import annotations

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies import lookup_table as lt
from bim_orchestrator.policies.autonomy import AutonomyPolicy


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(yaml.safe_dump({
        "mutations": {"parameters": {"set_value": {"severity_high": "approve"}}},
        "severity_rules": {"rule_violation": "severity_high"},
    }), encoding="utf-8")
    return AutonomyPolicy.load(cfg)


def _setup_rules(tmp_path):
    # 2-key lookup table beside the rules file → QC _config_dir resolves it.
    (tmp_path / "lookup.ibc716.yaml").write_text(yaml.safe_dump({
        "name": "ibc716",
        "keys": [
            {"param": "host.Fire Rating", "dimension": "fire_rating"},
            {"param": "host.Fire Function", "dimension": "string"},
        ],
        "rows": [
            {"when": ["1 HR", "Corridor"], "require": "20 min"},
            {"when": ["1 HR", "*"], "require": "60 min"},
            {"when": ["2 HR", "*"], "require": "90 min"},
            {"when": ["3 HR", "*"], "require": "3 HR"},
        ],
    }), encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({
        "scenario": "ibc716", "target_category": "Doors",
        "rules": [{
            "id": "doors.fire_rating.ibc716", "parameter": "Fire Rating",
            "requirement": "relation_compare", "category": "Doors",
            "compare_kind": "fire_rating", "operator": ">=", "lookup": "ibc716",
            "severity_tag": "rule_violation", "severity_level": "severity_high",
            "description": "IBC 716", "fixability": "manual",
            "autofill": {"strategy": "none"},
            "remediation": {"action": "create_acc_issue"},
        }],
    }), encoding="utf-8")
    return rules


def _door(eid, *, rating=None, host_rating=None, host_use=None):
    params: dict = {}
    if rating is not None:
        params["Fire Rating"] = rating
    if host_rating is not None:
        params["host.Fire Rating"] = host_rating
    if host_use is not None:
        params["host.Fire Function"] = host_use
    return {"id": eid, "category": "Doors", "name": f"Door {eid}", "params": params}


def _state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def _run(tmp_path, elements):
    lt.clear_cache()
    agent = QCAgent(_setup_rules(tmp_path), _autonomy(tmp_path))
    return agent.run(_state(elements))


class TestIBC716TwoKey:
    def test_corridor_lighter_door_compliant(self, tmp_path):
        # 1-hr CORRIDOR needs only 20 min → a 20-min door passes.
        st = _run(tmp_path, [_door("1", rating="20 min", host_rating="1 HR", host_use="Corridor")])
        assert st["outcomes_summary"]["compliant"] == 1

    def test_barrier_same_rating_needs_more(self, tmp_path):
        # Same 1-hr rating but a fire BARRIER needs 60 min → the 20-min door FAILS.
        st = _run(tmp_path, [_door("1", rating="20 min", host_rating="1 HR", host_use="Fire Barrier")])
        assert st["outcomes_summary"]["non_compliant"] == 1

    def test_missing_use_defaults_to_barrier(self, tmp_path):
        # No wall use populated → stricter barrier default (60 min) → 20-min door FAILS.
        st = _run(tmp_path, [_door("1", rating="20 min", host_rating="1 HR")])
        assert st["outcomes_summary"]["non_compliant"] == 1

    def test_exceeds_required_via_minutes(self, tmp_path):
        # door "2 HR" (120) ≥ required 90 (2-hr wall) → compliant.
        st = _run(tmp_path, [_door("1", rating="2 HR", host_rating="2 HR", host_use="Shaft")])
        assert st["outcomes_summary"]["compliant"] == 1

    def test_explicitly_not_rated_wall_exempt(self, tmp_path):
        # A WRITTEN "NR" on the wall is a declaration: §716 imposes nothing.
        st = _run(tmp_path, [_door("1", rating="20 min", host_rating="NR")])
        assert st["outcomes_summary"]["compliant"] == 1
        assert st["outcomes_summary"]["non_compliant"] == 0
        # The exemption is stamped into check_trace so the verification report
        # can show compliant-BY-EXEMPTION, not a bare pass (PASS-set contract).
        trace = {r["element_id"]: r for r in st["check_trace"]}
        assert trace["1"].get("exempt") is True

    def test_blank_wall_rating_is_manual_review_not_exempt(self, tmp_path):
        """B-4 (review round 7, 2026-08-16): a wall with NO recorded rating is
        "not recorded", not "no requirement" — most real wall types ship
        blank, and the old exempt classification silently certified every
        door in them. Now it routes to manual_review like the junk case."""
        st = _run(tmp_path, [_door("1", rating="20 min", host_rating=None)])
        assert st["outcomes_summary"]["manual_review"] == 1
        assert st["outcomes_summary"]["compliant"] == 0

    def test_unknown_wall_rating_manual_review(self, tmp_path):
        st = _run(tmp_path, [_door("1", rating="60 min", host_rating="4 HR", host_use="Fire Barrier")])
        assert st["outcomes_summary"]["manual_review"] == 1
        assert len(st["manual_review_items"]) == 1

    def test_junk_host_rating_is_manual_review_not_exempt(self, tmp_path):
        """M5: a host wall with a NON-BLANK but unparseable Fire Rating (junk
        like a UL listing string) must route to manual_review, NOT be
        silently classified compliant-by-exemption. Before the fix,
        ``lookup_table.match`` treated any unparseable rating (blank or
        junk) as "not-rated" → exempt → compliant, a false negative for a
        door that might be badly under-spec behind a real (but unreadable)
        rating."""
        st = _run(tmp_path, [
            _door("1", rating="20 min", host_rating="2 HR (UL U419)", host_use="Corridor")
        ])
        assert st["outcomes_summary"]["manual_review"] == 1
        assert st["outcomes_summary"]["compliant"] == 0
        assert len(st["manual_review_items"]) == 1
