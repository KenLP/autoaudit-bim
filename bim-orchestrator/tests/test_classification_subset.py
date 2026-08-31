"""Phase 2 GĐ2 — value_in_subset: turning a slice of "meaning" into a
deterministic membership check.

Covers the primitive, the category→codes resolver, and the QC DETECT side —
the headline being that a *well-formed but wrong-object* classification code
(a window code on a door) is rejected BY MACHINE, no LLM judge, no human.

SPEC_LLM_PLUGIN_SPLIT (2026-07-07): the REMEDIATION closed-loop half of this
suite (constructing a real ``RemediationLLMAgent`` to accept/reject a proposed
code, prompt-content assertions, evidence-on-fix) moved to the private
``bim-orchestrator-llm`` plugin's ``tests/test_classification_subset_llm.py`` —
it exercises that agent's own prompt design, not just this engine's primitive.
What's left here needs no LLM agent at all.
"""

from __future__ import annotations

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.classification import ClassificationCatalog
from bim_orchestrator.policies.rules_engine import evaluate, value_in_subset


# ---- engine primitive ------------------------------------------------------


def test_value_in_subset_membership() -> None:
    assert value_in_subset("Pr_30_59_24", ["Pr_30_59_24", "X"]) is True
    assert value_in_subset("Pr_99_99_99_99", ["Pr_30_59_24"]) is False
    assert value_in_subset("  Pr_30_59_24 ", ["Pr_30_59_24"]) is True  # trim-tolerant
    assert value_in_subset("", ["X"]) is True   # blank = non-applicable (pass)
    assert value_in_subset(None, ["X"]) is True
    assert value_in_subset("X", []) is False    # empty subset → nothing allowed


def test_evaluate_dispatch() -> None:
    with pytest.raises(ValueError):
        evaluate("value_in_subset", "X")  # subset required
    assert evaluate("value_in_subset", "A", subset=["A", "B"]) is True
    assert evaluate("value_in_subset", "C", subset=["A", "B"]) is False


# ---- resolver --------------------------------------------------------------


def test_resolver_from_yaml(tmp_path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump(
            {"version": 1, "system": "Uniclass2015", "subsets": {"Doors": ["Pr_30_59_24"]}}
        ),
        encoding="utf-8",
    )
    cat = ClassificationCatalog.load(p, use_ost=False)
    assert cat.system == "Uniclass2015"
    assert cat.subset_for("Doors") == ["Pr_30_59_24"]
    assert cat.subset_for("doors") == ["Pr_30_59_24"]   # case-insensitive
    assert cat.subset_for("Spaceships") == []           # unknown
    assert cat.subset_for(None) == []


def test_shipped_sample_loads() -> None:
    """The committed sample table loads and has the demo categories."""
    cat = ClassificationCatalog.load(use_ost=False)
    assert cat.subset_for("Doors")        # non-empty
    assert cat.subset_for("Windows")


# ---- grounding (definitions) ------------------------------------------------


def test_definitions_resolver_from_sample() -> None:
    cat = ClassificationCatalog.load(use_ost=False)
    defs = cat.definitions_for("Doors")
    assert defs.get("Pr_30_59_24_14") == "Fire-resisting doorsets"
    assert cat.define("doors", "Pr_30_59_24") == "Doorsets"  # case-insensitive
    assert cat.define("Doors", "Pr_99_99_99_99") is None


# ---- QC DETECT side: detect-rule == accept-rule -----------------------------


def _autonomy(tmp_path) -> AutonomyPolicy:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "documents": {"create_issue": "auto"},
                    "parameters": {"set_value": "approve"},
                },
                "severity_rules": {"data_quality": "severity_medium"},
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _subset_rules_file(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "scenario": "door_classification",
                "target_category": "Doors",
                "rules": [
                    {
                        "id": "door.classification.subset",
                        "parameter": "Classification",
                        "requirement": "value_in_subset",
                        "severity_tag": "data_quality",
                        "severity_level": "severity_medium",
                        "description": "Door code must be a valid Uniclass door code",
                        "autofill": {"strategy": "none"},
                        "remediation": {
                            "action": "set_parameter",
                            "target_parameter": "Classification",
                            "new_value_strategy": "llm_propose",
                        },
                    }
                ],
            }
        )
    )
    return p


def _qc_state(elements):
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": elements, "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


def test_qc_detects_out_of_subset_code(tmp_path) -> None:
    cat = ClassificationCatalog(
        system="Uniclass2015",
        subsets={"Doors": ["Pr_30_59_24", "Pr_30_59_24_14"]},
    )
    agent = QCAgent(
        rules_path=_subset_rules_file(tmp_path),
        autonomy=_autonomy(tmp_path),
        classification=cat,
    )
    elements = [
        {"id": "1", "category": "Doors", "params": {"Classification": "Pr_30_59_24"}},      # valid
        {"id": "2", "category": "Doors", "params": {"Classification": "Pr_99_99_99_99"}},   # wrong object
    ]
    result = agent.run(_qc_state(elements))
    flagged = {f["element_id"] for f in result["findings"]}
    assert "2" in flagged   # detected by machine
    assert "1" not in flagged
