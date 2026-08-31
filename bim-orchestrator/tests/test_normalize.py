"""Tests for the `normalize` autofill strategy + type-targeted Path B (v1.4-K5).

normalize converts a present-but-mis-formatted value to canonical form
("180 MIN" → "3-hour"); for type-level params (Fire Rating) the fix writes the
family TYPE (deduped per type), and — when severity-gated — becomes an approval
proposal.
"""

from __future__ import annotations

import json

import pytest
import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.normalize import (
    auto_candidates,
    normalize_family_name,
    normalize_fire_rating,
    normalize_map,
    normalize_quantity,
    normalize_template,
    normalize_value,
)
from bim_orchestrator.policies.rules_schema import Rule
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
from tests.test_design_agent_path_b import _autonomy, _finding, _ruleset, _state

_PATTERN = r"^(NR|\d+(\.\d+)?-hour)$"


class TestNormalizeFireRatingFormat:
    """v1.4-K11 — output format is configurable (parse→render split), not hard-wired."""

    @pytest.mark.parametrize("val,fmt,expected", [
        ("180 MIN", None, "3-hour"),          # default unchanged (back-compat)
        ("180 MIN", "{h}-hour", "3-hour"),
        ("180 MIN", "{h} HR", "3 HR"),
        ("90 MIN", "{m} MIN", "90 MIN"),       # idempotent in MIN form
        ("2 HR", "{h}-hour", "2-hour"),
        # A-4b (owner decision 2026-08-17): an hour token renders only
        # LOSSLESSLY. Clean fractions round-trip exactly and STAY ("1.5 HR",
        # "0.75 HR" are acceptable labels — Ken); a lossy render like
        # "0.33 HR" (parses back to 19.8 min, not 20) falls back to minutes.
        ("1.5 hr", "{h} HR", "1.5 HR"),
        ("90 minutes", "{h}-hour", "1.5-hour"),
        ("45 MIN", "{h} HR", "0.75 HR"),
        ("20 MIN", "{h} HR", "20 MIN"),        # the review's smoking gun, fixed
        ("120 min", "{h} HR", "2 HR"),         # whole hours untouched
    ])
    def test_render_formats(self, val, fmt, expected):
        assert normalize_value(val, "fire_rating", fmt) == expected

    def test_canonical_output_round_trips_through_its_own_normalizer(self):
        # K13 invariant, extended to the A-4b fallback: the canonical output
        # must normalize to ITSELF, else canonical_format flags its own output.
        for canonical in ("1.5 HR", "0.75 HR", "20 MIN", "2 HR"):
            assert normalize_value(canonical, "fire_rating", "{h} HR") == canonical

    def test_lossy_hour_render_never_escapes(self):
        # The exact drift the review measured: 20 MIN → "0.33 HR" → 19.8 min.
        # No duration input may render an hour string that fails to parse back
        # to its own base minutes.
        from bim_orchestrator.policies.fire_rating_units import parse_to_minutes
        for minutes in (20, 40, 100):  # 1/3 h, 2/3 h, 5/3 h — all lossy
            out = normalize_value(f"{minutes} MIN", "fire_rating", "{h} HR")
            assert out == f"{minutes} MIN"
            assert parse_to_minutes(out) == minutes

    def test_non_duration_still_none(self):
        assert normalize_value("NR", "fire_rating", "{h} HR") is None


