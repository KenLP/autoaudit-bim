"""Forma/AECDM Query Agent (v1.3) — rules-driven fetch via Forma MCP.

v1.3 redesign: the agent takes a ``RuleSet`` + ``OSTCatalog`` and derives
its query plan via :func:`policies.query_specs.derive_specs` rather than
receiving a hardcoded category string. The alternate ``categories=``
keyword keeps a rules-free fetch shape available for ad-hoc consumers.

AECDM specifics
---------------
* AECDM's ``aecdm_query_elements`` is read-only and accepts one category
  per call. Multi-category targets become N sequential calls; the
  element's stamped ``category`` records which call produced it so the
  downstream QC per-rule ``category`` filter still works.
* The response already flattens properties for us — there's no Type vs
  Instance separation to merge (unlike Revit MCP). ``_attach_params``
  simply lifts the ``properties: [{name, value}, ...]`` array into a
  ``params: {name: value}`` dict for O(1) rule lookup.
* Catalog ``aecdm_label`` may be ``None`` for categories AECDM doesn't
  expose (e.g. structural rebar) — :func:`derive_specs` drops those
  cleanly with a warn. The run continues with whatever resolved.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from bim_orchestrator.mcp_clients.forma import FormaMCPClient
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.query_specs import QuerySpec, derive_specs_with_coverage
from bim_orchestrator.policies.rules_schema import RuleSet
from bim_orchestrator.state import OrchestratorState

log = structlog.get_logger(__name__)


class QueryAgent:
    """LangGraph-compatible query node backed by Forma MCP (AECDM).

    Two construction modes:

      * **Rules-driven** (production):
        ``QueryAgent(mcp, element_group_id, rules=ruleset, catalog=cat)``
        — derives one fetch per category in the ruleset, with category
        labels resolved through the catalog.

      * **Categories-only** (ad-hoc fetch):
        ``QueryAgent(mcp, element_group_id, categories=["Rooms"])`` —
        a rules-free fetch that simply queries the given categories.
        Retained as a back-compat surface; the v1.4-F5 ``LLMQueryAgent``
        consumer was removed when Rule Builder superseded ``--ask``.

    Exactly one of ``rules`` or ``categories`` must be supplied.
    ``catalog`` is loaded lazily from the default path when omitted
    (both modes resolve labels through it).
    """

    def __init__(
        self,
        mcp: FormaMCPClient,
        element_group_id: str,
        *,
        rules: RuleSet | None = None,
        catalog: OSTCatalog | None = None,
        categories: str | Sequence[str] | None = None,
        config_dir: "Path | None" = None,
    ) -> None:
        if (rules is None) == (categories is None):
            raise ValueError(
                "QueryAgent requires exactly one of `rules=` or `categories=`"
            )
        self._mcp = mcp
        self._element_group_id = element_group_id
        self._catalog = catalog if catalog is not None else OSTCatalog.load()
        if rules is not None:
            self._specs, self._coverage = derive_specs_with_coverage(
                rules, backend="aecdm", catalog=self._catalog, config_dir=config_dir
            )
        else:
            assert categories is not None  # narrowing for type-checker
            self._specs = _specs_from_categories(categories, self._catalog)
            # categories= is the legacy/manual path: the caller named the
            # categories directly, so "requested" == "resolved" by construction.
            self._coverage = {
                "targets_requested": list(categories),
                "categories_resolved": [s.category_label for s in self._specs],
                "categories_dropped": [],
                "rule_count": 0,
            }

    @property
    def specs(self) -> list[QuerySpec]:
        """Read-only view of the derived QuerySpecs (useful for assertions)."""
        return list(self._specs)

    async def run(self, state: OrchestratorState) -> OrchestratorState:
        log.info(
            "query_agent.start",
            element_group_id=self._element_group_id,
            iteration=state["iteration"],
            categories=[s.category_label for s in self._specs],
            backend_categories=[s.backend_category for s in self._specs],
        )
        if not self._specs:
            # Either derive_specs returned nothing (every target dropped on
            # backend resolution) or categories= was an empty list. Either
            # way: nothing to fetch, but not a failure — yield empty list
            # so QC sees a valid (empty) state.
            log.warning("query_agent.no_specs", coverage=self._coverage)
            # Still "checking", not "failed": an empty categories= list is a
            # legitimate no-op. The coverage record is what lets the
            # orchestrator tell that apart from "every category was dropped,
            # so nothing was ever audited" — see `_exit_code_for`.
            return {
                **state,
                "elements": [],
                "status": "checking",
                "query_coverage": self._coverage,
            }

        elements: list[dict[str, Any]] = []
        for spec in self._specs:
            try:
                raw = await self._mcp.query_elements(
                    element_group_id=self._element_group_id,
                    category=spec.backend_category,
                )
            except Exception as exc:
                # Surface which category failed so a rule YAML typo is
                # immediately actionable.
                log.exception(
                    "query_agent.failed",
                    backend_category=spec.backend_category,
                    category_label=spec.category_label,
                )
                return {
                    **state,
                    "status": "failed",
                    "error": (
                        f"query_elements({spec.backend_category}) failed: {exc}"
                    ),
                }
            elements.extend(_attach_params(el, spec.category_label) for el in raw)

        log.info(
            "query_agent.done",
            count=len(elements),
            per_category={
                s.category_label: sum(
                    1 for e in elements if e.get("category") == s.category_label
                )
                for s in self._specs
            },
        )
        return {
            **state,
            "elements": elements,
            "status": "checking",
            "query_coverage": self._coverage,
        }


def _attach_params(element: dict[str, Any], category: str) -> dict[str, Any]:
    """Flatten AECDM `properties: [{name, value}, ...]` into a dict on `params`.

    Keeps the original `id`, `name`, and raw `properties` for traceability and
    adds:
      - `category`: the catalog display label (matches what rules write)
      - `params`: {parameter_name: value} for O(1) rule lookup
    """
    raw_props = element.get("properties", []) or []
    params: dict[str, Any] = {}
    for prop in raw_props:
        name = prop.get("name")
        if not name:
            continue
        params[name] = prop.get("value")
    return {**element, "category": category, "params": params}


def _specs_from_categories(
    categories: str | Sequence[str], catalog: OSTCatalog
) -> list[QuerySpec]:
    """Build rules-free QuerySpecs from a literal category list (LLM ad-hoc).

    Each entry is resolved through the catalog so the LLM saying
    ``"rooms"`` still fetches ``"Rooms"`` via the case-insensitive index.
    Unknown labels are skipped with a warn (the catalog already logged
    them) — the run continues with whatever resolved.

    ``params`` is left empty: no rule means no allowlist; the AECDM
    response will pour all properties into ``element.params`` anyway.
    """
    if isinstance(categories, str):
        cat_list: list[str] = [categories] if categories else []
    else:
        cat_list = [c for c in categories if isinstance(c, str) and c]
    specs: list[QuerySpec] = []
    for label in cat_list:
        backend_label = catalog.resolve(label, "aecdm")
        if backend_label is None:
            continue
        entry = catalog.find(label)
        if entry is None:  # pragma: no cover — invariant
            continue
        specs.append(
            QuerySpec(
                category_label=entry.display,
                backend_category=backend_label,
                params=frozenset(),
                follow_host=False,
                host_params=frozenset(),
                discipline=entry.discipline,
            )
        )
    return specs
