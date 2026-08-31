"""P1-GEO-01/02 + P1-RENDER-01/02 — the geometry vertical slice.

The independent review (2026-07-26) found four findings that were really one
defect wearing four hats: `[]` from the geometry agent meant BOTH "checked,
found nothing" and "the check never ran", and that ambiguity travelled the
whole way out —

    rule intent → link resolution → MCP envelope → coverage state
        → run status / exit code → report headline + detail

Patching any single layer would have left the ambiguity leaking into the next
one, so these tests walk the chain end to end. They are deliberately in one
file: the invariant is that the layers AGREE, which no per-layer test can pin.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.audit_report import render_audit_report
from bim_orchestrator.orchestrator import _exit_code_for
from bim_orchestrator.state import geometry_verdict, recorded_status

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_EMPTY_SUMMARY = {
    "total": 0, "compliant": 0, "non_compliant": 0,
    "manual_review": 0, "missing_data": 0,
}


def _cov(verdict, *, executed=(), failed=(), truncated=(), requested=None):
    out = {
        "rules_requested": requested if requested is not None
        else len(executed) + len(failed),
        "rules_executed": list(executed),
        "rules_failed": list(failed),
        "verdict": verdict,
    }
    if truncated:
        out["truncated"] = list(truncated)
    return out


def _state(**kw):
    base = {
        "check_trace": [], "findings": [], "proposed_fixes": [],
        "outcomes_summary": dict(_EMPTY_SUMMARY),
        "status": "converged",
    }
    base.update(kw)
    return base


def _geo_finding(eid="d1"):
    return {
        "rule_id": "ducts.floor_clearance", "element_id": eid,
        "parameter": "-", "severity_tag": "geometry_violation",
        "severity": "severity_high", "message": "duct too close to slab",
        "status": "non_compliant",
    }


# ---------------------------------------------------------------------------
# layer 1 — the verdict itself
# ---------------------------------------------------------------------------


class TestGeometryVerdict:
    def test_no_geometry_rules_is_not_applicable(self):
        # Must stay None, not "ok": a parameter-only run has no geometry
        # opinion, and inventing one would put a geometry line on every report.
        assert geometry_verdict(_state()) is None
        assert geometry_verdict(_state(geometry_coverage={})) is None

    @pytest.mark.parametrize("verdict", ["ok", "partial", "no_audit"])
    def test_verdict_is_read_from_coverage(self, verdict):
        assert geometry_verdict(_state(geometry_coverage=_cov(verdict))) == verdict

    def test_malformed_coverage_does_not_raise(self):
        # Same posture as A-02: a shape defect must not take the run down.
        assert geometry_verdict(_state(geometry_coverage="nonsense")) is None
        assert geometry_verdict(_state(geometry_coverage={"verdict": "weird"})) is None


# ---------------------------------------------------------------------------
# layer 2 — recorded status (metadata.json / report must not disagree)
# ---------------------------------------------------------------------------


class TestRecordedStatus:
    def test_geometry_no_audit_downgrades_the_recorded_status(self):
        st = _state(geometry_coverage=_cov("no_audit", failed=[{"rule_id": "r1"}]))
        assert recorded_status("completed", st) == "no_audit"

    def test_partial_geometry_keeps_the_status(self):
        # Partial is disclosed, not downgraded — some rules DID produce real
        # answers, so the run is a real (narrower) audit.
        st = _state(geometry_coverage=_cov("partial", executed=["r1"],
                                           failed=[{"rule_id": "r2"}]))
        assert recorded_status("completed", st) == "completed"

    def test_ok_geometry_keeps_the_status(self):
        st = _state(geometry_coverage=_cov("ok", executed=["r1"]))
        assert recorded_status("completed", st) == "completed"


# ---------------------------------------------------------------------------
# layer 3 — exit code (what a scheduled job actually reads)
# ---------------------------------------------------------------------------


class TestExitCode:
    def test_geometry_no_audit_exits_nonzero(self):
        st = _state(geometry_coverage=_cov(
            "no_audit", failed=[{"rule_id": "ducts.floor_clearance",
                                 "reason": "mcp_error"}]))
        assert _exit_code_for(st) == 1

    def test_geometry_no_audit_fails_without_the_strictness_flag(self):
        # Deliberately NOT behind --fail-on-partial-coverage. That flag is for
        # a narrowed scope; this is a check that did not happen at all.
        st = _state(geometry_coverage=_cov(
            "no_audit", failed=[{"rule_id": "r1", "reason": "mcp_error"}]))
        assert _exit_code_for(st, fail_on_partial_coverage=False) == 1

    def test_partial_geometry_still_exits_zero(self):
        st = _state(geometry_coverage=_cov("partial", executed=["r1"],
                                           failed=[{"rule_id": "r2"}]))
        assert _exit_code_for(st) == 0

    def test_healthy_geometry_exits_zero(self):
        st = _state(geometry_coverage=_cov("ok", executed=["r1"]))
        assert _exit_code_for(st) == 0


# ---------------------------------------------------------------------------
# layer 4 — the rendered document a human signs
# ---------------------------------------------------------------------------


class TestReportHeadline:
    def test_geometry_only_violations_are_in_the_headline(self):
        """P1-RENDER-01. The headline summarised `check_trace` only — i.e.
        parameter rules — while geometry findings ride a separate bucket. So a
        geometry-only run with real clashes opened with 'nothing checked' and
        contradicted itself two sections later, on the one surface a PM reads
        before deciding."""
        md = render_audit_report(_state(
            geometry_findings=[_geo_finding("d1"), _geo_finding("d2")],
            geometry_coverage=_cov("ok", executed=["ducts.floor_clearance"]),
        ))
        headline = next(ln for ln in md.splitlines() if "Headline:" in ln)
        assert "2 geometry violation" in headline
        assert "nothing checked" not in headline

    def test_geometry_execution_failure_owns_the_headline(self):
        md = render_audit_report(_state(
            geometry_coverage=_cov("no_audit", failed=[
                {"rule_id": "ducts.floor_clearance", "reason": "mcp_error"}]),
        ))
        headline = next(ln for ln in md.splitlines() if "Headline:" in ln)
        assert "GEOMETRY AUDIT DID NOT RUN" in headline
        assert "NOT evidence" in md

    def test_a_clean_parameter_run_cannot_say_PASS_while_geometry_failed(self):
        # The mixed case the review called out: passing parameter checks must
        # not launder a geometry half that never executed.
        md = render_audit_report(_state(
            outcomes_summary={**_EMPTY_SUMMARY, "total": 5, "compliant": 5},
            check_trace=[{"rule_id": "r", "element_id": str(i), "parameter": "Mark",
                          "requirement": "present_and_nonempty", "raw_value": "A",
                          "value": "A", "passed": True, "status": "compliant"}
                         for i in range(5)],
            geometry_coverage=_cov("no_audit", failed=[
                {"rule_id": "g1", "reason": "mcp_error"}]),
        ))
        headline = next(ln for ln in md.splitlines() if "Headline:" in ln)
        assert "PASS" not in headline
        assert "GEOMETRY AUDIT DID NOT RUN" in headline

    def test_mixed_run_headline_mentions_both_halves(self):
        md = render_audit_report(_state(
            outcomes_summary={**_EMPTY_SUMMARY, "total": 3, "compliant": 2,
                              "non_compliant": 1},
            check_trace=[{"rule_id": "r", "element_id": "1", "parameter": "Mark",
                          "requirement": "present_and_nonempty", "raw_value": None,
                          "value": None, "passed": False, "status": "non_compliant"}],
            geometry_findings=[_geo_finding()],
            geometry_coverage=_cov("ok", executed=["g1"]),
        ))
        headline = next(ln for ln in md.splitlines() if "Headline:" in ln)
        assert "of 3 checks need attention" in headline
        assert "1** geometry violation" in headline

    def test_parameter_only_run_is_completely_unchanged(self):
        # Guard: no geometry coverage → no geometry wording anywhere.
        md = render_audit_report(_state(
            outcomes_summary={**_EMPTY_SUMMARY, "total": 1, "compliant": 1},
            check_trace=[{"rule_id": "r", "element_id": "1", "parameter": "Mark",
                          "requirement": "present_and_nonempty", "raw_value": "A",
                          "value": "A", "passed": True, "status": "compliant"}],
        ))
        assert "**Headline: PASS**" in md
        assert "Geometry coverage" not in md


class TestReportGeometryCoverage:
    def test_failed_rules_are_named_with_absence_spelled_out(self):
        md = render_audit_report(_state(
            geometry_coverage=_cov("partial", executed=["g_ok"], failed=[
                {"rule_id": "g_bad", "reason": "mcp_error", "detail": "boom"}]),
        ))
        assert "Geometry coverage" in md
        assert "g_bad" in md
        # The sentence that does the work: absence here is not evidence.
        assert "*not checked*, not *no clashes*" in md

    def test_unresolved_link_says_which_model_was_wanted(self):
        """P1-GEO-02. The rule id alone does not tell an author that the
        `linked_arch` model is missing — and the old behaviour did not even
        tell them a substitution had happened."""
        md = render_audit_report(_state(
            geometry_coverage=_cov("no_audit", failed=[{
                "rule_id": "ducts.floor_clearance",
                "reason": "link_not_found",
                "reference_source": "linked_arch",
                "available": ["Snowdon Towers Sample HVAC"],
            }]),
        ))
        assert "linked_arch" in md
        assert "NOT re-pointed" in md
        assert "Snowdon Towers Sample HVAC" in md

    def test_truncated_result_is_reported_as_a_floor_not_a_total(self):
        """P1-RENDER-02. A capped clash list was rendered as if complete, so
        '500' read as exact when the truth was 'at least 500' — a number a
        coordinator sizes remediation from."""
        md = render_audit_report(_state(
            geometry_coverage=_cov("ok", executed=["g1"], truncated=[
                {"rule_id": "g1", "cap": 500, "returned": 500}]),
        ))
        assert "AT LEAST 500" in md
        assert "capped at 500" in md
        assert "a floor, not a total" in md

    def test_unsupported_check_types_are_disclosed_not_hidden(self):
        cov = _cov("ok", executed=["g1"])
        cov["rules_unsupported"] = ["g_future"]
        md = render_audit_report(_state(geometry_coverage=cov))
        assert "g_future" in md
        assert "unsupported" in md

    def test_healthy_geometry_run_says_so_without_alarm(self):
        md = render_audit_report(_state(
            geometry_findings=[_geo_finding()],
            geometry_coverage=_cov("ok", executed=["g1", "g2"]),
        ))
        assert "executed: **2**" in md
        assert "did NOT run: **0**" in md
        assert "NO result" not in md
