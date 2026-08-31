"""Tests for v1 task BB: 4-state QC outcomes (compliant / non_compliant /
manual_review / missing_data).

These cover only the new behavior. Pre-existing QC tests (test_qc_agent.py,
test_qc_room_compliance.py) are updated in-place to assert against the
correct bucket for their scenario.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent, _is_missing
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import OrchestratorState


# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {"parameters": {"set_value": {"severity_medium": "approve"}}},
                "severity_rules": {
                    "missing_required_param": "severity_medium",
                    "missing_optional_param": "severity_low",
                    "invalid_value_range": "severity_medium",
                    "geometric_violation": "severity_high",
                },
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _rules(tmp_path, rules):
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "test",
                "target_category": "Rooms",
                "rules": rules,
            }
        )
    )
    return path


def _state(elements: list[dict]) -> OrchestratorState:
    return {
        "project_id": "p",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }


def _el(id_: str, params: dict) -> dict:
    return {"id": id_, "category": "Rooms", "name": id_, "params": params}


# ── _is_missing helper ───────────────────────────────────────────────────────


def test_is_missing_recognises_none():
    assert _is_missing(None) is True


def test_is_missing_recognises_empty_and_whitespace_string():
    assert _is_missing("") is True
    assert _is_missing("   ") is True
    assert _is_missing("\t\n") is True


def test_is_missing_does_not_treat_zero_or_false_as_missing():
    # Numeric zero is a real value (Area=0 → non_compliant, not missing).
    # False likewise.
    assert _is_missing(0) is False
    assert _is_missing(0.0) is False
    assert _is_missing(False) is False
    assert _is_missing("0") is False


def test_is_missing_does_not_treat_populated_string_as_missing():
    assert _is_missing("Office") is False


# ── outcomes_summary aggregate ───────────────────────────────────────────────


def test_summary_buckets_sum_to_total(tmp_path, autonomy):
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.min",
                "parameter": "Area",
                "requirement": "numeric_min",
                "threshold": 10.0,
                "severity_tag": "geometric_violation",
                "description": "Area >= 10 m2",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    elements = [
        _el("r1", {"Area": 12.0}),       # compliant
        _el("r2", {"Area": 5.0}),        # non_compliant
        _el("r3", {"Area": None}),       # missing_data
        _el("r4", {"Area": 20.0}),       # compliant
    ]
    result = agent.run(_state(elements))
    summary = result["outcomes_summary"]
    assert summary["total"] == 4
    assert summary["compliant"] == 2
    assert summary["non_compliant"] == 1
    assert summary["missing_data"] == 1
    assert summary["manual_review"] == 0
    assert (
        summary["compliant"]
        + summary["non_compliant"]
        + summary["missing_data"]
        + summary["manual_review"]
        == summary["total"]
    )


def test_total_equals_elements_times_rules(tmp_path, autonomy):
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "r1",
                "parameter": "P1",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param",
                "description": "P1",
                "autofill": {"strategy": "none"},
            },
            {
                "id": "r2",
                "parameter": "P2",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param",
                "description": "P2",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    elements = [_el(f"r{i}", {"P1": "x", "P2": "y"}) for i in range(5)]
    result = agent.run(_state(elements))
    # 5 elements × 2 rules = 10 checks total, all compliant
    assert result["outcomes_summary"]["total"] == 10
    assert result["outcomes_summary"]["compliant"] == 10


# ── v1.5-R6 (2.1a): skipped_out_of_scope counter ─────────────────────────────


def test_skipped_out_of_scope_counts_per_rule_category_filter(tmp_path, autonomy):
    """A rule's per-category filter (rule.category set, element in a different
    category) must not silently vanish from the Coverage picture — it's
    counted separately from `total`, not folded into 'compliant by default'."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "doors.only",
                "parameter": "Width",
                "requirement": "present_and_nonempty",
                "category": "Doors",
                "severity_tag": "missing_required_param",
                "description": "Doors only",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    # target_category is "Rooms" (see _rules helper) so both elements are
    # in-scope for the RULESET, but the RULE itself only applies to Doors.
    elements = [_el("r1", {"Width": "1"}), _el("r2", {"Width": "1"})]
    for el in elements:
        el["category"] = "Rooms"
    result = agent.run(_state(elements))
    summary = result["outcomes_summary"]
    assert summary["total"] == 0
    assert summary["skipped_out_of_scope"] == 2


