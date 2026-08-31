"""L2-13 + L2-09 + L2-11 — the core↔plugin contract, checked from core's side.

The Phase 2 review's measurement: a reviewer mutated the REAL plugin three ways
— added a required keyword to a builder, deleted ``propose_batch``, changed a
return shape — and this suite stayed at 2052 passed with nothing moving, while
all three crash the first time a ``BIM_LLM_*`` flag is on. Everything here
exists so that sentence stops being true.

Three layers, deliberately:

* the harness itself (``llm/conformance.py``) detects each of those mutations;
* ``llm/factory.py`` USES it, so a drifted plugin degrades instead of crashing;
* the REAL plugin, when installed, passes it — including the runtime shapes a
  signature cannot see. That last class skips in public CI (the plugin is
  private) and runs wherever the plugin actually is, which is exactly where the
  drift happens.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bim_orchestrator.llm import factory as llm_factory
from bim_orchestrator.llm.conformance import (
    PLUGIN_CONTRACT_VERSION,
    ScriptedLLMClient,
    check_agent,
    check_agent_runtime,
    check_plugin_module,
)
from bim_orchestrator.llm.usage import LLMRunContext, UsageRecorder

from ._llm_stubs import (
    StubDiagnosticAgent,
    StubRemediationAgent,
    StubSupervisorAgent,
)


# --------------------------------------------------------------------------
# The harness detects the mutations that slipped through
# --------------------------------------------------------------------------


class _GoodModule:
    """A plugin module shaped exactly as core calls it."""

    @staticmethod
    def make_remediation_agent(*, client=None, ctx=None):
        return StubRemediationAgent()

    @staticmethod
    def make_diagnostic_agent(*, client=None, rules=None, ctx=None):
        return StubDiagnosticAgent()

    @staticmethod
    def make_supervisor_agent(*, client=None, rules=None, ctx=None):
        return StubSupervisorAgent()


def test_a_conforming_module_has_no_problems():
    assert check_plugin_module(_GoodModule) == []


def test_builder_that_grew_a_required_keyword_is_caught():
    """Mutation #1 from the review: `TypeError` on every call from core."""

    class Drifted(_GoodModule):
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None, project_id):
            return StubRemediationAgent()

    problems = check_plugin_module(Drifted)
    assert any("project_id" in p for p in problems), problems


def test_builder_that_dropped_a_keyword_core_passes_is_caught():
    class Drifted(_GoodModule):
        @staticmethod
        def make_diagnostic_agent(*, client=None, ctx=None):  # lost `rules`
            return StubDiagnosticAgent()

    problems = check_plugin_module(Drifted)
    assert any("rules" in p for p in problems), problems


def test_renamed_builder_is_caught():
    class Drifted(_GoodModule):
        make_supervisor_agent = None

    assert any("make_supervisor_agent" in p for p in check_plugin_module(Drifted))


def test_agent_that_lost_propose_batch_is_caught():
    """Mutation #2: only visible on the instance, not on the builder."""

    class NoBatch(StubRemediationAgent):
        propose_batch = None

    problems = check_agent(NoBatch(), kind="remediation")
    assert any("propose_batch" in p for p in problems), problems


def test_agent_method_that_stopped_being_async_is_caught():
    class Sync(StubRemediationAgent):
        def propose_batch(self, items, *, context=None):  # core awaits this
            return {}

    assert any("async" in p for p in check_agent(Sync(), kind="remediation"))


def test_stubs_in_this_suite_satisfy_the_contract():
    """Pins the test doubles themselves — a stub that drifts from the Protocol
    turns every wiring test into a test of something core will never meet."""
    assert check_agent(StubRemediationAgent(), kind="remediation") == []
    assert check_agent(StubDiagnosticAgent(), kind="diagnostic") == []
    assert check_agent(StubSupervisorAgent(), kind="supervisor") == []


def test_none_is_not_a_contract_problem():
    """`None` is the documented "disabled" answer, handled everywhere in core."""
    assert check_agent(None, kind="remediation") == []


# --------------------------------------------------------------------------
# Runtime shapes — mutation #3
# --------------------------------------------------------------------------


def test_runtime_check_catches_a_changed_return_shape():
    class DictReturning(StubRemediationAgent):
        async def propose(self, finding, *, validate, context=None, safety_critical=False):
            return {"proposed_value": "X", "autonomy": "approve"}  # was an object

    problems = asyncio.run(check_agent_runtime(DictReturning(), kind="remediation"))
    assert any("proposed_value" in p for p in problems), problems


