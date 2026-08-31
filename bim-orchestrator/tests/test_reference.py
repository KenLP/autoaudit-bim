"""Tests for reference-data matching (v1.4-K21, tiers 1–2).

A reference set is an authoritative list of allowed values; ``match`` resolves a
value to its canonical entry deterministically (exact / alias / slug+case), or
returns None when no deterministic member exists (→ Path A; fuzzy is Phase 2).
"""

from __future__ import annotations

import pytest
import yaml

from bim_orchestrator.policies.reference import (
    ReferenceSet,
    clear_cache,
    load_reference,
    normalize_reference,
)


def _set(case_sensitive: bool = False) -> ReferenceSet:
    return ReferenceSet.model_validate({
        "name": "materials",
        "case_sensitive": case_sensitive,
        "entries": [
            {"canonical": "Oak", "aliases": ["wood-oak", "white oak"]},
            {"canonical": "Steel-Brushed", "aliases": ["brushed steel", "ss-brushed"]},
            {"canonical": "Laminate-White"},
        ],
    })


class TestTier1Exact:
    def test_exact_canonical_returns_itself(self):
        assert _set().match("Oak") == "Oak"

    def test_exact_with_surrounding_whitespace(self):
        assert _set().match("  Oak  ") == "Oak"

    def test_canonical_with_no_aliases(self):
        assert _set().match("Laminate-White") == "Laminate-White"


class TestTier2AliasAndSlug:
    def test_alias_maps_to_canonical(self):
        assert _set().match("brushed steel") == "Steel-Brushed"

    def test_case_insensitive_canonical(self):
        # "oak" is not the canonical FORM ("Oak") → recognised, snaps to canonical
        assert _set().match("oak") == "Oak"

    def test_separator_variation_via_slug(self):
        # "Steel Brushed" / "steel_brushed" → slug-folds onto "Steel-Brushed"
        assert _set().match("Steel Brushed") == "Steel-Brushed"
        assert _set().match("steel_brushed") == "Steel-Brushed"

    def test_alias_case_and_separator(self):
        assert _set().match("Wood Oak") == "Oak"
        assert _set().match("WHITE OAK") == "Oak"


class TestTier3Miss:
    def test_off_list_returns_none(self):
        assert _set().match("Pine") is None
        assert _set().match("Wood - Pine") is None

    def test_none_and_blank_return_none(self):
        s = _set()
        assert s.match(None) is None
        assert s.match("") is None
        assert s.match("   ") is None


class TestCaseSensitive:
    def test_case_sensitive_rejects_wrong_case(self):
        s = _set(case_sensitive=True)
        assert s.match("Oak") == "Oak"      # exact still works
        assert s.match("oak") is None       # case matters → not a member

    def test_case_sensitive_alias_exact(self):
        s = _set(case_sensitive=True)
        assert s.match("wood-oak") == "Oak"     # alias declared lower-case
        assert s.match("Wood-Oak") is None      # different case → miss


class TestLoadReference:
    def test_load_from_file_and_cache(self, tmp_path):
        clear_cache()
        (tmp_path / "reference.palette.yaml").write_text(
            yaml.safe_dump({
                "name": "palette",
                "entries": [{"canonical": "Oak", "aliases": ["white oak"]}],
            }),
            encoding="utf-8",
        )
        ref = load_reference("palette", config_dir=tmp_path)
        assert ref.match("white oak") == "Oak"
        # second load returns the cached instance (no re-read)
        assert load_reference("palette", config_dir=tmp_path) is ref

    def test_normalize_reference_delegates_to_match(self):
        s = _set()
        assert normalize_reference("grey fabric", s) is None  # not in this set
        assert normalize_reference("brushed steel", s) == "Steel-Brushed"

    def test_demo_palette_ships_in_config(self):
        clear_cache()
        ref = load_reference("approved_materials")  # default config dir
        assert ref.match("grey fabric") == "Fabric-Grey"
        assert ref.match("Oak") == "Oak"
        assert ref.match("Pine") is None

    def test_falls_back_to_shared_config_dir(self, tmp_path):
        # A rule-pack folder without the set → falls back to the shared config/ one.
        clear_cache()
        ref = load_reference("approved_materials", tmp_path)
        assert ref.entries and ref.match("Oak") == "Oak"
        clear_cache()

    def test_pack_local_set_wins(self, tmp_path):
        # A set present in the pack folder is used (not the config/ fallback).
        clear_cache()
        (tmp_path / "reference.packonly.yaml").write_text(yaml.safe_dump({
            "name": "packonly", "entries": [{"canonical": "Foo", "aliases": ["f"]}],
        }), encoding="utf-8")
        ref = load_reference("packonly", tmp_path)
        assert ref.match("f") == "Foo"
        clear_cache()
