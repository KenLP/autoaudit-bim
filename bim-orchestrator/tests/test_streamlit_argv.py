"""Tests for streamlit_app/app.py's `_build_orchestrator_argv`.

The Streamlit app spawns the orchestrator as a subprocess. The argv
builder is the only place that translates Streamlit's session_state
into CLI flags, so it's the natural seam to pin with tests -- v1.2
added the run-revit specific flags (--max-iterations, --no-forma) and
these regressions would be silent without coverage.

We stub `st.session_state` with a plain dict because the argv builder
only reads keys; no widgets or Streamlit runtime needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMLIT_APP_DIR = REPO_ROOT / "streamlit_app"


class _MockStreamlit:
    """Catch-all stub for the streamlit module.

    Any attribute access returns a callable+context-manager+attr-accessing
    proxy, so the module-import side-effects in streamlit_app/app.py
    (st.title, st.set_page_config, with st.sidebar:, with st.form: etc.)
    all become no-ops. The only attribute we actually CARE about is
    `session_state`, which the tests patch with a real dict.
    """

    def __init__(self) -> None:
        self.session_state: dict = {}

    def columns(self, spec, **kwargs):
        # st.columns(2) -> (col1, col2) ; st.columns([1, 2, 1]) -> 3-tuple
        n = spec if isinstance(spec, int) else len(spec)
        return tuple(_Proxy() for _ in range(n))

    def tabs(self, names, **kwargs):
        return [_Proxy() for _ in names]

    def number_input(self, label, *args, **kwargs):
        # Return the real default so int(...) on the result works at import.
        return kwargs.get("value", 0)

    def __getattr__(self, name):
        # Recurse: an arbitrary attribute returns another callable proxy
        # so `st.sidebar.markdown(...)` and `with st.form("..."): ...`
        # both resolve.
        return _Proxy()


class _Proxy:
    def __call__(self, *args, **kwargs):
        return _Proxy()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        # st.columns([1, 2]) returns iterable -- mimic an arbitrary
        # length tuple of proxies.
        return iter([_Proxy() for _ in range(8)])

    def __len__(self):
        return 8

    def __getitem__(self, key):
        # `tabs[0]` indexing into the return of st.tabs([...])
        return _Proxy()

    def __bool__(self):
        # Truthy by default so any `if st.button(...)` short-circuits
        # consistently regardless of mock state.
        return False

    def __getattr__(self, name):
        return _Proxy()


@pytest.fixture
def app_module():
    """Import streamlit_app/app.py with a stubbed streamlit module so the
    file-load top-level code (st.set_page_config etc.) doesn't blow up."""
    import types

    fake_st = _MockStreamlit()
    sys.modules["streamlit"] = fake_st  # type: ignore[assignment]

    # dotenv stub too -- the app calls load_dotenv at import
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules.setdefault("dotenv", fake_dotenv)

    sys.path.insert(0, str(STREAMLIT_APP_DIR))
    import importlib

    if "app" in sys.modules:
        del sys.modules["app"]
    app = importlib.import_module("app")
    return app


def _state(**overrides):
    """Build a minimal session_state dict for argv tests."""
    base = {
        "hub_id": "b.hub",
        "project_id": "b.proj",
        "aecdm_project_id": "urn:proj",
        "element_group_id": "eg",
        "rules_path": "",       # set below if needed
        "run_mode": "check",
        "limit": 2,
        "dry_run": False,
        "max_iterations": 1,
        "no_forma": True,
        "last_run_id": None,
        "is_running": False,
        "selected_run_id": None,
    }
    base.update(overrides)
    return base


class TestArgvCheckMode:
    def test_check_mode_baseline(self, app_module):
        with patch.object(app_module.st, "session_state", _state(run_mode="check")):
            argv = app_module._build_orchestrator_argv("check")
        assert argv[1:] == ["-m", "bim_orchestrator.orchestrator", "--check"]

    def test_check_ignores_runrevit_flags_even_if_set(self, app_module):
        """run-revit flags MUST NOT leak into --check argv; argparse would
        reject them since --check doesn't know --no-forma etc."""
        state = _state(
            run_mode="check",
            no_forma=True,
            max_iterations=5,
        )
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("check")
        for flag in ("--no-forma", "--max-iterations"):
            assert flag not in argv, f"{flag} leaked into --check argv: {argv}"


class TestArgvRunRevitMode:
    def test_runrevit_emits_all_specific_flags(self, app_module):
        state = _state(
            run_mode="run-revit",
            limit=60,
            max_iterations=3,
            no_forma=True,
        )
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert "--run-revit" in argv
        assert "--limit" in argv
        assert argv[argv.index("--limit") + 1] == "60"
        assert "--max-iterations" in argv
        assert argv[argv.index("--max-iterations") + 1] == "3"
        assert "--no-forma" in argv

    def test_runrevit_skip_max_iterations_when_default_1(self, app_module):
        """max_iterations=1 is the orchestrator default; skip emitting it
        to keep the argv terse for the common case."""
        state = _state(run_mode="run-revit", max_iterations=1)
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert "--max-iterations" not in argv

    def test_runrevit_skip_optional_flags_when_off(self, app_module):
        state = _state(
            run_mode="run-revit",
            bep_fixture=False,
            no_forma=False,
        )
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert "--no-forma" not in argv

    def test_runrevit_emits_rules_path(self, app_module, tmp_path):
        rules = tmp_path / "rules.test.yaml"
        rules.write_text("scenario: test\ntarget_category: Rooms\nrules: []\n")
        state = _state(run_mode="run-revit", rules_path=str(rules))
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert "--rules" in argv
        assert str(rules) in argv

    def test_runrevit_dry_run_flag(self, app_module):
        state = _state(run_mode="run-revit", dry_run=True)
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert "--dry-run" in argv

    def test_emits_multiple_rules_paths(self, app_module, tmp_path):
        # v1.4-K6: several selected YAMLs → one --rules flag with N values.
        r1 = tmp_path / "rules.a.yaml"
        r2 = tmp_path / "rules.b.yaml"
        for r in (r1, r2):
            r.write_text("scenario: t\ntarget_category: Rooms\nrules: []\n")
        state = _state(run_mode="run-revit", rules_paths=[str(r1), str(r2)])
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        i = argv.index("--rules")
        # nargs="+" form: both paths follow the single flag, contiguously.
        assert argv[i + 1] == str(r1)
        assert argv[i + 2] == str(r2)

class TestParseAecdmProjects:
    """v1.4-K9 — dual-id project parser: one aecdm_list_projects call yields
    both the AECDM URN (queries) and the DM/Issues id (b.<uuid>)."""

    _BLOB = (
        "Found 2 AEC project(s):\n\n"
        "• Some Office\n"
        "    AECDM id: urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000008\n"
        "    DM/Issues id: b.00000000-0000-0000-0000-000000000007\n"
        "• Sample ACC Project\n"
        "    AECDM id: urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000002\n"
        "    DM/Issues id: b.00000000-0000-0000-0000-000000000001\n"
    )

    class _T:
        def __init__(self, text):
            self.text = text

    def test_extracts_both_ids(self, app_module):
        rows = app_module._parse_aecdm_projects([self._T(self._BLOB)])
        assert rows == [
            (
                "urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000008",
                "b.00000000-0000-0000-0000-000000000007",
                "Some Office",
            ),
            (
                "urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000002",
                "b.00000000-0000-0000-0000-000000000001",
                "Sample ACC Project",
            ),
        ]

    def test_project_without_dm_id_keeps_empty(self, app_module):
        blob = (
            "• No Container Project\n"
            "    AECDM id: urn:adsk.workspace:prod.project:abc\n"
        )
        rows = app_module._parse_aecdm_projects([self._T(blob)])
        assert rows == [("urn:adsk.workspace:prod.project:abc", "", "No Container Project")]


