"""Tests for report_trace — the structured (element, rule) trace + outcome join.

Two halves:
  * build_check_record captures the QC evidence (value pulled + comparison
    operands) per requirement type, incl. the PASS set.
  * index_fixes / outcome_for join the Design/Result stage (Path A vs B, ACC
    issue, before -> after) back onto each record from proposed_fixes alone —
    DesignAgent is never modified.

Plus one end-to-end check that QCAgent actually emits check_trace through the
real query+mock pipeline (the fire-door relation_compare+lookup scenario).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from types import SimpleNamespace

import pytest

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.revit_query import _design_option_of
from bim_orchestrator.audit_report import render_audit_report
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill
from bim_orchestrator.report_trace import (
    build_check_record,
    index_fixes,
    outcome_for,
)
from tests._mocks import MockRevitMCPClient

CONFIG = Path(__file__).resolve().parents[1] / "config"


def mk_rule(requirement: str, **kw) -> Rule:
    defaults = dict(
        id=kw.pop("id", f"test.{requirement}"),
        parameter=kw.pop("parameter", "Fire Rating"),
        requirement=requirement,
        severity_tag="rule_violation",
        description="trace test rule",
        autofill=RuleAutofill(strategy="none"),
    )
    defaults.update(kw)
    return Rule(**defaults)  # type: ignore[arg-type]


def _element(eid="200", name="Door A", category="Doors", **params):
    return {"id": eid, "name": name, "category": category, "params": params}


# ── build_check_record: identity + verdict ───────────────────────────────────


def test_build_record_carries_identity_and_verdict():
    rule = mk_rule("present_and_nonempty", parameter="Department")
    el = _element(eid="42", name="Closet 11A", category="Rooms", Department="")
    rec = build_check_record(
        rule, el, raw_value="", value="", passed=False, status="missing_data"
    )
    assert rec["rule_id"] == "test.present_and_nonempty"
    assert rec["element_id"] == "42"
    assert rec["element_name"] == "Closet 11A"
    assert rec["revit_element_id"] == 42  # integer Revit id → usable in Select-by-ID
    assert rec["status"] == "missing_data"
    assert rec["passed"] is False
    assert rec["category"] == "Rooms"


def test_build_record_acc_urn_has_no_revit_id():
    rule = mk_rule("present_and_nonempty", parameter="Department")
    el = _element(eid="urn:adsk:long-base64-blah", name="Room", category="Rooms")
    rec = build_check_record(rule, el, raw_value=None, value=None,
                             passed=False, status="missing_data")
    assert rec["revit_element_id"] is None  # URN → fall back to viewer path


def test_build_record_type_level_param_shows_family_type_name():
    """Mirror QC's name choice: a TYPE-level param shows 'family - type'."""
    rule = mk_rule("relation_compare", parameter="Fire Rating")
    el = {
        "id": "200", "name": "36in", "category": "Doors",
        "params": {
            "Fire Rating": "180 MIN", "type.Fire Rating": "180 MIN",
            "_family_name": "Door-Single-Flush", "_type_name": "36in (180 MIN)",
        },
    }
    rec = build_check_record(rule, el, raw_value="180 MIN", value="180 MIN",
                             passed=True, status="compliant")
    assert rec["element_name"] == "Door-Single-Flush - 36in (180 MIN)"


# ── build_check_record: operands per requirement type ────────────────────────


def test_build_record_numeric_captures_threshold_operator_unit():
    rule = mk_rule("numeric_compare", parameter="Width",
                   operator=">=", threshold=0.9, unit="m")
    el = _element(category="Doors", Width=0.8)
    rec = build_check_record(rule, el, raw_value=0.8, value=0.8,
                             passed=False, status="non_compliant", severity="severity_high")
    assert rec["threshold"] == 0.9
    assert rec["operator"] == ">="
    assert rec["unit"] == "m"
    assert rec["severity"] == "severity_high"


def test_build_record_regex_captures_pattern():
    rule = mk_rule("matches_regex", parameter="Type Mark", pattern=r"[A-Z]\d{2}")
    el = _element(category="Walls")
    rec = build_check_record(rule, el, raw_value="bad", value="bad",
                             passed=False, status="non_compliant")
    assert rec["pattern"] == r"[A-Z]\d{2}"


