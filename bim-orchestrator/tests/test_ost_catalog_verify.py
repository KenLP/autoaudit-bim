"""Tests for policies.ost_catalog_verify (Task #1 post-v1.3).

Two layers:
  * Unit tests with fabricated mini-catalogs + tiny Revit/Forma probes
    that return scripted results. Lets us assert each bucket
    (present/absent/null/skipped) independently.
  * Real-catalog smoke: load the on-disk ``ost_catalog.yaml`` and run
    verify() with mocked probes — confirms the report renders without
    crashing on every entry the catalog actually holds. The count is read
    from the catalog, never written down: these comments said "61" for the
    two releases after it became 63 (L-14).
"""

from __future__ import annotations

from typing import Any

import pytest

from bim_orchestrator.policies.ost_catalog import CatalogEntry, OSTCatalog
from bim_orchestrator.policies.ost_catalog_verify import (
    CatalogVerifyReport,
    verify_catalog,
)


def _entry(key, display, ost, *, aecdm_label=..., discipline="architecture"):
    if aecdm_label is ...:
        aecdm_label = display
    return CatalogEntry(
        key=key, display=display, ost=ost,
        aecdm_label=aecdm_label, discipline=discipline, aliases=[],
    )


@pytest.fixture
def mini_catalog() -> OSTCatalog:
    return OSTCatalog([
        _entry("walls", "Walls", "OST_Walls"),
        _entry("doors", "Doors", "OST_Doors"),
        _entry("trusses", "Trusses", "OST_StructuralTruss",
               aecdm_label=None, discipline="structure"),
    ])


# ---------------------------------------------------------------------------
# Mock probes
# ---------------------------------------------------------------------------


class FakeRevit:
    """list_categories returns whatever we hand it."""

    def __init__(self, categories: list[dict[str, Any]]):
        self.categories = categories

    async def list_categories(self) -> list[dict[str, Any]]:
        return list(self.categories)


class FakeForma:
    """query_elements: returns [] for known labels, raises for unknown.

    Pair with a callable that returns "ok" / "fail" so tests can script
    per-label behaviour.
    """

    def __init__(self, behaviour: dict[str, str]):
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def query_elements(
        self, element_group_id: str, category: str
    ) -> list[dict[str, Any]]:
        self.calls.append(category)
        if self.behaviour.get(category) == "fail":
            raise RuntimeError(f"AECDM rejected category: {category}")
        return []


# ---------------------------------------------------------------------------
# Skip semantics
# ---------------------------------------------------------------------------


class TestSkipBehaviour:
    @pytest.mark.asyncio
    async def test_both_backends_skipped(self, mini_catalog: OSTCatalog):
        report = await verify_catalog(mini_catalog)
        assert report.revit_skipped is True
        assert report.aecdm_skipped is True
        for v in report.verdicts:
            assert v.revit_verdict == "skipped"
            assert v.aecdm_verdict == "skipped"
        assert report.recommendations == []

    @pytest.mark.asyncio
    async def test_forma_skipped_when_no_egid(self, mini_catalog: OSTCatalog):
        forma = FakeForma({})
        report = await verify_catalog(
            mini_catalog, forma_mcp=forma, element_group_id=None
        )
        assert report.aecdm_skipped is True
        assert forma.calls == []  # never probed


# ---------------------------------------------------------------------------
# Revit probe
# ---------------------------------------------------------------------------


class TestRevitProbe:
    @pytest.mark.asyncio
    async def test_marks_present_entries_with_canonical_shape(
        self, mini_catalog: OSTCatalog
    ):
        """Canonical shape from RevitAddin: {builtInCategory, instanceCount}."""
        revit = FakeRevit([
            {"id": -2000011, "name": "Walls",
             "builtInCategory": "OST_Walls", "instanceCount": 42},
            {"id": -2000023, "name": "Doors",
             "builtInCategory": "OST_Doors", "instanceCount": 15},
        ])
        report = await verify_catalog(mini_catalog, revit_mcp=revit)
        by_key = {v.key: v for v in report.verdicts}
        assert by_key["walls"].revit_verdict == "present"
        assert by_key["walls"].revit_count == 42
        assert by_key["doors"].revit_verdict == "present"
        assert by_key["doors"].revit_count == 15
        # Trusses not in model → absent
        assert by_key["trusses"].revit_verdict == "absent"
        assert by_key["trusses"].revit_count == 0

    @pytest.mark.asyncio
    async def test_marks_present_entries_with_legacy_shape(
        self, mini_catalog: OSTCatalog
    ):
        """Back-compat: also accept {enum, count} so older fixtures still work."""
        revit = FakeRevit([
            {"name": "Walls", "enum": "OST_Walls", "count": 7},
        ])
        report = await verify_catalog(mini_catalog, revit_mcp=revit)
        walls = next(v for v in report.verdicts if v.key == "walls")
        assert walls.revit_verdict == "present"
        assert walls.revit_count == 7

    @pytest.mark.asyncio
    async def test_revit_probe_failure_falls_back_to_skip(
        self, mini_catalog: OSTCatalog
    ):
        class BrokenRevit:
            async def list_categories(self):
                raise RuntimeError("transport_error")
        report = await verify_catalog(mini_catalog, revit_mcp=BrokenRevit())
        assert report.revit_skipped is True


