"""End-to-end: sheet-level rules run against a (mock) live model.

Confirms the missing link the black-box QA harness surfaced — a Sheets rule
authored by the Rule Builder + evaluated correctly in QC could NOT run live
because OST_Sheets had no fetch path. With the catalog entry + the
RevitQueryAgent ``list_sheets`` branch in place, the full chain works:

    RevitQueryAgent.run()  (OST_Sheets → revit_list_sheets → flattened params)
        → QCAgent.run()    (unique_in_set + present_and_nonempty)
        → 4-bucket outcomes

The mock sheet fixture (``SAMPLE_REVIT_SHEETS``) carries deliberate
violators: two sheets share number "A-102" (uniqueness) and one sheet has a
blank name (presence).
"""

from __future__ import annotations

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import RuleSet
from bim_orchestrator.state import OrchestratorState
from tests._mocks import MockRevitMCPClient


@pytest.fixture
def autonomy(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "parameters": {"set_value": {"severity_medium": "approve"}},
                },
                "severity_rules": {},
            }
        )
    )
    return AutonomyPolicy.load(cfg)


_SHEET_RULES = {
    "scenario": "novel_sheets",
    "target_category": "Sheets",
    "rules": [
        {
            "id": "sheets.number.unique",
            "parameter": "Sheet Number",
            "requirement": "unique_in_set",
            "severity_tag": "data_consistency",
            "severity_level": "severity_medium",
            "description": "Sheet Number must be unique",
            "autofill": {"strategy": "none"},
        },
        {
            "id": "sheets.name.present",
            "parameter": "Sheet Name",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "severity_level": "severity_medium",
            "description": "Sheet Name must be present",
            "autofill": {"strategy": "none"},
        },
    ],
}


def _state(elements) -> OrchestratorState:
    return {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }


@pytest.mark.asyncio
async def test_sheet_rules_run_end_to_end(autonomy, tmp_path):
    catalog = OSTCatalog.load()
    rules = RuleSet.model_validate(_SHEET_RULES)

    # 1) Live fetch: RevitQueryAgent resolves Sheets → OST_Sheets → list_sheets.
    async with MockRevitMCPClient() as client:
        query_state = await RevitQueryAgent(
            mcp=client, rules=rules, catalog=catalog
        ).run(_state([]))

    assert query_state["status"] == "checking"
    elements = query_state["elements"]
    assert len(elements) == 5  # SAMPLE_REVIT_SHEETS
    # Both rule params surfaced on each element (union of the two rules).
    for el in elements:
        assert el["category"] == "Sheets"
        assert set(el["params"]) == {"Sheet Number", "Sheet Name"}

    # 2) QC evaluates the two sheet rules against the fetched elements.
    rules_path = tmp_path / "rules.sheets.yaml"
    rules_path.write_text(yaml.safe_dump(_SHEET_RULES))
    qc_state = QCAgent(rules_path=rules_path, autonomy=autonomy).run(
        _state(elements)
    )

    summary = qc_state["outcomes_summary"]
    # 5 sheets × 2 rules = 10 evaluations.
    assert summary["total"] == 10
    # Sheet Number: A-102 appears twice → 2 non_compliant; 3 unique compliant.
    assert summary["non_compliant"] == 2
    # Sheet Name: the blank-name sheet → 1 missing_data; 4 present compliant.
    assert summary["missing_data"] == 1
    assert summary["compliant"] == 7

    # The uniqueness violations are exactly the two A-102 sheets.
    dup_ids = {f["element_id"] for f in qc_state["findings"]}
    assert dup_ids == {"301", "302"}
    # The presence gap is the blank-name sheet.
    miss_ids = {f["element_id"] for f in qc_state["missing_data_items"]}
    assert miss_ids == {"304"}