def test_build_record_relation_lookup_uses_required_as_operand():
    rule = mk_rule("relation_compare", parameter="Fire Rating",
                   operator=">=", lookup="ibc716", compare_kind="fire_rating")
    el = _element(category="Doors", **{"Fire Rating": "60 MIN"})
    rec = build_check_record(
        rule, el, raw_value="60 MIN", value="60 MIN", passed=False,
        status="non_compliant", other_value="90 min", required="90 min",
    )
    assert rec["operand"] == "90 min"
    assert rec["operand_source"] == "lookup table 'ibc716'"
    assert rec["operator"] == ">="


def test_build_record_relation_without_lookup_uses_other_param():
    rule = mk_rule("relation_compare", parameter="Fire Rating",
                   operator=">=", other_param="host.Fire Rating",
                   compare_kind="fire_rating")
    el = _element(category="Doors", **{"Fire Rating": "60 MIN"})
    rec = build_check_record(rule, el, raw_value="60 MIN", value="60 MIN",
                             passed=False, status="non_compliant",
                             other_value="2 HR")
    assert rec["operand"] == "2 HR"
    assert rec["operand_source"] == "host.Fire Rating"


def test_build_record_exempt_flag_set():
    rule = mk_rule("relation_compare", parameter="Fire Rating", lookup="ibc716")
    el = _element(category="Doors")
    rec = build_check_record(rule, el, raw_value="NR", value="NR",
                             passed=True, status="compliant", exempt=True)
    assert rec["exempt"] is True


# ── index_fixes / outcome_for / normalize_outcome ────────────────────────────


def _fix(finding_id, *, executed=False, new_value=None, preview=None, autonomy="auto"):
    return {
        "finding_id": finding_id, "element_id": finding_id.split("::")[-1],
        "parameter": "Fire Rating", "new_value": new_value, "autonomy": autonomy,
        "approval_token": None, "preview": preview or {}, "executed": executed,
    }


