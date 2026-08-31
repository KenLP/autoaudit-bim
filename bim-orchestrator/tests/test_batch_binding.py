"""L2-09 — a batch answer must say which question it answers.

``_prewarm_llm_batches`` asks one model call for N elements of the same rule and
re-validates each returned value. That re-validation is real, and it is not
enough: a rule reaches this path only when its validator is element-INDEPENDENT
(that is exactly the ``_is_batchable`` test), so if the answers arrive out of
step with the ids, every value passes and every element is written with another
element's correction. No attacker is required — rows drifting by one is ordinary
model behaviour on a long list, and the human backstop is weak because the ACC
body trims.

What the echo does and does not buy:

* it CATCHES misalignment — the model reports the current value it actually
  corrected, so a value that travelled with the wrong id no longer matches;
* it does NOT catch a model that fabricates the echo to match. Nothing in a
  single call could. That case is bounded by the same two things as before: the
  deterministic validator, and a human approving the write.

The failure mode of the check is cost, never a wrong write: an unbound answer is
dropped and the per-element path re-asks — one question, one answer.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from bim_orchestrator.agents.design import (
    _echo_matches,
    _fit_acc_description,
    _unpack_batch_entry,
)
from bim_orchestrator.agents.qc import Rule, RuleSet
from bim_orchestrator.policies.autonomy import AutonomyPolicy

from ._llm_stubs import StubRemediationAgent

_PATTERN = r"^[A-Z]{2}-\d{3}$"

# Four distinct current values, four individually-valid corrections. Every
# proposal below satisfies _PATTERN, which is the point: validation cannot rank
# them, so only the binding can.
_CURRENT = {"101": "aa 1", "102": "bb 2", "103": "cc 3", "104": "dd 4"}
_CORRECT = {"101": "AA-001", "102": "BB-002", "103": "CC-003", "104": "DD-004"}


def _agent(tmp_path, llm_agent):
    from bim_orchestrator.agents.design import DesignAgent

    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "actions": {"parameters": {"set_value": "approve"}},
                "severity_levels": {
                    "severity_high": "approve",
                    "severity_medium": "approve",
                    "severity_low": "approve",
                },
            }
        ),
        encoding="utf-8",
    )
    rule = Rule(
        id="doors.mark.format",
        category="Doors",
        parameter="Mark",
        requirement="matches_regex",
        pattern=_PATTERN,
        severity_tag="rule_violation",
        severity_level="severity_medium",
        description="Mark must follow XX-000",
        fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "llm_propose"},
    )
    agent = DesignAgent(
        mcp=None,
        autonomy=AutonomyPolicy.load(cfg),
        project_id="t",
        rules=RuleSet(scenario="t", target_category="Doors", rules=[rule]),
        llm_agent=llm_agent,
    )
    return agent, rule


def _findings_and_elements():
    findings = [
        {
            "rule_id": "doors.mark.format",
            "element_id": eid,
            "parameter": "Mark",
            "severity_tag": "rule_violation",
            "severity": "severity_medium",
            "message": "bad mark",
            "status": "non_compliant",
        }
        for eid in _CURRENT
    ]
    elements = {
        eid: {"id": eid, "category": "Doors", "params": {"Mark": cur}}
        for eid, cur in _CURRENT.items()
    }
    return findings, elements


def _memo_values(agent):
    return sorted(agent._llm_propose_memo.values())


class TestBatchBinding:
    def test_a_well_bound_batch_is_cached(self, tmp_path):
        """The check must not break the case it is protecting."""
        agent, _rule = _agent(tmp_path, StubRemediationAgent(batch_values=_CORRECT))
        findings, elements = _findings_and_elements()
        asyncio.run(agent._prewarm_llm_batches(findings, elements))
        assert _memo_values(agent) == sorted(_CORRECT.values())

    def test_answers_that_drifted_one_row_are_all_rejected(self, tmp_path):
        """The realistic failure: each answer is right for the NEXT element.

        Pre-fix all four were cached, so four doors were proposed another
        door's Mark — every one of them compliant with the rule and wrong.
        """
        ids = list(_CURRENT)
        shifted = {
            eid: (_CORRECT[ids[(i + 1) % len(ids)]], _CURRENT[ids[(i + 1) % len(ids)]])
            for i, eid in enumerate(ids)
        }
        agent, _rule = _agent(tmp_path, StubRemediationAgent(batch_values=shifted))
        findings, elements = _findings_and_elements()
        asyncio.run(agent._prewarm_llm_batches(findings, elements))
        assert agent._llm_propose_memo == {}, (
            "a shifted batch was cached — each element would be written with "
            "the value computed for another element, and every one passes the rule"
        )

    def test_one_unbound_answer_does_not_poison_the_others(self, tmp_path):
        values = dict(_CORRECT)
        values["102"] = (_CORRECT["102"], "something else entirely")
        agent, _rule = _agent(tmp_path, StubRemediationAgent(batch_values=values))
        findings, elements = _findings_and_elements()
        asyncio.run(agent._prewarm_llm_batches(findings, elements))
        assert _memo_values(agent) == sorted(
            v for k, v in _CORRECT.items() if k != "102"
        )

    def test_a_contract_v1_plugin_gets_nothing_cached(self, tmp_path):
        """A bare `{id: value}` map carries no evidence of which element it
        answers. Fail closed to the per-element path rather than trust it."""

        class V1Agent(StubRemediationAgent):
            async def propose_batch(self, items, *, context=None):
                return dict(_CORRECT)

        agent, _rule = _agent(tmp_path, V1Agent())
        findings, elements = _findings_and_elements()
        asyncio.run(agent._prewarm_llm_batches(findings, elements))
        assert agent._llm_propose_memo == {}

    def test_a_bound_but_non_compliant_value_is_still_rejected(self, tmp_path):
        """Binding is added to validation, not substituted for it."""
        values = {eid: "not a mark" for eid in _CURRENT}
        agent, _rule = _agent(tmp_path, StubRemediationAgent(batch_values=values))
        findings, elements = _findings_and_elements()
        asyncio.run(agent._prewarm_llm_batches(findings, elements))
        assert agent._llm_propose_memo == {}


class TestUnpackAndEcho:
    def test_v2_entry_yields_value_and_echo(self):
        assert _unpack_batch_entry({"value": " X ", "for_current_value": "old"}) == (
            "X",
            "old",
        )

    def test_v1_entry_yields_no_echo(self):
        assert _unpack_batch_entry("X") == ("X", None)

    def test_garbage_entry_yields_nothing(self):
        assert _unpack_batch_entry(["X"]) == (None, None)

    @pytest.mark.parametrize(
        ("echo", "asked", "expected"),
        [
            ("aa 1", "aa 1", True),
            ("'aa 1'", "aa 1", True),   # the prompt renders values with repr
            (" aa 1 ", "aa 1", True),
            ("aa 2", "aa 1", False),
            (None, "aa 1", False),
            ("", "aa 1", False),
        ],
    )
    def test_echo_matching_is_tight(self, echo, asked, expected):
        """Every loosening here makes two different elements look like one."""
        assert _echo_matches(echo, asked) is expected


class TestTrimFooterSaysHowMuchIsMissing:
    """The footer always admitted the list was trimmed. An approver reading 14
    lines of a 120-line proposal still could not tell whether they had seen most
    of it or a tenth — on the document that authorises the write."""

    def test_a_trimmed_body_states_both_counts(self):
        body = "\n".join(f"- line {i}" for i in range(400))
        out = _fit_acc_description(body)
        assert "of 400 lines" in out
        assert len(out) <= 1000

    def test_an_untrimmed_body_is_returned_verbatim(self):
        body = "- short list\n- two lines"
        assert _fit_acc_description(body) == body

    def test_the_reserve_is_still_respected(self):
        body = "\n".join(f"- line {i}" for i in range(400))
        out = _fit_acc_description(body, reserve=200)
        assert len(out) <= 800
