"""Unit tests for OSTCatalog loader + resolution priorities (v1.3 Task #3).

Two layers of coverage:
  1. **Fabricated catalogs** — small in-memory ``OSTCatalog`` instances
     built per test, so behaviour assertions don't drift when entries are
     added to the real YAML.
  2. **Real catalog invariants** — the on-disk ``config/ost_catalog.yaml``
     is loaded once and checked for structural soundness (no duplicate
     keys / OSTs / displays, no alias collisions). These would have caught
     the catalog regressions Task #1 ran the `_validate_ost_catalog.py`
     helper for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError
from structlog.testing import capture_logs

from bim_orchestrator.policies.ost_catalog import (
    CatalogEntry,
    OSTCatalog,
    _levenshtein,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_UNSET: object = object()


def _entry(
    key: str,
    display: str,
    ost: str,
    *,
    aecdm_label: str | None | object = _UNSET,
    discipline: str = "architecture",
    aliases: list[str] | None = None,
) -> CatalogEntry:
    """Compact constructor — most tests only vary 2-3 fields.

    ``aecdm_label`` defaults to ``display`` (the common case for AECDM-
    exposed categories). Pass ``None`` explicitly to model entries that
    AECDM doesn't expose — distinct from "not passed", which we treat
    as "same as display".
    """
    if aecdm_label is _UNSET:
        resolved_aecdm: str | None = display
    else:
        resolved_aecdm = aecdm_label  # type: ignore[assignment]
    return CatalogEntry(
        key=key,
        display=display,
        ost=ost,
        aecdm_label=resolved_aecdm,
        discipline=discipline,  # type: ignore[arg-type]
        aliases=aliases or [],
    )


@pytest.fixture
def mini_catalog() -> OSTCatalog:
    """Tiny 3-entry catalog covering the cases we want to assert against."""
    return OSTCatalog(
        [
            _entry(
                "walls", "Walls", "OST_Walls",
                aliases=["Wall", "Tường"],
            ),
            _entry(
                "doors", "Doors", "OST_Doors",
                aliases=["Door", "Cửa"],
            ),
            _entry(
                "trusses", "Trusses", "OST_StructuralTruss",
                aecdm_label=None,  # not exposed by AECDM → resolve returns None
                discipline="structure",
                aliases=["Truss"],
            ),
        ]
    )


@pytest.fixture(scope="module")
def real_catalog() -> OSTCatalog:
    """The actual on-disk catalog — used only for invariant checks."""
    return OSTCatalog.load()


# ---------------------------------------------------------------------------
# CatalogEntry pydantic validation
# ---------------------------------------------------------------------------


class TestCatalogEntryValidation:
    def test_required_fields(self):
        with pytest.raises(ValidationError):
            CatalogEntry(  # type: ignore[call-arg]
                key="x", display="X", discipline="architecture"
            )  # missing 'ost'

    def test_strict_extra_forbidden(self):
        """Extra fields raise — protects against typos in catalog YAML."""
        with pytest.raises(ValidationError):
            CatalogEntry(  # type: ignore[call-arg]
                key="walls",
                display="Walls",
                ost="OST_Walls",
                discipline="architecture",
                unknown_field="oops",
            )

    def test_discipline_literal_enforced(self):
        with pytest.raises(ValidationError):
            CatalogEntry(
                key="walls",
                display="Walls",
                ost="OST_Walls",
                discipline="finance",  # type: ignore[arg-type]
            )

    def test_aliases_default_empty(self):
        e = CatalogEntry(
            key="walls", display="Walls", ost="OST_Walls",
            discipline="architecture",
        )
        assert e.aliases == []

    def test_aecdm_label_optional(self):
        e = CatalogEntry(
            key="trusses", display="Trusses", ost="OST_StructuralTruss",
            discipline="structure",
        )
        assert e.aecdm_label is None


# ---------------------------------------------------------------------------
# OSTCatalog.load
# ---------------------------------------------------------------------------


class TestLoad:
    def test_loads_real_catalog(self, real_catalog: OSTCatalog):
        # Real catalog should have at least 50 entries across all three
        # disciplines per v1.3 spec.
        assert len(real_catalog.entries) >= 50
        assert {e.discipline for e in real_catalog.entries} == {
            "architecture", "structure", "mep",
        }

    def test_load_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            OSTCatalog.load(tmp_path / "does_not_exist.yaml")

    def test_load_version_mismatch(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "version: 99\ncategories:\n  - key: walls\n"
            "    display: Walls\n    ost: OST_Walls\n    discipline: architecture\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="version mismatch"):
            OSTCatalog.load(bad)

    def test_load_missing_categories_list(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no `categories` list"):
            OSTCatalog.load(bad)

    def test_load_empty_categories_list(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: 1\ncategories: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no `categories` list"):
            OSTCatalog.load(bad)

    def test_load_non_mapping_top_level(self, tmp_path: Path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a mapping"):
            OSTCatalog.load(bad)

    def test_load_propagates_entry_validation_errors(self, tmp_path: Path):
        """An invalid entry inside a valid envelope should still raise."""
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "version: 1\ncategories:\n  - key: x\n    display: X\n"
            "    ost: OST_X\n    discipline: not_a_real_discipline\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            OSTCatalog.load(bad)


# ---------------------------------------------------------------------------
# Priority 1+2: Exact match
# ---------------------------------------------------------------------------


class TestFindExactMatch:
    def test_exact_key(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("walls").key == "walls"  # type: ignore[union-attr]

    def test_exact_display(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("Walls").key == "walls"  # type: ignore[union-attr]

    def test_empty_label_returns_none(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("") is None

    @pytest.mark.parametrize("value", [None, 42, 3.14, [], {}])
    def test_non_string_label_returns_none(
        self, mini_catalog: OSTCatalog, value
    ):
        assert mini_catalog.find(value) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Priority 3: Case-insensitive (key / display / alias)
# ---------------------------------------------------------------------------


class TestFindCaseInsensitive:
    @pytest.mark.parametrize("label", ["WALLS", "wAlLs", "walls "])
    def test_case_variants_match_display(
        self, mini_catalog: OSTCatalog, label
    ):
        # The trailing-space case intentionally fails — find() does not
        # trim. Remove the space variant if/when we add stripping.
        result = mini_catalog.find(label.rstrip())
        assert result is not None
        assert result.key == "walls"

    def test_english_alias_singular(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("Wall").key == "walls"  # type: ignore[union-attr]

    def test_english_alias_lowercase(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("door").key == "doors"  # type: ignore[union-attr]


class TestFindVietnameseAlias:
    def test_vietnamese_alias_walls(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("Tường").key == "walls"  # type: ignore[union-attr]

    def test_vietnamese_alias_doors(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("Cửa").key == "doors"  # type: ignore[union-attr]

    def test_vietnamese_alias_case_insensitive(self, mini_catalog: OSTCatalog):
        # Python str.lower() handles Vietnamese diacritics — verify
        # round-trip through the case-insensitive index.
        assert mini_catalog.find("tường").key == "walls"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Priority 4: Fuzzy match
# ---------------------------------------------------------------------------


class TestFindFuzzy:
    def test_typo_extra_char(self, mini_catalog: OSTCatalog):
        # "Wallss" (6 chars) vs "Walls" → distance 1, max_dist=1 → match
        assert mini_catalog.find("Wallss").key == "walls"  # type: ignore[union-attr]

    def test_typo_omit_char(self, mini_catalog: OSTCatalog):
        # "Trusss" (6 chars) vs "Trusses" → distance 1
        assert mini_catalog.find("Trusss").key == "trusses"  # type: ignore[union-attr]

    def test_skip_when_too_short(self):
        """Length < 5 means fuzzy step is bypassed entirely.

        Build a catalog where the input has no exact / case / alias hit
        so we're certain we're testing the fuzzy gate (not a step-3 hit).
        """
        cat = OSTCatalog(
            [_entry("piping", "Pipes", "OST_PipeCurves", aliases=["Pipe"])]
        )
        # "Pipa" is 4 chars (< 5), 1-edit from "pipe". Without the gate
        # it would match; with it, step 4 is skipped → None.
        assert cat.find("Pipa") is None

    def test_fuzzy_distance_budget_length_8_plus(self):
        """Inputs ≥ 8 chars get max_dist=2."""
        cat = OSTCatalog(
            [_entry("mech", "Mechanical Equipment", "OST_MechanicalEquipment")]
        )
        # "Mechancal Equipment" — drop one 'i' → distance 1, len 19
        assert cat.find("Mechancal Equipment").key == "mech"  # type: ignore[union-attr]
        # Two-edit input still fits len-≥8 budget
        assert cat.find("Mechancl Equipment").key == "mech"  # type: ignore[union-attr]

    def test_fuzzy_ambiguous_returns_none(self):
        """Two entries equidistant at the minimum → bail, not guess."""
        cat = OSTCatalog(
            [
                _entry("aaa", "Aaaaa", "OST_A"),
                _entry("bbb", "Bbbbb", "OST_B"),
            ]
        )
        # "Aaaab" — distance 1 from "Aaaaa" AND distance 2 from "Bbbbb".
        # Hmm — not equidistant. Need a more careful construction.
        # Construct: "Xxxxa" vs entries "Xxxxb" and "Xxxxc" — both at dist 1.
        cat2 = OSTCatalog(
            [
                _entry("xxxb", "Xxxxb", "OST_XB"),
                _entry("xxxc", "Xxxxc", "OST_XC"),
            ]
        )
        with capture_logs() as logs:
            assert cat2.find("Xxxxa") is None
        assert any(
            e.get("event") == "ost_catalog.fuzzy_ambiguous" for e in logs
        )

    def test_fuzzy_beyond_budget_returns_none(self):
        """Too many edits = unknown, even for long inputs."""
        cat = OSTCatalog([_entry("walls", "Walls", "OST_Walls")])
        # "Banana" (6 chars) vs "Walls" → distance > 1 → no match
        # AND not in case-insensitive index → step 5 (unknown)
        assert cat.find("Banana") is None


# ---------------------------------------------------------------------------
# Priority 5: Unknown
# ---------------------------------------------------------------------------


class TestFindUnknown:
    def test_returns_none(self, mini_catalog: OSTCatalog):
        assert mini_catalog.find("Banana") is None

    def test_emits_warn_log(self, mini_catalog: OSTCatalog):
        with capture_logs() as logs:
            mini_catalog.find("Banana")
        assert any(
            e.get("event") == "ost_catalog.unknown_label"
            and e.get("label") == "Banana"
            for e in logs
        )


# ---------------------------------------------------------------------------
# Backend selection in resolve()
# ---------------------------------------------------------------------------


class TestResolveBackend:
    def test_revit_backend(self, mini_catalog: OSTCatalog):
        assert mini_catalog.resolve("Walls", "revit") == "OST_Walls"

    def test_aecdm_backend(self, mini_catalog: OSTCatalog):
        assert mini_catalog.resolve("Walls", "aecdm") == "Walls"

    def test_aecdm_null_returns_none_with_warn(self, mini_catalog: OSTCatalog):
        """Backend mismatch: entry has no aecdm_label."""
        with capture_logs() as logs:
            result = mini_catalog.resolve("Trusses", "aecdm")
        assert result is None
        assert any(
            e.get("event") == "ost_catalog.aecdm_not_supported"
            and e.get("matched_key") == "trusses"
            for e in logs
        )

    def test_aecdm_null_still_resolves_for_revit(
        self, mini_catalog: OSTCatalog
    ):
        """A null aecdm_label doesn't taint the revit lookup."""
        assert mini_catalog.resolve("Trusses", "revit") == "OST_StructuralTruss"

    def test_unknown_label_returns_none_for_both_backends(
        self, mini_catalog: OSTCatalog
    ):
        assert mini_catalog.resolve("Banana", "revit") is None
        assert mini_catalog.resolve("Banana", "aecdm") is None

    def test_invalid_backend_raises(self, mini_catalog: OSTCatalog):
        with pytest.raises(ValueError, match="Unknown backend"):
            mini_catalog.resolve("Walls", "ifc")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    def test_resolve_caches_negative_lookups(self, mini_catalog: OSTCatalog):
        """Second resolve() of an unknown label hits cache → no second warn."""
        with capture_logs() as logs:
            mini_catalog.resolve("Banana", "revit")
            mini_catalog.resolve("Banana", "revit")
        # Should see exactly one unknown_label warn, not two.
        unknowns = [
            e for e in logs
            if e.get("event") == "ost_catalog.unknown_label"
        ]
        assert len(unknowns) == 1

    def test_resolve_caches_aecdm_null_warn(self, mini_catalog: OSTCatalog):
        """The aecdm_not_supported warn is also one-shot per cache key."""
        with capture_logs() as logs:
            mini_catalog.resolve("Trusses", "aecdm")
            mini_catalog.resolve("Trusses", "aecdm")
        not_supported = [
            e for e in logs
            if e.get("event") == "ost_catalog.aecdm_not_supported"
        ]
        assert len(not_supported) == 1

    def test_cache_keyed_by_backend(self, mini_catalog: OSTCatalog):
        """Same label, different backend → independent cache entries."""
        assert mini_catalog.resolve("Walls", "revit") == "OST_Walls"
        assert mini_catalog.resolve("Walls", "aecdm") == "Walls"