class TestNormalizeFamilyName:
    """v1.4-K9 — deterministic family/type name separator normalization."""

    @pytest.mark.parametrize("raw,expected", [
        ("ADSK Fur Chair Viper", "ADSK_Fur_Chair_Viper"),   # spaces
        ("ADSK-Dr-Single-Flush", "ADSK_Dr_Single_Flush"),   # hyphens
        ("ADSK_Dr__Single_Flush", "ADSK_Dr_Single_Flush"),  # double underscore
        ("  ADSK_Dr_Single  ", "ADSK_Dr_Single"),           # stray whitespace
        ("ADSK Dr - Single", "ADSK_Dr_Single"),             # mixed separators
    ])
    def test_format_violations_fixed(self, raw, expected):
        assert normalize_family_name(raw) == expected

    @pytest.mark.parametrize("raw", [
        "ADSK_Dr_Single_Flush",  # already conforming → nothing to fix
        "Door1",                  # semantic, no separators → can't deterministically fix
        "SingleFlush",
    ])
    def test_no_deterministic_fix_returns_none(self, raw):
        assert normalize_family_name(raw) is None

    def test_non_string_returns_none(self):
        assert normalize_family_name(None) is None
        assert normalize_family_name(42) is None

    def test_dispatch_via_normalize_value(self):
        assert normalize_value("ADSK Dr Single", "family_name") == "ADSK_Dr_Single"


class TestNormalizeRegexGuard:
    """v1.4-K9 — QC must not PROPOSE a normalized value that still violates the
    rule's matches_regex pattern (else a rename would land a still-wrong name)."""

    def _qc(self, tmp_path):
        p = tmp_path / "naming.yaml"
        p.write_text(yaml.safe_dump({
            "scenario": "n", "target_category": "Doors", "rules": [{
                "id": "d.name", "parameter": "Family Name",
                "requirement": "matches_regex",
                "pattern": r"^ADSK_Dr_[A-Za-z0-9]+(_[A-Za-z0-9]+)*$",
                "category": "Doors", "severity_tag": "naming_violation",
                "description": "x", "fixability": "auto",
                "autofill": {"strategy": "normalize", "normalize_kind": "family_name"},
                "remediation": {"action": "rename_element", "target": "type"},
            }]}))
        return QCAgent(rules_path=p, autonomy=_autonomy(tmp_path))

    def _suggest_for(self, qc, family_name):
        rule = qc.rules.rules[0]
        return qc._suggest(rule, {"id": "1", "params": {"Family Name": family_name}})

    def test_conforming_format_fix_is_suggested(self, tmp_path):
        # "ADSK Dr Single Flush" → "ADSK_Dr_Single_Flush" matches → propose it
        assert self._suggest_for(self._qc(tmp_path), "ADSK Dr Single Flush") == \
            "ADSK_Dr_Single_Flush"

    def test_still_nonconforming_is_not_suggested(self, tmp_path):
        # "Door 1" → "Door_1" still fails the ADSK_Dr_ pattern → None (→ Path A)
        assert self._suggest_for(self._qc(tmp_path), "Door 1") is None


# ---- pure normalizer -------------------------------------------------------

class TestNormalizeFn:
    @pytest.mark.parametrize("raw,expected", [
        ("180 MIN", "3-hour"),
        ("90 MIN", "1.5-hour"),
        ("2 HR", "2-hour"),
        ("2 hours", "2-hour"),
        ("1.5 hr", "1.5-hour"),
        ("120 minutes", "2-hour"),
        ("20 MIN", "20 MIN"),      # A-4b: lossy hour render falls back to minutes
    ])
    def test_durations(self, raw, expected):
        assert normalize_fire_rating(raw) == expected

    @pytest.mark.parametrize("raw", ["NR", "rated", "", None, "abc"])
    def test_non_durations_none(self, raw):
        assert normalize_fire_rating(raw) is None

    def test_canonical_form_round_trips(self):
        # v1.4-K13: the canonical output must parse back to itself, else
        # canonical_format would flag its OWN output as non-compliant. The old
        # regex couldn't parse "3-hour" (hyphen separator) — now it can.
        assert normalize_fire_rating("3-hour", "{h}-hour") == "3-hour"
        assert normalize_fire_rating("180 Min", "{m} Min") == "180 Min"

    def test_dispatch(self):
        assert normalize_value("180 MIN", "fire_rating") == "3-hour"
        assert normalize_value("180 MIN", "unknown_kind") is None


