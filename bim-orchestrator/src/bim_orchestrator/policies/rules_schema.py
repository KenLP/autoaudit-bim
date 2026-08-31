"""Rule + RuleSet schema (extracted from agents/qc.py in v1.3).

This module owns the pydantic models that describe a compliance rule set.
It used to live in ``agents/qc.py`` for historical reasons, but ``policies/``
modules (rules_engine, autonomy, query_specs) need to reference these
types — and ``policies/`` is the lower architectural layer, so it cannot
import from ``agents/``. Moving the schema down here fixes that and
matches the layering convention already used for ``AutonomyPolicy``.

Back-compat: ``agents/qc.py`` re-exports every name defined here so
existing imports (``from bim_orchestrator.agents.qc import Rule, RuleSet,
CitationPolicy, ...``) keep working unchanged.

Anything here is **pure schema** — no I/O, no evaluation. Loading from
YAML still lives in ``QCAgent.__init__`` since it pairs naturally with
the autonomy policy load that follows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Literal type aliases
# ---------------------------------------------------------------------------

Requirement = Literal[
    "present_and_nonempty",
    "positive_number",
    "matches_regex",
    "matches_regex_if_present",
    "not_matches_regex",
    "numeric_min",
    "numeric_min_conditional",
    "unique_in_set",
    "fire_rating_ge",
    # v1.4-K10 consolidation: one numeric check with an explicit operator —
    # subsumes positive_number (> 0) and numeric_min (>= threshold). Pair with
    # `Rule.operator` + `Rule.threshold`.
    "numeric_compare",
    # v1.4-K10: generalised cross-element comparison — `this.<parameter>`
    # <operator> `related.<other_param>`. Subsumes fire_rating_ge (which is just
    # `compare_kind="fire_rating"`, `operator=">="`). Pair with `Rule.operator`,
    # `Rule.other_param`, `Rule.compare_kind`.
    "relation_compare",
    # Phase 2 GĐ2: value must be EXACTLY one of an allowed set (the subset is
    # resolved by the caller — e.g. the valid classification codes for an
    # element's category). Turns a slice of "meaning" (is this the right
    # Uniclass object?) into a deterministic relationship check, so the
    # remediation closed loop can reject a well-formed-but-wrong code by machine.
    # Pair with a subset source (policies/classification.ClassificationCatalog).
    "value_in_subset",
    # v1.4-K12: canonical-format check whose CHECK and FIX derive from ONE
    # declaration (the `normalize` autofill). Compliant iff the value already
    # equals its canonical form; the fix is that same canonical form → the two
    # can never drift (no separate pattern to keep in sync). Pair with
    # `autofill.strategy=normalize` (+ normalize_kind / normalize_format).
    "canonical_format",
]
# v1.4-K10: numeric/relational comparison operators for numeric_compare +
# relation_compare. ">=" keeps the legacy numeric_min/fire_rating_ge semantics.
ComparisonOperator = Literal[">", ">=", "<", "<=", "==", "!="]
# v1.4-K10: how relation_compare compares the two values. "fire_rating" parses
# HR/MIN to minutes (the old fire_rating_ge path); "numeric" casts to float;
# "string" compares trimmed strings (only "==" / "!=" meaningful).
CompareKind = Literal["numeric", "fire_rating", "string"]
# v1.4-K10: a plain severity LEVEL the user sets by importance — decoupled from
# the requirement "kind". When set on a Rule it wins over the severity_tag→level
# mapping in autonomy.yaml; the tag is kept only as a category label for reports.
SeverityLevel = Literal["severity_low", "severity_medium", "severity_high"]
AutofillStrategy = Literal[
    "infer_from_adjacent", "infer_from_room_name", "compose_template",
    "normalize", "inherit_from_host", "inherit_then_normalize", "none",
]
CitationMode = Literal["hard", "soft"]
OnMissingCitation = Literal["warn", "downgrade"]
Fixability = Literal["manual", "auto"]
RemediationAction = Literal["create_acc_issue", "set_parameter", "rename_element"]
# v1.4-K11 + Phase 2: "llm_propose" lets a RemediationLLMAgent originate the
# value for findings the deterministic strategies can't fix (open-vocabulary
# inputs — e.g. legacy family name -> naming convention). The LLM proposes; the
# SAME rule that flagged the finding re-validates it (closed loop); a value an
# LLM originates is NEVER auto-applied (autonomy capped in DesignAgent). Phase 1
# is unaffected: no rule ships with this strategy and DesignAgent only honours it
# when an LLM agent is injected — otherwise it falls back to a Path A ACC issue.
NewValueStrategy = Literal["fixed", "inferred", "next_available", "llm_propose"]

# v1.4: conceptual rule grouping. Layer ABOVE Requirement — orthogonal
# to mechanical dispatch. Used by ExtractionAgent to derive sensible
# defaults for fixability/remediation and by reports for grouping.
# Adding values here is non-breaking (optional field on Rule).
RuleType = Literal[
    "parameter_completeness",     # must exist + non-empty (most common)
    "value_constraint",           # match regex / >= threshold / in allowed set
    "naming_convention",          # special-case value_constraint for identifier patterns
    "uniqueness_constraint",      # value unique across siblings
    "cross_element_relationship", # depends on another element (host, adjacent, ...)
]

# v1.4: extraction-time outcome per rule. Tells the downstream pipeline
# whether the rule is ready to run, needs catalog work, or should land
# in the human review queue instead of the executable rules YAML.
ExecutionStatus = Literal[
    "executable",                 # green light — runs in rules_engine
    "needs_domain_mapping",       # OSTCatalog could not resolve the category
    "not_model_checkable",        # ambiguous / process / requires custom checker
]

# v1.4-J: geometry check types — captured for v2 geometric evaluator.
# Not executed by the current rules_engine; stored with execution_status
# "not_model_checkable" to surface in the human-review queue.
GeometryCheckType = Literal[
    "clearance_min",        # distance from element face to reference >= threshold
    "clearance_max",        # nearest MEASURED reference must be <= threshold away
                            #   (max gap allowed). Vertical raycast only
                            #   (below/above — horizontal fails closed: bbox mode
                            #   reports no distance); elements with no reference
                            #   hit inside the probe window are not evaluated —
                            #   see geometry_query._run_max_rule (H-01) and
                            #   a gap report forwarded to the add-in team.
    "spatial_containment",  # element must be inside a named spatial container
    "min_spacing",          # distance to nearest sibling element >= threshold
]
ClearanceDirection = Literal["below", "above", "horizontal"]
GeometryReferenceSource = Literal[
    "same_model",    # reference elements are in the same Revit file
    "linked_arch",   # reference in linked architectural model
    "linked_struct", # reference in linked structural model
    "linked_mep",    # reference in linked MEP model
]


# ---------------------------------------------------------------------------
# Component models
# ---------------------------------------------------------------------------


# Every contract object below is STRICT. Pydantic's default would silently
# ignore an unknown key and apply the field default instead, so a typo like
# `requires_humann: true` quietly becomes `requires_human=False`, and a misspelt
# `scope_filter` quietly widens a rule to every element — configuration
# corruption that a green validation run cannot see. Strict is already the house
# style for every other config contract in policies/ (ost_catalog,
# param_catalog, lookup_table, reference, shared_params); rules were the gap.
# `RuleSet.metadata` stays a declared free-form dict, so provenance bags are
# unaffected.
_STRICT = ConfigDict(extra="forbid")


class GeometryRuleSpatialFilter(BaseModel):
    """Restricts which elements are checked to those inside a spatial container."""
    model_config = _STRICT

    category: str | None = None       # e.g. "Spaces"
    name_contains: str | None = None  # e.g. "Parking"
    name_exact: str | None = None


class GeometryRule(BaseModel):
    """Geometry-based check — captured for v2, not executed by current rules_engine.

    Stores requirements that depend on 3D geometry, spatial containment, or
    linked-file traversal. Always written with ``execution_status =
    "not_model_checkable"`` so the Rule Builder can persist them losslessly
    while the geometric evaluator registry (Tier 3 roadmap) is built.
    """
    model_config = _STRICT

    id: str
    category: str
    check_type: GeometryCheckType
    description: str
    threshold_mm: float | None = None
    clearance_direction: ClearanceDirection | None = None
    reference_category: str | None = None
    reference_source: GeometryReferenceSource = "same_model"
    # Optional substring naming the specific linked file to query, matched
    # (case-insensitively) against the loaded link names. Takes priority over
    # the discipline keywords derived from ``reference_source`` — needed when a
    # model loads several links of one discipline (e.g. "...HVAC",
    # "...Plumbing", "...Electrical" all map to linked_mep) so the author can
    # point a rule at exactly one ("HVAC"). Ignored for same_model.
    reference_link_hint: str | None = None
    spatial_filter: GeometryRuleSpatialFilter | None = None
    severity_tag: str = "geometric_violation"
    execution_status: ExecutionStatus = "not_model_checkable"
    # Optional Revit 3D view element ID to scope the raycast. Required for
    # axis=Z clearance checks — without it the addin uses the active view,
    # which may not be a 3D view and will return 0 results.
    view_id: int | None = None
    notes: list[str] | None = None


class RuleAutofill(BaseModel):
    model_config = _STRICT
    strategy: AutofillStrategy
    fallback: Any | None = None
    # v1.4-K3 — used by strategy="compose_template": build the suggested value
    # from a token template referencing element params, e.g.
    #   "{_containing_space}-{Reference Level}-{System Name}-{seq}".
    # ``{seq}`` is a per-group sequence (01, 02, …) assigned by QCAgent over the
    # elements grouped by ``sequence_scope``. If any referenced param is absent,
    # the suggested value is None → the finding routes to a Path A ACC Issue.
    template: str | None = None
    sequence_scope: list[str] | None = None
    # v1.4-K5 — used by strategy="normalize": which canonicaliser to apply to
    # the current (non-canonical) value, e.g. "fire_rating" ("2 HR" → "2-hour").
    normalize_kind: str | None = None
    # v1.4-K11 — output FORMAT template for a unit-bearing normalizer, so the
    # canonical form isn't hard-wired. The normalizer parses the input to a
    # magnitude, then renders this template. For fire_rating the tokens are
    # ``{h}`` (hours, trimmed) and ``{m}`` (minutes): "{h}-hour"→"3-hour",
    # "{h} HR"→"3 HR", "{m} MIN"→"180 MIN". Defaults to the dimension's canonical
    # when unset. v1.4-K13: normalize_kind generalised to any unit DIMENSION
    # (duration/length/area) — the format token picks the output unit
    # ({mm}/{cm}/{m}, {m2}, …), not just fire-rating.
    normalize_format: str | None = None
    # v1.4-K13 — used by normalize_kind="map": {accepted-variant -> canonical}
    # for FIXED/enumerated text (e.g. {"nr": "Not Rated"}). Lookup is case- and
    # whitespace-insensitive; a value already equal to a canonical target maps
    # to itself; a miss → None → Path A. Generalises "canonical is a fixed string".
    normalize_map: dict[str, str] | None = None
    # v1.4-K15 — used by normalize_kind="template": a regex with capture groups
    # that PARSES the current value; normalize_format then renders the canonical
    # form from those groups (e.g. source captures fn/d1 → "ADSK_Fur_{fn}_{d1}").
    # The general deterministic naming transform — restructure a name that
    # already contains the tokens. No match → None → Path A.
    normalize_source: str | None = None
    # v1.4-K21 — used by normalize_kind="reference": the NAME of a reference set
    # (``config/reference.<name>.yaml``) whose ``entries[].canonical`` are the
    # only allowed values. QC checks membership and snaps off-form-but-recognised
    # values (alias / separator / case) to the canonical entry (tiers 1–2); a
    # value not deterministically in the set → None → Path A. Fuzzy/semantic
    # matching is Phase 2. Pair with ``requirement: canonical_format``.
    normalize_reference: str | None = None
    # v1.4-K20 — used by strategy="inherit_from_host": which HOST parameter to
    # copy down when the element's own value is missing (e.g. a door's empty
    # Fire Rating inherits the host wall's Fire Rating). Defaults to the rule's
    # OWN parameter (same-named inheritance) when unset. The query layer fetches
    # it via the host hop and surfaces it as ``host.<name>``; an absent/blank
    # host value yields None → the finding routes to Path A (never invented).
    host_param: str | None = None


class RuleRemediation(BaseModel):
    """How DesignAgent should act on a finding produced by this rule.

    * ``action=create_acc_issue`` (default): the Phase 1 path — DesignAgent
      builds an ACC Issue via Forma MCP. Used for ``fixability=manual``
      cases where a human (or another model) must judge the fix.
    * ``action=set_parameter``: write a single parameter via Revit MCP.
      Pair with ``target_parameter`` (defaults to the rule's parameter) and
      a ``new_value_strategy``. ``inferred`` reuses the existing autofill
      pipeline; ``fixed`` writes ``new_value`` verbatim; ``next_available``
      lets DesignAgent compute a unique replacement (e.g. for duplicate
      room numbers).
    * ``action=rename_element``: write the element's Name parameter.

    ``comments_template`` — when set, DesignAgent will also tag the room's
    Comments parameter so the audit trail is visible in Revit. Supports
    ``{value}`` / ``{old_value}`` / ``{new_value}`` / ``{rule_id}``
    placeholders.

    ``llm_safety_critical`` — Phase 2 governance second axis (design doc §4):
    when ``new_value_strategy="llm_propose"`` and this is True, the proposal is
    forced ``human-only`` (life-safety params: fire rating, egress widths). The
    default (False) caps an LLM proposal at ``approve``. Either way an
    LLM-originated value is NEVER ``auto`` — only deterministic strategies earn
    that. Ignored unless the strategy is ``llm_propose``.
    """
    model_config = _STRICT

    action: RemediationAction = "create_acc_issue"
    target_parameter: str | None = None
    new_value: Any | None = None
    new_value_strategy: NewValueStrategy = "inferred"
    comments_template: str | None = None
    # v1.4-K5 — write target for set_parameter. "instance" (default) writes the
    # element itself; "type" writes the element's family type (via the
    # `_type_id` breadcrumb) — needed for type-level params like Fire Rating.
    # "family" resolves the FAMILY element id (by its name via list_families) —
    # needed to rename the *Family Name* (a read-only `Element.Name`, NOT a
    # settable Parameter); type rename only touches the Type Name (v1.4-K17).
    # DesignAgent dedups Path B writes by (resolved target, parameter).
    #
    # v1.4-K19 — "auto" lets the author skip the write-target decision; the
    # DesignAgent resolves it per element from the parameter being checked
    # (Family Name → rename family, Type Name → rename type, a Type-carried
    # param like Fire Rating → type, otherwise → instance). The action field is
    # ALSO resolved in the auto case (set_parameter vs rename_element), so a
    # rule may store action=set_parameter with target=auto and still rename.
    # A non-auto target/action pair is honoured verbatim (explicit override).
    # The schema default stays "instance" so existing YAML rules that relied on
    # the implicit default are unchanged; the Rule Builder emits "auto" for NEW
    # rules. See ``DesignAgent._effective_remediation``.
    target: Literal["instance", "type", "family", "auto"] = "instance"
    # Phase 2 — governance flag for new_value_strategy="llm_propose" (see above).
    llm_safety_critical: bool = False


class CitationPolicy(BaseModel):
    """Per-rule citation enforcement policy (Phase 2 Day 4).

    * `mode=soft` (default): citation is bonus metadata. Backward compatible —
      rules YAML without a `citation:` block falls into this branch.
    * `mode=hard`: every finding produced by this rule MUST cite a source.
      Missing citations are flagged via `citation_missing` (warn) or by
      lowering severity by one level (downgrade).
    * `source_filter`: when set, only chunks whose metadata.source is in this
      list are eligible — prevents a BEP rule citing an IBC chunk by accident.
    """
    model_config = _STRICT

    mode: CitationMode = "soft"
    source_filter: list[str] | None = None
    on_missing: OnMissingCitation = "warn"


class ExtractionMeta(BaseModel):
    """v1.4 ExtractionAgent provenance attached to extracted rules.

    Optional — hand-authored YAML rules don't carry this. ``Rule`` instances
    produced by the v1.4 ExtractionAgent (or the D0 Claude Desktop skill
    pack workflow) populate it so the user can trace any finding back to
    the BEP clause that justified it.

    Fields:
      * ``confidence`` — LLM self-reported certainty (0.0–1.0). Acts as
        an automatic fixability gate: below 0.75 we force-bump to manual.
      * ``source_text`` — exact quote from the BEP for traceability.
      * ``source_location`` — human-readable pointer (``"BEP §1.7 page 12"``).
      * ``extracted_by`` — which LLM (or human) produced this rule.
      * ``extracted_at`` — ISO-8601 timestamp of extraction.
      * ``execution_status`` — see :data:`ExecutionStatus`. Only entries
        with ``executable`` actually load into the runtime RuleSet; the
        other two land in the extraction review queue with the reason
        captured in ``status_reason``.
      * ``notes`` — optional list of interpretation guidance from the
        source that doesn't fit in ``description`` (measurement
        methodology, definition clarifications, scope nuances). For
        example, IBC §1003.2 has a note explaining headroom is measured
        "from finished floor to underside of obstruction" — that goes
        here, not in the rule's description. The runtime QC engine
        currently ignores notes; they're informational for downstream
        custom checkers and human reviewers.
    """
    model_config = _STRICT

    confidence: float = Field(ge=0.0, le=1.0)
    source_text: str
    source_location: str = ""
    extracted_by: str = ""
    extracted_at: str = ""
    execution_status: ExecutionStatus = "executable"
    status_reason: str | None = None
    notes: list[str] | None = None


# ---------------------------------------------------------------------------
# Rule + RuleSet
# ---------------------------------------------------------------------------


class RuleScopeFilter(BaseModel):
    """v1.4-K10: universal 'applies to' filter for a parameter rule.

    The rule only evaluates an element when ``element.params[param]`` matches
    ``pattern`` (regex search). Out-of-scope elements quietly pass. This makes
    the conditional gate a first-class dimension of ANY check (Solibri's
    'applicable components') rather than a numeric_min-only variant.
    """
    model_config = _STRICT

    param: str
    pattern: str


class Rule(BaseModel):
    model_config = _STRICT
    id: str
    parameter: str
    requirement: Requirement
    pattern: str | None = None
    threshold: float | None = None
    # v1.4-D0.5: explicit unit for numeric thresholds. When set, the QC
    # engine converts the raw Revit param value (via the storage-unit
    # lookup in ``policies/revit_units.py``) before comparing to the
    # threshold. Examples: "m", "mm", "ft", "m²", "ft²". Leave None for
    # text / Yes-No / enum parameters where unit doesn't apply, or when
    # the value is already in the threshold's unit (e.g. legacy metric
    # mirrors like ``Unbounded Height (m)`` from RevitQueryAgent).
    unit: str | None = None
    when_param: str | None = None
    when_pattern: str | None = None
    # Phase 2 Week 7 D1: cross-param comparisons (fire_rating_ge).
    # When set, QCAgent reads ``element.params[other_param]`` and passes it
    # to the evaluator as ``other_value``.
    other_param: str | None = None
    # v1.4-K10: explicit operator for `numeric_compare` / `relation_compare`.
    # Defaults to ">=" so a numeric_compare with no operator behaves like the old
    # numeric_min, and a relation_compare like fire_rating_ge.
    operator: ComparisonOperator | None = None
    # v1.4-K10: how `relation_compare` compares the two values (numeric /
    # fire_rating / string). Defaults to numeric when unset.
    compare_kind: CompareKind | None = None
    # Phase 2 GĐ2: explicit allowed set for `value_in_subset`. When set, QC and
    # the remediation closed loop use THIS list directly (and IDS export emits it
    # as xs:enumeration). When None, the subset is resolved per-element from the
    # classification table by the element's category. Maps to / from IDS
    # xs:enumeration for round-trip.
    allowed_values: list[str] | None = None
    # Table-driven relational check (IBC §716 POC): when set on a relation_compare
    # rule, the related value (`other_param`, e.g. host.Fire Rating) is mapped
    # through the lookup table `config/lookup.<lookup>.yaml` to a REQUIRED value
    # before comparing. A host value not in the table → manual review (never
    # guessed). None = compare the related value directly (the original behaviour).
    lookup: str | None = None
    # v1.4-K10: UNIVERSAL scope filter — a rule only evaluates elements whose
    # `scope_filter.param` matches `scope_filter.pattern`; out-of-scope elements
    # quietly pass (like the old numeric_min_conditional, but for ANY requirement,
    # e.g. "only external doors", "only fire-rated walls").
    scope_filter: "RuleScopeFilter | None" = None
    # Phase 2 Week 7 D1: per-rule category filter. When set, restricts a rule
    # to elements matching this category — useful when a single rules file
    # targets multiple categories (e.g. walls + doors with different rules).
    category: str | None = None
    severity_tag: str
    # v1.4-K10: explicit severity LEVEL, decoupled from severity_tag. When set,
    # QC uses it directly; otherwise it falls back to the severity_tag→level
    # mapping in autonomy.yaml. The Rule Builder sets this from a Low/Med/High
    # picker so the user states *importance* once, separate from the check kind.
    severity_level: SeverityLevel | None = None
    description: str
    autofill: RuleAutofill
    citation: CitationPolicy = Field(default_factory=CitationPolicy)
    # Phase 2 Week 6: DesignAgent path B dispatch (Forma vs Revit)
    fixability: Fixability = "manual"
    remediation: RuleRemediation = Field(default_factory=RuleRemediation)
    # v1 task BB: when True, a failing evaluation routes the Finding to the
    # `manual_review_items` bucket rather than `findings` (which goes to ACC
    # issues). Use for rules that are deterministic but produce results a
    # human must judge (e.g. borderline tolerance, ambiguous classification).
    # Missing-data wins over this flag: if the parameter is absent, the
    # Finding goes to `missing_data_items` regardless of requires_human.
    requires_human: bool = False
    # v1.4: optional conceptual grouping. ExtractionAgent emits it for
    # default routing + report grouping; hand-authored YAML may omit.
    # See ``RuleType`` and the RULE_TYPE_DEFAULTS table consumed by the
    # JSON-to-YAML extraction script.
    rule_type: RuleType | None = None
    # v1.4: extraction provenance. Only present on rules emitted by the
    # ExtractionAgent / skill-pack workflow; pre-v1.4 YAML rules stay
    # without it (Pydantic default of None preserves back-compat).
    extraction_meta: ExtractionMeta | None = None
    # Binding layer: when a canonical rule's ``parameter`` name does not
    # match the actual Revit parameter in the target model, set this to
    # the real Revit param name. QCAgent and the query pipeline use
    # ``bound_parameter`` for fetch + unit-conversion; ``parameter``
    # is preserved as the canonical intent label for findings + reports.
    # Omit (None) when the canonical name already matches the model.
    bound_parameter: str | None = None


def fetch_name(rule: "Rule") -> str:
    """The actual Revit parameter name to read/write for ``rule``.

    ``bound_parameter`` (the real Revit param) wins when set; otherwise the
    canonical ``parameter`` intent label doubles as the Revit name. Lives here
    (pure schema layer) rather than in ``agents/qc.py`` or ``agents/design.py``
    because BOTH agents need it — M2 (2026-07): before this helper existed,
    several read/write sites (unique_in_set siblings, ``_suggest`` normalize,
    DesignAgent's write target / old_value / dedup key) read ``rule.parameter``
    directly while the main fetch used ``bound_parameter or parameter`` — a
    bound rule then silently never fired (false negative) or wrote/deduped
    against a param name that doesn't exist on the element. Every read/write
    site must resolve through this ONE function so the binding layer can't
    drift between call sites again.
    """
    return rule.bound_parameter or rule.parameter


class RuleSet(BaseModel):
    model_config = _STRICT
    scenario: str
    # Phase 2 Week 7 D1: target_category may be a single category (Phase 1
    # default) or a list — e.g. ``[Walls, Doors]`` for the fire-rating
    # scenario. Per-rule ``category`` further narrows the scope.
    target_category: str | list[str]
    rules: list[Rule] = Field(default_factory=list)
    # v1.4: free-form metadata bag for extraction provenance + workflow
    # prep notes. QC / Query agents IGNORE this field at runtime. Common
    # subkeys populated by the ExtractionAgent / Skill Pack workflow:
    #   * ``source`` — original PDF / spec / BEP filename + revision
    #   * ``extracted_by`` / ``extracted_at`` — same as per-rule meta,
    #     but at the scenario level
    #   * ``custom_parameters_required`` — list of shared/project
    #     parameters BIM team must add before running this ruleset.
    #     Each entry: ``{name, type, applies_to, note}``.
    #   * ``cross_references`` — list of referenced code sections that
    #     should be extracted as separate scenarios (e.g. IBC §1003.2
    #     Exception 4 → §1011.3 stair headroom).
    metadata: dict[str, Any] | None = None
    # v1.4-J: geometry rules — stored separately so parameter rules_engine
    # never sees them. QCAgent ignores this field; a future GeometricQCAgent
    # will consume it. Field is optional so existing YAML files keep loading.
    geometry_rules: list[GeometryRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> RuleSet:
        """``rule.id`` is a foreign key, so a collision is invalid input.

        The id travels QC → DesignAgent → report → ACC issue → approval record.
        Two rules sharing one id split the engine's brain: QC iterates the LIST
        and evaluates BOTH (each stamping its own severity), while DesignAgent
        looks the rule up in a ``{id: rule}`` DICT and therefore sees only the
        LAST — so a finding raised by rule A is remediated with rule B's
        fixability/remediation. That is an identity defect, not an authoring
        style warning, and no ``--rule`` filter, issue dedup or fingerprint
        grouping downstream can tell the two apart.

        Parameter and geometry rules share ONE namespace here: they land in the
        same reports and the same ``--rule`` selector, so a geometry rule may
        not reuse a parameter rule's id either.
        """
        seen: dict[str, str] = {}
        dupes: list[str] = []
        for kind, items in (("rule", self.rules), ("geometry_rule", self.geometry_rules)):
            for item in items:
                if item.id in seen:
                    dupes.append(f"{item.id!r} ({seen[item.id]} + {kind})")
                else:
                    seen[item.id] = kind
        if dupes:
            raise ValueError(
                "duplicate rule id(s) in scenario "
                f"{self.scenario!r}: {', '.join(dupes)}. "
                "Rule ids must be unique across rules + geometry_rules — they "
                "are the key QC, DesignAgent, reports and approvals join on."
            )
        return self


def merge_rulesets(rulesets: "Sequence[RuleSet]") -> RuleSet:
    """Merge several RuleSets into one (v1.4-K6 multi-scenario).

    Lets the user check parameter + geometry + naming scenarios in ONE run by
    combining their YAMLs. Pure (no I/O); the engine already handles a large
    RuleSet, so the merged result runs exactly like a hand-authored one.

    Semantics:
      * **Identity on a single input** — returns it unchanged, so the common
        one-file case is byte-for-byte the old behaviour (no merge overhead,
        original scenario name + metadata preserved).
      * ``rules`` / ``geometry_rules`` are concatenated and deduped by ``id``.
        A duplicate id whose content is IDENTICAL (the same rule shipped in two
        packs) collapses to one entry; a duplicate whose content DIFFERS raises
        ``ValueError``. Silent first-wins used to let the second file's rule
        vanish while its id kept resolving to the first file's remediation —
        the same identity split :meth:`RuleSet._unique_rule_ids` rejects within
        a file. Use :func:`duplicate_rule_ids` to list collisions up front.
      * ``target_category`` becomes the de-duplicated UNION across inputs (a
        list; a lone category stays a bare string). Per-rule ``category`` still
        narrows scope, so widening the union never broadens a rule's reach.
      * ``scenario`` is the ``+``-joined source scenarios; ``metadata`` records
        ``merged_from`` for traceability.

    Raises ``ValueError`` on an empty input (a run needs at least one ruleset).
    """
    sets = list(rulesets)
    if not sets:
        raise ValueError("merge_rulesets requires at least one RuleSet")
    if len(sets) == 1:
        return sets[0]

    rules: list[Rule] = []
    geometry_rules: list[GeometryRule] = []
    # id -> (scenario it came from, canonical content) so a collision can say
    # WHICH two packs disagree, not just which id repeated.
    seen: dict[str, tuple[str, Any]] = {}
    categories: list[str] = []
    seen_cat: set[str] = set()
    conflicts: list[str] = []

    for rs in sets:
        tc = rs.target_category
        for c in [tc] if isinstance(tc, str) else tc:
            if c not in seen_cat:
                seen_cat.add(c)
                categories.append(c)
        for item, bucket in (
            *[(r, rules) for r in rs.rules],
            *[(g, geometry_rules) for g in rs.geometry_rules],
        ):
            content = item.model_dump(mode="json")
            prev = seen.get(item.id)
            if prev is None:
                seen[item.id] = (rs.scenario, content)
                bucket.append(item)
            elif prev[1] != content:
                conflicts.append(
                    f"{item.id!r} differs between {prev[0]!r} and {rs.scenario!r}"
                )
            # identical duplicate → already present, drop silently

    if conflicts:
        raise ValueError(
            "cannot merge rulesets — same rule id, different definition: "
            + "; ".join(conflicts)
            + ". Two definitions cannot share one id: it is the key QC findings, "
            "ACC issues and approval records join on. If these packs are "
            "ALTERNATIVES (e.g. a deterministic vs an LLM variant of the same "
            "check), select just one; otherwise rename the id in one pack."
        )

    target_category: str | list[str] = (
        categories[0] if len(categories) == 1 else categories
    )
    return RuleSet(
        scenario="+".join(rs.scenario for rs in sets),
        target_category=target_category,
        rules=rules,
        metadata={"merged_from": [rs.scenario for rs in sets]},
        geometry_rules=geometry_rules,
    )


def duplicate_rule_ids(rulesets: "Sequence[RuleSet]") -> list[str]:
    """Rule ids that appear in more than one RuleSet (for a merge-time warning).

    Pure; reports collisions so a caller can log which ids were shadowed by
    :func:`merge_rulesets`' first-wins dedup. Considers both parameter and
    geometry rule ids (they share the same id namespace for ``--rule`` filters).
    """
    counts: dict[str, int] = {}
    for rs in rulesets:
        for rid in [r.id for r in rs.rules] + [g.id for g in rs.geometry_rules]:
            counts[rid] = counts.get(rid, 0) + 1
    return [rid for rid, n in counts.items() if n > 1]


__all__ = [
    "AutofillStrategy",
    "CitationMode",
    "CitationPolicy",
    "ClearanceDirection",
    "CompareKind",
    "ComparisonOperator",
    "duplicate_rule_ids",
    "ExecutionStatus",
    "ExtractionMeta",
    "Fixability",
    "GeometryCheckType",
    "GeometryReferenceSource",
    "GeometryRule",
    "GeometryRuleSpatialFilter",
    "merge_rulesets",
    "NewValueStrategy",
    "OnMissingCitation",
    "RemediationAction",
    "Requirement",
    "Rule",
    "RuleAutofill",
    "RuleRemediation",
    "RuleScopeFilter",
    "RuleSet",
    "RuleType",
    "SeverityLevel",
]
