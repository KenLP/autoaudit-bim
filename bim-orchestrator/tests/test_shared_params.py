"""Tests for the shared-parameter conventions loader (policies/shared_params.py).

Exercises the real shipped ``config/shared_param_conventions.yaml`` so the tests
double as a contract check on the conventions file itself.
"""

from __future__ import annotations

import textwrap

import pytest

from bim_orchestrator.policies import shared_params as sp
from bim_orchestrator.policies.shared_params import (
    SharedParamConventions,
    load_shared_param_conventions,
)


@pytest.fixture()
def conv() -> SharedParamConventions:
    return SharedParamConventions.load()


# ---- load + shape ------------------------------------------------------


def test_loads_shipped_file(conv: SharedParamConventions) -> None:
    names = {c.parameter for c in conv.conventions}
    # COBie + classification + IFC Pset energy must all be present (Revit-verified
    # names: "Classification Number" + "Thermal Transmittance (U)").
    assert "COBie.Type.Category" in names
    assert "Classification Number" in names
    assert "Thermal Transmittance (U)" in names


def test_resolve_is_case_insensitive(conv: SharedParamConventions) -> None:
    c = conv.resolve("cobie.type.category")
    assert c is not None and c.parameter == "COBie.Type.Category"
    assert c.reference == "omniclass_table23"
    assert conv.resolve("nope") is None


def test_for_category_scopes(conv: SharedParamConventions) -> None:
    walls = {c.parameter for c in conv.for_category("Walls")}
    assert "Thermal Transmittance (U)" in walls      # applies_to includes Walls
    assert "COBie.Component.Space" not in walls      # only Doors/Windows/Ducts/Pipes


def test_match_intent_longest_wins(conv: SharedParamConventions) -> None:
    # "u-value" → the Revit "Thermal Transmittance (U)" param; scoped to a category.
    c = conv.match_intent("the u-value of this wall", "Walls")
    assert c is not None and c.parameter == "Thermal Transmittance (U)"
    # COBie category intent
    c2 = conv.match_intent("cobie category from the picklist", "Doors")
    assert c2 is not None and c2.parameter == "COBie.Type.Category"
    # no match → None
    assert conv.match_intent("the wall height") is None


def test_applies_empty_means_any() -> None:
    from bim_orchestrator.policies.shared_params import SharedParamConvention

    universal = SharedParamConvention(parameter="GlobalId", applies_to=())
    assert universal.applies("Walls") and universal.applies("Anything")
    scoped = SharedParamConvention(parameter="X", applies_to=("Walls",))
    assert scoped.applies("Walls") and not scoped.applies("Doors")


# ---- caching + validation ---------------------------------------------


def test_loader_caches_by_path(tmp_path) -> None:
    sp.clear_cache()
    a = load_shared_param_conventions()
    b = load_shared_param_conventions()
    assert a is b  # same cached instance


def test_version_mismatch_raises(tmp_path) -> None:
    bad = tmp_path / "shared_param_conventions.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            version: 99
            conventions:
              - parameter: X
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="version mismatch"):
        SharedParamConventions.load(path=bad)


def test_empty_conventions_raises(tmp_path) -> None:
    bad = tmp_path / "shared_param_conventions.yaml"
    bad.write_text("version: 1\nconventions: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no `conventions`"):
        SharedParamConventions.load(path=bad)


def test_extra_field_forbidden(tmp_path) -> None:
    bad = tmp_path / "shared_param_conventions.yaml"
    bad.write_text(
        "version: 1\nconventions:\n  - parameter: X\n    bogus: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):  # pydantic ValidationError (extra=forbid)
        SharedParamConventions.load(path=bad)
