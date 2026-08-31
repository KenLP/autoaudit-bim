"""Tests for v1.4-K10 consolidated requirements.

numeric_compare (operator+threshold, subsumes positive_number/numeric_min),
relation_compare (cross-element, subsumes fire_rating_ge), the universal
scope_filter, and severity_level decoupling.
"""

from __future__ import annotations

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.rules_engine import (
    evaluate,
    numeric_compare,
    relation_compare,
)
from tests.test_design_agent_path_b import _autonomy


class TestNumericCompare:
    @pytest.mark.parametrize("op,val,thr,ok", [
        (">", 5, 0, True), (">", 0, 0, False),          # subsumes positive_number
        (">=", 10, 10, True), (">=", 9, 10, False),      # subsumes numeric_min
        ("<", 3, 5, True), ("<=", 5, 5, True),
        ("==", 2, 2, True), ("!=", 2, 3, True),
    ])
    def test_operators(self, op, val, thr, ok):
        assert numeric_compare(val, thr, op) is ok

    @pytest.mark.parametrize("bad", [None, True, "abc", float("nan")])
    def test_non_numeric_fails(self, bad):
        assert numeric_compare(bad, 0, ">=") is False

    def test_via_evaluate(self):
        assert evaluate("numeric_compare", 7, threshold=5, operator=">") is True
        assert evaluate("numeric_compare", 7, threshold=10, operator=">=") is False


class TestRelationCompare:
    def test_fire_rating_kind_parses_units(self):
        # door "120 MIN" >= host "2 HR" (120 min) → ok
        assert relation_compare("120 MIN", "2 HR", operator=">=", compare_kind="fire_rating")
        # door "1 HR" (60) < host "2 HR" (120) → fail
        assert not relation_compare("1 HR", "2 HR", operator=">=", compare_kind="fire_rating")

    def test_missing_reference_passes(self):
        # no host rating → out of scope → pass (matches fire_rating_ge)
        assert relation_compare(None, None, operator=">=", compare_kind="fire_rating")
        assert relation_compare(5, None, operator=">=", compare_kind="numeric")

    def test_numeric_kind(self):
        assert relation_compare(10, 8, operator=">", compare_kind="numeric")
        assert not relation_compare(8, 10, operator=">=", compare_kind="numeric")

    def test_string_kind(self):
        assert relation_compare("A", "A", operator="==", compare_kind="string")
        assert relation_compare(" A ", "A", operator="==", compare_kind="string")
        assert relation_compare("A", "B", operator="!=", compare_kind="string")

    def test_via_evaluate(self):
        assert evaluate(
            "relation_compare", "3 HR", other_value="2 HR",
            operator=">=", compare_kind="fire_rating",
        ) is True


def _qc(tmp_path, rule: dict) -> QCAgent:
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump({
        "scenario": "s", "target_category": "Doors", "rules": [rule],
    }))
    return QCAgent(rules_path=p, autonomy=_autonomy(tmp_path))


def _state(elements):
    return {
        "project_id": "p", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


class TestScopeFilter:
    _RULE = {
        "id": "ext.rating", "parameter": "Fire Rating",
        "requirement": "present_and_nonempty", "category": "Doors",
        "severity_tag": "x", "description": "external doors need a rating",
        "autofill": {"strategy": "none"},
        "scope_filter": {"param": "IsExternal", "pattern": "(?i)^(true|yes)$"},
    }

    def test_out_of_scope_element_skipped(self, tmp_path):
        qc = _qc(tmp_path, self._RULE)
        # internal door (IsExternal=No), blank rating → must be SKIPPED, not flagged
        out = qc.run(_state([
            {"id": "1", "category": "Doors", "params": {"IsExternal": "No", "Fire Rating": ""}},
        ]))
        assert out["outcomes_summary"]["total"] == 0
        assert len(out["findings"]) + len(out.get("missing_data_items", [])) == 0

    def test_in_scope_element_checked(self, tmp_path):
        qc = _qc(tmp_path, self._RULE)
        out = qc.run(_state([
            {"id": "2", "category": "Doors", "params": {"IsExternal": "Yes", "Fire Rating": ""}},
        ]))
        # external door with blank rating → evaluated → missing_data
        assert out["outcomes_summary"]["total"] == 1
        assert len(out.get("missing_data_items", [])) == 1


class TestSeverityLevel:
    def test_explicit_level_wins_over_tag(self, tmp_path):
        rule = {
            "id": "n", "parameter": "Mark", "requirement": "matches_regex",
            "pattern": "^X$", "category": "Doors", "severity_tag": "naming_violation",
            "severity_level": "severity_high", "description": "d",
            "autofill": {"strategy": "none"},
        }
        qc = _qc(tmp_path, rule)
        out = qc.run(_state([
            {"id": "3", "category": "Doors", "params": {"Mark": "WRONG"}},
        ]))
        assert out["findings"][0]["severity"] == "severity_high"


class TestAutoNormalize:
    """v1.4-K16 — normalize_kind="auto": the engine self-proposes a fix by trying
    every deterministic canonicaliser and keeping the one matching the pattern.
    The rule declares NOTHING but the pattern."""

    _RULE = {
        "id": "fr.auto", "parameter": "Fire Rating", "category": "Doors",
        "requirement": "matches_regex", "pattern": r"^(0\.5|1|1\.5|2|3)\s+HR$",
        "severity_tag": "x", "severity_level": "severity_medium",
        "description": "X HR", "fixability": "auto",
        "autofill": {"strategy": "normalize", "normalize_kind": "auto"},
        "remediation": {"action": "set_parameter", "target": "type"},
    }

    def _run(self, tmp_path, value):
        qc = _qc(tmp_path, self._RULE)
        return qc.run(_state([{"id": "1", "category": "Doors",
                               "params": {"Fire Rating": value}}]))

    def test_picks_pattern_matching_form(self, tmp_path):
        out = self._run(tmp_path, "180 MIN")
        assert out["findings"][0]["suggested_value"] == "3 HR"

    def test_already_compliant(self, tmp_path):
        out = self._run(tmp_path, "3 HR")
        assert out["outcomes_summary"]["compliant"] == 1

    def test_unfixable_to_path_a(self, tmp_path):
        out = self._run(tmp_path, "NR")
        f = out["findings"][0]
        assert f["status"] == "non_compliant" and f["suggested_value"] is None


class TestCanonicalFormat:
    """v1.4-K12 — check + fix from ONE declaration (the normalize autofill).
    Compliant iff already canonical; the fix is that same canonical form."""

    _RULE = {
        "id": "fr.canon", "parameter": "Fire Rating", "category": "Doors",
        "requirement": "canonical_format", "severity_tag": "x",
        "severity_level": "severity_medium", "description": "canonical",
        "fixability": "auto",
        "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating",
                     "normalize_format": "{h} HR"},
        "remediation": {"action": "set_parameter", "target": "type"},
    }

    def _run(self, tmp_path, value):
        qc = _qc(tmp_path, self._RULE)
        return qc.run(_state([{"id": "1", "category": "Doors",
                               "params": {"Fire Rating": value}}]))

    def test_already_canonical_is_compliant(self, tmp_path):
        out = self._run(tmp_path, "3 HR")
        assert out["outcomes_summary"]["compliant"] == 1
        assert out["findings"] == []

    def test_non_canonical_flagged_and_fixed(self, tmp_path):
        out = self._run(tmp_path, "180 MIN")
        f = out["findings"][0]
        assert f["status"] == "non_compliant"
        assert f["suggested_value"] == "3 HR"   # fix == canonical form

    def test_unparseable_flagged_unfixable(self, tmp_path):
        # L-03: this used to pass "NR" as its example of "unparseable". It is
        # not — it is the explicit not-rated SENTINEL (see the class below).
        # The intent here (a value the normalizer cannot canonicalise routes
        # to Path A, unfixable) is still exactly right, so it keeps its
        # coverage with a value that is genuinely junk: this door DOES carry a
        # rating, we just cannot read it.
        out = self._run(tmp_path, "2 HR (UL U419)")
        f = out["findings"][0]
        assert f["status"] == "non_compliant"
        assert f["suggested_value"] is None      # → routes to Path A


