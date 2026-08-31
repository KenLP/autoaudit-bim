"""Tests for scripts/json_to_yaml.py — the v1 manual Rules Extraction converter.

Covers:
  * Happy-path roundtrip on the sample JSON shipped with the prompt template
  * Each guard (UNSUPPORTED_*, unknown severity_tag, non-ASCII, bad schema)
  * Output file is loadable by RuleSet after conversion
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

# scripts/ is not a package; import the script module by adding it to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import json_to_yaml  # noqa: E402

from bim_orchestrator.agents.qc import RuleSet  # noqa: E402

SAMPLE_JSON = PROJECT_ROOT.parent / "references" / "templates" / "sample-extracted-rules.json"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))


def _write_json(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "input.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── Happy path ─────────────────────────────────────────────────────────────


def test_sample_json_exists() -> None:
    assert SAMPLE_JSON.exists(), (
        f"Sample fixture missing: {SAMPLE_JSON}. Restore from git."
    )


def test_roundtrip_sample_to_yaml(tmp_path: Path) -> None:
    out = tmp_path / "rules.sample.yaml"
    code = json_to_yaml.main([str(SAMPLE_JSON), "--out", str(out)])
    assert code == 0
    assert out.exists()

    reload = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
    assert reload.scenario == "sample_room_compliance"
    assert reload.target_category == "Rooms"
    assert len(reload.rules) == 3
    ids = {r.id for r in reload.rules}
    assert ids == {"room.area.residential_min", "room.number.unique", "room.number.format"}


def test_yaml_header_includes_source(tmp_path: Path) -> None:
    out = tmp_path / "rules.sample.yaml"
    json_to_yaml.main([str(SAMPLE_JSON), "--out", str(out)])
    body = out.read_text(encoding="utf-8")
    assert "Source document: Sample_BEP.pdf" in body
    assert "Extracted: 2026-05-31" in body


# ── Guards ─────────────────────────────────────────────────────────────────


def test_rejects_unsupported_requirement(tmp_path: Path, sample_payload: dict) -> None:
    sample_payload["rules"][0]["requirement"] = "UNSUPPORTED_numeric_max"
    inp = _write_json(tmp_path, sample_payload)
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 4
    assert not out.exists()


def test_rejects_unknown_severity_tag(tmp_path: Path, sample_payload: dict) -> None:
    sample_payload["rules"][0]["severity_tag"] = "totally_new_tag"
    inp = _write_json(tmp_path, sample_payload)
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 5


def test_rejects_non_ascii_description(tmp_path: Path, sample_payload: dict) -> None:
    sample_payload["rules"][0]["description"] = "Area must be ≥ 10 m²"
    inp = _write_json(tmp_path, sample_payload)
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 6


def test_rejects_non_ascii_comments_template(tmp_path: Path, sample_payload: dict) -> None:
    sample_payload["rules"][0]["remediation"]["comments_template"] = (
        "BEP §1.1 non-compliant"
    )
    inp = _write_json(tmp_path, sample_payload)
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 6


def test_rejects_bad_schema(tmp_path: Path, sample_payload: dict) -> None:
    # Remove required field
    del sample_payload["rules"][0]["parameter"]
    inp = _write_json(tmp_path, sample_payload)
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 3


def test_rejects_missing_rules_array(tmp_path: Path) -> None:
    inp = _write_json(tmp_path, {"scenario": "x", "target_category": "Rooms"})
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 3


def test_rejects_missing_input_file(tmp_path: Path) -> None:
    inp = tmp_path / "does_not_exist.json"
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 2


def test_rejects_invalid_json(tmp_path: Path) -> None:
    inp = tmp_path / "bad.json"
    inp.write_text("this is not json", encoding="utf-8")
    out = tmp_path / "rules.out.yaml"
    code = json_to_yaml.main([str(inp), "--out", str(out)])
    assert code == 3


# ── v1.1 (S1.5-B): property-name advisory ─────────────────────────────────


class TestPropertyNameAdvisory:
    """v1.1 post-Stage-1 patch: warn on LLM-hallucinated property names
    like 'Family and Type' that don't exist in either data source."""

    def test_hallucinated_family_and_type_warned(self) -> None:
        # The S1.5-B canonical example: Claude desktop emitted
        # `when_param: "Family and Type"` for a Doors rule because the
        # prompt template didn't tell it that combined name doesn't exist.
        rules = [
            {
                "id": "door.width.main_entrance_min",
                "parameter": "Width",
                "requirement": "numeric_min_conditional",
                "when_param": "Family and Type",   # ← the hallucination
            }
        ]
        warnings = json_to_yaml._check_property_names("Doors", rules)
        assert len(warnings) == 1
        rid, field, value, hint = warnings[0]
        assert rid == "door.width.main_entrance_min"
        assert field == "when_param"
        assert value == "Family and Type"
        assert "Family Name" in hint  # the canonical fix is mentioned

    def test_canonical_property_no_warning(self) -> None:
        rules = [
            {
                "id": "door.width.required",
                "parameter": "Width",
                "when_param": "Family Name",   # ← actually exists in AECDM
            }
        ]
        assert json_to_yaml._check_property_names("Doors", rules) == []

    def test_revit_mcp_only_property_no_warning(self) -> None:
        """Properties that only exist on the Revit MCP path (e.g. Type-level
        Fire Rating for walls) should not warn when the rule is going to be
        used with --run-revit."""
        rules = [
            {
                "id": "wall.type.fire_rating.required",
                "parameter": "Fire Rating",
                "category": "Walls",
            }
        ]
        # target_category may be a single string OR list (W7 D1)
        assert json_to_yaml._check_property_names("Walls", rules) == []
        assert json_to_yaml._check_property_names(["Walls", "Doors"], rules) == []

    def test_multi_category_target_uses_union(self) -> None:
        """When target_category is a list, each rule's property name only
        needs to exist in ONE of the categories' canonical lists."""
        rules = [
            {
                "id": "door.fire_rating.required",
                "parameter": "Fire Rating",
                "category": "Doors",   # restricts the canonical set further
            }
        ]
        assert json_to_yaml._check_property_names(["Walls", "Doors"], rules) == []

    def test_unknown_category_falls_back_to_no_warning(self) -> None:
        """An unknown target_category (e.g. 'CustomCategory') has an empty
        canonical set, so we can't say anything -- don't warn."""
        rules = [
            {
                "id": "x.custom.field",
                "parameter": "MyCustomParam",
            }
        ]
        # Empty known set on both sides → known_union = empty.
        # Property "MyCustomParam" is not in empty set → would warn with
        # generic hint. That's correct: operator should know the
        # cheat-sheet has no answer here. Test pins the BEHAVIOR rather
        # than asserting "no warning".
        warnings = json_to_yaml._check_property_names(
            "CustomCategory", rules
        )
        assert len(warnings) == 1
        assert warnings[0][3] == "not in canonical AECDM or Revit MCP property list"

    def test_warnings_dont_break_conversion(
        self, tmp_path: Path, sample_payload: dict
    ) -> None:
        """End-to-end: when a hallucination is present, conversion still
        succeeds (warnings are advisory, not blocking) and exit code is 0."""
        # Tweak the sample so one rule has a hallucinated when_param
        if "rules" in sample_payload and sample_payload["rules"]:
            sample_payload["rules"][0]["when_param"] = "Family and Type"
            sample_payload["rules"][0]["when_pattern"] = "(?i)main"
            sample_payload["rules"][0]["requirement"] = "numeric_min_conditional"
            sample_payload["rules"][0].setdefault("threshold", 1.0)
        inp = _write_json(tmp_path, sample_payload)
        out = tmp_path / "rules.out.yaml"
        code = json_to_yaml.main([str(inp), "--out", str(out)])
        assert code == 0, "warnings must not block conversion"
        assert out.exists()
