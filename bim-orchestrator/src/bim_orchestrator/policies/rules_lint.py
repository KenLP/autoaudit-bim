"""Static rules-lint (v1.5-R7, R1-Stage 2) — Aiken-Widom-Hellerstein-style
termination + confluence analysis over a RuleSet's parameter read/write
footprint.

Full algorithm derivation: docs/260711_Autofix Loop.md, "(3) Proposed static
check" — read that section before touching this module. Summary:

* Build a read/write footprint per rule (:func:`extract_footprint`).
* An edge ``ri -> rj`` exists when ``Writes(ri) ∩ Reads(rj) != ∅`` (ri's fix
  can change a parameter rj's condition tests) — the SAME edge set doubles as
  both the triggering graph ``G_T`` (termination) and the write→read DAG
  ``G_D`` (canonical fix order): the research doc's Step A/Step D pseudocode
  defines them identically.
* Acyclic → the loop provably terminates AND a topological order is the
  canonical (order-independent) fix sequence. Cyclic → a termination-risk
  ERROR (unless the only cycle is a harmless idempotent self-loop).
* Two rules writing the SAME (category, parameter) slot → a write-write
  ERROR (they race to set one slot, possibly to different values).
* A one-directional write→read edge between two co-enabled rules → a
  critical-pair WARNING — order-dependent but resolvable (the topo order
  resolves it); not raised when the graph is already cyclic (that pair is
  reported as the stronger ERROR instead).

PURE — no I/O, no model instance, no evaluation of any rule. Caller loads the
YAML (``RuleSet``) and, for a rule with ``lookup`` set, pre-resolves the
lookup table's key params into ``extra_reads`` (this module never touches the
filesystem — see ``lint()``'s docstring).

Fail-closed: any rule whose read/write footprint can't be statically
determined (an LLM-proposed value, a GeometryRule, or any future construct
this module doesn't recognise) is marked ``analyzable=False`` and excluded
from the graph — surfaced as a warning, never silently ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Any

from bim_orchestrator.policies.rules_schema import (
    GeometryRule,
    Rule,
    RuleSet,
    fetch_name,
)

# Mirrors qc.py's _TEMPLATE_TOKEN — compose_template's ``{token}`` syntax.
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]+)\}")

# Autofill strategies whose fix is IDEMPOTENT — re-applying to already-
# canonical input reproduces the same output, so a self-loop (the rule reads
# and writes the same slot) is harmless, not a termination-risk error.
# `normalize`'s sub-kinds (map/template/reference/fire_rating/...) are all
# still `autofill.strategy == "normalize"` at this level.
_IDEMPOTENT_STRATEGIES = frozenset({"normalize", "compose_template"})

# Inherit strategies are exempt from the SELF-loop error for a different,
# proof-backed reason (2026-07-12 analysis — AWH "discharge the SCC with a
# well-founded measure", docs/260711_Autofix Loop.md Step B):
#   CONDITION-ELIMINATING: since K9 + the v1.5-R2 M3 fullmatch guard, every
#   suggested value is validated through the SAME rules_engine requirement
#   that flagged it BEFORE being proposed — so a landed fix makes its own
#   rule's condition FALSE on that element, and the self-edge fires at most
#   ONCE per element (measure: #elements with an open finding for this rule,
#   strictly decreasing). The second application of inherit_then_normalize
#   (value present, non-canonical) reduces to plain `normalize`, which is
#   idempotent by the K13 round-trip invariant.
# NOTE this exemption covers ONLY the self-edge. The real inherit hazard —
# "the HOST's value must be final first" — is a CROSS-rule edge (a wall rule
# writing the param this rule reads via `host.<p>`), modeled separately via
# the wildcard ParamRef, and it STAYS flagged as a critical pair / ordering
# edge. Exempting the self-loop does not hide it.
_CONDITION_ELIMINATING_STRATEGIES = frozenset(
    {"inherit_from_host", "inherit_then_normalize"}
)
_SELF_LOOP_SAFE_STRATEGIES = _IDEMPOTENT_STRATEGIES | _CONDITION_ELIMINATING_STRATEGIES

# B-1 (review round 7): the same discharge argument, for the WRITE VALUE
# strategies that bypass the autofill pipeline (their footprint write landed
# in extract_footprint with this fix — without a discharge every such rule
# would self-cycle):
#   * next_available — condition-eliminating: the landed renumber makes THIS
#     element's value unique, so its own condition goes false; the replacement
#     is chosen unused, so it cannot mint a NEW duplicate elsewhere. Measure
#     (#elements with an open finding) strictly decreases.
#   * fixed — idempotent: the write is a constant, so a second application
#     changes nothing (fixpoint after one write). If the constant doesn't
#     satisfy the requirement the FINDING persists, but the value stops
#     moving and fingerprint convergence halts the loop — termination holds.
_SELF_LOOP_SAFE_VALUE_STRATEGIES = frozenset({"fixed", "next_available"})


@dataclass(frozen=True)
class ParamRef:
    """A (category, parameter) slot.

    ``category=None`` is a WILDCARD — it coincides with every concrete
    category. Used for cross-element reads (``host.<p>`` — the value lives on
    a DIFFERENT element, whose category this rule doesn't pin down) and for a
    rule with no resolvable category of its own.
    """

    category: str | None
    param: str


@dataclass
class Footprint:
    rule_id: str
    reads: set[ParamRef] = field(default_factory=set)
    writes: set[ParamRef] = field(default_factory=set)
    analyzable: bool = True


@dataclass
class LintReport:
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    order: list[str] | None
    graph: dict[str, list[str]]


def _rule_category(rule: Rule, ruleset: RuleSet) -> str | None:
    if rule.category:
        return rule.category
    tc = ruleset.target_category
    return tc if isinstance(tc, str) else None


def _host_aware_ref(name: str, category: str | None) -> ParamRef:
    """A ``host.<p>`` name always reads a DIFFERENT (unknown-category)
    element — wildcard category, cross-element."""
    if name.startswith("host."):
        return ParamRef(None, name.removeprefix("host."))
    return ParamRef(category, name)


def extract_footprint(rule: Rule | GeometryRule, ruleset: RuleSet) -> Footprint:
    """Statically extract one rule's read/write parameter footprint.

    Mirrors the REAL read/write semantics of ``rules_engine`` /
    ``qc.py:_suggest`` / ``design.py:_effective_remediation`` / ``_partition``
    — grep each before changing this function; a construct added there and
    not mirrored here silently becomes "unanalyzable" (fail-closed, but still
    a lint blind spot worth fixing).
    """
    if not isinstance(rule, Rule):
        # GeometryRule (3D geometry) — outside this static parameter
        # footprint's domain.
        return Footprint(rule_id=rule.id, analyzable=False)

    if rule.remediation.new_value_strategy == "llm_propose":
        # An LLM originates the value — not a deterministic, statically
        # analyzable footprint.
        return Footprint(rule_id=rule.id, analyzable=False)

    cat = _rule_category(rule, ruleset)
    own_param = fetch_name(rule)
    reads: set[ParamRef] = {ParamRef(cat, own_param)}
    writes: set[ParamRef] = set()

    if rule.scope_filter is not None:
        reads.add(ParamRef(cat, rule.scope_filter.param))

    if rule.other_param:
        reads.add(_host_aware_ref(rule.other_param, cat))

    strategy = rule.autofill.strategy
    if strategy in ("inherit_from_host", "inherit_then_normalize"):
        host_param = rule.autofill.host_param or own_param
        reads.add(ParamRef(None, host_param))
    elif strategy == "compose_template" and rule.autofill.template:
        for token in _TEMPLATE_TOKEN.findall(rule.autofill.template):
            if token == "seq":
                continue
            reads.add(_host_aware_ref(token, cat))
    elif strategy == "infer_from_room_name":
        # This derives its value from the element's NAME (qc._suggest reads
        # `element["name"]`), not from the rule's own parameter — and a rename
        # rule WRITES that same slot below as `__name__`. Without this read the
        # pair "rule A renames the element / rule B infers from its name" is a
        # genuine write→read edge that the lint cannot see, and it would report
        # the rule set clean.
        reads.add(ParamRef(cat, "__name__"))

    # WRITES only when the rule has an actual fix path: DesignAgent only ever
    # commits a Path B write for fixability=="auto" (design.py:_partition);
    # a fixability=="manual" rule's autofill (if any) only fills
    # Finding.suggested_value for DISPLAY in an ACC issue — it never mutates
    # the model, so it must not appear as a WRITE in this footprint.
    #
    # B-1 (review round 7, 2026-08-16): the written value's ORIGIN dispatches
    # on `remediation.new_value_strategy` (design._compute_new_value), not on
    # `autofill.strategy` — only "inferred" routes through the autofill
    # pipeline; "fixed" writes the literal `new_value` and "next_available"
    # computes a renumber, both with `autofill.strategy: none`. Keying has_fix
    # on the autofill alone made a `next_available` rule's footprint show ZERO
    # writes while staying analyzable=True: two rules renumbering Rooms.Number
    # linted "clean" — a blind spot on the exact surface (write-write
    # conflict) this lint exists to catch. Same L2-14 lesson as DesignAgent's
    # autonomy gate, one review later, on the read side.
    value_strategy = rule.remediation.new_value_strategy
    if value_strategy not in ("inferred", "fixed", "next_available"):
        # llm_propose is handled above; anything ELSE is a strategy this
        # mirror doesn't know → fail closed (v1.5-R7 convention), never a
        # silently-empty footprint.
        return Footprint(rule_id=rule.id, analyzable=False)
    has_fix = rule.fixability == "auto" and (
        rule.remediation.action == "rename_element"
        or strategy != "none"
        or value_strategy in ("fixed", "next_available")
    )
    if has_fix:
        if rule.remediation.action == "rename_element":
            # Pseudo-param: two rules renaming the same scope is a
            # write-write conflict just like any real parameter.
            writes.add(ParamRef(cat, "__name__"))
        else:
            target_param = rule.remediation.target_parameter or own_param
            writes.add(ParamRef(cat, target_param))

    return Footprint(rule_id=rule.id, reads=reads, writes=writes, analyzable=True)


def _refs_overlap(a: ParamRef, b: ParamRef) -> bool:
    if a.param != b.param:
        return False
    return a.category is None or b.category is None or a.category == b.category


def _shared_params(a: set[ParamRef], b: set[ParamRef]) -> set[str]:
    return {x.param for x in a for y in b if _refs_overlap(x, y)}


def lint(
    ruleset: RuleSet, extra_reads: dict[str, set[ParamRef]] | None = None
) -> LintReport:
    """Static termination + confluence check over ``ruleset``.

    ``extra_reads`` — pre-resolved extra reads per rule id, keyed by
    ``rule.id``. Used for a ``rule.lookup`` rule's lookup-table key params
    (``config/lookup.<name>.yaml``) — loading that file is I/O, so this
    module never does it itself; the CALLER loads the table and passes the
    resolved :class:`ParamRef` set here. Omit (None) to skip lookup-key reads
    entirely (the rule stays analyzable; the lint is just slightly less
    complete for that one rule).
    """
    rules_by_id: dict[str, Rule] = {r.id: r for r in ruleset.rules}
    footprints: dict[str, Footprint] = {}
    for r in ruleset.rules:
        fp = extract_footprint(r, ruleset)
        if extra_reads and r.id in extra_reads:
            fp.reads = fp.reads | set(extra_reads[r.id])
        footprints[r.id] = fp
    for g in ruleset.geometry_rules:
        footprints[g.id] = extract_footprint(g, ruleset)

    analyzable_ids = [rid for rid, fp in footprints.items() if fp.analyzable]

    # The SAME edge set is both the triggering graph G_T (termination) and
    # the write→read DAG G_D (canonical order) — see module docstring.
    edges: dict[tuple[str, str], set[str]] = {}
    for ri in analyzable_ids:
        for rj in analyzable_ids:
            shared = _shared_params(footprints[ri].writes, footprints[rj].reads)
            if shared:
                edges[(ri, rj)] = shared

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for rid, fp in footprints.items():
        if not fp.analyzable:
            warnings.append({"type": "unanalyzable", "rule": rid})

    # Write-write conflicts: two DIFFERENT rules writing the same slot.
    for i, ri in enumerate(analyzable_ids):
        for rj in analyzable_ids[i + 1 :]:
            shared_w = _shared_params(footprints[ri].writes, footprints[rj].writes)
            if shared_w:
                errors.append({
                    "type": "write_write_conflict",
                    "rules": sorted([ri, rj]),
                    "params": sorted(shared_w),
                })

    # Termination + canonical order over G_T == G_D. Idempotent self-loops
    # (Step B's discharge condition) are excluded from the graph entirely —
    # they're provably harmless, not merely undetected.
    ts: TopologicalSorter[str] = TopologicalSorter()
    for rid in analyzable_ids:
        ts.add(rid)
    for (ri, rj) in edges:
        if ri == rj:
            rule_i = rules_by_id.get(ri)
            strategy = getattr(getattr(rule_i, "autofill", None), "strategy", None)
            value_strategy = getattr(
                getattr(rule_i, "remediation", None), "new_value_strategy", None
            )
            if strategy in _SELF_LOOP_SAFE_STRATEGIES:
                continue
            # B-1: when the write's value comes from the remediation strategy
            # (autofill "none"), the discharge argument is the VALUE
            # strategy's — see _SELF_LOOP_SAFE_VALUE_STRATEGIES.
            if (
                strategy in (None, "none")
                and value_strategy in _SELF_LOOP_SAFE_VALUE_STRATEGIES
            ):
                continue
            ts.add(ri, ri)
            continue
        ts.add(rj, ri)  # rj depends on ri: ri writes what rj reads

    order: list[str] | None
    try:
        order = list(ts.static_order())
    except CycleError as exc:
        order = None
        cyclic = list(exc.args[1]) if len(exc.args) > 1 else []
        errors.append({"type": "cycle", "rules": cyclic})

    # Critical-pair warnings: one-directional write→read edges. Only emitted
    # when the graph is acyclic overall — a pair already inside a detected
    # cycle is reported as the stronger ERROR above, not double-counted here.
    if order is not None:
        for (ri, rj), shared in edges.items():
            if ri == rj:
                continue
            warnings.append({
                "type": "critical_pair",
                "rules": [ri, rj],
                "params": sorted(shared),
            })

    graph = {f"{ri}->{rj}": sorted(shared) for (ri, rj), shared in edges.items()}

    return LintReport(errors=errors, warnings=warnings, order=order, graph=graph)


__all__ = ["Footprint", "LintReport", "ParamRef", "extract_footprint", "lint"]
