"""Per-level "Highlight in Revit" walk (2026-08-17).

The behaviour these tests pin was measured live on Snowdon before the module
existed: ``zoom_to_elements`` is Revit's ``ShowElements``, so a set spanning
L3/L4/L5 activated ONE view (L3) and left the other two doors selected but
off-screen. The walk fixes that; the fallbacks keep every case the walk can't
serve honest rather than silently doing less.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.highlight import (
    MAX_PER_LEVEL_WALK,
    highlight_elements,
    pick_plan_view,
)

# Shaped exactly like the live `revit_get_views` payload (id/name/viewType/
# levelId/levelName/isTemplate), trimmed to the Snowdon L2+L3 plans that
# matter — including the special-purpose plans that must NOT be chosen.
_VIEWS = [
    {"id": 607509, "name": "L2_SD", "viewType": "FloorPlan",
     "levelId": 593177, "levelName": "L2", "isTemplate": False},
    {"id": 756102, "name": "L2 Wall Top", "viewType": "FloorPlan",
     "levelId": 593177, "levelName": "L2", "isTemplate": False},
    {"id": 1350631, "name": "L2", "viewType": "FloorPlan",
     "levelId": 593177, "levelName": "L2", "isTemplate": False},
    {"id": 2156937, "name": "L2 Life Safety Plan", "viewType": "FloorPlan",
     "levelId": 593177, "levelName": "L2", "isTemplate": False},
    {"id": 853429, "name": "L2", "viewType": "AreaPlan",
     "levelId": 593177, "levelName": "L2", "isTemplate": False},
    {"id": 1350675, "name": "L3", "viewType": "FloorPlan",
     "levelId": 593147, "levelName": "L3", "isTemplate": False},
    {"id": 607499, "name": "L3_SD", "viewType": "FloorPlan",
     "levelId": 593147, "levelName": "L3", "isTemplate": False},
    # A level whose only plan is a template — not activatable.
    {"id": 999001, "name": "L9", "viewType": "FloorPlan",
     "levelId": 593199, "levelName": "L9", "isTemplate": True},
]

_L2, _L3, _L9 = 593177, 593147, 593199


class FakeRevit:
    """Records the call ORDER — the whole point is that the view is activated
    before the zoom, and that the selection lands once, at the end."""

    def __init__(self, levels: dict[int, int], *, views=None, fail_open_view=None):
        self._levels = levels          # element id -> level id (-1 = no level)
        self._views = views if views is not None else _VIEWS
        self._fail_open_view = fail_open_view
        self.calls: list[tuple] = []

    async def get_element_info(self, element_id):
        return {"id": element_id, "levelId": self._levels[element_id]}

    async def get_views(self):
        self.calls.append(("get_views",))
        return self._views

    async def open_view(self, view_id, **kw):
        self.calls.append(("open_view", view_id))
        if self._fail_open_view is not None and view_id == self._fail_open_view:
            raise RuntimeError("view could not be activated")
        return {"viewId": view_id}

    async def override_element_graphics(self, *, view_id, element_ids, color=None,
                                        transparency=None, reset=False):
        self.calls.append(
            ("override", view_id, list(element_ids), None if reset else color, reset)
        )
        return {"ok": True}

    async def zoom_to_elements(self, ids):
        self.calls.append(("zoom", list(ids)))
        return {"zoomed": len(ids)}

    async def select_elements(self, ids):
        self.calls.append(("select", list(ids)))
        return {"selected": len(ids)}


class TestPickPlanView:
    def test_prefers_the_plan_named_after_its_level(self):
        """Snowdon has 8-13 FloorPlans per level. The plain one (name == level
        name) is what Revit's own ShowElements activated in the live probe —
        not 'L2_SD', not 'L2 Life Safety Plan'."""
        assert pick_plan_view(_VIEWS, _L2)["id"] == 1350631

    def test_ignores_other_view_types_and_templates(self):
        # The AreaPlan also called "L2" must not win; a template-only level
        # yields None rather than an unactivatable view.
        assert pick_plan_view(_VIEWS, _L2)["viewType"] == "FloorPlan"
        assert pick_plan_view(_VIEWS, _L9) is None

    def test_falls_back_to_lowest_id_when_no_name_matches(self):
        views = [
            {"id": 300, "name": "Plan B", "viewType": "FloorPlan",
             "levelId": 7, "levelName": "L7", "isTemplate": False},
            {"id": 200, "name": "Plan A", "viewType": "FloorPlan",
             "levelId": 7, "levelName": "L7", "isTemplate": False},
        ]
        # Deterministic tie-break so two runs never disagree.
        assert pick_plan_view(views, 7)["id"] == 200

    def test_unknown_level_is_none(self):
        assert pick_plan_view(_VIEWS, 424242) is None


@pytest.mark.asyncio
class TestPerLevelWalk:
    async def test_each_level_gets_its_own_view_activated_before_the_zoom(self):
        """The measured defect: 3 doors on 3 levels used to frame ONE. Now each
        level's plan is activated, then that level's doors are framed in it."""
        revit = FakeRevit({11: _L2, 22: _L3})
        out = await highlight_elements(revit, [11, 22])

        assert [c for c in revit.calls if c[0] in {"open_view", "zoom"}] == [
            ("open_view", 1350631), ("zoom", [11]),
            ("open_view", 1350675), ("zoom", [22]),
        ]
        assert [(v.level_id, v.view_id, v.status) for v in out.views] == [
            (_L2, 1350631, "shown"), (_L3, 1350675, "shown"),
        ]

    async def test_walk_order_follows_the_callers_list(self):
        # Deterministic without needing elevations, and it makes the view the
        # user is LEFT looking at predictable (the last group's).
        revit = FakeRevit({22: _L3, 11: _L2})
        out = await highlight_elements(revit, [22, 11])
        assert [v.level_id for v in out.views] == [_L3, _L2]

    async def test_selection_happens_once_over_the_whole_set_at_the_end(self):
        """Selecting per level would leave only the last group selected —
        the user must end up with everything they asked for selected."""
        revit = FakeRevit({11: _L2, 22: _L3})
        out = await highlight_elements(revit, [11, 22])
        selects = [c for c in revit.calls if c[0] == "select"]
        assert selects == [("select", [11, 22])]
        assert revit.calls[-1] == ("select", [11, 22])
        assert out.selected == 2

    async def test_colour_is_opt_in_and_writes_nothing_by_default(self):
        # A graphic override modifies the document; navigation must not.
        revit = FakeRevit({11: _L2})
        out = await highlight_elements(revit, [11])
        assert not [c for c in revit.calls if c[0] == "override"]
        assert out.views[0].colored is False

    async def test_colour_overrides_per_view_when_asked(self):
        revit = FakeRevit({11: _L2, 22: _L3})
        red = {"r": 255, "g": 0, "b": 0}
        out = await highlight_elements(revit, [11, 22], color=red)
        assert [c for c in revit.calls if c[0] == "override"] == [
            ("override", 1350631, [11], red, False),
            ("override", 1350675, [22], red, False),
        ]
        assert all(v.colored for v in out.views)
        # Colour is applied AFTER the view is active and BEFORE the zoom.
        order = [c[0] for c in revit.calls if c[0] in {"open_view", "override", "zoom"}]
        assert order == ["open_view", "override", "zoom"] * 2

    async def test_reset_clears_without_navigating_or_selecting(self):
        """A cleanup is not a navigation: it must not yank the user's view or
        selection around just to undo colours."""
        revit = FakeRevit({11: _L2, 22: _L3})
        out = await highlight_elements(revit, [11, 22], reset=True)
        assert [c for c in revit.calls if c[0] == "override"] == [
            ("override", 1350631, [11], None, True),
            ("override", 1350675, [22], None, True),
        ]
        assert not [c for c in revit.calls if c[0] in {"open_view", "zoom", "select"}]
        assert [v.status for v in out.views] == ["cleared", "cleared"]
        assert out.selected == 0


@pytest.mark.asyncio
class TestHonestFallbacks:
    async def test_per_level_false_restores_the_legacy_one_shot(self):
        revit = FakeRevit({11: _L2, 22: _L3})
        out = await highlight_elements(revit, [11, 22], per_level=False)
        assert revit.calls == [("select", [11, 22]), ("zoom", [11, 22])]
        assert out.views[0].status == "degraded"

    async def test_too_many_elements_degrades_and_says_so(self):
        n = MAX_PER_LEVEL_WALK + 1
        revit = FakeRevit({i: _L2 for i in range(n)})
        out = await highlight_elements(revit, list(range(n)))
        assert [c[0] for c in revit.calls] == ["select", "zoom"]
        assert out.views[0].status == "degraded"
        assert str(MAX_PER_LEVEL_WALK) in (out.views[0].detail or "")

    async def test_level_without_an_activatable_plan_is_reported_not_skipped(self):
        # L9's only plan is a template. The element still ends up selected.
        revit = FakeRevit({11: _L2, 99: _L9})
        out = await highlight_elements(revit, [11, 99])
        statuses = {v.level_id: v.status for v in out.views}
        assert statuses == {_L2: "shown", _L9: "no_view"}
        assert revit.calls[-1] == ("select", [11, 99])

    async def test_elements_without_a_level_are_reported_and_still_selected(self):
        revit = FakeRevit({11: _L2, 77: -1})
        out = await highlight_elements(revit, [11, 77])
        no_level = [v for v in out.views if v.status == "no_level"]
        assert no_level and no_level[0].element_ids == [77]
        assert revit.calls[-1] == ("select", [11, 77])

    async def test_no_element_resolves_a_level_degrades_to_legacy(self):
        revit = FakeRevit({77: -1, 78: -1})
        out = await highlight_elements(revit, [77, 78])
        assert [c[0] for c in revit.calls] == ["select", "zoom"]
        assert out.views[0].status == "degraded"

    async def test_one_bad_level_does_not_sink_the_others(self):
        revit = FakeRevit({11: _L2, 22: _L3}, fail_open_view=1350631)
        out = await highlight_elements(revit, [11, 22])
        by_level = {v.level_id: v for v in out.views}
        assert by_level[_L2].status == "error"
        assert by_level[_L3].status == "shown"
        assert out.selected == 2

    async def test_unreadable_element_info_does_not_sink_the_walk(self):
        class _Flaky(FakeRevit):
            async def get_element_info(self, element_id):
                if element_id == 66:
                    raise RuntimeError("addin hiccup")
                return await super().get_element_info(element_id)

        revit = _Flaky({11: _L2})
        out = await highlight_elements(revit, [11, 66])
        assert {v.status for v in out.views} == {"shown", "no_level"}

    async def test_empty_input_does_nothing(self):
        revit = FakeRevit({})
        out = await highlight_elements(revit, [])
        assert revit.calls == [] and out.selected == 0 and out.views == []


# ── transport parity for the highlight surface (2026-08-26) ────────────────
#
# `highlight_elements` takes whichever client `make_revit_client` returns.
# The four UI-action methods existed only on RevitHTTPClient; on a machine
# with the Node bridge configured the factory returns the STDIO client, and
# the service's "Highlight in Revit" died with AttributeError. Nobody had
# ever hit it on that transport because a separate UI bug (axes booleans
# read as HealthStatus strings) kept the button permanently disabled. This
# pins the whole surface on BOTH classes so a new highlight dependency
# cannot ship on one transport only.
def test_highlight_surface_exists_on_both_transports():
    from bim_orchestrator.mcp_clients.revit import RevitHTTPClient, RevitMCPClient

    needed = (
        "get_element_info",
        "get_views",
        "open_view",
        "override_element_graphics",
        "select_elements",
        "zoom_to_elements",
    )
    for cls in (RevitMCPClient, RevitHTTPClient):
        missing = [m for m in needed if not callable(getattr(cls, m, None))]
        assert not missing, f"{cls.__name__} lacks highlight methods: {missing}"
