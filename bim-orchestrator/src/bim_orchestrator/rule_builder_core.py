"""Rule Builder core logic (B16, Phase 3b M2-A).

Extracted from ``streamlit_app/app.py`` so the AutoAudit UI's M2 builder
endpoints (``service/routes_builder.py``) and the Streamlit Rule Builder tab
share the EXACT same NL-extraction prompt, grounding, validation and
enforcement logic — instead of two implementations drifting apart.

Hard rule (SPEC_3B_M2_RULE_BUILDER_NOW.md): this module MUST NOT import
``streamlit`` and MUST NOT do I/O beyond reading config through the
``policies/`` layer (OSTCatalog, param_catalog, shared_params, lookup_table) —
same posture as ``policies/`` itself, one layer up (this module is allowed to
call the LLM seam, which ``policies/`` is not).

``streamlit_app/app.py`` imports from here and keeps its old private names as
thin aliases (mechanical — see the module docstring there); this module is
the one source of truth going forward. Golden test:
``tests/test_rule_builder_core.py::test_golden_s1_canonical_format_reference``
pins that a representative canonical_format+reference rule saves to
byte-identical YAML before/after this extraction.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# rule_builder_core.py lives at src/bim_orchestrator/rule_builder_core.py:
# parents[0]=bim_orchestrator, [1]=src, [2]=<repo-root>.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_DIR = _REPO_ROOT / "config"

# ── NL extraction system prompt (verbatim from streamlit_app/app.py) ───────

RB_EXTRACT_SYSTEM = """\
You are a Revit BIM compliance rule extractor.
Given a requirement described in natural language (Vietnamese or English),
extract ONE compliance rule as a JSON object.

Output ONLY valid JSON with exactly these fields:
{
  "id": "category_slug.parameter_slug.check_slug",
  "category": "Revit category display name — one of: Rooms | Walls | Doors | Windows | Floors | Ceilings | Structural Columns | Structural Framing | Generic Models | Furniture | Ducts | Pipes | Cable Trays",
  "parameter": "canonical Revit parameter name (e.g. Mark, Fire Rating, Unbounded Height, Name, Number, Department, Family Name)",
  "requirement": "one of: present_and_nonempty | canonical_format | numeric_compare | matches_regex | not_matches_regex | unique_in_set | relation_compare",
  "operator": null or one of ">=|>|<=|<|==|!=" (only for numeric_compare / relation_compare),
  "threshold": null or number (only for numeric_compare, expressed in the unit below),
  "unit": null or "m" or "mm" or "ft" or "m²" or "ft²" (only when threshold is set),
  "pattern": null or regex string (only for matches_regex / not_matches_regex — NOT for canonical_format),
  "severity_level": "severity_low" | "severity_medium" | "severity_high",
  "fixability": "auto" or "manual",
  "autofill": null or {"strategy": "normalize", "normalize_kind": "...", "normalize_format": "...", "normalize_map": {...}},
  "remediation": null or {"action": "set_parameter", "target": "auto"},
  "scope_filter": null or {"param": "<other Revit param>", "pattern": "<regex the param must match>"},
  "lookup": null or "<lookup table name>" (only for a relation_compare whose required value comes from a code TABLE),
  "description": "concise English description of the check"
}

