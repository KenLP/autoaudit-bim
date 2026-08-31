"""Phase 3b M2-A (B16) — bim_orchestrator.rule_builder_core.

Most of this module needs no streamlit at all (validate_rule / grounding_block /
draft_rule / enforce_* are pure functions over policies/ data) — those tests
import the package normally. The ONE exception is the S1 golden test, which
exercises the Streamlit save PATH end-to-end (``app._save_rule_to_yaml`` ->
``rule_builder_core.enforce_reference_membership`` -> YAML) to pin that B16
(moving the Rule Builder's NL-extraction/validation/enforcement logic out of
streamlit_app/app.py into this module) did not change what gets written to
disk. It reuses the same streamlit-stub fixture as tests/test_streamlit_argv.py.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from bim_orchestrator import rule_builder_core as rbc

REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_APP_DIR = REPO_ROOT / "streamlit_app"


# ── S1 golden (Streamlit save path, unchanged through B16) ─────────────────


class _MockStreamlit:
    """Same stub as tests/test_streamlit_argv.py — see that file for rationale."""

    def __init__(self) -> None:
        self.session_state: dict = {}

    def columns(self, spec, **kwargs):
        n = spec if isinstance(spec, int) else len(spec)
        return tuple(_Proxy() for _ in range(n))

    def tabs(self, names, **kwargs):
        return [_Proxy() for _ in names]

    def number_input(self, label, *args, **kwargs):
        return kwargs.get("value", 0)

    def __getattr__(self, name):
        return _Proxy()


class _Proxy:
    def __call__(self, *args, **kwargs):
        return _Proxy()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter([_Proxy() for _ in range(8)])

    def __len__(self):
        return 8

    def __getitem__(self, key):
        return _Proxy()

    def __bool__(self):
        return False

    def __getattr__(self, name):
        return _Proxy()


@pytest.fixture
def app_module():
    """Import streamlit_app/app.py with a stubbed streamlit module."""
    fake_st = _MockStreamlit()
    sys.modules["streamlit"] = fake_st  # type: ignore[assignment]

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules.setdefault("dotenv", fake_dotenv)

    sys.path.insert(0, str(STREAMLIT_APP_DIR))
    import importlib

    if "app" in sys.modules:
        del sys.modules["app"]
    return importlib.import_module("app")


# Captured 2026-07-12 from the PRE-B16 Streamlit save path (app._save_rule_to_yaml
# -> app._enforce_reference_membership -> extraction-skills json_to_yaml writer) —
# see SPEC_3B_M2_RULE_BUILDER_NOW.md S1. A canonical_format+reference rule is the
# representative case the spec names (exercises both the NL-extraction schema
# shape AND the enforce_reference_membership guard in one save).
_GOLDEN_S1_YAML = """\
scenario: m2_golden_s1_capture
target_category: Furniture
rules:
- id: furniture.material.approved
  parameter: Material
  requirement: canonical_format
  category: Furniture
  severity_tag: rule_violation
  severity_level: severity_medium
  description: material must be from approved palette
  autofill:
    strategy: normalize
    normalize_kind: reference
    normalize_reference: m2_golden_materials
  citation:
    mode: soft
    on_missing: warn
  fixability: auto
  remediation:
    action: set_parameter
    new_value_strategy: inferred
    target: auto
    llm_safety_critical: false
  requires_human: false
  extraction_meta:
    confidence: 1.0
    source_text: material must be from approved palette
    source_location: Rule Builder (UI)
    extracted_by: ''
    extracted_at: ''
    execution_status: executable