class TestSaveRuleK10:
    """v1.4-K10 / Proposal A — the Rule Builder can author a Path-B (auto-fix)
    rule with the new fields, and they survive the save→YAML→load round-trip."""

    def test_path_b_remediation_roundtrip(self, app_module):
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "doors.fr.k10test", "category": "Doors", "parameter": "Fire Rating",
            "requirement": "matches_regex_if_present",
            "pattern": r"^(0.5|1|1.5|2|3)-hour$", "description": "fr",
            "severity_level": "severity_high",
            "autofill": {"strategy": "normalize", "normalize_kind": "fire_rating"},
            "remediation": {"action": "set_parameter", "target": "type"},
            "scope_filter": {"param": "IsExternal", "pattern": "(?i)true"},
            "operator": None, "fixability": "auto",
        }
        scenario = "k10_roundtrip_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.requirement == "matches_regex_if_present"
            assert r.severity_level == "severity_high"
            assert r.autofill.strategy == "normalize"
            assert r.autofill.normalize_kind == "fire_rating"
            assert r.remediation.action == "set_parameter"
            assert r.remediation.target == "type"
            assert r.scope_filter is not None and r.scope_filter.param == "IsExternal"
        finally:
            out.unlink(missing_ok=True)

    def test_numeric_compare_roundtrip(self, app_module):
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "x.numcmp", "category": "Doors", "parameter": "Width",
            "requirement": "numeric_compare", "operator": ">", "threshold": 800.0,
            "description": "width", "severity_level": "severity_medium",
            "fixability": "manual",
        }
        scenario = "k10_numcmp_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.requirement == "numeric_compare"
            assert r.operator == ">"
            assert r.threshold == 800.0
        finally:
            out.unlink(missing_ok=True)

    def test_map_kind_roundtrip(self, app_module):
        """v1.4-K13 — a normalize_kind=map (fixed text) rule survives save→load."""
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "x.map", "category": "Doors", "parameter": "Fire Rating",
            "requirement": "canonical_format", "description": "fixed text",
            "severity_level": "severity_medium", "fixability": "auto",
            "autofill": {"strategy": "normalize", "normalize_kind": "map",
                         "normalize_map": {"nr": "Not Rated", "0": "Not Rated"}},
            "remediation": {"action": "set_parameter", "target": "type"},
        }
        scenario = "k13_map_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.autofill.normalize_kind == "map"
            assert r.autofill.normalize_map == {"nr": "Not Rated", "0": "Not Rated"}
        finally:
            out.unlink(missing_ok=True)

    def test_template_kind_roundtrip(self, app_module):
        """v1.4-K15 — normalize_kind=template (regex→template name transform)
        keeps normalize_source through save→load."""
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "x.tmpl", "category": "Furniture", "parameter": "Family Name",
            "requirement": "matches_regex", "pattern": r"^ADSK_Fur_\w+_\w+$",
            "description": "naming", "severity_level": "severity_medium",
            "fixability": "auto",
            "autofill": {
                "strategy": "normalize", "normalize_kind": "template",
                "normalize_source": r"(?i)^adsk[ _-]*fur[ _-]*(?P<fn>[a-z]+)[ _-]*(?P<d1>[a-z0-9]+)",
                "normalize_format": "ADSK_Fur_{fn}_{d1}",
            },
            "remediation": {"action": "rename_element", "target": "type"},
        }
        scenario = "k15_tmpl_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.autofill.normalize_kind == "template"
            assert r.autofill.normalize_source.startswith("(?i)^adsk")
            assert r.autofill.normalize_format == "ADSK_Fur_{fn}_{d1}"
            assert r.remediation.action == "rename_element"
        finally:
            out.unlink(missing_ok=True)

    def test_reference_kind_roundtrip(self, app_module):
        """v1.4-K21 — normalize_kind=reference keeps normalize_reference through
        save→load and pairs with canonical_format."""
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "x.ref", "category": "Furniture", "parameter": "Material",
            "requirement": "canonical_format", "description": "approved palette",
            "severity_level": "severity_medium", "fixability": "auto",
            "autofill": {"strategy": "normalize", "normalize_kind": "reference",
                         "normalize_reference": "approved_materials"},
            "remediation": {"action": "set_parameter", "target": "auto"},
        }
        scenario = "k21_ref_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.autofill.normalize_kind == "reference"
            assert r.autofill.normalize_reference == "approved_materials"
            assert r.requirement == "canonical_format"
        finally:
            out.unlink(missing_ok=True)

    def test_inherit_then_normalize_roundtrip(self, app_module):
        """v1.4-K22 — the compound strategy survives save→load with host_param +
        normalize_kind/format and the canonical_format requirement."""
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "x.ihn", "category": "Doors", "parameter": "Fire Rating",
            "requirement": "canonical_format", "description": "inherit+format",
            "severity_level": "severity_medium", "fixability": "auto",
            "autofill": {"strategy": "inherit_then_normalize",
                         "normalize_kind": "duration", "normalize_format": "{h} HR"},
            "remediation": {"action": "set_parameter", "target": "auto"},
        }
        scenario = "k22_ihn_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.autofill.strategy == "inherit_then_normalize"
            assert r.autofill.normalize_format == "{h} HR"
            assert r.requirement == "canonical_format"
        finally:
            out.unlink(missing_ok=True)

    def test_parse_reference_lines(self, app_module):
        """v1.4-K21 — the inline reference editor parser: canonical = aliases."""
        entries = app_module._parse_reference_lines(
            "Oak = white oak, wood-oak\nLaminate-White\n\n  = orphan"
        )
        assert entries == [
            {"canonical": "Oak", "aliases": ["white oak", "wood-oak"]},
            {"canonical": "Laminate-White", "aliases": []},
        ]

    def test_length_dimension_roundtrip(self, app_module):
        """v1.4-K13 — a non-time unit dimension (length) survives save→load."""
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "x.len", "category": "Doors", "parameter": "Width",
            "requirement": "canonical_format", "description": "mm form",
            "severity_level": "severity_low", "fixability": "auto",
            "autofill": {"strategy": "normalize", "normalize_kind": "length",
                         "normalize_format": "{mm} mm"},
            "remediation": {"action": "set_parameter", "target": "instance"},
        }
        scenario = "k13_len_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.autofill.normalize_kind == "length"
            assert r.autofill.normalize_format == "{mm} mm"
        finally:
            out.unlink(missing_ok=True)


class TestRuleBuilderCatalogV2:
    """v2 catalog-powered Rule Builder glue: the param dropdown label + the
    category-display → OST join that feeds the catalog lookup."""

    def test_param_option_label_badges(self, app_module):
        from bim_orchestrator.policies.param_catalog import ParamSpec

        ro = ParamSpec(name="Width", storage="double", binding="type",
                       writable=False, dimension="length")
        assert "read-only" in app_module._param_option_label(ro)
        assert "type" in app_module._param_option_label(ro)

        rw = ParamSpec(name="Fire Rating", storage="string", binding="type",
                       writable=True, dimension="text")
        assert "ghi được" in app_module._param_option_label(rw)

        rn = ParamSpec(name="Family Name", storage="string", binding="type",
                       writable=False, dimension="text", rename_only=True)
        assert "rename" in app_module._param_option_label(rn)

    def test_ost_for_display_join(self, app_module):
        # The catalog joins by OST, so this glue must resolve the display label.
        assert app_module._ost_for_display("Walls") == "OST_Walls"
        assert app_module._ost_for_display("Stair Runs") == "OST_StairsRuns"
        assert app_module._ost_for_display("Tường") == "OST_Walls"  # alias

    def test_grounding_block_lists_stairs_and_params(self, app_module):
        # Q1: the extraction prompt must include Stairs/Stair Runs (the hard-coded
        # enum omitted them → "stair run" misread as Structural Framing).
        plain = app_module._rb_grounding_block(False)
        assert "Stair Runs" in plain and "Stairs" in plain and "Ramps" in plain
        # catalog-grounded also lists the valid params + intent aliases (F1b).
        grounded = app_module._rb_grounding_block(True)
        assert "Actual Run Width" in grounded and "Fire Rating" in grounded
        assert "Unbounded Height" in grounded and "ceiling height" in grounded

    def test_stairs_vs_stair_runs_disambiguation(self, app_module):
        # User confusion: Stairs vs Stair Runs. The grounding must steer compliance
        # width checks to Stair Runs / Actual Run Width (the as-built value), not
        # Stairs / Minimum Run Width (the design setting).
        g = app_module._rb_grounding_block(False)
        assert "STAIRS vs STAIR RUNS" in g
        assert "Actual Run Width" in g and "Minimum Run Width" in g
        # a UI note exists for both categories
        assert "Stairs" in app_module._CATEGORY_NOTES
        assert "Stair Runs" in app_module._CATEGORY_NOTES

    def test_scope_filter_in_extraction_schema(self, app_module):
        # F4: scope_filter must be an output field + a decision rule.
        assert "scope_filter" in app_module._RB_EXTRACT_SYSTEM
        assert "only external doors" in app_module._RB_EXTRACT_SYSTEM.lower()

    def test_intent_alias_param(self, app_module):
        # F1b: the non-obvious intent → real param mappings.
        rooms = ["Unbounded Height", "Ceiling Height", "Area"]
        assert app_module._alias_param("OST_Rooms", "minimum ceiling height", rooms) == "Unbounded Height"
        runs = ["Actual Run Width", "Run Height"]
        assert app_module._alias_param("OST_StairsRuns", "clear width of the run", runs) == "Actual Run Width"
        # no alias for the category → None
        assert app_module._alias_param("OST_Walls", "clear width", ["Width"]) is None
        # alias target absent from names → None (don't suggest a missing param)
        assert app_module._alias_param("OST_Rooms", "ceiling height", ["Area"]) is None

    def test_suggest_index_uses_alias_before_token(self, app_module):
        # "ceiling height" must map to Unbounded Height, NOT token-match "Ceiling Height".
        names = ["Unbounded Height", "Ceiling Height", "Area"]
        i = app_module._suggest_param_index("clear ceiling height", names, "OST_Rooms")
        assert names[i] == "Unbounded Height"

    def test_suggest_param_index(self, app_module):
        names = ["Actual Run Width", "Actual Tread Depth", "Mark", "Comments"]
        custom = len(names)
        # exact (case-insensitive)
        assert app_module._suggest_param_index("mark", names) == 2
        # intent → token overlap ("clear width" shares "width")
        assert app_module._suggest_param_index("Clear Width", names) == 0
        # empty intent → first param (auto-suggest, not custom)
        assert app_module._suggest_param_index("", names) == 0
        # no overlap → custom sentinel (free-text box appears)
        assert app_module._suggest_param_index("Occupancy Load", names) == custom

    def test_migrate_legacy_requirement(self, app_module):
        # Q3: a loaded numeric_min rule shows as numeric_compare(>=) in the form.
        out = app_module._migrate_legacy_rule(
            {"requirement": "numeric_min", "threshold": 1000.0, "unit": "mm"}
        )
        assert out["requirement"] == "numeric_compare"
        assert out["operator"] == ">=" and out["threshold"] == 1000.0
        # positive_number → numeric_compare(> 0)
        pn = app_module._migrate_legacy_rule({"requirement": "positive_number"})
        assert pn["requirement"] == "numeric_compare" and pn["operator"] == ">"
        # fire_rating_ge → relation_compare(fire_rating)
        fr = app_module._migrate_legacy_rule({"requirement": "fire_rating_ge"})
        assert fr["requirement"] == "relation_compare" and fr["compare_kind"] == "fire_rating"
        # modern requirement passes through untouched (same object)
        modern = {"requirement": "numeric_compare", "operator": ">"}
        assert app_module._migrate_legacy_rule(modern) is modern