def test_runtime_check_catches_a_batch_that_lost_its_binding():
    """Contract v2: an entry with no echo cannot be tied to its question."""

    class V1Batch(StubRemediationAgent):
        async def propose_batch(self, items, *, context=None):
            return {str(i["element_id"]): "VALUE" for i in items}   # bare str = v1

    problems = asyncio.run(check_agent_runtime(V1Batch(), kind="remediation"))
    assert any("for_current_value" in p or "mapping" in p for p in problems), problems


def test_runtime_check_catches_a_plugin_that_ignores_its_validator():
    """The P2-01 guarantee, asserted from OUTSIDE the plugin for the first time."""
    from ._llm_stubs import UnvalidatedRemediationAgent

    problems = asyncio.run(
        check_agent_runtime(UnvalidatedRemediationAgent(), kind="remediation")
    )
    assert any("REJECTED" in p for p in problems), problems


def test_runtime_check_catches_a_supervisor_that_returns_extra_keys():
    class Greedy(StubSupervisorAgent):
        async def run(self, state):
            return {"supervisor_directive": dict(self.directive), "findings": []}

    problems = asyncio.run(check_agent_runtime(Greedy(), kind="supervisor"))
    assert any("findings" in p for p in problems), problems


def test_stubs_pass_the_runtime_check_too():
    assert asyncio.run(check_agent_runtime(StubRemediationAgent(), kind="remediation")) == []
    assert asyncio.run(check_agent_runtime(StubDiagnosticAgent(), kind="diagnostic")) == []
    assert asyncio.run(check_agent_runtime(StubSupervisorAgent(), kind="supervisor")) == []


def test_scripted_client_synthesises_from_the_schema():
    """The harness must not need a network or the plugin's prompts."""
    client = ScriptedLLMClient(overrides={"element_id": "E7"})
    out = asyncio.run(
        client.complete_json(
            system="s",
            prompt="p",
            schema={
                "type": "object",
                "properties": {
                    "proposals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["element_id", "value"],
                        },
                    }
                },
                "required": ["proposals"],
            },
        )
    )
    assert out["proposals"][0]["element_id"] == "E7"


# --------------------------------------------------------------------------
# factory.py acts on the check: degrade, never crash
# --------------------------------------------------------------------------


def test_factory_refuses_an_incompatible_plugin_instead_of_crashing(monkeypatch):
    class Drifted:
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None, project_id):
            return StubRemediationAgent()

        make_diagnostic_agent = _GoodModule.make_diagnostic_agent
        make_supervisor_agent = _GoodModule.make_supervisor_agent

    monkeypatch.setattr("importlib.import_module", lambda _n: Drifted)
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    # Pre-fix this raised TypeError from inside the factory, aborting a run
    # whose documented answer to "not available" is None.
    assert llm_factory.make_remediation_agent() is None


def test_factory_degrades_when_a_builder_raises(monkeypatch):
    class Exploding:
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None):
            raise RuntimeError("plugin blew up during construction")

        make_diagnostic_agent = _GoodModule.make_diagnostic_agent
        make_supervisor_agent = _GoodModule.make_supervisor_agent

    monkeypatch.setattr("importlib.import_module", lambda _n: Exploding)
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    assert llm_factory.make_remediation_agent() is None


def test_factory_degrades_when_the_built_agent_is_missing_a_method(monkeypatch):
    class NoBatch(StubRemediationAgent):
        propose_batch = None

    class Mod:
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None):
            return NoBatch()

        make_diagnostic_agent = _GoodModule.make_diagnostic_agent
        make_supervisor_agent = _GoodModule.make_supervisor_agent

    monkeypatch.setattr("importlib.import_module", lambda _n: Mod)
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    assert llm_factory.make_remediation_agent() is None


def test_a_conforming_plugin_still_wires_up(monkeypatch):
    """The check must not become a way to break a working install."""
    monkeypatch.setattr("importlib.import_module", lambda _n: _GoodModule)
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    monkeypatch.setenv("BIM_LLM_DIAGNOSTIC", "1")
    monkeypatch.setenv("BIM_LLM_SUPERVISOR", "1")
    assert llm_factory.make_remediation_agent() is not None
    assert llm_factory.make_diagnostic_agent() is not None
    assert llm_factory.make_supervisor_agent() is not None


# --------------------------------------------------------------------------
# L2-11 — an injected client is still counted against the run budget
# --------------------------------------------------------------------------


class _CountingClient:
    model = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, system, prompt, max_tokens=512):
        self.calls += 1
        return ""

    async def complete_json(self, *, system, prompt, schema=None, max_tokens=512):
        self.calls += 1
        return {"reason": "r", "value": "V"}


