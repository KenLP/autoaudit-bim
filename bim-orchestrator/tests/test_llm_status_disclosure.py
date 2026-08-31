"""L2-12 — a run must be able to say whether an AI was asked to take part.

Three ways Phase 2 could be requested and not happen, all of which produced a
run byte-identical to a plain Phase-1 one:

* a flag value the loader didn't recognise (``BIM_LLM_REMEDIATION=y``) — the
  plugin-missing path at least logged, the typo path said nothing at all;
* a flag on with no agent behind it (extension absent or incompatible);
* a ruleset declaring ``new_value_strategy: llm_propose`` on N rules with no
  remediation agent to serve them, so all N degraded to Path A issues.

In every case the findings are correct and the audit is valid. What was missing
is the reader's ability to tell **"the AI proposed nothing today"** from **"the
AI was never asked"** — which is the difference between a clean model and an
unrun feature. Hence recorded, not enforced: failing the run would be
disproportionate to a degrade that is also the safe default.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.audit_report import _render_llm_status
from bim_orchestrator.llm.factory import (
    _enabled,
    llm_flag_problems,
    llm_remediation_enabled,
)
from bim_orchestrator.orchestrator import _print_llm_status, _stamp_llm_status


# ---------------------------------------------------------------------------
# The flag itself
# ---------------------------------------------------------------------------


class _Rule:
    def __init__(self, rid, strategy=None):
        self.id = rid
        self.remediation = type("R", (), {"new_value_strategy": strategy})()


class _Rules:
    def __init__(self, *rules):
        self.rules = list(rules)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "y", "t", "enabled", "ENABLE"])
def test_values_an_operator_plainly_means_as_on(monkeypatch, value):
    monkeypatch.setenv("BIM_LLM_REMEDIATION", value)
    assert llm_remediation_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "n", "off", "disabled"])
def test_explicit_off_values(monkeypatch, value):
    monkeypatch.setenv("BIM_LLM_REMEDIATION", value)
    assert llm_remediation_enabled() is False


def test_unset_is_off_and_silent(monkeypatch):
    """Absence is the documented default — it must not produce a warning."""
    monkeypatch.delenv("BIM_LLM_REMEDIATION", raising=False)
    assert _enabled("BIM_LLM_REMEDIATION") is False
    assert llm_flag_problems() == []


def test_an_unrecognised_value_is_off_but_reported(monkeypatch):
    """Off is the SAFE direction here (unlike BIM_LLM_PROVIDER, which L2-04
    made raise, because there the wrong answer ships data to a third party).
    The defect was the silence, not the direction."""
    monkeypatch.delenv("BIM_LLM_DIAGNOSTIC", raising=False)
    monkeypatch.delenv("BIM_LLM_SUPERVISOR", raising=False)
    monkeypatch.setenv("BIM_LLM_REMEDIATION", "yeah nah")
    assert llm_remediation_enabled() is False
    assert llm_flag_problems() == [
        {"flag": "BIM_LLM_REMEDIATION", "value": "yeah nah"}
    ]


# ---------------------------------------------------------------------------
# The cross-check nobody was doing
# ---------------------------------------------------------------------------


class _Agent:
    pass


class TestStampLLMStatus:
    def test_a_pure_phase_1_run_says_nothing(self, monkeypatch):
        """No flags, no LLM rules, no bad values → the artifact is unchanged."""
        for flag in ("BIM_LLM_REMEDIATION", "BIM_LLM_DIAGNOSTIC", "BIM_LLM_SUPERVISOR"):
            monkeypatch.delenv(flag, raising=False)
        state: dict = {}
        _stamp_llm_status(state, rules=_Rules(_Rule("r1")))
        assert "llm_status" not in state

    def test_rules_asking_for_an_llm_with_no_agent_are_disclosed(self, monkeypatch):
        """The headline case: N rules requested a model-proposed value, no model
        was available, and every one silently became a Path A issue."""
        for flag in ("BIM_LLM_REMEDIATION", "BIM_LLM_DIAGNOSTIC", "BIM_LLM_SUPERVISOR"):
            monkeypatch.delenv(flag, raising=False)
        state: dict = {}
        _stamp_llm_status(
            state,
            rules=_Rules(
                _Rule("furniture.name", "llm_propose"),
                _Rule("door.mark", "llm_propose"),
                _Rule("wall.rating", "normalize"),
            ),
        )
        status = state["llm_status"]
        assert status["rules_requesting_llm"] == ["furniture.name", "door.mark"]
        assert status["llm_rules_degraded_to_path_a"] is True

    def test_a_wired_agent_clears_the_degrade_flag(self, monkeypatch):
        monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
        state: dict = {}
        _stamp_llm_status(
            state,
            rules=_Rules(_Rule("furniture.name", "llm_propose")),
            remediation_agent=_Agent(),
        )
        status = state["llm_status"]
        assert status["wired"]["remediation"] is True
        assert status["llm_rules_degraded_to_path_a"] is False

    def test_a_flag_on_with_no_agent_is_recorded(self, monkeypatch):
        """Plugin missing or incompatible: requested and wired disagree."""
        monkeypatch.setenv("BIM_LLM_SUPERVISOR", "1")
        state: dict = {}
        _stamp_llm_status(state, rules=_Rules(_Rule("r1")))
        status = state["llm_status"]
        assert status["requested"]["supervisor"] is True
        assert status["wired"]["supervisor"] is False

    def test_a_typo_reaches_the_artifact(self, monkeypatch):
        monkeypatch.setenv("BIM_LLM_REMEDIATION", "ye")
        state: dict = {}
        _stamp_llm_status(state, rules=_Rules(_Rule("r1")))
        assert state["llm_status"]["flag_problems"] == [
            {"flag": "BIM_LLM_REMEDIATION", "value": "ye"}
        ]

    def test_a_broken_ruleset_cannot_break_a_finished_run(self, monkeypatch):
        """Disclosure is the least important thing happening at this point."""
        monkeypatch.setenv("BIM_LLM_REMEDIATION", "1")
        state: dict = {}
        _stamp_llm_status(state, rules=object())   # no `.rules`
        assert state.get("llm_status", {}).get("rules_requesting_llm") == []


# ---------------------------------------------------------------------------
# It has to reach the reader, not just the dict
# ---------------------------------------------------------------------------


class TestRendering:
    @staticmethod
    def _status(**over):
        base = {
            "requested": {"remediation": False, "diagnostic": False, "supervisor": False},
            "wired": {"remediation": False, "diagnostic": False, "supervisor": False},
            "flag_problems": [],
            "rules_requesting_llm": [],
            "llm_rules_degraded_to_path_a": False,
        }
        base.update(over)
        return base

    def test_report_states_requested_versus_active(self):
        L: list[str] = []
        _render_llm_status(L, {"llm_status": self._status(
            requested={"remediation": True, "diagnostic": False, "supervisor": False},
            wired={"remediation": False, "diagnostic": False, "supervisor": False},
        )})
        body = "\n".join(L)
        assert "requested: **remediation**" in body
        assert "active: **none**" in body
        assert "could not be" in body

    def test_report_names_the_degraded_rules(self):
        L: list[str] = []
        _render_llm_status(L, {"llm_status": self._status(
            rules_requesting_llm=["furniture.name"],
            llm_rules_degraded_to_path_a=True,
        )})
        body = "\n".join(L)
        assert "furniture.name" in body
        assert "issues for a person" in body

    def test_report_flags_an_unreadable_value(self):
        L: list[str] = []
        _render_llm_status(L, {"llm_status": self._status(
            flag_problems=[{"flag": "BIM_LLM_REMEDIATION", "value": "ye"}],
        )})
        assert "not understood" in "\n".join(L)

    def test_a_phase_1_report_is_unchanged(self):
        L: list[str] = []
        _render_llm_status(L, {})
        assert L == []

    def test_the_executive_summary_actually_calls_it(self):
        """Pins the WIRING, not just the renderer.

        The first mutation I tried for this fix deleted the call site and
        nothing failed — because every test above invokes ``_render_llm_status``
        directly. A renderer nobody calls discloses nothing.
        """
        from bim_orchestrator.audit_report import render_audit_report

        md = render_audit_report(
            {
                "project_id": "b.demo", "iteration": 1, "max_iterations": 3,
                "elements": [], "findings": [], "proposed_fixes": [],
                "status": "converged", "error": None,
                "llm_status": self._status(
                    rules_requesting_llm=["furniture.name"],
                    llm_rules_degraded_to_path_a=True,
                ),
            },
            run_id="run-demo01",
        )
        assert "AI assistance" in md
        assert "furniture.name" in md

    def test_the_operator_sees_it_on_screen_too(self, capsys):
        """The log line is for the record; this is for the person about to read
        the findings and conclude the model is clean."""
        _print_llm_status({"llm_status": self._status(
            rules_requesting_llm=["furniture.name"],
            llm_rules_degraded_to_path_a=True,
        )})
        assert "1 rule(s) ask for an AI-proposed value" in capsys.readouterr().out

    def test_nothing_is_printed_on_a_phase_1_run(self, capsys):
        _print_llm_status({})
        assert capsys.readouterr().out == ""


def test_metadata_carries_the_status(tmp_path):
    """Same route query_coverage / geometry_coverage / llm_usage already take."""
    import json

    from bim_orchestrator.run_recorder import RunFolder

    folder = RunFolder.create(tmp_path, "run")
    state = {
        "findings": [],
        "llm_status": {"requested": {"remediation": True}, "wired": {"remediation": False},
                       "flag_problems": [], "rules_requesting_llm": ["r1"],
                       "llm_rules_degraded_to_path_a": True},
    }
    folder.write_metadata(state=state, status="converged")
    meta = json.loads(folder.metadata_path.read_text(encoding="utf-8"))
    assert meta["llm_status"]["llm_rules_degraded_to_path_a"] is True
