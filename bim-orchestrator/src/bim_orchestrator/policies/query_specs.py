"""Derive backend-resolved query specs from a RuleSet (v1.3 Task #4).

The v1.3 redesign hands ``QueryAgent`` a ``RuleSet`` instead of a flat
category string list. The agent calls ``derive_specs()`` here to translate
the ruleset into one ``QuerySpec`` per category it needs to query:

  * Which backend label to use (``OST_Walls`` for Revit, ``"Walls"`` for
    AECDM) — resolved via :class:`OSTCatalog`. Unknown labels or
    AECDM-null entries are dropped (with a warn) so the run continues.
  * Which parameters every rule in that category might read — the
    union of ``rule.parameter``, ``rule.when_param``, and
    ``rule.other_param`` (the last with ``host.`` prefix stripped). The
    agent surfaces these in ``element.params`` so QC sees a value
    rather than ``None``.
  * Whether to follow Host relationships — true iff any rule in that
    category references a ``host.*`` cross-element param.

This module is **pure** — it takes data, returns data, never touches an
MCP. Both the Forma and Revit query agents call it identically.

Layering note: this module lives in ``policies/`` and imports from
``policies/rules_schema.py`` and ``policies/ost_catalog.py``. It must
not import from ``agents/`` (that would invert the layering).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # `Path` is annotation-only here (see `config_dir` below)
    from pathlib import Path

from bim_orchestrator.policies.ost_catalog import (
    Backend,
    Discipline,
    OSTCatalog,
)
from bim_orchestrator.policies.rules_schema import Rule, RuleSet, fetch_name

log = structlog.get_logger(__name__)

_HOST_PREFIX = "host."
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]+)\}")


def _template_param_tokens(rule: Rule) -> set[str]:
    """Real Revit params a compose_template rule references (so the query
    fetches them). Excludes ``{seq}`` (computed) and derived params prefixed
    with ``_`` (e.g. ``_containing_space``, attached by enrichment)."""
    af = getattr(rule, "autofill", None)
    if af is None or af.strategy != "compose_template":
        return set()
    names: set[str] = set(_TEMPLATE_TOKEN.findall(af.template or ""))
    names.update(af.sequence_scope or [])
    return {n for n in names if n != "seq" and not n.startswith("_")}


@dataclass(frozen=True)
class QuerySpec:
    """One backend-resolved category fetch.

    Attributes:
        category_label: The canonical display label from the catalog
            (e.g. ``"Walls"``). Stamped into ``element["category"]`` so
            QC's per-rule ``rule.category`` filter still works downstream.
        backend_category: The string passed to the MCP — ``"OST_Walls"``
            for Revit, ``"Walls"`` for AECDM. Already resolved.
        params: Names of all parameters any rule in this category reads.
            The query agent must surface each of these in
            ``element.params`` (instance values shadowing type values per
            v1.3 precedence).
        follow_host: True when at least one rule references a ``host.*``
            cross-element parameter; tells the Revit agent to do the
            host-instance + host-type hop. Ignored by the Forma agent
            (AECDM already returns flattened properties).
        host_params: Names of host parameters to hydrate (the part after
            the ``host.`` prefix). Empty when ``follow_host`` is False.
        discipline: Carried from the catalog entry for logging/reports.
    """

    category_label: str
    backend_category: str
    params: frozenset[str]
    follow_host: bool = False
    host_params: frozenset[str] = field(default_factory=frozenset)
    discipline: Discipline = "architecture"


def derive_specs_with_coverage(
    rules: RuleSet,
    *,
    backend: Backend,
    catalog: OSTCatalog,
    config_dir: Path | None = None,
) -> tuple[list[QuerySpec], dict[str, Any]]:
    """Build one :class:`QuerySpec` per effective category in ``rules``.

    Resolution rules:
      * Categories to query = ``rules.target_category`` (str or list).
      * A rule with ``rule.category=None`` applies to every target
        category (its params join every spec).
      * A rule with ``rule.category=X`` where X is in the target set
        applies only to that spec.
      * A rule with ``rule.category=X`` where X is **outside** the
        target set is unreachable (QC's in-scope filter would drop
        every element for it) — we log a warning and skip it.

    Backend resolution failures (unknown label, or AECDM-null entry)
    cause the affected category to be dropped from the returned list —
    the warning is already emitted by :meth:`OSTCatalog.resolve`. The
    run continues with whatever categories did resolve.

    Returns an empty list (and warns) when ``target_category`` is
    missing or empty.

    The second element is the **query-plan coverage** record: what was
    requested, what actually resolved, and what was dropped (with a
    reason). Dropping every category used to be indistinguishable from
    "the model genuinely has no matching elements" — both produced zero
    findings and a converged run. Callers stamp this on the state so the
    orchestrator can tell "audited, all clean" apart from "never audited".
    """
    targets = _normalize_targets(rules.target_category)
    coverage: dict[str, Any] = {
        "targets_requested": list(targets),
        "categories_resolved": [],
        "categories_dropped": [],
        "rule_count": len(rules.rules),
    }
    if not targets:
        log.warning(
            "query_specs.empty_target_category",
            scenario=rules.scenario,
            rule_count=len(rules.rules),
        )
        return [], coverage

    # Group rules by their effective category. A rule with category=None
    # joins every target's bucket.
    rules_per_cat: dict[str, list[Rule]] = {cat: [] for cat in targets}
    for r in rules.rules:
        if r.category is None:
            for cat in targets:
                rules_per_cat[cat].append(r)
        elif r.category in rules_per_cat:
            rules_per_cat[r.category].append(r)
        else:
            log.warning(
                "query_specs.rule_category_out_of_scope",
                rule_id=r.id,
                rule_category=r.category,
                target_category=targets,
                note="rule will never fire; QC's in-scope filter drops elements with this category",
            )

    specs: list[QuerySpec] = []
    for cat in targets:
        rules_for_cat = rules_per_cat[cat]
        if not rules_for_cat:
            log.warning(
                "query_specs.no_rules_for_category",
                category=cat,
                scenario=rules.scenario,
            )
            coverage["categories_dropped"].append(
                {"category": cat, "reason": "no_rules_for_category"}
            )
            continue

        backend_label = catalog.resolve(cat, backend)
        if backend_label is None:
            # catalog.resolve already logged a structured warn — either
            # ost_catalog.unknown_label or ost_catalog.aecdm_not_supported.
            # We just skip this category cleanly.
            coverage["categories_dropped"].append(
                {"category": cat, "reason": f"unresolved_on_{backend}"}
            )
            continue

        entry = catalog.find(cat)
        # Defensive: if resolve() succeeded, find() must also.
        if entry is None:  # pragma: no cover — invariant
            coverage["categories_dropped"].append(
                {"category": cat, "reason": "catalog_entry_missing"}
            )
            continue

        params, follow_host, host_params = _collect_params(rules_for_cat, config_dir)

        specs.append(
            QuerySpec(
                category_label=entry.display,
                backend_category=backend_label,
                params=frozenset(params),
                follow_host=follow_host,
                host_params=frozenset(host_params),
                discipline=entry.discipline,
            )
        )

    coverage["categories_resolved"] = [s.category_label for s in specs]
    log.info(
        "query_specs.derived",
        backend=backend,
        scenario=rules.scenario,
        spec_count=len(specs),
        categories=[s.category_label for s in specs],
        follow_host_for=[s.category_label for s in specs if s.follow_host],
        dropped=coverage["categories_dropped"],
    )
    return specs, coverage


def derive_specs(
    rules: RuleSet,
    *,
    backend: Backend,
    catalog: OSTCatalog,
    config_dir: Path | None = None,
) -> list[QuerySpec]:
    """Specs-only view of :func:`derive_specs_with_coverage`.

    Kept so every existing caller/test that only needs the query plan is
    unaffected by the coverage record.
    """
    specs, _ = derive_specs_with_coverage(
        rules, backend=backend, catalog=catalog, config_dir=config_dir
    )
    return specs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _normalize_targets(target_category: str | list[str]) -> list[str]:
    """Return ``target_category`` as a deduplicated list, preserving order.

    Order preservation matters for log readability (rules YAML author
    writes ``[Walls, Doors]`` — we don't want the warn output to flip
    them).
    """
    if isinstance(target_category, str):
        return [target_category] if target_category else []
    seen: set[str] = set()
    out: list[str] = []
    for cat in target_category:
        if not isinstance(cat, str) or not cat or cat in seen:
            continue
        seen.add(cat)
        out.append(cat)
    return out


def _collect_params(
    rules: list[Rule],
    config_dir: Path | None = None,
) -> tuple[set[str], bool, set[str]]:
    """Compute the param union + host-hop flags for one category's rules.

    Returns ``(params, follow_host, host_params)``. ``host_params`` holds
    the host parameter names with the ``host.`` prefix stripped — the
    Revit agent uses these to know what to fetch from the host
    instance's type after the host hop.
    """
    params: set[str] = set()
    host_params: set[str] = set()
    follow_host = False
    for r in rules:
        # Use bound_parameter for the Revit fetch when set; the canonical
        # r.parameter is the intent label (kept in findings/reports only).
        params.add(r.bound_parameter if r.bound_parameter else r.parameter)
        if r.when_param:
            params.add(r.when_param)
        if r.other_param:
            if r.other_param.startswith(_HOST_PREFIX):
                follow_host = True
                bare = r.other_param[len(_HOST_PREFIX):]
                if bare:
                    host_params.add(bare)
            else:
                # Cross-param within the same element — same fetch path
                # as a normal param.
                params.add(r.other_param)
        # v1.4-K3: a compose_template autofill references extra params (e.g.
        # "Reference Level", "System Name") the rule doesn't otherwise read —
        # fetch them so QC can build the suggested value.
        params.update(_template_param_tokens(r))
        # v1.4-K10: a universal scope_filter reads another param — fetch it so
        # QC can decide whether the rule applies to each element (else the param
        # is absent → every element looks out-of-scope and the rule never runs).
        if r.scope_filter is not None:
            params.add(r.scope_filter.param)
        # v1.4-K20/K22: an inherit autofill reads a HOST parameter — turn on the
        # host hop and hydrate that param (defaults to the rule's own param). Both
        # the plain inherit and the compound inherit-then-normalize need it.
        af = getattr(r, "autofill", None)
        if af is not None and getattr(af, "strategy", None) in (
            "inherit_from_host", "inherit_then_normalize"
        ):
            follow_host = True
            # Binding layer: the host's REAL Revit param name is the bound name —
            # must match what QC's _suggest reads (host.<name>), or the hydrated
            # key and the lookup key silently diverge for bound rules.
            host_params.add(getattr(af, "host_param", None) or fetch_name(r))
        # A lookup table self-declares the params it keys on — hydrate them,
        # or the key arrives as None and no row can ever match. Cached load;
        # try-wrapped so a missing/invalid table adds nothing.
        if getattr(r, "lookup", None):
            try:
                from bim_orchestrator.policies.lookup_table import load_lookup
                # Medium: resolve the lookup relative to the RULES-FILE dir (with
                # config/ fallback) so a pack-local table's host params are hydrated
                # — otherwise query_specs looked only in config/ while QC resolved
                # it pack-local, so the host hop was never turned on and the §716
                # rule silently had no host.Fire Rating to compare.
                table = load_lookup(r.lookup, config_dir)
                for bare in table.host_params:
                    follow_host = True
                    host_params.add(bare)
                # 2026-08-18: keys on the element's OWN params need the same
                # treatment. Every shipped table keyed on the host (IBC §716),
                # so this branch never existed until the IRC room-minimum
                # table keyed on `Name` — and all 30 rooms came back
                # manual_review with an empty operand because `Name` was
                # never fetched.
                params.update(table.own_params)
            except Exception:  # noqa: BLE001 — table absent/invalid → no add
                pass
    return params, follow_host, host_params