class TestUnitRegistry:
    """v1.4-K13 — normalize generalised to a unit registry (not just time)."""

    def test_duration_output_unit_chosen_by_format(self):
        # the user's exact case: "X Min" canonical, fed any time spelling
        assert normalize_quantity("3 HR", "duration", "{m} Min") == "180 Min"
        assert normalize_quantity("180 MIN", "duration", "{m} Min") == "180 Min"
        assert normalize_quantity("1.5 hr", "duration", "{h} HR") == "1.5 HR"

    @pytest.mark.parametrize("raw,fmt,expected", [
        ("2.4 m", "{mm} mm", "2400 mm"),
        ("2400mm", "{m} m", "2.4 m"),
        ("240 cm", "{mm} mm", "2400 mm"),
        ("2400 mm", "{cm} cm", "240 cm"),
    ])
    def test_length_unit_conversion(self, raw, fmt, expected):
        assert normalize_quantity(raw, "length", fmt) == expected

    def test_area_unit_conversion(self):
        assert normalize_quantity("10 m²", "area", "{m2} m²") == "10 m²"
        # 120 sf ≈ 11.15 m²
        assert normalize_quantity("120 sf", "area", "{m2} m²") == "11.15 m²"

    @pytest.mark.parametrize("raw", ["NR", "", None, "abc", "5"])
    def test_unparseable_or_missing_unit_none(self, raw):
        assert normalize_quantity(raw, "length", "{mm} mm") is None

    def test_unknown_dimension_or_token_none(self):
        assert normalize_quantity("5 m", "mass", "{kg} kg") is None       # no such dim
        assert normalize_quantity("5 m", "length", "{ly} ly") is None     # no such token


class TestAutoCandidates:
    """v1.4-K16 — auto mode: engine tries every deterministic canonicaliser; the
    caller keeps whichever output satisfies the rule's pattern."""

    def test_duration_value_yields_hr_and_min_forms(self):
        cands = auto_candidates("180 MIN")
        assert "3 HR" in cands and "3-hour" in cands and "180 Min" in cands

    def test_length_value_yields_unit_forms(self):
        cands = auto_candidates("2.4 m")
        assert "2400 mm" in cands and "240 cm" in cands

    def test_includes_separator_fix(self):
        assert "ADSK_Fur_Chair" in auto_candidates("ADSK-Fur-Chair")

    def test_unparseable_value_few_or_no_candidates(self):
        # "NR" is no quantity and has no separator to fix → nothing useful
        assert "3 HR" not in auto_candidates("NR")

    def test_deduped_and_ordered(self):
        cands = auto_candidates("180 MIN")
        assert len(cands) == len(set(cands))            # no dupes
        assert cands.index("3 HR") < cands.index("180 Min")  # dimension order


