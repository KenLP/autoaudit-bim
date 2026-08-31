"""Tests for RevitQueryAgent._containing_space enrichment (v1.4-K3 Layer 2b).

Verifies the geometry-gated spatial enrichment: a duct centroid is mapped to
the SMALLEST-volume MEP space whose bbox contains it, and the enrichment only
runs when a rule references `_containing_space`.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill, RuleSet
from tests._mocks import MockRevitMCPClient


@pytest.fixture(scope="module")
def catalog():
    return OSTCatalog.load()


def _bbox(x0, y0, z0, x1, y1, z1):
    return {"min": {"x": x0, "y": y0, "z": z0}, "max": {"x": x1, "y": y1, "z": z1}}


def _mark_rule(*, compose: bool) -> Rule:
    if compose:
        autofill = RuleAutofill(
            strategy="compose_template",
            template="{_containing_space}-{Reference Level}-{System Name}-{seq}",
            sequence_scope=["_containing_space"],
        )
    else:
        autofill = RuleAutofill(strategy="none")
    return Rule(
        id="ducts.mark.required",
        parameter="Mark",
        requirement="present_and_nonempty",
        category="Ducts",
        severity_tag="missing_required_param",
        description="Mark required",
        fixability="auto",
        autofill=autofill,
    )


def _ruleset(rule: Rule) -> RuleSet:
    return RuleSet(scenario="m", target_category="Ducts", rules=[rule])


def _duct_listing():
    return [
        {"id": 100, "name": "Duct A", "category": "Ducts",
         "categoryEnum": "OST_DuctCurves", "typeId": 9000},
        {"id": 101, "name": "Duct B", "category": "Ducts",
         "categoryEnum": "OST_DuctCurves", "typeId": 9000},
    ]


def _element_info():
    duct_params = [{"name": "Mark", "value": None, "valueString": None}]
    return {
        # Duct A centroid (15,15,5) → inside Small Room (tightest) + Big Room
        100: {"id": 100, "name": "Duct A", "category": "Ducts", "typeId": 9000,
              "parameters": duct_params, "boundingBox": _bbox(14, 14, 4, 16, 16, 6)},
        # Duct B centroid (61,61,5) → inside Big Room only
        101: {"id": 101, "name": "Duct B", "category": "Ducts", "typeId": 9000,
              "parameters": duct_params, "boundingBox": _bbox(60, 60, 4, 62, 62, 6)},
        # Spaces (bbox lives in element_info so get_element_geometry resolves it)
        7001: {"id": 7001, "name": "Big Room", "boundingBox": _bbox(0, 0, 0, 100, 100, 10)},
        7002: {"id": 7002, "name": "Small Room", "boundingBox": _bbox(10, 10, 0, 20, 20, 10)},
    }


def _client():
    return MockRevitMCPClient(
        elements_by_category={"OST_DuctCurves": _duct_listing()},
        element_info=_element_info(),
        spaces=[
            {"id": 7001, "name": "Big Room", "volume": 100000.0},
            {"id": 7002, "name": "Small Room", "volume": 1000.0},
        ],
    )


def _state():
    return {  # type: ignore[return-value]
        "project_id": "t", "iteration": 0, "max_iterations": 1,
        "elements": [], "findings": [], "proposed_fixes": [],
        "status": "checking", "error": None,
    }


@pytest.mark.asyncio
class TestSpaceEnrichment:
    async def test_smallest_containing_space_wins(self, catalog):
        async with _client() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=_ruleset(_mark_rule(compose=True)), catalog=catalog
            ).run(_state())
        by_id = {el["id"]: el for el in state["elements"]}
        # Duct A is inside both Big + Small; smallest volume (Small Room) wins.
        assert by_id["100"]["params"]["_containing_space"] == "Small Room"
        # Duct B is only inside Big Room.
        assert by_id["101"]["params"]["_containing_space"] == "Big Room"

    async def test_enrichment_skipped_without_compose_template(self, catalog):
        """No rule references _containing_space → no spaces fetched, no param."""
        async with _client() as client:
            state = await RevitQueryAgent(
                mcp=client, rules=_ruleset(_mark_rule(compose=False)), catalog=catalog
            ).run(_state())
            assert client.calls_to("revit_list_spaces") == []
        for el in state["elements"]:
            assert "_containing_space" not in el["params"]

    async def test_no_spaces_degrades_gracefully(self, catalog):
        client = MockRevitMCPClient(
            elements_by_category={"OST_DuctCurves": _duct_listing()},
            element_info=_element_info(),
            spaces=[],  # none placed
        )
        async with client:
            state = await RevitQueryAgent(
                mcp=client, rules=_ruleset(_mark_rule(compose=True)), catalog=catalog
            ).run(_state())
        # No crash; no element gets a space.
        for el in state["elements"]:
            assert "_containing_space" not in el["params"]


@pytest.mark.asyncio
class TestEnrichmentFailsSoft:
    """L-04's sibling on the read side: enrichment is best-effort, so it must
    never be the thing that kills a run.

    The call sat OUTSIDE `run()`'s try, the inner `float(space["volume"])` sat
    outside `_space_box`'s try, and neither `gather` used
    `return_exceptions` — so ONE space whose volume came back as text threw
    straight out of the agent, took the run with it, and left its sibling
    geometry tasks unawaited.

    Failing soft is safe here specifically because a MISSING
    `_containing_space` cannot produce a wrong value: `qc._fill_template`
    returns None when any token is absent, so the affected rules drop to Path
    A rather than composing a malformed Mark.
    """

    async def test_an_unparseable_volume_does_not_kill_the_run(self, catalog):
        client = MockRevitMCPClient(
            elements_by_category={"OST_DuctCurves": _duct_listing()},
            element_info=_element_info(),
            spaces=[
                {"id": 7001, "name": "Big Room", "volume": "12,5 m3"},  # text!
                {"id": 7002, "name": "Small Room", "volume": 1000.0},
            ],
        )
        async with client:
            state = await RevitQueryAgent(
                mcp=client, rules=_ruleset(_mark_rule(compose=True)), catalog=catalog
            ).run(_state())

        # The run completed normally...
        assert state["status"] != "failed"
        # ...the readable space still enriched what it contains...
        by_id = {el["id"]: el for el in state["elements"]}
        assert by_id["100"]["params"]["_containing_space"] == "Small Room"
        # ...and the unreadable one simply contributed nothing (Duct B was only
        # inside Big Room, so it now has no space rather than a wrong one).
        assert "_containing_space" not in by_id["101"]["params"]

    async def test_a_geometry_explosion_is_recorded_in_coverage(self, catalog):
        """When enrichment fails outright the run continues, but the artifact
        must say it tried — an empty result has to state whether it looked."""
        client = MockRevitMCPClient(
            elements_by_category={"OST_DuctCurves": _duct_listing()},
            element_info=_element_info(),
            spaces=[{"id": 7001, "name": "Big Room", "volume": 1.0}],
        )

        async def boom(*a, **kw):
            raise RuntimeError("geometry service down")

        async with client:
            agent = RevitQueryAgent(
                mcp=client, rules=_ruleset(_mark_rule(compose=True)), catalog=catalog
            )
            # Break the enrichment as a whole (not a single element).
            agent._enrich_containing_space = boom  # type: ignore[method-assign]
            state = await agent.run(_state())

        assert state["status"] != "failed"
        cov = state.get("query_coverage") or {}
        assert cov["space_enrichment"]["status"] == "failed"
        assert "geometry service down" in cov["space_enrichment"]["detail"]