# ---------------------------------------------------------------------------
# AECDM probe
# ---------------------------------------------------------------------------


class TestAecdmProbe:
    @pytest.mark.asyncio
    async def test_known_label_marks_present(self, mini_catalog: OSTCatalog):
        forma = FakeForma({"Walls": "ok", "Doors": "ok", "Trusses": "ok"})
        report = await verify_catalog(
            mini_catalog, forma_mcp=forma, element_group_id="eg-test"
        )
        by_key = {v.key: v for v in report.verdicts}
        assert by_key["walls"].aecdm_verdict == "present"
        # Trusses has aecdm_label=None — the fallback probes the display
        # label "Trusses", which our fake says is OK.
        assert by_key["trusses"].aecdm_verdict == "present"
        assert by_key["trusses"].aecdm_label is None

    @pytest.mark.asyncio
    async def test_fillable_recommendation_for_null_label(
        self, mini_catalog: OSTCatalog
    ):
        # Trusses aecdm_label=None but display "Trusses" probes OK
        forma = FakeForma({"Walls": "ok", "Doors": "ok", "Trusses": "ok"})
        report = await verify_catalog(
            mini_catalog, forma_mcp=forma, element_group_id="eg"
        )
        fillable = report.aecdm_fillable()
        assert len(fillable) == 1
        assert fillable[0].key == "trusses"
        # Recommendation mentions the suggested label
        joined = "\n".join(report.recommendations)
        assert "trusses" in joined
        assert "aecdm_label: Trusses" in joined

    @pytest.mark.asyncio
    async def test_dead_label_recommendation(self, mini_catalog: OSTCatalog):
        # Walls fails — catalog claims AECDM exposes it but probe rejects
        forma = FakeForma({"Walls": "fail", "Doors": "ok", "Trusses": "ok"})
        report = await verify_catalog(
            mini_catalog, forma_mcp=forma, element_group_id="eg"
        )
        dead = report.aecdm_dead()
        assert len(dead) == 1
        assert dead[0].key == "walls"
        assert dead[0].aecdm_error and "AECDM rejected" in dead[0].aecdm_error
        joined = "\n".join(report.recommendations)
        assert "walls" in joined

    @pytest.mark.asyncio
    async def test_null_stays_null_when_fallback_fails(
        self, mini_catalog: OSTCatalog
    ):
        # Trusses display probe also fails → stays null_in_catalog
        forma = FakeForma({"Walls": "ok", "Doors": "ok", "Trusses": "fail"})
        report = await verify_catalog(
            mini_catalog, forma_mcp=forma, element_group_id="eg"
        )
        by_key = {v.key: v for v in report.verdicts}
        assert by_key["trusses"].aecdm_verdict == "null_in_catalog"
        # No fillable recommendation for this case
        assert by_key["trusses"] not in report.aecdm_fillable()


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    @pytest.mark.asyncio
    async def test_renders_without_crashing(self, mini_catalog: OSTCatalog):
        revit = FakeRevit([
            {"name": "Walls", "builtInCategory": "OST_Walls", "instanceCount": 1},
        ])
        forma = FakeForma({"Walls": "ok", "Doors": "fail", "Trusses": "ok"})
        report = await verify_catalog(
            mini_catalog,
            revit_mcp=revit, forma_mcp=forma, element_group_id="eg",
        )
        md = report.render_markdown()
        # Spot-checks — header + table + recommendations all present
        assert "# OST Catalog verification report" in md
        assert "| key |" in md
        assert "OST_Walls" in md
        assert "Recommendations" in md  # we have at least 1 (fillable + dead)

    @pytest.mark.asyncio
    async def test_skipped_sections_say_skipped(self, mini_catalog: OSTCatalog):
        report = await verify_catalog(mini_catalog)
        md = report.render_markdown()
        assert "skipped" in md.lower()


# ---------------------------------------------------------------------------
# Real catalog smoke
# ---------------------------------------------------------------------------


class TestRealCatalogSmoke:
    @pytest.mark.asyncio
    async def test_real_catalog_runs_clean(self):
        catalog = OSTCatalog.load()
        # No backends → everything skipped. We're just verifying the
        # real catalog doesn't trip any per-entry crash, whatever its size.
        report = await verify_catalog(catalog)
        assert len(report.verdicts) == len(catalog.entries)
        md = report.render_markdown()
        # Every entry should appear in the table by its key
        for e in catalog.entries:
            assert e.key in md
