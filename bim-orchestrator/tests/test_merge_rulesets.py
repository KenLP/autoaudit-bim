"""Tests for multi-scenario merge (v1.4-K6).

`merge_rulesets` + `duplicate_rule_ids` (pure) and QCAgent accepting several
rules paths. The hard contract: a SINGLE input is identity (the common
one-file case is byte-for-byte the old behaviour).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.rules_schema import (
    GeometryRule,
    Rule,
    RuleAutofill,
    RuleSet,
    duplicate_rule_ids,
    merge_rulesets,
)

AUTONOMY_PATH = Path(__file__).resolve().parents[1] / "config" / "autonomy.yaml"


def _rule(rule_id: str, parameter: str = "Department") -> Rule:
    return Rule(
        id=rule_id,
        parameter=parameter,
        requirement="present_and_nonempty",
        severity_tag="quality_change",
        description=f"{rule_id}",
        autofill=RuleAutofill(strategy="none"),
    )


def _geo(rule_id: str) -> GeometryRule:
    return GeometryRule(
        id=rule_id,
        category="Ducts",
        check_type="clearance_min",
        description=f"{rule_id}",
        threshold_mm=2400.0,
    )


def _rs(scenario: str, target: str | list[str], rules, geo=None) -> RuleSet:
    return RuleSet(
        scenario=scenario,
        target_category=target,
        rules=list(rules),
        geometry_rules=list(geo or []),
    )


class TestRuleSetIdentity:
    """P1-02: a duplicate id inside ONE file is invalid input.

    QC iterates the rules LIST (both duplicates fire, each stamping its own
    severity) while DesignAgent looks rules up in a `{id: rule}` DICT (last
    wins) — so a finding raised by the first rule gets remediated with the
    second rule's fixability/remediation. Rejecting at load is the only place
    that can catch it; nothing downstream can tell the two apart.
    """

    def test_duplicate_rule_id_in_one_file_is_rejected(self):
        with pytest.raises(ValidationError, match="duplicate rule id"):
            _rs("dup", "Rooms", [_rule("dept"), _rule("dept", parameter="Other")])

    def test_geometry_rule_may_not_reuse_a_parameter_rule_id(self):
        # One namespace: both land in the same report and the same --rule filter.
        with pytest.raises(ValidationError, match="duplicate rule id"):
            _rs("dup", "Rooms", [_rule("clash")], geo=[_geo("clash")])

    def test_distinct_ids_still_load(self):
        rs = _rs("ok", "Rooms", [_rule("a"), _rule("b")], geo=[_geo("g")])
        assert [r.id for r in rs.rules] == ["a", "b"]


class TestStrictSchema:
    """P1-04: an unknown key must fail loudly, never be silently dropped."""

    def test_typo_on_a_top_level_field_is_rejected(self):
        # `requires_humann` would silently have become requires_human=False.
        with pytest.raises(ValidationError, match="requires_humann"):
            Rule.model_validate({
                "id": "x", "parameter": "P", "requirement": "present_and_nonempty",
                "severity_tag": "quality_change", "description": "d",
                "autofill": {"strategy": "none"}, "requires_humann": True,
            })

    def test_typo_on_a_nested_object_is_rejected(self):
        # A misspelt scope_filter would silently widen the rule to every element.
        with pytest.raises(ValidationError, match="scope_filterr"):
            Rule.model_validate({
                "id": "x", "parameter": "P", "requirement": "present_and_nonempty",
                "severity_tag": "quality_change", "description": "d",
                "autofill": {"strategy": "none"},
                "scope_filterr": {"param": "Function", "pattern": "External"},
            })

    def test_typo_inside_autofill_is_rejected(self):
        with pytest.raises(ValidationError, match="normalize_kindd"):
            Rule.model_validate({
                "id": "x", "parameter": "P", "requirement": "canonical_format",
                "severity_tag": "quality_change", "description": "d",
                "autofill": {"strategy": "normalize", "normalize_kindd": "auto"},
            })

    def test_ruleset_metadata_stays_free_form(self):
        # The provenance bag is a declared dict — strictness must not touch it.
        rs = RuleSet(
            scenario="s", target_category="Rooms", rules=[_rule("a")],
            metadata={"source": "BEP.pdf", "anything": {"nested": [1, 2]}},
        )
        assert rs.metadata["anything"] == {"nested": [1, 2]}


class TestMergeRulesets:
    def test_single_input_is_identity(self):
        rs = _rs("solo", "Rooms", [_rule("a")])
        merged = merge_rulesets([rs])
        assert merged is rs  # same object — no copy, no scenario rename

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            merge_rulesets([])

    def test_two_rulesets_concat_rules(self):
        merged = merge_rulesets([
            _rs("params", "Rooms", [_rule("dept"), _rule("occ")]),
            _rs("naming", "Doors", [_rule("mark")]),
        ])
        ids = [r.id for r in merged.rules]
        assert ids == ["dept", "occ", "mark"]
        assert merged.scenario == "params+naming"
        assert merged.metadata == {"merged_from": ["params", "naming"]}

    def test_identical_duplicate_collapses_to_one(self):
        """The same rule shipped in two packs is not a conflict — dedup it."""
        merged = merge_rulesets([
            _rs("a", "Rooms", [_rule("dept", parameter="Department")]),
            _rs("b", "Rooms", [_rule("dept", parameter="Department")]),
        ])
        assert [r.id for r in merged.rules] == ["dept"]

    def test_differing_duplicate_is_a_hard_error(self):
        """Was: silent first-wins. Now: refuse the merge.

        `rule.id` is the key QC findings, ACC issues, approval records and the
        `--rule` filter all join on. Letting the second pack's DIFFERENT rule
        vanish while its id still resolved to the first pack's remediation is an
        identity swap, not a precedence rule — the run would report findings
        under an id whose meaning the operator never selected.
        """
        with pytest.raises(ValueError, match="different definition"):
            merge_rulesets([
                _rs("a", "Rooms", [_rule("dept", parameter="Department")]),
                _rs("b", "Rooms", [_rule("dept", parameter="SHADOWED")]),
            ])

    def test_error_names_both_source_scenarios(self):
        # An operator needs to know WHICH two packs disagree, not just the id.
        with pytest.raises(ValueError) as exc:
            merge_rulesets([
                _rs("naming_deterministic", "Rooms", [_rule("dept", parameter="A")]),
                _rs("naming_llm", "Rooms", [_rule("dept", parameter="B")]),
            ])
        assert "naming_deterministic" in str(exc.value)
        assert "naming_llm" in str(exc.value)

    def test_target_category_union_dedup(self):
        merged = merge_rulesets([
            _rs("a", "Rooms", [_rule("x")]),
            _rs("b", ["Doors", "Rooms"], [_rule("y")]),
        ])
        # Order preserved, deduped → Rooms (from a) then Doors (new from b)
        assert merged.target_category == ["Rooms", "Doors"]

    def test_single_category_stays_bare_string(self):
        merged = merge_rulesets([
            _rs("a", "Rooms", [_rule("x")]),
            _rs("b", "Rooms", [_rule("y")]),
        ])
        assert merged.target_category == "Rooms"  # not ["Rooms"]

    def test_geometry_rules_merged_and_deduped(self):
        merged = merge_rulesets([
            _rs("a", "Ducts", [_rule("p1")], geo=[_geo("g1")]),
            _rs("b", "Ducts", [_rule("p2")], geo=[_geo("g1"), _geo("g2")]),
        ])
        assert [g.id for g in merged.geometry_rules] == ["g1", "g2"]


class TestDuplicateRuleIds:
    def test_reports_cross_file_collisions(self):
        dups = duplicate_rule_ids([
            _rs("a", "Rooms", [_rule("dept"), _rule("occ")]),
            _rs("b", "Rooms", [_rule("dept")], geo=[_geo("g1")]),
            _rs("c", "Ducts", [_rule("x")], geo=[_geo("g1")]),
        ])
        assert set(dups) == {"dept", "g1"}

    def test_no_collisions_empty(self):
        assert duplicate_rule_ids([
            _rs("a", "Rooms", [_rule("x")]),
            _rs("b", "Doors", [_rule("y")]),
        ]) == []


class TestQCAgentMultiPath:
    def _write(self, path: Path, scenario: str, target: str, rule_id: str) -> Path:
        path.write_text(
            yaml.safe_dump({
                "scenario": scenario,
                "target_category": target,
                "rules": [{
                    "id": rule_id,
                    "parameter": "Department",
                    "requirement": "present_and_nonempty",
                    "severity_tag": "quality_change",
                    "description": rule_id,
                    "autofill": {"strategy": "none"},
                }],
            }),
            encoding="utf-8",
        )
        return path

    def test_single_path_unchanged(self, tmp_path):
        p = self._write(tmp_path / "a.yaml", "solo", "Rooms", "dept")
        qc = QCAgent(rules_path=p, autonomy=AutonomyPolicy.load(AUTONOMY_PATH))
        assert qc.rules.scenario == "solo"
        assert [r.id for r in qc.rules.rules] == ["dept"]

    def test_list_of_paths_merges(self, tmp_path):
        p1 = self._write(tmp_path / "a.yaml", "params", "Rooms", "dept")
        p2 = self._write(tmp_path / "b.yaml", "naming", "Doors", "mark")
        qc = QCAgent(rules_path=[p1, p2], autonomy=AutonomyPolicy.load(AUTONOMY_PATH))
        assert qc.rules.scenario == "params+naming"
        assert sorted(r.id for r in qc.rules.rules) == ["dept", "mark"]
        assert qc.rules.target_category == ["Rooms", "Doors"]


class TestAuditProfilePartialCoveragePolicy:
    """Owner decision 2026-07-25: opt-IN strictness, settable per profile."""

    def test_default_is_permissive(self):
        from bim_orchestrator.policies.audit_profile import AuditRunOptions

        assert AuditRunOptions().fail_on_partial_coverage is False

    def test_profile_can_demand_full_coverage(self):
        from bim_orchestrator.policies.audit_profile import AuditRunOptions

        opts = AuditRunOptions(fail_on_partial_coverage=True)
        assert opts.fail_on_partial_coverage is True
