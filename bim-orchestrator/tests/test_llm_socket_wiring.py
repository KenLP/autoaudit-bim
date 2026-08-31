"""Phase 2 — DesignAgent ``llm_propose`` socket: wiring + governance, agent-agnostic.

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): the real ``RemediationLLMAgent`` (prompt
construction, repair loop, batching, memo, all the scenario coverage) moved to
the private ``bim-orchestrator-llm`` plugin — see its
``tests/test_remediation_llm_wiring.py`` / ``test_remediation_llm_scenarios.py``.

What's pinned here is what DesignAgent itself guarantees for ANYTHING that
satisfies ``RemediationAgentProtocol`` (a canned-value stub is enough — see
``tests/_llm_stubs.py``), independent of the concrete agent's own judgment:

  1. Closed loop — ``_make_validator`` re-runs the rule's own requirement
     (no agent involved at all).
  2. Phase-1-safe — no agent injected → no value → Path A.
  3. Never auto — an LLM-originated value is at most ``approve``-gated, and
     ``human-only`` for ``llm_safety_critical`` params, REGARDLESS of an
     ``auto`` autonomy policy.
  4. M12 regression — a human-only-gated fix must fold into a Path A ACC
     issue (not vanish as an orphan preview) while the Path-B write itself
     stays ``executed=False``.
  5. A rejected/None proposal routes to Path A.
  6. The fix preview is stamped ``value_source="llm"`` for the Approvals UI.

All offline, no LLM client of any kind — the stub returns a canned value.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.qc import Rule, RuleSet
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import Finding
from tests._llm_stubs import StubRemediationAgent, UnvalidatedRemediationAgent
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient

FURNITURE_PATTERN = r"^ADSK_Fur_[A-Za-z0-9]+(_[A-Za-z0-9]+)?$"


def _autonomy(tmp_path, *, set_value: str = "auto") -> AutonomyPolicy:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "documents": {"create_issue": "auto"},
                    "parameters": {"set_value": set_value},
                },
                "severity_rules": {"naming_violation": "severity_low"},
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _furniture_rule(*, safety_critical: bool = False, action: str = "rename_element") -> Rule:
    return Rule.model_validate(
        {
            "id": "furniture.family_name.naming_format",
            "parameter": "Family Name",
            "requirement": "matches_regex",
            "pattern": FURNITURE_PATTERN,
            "severity_tag": "naming_violation",
            "severity_level": "severity_low",
            "description": "Furniture family names must match ADSK_Fur_Item_Optional",
            "fixability": "auto",
            "remediation": {
                "action": action,
                "target_parameter": "Family Name",
                "new_value_strategy": "llm_propose",
                "llm_safety_critical": safety_critical,
            },
            "autofill": {"strategy": "none"},
        }
    )


def _finding(eid: str = "501") -> Finding:
    return Finding(
        rule_id="furniture.family_name.naming_format",
        element_id=eid,
        parameter="Family Name",
        severity_tag="naming_violation",
        severity="severity_low",  # type: ignore[arg-type]
        message="furniture name 'Table_Round_withchair' violates naming format",
        suggested_value=None,
        citation=None,
    )


def _element(eid: str = "501", name: str = "Table_Round_withchair") -> dict[str, Any]:
    return {
        "id": eid,
        "name": name,
        "category": "Furniture",
        "params": {"Family Name": name},
        "params_display": {"Family Name": name},
    }


def _revit_with_furniture(eid: int = 501, name: str = "Table_Round_withchair") -> MockRevitMCPClient:
    return MockRevitMCPClient(
        element_info={
            eid: {
                "id": eid,
                "name": name,
                "category": "Furniture",
                "parameters": [
                    {"name": "Name", "value": name, "valueString": name},
                    {"name": "Family Name", "value": name, "valueString": name},
                ],
            }
        }
    )


def _agent(tmp_path, *, llm_agent, set_value: str = "auto") -> DesignAgent:
    return DesignAgent(
        mcp=MockFormaMCPClient(elements=[]),
        autonomy=_autonomy(tmp_path, set_value=set_value),
        project_id="t.test",
        max_issues=10,
        rule_filter=None,
        revit_mcp=_revit_with_furniture(),
        rules=RuleSet(scenario="t", target_category="Furniture", rules=[_furniture_rule()]),
        llm_agent=llm_agent,
    )


# ---- 1. closed loop (no agent needed) --------------------------------------


def test_validator_is_the_detect_rule() -> None:
    """_make_validator re-runs the rule's own requirement on a single value."""
    agent = DesignAgent(
        mcp=None, autonomy=None, project_id="t", revit_mcp=MockRevitMCPClient(),  # type: ignore[arg-type]
        rules=RuleSet(scenario="t", target_category="Furniture", rules=[_furniture_rule()]),
    )
    validate = agent._make_validator(_furniture_rule())
    assert validate("ADSK_Fur_Chair_Viper") is True
    assert validate("Table_Round_withchair") is False


