"""L2-15, L2-16, L2-18, L2-19 — the last four from the Phase 2 review.

Graded Low because none of them writes a wrong value on its own. They share a
shape with the Highs that came before: each is a place where the system knows
something and does not say it, or accepts something it never agreed to accept.

(L2-17 — the structured→legacy fallback billing twice and blaming the SDK — was
already closed in #27 alongside L2-02; it is not repeated here.)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import yaml

from bim_orchestrator.agents.design import (
    MAX_PROPOSED_VALUE_CHARS,
    DesignAgent,
    _proposal_shape_ok,
)
from bim_orchestrator.agents.qc import Rule, RuleSet
from bim_orchestrator.llm.usage import MeteredLLMClient, UsageRecorder
from bim_orchestrator.policies.autonomy import AutonomyPolicy


# ---------------------------------------------------------------------------
# L2-15 — the closed loop is only as strong as what the requirement constrains
# ---------------------------------------------------------------------------


def _agent(tmp_path, rule) -> DesignAgent:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "version": 1,
            "actions": {"parameters": {"set_value": "approve"}},
            "severity_levels": {"severity_high": "approve",
                                "severity_medium": "approve",
                                "severity_low": "approve"},
        }),
        encoding="utf-8",
    )
    return DesignAgent(
        mcp=None, autonomy=AutonomyPolicy.load(cfg), project_id="t",
        rules=RuleSet(scenario="t", target_category="Doors", rules=[rule]),
    )


def _present_rule() -> Rule:
    """The most natural rule a BIM manager writes — and the weakest validator.

    `present_and_nonempty` constrains almost nothing, so before this fix a
    dict, a list, an int and a 1 MB string were all acceptable "fixes".
    """
    return Rule.model_validate({
        "id": "doors.mark.present",
        "category": "Doors",
        "parameter": "Mark",
        "requirement": "present_and_nonempty",
        "severity_tag": "missing_required_param",
        "description": "Mark must be present",
        "fixability": "auto",
        "autofill": {"strategy": "none"},
        "remediation": {"action": "set_parameter",
                        "new_value_strategy": "llm_propose"},
    })


class TestProposalShape:
    @pytest.mark.parametrize(
        "value",
        [
            {"value": "DR-001"},          # the model returned its own envelope
            ["DR-001"],
            42,
            None,
            True,
            b"DR-001",
        ],
    )
    def test_non_strings_are_refused(self, value):
        assert _proposal_shape_ok(value, _present_rule()) is False

    def test_an_unbounded_payload_is_refused(self):
        assert _proposal_shape_ok("x" * (MAX_PROPOSED_VALUE_CHARS + 1), _present_rule()) is False

    def test_a_value_at_the_cap_is_fine(self):
        assert _proposal_shape_ok("x" * MAX_PROPOSED_VALUE_CHARS, _present_rule()) is True

    @pytest.mark.parametrize("value", ["DR\n001", "DR\x00001", "DR\r001"])
    def test_control_characters_are_refused(self, value):
        """A newline or NUL in a Revit parameter is a corrupt value, not a long one."""
        assert _proposal_shape_ok(value, _present_rule()) is False

    def test_whitespace_only_is_refused(self):
        assert _proposal_shape_ok("   ", _present_rule()) is False

    def test_an_ordinary_value_passes(self):
        assert _proposal_shape_ok("DR-001", _present_rule()) is True

    def test_the_validator_itself_refuses_them(self, tmp_path):
        """The guard has to live INSIDE the validator: that object is what the
        plugin's own repair loop calls, and a guard the plugin can skip is not
        a guard."""
        rule = _present_rule()
        validate = _agent(tmp_path, rule)._make_validator(rule)
        assert validate("DR-001") is True
        assert validate({"value": "DR-001"}) is False        # type: ignore[arg-type]
        assert validate("x" * 10_000) is False

    def test_a_hostile_shape_cannot_reach_a_write(self, tmp_path):
        """End to end: a plugin that returns a dict gets None, i.e. Path A."""
        from tests._llm_stubs import StubRemediationAgent

        rule = _present_rule()
        agent = _agent(tmp_path, rule)
        agent._llm_agent = StubRemediationAgent({"value": "DR-001"})  # type: ignore[arg-type]
        finding = {"rule_id": rule.id, "element_id": "1", "parameter": "Mark",
                   "severity_tag": "missing_required_param",
                   "severity": "severity_medium", "message": "m",
                   "status": "non_compliant"}
        element = {"id": "1", "category": "Doors", "params": {"Mark": ""}}
        assert asyncio.run(agent._llm_propose(finding, element, rule)) is None


# ---------------------------------------------------------------------------
# L2-16 — a run where every call failed printed like a clean one
# ---------------------------------------------------------------------------


class _Client:
    model = "m"

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    async def complete(self, *, system, prompt, max_tokens=512):
        if self.fail:
            raise RuntimeError("upstream is down")
        return "ok"

    async def complete_json(self, *, system, prompt, schema=None, max_tokens=512):
        if self.fail:
            raise RuntimeError("upstream is down")
        return {}


def _run_calls(*, fail: bool, n: int = 3) -> UsageRecorder:
    rec = UsageRecorder()
    client = MeteredLLMClient(inner=_Client(fail=fail), recorder=rec, agent="remediation")

    async def _go():
        for _ in range(n):
            try:
                await client.complete(system="s", prompt="p")
            except RuntimeError:
                pass

    asyncio.run(_go())
    return rec


class TestFailedCallsAreVisible:
    def test_failures_are_counted(self):
        assert _run_calls(fail=True).failed_calls == 3

    def test_successes_are_not(self):
        assert _run_calls(fail=False).failed_calls == 0

    def test_a_failing_run_no_longer_reads_like_a_clean_one(self):
        """The whole complaint in one assertion: 3 calls, 0 answers, and the
        line used to be identical to 3 calls, 3 answers."""
        bad = _run_calls(fail=True).format_line()
        good = _run_calls(fail=False).format_line()
        assert bad != good
        assert "FAILED" in bad
        assert "FAILED" not in good

    def test_the_artifact_carries_it(self):
        s = _run_calls(fail=True).summary()
        assert s["failed_calls"] == 3
        assert s["failed_by_agent"] == {"remediation": 3}

    def test_total_calls_still_counts_attempts(self):
        """A failed call was still made and still billed — it must not vanish
        from the total, only stop masquerading as an answer."""
        assert _run_calls(fail=True).total_calls == 3


# ---------------------------------------------------------------------------
# L2-18 — the flags are inert on --check / --apply, and now say so
# ---------------------------------------------------------------------------


class _R:
    def __init__(self, rid, strategy=None):
        self.id = rid
        self.remediation = type("X", (), {"new_value_strategy": strategy})()


class _RS:
    def __init__(self, *rules):
        self.rules = list(rules)


class TestCheckAndApplyDisclose:
    def test_check_stamps_the_status(self, monkeypatch):
        """`--check` proposes nothing by design; an operator who set the flags
        still deserves to learn they did nothing."""
        from bim_orchestrator.orchestrator import _stamp_llm_status

        monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
        state: dict = {}
        _stamp_llm_status(state, rules=_RS(_R("r1", "llm_propose")))
        assert state["llm_status"]["requested"]["remediation"] is True
        assert state["llm_status"]["llm_rules_degraded_to_path_a"] is True

    def test_check_calls_it(self):
        """Pins the WIRING — the renderer tests in test_llm_status_disclosure
        would pass even if nobody called it from these two modes."""
        import inspect

        from bim_orchestrator import orchestrator

        for fn in (orchestrator.check, orchestrator.apply):
            src = inspect.getsource(fn)
            assert "_stamp_llm_status" in src, f"{fn.__name__} does not disclose"
            assert "_print_llm_status" in src, f"{fn.__name__} does not print"


# ---------------------------------------------------------------------------
# L2-19 — the PDF import surface runs the same authoring gate as the others
# ---------------------------------------------------------------------------


class TestExtractionSemanticGate:
    @staticmethod
    def _rule(**over: Any) -> dict[str, Any]:
        base = {
            "id": "doors.mark.present",
            "category": "Doors",
            "parameter": "Mark",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "description": "Mark must be present",
            "fixability": "auto",
            "autofill": {"strategy": "none"},
            "remediation": {"action": "create_acc_issue"},
        }
        base.update(over)
        return base

    def test_a_valid_rule_survives(self):
        from bim_orchestrator.service.routes_extraction import _split_semantically_valid

        kept, warnings = _split_semantically_valid([self._rule()], "s1")
        assert len(kept) == 1
        assert warnings == []

    @pytest.mark.parametrize(
        ("label", "over"),
        [
            # Exactly the shapes an extraction model produces: it names a
            # pattern requirement and then doesn't write the pattern, or writes
            # one that will not compile.
            ("no pattern", {"requirement": "matches_regex"}),
            ("uncompilable", {"requirement": "matches_regex", "pattern": "["}),
            ("empty parameter", {"parameter": ""}),
        ],
    )
    def test_a_semantically_invalid_rule_is_dropped_and_named(self, label, over):
        """pydantic accepts all three; the authoring validator refuses them.
        This route used to return them as executable, and the PUT behind it
        then refused the very rules it had just handed the author."""
        from bim_orchestrator.service.routes_extraction import _split_semantically_valid

        kept, warnings = _split_semantically_valid([self._rule(**over)], "s1")
        assert kept == [], label
        assert warnings and "doors.mark.present" in warnings[0]

    def test_one_bad_rule_does_not_discard_the_document(self):
        from bim_orchestrator.service.routes_extraction import _split_semantically_valid

        bad = self._rule(id="broken", requirement="matches_regex", pattern="[")
        kept, warnings = _split_semantically_valid([self._rule(), bad], "s1")
        assert [r["id"] for r in kept] == ["doors.mark.present"]
        assert len(warnings) == 1

    def test_the_route_applies_it(self):
        import inspect

        from bim_orchestrator.service import routes_extraction

        src = inspect.getsource(routes_extraction)
        assert "_split_semantically_valid(" in src.split("def _split_semantically_valid")[-1]