class TestNormalizeTemplate:
    """v1.4-K15 — general deterministic naming transform (regex capture → template).
    The deterministic ceiling: restructures a name that CONTAINS the tokens."""

    _SRC = r"(?i)^adsk[ _-]*fur[ _-]*(?P<fn>[a-z]+)[ _-]*(?P<d1>[a-z0-9]+)"
    _FMT = "ADSK_Fur_{fn}_{d1}"

    @pytest.mark.parametrize("raw", [
        "ADSK Fur Chair Viper", "ADSK-Fur-Chair-Viper", "ADSK_Fur_Chair_Viper",
    ])
    def test_restructures_to_canonical(self, raw):
        assert normalize_template(raw, self._SRC, self._FMT) == "ADSK_Fur_Chair_Viper"

    def test_preserves_token_casing_verbatim(self):
        # honest deterministic limit: captured tokens keep their case (the literal
        # ADSK_Fur in the template is fixed, but {fn}/{d1} are passed through).
        assert normalize_template("adsk fur chair viper", self._SRC, self._FMT) \
            == "ADSK_Fur_chair_viper"

    @pytest.mark.parametrize("raw,expected", [
        # v1.4-K15: multi-word captured tokens are slugged (inner space/hyphen → _)
        ("M_Table-Dining Round w Chairs", "ADSK_Fur_Table_Dining_Round_w_Chairs"),
        ("Table-Night Stand",             "ADSK_Fur_Table_Night_Stand"),
        ("Chair-Viper",                   "ADSK_Fur_Chair_Viper"),
    ])
    def test_slugs_multiword_tokens(self, raw, expected):
        src = r"^(?:M_)?(?P<fn>[A-Za-z]+)(?:[-_ ](?P<d1>.+))?$"
        assert normalize_template(raw, src, "ADSK_Fur_{fn}_{d1}") == expected

    @pytest.mark.parametrize("raw", ["M_Desk", "Wastebasket2"])
    def test_missing_description_still_path_a(self, raw):
        # no Des1 token in the name → can't invent → None → Path A (same regex as
        # the 8-name demo: optional d1, so M_Desk parses fn=Desk with d1 absent).
        src = r"^(?:M_)?(?P<fn>[A-Za-z]+)(?:[-_ ](?P<d1>.+))?$"
        assert normalize_template(raw, src, "ADSK_Fur_{fn}_{d1}") is None

    def test_round_trips_its_own_output(self):
        out = normalize_template("ADSK_Fur_Chair_Viper", self._SRC, self._FMT)
        assert out == "ADSK_Fur_Chair_Viper"

    @pytest.mark.parametrize("raw", ["Chair", "", None, "random text"])
    def test_missing_tokens_none(self, raw):
        # cannot invent the missing token → escalate to Path A
        assert normalize_template(raw, self._SRC, self._FMT) is None

    def test_numbered_groups(self):
        assert normalize_template("Chair-Wood", r"^(\w+)-(\w+)$", "ADSK_Fur_{g1}_{g2}") \
            == "ADSK_Fur_Chair_Wood"

    def test_template_referencing_uncaptured_group_none(self):
        # fmt wants {d2} but source has no such group → None, not a crash
        assert normalize_template("Chair", r"^(?P<fn>\w+)$", "X_{fn}_{d2}") is None

    def test_bad_regex_none(self):
        assert normalize_template("x", r"(?P<unclosed", "{x}") is None

    def test_via_dispatch(self):
        assert normalize_value("ADSK Fur Chair Viper", "template",
                               self._FMT, source=self._SRC) == "ADSK_Fur_Chair_Viper"


class TestNormalizeMap:
    """v1.4-K13 — fixed/enumerated text canonicalisation."""

    _MAP = {"nr": "Not Rated", "n/r": "Not Rated", "0": "Not Rated", "none": "Not Rated"}

    @pytest.mark.parametrize("raw,expected", [
        ("NR", "Not Rated"), ("n/r", "Not Rated"), (" 0 ", "Not Rated"),
        ("Not Rated", "Not Rated"),   # already canonical → maps to itself
        ("NOT RATED", "Not Rated"),   # case-insensitive
    ])
    def test_hits(self, raw, expected):
        assert normalize_map(raw, self._MAP) == expected

    @pytest.mark.parametrize("raw", ["unknown", "", None])
    def test_miss_none(self, raw):
        assert normalize_map(raw, self._MAP) is None

    def test_empty_mapping_none(self):
        assert normalize_map("NR", None) is None
        assert normalize_value("NR", "map", mapping={}) is None

    def test_uppercase_map_keys_still_match(self):
        # Medium: case-insensitive on the MAP KEY too, not only the input value.
        # {"NR": ...} previously missed "nr"/"NR" because only the value was lowered.
        m = {"NR": "Not Rated", "1 HR": "1-hour"}
        assert normalize_map("nr", m) == "Not Rated"
        assert normalize_map("NR", m) == "Not Rated"
        assert normalize_map("1 hr", m) == "1-hour"


