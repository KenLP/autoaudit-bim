"""L2-14 — the unattended-write grant must key on what produced the value.

v1.4-K4 Opt B lets a fix bypass ``autonomy.yaml`` and write Revit unattended.
Its argument is specific: a ``compose_template`` fill is COMPUTED, not judged,
so no human adds anything by approving it.

The gate did not check that. It read ``autofill.strategy`` alone — a field that
does not decide where the written value comes from. ``_compute_new_value``
dispatches on ``remediation.new_value_strategy``, and only ``inferred`` actually
routes through the autofill pipeline. So a rule carrying

    remediation: {new_value_strategy: fixed, new_value: <literal>}
    autofill:    {strategy: compose_template, ...}

had its LITERAL written unattended on the strength of a template that never
ran — overriding the operator's own policy file, which is the one place they
declared what may happen without them.

"A literal in the YAML" is also not automatically "a value a human chose":
since C1, rules are drafted by the extraction agent from a spec PDF, so the
literal can be a model's paraphrase of a clause. That is precisely the class of
value the Phase 2 governance rule says is never ``auto``.

The fix requires both halves to agree. Everything else falls to
``autonomy.yaml`` — where an operator expects the decision to be made.
"""

from __future__ import annotations

from typing import Any

import pytest

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.qc import Rule
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
from tests.test_design_agent_path_b import (
    _autonomy,
    _finding,
    _room,
    _ruleset,
    _state,
)

_EID = "829712"  # present in SAMPLE_REVIT_ELEMENT_INFO


def _rule(*, value_strategy: str, autofill_strategy: str | None, **extra: Any) -> Rule:
    body: dict[str, Any] = {
        "id": "ducts.mark.required",
        "parameter": "Mark",
        "requirement": "present_and_nonempty",
        "severity_tag": "missing_required_param",  # → severity_medium
        "description": "Mark naming convention",
        "fixability": "auto",
        "remediation": {
            "action": "set_parameter",
            "target_parameter": "Mark",
            "new_value_strategy": value_strategy,
            **extra,
        },
    }
    if autofill_strategy == "compose_template":
        body["autofill"] = {
            "strategy": "compose_template",
            "template": "{X}",
            "sequence_scope": [],
        }
    else:
        # `autofill` is a required field; "none" is the schema's way of saying
        # this rule has no fill strategy at all.
        body["autofill"] = {"strategy": autofill_strategy or "none"}
    return Rule.model_validate(body)


async def _run_gate(
    tmp_path, rule: Rule, *, suggested: Any = "Mark-1", set_value: str = "approve"
) -> tuple[str, MockRevitMCPClient]:
    """Run one finding through the Path B gate; return (decision, revit mock).

    ``set_value`` is the operator's own policy for ``parameters.set_value``.
    The default "approve" isolates the K4 Opt B bypass (any "auto" came from
    the bypass and nothing else); pass "auto" to reproduce the shipped
    ``severity_low: auto`` default, which is the OTHER way a write reaches the
    model unattended — the one a policy-side assertion cannot see.
    """
    revit = MockRevitMCPClient()
    agent = DesignAgent(
        mcp=MockFormaMCPClient(elements=[]),
        autonomy=_autonomy(tmp_path, set_value=set_value),
        project_id="b.test",
        max_issues=10,
        rule_filter=None,
        revit_mcp=revit,
        rules=_ruleset(rule),
    )
    state = await agent.run(
        _state(
            [
                _finding(
                    rule_id=rule.id,
                    element_id=_EID,
                    parameter="Mark",
                    suggested=suggested,
                )
            ],
            [_room(_EID, "R1")],
        )
    )
    fixes = state["proposed_fixes"]
    assert fixes, "no Path B fix was produced — the test is not exercising the gate"
    return fixes[0]["autonomy"], revit


async def _decide(tmp_path, rule: Rule, *, suggested: Any = "Mark-1") -> str:
    decision, _ = await _run_gate(tmp_path, rule, suggested=suggested)
    return decision


def _committed_writes(revit: MockRevitMCPClient) -> list[dict[str, Any]]:
    """Every call that actually MUTATES the model.

    A dry-run carries the same tool name as the real thing — the whole Path B
    design is "preview first, then maybe commit" — so filtering by tool name
    alone counts the preview as a write and the assertion passes/fails for the
    wrong reason. `dryRun` is the only thing that separates the two.
    """
    mutating = ("revit_set_parameter", "revit_batch", "revit_rename_element")
    return [
        args
        for name, args in revit.calls
        if name in mutating and not args.get("dryRun")
    ]


