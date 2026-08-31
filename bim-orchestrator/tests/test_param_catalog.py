"""Tests for the Parameter Catalog loader (policies/param_catalog.py).

Exercises the real shipped ``config/param_catalog.2027.yaml`` so the tests double
as a contract check on the catalog itself (every assertion below is a live finding
from R27 Snowdon).
"""

from __future__ import annotations

import textwrap

import pytest

from bim_orchestrator.policies import param_catalog as pc
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.param_catalog import ParamCatalog, load_param_catalog


@pytest.fixture()
def cat() -> ParamCatalog:
    return ParamCatalog.load("2027")


# ---- load + shape ------------------------------------------------------


def test_loads_2027(cat: ParamCatalog) -> None:
    assert cat.revit_version == "2027"
    assert cat.provenance_tier == "mcp-probe"
    osts = {c.ost for c in cat.categories}
    assert {"OST_Walls", "OST_StairsRuns", "OST_Rooms", "OST_Stairs", "OST_Ramps"} <= osts
    # structural (loadable, common built-in params only)
    assert {"OST_StructuralFraming", "OST_StructuralColumns", "OST_StructuralFoundation"} <= osts
    # mep system families (hvac ducts + plumbing pipes)
    assert {"OST_DuctCurves", "OST_FlexDuctCurves", "OST_PipeCurves"} <= osts


def test_pipe_plumbing_params(cat: ParamCatalog) -> None:
    pipes = cat.category("OST_PipeCurves")
    assert pipes is not None and pipes.family_kind == "system"
    fu = cat.resolve_param("OST_PipeCurves", "Fixture Units")
    assert fu is not None and fu.dimension == "number" and fu.needs_unit is False
    # System Type writable (assignable), Pipe Segment writable (schedule swap).
    assert cat.is_write_target("OST_PipeCurves", "System Type") is True
    assert cat.is_write_target("OST_PipeCurves", "Pipe Segment") is True
    assert cat.dimension_of("OST_PipeCurves", "Invert Elevation") == "length"


def test_duct_is_system_and_metric_length_semantic(cat: ParamCatalog) -> None:
    ducts = cat.category("OST_DuctCurves")
    assert ducts is not None and ducts.family_kind == "system"
    # Metric model shows "Length" unit-less ("102"); dimension set semantically.
    length = cat.resolve_param("OST_DuctCurves", "Length")
    assert length is not None and length.dimension == "length"
    diam = cat.resolve_param("OST_DuctCurves", "Diameter")
    assert diam is not None and diam.binding == "instance" and diam.writable is True
    # System assignment is a writable reference (System Type), name a read-only text.
    assert cat.is_write_target("OST_DuctCurves", "System Type") is True
    assert cat.is_write_target("OST_DuctCurves", "System Name") is False


def test_load_cached(tmp_path) -> None:
    a = load_param_catalog("2027")
    b = load_param_catalog("2027")
    assert a is b  # same object from cache
    pc.clear_cache()


