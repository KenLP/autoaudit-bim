"""Integration tests for QC with new numeric / set-scope evaluators.

These cover the Phase 2 Week 6 Day 2 evaluators end-to-end through the
QCAgent: from YAML rule → element loop → Finding emission. Uses the
Snowdon-derived SAMPLE_REVIT_ROOMS fixture via inline param flattening so
we don't need a live Revit connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import OrchestratorState


def _make_autonomy(tmp_path: Path) -> AutonomyPolicy:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "parameters": {"set_value": {"severity_medium": "approve"}},
                },
                "severity_rules": {
                    "missing_required_param": "severity_medium",
                    "geometric_violation": "severity_high",
                    "duplicate_identifier": "severity_medium",
                },
            }
        )
    )
    return AutonomyPolicy.load(cfg)


def _empty_state(elements: list[dict[str, Any]]) -> OrchestratorState:
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


def _element(eid: str, name: str, **params: Any) -> dict[str, Any]:
    """Build an element shape the QCAgent expects (already param-flattened)."""
    return {
        "id": eid,
        "name": name,
        "category": "Rooms",
        "params": params,
    }


# ---- numeric_min ----------------------------------------------------------


class TestNumericMinRule:
    def _rules_yaml(self, tmp_path: Path) -> Path:
        path = tmp_path / "rules_area.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "scenario": "room area minimum",
                    "target_category": "Rooms",
                    "rules": [
                        {
                            "id": "room.area.other_min",
                            "parameter": "areaMetric",
                            "requirement": "numeric_min",
                            "threshold": 9.0,
                            "severity_tag": "geometric_violation",
                            "description": "Other occupied rooms must be ≥ 9 m².",
                            "autofill": {"strategy": "none"},
                        }
                    ],
                }
            )
        )
        return path

    def test_fires_on_violator(self, tmp_path: Path) -> None:
        elements = [
            _element("p04", "Storage P04", areaMetric=5.32),
            _element("e1", "Elevator E1", areaMetric=5.74),
            _element("s203", "Studio Unit 203", areaMetric=55.08),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        findings = result["findings"]
        assert len(findings) == 2
        ids = {f["element_id"] for f in findings}
        assert ids == {"p04", "e1"}

    def test_passes_when_all_meet_threshold(self, tmp_path: Path) -> None:
        elements = [_element("s203", "Studio 203", areaMetric=55.08)]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        assert result["findings"] == []


# ---- numeric_min_conditional ---------------------------------------------


class TestNumericMinConditional:
    def _rules_yaml(self, tmp_path: Path) -> Path:
        path = tmp_path / "rules_res.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "scenario": "residential bedroom area",
                    "target_category": "Rooms",
                    "rules": [
                        {
                            "id": "room.area.residential_min",
                            "parameter": "areaMetric",
                            "requirement": "numeric_min_conditional",
                            "threshold": 10.0,
                            "when_param": "Occupancy",
                            "when_pattern": r"^Residential",
                            "severity_tag": "geometric_violation",
                            "description": "Residential rooms must be ≥ 10 m².",
                            "autofill": {"strategy": "none"},
                        }
                    ],
                }
            )
        )
        return path

    def test_fires_on_residential_below_threshold(self, tmp_path: Path) -> None:
        # Synthetic small bedroom — out-of-scope storage room should NOT fire
        elements = [
            _element(
                "b999", "Bedroom 999",
                areaMetric=8.5, Occupancy="Residential One Story",
            ),
            _element("p04", "Storage P04", areaMetric=5.32, Occupancy=""),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0]["element_id"] == "b999"

    def test_passes_when_condition_value_missing(self, tmp_path: Path) -> None:
        elements = [
            _element("c201", "Corridor 201", areaMetric=55.38, Occupancy=None),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        assert result["findings"] == []


# ---- unique_in_set --------------------------------------------------------


class TestUniqueInSetRule:
    def _rules_yaml(self, tmp_path: Path) -> Path:
        path = tmp_path / "rules_unique.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "scenario": "room number uniqueness",
                    "target_category": "Rooms",
                    "rules": [
                        {
                            "id": "room.number.unique",
                            "parameter": "Number",
                            "requirement": "unique_in_set",
                            "severity_tag": "duplicate_identifier",
                            "description": "Room numbers must be unique.",
                            "autofill": {"strategy": "none"},
                        }
                    ],
                }
            )
        )
        return path

    def test_fires_on_both_duplicates(self, tmp_path: Path) -> None:
        elements = [
            _element("s203a", "Studio 203", Number="203"),
            _element("s203b", "Duplicate 203", Number="203"),
            _element("s204", "Studio 204", Number="204"),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        findings = result["findings"]
        # Both 203 rooms fire; 204 is unique → passes
        ids = {f["element_id"] for f in findings}
        assert ids == {"s203a", "s203b"}

    def test_empty_number_does_not_fire(self, tmp_path: Path) -> None:
        # Two rooms with blank Number → not duplicates, just missing
        elements = [
            _element("r1", "Room A", Number=""),
            _element("r2", "Room B", Number=""),
            _element("r3", "Room C", Number="201"),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        assert result["findings"] == []

    def test_all_unique_no_findings(self, tmp_path: Path) -> None:
        elements = [
            _element("a", "A", Number="101"),
            _element("b", "B", Number="102"),
            _element("c", "C", Number="103"),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        assert result["findings"] == []


# ---- Combined rule set (mimics rules.room_compliance.yaml shape) ----------


class TestCombinedRoomCompliance:
    """End-to-end: 3 rules + Snowdon-shaped fixture → expected fire pattern."""

    def _rules_yaml(self, tmp_path: Path) -> Path:
        path = tmp_path / "rules_combo.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "scenario": "room compliance combined",
                    "target_category": "Rooms",
                    "rules": [
                        {
                            "id": "room.area.other_min",
                            "parameter": "areaMetric",
                            "requirement": "numeric_min",
                            "threshold": 9.0,
                            "severity_tag": "geometric_violation",
                            "description": "Other rooms ≥ 9 m².",
                            "autofill": {"strategy": "none"},
                        },
                        {
                            "id": "room.area.residential_min",
                            "parameter": "areaMetric",
                            "requirement": "numeric_min_conditional",
                            "threshold": 10.0,
                            "when_param": "Occupancy",
                            "when_pattern": r"^Residential",
                            "severity_tag": "geometric_violation",
                            "description": "Residential rooms ≥ 10 m².",
                            "autofill": {"strategy": "none"},
                        },
                        {
                            "id": "room.number.unique",
                            "parameter": "Number",
                            "requirement": "unique_in_set",
                            "severity_tag": "duplicate_identifier",
                            "description": "Room numbers must be unique.",
                            "autofill": {"strategy": "none"},
                        },
                    ],
                }
            )
        )
        return path

    def test_snowdon_like_dataset(self, tmp_path: Path) -> None:
        # Mirrors SAMPLE_REVIT_ROOMS but flattened to params shape
        elements = [
            _element(
                "studio-203", "Studio Unit 203",
                areaMetric=55.08, Occupancy="Residential One Story", Number="203",
            ),
            _element(
                "duplicate-203", "Duplicate Studio 203",
                areaMetric=55.74, Occupancy="Residential One Story", Number="203",
            ),
            _element(
                "storage-p04", "Storage P04",
                areaMetric=5.32, Occupancy="", Number="P04",
            ),
            _element(
                "corridor-201", "Corridor 201",
                areaMetric=55.38, Occupancy="", Number="201",
            ),
            _element(
                "bedroom-999", "Bedroom 999",
                areaMetric=8.5, Occupancy="Residential One Story", Number="999",
            ),
        ]
        qc = QCAgent(self._rules_yaml(tmp_path), _make_autonomy(tmp_path))
        result = qc.run(_empty_state(elements))
        by_rule: dict[str, set[str]] = {}
        for f in result["findings"]:
            by_rule.setdefault(f["rule_id"], set()).add(f["element_id"])

        # other_min fires on storage-p04 (5.32 < 9) AND bedroom-999 (8.5 < 9)
        assert by_rule["room.area.other_min"] == {"storage-p04", "bedroom-999"}
        # residential_min fires on bedroom-999 (8.5 < 10, Occupancy matches);
        # other residentials (studio-203, duplicate-203) pass at ~55 m²
        assert by_rule["room.area.residential_min"] == {"bedroom-999"}
        # number unique fires on both 203 rooms
        assert by_rule["room.number.unique"] == {"studio-203", "duplicate-203"}