# ---- 2. Phase-1-safe + closed-loop acceptance/rejection --------------------


@pytest.mark.asyncio
async def test_compliant_proposal_accepted(tmp_path) -> None:
    stub = StubRemediationAgent("ADSK_Fur_Table_Round")
    agent = _agent(tmp_path, llm_agent=stub)
    value = await agent._llm_propose(_finding(), _element(), _furniture_rule())
    assert value == "ADSK_Fur_Table_Round"


@pytest.mark.asyncio
async def test_noncompliant_proposal_rejected(tmp_path) -> None:
    """A value that fails the detect-rule is rejected → None (→ Path A)."""
    stub = StubRemediationAgent("still a bad name")
    agent = _agent(tmp_path, llm_agent=stub)
    value = await agent._llm_propose(_finding(), _element(), _furniture_rule())
    assert value is None


@pytest.mark.asyncio
async def test_disabled_without_agent(tmp_path) -> None:
    """Phase 1 default: no LLM agent → no value (the finding routes to Path A)."""
    agent = _agent(tmp_path, llm_agent=None)
    value = await agent._llm_propose(_finding(), _element(), _furniture_rule())
    assert value is None


# ---- 3. governance: never auto ---------------------------------------------


@pytest.mark.asyncio
async def test_llm_value_never_auto_even_when_policy_is_auto(tmp_path) -> None:
    """Even with parameters.set_value=auto, an LLM-originated value is gated to
    ``approve`` and the fix is NOT executed."""
    stub = StubRemediationAgent("ADSK_Fur_Table_Round")
    agent = _agent(tmp_path, llm_agent=stub, set_value="auto")
    fix, spec = await agent._prepare_revit_fix(_finding(), _element(), _furniture_rule())
    assert fix is not None
    assert fix["autonomy"] == "approve"
    assert fix["executed"] is False
    assert spec is None  # nothing handed to the batch committer
    assert fix["new_value"] == "ADSK_Fur_Table_Round"


@pytest.mark.asyncio
async def test_llm_fix_carries_value_source_provenance(tmp_path) -> None:
    """The fix preview is stamped value_source='llm' so the Approvals UI can
    badge an LLM-proposed value (vs a deterministic one)."""
    stub = StubRemediationAgent("ADSK_Fur_Table_Round")
    agent = _agent(tmp_path, llm_agent=stub)
    fix, _ = await agent._prepare_revit_fix(_finding(), _element(), _furniture_rule())
    assert fix is not None
    assert (fix.get("preview") or {}).get("value_source") == "llm"


@pytest.mark.asyncio
async def test_safety_critical_forces_human_only(tmp_path) -> None:
    rule = _furniture_rule(safety_critical=True)
    stub = StubRemediationAgent("ADSK_Fur_Table_Round")
    agent = _agent(tmp_path, llm_agent=stub)
    fix, spec = await agent._prepare_revit_fix(_finding(), _element(), rule)
    assert fix is not None
    assert fix["autonomy"] == "human-only"
    assert fix["executed"] is False


