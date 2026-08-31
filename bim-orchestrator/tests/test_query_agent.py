"""Tests for v1.3 Forma QueryAgent + _attach_params property flattener.

Two construction modes exercised:
  * ``rules=`` — production path (RuleSet → derive_specs → fetch per spec)
  * ``categories=`` — LLM ad-hoc path (literal label list → fetch)

The legacy ``category=`` kwarg is gone; tests pre-v1.3 that used it have
been ported to the appropriate new mode.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.agents.query import QueryAgent, _attach_params
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.state import OrchestratorState
from tests._mocks import SAMPLE_ROOMS, MockFormaMCPClient, make_test_ruleset


def _empty_state() -> OrchestratorState:
    return {
        "project_id": "b.test",
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


# ---- _attach_params (pure function) ----------------------------------------


class TestAttachParams:
    def test_basic_flattening(self):
        el = {
            "id": "e1",
            "name": "Closet 11A",
            "properties": [
                {"name": "Department", "value": "Storage"},
                {"name": "Area", "value": 12.5},
            ],
        }
        result = _attach_params(el, "Rooms")
        assert result["id"] == "e1"
        assert result["name"] == "Closet 11A"
        assert result["category"] == "Rooms"
        assert result["params"] == {"Department": "Storage", "Area": 12.5}
        # Original properties preserved for traceability
        assert result["properties"] == el["properties"]

    def test_null_values_preserved(self):
        el = {
            "id": "e1",
            "name": "Closet",
            "properties": [
                {"name": "Department", "value": None},
                {"name": "Occupancy", "value": ""},
            ],
        }
        result = _attach_params(el, "Rooms")
        # Null and empty string are preserved as-is (the rules engine decides)
        assert result["params"]["Department"] is None
        assert result["params"]["Occupancy"] == ""

    def test_missing_properties_key(self):
        el = {"id": "e1", "name": "Room"}
        result = _attach_params(el, "Rooms")
        assert result["params"] == {}
        assert result["category"] == "Rooms"

    def test_empty_properties_list(self):
        el = {"id": "e1", "name": "Room", "properties": []}
        result = _attach_params(el, "Rooms")
        assert result["params"] == {}

    def test_property_with_missing_name_skipped(self):
        el = {
            "id": "e1",
            "name": "Room",
            "properties": [
                {"name": "Department", "value": "Admin"},
                {"value": "orphaned"},  # missing name → skip
                {"name": "", "value": "empty-name"},  # empty name → skip
                {"name": "Area", "value": 100},
            ],
        }
        result = _attach_params(el, "Rooms")
        assert result["params"] == {"Department": "Admin", "Area": 100}

    def test_duplicate_property_names_last_wins(self):
        """Revit shouldn't return duplicate property names, but if it does,
        the last value should win (defensive)."""
        el = {
            "id": "e1",
            "name": "Room",
            "properties": [
                {"name": "Department", "value": "First"},
                {"name": "Department", "value": "Last"},
            ],
        }
        result = _attach_params(el, "Rooms")
        assert result["params"]["Department"] == "Last"

    def test_complex_property_values(self):
        """Property values can be numbers, strings, lists, bools, or None."""
        el = {
            "id": "e1",
            "name": "Room",
            "properties": [
                {"name": "Area", "value": 12.345},
                {"name": "Visible", "value": True},
                {"name": "Tags", "value": ["fire-rated", "egress"]},
                {"name": "Notes", "value": "Multi-line\nnote"},
            ],
        }
        result = _attach_params(el, "Rooms")
        assert result["params"]["Area"] == 12.345
        assert result["params"]["Visible"] is True
        assert result["params"]["Tags"] == ["fire-rated", "egress"]
        assert "\n" in result["params"]["Notes"]

    def test_does_not_mutate_input(self):
        el = {
            "id": "e1",
            "name": "Room",
            "properties": [{"name": "Department", "value": "X"}],
        }
        original = {**el, "properties": list(el["properties"])}
        _attach_params(el, "Rooms")
        # Input should be untouched
        assert el == original


# ---- QueryAgent constructor — argument validation --------------------------


class TestQueryAgentConstruction:
    def test_requires_rules_or_categories(self, catalog):
        mcp = MockFormaMCPClient()
        with pytest.raises(ValueError, match="exactly one of"):
            QueryAgent(mcp=mcp, element_group_id="eg")

    def test_rejects_both_rules_and_categories(self, catalog):
        mcp = MockFormaMCPClient()
        rs = make_test_ruleset(target_category="Rooms")
        with pytest.raises(ValueError, match="exactly one of"):
            QueryAgent(
                mcp=mcp,
                element_group_id="eg",
                rules=rs,
                categories="Rooms",
                catalog=catalog,
            )

    def test_loads_default_catalog_when_omitted(self):
        mcp = MockFormaMCPClient()
        rs = make_test_ruleset(target_category="Rooms")
        # No catalog passed → falls back to OSTCatalog.load()
        agent = QueryAgent(mcp=mcp, element_group_id="eg", rules=rs)
        assert len(agent.specs) == 1
        assert agent.specs[0].category_label == "Rooms"


# ---- QueryAgent — rules-driven path ----------------------------------------


@pytest.mark.asyncio
async def test_rules_driven_returns_normalized_elements(catalog):
    mcp = MockFormaMCPClient()  # default elements = SAMPLE_ROOMS
    rs = make_test_ruleset(target_category="Rooms")
    agent = QueryAgent(mcp=mcp, element_group_id="eg-test", rules=rs, catalog=catalog)
    result = await agent.run(_empty_state())

    assert result["status"] == "checking"
    assert len(result["elements"]) == len(SAMPLE_ROOMS)
    first = result["elements"][0]
    assert "params" in first
    assert "Name" in first["params"]
    assert "Number" in first["params"]
    assert first["category"] == "Rooms"


@pytest.mark.asyncio
async def test_rules_driven_calls_correct_mcp_tool(catalog):
    mcp = MockFormaMCPClient()
    rs = make_test_ruleset(target_category="Walls", parameter="FireRating")
    agent = QueryAgent(mcp=mcp, element_group_id="eg-X", rules=rs, catalog=catalog)
    await agent.run(_empty_state())

    query_calls = mcp.calls_to("aecdm_query_elements")
    assert len(query_calls) == 1
    assert query_calls[0]["element_group_id"] == "eg-X"
    assert query_calls[0]["category"] == "Walls"  # AECDM label from catalog


@pytest.mark.asyncio
async def test_rules_driven_handles_empty_elements(catalog):
    mcp = MockFormaMCPClient(elements=[])
    rs = make_test_ruleset(target_category="Rooms")
    agent = QueryAgent(mcp=mcp, element_group_id="eg", rules=rs, catalog=catalog)
    result = await agent.run(_empty_state())

    assert result["status"] == "checking"
    assert result["elements"] == []


@pytest.mark.asyncio
async def test_failure_sets_failed_status_with_category(catalog):
    mcp = MockFormaMCPClient(fail_on={"aecdm_query_elements"})
    rs = make_test_ruleset(target_category="Rooms")
    agent = QueryAgent(mcp=mcp, element_group_id="eg", rules=rs, catalog=catalog)
    result = await agent.run(_empty_state())

    assert result["status"] == "failed"
    assert result["error"] is not None
    assert "Simulated failure" in result["error"]
    # Error names the offending category for operator triage
    assert "Rooms" in result["error"]


@pytest.mark.asyncio
async def test_preserves_iteration(catalog):
    mcp = MockFormaMCPClient()
    rs = make_test_ruleset(target_category="Rooms")
    agent = QueryAgent(mcp=mcp, element_group_id="eg", rules=rs, catalog=catalog)
    state = _empty_state()
    state["iteration"] = 2
    result = await agent.run(state)
    assert result["iteration"] == 2


@pytest.mark.asyncio
async def test_passes_through_all_sample_properties(catalog):
    """End-to-end: SAMPLE_ROOMS shape → flattened params should contain all keys."""
    mcp = MockFormaMCPClient()
    rs = make_test_ruleset(target_category="Rooms")
    agent = QueryAgent(mcp=mcp, element_group_id="eg", rules=rs, catalog=catalog)
    result = await agent.run(_empty_state())

    closet = next(e for e in result["elements"] if e["id"] == "elem-closet-11a")
    assert closet["params"]["Name"] == "Closet"
    assert closet["params"]["Number"] == "11A"
    assert closet["params"]["Department"] is None
    assert closet["params"]["Area"] == 0.83


# ---- QueryAgent — multi-category target_category (v1.2.2 carryover) -------


@pytest.mark.asyncio
async def test_multi_category_target(catalog):
    """target_category=[Walls, Doors] → one AECDM call per category, merged."""
    from tests._mocks import SAMPLE_WALLS

    mcp = MockFormaMCPClient(
        elements_by_category={
            "Walls": list(SAMPLE_WALLS),
            "Doors": [
                {"id": "door-1", "name": "D-101", "properties": [
                    {"name": "Family Name", "value": "Door-Single"},
                    {"name": "Width", "value": 0.914},
                ]},
            ],
        }
    )
    rs = make_test_ruleset(target_category=["Walls", "Doors"], parameter="FireRating")
    agent = QueryAgent(
        mcp=mcp, element_group_id="eg-multi", rules=rs, catalog=catalog
    )
    result = await agent.run(_empty_state())

    assert result["status"] == "checking"
    expected = len(SAMPLE_WALLS) + 1
    assert len(result["elements"]) == expected
    # Each element carries its source category for the downstream QC filter
    cats = {e["category"] for e in result["elements"]}
    assert cats == {"Walls", "Doors"}
    # AECDM was called once per category
    calls = mcp.calls_to("aecdm_query_elements")
    called_cats = sorted(c["category"] for c in calls)
    assert called_cats == ["Doors", "Walls"]


@pytest.mark.asyncio
async def test_multi_category_failure_reports_offending_category(catalog):
    """When the loop crashes mid-fetch the error must name the bad category."""
    from tests._mocks import SAMPLE_WALLS

    mcp = MockFormaMCPClient(
        elements_by_category={"Walls": list(SAMPLE_WALLS), "Doors": []},
        fail_on={"aecdm_query_elements"},
    )
    rs = make_test_ruleset(target_category=["Walls", "Doors"], parameter="FireRating")
    agent = QueryAgent(
        mcp=mcp, element_group_id="eg-multi", rules=rs, catalog=catalog
    )
    result = await agent.run(_empty_state())

    assert result["status"] == "failed"
    assert "Walls" in result["error"] or "Doors" in result["error"]


# ---- QueryAgent — categories= (LLM ad-hoc) path ----------------------------


@pytest.mark.asyncio
async def test_categories_only_single_string(catalog):
    """categories='Rooms' → one fetch, same shape as rules-driven."""
    mcp = MockFormaMCPClient()
    agent = QueryAgent(
        mcp=mcp, element_group_id="eg", categories="Rooms", catalog=catalog
    )
    result = await agent.run(_empty_state())

    assert result["status"] == "checking"
    assert all(e["category"] == "Rooms" for e in result["elements"])
    assert len(mcp.calls_to("aecdm_query_elements")) == 1


@pytest.mark.asyncio
async def test_categories_only_list(catalog):
    """categories=['Walls', 'Doors'] → two fetches."""
    from tests._mocks import SAMPLE_WALLS

    mcp = MockFormaMCPClient(
        elements_by_category={
            "Walls": list(SAMPLE_WALLS),
            "Doors": [{"id": "d1", "name": "D1", "properties": []}],
        }
    )
    agent = QueryAgent(
        mcp=mcp,
        element_group_id="eg",
        categories=["Walls", "Doors"],
        catalog=catalog,
    )
    result = await agent.run(_empty_state())

    assert {e["category"] for e in result["elements"]} == {"Walls", "Doors"}


@pytest.mark.asyncio
async def test_categories_alias_resolves_via_catalog(catalog):
    """LLM might say 'wall' (singular, lowercase) — catalog still finds 'Walls'."""
    from tests._mocks import SAMPLE_WALLS

    mcp = MockFormaMCPClient(elements_by_category={"Walls": list(SAMPLE_WALLS)})
    agent = QueryAgent(
        mcp=mcp, element_group_id="eg", categories="wall", catalog=catalog
    )
    result = await agent.run(_empty_state())

    # AECDM call used the canonical "Walls" label
    calls = mcp.calls_to("aecdm_query_elements")
    assert calls[0]["category"] == "Walls"
    # Elements stamped with the canonical display
    assert all(e["category"] == "Walls" for e in result["elements"])


@pytest.mark.asyncio
async def test_categories_unknown_label_skipped(catalog):
    """Catalog miss → category dropped, no crash."""
    mcp = MockFormaMCPClient()
    agent = QueryAgent(
        mcp=mcp, element_group_id="eg", categories="Bananas", catalog=catalog
    )
    result = await agent.run(_empty_state())

    # No specs produced → empty elements, status still checking (not failed)
    assert result["status"] == "checking"
    assert result["elements"] == []
    # No AECDM call attempted for the unknown category
    assert mcp.calls_to("aecdm_query_elements") == []
