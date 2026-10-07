"""Profile-scoped LLM agents (2026-08-31).

The three agent flags used to be process-wide, so switching the AI layer on
for the one demo that needs it switched it on for every audit the AuditHub
service ran afterwards. Now ``AuditProfile.llm`` names the agents and
``audit()`` sets the flags only for the duration of that run.

Pins: the schema (defaults off, only ON flags become overrides), the scoped
override (sets, restores "unset", refuses unknown keys), the committed
demo profile, and the integration — during ``audit()``'s dispatch the flag is
visible; after it returns, the environment is exactly what it was.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bim_orchestrator.llm.factory import (
    agent_flag_overrides,
    llm_diagnostic_enabled,
    llm_remediation_enabled,
)
from bim_orchestrator.policies.audit_profile import (
    LlmAgentsConfig,
    load_audit_profile,
)

_CONFIG = Path(__file__).resolve().parents[1] / "config"


# ---- schema ----------------------------------------------------------------


def test_defaults_are_off_and_produce_no_override() -> None:
    cfg = LlmAgentsConfig()
    assert not (cfg.remediation or cfg.diagnostic or cfg.supervisor)
    assert cfg.env_overrides() == {}


def test_only_enabled_agents_become_overrides() -> None:
    cfg = LlmAgentsConfig(remediation=True, supervisor=True)
    assert cfg.env_overrides() == {
        "BIM_LLM_REMEDIATION": "1",
        "BIM_LLM_SUPERVISOR": "1",
    }


def test_profile_yaml_carries_the_block(tmp_path: Path) -> None:
    rules = tmp_path / "rules.x.yaml"
    rules.write_text("scenario: x\nrules: []\n", encoding="utf-8")
    p = tmp_path / "audit.x.yaml"
    p.write_text(
        f"name: x\nrules:\n  - {rules.as_posix()}\nrun:\n  mode: check\n"
        "llm:\n  diagnostic: true\n",
        encoding="utf-8",
    )
    prof = load_audit_profile(p)
    assert prof.llm.diagnostic is True
    assert prof.llm.remediation is False
    assert prof.llm.env_overrides() == {"BIM_LLM_DIAGNOSTIC": "1"}


def test_profile_without_the_block_is_unchanged(tmp_path: Path) -> None:
    rules = tmp_path / "rules.x.yaml"
    rules.write_text("scenario: x\nrules: []\n", encoding="utf-8")
    p = tmp_path / "audit.x.yaml"
    p.write_text(f"name: x\nrules:\n  - {rules.as_posix()}\n", encoding="utf-8")
    assert load_audit_profile(p).llm.env_overrides() == {}


def test_committed_au_demo_llm_profile_loads() -> None:
    prof = load_audit_profile(_CONFIG / "audit.au_demo_llm.yaml")
    assert prof.run.mode == "demo"
    assert [Path(r).name for r in prof.rules] == ["rules.p2_demo.yaml"]
    assert prof.llm.env_overrides() == {
        "BIM_LLM_REMEDIATION": "1",
        "BIM_LLM_DIAGNOSTIC": "1",
        "BIM_LLM_SUPERVISOR": "1",
    }


# ---- scoped override --------------------------------------------------------


def test_override_sets_then_restores_unset(monkeypatch) -> None:
    monkeypatch.delenv("BIM_LLM_REMEDIATION", raising=False)
    assert not llm_remediation_enabled()
    with agent_flag_overrides({"BIM_LLM_REMEDIATION": "1"}):
        assert llm_remediation_enabled()
    assert "BIM_LLM_REMEDIATION" not in os.environ


def test_override_restores_a_previous_value(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_DIAGNOSTIC", "0")
    with agent_flag_overrides({"BIM_LLM_DIAGNOSTIC": "1"}):
        assert llm_diagnostic_enabled()
    assert os.environ["BIM_LLM_DIAGNOSTIC"] == "0"


def test_override_restores_even_when_the_body_raises(monkeypatch) -> None:
    monkeypatch.delenv("BIM_LLM_SUPERVISOR", raising=False)
    with pytest.raises(RuntimeError):
        with agent_flag_overrides({"BIM_LLM_SUPERVISOR": "1"}):
            raise RuntimeError("mid-run crash")
    assert "BIM_LLM_SUPERVISOR" not in os.environ


def test_override_refuses_non_agent_keys(monkeypatch) -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        with agent_flag_overrides({"ANTHROPIC_API_KEY": "leak"}):
            pass  # pragma: no cover


def test_empty_override_is_a_no_op(monkeypatch) -> None:
    monkeypatch.delenv("BIM_LLM_REMEDIATION", raising=False)
    with agent_flag_overrides({}):
        assert not llm_remediation_enabled()


# ---- integration: audit() scopes the flags to the dispatch ------------------


@pytest.mark.asyncio
async def test_audit_scopes_flags_to_the_run(monkeypatch, tmp_path: Path) -> None:
    """During the dispatch the profile's agent is ON; after audit() returns the
    process is back to what it was — the service runs many audits in one
    process, and a demo must not leave the AI layer on for the next one."""
    from bim_orchestrator import audit_axes, orchestrator

    monkeypatch.delenv("BIM_LLM_REMEDIATION", raising=False)

    rules = tmp_path / "rules.x.yaml"
    rules.write_text("scenario: x\nrules: []\n", encoding="utf-8")
    profile = tmp_path / "audit.x.yaml"
    profile.write_text(
        f"name: x\nrules:\n  - {rules.as_posix()}\nrun:\n  mode: check\n"
        "llm:\n  remediation: true\n",
        encoding="utf-8",
    )

    async def fake_axes(*_a, **_k):
        return SimpleNamespace(findings=[], skipped=[])

    seen: dict[str, object] = {}

    async def fake_check(*_a, **_k):
        seen["during"] = llm_remediation_enabled()
        return 0

    monkeypatch.setattr(audit_axes, "run_audit_axes", fake_axes)
    monkeypatch.setattr(orchestrator, "check", fake_check)

    rc = await orchestrator.audit(
        profile,
        orchestrator.DEFAULT_AUTONOMY_PATH,
        tmp_path / "findings.json",
        max_iterations=1,
        checkpoint_dir=tmp_path / "ckpt",
    )
    assert rc == 0
    assert seen["during"] is True
    assert "BIM_LLM_REMEDIATION" not in os.environ


@pytest.mark.asyncio
async def test_audit_restores_flags_when_the_run_raises(monkeypatch, tmp_path: Path) -> None:
    from bim_orchestrator import audit_axes, orchestrator

    monkeypatch.delenv("BIM_LLM_DIAGNOSTIC", raising=False)
    rules = tmp_path / "rules.x.yaml"
    rules.write_text("scenario: x\nrules: []\n", encoding="utf-8")
    profile = tmp_path / "audit.x.yaml"
    profile.write_text(
        f"name: x\nrules:\n  - {rules.as_posix()}\nrun:\n  mode: check\n"
        "llm:\n  diagnostic: true\n",
        encoding="utf-8",
    )

    async def fake_axes(*_a, **_k):
        return SimpleNamespace(findings=[], skipped=[])

    async def exploding_check(*_a, **_k):
        raise RuntimeError("mid-run")

    monkeypatch.setattr(audit_axes, "run_audit_axes", fake_axes)
    monkeypatch.setattr(orchestrator, "check", exploding_check)

    with pytest.raises(RuntimeError):
        await orchestrator.audit(
            profile,
            orchestrator.DEFAULT_AUTONOMY_PATH,
            tmp_path / "findings.json",
            max_iterations=1,
            checkpoint_dir=tmp_path / "ckpt",
        )
    assert "BIM_LLM_DIAGNOSTIC" not in os.environ