def test_outcome_path_b_committed_write():
    fix = _fix("r1::200", executed=True, new_value="2 HR", preview={
        "executed_via": "revit_batch", "old_value": "60 MIN",
        "data": {"changes": {"before": "60 MIN", "after": "2 HR"}},
        "action": "set_parameter", "target": "type", "rule_id": "r1",
    })
    by_el, by_rb = index_fixes([fix])
    rec = {"rule_id": "r1", "element_id": "200", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out is not None
    assert out["path"] == "B"
    assert out["before"] == "60 MIN"
    assert out["after"] == "2 HR"
    assert out["executed"] is True


def test_outcome_path_a_grouped_joins_by_rule_bucket():
    fix = _fix("rulegroup::r2::manual_review", executed=True, preview={
        "executed_issue": {"id": "issue-mock-1", "displayId": 1001,
                           "title": "[AutoAudit] Doors", "status": "open"},
        "grouped_rule_ids": ["r2"], "grouped_count": 3,
    })
    by_el, by_rb = index_fixes([fix])
    rec = {"rule_id": "r2", "element_id": "200", "status": "manual_review"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out is not None
    assert out["path"] == "A"
    assert out["issue_display_id"] == 1001
    assert out["issue_status"] == "open"


def test_outcome_path_b_proposal_awaiting_approval():
    fix = _fix("r3::200", executed=False, new_value="2 HR", autonomy="approve",
               preview={"proposal_issue_id": "issue-mock-9", "old_value": "",
                        "action": "set_parameter"})
    by_el, by_rb = index_fixes([fix])
    rec = {"rule_id": "r3", "element_id": "200", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out["path"] == "B-proposal"
    assert out["proposal_issue_id"] == "issue-mock-9"
    assert out["after"] == "2 HR"


def test_outcome_parked():
    fix = _fix("r4::200", executed=False, preview={"reason": "issue budget reached"})
    by_el, by_rb = index_fixes([fix])
    rec = {"rule_id": "r4", "element_id": "200", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out["path"] == "parked"
    assert out["reason"] == "issue budget reached"


def test_outcome_none_for_compliant_with_no_fix():
    by_el, by_rb = index_fixes([])
    rec = {"rule_id": "r5", "element_id": "200", "status": "compliant"}
    assert outcome_for(rec, by_el, by_rb) is None  # type: ignore[arg-type]


def test_normalize_outcome_prefers_per_element_over_bucket():
    """A per-element fix wins over a same-rule grouped fix (precedence)."""
    el_fix = _fix("r6::200", executed=True, new_value="X", preview={"executed_via": "x"})
    grp_fix = _fix("rulegroup::r6::non_compliant", executed=True, preview={
        "executed_issue": {"id": "i", "displayId": 1}})
    by_el, by_rb = index_fixes([el_fix, grp_fix])
    rec = {"rule_id": "r6", "element_id": "200", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out["path"] == "B"  # per-element write, not the grouped issue


def test_outcome_sibling_dedup_by_type_write_resolves_via_write_eid():
    """Low4: two door INSTANCES (element_id "200" and "201") share ONE Type
    (Revit type id 500) and one Fire Rating write. DesignAgent's
    ``_dedup_by_write_target`` collapses both to a SINGLE representative
    ProposedFix (finding_id "r7::200"), so ``proposed_fixes`` only ever
    contains that one entry — element "201" has NO fix with its own
    element_id. Before the fix, the record for "201" fell through the
    (rule_id, element_id) join AND the (rule_id, bucket) join (this isn't a
    grouped Path A issue) → outcome_for returned None → rendered "—", which
    reads as "dropped by the issue budget" when it was actually written. Now
    the record carries its own resolved ``_type_id`` (500, stamped by
    build_check_record for a type-level parameter) and index_fixes aliases
    the fix under (rule_id, "500") too, so BOTH records resolve to the SAME
    fix's outcome."""
    fix = _fix("r7::200", executed=True, new_value="2 HR", preview={
        "executed_via": "revit_batch", "old_value": "60 MIN", "write_eid": 500,
        "action": "set_parameter", "target": "type", "rule_id": "r7",
    })
    by_el, by_rb = index_fixes([fix])

    # The record DesignAgent kept as the representative (element_id "200")
    # still resolves via the direct (rule_id, element_id) key.
    rec_representative = {
        "rule_id": "r7", "element_id": "200", "status": "non_compliant",
        "_type_id": 500,
    }
    out_representative = outcome_for(rec_representative, by_el, by_rb)  # type: ignore[arg-type]
    assert out_representative is not None
    assert out_representative["path"] == "B"
    assert out_representative["after"] == "2 HR"

    # The SIBLING instance (element_id "201") has no direct fix entry, but
    # shares the same resolved _type_id (500) — must resolve to the SAME
    # outcome via the write_eid alias, not None/"—".
    rec_sibling = {
        "rule_id": "r7", "element_id": "201", "status": "non_compliant",
        "_type_id": 500,
    }
    out_sibling = outcome_for(rec_sibling, by_el, by_rb)  # type: ignore[arg-type]
    assert out_sibling is not None
    assert out_sibling["path"] == "B"
    assert out_sibling["after"] == "2 HR"
    assert out_sibling["executed"] is True


def test_outcome_write_eid_alias_never_shadows_a_real_per_element_fix():
    """Low4 safety: the write_eid alias must only fill in when the direct
    (rule_id, element_id) key is ABSENT — it must never shadow a real
    per-element fix whose element_id happens to equal another fix's
    write_eid."""
    # fix A is a real per-element write for element "500" itself.
    fix_a = _fix("r8::500", executed=True, new_value="OWN VALUE", preview={
        "executed_via": "x", "rule_id": "r8",
    })
    # fix B writes a TYPE (write_eid 500) on behalf of a DIFFERENT element.
    fix_b = _fix("r8::999", executed=True, new_value="TYPE VALUE", preview={
        "executed_via": "x", "write_eid": 500, "rule_id": "r8",
    })
    by_el, by_rb = index_fixes([fix_a, fix_b])
    rec = {"rule_id": "r8", "element_id": "500", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    # Must resolve to fix A (the element's OWN fix), not fix B's alias.
    assert out["after"] == "OWN VALUE"


# ── end-to-end: QCAgent emits check_trace (fire-door lookup scenario) ─────────


def test_qc_emits_check_trace_for_relation_lookup_scenario():
    async def run():
        autonomy = AutonomyPolicy.load(CONFIG / "autonomy.yaml")
        qc = QCAgent(rules_path=CONFIG / "rules.ibc716_door_rating.yaml",
                     autonomy=autonomy)
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        q = RevitQueryAgent(mcp=mock, rules=qc.rules, catalog=catalog)
        state = {"project_id": "p", "iteration": 0, "max_iterations": 1,
                 "elements": [], "findings": [], "proposed_fixes": [],
                 "status": "init", "error": None}
        state = await q.run(state)
        return qc.run(state)

    state = asyncio.run(run())
    trace = state["check_trace"]
    # One record per (element, rule) — 4 doors x 1 rule, all four buckets real.
    # B-4/B-5 (review round 7, 2026-08-16) upgraded two of these verdicts from
    # a shrug to an answer, and demoted one from a false pass:
    assert len(trace) == 4
    by_id = {r["element_id"]: r for r in trace}
    # Door 202 PASSES: 180 MIN >= required 90 min (the operand is captured)
    assert by_id["202"]["status"] == "compliant"
    assert by_id["202"]["operand"] == "90 min"
    assert by_id["202"]["operand_source"] == "lookup table 'ibc716'"
    # Door 200 (180 MIN in the 4-HR wall): the old table had NO 4-HR row, so
    # the HEAVIEST wall fell to manual_review — B-5 added it (4 HR → 3-hr door),
    # and 180 MIN meets it.
    assert by_id["200"]["status"] == "compliant"
    # Door 201 ("NR" in the same 4-HR wall) is a REAL violation the old table
    # could not see — an unrated door in the heaviest fire wall.
    assert by_id["201"]["status"] == "non_compliant"
    # Door 203's host wall type has a BLANK Fire Rating — the very wall the
    # sibling rule flags as "missing Fire Rating". Pre-B-4 this door was
    # compliant-by-exemption (one rule calling the blank a defect while
    # another treated it as an exemption); now blank → manual_review.
    assert by_id["203"]["status"] == "manual_review"
    assert by_id["203"].get("exempt") is not True


# ── v1.5-R6 wave 1: bound_parameter chain (1.3) ──────────────────────────────


def test_revit_parameter_of_prefers_bound_parameter():
    from bim_orchestrator.report_trace import revit_parameter_of

    rule = mk_rule("present_and_nonempty", parameter="Fire Rating",
                    bound_parameter="FR_RATING_INT")
    assert revit_parameter_of(rule) == "FR_RATING_INT"


def test_revit_parameter_of_falls_back_to_canonical_parameter():
    from bim_orchestrator.report_trace import revit_parameter_of

    rule = mk_rule("present_and_nonempty", parameter="Fire Rating")
    assert revit_parameter_of(rule) == "Fire Rating"


def test_revit_parameter_of_tolerates_simplenamespace_without_bound_parameter():
    """rule_view_from_record's fallback rule has no `bound_parameter` attribute
    at all (pre-fix) — revit_parameter_of must not raise on it."""
    from types import SimpleNamespace

    from bim_orchestrator.report_trace import revit_parameter_of

    rule = SimpleNamespace(parameter="Width")
    assert revit_parameter_of(rule) == "Width"


def test_build_record_stamps_revit_parameter_and_type_detection_uses_it():
    """A bound_parameter rule's `type.<bound>` key (not `type.<canonical>`) is
    what query_specs actually writes on a TYPE-level element — build_check_record
    must detect against the BOUND name or it silently misses the type_id stash."""
    rule = mk_rule("present_and_nonempty", parameter="Fire Rating",
                    bound_parameter="FR_RATING_INT", category="Doors")
    el = _element(category="Doors", **{
        "type.FR_RATING_INT": "90 MIN", "_family_name": "Fam", "_type_name": "Typ",
        "_type_id": 777,
    })
    rec = build_check_record(rule, el, raw_value="90 MIN", value="90 MIN",
                             passed=True, status="compliant")
    assert rec["revit_parameter"] == "FR_RATING_INT"
    assert rec.get("_type_id") == 777
    assert rec["element_name"] == "Fam - Typ"


def test_rule_view_from_record_roundtrips_revit_parameter():
    from bim_orchestrator.report_trace import revit_parameter_of, rule_view_from_record

    rule = mk_rule("present_and_nonempty", parameter="Fire Rating",
                    bound_parameter="FR_RATING_INT", category="Doors")
    el = _element(category="Doors", **{"type.FR_RATING_INT": "90 MIN"})
    rec = build_check_record(rule, el, raw_value="90 MIN", value="90 MIN",
                             passed=True, status="compliant")
    rebuilt = rule_view_from_record(rec)
    assert revit_parameter_of(rebuilt) == "FR_RATING_INT"


# ── v1.5-R6 wave 1: _split_finding_id tolerates "::" in rule_id (3.4) ────────


def test_split_finding_id_per_element_rule_id_with_double_colon():
    from bim_orchestrator.report_trace import _split_finding_id

    rule_id, element_id, bucket = _split_finding_id("doors::fire::ibc716::200")
    assert rule_id == "doors::fire::ibc716"
    assert element_id == "200"
    assert bucket is None


def test_split_finding_id_no_separator_preserves_old_semantics():
    from bim_orchestrator.report_trace import _split_finding_id

    rule_id, element_id, bucket = _split_finding_id("bare-id-no-separator")
    assert rule_id == "bare-id-no-separator"
    assert element_id == ""
    assert bucket is None


# ── v1.5-R6 wave 1: index_fixes skip-stub / prefer-rich (join-hardening) ────


def test_index_fixes_skips_skipped_duplicate_stub_keeps_earlier_real_issue():
    """A Path A group's in-run dedup stub (skipped_duplicate=True, no issue
    info) must never displace the REAL fix from an earlier iteration at the
    same (rule_id, bucket) key."""
    real = _fix("rulegroup::r9::non_compliant", executed=True, preview={
        "executed_issue": {"id": "issue-real", "displayId": 5001, "status": "open"},
    })
    stub = _fix("rulegroup::r9::non_compliant", executed=False, preview={
        "skipped_duplicate": True, "grouped_rule_ids": ["r9"], "grouped_count": 2,
    })
    by_el, by_rb = index_fixes([real, stub])
    fix = by_rb[("r9", "non_compliant")]
    assert fix is real


def test_index_fixes_rich_fix_never_displaced_by_thin_one_same_key():
    """Even without the skipped_duplicate marker, a richer fix (has an issue/
    write) at a key must not be overwritten by a later thinner one (e.g. a
    freshly-built but not-yet-stamped per-element proposal fix)."""
    rich = _fix("r10::300", executed=False, autonomy="approve", preview={
        "proposal_issue_id": "issue-parked-1", "old_value": "", "action": "set_parameter",
    })
    thin = _fix("r10::300", executed=False, autonomy="approve", preview={
        "rule_id": "r10", "old_value": "",
    })
    by_el, by_rb = index_fixes([rich, thin])
    fix = by_el[("r10", "300")]
    assert fix is rich
    assert (fix.get("preview") or {}).get("proposal_issue_id") == "issue-parked-1"


# ── v1.5-R6 wave 1: human-only outcome never reads as a pending write (1.1c) ─


def test_human_only_outcome_prefers_group_issue_over_dangling_per_element_fix():
    """A human-only (LLM safety-critical) fix always leaves a per-element
    dangling preview (write_eid stamped, never executed) AND, once routed,
    a grouped Path A issue at (rule_id, "human_only"). The direct per-element
    lookup must not win — it would misreport 'Revit write (pending)'."""
    dangling = _fix("r11::400", executed=False, new_value="2 HR", autonomy="human-only",
                    preview={"write_eid": 400, "old_value": "60 MIN", "rule_id": "r11"})
    group = _fix("rulegroup::r11::human_only", executed=True, preview={
        "executed_issue": {"id": "issue-ho-1", "displayId": 3001, "status": "open"},
    })
    by_el, by_rb = index_fixes([dangling, group])
    rec = {"rule_id": "r11", "element_id": "400", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out is not None
    assert out["path"] == "human_only"
    assert out["issue_display_id"] == 3001


def test_human_only_outcome_falls_back_honestly_when_no_group_issue_exists():
    """No grouped issue was created yet (e.g. dry-run) — still must never
    claim a pending Revit write for a human-only fix."""
    dangling = _fix("r12::401", executed=False, new_value="2 HR", autonomy="human-only",
                    preview={"write_eid": 401, "old_value": "60 MIN", "rule_id": "r12"})
    by_el, by_rb = index_fixes([dangling])
    rec = {"rule_id": "r12", "element_id": "401", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out is not None
    assert out["path"] == "human_only"


def test_outcome_carries_llm_provenance_for_the_report_tag():
    fix = _fix("r13::500", executed=False, new_value="Storage", autonomy="approve",
               preview={"proposal_issue_id": "issue-llm-1", "value_source": "llm",
                        "evidence": "Storage = residential occupancy (Uniclass)"})
    by_el, by_rb = index_fixes([fix])
    rec = {"rule_id": "r13", "element_id": "500", "status": "non_compliant"}
    out = outcome_for(rec, by_el, by_rb)  # type: ignore[arg-type]
    assert out["value_source"] == "llm"
    assert "residential" in out["evidence"]


class TestUnitAwareDisplay:
    """L-02 (2026-07-25 live review) — a rule reading "Door width must be at
    least 900 mm" reported its evidence as ``2.8333333333333335``: Revit's raw
    storage feet, unlabelled. The verdict was computed correctly in mm; only
    the DISPLAY fell back to ``raw_value``. That is the one thing a
    verification report cannot do — the person signing it could not check the
    number against the rule.

    The converted value was already in the trace next to the raw one, so this
    is a display-precedence fix, not new arithmetic. The comparison still runs
    on ``value``; ``raw_value`` and ``value`` are both still recorded.
    """

    @staticmethod
    def _rule(unit=None, parameter="Width"):
        return SimpleNamespace(
            id="r1", parameter=parameter, requirement="numeric_compare",
            unit=unit, threshold=900, operator=">=", bound_parameter=None,
            lookup=None, other_param=None, compare_kind=None, pattern=None,
        )

    def test_unit_rule_displays_converted_value_with_its_unit(self):
        rec = build_check_record(
            self._rule(unit="mm"),
            {"id": "1", "params": {"Width": 3.0}},
            raw_value=3.0, value=914.4000000000002,
            passed=True, status="compliant",
        )
        assert rec["value_display"] == "914.4 mm"
        # The audit trail keeps BOTH numbers — the fix is what humans read,
        # not what is recorded.
        assert rec["raw_value"] == 3.0
        assert rec["value"] == 914.4000000000002

    def test_no_unit_declared_leaves_display_alone(self):
        rec = build_check_record(
            self._rule(unit=None),
            {"id": "1", "params": {"Width": 3.0}},
            raw_value=3.0, value=3.0, passed=True, status="compliant",
        )
        assert "value_display" not in rec

    def test_revit_display_string_still_wins_when_there_is_no_unit(self):
        # params_display exists to show "L2" instead of a levelId; a
        # unit-less rule must keep getting it.
        rec = build_check_record(
            self._rule(unit=None, parameter="Level"),
            {"id": "1", "params": {"Level": 12345},
             "params_display": {"Level": "L2"}},
            raw_value=12345, value=12345, passed=True, status="compliant",
        )
        assert rec["value_display"] == "L2"

    def test_non_numeric_value_is_never_given_a_unit_suffix(self):
        # A string value has no unit arithmetic behind it; labelling it would
        # be a second kind of lie.
        rec = build_check_record(
            self._rule(unit="mm"),
            {"id": "1", "params": {"Width": "N/A"}},
            raw_value="N/A", value="N/A", passed=False, status="non_compliant",
        )
        assert rec.get("value_display", "N/A") == "N/A"

    def test_report_table_renders_the_unit_cell(self):
        # End of the wire: the renderer reads value_display before raw_value,
        # so the fix has to actually reach the rendered markdown.
        rec = build_check_record(
            self._rule(unit="mm"),
            {"id": "1", "params": {"Width": 3.0}},
            raw_value=3.0, value=914.4000000000002,
            passed=False, status="non_compliant",
        )
        md = render_audit_report({
            "check_trace": [rec], "findings": [], "proposed_fixes": [],
            "outcomes_summary": {"total": 1, "compliant": 0, "non_compliant": 1,
                                 "manual_review": 0, "missing_data": 0},
        })
        assert "914.4 mm" in md
        assert "2.8333333333333335" not in md


class TestEmptyCategoryIsDisclosed:
    """F-01 (2026-07-26 live probe) — a category that RESOLVES but returns zero
    elements used to read as a completed audit.

    Coverage is built at PLAN time, where "resolved" only ever meant *the
    catalog knew this label*, never *the model actually had any*. Live on
    Snowdon (an ARCHITECTURAL host) a "every duct must carry a Mark" rule
    produced 0 check records while the report said `resolved 2` and
    `evaluability 100%`, exit 0, no warning — the ducts were in the linked
    HVAC model. In a federated project, which is the normal case, zero
    elements usually means the rule is pointed at the wrong document.
    """

    @staticmethod
    def _render(coverage, trace=None):
        return render_audit_report({
            "check_trace": trace or [], "findings": [], "proposed_fixes": [],
            "outcomes_summary": {"total": 0, "compliant": 0, "non_compliant": 0,
                                 "manual_review": 0, "missing_data": 0},
            "query_coverage": coverage,
        })

    _COV = {
        "targets_requested": ["Ducts", "Doors"],
        "categories_resolved": ["Ducts", "Doors"],
        "categories_dropped": [],
    }

    def test_empty_category_is_named_and_called_out(self):
        md = self._render({**self._COV, "categories_empty": ["Ducts"]})
        assert "CHECKED NOTHING" in md
        assert "Ducts" in md
        # The distinction that matters to whoever signs it.
        assert "not evidence of compliance" in md

    def test_linked_models_are_named_as_the_likely_explanation(self):
        md = self._render({
            **self._COV,
            "categories_empty": ["Ducts"],
            "linked_models": ["Snowdon Towers Sample HVAC"],
        })
        assert "loaded link" in md
        assert "Snowdon Towers Sample HVAC" in md
        assert "HOST document" in md

    def test_nothing_is_said_when_every_category_had_elements(self):
        # The disclosure must not become background noise on a normal run.
        md = self._render(self._COV)
        assert "CHECKED NOTHING" not in md

    def test_still_rendered_without_the_link_hint(self):
        # An older addin with no get_linked_files still gets the warning; only
        # the hint is missing.
        md = self._render({**self._COV, "categories_empty": ["Ducts"]})
        assert "CHECKED NOTHING" in md
        assert "loaded link" not in md

    def test_a_malformed_coverage_still_wins(self):
        # F-01 must not weaken A-02: shape defects are reported first, since
        # nothing below them can be trusted.
        md = self._render({"targets_requested": 3, "categories_empty": ["Ducts"]})
        assert "COVERAGE DATA IS MALFORMED" in md
        assert "CHECKED NOTHING" not in md


class TestDesignOptionIsDisclosed:
    """F-02 (2026-07-26 live probe) — elements inside a Revit design option
    were audited and reported indistinguishably from the real design.

    A design option holds an ALTERNATIVE: at most one option per set is ever
    built. On Snowdon, 6 of 149 doors sat in
    `Office Units : 301 Iron Bridge Accounting` and produced 24 check records,
    10 of them failures, with nothing in the trace or the report saying so.
    """

    @staticmethod
    def _rule():
        return SimpleNamespace(
            id="r1", parameter="Mark", requirement="present_and_nonempty",
            unit=None, threshold=None, operator=None, bound_parameter=None,
            lookup=None, other_param=None, compare_kind=None, pattern=None,
        )

    def test_the_option_rides_on_the_check_record(self):
        rec = build_check_record(
            self._rule(),
            {"id": "3083667", "params": {"_design_option": "Office Units : 301"}},
            raw_value=None, value=None, passed=False, status="missing_data",
        )
        assert rec["design_option"] == "Office Units : 301"

    def test_main_model_elements_carry_nothing(self):
        # Absent means main model, so old traces read correctly with no
        # migration and normal runs gain no noise.
        rec = build_check_record(
            self._rule(), {"id": "1", "params": {}},
            raw_value="A", value="A", passed=True, status="compliant",
        )
        assert "design_option" not in rec

    def test_report_states_how_many_checks_describe_an_alternative(self):
        recs = [
            build_check_record(
                self._rule(),
                {"id": str(i), "params": {"_design_option": "Office Units : 301"}},
                raw_value=None, value=None, passed=False, status="missing_data",
            )
            for i in range(3)
        ]
        recs.append(build_check_record(
            self._rule(), {"id": "9", "params": {}},
            raw_value="A", value="A", passed=True, status="compliant",
        ))
        md = render_audit_report({
            "check_trace": recs, "findings": [], "proposed_fixes": [],
            "outcomes_summary": {"total": 4, "compliant": 1, "non_compliant": 0,
                                 "manual_review": 0, "missing_data": 3},
        })
        line = next(ln for ln in md.splitlines() if "Design options:" in ln)
        assert "Office Units : 301" in line
        assert "**3** of the checks" in line   # the 3 option records, not the 4th
        assert "**3** failed" in line          # all 3 were missing_data
        assert "never exist" in line

    def test_the_element_row_itself_is_marked(self):
        # A summary line alone is not enough — someone reading one row must
        # see it without cross-referencing.
        rec = build_check_record(
            self._rule(),
            {"id": "3083667", "name": "Door 3C06",
             "params": {"_design_option": "Office Units : 301"}},
            raw_value=None, value=None, passed=False, status="missing_data",
        )
        md = render_audit_report({
            "check_trace": [rec], "findings": [], "proposed_fixes": [],
            "outcomes_summary": {"total": 1, "compliant": 0, "non_compliant": 0,
                                 "manual_review": 0, "missing_data": 1},
        })
        assert "⧉ option: Office Units : 301" in md

    def test_a_run_with_no_options_says_nothing_about_them(self):
        rec = build_check_record(
            self._rule(), {"id": "1", "params": {}},
            raw_value="A", value="A", passed=True, status="compliant",
        )
        md = render_audit_report({
            "check_trace": [rec], "findings": [], "proposed_fixes": [],
            "outcomes_summary": {"total": 1, "compliant": 1, "non_compliant": 0,
                                 "manual_review": 0, "missing_data": 0},
        })
        assert "Design options:" not in md


class TestDesignOptionParsing:
    """F-02 parsing — Revit exposes `Design Option` TWICE on the same element:
    an ElementId (opaque integer) and a String (the readable name). Picking
    the String explicitly, because this value lands in a document a human
    reads and "3082902" tells them nothing."""

    def test_picks_the_string_entry_not_the_elementid(self):
        params = [
            {"name": "Design Option", "storageType": "ElementId",
             "value": 3082902, "valueString": "3082902"},
            {"name": "Design Option", "storageType": "String",
             "value": "Office Units : 301 Iron Bridge Accounting",
             "valueString": "Office Units : 301 Iron Bridge Accounting"},
        ]
        assert _design_option_of(params) == "Office Units : 301 Iron Bridge Accounting"

    def test_main_model_is_not_an_option(self):
        params = [{"name": "Design Option", "storageType": "String",
                   "value": "Main Model", "valueString": "Main Model"}]
        assert _design_option_of(params) is None

    @pytest.mark.parametrize("params", [
        [],
        [{"name": "Mark", "storageType": "String", "value": "A"}],
        [{"name": "Design Option", "storageType": "String", "value": "  "}],
        [{"name": "Design Option", "storageType": "String", "value": None}],
    ])
    def test_absent_or_blank_yields_none(self, params):
        assert _design_option_of(params) is None