@pytest.mark.asyncio
class TestAutoGrantFollowsTheValue:
    async def test_a_literal_is_not_auto_just_because_a_template_exists(self, tmp_path):
        """The headline case: `fixed` writes `remediation.new_value` verbatim.

        The template never ran, so nothing here is 'computed' — yet the gate
        granted auto and the write went in without the operator.
        """
        rule = _rule(
            value_strategy="fixed",
            autofill_strategy="compose_template",
            new_value="WHATEVER-THE-PDF-SAID",
        )
        assert await _decide(tmp_path, rule) == "approve"

    async def test_a_real_compose_template_fill_is_still_auto(self, tmp_path):
        """K4 Opt B must survive intact — this is the case it was written for."""
        rule = _rule(value_strategy="inferred", autofill_strategy="compose_template")
        assert await _decide(tmp_path, rule) == "auto"

    async def test_next_available_is_not_auto(self, tmp_path):
        """Deterministic, but never granted auto by this gate; it only ever
        looked that way when an unrelated autofill happened to be present."""
        rule = _rule(value_strategy="next_available", autofill_strategy="compose_template")
        assert await _decide(tmp_path, rule, suggested="Mark-1") == "approve"

    async def test_next_available_is_never_auto_even_when_POLICY_says_auto(
        self, tmp_path
    ):
        """F-02 — the guarantee, pinned at the end that matters.

        The test above only ever proved "policy said approve, we returned
        approve": it pinned the wire, not the guarantee. `next_available` is not
        in `_AUTO_AUTOFILL_STRATEGIES`, so it fell through to `autonomy.yaml` —
        and the SHIPPED `autonomy.yaml` says `severity_low: auto`. A renumber
        authored at low severity (the Rule Builder lets Haiku call a naming fix
        "cosmetic") therefore wrote the model with no human, while the public
        capability catalog promised "never a silent write".
        """
        rule = _rule(
            value_strategy="next_available", autofill_strategy="compose_template"
        )
        decision, revit = await _run_gate(
            tmp_path, rule, suggested="Mark-1", set_value="auto"
        )
        assert decision == "approve"
        assert _committed_writes(revit) == [], (
            "a next_available renumber reached the model without a human"
        )

    async def test_next_available_demote_never_LOOSENS_a_stricter_policy(
        self, tmp_path
    ):
        """Demote-only. An operator who set `human-only` must still get
        human-only — the fix narrows what writes unattended, it does not
        relocate the decision away from `autonomy.yaml`."""
        rule = _rule(
            value_strategy="next_available", autofill_strategy="compose_template"
        )
        decision, _ = await _run_gate(
            tmp_path, rule, suggested="Mark-1", set_value="human-only"
        )
        assert decision == "human-only"

    async def test_a_real_compose_template_fill_survives_the_next_available_demote(
        self, tmp_path
    ):
        """The neighbouring grant must be untouched: this fix is about ONE
        value strategy, not a re-narrowing of K4 Opt B."""
        rule = _rule(value_strategy="inferred", autofill_strategy="compose_template")
        decision, _ = await _run_gate(tmp_path, rule, set_value="auto")
        assert decision == "auto"

    async def test_a_literal_with_no_autofill_is_unchanged(self, tmp_path):
        """The pre-existing correct behaviour, pinned so the fix is a
        NARROWING of auto and not a reshuffle."""
        rule = _rule(
            value_strategy="fixed", autofill_strategy=None, new_value="FIXED-1"
        )
        assert await _decide(tmp_path, rule) == "approve"

    async def test_inferred_without_a_deterministic_autofill_is_severity_gated(
        self, tmp_path
    ):
        """`inferred` alone is not enough — the autofill has to be one of the
        exact-output strategies. An inference is a judgment."""
        rule = _rule(value_strategy="inferred", autofill_strategy="infer_from_room_name")
        assert await _decide(tmp_path, rule) == "approve"

    async def test_an_llm_value_is_still_never_auto(self, tmp_path):
        """Governance invariant #2, re-pinned here because this fix rewrites the
        branch immediately above it."""
        from tests._llm_stubs import StubRemediationAgent

        rule = _rule(value_strategy="llm_propose", autofill_strategy="compose_template")
        # autonomy.yaml says AUTO here, so nothing but the governance branch
        # itself can hold the write back.
        agent = DesignAgent(
            mcp=MockFormaMCPClient(elements=[]),
            autonomy=_autonomy(tmp_path, set_value="auto"),
            project_id="b.test",
            max_issues=10,
            rule_filter=None,
            revit_mcp=MockRevitMCPClient(),
            rules=_ruleset(rule),
            llm_agent=StubRemediationAgent("MARK-LLM"),
        )
        state = await agent.run(
            _state(
                [_finding(rule_id=rule.id, element_id=_EID, parameter="Mark",
                          suggested="Mark-1")],
                [_room(_EID, "R1")],
            )
        )
        path_b = [f for f in state["proposed_fixes"] if f.get("new_value") is not None]
        assert path_b, "expected a Path B fix carrying the LLM value"
        assert all(f["autonomy"] != "auto" for f in path_b)


def test_the_auto_strategy_list_is_explicit():
    """A governance list, not an inline literal: widening it widens what this
    product writes without a human, and that should be a visible edit."""
    from bim_orchestrator.agents.design import _AUTO_AUTOFILL_STRATEGIES

    assert _AUTO_AUTOFILL_STRATEGIES == frozenset({"compose_template"})
