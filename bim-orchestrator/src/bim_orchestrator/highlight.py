"""Show a finding's elements in Revit — one view per level (2026-08-17).

The "Highlight in Revit" action used to be two calls: ``select_elements`` +
``zoom_to_elements``. That is fine for one element and misleading for many:
``zoom_to_elements`` is Revit's ``ShowElements``, which activates ONE view for
the whole set, so a set spanning L3/L4/L5 framed the L3 door and left the other
two selected but off-screen (live-probed 2026-08-17).

This module walks the levels instead: group the elements by their level, pick
that level's plan, activate it, optionally colour them, frame them. It is pure
orchestration over an injected Revit client — the AuditHub service holds zero
business logic (P3 D6/D7), so the endpoint just calls in here.

Two honesty rules this module keeps, because they are easy to blur:

* **Colour is presentation, never evidence.** A per-element override paints OUR
  verdict onto the model; it does not re-evaluate anything and goes stale the
  moment the model changes. The native re-check artifacts stay the schedule
  (``verification_views``) and — once the addin grows filter operators — the
  view filter. So ``color`` is OPT-IN, off by default, and ``reset`` clears it.
* **Colour WRITES.** A graphic override lives in the view, so the document
  becomes modified (cloud models then need Sync or discard). Navigation alone
  (open/select/zoom) writes nothing. Never call either from an unattended run:
  ``ShowElements`` can raise a modal dialog that would halt the watchdog.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Per-element level lookups are one addin round trip each and Revit's API is
# single-threaded, so a huge set would freeze the UI mid-walk for no benefit
# (nobody reads 300 highlighted doors one plan at a time). Above the cap we
# degrade to the legacy one-shot select+zoom and SAY so in the result.
MAX_PER_LEVEL_WALK = 120

# A view whose name IS its level's name is the plain working plan — the one
# Revit's own ShowElements picked for L2/L3/L5 on Snowdon. Everything else on a
# level is a special-purpose plan (Life Safety, Wall Base, Facade Modeling, …).
_PLAN_VIEW_TYPE = "FloorPlan"


@dataclass
class ViewHighlight:
    """What happened for ONE level (or for the whole set, when degraded)."""

    element_ids: list[int]
    status: str  # shown | cleared | no_view | no_level | degraded | error
    level_id: int | None = None
    level_name: str | None = None
    view_id: int | None = None
    view_name: str | None = None
    colored: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HighlightOutcome:
    """The whole action: per-level results + what the user ends up looking at."""

    views: list[ViewHighlight] = field(default_factory=list)
    selected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"views": [v.to_dict() for v in self.views], "selected": self.selected}


def pick_plan_view(views: list[dict[str, Any]], level_id: int) -> dict[str, Any] | None:
    """The plan to activate for ``level_id`` — deterministic, or None.

    Snowdon has 8-13 FloorPlan views per level, so "the level's plan" needs a
    rule rather than a guess. Prefer the view whose NAME equals its LEVEL NAME
    (the plain working plan — this reproduces the view Revit's own
    ``ShowElements`` activated in the live probe), then the lowest element id so
    two runs never disagree. Templates are excluded: they can't be activated.
    """
    candidates = [
        v
        for v in views
        if str(v.get("viewType") or "") == _PLAN_VIEW_TYPE
        and not v.get("isTemplate")
        and v.get("levelId") == level_id
    ]
    if not candidates:
        return None
    named = [
        v
        for v in candidates
        if str(v.get("name") or "").strip().casefold()
        == str(v.get("levelName") or "").strip().casefold()
    ]
    pool = named or candidates
    return min(pool, key=lambda v: int(v.get("id") or 0))


async def _group_by_level(
    client: Any, element_ids: list[int]
) -> tuple[dict[int, list[int]], list[int]]:
    """(level_id → ids, ids whose level we could not resolve).

    Insertion-ordered by FIRST APPEARANCE in ``element_ids`` — so the walk
    follows the order the caller listed (the findings table's order), which is
    deterministic without needing elevations, and makes the LAST view the user
    is left looking at predictable.
    """
    grouped: dict[int, list[int]] = {}
    unresolved: list[int] = []
    for eid in element_ids:
        try:
            info = await client.get_element_info(eid)
        except Exception as exc:  # one unreadable element must not sink the rest
            log.warning("highlight.element_info_failed", element_id=eid, error=str(exc))
            unresolved.append(eid)
            continue
        level_id = (info or {}).get("levelId")
        if not isinstance(level_id, int) or level_id < 0:
            unresolved.append(eid)
            continue
        grouped.setdefault(level_id, []).append(eid)
    return grouped, unresolved


async def _legacy_show(client: Any, element_ids: list[int], detail: str) -> ViewHighlight:
    """The pre-2026-08-17 behaviour: select + let Revit pick one view.

    Kept as the honest fallback for every case the per-level walk can't serve
    (too many elements, no level on any element, no plan for a level) — the
    user still gets the elements selected and one of them framed, and the
    result says which case it was rather than silently doing less.
    """
    await client.select_elements(element_ids)
    await client.zoom_to_elements(element_ids)
    return ViewHighlight(
        element_ids=list(element_ids), status="degraded", detail=detail
    )


async def highlight_elements(
    client: Any,
    element_ids: list[int],
    *,
    color: dict[str, int] | None = None,
    reset: bool = False,
    per_level: bool = True,
    max_per_level_walk: int = MAX_PER_LEVEL_WALK,
) -> HighlightOutcome:
    """Show ``element_ids`` in Revit, one view per level.

    ``color`` — optional ``{"r","g","b"}``; when set, each level's elements get
    a per-element graphic override in that level's plan. **This writes to the
    model** (see the module docstring). Off by default.

    ``reset`` — clear the overrides for the same elements in the same views
    instead of showing them. No view is activated and no selection changes: a
    cleanup is not a navigation.

    ``per_level=False`` — force the legacy one-shot select+zoom.
    """
    ids = [int(e) for e in element_ids]
    if not ids:
        return HighlightOutcome()

    if not per_level:
        legacy = await _legacy_show(client, ids, "per_level=False")
        return HighlightOutcome(views=[legacy], selected=len(ids))
    if len(ids) > max_per_level_walk:
        detail = (
            f"{len(ids)} elements exceeds the per-level walk cap "
            f"({max_per_level_walk}) — showed them in one view instead"
        )
        legacy = await _legacy_show(client, ids, detail)
        return HighlightOutcome(views=[legacy], selected=len(ids))

    grouped, unresolved = await _group_by_level(client, ids)
    if not grouped:
        return HighlightOutcome(
            views=[await _legacy_show(client, ids, "no element resolved to a level")],
            selected=len(ids),
        )

    views = await client.get_views()
    results: list[ViewHighlight] = []

    for level_id, group_ids in grouped.items():
        plan = pick_plan_view(views, level_id)
        if plan is None:
            # The level exists but has no activatable plan — say so; the final
            # selection below still includes these elements.
            results.append(
                ViewHighlight(
                    element_ids=group_ids,
                    status="no_view",
                    level_id=level_id,
                    detail=f"no non-template {_PLAN_VIEW_TYPE} view on level {level_id}",
                )
            )
            continue

        view_id = int(plan.get("id") or 0)
        entry = ViewHighlight(
            element_ids=group_ids,
            status="cleared" if reset else "shown",
            level_id=level_id,
            level_name=plan.get("levelName"),
            view_id=view_id,
            view_name=plan.get("name"),
        )
        try:
            if reset:
                await client.override_element_graphics(
                    view_id=view_id, element_ids=group_ids, reset=True
                )
            else:
                # Activate FIRST: zoom is ShowElements, and with the right view
                # already active it frames there instead of choosing its own.
                await client.open_view(view_id)
                if color is not None:
                    await client.override_element_graphics(
                        view_id=view_id, element_ids=group_ids, color=color
                    )
                    entry.colored = True
                await client.zoom_to_elements(group_ids)
        except Exception as exc:
            # One bad level degrades that level only — the others still show.
            entry.status = "error"
            entry.detail = str(exc)
            log.warning(
                "highlight.level_failed", level_id=level_id, view_id=view_id, error=str(exc)
            )
        results.append(entry)

    if unresolved:
        results.append(
            ViewHighlight(
                element_ids=unresolved,
                status="no_level",
                detail="element has no level (or its info could not be read)",
            )
        )

    selected = 0
    if not reset:
        # ONE selection at the end, over the whole set: selecting per level would
        # leave only the last group selected, and selection does not move the
        # camera, so this can't undo the framing above.
        await client.select_elements(ids)
        selected = len(ids)

    log.info(
        "highlight.done",
        elements=len(ids),
        levels=len(grouped),
        unresolved=len(unresolved),
        colored=bool(color) and not reset,
        reset=reset,
    )
    return HighlightOutcome(views=results, selected=selected)


__all__ = [
    "MAX_PER_LEVEL_WALK",
    "HighlightOutcome",
    "ViewHighlight",
    "highlight_elements",
    "pick_plan_view",
]
