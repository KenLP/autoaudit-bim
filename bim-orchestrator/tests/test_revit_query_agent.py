"""Tests for the unified v1.3 RevitQueryAgent.

Coverage merges what used to live in two separate files:
  * Rooms-flavoured tests (old ``test_revit_query_agent.py``) — list_rooms
    metric mirrors, Occupancy / levelName carry-through, partial failure.
  * Multi-category + host-hop tests (old ``test_revit_elements_query.py``)
    — type-cache dedup, Doors hosting Walls Fire Rating, follow_host
    derived from rules.

The unified agent reads everything from a ``RuleSet`` + ``OSTCatalog``,
so each test builds a small ``make_test_ruleset`` and constructs the
agent with ``mcp=client, rules=rs``. The default ``OSTCatalog.load()``
fallback keeps tests terse.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bim_orchestrator.agents.revit_query import (
    RevitQueryAgent,
    _apply_option_display,
    _attach_room_metrics,
    _flatten,
    _flatten_sheet,
    _merge_params,
)
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill
from bim_orchestrator.state import OrchestratorState
from tests._mocks import (
    SAMPLE_REVIT_DOORS,
    SAMPLE_REVIT_ROOMS_AS_ELEMENTS,
    SAMPLE_REVIT_SHEETS,
    SAMPLE_REVIT_WALLS,
    MockRevitMCPClient,
    make_test_ruleset,
)


def _empty_state() -> OrchestratorState:
    return {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }


@pytest.fixture(scope="module")
def catalog() -> OSTCatalog:
    return OSTCatalog.load()


# ---------------------------------------------------------------------------
# Pure helpers — _flatten, _merge_params, _attach_room_metrics
# ---------------------------------------------------------------------------


class TestFlatten:
    def test_basic(self):
        out = _flatten([
            {"name": "Department", "value": "Storage"},
            {"name": "Area", "value": 12.5},
        ])
        assert out == {"Department": "Storage", "Area": 12.5}

    def test_skips_missing_name(self):
        out = _flatten([
            {"name": "A", "value": 1},
            {"value": "orphan"},
            {"name": "", "value": "empty"},
            {"name": "B", "value": 2},
        ])
        assert out == {"A": 1, "B": 2}

    def test_empty(self):
        assert _flatten([]) == {}


class TestMergeParams:
    def test_instance_wins_when_both_present(self):
        out = _merge_params(
            wanted=frozenset({"Comments"}),
            instance_params={"Comments": "inst"},
            type_params={"Comments": "type"},
        )
        assert out["Comments"] == "inst"
        # Type still reachable via the type.* alias
        assert out["type.Comments"] == "type"

    def test_type_fallback_when_instance_missing(self):
        out = _merge_params(
            wanted=frozenset({"Fire Rating"}),
            instance_params={},
            type_params={"Fire Rating": "2 HR"},
        )
        assert out["Fire Rating"] == "2 HR"
        assert out["type.Fire Rating"] == "2 HR"

    def test_instance_none_falls_back_to_type(self):
        out = _merge_params(
            wanted=frozenset({"Fire Rating"}),
            instance_params={"Fire Rating": None},
            type_params={"Fire Rating": "2 HR"},
        )
        assert out["Fire Rating"] == "2 HR"

    # v1.4-F1: blank-string instance value must NOT shadow a real Type value.
    # Pre-fix, Revit returning ``""`` on an instance override (common for
    # Fire Rating / Type Mark when the instance row is empty but the Type
    # holds the canonical value) used to mask the Type, causing QC to
    # falsely report missing/non-compliant.

    def test_instance_empty_string_falls_back_to_type(self):
        out = _merge_params(
            wanted=frozenset({"Fire Rating"}),
            instance_params={"Fire Rating": ""},
            type_params={"Fire Rating": "2 HR"},
        )
        assert out["Fire Rating"] == "2 HR"
        assert out["type.Fire Rating"] == "2 HR"

    def test_instance_whitespace_string_falls_back_to_type(self):
        out = _merge_params(
            wanted=frozenset({"Type Mark"}),
            instance_params={"Type Mark": "   "},
            type_params={"Type Mark": "D01"},
        )
        assert out["Type Mark"] == "D01"

    def test_instance_zero_is_real_value_not_fallback(self):
        """Numeric zero is a real value — must NOT fall back to type."""
        out = _merge_params(
            wanted=frozenset({"Width"}),
            instance_params={"Width": 0},
            type_params={"Width": 999},
        )
        assert out["Width"] == 0

    def test_instance_false_is_real_value_not_fallback(self):
        """Boolean False is a real value — must NOT fall back to type."""
        out = _merge_params(
            wanted=frozenset({"Is Reference"}),
            instance_params={"Is Reference": False},
            type_params={"Is Reference": True},
        )
        assert out["Is Reference"] is False

    def test_type_only_prefix_skips_instance(self):
        out = _merge_params(
            wanted=frozenset({"type.Fire Rating"}),
            instance_params={"Fire Rating": "INST"},
            type_params={"Fire Rating": "TYPE"},
        )
        assert out["type.Fire Rating"] == "TYPE"
        # Plain name should NOT appear — only the type.* form was requested
        assert "Fire Rating" not in out

    def test_missing_everywhere_yields_none(self):
        out = _merge_params(
            wanted=frozenset({"Mystery"}),
            instance_params={},
            type_params={},
        )
        assert out == {"Mystery": None}

    def test_bare_type_prefix_skipped(self):
        """Edge: rule wrote bare 'type.' — defensive no-op."""
        out = _merge_params(
            wanted=frozenset({"type."}),
            instance_params={},
            type_params={},
        )
        assert out == {}


class TestApplyOptionDisplay:
    """v1.4-K25: option/reference params surface their valueString, not the raw
    enum integer / ElementId, so text rules (scope_filter, matches_regex) match."""

    def test_overrides_type_value_with_display(self):
        params = {"Function": 1, "type.Function": 1}
        _apply_option_display(
            params,
            display_names=frozenset({"Function"}),
            instance_disp={},
            type_disp={"Function": "Exterior"},
        )
        assert params["Function"] == "Exterior"
        assert params["type.Function"] == "Exterior"

    def test_instance_display_wins_over_type(self):
        params = {"Function": 0, "type.Function": 5}
        _apply_option_display(
            params,
            display_names=frozenset({"Function"}),
            instance_disp={"Function": "Interior"},
            type_disp={"Function": "Core-shaft"},
        )
        assert params["Function"] == "Interior"
        # type.* alias still reflects the Type display
        assert params["type.Function"] == "Core-shaft"

    def test_keeps_raw_value_when_no_display_string(self):
        """Missing valueString → leave the raw value (an integer beats nothing)."""
        params = {"Function": 3}
        _apply_option_display(
            params,
            display_names=frozenset({"Function"}),
            instance_disp={},
            type_disp={},
        )
        assert params["Function"] == 3

    def test_ignores_params_not_in_display_set(self):
        params = {"Fire Rating": "2 HR", "Function": 1}
        _apply_option_display(
            params,
            display_names=frozenset({"Function"}),
            instance_disp={"Fire Rating": "TWO HOURS"},
            type_disp={"Function": "Exterior"},
        )
        assert params["Fire Rating"] == "2 HR"  # untouched — not an option param
        assert params["Function"] == "Exterior"


class TestAttachRoomMetrics:
    def test_uses_bulk_areaMetric_when_present(self):
        params: dict[str, Any] = {}
        _attach_room_metrics(
            params,
            list_item={"areaMetric": 55.08, "levelName": "L2", "perimeter": 108.84},
            instance_params={},
        )
        assert params["areaMetric"] == 55.08
        assert params["levelName"] == "L2"
        assert params["perimeter"] == 108.84

    def test_computes_areaMetric_from_imperial_when_bulk_missing(self):
        params: dict[str, Any] = {}
        _attach_room_metrics(
            params,
            list_item={},
            instance_params={"Area": 592.88},  # ft²
        )
        # 592.88 ft² ≈ 55.08 m²
        assert params["areaMetric"] == pytest.approx(55.08, rel=1e-3)

    def test_unbounded_height_metric(self):
        params: dict[str, Any] = {}
        _attach_room_metrics(
            params,
            list_item={},
            instance_params={"Unbounded Height": 10.25},  # ft
        )
        assert params["Unbounded Height (m)"] == pytest.approx(3.1242, rel=1e-3)

    def test_non_numeric_area_skipped_gracefully(self):
        params: dict[str, Any] = {}
        _attach_room_metrics(
            params, list_item={}, instance_params={"Area": "not-a-number"}
        )
        assert "areaMetric" not in params

    def test_does_not_overwrite_existing_level(self):
        params = {"levelName": "Existing"}
        _attach_room_metrics(
            params, list_item={"levelName": "FromBulk"}, instance_params={}
        )
        assert params["levelName"] == "Existing"


# ---------------------------------------------------------------------------
# RevitQueryAgent — Rooms path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRoomsPath:
    async def test_fetches_all_rooms_via_list_elements(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert state["status"] == "checking"
        assert len(state["elements"]) == len(SAMPLE_REVIT_ROOMS_AS_ELEMENTS)
        # list_rooms NOT used anymore — verify via call log
        calls = [tool for tool, _ in client.calls]
        assert "revit_list_rooms" not in calls
        assert "revit_list_elements" in calls

    async def test_uses_OST_Rooms_label_for_revit_mcp(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        list_call = client.calls_to("revit_list_elements")[0]
        assert list_call["category"] == "OST_Rooms"

    async def test_default_element_cap_applied_to_list_elements(self, catalog):
        """P0: default max_elements_per_category=300 → list_elements limit=300."""
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        list_call = client.calls_to("revit_list_elements")[0]
        assert list_call["limit"] == 300

    async def test_custom_element_cap_applied(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog,
                max_elements_per_category=50,
            ).run(_empty_state())
        list_call = client.calls_to("revit_list_elements")[0]
        assert list_call["limit"] == 50

    async def test_cap_none_disables_limit(self, catalog):
        """Passing None restores unbounded (pre-K3) behaviour — no limit arg."""
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog,
                max_elements_per_category=None,
            ).run(_empty_state())
        list_call = client.calls_to("revit_list_elements")[0]
        assert "limit" not in list_call

    async def test_each_element_has_qc_shape(self, catalog):
        rs = make_test_ruleset(target_category="Rooms", parameter="Department")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        for el in state["elements"]:
            assert el["category"] == "Rooms"
            assert isinstance(el["id"], str)
            assert isinstance(el["params"], dict)
            assert "name" in el

    async def test_carries_areaMetric_from_bulk_response(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        studio = next(el for el in state["elements"] if el["id"] == "829712")
        assert studio["params"]["areaMetric"] == pytest.approx(55.08, rel=1e-3)

    async def test_derives_metric_height_from_imperial_parameter(self, catalog):
        # Need a rule that pulls Unbounded Height into spec.params, otherwise
        # the unified agent won't surface it (rules-driven allowlist).
        rs = make_test_ruleset(
            target_category="Rooms", parameter="Unbounded Height"
        )
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        studio = next(el for el in state["elements"] if el["id"] == "829712")
        # 10.25 ft → 3.124 m
        assert studio["params"]["Unbounded Height (m)"] == pytest.approx(
            3.124, rel=1e-3
        )

    async def test_carries_occupancy_for_residential_rule(self, catalog):
        """The rule needs Occupancy in spec.params for it to be hoisted."""
        rs = make_test_ruleset(target_category="Rooms", parameter="Occupancy")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        studio = next(el for el in state["elements"] if el["id"] == "829712")
        assert studio["params"]["Occupancy"] == "Residential One Story"

    async def test_carries_level_name_from_bulk_response(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        studio = next(el for el in state["elements"] if el["id"] == "829712")
        assert studio["params"]["levelName"] == "L2"

    async def test_list_elements_failure_propagates(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient(fail_on={"revit_list_elements"}) as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert state["status"] == "failed"
        assert state["error"] and "simulated_failure" in state["error"]

    async def test_state_iteration_preserved(self, catalog):
        rs = make_test_ruleset(target_category="Rooms")
        async with MockRevitMCPClient() as client:
            state = _empty_state()
            state["iteration"] = 2
            result = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(state)
        assert result["iteration"] == 2


# ---------------------------------------------------------------------------
# RevitQueryAgent — Walls / Doors host-hop path
# ---------------------------------------------------------------------------


def _fire_rating_rule(category: str, *, follow_host: bool = False) -> Rule:
    return Rule(
        id=f"fr.{category.lower()}",
        parameter="Fire Rating",
        requirement="present_and_nonempty",
        category=category,
        other_param="host.Fire Rating" if follow_host else None,
        severity_tag="fire_safety_change",
        description="fire-rating test rule",
        autofill=RuleAutofill(strategy="none"),
    )


@pytest.mark.asyncio
class TestFireRatingPath:
    async def test_single_category_walls(self, catalog):
        rs = make_test_ruleset(
            target_category="Walls",
            parameter="Fire Rating",
        )
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert state["status"] == "checking"
        assert len(state["elements"]) == len(SAMPLE_REVIT_WALLS)
        for el in state["elements"]:
            assert el["category"] == "Walls"
            assert not any(k.startswith("host.") for k in el["params"])

    async def test_walls_hoist_fire_rating_from_type(self, catalog):
        """Wall instance has no Fire Rating; agent falls back to Type."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # Wall 100 → type 1000 → Fire Rating = "4 HR"
        assert by_id["100"]["params"]["Fire Rating"] == "4 HR"
        # Wall 102 → type 1002 → Fire Rating = "" (the missing-data source)
        assert by_id["102"]["params"]["Fire Rating"] == ""

    async def test_type_alias_always_present_when_type_has_value(self, catalog):
        """v1.3 contract: type.<name> alias mirrors Type value."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        wall_100 = next(el for el in state["elements"] if el["id"] == "100")
        assert wall_100["params"]["type.Fire Rating"] == "4 HR"

    async def test_host_option_param_surfaces_display_text(self, catalog):
        """2026-08-18: the K25 display rule (option/reference dims surface
        their valueString) must apply to the HOST hop too. Wall `Function` is
        an option enum — raw value 5, display "Core-shaft" — and a lookup
        table row written for the TEXT can never match the raw int. Found
        live: the IBC 716.1(3) windows table resolved NO row for 125 windows
        sitting in catalogued "2 HR"/Exterior walls because `host.Function`
        arrived as the integer 1."""
        rs = make_test_ruleset(
            target_category="Doors",
            parameter="Fire Rating",
            other_param="host.Function",
        )
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # Door 200 → wall 100 → type 1000: Function raw 5 / display "Core-shaft"
        assert by_id["200"]["params"]["host.Function"] == "Core-shaft"
        # Door 202 → wall 101 → type 1001: Function raw 1 / display "Exterior"
        assert by_id["202"]["params"]["host.Function"] == "Exterior"

    async def test_doors_follow_host_via_rule(self, catalog):
        """Doors target with host.* other_param triggers the host hop."""
        rs = make_test_ruleset(
            target_category="Doors",
            extra_rules=[],
            parameter="Fire Rating",
            other_param="host.Fire Rating",
        )
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert len(state["elements"]) == len(SAMPLE_REVIT_DOORS)
        by_id = {el["id"]: el for el in state["elements"]}
        # Door 200 hosted in wall 100 (type 1000 "4 HR")
        assert by_id["200"]["params"]["host.Fire Rating"] == "4 HR"
        # Door 203 hosted in wall 102 (type 1002 "")
        assert by_id["203"]["params"]["host.Fire Rating"] == ""

    async def test_multi_category_walls_plus_doors(self, catalog):
        """target_category=[Walls, Doors] queries both; only Doors host-hops."""
        rs = make_test_ruleset(
            scenario="fire_rating",
            target_category=["Walls", "Doors"],
            rule_id="wall.fr",
            parameter="Fire Rating",
            category="Walls",
            extra_rules=[
                Rule(
                    id="door.fr",
                    parameter="Fire Rating",
                    requirement="fire_rating_ge",
                    category="Doors",
                    other_param="host.Fire Rating",
                    severity_tag="fire_safety_change",
                    description="door rating ≥ host wall rating",
                    autofill=RuleAutofill(strategy="none"),
                ),
            ],
        )
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        cats = {el["category"] for el in state["elements"]}
        assert cats == {"Walls", "Doors"}
        # Walls don't carry host.* keys
        for el in state["elements"]:
            if el["category"] == "Walls":
                assert not any(k.startswith("host.") for k in el["params"])

    async def test_type_info_cache_dedupes_fetches(self, catalog):
        """4 walls share 3 types → revit_get_element_info hit ≤ 4+3 times."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
            info_calls = client.calls_to("revit_get_element_info")
        # 4 wall instances × 1 instance fetch + at most 3 unique types
        # (cached, so the 4th wall hits the cache).
        ids = sorted({c["id"] for c in info_calls})
        # Instances: 100, 101, 102, 103. Types: 1000, 1001, 1002 (1000 shared).
        assert set(ids) >= {100, 101, 102, 103, 1000, 1001, 1002}
        # Cache hit means 1000 was NOT fetched twice
        type_1000_fetches = sum(1 for c in info_calls if c["id"] == 1000)
        assert type_1000_fetches == 1

    async def test_caches_reset_each_run(self, catalog):
        """LangGraph re-invokes run() across iterations — caches must clear."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            await agent.run(_empty_state())
            cache_size_after_run_1 = len(agent._type_info_cache)  # noqa: SLF001
            assert cache_size_after_run_1 > 0
            # Second run: caches should be replaced, not appended
            await agent.run(_empty_state())
            # Same size — proves we re-built from scratch (not accumulated)
            assert len(agent._type_info_cache) == cache_size_after_run_1  # noqa: SLF001
            # L-10: the instance in-flight map is per-run state too, and it
            # must be EMPTY at rest — a leftover entry would serve a stale
            # Future to the next run.
            assert agent._instance_inflight == {}  # noqa: SLF001

    async def test_concurrent_requests_for_one_instance_fetch_once(self, catalog):
        """L-10: `_get_instance` had a cache but no in-flight map.

        The cache is only consulted BEFORE the fetch, so a fan-out that all
        wants the same element — every door in a wall asking for that wall
        during the host hop — issued one `get_element_info` per concurrent
        task instead of one per distinct element. `_get_type` has had this
        dedup since K8; this is the same fix one layer down.
        """
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            # The mock answers synchronously, so without a real suspension
            # point each caller would run to completion before the next one
            # starts and the plain cache would already be enough — the test
            # would pass against the unfixed code (it did). One `sleep(0)`
            # makes the fetch yield, which is what an actual Revit round-trip
            # does and what creates the window this dedup exists to close.
            real_get = client.get_element_info

            async def slow_get(element_id):
                await asyncio.sleep(0)
                return await real_get(element_id)

            client.get_element_info = slow_get  # type: ignore[method-assign]
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            sem = asyncio.Semaphore(8)
            # Ten concurrent callers, one element, cold cache.
            results = await asyncio.gather(
                *(agent._get_instance(100, sem) for _ in range(10))  # noqa: SLF001
            )
            fetches = [
                c for c in client.calls_to("revit_get_element_info") if c["id"] == 100
            ]

        assert len(fetches) == 1, (
            f"one element, ten concurrent askers → {len(fetches)} Revit "
            "round-trips; the in-flight dedup is not holding"
        )
        # Every caller still gets the real answer, not None.
        assert all(r is not None and r["id"] == 100 for r in results)
        assert agent._instance_inflight == {}  # noqa: SLF001 — slot released

    async def test_a_failed_instance_fetch_releases_its_slot(self, catalog):
        """The `finally: set_result` half of K8's lesson: a failure must still
        resolve the shared Future (or every awaiter hangs) and drop the slot
        (or the failure is cached for the rest of the run)."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            sem = asyncio.Semaphore(4)
            results = await asyncio.gather(
                *(agent._get_instance(999_999, sem) for _ in range(3))  # noqa: SLF001
            )

        assert results == [None, None, None]
        assert agent._instance_inflight == {}  # noqa: SLF001

    async def test_missing_host_degrades_gracefully(self, catalog):
        """A door whose host doesn't exist still appears, without host.* keys."""
        rs = make_test_ruleset(
            target_category="Doors",
            parameter="Fire Rating",
            other_param="host.Fire Rating",
        )
        client = MockRevitMCPClient()
        # Remove door 203's host wall (102) so the hop fails
        del client.element_info[102]
        async with client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        door_203 = next(el for el in state["elements"] if el["id"] == "203")
        # Door still in elements; no host. keys hydrated
        assert "host.Fire Rating" not in door_203["params"]