# ---- QC computes the canonical suggested_value -----------------------------

def _qc_autonomy(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text(yaml.safe_dump({
        "mutations": {"parameters": {"set_value": {"severity_high": "approve"}}},
        "severity_rules": {"fire_safety_change": "severity_high"},
    }))
    return AutonomyPolicy.load(cfg)


def _qc_rules(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump({
        "scenario": "fr", "target_category": "Doors",
        "rules": [{
            "id": "doors.fr.format", "parameter": "Fire Rating",
            "requirement": "matches_regex_if_present", "pattern": _PATTERN, "category": "Doors",
            "severity_tag": "fire_safety_change", "description": "canonical fire rating",
            "fixability": "auto",
            "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating"},
            "remediation": {"action": "set_parameter", "target_parameter": "Fire Rating",
                            "new_value_strategy": "inferred", "target": "type"},
        }],
    }))
    return p


class TestQcNormalize:
    def test_misformatted_value_gets_canonical_suggestion(self, tmp_path):
        agent = QCAgent(_qc_rules(tmp_path), _qc_autonomy(tmp_path))
        state = {  # type: ignore[var-annotated]
            "project_id": "t", "iteration": 0, "max_iterations": 1,
            "elements": [{"id": "d1", "category": "Doors",
                          "params": {"Fire Rating": "2 HR", "_type_id": "999"}}],
            "findings": [], "proposed_fixes": [], "status": "checking", "error": None,
        }
        out = agent.run(state)  # type: ignore[arg-type]
        assert len(out["findings"]) == 1
        assert out["findings"][0]["status"] == "non_compliant"
        assert out["findings"][0]["suggested_value"] == "2-hour"

    def test_canonical_value_is_compliant(self, tmp_path):
        agent = QCAgent(_qc_rules(tmp_path), _qc_autonomy(tmp_path))
        state = {  # type: ignore[var-annotated]
            "project_id": "t", "iteration": 0, "max_iterations": 1,
            "elements": [{"id": "d1", "category": "Doors",
                          "params": {"Fire Rating": "NR", "_type_id": "999"}}],
            "findings": [], "proposed_fixes": [], "status": "checking", "error": None,
        }
        out = agent.run(state)  # type: ignore[arg-type]
        assert out["findings"] == []
        assert out["outcomes_summary"]["compliant"] == 1

    def test_missing_value_is_compliant_not_path_a(self, tmp_path):
        # blank Fire Rating must NOT be flagged (matches_regex_if_present) →
        # no missing_data spam → no Path A issue flood.
        agent = QCAgent(_qc_rules(tmp_path), _qc_autonomy(tmp_path))
        state = {  # type: ignore[var-annotated]
            "project_id": "t", "iteration": 0, "max_iterations": 1,
            "elements": [{"id": "d1", "category": "Doors",
                          "params": {"Fire Rating": None, "_type_id": "999"}}],
            "findings": [], "proposed_fixes": [], "status": "checking", "error": None,
        }
        out = agent.run(state)  # type: ignore[arg-type]
        assert out["findings"] == []
        assert out["missing_data_items"] == []
        assert out["outcomes_summary"]["compliant"] == 1


# ---- DesignAgent: type-targeted write + dedup + proposal record ------------

def _fr_rule() -> Rule:
    return Rule.model_validate({
        "id": "doors.fr.format", "parameter": "Fire Rating",
        "requirement": "matches_regex", "pattern": _PATTERN,
        "severity_tag": "fire_safety_change", "description": "canonical fire rating",
        "fixability": "auto",
        "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating"},
        "remediation": {"action": "set_parameter", "target_parameter": "Fire Rating",
                        "new_value_strategy": "inferred", "target": "type"},
    })