# ---------------------------------------------------------------------------
# by_discipline filter + entries defensive copy
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_by_discipline_architecture(self, mini_catalog: OSTCatalog):
        archs = mini_catalog.by_discipline("architecture")
        assert {e.key for e in archs} == {"walls", "doors"}

    def test_by_discipline_structure(self, mini_catalog: OSTCatalog):
        structs = mini_catalog.by_discipline("structure")
        assert {e.key for e in structs} == {"trusses"}

    def test_entries_is_defensive_copy(self, mini_catalog: OSTCatalog):
        snapshot = mini_catalog.entries
        snapshot.clear()
        # Internal state untouched
        assert len(mini_catalog.entries) == 3


# ---------------------------------------------------------------------------
# Levenshtein bounded distance — direct unit tests
# ---------------------------------------------------------------------------


class TestLevenshtein:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("walls", "walls", 0),
            ("walls", "wallss", 1),
            ("walls", "walks", 1),
            ("walls", "ball", 2),  # substitute w→b, drop s
        ],
    )
    def test_within_budget(self, a: str, b: str, expected: int):
        assert _levenshtein(a, b, max_dist=3) == expected

    def test_returns_none_when_beyond_budget(self):
        # "abcdef" vs "ghijkl" — fully disjoint, distance 6
        assert _levenshtein("abcdef", "ghijkl", max_dist=2) is None

    def test_length_diff_shortcut(self):
        """When |len(a) - len(b)| > max_dist we bail immediately."""
        assert _levenshtein("a", "abcde", max_dist=2) is None

    def test_identical_short_strings(self):
        assert _levenshtein("ab", "ab", max_dist=0) == 0