# ---------------------------------------------------------------------------
# v1.4-K25 — enum/option param display-ification (live R27 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOptionDisplay:
    """Walls ``Function`` is an integer enum (option dimension) whose display
    text lives in valueString. The agent must surface "Exterior"/"Interior" so
    a scope_filter / matches_regex rule on Function actually matches — the live
    R27 bug where the envelope pack scoped 0 of 1241 walls."""

    async def test_function_surfaces_display_text_not_integer(self, catalog):
        rs = make_test_ruleset(target_category="Walls", parameter="Function")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # Wall 101 → type 1001 → Function value 1 / valueString "Exterior"
        assert by_id["101"]["params"]["Function"] == "Exterior"
        # Wall 100/103 → type 1000 → value 5 / "Core-shaft"
        assert by_id["100"]["params"]["Function"] == "Core-shaft"
        # Wall 102 → type 1002 → value 0 / "Interior" (the value=0 edge: still
        # display-ified, not left as the falsy integer)
        assert by_id["102"]["params"]["Function"] == "Interior"
        # type.* alias also display-ified
        assert by_id["101"]["params"]["type.Function"] == "Exterior"

    async def test_bulk_path_also_display_ifies(self, catalog):
        """The bulk find_elements path must surface display text too."""
        rs = make_test_ruleset(target_category="Walls", parameter="Function")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog, bulk_fields=True
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        assert by_id["101"]["params"]["Function"] == "Exterior"
        assert by_id["102"]["params"]["Function"] == "Interior"

    async def test_text_dimension_param_left_as_is(self, catalog):
        """A text param (Fire Rating) is NOT in the display set — value unchanged."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        assert by_id["100"]["params"]["Fire Rating"] == "4 HR"

    async def test_no_catalog_degrades_gracefully(self, catalog):
        """When the param catalog is unavailable, display-ification is a no-op
        (raw enum integer surfaces) rather than crashing the run."""
        rs = make_test_ruleset(target_category="Walls", parameter="Function")
        agent = RevitQueryAgent(mcp=MockRevitMCPClient(), rules=rs, catalog=catalog)
        agent._param_catalog = None  # noqa: SLF001
        agent._display_names = agent._compute_display_names()  # noqa: SLF001
        async with MockRevitMCPClient() as client:
            agent._mcp = client  # noqa: SLF001
            state = await agent.run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # Raw integer surfaces (no display-ification) — and no exception.
        assert by_id["101"]["params"]["Function"] == 1


# ---------------------------------------------------------------------------
# Construction / spec exposure
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_specs_property_exposes_derived(self, catalog):
        rs = make_test_ruleset(
            target_category=["Walls", "Doors"], parameter="Fire Rating"
        )
        client = MockRevitMCPClient()
        agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
        assert {s.category_label for s in agent.specs} == {"Walls", "Doors"}
        assert {s.backend_category for s in agent.specs} == {"OST_Walls", "OST_Doors"}

    def test_loads_default_catalog_when_omitted(self):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        client = MockRevitMCPClient()
        agent = RevitQueryAgent(mcp=client, rules=rs)
        assert agent.specs and agent.specs[0].backend_category == "OST_Walls"


# ---------------------------------------------------------------------------
# v1.4-K3 (P1) — bulk find_elements path (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBulkFieldsPath:
    async def test_default_off_uses_list_elements_and_per_instance(self, catalog):
        """bulk_fields defaults off → list_elements + per-instance get_element_info."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert "revit_find_elements" not in [t for t, _ in client.calls]
        assert "revit_list_elements" in [t for t, _ in client.calls]
        # Instance get_element_info calls happened (100..103)
        info_ids = {c["id"] for c in client.calls_to("revit_get_element_info")}
        assert {100, 101, 102, 103} <= info_ids

    async def test_bulk_on_uses_find_elements_no_instance_fetch(self, catalog):
        """bulk_fields=True on Walls → find_elements; NO per-instance get_element_info."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog, bulk_fields=True
            ).run(_empty_state())
        assert "revit_find_elements" in [t for t, _ in client.calls]
        info_ids = {c["id"] for c in client.calls_to("revit_get_element_info")}
        # Instances 100..103 must NOT be fetched (pre-seeded from find_elements)
        assert not ({100, 101, 102, 103} & info_ids)
        # Types ARE still fetched (so Instance>Type merge survives)
        assert {1000, 1001, 1002} <= info_ids

    async def test_bulk_preserves_type_fallback(self, catalog):
        """The whole point: implicit Instance>Type fallback still resolves on bulk path."""
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog, bulk_fields=True
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # Wall 100 instance has no Fire Rating → falls back to type 1000 "4 HR"
        assert by_id["100"]["params"]["Fire Rating"] == "4 HR"
        # type.<name> alias still mirrored
        assert by_id["100"]["params"]["type.Fire Rating"] == "4 HR"

    async def test_bulk_skips_rooms_category(self, catalog):
        """Rooms keep the list_elements path even with bulk_fields on (metric enrichment)."""
        rs = make_test_ruleset(target_category="Rooms", parameter="Department")
        async with MockRevitMCPClient() as client:
            await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog, bulk_fields=True
            ).run(_empty_state())
        assert "revit_find_elements" not in [t for t, _ in client.calls]
        assert "revit_list_elements" in [t for t, _ in client.calls]

    async def test_bulk_skips_follow_host_specs(self, catalog):
        """Host-hop scenarios keep the per-element path even with bulk_fields on."""
        rs = make_test_ruleset(
            target_category="Doors",
            parameter="Fire Rating",
            other_param="host.Fire Rating",
        )
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog, bulk_fields=True
            ).run(_empty_state())
        assert "revit_find_elements" not in [t for t, _ in client.calls]
        # Host hop still resolves
        by_id = {el["id"]: el for el in state["elements"]}
        assert by_id["200"]["params"]["host.Fire Rating"] == "4 HR"


@pytest.mark.asyncio
class TestTypeFetchDedup:
    """v1.4-K8: concurrent type fetches for the same type_id collapse to ONE
    get_element_info (thundering-herd fix). Live on R27 this cut type fetches
    from 300 → 3 (hit ratio 0.99) when 300 ducts shared 3 types."""

    async def test_concurrent_same_type_one_fetch(self, catalog):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            calls = {"n": 0}

            async def counting(eid: int) -> dict[str, Any]:
                calls["n"] += 1
                await asyncio.sleep(0.01)  # hold the slot so the herd piles up
                return {"id": str(eid), "params": {"Fire Rating": "2 HR"}}

            agent._mcp.get_element_info = counting  # type: ignore[assignment]
            sem = asyncio.Semaphore(8)
            results = await asyncio.gather(
                *[agent._get_type(4242, sem) for _ in range(50)]
            )

        assert calls["n"] == 1                          # 50 asks → 1 Revit round-trip
        assert all(r == results[0] for r in results)    # all served the same record
        assert agent._cache_misses_type == 1
        assert agent._cache_hits_type == 49

    async def test_distinct_types_each_fetched_once(self, catalog):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            fetched: list[int] = []

            async def counting(eid: int) -> dict[str, Any]:
                fetched.append(eid)
                await asyncio.sleep(0.01)
                return {"id": str(eid), "params": {}}

            agent._mcp.get_element_info = counting  # type: ignore[assignment]
            sem = asyncio.Semaphore(8)
            # 30 asks across 3 type ids → exactly 3 fetches
            await asyncio.gather(
                *[agent._get_type(1000 + (i % 3), sem) for i in range(30)]
            )

        assert sorted(fetched) == [1000, 1001, 1002]


@pytest.mark.asyncio
class TestTypeFetchInFlightHardening:
    """Low9 — get_running_loop() (not the deprecated get_event_loop()), and
    the shared in-flight Future is shielded from an individual awaiter's own
    cancellation so cancelling ONE sibling doesn't poison the Future for the
    others still waiting on the same type_id."""

    async def test_get_running_loop_does_not_raise_and_dedup_still_works(
        self, catalog
    ):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            calls = {"n": 0}

            async def counting(eid: int) -> dict[str, Any]:
                calls["n"] += 1
                await asyncio.sleep(0.01)
                return {"id": str(eid), "params": {"Fire Rating": "2 HR"}}

            agent._mcp.get_element_info = counting  # type: ignore[assignment]
            sem = asyncio.Semaphore(8)
            results = await asyncio.gather(
                *[agent._get_type(5555, sem) for _ in range(10)]
            )

        assert calls["n"] == 1
        assert all(r == results[0] for r in results)

    async def test_cancelling_one_awaiter_does_not_poison_siblings(self, catalog):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)

            async def slow(eid: int) -> dict[str, Any]:
                await asyncio.sleep(0.05)
                return {"id": str(eid), "params": {"Fire Rating": "2 HR"}}

            agent._mcp.get_element_info = slow  # type: ignore[assignment]
            sem = asyncio.Semaphore(8)

            # First caller creates the in-flight Future (the "leader").
            leader = asyncio.ensure_future(agent._get_type(7777, sem))
            await asyncio.sleep(0)  # let the leader register the in-flight slot

            # Second caller awaits the SAME in-flight Future via the sibling
            # path (asyncio.shield branch), then gets cancelled itself.
            sibling = asyncio.ensure_future(agent._get_type(7777, sem))
            await asyncio.sleep(0)
            sibling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sibling

            # A third awaiter joining after the cancellation must still get
            # the real result — the shared Future must not have been
            # cancelled/poisoned by the second awaiter's cancellation.
            third = await agent._get_type(7777, sem)
            leader_result = await leader

            assert third == leader_result
            assert third is not None
            assert third["params"]["Fire Rating"] == "2 HR"


@pytest.mark.asyncio
class TestHydrationPartialFailure:
    """M8 — the per-category hydration fan-out (``asyncio.gather`` over
    ``hydrate(item)`` for every listed instance) must not let ONE element's
    unexpected exception (e.g. a raw transport error that ``_get_instance``
    didn't already catch as ``RevitEnvelopeError``) blow up the whole
    category and orphan the other in-flight hydrate() tasks. Partial
    failure -> partial (still usable) results + a per-element warning log.
    Total failure (every element in the category errors -> the fan-out
    itself is dead, e.g. the addin crashed) -> re-raise, preserving the
    existing fail-fast contract (fe65fbc: a failed query -> run status
    'failed', not a silently empty category)."""

    async def test_one_bad_element_is_dropped_others_still_hydrate(self, catalog):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)
            real_get_element_info = agent._mcp.get_element_info

            async def flaky(eid: int):
                if eid == 101:
                    raise ConnectionError("transport reset by peer")
                return await real_get_element_info(eid)

            agent._mcp.get_element_info = flaky  # type: ignore[assignment]
            state = await agent.run(_empty_state())

        assert state["status"] == "checking"
        ids = {el["id"] for el in state["elements"]}
        # Wall 101 dropped; the other 3 walls (100, 102, 103) still hydrated.
        assert "101" not in ids
        assert {"100", "102", "103"} <= ids
        assert len(state["elements"]) == len(SAMPLE_REVIT_WALLS) - 1

    async def test_all_elements_failing_reraises(self, catalog):
        rs = make_test_ruleset(target_category="Walls", parameter="Fire Rating")
        async with MockRevitMCPClient() as client:
            agent = RevitQueryAgent(mcp=client, rules=rs, catalog=catalog)

            async def always_fails(eid: int):
                raise ConnectionError("addin unreachable")

            agent._mcp.get_element_info = always_fails  # type: ignore[assignment]
            state = await agent.run(_empty_state())

        # run() wraps _query_category exceptions into a failed status
        # (existing fe65fbc contract) rather than propagating raw.
        assert state["status"] == "failed"
        assert "addin unreachable" in (state["error"] or "")


# ---------------------------------------------------------------------------
# Sheets path — documentation elements via list_sheets (NOT list_elements)
# ---------------------------------------------------------------------------


class TestFlattenSheet:
    def test_canonicalises_camelcase_fields(self):
        flat = _flatten_sheet({"id": 1, "sheetNumber": "A-101", "name": "Plan"})
        assert flat["Sheet Number"] == "A-101"
        assert flat["Sheet Name"] == "Plan"

    def test_prefers_explicit_canonical_keys(self):
        flat = _flatten_sheet(
            {"Sheet Number": "C-1", "sheetNumber": "X-9", "Sheet Name": "Cover"}
        )
        assert flat["Sheet Number"] == "C-1"
        assert flat["Sheet Name"] == "Cover"

    def test_title_fallback_for_name(self):
        flat = _flatten_sheet({"number": "S-1", "title": "Schedule"})
        assert flat["Sheet Number"] == "S-1"
        assert flat["Sheet Name"] == "Schedule"

    def test_blank_name_becomes_none(self):
        flat = _flatten_sheet({"sheetNumber": "A-104", "name": "   "})
        assert flat["Sheet Number"] == "A-104"
        assert flat["Sheet Name"] is None

    def test_carries_raw_fields_verbatim(self):
        flat = _flatten_sheet({"id": 7, "sheetNumber": "A", "Approved By": "JD"})
        assert flat["Approved By"] == "JD"


@pytest.mark.asyncio
class TestSheetsPath:
    async def test_uses_list_sheets_not_list_elements(self, catalog):
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert state["status"] == "checking"
        assert len(state["elements"]) == len(SAMPLE_REVIT_SHEETS)
        calls = [tool for tool, _ in client.calls]
        assert "revit_list_sheets" in calls
        # Sheets are documentation — no list_elements / per-element hydration.
        assert "revit_list_elements" not in calls
        assert "revit_get_element_info" not in calls

    async def test_resolves_sheets_to_OST_Sheets(self, catalog):
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        agent = RevitQueryAgent(mcp=MockRevitMCPClient(), rules=rs, catalog=catalog)
        assert agent.specs[0].backend_category == "OST_Sheets"
        assert agent.specs[0].category_label == "Sheets"

    async def test_flattens_sheet_number_into_params(self, catalog):
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        assert by_id["300"]["params"]["Sheet Number"] == "A-101"
        assert by_id["301"]["params"]["Sheet Number"] == "A-102"
        assert by_id["302"]["params"]["Sheet Number"] == "A-102"  # duplicate
        for el in state["elements"]:
            assert el["category"] == "Sheets"
            assert isinstance(el["id"], str)

    async def test_blank_sheet_name_surfaces_as_none(self, catalog):
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Name")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # id 304 has name "" → None so QC's _is_missing fires
        assert by_id["304"]["params"]["Sheet Name"] is None
        assert by_id["300"]["params"]["Sheet Name"] == "Floor Plan"

    async def test_only_requested_params_surfaced(self, catalog):
        """Rules-driven allowlist — a Sheet Number rule fetches only that param."""
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        sheet = state["elements"][0]
        assert set(sheet["params"]) == {"Sheet Number"}

    async def test_empty_sheets_yields_no_elements(self, catalog):
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        client = MockRevitMCPClient(sheets=[])
        async with client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert state["status"] == "checking"
        assert state["elements"] == []

    async def test_element_cap_truncates_sheets_client_side(self, catalog):
        """revit_list_sheets takes no args (returns all); the cap is applied
        client-side, so the agent yields at most `max_elements_per_category`."""
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        async with MockRevitMCPClient() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog,
                max_elements_per_category=2,
            ).run(_empty_state())
        assert len(state["elements"]) == 2
        # No limit is sent to the addin — it isn't part of the tool contract.
        sheets_call = client.calls_to("revit_list_sheets")[0]
        assert "limit" not in sheets_call

    async def test_list_sheets_failure_propagates(self, catalog):
        rs = make_test_ruleset(target_category="Sheets", parameter="Sheet Number")
        async with MockRevitMCPClient(fail_on={"revit_list_sheets"}) as client:
            state = await RevitQueryAgent(
                mcp=client, rules=rs, catalog=catalog
            ).run(_empty_state())
        assert state["status"] == "failed"
        assert state["error"] and "simulated_failure" in state["error"]