class TestUniqueAutoEnforce:
    """QA F2 — unique_in_set is hard-enforced to Path-B auto (next-available renumber)."""

    def test_enforce_unique_autofix(self, app_module):
        out = app_module._enforce_unique_autofix(
            {"requirement": "unique_in_set", "parameter": "Number", "fixability": "manual"}
        )
        assert out["fixability"] == "auto"
        assert out["remediation"]["action"] == "set_parameter"
        assert out["remediation"]["target"] == "instance"  # per-element unique value
        assert out["remediation"]["new_value_strategy"] == "next_available"
        assert out["remediation"]["target_parameter"] == "Number"

    def test_non_unique_passes_through(self, app_module):
        rule = {"requirement": "present_and_nonempty", "fixability": "manual"}
        assert app_module._enforce_unique_autofix(rule) is rule

    def test_unique_roundtrip_saves_auto(self, app_module):
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        REPO_ROOT = Path(__file__).resolve().parents[1]
        rule = {
            "id": "rooms.number.unique", "category": "Rooms", "parameter": "Number",
            "requirement": "unique_in_set", "description": "unique numbers",
            "severity_level": "severity_medium", "fixability": "manual",  # LLM said manual
        }
        scenario = "f2_unique_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.fixability == "auto"  # overridden despite LLM "manual"
            assert r.remediation.new_value_strategy == "next_available"
        finally:
            out.unlink(missing_ok=True)


class TestReferenceMembershipEnforce:
    """QA G7 — a value-validity rule on a param whose shared-param convention names a
    `reference` set is steered to canonical_format + normalize_reference:<that set>.

    Repro: vague "Mã phân loại của tường phải hợp lệ" binds Walls·Assembly Code but
    Haiku oscillates {canonical_format, relation_compare, present_and_nonempty} and even
    mislabels the reference set as a `lookup` table + invents the set name. The bound
    param is the stable signal, so we override deterministically at save."""

    def test_relation_compare_mislabel_is_steered(self, app_module):
        # The exact repro: relation_compare + lookup naming a REFERENCE set.
        out = app_module._enforce_reference_membership({
            "requirement": "relation_compare", "parameter": "Assembly Code",
            "operator": ">=", "compare_kind": "string", "lookup": "classification_codes",
            "fixability": "manual",
        })
        assert out["requirement"] == "canonical_format"
        assert out["fixability"] == "auto"
        assert out["autofill"]["normalize_kind"] == "reference"
        assert out["autofill"]["normalize_reference"] == "classification_codes"
        assert out["remediation"] == {"action": "set_parameter", "target": "auto"}
        # relation_compare leftovers dropped so the saved rule is coherent
        for k in ("lookup", "operator", "compare_kind", "other_param"):
            assert k not in out

    def test_invented_reference_name_is_canonicalised(self, app_module):
        # Secondary bug: model picks canonical_format but invents the set name.
        out = app_module._enforce_reference_membership({
            "requirement": "canonical_format", "parameter": "Assembly Code",
            "autofill": {"strategy": "normalize", "normalize_kind": "reference",
                         "normalize_reference": "uniformat_codes"},
        })
        assert out["autofill"]["normalize_reference"] == "classification_codes"

    def test_classification_number_maps_to_uniclass_pr(self, app_module):
        out = app_module._enforce_reference_membership({
            "requirement": "canonical_format", "parameter": "Classification Number",
        })
        assert out["autofill"]["normalize_reference"] == "uniclass_pr"

    def test_present_and_nonempty_passes_through(self, app_module):
        # A completeness-ONLY ask ("Assembly Code phải được điền") is NOT membership.
        rule = {"requirement": "present_and_nonempty", "parameter": "Assembly Code"}
        assert app_module._enforce_reference_membership(rule) is rule

    def test_param_without_reference_convention_passes_through(self, app_module):
        # Fire Rating is a built-in, not a reference-bearing shared-param convention.
        rule = {"requirement": "relation_compare", "parameter": "Fire Rating",
                "lookup": "ibc716", "operator": ">="}
        assert app_module._enforce_reference_membership(rule) is rule

    def test_cobie_manufacturer_no_reference_passes_through(self, app_module):
        # The convention exists but has NO reference (presence-only field).
        rule = {"requirement": "relation_compare", "parameter": "COBie.Type.Manufacturer"}
        assert app_module._enforce_reference_membership(rule) is rule

    def test_save_roundtrips_steered_reference(self, app_module):
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        rule = {
            "id": "walls.assembly_code.valid", "category": "Walls",
            "parameter": "Assembly Code", "requirement": "relation_compare",
            "operator": ">=", "lookup": "classification_codes",
            "description": "classification code must be valid",
            "severity_level": "severity_medium", "fixability": "manual",
        }
        scenario = "g7_reference_pytest"
        out = REPO_ROOT / "config" / f"rules.{scenario}.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, scenario)
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            r = rs.rules[0]
            assert r.requirement == "canonical_format"
            assert r.autofill.normalize_reference == "classification_codes"
            assert r.lookup is None  # leftover not written
        finally:
            out.unlink(missing_ok=True)

    def test_disambiguation_guidance_in_prompt(self, app_module):
        p = app_module._RB_EXTRACT_SYSTEM
        assert "DISAMBIGUATE the two value-validity mechanisms" in p
        assert "MEMBERSHIP in an APPROVED SET" in p
        assert "REQUIRED BY A CODE TABLE keyed by a RELATED element" in p


class TestRelationCompareGuidance:
    """QA F3 — the prompt must teach choosing compare_kind, never defaulting to
    fire_rating (string for room/space names+numbers; numeric for quantities)."""

    def test_prompt_teaches_compare_kind_choice(self, app_module):
        p = app_module._RB_EXTRACT_SYSTEM
        assert "NEVER default to fire_rating" in p
        assert '"string"' in p and '"numeric" (DEFAULT)' in p
        # the room/space string cases the user called out
        assert "MEP space" in p and "Number" in p


