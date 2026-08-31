"""GeometricQueryAgent — runs geometry checks against the Revit MCP server.

v1.4-K2 optimizations:
- Tầng 1 — Parallelism: asyncio.gather runs all batch groups concurrently;
  wall-clock = slowest single call instead of sum of all calls.
- Tầng 2 — Batching: clearance_min rules that share the same (setA, setB,
  axis, direction, view_id) key are collapsed into one revit_check_clearance
  call (threshold = max over group). Python-side filtering then re-applies
  each rule's own threshold to the returned clashes — but ONLY for axis="Z",
  the one mode that measures a distance per hit. axis="bbox" (horizontal)
  returns bare pairs, so its threshold joins the batch key (bbox-refilter
  bug, H-01 follow-up) and each bbox batch is one threshold, filtered exactly
  by the addin's own AABB inflation.
- Dedup: multiple findings for the same element_id are merged into one
  Finding (worst severity wins, rule_ids joined, messages concatenated)
  so DesignAgent creates at most one ACC Issue per physical element.
- view_id auto-resolution: active ThreeDimensional view → named 3D view
  (keyword priority: 3D, Coordination, Clash, Check, MEP) → first 3D view
  → None. view_id is no longer required in YAML.

Spatial-containment and min_spacing rules are captured in the YAML schema
but not yet dispatched by this agent (they require a dedicated MCP tool
not yet released). They produce zero findings and log a warning.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import structlog

from bim_orchestrator.agents.revit_query import _point_in_bbox
from bim_orchestrator.mcp_clients.revit import AnyRevitClient
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import GeometryRule
from bim_orchestrator.state import Finding, Severity

log = structlog.get_logger(__name__)

# OVERRIDES ONLY — the shared OST catalog (`config/ost_catalog.yaml`) is the
# category layer, and `_to_ost` consults it for everything not listed here.
# This map used to be the WHOLE resolution, and that made the geometry feature
# quietly unusable outside its own 14 labels: `_to_ost` fell through to
# `.get(label, label)`, handed the addin a display name, and the addin
# answered `[invalid_parameter] Unknown BuiltInCategory 'Doors'`. Every
# geometry rule on a category not listed here failed — 50 of the catalog's 63
# — which nothing noticed because no shipped config carries a geometry rule
# and the tests use the MEP labels the feature was built for. Found by
# probing live on Snowdon, 2026-08-03.
#
# Two entries below are NOT redundant with the catalog and are the reason this
# map still exists at all:
#   * "Columns" -> OST_Columns. The catalog lists "Columns" as an ALIAS of
#     Structural Columns (OST_StructuralColumns), so routing it through the
#     catalog would silently retarget an architectural-column rule at the
#     structural category — in Snowdon that is 140 instances versus a category
#     with none, i.e. a clean bill of health from an empty set. The catalog has
#     no entry for architectural columns at all; reconciling that is a catalog
#     decision, not something to settle by deleting a line here.
#   * "Spaces"/"Rooms" and the MEP curve labels are kept verbatim so this
#     module's answers cannot move if the catalog's aliases are ever retuned.
# Everything else here agrees with the catalog and is retained only so the
# resolution for the labels this feature shipped with cannot change.
_OST_BY_LABEL: dict[str, str] = {
    "Ducts": "OST_DuctCurves",
    "Duct Fittings": "OST_DuctFitting",
    "Pipes": "OST_PipeCurves",
    "Pipe Fittings": "OST_PipeFitting",
    "Floors": "OST_Floors",
    "Ceilings": "OST_Ceilings",
    "Walls": "OST_Walls",
    "Columns": "OST_Columns",
    "Structural Columns": "OST_StructuralColumns",
    "Structural Framing": "OST_StructuralFraming",
    "Cable Trays": "OST_CableTray",
    "Conduits": "OST_Conduit",
    "Spaces": "OST_MEPSpaces",
    "Rooms": "OST_Rooms",
}

# Candidate name keywords per reference_source, checked (lowercase substring)
# against the loaded link names in order. MEP models are named by DISCIPLINE,
# not "MEP" (verified live on R27: "...HVAC", "...Plumbing", "...Electrical",
# "...Structural", "...Site"), so linked_mep must try every discipline keyword
# — matching only "MEP" silently resolved nothing and clearance checks fell
# back to the host model (0 clashes). A rule that loads several links of one
# discipline should disambiguate with GeometryRule.reference_link_hint.
_SOURCE_TO_HINTS: dict[str, tuple[str, ...]] = {
    "linked_arch": ("Architectural", "Arch"),
    "linked_struct": ("Structural", "Struct"),
    "linked_mep": ("MEP", "HVAC", "Mechanical", "Plumbing", "Electrical", "Duct", "Pipe"),
}

# Keyword priority for auto-selecting a named 3D view (lowercase, checked with `in`).
_VIEW_KEYWORDS = ("3d", "coordination", "clash", "check", "mep")

# Revit MCP serialises ViewType.ThreeD as "ThreeD" (verified live on the
# RevitMCPServer addin). Accept the spelled-out + "3D" forms too so the
# resolution is robust across addin versions.
_THREE_D_VIEW_TYPES = {"ThreeD", "ThreeDimensional", "3D"}

_SEV_ORDER: dict[str, int] = {"severity_high": 0, "severity_medium": 1, "severity_low": 2}


@lru_cache(maxsize=1)
def _shared_catalog() -> OSTCatalog | None:
    """The OST catalog, loaded once. None if it cannot be read.

    `OSTCatalog.load()` re-reads and re-validates the YAML on every call, and
    `_to_ost` runs once per rule per batch, so this is cached. A failure to
    load must not take the geometry agent down: resolution then degrades to
    the override map exactly as it behaved before the catalog was consulted,
    and the caller still gets a loud `invalid_parameter` from the addin rather
    than a wrong category.
    """
    try:
        return OSTCatalog.load()
    except Exception as exc:  # degrade, never fail the run
        log.warning("geometry_query.catalog_unavailable", error=str(exc))
        return None


def _to_ost(label: str) -> str:
    """Resolve a rule's display label to a Revit BuiltInCategory enum name.

    Order: the override map above, then the shared OST catalog, then the label
    unchanged. The last step is not a fallback anyone should rely on — it
    reaches the addin as an unknown category and fails the rule loudly — but
    returning the label is still better than raising, because the geometry
    coverage record then reports WHICH rule could not be resolved instead of
    losing the whole batch.
    """
    mapped = _OST_BY_LABEL.get(label)
    if mapped is not None:
        return mapped
    catalog = _shared_catalog()
    entry = catalog.find(label) if catalog else None
    if entry is not None and entry.ost:
        return entry.ost
    return label


def _clearance_actual_mm(clash: dict[str, Any]) -> float:
    """Read the actual clearance from a clash payload, preferring
    ``clearanceActualMm`` then ``clearanceMm`` (then ``actualMm`` for
    ``_clash_to_finding``'s extra fallback key).

    Low1: this MUST coalesce by ``is not None``, not Python's ``or`` chain.
    ``0.0`` mm (an element touching/overlapping its reference — the WORST
    clash, not a missing reading) is falsy, so ``a or b or 0`` silently
    swallows a real 0.0 reading and falls through to the next key (or the
    ``0`` default) — same numeric result by luck here, but a genuine 0.0 in
    ``clearanceMm`` while ``clearanceActualMm`` is absent would have been
    read correctly either way; the real bug is when the PREFERRED key is
    legitimately 0.0 and a defensive rewrite later reorders the keys or adds
    a non-zero fallback — this helper makes "0.0 is a value, not missing"
    explicit so that can't regress silently.
    """
    for key in ("clearanceActualMm", "clearanceMm", "actualMm"):
        val = clash.get(key)
        if val is not None:
            return float(val)
    return 0.0


def _has_measured_clearance(clash: dict[str, Any]) -> bool:
    """True when the payload actually carries a distance reading.

    Only the Z raycast measures one (``RunRaycastClash`` writes
    ``clearanceActualMm``); a bbox hit is a bare pair. The distinction matters
    because ``_clearance_actual_mm``'s 0.0 default is indistinguishable from a
    real touching-pair reading, and printing it renders "we did not measure"
    as "we measured zero" — the same class of dishonest-clean the geometry
    path has been burned by twice (P1-GEO-01, H-01).
    """
    return any(
        clash.get(key) is not None
        for key in ("clearanceActualMm", "clearanceMm", "actualMm")
    )


def _axis_and_direction(direction: str) -> tuple[str, str | None]:
    """Map a GeometryRule clearance_direction to the (axis, direction) the
    revit_check_clearance tool accepts.

    The tool's enums are axis ∈ {Z, bbox} and direction ∈ {below, above}:
      below/above → axis='Z'  (vertical raycast, direction passed through)
      horizontal  → axis='bbox' (omnidirectional AABB proximity, no direction)
    """
    if direction == "horizontal":
        return "bbox", None
    return "Z", direction


def _severity_from_fraction(actual_mm: float, threshold_mm: float) -> Severity:
    """Classify severity by what fraction of the required clearance is present."""
    if threshold_mm <= 0:
        return "severity_low"
    fraction = actual_mm / threshold_mm
    if fraction < 0.05:       # < 5 % — nearly no clearance
        return "severity_high"
    if fraction < 0.50:       # 5–50 %
        return "severity_medium"
    return "severity_low"     # 50–100 % — near-miss


def _severity_from_excess(actual_mm: float, threshold_mm: float) -> Severity:
    """Severity for a clearance_max violation: how far PAST the allowed
    maximum the element sits. The min-rule scale above runs the wrong way for
    a max rule (there, the SMALLER the reading the worse; here the larger) —
    H-01 shipped max findings graded on that inverted scale.
    """
    if threshold_mm <= 0:
        # "Must be touching" — any measurable gap at all breaks it outright.
        return "severity_high"
    ratio = actual_mm / threshold_mm
    if ratio >= 2.0:          # over double the allowed gap
        return "severity_high"
    if ratio >= 1.5:          # 150-200 %
        return "severity_medium"
    return "severity_low"     # 100-150 % - near-miss


# ── clearance_max probe (H-01) ────────────────────────────────────────────────
# The addin's check_clearance only ever RETURNS pairs CLOSER than the
# clearanceMm it was called with (CheckClearanceCommand.cs, RunRaycastClash:
# ``if (proximityMm >= clearanceMm) continue``). A max rule's violations are
# the pairs FARTHER than the threshold — so calling the tool with the rule's
# own threshold makes every real violation invisible and returns only the
# compliant pairs. The tool must be probed WIDER than the threshold and the
# verdict computed client-side from the measured ``clearanceActualMm``.
_MAX_PROBE_FACTOR = 4.0
_MAX_PROBE_MARGIN_MM = 2000.0


def _max_rule_probe_mm(threshold_mm: float) -> float:
    """How far past a max rule's threshold to ask the tool to measure.

    The trade: a violation farther than the probe is indistinguishable from
    "no reference there at all" (the response carries no set-A roster), while
    every measured pair inside the probe costs one row of the maxResults
    budget. 4x the threshold with a 2 m floor comfortably spans a storey for
    the realistic thresholds ("duct within 2400 mm of the slab" → 9.6 m probe)
    without asking the tool to measure the whole model.
    """
    return max(threshold_mm * _MAX_PROBE_FACTOR, threshold_mm + _MAX_PROBE_MARGIN_MM)


@dataclass(frozen=True)
class _ClearanceKey:
    """Hashable grouping key for batching clearance_min MCP calls.

    ``bbox_threshold_mm`` (bbox-refilter bug, 2026-08-01 — found while fixing
    the clearance_max dead check, H-01) is the reason a bbox batch
    can hold only ONE threshold. Batching asks the tool for the group's LARGEST
    threshold and re-applies each rule's own in Python — which needs a measured
    distance per hit. Only the Z raycast reports one (``clearanceActualMm``);
    ``RunBboxClash`` emits a bare pair, so ``_clearance_actual_mm`` reads 0.0
    and the ``< threshold`` refilter degenerates to "always true": batching a
    100 mm and a 500 mm horizontal rule handed the 100 mm rule every pair out
    to 500 mm. Putting the threshold in the KEY makes each bbox batch a single
    threshold, and the addin's own AABB inflation — exact, on real geometry —
    becomes the filter. ``None`` for axis="Z", which keeps the existing (and
    correct) Z batching untouched.
    """
    set_a_ost: str
    set_b_ost: str
    set_b_link_id: int | None
    axis: str
    direction: str | None
    view_id: int | None
    bbox_threshold_mm: float | None = None


def _dedup_by_element(findings: list[Finding]) -> list[Finding]:
    """Merge findings that share the same element_id into one Finding.

    Worst severity wins; rule_ids are joined with ' | '; individual messages
    are numbered and concatenated so the ACC Issue body captures all issues.
    Single-element groups pass through unchanged.
    """
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        groups[f["element_id"]].append(f)

    merged: list[Finding] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        group.sort(key=lambda f: _SEV_ORDER.get(f["severity"], 9))
        worst = group[0]
        rule_ids = " | ".join(dict.fromkeys(f["rule_id"] for f in group))
        msgs = "\n".join(f"  [{i + 1}] {f['message']}" for i, f in enumerate(group))
        m: Finding = {
            **worst,
            "rule_id": rule_ids,
            "message": f"{len(group)} violations on this element:\n{msgs}",
        }
        merged.append(m)
    return merged


class GeometricQueryAgent:
    """Runs geometry checks defined in a list of GeometryRules against Revit MCP.

    Args:
        mcp: Connected Revit client (AnyRevitClient or any object exposing
             ``check_clearance``, ``get_linked_files``, ``get_active_view``,
             and ``get_views``).
        geometry_rules: GeometryRule objects from the loaded RuleSet.
        linked_file_ids: Optional dict mapping a link lookup key to the Revit
            link element ID (integer). The key is a rule's
            ``reference_link_hint`` when set, else its ``reference_source``
            (``linked_arch``/``linked_struct``/``linked_mep``). When a key is
            absent the agent calls ``revit_get_linked_files`` and matches by
            keyword hint (per-discipline keywords; see ``_SOURCE_TO_HINTS``).
        view_id: Agent-level view override (highest priority). When None the
            agent resolves the view automatically:
            active 3D view → named 3D view → first 3D view → None.
            Per-rule view_id in YAML takes priority over this value.
        sample_count: Ray samples per element edge (default 3).
        max_elements: P0 element budget — caps how many setA elements the
            addin loads per check (maps to setA.limit). Default 300.
        max_clashes: caps returned clash pairs (maps to maxResults). Default
            500 — generous over the element budget so a duct hitting several
            floors isn't silently clipped, but well under the tool max (2000).
    """

    def __init__(
        self,
        mcp: AnyRevitClient,
        geometry_rules: list[GeometryRule],
        *,
        linked_file_ids: dict[str, int] | None = None,
        view_id: int | None = None,
        sample_count: int = 3,
        max_elements: int = 300,
        max_clashes: int = 500,
    ) -> None:
        self._mcp = mcp
        self._rules = [
            r for r in geometry_rules
            if r.check_type in ("clearance_min", "clearance_max")
        ]
        self._skipped = [
            r for r in geometry_rules
            if r.check_type not in ("clearance_min", "clearance_max")
        ]
        self._linked_file_ids: dict[str, int] = dict(linked_file_ids or {})
        self._view_id = view_id
        self._sample_count = sample_count
        self._max_elements = max_elements
        self._max_clashes = max_clashes
        # P1-GEO-01/02 (2026-07-26 independent review): per-run execution
        # evidence. `[]` used to mean BOTH "checked, nothing found" and "the
        # check never ran", and that ambiguity travelled all the way to
        # exit 0 + a report headline. These three lists are what separates them.
        self._unresolved_links: dict[str, dict[str, Any]] = {}
        self._executed: list[str] = []
        self._failed: list[dict[str, Any]] = []
        self._truncated: list[dict[str, Any]] = []
        # spatial_filter support (2026-08-26): lazy caches, built only when a
        # rule actually declares one — unfiltered runs pay nothing.
        self._space_boxes: list[tuple[float, str, dict[str, Any]]] | None = None
        self._centroid_cache: dict[int, dict[str, float] | None] = {}

    @property
    def coverage(self) -> dict[str, Any]:
        """What this run actually managed to CHECK — the geometry twin of
        ``query_coverage`` (mirrors that shape on purpose so the orchestrator,
        run recorder and report can treat both the same way).

        ``verdict``:
          * ``ok``       — every dispatched rule got a real answer from Revit.
          * ``partial``  — some rules ran, some could not.
          * ``no_audit`` — rules were requested and NONE of them ran. This is
            the case that used to render as a clean audit.
          * ``None``     — no geometry rules at all (not applicable).
        """
        requested = len(self._rules) + len(self._skipped)
        if requested == 0:
            return {}
        if self._executed and not self._failed:
            verdict = "ok"
        elif self._executed:
            verdict = "partial"
        else:
            verdict = "no_audit"
        cov: dict[str, Any] = {
            "rules_requested": requested,
            "rules_executed": sorted(self._executed),
            "rules_failed": self._failed,
            "verdict": verdict,
        }
        if self._skipped:
            cov["rules_unsupported"] = sorted(r.id for r in self._skipped)
        if self._truncated:
            cov["truncated"] = self._truncated
        return cov

    # ── public entry point ─────────────────────────────────────────────────

    async def run(self) -> list[Finding]:
        for r in self._skipped:
            log.warning(
                "geometry_query.check_type_unsupported",
                rule_id=r.id,
                check_type=r.check_type,
                detail="Only clearance_min/clearance_max dispatched in this version",
            )
        if not self._rules:
            return []

        # Step 1: resolve view once up-front
        agent_view_id = await self._resolve_view_id()

        # Step 2: prefetch all needed link IDs in one API call
        await self._prefetch_link_ids()

        # Step 3: group clearance_min into batches; clearance_max run per-rule
        min_batches, max_rules = self._build_batches(agent_view_id)

        # Step 4: all tasks run in parallel. Keep each task's rules alongside it
        # so an UNEXPECTED exception (one the inner handlers didn't catch) still
        # names the rules it took down — P1-GEO-01: a task index in a log line
        # is not something a coverage record or a report can use.
        tasks: list = []
        task_rules: list[list[GeometryRule]] = []
        for key, rules in min_batches.items():
            tasks.append(self._run_min_batch(key, rules))
            task_rules.append(rules)
        for rule in max_rules:
            tasks.append(self._run_max_rule(rule, agent_view_id))
            task_rules.append([rule])

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_findings: list[Finding] = []
        for i, result in enumerate(results):
            # L-09: a cancellation is NOT a per-task failure. CancelledError is
            # a BaseException, so the `isinstance(result, Exception)` test below
            # let it fall through to the else branch, where `extend()` raised
            # `TypeError: 'CancelledError' object is not iterable` — the audit
            # dying of a confusing type error instead of stopping, and every
            # other task's rules missing from `_failed` because the loop never
            # finished. Same shape as `revit_query`'s hydrate fan-out.
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
            if isinstance(result, Exception):
                log.error("geometry_query.task_failed", task_index=i, error=str(result))
                for rule in task_rules[i]:
                    if rule.id in self._executed:
                        continue
                    self._failed.append({
                        "rule_id": rule.id,
                        "reason": "task_error",
                        "detail": str(result),
                    })
            else:
                all_findings.extend(result)  # type: ignore[arg-type]

        # Step 5: one ACC Issue per physical element
        return _dedup_by_element(all_findings)

    # ── view resolution ────────────────────────────────────────────────────

    async def _resolve_view_id(self) -> int | None:
        """Resolve agent-level view_id with priority:
        1. Constructor view_id  2. active ThreeDimensional view
        3. named 3D view (keyword priority)  4. first 3D view  5. None
        """
        if self._view_id is not None:
            return self._view_id

        try:
            active = await self._mcp.get_active_view()
            if active and active.get("viewType") in _THREE_D_VIEW_TYPES:
                vid = active.get("id")
                if vid is not None:
                    log.info(
                        "geometry_query.view_resolved",
                        source="active_view",
                        view_id=int(vid),
                    )
                    return int(vid)
        except Exception as exc:
            log.debug("geometry_query.get_active_view_failed", error=str(exc))

        try:
            views = await self._mcp.get_views()
            three_d = [v for v in views if v.get("viewType") in _THREE_D_VIEW_TYPES]
            for kw in _VIEW_KEYWORDS:
                for v in three_d:
                    if kw in (v.get("name") or "").lower():
                        log.info(
                            "geometry_query.view_resolved",
                            source="named_3d",
                            name=v.get("name"),
                            view_id=v.get("id"),
                        )
                        return int(v["id"])
            if three_d:
                log.info(
                    "geometry_query.view_resolved",
                    source="first_3d",
                    name=three_d[0].get("name"),
                    view_id=three_d[0].get("id"),
                )
                return int(three_d[0]["id"])
        except Exception as exc:
            log.debug("geometry_query.get_views_failed", error=str(exc))

        log.warning("geometry_query.no_3d_view_found")
        return None

    # ── link prefetch ──────────────────────────────────────────────────────

    @staticmethod
    def _link_lookup_key(rule: GeometryRule) -> str | None:
        """Cache key for the link a rule references; None for same_model.

        A rule-level ``reference_link_hint`` keys the cache by that hint string
        (so two linked_mep rules pointing at different links — "HVAC" vs
        "Plumbing" — resolve and batch independently); otherwise the key is the
        ``reference_source`` (back-compatible with constructor-supplied ids).
        """
        if rule.reference_source == "same_model":
            return None
        return rule.reference_link_hint or rule.reference_source

    @staticmethod
    def _hint_candidates(rule: GeometryRule) -> tuple[str, ...]:
        """Ordered link-name keywords to match for a rule's reference link."""
        if rule.reference_link_hint:
            return (rule.reference_link_hint,)
        return _SOURCE_TO_HINTS.get(rule.reference_source, (rule.reference_source,))

    @staticmethod
    def _match_link(
        links: list[dict[str, Any]], candidates: tuple[str, ...]
    ) -> tuple[int, str] | None:
        """First (link_id, name) whose name contains any candidate keyword."""
        for cand in candidates:
            needle = cand.lower()
            for link in links:
                name: str = (
                    link.get("name") or link.get("title") or link.get("fileName") or ""
                )
                if needle in name.lower():
                    link_id = (
                        link.get("id") or link.get("elementId") or link.get("linkId")
                    )
                    if link_id is not None:
                        return int(link_id), name
        return None

    async def _prefetch_link_ids(self) -> None:
        """Populate self._linked_file_ids for any link not yet cached.

        Makes at most one revit_get_linked_files call regardless of how many
        rules reference linked files. Keyed by the effective hint
        (``reference_link_hint`` when set, else ``reference_source``) so a
        per-rule hint can disambiguate among several same-discipline links.
        """
        needed: dict[str, tuple[str, ...]] = {}
        for r in self._rules:
            key = self._link_lookup_key(r)
            if key is None or key in self._linked_file_ids:
                continue
            needed.setdefault(key, self._hint_candidates(r))
        if not needed:
            return

        try:
            links = await self._mcp.get_linked_files()
        except Exception as exc:
            log.warning("geometry_query.link_prefetch_failed", error=str(exc))
            # P1-GEO-02: returning here left every needed key unresolved, and
            # the rules then ran against the HOST model. Record them so the
            # batch builder blocks those rules instead of quietly rescoping.
            for key, candidates in needed.items():
                self._unresolved_links[key] = {
                    "reason": "link_discovery_failed",
                    "candidates": list(candidates),
                    "detail": str(exc),
                }
            return

        for key, candidates in needed.items():
            matched = self._match_link(links, candidates)
            if matched is not None:
                link_id, name = matched
                self._linked_file_ids[key] = link_id
                log.info(
                    "geometry_query.link_resolved",
                    key=key,
                    candidates=candidates,
                    link_id=link_id,
                    name=name,
                )
            else:
                available = [
                    (l.get("name") or l.get("title") or l.get("fileName") or "")
                    for l in links
                ]
                log.warning(
                    "geometry_query.link_not_found",
                    key=key,
                    candidates=candidates,
                    available=available,
                )
                # P1-GEO-02: this is a SCOPE failure, not a soft miss. The rule
                # asked about a specific federated model; running it against
                # the host answers a different question and reports it as if
                # it were the one asked.
                self._unresolved_links[key] = {
                    "reason": "link_not_found",
                    "candidates": list(candidates),
                    "available": available,
                }

    # ── batch building ─────────────────────────────────────────────────────

    def _block_if_link_unresolved(self, rule: GeometryRule) -> bool:
        """True when ``rule`` must not run because its reference link is missing.

        P1-GEO-02. Records a coverage failure carrying what the rule ASKED for
        (`reference_source` / hint) next to what the model actually offered, so
        the report can tell the author which federated model is missing rather
        than just "0 violations".
        """
        key = self._link_lookup_key(rule)
        if key is None:
            return False                      # same_model — host is correct here
        info = self._unresolved_links.get(key)
        if info is None:
            return False                      # resolved (or supplied by caller)
        log.error(
            "geometry_query.rule_blocked_unresolved_link",
            rule_id=rule.id,
            reference_source=rule.reference_source,
            link_hint=rule.reference_link_hint,
            reason=info.get("reason"),
            detail="rule NOT run — refusing to silently check the host model",
        )
        self._failed.append({
            "rule_id": rule.id,
            "reason": info.get("reason", "link_unresolved"),
            "reference_source": rule.reference_source,
            "link_hint": rule.reference_link_hint,
            "candidates": info.get("candidates", []),
            "available": info.get("available", []),
        })
        return True

    def _build_batches(
        self, agent_view_id: int | None
    ) -> tuple[dict[_ClearanceKey, list[GeometryRule]], list[GeometryRule]]:
        """Partition rules into clearance_min batches and clearance_max singletons."""
        min_batches: dict[_ClearanceKey, list[GeometryRule]] = defaultdict(list)
        max_rules: list[GeometryRule] = []

        for rule in self._rules:
            # P1-GEO-02: a rule whose reference link never resolved is BLOCKED,
            # not degraded. Letting it through with link_id=None makes the
            # client send `setB.source="host"` — the check still runs, still
            # reports, and answers a question nobody asked.
            if self._block_if_link_unresolved(rule):
                continue
            if rule.check_type == "clearance_max":
                max_rules.append(rule)
                continue
            raw_direction = rule.clearance_direction or "below"
            axis, direction = _axis_and_direction(raw_direction)
            link_key = self._link_lookup_key(rule)
            link_id = self._linked_file_ids.get(link_key) if link_key else None
            # Per-rule view_id takes priority over agent-level resolved view.
            view_id = rule.view_id if rule.view_id is not None else agent_view_id
            key = _ClearanceKey(
                set_a_ost=_to_ost(rule.category),
                set_b_ost=_to_ost(rule.reference_category or ""),
                set_b_link_id=link_id,
                axis=axis,
                direction=direction,
                view_id=view_id,
                # bbox-refilter bug (H-01 follow-up): bbox hits carry no measured
                # distance, so the Python
                # refilter can't separate two thresholds inside one batch —
                # the threshold has to BE the batch boundary. See _ClearanceKey.
                bbox_threshold_mm=(rule.threshold_mm or 0.0) if axis == "bbox" else None,
            )
            min_batches[key].append(rule)

        return dict(min_batches), max_rules

    # ── async tasks ────────────────────────────────────────────────────────

    async def _load_space_boxes(self) -> list[tuple[float, str, dict[str, Any]]]:
        """Space name + bbox, smallest volume first — same containment idiom as
        ``revit_query._enrich_containing_space`` (K4): tightest fit wins when
        axis-aligned boxes overlap. Cached for the run."""
        if self._space_boxes is not None:
            return self._space_boxes
        boxes: list[tuple[float, str, dict[str, Any]]] = []
        try:
            spaces = await self._mcp.list_spaces(limit=self._max_elements)
        except Exception as exc:
            log.warning("geometry_query.spatial_filter_space_list_failed", error=str(exc))
            self._space_boxes = boxes
            return boxes
        sem = asyncio.Semaphore(8)

        async def _box(space: dict[str, Any]):
            async with sem:
                try:
                    g = await self._mcp.get_element_geometry(int(space["id"]))
                except Exception:
                    return None
            bbox = (g or {}).get("boundingBox")
            if not bbox:
                return None
            try:
                return (float(space.get("volume") or 0.0), str(space.get("name") or ""), bbox)
            except (TypeError, ValueError):
                return None

        results = await asyncio.gather(*(_box(s) for s in spaces), return_exceptions=True)
        boxes = [b for b in results if b is not None and not isinstance(b, BaseException)]
        boxes.sort(key=lambda b: b[0])
        self._space_boxes = boxes
        return boxes

    async def _containing_space_name(self, element_id: int) -> str | None:
        """Name of the smallest space whose bbox contains the element centroid."""
        if element_id not in self._centroid_cache:
            try:
                g = await self._mcp.get_element_geometry(element_id)
                self._centroid_cache[element_id] = (g or {}).get("centroid")
            except Exception:
                self._centroid_cache[element_id] = None
        c = self._centroid_cache[element_id]
        if not c:
            return None
        for _vol, name, bbox in await self._load_space_boxes():
            if _point_in_bbox(c, bbox):
                return name
        return None

    async def _apply_spatial_filter(
        self, rule: GeometryRule, clashes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Keep only clashes whose set-A element sits inside a matching space.

        The schema (and the Builder's Geometry tab) have carried
        ``spatial_filter`` since 3b — but until 2026-08-26 NOTHING consumed it:
        a scoped rule silently behaved unscoped, the exact silent-blind-spot
        class rules-lint exists to prevent. Motivating case (Ken, HVAC): scope
        the headroom rule to the Parking space, because service platforms in
        the mech shafts legitimately sit 300-700 mm under duct runs and drown
        the walkable-space story.

        Containment is centroid-in-space-bbox (smallest volume wins) — the
        SAME rule the Mark composer uses, so "the duct's space" means one
        thing across the product. Matching: ``name_exact`` (case-insensitive)
        when given, else ``name_contains`` (case-insensitive substring).
        Cheap by construction: only VIOLATIONS are looked up, never all of
        set A. A failed space lookup fails OPEN (unfiltered + warning) — extra
        out-of-scope findings are visible noise; dropping real violations
        because a lookup hiccuped would be a silent hole.
        """
        sf = rule.spatial_filter
        if sf is None or not (sf.name_exact or sf.name_contains):
            return clashes
        boxes = await self._load_space_boxes()
        if not boxes:
            log.warning(
                "geometry_query.spatial_filter_unresolved",
                rule_id=rule.id, reason="no space boxes — findings left unfiltered",
            )
            return clashes
        kept: list[dict[str, Any]] = []
        for c in clashes:
            try:
                eid = int((c.get("elementA") or {}).get("id"))
            except (TypeError, ValueError):
                kept.append(c)  # unidentifiable set-A element: fail open
                continue
            name = await self._containing_space_name(eid) or ""
            if sf.name_exact is not None:
                match = name.casefold() == sf.name_exact.casefold()
            else:
                match = (sf.name_contains or "").casefold() in name.casefold()
            if match:
                kept.append(c)
        log.info(
            "geometry_query.spatial_filter_applied",
            rule_id=rule.id,
            kept=len(kept),
            dropped=len(clashes) - len(kept),
            name_exact=sf.name_exact,
            name_contains=sf.name_contains,
        )
        return kept

    async def _run_min_batch(
        self, key: _ClearanceKey, rules: list[GeometryRule]
    ) -> list[Finding]:
        """One MCP call for the batch; Python-side per-rule threshold filtering."""
        max_threshold = max(r.threshold_mm or 0.0 for r in rules)
        try:
            clashes = await self._mcp.check_clearance(
                set_a_category=key.set_a_ost,
                set_b_category=key.set_b_ost,
                axis=key.axis,
                direction=key.direction,
                clearance_mm=max_threshold,
                set_b_link_id=key.set_b_link_id,
                view_id=key.view_id,
                sample_count=self._sample_count,
                set_a_limit=self._max_elements,
                max_results=self._max_clashes,
            )
        except Exception as exc:
            log.error(
                "geometry_query.batch_failed",
                set_a=key.set_a_ost,
                set_b=key.set_b_ost,
                error=str(exc),
            )
            # P1-GEO-01: `return []` here is what made "the check crashed" and
            # "the check found nothing" the same value downstream.
            for rule in rules:
                self._failed.append({
                    "rule_id": rule.id,
                    "reason": "mcp_error",
                    "detail": str(exc),
                })
            return []
        self._warn_if_clipped(clashes, key.set_a_ost, key.set_b_ost, rules)

        # bbox-refilter bug (H-01 follow-up): only the Z raycast measures a
        # distance per hit, so only Z can be
        # refiltered per rule. A bbox batch carries ONE threshold by key, and the
        # addin already applied it via AABB inflation — every returned pair is a
        # violation of this rule. (Refiltering bbox anyway wasn't merely
        # redundant: `0.0 < threshold` waved through every pair of the batch's
        # LARGEST threshold, and for a hard-clash rule — threshold 0 — `0.0 <
        # 0.0` dropped every real hit instead.)
        measured = key.axis == "Z"
        findings: list[Finding] = []
        for rule in rules:
            self._executed.append(rule.id)
            threshold = rule.threshold_mm or 0.0
            rule_clashes = [
                c for c in clashes
                if _clearance_actual_mm(c) < threshold
            ] if measured else list(clashes)
            rule_clashes = await self._apply_spatial_filter(rule, rule_clashes)
            findings.extend(
                self._clash_to_finding(c, rule, threshold) for c in rule_clashes
            )
            log.info(
                "geometry_query.rule_done",
                rule_id=rule.id,
                check_type=rule.check_type,
                violations=len(rule_clashes),
            )

        return findings

    async def _run_max_rule(
        self, rule: GeometryRule, agent_view_id: int | None
    ) -> list[Finding]:
        """Single MCP call for one clearance_max rule (cannot batch with min).

        H-01 (2026-08-01 review): this used to call the tool with the rule's
        own threshold and turn every returned pair into a finding. The tool
        only returns pairs CLOSER than the requested distance (see the probe
        note above), so real violations (farther than the threshold) never
        came back — the check read as clean — while the pairs that did come
        back were the COMPLIANT ones, flagged. A dead check that also cried
        wolf. Now:

          * probe wider than the threshold, judge from ``clearanceActualMm``:
            nearest measured reference farther than the threshold → violation
            (with the measured distance in the message), nearer → compliant;
          * set-A elements with NOTHING measured inside the probe are NOT
            evaluated — the response carries no roster, so an element whose
            ray hit nothing (no reference there, masked by a non-reference
            element, or skipped by the addin's vertical-element filter)
            cannot even be named client-side, let alone judged. Closing that
            needs an addin-side max mode:
            a gap report forwarded to the add-in team;
          * only the vertical raycast (below/above) reports measured
            distances — bbox mode (horizontal) emits bare pair hits, so a
            horizontal max rule fails CLOSED as unsupported rather than
            guessing (P1-GEO-01: "could not check" must never render as
            "checked, clean").
        """
        threshold = rule.threshold_mm or 0.0
        raw_direction = rule.clearance_direction or "below"
        axis, direction = _axis_and_direction(raw_direction)
        if axis != "Z":
            log.warning(
                "geometry_query.max_rule_unsupported_direction",
                rule_id=rule.id, direction=raw_direction,
            )
            self._failed.append({
                "rule_id": rule.id,
                "reason": "unsupported_direction",
                "detail": (
                    "clearance_max needs a measured distance; only the vertical "
                    "raycast (below/above) reports clearanceActualMm — horizontal "
                    "bbox hits carry none"
                ),
            })
            return []

        probe_mm = _max_rule_probe_mm(threshold)
        link_key = self._link_lookup_key(rule)
        link_id = self._linked_file_ids.get(link_key) if link_key else None
        view_id = rule.view_id if rule.view_id is not None else agent_view_id

        try:
            clashes = await self._mcp.check_clearance(
                set_a_category=_to_ost(rule.category),
                set_b_category=_to_ost(rule.reference_category or ""),
                axis=axis,
                direction=direction,
                clearance_mm=probe_mm,
                set_b_link_id=link_id,
                view_id=view_id,
                sample_count=self._sample_count,
                set_a_limit=self._max_elements,
                max_results=self._max_clashes,
            )
        except Exception as exc:
            log.error("geometry_query.check_failed", rule_id=rule.id, error=str(exc))
            self._failed.append({
                "rule_id": rule.id, "reason": "mcp_error", "detail": str(exc),
            })
            return []
        self._warn_if_clipped(
            clashes, _to_ost(rule.category),
            _to_ost(rule.reference_category or ""), [rule],
        )
        self._executed.append(rule.id)

        # The NEAREST reference decides the verdict: an element within the
        # threshold of any reference complies, however far the others are.
        nearest: dict[str, dict[str, Any]] = {}
        for c in clashes:
            eid = str((c.get("elementA") or {}).get("id") or c.get("elementId") or "")
            if not eid:
                continue
            prev = nearest.get(eid)
            if prev is None or _clearance_actual_mm(c) < _clearance_actual_mm(prev):
                nearest[eid] = c
        violations = [
            c for c in nearest.values() if _clearance_actual_mm(c) > threshold
        ]
        violations = await self._apply_spatial_filter(rule, violations)

        log.info(
            "geometry_query.rule_done",
            rule_id=rule.id,
            check_type=rule.check_type,
            violations=len(violations),
            measured_elements=len(nearest),
            probe_mm=probe_mm,
        )
        return [
            self._clash_to_finding(c, rule, threshold, exceeds=True)
            for c in violations
        ]

    def _warn_if_clipped(
        self, clashes: list[Any], set_a: str, set_b: str,
        rules: list[GeometryRule] | None = None,
    ) -> None:
        """Surface when the maxResults cap likely truncated the clash list.

        P1-RENDER-02: this used to log and stop there, so the run artifact kept
        no trace and the report presented a capped count as if it were the
        total — "500 clashes" reading as exact when the truth is "at least
        500". The cap now rides on coverage so the renderer can say so.
        """
        if len(clashes) >= self._max_clashes:
            log.warning(
                "geometry_query.clash_cap_hit",
                set_a=set_a,
                set_b=set_b,
                cap=self._max_clashes,
                detail=(
                    f"Returned clashes hit the cap ({self._max_clashes}); some "
                    f"violations may be omitted. Raise max_clashes or narrow scope."
                ),
            )
            for rule in rules or []:
                self._truncated.append({
                    "rule_id": rule.id,
                    "cap": self._max_clashes,
                    "returned": len(clashes),
                    "set_a": set_a,
                    "set_b": set_b,
                })

    # ── finding builder ────────────────────────────────────────────────────

    def _clash_to_finding(
        self,
        clash: dict[str, Any],
        rule: GeometryRule,
        threshold_mm: float,
        *,
        exceeds: bool = False,
    ) -> Finding:
        """``exceeds=True`` is the clearance_max sense: the element is flagged
        for being FARTHER than the threshold, so the message says which way
        the comparison failed and severity grades the excess, not the
        shortfall (H-01: max findings used to ride the min-rule scale, which
        runs the wrong way — the worst offender read as the mildest)."""
        elem_a = clash.get("elementA") or {}
        elem_b = clash.get("elementB") or {}

        actual_mm = _clearance_actual_mm(clash)
        element_id = str(elem_a.get("id") or clash.get("elementId") or "")
        element_name = str(elem_a.get("name") or "")
        ref_name = str(elem_b.get("name") or "")

        if exceeds:
            severity = _severity_from_excess(actual_mm, threshold_mm)
            msg = (
                f"{rule.description}: actual {actual_mm:.1f} mm "
                f"exceeds maximum {threshold_mm:.0f} mm"
            )
        else:
            if _has_measured_clearance(clash):
                severity = _severity_from_fraction(actual_mm, threshold_mm)
                msg = (
                    f"{rule.description}: actual {actual_mm:.1f} mm "
                    f"(threshold {threshold_mm:.0f} mm)"
                )
            elif threshold_mm <= 0:
                # Unmeasured row on a threshold-0 rule = the addin's OWN
                # "hard_clash" classification (RunBboxClash: `clearanceFt > 0 ?
                # "clearance_violation" : "hard_clash"` — same boundary as this
                # branch): the elements INTERSECT. The worst outcome this check
                # can find, yet the fraction scale graded it severity_low — its
                # threshold<=0 branch was written for the measured path, where
                # these rows never appear (a Z ray probed with clearanceMm=0
                # returns nothing). Owner decision 2026-08-02: hard clash →
                # high, unmeasured proximity → medium.
                severity = "severity_high"
                msg = (
                    f"{rule.description}: elements intersect "
                    f"(hard clash — distance not measured)"
                )
            else:
                # bbox proximity: the violation is real — the addin's AABB
                # inflation applied THIS rule's threshold — but its magnitude
                # was never taken, and an unknown gap is not evidence of the
                # worst gap (the fraction scale read the 0.0 default as
                # "measured zero" and graded every such hit high). Medium, and
                # the message says the distance was not measured.
                severity = "severity_medium"
                msg = (
                    f"{rule.description}: within {threshold_mm:.0f} mm "
                    f"(bbox proximity — distance not measured)"
                )
        if ref_name:
            msg += f" — nearest: {ref_name}"

        f: Finding = {
            "rule_id": rule.id,
            "element_id": element_id,
            "parameter": "clearance_mm",
            "severity_tag": rule.severity_tag,
            "severity": severity,
            "message": msg,
            "suggested_value": None,
            "citation": None,
            "status": "non_compliant",
        }
        if element_name:
            f["element_name"] = element_name
        return f