def test_injected_client_is_metered_when_a_run_context_exists(monkeypatch):
    """Pre-fix the plugin used an injected client verbatim, so its calls were
    unbudgeted AND invisible — the recorder saw zero, `format_line()` returned
    None, and the run printed nothing at all. Quieter than the correct path."""
    captured: dict[str, Any] = {}

    class Mod:
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None):
            captured["client"] = client
            return StubRemediationAgent()

        make_diagnostic_agent = _GoodModule.make_diagnostic_agent
        make_supervisor_agent = _GoodModule.make_supervisor_agent

    monkeypatch.setattr("importlib.import_module", lambda _n: Mod)
    inner = _CountingClient()
    ctx = LLMRunContext(client=inner, recorder=UsageRecorder(max_calls=5))
    llm_factory.make_remediation_agent(client=inner, ctx=ctx)

    handed = captured["client"]
    assert handed is not inner, "core handed the raw client through — no metering"
    asyncio.run(handed.complete_json(system="s", prompt="p"))
    assert ctx.recorder.total_calls == 1
    assert ctx.recorder.format_line() is not None


def test_injected_client_without_a_context_is_passed_through(monkeypatch):
    """No ctx means no budget to enforce — wrapping would only add a layer."""
    captured: dict[str, Any] = {}

    class Mod:
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None):
            captured["client"] = client
            return StubRemediationAgent()

        make_diagnostic_agent = _GoodModule.make_diagnostic_agent
        make_supervisor_agent = _GoodModule.make_supervisor_agent

    monkeypatch.setattr("importlib.import_module", lambda _n: Mod)
    inner = _CountingClient()
    llm_factory.make_remediation_agent(client=inner)
    assert captured["client"] is inner


def test_injected_client_respects_the_budget(monkeypatch):
    """The circuit breaker has to fire on this path too, not just the ctx path."""
    from bim_orchestrator.llm.client import LLMError

    captured: dict[str, Any] = {}

    class Mod:
        @staticmethod
        def make_remediation_agent(*, client=None, ctx=None):
            captured["client"] = client
            return StubRemediationAgent()

        make_diagnostic_agent = _GoodModule.make_diagnostic_agent
        make_supervisor_agent = _GoodModule.make_supervisor_agent

    monkeypatch.setattr("importlib.import_module", lambda _n: Mod)
    inner = _CountingClient()
    ctx = LLMRunContext(client=inner, recorder=UsageRecorder(max_calls=1))
    llm_factory.make_remediation_agent(client=inner, ctx=ctx)
    handed = captured["client"]
    asyncio.run(handed.complete_json(system="s", prompt="p"))
    with pytest.raises(LLMError):
        asyncio.run(handed.complete_json(system="s", prompt="p"))


# --------------------------------------------------------------------------
# The REAL plugin, where it is installed
# --------------------------------------------------------------------------


plugin = pytest.importorskip(
    "bim_orchestrator_llm",
    reason="private extension not installed — the contract is checked wherever it is",
)


def test_real_plugin_matches_the_module_contract():
    assert check_plugin_module(plugin) == [], (
        f"installed plugin does not match socket contract v{PLUGIN_CONTRACT_VERSION}"
    )


@pytest.mark.parametrize(
    ("builder", "kind"),
    [
        ("make_remediation_agent", "remediation"),
        ("make_diagnostic_agent", "diagnostic"),
        ("make_supervisor_agent", "supervisor"),
    ],
)
def test_real_plugin_agents_match_the_agent_contract(builder, kind):
    agent = getattr(plugin, builder)(client=ScriptedLLMClient())
    assert check_agent(agent, kind=kind) == []


def test_real_remediation_agent_matches_the_runtime_contract():
    """Signatures can't see a changed return shape, a batch that dropped its
    echo, or a plugin that stopped honouring its validator. This can."""
    agent = plugin.make_remediation_agent(
        client=ScriptedLLMClient(overrides={"element_id": "E1"})
    )
    assert asyncio.run(check_agent_runtime(agent, kind="remediation")) == []


def test_real_advisory_agents_match_the_runtime_contract():
    state = {
        "findings": [
            {
                "element_id": "E1", "rule_id": "R1", "parameter": "Mark",
                "status": "non_compliant", "message": "m", "severity": "Medium",
            }
        ],
        "iteration": 1,
        "max_iterations": 3,
    }
    diag = plugin.make_diagnostic_agent(client=ScriptedLLMClient())
    assert asyncio.run(
        check_agent_runtime(diag, kind="diagnostic", state_factory=lambda: dict(state))
    ) == []
    sup = plugin.make_supervisor_agent(client=ScriptedLLMClient())
    assert asyncio.run(
        check_agent_runtime(sup, kind="supervisor", state_factory=lambda: dict(state))
    ) == []