@pytest.mark.asyncio
async def test_human_only_fix_routes_to_path_a_issue_with_suggested_value(
    tmp_path,
) -> None:
    """M12: a full run() with a human-only-gated fix must NOT vanish from the
    ACC loop. Before the fix, a fix with autonomy=="human-only" fell through
    BOTH the auto-commit branch (decision != auto) AND the approve-gated
    proposal branch (which only checks autonomy=="approve") — landing in
    `proposed_fixes` as an orphan preview, invisible to ACC, forever
    executed=False. Now it must be folded into a Path A ACC issue that shows
    the suggested value, while the original Path-B fix itself stays
    executed=False (it is NEVER auto-applied or approve-gated for write)."""
    rule = _furniture_rule(safety_critical=True)
    forma = MockFormaMCPClient(elements=[])
    stub = StubRemediationAgent("ADSK_Fur_Table_Round")
    agent = DesignAgent(
        mcp=forma,
        autonomy=_autonomy(tmp_path, set_value="auto"),
        project_id="t.test",
        max_issues=10,
        rule_filter=None,
        revit_mcp=_revit_with_furniture(),
        rules=RuleSet(scenario="t", target_category="Furniture", rules=[rule]),
        llm_agent=stub,
    )
    state = {
        "project_id": "t.test", "iteration": 0, "max_iterations": 1,
        "elements": [_element()], "findings": [_finding()],
        "proposed_fixes": [], "status": "designing", "error": None,
    }
    result = await agent.run(state)  # type: ignore[arg-type]

    # Exactly one ACC issue was created (dry-run + real), containing the
    # suggested value the stub proposed.
    create_calls = [
        a for name, a in forma.calls if name == "create_issue" and a.get("dry_run") is False
    ]
    assert len(create_calls) == 1
    assert "ADSK_Fur_Table_Round" in create_calls[0]["description"]
    assert "MANUAL write" in create_calls[0]["description"]

    # The original Path-B fix (the write attempt) stays executed=False and is
    # NEVER routed through the approve-gated proposal/watcher path.
    b_fixes = [f for f in result["proposed_fixes"] if f["finding_id"].startswith("furniture.")]
    assert len(b_fixes) == 1
    assert b_fixes[0]["executed"] is False
    assert b_fixes[0]["autonomy"] == "human-only"


@pytest.mark.asyncio
async def test_rejected_proposal_routes_to_path_a(tmp_path) -> None:
    """No usable value → (None, None) sentinel = re-route to a Path A issue."""
    stub = StubRemediationAgent("nope")  # fails the furniture regex
    agent = _agent(tmp_path, llm_agent=stub)
    fix, spec = await agent._prepare_revit_fix(_finding(), _element(), _furniture_rule())
    assert fix is None and spec is None


class TestCoreRevalidatesEveryProposal:
    """P2-01 (2026-07-25 live review) — the single-element path accepted
    ``proposal.proposed_value`` verbatim.

    The batch path already re-ran the validator core-side; this one did not.
    So the guarantee "every proposal is re-validated by the rule that flagged
    it" held only because the PLUGIN calls the validator handed to it — code in
    a separate repo, on its own version. An invariant enforced here had quietly
    become an invariant trusted of somebody else, on exactly one branch.

    Autonomy still caps LLM values independently, so this was never an
    unauthorised-write hole. It is about where the guarantee LIVES.
    """

    @pytest.mark.asyncio
    async def test_a_plugin_that_skips_validation_cannot_get_a_value_through(
        self, tmp_path
    ) -> None:
        stub = UnvalidatedRemediationAgent("still a bad name")
        agent = _agent(tmp_path, llm_agent=stub)
        value = await agent._llm_propose(_finding(), _element(), _furniture_rule())
        assert value is None, "core accepted a value its own rule rejects"

    @pytest.mark.asyncio
    async def test_the_rejection_is_logged_as_a_core_rejection(
        self, tmp_path
    ) -> None:
        # This must be diagnosable as "the plugin misbehaved", not blend into
        # the ordinary "model had no value" path — they call for different
        # fixes by different people.
        from structlog.testing import capture_logs

        agent = _agent(
            tmp_path, llm_agent=UnvalidatedRemediationAgent("still a bad name")
        )
        with capture_logs() as logs:
            await agent._llm_propose(_finding(), _element(), _furniture_rule())
        events = [entry.get("event") for entry in logs]
        assert "design_agent.llm_propose.rejected_by_core" in events
        assert "design_agent.llm_propose.accepted" not in events

    @pytest.mark.asyncio
    async def test_a_compliant_value_from_the_same_bad_plugin_still_passes(
        self, tmp_path
    ) -> None:
        # The re-check accepts on merit, not on whether the plugin behaved:
        # skipping validation is not itself disqualifying if the value is good.
        stub = UnvalidatedRemediationAgent("ADSK_Fur_Table_Round")
        agent = _agent(tmp_path, llm_agent=stub)
        value = await agent._llm_propose(_finding(), _element(), _furniture_rule())
        assert value == "ADSK_Fur_Table_Round"

    @pytest.mark.asyncio
    async def test_a_well_behaved_plugin_is_unaffected(self, tmp_path) -> None:
        # Guard: the extra check must not change the normal path, and must not
        # double-reject a value the plugin already validated.
        stub = StubRemediationAgent("ADSK_Fur_Table_Round")
        agent = _agent(tmp_path, llm_agent=stub)
        assert await agent._llm_propose(
            _finding(), _element(), _furniture_rule()
        ) == "ADSK_Fur_Table_Round"
