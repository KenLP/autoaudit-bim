"""``llm/factory.py``'s lazy-import socket — plugin present, absent, required.

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): the 3 ``make_*_agent`` functions no longer
construct an agent class directly; they gate on the flag (or an injected
``client``) and lazily ``importlib.import_module("bim_orchestrator_llm")``.
These tests pin that socket in isolation from whichever concrete agent class
the plugin ships — a fake module standing in for the real one is enough for
the gate/happy-path shape. The one test that needs the REAL plugin
(``test_real_plugin_smoke_when_installed``) mirrors M14's ``rules_extractor``
optional-dependency gate: skip cleanly when the plugin isn't installed, UNLESS
``BIM_REQUIRE_LLM_PLUGIN=1`` (the CI opt-in for an environment that's SUPPOSED
to have it), in which case a missing plugin is a hard failure, not a skip.
"""

from __future__ import annotations

import importlib
import os
import types

import pytest
from structlog.testing import capture_logs

from bim_orchestrator.llm import factory


def _plugin_installed() -> bool:
    try:
        importlib.import_module("bim_orchestrator_llm")
    except ImportError:
        return False
    return True


# ---- flag off / no client: no import attempted at all ----------------------


def test_disabled_flags_never_import_plugin(monkeypatch) -> None:
    monkeypatch.delenv("BIM_LLM_REMEDIATION", raising=False)
    monkeypatch.delenv("BIM_LLM_DIAGNOSTIC", raising=False)
    monkeypatch.delenv("BIM_LLM_SUPERVISOR", raising=False)

    def _boom(flag: str):
        raise AssertionError(f"should not attempt to load the plugin for {flag!r}")

    monkeypatch.setattr(factory, "_load_plugin", _boom)
    assert factory.make_remediation_agent() is None
    assert factory.make_diagnostic_agent() is None
    assert factory.make_supervisor_agent() is None


# ---- flag on, plugin missing: warn + None, never crash ---------------------


def test_flag_on_plugin_missing_warns_and_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    monkeypatch.setattr(factory, "_PLUGIN_MODULE", "bim_orchestrator_llm_does_not_exist_xyz")
    with capture_logs() as logs:
        result = factory.make_remediation_agent()
    assert result is None
    assert any(e.get("event") == "llm.plugin_missing" for e in logs)


def test_load_plugin_missing_logs_warning_with_hint(monkeypatch) -> None:
    monkeypatch.setattr(factory, "_PLUGIN_MODULE", "bim_orchestrator_llm_does_not_exist_xyz")
    with capture_logs() as logs:
        result = factory._load_plugin("BIM_LLM_SUPERVISOR")
    assert result is None
    events = [e for e in logs if e.get("event") == "llm.plugin_missing"]
    assert len(events) == 1
    assert events[0]["flag"] == "BIM_LLM_SUPERVISOR"
    assert "bim_orchestrator_llm_does_not_exist_xyz" in events[0]["hint"]


# ---- flag on, plugin present (fake module): happy path ---------------------


def test_flag_on_plugin_present_builds_agent(monkeypatch) -> None:
    # L2-13: this sentinel used to be a bare `object()`, which passed only
    # because nothing ever looked at what came back — precisely the gap the
    # contract check closes (see tests/test_plugin_contract.py). A stand-in for
    # a working plugin now has to look like a working plugin's agent.
    from ._llm_stubs import StubRemediationAgent

    monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
    sentinel = StubRemediationAgent()
    seen: dict = {}

    def _fake_builder(**kw):
        seen.update(kw)
        return sentinel

    fake_module = types.SimpleNamespace(make_remediation_agent=_fake_builder)
    monkeypatch.setattr(factory, "_load_plugin", lambda flag: fake_module)
    result = factory.make_remediation_agent()
    assert result is sentinel
    assert "client" in seen and "ctx" in seen  # args forwarded verbatim


def test_client_injection_bypasses_flag_but_still_loads_plugin(monkeypatch) -> None:
    """An explicit `client=` used to build the agent directly, regardless of the
    flag — that historical behaviour is preserved: no flag needed, but the
    agent CLASS still lives in the plugin, so the same lazy import applies."""
    from ._llm_stubs import StubDiagnosticAgent

    monkeypatch.delenv("BIM_LLM_DIAGNOSTIC", raising=False)
    sentinel = StubDiagnosticAgent()   # see the note above on bare object()
    fake_module = types.SimpleNamespace(make_diagnostic_agent=lambda **kw: sentinel)
    monkeypatch.setattr(factory, "_load_plugin", lambda flag: fake_module)
    result = factory.make_diagnostic_agent(client=object())
    assert result is sentinel


def test_supervisor_socket_forwards_rules(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_SUPERVISOR", "1")
    seen: dict = {}
    fake_module = types.SimpleNamespace(
        make_supervisor_agent=lambda **kw: seen.update(kw) or object()
    )
    monkeypatch.setattr(factory, "_load_plugin", lambda flag: fake_module)
    rules_sentinel = object()
    factory.make_supervisor_agent(rules=rules_sentinel)
    assert seen["rules"] is rules_sentinel


# ---- gate: BIM_REQUIRE_LLM_PLUGIN=1 must not silently pass without it ------


@pytest.mark.skipif(
    not _plugin_installed() and os.environ.get("BIM_REQUIRE_LLM_PLUGIN", "").strip() != "1",
    reason="bim_orchestrator_llm plugin not installed (optional extension)",
)
def test_real_plugin_smoke_when_installed(monkeypatch) -> None:
    if not _plugin_installed():
        pytest.fail(
            "BIM_REQUIRE_LLM_PLUGIN=1 but bim_orchestrator_llm is not installed "
            "(uv pip install -e <path-to>/bim-orchestrator-llm); did `uv "
            "sync` remove the local editable? Use `uv sync --extra dev "
            "--inexact` once installed."
        )
    from bim_orchestrator.llm.fake import FakeLLMClient

    agent = factory.make_remediation_agent(
        client=FakeLLMClient(default_json={"reason": "x", "value": "y"})
    )
    assert agent is not None