geometry_rules: []
"""


class TestGoldenS1:
    """S1 (SPEC_3B_M2_RULE_BUILDER_NOW.md): the Rule Builder Streamlit save path
    for a canonical_format+reference rule must keep producing byte-identical
    YAML across the B16 rule_builder_core extraction."""

    def test_canonical_format_reference_save_is_byte_identical(self, app_module, tmp_path):
        rule = {
            "id": "furniture.material.approved", "category": "Furniture", "parameter": "Material",
            "requirement": "canonical_format",
            "description": "material must be from approved palette",
            "severity_level": "severity_medium", "fixability": "auto",
            "autofill": {"strategy": "normalize", "normalize_kind": "reference",
                         "normalize_reference": "m2_golden_materials"},
            "remediation": {"action": "set_parameter", "target": "auto"},
        }
        scenario = "m2_golden_s1_capture"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            assert out.read_text(encoding="utf-8") == _GOLDEN_S1_YAML
        finally:
            out.unlink(missing_ok=True)


# ── validate_rule ────────────────────────────────────────────────────────


class TestValidateRuleParameter:
    def test_empty_id_and_parameter(self):
        result = rbc.validate_rule({}, is_geometry=False)
        assert result.ok is False
        fields = {e.field for e in result.errors}
        assert {"id", "parameter"} <= fields

    def test_readonly_param_blocks_autofix_write_target(self):
        # Walls / Width is a read-only built-in in the param catalog (per
        # param_catalog.py's own docstring example) — an "auto" fixability
        # rule that targets it as a set_parameter write must be rejected.
        result = rbc.validate_rule(
            {
                "id": "walls.width.readonly", "category": "Walls", "parameter": "Width",
                "fixability": "auto",
                "remediation": {"action": "set_parameter", "target": "instance"},
            },
            is_geometry=False,
        )
        assert result.ok is False
        msg = " ".join(e.message for e in result.errors)
        assert "read-only" in msg

    def test_relation_compare_needs_source(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "relation_compare"},
            is_geometry=False,
        )
        assert result.ok is False
        assert any(e.field == "other_param" for e in result.errors)

    def test_relation_compare_with_lookup_is_fine(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "relation_compare", "lookup": "ibc716"},
            is_geometry=False,
        )
        assert not any(e.field == "other_param" for e in result.errors)

    def test_legacy_numeric_min_needs_threshold(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_min"},
            is_geometry=False,
        )
        assert any(e.field == "threshold" for e in result.errors)

    def test_pattern_requirement_needs_pattern(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "matches_regex"},
            is_geometry=False,
        )
        assert any(e.field == "pattern" for e in result.errors)

    def test_matches_regex_if_present_needs_pattern(self):
        # The "skip if empty" variant was missing from the guard. With an empty
        # pattern it fullmatches against "", so every NON-empty value fails —
        # an always-fail noise flood, saved without a murmur.
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "matches_regex_if_present"},
            is_geometry=False,
        )
        assert any(e.field == "pattern" for e in result.errors)

    def test_numeric_compare_needs_a_threshold(self):
        # The OFFERED numeric requirement: the legacy `numeric_min` guard never
        # covered it, so a compare with no limit at all saved clean.
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_compare",
             "operator": ">="},
            is_geometry=False,
        )
        assert result.ok is False
        assert any(e.field == "threshold" for e in result.errors)

    def test_numeric_compare_ge_zero_is_rejected(self):
        # `>= 0` passes for every non-negative value: an unfinished
        # "width must be at least 900 mm" that reports 100% compliance.
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_compare",
             "operator": ">=", "threshold": 0},
            is_geometry=False,
        )
        assert result.ok is False
        assert any(e.field == "threshold" for e in result.errors)

    def test_numeric_compare_gt_zero_is_allowed(self):
        # ...but `> 0` REJECTS 0 — it is the legitimate "must be positive"
        # check that `positive_number` migrates to (config/rules.va_bim.yaml).
        # Blocking it would have made 4 shipped rules unsaveable.
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_compare",
             "operator": ">", "threshold": 0},
            is_geometry=False,
        )
        assert not any(e.field == "threshold" for e in result.errors)

    def test_numeric_compare_string_zero_is_rejected(self):
        # M6: a scripted PUT can send the STRING "0" (truthy), which slipped past
        # the `not threshold` `>= 0` block and was then coerced to 0.0 and saved
        # as "width >= 0". Coercing before the test blocks it identically to 0.
        for bad in ("0", "0.0", " 0 "):
            result = rbc.validate_rule(
                {"id": "x", "parameter": "P", "requirement": "numeric_compare",
                 "operator": ">=", "threshold": bad},
                is_geometry=False,
            )
            assert result.ok is False, bad
            assert any(e.field == "threshold" for e in result.errors), bad

    def test_numeric_compare_non_numeric_string_reports_number_error(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_compare",
             "operator": ">=", "threshold": "abc"},
            is_geometry=False,
        )
        assert result.ok is False
        assert any(e.field == "threshold" and "number" in e.message.lower()
                   for e in result.errors)
        # exactly one threshold error, not the doubled "must be a number" +
        # "must declare a Threshold"
        assert sum(1 for e in result.errors if e.field == "threshold") == 1

    def test_numeric_compare_string_positive_is_allowed(self):
        # "> 0" as a string is still the valid positive check.
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_compare",
             "operator": ">", "threshold": "0"},
            is_geometry=False,
        )
        assert not any(e.field == "threshold" for e in result.errors)

    def test_numeric_compare_eq_zero_warns_but_saves(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "numeric_compare",
             "operator": "==", "threshold": 0},
            is_geometry=False,
        )
        assert not any(e.field == "threshold" for e in result.errors)
        assert result.warnings          # the plumbing is no longer always-empty

    def test_uncompilable_pattern_is_rejected_at_save(self):
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "matches_regex",
             "pattern": "["},
            is_geometry=False,
        )
        assert result.ok is False
        assert any(e.field == "pattern" for e in result.errors)

    def test_uncompilable_scope_filter_pattern_is_rejected_at_save(self):
        # The worst of the two: a bad SCOPE regex surfaces nowhere at run time.
        # `_passes_scope_filter` fails closed, so every element is silently
        # skipped, the rule evaluates nothing, and the run converges "clean".
        result = rbc.validate_rule(
            {"id": "x", "parameter": "P", "requirement": "present_and_nonempty",
             "scope_filter": {"param": "Function", "pattern": "(unclosed"}},
            is_geometry=False,
        )
        assert result.ok is False
        assert any(e.field == "scope_filter.pattern" for e in result.errors)

    def test_normalize_template_needs_source_and_format(self):
        result = rbc.validate_rule(
            {
                "id": "x", "parameter": "P",
                "autofill": {"strategy": "normalize", "normalize_kind": "template"},
            },
            is_geometry=False,
        )
        assert any(e.field == "autofill.normalize_source" for e in result.errors)

    def test_normalize_map_needs_entries(self):
        result = rbc.validate_rule(
            {
                "id": "x", "parameter": "P",
                "autofill": {"strategy": "normalize", "normalize_kind": "map"},
            },
            is_geometry=False,
        )
        assert any(e.field == "autofill.normalize_map" for e in result.errors)

    def test_normalize_auto_needs_pattern(self):
        result = rbc.validate_rule(
            {
                "id": "x", "parameter": "P",
                "autofill": {"strategy": "normalize", "normalize_kind": "auto"},
            },
            is_geometry=False,
        )
        assert any(e.field == "pattern" for e in result.errors)

    def test_fully_specified_rule_is_ok(self):
        result = rbc.validate_rule(
            {
                "id": "doors.mark.present", "category": "Doors", "parameter": "Mark",
                "requirement": "present_and_nonempty", "fixability": "manual",
                "remediation": {"action": "create_acc_issue"},
            },
            is_geometry=False,
        )
        assert result.ok is True
        assert result.errors == []


class TestValidateRuleGeometry:
    def test_valid_geometry_rule_ok(self):
        result = rbc.validate_rule(
            {
                "id": "ducts.parking.clearance", "category": "Ducts",
                "check_type": "clearance_min", "description": "clearance check",
                "threshold_mm": 2400.0,
            },
            is_geometry=True,
        )
        assert result.ok is True

    def test_invalid_geometry_rule_reports_field_errors(self):
        result = rbc.validate_rule({}, is_geometry=True)
        assert result.ok is False
        assert result.errors  # pydantic ValidationError -> at least one field


# ── grounding_block ──────────────────────────────────────────────────────


class TestGroundingBlock:
    def test_lists_categories_including_stairs(self):
        block = rbc.grounding_block(False)
        assert "Stairs" in block and "Stair Runs" in block and "Ramps" in block

    def test_catalog_grounded_lists_params_and_intent_aliases(self):
        block = rbc.grounding_block(True)
        assert "Actual Run Width" in block and "Fire Rating" in block
        assert "Unbounded Height" in block and "ceiling height" in block

    def test_lists_lookup_tables_and_shared_params(self):
        block = rbc.grounding_block(False)
        assert "AVAILABLE LOOKUP TABLES" in block
        assert "SHARED / openBIM PARAMETERS" in block


# ── draft_rule ───────────────────────────────────────────────────────────


class TestDraftRule:
    def test_missing_api_key_raises_not_configured_silently(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("BIM_LLM_PROVIDER", raising=False)
        with pytest.raises(rbc.LLMNotConfiguredError) as excinfo:
            rbc.draft_rule("Doors must have a Mark value")
        assert excinfo.value.silent is True


# ── apply_nl_intents (Layer 2 deterministic belt, v1.7-3bP7) ──────────────


def _dur_rule(strategy="normalize", fmt="{h}-hour", kind="duration"):
    return {
        "requirement": "canonical_format",
        "parameter": "Fire Rating",
        "autofill": {
            "strategy": strategy,
            "normalize_kind": kind,
            "normalize_format": fmt,
        },
    }


# NL whose only quoted literal is 'X HR' AND which has both empty+inherit clauses.
_NL_COMPOUND = (
    "A door's Fire Rating must be recorded as 'X HR'; when empty, inherit the "
    "fire rating of its host wall and normalise that to the same canonical form."
)


class TestApplyNlIntentsFormat:
    """(2a) literal-format fidelity — gap 1."""

    def test_x_hr_overwrites_invented_hour(self):
        out = rbc.apply_nl_intents("must be recorded as 'X HR'.", _dur_rule(fmt="{h}-hour"))
        assert out["autofill"]["normalize_format"] == "{h} HR"

    def test_x_hour_preserved_when_draft_faithful(self):
        # NL literally asks for 'X-hour'; a faithful {h}-hour draft is NOT rewritten.
        out = rbc.apply_nl_intents("must read 'X-hour'.", _dur_rule(fmt="{h}-hour"))
        assert out["autofill"]["normalize_format"] == "{h}-hour"

    def test_x_min_maps_to_minute_token(self):
        out = rbc.apply_nl_intents("must be 'X Min'.", _dur_rule(fmt="{h}-hour"))
        assert out["autofill"]["normalize_format"] == "{m} Min"

    def test_no_quoted_literal_untouched(self):
        out = rbc.apply_nl_intents("Fire rating must be normalised.", _dur_rule(fmt="{h}-hour"))
        assert out["autofill"]["normalize_format"] == "{h}-hour"

    def test_two_different_literals_ambiguous_untouched(self):
        out = rbc.apply_nl_intents("must be 'X HR' or 'X Min'.", _dur_rule(fmt="{h}-hour"))
        assert out["autofill"]["normalize_format"] == "{h}-hour"

    def test_same_derived_form_twice_still_applies(self):
        # 'X HR' and '2 HR' are different strings but both derive {h} HR — not ambiguous.
        out = rbc.apply_nl_intents(
            "match 'X HR' (e.g. '2 HR').", _dur_rule(strategy="normalize", fmt="{h}-hour")
        )
        assert out["autofill"]["normalize_format"] == "{h} HR"

    def test_non_duration_kind_untouched(self):
        out = rbc.apply_nl_intents(
            "must be 'X HR'.", _dur_rule(fmt="{h}-hour", kind="reference")
        )
        assert out["autofill"]["normalize_format"] == "{h}-hour"


class TestApplyNlIntentsInherit:
    """(2b) empty→inherit upgrade — gap 2."""

    def test_both_clauses_upgrade(self):
        out = rbc.apply_nl_intents(_NL_COMPOUND, _dur_rule(strategy="normalize"))
        assert out["autofill"]["strategy"] == "inherit_then_normalize"
        # other autofill keys intact
        assert out["autofill"]["normalize_kind"] == "duration"

    def test_empty_clause_only_untouched(self):
        out = rbc.apply_nl_intents("when empty do nothing special", _dur_rule())
        assert out["autofill"]["strategy"] == "normalize"

    def test_inherit_clause_only_untouched(self):
        out = rbc.apply_nl_intents("inherit the host wall rating", _dur_rule())
        assert out["autofill"]["strategy"] == "normalize"

    def test_inherit_from_host_never_touched(self):
        rule = {"autofill": {"strategy": "inherit_from_host"}}
        assert rbc.apply_nl_intents(_NL_COMPOUND, rule) is rule


def _template_rule(source, fmt="ADSK_{rest}"):
    return {
        "requirement": "canonical_format",
        "parameter": "Family Name",
        "autofill": {
            "strategy": "normalize",
            "normalize_kind": "template",
            "normalize_source": source,
            "normalize_format": fmt,
        },
    }


class TestApplyNlIntentsPrefixTolerance:
    """(2c) template prefix-strip tolerance.

    The model writes the strip group as the literal canonical prefix
    ("(?:ADSK_)?"), which strips only that exact spelling — every other
    spelling gets the prefix stacked: "ADSK Fur Chair Viper" came back as
    "ADSK_ADSK_Fur_Chair_Viper" in the Rule Builder preview (2026-08-25).
    One draft in three emitted the strict form, so like "{1}" this is a
    coin flip, not a corner case.
    """

    def test_strict_group_is_widened(self):
        out = rbc.apply_nl_intents(
            "names must start with ADSK_",
            _template_rule(r"(?i)^(?:ADSK_)?(?P<rest>.+)$"),
        )
        assert (
            out["autofill"]["normalize_source"]
            == r"(?i)^(?:ADSK[ _-]+)?(?P<rest>.+)$"
        )

    def test_widened_source_fixes_the_observed_double_prefix(self):
        # End to end through the real engine: the exact preview value Ken saw.
        from bim_orchestrator.policies.normalize import normalize_template

        out = rbc.apply_nl_intents(
            "names must start with ADSK_",
            _template_rule(r"(?i)^(?:ADSK_)?(?P<rest>.+)$"),
        )
        af = out["autofill"]
        got = normalize_template(
            "ADSK Fur Chair Viper", af["normalize_source"], af["normalize_format"]
        )
        assert got == "ADSK_Fur_Chair_Viper"  # was ADSK_ADSK_Fur_Chair_Viper

    def test_already_tolerant_source_is_untouched_and_idempotent(self):
        tolerant = r"(?i)^(?:adsk[ _-]*)?(?P<rest>.+)$"
        once = rbc.apply_nl_intents("x", _template_rule(tolerant))
        assert once["autofill"]["normalize_source"] == tolerant
        twice = rbc.apply_nl_intents("x", once)
        assert twice == once

    def test_source_casing_is_preserved_not_the_formats(self):
        # A lowercase group without (?i): substituting the format's casing
        # would silently stop the strip matching lowercase values.
        out = rbc.apply_nl_intents(
            "x", _template_rule(r"^(?:adsk_)?(?P<rest>.+)$")
        )
        assert (
            out["autofill"]["normalize_source"] == r"^(?:adsk[ _-]+)?(?P<rest>.+)$"
        )

    def test_multi_word_prefix(self):
        out = rbc.apply_nl_intents(
            "x",
            _template_rule(r"(?i)^(?:A_Wall_)?(?P<rest>.+)$", fmt="A_Wall_{rest}"),
        )
        assert (
            out["autofill"]["normalize_source"]
            == r"(?i)^(?:A[ _-]+Wall[ _-]+)?(?P<rest>.+)$"
        )

    def test_format_without_literal_head_is_untouched(self):
        src = r"(?P<a>\w+)[ _-]*(?P<b>\w+)"
        out = rbc.apply_nl_intents("x", _template_rule(src, fmt="{a}_{b}"))
        assert out["autofill"]["normalize_source"] == src

    def test_non_template_kind_is_untouched(self):
        rule = _dur_rule(fmt="{h} HR")
        out = rbc.apply_nl_intents("must be 'X HR'.", rule)
        assert "normalize_source" not in out["autofill"]


class TestApplyNlIntentsRobustness:
    def test_idempotent_on_compound(self):
        once = rbc.apply_nl_intents(_NL_COMPOUND, _dur_rule(fmt="{h}-hour"))
        twice = rbc.apply_nl_intents(_NL_COMPOUND, once)
        assert twice == once

    def test_none_autofill_passthrough(self):
        rule = {"requirement": "present_and_nonempty", "autofill": None}
        assert rbc.apply_nl_intents("anything", rule) is rule

    def test_non_normalize_strategy_passthrough(self):
        rule = {"autofill": {"strategy": "none"}}
        assert rbc.apply_nl_intents("must be 'X HR'.", rule) is rule

    def test_does_not_mutate_input(self):
        rule = _dur_rule(fmt="{h}-hour")
        rbc.apply_nl_intents("must be 'X HR'.", rule)
        assert rule["autofill"]["normalize_format"] == "{h}-hour"  # original unchanged


# ── enforce_* (moved from app.py; existing app_module aliases already cover
#    these end-to-end via tests/test_streamlit_argv.py — a couple direct
#    checks here pin the module's own public surface). ────────────────────


class TestEnforceUniqueAutofix:
    def test_unique_in_set_forced_auto(self):
        out = rbc.enforce_unique_autofix(
            {"requirement": "unique_in_set", "parameter": "Number", "fixability": "manual"}
        )
        assert out["fixability"] == "auto"
        assert out["remediation"]["target"] == "instance"

    def test_other_requirement_passes_through(self):
        rule = {"requirement": "present_and_nonempty"}
        assert rbc.enforce_unique_autofix(rule) is rule

    def test_target_parameter_uses_the_bound_name(self):
        """Baking the CANONICAL name here pre-empts the runtime resolver.

        Every write site reads ``remediation.target_parameter or
        fetch_name(rule)``, so a canonical alias stored at save time wins over
        ``bound_parameter`` forever — the write targets a parameter that does
        not exist on the element and the fix degrades to a Path A issue.
        """
        out = rbc.enforce_unique_autofix({
            "requirement": "unique_in_set",
            "parameter": "So_phong",          # canonical intent label (VN)
            "bound_parameter": "Number",      # real Revit param
        })
        assert out["remediation"]["target_parameter"] == "Number"

    def test_readonly_guard_matches_the_bound_name(self):
        """L1 (audit): the catalog is keyed by REAL Revit parameter names.

        Matching on the canonical label meant a bound rule whose intent label is
        an alias never found its ParamSpec, so the read-only guard silently
        never fired — precisely the rules most likely to need it. Walls/Width is
        the read-only built-in the sibling test above uses.
        """
        result = rbc.validate_rule(
            {
                "id": "walls.width.bound", "category": "Walls",
                "parameter": "Chieu_rong",        # canonical alias (VN)
                "bound_parameter": "Width",       # real, read-only built-in
                "fixability": "auto",
                "remediation": {"action": "set_parameter", "target": "instance"},
            },
            is_geometry=False,
        )
        assert result.ok is False
        assert "read-only" in " ".join(e.message for e in result.errors)

    def test_readonly_guard_checks_the_actual_write_target(self):
        """P1-05: the guard must ask about the parameter that will be WRITTEN.

        Every write site resolves `remediation.target_parameter or
        fetch_name(rule)`, so naming a writable parameter in `parameter` and a
        read-only one in `target_parameter` used to sail through validation.
        """
        result = rbc.validate_rule(
            {
                "id": "x", "category": "Walls", "parameter": "Comments",
                "fixability": "auto",
                "remediation": {
                    "action": "set_parameter", "target": "instance",
                    "target_parameter": "Width",      # read-only built-in
                },
            },
            is_geometry=False,
        )
        assert result.ok is False
        assert "read-only" in " ".join(e.message for e in result.errors)

    def test_readonly_guard_uses_ruleset_category_when_rule_has_none(self):
        """P1-05: `Rule.category` is optional — an unset one inherits the
        ruleset's targets. Looking up only the per-rule category meant the
        catalog lookup ran against "" and the guard silently never fired for
        the most common authoring shape."""
        rule = {
            "id": "x", "parameter": "Width", "fixability": "auto",
            "remediation": {"action": "set_parameter", "target": "instance"},
        }
        assert rbc.validate_rule(rule, is_geometry=False).ok is True   # no context
        assert rbc.validate_rule(
            rule, is_geometry=False, ruleset_categories="Walls"
        ).ok is False

    def test_readonly_guard_fails_closed_across_multiple_categories(self):
        # Read-only in ANY target category blocks it — the run hits them all.
        result = rbc.validate_rule(
            {
                "id": "x", "parameter": "Width", "fixability": "auto",
                "remediation": {"action": "set_parameter", "target": "instance"},
            },
            is_geometry=False,
            ruleset_categories=["Doors", "Walls"],
        )
        assert result.ok is False

    def test_writable_parameter_still_passes_with_context(self):
        assert rbc.validate_rule(
            {
                "id": "x", "parameter": "Comments", "fixability": "auto",
                "remediation": {"action": "set_parameter", "target": "instance"},
            },
            is_geometry=False, ruleset_categories="Walls",
        ).ok is True

    def test_target_parameter_falls_back_to_canonical_when_unbound(self):
        out = rbc.enforce_unique_autofix(
            {"requirement": "unique_in_set", "parameter": "Number"}
        )
        assert out["remediation"]["target_parameter"] == "Number"


class TestEnforceReferenceMembership:
    def test_steers_to_canonical_format_reference(self):
        out = rbc.enforce_reference_membership({
            "requirement": "relation_compare", "parameter": "Assembly Code",
            "operator": ">=", "lookup": "classification_codes",
        })
        assert out["requirement"] == "canonical_format"
        assert out["autofill"]["normalize_reference"] == "classification_codes"

    def test_no_convention_passes_through(self):
        rule = {"requirement": "relation_compare", "parameter": "Fire Rating", "lookup": "ibc716"}
        assert rbc.enforce_reference_membership(rule) is rule

    def test_convention_resolves_off_the_bound_parameter(self):
        """The bound parameter is the stable signal — as the docstring says.

        Resolving the canonical label FIRST made the ``bound_parameter``
        fallback dead code (``parameter`` is always non-empty), so a rule
        whose canonical name is an alias never picked up its convention and
        the reference guard silently never fired.
        """
        out = rbc.enforce_reference_membership({
            "requirement": "canonical_format",
            "parameter": "Ma_phan_loai",         # canonical alias, no convention
            "bound_parameter": "Assembly Code",  # the real param that HAS one
        })
        assert out["requirement"] == "canonical_format"
        assert out["autofill"]["normalize_reference"] == "classification_codes"

    def test_canonical_still_resolves_when_binding_has_no_convention(self):
        # Fallback preserved: an unrelated binding must not mask a convention
        # that the canonical name legitimately carries.
        out = rbc.enforce_reference_membership({
            "requirement": "canonical_format",
            "parameter": "Assembly Code",
            "bound_parameter": "SomeCustomParam",
        })
        assert out["autofill"]["normalize_reference"] == "classification_codes"


# ── geometry-from-NL: the direction belt (2026-08-26) ──────────────────────
#
# Measured before shipping the router: a taught model emits check_type,
# threshold, unit conversion, reference and scope correctly, and gets
# clearance_direction WRONG whenever the sentence's preposition points the
# opposite way to the ray ("higher than 2100 mm ABOVE the floor BELOW it" ->
# it answered "above"; the ray fires down). Same teach+guarantee split as the
# duration-format and prefix-strip belts: the prompt says it, this proves it.
_GEO = {"check_type": "clearance_min", "clearance_direction": "above"}


class TestGeometryDirectionBelt:
    def test_floor_below_forces_below(self):
        nl = ("In the parking space, the lowest part of a duct must be higher "
              "than 2100 mm above the floor below it.")
        assert rbc.apply_geometry_nl_intents(nl, _GEO)["clearance_direction"] == "below"

    def test_headroom_alone_forces_below(self):
        nl = "Ducts must keep at least 2.1 m of headroom, only in the parking garage."
        assert rbc.apply_geometry_nl_intents(nl, _GEO)["clearance_direction"] == "below"

    def test_clear_above_is_left_alone(self):
        nl = "Keep 300 mm clear above every pipe for insulation."
        assert rbc.apply_geometry_nl_intents(nl, _GEO)["clearance_direction"] == "above"

    def test_ceiling_above_forces_above(self):
        nl = "Ducts must clear the ceiling above by 250 mm."
        rule = {"check_type": "clearance_min", "clearance_direction": "below"}
        assert rbc.apply_geometry_nl_intents(nl, rule)["clearance_direction"] == "above"

    def test_no_positional_evidence_leaves_the_draft_alone(self):
        # Silence beats guessing: nothing in the sentence says where the
        # reference sits.
        nl = "Ducts must keep 500 mm from the nearest obstruction."
        assert rbc.apply_geometry_nl_intents(nl, _GEO)["clearance_direction"] == "above"

    def test_contradictory_evidence_leaves_the_draft_alone(self):
        nl = "Keep clear above the duct and clear to the floor below it."
        assert rbc.apply_geometry_nl_intents(nl, _GEO)["clearance_direction"] == "above"

    def test_idempotent(self):
        nl = "headroom to the floor below"
        once = rbc.apply_geometry_nl_intents(nl, _GEO)
        assert rbc.apply_geometry_nl_intents(nl, once) == once

    def test_non_geometry_rule_passes_through_untouched(self):
        rule = {"requirement": "canonical_format", "parameter": "Fire Rating"}
        assert rbc.apply_geometry_nl_intents("headroom below", rule) is rule

    def test_does_not_mutate_input(self):
        rule = dict(_GEO)
        rbc.apply_geometry_nl_intents("headroom to the floor below", rule)
        assert rule["clearance_direction"] == "above"


class TestGeometryDraftShape:
    def test_prompt_teaches_the_ray_not_the_preposition(self):
        # The belt is the guarantee; the prompt must still TEACH it, or every
        # draft arrives wrong and leans on a regex that only covers the
        # phrasings we have seen.
        assert "DIRECTION IS THE RAY" in rbc.RB_GEOMETRY_SYSTEM
        for key in ("check_type", "threshold_mm", "clearance_direction",
                    "reference_source", "spatial_filter"):
            assert key in rbc.RB_GEOMETRY_SYSTEM


def test_router_failure_falls_back_without_crashing(monkeypatch):
    """The router's except branch promised a soft fallback and delivered a crash.

    Its comment reads "Router failure must not break rule drafting: fall back to
    the parameter path" — but the handler called `log.warning`, and the module
    has no `log`. Any router failure raised NameError instead of degrading, so
    the safety net was the thing that broke.
    """
    from bim_orchestrator import rule_builder_core as rbc

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(rbc, "build_llm_client", lambda: object(), raising=False)
    monkeypatch.setattr(
        "bim_orchestrator.llm.factory.build_llm_client", lambda: object()
    )

    calls: list[str] = []

    def _route_dies_then_extract_works(client, system, text):
        calls.append(system)
        if system is rbc._RB_ROUTE_SYSTEM:
            raise RuntimeError("router unavailable")
        return {"id": "fell.back", "parameter": "Fire Rating"}

    monkeypatch.setattr(rbc, "_complete_json_sync", _route_dies_then_extract_works)

    # Must not raise NameError; must land on the parameter path.
    rule, _warnings = rbc.draft_rule("a door must have a fire rating")

    assert rule["id"] == "fell.back"
    assert len(calls) == 2, "router attempted, then the parameter path ran"
    assert calls[0] is rbc._RB_ROUTE_SYSTEM
