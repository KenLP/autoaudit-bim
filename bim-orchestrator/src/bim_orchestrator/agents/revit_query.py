"""Unified Revit Query Agent (v1.3) — rules-driven fetch via Revit MCP.

v1.3 merges the two pre-existing Revit query agents (rooms-only
``RevitQueryAgent`` + generic ``RevitElementsQueryAgent``) into a single
class that handles every category via ``list_elements`` + per-element
``get_element_info``. Categories are derived from the active ``RuleSet``;
the legacy ``CategorySpec``-driven entry point is gone.

Pipeline per spec
-----------------
1. ``revit_list_elements(spec.backend_category, limit=..., only_instances=True)``
2. For each returned instance, in parallel (bounded by ``fetch_concurrency``):
     a. ``revit_get_element_info(instance_id)`` → instance parameters.
     b. If ``instance.typeId > 0``: ``revit_get_element_info(type_id)`` →
        type parameters (cached, so N instances sharing one type hit the
        bridge once).
     c. Merge: build ``element.params`` from the union of names in
        ``spec.params`` (after stripping any ``type.`` prefix). For each
        bare name, ``Instance > Type`` precedence; the Type-only value
        is also exposed under ``type.<name>`` so a rule that needs to
        bypass the override can reach it explicitly.
     d. If ``spec.follow_host``: read ``Host Id`` (instance param or
        instance.hostId), fetch host instance → host type, surface each
        name in ``spec.host_params`` under ``host.<name>``.
     e. If category label is ``Rooms``: post-process to add
        ``areaMetric`` (m²) + ``Unbounded Height (m)`` mirrors derived
        from imperial parameters, plus ``levelName`` / ``perimeter``
        breadcrumbs from the bulk ``list_elements`` response.

Param precedence cheat-sheet
----------------------------
A rule reading ``params['Fire Rating']`` gets the Instance value if
present, else falls back to the Type value. A rule that explicitly
wants the Type value writes ``parameter='type.Fire Rating'`` — the
unified agent recognises the ``type.`` prefix and only consults the
Type side. Host hops always read the host's Type (matches v1.2
behaviour — fire-rating rules want host wall TYPE rating).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from bim_orchestrator.mcp_clients.revit import (
    AnyRevitClient,
    RevitEnvelopeError,
    feet_to_meters,
    sqft_to_sqm,
)
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.param_catalog import ParamCatalog, load_param_catalog
from bim_orchestrator.policies.query_specs import QuerySpec, derive_specs_with_coverage
from bim_orchestrator.policies.rules_schema import RuleSet
from bim_orchestrator.state import OrchestratorState

log = structlog.get_logger(__name__)

# Param-name prefix used by rules that want the Type value bypassing the
# Instance>Type precedence override. Kept in sync with the convention used
# by derive_specs / QC.
_TYPE_PREFIX = "type."

# v1.4-K25: param-catalog dimensions whose raw flattened value is an opaque
# integer/ElementId, NOT the human-readable text. ``option`` enums (wall
# ``Function`` → integer ``1`` whose display is ``"Exterior"``) and
# ``reference`` ElementId params (``Reference Level`` → levelId whose display is
# ``"L2"``) both flatten to a number that text rules (scope_filter regex,
# matches_regex, string relation_compare) can never match. For these we prefer
# the addin's ``valueString`` so element.params carries the display text.
_DISPLAY_DIMENSIONS = frozenset({"option", "reference"})


def _drop_failures(results: list[Any], stage: str) -> list[Any]:
    """Filter a ``gather(return_exceptions=True)`` result set (L-11).

    ``return_exceptions=True`` is what stops one bad element from aborting the
    whole fan-out and orphaning its siblings — but it also captures
    BaseExceptions, so a cancellation would otherwise be quietly demoted to a
    per-element failure and dropped. Re-raise those; log and drop the rest.
    Same rule as the hydrate fan-out (`_hydrate_all`) and the geometry agent.
    """
    out: list[Any] = []
    for res in results:
        if isinstance(res, BaseException) and not isinstance(res, Exception):
            raise res
        if isinstance(res, Exception):
            log.warning(
                f"revit_query.{stage}_failed",
                error=str(res),
                note="element skipped; enrichment continues",
            )
            continue
        out.append(res)
    return out


class RevitQueryAgent:
    """Rules-driven Revit query node. Works for any BuiltInCategory.

    Construction::

        catalog = OSTCatalog.load()
        agent = RevitQueryAgent(
            mcp=client,
            rules=qc.rules,
            catalog=catalog,
            max_elements_per_category=200,  # optional cap
            fetch_concurrency=4,            # optional bridge throttle
        )

    The agent never touches Forma/ACC — pair it with a separate
    ``DesignAgent`` if Revit + Forma run in the same graph (the
    orchestrator's ``run_revit`` mode does exactly that).
    """

    def __init__(
        self,
        mcp: AnyRevitClient,
        *,
        rules: RuleSet,
        catalog: OSTCatalog | None = None,
        fetch_concurrency: int = 4,
        max_elements_per_category: int | None = 300,
        bulk_fields: bool = False,
        config_dir: "Path | None" = None,
    ) -> None:
        self._mcp = mcp
        self._catalog = catalog if catalog is not None else OSTCatalog.load()
        # config_dir = the rules-file dir, so a pack-local lookup table's host
        # params are hydrated (host-hop). Falls back to config/ inside load_lookup.
        self._specs, self._coverage = derive_specs_with_coverage(
            rules, backend="revit", catalog=self._catalog, config_dir=config_dir
        )
        self._fetch_concurrency = max(1, fetch_concurrency)
        # v1.4-K3 (P0): hard cap on elements fetched+hydrated per category.
        # Bounds demo/share runs to a predictable element budget (default 300).
        # Pass None to disable (unbounded — pre-K3 behaviour).
        self._max_elements_per_category = max_elements_per_category
        # v1.4-K3 (P1): opt-in bulk param fetch. When True, eligible specs
        # (no host hop, not Rooms) fetch all instance-level params in ONE
        # ``revit_find_elements`` call instead of N ``get_element_info`` calls.
        # Correctness is preserved by pre-seeding the instance cache and
        # letting the existing hydrate path still fetch + merge the Type
        # (so implicit Instance>Type fallback, e.g. fire-rating, still works).
        # Default off — opt in for instance-dense scenarios (Ducts/Walls).
        self._bulk_fields = bulk_fields
        # v1.4-K3 (Layer 2b): only do the geometry-heavy space-containment
        # enrichment when a rule actually references `_containing_space`.
        self._needs_space = self._space_enrichment_needed(rules)
        # v1.4-K3: when a compose_template rule is present, also surface each
        # param's display string (valueString) in `params_display` so the
        # template can render "L2" instead of the raw levelId for
        # ElementId-valued params like Reference Level.
        self._compose_template = any(
            getattr(getattr(r, "autofill", None), "strategy", None)
            == "compose_template"
            for r in rules.rules
        )
        # v1.4-K25: per-spec set of params that must be surfaced as their display
        # string (valueString) rather than the raw flattened value — option enums
        # + reference ElementIds (see _DISPLAY_DIMENSIONS). Derived from the
        # param catalog; empty when the catalog is unavailable (graceful no-op).
        self._param_catalog: ParamCatalog | None = self._try_load_param_catalog()
        self._display_names: dict[str, frozenset[str]] = self._compute_display_names()
        # 2026-08-18: display-name sets for HOST categories (see
        # `_host_display_names`) — keyed by the category string the addin
        # reports for the host, filled lazily on the first hop per category.
        self._host_display_cache: dict[str, frozenset[str]] = {}
        # Caches reset per ``run()`` so stale Revit state from a prior
        # iteration doesn't bleed into the next.
        self._type_info_cache: dict[int, dict[str, Any]] = {}
        self._instance_info_cache: dict[int, dict[str, Any]] = {}
        # v1.4-K8: in-flight dedup for type fetches. The hydration fan-out runs
        # ~N coroutines concurrently; without this, every duct that shares a
        # type checks the (still-empty) cache and fires its own get_element_info
        # before any of them populates it — a thundering herd (300 fetches for
        # 3 types). Holding one Future per type_id collapses the burst to a
        # single fetch; later callers await it. Keyed by type_id; cleared per run.
        self._type_inflight: dict[int, asyncio.Future[dict[str, Any] | None]] = {}
        # L-10: the same, one layer down — see `_get_instance`.
        self._instance_inflight: dict[int, asyncio.Future[dict[str, Any] | None]] = {}
        # Perf counters — useful for the v1.3 audit + ad-hoc tuning of
        # ``fetch_concurrency``. Reset per run.
        self._cache_hits_type = 0
        self._cache_hits_instance = 0
        self._cache_misses_type = 0
        self._cache_misses_instance = 0

    @property
    def specs(self) -> list[QuerySpec]:
        return list(self._specs)

    # ---- LangGraph entry ------------------------------------------------

    async def run(self, state: OrchestratorState) -> OrchestratorState:
        log.info(
            "revit_query.start",
            iteration=state["iteration"],
            categories=[s.category_label for s in self._specs],
            backend_categories=[s.backend_category for s in self._specs],
            follow_host_for=[s.category_label for s in self._specs if s.follow_host],
            fetch_concurrency=self._fetch_concurrency,
        )
        # Fresh caches + counters per invocation (LangGraph loops re-call run()).
        self._type_info_cache = {}
        self._instance_info_cache = {}
        self._type_inflight = {}
        self._instance_inflight = {}
        self._cache_hits_type = 0
        self._cache_hits_instance = 0
        self._cache_misses_type = 0
        self._cache_misses_instance = 0

        if not self._specs:
            log.warning("revit_query.no_specs", coverage=self._coverage)
            # See QueryAgent.run: "checking" keeps the agent contract, while the
            # coverage record lets `_exit_code_for` distinguish an empty plan
            # from a genuinely empty model.
            return {
                **state,
                "elements": [],
                "status": "checking",
                "query_coverage": self._coverage,
            }

        run_start = time.perf_counter()
        all_elements: list[dict[str, Any]] = []
        for spec in self._specs:
            try:
                cat_elements = await self._query_category(spec)
                all_elements.extend(cat_elements)
            except Exception as exc:
                log.exception(
                    "revit_query.category_failed",
                    backend_category=spec.backend_category,
                    category_label=spec.category_label,
                )
                return {**state, "status": "failed", "error": str(exc)}
        # v1.4-K3 (Layer 2b): attach `_containing_space` (geometry-heavy) only
        # when a rule needs it. Done once over the full element set so it's
        # available to compose_template autofill in QC.
        if self._needs_space and all_elements:
            # L-11: enrichment is best-effort, and this call sat OUTSIDE the
            # try that guards every other query step. A single space with an
            # unparseable volume (a string with a unit, a comma decimal) threw
            # straight out of the agent and took the whole run with it — a
            # crash, not the honest `status: "failed"` every other query
            # failure produces.
            #
            # Failing SOFT rather than failing the run: losing
            # `_containing_space` cannot produce a wrong value, only a missing
            # one — `qc._fill_template` returns None when any token is absent,
            # so the affected rules drop to Path A instead of composing a
            # malformed Mark. Killing an unattended nightly over one bad
            # volume cell would cost far more than the auto-fixes it saves.
            # The failure is recorded in coverage, not swallowed: an empty
            # result must say whether it looked.
            try:
                await self._enrich_containing_space(all_elements)
            except Exception as exc:
                log.warning(
                    "revit_query.space_enrichment_failed",
                    error=str(exc),
                    note="containing-space attach skipped; rules needing it "
                    "fall back to Path A",
                )
                self._coverage["space_enrichment"] = {
                    "status": "failed",
                    "detail": str(exc),
                }

        elapsed_ms = round((time.perf_counter() - run_start) * 1000, 1)

        # Cache effectiveness — high hit ratio = many instances share types
        # (typical for walls/doors). Low ratio = either model is diverse or
        # the elements list is mostly singletons.
        inst_total = self._cache_hits_instance + self._cache_misses_instance
        type_total = self._cache_hits_type + self._cache_misses_type
        inst_hit_ratio = (
            self._cache_hits_instance / inst_total if inst_total else 0.0
        )
        type_hit_ratio = (
            self._cache_hits_type / type_total if type_total else 0.0
        )

        per_category = {
            s.category_label: sum(
                1 for e in all_elements if e.get("category") == s.category_label
            )
            for s in self._specs
        }
        coverage = await self._coverage_with_empties(per_category)

        log.info(
            "revit_query.done",
            count=len(all_elements),
            per_category=per_category,
            type_cache_size=len(self._type_info_cache),
            instance_cache_size=len(self._instance_info_cache),
            elapsed_ms=elapsed_ms,
            elements_per_sec=(
                round(len(all_elements) / (elapsed_ms / 1000), 1)
                if elapsed_ms > 0
                else None
            ),
            type_cache_hits=self._cache_hits_type,
            type_cache_misses=self._cache_misses_type,
            type_hit_ratio=round(type_hit_ratio, 3),
            instance_cache_hits=self._cache_hits_instance,
            instance_hit_ratio=round(inst_hit_ratio, 3),
        )
        return {
            **state,
            "elements": all_elements,
            "status": "checking",
            "query_coverage": coverage,
        }

    async def _coverage_with_empties(
        self, per_category: dict[str, int]
    ) -> dict[str, Any]:
        """F-01: record categories that RESOLVED but returned zero elements.

        Coverage is built at PLAN time, where "resolved" only ever meant *the
        catalog knew this label* — never *the model actually had any*. A
        category that resolves and comes back empty was therefore
        indistinguishable from one that was fully audited: the run exited 0 and
        the report said "evaluability 100%" while that category's rules had
        checked nothing.

        In a federated project — the normal case — whole disciplines live in
        linked models, so the most likely reason for zero elements is that the
        rule is pointed at the wrong document. Live on Snowdon (an
        ARCHITECTURAL host), a "every duct must have a Mark" rule produced zero
        check records and a clean bill of health while the ducts sat in the
        linked HVAC file.

        Parameter rules read the host document only (``linked_mep`` covers
        GEOMETRY rules). That limit is defensible; being undeclared in the
        document a human signs is not. So when something came back empty AND
        the model has loaded links, their names ride along for the report to
        name — that turns "checked nothing" into "checked nothing, and here is
        where those elements probably are".
        """
        empty = sorted(label for label, n in per_category.items() if n == 0)
        if not empty:
            return self._coverage
        coverage = dict(self._coverage)
        coverage["categories_empty"] = empty
        links = await self._loaded_link_names()
        if links:
            coverage["linked_models"] = links
        log.warning(
            "revit_query.categories_empty",
            categories=empty, linked_models=links,
            detail="these categories resolved but returned 0 elements — their "
                   "rules checked nothing",
        )
        return coverage

    async def _loaded_link_names(self) -> list[str]:
        """Titles of loaded Revit links, best-effort (F-01 hint only).

        Never raises: an older addin without the command, or the Forma path,
        simply yields no hint. A missing hint must not cost the run.
        """
        try:
            links = await self._mcp.get_linked_files()
        except Exception as exc:  # advisory hint only — never fail the run
            log.info("revit_query.linked_files_unavailable", error=str(exc))
            return []
        names: list[str] = []
        for link in links or []:
            if not isinstance(link, dict) or not link.get("isLoaded", True):
                continue
            title = link.get("linkedDocTitle") or link.get("name")
            if title:
                names.append(str(title))
        return sorted(set(names))

    # ---- per-category pipeline -----------------------------------------

    async def _query_category(self, spec: QuerySpec) -> list[dict[str, Any]]:
        # Sheets are documentation, not model geometry — they don't come back
        # from list_elements/get_element_info. Route to the dedicated
        # list_sheets fetch that flattens Sheet Number / Sheet Name into params.
        if spec.backend_category == "OST_Sheets":
            return await self._query_sheets(spec)
        cat_start = time.perf_counter()
        cap = self._max_elements_per_category
        # v1.4-K3 (P1): bulk path eligible when opted in AND the spec needs
        # neither a host hop (per-element Host Id) nor Rooms metric enrichment
        # (which rides the list_elements top-level response, not find_elements).
        use_bulk = (
            self._bulk_fields
            and not spec.follow_host
            and spec.category_label != "Rooms"
        )
        if use_bulk:
            listing = await self._prime_bulk_listing(spec, cap)
        else:
            listing = await self._mcp.list_elements(
                spec.backend_category,
                limit=cap,
                only_instances=True,
            )
        if not listing:
            log.info(
                "revit_query.empty_category",
                backend_category=spec.backend_category,
            )
            return []
        # The mixin drops the addin's `truncated` flag, so infer truncation
        # from a full page. Surfaced so demo operators know the cap bit.
        if cap is not None and len(listing) >= cap:
            log.warning(
                "revit_query.element_cap_hit",
                backend_category=spec.backend_category,
                category_label=spec.category_label,
                cap=cap,
                detail=(
                    f"Category returned >= cap ({cap}) elements; only the first "
                    f"{cap} are checked. Raise --max-elements or narrow the rule."
                ),
            )

        sem = asyncio.Semaphore(self._fetch_concurrency)

        async def hydrate(item: dict[str, Any]) -> dict[str, Any] | None:
            return await self._hydrate_element(item, spec, sem)

        # M8: don't let one element's unexpected exception (e.g. a raw
        # transport error _get_instance/_get_type didn't already catch as
        # RevitEnvelopeError) blow up the whole gather and orphan the other
        # in-flight hydrate() tasks. Collect exceptions, log + drop the
        # offending element, and keep the rest — "partial data still
        # produces a usable element" (see _hydrate_element docstring). Only
        # when EVERY element failed (fan-out wiped out — addin likely down)
        # do we re-raise, preserving the fail-fast semantics run() relies on
        # (fe65fbc: query fail -> END) instead of silently returning an
        # empty category.
        raw_results = await asyncio.gather(
            *(hydrate(it) for it in listing), return_exceptions=True
        )
        elements: list[dict[str, Any]] = []
        errors: list[Exception] = []
        for item, res in zip(listing, raw_results):
            # A real cancellation (asyncio.CancelledError / KeyboardInterrupt)
            # must keep propagating as a control-flow signal immediately, not
            # get log-and-dropped as if it were an ordinary per-element
            # hydrate failure.
            if isinstance(res, BaseException) and not isinstance(res, Exception):
                raise res
            if isinstance(res, Exception):
                errors.append(res)
                log.warning(
                    "revit_query.hydrate_failed",
                    element_id=item.get("id"),
                    backend_category=spec.backend_category,
                    error=str(res),
                )
            elif res is not None:
                elements.append(res)
        if errors and len(errors) == len(raw_results):
            raise errors[0]
        log.info(
            "revit_query.category_done",
            category_label=spec.category_label,
            backend_category=spec.backend_category,
            listing_size=len(listing),
            hydrated=len(elements),
            failed=len(errors),
            follow_host=spec.follow_host,
            elapsed_ms=round((time.perf_counter() - cat_start) * 1000, 1),
        )
        return elements

    async def _query_sheets(self, spec: QuerySpec) -> list[dict[str, Any]]:
        """Fetch drawing sheets via ``list_sheets`` (one bulk call, no
        per-element hydration — sheets carry their number/name inline).

        Each sheet record is flattened to canonical ``Sheet Number`` /
        ``Sheet Name`` params (plus any verbatim addin field), then projected
        through ``spec.params`` so QC sees a value (or ``None`` → missing) for
        exactly the params the sheet rules read.
        """
        cat_start = time.perf_counter()
        cap = self._max_elements_per_category
        records = await self._mcp.list_sheets(limit=cap)
        if not records:
            log.info(
                "revit_query.empty_category",
                backend_category=spec.backend_category,
            )
            return []
        if cap is not None and len(records) >= cap:
            log.warning(
                "revit_query.element_cap_hit",
                backend_category=spec.backend_category,
                category_label=spec.category_label,
                cap=cap,
                detail=(
                    f"Category returned >= cap ({cap}) sheets; only the first "
                    f"{cap} are checked. Raise --max-elements or narrow the rule."
                ),
            )

        elements: list[dict[str, Any]] = []
        for rec in records:
            flat = _flatten_sheet(rec)
            params = {name: flat.get(name) for name in spec.params}
            eid = rec.get("id")
            elements.append(
                {
                    "id": str(eid) if eid is not None else "",
                    "name": (
                        flat.get("Sheet Name")
                        or flat.get("Sheet Number")
                        or (f"Sheet {eid}" if eid is not None else "Sheet")
                    ),
                    "category": spec.category_label,
                    "params": params,
                }
            )
        log.info(
            "revit_query.category_done",
            category_label=spec.category_label,
            backend_category=spec.backend_category,
            listing_size=len(records),
            hydrated=len(elements),
            follow_host=False,
            elapsed_ms=round((time.perf_counter() - cat_start) * 1000, 1),
        )
        return elements

    @staticmethod
    def _try_load_param_catalog() -> ParamCatalog | None:
        """Load the param catalog for the active Revit version, best-effort.

        Returns ``None`` (and warns) when the catalog file is absent/invalid —
        display-ification then degrades to a no-op rather than failing the run.
        """
        try:
            return load_param_catalog()
        except Exception as exc:  # noqa: BLE001 — missing/invalid catalog → no-op
            log.warning("revit_query.param_catalog_unavailable", error=str(exc))
            return None

    def _compute_display_names(self) -> dict[str, frozenset[str]]:
        """Per-spec set of params to surface as display text (option/reference).

        Keyed by ``backend_category`` (the OST string — unique per spec). Only
        params catalogued with a ``_DISPLAY_DIMENSIONS`` dimension qualify; a
        spec with none is omitted so the hydrate path skips the override entirely.
        """
        cat = self._param_catalog
        if cat is None:
            return {}
        out: dict[str, frozenset[str]] = {}
        for spec in self._specs:
            names: set[str] = set()
            for raw in spec.params:
                bare = (
                    raw[len(_TYPE_PREFIX):] if raw.startswith(_TYPE_PREFIX) else raw
                )
                if not bare:
                    continue
                ps = cat.resolve_param(spec.backend_category, bare)
                if ps is not None and ps.dimension in _DISPLAY_DIMENSIONS:
                    names.add(bare)
            if names:
                out[spec.backend_category] = frozenset(names)
        return out

    @staticmethod
    def _space_enrichment_needed(rules: RuleSet) -> bool:
        """True when any rule's compose_template references _containing_space."""
        for rule in rules.rules:
            af = getattr(rule, "autofill", None)
            if af is None or af.strategy != "compose_template":
                continue
            if "_containing_space" in (af.template or ""):
                return True
            if "_containing_space" in (af.sequence_scope or []):
                return True
        return False

    async def _enrich_containing_space(self, elements: list[dict[str, Any]]) -> None:
        """Attach ``_containing_space`` (MEP Space name) to each element whose
        centroid falls inside a space's bbox — smallest-volume space wins
        (axis-aligned bboxes overlap; the tightest fit is the right room).

        Geometry-heavy: one ``get_element_geometry`` per space + per element.
        Gated by ``_needs_space`` so normal runs pay nothing.
        """
        try:
            spaces = await self._mcp.list_spaces(
                limit=self._max_elements_per_category
            )
        except Exception as exc:
            log.warning("revit_query.space_list_failed", error=str(exc))
            return
        if not spaces:
            log.warning("revit_query.no_spaces_for_enrichment")
            return

        sem = asyncio.Semaphore(self._fetch_concurrency)

        async def _space_box(space: dict[str, Any]):
            async with sem:
                try:
                    g = await self._mcp.get_element_geometry(int(space["id"]))
                except Exception:
                    return None
            # L-11: the parse belongs INSIDE the guard. `float(...)` on a
            # volume the addin returned as text ("12,5 m³") raised out of the
            # task, and because the gather below had no `return_exceptions`
            # it propagated immediately while the sibling tasks kept running
            # unawaited ("Task exception was never retrieved").
            try:
                bbox = (g or {}).get("boundingBox")
                if not bbox:
                    return None
                return (
                    float(space.get("volume") or 0.0),
                    str(space.get("name") or ""),
                    bbox,
                )
            except (TypeError, ValueError, AttributeError) as exc:
                log.warning(
                    "revit_query.space_box_unreadable",
                    space_id=space.get("id"), error=str(exc),
                )
                return None

        boxes = [
            b
            for b in _drop_failures(
                await asyncio.gather(
                    *(_space_box(s) for s in spaces), return_exceptions=True
                ),
                "space_box",
            )
            if b
        ]
        boxes.sort(key=lambda b: b[0])  # smallest volume first → tightest fit

        async def _centroid(el: dict[str, Any]):
            async with sem:
                try:
                    g = await self._mcp.get_element_geometry(int(el["id"]))
                except Exception:
                    return None
            try:
                return (g or {}).get("centroid")
            except AttributeError:
                return None

        centroids = _drop_failures(
            await asyncio.gather(
                *(_centroid(el) for el in elements), return_exceptions=True
            ),
            "centroid",
        )
        mapped = 0
        for el, c in zip(elements, centroids):
            if not c:
                continue
            for _vol, name, bbox in boxes:
                if _point_in_bbox(c, bbox):
                    el.setdefault("params", {})["_containing_space"] = name
                    mapped += 1
                    break
        log.info(
            "revit_query.space_enriched",
            spaces=len(boxes),
            elements=len(elements),
            mapped=mapped,
            unmapped=len(elements) - mapped,
        )

    async def _prime_bulk_listing(
        self, spec: QuerySpec, cap: int | None
    ) -> list[dict[str, Any]]:
        """v1.4-K3 (P1): fetch instance params in one ``find_elements`` call.

        Projects ``spec.params`` (``type.`` prefix stripped — the addin reads
        real Revit names) and pre-seeds the instance cache with a synthetic
        ``get_element_info``-shaped record per element. Returning the listing
        unchanged means the existing ``_hydrate_element`` loop runs as-is:
        ``_get_instance`` hits the primed cache (zero per-element MCP calls)
        while the Type fetch + ``_merge_params`` still apply, so implicit
        Instance>Type fallback is preserved.
        """
        fields = sorted({
            name[len(_TYPE_PREFIX):] if name.startswith(_TYPE_PREFIX) else name
            for name in spec.params
            if name != _TYPE_PREFIX  # drop a bare "type." sentinel
        })
        listing = await self._mcp.find_elements(
            spec.backend_category,
            fields=fields,
            limit=cap,
        )
        for el in listing:
            eid_raw = el.get("id")
            if eid_raw is None:
                continue
            eid = int(eid_raw)
            flds = el.get("fields") or {}
            params_list = [
                {
                    "name": k,
                    "value": v,
                    "valueString": flds.get(f"{k}_display"),
                }
                for k, v in flds.items()
                if not k.endswith("_display")
            ]
            self._instance_info_cache[eid] = {
                "id": eid,
                "name": el.get("name"),
                "typeId": el.get("typeId"),
                "parameters": params_list,
            }
        return listing

    async def _hydrate_element(
        self,
        item: dict[str, Any],
        spec: QuerySpec,
        sem: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Build the QC-friendly element dict for one instance.

        Returns ``None`` only when the instance get_element_info call
        fails outright; partial data (e.g. missing type) still produces
        a usable element with whatever params we could gather.
        """
        eid = int(item["id"])
        instance_info = await self._get_instance(eid, sem)
        if instance_info is None:
            return None

        type_id = item.get("typeId") or instance_info.get("typeId")
        type_info: dict[str, Any] | None = None
        if isinstance(type_id, int) and type_id > 0:
            type_info = await self._get_type(type_id, sem)

        instance_params = _flatten(instance_info.get("parameters") or [])
        type_params = _flatten(type_info.get("parameters") or []) if type_info else {}

        params: dict[str, Any] = _merge_params(spec.params, instance_params, type_params)

        # v1.4-K25: surface option/reference params as their display string so
        # text rules (scope_filter regex, matches_regex) can match — the raw
        # flattened value is an opaque integer/ElementId. No-op unless this spec
        # has catalogued option/reference params.
        display_names = self._display_names.get(spec.backend_category)
        if display_names:
            inst_disp = _flatten_display(instance_info.get("parameters") or [])
            type_disp = (
                _flatten_display(type_info.get("parameters") or [])
                if type_info
                else {}
            )
            _apply_option_display(params, display_names, inst_disp, type_disp)

        # Host hop (driven by spec.follow_host derived from rules)
        if spec.follow_host:
            await self._apply_host_hop(
                params=params,
                instance_info=instance_info,
                instance_params=instance_params,
                spec=spec,
                sem=sem,
            )

        # Metadata breadcrumbs (underscored so they're easy to skip in QC).
        # F-02: which design option this element lives in, when it lives in one
        # at all. Carried as a breadcrumb rather than a rule-requested param so
        # EVERY run gets it — an author who never thought about design options
        # is exactly the author who needs the report to mention them.
        _option = _design_option_of(instance_info.get("parameters") or [])
        if _option:
            params["_design_option"] = _option
        if isinstance(type_id, int) and type_id > 0:
            params["_type_id"] = str(type_id)
        # v1.4-K22.1: stash the family + type name (from the already-fetched type
        # info) so reports can show "<family> - <type>" for a type-level param —
        # a door instance's own name is just its type, which isn't enough context.
        if type_info is not None:
            fam = type_params.get("Family Name")
            tnm = type_params.get("Type Name") or type_info.get("name")
            if fam not in (None, ""):
                params["_family_name"] = str(fam)
            if tnm not in (None, ""):
                params["_type_name"] = str(tnm)

        # Rooms-specific post-processing: metric mirrors + level/perimeter
        # carryover from the bulk list_elements response. Compatible with
        # both the legacy list_rooms shape and the v1.3 list_elements shape.
        if spec.category_label == "Rooms":
            _attach_room_metrics(params, item, instance_params)

        result: dict[str, Any] = {
            "id": str(eid),
            "name": (
                item.get("name") or instance_info.get("name") or f"Element {eid}"
            ),
            "category": spec.category_label,
            "params": params,
        }
        # v1.4-K3: display-string sidecar for compose_template rendering.
        if self._compose_template:
            inst_disp = _flatten_display(instance_info.get("parameters") or [])
            type_disp = (
                _flatten_display(type_info.get("parameters") or []) if type_info else {}
            )
            display: dict[str, Any] = {}
            for raw_name in spec.params:
                bare = (
                    raw_name[len(_TYPE_PREFIX):]
                    if raw_name.startswith(_TYPE_PREFIX)
                    else raw_name
                )
                v = inst_disp.get(bare)
                if v is None or (isinstance(v, str) and not v.strip()):
                    v = type_disp.get(bare)
                if v is not None and str(v).strip():
                    display[bare] = v
            result["params_display"] = display
        return result

    async def _apply_host_hop(
        self,
        *,
        params: dict[str, Any],
        instance_info: dict[str, Any],
        instance_params: dict[str, Any],
        spec: QuerySpec,
        sem: asyncio.Semaphore,
    ) -> None:
        """Resolve ``host.<name>`` params by fetching the host's type.

        Host detection: ``Host Id`` instance param wins; falls back to the
        ``hostId`` top-level field if Revit MCP populated it. A non-positive
        or unparseable host id is treated as "no host" — common for
        unhosted elements.
        """
        host_id_raw = instance_params.get("Host Id") or instance_info.get("hostId")
        try:
            host_id = int(host_id_raw) if host_id_raw is not None else None
        except (TypeError, ValueError):
            host_id = None
        if host_id is None or host_id <= 0:
            return

        host_info = await self._get_instance(host_id, sem)
        if host_info is None:
            return

        host_type_id_raw = host_info.get("typeId")
        try:
            host_type_id = (
                int(host_type_id_raw) if host_type_id_raw is not None else None
            )
        except (TypeError, ValueError):
            host_type_id = None
        if host_type_id is None or host_type_id <= 0:
            return

        host_type_info = await self._get_type(host_type_id, sem)
        if host_type_info is None:
            return
        host_type_params = _flatten(host_type_info.get("parameters") or [])
        # 2026-08-18: K25 display-ification applied to the HOST side too. The
        # element's own params go through `_apply_option_display`, but a host
        # param surfaced here used to carry the RAW flattened value — so
        # `host.Function` (wall Function, an option enum) arrived as the
        # integer 1, and a lookup-table row written "Exterior" could never
        # match (found live: the IBC 716.1(3) windows table resolved NO row
        # for 125 windows sitting in catalogued 2-HR Exterior walls). Same
        # catalog-driven rule as the element side: only params whose dimension
        # is option/reference are swapped, so text params (host.Fire Rating)
        # are untouched and no shipped rule changes behaviour.
        host_display = self._host_display_names(
            host_info.get("categoryEnum") or host_info.get("category")
        )
        host_type_disp = (
            _flatten_display(host_type_info.get("parameters") or [])
            if host_display
            else {}
        )
        for name in spec.host_params:
            if name in host_type_params:
                value = host_type_params[name]
                if name in host_display:
                    disp = host_type_disp.get(name)
                    if disp is not None and str(disp).strip():
                        value = disp
                params[f"host.{name}"] = value
        params["host.id"] = str(host_id)
        params["_host_type_id"] = str(host_type_id)

    def _host_display_names(self, host_category: Any) -> frozenset[str]:
        """Catalogued option/reference params of the HOST's category.

        Sibling of `_compute_display_names`, which is keyed by the SPEC's own
        category and therefore never covers the host hop. The caller passes
        the host's ``categoryEnum`` (OST string — the catalog's preferred key)
        with the display label as fallback; ``ParamCatalog.category`` resolves
        OST/key but NOT display labels, so the label is additionally tried
        lowercased (display "Walls" → key "walls"). Cached per input string;
        empty set when the catalog is unavailable or the category unknown."""
        if self._param_catalog is None or not isinstance(host_category, str):
            return frozenset()
        cached = self._host_display_cache.get(host_category)
        if cached is not None:
            return cached
        cat_entry = self._param_catalog.category(host_category)
        if cat_entry is None:
            cat_entry = self._param_catalog.category(host_category.strip().lower())
        names: set[str] = set()
        if cat_entry is not None:
            for ps in cat_entry.params:
                if ps.dimension in _DISPLAY_DIMENSIONS:
                    names.add(ps.name)
        result = frozenset(names)
        self._host_display_cache[host_category] = result
        return result

    # ---- cached MCP calls ----------------------------------------------

    async def _get_instance(
        self, element_id: int, sem: asyncio.Semaphore
    ) -> dict[str, Any] | None:
        cached = self._instance_info_cache.get(element_id)
        if cached is not None:
            self._cache_hits_instance += 1
            return cached
        # L-10: same in-flight dedup K8 gave `_get_type`, for the same reason
        # one layer down. The cache is only consulted BEFORE the fetch, so a
        # concurrent fan-out that all wants one element — every door in a wall
        # asking for that wall during the host hop — issues one
        # get_element_info per in-flight task, not one per distinct element.
        # Bounded by `fetch_concurrency` rather than by N, so it is waste
        # rather than a stampede, but it is waste on Revit's single-threaded
        # main loop, where fewer-fatter calls is the whole optimisation story.
        inflight = self._instance_inflight.get(element_id)
        if inflight is not None:
            self._cache_hits_instance += 1
            # shield: a caller cancelled elsewhere must not poison the shared
            # Future for every sibling awaiting the same element (K8's Low9).
            try:
                return await asyncio.shield(inflight)
            except asyncio.CancelledError:
                raise
        self._cache_misses_instance += 1
        fut: asyncio.Future[dict[str, Any] | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._instance_inflight[element_id] = fut
        info: dict[str, Any] | None = None
        try:
            async with sem:
                info = await self._mcp.get_element_info(element_id)
            self._instance_info_cache[element_id] = info
        except RevitEnvelopeError as exc:
            log.warning(
                "revit_query.instance_info_failed",
                element_id=element_id,
                code=exc.code,
            )
            info = None
        finally:
            # Always resolve, or every awaiter on this element hangs forever.
            if not fut.done():
                fut.set_result(info)
            self._instance_inflight.pop(element_id, None)
        return info

    async def _get_type(
        self, type_id: int, sem: asyncio.Semaphore
    ) -> dict[str, Any] | None:
        cached = self._type_info_cache.get(type_id)
        if cached is not None:
            self._cache_hits_type += 1
            return cached
        # v1.4-K8: a concurrent fetch for the same type_id is already running —
        # await it instead of issuing a duplicate get_element_info. Counts as a
        # hit (served without a new Revit round-trip).
        inflight = self._type_inflight.get(type_id)
        if inflight is not None:
            self._cache_hits_type += 1
            # Low9: shield the shared Future from THIS awaiter's own
            # cancellation. Without shield(), a caller (e.g. a hydrate() task
            # cancelled by asyncio.gather teardown elsewhere) cancelling its
            # await here would propagate the cancellation into the shared
            # Future itself, poisoning it for every sibling awaiter still
            # waiting on the same type_id — not just this one. If THIS task
            # is the one being cancelled, re-raise after unshielding so the
            # caller still observes its own cancellation.
            try:
                return await asyncio.shield(inflight)
            except asyncio.CancelledError:
                raise
        self._cache_misses_type += 1
        # Low9: get_running_loop() (not the deprecated get_event_loop()) —
        # this always runs inside a running loop (an `async def`), so there's
        # no "no loop yet" case to fall back on.
        fut: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._type_inflight[type_id] = fut
        info: dict[str, Any] | None = None
        try:
            async with sem:
                info = await self._mcp.get_element_info(type_id)
            self._type_info_cache[type_id] = info
        except RevitEnvelopeError as exc:
            log.warning(
                "revit_query.type_info_failed", type_id=type_id, code=exc.code
            )
            # Low9 (kept intentionally): one failed fetch poisons the WHOLE
            # cohort awaiting this Future with `info = None` — every sibling
            # sharing this type_id gets None this run, not just this caller.
            # Accepted as-is: the failure is a single Revit round-trip for
            # the type, not per-caller, so there is nothing more specific to
            # give any individual awaiter; a transient failure self-heals on
            # the caller's next retry/run (fresh caches, fresh in-flight map).
            info = None
        finally:
            # Always resolve so awaiting callers never hang, then drop the slot.
            if not fut.done():
                fut.set_result(info)
            self._type_inflight.pop(type_id, None)
        return info


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


def _point_in_bbox(pt: dict[str, float], bbox: dict[str, Any]) -> bool:
    """True when point ``pt`` (x/y/z) lies inside the axis-aligned ``bbox``."""
    lo, hi = bbox.get("min", {}), bbox.get("max", {})
    try:
        return (
            lo["x"] <= pt["x"] <= hi["x"]
            and lo["y"] <= pt["y"] <= hi["y"]
            and lo["z"] <= pt["z"] <= hi["z"]
        )
    except (KeyError, TypeError):
        return False


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """First key in ``keys`` whose value in ``record`` is present (not None,
    not blank string). Returns None when none qualify."""
    for k in keys:
        v = record.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _flatten_sheet(record: dict[str, Any]) -> dict[str, Any]:
    """Normalise one ``list_sheets`` record to canonical params.

    Field spellings vary across Revit MCP addins — a ViewSheet's number is
    surfaced as ``sheetNumber`` / ``number`` and its name as ``sheetName`` /
    ``name`` / ``title``. We carry the raw record verbatim (so a rule can read
    a niche addin field by its native name) and add the canonical ``Sheet
    Number`` / ``Sheet Name`` keys the Rule Builder / catalog use as the
    compliance-facing parameter names.
    """
    flat = dict(record)
    flat["Sheet Number"] = _first_present(
        record, ("Sheet Number", "sheetNumber", "number", "sheet_number")
    )
    flat["Sheet Name"] = _first_present(
        record, ("Sheet Name", "sheetName", "name", "title", "sheet_name")
    )
    return flat


def _flatten_display(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten to ``{name: valueString}`` — the human-readable display string
    (e.g. "L2" for Reference Level, whose raw ``value`` is the levelId)."""
    out: dict[str, Any] = {}
    for p in parameters:
        name = p.get("name")
        if name:
            out[name] = p.get("valueString")
    return out


def _flatten(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten Revit's ``[{name, value, ...}]`` list into ``{name: value}``."""
    out: dict[str, Any] = {}
    for p in parameters:
        name = p.get("name")
        if not name:
            continue
        out[name] = p.get("value")
    return out


# F-02: Revit reports "Main Model" for an element belonging to no design
# option. Only a real option name is worth carrying.
_MAIN_MODEL = "Main Model"


def _design_option_of(parameters: list[dict[str, Any]]) -> str | None:
    """The element's design-option NAME, or None when it is in the main model.

    F-02 (2026-07-26 live probe): a design option holds an ALTERNATIVE — one
    of several layouts being explored, at most one of which ever gets built.
    Those elements come back from ``list_elements`` looking exactly like real
    ones, so findings on an alternative were indistinguishable from findings
    on the actual design. On Snowdon that was 6 of 149 doors producing 24
    check records, 10 of them failures, with nothing anywhere saying so.

    Revit exposes ``Design Option`` TWICE on the same element: as an
    ElementId (an opaque integer) and as a String (the readable
    "<set> : <option>"). The String entry is picked explicitly rather than
    trusting which one ``_flatten`` happens to overwrite last — this value
    ends up in a document a human reads, where "3082902" would be useless.
    """
    for p in parameters:
        if p.get("name") != "Design Option" or p.get("storageType") != "String":
            continue
        value = p.get("valueString") or p.get("value")
        text = str(value).strip() if value is not None else ""
        return text if text and text != _MAIN_MODEL else None
    return None


def _has_value(value: Any) -> bool:
    """Decide whether a Revit-side value counts as "present" for fallback.

    Mirrors the semantic used by ``qc._is_missing`` so the Instance > Type
    fallback agrees with the missing-data classifier downstream. A
    whitespace-only string is treated the same as ``None`` — Revit
    occasionally surfaces ``""`` for unset instance overrides while the
    real Type value sits behind it; without this check the QC engine
    would incorrectly flag the element as missing/non-compliant even
    though the Type holds a valid value.

    Numeric zero, ``False``, and empty collections ARE real values —
    only ``None`` and blank strings count as absent.
    """
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _merge_params(
    wanted: frozenset[str],
    instance_params: dict[str, Any],
    type_params: dict[str, Any],
) -> dict[str, Any]:
    """Build ``element.params`` per the v1.3 precedence rules.

    For each name in ``wanted``:
      * ``"type.<X>"`` → surface only the Type value at ``type.<X>``.
      * ``"<X>"``      → Instance > Type at ``<X>``; mirror Type at
                         ``type.<X>`` when present so rules that need
                         the Type value can still reach it.

    Names with no value at either layer are stored as ``None`` so the
    QC ``_is_missing`` check fires (rather than silently treating the
    rule as compliant).
    """
    params: dict[str, Any] = {}
    for raw_name in wanted:
        if raw_name.startswith(_TYPE_PREFIX):
            bare = raw_name[len(_TYPE_PREFIX):]
            if not bare:
                # Edge: rule wrote bare "type." — treat as nothing to fetch.
                continue
            params[raw_name] = type_params.get(bare)
            continue
        bare = raw_name
        instance_val = instance_params.get(bare)
        type_val = type_params.get(bare)
        # Instance wins; fall back to Type when Instance has no usable
        # value (None, missing key, OR blank string — see _has_value).
        # The blank-string case matters in practice: Revit can surface
        # ``""`` on a Type-driven instance parameter when the user clears
        # the instance override but the Type still holds the real value
        # (Fire Rating, Type Mark, …). Pre-v1.4-F1 this would shadow the
        # Type value and produce false missing/non-compliant findings.
        if _has_value(instance_val):
            params[bare] = instance_val
        else:
            params[bare] = type_val  # may itself be None — that's fine
        # Always expose the Type-side value (when present) under the
        # type.<bare> alias so a rule can force-read the Type.
        if bare in type_params:
            params[f"type.{bare}"] = type_val
    return params


def _apply_option_display(
    params: dict[str, Any],
    display_names: frozenset[str],
    instance_disp: dict[str, Any],
    type_disp: dict[str, Any],
) -> None:
    """Override option/reference params in ``params`` with their display string.

    For each bare name in ``display_names`` that ``_merge_params`` already
    surfaced, replace the raw flattened value (an opaque enum integer or
    ElementId) with the addin's ``valueString`` (e.g. ``1`` → ``"Exterior"``,
    levelId → ``"L2"``) so text rules can match. Mirrors the Instance>Type
    precedence of ``_merge_params`` and only overrides when a non-blank display
    string is actually present — a missing valueString leaves the raw value
    untouched (better an integer than nothing).
    """
    for bare in display_names:
        if bare in params:
            disp = instance_disp.get(bare)
            if not _has_value(disp):
                disp = type_disp.get(bare)
            if _has_value(disp):
                params[bare] = disp
        type_key = f"{_TYPE_PREFIX}{bare}"
        if type_key in params:
            tdisp = type_disp.get(bare)
            if _has_value(tdisp):
                params[type_key] = tdisp


def _attach_room_metrics(
    params: dict[str, Any],
    list_item: dict[str, Any],
    instance_params: dict[str, Any],
) -> None:
    """Add Room-specific convenience mirrors to ``params`` in place.

    The legacy ``list_rooms`` path supplied ``areaMetric``, ``levelName``
    and ``perimeter`` directly in the bulk response. ``list_elements``
    may or may not — when missing, ``areaMetric`` is computed from the
    instance ``Area`` parameter (ft²) and ``Unbounded Height (m)`` from
    the ``Unbounded Height`` parameter (ft).

    Treats an existing ``None`` value the same as "not set" so a rule
    requesting these derived params (which ``_merge_params`` pre-seeds
    to ``None`` because they're not real Revit parameter names) still
    gets the computed value enriched in.
    """
    # areaMetric: prefer bulk-response value, fall back to converting Area.
    if params.get("areaMetric") is None:
        if "areaMetric" in list_item:
            params["areaMetric"] = list_item["areaMetric"]
        else:
            area_ft2 = instance_params.get("Area")
            try:
                if area_ft2 is not None:
                    params["areaMetric"] = sqft_to_sqm(float(area_ft2))
            except (TypeError, ValueError):
                # Swallows exactly: Area came back non-numeric (text/odd
                # shape), so the metric mirror is not derivable. areaMetric
                # stays ABSENT — downstream reads that as missing data; a
                # guessed 0.0 here would be the default-dressed-as-measurement
                # bug class (R12). Nothing else can raise: float()+sqft_to_sqm
                # are the only calls in the try.
                pass

    # Unbounded Height (m): derive from the imperial param when present.
    height_ft = instance_params.get("Unbounded Height")
    if height_ft is not None and params.get("Unbounded Height (m)") is None:
        try:
            params["Unbounded Height (m)"] = feet_to_meters(float(height_ft))
        except (TypeError, ValueError):
            params["Unbounded Height (m)"] = None

    # Other breadcrumbs from the bulk listing — don't overwrite a real
    # value already in params, but do fill in if absent or None.
    for k in ("levelName", "perimeter"):
        if k in list_item and params.get(k) is None:
            params[k] = list_item[k]