class TestLookupTableUI:
    """① Rule Builder lookup picker + grounding (IBC §716 table-driven check)."""

    def test_available_lookup_tables_lists_ibc716(self, app_module):
        assert "ibc716" in app_module._available_lookup_tables()

    def test_grounding_lists_lookup_tables(self, app_module):
        g = app_module._rb_grounding_block(False)
        assert "AVAILABLE LOOKUP TABLES" in g and "ibc716" in g

    def test_grounding_lists_shared_params(self, app_module):
        # Shared / openBIM params (COBie, classification, U-value) must be grounded
        # so a deliverable rule binds to the agreed name, not an invented one.
        g = app_module._rb_grounding_block(False)
        assert "SHARED / openBIM PARAMETERS" in g
        assert "COBie.Type.Category" in g and "Thermal Transmittance (U)" in g
        assert "u-value" in g.lower()              # intent phrase surfaced
        assert "omniclass_table23" in g            # cited reference set

    def test_prompt_teaches_lookup_citation(self, app_module):
        p = app_module._RB_EXTRACT_SYSTEM
        assert '"lookup"' in p              # output field
        assert "CODE TABLE" in p and 'lookup="' in p  # decision rule

    def test_parse_lookup_keys(self, app_module):
        keys = app_module._parse_lookup_keys(
            "host.Fire Rating : fire_rating\nhost.Fire Function : string\nBare Param\n"
        )
        assert keys == [
            {"param": "host.Fire Rating", "dimension": "fire_rating"},
            {"param": "host.Fire Function", "dimension": "string"},
            {"param": "Bare Param", "dimension": "string"},  # default dimension
        ]

    def test_parse_lookup_rows(self, app_module):
        rows = app_module._parse_lookup_rows(
            "1 HR | Corridor -> 20 min\n1 HR | * -> 60 min\nnonsense line\n"
        )
        assert rows == [
            {"when": ["1 HR", "Corridor"], "require": "20 min"},
            {"when": ["1 HR", "*"], "require": "60 min"},  # wildcard kept
        ]

    def test_write_lookup_roundtrips(self, app_module):
        from bim_orchestrator.policies import lookup_table as lt

        repo_root = Path(__file__).resolve().parents[1]
        name = "uitest_pytest"
        out = repo_root / "config" / f"lookup.{name}.yaml"
        try:
            app_module._write_lookup_file(
                name,
                [{"param": "host.Fire Rating", "dimension": "fire_rating"},
                 {"param": "host.Fire Function", "dimension": "string"}],
                [{"when": ["1 HR", "Corridor"], "require": "20 min"},
                 {"when": ["1 HR", "*"], "require": "60 min"}],
                "test table",
            )
            lt.clear_cache()
            t = lt.load_lookup(name)
            assert t.match({"host.Fire Rating": "1 HR", "host.Fire Function": "Corridor"}) == ("20 min", False)
            assert t.match({"host.Fire Rating": "1 HR"}) == ("60 min", False)  # via "*"
        finally:
            out.unlink(missing_ok=True)
            lt.clear_cache()

    def test_save_roundtrips_lookup(self, app_module):
        import yaml
        from bim_orchestrator.policies.rules_schema import RuleSet

        repo_root = Path(__file__).resolve().parents[1]
        rule = {
            "id": "doors.fr.ibc716", "category": "Doors", "parameter": "Fire Rating",
            "requirement": "relation_compare", "compare_kind": "fire_rating",
            "operator": ">=", "lookup": "ibc716", "description": "ibc716",
            "severity_level": "severity_high", "fixability": "manual",
            "remediation": {"action": "create_acc_issue"},
        }
        out = repo_root / "config" / "rules.ui_lookup_pytest.yaml"
        try:
            ok, msg = app_module._save_rule_to_yaml(rule, "ui_lookup_pytest")
            assert ok, msg
            rs = RuleSet.model_validate(yaml.safe_load(out.read_text(encoding="utf-8")))
            assert rs.rules[0].lookup == "ibc716"
            assert rs.rules[0].requirement == "relation_compare"
        finally:
            out.unlink(missing_ok=True)


class TestParseMapLines:
    """v1.4-K13 — the Rule Builder's map-editor text → {variant: canonical} dict."""

    def test_basic(self, app_module):
        out = app_module._parse_map_lines("NR = Not Rated\n0 = Not Rated")
        assert out == {"nr": "Not Rated", "0": "Not Rated"}

    def test_skips_blank_and_malformed(self, app_module):
        out = app_module._parse_map_lines("\nNR = Not Rated\nnonsense line\n  \n")
        assert out == {"nr": "Not Rated"}

    def test_empty(self, app_module):
        assert app_module._parse_map_lines("") == {}


class TestIssueCreationDefault:
    """v1.4-K14 — issues are created by DEFAULT (off-by-default used to leave the
    Approvals inbox silently empty)."""

    def test_default_no_forma_is_false(self, app_module):
        assert app_module._DEFAULTS["no_forma"] is False

    def test_default_run_revit_does_not_suppress_forma(self, app_module):
        # a fresh session (no_forma=False) in run-revit must NOT pass --no-forma,
        # so proposal issues + Approvals records get created.
        state = _state(run_mode="run-revit", no_forma=False)
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert "--no-forma" not in argv


class TestRuleActionSummary:
    """v1.4-K16 — the one-line action label in the 'Rule đã tạo' list."""

    def test_path_a_issue(self, app_module):
        r = {"remediation": {"action": "create_acc_issue"}, "autofill": {"strategy": "none"}}
        assert app_module._rule_action_summary(r) == "📋 ACC Issue (Path A)"

    def test_normalize_shows_kind_and_target(self, app_module):
        r = {"remediation": {"action": "set_parameter", "target": "type"},
             "autofill": {"strategy": "normalize", "normalize_kind": "duration"}}
        out = app_module._rule_action_summary(r)
        assert "normalize" in out and "duration" in out and "type" in out

    def test_rename(self, app_module):
        r = {"remediation": {"action": "rename_element"},
             "autofill": {"strategy": "normalize", "normalize_kind": "template"}}
        out = app_module._rule_action_summary(r)
        assert "đổi tên" in out and "template" in out

    def test_fixed_value(self, app_module):
        r = {"remediation": {"action": "set_parameter", "target": "instance",
                             "new_value_strategy": "fixed"}, "autofill": {"strategy": "none"}}
        assert "cố định" in app_module._rule_action_summary(r)

    def test_missing_fields_defaults_to_issue(self, app_module):
        assert app_module._rule_action_summary({}) == "📋 ACC Issue (Path A)"


class TestApprovalsLifecycle:
    """v1.4-K14 — Approvals inbox lifecycle helpers (status badge, rule label,
    ignore-archive)."""

    def test_pending_lifecycle(self, app_module):
        badge, istat = app_module._proposal_lifecycle(
            {"applied": False, "status": "pending_approval", "issue_status": "open"})
        assert "Chờ duyệt" in badge
        assert "mở" in istat.lower() or "in progress" in istat.lower()

    def test_applied_closed_lifecycle(self, app_module):
        badge, istat = app_module._proposal_lifecycle(
            {"applied": True, "status": "applied", "issue_status": "closed"})
        assert "Đã áp dụng" in badge
        assert "đóng" in istat.lower() or "closed" in istat.lower()

    def test_applied_close_failed_lifecycle(self, app_module):
        _badge, istat = app_module._proposal_lifecycle(
            {"applied": True, "issue_status": "applied_pending_close"})
        assert "chưa đóng" in istat.lower()

    def test_old_record_without_issue_status_falls_back(self, app_module):
        # back-compat: an applied record predating issue_status → treated closed
        _badge, istat = app_module._proposal_lifecycle({"applied": True})
        assert "đóng" in istat.lower() or "closed" in istat.lower()

    def test_proposal_rules_parsed_from_finding_id(self, app_module):
        rec = {"fixes": [
            {"finding_id": "doors.fr.canon::123"},
            {"finding_id": "doors.fr.canon::456"},
            {"finding_id": "doors.naming::789"},
        ]}
        assert app_module._proposal_rules(rec) == "doors.fr.canon, doors.naming"

    def test_proposal_rules_empty(self, app_module):
        assert app_module._proposal_rules({"fixes": []}) == "—"

    def test_ignore_archives_record(self, app_module, tmp_path):
        rec = tmp_path / "issue1.json"
        rec.write_text("{}", encoding="utf-8")
        app_module._ignore_proposal(rec)
        # moved out of the inbox into _ignored/ (reversible, not deleted)
        assert not rec.exists()
        assert (tmp_path / "_ignored" / "issue1.json").exists()
        # the non-recursive *.json glob no longer sees it
        assert list(tmp_path.glob("*.json")) == []


class TestSkipsMissingRules:
    def test_skips_missing_rules_paths(self, app_module, tmp_path):
        real = tmp_path / "rules.real.yaml"
        real.write_text("scenario: t\ntarget_category: Rooms\nrules: []\n")
        missing = str(tmp_path / "nope.yaml")
        state = _state(run_mode="run-revit", rules_paths=[str(real), missing])
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("run-revit")
        assert str(real) in argv
        assert missing not in argv


class TestArgvApplyMode:
    def test_apply_does_not_emit_runrevit_flags(self, app_module):
        state = _state(run_mode="apply", no_forma=True)
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("apply")
        for flag in ("--no-forma", "--max-iterations"):
            assert flag not in argv

    def test_apply_emits_limit_and_dry_run(self, app_module):
        state = _state(run_mode="apply", limit=5, dry_run=True)
        with patch.object(app_module.st, "session_state", state):
            argv = app_module._build_orchestrator_argv("apply")
        assert "--limit" in argv
        assert argv[argv.index("--limit") + 1] == "5"
        assert "--dry-run" in argv


