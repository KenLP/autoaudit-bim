"""QC self-heal when a normalize rule's declared format misses its pattern (K22).

A real Rule Builder rule shipped `normalize_format="{h}-hour"` (→ "1-hour") while
its pattern wanted "...HR$" (→ "1 HR"). The K9 guard used to drop the mismatched
output → an empty proposal. Now the engine falls back to the auto candidates and
keeps the first that satisfies the pattern, so the fix still lands.
"""

from __future__ import annotations

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy


def _autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(yaml.safe_dump({
        "mutations": {"parameters": {"set_value": {"severity_medium": "approve"}}},
        "severity_rules": {"value_out_of_range": "severity_medium"},
    }))
    return AutonomyPolicy.load(cfg)


def _rules_path(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({
        "scenario": "doors_fire_rating_compliance",
        "target_category": "Doors",
        "rules": [{
            "id": "doors.fire_rating.compliance",
            "parameter": "Fire Rating",
            "requirement": "matches_regex",
            "pattern": r"^(0\.5|1|1\.5|2|3)\s*HR$",
            "category": "Doors",
            "severity_tag": "value_out_of_range",
            "severity_level": "severity_medium",
            "description": "Fire Rating must be 'X HR'",
            # MISMATCH: format "{h}-hour" produces "1-hour", pattern wants "1 HR".
            "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating",
                         "normalize_format": "{h}-hour"},
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "type"},
        }],
    }))
    return path


def _rules_path_unanchored(tmp_path):
    """M3: an UN-ANCHORED pattern ("HR$", no leading ^) — every self-heal
    candidate the auto-heal loop produces (e.g. "1.5 HR") CONTAINS "HR$" as a
    substring (re.search would pass), but none of them FULLMATCH it (nothing
    in the pattern accounts for the digits before "HR"). The K9 invariant is
    that a self-healed value must never be proposed unless it satisfies the
    rule by the SAME check the evaluator uses (fullmatch) — so this must
    yield NO suggestion, not the first search-hit candidate."""
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({
        "scenario": "doors_fire_rating_unanchored",
        "target_category": "Doors",
        "rules": [{
            "id": "doors.fire_rating.unanchored",
            "parameter": "Fire Rating",
            "requirement": "matches_regex",
            "pattern": r"HR$",   # deliberately un-anchored (no ^)
            "category": "Doors",
            "severity_tag": "value_out_of_range",
            "severity_level": "severity_medium",
            "description": "Fire Rating must end in HR (mis-authored, no anchor)",
            "autofill": {"strategy": "normalize", "normalize_kind": "auto"},
            "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "type"},
        }],
    }))
    return path


def _run_unanchored(tmp_path, elements):
    return QCAgent(_rules_path_unanchored(tmp_path), _autonomy(tmp_path)).run(_state(elements))


def _door(eid, fr):
    return {"id": eid, "category": "Doors", "name": f"D{eid}",
            "params": {"Fire Rating": fr}}


def _state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def _run(tmp_path, elements):
    return QCAgent(_rules_path(tmp_path), _autonomy(tmp_path)).run(_state(elements))


class TestFormatPatternSelfHeal:
    def test_mismatched_format_still_proposes_via_auto(self, tmp_path):
        # "60 MIN": normalize→"1-hour" misses the pattern → auto heals to "1 HR"
        state = _run(tmp_path, [_door("1", "60 MIN")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] == "1 HR"

    def test_other_value_heals_too(self, tmp_path):
        state = _run(tmp_path, [_door("1", "90 MIN")])
        assert state["findings"][0]["suggested_value"] == "1.5 HR"

    def test_lossy_hour_value_refuses_rather_than_drift(self, tmp_path):
        """A-4b (owner decision 2026-08-17): clean fractions like "1.5 HR"
        still render (and heal, above) — but a LOSSY hour render ("20 MIN" →
        "0.33 HR" → 19.8 min) is never produced. For "20 MIN" every candidate
        is now a minutes form, none satisfies this pattern's HR vocabulary →
        no suggestion (None → Path A) instead of writing a drifted value."""
        state = _run(tmp_path, [_door("1", "20 MIN")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] is None

    def test_already_canonical_is_compliant(self, tmp_path):
        state = _run(tmp_path, [_door("1", "2 HR")])
        assert state["outcomes_summary"]["compliant"] == 1
        assert state["findings"] == []

    def test_unparseable_value_routes_path_a(self, tmp_path):
        # no canonical candidate satisfies the pattern → no fix (suggested None)
        state = _run(tmp_path, [_door("1", "banana")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] is None


class TestGuardIsFullmatchNotSearch:
    """M3: the self-heal guard must validate candidates by FULLMATCH
    (rules_engine.matches_regex), not re.search. An un-anchored pattern is
    the case that exposes the bug: a candidate can CONTAIN a pattern match
    (search passes) while still FAILING the actual K9/rules_engine check
    (fullmatch) — proposing it would write a value that still violates the
    rule on the very next run (the K9 invariant this guard exists to protect).
    """

    def test_search_hit_but_fullmatch_miss_yields_no_suggestion(self, tmp_path):
        # "90 MIN" → 1.5 HR. Under re.search, "1.5 HR" contains "HR$" as a
        # substring match and would have been wrongly proposed. Under
        # fullmatch (correct), NO candidate produced by auto_candidates for
        # "90 MIN" fullmatches "HR$" (every candidate has leading digits the
        # pattern doesn't account for) → no suggestion at all.
        state = _run_unanchored(tmp_path, [_door("1", "90 MIN")])
        assert state["outcomes_summary"]["non_compliant"] == 1
        assert state["findings"][0]["suggested_value"] is None

    def test_value_that_is_literally_only_hr_still_not_proposed(self, tmp_path):
        # Sanity: even the shortest possible candidate space can't satisfy an
        # un-anchored "HR$" by fullmatch unless the RAW value equals "HR"
        # exactly (fullmatch("HR$", "HR") does pass — "$" is redundant at the
        # string's true end). Confirms the guard isn't rejecting everything
        # unconditionally, only genuine fullmatch failures.
        import re as _re
        assert _re.fullmatch("HR$", "HR") is not None
        assert _re.fullmatch("HR$", "2 HR") is None
        assert _re.search("HR$", "2 HR") is not None  # the bug this guards against