Decision rules:
- A value that must be in a STANDARD FORMAT / canonical form that the system can
  AUTO-FIX (e.g. "Fire Rating phải là 'X Min'", "X-hour", a length in mm) →
  requirement="canonical_format" (NO pattern), fixability="auto", and emit
  autofill+remediation. canonical_format means: compliant iff already canonical;
  the fix is that canonical form. Pick normalize_kind + normalize_format from the
  unit the user states (DERIVE the format from their literal, never default it):
    * LITERAL FORM IS LAW: when the NL quotes a target form (must read 'X HR',
      "recorded as 'X Min'"), normalize_format MUST reproduce that quoted literal
      exactly — same unit word, same separator, same spacing and case ('X HR' →
      "{h} HR", NEVER "{h}-hour"). Substituting a conventional form makes values
      that ALREADY match the demanded form non-compliant — a false positive on a
      compliant element, the worst failure direction. (If the NL instead says the
      value must "match the PATTERN 'X HR'", that wording selects the
      matches_regex + normalize_kind="auto" path below — the quoted form then
      defines the regex, not a canonical_format.)
    * time/fire-rating → normalize_kind="duration"; tokens {m}=minutes, {h}=hours.
      "X Min" (minutes) → normalize_format="{m} Min".  "X-hour" → "{h}-hour".  "X HR" → "{h} HR".
    * length → normalize_kind="length"; tokens {mm},{cm},{m}.  "X mm" → "{mm} mm".
    * area → normalize_kind="area"; token {m2}.  "X m²" → "{m2} m²".
    * a FIXED/enumerated text (e.g. blank/"NR"/"0" must read "Not Rated") →
      normalize_kind="map", normalize_map={"nr":"Not Rated","0":"Not Rated"} (NO format).
    * a value that must come from an AUTHORITATIVE LIST / approved catalog (approved
      materials, a classification code set, a valid type list — "Material ∈ approved
      palette", "phải thuộc bảng vật liệu được duyệt") → normalize_kind="reference",
      normalize_reference="<set name slug>" (NO format/map). The reference table itself
      is defined separately; off-list values become ACC issues.
    * a FORMAT rule that ALSO says "when empty / if missing, inherit from the
      host" (e.g. "Fire Rating must be 'X HR'; when empty, inherit the host
      wall's fire rating") → keep requirement="canonical_format" and set
      autofill={"strategy":"inherit_then_normalize","normalize_kind":...,
      "normalize_format":...} (kind/format derived exactly as above; add
      "host_param" only when the host parameter has a DIFFERENT name). This is
      ONE compound rule — empty value → inherit the host's → normalise it into
      the canonical form. Do NOT drop the inherit clause, do NOT downgrade to
      plain strategy="normalize", and do NOT split into two rules.
    * a name that only needs SEPARATORS fixed (spaces/hyphens → "_") →
      normalize_kind="family_name" (NO format).
    * a name that must be RESTRUCTURED into "PREFIX_{tokenA}_{tokenB}" from the
      current name (e.g. "ADSK_Fur_<Function>_<Desc>") → normalize_kind="template",
      normalize_source = a regex with named capture groups for each token, and
      normalize_format = the target template referencing them
      (source r"(?i)^adsk[ _-]*fur[ _-]*(?P<fn>[a-z]+)[ _-]*(?P<d1>[a-z0-9]+)",
       format "ADSK_Fur_{fn}_{d1}"). Use when tokens come FROM the current name.
      normalize_source must be IDEMPOTENT on the target form: it must ALSO match
      a name that is ALREADY canonical (mentally run the target template's own
      output through your source regex — if it does not match, the rule will
      flag correct names, which is broken). Three mechanical consequences:
        - separators between tokens are ALWAYS "[ _-]*" — never "\\s+"/"\\s*" —
          because the canonical form joins tokens with "_" while messy names
          use spaces;
        - the OPTIONAL prefix-strip group is separator-tolerant too:
          "(?:adsk[ _-]+)?", NEVER "(?:ADSK_)?" — the strict spelling strips
          only that exact form, so "ADSK Fur X" comes back DOUBLED as
          "ADSK_ADSK_Fur_X";
        - do NOT put the category word ("wall", "door") or the messy examples'
          prefix ("Basic Wall") in the source at all, and do NOT end with "$"
          after an optional suffix — match by FINDING the tokens (re.search),
          not by describing one messy shape end-to-end;
        - only anchor on a prefix literal when the canonical form shares that
          same prefix (like "ADSK" above); when they differ ("A_Wall" vs
          "Basic Wall"), skip the prefix entirely.
      Example: for target "A_Wall_{fn}_{th}_{fr}" use
      source r"(?i)(?P<fn>Ext|Int)[ _-]*(?P<th>\\d+)(?:mm)?[ _-]*(?P<fr>\\d+ ?HR|NR)"
      — it finds the tokens in "Wall Ext 200mm 2HR Copy" AND re-matches
      "A_Wall_Ext_200_2HR".
    * TRIGGER PHRASING for the template path above: "follow / match / conform
      to the naming convention/format <PREFIX_{tokenA}_{tokenB}...>" is ALWAYS
      this template case — NEVER requirement="matches_regex" for a naming-
      convention sentence that names a template with tokens, even though the
      sentence itself says "check"/"follow". Example: "Check that wall types
      follow the naming convention A_Wall_{Function}_{Thickness}_{FireRating}"
      → category="Walls", parameter="Type Name", requirement="canonical_format",
      autofill={"strategy":"normalize","normalize_kind":"template",
      "normalize_source":"<regex capturing Function/Thickness/FireRating tokens
      from the CURRENT name>","normalize_format":"A_Wall_{fn}_{th}_{fr}"}.
  remediation: DEFAULT to {"action":"set_parameter","target":"auto"} — the engine
  resolves the write target at run time from the parameter (Family Name → rename
  family, Type Name → rename type, a Type-carried param like Fire Rating → type,
  otherwise instance). Only pin an explicit target/action when the user clearly
  wants a specific one or the parameter is bound to BOTH instance and type:
  type-level PARAMETER → target="type"; **Family Name** rename →
  action="rename_element", target="family"; Type Name → action="rename_element",
  target="type"; an instance parameter → target="instance".
- A PATTERN-based format rule that CAN be auto-fixed by reshaping units/separators
  (e.g. Fire Rating must be "X HR", a length must be "X mm") → requirement=
  "matches_regex" with the pattern, fixability="auto", and autofill=
  {"strategy":"normalize","normalize_kind":"auto"} + remediation. With "auto" the
  engine tries every deterministic canonicaliser and keeps the one matching the
  pattern — you do NOT specify the unit/format. Prefer this over canonical_format
  when a precise pattern is natural. Two hard constraints on this path:
    * normalize_kind here is ALWAYS the literal string "auto" — never a concrete
      kind like "duration"/"length" (a concrete kind needs normalize_format,
      which a pattern rule does not carry; "auto" derives everything from the
      pattern itself).
    * the pattern must ACCEPT every value the requested conversion can produce:
      unit conversions yield DECIMALS ("90 min" → "1.5 HR"), so write
      "^\\d+(\\.\\d+)? HR$", not "^\\d+ HR$", even when the NL's examples are all
      integers — a pattern that rejects its own converted value defeats the fix
      it asked for (the engine drops any candidate the pattern rejects → no
      suggestion at all).
- A free-text PATTERN that is NOT a unit/separator canonicalisation (e.g. "Mark
  matches ABC-###") → requirement="matches_regex", give pattern; fixability "manual".
- "không trùng nhau" / uniqueness → requirement="unique_in_set", fixability="auto".
- presence / completeness only (any non-empty value ok) → requirement="present_and_nonempty".
- presence that INHERITS from the HOST when empty (e.g. "Door must have Fire Rating;
  if not set, take the host Wall's Fire Rating") → requirement="present_and_nonempty",
  fixability="auto", autofill={"strategy":"inherit_from_host","host_param":"<the host
  parameter>"} (omit host_param to inherit the SAME-named parameter), and
  remediation={"action":"set_parameter","target":"auto"}. The engine fetches the
  host's value and writes it down; if the host has no value the finding becomes an
  ACC issue. Use this whenever the user says "inherit / lấy theo / kế thừa từ host".
  ⚠️ ONLY when NO target form is quoted anywhere in the sentence. "Must have"
  does NOT decide the requirement — a quoted form does. CONTRAST:
    * "Doors must have a fire rating — inherit from the host wall when empty"
      → present_and_nonempty + inherit_from_host (no form quoted).
    * "Doors must have a fire rating, written as 'X HR' — inherit it from the
      host wall when empty" → requirement="canonical_format" +
      autofill={"strategy":"inherit_then_normalize","normalize_kind":"duration",
      "normalize_format":"{h} HR","host_param":"Fire Rating"} — the quoted
      'X HR' makes it a FORMAT rule (see the canonical_format section above),
      even though the sentence opens with "must have".
- numeric threshold/comparison → requirement="numeric_compare", set operator+threshold(+unit).
- one element's value vs a RELATED element's → requirement="relation_compare". Set:
  * other_param = the related element's parameter, PREFIXED by its source — use
    "host.<param>" for the hosting element (a door/window's host wall, an opening's
    host) e.g. "host.Fire Rating"; for a containing room/MEP space use the space's
    param name (e.g. "Number", "Name") as the model exposes it.
  * compare_kind = CHOOSE by the parameter's NATURE (NEVER default to fire_rating):
    - "fire_rating" ONLY when comparing FIRE RATINGS (parses "2 HR"/"90 min" to
      minutes before comparing). Using it on non-ratings BREAKS the check.
    - "string" for TEXT identity (operator "==" / "!="): names, codes, identifiers —
      room Name / room Number, MEP space Name / Number, type names, system names.
    - "numeric" (DEFAULT) for any numeric quantity (widths, heights, counts, areas).
  * operator: >=,>,<=,< for numeric & fire_rating; "==" or "!=" for string.
  Examples — door FireRating ≥ host wall: other_param="host.Fire Rating",
  compare_kind="fire_rating", operator=">=". Element's Number must match its
  containing space Number: other_param="Number", compare_kind="string", operator="==".
  * If the phrasing ALSO says the element must "carry / inherit / take on (at
    least) the host's X" (not merely be COMPARED to it) — i.e. a non-compliant
    element should be AUTO-CORRECTED by copying the host's value — ALSO add
    autofill={"strategy":"inherit_from_host","host_param":"<param>"} and
    remediation={"action":"set_parameter","target":"auto"}, fixability="auto".
    relation_compare defines what "compliant" means; the inherit_from_host
    autofill is INDEPENDENT and fixes non-compliant elements by copying the
    host's value — do NOT drop one for the other. Example: "Doors in rated
    walls must carry at least the host wall's fire rating" → category="Doors",
    parameter="Fire Rating", requirement="relation_compare", operator=">=",
    other_param="host.Fire Rating", compare_kind="fire_rating",
    autofill={"strategy":"inherit_from_host","host_param":"Fire Rating"},
    remediation={"action":"set_parameter","target":"auto"}, fixability="auto".
- A value required by a CODE TABLE / lookup (the requirement is "≥ the value the code
  table gives for this element's situation" — e.g. IBC §716 door rating by the host
  wall's use × rating, occupant-load factor by occupancy, fixture count by occupancy)
  AND a matching lookup table is listed under AVAILABLE LOOKUP TABLES below →
  requirement="relation_compare", set lookup="<that table name>", operator usually
  ">=", and OMIT other_param (the table self-declares its key params). Do NOT
  transcribe the table or invent thresholds. If NO matching table is listed, fall
  back to a plain relation_compare or numeric_compare and note the table is needed.
- DISAMBIGUATE the two value-validity mechanisms — a vague "valid / hợp lệ" is NOT
  enough to tell them apart, so decide by whether the required value depends on a
  RELATED element:
  * MEMBERSHIP in an APPROVED SET (the value's validity is INTRINSIC — it stands on
    its own) — "must be valid / hợp lệ / approved", "must be one of the approved
    <classification / Uniformat / Uniclass / material / category> values". A
    classification / category / material code being "valid" is ALWAYS this →
    requirement="canonical_format" + normalize_kind="reference" +
    normalize_reference="<set>". NOT relation_compare, NOT mere present_and_nonempty
    (presence ≠ validity). When the SHARED / openBIM PARAMETERS block below cites a
    reference set for the bound parameter, use THAT exact set name — never invent one.
  * REQUIRED BY A CODE TABLE keyed by a RELATED element (the needed value depends on
    another element's situation — the host wall's rating, the room's occupancy) →
    requirement="relation_compare" + lookup="<table>". Use ONLY when the requirement
    references a host / containing / related element. A bare "valid classification
    code" has NO related element → it is membership (above), not this.
- APPLICABILITY / scope ("ONLY external doors", "chỉ áp dụng cho phòng Residential",
  "for fire-rated walls only", "applies to exterior walls") → set
  scope_filter={"param":"<gating param>","pattern":"<regex it must match>"} so the
  rule runs ONLY on elements whose <param> matches. Derive the regex from the
  user's own qualifying word. Examples: "only external doors"
  → {"param":"Function","pattern":"(?i)exterior"}; "only residential rooms" →
  {"param":"Department","pattern":"(?i)residential"}; a custom flag "IsExternal" →
  {"param":"IsExternal","pattern":"(?i)^(true|yes|1)$"}; "accessible-route doors
  must have Width ≥ 900mm" → {"param":"Function","pattern":"(?i)access"};
  BUT "a fire door's Fire Rating must match 'X HR'" → scope_filter=null — "fire"
  already appears in the checked parameter's name (Fire Rating), so it restates
  WHAT is checked rather than gating WHO is checked, and doors carry no "fire"
  flag param (a guessed gate like Function~"(?i)fire" matches nothing → the rule
  silently finds zero elements). Leave null = applies to ALL
  elements. scope_filter does NOT change requirement/parameter — it only gates WHICH
  elements the check runs on. Capture it whenever the requirement is conditioned on
  a subset ("only…", "chỉ…", "for … only", or a qualifying noun-phrase like
  "accessible-route doors" whose word does NOT appear in the checked parameter's
  name).
- severity_level: safety/code-critical (fire, egress, structure) → severity_high;
  naming/format/metadata → severity_medium; cosmetic → severity_low.
- id must be lowercase dot-separated: e.g. "doors.fire_rating.canonical".
- Respond with ONLY the JSON object. No markdown, no explanation.\
"""

# Autocomplete/fallback hints per catalog display name — used ONLY when
# OSTCatalog fails to load (mirrors the app.py _PARAM_HINTS fallback so
# grounding_block's category list degrades identically either side of B16).
_PARAM_HINTS_FALLBACK: dict[str, list[str]] = {
    "Rooms":               ["Name", "Number", "Department", "Occupancy", "Area", "Unbounded Height", "Level", "Comments"],
    "Walls":               ["Type", "Fire Rating", "Width", "Length", "Area", "Unconnected Height"],
    "Doors":               ["Mark", "Type Mark", "Fire Rating", "Width", "Height", "Level", "Comments"],
    "Windows":             ["Mark", "Type Mark", "Width", "Height", "Sill Height", "Level"],
    "Structural Columns":  ["Mark", "Type", "Family Name", "Level", "Length", "Base Level", "Top Level", "Comments"],
    "Structural Framing":  ["Mark", "Type", "Family Name", "Level", "Length", "Comments"],
    "Floors":              ["Type", "Level", "Thickness", "Area", "Comments"],
    "Ceilings":            ["Type", "Level", "Height Offset From Level", "Area", "Comments"],
    "Generic Models":      ["Mark", "Type", "Family Name", "Level", "Comments"],
    "Furniture":           ["Mark", "Type", "Family Name", "Level", "Comments"],
    "Ducts":               ["Mark", "Type", "System Type", "Level", "Size", "Length"],
    "Pipes":               ["Mark", "Type", "System Type", "Level", "Size", "Length"],
}

# Clarifying notes for categories users confuse (shown under the category picker
# in Streamlit; surfaced verbatim on GET /api/catalogs/categories for M2).
CATEGORY_NOTES: dict[str, str] = {
    "Stairs": (
        "🪜 **Stairs** = the whole staircase (assembly). The Type holds *design "
        "rules* (`Minimum Run Width`, `Maximum Riser Height`). To check the "
        "**ACTUAL width/risers** (egress, compliance) → switch to **Stair Runs** "
        "+ `Actual Run Width`."
    ),
    "Stair Runs": (
        "🪜 **Stair Runs** = each flight. The Instance holds the **as-built** "
        "per-run dimensions (`Actual Run Width` = the real width). This is the "
        "right category for **compliance checks** on width/risers. (`Minimum Run "
        "Width` on Stairs is only a *minimum setting*, not a measured value.)"
    ),
    "Mechanical Equipment": (
        "🔧 A **\"maintainable asset\"** is not one Revit category — it spans "
        "**Mechanical Equipment** (pumps/chillers/AHUs, the default), "
        "**Electrical Equipment** (panels/transformers), **Plumbing Fixtures** "
        "(fixtures/valves) and **Sprinklers** (heads). If the source text names a "
        "specific equipment type, switch the category to match; otherwise this "
        "rule applies only to Mechanical Equipment — duplicate it manually for the "
        "other three categories if needed."
    ),
}

# Intent → real built-in parameter, per OST. These are the "non-obvious" mappings a
# QA/model-checker hits: the colloquial phrase ≠ the Revit param name, and a literal
# match silently checks the WRONG (often-empty) param. Sourced from the catalog +
# Revit/Solibri community practice (room clear/ceiling height → Unbounded Height; a
# door's clear/egress width → Width, since true clear width is usually a custom
# param). Used both to GROUND the LLM and to steer the dropdown auto-suggest.
INTENT_ALIASES: dict[str, dict[str, str]] = {
    "OST_Rooms": {
        "ceiling height": "Unbounded Height", "clear height": "Unbounded Height",
        "room height": "Unbounded Height", "floor to ceiling": "Unbounded Height",
        "headroom": "Unbounded Height",
    },
    "OST_Doors": {
        "clear width": "Width", "clear opening width": "Width",
        "opening width": "Width", "egress width": "Width", "leaf width": "Width",
        "clear height": "Height", "opening height": "Height",
    },
    "OST_Windows": {
        "sill height": "Sill Height", "clear width": "Width", "opening width": "Width",
    },
    "OST_StairsRuns": {
        "clear width": "Actual Run Width", "run width": "Actual Run Width",
        "stair width": "Actual Run Width", "minimum width": "Actual Run Width",
    },
    "OST_Stairs": {"minimum width": "Minimum Run Width", "stair width": "Minimum Run Width"},
    "OST_PipeCurves": {"pipe diameter": "Diameter", "nominal diameter": "Diameter"},
    "OST_DuctCurves": {"duct diameter": "Diameter", "duct size": "Size"},
}


def _get_catalog_categories() -> list[str]:
    """Sorted display names from OSTCatalog; fall back to the hint table.

    Uses ``policies.ost_catalog.OSTCatalog`` directly — the SAME class the
    extraction-skills ``json_to_yaml.py`` re-exports as ``OSTCatalog``, so this
    is behaviour-identical to the pre-B16 app.py helper, just without the
    by-path module load (a "policies/" import is enough here).
    """
    try:
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        return sorted({e.display for e in OSTCatalog.load()._entries})
    except Exception:
        return sorted(_PARAM_HINTS_FALLBACK.keys())


def _ost_display_map() -> dict:
    """OST → display-name map (so catalog params can be labelled by category)."""
    try:
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        return {e.ost: e.display for e in OSTCatalog.load()._entries}
    except Exception:
        return {}


def _load_param_catalog_cached():
    """Load the parameter catalog (None if unavailable). ``load_param_catalog``
    already caches by resolved path."""
    try:
        from bim_orchestrator.policies.param_catalog import load_param_catalog
        return load_param_catalog()
    except Exception:
        return None


def _load_shared_conventions_cached():
    """Load the shared-parameter conventions (None if unavailable).
    ``load_shared_param_conventions`` caches by resolved path."""
    try:
        from bim_orchestrator.policies.shared_params import load_shared_param_conventions
        return load_shared_param_conventions()
    except Exception:
        return None


def _catalog_params_for(category: str) -> list:
    """ParamSpec list for a category display label ([] if not catalogued)."""
    try:
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        ost = OSTCatalog.load().resolve(category, backend="revit")
    except Exception:
        ost = None
    pcat = _load_param_catalog_cached()
    return pcat.params_for(ost) if (pcat and ost) else []


def grounding_block(catalog_grounded: bool) -> str:
    """Append the LIVE category list (+ catalog params, when grounded) to the
    extraction prompt.

    The hard-coded enum in ``RB_EXTRACT_SYSTEM`` omits Stairs/Runs/Ramps/etc., so
    a small model is forced to pick the nearest listed category (a "stair run"
    became "Structural Framing"). Feeding the real OSTCatalog categories — and, in
    catalog mode, the valid built-in params per category — grounds the model so it
    picks the right category + maps intent ("clear width") to the real param
    ("Actual Run Width"). This is retrieval-grounding, not a bigger model.
    """
    cats = _get_catalog_categories()
    block = (
        "\n\n=== AUTHORITATIVE CATEGORIES (override any list above) ===\n"
        "Set `category` EXACTLY to one of:\n" + " | ".join(cats) + "\n"
        "Mapping hints: a landing → 'Stair Landings'; a ramp → 'Ramps'; a beam → "
        "'Structural Framing'; a column → 'Structural Columns'; a footing/"
        "foundation → 'Structural Foundations'. A 'maintainable asset' / "
        "'thiết bị bảo trì' (a generic FM/MEP concept spanning several "
        "categories, not a Revit category itself) → pick the MOST SPECIFIC "
        "category the sentence names (e.g. a pump/chiller/AHU → 'Mechanical "
        "Equipment'; a panel/transformer → 'Electrical Equipment'; a fixture/"
        "valve → 'Plumbing Fixtures'; a sprinkler head → 'Sprinklers'); if the "
        "sentence names no specific asset, pick ONE representative category "
        "(default 'Mechanical Equipment') and say in `description` that the "
        "rule should be duplicated for the other maintainable-asset categories "
        "(Electrical Equipment, Plumbing Fixtures, Sprinklers) — do NOT try to "
        "emit more than one rule.\n"
        "STAIRS vs STAIR RUNS (important — they share width params): 'Stairs' is the "
        "whole assembly; its TYPE holds DESIGN-RULE settings ('Minimum Run Width', "
        "'Maximum Riser Height', 'Minimum Tread Depth'). 'Stair Runs' is an individual "
        "FLIGHT; its INSTANCE holds the AS-BUILT geometry ('Actual Run Width', 'Actual "
        "Riser Height', 'Actual Tread Depth'). A code/compliance check measures the "
        "REAL value, so 'minimum clear width / tread / riser of a stair' → category "
        "'Stair Runs' + 'Actual Run Width' (NOT 'Stairs'/'Minimum Run Width', which is "
        "only the design setting). Use 'Stairs' only when the rule is explicitly about "
        "the stair's overall config or its design-rule type settings."
    )
    if catalog_grounded:
        pcat = _load_param_catalog_cached()
        if pcat is not None:
            dmap = _ost_display_map()
            lines = [
                f"- {dmap.get(c.ost, c.key)}: " + ", ".join(s.name for s in c.params)
                for c in pcat.categories
            ]
            block += (
                "\n\n=== VALID BUILT-IN PARAMETERS per category ===\n"
                "Set `parameter` to a name from the chosen category's list, mapping "
                "the user's wording to the closest real parameter (e.g. a stair "
                "run's 'clear width' → 'Actual Run Width'; a door's 'fire rating' → "
                "'Fire Rating'). Only if NONE fits, output the exact Revit shared/"
                "project parameter name verbatim.\n" + "\n".join(lines)
            )
            alias_lines = [
                f"- {dmap.get(ost, ost)}: "
                + "; ".join(f'"{k}" → {tgt}' for k, tgt in amap.items())
                for ost, amap in INTENT_ALIASES.items()
            ]
            block += (
                "\n\n=== INTENT → PARAMETER aliases (non-obvious; map these EXACTLY) "
                "===\nThe colloquial phrase on the left is NOT the Revit param name; "
                "use the param on the right (a literal match silently checks the "
                "wrong, often-empty param):\n" + "\n".join(alias_lines)
            )
    # Available lookup tables (code tables a relation_compare can cite by name).
    import yaml as _yaml_lk
    tbl_lines = []
    for p in sorted(_CONFIG_DIR.glob("lookup.*.yaml")):
        name = p.stem[len("lookup."):]
        try:
            desc = (_yaml_lk.safe_load(p.read_text(encoding="utf-8")) or {}).get("description", "")
        except Exception:
            desc = ""
        tbl_lines.append(f"- {name}: {str(desc).strip().splitlines()[0] if desc else ''}")
    if tbl_lines:
        block += (
            "\n\n=== AVAILABLE LOOKUP TABLES (cite by name on a relation_compare; "
            "do NOT transcribe) ===\n" + "\n".join(tbl_lines)
        )
    # Shared / openBIM parameters (COBie, classification, IFC Pset) — NOT native
    # built-ins, so they're absent from the per-category param list above. Ground
    # the model so a deliverable rule binds to the agreed shared-param name.
    sconv = _load_shared_conventions_cached()
    if sconv is not None and sconv.conventions:
        sp_lines = []
        for c in sconv.conventions:
            scope = ", ".join(c.applies_to) if c.applies_to else "any"
            intent = "; ".join(c.intent[:3])
            ref = f" → cite reference '{c.reference}'" if c.reference else ""
            sp_lines.append(
                f'- "{intent}" → parameter `{c.parameter}` '
                f"({c.binding}, {c.standard}; applies to: {scope}){ref}"
            )
        block += (
            "\n\n=== SHARED / openBIM PARAMETERS (COBie · classification · IFC Pset) "
            "===\nThese are deliverable parameters that are NOT Revit built-ins, so "
            "they are absent from the per-category list above. When the user asks for "
            "one of these, set `parameter` to the exact name on the right and "
            "`bound_parameter` to the same (it's a shared/project param). For a "
            "value-constraint, use `requirement: canonical_format` + "
            "`autofill.normalize_reference: <the cited set>`; for a completeness check "
            "use `present_and_nonempty`:\n" + "\n".join(sp_lines)
        )
    return block


# ── LLM draft (NL → rule dict) ──────────────────────────────────────────────


class LLMNotConfiguredError(RuntimeError):
    """No usable LLM client for :func:`draft_rule`.

    ``silent`` distinguishes the two source states carried over from the
    pre-B16 Streamlit code: a missing Anthropic API key (Streamlit's Generate
    button is already disabled in this state, so a caller should degrade
    silently — no error banner) vs a client construction failure (worth
    surfacing). The M2 ``POST /api/builder/draft`` endpoint always maps this
    to HTTP 503 regardless of ``silent`` (there is no "disabled button" to
    lean on over an API).
    """

    def __init__(self, message: str, *, silent: bool = False) -> None:
        super().__init__(message)
        self.silent = silent


class RuleDraftError(RuntimeError):
    """The LLM call for :func:`draft_rule` failed (bad JSON / provider error)."""


def _complete_json_sync(client: Any, system: str, prompt: str) -> dict:
    """Run one ``complete_json`` from sync code.

    Lifted verbatim out of ``draft_rule`` (Streamlit-era shape) so the router
    and the geometry path reuse the same event-loop handling rather than
    copying it — the loop fallback exists because a caller may already be
    inside a running loop.
    """
    import asyncio as _asyncio

    async def _run() -> dict:
        return await client.complete_json(system=system, prompt=prompt)

    try:
        return _asyncio.run(_run())
    except RuntimeError:  # an event loop is already running in this thread
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()


# ── Geometry-from-NL (2026-08-26) — a ROUTER, not a bigger prompt ───────────
#
# WHY A SECOND CALL INSTEAD OF ONE FATTER PROMPT. `RB_EXTRACT_SYSTEM` carries
# the proven 63/63 rail-B NL suite. Teaching it a geometry branch would put
# that suite at risk days before a recording, for a feature it has never
# covered. A cheap classifier in front leaves the parameter prompt BYTE
# IDENTICAL — the parameter path cannot regress by construction — at the cost
# of ~1s per Generate. Revisit the merge when the suite can be re-run here.
#
# WHY IT IS NEEDED AT ALL. Before this, a clearance sentence did not fail — it
# produced a plausible-looking PARAMETER rule. Measured 2026-08-26 on "In the
# parking space, the lowest part of a duct must be higher than 2100 mm above
# the floor below it": `parameter: Middle Elevation, numeric_compare >= 2100,
# scope_filter System Name ~ parking`. Middle Elevation is height above the
# element's LEVEL, not clearance to the floor beneath; System Name is the MEP
# system ("Mechanical Supply Air"), not a space. It runs, it reports, and it
# means nothing — the silently-wrong class, worse than a refusal.

_RB_ROUTE_SYSTEM = """Classify ONE building-rule sentence. Return JSON:
{"kind": "parameter" | "geometry"}

"geometry" = the requirement is a DISTANCE IN SPACE between the element and
something else: clearance, headroom, "clear to/from", "above/below the
floor/ceiling/slab", "keep N mm from", "must not be closer than". No element
stores its distance to another element as a value, so a distance requirement
is never a parameter rule.

"parameter" = everything else: a value ON the element (fire rating, mark,
name, width, material, classification), presence, format, uniqueness, or a
comparison against a related element's VALUE or a code table.

Careful: an element's OWN dimension ("doors must be at least 900 mm wide",
"a duct's height must be 300 mm") is a PARAMETER — the width/height is stored
on the element. Only a gap BETWEEN things is geometry.

Return only the JSON object."""

RB_GEOMETRY_SYSTEM = """You convert ONE building-rule sentence into a JSON
geometry rule. Return ONLY the JSON object, with exactly these keys:

  id                  lowercase dot-separated, e.g. "ducts.parking_headroom_min"
  category            element category being CHECKED, e.g. "Ducts"
  check_type          "clearance_min" (a minimum gap) | "clearance_max"
  description         one sentence restating the requirement
  threshold_mm        number in MILLIMETRES (convert: 2.1 m -> 2100)
  clearance_direction "below" | "above" | "horizontal"
  reference_category  what the distance is measured TO, e.g. "Floors", "Ceilings"
  reference_source    "same_model" | "linked_arch" | "linked_struct" | "linked_mep"
  spatial_filter      {"category":"Spaces","name_contains":"<word>","name_exact":null}
                      when the sentence limits the check to a named space,
                      otherwise null
  severity_tag        "geometric_violation"
  execution_status    "not_model_checkable"
  view_id             null
  notes               null

DIRECTION IS THE RAY, NOT THE PREPOSITION. `clearance_direction` is which way
the measurement fires FROM the checked element. Headroom under a duct is
measured DOWNWARD to the floor beneath it, so it is "below" — even though the
sentence says the duct must be *above* the floor. Ask: where is the reference
element? Beneath -> "below". Overhead -> "above". Beside -> "horizontal".

REFERENCE SOURCE. Floors, walls, ceilings and roofs of the building live in
the ARCHITECTURAL model, so from an MEP model use "linked_arch". Structural
framing/columns -> "linked_struct". Same-discipline elements -> "same_model".

SCOPE. "in the parking garage", "only in the plant room" -> spatial_filter
with the distinctive word ("parking", "plant"). No such phrase -> null."""


# Direction words, for the Layer-2 guarantee below. Measured 2026-08-26: the
# model got check_type / threshold / unit conversion / reference / scope right
# on 3 of 3 sentences and the DIRECTION wrong on the 2 where the sentence's
# preposition ("above the floor") pointed the opposite way to the ray.
_REF_BELOW_RE = re.compile(
    r"(?i)\b(?:floor|slab|ground|deck)\s+(?:below|beneath|under(?:neath)?)\b"
    r"|\b(?:below|beneath|under(?:neath)?)\s+(?:it|the\s+(?:duct|pipe|run|element))\b"
    r"|\bhead\s?room\b|\bclear\s+to\s+the\s+floor\b"
)
_REF_ABOVE_RE = re.compile(
    r"(?i)\b(?:ceiling|slab|soffit|beam|structure)\s+above\b"
    r"|\babove\s+(?:it|the\s+(?:duct|pipe|run|element))\b"
    r"|\bclear\s+above\b|\boverhead\b"
)


def apply_geometry_nl_intents(nl: str, rule: dict) -> dict:
    """Deterministically fix the one thing the geometry draft gets wrong.

    Pure, idempotent, returns a corrected COPY — same contract and doctrine as
    :func:`apply_nl_intents` ("the LLM never holds the pen"). ONE correction,
    because one is what the evidence supports:

    * **direction follows the REFERENCE, not the preposition.** "must be higher
      than 2100 mm above the floor below it" made the model emit
      ``clearance_direction: "above"`` — it read the word *above*. The ray
      fires DOWN to that floor, so the rule measured the wrong half-space and
      would have reported nothing. When the sentence says where the reference
      sits (floor below / headroom / clear above / ceiling above), that wins.

    Silent when the sentence gives no positional evidence, and when both
    patterns match (genuinely ambiguous — do not guess).
    """
    if not isinstance(rule, dict) or not isinstance(nl, str):
        return rule
    if not str(rule.get("check_type") or "").startswith("clearance"):
        return rule
    below, above = bool(_REF_BELOW_RE.search(nl)), bool(_REF_ABOVE_RE.search(nl))
    if below == above:  # neither, or contradictory -> leave the draft alone
        return rule
    want = "below" if below else "above"
    if rule.get("clearance_direction") == want:
        return rule
    out = copy.deepcopy(rule)
    out["clearance_direction"] = want
    return out


def draft_geometry_rule(text: str, client: Any) -> dict:
    """NL -> geometry rule dict (already direction-corrected)."""
    return apply_geometry_nl_intents(
        text, _complete_json_sync(client, RB_GEOMETRY_SYSTEM, text)
    )


def draft_rule(text: str) -> tuple[dict, list[str]]:
    """NL → one Rule dict, via the provider-agnostic LLM seam.

    Always catalog-grounded (the experimental "ungrounded" mode was only ever
    used by the merged v2 tab's default call — see the pre-B16
    ``_call_claude_for_rule(..., catalog_grounded=True)`` call site). Routed
    through ``build_llm_client()`` so the Rule Builder honours
    ``BIM_LLM_PROVIDER`` (cloud Anthropic default, or a local Ollama model) —
    the same switch the runtime agents use.

    Returns ``(rule_dict, warnings)``. ``warnings`` is currently always empty
    (reserved for parity with the PDF-extraction path's warning list — M2-C,
    not wired here). Raises :class:`LLMNotConfiguredError` when there's no
    usable client, :class:`RuleDraftError` when the call itself fails.
    """
    from bim_orchestrator.llm.client import LLMError
    from bim_orchestrator.llm.factory import build_llm_client, llm_provider

    # Anthropic needs a key; local Ollama does not.
    if llm_provider() != "ollama" and not os.environ.get("ANTHROPIC_API_KEY", ""):
        raise LLMNotConfiguredError(
            "ANTHROPIC_API_KEY is not configured (and the current provider is not ollama)",
            silent=True,
        )
    try:
        client = build_llm_client()
    except Exception as exc:
        raise LLMNotConfiguredError(f"Could not initialize the LLM client: {exc}") from exc

    # Router first (see _RB_ROUTE_SYSTEM): a clearance sentence must NOT reach
    # the parameter prompt, which answers it with a plausible, meaningless rule.
    try:
        kind = str(
            (_complete_json_sync(client, _RB_ROUTE_SYSTEM, text) or {}).get("kind")
            or "parameter"
        ).strip().lower()
    except Exception as exc:
        # Router failure must not break rule drafting: fall back to the
        # parameter path, which is exactly the pre-router behaviour.
        # `log` is imported here, not at module scope, because this module
        # is imported by the Streamlit app before logging is configured.
        import structlog

        structlog.get_logger(__name__).warning(
            "rule_builder.route_failed", error=str(exc)
        )
        kind = "parameter"
    if kind == "geometry":
        try:
            return draft_geometry_rule(text, client), []
        except LLMError as exc:
            raise RuleDraftError(f"The LLM returned invalid JSON / errored: {exc}") from exc

    try:
        rule = _complete_json_sync(
            client, RB_EXTRACT_SYSTEM + grounding_block(True), text
        )
    except LLMError as exc:
        raise RuleDraftError(f"The LLM returned invalid JSON / errored: {exc}") from exc
    except Exception as exc:
        raise RuleDraftError(f"LLM error: {exc}") from exc
    return apply_nl_intents(text, rule), []


# ── Deterministic NL-intent belt (v1.7-3bP7, Layer 2 of the Ken-approved
#    teach+belt design) ────────────────────────────────────────
#
# House doctrine: "the LLM never holds the pen." Two NL-20 gaps (1: a quoted
# literal format overridden by an invented one — makes a COMPLIANT element fail;
# 2: "when empty, inherit from host" silently dropped) survived a strengthened
# prompt (Layer 1, v1.7-3bP6) at the demo level, but a prompt is hope, not a
# guarantee — a future model can regress. This belt mechanically re-derives the
# two intents from the NL AFTER the draft, exactly as enforce_reference_membership
# (K24) guarantees a fact the prompt merely states. Called from draft_rule (which
# has the NL in scope), NOT from the enforce_* save chain (those receive only the
# rule dict, without the NL). Pure + idempotent + import-safe so the rail-B
# harness can import it and stay behaviourally identical (§Mirror contract).

# The quoted-placeholder convention every failing duration NL uses: 'X HR',
# 'X Min', 'X-hour', '2 HR'. Captures placeholder / separator / unit word so the
# derived format preserves the user's separator + casing verbatim.
_DURATION_LITERAL_RE = re.compile(
    r"(?i)^(?:X|N|\d+(?:\.\d+)?)(?P<sep>[\s_-]?)(?P<unit>HR|Hours?|hour|Min|Minutes?|giờ|phút)$"
)
_QUOTED_RE = re.compile(r"['\"]([^'\"]{1,16})['\"]")
_EMPTY_CLAUSE_RE = re.compile(
    r"(?i)(when|if|khi|nếu)\b.{0,40}\b(empty|blank|missing|trống|thiếu)"
)
_INHERIT_CLAUSE_RE = re.compile(r"(?i)(inherit|kế thừa|lấy theo)")


def _duration_format_from_literal(literal: str) -> str | None:
    """A single quoted literal ('X HR', 'X-hour', '2 Min') → its canonical
    normalize_format token string ('{h} HR', '{h}-hour', '{m} Min'), or None if
    it is not a duration literal. Preserves the literal's separator + casing."""
    m = _DURATION_LITERAL_RE.match(literal.strip())
    if m is None:
        return None
    unit = m.group("unit")
    token = "{h}" if unit.lower().rstrip("s") in ("hr", "hour", "giờ") else "{m}"
    return f"{token}{m.group('sep')}{unit}"


_SEP_RUN_RE = re.compile(r"[ _-]+")


def _widen_prefix_strip(source: str, fmt: str) -> str | None:
    """Make a template's optional prefix-strip group separator-tolerant (2c).

    The model writes the strip group as the literal canonical prefix —
    ``(?:ADSK_)?`` — which only strips that exact spelling. Every OTHER
    spelling of the same prefix then gets the prefix stacked on top:
    "ADSK Fur Chair Viper" → "ADSK_ADSK_Fur_Chair_Viper",
    "adsk-Chair-X" → "ADSK_adsk_Chair_X". The prompt already teaches
    ``[ _-]`` tolerance and one draft in three still emitted the strict form
    (measured 2026-08-25) — same coin-flip class as the "{1}" field.

    Returns the widened source, or None when the shape doesn't apply (format
    has no literal separator-terminated head, or the source has no strict
    group for it). The rewrite keeps the SOURCE's own spelling/casing of the
    prefix — substituting the format's casing could change behaviour for a
    source without ``(?i)``.
    """
    if not isinstance(source, str) or not isinstance(fmt, str):
        return None
    head = fmt.split("{", 1)[0]
    if not head or not re.fullmatch(r"[A-Za-z0-9 _-]+", head) or head[-1] not in " _-":
        return None
    words = [w for w in _SEP_RUN_RE.split(head) if w]
    if not words:
        return None
    strict = re.compile(
        r"\(\?:" + r"[ _-]+".join(re.escape(w) for w in words) + r"[ _-]+\)\?",
        re.IGNORECASE,
    )

    def _widen(m: re.Match[str]) -> str:
        inner = m.group(0)[3:-2]  # drop "(?:" and ")?"
        return "(?:" + _SEP_RUN_RE.sub("[ _-]+", inner) + ")?"

    return strict.sub(_widen, source)


def apply_nl_intents(nl: str, rule: dict) -> dict:
    """Deterministically re-assert NL intents a drifting model may drop.

    Pure (no I/O/LLM/streamlit), idempotent (``f(f(x)) == f(x)``), returns a
    corrected COPY. Three corrections, all guarded to NEVER change an existing
    verdict:

    * (2a) literal-format fidelity — when a duration normalize rule's NL quotes
      exactly one target form, force ``normalize_format`` to that literal's
      canonical tokens. Fixes gap 1 (an invented ``{h}-hour`` flags a compliant
      ``2 HR`` door). Ambiguous (two different derived forms) or absent → leave
      the draft alone.
    * (2b) empty→inherit upgrade — a plain ``normalize`` whose NL has BOTH an
      empty-clause and an inherit-clause becomes ``inherit_then_normalize``.
      Fixes gap 2. False-positive-safe: ``inherit_then_normalize`` behaves
      identically to ``normalize`` on non-empty values (the inherit leg only
      fires when empty), so a spurious upgrade can only ADD a suggestion where
      there was none, never flip a verdict.
    * (2c) template prefix-strip tolerance — a ``template`` autofill whose
      format opens with a literal prefix ("ADSK_{rest}") gets its strict strip
      group ``(?:ADSK_)?`` widened to ``(?:ADSK[ _-]+)?``. Verdict-safe: the
      change only broadens what the OPTIONAL group strips, so an already-
      canonical name still round-trips unchanged and a non-compliant one stays
      non-compliant — its proposed fix just stops stacking the prefix twice.
      (Not NL-driven; it fires on rule shape alone, but lives here because
      this is the one deterministic pen every draft passes through.)
    """
    if not isinstance(rule, dict) or not isinstance(nl, str):
        return rule
    autofill = rule.get("autofill")
    if not isinstance(autofill, dict):
        return rule
    strategy = autofill.get("strategy")
    if strategy not in ("normalize", "inherit_then_normalize"):
        return rule

    out = copy.deepcopy(rule)
    af = out["autofill"]

    # (2a) — duration only (the observed failure class; length/area share the
    # mechanism and can join in a later version).
    if af.get("normalize_kind") == "duration":
        derived = {
            fmt
            for literal in _QUOTED_RE.findall(nl)
            if (fmt := _duration_format_from_literal(literal)) is not None
        }
        if len(derived) == 1:
            only = next(iter(derived))
            if af.get("normalize_format") != only:
                af["normalize_format"] = only

    # (2b) — upgrade a plain normalize to the compound strategy. Never touch
    # inherit_from_host (relation cases) and never downgrade.
    if (
        af.get("strategy") == "normalize"
        and _EMPTY_CLAUSE_RE.search(nl)
        and _INHERIT_CLAUSE_RE.search(nl)
    ):
        af["strategy"] = "inherit_then_normalize"

    # (2c) — see the docstring; shape-driven, no NL needed.
    if af.get("normalize_kind") == "template":
        widened = _widen_prefix_strip(
            af.get("normalize_source"), af.get("normalize_format")
        )
        if widened is not None and widened != af.get("normalize_source"):
            af["normalize_source"] = widened

    return out


# ── Deterministic save-time enforcement (QA F2 / G7) ────────────────────────


def fetch_name_dict(rule: dict) -> str:
    """Dict-form twin of :func:`policies.rules_schema.fetch_name` (L1, audit).

    ``fetch_name`` resolves ``bound_parameter or parameter`` for a validated
    Rule OBJECT, but the authoring guards below run on the raw draft DICT,
    BEFORE Pydantic validation. Each of them used to inline its own
    ``bound or canonical`` expression — and that duplication is precisely what
    let one site (the read-only catalog guard) keep reading the canonical name
    only. One resolver for dicts, so a new guard can't drift again.

    Why bound wins: the canonical ``parameter`` is the human intent label
    (possibly a Vietnamese alias); ``bound_parameter`` is the real Revit
    parameter the model actually carries, so it is what any catalog lookup,
    write target, or params-dict read must key on.
    """
    return str(rule.get("bound_parameter") or rule.get("parameter") or "").strip()


def enforce_unique_autofix(rule: dict) -> dict:
    """QA F2 — HARD-ENFORCE: a ``unique_in_set`` rule is ALWAYS Path-B auto.

    The prompt already says "uniqueness → fixability=auto", but Haiku is unreliable
    on this fixed fact (sometimes returns ``manual``). Uniqueness is *always*
    auto-fixable — the engine renumbers a duplicate to the next-available value
    (approve-gated via the ACC proposal, so it's safe to enable: the autonomy
    gate in ``design._prepare_revit_fix`` demotes any ``next_available`` write
    out of ``auto``, F-02 — this docstring used to ASSUME that guarantee while
    nothing enforced it). So we override deterministically at save rather than
    trusting the model. INSTANCE
    target: each element needs its OWN unique value (a type write would collapse
    them all to one). Returns a copy; non-unique rules pass through unchanged.
    """
    if rule.get("requirement") != "unique_in_set":
        return rule
    r = dict(rule)
    r["fixability"] = "auto"
    r["autofill"] = {"strategy": "none"}
    rem = dict(r.get("remediation") or {})
    rem["action"] = "set_parameter"
    rem["target"] = "instance"
    # Bake in the BOUND name when there is one. Every write site resolves
    # `remediation.target_parameter or fetch_name(rule)`, so a canonical alias
    # stored here PRE-EMPTS the runtime resolver — the write then targets a
    # parameter that does not exist on the element, the preview fails, and the
    # fix silently degrades to a Path A issue.
    rem.setdefault("target_parameter", fetch_name_dict(r))
    rem.setdefault("new_value_strategy", "next_available")
    r["remediation"] = rem
    return r


def enforce_reference_membership(rule: dict) -> dict:
    """QA G7 — steer a value-validity rule to ``canonical_format`` + reference SET.

    When the bound parameter has a shared-param convention that names an authoritative
    membership ``reference`` set (Assembly Code → ``classification_codes``,
    Classification Number → ``uniclass_pr``, COBie.Type.Category → ``omniclass_table23``),
    "the value must be valid" is membership-in-an-approved-set — i.e.
    ``canonical_format`` + ``autofill.normalize_reference``. Vague NL ("phải hợp lệ" /
    "must be valid") leaves Haiku oscillating between THAT and ``relation_compare`` +
    ``lookup`` — the DIFFERENT mechanism (a value REQUIRED BY A CODE TABLE keyed by a
    RELATED element, e.g. IBC §716 door rating by host wall) — and it even mislabels the
    reference set as a lookup table and INVENTS the set name (``uniformat_codes`` ≠
    ``classification_codes``). The bound parameter is the stable signal (the model binds
    it correctly even when vague), so we override deterministically at save off the
    convention — exactly like :func:`enforce_unique_autofix`. This also canonicalises an
    invented set name to the convention's real one.

    Only the two value-validity requirements are steered; ``present_and_nonempty`` (a
    legitimate completeness-ONLY ask — "Assembly Code phải được điền") and unrelated
    requirements pass through untouched. Returns a copy when steered, else the original.
    """
    if rule.get("requirement") not in ("canonical_format", "relation_compare"):
        return rule
    sconv = _load_shared_conventions_cached()
    if sconv is None:
        return rule
    # Bound first — as this function's own docstring says, the bound parameter
    # is the stable signal. Resolving the canonical label first made the
    # `bound_parameter` fallback dead code (`parameter` is always non-empty),
    # so a rule whose canonical name is an alias never picked up its
    # convention and the reference guard silently never fired. Canonical stays
    # as the fallback for unbound rules and for bindings with no convention.
    primary = fetch_name_dict(rule)          # bound when present, else canonical
    canonical = (rule.get("parameter") or "").strip()
    conv = sconv.resolve(primary) if primary else None
    if conv is None and canonical and canonical != primary:
        conv = sconv.resolve(canonical)
    if conv is None or not conv.reference:
        return rule
    r = dict(rule)
    r["requirement"] = "canonical_format"
    r["fixability"] = "auto"
    r["autofill"] = {
        "strategy": "normalize",
        "normalize_kind": "reference",
        "normalize_reference": conv.reference,
    }
    # set_parameter + auto: K19 resolves the write target (Assembly Code etc. are
    # type-carried); off-list values still become ACC issues via QC's reference tier-3.
    r["remediation"] = {"action": "set_parameter", "target": "auto"}
    # Drop relation_compare / numeric leftovers so the saved rule is a coherent
    # canonical_format (these are only copied through when truthy at save time).
    for k in ("lookup", "other_param", "compare_kind", "operator", "threshold",
              "unit", "pattern"):
        r.pop(k, None)
    return r


# ── Validation (POST /api/builder/validate + the Streamlit live-validation) ─


@dataclass
class ValidationIssue:
    field: str
    message: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_rule(
    rule: dict,
    is_geometry: bool,
    *,
    ruleset_categories: str | list[str] | None = None,
) -> ValidationResult:
    """Validate a Rule Builder draft. Mirrors the Streamlit live-validation
    block verbatim for the parameter-check path (was app.py's "Step 2" block,
    now the ONE source shared with ``POST /api/builder/validate``).

    Geometry rules have no equivalent hand-authored validation in the
    pre-B16 Streamlit tab (only the final ``GeometryRule.model_validate`` at
    save time) — this reuses that same schema so the M2 builder gets useful
    per-field errors instead of validating nothing.

    ``ruleset_categories`` is the enclosing ``RuleSet.target_category``
    (P1-05). A rule may leave ``category`` unset and inherit the ruleset's
    targets; without that context the catalog guard below looked up an empty
    category, found no ParamSpec and passed everything — the guard silently
    did nothing for the most common authoring shape.
    """
    if is_geometry:
        return _validate_geometry_rule(rule)
    return _validate_parameter_rule(rule, ruleset_categories=ruleset_categories)


def _validate_geometry_rule(rule: dict) -> ValidationResult:
    from pydantic import ValidationError

    from bim_orchestrator.policies.rules_schema import GeometryRule

    try:
        GeometryRule.model_validate(rule)
    except ValidationError as exc:
        errors = [
            ValidationIssue(
                field=".".join(str(p) for p in e["loc"]) or "rule",
                message=e["msg"],
            )
            for e in exc.errors()
        ]
        return ValidationResult(ok=False, errors=errors, warnings=[])
    return ValidationResult(ok=True, errors=[], warnings=[])


def write_target_parameter(rule: dict) -> str:
    """The parameter a Path-B fix will actually WRITE (P1-05).

    Mirrors the runtime resolution used at every write site in DesignAgent
    (``remediation.target_parameter or fetch_name(rule)``). The read-only guard
    has to ask the same question the executor will, otherwise a rule can name a
    writable parameter in ``parameter`` and a read-only one in
    ``target_parameter`` and sail through validation.
    """
    rem = rule.get("remediation") or {}
    return str(rem.get("target_parameter") or "").strip() or fetch_name_dict(rule)


def _effective_categories(
    rule: dict, ruleset_categories: str | list[str] | None
) -> list[str]:
    """Categories a rule actually applies to: its own, else the ruleset's.

    ``Rule.category`` is optional — an unset one inherits every
    ``RuleSet.target_category``. Looking up only ``rule["category"]`` meant the
    catalog lookup ran against ``""`` for the common case.
    """
    own = str(rule.get("category") or "").strip()
    if own:
        return [own]
    if isinstance(ruleset_categories, str):
        return [ruleset_categories] if ruleset_categories.strip() else []
    return [str(c) for c in (ruleset_categories or []) if str(c).strip()]


def _validate_parameter_rule(
    rule: dict, *, ruleset_categories: str | list[str] | None = None
) -> ValidationResult:
    errors: list[ValidationIssue] = []
    warnings: list[str] = []
    if not str(rule.get("id", "")).strip():
        errors.append(ValidationIssue("id", "Rule ID must not be empty"))
    if not str(rule.get("parameter", "")).strip():
        errors.append(ValidationIssue("parameter", "Revit parameter must not be empty"))
    # Catalog guard: block a Path-B fix that targets a read-only built-in param.
    rem = rule.get("remediation") or {}
    if rule.get("fixability") == "auto" and rem.get("action") == "set_parameter":
        # L1 (audit): match the catalog on the BOUND name — the catalog is keyed
        # by real Revit parameter names, so a canonical alias never matched.
        # P1-05: ask about the parameter that will actually be WRITTEN, and ask
        # it for EVERY category the rule reaches (its own, else the ruleset's).
        # Fail closed: read-only in ANY target category blocks the rule, since
        # the run will hit all of them.
        target = write_target_parameter(rule)
        for cat in _effective_categories(rule, ruleset_categories):
            sp = next(
                (s for s in _catalog_params_for(cat) if s.name == target),
                None,
            )
            if sp is not None and not sp.is_write_target:
                where = f" in {cat}" if cat else ""
                errors.append(ValidationIssue(
                    "parameter",
                    f"`{sp.name}` is read-only{where} — it can't be auto-fixed "
                    "(Path B). Pick a writable parameter or switch to "
                    "📋 Create ACC issue.",
                ))
                break
    # relation_compare needs a comparison source: a related param OR a lookup table.
    # (In "Tra bảng" mode while still creating a new table, neither is set yet.)
    if rule.get("requirement") == "relation_compare" \
            and not rule.get("lookup") and not str(rule.get("other_param") or "").strip():
        errors.append(ValidationIssue(
            "other_param",
            "A relationship compare needs a **related-element parameter** or a "
            "**lookup table** (save the new table, then pick it in the dropdown).",
        ))
    if rule.get("requirement") in ("numeric_min", "numeric_min_conditional"):
        if not rule.get("threshold"):
            errors.append(ValidationIssue(
                "threshold", "A minimum-number requirement must declare a Threshold > 0",
            ))
    # The guard above only covers LEGACY requirements — which the Builder no
    # longer authors (it folds every numeric check to `numeric_compare`), so it
    # was dead code for anything a user can actually create. A blank threshold
    # reaches here as 0/None and `>= 0` / `> 0` passes for every non-negative
    # value: an unfinished "width must be at least 900 mm" saves as "width >= 0"
    # and then reports 100% compliance. An omitted value is not an authored 0 —
    # but `== 0` / `!= 0` IS a legitimate check, so gate on the operator.
    if rule.get("requirement") == "numeric_compare":
        operator = str(rule.get("operator") or ">=")
        # Coerce BEFORE the truthiness test. The UI emits number|null, but a
        # scripted PUT can send the STRING "0"/"0.0" — truthy, so a raw
        # `not threshold` let it slip past the `>= 0` block, then Pydantic
        # lax-coerced it to 0.0 and saved "width >= 0" (always-passes). A
        # non-numeric string surfaces its own error; blank/None → no threshold.
        raw_threshold = rule.get("threshold")
        bad_number = False
        if isinstance(raw_threshold, str):
            stripped = raw_threshold.strip()
            if stripped == "":
                threshold = None
            else:
                try:
                    threshold = float(stripped)
                except ValueError:
                    errors.append(ValidationIssue(
                        "threshold", f"Threshold must be a number, got {raw_threshold!r}",
                    ))
                    threshold, bad_number = None, True
        else:
            threshold = raw_threshold
        if bad_number:
            pass  # already reported a precise "must be a number" error
        elif threshold is None:
            # No limit at all is not a check. Reachable once the Builder stops
            # coercing a blank field to 0 — an omitted value must never be
            # conflated with an authored 0, which is why the two fixes pair.
            errors.append(ValidationIssue(
                "threshold", "A numeric compare must declare a Threshold",
            ))
        elif operator == ">=" and not threshold:
            # `>= 0` passes for every non-negative value — an unfinished
            # "width must be at least 900 mm" would report 100% compliance.
            # NOTE `> 0` is deliberately NOT blocked: it rejects 0 and is
            # exactly what the legacy `positive_number` requirement migrates
            # to (see config/rules.va_bim.yaml — "area must be positive").
            errors.append(ValidationIssue(
                "threshold",
                "A `>=` compare with threshold 0 passes for every non-negative "
                "value — declare the real limit (or use `>` for 'must be positive').",
            ))
        elif operator in ("==", "!=") and not threshold:
            warnings.append(
                f"Threshold is 0 for a `{operator}` compare — valid, but "
                "double-check it is the limit you meant."
            )
    # `matches_regex_if_present` belongs here too: with an empty pattern it
    # fullmatches against "", so every non-empty value FAILS — an always-fail
    # noise flood rather than a silent pass, but just as wrong.
    if rule.get("requirement") in (
        "matches_regex", "not_matches_regex", "matches_regex_if_present",
    ):
        if not str(rule.get("pattern") or "").strip():
            errors.append(ValidationIssue(
                "pattern", "A match-pattern requirement must declare a Pattern",
            ))
    # Compile every authored regex at save time. A bad MAIN pattern at least
    # surfaces per element as manual_review (qc.py M-a); a bad SCOPE pattern
    # surfaces as manual_review at run time (`qc._scope_result` → "unknown"),
    # but blocking it at save is the earlier, clearer gate.
    for field_name, raw_pattern in (
        ("pattern", rule.get("pattern")),
        ("scope_filter.pattern", (rule.get("scope_filter") or {}).get("pattern")),
    ):
        text = str(raw_pattern or "").strip()
        if not text:
            continue
        try:
            re.compile(text)
        except re.error as exc:
            errors.append(ValidationIssue(
                field_name, f"Not a valid regular expression: {exc}",
            ))
    # v1.4-K16: a normalize fix must be fully specified, else it silently yields
    # None for every element (empty "→ proposed"). Block save until filled.
    af = rule.get("autofill") or {}
    if af.get("strategy") == "normalize":
        nk = af.get("normalize_kind")
        if nk == "template" and not (
            str(af.get("normalize_source") or "").strip()
            and str(af.get("normalize_format") or "").strip()
        ):
            errors.append(ValidationIssue(
                "autofill.normalize_source",
                "normalize_kind=template needs a **Source regex** + **Target "
                "template** (empty → every proposal comes out None)",
            ))
        elif nk == "map" and not (af.get("normalize_map") or {}):
            errors.append(ValidationIssue(
                "autofill.normalize_map",
                "normalize_kind=map needs at least one row in the Mapping table",
            ))
        elif nk == "auto" and not str(rule.get("pattern") or "").strip():
            errors.append(ValidationIssue(
                "pattern", "normalize_kind=auto needs a **Pattern** for the engine to pick a result",
            ))
    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