def test_bad_version_rejected(tmp_path) -> None:
    p = tmp_path / "param_catalog.x.yaml"
    p.write_text("version: 99\ncategories: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="version mismatch"):
        ParamCatalog.load(path=p)


# ---- resolution: OST + key + case-insensitive --------------------------


def test_resolve_by_ost_and_key(cat: ParamCatalog) -> None:
    by_ost = cat.resolve_param("OST_Walls", "Fire Rating")
    by_key = cat.resolve_param("walls", "Fire Rating")
    assert by_ost is not None and by_ost is by_key


def test_param_name_case_insensitive(cat: ParamCatalog) -> None:
    assert cat.resolve_param("OST_Walls", "fire rating") is not None
    assert cat.resolve_param("OST_Walls", "  FIRE RATING  ") is not None


def test_unknown_param_and_category(cat: ParamCatalog) -> None:
    assert cat.resolve_param("OST_Walls", "Totally Made Up") is None
    assert cat.resolve_param("OST_Nonexistent", "Mark") is None
    assert cat.params_for("OST_Nonexistent") == []


# ---- the four wins -----------------------------------------------------


def test_fire_rating_is_writable_type_text(cat: ParamCatalog) -> None:
    spec = cat.resolve_param("OST_Walls", "Fire Rating")
    assert spec is not None
    assert spec.binding == "type"
    assert spec.writable is True
    assert spec.dimension == "text"
    assert spec.is_write_target is True
    assert spec.needs_unit is False


def test_readonly_geometry_refused_as_write_target(cat: ParamCatalog) -> None:
    # wall Width is a read-only TYPE length → checkable, never a write target.
    spec = cat.resolve_param("OST_Walls", "Width")
    assert spec is not None and spec.writable is False
    assert spec.dimension == "length" and spec.needs_unit is True
    assert cat.is_write_target("OST_Walls", "Width") is False


def test_rename_only_is_a_write_target(cat: ParamCatalog) -> None:
    # Family/Type Name are read-only as params but renamable (K19).
    fam = cat.resolve_param("OST_Walls", "Family Name")
    assert fam is not None and fam.writable is False and fam.rename_only is True
    assert fam.is_write_target is True
    assert cat.is_write_target("OST_Walls", "Type Name") is True


def test_unknown_write_target_is_none(cat: ParamCatalog) -> None:
    # None (unknown) is distinct from False (known + read-only).
    assert cat.is_write_target("OST_Walls", "Bogus") is None


def test_actual_run_width_instance_writable_length(cat: ParamCatalog) -> None:
    spec = cat.resolve_param("OST_StairsRuns", "Actual Run Width")
    assert spec is not None
    assert spec.binding == "instance"
    assert spec.writable is True
    assert spec.dimension == "length"
    assert spec.needs_unit is True


def test_stairs_type_minimum_run_width(cat: ParamCatalog) -> None:
    # The type-level egress constraint param (writable on the Stairs type).
    assert cat.binding_of("OST_Stairs", "Minimum Run Width") == "type"
    assert cat.is_write_target("OST_Stairs", "Minimum Run Width") is True


def test_ramp_accessibility_params(cat: ParamCatalog) -> None:
    width = cat.resolve_param("OST_Ramps", "Width")
    assert width is not None and width.binding == "instance" and width.writable is True
    slope = cat.resolve_param("OST_Ramps", "Ramp Max Slope (1/x)")
    assert slope is not None and slope.dimension == "number" and slope.needs_unit is False


def test_dimension_and_binding_helpers(cat: ParamCatalog) -> None:
    assert cat.dimension_of("OST_Walls", "Area") == "area"
    assert cat.binding_of("OST_Walls", "Mark") == "instance"
    assert cat.dimension_of("OST_Walls", "Nope") is None


# ---- category-level facts ---------------------------------------------


def test_structural_is_loadable(cat: ParamCatalog) -> None:
    framing = cat.category("OST_StructuralFraming")
    assert framing is not None and framing.family_kind == "loadable"
    # profile dims (b/h) are family-authored → deliberately NOT catalogued.
    assert cat.resolve_param("OST_StructuralFraming", "h") is None
    assert cat.resolve_param("OST_StructuralFraming", "b") is None


def test_angle_dimension_needs_unit(cat: ParamCatalog) -> None:
    spec = cat.resolve_param("OST_StructuralFraming", "Cross-Section Rotation")
    assert spec is not None and spec.dimension == "angle"
    assert spec.needs_unit is True  # angle carries a physical unit


def test_foundation_width_is_type_writable(cat: ParamCatalog) -> None:
    # instance Width is read-only; only the type-writable Width is catalogued.
    spec = cat.resolve_param("OST_StructuralFoundation", "Width")
    assert spec is not None
    assert spec.binding == "type" and spec.writable is True
    assert cat.is_write_target("OST_StructuralFoundation", "Foundation Thickness") is True


def test_doors_windows_loadable_basics(cat: ParamCatalog) -> None:
    # F1b: Doors/Windows are loadable but carry common built-in size/identity params.
    doors = cat.category("OST_Doors")
    assert doors is not None and doors.family_kind == "loadable"
    w = cat.resolve_param("OST_Doors", "Width")
    assert w is not None and w.binding == "type" and w.writable is True and w.dimension == "length"
    assert cat.resolve_param("OST_Doors", "Fire Rating") is not None
    # the true clear-opening width is NOT a built-in → not catalogued (use ✏️ Khác).
    assert cat.resolve_param("OST_Doors", "Clear Opening Width") is None
    win = cat.resolve_param("OST_Windows", "Sill Height")
    assert win is not None and win.dimension == "length"


def test_rooms_have_no_type(cat: ParamCatalog) -> None:
    rooms = cat.category("OST_Rooms")
    assert rooms is not None and rooms.has_type is False
    assert all(p.binding == "instance" for p in rooms.params)


def test_params_for_is_dropdown_source(cat: ParamCatalog) -> None:
    params = cat.params_for("OST_StairsRuns")
    assert len(params) > 5
    assert any(p.name == "Actual Run Width" for p in params)


# ---- integration with ost_catalog (the intended join) ------------------


def test_join_through_ost_catalog(cat: ParamCatalog) -> None:
    # A rule names a label; OSTCatalog → OST; param_catalog → the param facts.
    ost = OSTCatalog.load().resolve("Tường", backend="revit")  # Vietnamese alias
    assert ost == "OST_Walls"
    assert cat.resolve_param(ost, "Fire Rating") is not None


# ---- a tiny hand-written catalog still validates -----------------------


def test_minimal_catalog_roundtrips(tmp_path) -> None:
    p = tmp_path / "param_catalog.test.yaml"
    p.write_text(
        textwrap.dedent(
            """
            version: 1
            revit_version: "test"
            provenance_tier: mcp-probe
            categories:
              - key: walls
                ost: OST_Walls
                has_type: true
                params:
                  - { name: Mark, storage: string, binding: instance, writable: true, dimension: text }
            """
        ),
        encoding="utf-8",
    )
    c = ParamCatalog.load(path=p)
    assert c.resolve_param("OST_Walls", "mark") is not None
    assert c.is_write_target("OST_Walls", "Mark") is True