class TestNotRatedSentinelIsExempt:
    """L-03 (2026-07-25 live review) — "NR" is a valid BIM statement ("this
    door carries no fire-resistance requirement"), not a malformed duration.

    Live on Snowdon, a `canonical_format` + `normalize_kind: duration` rule on
    Fire Rating flagged 116/149 doors, 51 of them purely because their value
    was `NR`. A BIM manager's first run therefore came back essentially all
    red, which is how a tool loses its reader. The engine already knew the
    sentinel on the `lookup_table` path (a not-rated host imposes no
    requirement -> exempt); the normalize path did not, so one concept had two
    disagreeing definitions. They now share `fire_rating_units.is_not_rated`.
    """

    _RULE = TestCanonicalFormat._RULE

    def _run(self, tmp_path, value):
        qc = _qc(tmp_path, self._RULE)
        return qc.run(_state([{"id": "1", "category": "Doors",
                               "params": {"Fire Rating": value}}]))

    @pytest.mark.parametrize("value", ["NR", "nr", "None", "-", "Not Rated", "0 HR"])
    def test_sentinel_is_exempt_not_a_violation(self, tmp_path, value):
        out = self._run(tmp_path, value)
        assert out["findings"] == [], f"{value!r} was flagged as a violation"
        assert out["outcomes_summary"]["compliant"] == 1

    def test_exempt_is_recorded_as_exempt_not_silently_passed(self, tmp_path):
        # The check does not APPLY; that is not the same as passing it. The
        # report must be able to tell them apart — same `exempt` flag the
        # lookup path already sets.
        out = self._run(tmp_path, "NR")
        rec = out["check_trace"][0]
        assert rec["status"] == "compliant"
        assert rec.get("exempt") is True

    def test_blank_is_still_missing_data_not_exempt(self, tmp_path):
        # Absent is a DIFFERENT verdict from "declared as unrated" — the
        # element never made the statement, so there is nothing to exempt.
        out = self._run(tmp_path, "")
        assert out["outcomes_summary"]["missing_data"] == 1
        assert out["outcomes_summary"]["compliant"] == 0

    def test_unreadable_rating_is_still_non_compliant(self, tmp_path):
        # The dangerous direction: a door that DOES carry a rating we cannot
        # parse must never be exempted — that would silently pass a possibly
        # under-spec element, the exact false negative this system exists to
        # catch.
        out = self._run(tmp_path, "2 HR (UL U419)")
        assert out["findings"][0]["status"] == "non_compliant"

    def test_sentinel_on_a_non_duration_rule_still_fails(self, tmp_path):
        # Scope guard: "NR" only means no-rating-required for a DURATION. On
        # any other dimension it is just an unparseable value.
        rule = {
            **TestCanonicalFormat._RULE,
            "id": "len.canon",
            "parameter": "Width",
            "autofill": {"strategy": "normalize", "normalize_kind": "length",
                         "normalize_format": "{mm}"},
        }
        qc = _qc(tmp_path, rule)
        out = qc.run(_state([{"id": "1", "category": "Doors",
                              "params": {"Width": "NR"}}]))
        assert out["findings"][0]["status"] == "non_compliant"
