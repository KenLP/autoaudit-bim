"""Unit tests for ``policies.query_specs.derive_specs`` (v1.3 Task #4).

Coverage focus:
  * Category grouping (target_category str vs list, per-rule narrow,
    out-of-scope per-rule category warn).
  * Param union across rules in a category, including ``when_param`` and
    ``other_param`` flavours.
  * Host hop detection from ``other_param`` ``host.*`` prefix.
  * Backend resolution failures (unknown label, AECDM-null) skip the
    affected category without crashing.
  * Catalog-backed display label is what lands in ``category_label``.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from bim_orchestrator.policies.ost_catalog import CatalogEntry, OSTCatalog
from bim_orchestrator.policies.query_specs import (
    QuerySpec,
    _collect_params,
    _normalize_targets,
    derive_specs,
    derive_specs_with_coverage,
)
from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill, RuleSet


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rule(
    rule_id: str,
    parameter: str,
    *,
    category: str | None = None,
    when_param: str | None = None,
    other_param: str | None = None,
) -> Rule:
    """Compact Rule constructor — fills the mandatory fields with defaults."""
    return Rule(
        id=rule_id,
        parameter=parameter,
        requirement="present_and_nonempty",
        category=category,
        when_param=when_param,
        other_param=other_param,
        severity_tag="quality_change",
        description=f"{rule_id} ({parameter})",
        autofill=RuleAutofill(strategy="none"),
    )


def _entry(
    key: str,
    display: str,
    ost: str,
    *,
    aecdm_label: str | None = "__use_display__",
    discipline: str = "architecture",
    aliases: list[str] | None = None,
) -> CatalogEntry:
    """Mirror of the helper in test_ost_catalog.py — sentinel for null."""
    if aecdm_label == "__use_display__":
        resolved = display
    else:
        resolved = aecdm_label
    return CatalogEntry(
        key=key,
        display=display,
        ost=ost,
        aecdm_label=resolved,
        discipline=discipline,  # type: ignore[arg-type]
        aliases=aliases or [],
    )


@pytest.fixture
def catalog() -> OSTCatalog:
    """Mini catalog: 3 categories — Walls, Doors, Trusses (aecdm null)."""
    return OSTCatalog(
        [
            _entry("walls", "Walls", "OST_Walls"),
            _entry("doors", "Doors", "OST_Doors"),
            _entry(
                "trusses",
                "Trusses",
                "OST_StructuralTruss",
                aecdm_label=None,
                discipline="structure",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# _normalize_targets
# ---------------------------------------------------------------------------


class TestNormalizeTargets:
    def test_single_string(self):
        assert _normalize_targets("Walls") == ["Walls"]

    def test_list(self):
        assert _normalize_targets(["Walls", "Doors"]) == ["Walls", "Doors"]

    def test_dedupes_preserving_order(self):
        assert _normalize_targets(["Walls", "Doors", "Walls"]) == ["Walls", "Doors"]

    def test_drops_empty_strings(self):
        assert _normalize_targets(["Walls", "", "Doors"]) == ["Walls", "Doors"]

    def test_empty_string(self):
        assert _normalize_targets("") == []

    def test_empty_list(self):
        assert _normalize_targets([]) == []


# ---------------------------------------------------------------------------
# _collect_params
# ---------------------------------------------------------------------------


class TestCollectParams:
    def test_basic_parameter(self):
        rules = [_rule("r1", "Department")]
        params, fh, hosts = _collect_params(rules)
        assert params == {"Department"}
        assert fh is False
        assert hosts == set()

    def test_when_param_included(self):
        rules = [_rule("r1", "Department", when_param="Occupancy")]
        params, _, _ = _collect_params(rules)
        assert params == {"Department", "Occupancy"}

    def test_other_param_non_host(self):
        rules = [_rule("r1", "Mark", other_param="Type Mark")]
        params, fh, hosts = _collect_params(rules)
        assert params == {"Mark", "Type Mark"}
        assert fh is False
        assert hosts == set()

    def test_other_param_host_prefix_triggers_follow_host(self):
        rules = [_rule("r1", "Fire Rating", other_param="host.Fire Rating")]
        params, fh, hosts = _collect_params(rules)
        assert params == {"Fire Rating"}
        assert fh is True
        assert hosts == {"Fire Rating"}

    def test_lookup_table_host_keys_hydrated(self):
        # IBC §716: the lookup table self-declares its host keys (host.Fire Rating,
        # host.Fire Function) → _collect_params loads it and adds them for the host hop.
        rule = Rule(
            id="r1", parameter="Fire Rating", requirement="relation_compare",
            category="Doors", compare_kind="fire_rating", operator=">=",
            lookup="ibc716", severity_tag="rule_violation",
            description="ibc716 lookup", autofill=RuleAutofill(strategy="none"),
        )
        _params, fh, hosts = _collect_params([rule])
        assert fh is True
        assert {"Fire Rating", "Fire Function"} <= hosts

    def test_pack_local_lookup_host_hop_needs_config_dir(self, tmp_path):
        # Medium: a lookup table beside the RULES file (not in config/) only hydrates
        # its host params when the rules-file dir is threaded in. Without it,
        # query_specs looked only in config/ → host hop never turned on → the rule
        # silently had no host.* to compare (dir mismatch vs QC, which DID resolve it).
        import yaml
        import bim_orchestrator.policies.lookup_table as lt
        (tmp_path / "lookup.packonly.yaml").write_text(yaml.safe_dump({
            "name": "packonly",
            "keys": [{"param": "host.Fire Rating", "dimension": "fire_rating"}],
            "rows": [{"when": ["2 HR"], "require": "90 min"}],
        }), encoding="utf-8")
        rule = Rule(
            id="r1", parameter="Fire Rating", requirement="relation_compare",
            category="Doors", compare_kind="fire_rating", operator=">=",
            lookup="packonly", severity_tag="rule_violation",
            description="pack-local lookup", autofill=RuleAutofill(strategy="none"),
        )
        lt.clear_cache()
        _p, fh0, hosts0 = _collect_params([rule])                 # config/ only
        assert fh0 is False and hosts0 == set()
        lt.clear_cache()
        _p, fh1, hosts1 = _collect_params([rule], config_dir=tmp_path)
        assert fh1 is True and "Fire Rating" in hosts1

    def test_lookup_table_own_keys_are_hydrated_too(self, tmp_path):
        """2026-08-18: a table may key on the element's OWN param, not the
        host's. Every shipped table keyed on the host (IBC §716), so this
        path never existed — and the first own-key table (IRC room minimums,
        keyed on a room's `Name`) sent all 30 rooms to manual_review with an
        empty operand, because `Name` was never fetched."""
        import yaml

        import bim_orchestrator.policies.lookup_table as lt

        (tmp_path / "lookup.roomsize.yaml").write_text(yaml.safe_dump({
            "name": "roomsize",
            "keys": [{"param": "Name", "dimension": "string"}],
            "rows": [{"when": ["Bedroom 1"], "require": "70"}],
        }), encoding="utf-8")
        rule = Rule(
            id="r1", parameter="Area", requirement="relation_compare",
            category="Rooms", compare_kind="numeric", operator=">=",
            lookup="roomsize", severity_tag="rule_violation",
            description="own-key lookup", autofill=RuleAutofill(strategy="none"),
        )
        lt.clear_cache()
        params, fh, hosts = _collect_params([rule], config_dir=tmp_path)
        assert "Name" in params, "the table's own key was never fetched"
        assert "Area" in params            # the rule's own parameter, unchanged
        # An own-key table must NOT switch the (expensive) host hop on.
        assert fh is False and hosts == set()
        lt.clear_cache()

    def test_host_only_prefix_ignored(self):
        # 'host.' with nothing after — treat as no host param to fetch
        # but still flip follow_host (defensive).
        rules = [_rule("r1", "Fire Rating", other_param="host.")]
        params, fh, hosts = _collect_params(rules)
        assert fh is True
        assert hosts == set()

    def test_multi_rule_union(self):
        rules = [
            _rule("r1", "Department"),
            _rule("r2", "Occupancy"),
            _rule("r3", "Department"),  # dup
        ]
        params, _, _ = _collect_params(rules)
        assert params == {"Department", "Occupancy"}

    def test_inherit_from_host_triggers_follow_host_same_name(self):
        # v1.4-K20: an inherit_from_host autofill turns on the host hop and, with
        # no explicit host_param, hydrates the rule's OWN parameter from the host.
        rule = Rule(
            id="r1", parameter="Fire Rating", requirement="present_and_nonempty",
            severity_tag="quality_change", description="inherit",
            autofill=RuleAutofill(strategy="inherit_from_host"),
        )
        params, fh, hosts = _collect_params([rule])
        assert fh is True
        assert hosts == {"Fire Rating"}

    def test_inherit_from_host_bound_rule_hydrates_bound_name(self):
        # M2 follow-up (2026-07-04): with a binding layer, the host element's
        # REAL Revit param is the bound name. The collected host param must be
        # fetch_name(rule) (bound over canonical) so the hydrated key
        # ``host.<name>`` matches what QC's _suggest reads — before this fix
        # query_specs collected ``rule.parameter`` while _suggest read the
        # bound name, and the two silently diverged for bound rules.
        rule = Rule(
            id="r1", parameter="Mã chống cháy", bound_parameter="Fire Rating",
            requirement="present_and_nonempty",
            severity_tag="quality_change", description="inherit bound",
            autofill=RuleAutofill(strategy="inherit_from_host"),
        )
        _params, fh, hosts = _collect_params([rule])
        assert fh is True
        assert hosts == {"Fire Rating"}

    def test_inherit_from_host_explicit_host_param(self):
        rule = Rule(
            id="r1", parameter="Fire Rating", requirement="present_and_nonempty",
            severity_tag="quality_change", description="inherit",
            autofill=RuleAutofill(strategy="inherit_from_host", host_param="Rating"),
        )
        _params, fh, hosts = _collect_params([rule])
        assert fh is True
        assert hosts == {"Rating"}

    def test_inherit_then_normalize_triggers_follow_host(self):
        # v1.4-K22: the compound strategy also needs the host hop.
        rule = Rule(
            id="r1", parameter="Fire Rating", requirement="canonical_format",
            severity_tag="quality_change", description="inherit+format",
            autofill=RuleAutofill(strategy="inherit_then_normalize",
                                  normalize_kind="duration", normalize_format="{h} HR"),
        )
        _params, fh, hosts = _collect_params([rule])
        assert fh is True
        assert hosts == {"Fire Rating"}


# ---------------------------------------------------------------------------
# derive_specs — happy paths
# ---------------------------------------------------------------------------


class TestDeriveSpecsHappyPath:
    def test_single_target_single_rule_revit(self, catalog: OSTCatalog):
        rs = RuleSet(
            scenario="walls_only",
            target_category="Walls",
            rules=[_rule("r1", "Fire Rating")],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert len(specs) == 1
        assert specs[0] == QuerySpec(
            category_label="Walls",
            backend_category="OST_Walls",
            params=frozenset({"Fire Rating"}),
            follow_host=False,
            host_params=frozenset(),
            discipline="architecture",
        )

    def test_single_target_single_rule_aecdm(self, catalog: OSTCatalog):
        rs = RuleSet(
            scenario="walls_aecdm",
            target_category="Walls",
            rules=[_rule("r1", "Fire Rating")],
        )
        specs = derive_specs(rs, backend="aecdm", catalog=catalog)
        assert specs[0].backend_category == "Walls"

    def test_multi_category_target(self, catalog: OSTCatalog):
        """[Walls, Doors] → 2 specs, params merged independently."""
        rs = RuleSet(
            scenario="fire_rating",
            target_category=["Walls", "Doors"],
            rules=[
                _rule("r.wall.fr", "Fire Rating", category="Walls"),
                _rule(
                    "r.door.fr",
                    "Fire Rating",
                    category="Doors",
                    other_param="host.Fire Rating",
                ),
            ],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert {s.category_label for s in specs} == {"Walls", "Doors"}
        walls = next(s for s in specs if s.category_label == "Walls")
        doors = next(s for s in specs if s.category_label == "Doors")
        assert walls.follow_host is False
        assert doors.follow_host is True
        assert doors.host_params == frozenset({"Fire Rating"})

    def test_rule_without_category_applies_to_all_targets(
        self, catalog: OSTCatalog
    ):
        """A rule with category=None spreads to every target spec."""
        rs = RuleSet(
            scenario="multi",
            target_category=["Walls", "Doors"],
            rules=[
                _rule("r.universal", "Comments"),  # no category → both
                _rule("r.wall_only", "Fire Rating", category="Walls"),
            ],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        walls = next(s for s in specs if s.category_label == "Walls")
        doors = next(s for s in specs if s.category_label == "Doors")
        assert walls.params == frozenset({"Comments", "Fire Rating"})
        assert doors.params == frozenset({"Comments"})

    def test_order_preserved_from_target_category(
        self, catalog: OSTCatalog
    ):
        rs = RuleSet(
            scenario="ordered",
            target_category=["Doors", "Walls"],
            rules=[_rule("r1", "Mark")],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert [s.category_label for s in specs] == ["Doors", "Walls"]

    def test_when_param_merged_into_spec(self, catalog: OSTCatalog):
        rs = RuleSet(
            scenario="conditional",
            target_category="Walls",
            rules=[
                _rule(
                    "r1",
                    "Fire Rating",
                    when_param="Function",
                ),
            ],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert specs[0].params == frozenset({"Fire Rating", "Function"})


# ---------------------------------------------------------------------------
# derive_specs — error paths
# ---------------------------------------------------------------------------


class TestQueryPlanCoverage:
    """The coverage record answers "was this model actually audited?".

    Dropping every target category yields zero elements → zero findings →
    a converged run, i.e. exactly the same output as a genuinely clean
    model. `derive_specs` alone cannot tell those apart; the coverage
    record can, and `orchestrator._exit_code_for` gates on it.
    """

    def test_all_resolved_reports_no_drops(self, catalog: OSTCatalog):
        rs = RuleSet(
            scenario="walls_only",
            target_category="Walls",
            rules=[_rule("r1", "Fire Rating")],
        )
        specs, cov = derive_specs_with_coverage(rs, backend="revit", catalog=catalog)
        assert len(specs) == 1
        assert cov["targets_requested"] == ["Walls"]
        assert cov["categories_resolved"] == ["Walls"]
        assert cov["categories_dropped"] == []
        assert cov["rule_count"] == 1

    def test_unresolvable_category_is_recorded_as_dropped(
        self, catalog: OSTCatalog
    ):
        rs = RuleSet(
            scenario="bogus",
            target_category="Bananas",           # not in the catalog
            rules=[_rule("r1", "X")],
        )
        specs, cov = derive_specs_with_coverage(rs, backend="revit", catalog=catalog)
        assert specs == []
        # Nothing resolved, but the ruleset was NOT empty — the run audited
        # nothing at all and must not be reported as a clean pass.
        assert cov["categories_resolved"] == []
        assert cov["rule_count"] == 1
        assert [d["category"] for d in cov["categories_dropped"]] == ["Bananas"]
        assert cov["categories_dropped"][0]["reason"] == "unresolved_on_revit"

    def test_partial_plan_records_both_sides(self, catalog: OSTCatalog):
        rs = RuleSet(
            scenario="mixed",
            target_category=["Walls", "Bananas"],
            rules=[_rule("r1", "Fire Rating")],
        )
        specs, cov = derive_specs_with_coverage(rs, backend="revit", catalog=catalog)
        assert [s.category_label for s in specs] == ["Walls"]
        assert cov["categories_resolved"] == ["Walls"]
        assert [d["category"] for d in cov["categories_dropped"]] == ["Bananas"]

    def test_empty_target_category_yields_empty_coverage(
        self, catalog: OSTCatalog
    ):
        rs = RuleSet(scenario="empty", target_category=[], rules=[_rule("r1", "X")])
        specs, cov = derive_specs_with_coverage(rs, backend="revit", catalog=catalog)
        assert specs == []
        assert cov["targets_requested"] == []
        assert cov["categories_resolved"] == []

    def test_derive_specs_wrapper_still_returns_specs_only(
        self, catalog: OSTCatalog
    ):
        # Every pre-existing caller keeps the plain list contract.
        rs = RuleSet(
            scenario="walls_only",
            target_category="Walls",
            rules=[_rule("r1", "Fire Rating")],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert isinstance(specs, list)
        assert [s.category_label for s in specs] == ["Walls"]


class TestDeriveSpecsErrorPaths:
    def test_empty_target_category_returns_empty_with_warn(
        self, catalog: OSTCatalog
    ):
        rs = RuleSet(
            scenario="empty",
            target_category=[],
            rules=[_rule("r1", "X")],
        )
        with capture_logs() as logs:
            specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert specs == []
        assert any(
            e.get("event") == "query_specs.empty_target_category"
            for e in logs
        )

    def test_rule_category_outside_target_warns_and_skips(
        self, catalog: OSTCatalog
    ):
        """A rule pointing at a category not in target_category is unreachable."""
        rs = RuleSet(
            scenario="stray_rule",
            target_category=["Walls"],
            rules=[
                _rule("r.wall", "Fire Rating", category="Walls"),
                _rule("r.stray", "X", category="Trusses"),  # not in target
            ],
        )
        with capture_logs() as logs:
            specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert len(specs) == 1
        assert specs[0].category_label == "Walls"
        # Stray rule's param is NOT in the spec
        assert specs[0].params == frozenset({"Fire Rating"})
        # Warn emitted for the stray
        stray_warns = [
            e for e in logs
            if e.get("event") == "query_specs.rule_category_out_of_scope"
            and e.get("rule_id") == "r.stray"
        ]
        assert len(stray_warns) == 1

    def test_category_with_no_rules_skipped(self, catalog: OSTCatalog):
        """Target category that no rule ever matches — skipped with warn."""
        rs = RuleSet(
            scenario="orphan_target",
            target_category=["Walls", "Doors"],
            rules=[_rule("r.wall", "Fire Rating", category="Walls")],
        )
        with capture_logs() as logs:
            specs = derive_specs(rs, backend="revit", catalog=catalog)
        # Doors has no rule → no spec for it
        assert {s.category_label for s in specs} == {"Walls"}
        assert any(
            e.get("event") == "query_specs.no_rules_for_category"
            and e.get("category") == "Doors"
            for e in logs
        )

    def test_unknown_category_dropped(self, catalog: OSTCatalog):
        """Target category not in catalog → catalog warns, derive skips."""
        rs = RuleSet(
            scenario="unknown_cat",
            target_category=["Walls", "Bananas"],
            rules=[_rule("r1", "X")],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert {s.category_label for s in specs} == {"Walls"}

    def test_aecdm_null_category_dropped(self, catalog: OSTCatalog):
        """Trusses has aecdm_label=None → drop from AECDM-backed specs."""
        rs = RuleSet(
            scenario="mixed",
            target_category=["Walls", "Trusses"],
            rules=[
                _rule("r.wall", "Mark", category="Walls"),
                _rule("r.truss", "Mark", category="Trusses"),
            ],
        )
        specs = derive_specs(rs, backend="aecdm", catalog=catalog)
        assert {s.category_label for s in specs} == {"Walls"}

    def test_aecdm_null_category_keeps_for_revit(self, catalog: OSTCatalog):
        """Same RuleSet under Revit backend: Trusses spec is produced."""
        rs = RuleSet(
            scenario="mixed_revit",
            target_category=["Walls", "Trusses"],
            rules=[
                _rule("r.wall", "Mark", category="Walls"),
                _rule("r.truss", "Mark", category="Trusses"),
            ],
        )
        specs = derive_specs(rs, backend="revit", catalog=catalog)
        assert {s.category_label for s in specs} == {"Walls", "Trusses"}
        truss = next(s for s in specs if s.category_label == "Trusses")
        assert truss.backend_category == "OST_StructuralTruss"
        assert truss.discipline == "structure"


# ---------------------------------------------------------------------------
# Real catalog smoke
# ---------------------------------------------------------------------------


class TestDeriveSpecsRealCatalog:
    """Drive derive_specs against the real on-disk catalog to catch
    regressions in either layer.
    """

    @pytest.fixture(scope="class")
    def real_catalog(self) -> OSTCatalog:
        return OSTCatalog.load()

    def test_fire_rating_scenario_revit(self, real_catalog: OSTCatalog):
        rs = RuleSet(
            scenario="fire_rating_compliance",
            target_category=["Walls", "Doors"],
            rules=[
                _rule(
                    "r.wall.fr",
                    "Fire Rating",
                    category="Walls",
                    when_param="Function",
                ),
                _rule(
                    "r.door.fr",
                    "Fire Rating",
                    category="Doors",
                    other_param="host.Fire Rating",
                ),
            ],
        )
        specs = derive_specs(rs, backend="revit", catalog=real_catalog)
        by_label = {s.category_label: s for s in specs}
        assert by_label["Walls"].backend_category == "OST_Walls"
        assert by_label["Doors"].backend_category == "OST_Doors"
        assert by_label["Doors"].follow_host is True
        assert by_label["Doors"].host_params == frozenset({"Fire Rating"})
        assert by_label["Walls"].params == frozenset({"Fire Rating", "Function"})

    def test_room_completeness_aecdm(self, real_catalog: OSTCatalog):
        rs = RuleSet(
            scenario="param_completeness",
            target_category="Rooms",
            rules=[
                _rule("r.dept", "Department"),
                _rule("r.occ", "Occupancy"),
            ],
        )
        specs = derive_specs(rs, backend="aecdm", catalog=real_catalog)
        assert len(specs) == 1
        assert specs[0].backend_category == "Rooms"
        assert specs[0].params == frozenset({"Department", "Occupancy"})