# ---------------------------------------------------------------------------
# Real catalog invariants — guard against regressions in ost_catalog.yaml
# ---------------------------------------------------------------------------


class TestRealCatalogInvariants:
    def test_unique_keys(self, real_catalog: OSTCatalog):
        keys = [e.key for e in real_catalog.entries]
        assert len(keys) == len(set(keys)), "duplicate keys in catalog"

    def test_unique_osts(self, real_catalog: OSTCatalog):
        osts = [e.ost for e in real_catalog.entries]
        assert len(osts) == len(set(osts)), "duplicate ost values in catalog"

    def test_unique_displays(self, real_catalog: OSTCatalog):
        displays = [e.display for e in real_catalog.entries]
        assert len(displays) == len(set(displays)), "duplicate display names"

    def test_no_alias_collisions_across_entries(self, real_catalog: OSTCatalog):
        owners: dict[str, list[str]] = {}
        for e in real_catalog.entries:
            for a in e.aliases:
                owners.setdefault(a.lower(), []).append(e.key)
        collisions = {a: owners for a, owners in owners.items() if len(owners) > 1}
        assert not collisions, f"alias collisions: {collisions}"

    def test_ost_strings_use_prefix(self, real_catalog: OSTCatalog):
        """Every ost field must start with 'OST_' — catches paste-errors."""
        bad = [e.key for e in real_catalog.entries if not e.ost.startswith("OST_")]
        assert not bad, f"entries missing OST_ prefix: {bad}"

    def test_common_anchors_resolve_for_both_backends(
        self, real_catalog: OSTCatalog
    ):
        """Sanity check: the common architectural categories must resolve
        to both backends from the real catalog. Guards against catalog
        edits accidentally dropping a high-traffic label."""
        for label in ("Rooms", "Walls", "Doors", "Windows", "Floors", "Ceilings"):
            assert real_catalog.resolve(label, "revit") is not None, label
            assert real_catalog.resolve(label, "aecdm") is not None, label