class TestYamlHasGeometryRules:
    """Tests for _yaml_has_geometry_rules — the helper that drives the
    mode-lock in Setup tab Section 3 when a geometry-only YAML is selected."""

    def test_returns_true_for_yaml_with_geometry_rules(self, app_module, tmp_path):
        p = tmp_path / "rules.geo.yaml"
        p.write_text(
            "scenario: duct_clearance\n"
            "target_category: Ducts\n"
            "rules: []\n"
            "geometry_rules:\n"
            "  - id: ducts.floor_clearance\n"
            "    category: Ducts\n"
            "    check_type: clearance_min\n"
            "    description: test\n"
            "    threshold_mm: 2400.0\n",
            encoding="utf-8",
        )
        assert app_module._yaml_has_geometry_rules(str(p)) is True

    def test_returns_false_for_yaml_without_geometry_rules(self, app_module, tmp_path):
        p = tmp_path / "rules.param.yaml"
        p.write_text(
            "scenario: rooms\ntarget_category: Rooms\nrules: []\n",
            encoding="utf-8",
        )
        assert app_module._yaml_has_geometry_rules(str(p)) is False

    def test_returns_false_for_empty_geometry_rules_list(self, app_module, tmp_path):
        p = tmp_path / "rules.empty.yaml"
        p.write_text(
            "scenario: rooms\ntarget_category: Rooms\nrules: []\ngeometry_rules: []\n",
            encoding="utf-8",
        )
        assert app_module._yaml_has_geometry_rules(str(p)) is False

    def test_returns_false_for_empty_path(self, app_module):
        assert app_module._yaml_has_geometry_rules("") is False

    def test_returns_false_for_nonexistent_file(self, app_module):
        assert app_module._yaml_has_geometry_rules("/no/such/file.yaml") is False

    def test_returns_false_for_invalid_yaml(self, app_module, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("{{invalid: yaml: content", encoding="utf-8")
        assert app_module._yaml_has_geometry_rules(str(p)) is False


class TestStreamSubprocessTimeout:
    """H5: a hung child (e.g. Revit modal dialog) must be terminated at the
    wall-clock cap, not left to freeze the whole UI forever."""

    def test_wall_clock_timeout_terminates(self, app_module):
        import os
        import time

        class _Box:
            def code(self, *a, **k):
                return None

        start = time.monotonic()
        rc, log = app_module._stream_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            os.environ.copy(),
            _Box(),
            timeout_s=1,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 15          # terminated promptly, not after 30s
        assert rc != 0               # non-zero: terminated / killed
        assert "TIMEOUT" in log


class _StopRerun(Exception):
    """Stands in for Streamlit's internal rerun stop-exception, which can be
    raised from inside any st.* call (e.g. box.code(...))."""


class TestStreamSubprocessOrphanGuard:
    """M9 — a Streamlit rerun raises a stop-exception INSIDE box.code(...) in
    the streaming loop. Before the fix this unwound out of _stream_subprocess
    without ever reaching proc.terminate()/proc.wait(), leaving the child
    alive; a second Run click then raced a second write into the same Revit
    document. The whole loop is now wrapped in try/finally."""

    def test_box_exception_mid_stream_still_terminates_process(self, app_module):
        import os
        import time

        class _RaisingBox:
            """Raises on the FIRST line so the streaming loop unwinds through
            box.code(...) exactly like a real Streamlit rerun would."""

            def code(self, *a, **k):
                raise _StopRerun("simulated Streamlit rerun")

        # A long-lived child so if terminate() is skipped, it's still alive
        # long after this test function returns -- easy to detect via poll().
        proc_holder: dict = {}

        def _on_start(proc):
            proc_holder["proc"] = proc

        with pytest.raises(_StopRerun):
            app_module._stream_subprocess(
                # Must print at least one line so the loop reaches box.code(...)
                # (where the simulated rerun stop-exception fires) instead of
                # blocking forever on an empty stdout queue.
                [sys.executable, "-c", "print('hi'); import time; time.sleep(30)"],
                os.environ.copy(),
                _RaisingBox(),
                on_start=_on_start,
            )

        proc = proc_holder["proc"]
        # Give terminate() a brief moment to take effect, then assert it's dead.
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert proc.poll() is not None, "child process was left running (orphaned)"

    def test_normal_completion_still_returns_rc_and_log(self, app_module):
        import os

        class _Box:
            def code(self, *a, **k):
                return None

        rc, log = app_module._stream_subprocess(
            [sys.executable, "-c", "print('hello')"],
            os.environ.copy(),
            _Box(),
        )
        assert rc == 0
        assert "hello" in log


class TestLaunchGuarded:
    """M9 — a second launch attempt while a prior process handle is still
    alive (per session_state) must be BLOCKED, not raced. Blocking (rather
    than auto-terminating the old run) is the safer default for a live Revit
    write session."""

    def test_blocks_when_active_proc_still_alive(self, app_module, tmp_path):
        import os
        import subprocess as sp

        class _Box:
            def code(self, *a, **k):
                return None

        # A genuinely-alive process standing in for a prior in-flight run.
        alive = sp.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            state = _state()
            state[app_module._ACTIVE_PROC_KEY] = alive
            with patch.object(app_module.st, "session_state", state):
                result = app_module._launch_guarded(
                    [sys.executable, "-c", "print('should not run')"],
                    os.environ.copy(), _Box(),
                )
            assert result is None  # blocked -- no launch happened
        finally:
            alive.terminate()
            alive.wait(timeout=5)

    def test_allows_launch_when_prior_proc_finished(self, app_module):
        import os
        import subprocess as sp

        class _Box:
            def code(self, *a, **k):
                return None

        finished = sp.Popen([sys.executable, "-c", "pass"])
        finished.wait(timeout=5)  # ensure poll() is not None before the call

        state = _state()
        state[app_module._ACTIVE_PROC_KEY] = finished
        with patch.object(app_module.st, "session_state", state):
            result = app_module._launch_guarded(
                [sys.executable, "-c", "print('runs fine')"], os.environ.copy(), _Box(),
            )
        assert result is not None
        rc, log = result
        assert rc == 0
        assert "runs fine" in log
        # slot cleared after the guarded call completes
        assert state[app_module._ACTIVE_PROC_KEY] is None

    def test_no_prior_proc_allows_launch(self, app_module):
        import os

        class _Box:
            def code(self, *a, **k):
                return None

        state = _state()  # _active_run_proc defaults to None via _state()/_DEFAULTS shape
        state[app_module._ACTIVE_PROC_KEY] = None
        with patch.object(app_module.st, "session_state", state):
            result = app_module._launch_guarded(
                [sys.executable, "-c", "print('ok')"], os.environ.copy(), _Box(),
            )
        assert result is not None
        assert result[0] == 0


class TestBucketLabels:
    """Medium (i18n): the user-facing findings table must show a localized bucket
    label, not the raw engine key ('non_compliant')."""

    def test_findings_rows_localize_bucket(self, app_module):
        rows = app_module._findings_to_rows(
            [{"element_id": "e1", "parameter": "Department"}], "non_compliant"
        )
        assert rows[0]["bucket"] == "Không đạt"

    def test_unknown_bucket_falls_back_to_key(self, app_module):
        rows = app_module._findings_to_rows([{"element_id": "e1"}], "weird_key")
        assert rows[0]["bucket"] == "weird_key"


class TestPendingApprovalsCount:
    """M11 — `_pending_approvals_count` drives the sidebar badge. Must count
    only not-yet-applied records, and must tolerate a missing dir or a
    corrupt JSON record without raising (a bad file must not crash the whole
    sidebar on every rerun)."""

    def test_missing_dir_returns_zero(self, app_module, tmp_path):
        with patch.object(app_module, "RUNS_DIR", tmp_path / "no_such_runs_dir"):
            assert app_module._pending_approvals_count() == 0

    def test_counts_only_not_applied(self, app_module, tmp_path):
        approvals = tmp_path / "approvals"
        approvals.mkdir()
        (approvals / "a.json").write_text('{"applied": false}', encoding="utf-8")
        (approvals / "b.json").write_text('{"applied": true}', encoding="utf-8")
        (approvals / "c.json").write_text("{}", encoding="utf-8")  # no "applied" key -> pending
        with patch.object(app_module, "RUNS_DIR", tmp_path):
            assert app_module._pending_approvals_count() == 2

    def test_corrupt_json_record_is_skipped_not_raised(self, app_module, tmp_path):
        approvals = tmp_path / "approvals"
        approvals.mkdir()
        (approvals / "good.json").write_text('{"applied": false}', encoding="utf-8")
        (approvals / "corrupt.json").write_text("{not valid json", encoding="utf-8")
        with patch.object(app_module, "RUNS_DIR", tmp_path):
            # must not raise -- the corrupt file is skipped, the good one still counts
            assert app_module._pending_approvals_count() == 1

    def test_empty_approvals_dir_returns_zero(self, app_module, tmp_path):
        (tmp_path / "approvals").mkdir()
        with patch.object(app_module, "RUNS_DIR", tmp_path):
            assert app_module._pending_approvals_count() == 0


class TestCallWithHardTimeout:
    """M11 + Low — `_call_with_hard_timeout`'s timeout branch must raise (so
    callers' ``except (asyncio.TimeoutError, TimeoutError, FuturesTimeoutError)``
    catches it and shows the guidance message), and a normal / raising call
    must still propagate its result/exception unchanged. Also pins the Low
    fix: the worker must run on a genuine daemon thread (not a non-daemon
    ThreadPoolExecutor) so a hung call can't block interpreter shutdown.
    """

    def test_returns_result_when_within_budget(self, app_module):
        assert app_module._call_with_hard_timeout(lambda: 42, 5.0) == 42

    def test_timeout_branch_raises_timeout_error(self, app_module):
        import time

        with pytest.raises(TimeoutError):
            app_module._call_with_hard_timeout(lambda: time.sleep(5), 0.2)

    def test_timeout_error_is_caught_by_callers_except_clause(self, app_module):
        """Pins the exact except tuple used by _browse_forma_projects /
        _browse_element_groups: a plain built-in TimeoutError must satisfy it
        (asyncio.TimeoutError IS builtin TimeoutError on 3.11+)."""
        import asyncio
        import time
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        try:
            app_module._call_with_hard_timeout(lambda: time.sleep(5), 0.2)
            raised = None
        except (asyncio.TimeoutError, TimeoutError, FuturesTimeoutError) as exc:
            raised = exc
        assert raised is not None

    def test_propagates_exception_from_fn(self, app_module):
        def _boom():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            app_module._call_with_hard_timeout(_boom, 5.0)

    def test_worker_thread_is_daemon(self, app_module):
        """Low: the old ThreadPoolExecutor-based impl was NON-daemon, which
        blocks interpreter shutdown on a hung call (contradicting its own
        docstring). Assert the actual worker thread has daemon=True."""
        import threading

        seen: dict[str, bool] = {}

        def _record():
            seen["daemon"] = threading.current_thread().daemon
            return "ok"

        assert app_module._call_with_hard_timeout(_record, 5.0) == "ok"
        assert seen["daemon"] is True

    def test_abandoned_worker_does_not_prevent_return(self, app_module):
        """The leaked-thread contract: a call that overruns must still let the
        caller move on (the orphaned thread is abandoned, not joined)."""
        import time

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            app_module._call_with_hard_timeout(lambda: time.sleep(5), 0.2)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # returned near the timeout, not after the full 5s


class TestTrendWriteGating:
    """M11 — the Trend tab must not rewrite trend.md on every rerun (st.tabs
    re-executes every tab on every widget interaction anywhere in the app);
    it should only rewrite when the run set actually changed (new run_id or
    a different count), tracked via session_state['_trend_written_for']."""

    @staticmethod
    def _make_run(runs_dir, run_id: str) -> None:
        run_folder = runs_dir / run_id
        run_folder.mkdir(parents=True)
        (run_folder / "metadata.json").write_text(
            '{"started_at": "2026-01-01T00:00:00"}', encoding="utf-8"
        )
        (run_folder / "outcomes.json").write_text("{}", encoding="utf-8")

    def test_same_run_set_does_not_rewrite(self, app_module, tmp_path):
        self._make_run(tmp_path, "run-1")
        state = _state()
        write_calls = {"n": 0}

        def _fake_write_trend_report(_runs_dir):
            write_calls["n"] += 1
            (tmp_path / "trend.md").write_text("v1", encoding="utf-8")

        with patch.object(app_module, "RUNS_DIR", tmp_path), \
             patch.object(app_module.st, "session_state", state), \
             patch(
                 "bim_orchestrator.reports.write_trend_report",
                 side_effect=_fake_write_trend_report,
             ):
            app_module.render_trend_tab()
            assert write_calls["n"] == 1  # first render: file missing -> writes once
            app_module.render_trend_tab()
            assert write_calls["n"] == 1  # same run set, file present -> NOT rewritten

    def test_new_run_set_does_rewrite(self, app_module, tmp_path):
        self._make_run(tmp_path, "run-1")
        state = _state()
        write_calls = {"n": 0}

        def _fake_write_trend_report(_runs_dir):
            write_calls["n"] += 1
            (tmp_path / "trend.md").write_text("v1", encoding="utf-8")

        with patch.object(app_module, "RUNS_DIR", tmp_path), \
             patch.object(app_module.st, "session_state", state), \
             patch(
                 "bim_orchestrator.reports.write_trend_report",
                 side_effect=_fake_write_trend_report,
             ):
            app_module.render_trend_tab()
            assert write_calls["n"] == 1

        # A second run appears -- the (run_id, count) key changes -> must rewrite.
        self._make_run(tmp_path, "run-2")
        with patch.object(app_module, "RUNS_DIR", tmp_path), \
             patch.object(app_module.st, "session_state", state), \
             patch(
                 "bim_orchestrator.reports.write_trend_report",
                 side_effect=_fake_write_trend_report,
             ):
            app_module.render_trend_tab()
            assert write_calls["n"] == 2  # rewritten for the new run set


class TestSidebarRulesFallback:
    """Low (REVIEW_MULTI M11) — `_resolve_rules_selection` (shared by the
    sidebar status badge and the Run tab's pre-flight check) must fall back
    to the legacy single `rules_path` key when `rules_paths` is empty, so a
    session that only ever set the legacy key doesn't show "chưa chọn" in the
    sidebar while the Run tab's pre-flight (which already had this fallback)
    passes."""

    def test_falls_back_to_legacy_rules_path(self, app_module):
        state = _state(rules_paths=[], rules_path="config/rules.demo.yaml")
        with patch.object(app_module.st, "session_state", state):
            assert app_module._resolve_rules_selection() == ["config/rules.demo.yaml"]

    def test_rules_paths_takes_precedence_when_set(self, app_module):
        state = _state(rules_paths=["a.yaml", "b.yaml"], rules_path="legacy.yaml")
        with patch.object(app_module.st, "session_state", state):
            assert app_module._resolve_rules_selection() == ["a.yaml", "b.yaml"]

    def test_both_empty_returns_empty_list(self, app_module):
        state = _state(rules_paths=[], rules_path="")
        with patch.object(app_module.st, "session_state", state):
            assert app_module._resolve_rules_selection() == []


class TestValidateExtractedYaml:
    """M13a -- extracted YAML must be re-validated through the engine's OWN
    schema loader (RuleSet.model_validate) before it's ever written to disk.
    rules_extractor's own "executable" classification is a separate code path
    and must not be trusted as proof of schema validity."""

    _VALID_YAML = (
        "scenario: t\n"
        "target_category: Doors\n"
        "rules:\n"
        "  - id: doors.fr.present\n"
        "    parameter: Fire Rating\n"
        "    requirement: present_and_nonempty\n"
        "    description: fire rating must be present\n"
        "    severity_tag: fire_rating_violation\n"
        "    autofill: {strategy: none}\n"
    )

    def test_valid_yaml_returns_ruleset_no_error(self, app_module):
        rs, err = app_module._validate_extracted_yaml(self._VALID_YAML)
        assert err == ""
        assert rs is not None
        assert rs.scenario == "t"
        assert rs.rules[0].id == "doors.fr.present"

    def test_schema_invalid_yaml_returns_none_and_error(self, app_module):
        # missing required fields (parameter, requirement, description, ...)
        bad = "scenario: t\ntarget_category: Doors\nrules:\n  - id: doors.bad\n"
        rs, err = app_module._validate_extracted_yaml(bad)
        assert rs is None
        assert err  # a non-empty pydantic error message

    def test_malformed_yaml_syntax_returns_none_and_error(self, app_module):
        rs, err = app_module._validate_extracted_yaml("{{not: valid: yaml")
        assert rs is None
        assert err


class TestRulesetGroundingWarnings:
    """M13b -- a schema-valid rule can still cite a category or parameter that
    doesn't exist in the live OSTCatalog/param_catalog, in which case
    query_specs silently drops it at runtime (0 checks, false "run xanh"
    assurance). This must surface as an explicit, per-rule warning."""

    def _rule(self, **overrides):
        from bim_orchestrator.policies.rules_schema import Rule
        base = dict(
            id="x.rule", parameter="Fire Rating", requirement="present_and_nonempty",
            description="d", severity_tag="fire_rating_violation",
            autofill={"strategy": "none"},
        )
        base.update(overrides)
        return Rule.model_validate(base)

    def _ruleset(self, target_category, rules):
        from bim_orchestrator.policies.rules_schema import RuleSet
        return RuleSet(scenario="t", target_category=target_category, rules=rules)

    def test_unknown_category_warns_by_rule_id(self, app_module):
        rs = self._ruleset("Door Assemblies", [self._rule(id="doors.fr.bogus_cat")])
        warnings = app_module._ruleset_grounding_warnings(rs)
        assert len(warnings) == 1
        assert "doors.fr.bogus_cat" in warnings[0]
        assert "Door Assemblies" in warnings[0]
        assert "OST catalog" in warnings[0]

    def test_known_category_and_param_has_no_warning(self, app_module):
        rs = self._ruleset("Doors", [self._rule(parameter="Fire Rating")])
        assert app_module._ruleset_grounding_warnings(rs) == []

    def test_known_category_unknown_param_warns(self, app_module):
        rs = self._ruleset("Doors", [self._rule(parameter="Not A Real Param")])
        warnings = app_module._ruleset_grounding_warnings(rs)
        assert len(warnings) == 1
        assert "Not A Real Param" in warnings[0]
        assert "param_catalog" in warnings[0]

    def test_per_rule_category_overrides_target_category(self, app_module):
        # target_category resolves fine, but THIS rule's own category doesn't.
        rs = self._ruleset(
            "Doors", [self._rule(id="x.override", category="Door Assemblies")]
        )
        warnings = app_module._ruleset_grounding_warnings(rs)
        assert len(warnings) == 1
        assert "x.override" in warnings[0]

    def test_bound_parameter_used_over_canonical_name(self, app_module):
        # mirrors policies.rules_schema.fetch_name: bound_parameter wins.
        rs = self._ruleset(
            "Doors",
            [self._rule(parameter="Made Up Canonical Name", bound_parameter="Fire Rating")],
        )
        assert app_module._ruleset_grounding_warnings(rs) == []

    def test_multiple_rules_each_get_own_warning(self, app_module):
        rs = self._ruleset(
            "Doors",
            [
                self._rule(id="a", parameter="Fire Rating"),          # fine
                self._rule(id="b", parameter="Bogus Param 1"),        # warns
                self._rule(id="c", category="No Such Category"),      # warns
            ],
        )
        warnings = app_module._ruleset_grounding_warnings(rs)
        assert len(warnings) == 2
        joined = " ".join(warnings)
        assert "b" in joined and "c" in joined


class _FakeUpload:
    """Stands in for Streamlit's UploadedFile: .name + .getbuffer()."""

    def __init__(self, name: str, content: bytes = b"fake pdf bytes"):
        self.name = name
        self._content = content

    def getbuffer(self):
        return self._content


class TestRunPdfExtractionOverwriteGuard:
    """M13c -- the extraction pipeline's write step: schema-invalid YAML is
    NEVER written; a grounding warning is shown but does not by itself block
    the write; an existing file with different content requires an explicit
    overwrite confirmation before being clobbered.

    rules_extractor is an optional dependency (M14) and is not installed in
    this venv, so the whole module + the extraction_bridge client factory are
    stubbed via sys.modules / monkeypatch -- only rules_extractor's I/O
    boundary is faked; the validate/grounding/overwrite logic under test is
    the REAL app.py code.
    """

    _VALID_YAML = (
        "scenario: t\n"
        "target_category: Doors\n"
        "rules:\n"
        "  - id: doors.fr.present\n"
        "    parameter: Fire Rating\n"
        "    requirement: present_and_nonempty\n"
        "    description: fire rating must be present\n"
        "    severity_tag: fire_rating_violation\n"
        "    autofill: {strategy: none}\n"
    )

    _BOGUS_CATEGORY_YAML = (
        "scenario: t\n"
        "target_category: Door Assemblies\n"
        "rules:\n"
        "  - id: doors.fr.bogus\n"
        "    parameter: Fire Rating\n"
        "    requirement: present_and_nonempty\n"
        "    description: fire rating must be present\n"
        "    severity_tag: fire_rating_violation\n"
        "    autofill: {strategy: none}\n"
    )

    _SCHEMA_INVALID_YAML = "scenario: t\ntarget_category: Doors\nrules:\n  - id: bad\n"

    def _install_fake_rules_extractor(self, rules_yaml_by_scenario: dict[str, str | None]):
        """Registers a fake `rules_extractor` module + a fake extraction client
        factory, returning the module so the test can restore sys.modules after."""
        import types
        from dataclasses import dataclass, field

        @dataclass
        class _ScenarioResult:
            scenario: str
            rules_yaml: str | None
            review_md: str = ""
            executable: int = 1
            review: int = 0
            invalid: int = 0
            warnings: list = field(default_factory=list)

        @dataclass
        class _ConvertResult:
            scenarios: list

        @dataclass
        class _Coverage:
            title: str
            location: str
            rules: int
            status: str

        fake_mod = types.ModuleType("rules_extractor")
        fake_mod.load_contract = lambda: object()

        def _extract_sections(*a, **k):
            coverage = [_Coverage(title="S1", location="p1", rules=1, status="ok")]
            return object(), coverage

        def _convert_envelope(envelope, *, contract):
            scenarios = [
                _ScenarioResult(scenario=name, rules_yaml=yaml_text)
                for name, yaml_text in rules_yaml_by_scenario.items()
            ]
            return _ConvertResult(scenarios=scenarios)

        fake_mod.extract_sections = _extract_sections
        fake_mod.convert_envelope = _convert_envelope
        return fake_mod

    def _run(self, app_module, monkeypatch, tmp_path, rules_yaml_by_scenario, *, checkbox_value=False):
        import sys as _sys

        fake_mod = self._install_fake_rules_extractor(rules_yaml_by_scenario)
        monkeypatch.setitem(_sys.modules, "rules_extractor", fake_mod)

        class _FakeRecorder:
            def format_line(self):
                return ""

        monkeypatch.setattr(
            "bim_orchestrator.llm.usage.UsageRecorder", lambda: _FakeRecorder()
        )
        monkeypatch.setattr(
            "bim_orchestrator.llm.extraction_bridge.make_extraction_client",
            lambda recorder=None: object(),
        )
        monkeypatch.setattr(
            "bim_orchestrator.llm.extraction_bridge.extraction_model", lambda: "fake-model"
        )
        monkeypatch.setattr(app_module, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(app_module.st, "checkbox", lambda *a, **k: checkbox_value)

        upload = _FakeUpload("spec.pdf")
        app_module._run_pdf_extraction(upload, "acme", tables=False)

    def test_valid_yaml_is_written(self, app_module, monkeypatch, tmp_path):
        self._run(app_module, monkeypatch, tmp_path, {"rooms": self._VALID_YAML})
        out = tmp_path / "rules.acme_rooms.yaml"
        assert out.exists()
        assert out.read_text(encoding="utf-8") == self._VALID_YAML

    def test_schema_invalid_yaml_is_never_written(self, app_module, monkeypatch, tmp_path):
        self._run(app_module, monkeypatch, tmp_path, {"rooms": self._SCHEMA_INVALID_YAML})
        out = tmp_path / "rules.acme_rooms.yaml"
        assert not out.exists()

    def test_bogus_category_warns_and_still_writes_without_conflict(
        self, app_module, monkeypatch, tmp_path
    ):
        # No pre-existing file -> no overwrite prompt needed; a grounding
        # warning alone must not block the write (it's advisory, not a hard
        # gate -- only the overwrite guard and schema validity block a write).
        self._run(app_module, monkeypatch, tmp_path, {"rooms": self._BOGUS_CATEGORY_YAML})
        out = tmp_path / "rules.acme_rooms.yaml"
        assert out.exists()

    def test_existing_different_file_blocked_without_confirmation(
        self, app_module, monkeypatch, tmp_path
    ):
        out = tmp_path / "rules.acme_rooms.yaml"
        out.write_text("scenario: old\ntarget_category: Doors\nrules: []\n", encoding="utf-8")
        self._run(
            app_module, monkeypatch, tmp_path, {"rooms": self._VALID_YAML},
            checkbox_value=False,
        )
        # unchanged -- the new content must NOT have clobbered the old file
        assert out.read_text(encoding="utf-8") == "scenario: old\ntarget_category: Doors\nrules: []\n"

    def test_existing_different_file_written_when_confirmed(
        self, app_module, monkeypatch, tmp_path
    ):
        out = tmp_path / "rules.acme_rooms.yaml"
        out.write_text("scenario: old\ntarget_category: Doors\nrules: []\n", encoding="utf-8")
        self._run(
            app_module, monkeypatch, tmp_path, {"rooms": self._VALID_YAML},
            checkbox_value=True,
        )
        assert out.read_text(encoding="utf-8") == self._VALID_YAML

    def test_existing_identical_file_written_without_confirmation(
        self, app_module, monkeypatch, tmp_path
    ):
        # Same content -> not a real conflict, no confirmation needed.
        out = tmp_path / "rules.acme_rooms.yaml"
        out.write_text(self._VALID_YAML, encoding="utf-8")
        self._run(
            app_module, monkeypatch, tmp_path, {"rooms": self._VALID_YAML},
            checkbox_value=False,
        )
        assert out.read_text(encoding="utf-8") == self._VALID_YAML


class TestGuardImportedRuleset:
    """A-01 (2026-07-25 live review) — the IDS-import tab wrote straight to
    ``config/`` behind nothing but Pydantic's shape check.

    That tab explicitly invites files authored in OTHER tools ("import from
    Solibri, ..."), which makes it the least trusted input in the product and
    the only authoring surface with no save-time guards. An external IDS
    naming a read-only built-in as a ``set_parameter`` target landed on disk
    under a green "Da luu". The runtime net still refused the write (it
    degrades to Path A), so no model was ever harmed — the defect is that the
    tool was WRONG ABOUT WHAT IT HAD JUST SAVED.

    P1-05 closed two bypasses of the read-only guard earlier the same day and
    missed this one, which is the argument for the guard chain living in ONE
    named function instead of being re-typed at each call site.
    """

    @staticmethod
    def _ruleset(**rule_overrides):
        rule = {
            "id": "ids.door.mark",
            "category": "Doors",
            "parameter": "Mark",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "description": "imported from IDS",
            "fixability": "manual",
            "autofill": {"strategy": "none"},
            "remediation": {"action": "create_acc_issue"},
        }
        rule.update(rule_overrides)
        return {"scenario": "imported", "target_category": ["Doors"], "rules": [rule]}

    def test_clean_import_passes_and_is_returned_guarded(self, app_module):
        guarded, errors = app_module._guard_imported_ruleset(self._ruleset())
        assert errors == []
        assert len(guarded["rules"]) == 1
        assert guarded["scenario"] == "imported"

    def test_read_only_write_target_is_refused(self, app_module):
        # The headline case: an outside tool asks us to write a built-in that
        # Revit will not let anyone write. Before the fix this saved happily.
        _, errors = app_module._guard_imported_ruleset(
            self._ruleset(
                parameter="Area",
                requirement="numeric_compare",
                operator=">=",
                threshold=1.0,
                fixability="auto",
                autofill={"strategy": "infer_from_name"},
                remediation={"action": "set_parameter", "target": "instance"},
            )
        )
        assert errors, "a read-only write target was accepted"
        assert any("ids.door.mark" in e for e in errors)

    def test_error_message_names_the_offending_rule(self, app_module):
        # An IDS file can carry hundreds of rules; "validation failed" with no
        # id is not actionable for someone who has to go fix the source file.
        _, errors = app_module._guard_imported_ruleset(
            self._ruleset(id="ids.bad.regex", requirement="matches_regex",
                          pattern="([unclosed")
        )
        assert errors
        assert all("ids.bad.regex" in e for e in errors)

    def test_unnamed_rule_still_gets_a_locator(self, app_module):
        _, errors = app_module._guard_imported_ruleset(
            self._ruleset(id="", requirement="matches_regex", pattern="([unclosed")
        )
        assert errors
        assert any("rules[0]" in e for e in errors)

    def test_guards_are_applied_not_merely_checked(self, app_module):
        # enforce_unique_autofix REWRITES the rule (unique_in_set implies an
        # auto fixability). The returned ruleset must be the guarded one — if
        # the caller wrote `data` instead of the result, the guards would run
        # and then be thrown away.
        guarded, errors = app_module._guard_imported_ruleset(
            self._ruleset(requirement="unique_in_set", fixability="manual")
        )
        assert errors == []
        assert guarded["rules"][0]["fixability"] == "auto"

    def test_ruleset_category_context_reaches_the_guard(self, app_module):
        # P1-05: a rule with no `category` inherits the ruleset's
        # target_category. Without passing that down, the catalog guard looks
        # up an empty category and waves everything through.
        data = self._ruleset(
            category="",
            parameter="Area",
            requirement="numeric_compare",
            operator=">=",
            threshold=1.0,
            fixability="auto",
            autofill={"strategy": "infer_from_name"},
            remediation={"action": "set_parameter", "target": "instance"},
        )
        _, errors = app_module._guard_imported_ruleset(data)
        assert errors, "read-only target passed because category context was lost"

    def test_empty_ruleset_is_not_an_error(self, app_module):
        guarded, errors = app_module._guard_imported_ruleset(
            {"scenario": "empty", "target_category": ["Doors"], "rules": []}
        )
        assert errors == []
        assert guarded["rules"] == []


class TestExtractionAuthoringIsGuarded:
    """P1-AUTHOR-01 — extraction paths wrote semantically-invalid rules.

    A-01 closed the IDS import tab, but the fix was not generalised. Pydantic
    validates SHAPE only: it never compiles a regex, so an extracted rule with
    `pattern: "["` was written to `config/` under a green "saved" and only fell
    over at audit time — where every affected element lands in `manual_review`.
    The pipeline neither crashes nor mis-writes, which is what makes it nasty:
    the operator reads a flood of manual review as a MODEL-data problem rather
    than a broken rule, and the automation quietly stops paying for itself.

    The guard now lives at the converter's own gate (`_split_by_status`), which
    every persisting surface already passes through — Rule Builder save,
    Extraction Review and PDF extraction — so one check covers all three and a
    semantic failure lands in the `invalid` bucket they already refuse to write.
    """

    @staticmethod
    def _envelope(**rule_overrides):
        rule = {
            "id": "doors.mark.pattern",
            "category": "Doors",
            "parameter": "Mark",
            "requirement": "matches_regex",
            "pattern": "^D-[0-9]+$",
            "severity_tag": "naming_violation",
            "description": "Door marks follow D-<n>",
            "fixability": "manual",
            "extraction_meta": {
                "confidence": 1.0, "source_text": "spec",
                "source_location": "p1", "execution_status": "executable",
            },
        }
        rule.update(rule_overrides)
        return {"scenario": "guard_probe", "target_category": "Doors", "rules": [rule]}

    def _split(self, app_module, envelope):
        jty = app_module._load_json_to_yaml_module()
        return jty._split_by_status(envelope)

    def test_broken_regex_is_refused_not_written(self, app_module):
        executable, _review, invalid, _warnings = self._split(
            app_module, self._envelope(pattern="[")
        )
        assert executable == [], "a rule with an uncompilable regex was accepted"
        assert invalid, "the broken rule did not land in the invalid bucket"
        assert "pattern" in invalid[0]["error"]

    def test_the_error_names_the_rule_so_the_source_can_be_fixed(self, app_module):
        _executable, _review, invalid, _warnings = self._split(
            app_module, self._envelope(pattern="([unclosed")
        )
        assert invalid[0]["raw"]["id"] == "doors.mark.pattern"

    def test_a_valid_rule_still_converts(self, app_module):
        # Guard: the gate must not become "extraction is broken".
        executable, _review, invalid, _warnings = self._split(
            app_module, self._envelope()
        )
        assert invalid == []
        assert len(executable) == 1

    def test_extraction_save_refuses_to_write_yaml_for_a_broken_rule(
        self, app_module, tmp_path, monkeypatch
    ):
        """End of the wire: the SAVE must not report success. This is the
        assertion that matters — the earlier ones only prove the bucket."""
        import json as _json

        # The converter is loaded BY PATH from REPO_ROOT on every call, so grab
        # it while REPO_ROOT is still real, then redirect only the writes.
        jty = app_module._load_json_to_yaml_module()
        monkeypatch.setattr(app_module, "_load_json_to_yaml_module", lambda: jty)
        monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        result = app_module._save_extraction_payload(
            _json.dumps(self._envelope(pattern="["))
        )
        assert result["yaml_paths"] == [], "a broken rule reached config/"
        assert result["errors"], "the save reported no problem"
        assert not list((tmp_path / "config").glob("rules.*.yaml"))

    def test_extraction_save_still_works_for_a_valid_rule(
        self, app_module, tmp_path, monkeypatch
    ):
        import json as _json

        # The converter is loaded BY PATH from REPO_ROOT on every call, so grab
        # it while REPO_ROOT is still real, then redirect only the writes.
        jty = app_module._load_json_to_yaml_module()
        monkeypatch.setattr(app_module, "_load_json_to_yaml_module", lambda: jty)
        monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        result = app_module._save_extraction_payload(_json.dumps(self._envelope()))
        assert result["errors"] == []
        assert result["yaml_paths"]