@pytest.mark.asyncio
class TestTypeTargetedProposal:
    async def test_dedups_by_type_and_records_type_target(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient()
        # type 999 must exist for the preview write
        revit.element_info[999] = {
            "id": 999, "name": "Door Type 36x84",
            "parameters": [{"name": "Fire Rating", "value": "2 HR", "valueString": "2 HR"}],
        }
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="p1", max_issues=10, rule_filter=None,
            revit_mcp=revit, rules=_ruleset(_fr_rule()), approvals_dir=approvals,
        )
        # Two door instances of the SAME type 999, both mis-formatted.
        findings = [
            _finding(rule_id="doors.fr.format", element_id="d1", parameter="Fire Rating",
                     severity="severity_high", severity_tag="fire_safety_change",
                     suggested="2-hour"),
            _finding(rule_id="doors.fr.format", element_id="d2", parameter="Fire Rating",
                     severity="severity_high", severity_tag="fire_safety_change",
                     suggested="2-hour"),
        ]
        elements = [
            {"id": "d1", "category": "Doors", "params": {"Fire Rating": "2 HR", "_type_id": "999"}},
            {"id": "d2", "category": "Doors", "params": {"Fire Rating": "2 HR", "_type_id": "999"}},
        ]
        state = await agent.run(_state(findings, elements))
        # Dedup: one fix for the shared type (not two for the instances)
        assert len(state["proposed_fixes"]) == 1
        fix = state["proposed_fixes"][0]
        assert fix["autonomy"] == "approve" and fix["executed"] is False
        # No commit (parked); preview targeted the TYPE 999
        assert [c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is False] == []
        prev = [c for c in revit.calls_to("revit_set_parameter") if c["id"] == 999]
        assert prev and prev[0]["value"] == "2-hour"
        # Proposal record writes the TYPE id, not the instance
        rec = json.loads(next(approvals.glob("*.json")).read_text(encoding="utf-8"))
        assert len(rec["fixes"]) == 1
        assert rec["fixes"][0]["element_id"] == "999"
        assert rec["fixes"][0]["new_value"] == "2-hour"
        assert rec["fixes"][0]["flagged_instance"] == "d1"


# ── numbered template fields ────────────────────────────────────────────────
#
# "{g1}" is this module's spelling for the first numbered capture group, and
# nobody outside it would guess that. An author -- or the Rule Builder's model
# -- writes "{1}". str.format_map parses "{1}" as a POSITIONAL field and raises
# ValueError, which normalize_template swallowed into None: the rule's YAML
# looked perfect (fixability: auto, normalize_kind: template, remediation
# rename_element) and it proposed NOTHING, silently. Measured 2026-08-25: the
# Rule Builder emitted "{1}" on one of three drafts of the SAME sentence, so
# this was a coin-flip failure, not a corner case.
_PREFIX_SOURCE = r"(?i)^(?:ADSK_)?(.+)$"


def test_numbered_field_is_accepted_like_the_internal_spelling():
    assert (
        normalize_template("Chair-Viper", _PREFIX_SOURCE, "ADSK_{1}")
        == normalize_template("Chair-Viper", _PREFIX_SOURCE, "ADSK_{g1}")
        == "ADSK_Chair_Viper"
    )


def test_numbered_field_slugs_its_capture_like_a_named_one():
    assert (
        normalize_template(
            "Table-Dining Round w Chairs", _PREFIX_SOURCE, "ADSK_{1}"
        )
        == "ADSK_Table_Dining_Round_w_Chairs"
    )


def test_numbered_field_leaves_an_already_canonical_value_alone():
    # canonical_format compares against this output, so a compliant family name
    # must round-trip unchanged or every compliant element turns into a finding.
    assert normalize_template("ADSK_Desk", _PREFIX_SOURCE, "ADSK_{1}") == "ADSK_Desk"


def test_group_zero_is_still_not_addressable():
    # Only capture groups are exposed; "{0}" (the whole match) has no mapping
    # and must fail closed to None rather than silently render something.
    assert normalize_template("Chair-Viper", _PREFIX_SOURCE, "ADSK_{0}") is None
