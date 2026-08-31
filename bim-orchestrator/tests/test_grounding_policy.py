"""Tests for per-rule citation policy enforcement (Phase 2 Day 4)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bim_orchestrator.agents.grounding import GroundingAgent, _downgrade_severity
from bim_orchestrator.agents.qc import CitationPolicy, QCAgent, Rule, RuleSet
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.rag.store import VectorStore
from bim_orchestrator.state import Finding, OrchestratorState
from tests.test_grounding_agent import fake_embed


# ---- Severity helper ------------------------------------------------------


class TestDowngradeSeverity:
    def test_high_to_medium(self):
        assert _downgrade_severity("severity_high") == "severity_medium"

    def test_medium_to_low(self):
        assert _downgrade_severity("severity_medium") == "severity_low"

    def test_low_stays_low(self):
        assert _downgrade_severity("severity_low") == "severity_low"


# ---- Fixtures -------------------------------------------------------------


def _state_with(findings: list[Finding]) -> OrchestratorState:
    return {
        "project_id": "test", "iteration": 0, "max_iterations": 1,
        "elements": [], "findings": findings, "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def _finding(
    *,
    rule_id: str,
    parameter: str = "Department",
    severity: str = "severity_medium",
) -> Finding:
    return Finding(
        rule_id=rule_id, element_id="e1", parameter=parameter,
        severity_tag="missing_required_param",
        severity=severity,  # type: ignore[arg-type]
        message=f"{rule_id} failed",
        suggested_value=None, citation=None,
    )


def _build_rule(
    rule_id: str,
    *,
    parameter: str = "Department",
    citation_mode: str = "soft",
    source_filter: list[str] | None = None,
    on_missing: str = "warn",
) -> Rule:
    """Build a Rule with the given citation policy."""
    return Rule(
        id=rule_id,
        parameter=parameter,
        requirement="present_and_nonempty",
        severity_tag="missing_required_param",
        description="test rule",
        autofill={"strategy": "none", "fallback": None},  # type: ignore[arg-type]
        citation=CitationPolicy(
            mode=citation_mode,  # type: ignore[arg-type]
            source_filter=source_filter,
            on_missing=on_missing,  # type: ignore[arg-type]
        ),
    )


def _ruleset(rules: list[Rule]) -> RuleSet:
    return RuleSet(scenario="test", target_category="Rooms", rules=rules)


@pytest.fixture
def store_with_bep_and_ibc(tmp_path):
    store = VectorStore(
        persist_dir=tmp_path / "chroma", collection="policy-test",
        embed_fn=fake_embed,
    )
    store.ingest_text(
        "Every room shall declare a department for cost allocation.",
        source="BEP.pdf", section="§4.2", page=12,
    )
    store.ingest_text(
        "Fire rated walls require 2-hour rated assemblies per §711.",
        source="IBC.pdf", section="§711.2", page=45,
    )
    return store


@pytest.fixture
def empty_store(tmp_path):
    return VectorStore(
        persist_dir=tmp_path / "empty", collection="empty",
        embed_fn=fake_embed,
    )


# ---- Soft mode ------------------------------------------------------------


def test_soft_rule_with_hit_attaches_citation(store_with_bep_and_ibc):
    rules = _ruleset([_build_rule("room.department.required", citation_mode="soft")])
    agent = GroundingAgent(store=store_with_bep_and_ibc, rules=rules, min_score=0.0)
    findings = [_finding(rule_id="room.department.required")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    assert f["citation"] is not None
    assert "BEP.pdf" in f["citation"]
    # citation_missing should NOT be set for soft mode (backward compat)
    assert "citation_missing" not in f or f.get("citation_missing") is False


def test_soft_rule_with_miss_leaves_citation_none(empty_store):
    rules = _ruleset([_build_rule("room.department.required", citation_mode="soft")])
    agent = GroundingAgent(store=empty_store, rules=rules)
    findings = [_finding(rule_id="room.department.required")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    assert f["citation"] is None
    # No flag added — soft mode doesn't care
    assert "citation_missing" not in f


# ---- Hard mode (warn) ----------------------------------------------------


def test_hard_rule_with_hit_attaches_citation_and_sets_flag_false(store_with_bep_and_ibc):
    rules = _ruleset([_build_rule("room.department.required", citation_mode="hard")])
    agent = GroundingAgent(store=store_with_bep_and_ibc, rules=rules, min_score=0.0)
    findings = [_finding(rule_id="room.department.required")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    assert f["citation"] is not None
    assert f.get("citation_missing") is False


def test_hard_rule_with_miss_flags_citation_missing(empty_store):
    rules = _ruleset([_build_rule("room.department.required", citation_mode="hard")])
    agent = GroundingAgent(store=empty_store, rules=rules)
    findings = [_finding(rule_id="room.department.required", severity="severity_medium")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    assert f["citation"] is None
    assert f.get("citation_missing") is True
    # Severity should NOT change in warn mode
    assert f["severity"] == "severity_medium"


# ---- Hard mode (downgrade) -----------------------------------------------


def test_hard_downgrade_lowers_severity_on_miss(empty_store):
    rules = _ruleset([
        _build_rule("rule.high", citation_mode="hard", on_missing="downgrade"),
    ])
    agent = GroundingAgent(store=empty_store, rules=rules)
    findings = [_finding(rule_id="rule.high", severity="severity_high")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    assert f["severity"] == "severity_medium"
    assert f.get("citation_missing") is True


def test_hard_downgrade_low_stays_low(empty_store):
    """severity_low can't go any lower — stays low but flag is set."""
    rules = _ruleset([
        _build_rule("rule.low", citation_mode="hard", on_missing="downgrade"),
    ])
    agent = GroundingAgent(store=empty_store, rules=rules)
    findings = [_finding(rule_id="rule.low", severity="severity_low")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    assert f["severity"] == "severity_low"
    assert f.get("citation_missing") is True


def test_hard_downgrade_with_hit_does_not_lower(store_with_bep_and_ibc):
    """Citation found → no downgrade even with on_missing=downgrade."""
    rules = _ruleset([
        _build_rule("room.department.required",
                    citation_mode="hard", on_missing="downgrade"),
    ])
    agent = GroundingAgent(store=store_with_bep_and_ibc, rules=rules, min_score=0.0)
    findings = [_finding(rule_id="room.department.required", severity="severity_high")]
    result = agent.run(_state_with(findings))

    assert result["findings"][0]["severity"] == "severity_high"
    assert result["findings"][0].get("citation_missing") is False


# ---- source_filter -------------------------------------------------------


def test_source_filter_limits_to_named_source(store_with_bep_and_ibc):
    """A rule citing only BEP.pdf should never return an IBC.pdf chunk."""
    rules = _ruleset([
        _build_rule(
            "rule.bep_only",
            parameter="fire",  # Matches IBC content best, but filter forces BEP
            citation_mode="hard",
            source_filter=["BEP.pdf"],
        ),
    ])
    agent = GroundingAgent(store=store_with_bep_and_ibc, rules=rules, min_score=0.0)
    findings = [_finding(rule_id="rule.bep_only", parameter="fire")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    if f["citation"] is not None:
        # Every cited source must be from the filter
        assert "IBC.pdf" not in f["citation"]
        assert "BEP.pdf" in f["citation"]


def test_source_filter_multi_source(store_with_bep_and_ibc):
    """Filter with both BEP and IBC allows either."""
    rules = _ruleset([
        _build_rule(
            "rule.both",
            parameter="fire",
            citation_mode="hard",
            source_filter=["BEP.pdf", "IBC.pdf"],
        ),
    ])
    agent = GroundingAgent(store=store_with_bep_and_ibc, rules=rules, min_score=0.0)
    findings = [_finding(rule_id="rule.both", parameter="fire")]
    result = agent.run(_state_with(findings))
    assert result["findings"][0]["citation"] is not None


# ---- Backward compatibility -----------------------------------------------


def test_no_ruleset_falls_back_to_soft_default(store_with_bep_and_ibc):
    """When no RuleSet is passed, every finding uses default policy (soft)."""
    agent = GroundingAgent(store=store_with_bep_and_ibc, rules=None, min_score=0.0)
    findings = [_finding(rule_id="any.rule")]
    result = agent.run(_state_with(findings))

    f = result["findings"][0]
    # Citation attached if relevant (soft mode behavior)
    # citation_missing NOT set because rule has no policy → default soft
    assert "citation_missing" not in f


def test_rules_yaml_without_citation_block_defaults_to_soft(tmp_path, store_with_bep_and_ibc):
    """An existing rules YAML (no citation: block) loads cleanly into soft mode."""
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(yaml.safe_dump({
        "scenario": "legacy",
        "target_category": "Rooms",
        "rules": [{
            "id": "room.legacy",
            "parameter": "Department",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "description": "legacy rule, no citation block",
            "autofill": {"strategy": "none", "fallback": None},
        }],
    }))
    autonomy_path = tmp_path / "autonomy.yaml"
    autonomy_path.write_text(yaml.safe_dump({
        "mutations": {}, "severity_rules": {"missing_required_param": "severity_medium"},
    }))
    qc = QCAgent(rules_path=rules_path, autonomy=AutonomyPolicy.load(autonomy_path))

    # The rule's policy should be soft + warn (defaults)
    rule = qc.rules.rules[0]
    assert rule.citation.mode == "soft"
    assert rule.citation.on_missing == "warn"
    assert rule.citation.source_filter is None


# ---- Empty store + hard rules (option B) ---------------------------------


def test_empty_store_with_hard_rules_proceeds_with_warning(empty_store, caplog):
    """Option B: don't refuse to run. Mark hard findings as citation_missing."""
    import logging
    caplog.set_level(logging.WARNING)

    rules = _ruleset([
        _build_rule("rule.soft", citation_mode="soft"),
        _build_rule("rule.hard", citation_mode="hard"),
    ])
    agent = GroundingAgent(store=empty_store, rules=rules)
    findings = [
        _finding(rule_id="rule.soft"),
        _finding(rule_id="rule.hard"),
    ]
    result = agent.run(_state_with(findings))

    soft_finding = result["findings"][0]
    hard_finding = result["findings"][1]
    # Soft → no flag added
    assert "citation_missing" not in soft_finding
    # Hard → flagged
    assert hard_finding.get("citation_missing") is True
    assert hard_finding["citation"] is None