def test_bound_rule_finding_reads_display_and_type_under_bound_name(
    tmp_path, autonomy
):
    """L2 (audit): the display/provenance reads must resolve the BOUND name.

    `params`/`params_display`/`type.*`/`host.*` are all keyed by the name the
    query actually FETCHED (the bound one). Three sites still read the canonical
    intent label, so for a bound rule the Results table lost its display value,
    the element name lost its "family - type", and the inherit provenance went
    blank. The finding's `parameter` FIELD stays canonical on purpose — that is
    the declared report label, not a dict key.
    """
    rules_path = _rules(tmp_path, [
        {"id": "r.bound", "parameter": "Phong_ban",       # canonical alias (VN)
         "bound_parameter": "Department",                  # real Revit param
         "requirement": "matches_regex", "pattern": r"^OK$",
         "severity_tag": "rule_violation", "description": "dept",
         "autofill": {"strategy": "inherit_from_host"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    el = {
        "id": "r1", "category": "Rooms", "name": "Room 1",
        "params": {
            "Department": "",                 # empty → inherit path
            "type.Department": "TYPEVAL",     # type mirror, keyed by BOUND name
            "host.Department": "FROM-HOST",   # host hop, keyed by BOUND name
            "_family_name": "Fam", "_type_name": "T1",
        },
        "params_display": {"Department": "Phòng Kỹ thuật"},
    }
    result = agent.run(_state([el]))
    finding = (result["findings"] + result["manual_review_items"]
               + result["missing_data_items"])[0]
    # report label stays canonical (deliberate)
    assert finding["parameter"] == "Phong_ban"
    # ...but every params-dict read resolved the bound name
    assert finding["current_value"] == "Phòng Kỹ thuật"      # params_display
    assert finding["element_name"] == "Fam - T1"             # type.<bound> hit
    assert finding["inherited_from"] == "FROM-HOST"          # host.<bound> hit


def test_undetermined_scope_routes_to_manual_review_with_trace(tmp_path, autonomy):
    """M1 / reg#9: a scope filter that can't be EVALUATED (bad regex, or a
    missing gating value) must NOT silently skip — it is a counted pair whose
    outcome is manual_review + a CheckRecord, so an all-undetermined run can
    never converge "clean". A legitimate non-match still skips (below)."""
    rules_path = _rules(tmp_path, [
        {"id": "bad.scope.regex", "parameter": "Width",
         "requirement": "present_and_nonempty",
         "scope_filter": {"param": "Function", "pattern": "["},   # won't compile
         "severity_tag": "missing_required_param", "description": "bad scope",
         "autofill": {"strategy": "none"}},
        {"id": "missing.gate", "parameter": "Width",
         "requirement": "present_and_nonempty",
         "scope_filter": {"param": "Function", "pattern": "External"},
         "severity_tag": "missing_required_param", "description": "missing gate",
         "autofill": {"strategy": "none"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    # Element has NO Function value → rule 1 (bad regex) = unknown,
    # rule 2 (missing gating param) = unknown. Both → manual_review + trace.
    result = agent.run(_state([_el("r1", {"Width": "1"})]))
    s = result["outcomes_summary"]
    assert s["total"] == 2
    assert s["manual_review"] == 2
    assert s["skipped_out_of_scope"] == 0
    assert s["compliant"] == 0
    # Visible in the verification report, not a silent skip.
    assert len(result["check_trace"]) == 2
    assert all(t["status"] == "manual_review" for t in result["check_trace"])


def test_blank_scope_value_is_undetermined_not_out_of_scope(tmp_path, autonomy):
    """P1-03: a blank gating value must not silently drop the element.

    In Revit an empty string is how an unset parameter reads — `_is_missing`
    in this same module already treats blank as missing. Letting it fall
    through to the regex made it a quiet `no_match`, so the element left no
    finding, no trace record and no counted pair: it simply disappeared from
    the audit while the run still converged "clean".
    """
    rules_path = _rules(tmp_path, [
        {"id": "ext.only", "parameter": "Width",
         "requirement": "present_and_nonempty",
         "scope_filter": {"param": "Function", "pattern": "^External$"},
         "severity_tag": "missing_required_param", "description": "external only",
         "autofill": {"strategy": "none"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([
        _el("blank", {"Width": "1", "Function": ""}),
        _el("space", {"Width": "1", "Function": "   "}),
    ]))
    s = result["outcomes_summary"]
    assert s["total"] == 2
    assert s["manual_review"] == 2
    assert s["skipped_out_of_scope"] == 0
    assert len(result["check_trace"]) == 2


def test_legitimate_scope_non_match_still_skips_silently(tmp_path, autonomy):
    """The other half of M1's tri-state: a gating value that is PRESENT and
    does not match is a legitimate 'rule does not apply' — still a quiet skip,
    NOT manual_review (else every out-of-scope element floods the queue)."""
    rules_path = _rules(tmp_path, [
        {"id": "ext.only", "parameter": "Width",
         "requirement": "present_and_nonempty",
         "scope_filter": {"param": "Function", "pattern": "^External$"},
         "severity_tag": "missing_required_param", "description": "external only",
         "autofill": {"strategy": "none"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Width": "1", "Function": "Internal"})]))
    s = result["outcomes_summary"]
    assert s["total"] == 0
    assert s["manual_review"] == 0
    assert s["skipped_out_of_scope"] == 1


def test_skipped_out_of_scope_counts_universal_scope_filter_misses(tmp_path, autonomy):
    """v1.4-K10's universal scope_filter (e.g. 'only external doors') is the
    SECOND source of out-of-scope skips — must also be captured."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "ext.only",
                "parameter": "Width",
                "requirement": "present_and_nonempty",
                "scope_filter": {"param": "Function", "pattern": "External"},
                "severity_tag": "missing_required_param",
                "description": "External only",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    elements = [
        _el("r1", {"Width": "1", "Function": "External"}),  # in scope
        _el("r2", {"Width": "1", "Function": "Internal"}),  # skipped
    ]
    result = agent.run(_state(elements))
    summary = result["outcomes_summary"]
    assert summary["total"] == 1
    assert summary["skipped_out_of_scope"] == 1


# ── missing_data bucket ──────────────────────────────────────────────────────


def test_missing_data_routes_to_missing_data_items_not_findings(tmp_path, autonomy):
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.dept.required",
                "parameter": "Department",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param",
                "description": "Department required",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    elements = [_el("r1", {}), _el("r2", {"Department": ""}), _el("r3", {"Department": "Sales"})]
    result = agent.run(_state(elements))

    assert result["findings"] == []  # no non_compliant items
    assert len(result["missing_data_items"]) == 2  # None and "" both missing
    assert result["outcomes_summary"]["compliant"] == 1
    assert result["outcomes_summary"]["missing_data"] == 2
    assert result["outcomes_summary"]["non_compliant"] == 0

    statuses = {f["status"] for f in result["missing_data_items"]}
    assert statuses == {"missing_data"}


def test_missing_data_wins_over_requires_human(tmp_path, autonomy):
    """If a rule has requires_human=True but value is missing, missing_data
    bucket wins — we can't manually review a value that doesn't exist yet."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.min",
                "parameter": "Area",
                "requirement": "numeric_min",
                "threshold": 10.0,
                "severity_tag": "geometric_violation",
                "description": "Area >= 10 m2",
                "autofill": {"strategy": "none"},
                "requires_human": True,
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Area": None})]))

    assert result["outcomes_summary"]["missing_data"] == 1
    assert result["outcomes_summary"]["manual_review"] == 0
    assert len(result["missing_data_items"]) == 1
    assert result["manual_review_items"] == []


# ── manual_review bucket ─────────────────────────────────────────────────────


def test_requires_human_routes_failures_to_manual_review(tmp_path, autonomy):
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.borderline",
                "parameter": "Area",
                "requirement": "numeric_min",
                "threshold": 10.0,
                "severity_tag": "geometric_violation",
                "description": "Area >= 10 m2 (borderline needs human)",
                "autofill": {"strategy": "none"},
                "requires_human": True,
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    elements = [_el("r1", {"Area": 9.5}), _el("r2", {"Area": 12.0})]
    result = agent.run(_state(elements))

    assert result["findings"] == []  # no non_compliant — requires_human captured the fail
    assert len(result["manual_review_items"]) == 1
    assert result["manual_review_items"][0]["element_id"] == "r1"
    assert result["manual_review_items"][0]["status"] == "manual_review"
    assert result["outcomes_summary"]["manual_review"] == 1
    assert result["outcomes_summary"]["compliant"] == 1


def test_requires_human_pass_still_compliant(tmp_path, autonomy):
    """requires_human flag only affects failures. Passes are still compliant."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.min",
                "parameter": "Area",
                "requirement": "numeric_min",
                "threshold": 10.0,
                "severity_tag": "geometric_violation",
                "description": "Area >= 10 m2",
                "autofill": {"strategy": "none"},
                "requires_human": True,
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Area": 20.0})]))

    assert result["outcomes_summary"]["compliant"] == 1
    assert result["outcomes_summary"]["manual_review"] == 0
    assert result["manual_review_items"] == []


def test_requires_human_defaults_to_false(tmp_path, autonomy):
    """Rules without an explicit requires_human flag behave as before BB."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.min",
                "parameter": "Area",
                "requirement": "numeric_min",
                "threshold": 10.0,
                "severity_tag": "geometric_violation",
                "description": "Area >= 10 m2",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Area": 5.0})]))

    # Failure with requires_human absent → non_compliant (default)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["status"] == "non_compliant"
    assert result["manual_review_items"] == []


# ── finding shape ────────────────────────────────────────────────────────────


def test_findings_carry_status_field(tmp_path, autonomy):
    """Every Finding emitted by QC now carries an explicit status field."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.min",
                "parameter": "Area",
                "requirement": "numeric_min",
                "threshold": 10.0,
                "severity_tag": "geometric_violation",
                "description": "Area >= 10",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Area": 5.0}), _el("r2", {})]))

    for f in result["findings"]:
        assert f["status"] == "non_compliant"
    for f in result["missing_data_items"]:
        assert f["status"] == "missing_data"


def test_state_includes_all_new_keys(tmp_path, autonomy):
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "r",
                "parameter": "P",
                "requirement": "present_and_nonempty",
                "severity_tag": "missing_required_param",
                "description": "P",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"P": "x"})]))
    # New keys all present even when empty
    assert "outcomes_summary" in result
    assert "manual_review_items" in result
    assert "missing_data_items" in result
    assert "findings" in result


# ── interaction with conditional rules ───────────────────────────────────────


# ── QW-1: element_name surfacing ────────────────────────────────────────────


def test_finding_carries_element_name_when_present(tmp_path, autonomy):
    """QW-1: human-readable name surfaces in Findings so downstream
    reports + UI don't have to render the base64 URN."""
    rules_path = _rules(
        tmp_path,
        [{
            "id": "room.area.min",
            "parameter": "Area",
            "requirement": "numeric_min",
            "threshold": 10.0,
            "severity_tag": "geometric_violation",
            "description": "Area >= 10",
            "autofill": {"strategy": "none"},
        }],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    el = {
        "id": "urn:adsk:elem-aaaa-bbbb",
        "name": "Closet 11A",
        "category": "Rooms",
        "params": {"Area": 5.0},
    }
    result = agent.run(_state([el]))
    assert len(result["findings"]) == 1
    assert result["findings"][0]["element_name"] == "Closet 11A"


def test_finding_omits_element_name_when_absent(tmp_path, autonomy):
    """No name on the element -> element_name key absent (NotRequired)."""
    rules_path = _rules(
        tmp_path,
        [{
            "id": "room.area.min",
            "parameter": "Area",
            "requirement": "numeric_min",
            "threshold": 10.0,
            "severity_tag": "geometric_violation",
            "description": "Area >= 10",
            "autofill": {"strategy": "none"},
        }],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    el = {
        "id": "urn:adsk:elem-x",
        "category": "Rooms",
        "params": {"Area": 5.0},
    }
    result = agent.run(_state([el]))
    assert "element_name" not in result["findings"][0]


def test_conditional_rule_inapplicable_is_compliant(tmp_path, autonomy):
    """numeric_min_conditional with when_pattern mismatch → rule not applicable
    → outcome is compliant (not missing_data) even if the target value is None."""
    rules_path = _rules(
        tmp_path,
        [
            {
                "id": "room.area.residential_min",
                "parameter": "Area",
                "requirement": "numeric_min_conditional",
                "threshold": 10.0,
                "when_param": "Occupancy",
                "when_pattern": "^Residential",
                "severity_tag": "geometric_violation",
                "description": "Residential rooms need Area >= 10",
                "autofill": {"strategy": "none"},
            },
        ],
    )
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    # Commercial occupancy + no Area — rule should not apply, so compliant
    elements = [_el("r1", {"Occupancy": "Commercial"})]
    result = agent.run(_state(elements))

    assert result["outcomes_summary"]["compliant"] == 1
    assert result["outcomes_summary"]["missing_data"] == 0
    assert result["findings"] == []
    assert result["missing_data_items"] == []


# ── M-a / M-b: mis-authored rules route to manual_review, never crash ─────────

def test_bad_regex_rule_routes_to_manual_review_not_crash(tmp_path, autonomy):
    # M-a: an invalid user regex used to raise re.error and kill the whole run.
    # Now the element routes to manual_review (and other rules still evaluate).
    rules_path = _rules(tmp_path, [
        {"id": "room.name.pattern", "parameter": "Name",
         "requirement": "matches_regex", "pattern": "[",   # invalid regex
         "severity_tag": "invalid_value_range", "description": "bad pattern",
         "autofill": {"strategy": "none"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Name": "Office"})]))
    s = result["outcomes_summary"]
    assert s["total"] == 1
    assert s["manual_review"] == 1
    assert s["non_compliant"] == 0 and s["compliant"] == 0


def test_unit_mismatch_routes_to_manual_review(tmp_path, autonomy):
    # M-b: a param with a KNOWN storage unit (Width→ft) that can't convert to the
    # rule's declared unit (m²) routes to manual_review — not a silent wrong-unit
    # pass/fail behind a log line.
    rules_path = _rules(tmp_path, [
        {"id": "room.width.bad_unit", "parameter": "Width",
         "requirement": "numeric_compare", "operator": ">=", "threshold": 1.0,
         "unit": "m²",                                    # no ft→m² factor
         "severity_tag": "invalid_value_range", "description": "width mismatch",
         "autofill": {"strategy": "none"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Width": 3.0})]))
    s = result["outcomes_summary"]
    assert s["total"] == 1 and s["manual_review"] == 1
    assert s["compliant"] == 0 and s["non_compliant"] == 0
    # ...and it must be VISIBLE in the verification report. That report renders
    # `check_trace` and never re-derives, so an outcome counted in the summary
    # but missing from the trace is a per-element evidence hole: the run says
    # "1 manual_review", the report can't say which element or why.
    trace = result["check_trace"]
    assert len(trace) == 1
    assert trace[0]["status"] == "manual_review"
    assert trace[0]["passed"] is False


def test_every_counted_pair_leaves_a_trace_record(tmp_path, autonomy):
    """Invariant: ``outcomes_summary["total"] == len(check_trace)``.

    Each `total`-counted (element, rule) pair must produce exactly one
    CheckRecord — including the exceptional branches. Asserting bucket counts
    alone let the unit-mismatch branch drop its record unnoticed; this pins
    the whole class at once. Mixes a normal pass, a mis-authored rule (M-a)
    and an unconvertible unit (M-b) in one run.
    """
    rules_path = _rules(tmp_path, [
        {"id": "ok.name", "parameter": "Name",
         "requirement": "present_and_nonempty",
         "severity_tag": "missing_required_param", "description": "name present",
         "autofill": {"strategy": "none"}},
        {"id": "bad.regex", "parameter": "Name",
         "requirement": "matches_regex", "pattern": "[",
         "severity_tag": "invalid_value_range", "description": "bad pattern",
         "autofill": {"strategy": "none"}},
        {"id": "bad.unit", "parameter": "Width",
         "requirement": "numeric_compare", "operator": ">=", "threshold": 1.0,
         "unit": "m²",
         "severity_tag": "invalid_value_range", "description": "width mismatch",
         "autofill": {"strategy": "none"}},
    ])
    agent = QCAgent(rules_path=rules_path, autonomy=autonomy)
    result = agent.run(_state([_el("r1", {"Name": "Office", "Width": 3.0})]))
    s = result["outcomes_summary"]
    assert s["total"] == 3
    assert s["manual_review"] == 2          # M-a + M-b
    assert len(result["check_trace"]) == s["total"]
