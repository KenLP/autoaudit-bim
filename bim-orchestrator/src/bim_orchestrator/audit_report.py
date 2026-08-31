"""v1 report module — the verification report renderer (Markdown, canonical).

``render_audit_report`` turns one finished run's recorded artifacts into a
human-readable report that answers a skeptical BIM Director's question: *"Is what
the AI did accurate and trustworthy, and how do I check it MYSELF?"*

Design contract:
* **Renders, never re-derives.** Every verdict comes from ``state["check_trace"]``
  (captured by QC during the run) joined with ``state["proposed_fixes"]``. The
  report does not re-run a single check — a second evaluation could disagree and
  give two sources of truth. *Verification* is the USER reproducing a claim in
  native Revit/ACC tools, guided by the per-rule recipe.
* **General, not rule-specific.** The verify recipe per rule comes from the
  requirement-type registry in :mod:`verify_recipes`; nothing is hardcoded to
  fire ratings or doors.
* **Honest.** All four QC buckets are reported (incl. ``missing_data`` and
  ``manual_review``), plus a "What we did NOT touch, and why" section. The PASS
  set is listed alongside the failures so a reviewer can audit for false
  negatives.

Pure module (string in, string out). Layout sections:
  1. Executive summary   2. Trust ladder   3. Per-rule verification
  4. Per-element appendix 5. What we did NOT touch  6. Audit trail
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from bim_orchestrator.report_trace import (
    detect_fix_interactions,
    index_fixes,
    normalize_outcome,
    outcome_for,
    rule_view_from_record,
)
from bim_orchestrator.reports import _compliance_pct, render_axes_section
from bim_orchestrator.state import CheckRecord
from bim_orchestrator.verify_recipes import VerifyRecipe, recipe_for

# Cap table sizes so a 10k-element run stays readable; the full data is always in
# report_trace.json. Truncation is ANNOUNCED in the report (silent caps read as
# "covered everything" when they didn't).
_MAX_TABLE_ROWS = 50
_MAX_APPENDIX_ROWS = 150
_MAX_SELECT_IDS = 400

_STATUS_LABEL = {
    "compliant": "Compliant",
    "non_compliant": "Non-compliant",
    "manual_review": "Needs human review",
    "missing_data": "Missing data",
}

# v1.5-R6 (1.1f): a bare "—" reads as "nothing happened", when it usually
# means "no fix was attached to this record" — which can legitimately be
# because the finding was dropped by the issue budget, or genuinely never
# routed anywhere. Always say which, in words.
_NO_ACTION = "no action recorded — see review queue / issue budget"


# ── small formatting helpers ─────────────────────────────────────────────────


def _cell(v: Any) -> str:
    """Markdown-table-safe cell: blanks become ``(empty)``, pipes/newlines escaped."""
    if v is None:
        return "(empty)"
    s = str(v)
    if not s.strip():
        return "(empty)"
    return s.replace("|", "\\|").replace("\n", " ")


def _sev(s: str | None) -> str:
    if not s:
        return ""
    return s.replace("severity_", "").upper() if s.startswith("severity_") else s.upper()


def _first_sentence(text: str | None, *, limit: int = 320) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 3] + "..."
    return flat


def _requirement_phrase(rule: Any) -> str:
    """One-line plain-language statement of what a rule demands."""
    req = getattr(rule, "requirement", "")
    param = getattr(rule, "parameter", "?")
    op = getattr(rule, "operator", None) or ">="
    threshold = getattr(rule, "threshold", None)
    unit = getattr(rule, "unit", None)
    pattern = getattr(rule, "pattern", None)
    unit_s = f" {unit}" if unit else ""
    if req == "present_and_nonempty":
        return f"`{param}` must be present and non-empty."
    if req in ("numeric_compare", "numeric_min", "numeric_min_conditional"):
        return f"`{param}` must be {op} {threshold}{unit_s}."
    if req == "positive_number":
        return f"`{param}` must be greater than 0."
    if req in ("matches_regex", "matches_regex_if_present"):
        return f"`{param}` must match the pattern `{pattern}`."
    if req == "not_matches_regex":
        return f"`{param}` must NOT match the pattern `{pattern}`."
    if req == "canonical_format":
        return f"`{param}` must already be in its canonical form."
    if req == "unique_in_set":
        return f"`{param}` must be unique across all in-scope elements."
    if req in ("relation_compare", "fire_rating_ge"):
        lookup = getattr(rule, "lookup", None)
        if lookup:
            return (
                f"`{param}` must be {op} the value a code table (`{lookup}`) "
                f"requires for the related element."
            )
        other = getattr(rule, "other_param", None) or "the related element's value"
        return f"`{param}` must be {op} `{other}`."
    return f"`{param}` must satisfy `{req}`."


def _llm_tag(outcome: dict[str, Any]) -> str:
    """v1.5-R6 (1.1d): '(LLM-proposed)' provenance tag — the value came from
    the Remediation agent (``preview.value_source == "llm"``), not a
    deterministic pipeline. Callers append this after the main outcome text."""
    return " (LLM-proposed)" if outcome.get("value_source") == "llm" else ""


def _outcome_cell(outcome: dict[str, Any] | None) -> str:
    """Render the Design/Result outcome for one finding row."""
    if outcome is None:
        return _NO_ACTION
    path = outcome.get("path")
    if path == "human_only":
        # v1.5-R6 (1.1c): a human-only (LLM safety-critical) fix must never
        # read as a pending Revit write — the whole point of the gate is that
        # it will NEVER be auto-applied.
        did = outcome.get("issue_display_id")
        if did:
            st = outcome.get("issue_status") or "open"
            return (
                f"human-only → ACC Issue #{did} ({st}) (requires manual write)"
                + _llm_tag(outcome)
            )
        return "human-only (requires manual write) — awaiting ACC issue" + _llm_tag(outcome)
    if path == "A":
        did = outcome.get("issue_display_id")
        st = outcome.get("issue_status") or "open"
        return f"ACC Issue #{did} ({st})"
    if path == "B":
        before, after = outcome.get("before"), outcome.get("after")
        # v1.5-R6 (2.3): the `[revit_batch]`/`[per_element]` transport tag used
        # to ride in every cell — jargon that means nothing without the legend.
        # Dropped from the cell; the legend (top of §3) explains once that
        # writes may be batched or per-element, and the per-rule Auto-fixed
        # table (1.2) still has an explicit "Via" column for anyone who wants it.
        tag = "Revit write" if outcome.get("executed") else "Revit write (pending)"
        return f"{tag}: {_cell(before)} → {_cell(after)}" + _llm_tag(outcome)
    if path == "B-proposal":
        after = outcome.get("after")
        pid = outcome.get("proposal_issue_id")
        return (
            f"proposed → {_cell(after)} (awaiting approval, issue `{pid}`)"
            + _llm_tag(outcome)
        )
    if path == "parked":
        return f"parked: {outcome.get('reason')}"
    return _NO_ACTION


def _is_executed_auto_fix(outcome: dict[str, Any] | None) -> bool:
    """v1.5-R6 (polish round 2, N6): true when ``outcome`` is a Path B write
    that was actually committed this run — i.e. the record it's attached to
    re-queries as ``compliant`` only BECAUSE of that write, not because it
    was always fine. Shared by the PASS-set row renderer and the appendix so
    both call out "this passed because we just fixed it", not a bare
    unqualified "✓ pass" that reads as "nothing happened here"."""
    return bool(outcome) and outcome.get("path") == "B" and bool(outcome.get("executed"))


def _select_by_id_line(records: list[CheckRecord]) -> str:
    """Comma-separated Revit ElementId list for 'Manage → Select by ID'."""
    revit_ids = [
        str(r["revit_element_id"]) for r in records
        if r.get("revit_element_id") is not None
    ]
    acc_ids = [r["element_id"] for r in records if r.get("revit_element_id") is None]
    parts: list[str] = []
    if revit_ids:
        shown = revit_ids[:_MAX_SELECT_IDS]
        extra = len(revit_ids) - _MAX_SELECT_IDS
        suffix = "" if extra <= 0 else f" … (+{extra} more)"
        parts.append(
            "Revit **Manage → Inquiry → Select by ID** (paste): `"
            + ", ".join(shown) + "`" + suffix
        )
    if acc_ids:
        parts.append(
            f"{len(acc_ids)} ACC/AECDM element(s) (URN, not a Revit id) — open in "
            "the ACC model viewer instead."
        )
    return "  \n".join(parts) if parts else "_(no element ids)_"


def _rule_anchor(rule_id: str) -> str:
    """Stable HTML anchor id for a rule section — used both to STAMP the
    anchor (an explicit ``<a id=...>`` right before the ``### Rule`` heading)
    and to LINK to it from the executive summary / TOC (2.1b, 2.5). An
    explicit anchor is used instead of relying on a renderer's own heading→
    slug algorithm (GitHub / pandoc / VS Code all slugify differently, and a
    rule id's dots/colons would be mangled inconsistently) — raw HTML anchors
    are honoured by every renderer that matters here."""
    return "rule-" + "".join(c if c.isalnum() else "-" for c in rule_id.lower())


def _group_by_rule(check_trace: list[CheckRecord]) -> OrderedDict[str, list[CheckRecord]]:
    grouped: OrderedDict[str, list[CheckRecord]] = OrderedDict()
    for rec in check_trace:
        grouped.setdefault(rec["rule_id"], []).append(rec)
    return grouped


def _rule_id_for_fix(f: dict[str, Any]) -> str | None:
    """v1.5-R6 (polish round 2, N2): resolve the rule a fix belongs to for the
    Executive Summary's 'Issues by rule' / 'Proposals by rule' lines.

    A per-element Path B fix stamps ``preview.rule_id`` directly. A Path A
    GROUPED issue (``DesignAgent._propose_rule_group``) does NOT — it stamps
    ``preview.grouped_rule_ids`` instead, and its ``finding_id`` has the shape
    ``"rulegroup::<rule_id>::<bucket>"``. Naively splitting that finding_id on
    the FIRST ``::`` (the previous behaviour) returns the literal word
    "rulegroup" — a meaningless label with a dead anchor. Prefer the
    structured ``grouped_rule_ids`` field; fall back to parsing the
    ``rulegroup::`` shape (mirrors ``report_trace._split_finding_id``) only
    when it isn't present.
    """
    preview = f.get("preview") or {}
    rid = preview.get("rule_id")
    if rid:
        return str(rid)
    grouped = preview.get("grouped_rule_ids")
    if grouped:
        return str(grouped[0])
    finding_id = f.get("finding_id", "")
    if finding_id.startswith("rulegroup::"):
        parts = finding_id.split("::")
        return "::".join(parts[1:-1]) if len(parts) >= 3 else None
    return finding_id.split("::", 1)[0] or None


def _write_set_key(f: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """v1.5-R6 (polish round 2, N1): a stable identity for the REAL-WORLD
    write a Path B fix represents — ``(element_id, action, parameter,
    new_value)``. ``DesignAgent._create_proposal_issue``'s in-run memo-hit
    branch stamps the SAME ``proposal_issue_id`` onto a later iteration's
    re-proposal of an unchanged write-set (so the ApprovalWatcher record
    stays correct), but that later iteration's ``ProposedFix`` objects are
    STILL appended to ``state["proposed_fixes"]`` alongside the originals —
    the same write ends up counted twice in an "Awaiting approval (N)" tally
    that iterates ``proposed_fixes`` directly. Dedup by this key before
    counting so a re-propose of the identical write is counted once."""
    preview = f.get("preview") or {}
    return (f.get("element_id"), preview.get("action"), f.get("parameter"), f.get("new_value"))


# ── main entry point ─────────────────────────────────────────────────────────


def render_audit_report(
    state: dict[str, Any],
    *,
    rules: Any = None,
    run_id: str | None = None,
    mode: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    rules_path: str | None = None,
    trace_filename: str = "trace.md",
    banner: str | None = None,
    provenance: dict[str, Any] | None = None,
    max_elements: int | None = None,
    axes: dict[str, Any] | None = None,
    run_status: str | None = None,
) -> str:
    """Render the verification report.

    ``banner`` (2.4) — an optional caller-supplied warning line rendered right
    under the title (e.g. --demo's "simulated model" notice). ``provenance``
    (2.2) — a dict the ORCHESTRATOR captured at run time (tool version, user,
    machine, rules file hashes, ...); this function only renders it, never
    computes it (renders-never-re-derives extends to provenance too — the
    renderer must not call ``getpass``/``platform`` itself, or a later
    re-render from a saved ``report_trace.json`` would report the WRONG
    machine/user). ``max_elements`` (2.1a) — the ``--max-elements`` cap that
    was in effect, for the Coverage line; None when not applicable (e.g. a
    Forma-only run with no such cap).
    """
    check_trace: list[CheckRecord] = list(state.get("check_trace") or [])
    summary = state.get("outcomes_summary") or {}
    proposed_fixes = state.get("proposed_fixes") or []
    geometry_findings = state.get("geometry_findings") or []
    by_element, by_rule_bucket = index_fixes(proposed_fixes)
    rules_by_id = {r.id: r for r in rules.rules} if rules is not None else {}

    grouped = _group_by_rule(check_trace)
    L: list[str] = []

    # ── header ──
    L.append(f"# Verification report — {run_id or '(no run id)'}")
    if banner:
        L.append("")
        L.append(f"> {banner}")
    L.append("")
    has_project_in_provenance = _render_provenance(L, provenance)
    meta = [
        # v1.5-R6 (polish round 2, N4): when Provenance already named the
        # project (as "Project id"), don't repeat the identical value here
        # under a different label a few lines down.
        ("Project", None if has_project_in_provenance else state.get("project_id")),
        ("Mode", mode),
        ("Started", started_at),
        ("Finished", finished_at),
        ("Duration", f"{duration_seconds:.2f}s" if duration_seconds is not None else None),
        ("Iterations", state.get("iteration")),
        ("Rules", f"`{rules_path}`" if rules_path else None),
        # P1-07: the SAME status metadata.json records. `state["status"]` is the
        # graph's internal loop state — after `check()` QC leaves it "designing"
        # while the run recorder writes "completed", so the two artifacts of one
        # run contradicted each other for anyone comparing them. The caller
        # passes the recorded status; state is the fallback for direct callers.
        ("Final status", run_status or state.get("status")),
    ]
    for label, val in meta:
        if val is not None:
            L.append(f"- **{label}:** {val}")
    L.append("")

    _render_toc(L, grouped, bool(geometry_findings), has_axes=bool(axes))
    _render_executive_summary(
        L, state, summary, grouped, rules_by_id, by_element, by_rule_bucket,
        max_elements=max_elements,
    )
    _render_trust_ladder(L)
    _render_per_rule(L, grouped, rules_by_id, by_element, by_rule_bucket)
    _render_geometry(L, geometry_findings, by_rule_bucket)
    # P3-1: IFC audit axes — rendered from the run folder's saved envelopes
    # (axes/lod.json + axes/spatial.json), NEVER by re-invoking a satellite.
    L.extend(render_axes_section(axes, heading="## 3c. Audit axes (IFC satellites)"))
    _render_appendix(L, check_trace, by_element, by_rule_bucket)
    _render_not_touched(L, state, summary, proposed_fixes, geometry_findings)
    _render_audit_trail(L, trace_filename)
    return "\n".join(L)


_HASH_VERIFY_HINT = "Verify: PowerShell `Get-FileHash <file> -Algorithm SHA256`"


def _render_provenance(L: list[str], provenance: dict[str, Any] | None) -> bool:
    """v1.5-R6 (2.2): §0 — who/what/when produced this report, so a reviewer
    can tell a demo run from a production one and knows which rules file (by
    hash) was actually checked. Pure render — every value is read from
    ``provenance`` as captured by the orchestrator; nothing computed here.

    Returns whether a ``project_id`` line was rendered (polish round 2, N4) —
    the caller uses this to skip the redundant "Project" line in the meta
    block right below, which used to repeat the exact same value under a
    different label.
    """
    if not provenance:
        return False
    L.append("## 0. Provenance")
    L.append("")
    order = [
        ("tool_version", "Tool version"),
        ("user", "Run by"),
        ("machine", "Machine"),
        ("captured_at", "Captured at"),
        ("project_id", "Project id"),
        ("run_id", "Run id"),
        ("elements_fetched", "Elements fetched"),
    ]
    has_project = False
    for key, label in order:
        val = provenance.get(key)
        if val is not None:
            L.append(f"- **{label}:** {val}")
            if key == "project_id":
                has_project = True
    doc = provenance.get("document")
    if doc:
        model_val = f"`{doc.get('title', '?')}`"
        if doc.get("path"):
            model_val += f" — `{doc['path']}`"
        L.append(f"- **Model:** {model_val}")
        if doc.get("revit_version_name"):
            L.append(f"- **Revit:** {doc['revit_version_name']}")
        if doc.get("is_workshared") is not None:
            L.append(f"- **Workshared:** {'yes' if doc['is_workshared'] else 'no'}")
        if doc.get("is_modified"):
            L.append(
                "- ⚠ **Unsaved changes at capture time** — this report describes "
                "the in-session model state, not the saved file."
            )
    rules_files = provenance.get("rules_files") or []
    any_sha = False
    for rf in rules_files:
        path = rf.get("path", "?")
        sha = rf.get("sha256")
        L.append(f"- **Rules file:** `{path}`" + (f" — SHA-256 `{sha}`" if sha else ""))
        any_sha = any_sha or bool(sha)
    if any_sha:
        L.append(f"  - {_HASH_VERIFY_HINT}")
    L.append("")
    return has_project


def _render_toc(
    L: list[str], grouped: OrderedDict[str, list[CheckRecord]], has_geometry: bool,
    *, has_axes: bool = False,
) -> None:
    """v1.5-R6 (2.5): a flat table of contents, anchored — the report can run
    to hundreds of rules; without this a reviewer scrolls blind."""
    L.append("## Contents")
    L.append("")
    L.append("1. [Executive summary](#1-executive-summary)")
    L.append("2. [How to trust this report](#2-how-to-trust-this-report)")
    L.append("3. [Per-rule verification](#3-per-rule-verification)")
    for rule_id in grouped:
        L.append(f"   - [`{rule_id}`](#{_rule_anchor(rule_id)})")
    if has_geometry:
        L.append("   - [Geometry / clearance findings](#3b-geometry--clearance-findings)")
    if has_axes:
        L.append("   - [Audit axes (IFC satellites)](#3c-audit-axes-ifc-satellites)")
    L.append("4. [Per-element appendix](#4-per-element-appendix-query--qc--design--result)")
    L.append("5. [What we did NOT touch, and why](#5-what-we-did-not-touch-and-why)")
    L.append("6. [Audit trail](#6-audit-trail)")
    L.append("")


def _render_llm_status(L: list[str], state: dict[str, Any]) -> None:
    """L2-12: was an AI asked to take part, and did it?

    Sibling of ``_render_query_coverage``: pure render, one decision upstream.
    Three situations used to produce a document identical to a plain Phase-1
    run — a flag value the loader did not recognise, a flag that was on with no
    agent behind it, and a ruleset whose ``llm_propose`` rules had nothing to
    serve them. In all three the findings are correct and the audit is valid;
    what was missing is that the reader could not tell "the AI proposed
    nothing" from "the AI was never asked", which is the difference between a
    clean model and an unrun feature.
    """
    status = state.get("llm_status")
    if not isinstance(status, Mapping) or not status:
        return
    requested = status.get("requested") or {}
    wired = status.get("wired") or {}
    on = [name for name, want in requested.items() if want]
    live = [name for name, ok in wired.items() if ok]
    L.append(
        "- **AI assistance:** requested: "
        + (f"**{', '.join(sorted(on))}**" if on else "**none**")
        + " · active: "
        + (f"**{', '.join(sorted(live))}**" if live else "**none**")
    )
    for p in status.get("flag_problems") or []:
        L.append(
            f"  - ⚠️ `{p.get('flag')}={p.get('value')}` was not understood and "
            "was treated as **off** — this run is fully deterministic."
        )
    for name in sorted(on):
        if not wired.get(name):
            L.append(
                f"  - ⚠️ The **{name}** agent was requested but could not be "
                "built (extension missing or incompatible); that part of the "
                "run did not happen."
            )
    if status.get("llm_rules_degraded_to_path_a"):
        rules = list(status.get("rules_requesting_llm") or [])
        L.append(
            f"  - Note: **{len(rules)}** rule(s) ask for an AI-proposed value with "
            "no remediation agent available, so their findings became issues "
            "for a person rather than proposed fixes: "
            + ", ".join(f"`{r}`" for r in rules[:10])
            + (" …" if len(rules) > 10 else "")
        )


def _render_geometry_coverage(L: list[str], state: dict[str, Any]) -> None:
    """P1-GEO-01/02 + P1-RENDER-02: did the geometry checks actually run, and
    was the result complete?

    Pure render — the agent decides what executed, this only reports it. Three
    things the reader could not previously learn from this document:

      * a rule that CRASHED looked identical to one that found nothing;
      * a rule whose linked model never resolved silently answered about the
        HOST model instead, and reported that answer as the one asked for;
      * a clash list cut off at ``maxResults`` was presented as a total, so
        "500 clashes" read as exact when the truth was "at least 500".
    """
    cov = state.get("geometry_coverage")
    if not isinstance(cov, Mapping) or not cov:
        return
    executed = list(cov.get("rules_executed") or [])
    failed = list(cov.get("rules_failed") or [])
    requested = cov.get("rules_requested") or (len(executed) + len(failed))
    L.append(
        f"- **Geometry coverage:** rules requested: **{requested}** · "
        f"executed: **{len(executed)}** · did NOT run: **{len(failed)}**"
    )
    if failed:
        L.append(
            "  - ❌ **These geometry rules produced NO result.** Their absence "
            "from the findings below means *not checked*, not *no clashes*:"
        )
        for item in failed:
            reason = str(item.get("reason", "unknown"))
            line = f"    - `{item.get('rule_id')}` — {reason}"
            if reason in ("link_not_found", "link_discovery_failed"):
                # Name the scope that was asked for; that is what the author
                # has to fix, and it is invisible from the rule id alone.
                src = item.get("reference_source") or item.get("link_hint") or "?"
                line += (
                    f" (wanted the `{src}` model; the rule was NOT re-pointed "
                    "at the host — that would answer a different question)"
                )
                available = item.get("available") or []
                if available:
                    line += f" · links present: {', '.join(map(str, available))}"
            elif item.get("detail"):
                line += f" — {item['detail']}"
            L.append(line)
    for item in cov.get("truncated") or []:
        L.append(
            f"  - ⚠️ **`{item.get('rule_id')}`: AT LEAST {item.get('returned')} "
            f"violations — the result was capped at {item.get('cap')}.** The "
            "count below is a floor, not a total; narrow the scope or raise "
            "the cap before using it to size remediation."
        )
    unsupported = list(cov.get("rules_unsupported") or [])
    if unsupported:
        L.append(
            f"  - ℹ️ Not dispatched by this version (check type unsupported): "
            f"{', '.join(f'`{r}`' for r in unsupported)}."
        )


def _render_design_options(L: list[str], state: dict[str, Any]) -> None:
    """F-02: say up front how much of this audit describes an ALTERNATIVE.

    A Revit design option holds one of several layouts being explored; at most
    one per set is ever built. Those elements come back from the model looking
    exactly like real ones, so without this line a reader has no way to tell
    that some of the findings below describe a design that may never exist.
    Live on Snowdon, 6 of 149 doors sat in an option and produced 24 check
    records — 10 of them failures — with nothing anywhere saying so.

    Pure render: it counts records that already carry ``design_option``; the
    query layer decides what is an option element, this only reports it.
    """
    counts: dict[str, int] = {}
    fails = 0
    for rec in state.get("check_trace") or []:
        option = rec.get("design_option") if isinstance(rec, Mapping) else None
        if not option:
            continue
        counts[str(option)] = counts.get(str(option), 0) + 1
        if not rec.get("passed"):
            fails += 1
    if not counts:
        return
    total = sum(counts.values())
    names = ", ".join(f"`{o}` ({n})" for o, n in sorted(counts.items()))
    L.append(
        f"- **Design options:** **{total}** of the checks below were run on "
        f"elements inside a design option — {names}"
        + (f", of which **{fails}** failed" if fails else "")
        + ". A design option is an ALTERNATIVE: at most one option per set is "
        "ever built, so these findings may describe a layout that will never "
        "exist. Rows for them are marked `⧉ option:` in the tables."
    )


def _coverage_defect(cov: Any) -> str | None:
    """A-02: name the first structural problem in ``query_coverage``, or None.

    Returned text goes STRAIGHT into the report, so it has to read as evidence
    a human can act on ("targets_requested is int, expected list"), not as a
    stack trace. Only shape is checked — whether the categories are *correct*
    is the query agents' business, not the renderer's.
    """
    if not isinstance(cov, Mapping):
        return f"query_coverage is {type(cov).__name__}, expected mapping"
    for field in ("targets_requested", "categories_resolved", "categories_dropped"):
        val = cov.get(field)
        # A bare string is the nastiest case: it IS iterable and has a len(),
        # so it would render one bogus "category" per character instead of
        # failing. Excluded explicitly.
        if val is None:
            continue
        if isinstance(val, (str, bytes)) or not isinstance(val, Sequence):
            return f"{field} is {type(val).__name__}, expected list"
    return None


def _coverage_dropped(entry: Any) -> tuple[str, str]:
    """A-02: one dropped-category entry as ``(category, reason)``.

    The contract is ``{"category": ..., "reason": ...}``, but a bare string is
    tolerated (it names the category, reason unknown) rather than crashing —
    losing the reason is survivable, losing the whole report is not.
    """
    if isinstance(entry, Mapping):
        return str(entry.get("category")), str(entry.get("reason"))
    return str(entry), "reason not recorded"


def _render_query_coverage(L: list[str], state: dict[str, Any]) -> None:
    """P1-07: which target categories were actually queried.

    `query_coverage` (stamped by the query agents) reached `metadata.json` and
    the exit code, but never the report — so a run whose categories all failed
    to resolve read as a full audit to whoever signs the PDF. Coverage lives in
    the state; this only renders it (renders-never-re-derives).
    """
    cov = state.get("query_coverage")
    if not cov:
        return                                    # Forma-only / legacy run
    # A-02 (2026-07-25 live review): this block was written that same day and
    # had only ever met well-formed input. A `categories_dropped` holding
    # strings instead of dicts raised AttributeError; a non-list
    # `targets_requested` raised TypeError — and either one killed the WHOLE
    # report. Coverage is the section that says whether the audit covered the
    # model at all, so the failure mode was: malformed scope data destroys the
    # very document that would have disclosed it. Degrade LOUDLY instead —
    # never crash, and never fall silent, because silence here reads as
    # "scope was fine".
    problem = _coverage_defect(cov)
    if problem:
        L.append(
            "- **Query coverage:** ⚠️ **COVERAGE DATA IS MALFORMED** "
            f"({problem}) — the audited scope CANNOT be certified from this "
            "run. Treat everything below as covering an unknown subset."
        )
        return
    requested = list(cov.get("targets_requested") or [])
    resolved = list(cov.get("categories_resolved") or [])
    dropped = [_coverage_dropped(d) for d in (cov.get("categories_dropped") or [])]
    if not requested and not dropped:
        return
    L.append(
        f"- **Query coverage:** requested: **{len(requested)}** "
        f"({', '.join(map(str, requested)) or '—'}) · "
        f"resolved: **{len(resolved)}** ({', '.join(map(str, resolved)) or '—'})"
    )
    if dropped:
        names = ", ".join(cat for cat, _ in dropped)
        if not resolved:
            L.append(
                "  - ❌ **NO AUDIT WAS PERFORMED.** Every target category failed "
                f"to resolve ({names}), so no element was fetched or checked. "
                "This report describes an audit that did not run — it must not "
                "be used as evidence of compliance."
            )
        else:
            L.append(
                f"  - ⚠️ **PARTIAL COVERAGE.** Not audited: **{names}**. Findings "
                "below cover the resolved categories ONLY; elements in the "
                "dropped categories were never fetched."
            )
        for cat, reason in dropped:
            L.append(f"    - `{cat}` — {reason}")

    # F-01: a category that RESOLVED but came back with zero elements. Its
    # rules ran against nothing, yet everything above this line reads like a
    # completed audit — "resolved" only ever meant the catalog knew the label.
    empty = [str(c) for c in (cov.get("categories_empty") or [])]
    if empty:
        L.append(
            f"  - ⚠️ **CHECKED NOTHING: {', '.join(empty)}.** "
            "These categories resolved, but the model returned **0 elements** "
            "for them, so their rules evaluated nothing. A clean result here "
            "is not evidence of compliance — it is the absence of evidence."
        )
        links = [str(m) for m in (cov.get("linked_models") or [])]
        if links:
            # The most likely explanation in a federated project, stated
            # rather than left for the reader to guess.
            L.append(
                f"    - This model has **{len(links)} loaded link(s)** "
                f"({', '.join(links)}). Parameter rules read the HOST document "
                "only — if those elements live in a link, point the rule at "
                "that model instead."
            )


_STATUS_EXPLAIN = {
    "converged": (
        "converged = the final iteration re-checked every previously-fixed "
        "element and found no NEW findings — the loop stopped because it ran "
        "out of work, not because it gave up."
    ),
    "failed": (
        "failed = the run hit `max_iterations` (or a hard error) while "
        "findings were still changing — treat the results below as a "
        "snapshot, not a finished pass."
    ),
}

# v1.5-R6 (2.1c): severity sort — HIGH first, unset last.
_SEVERITY_RANK = {"severity_high": 0, "severity_medium": 1, "severity_low": 2}


def _rule_severity(rule: Any, recs: list[CheckRecord]) -> str | None:
    """A rule's severity is effectively constant across its records (it's
    resolved once from `rule.severity_level`/`severity_tag`, the same for
    every element) — prefer the rule's own attribute, fall back to the first
    record that carries one (self-contained-trace re-render, no RuleSet)."""
    lvl = getattr(rule, "severity_level", None)
    if lvl:
        return str(lvl)
    for r in recs:
        if r.get("severity"):
            return str(r["severity"])
    return None


def _render_executive_summary(
    L: list[str], state: dict[str, Any], summary: dict[str, Any],
    grouped: OrderedDict[str, list[CheckRecord]], rules_by_id: dict[str, Any],
    by_element: dict[Any, Any], by_rule_bucket: dict[Any, Any],
    *, max_elements: int | None = None,
) -> None:
    total = summary.get("total", 0)
    nc = summary.get("non_compliant", 0)
    mr = summary.get("manual_review", 0)
    md = summary.get("missing_data", 0)
    compliant = summary.get("compliant", 0)
    skipped = summary.get("skipped_out_of_scope", 0)
    check_trace = state.get("check_trace") or []
    n_elements = len({r["element_id"] for r in check_trace})
    n_fetched = len(state.get("elements") or [])

    # P1-GEO-01 / P1-RENDER-01: the headline used to summarise `check_trace`
    # ONLY — i.e. parameter rules. Geometry findings ride a separate bucket
    # (v1.4-K7), so a geometry-only run with real clashes opened with
    # "nothing checked" and contradicted itself two sections later. And a
    # geometry half that never EXECUTED must never be summarised as a pass.
    # Read the shared bucket directly — DesignAgent returns `{**state, ...}`,
    # so `geometry_findings` survives the design pass on both the geometry-only
    # and the mixed path. No sniffing at `findings` for geometry-shaped
    # entries: a heuristic here would be a second source of truth about what
    # counts as a geometry violation, and the first one to drift.
    geo_findings = list(state.get("geometry_findings") or [])
    geo_cov = state.get("geometry_coverage") or {}
    geo_verdict = geo_cov.get("verdict") if isinstance(geo_cov, dict) else None

    L.append('<a id="1-executive-summary"></a>')
    L.append("## 1. Executive summary")
    L.append("")
    if geo_verdict == "no_audit":
        L.append(
            "**Headline: GEOMETRY AUDIT DID NOT RUN** — every geometry rule "
            "failed to execute, so no clash/clearance check was performed. "
            "This report is NOT evidence that the model is clash-free."
        )
    elif (nc + mr + md) == 0 and total > 0 and not geo_findings:
        L.append("**Headline: PASS** — every evaluated check is compliant.")
    elif total == 0 and geo_findings:
        L.append(
            f"**Headline: {len(geo_findings)} geometry violation(s)** — no "
            "parameter (element, rule) pairs were evaluated in this run; the "
            "findings below come from clash/clearance checks (§3b)."
        )
    elif total == 0:
        L.append("**Headline: nothing checked** — no (element, rule) pairs evaluated.")
    else:
        extra = (
            f" Plus **{len(geo_findings)}** geometry violation(s) (§3b)."
            if geo_findings else ""
        )
        L.append(
            f"**Headline: {nc + mr + md} of {total} checks need attention** "
            f"({nc} non-compliant, {mr} need human review, {md} missing data)."
            + extra
        )
    if geo_verdict == "partial":
        L.append(
            "⚠ **Partial geometry coverage** — some geometry rules did not "
            "execute; see the geometry coverage line below."
        )
    status = state.get("status")
    stop_reason = state.get("stop_reason")
    # L2-07: `_STATUS_EXPLAIN["converged"]` states the loop "stopped because it
    # ran out of work, not because it gave up". That is false when a supervisor
    # MODEL asked to stop with findings still open — and it sits directly under
    # the headline. Suppress the wrong sentence and say what happened instead.
    if status in _STATUS_EXPLAIN and stop_reason != "supervisor":
        L.append(f"_{_STATUS_EXPLAIN[status]}_")
    if stop_reason == "supervisor":
        L.append(
            "⚠ **Stopped early on a supervisor (LLM) directive** — NOT because "
            "the loop ran out of work. A language model judged further "
            "iterations unlikely to help; anything still open below was not "
            "shown to be un-fixable. Re-run without `BIM_LLM_SUPERVISOR` for a "
            "purely deterministic stop."
        )
    if stop_reason == "iteration_cap":
        # v1.5-R7 (cap-hit honesty): the loop hit max_iterations before the
        # fix set reached a stable fixpoint — say so right under the status
        # line, before a reader trusts the results below as finished.
        L.append(
            f"⚠ Stopped at the iteration cap (max_iterations="
            f"{state.get('max_iterations')}) — the fix set had NOT reached a "
            "stable fixpoint; results may depend on fix order. Re-run or "
            "raise the cap; see 'Fix interactions' in §5."
        )
    L.append("")

    # v1.5-R6 (2.1a): Coverage block — the gap between "elements fetched" and
    # "elements evaluated" is exactly what a skeptical reviewer asks first
    # ("did you even LOOK at everything?"); state it instead of making them
    # infer it from two other numbers.
    evaluability = f"{100.0 * (total - md) / total:.0f}%" if total else "n/a"
    cap_note = f" (cap `--max-elements`={max_elements})" if max_elements else ""
    L.append(
        f"- **Coverage:** Elements fetched: **{n_fetched}** · evaluated: "
        f"**{n_elements}**{cap_note} · checks: **{total}** · "
        f"evaluability: **{evaluability}** (= (total−missing)/total)"
    )
    if skipped:
        L.append(
            f"  - Skipped as out-of-scope (rule category / scope filter): "
            f"**{skipped}** (element, rule) pair(s) — never counted toward "
            f"`total`, listed for transparency only."
        )
    _render_query_coverage(L, state)
    _render_geometry_coverage(L, state)
    _render_design_options(L, state)
    _render_llm_status(L, state)
    L.append(
        f"- Rules: **{len(grouped)}** · Compliant: **{compliant}** "
        f"({_compliance_pct(summary)}) · Non-compliant: **{nc}** · "
        f"Needs human: **{mr}** · Missing data: **{md}**"
    )

    fixes = state.get("proposed_fixes") or []
    issue_by_rule: dict[str, set[Any]] = {}
    for f in fixes:
        preview = f.get("preview") or {}
        did = (preview.get("executed_issue") or {}).get("displayId")
        rid = _rule_id_for_fix(f)
        if did is not None:
            issue_by_rule.setdefault(rid or "?", set()).add(did)
    issues = sorted({d for ids in issue_by_rule.values() for d in ids})
    writes = sum(
        1 for f in fixes
        if f.get("executed") and "executed_via" in (f.get("preview") or {})
    )
    proposal_by_rule: dict[str, set[Any]] = {}
    for f in fixes:
        preview = f.get("preview") or {}
        pid = preview.get("proposal_issue_id")
        if pid is not None:
            proposal_by_rule.setdefault(preview.get("rule_id") or "?", set()).add(pid)
    proposals = len({p for ids in proposal_by_rule.values() for p in ids})
    L.append(
        f"- ACC Issues created: **{len(issues)}** · Revit auto-writes: "
        f"**{writes}** · Approve-gated proposals: **{proposals}**"
    )
    # v1.5-R6 (2.1b): link each count down to its rule section + name the
    # issue ids, instead of leaving the reviewer to search for them.
    if issue_by_rule:
        parts = [
            f"[`{rid}`](#{_rule_anchor(rid)}) (#" + ", #".join(str(d) for d in sorted(ids)) + ")"
            for rid, ids in sorted(issue_by_rule.items())
        ]
        L.append(f"  - Issues by rule: {', '.join(parts)}")
    if proposal_by_rule:
        parts = [
            f"[`{rid}`](#{_rule_anchor(rid)})" for rid in sorted(proposal_by_rule)
        ]
        L.append(f"  - Proposals by rule: {', '.join(parts)}")
    L.append("")

    # per-rule one-line scoreboard — Severity added, sorted HIGH first (2.1c)
    L.append(
        "| Rule | Category | Severity | Parameter | Compliant | Non-compliant "
        "| Needs human | Missing |"
    )
    L.append("|------|----------|----------|-----------|-----------|---------------|-------------|---------|")
    rows: list[tuple[int, str]] = []
    for rule_id, recs in grouped.items():
        rule = rules_by_id.get(rule_id) or rule_view_from_record(recs[0])
        cat = getattr(rule, "category", None) or recs[0].get("category") or "?"
        sev = _rule_severity(rule, recs)
        counts = {k: 0 for k in _STATUS_LABEL}
        for r in recs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        rank = _SEVERITY_RANK.get(sev or "", 99)
        rows.append((
            rank,
            f"| [`{rule_id}`](#{_rule_anchor(rule_id)}) | {cat} | {_sev(sev) or '-'} "
            f"| `{rule.parameter}` | {counts['compliant']} | "
            f"{counts['non_compliant']} | {counts['manual_review']} | {counts['missing_data']} |",
        ))
    for _rank, line in sorted(rows, key=lambda t: t[0]):
        L.append(line)
    L.append("")


def _render_trust_ladder(L: list[str]) -> None:
    L.append('<a id="2-how-to-trust-this-report"></a>')
    L.append("## 2. How to trust this report")
    L.append("")
    L.append(
        "This report **renders what the run recorded**; it does not re-run any "
        "check. Verify each claim yourself, in tools you already trust — in "
        "increasing rigour (a *trust ladder*):"
    )
    L.append("")
    L.append(
        "1. **Select by ID** (Revit) / model viewer (ACC) — the atomic, "
        "interpretation-free anchor; jump straight to the exact elements."
    )
    L.append(
        "2. **Schedule** — recreate the finding set as a native schedule (recipe "
        "per rule below); it lists the PASS set too, so you can check for false "
        "negatives."
    )
    L.append(
        "3. **View filter + colour override** — make the flagged set visually "
        "obvious in a view (where the check maps to a native filter)."
    )
    L.append(
        "4. **ACC Issue + audit chain** — cross-check each raised issue and, via "
        "Forma `meta_verify_audit_chain`, its tamper-evident audit entries."
    )
    L.append("")
    L.append(
        "> Lead with the **schedule + filter anchored on the ElementId list** — "
        "native, nothing new to learn. Where a check can't be one native filter, "
        "the recipe says so and falls back to Select-by-ID + an operands schedule."
    )
    L.append("")


def _filter_value_display(rule: Any, f: dict[str, Any]) -> str:
    """v1.5-R6 (polish round 2, N3): a schedule filter's ``value`` (built by
    ``verify_recipes``) is in the parameter's Revit STORAGE unit (e.g. feet)
    — printing that bare float next to a rule stated in mm reads as a raw
    number with no unit, not a deliberate conversion. When the rule carries
    both a ``unit`` and a ``threshold``, show the rule's own value+unit first
    (what a human actually wrote in YAML), then the storage value rounded to
    4 decimals with an explicit "internal/storage units" label. Falls back to
    the bare value for filters with no numeric threshold (``has_no_value``,
    a ``not_equals`` membership chain, …).

    The filter value goes over the wire as a STRING (a deliberate choice for
    the older-addin failure mode — see ``verify_recipes._filter_value_text``;
    since bridge v0.8.23 a number would also be accepted), so the numeric
    case is recognised by PARSING it, not by its Python type. That is even
    more load-bearing now that the wire legitimately admits both forms:
    type-sniffing here silently dropped the unit gloss the moment the wire
    type changed, leaving a bare feet value under a rule written in mm
    (LIVE 2026-08-01)."""
    if "value" not in f:
        return ""
    value = f["value"]
    unit = getattr(rule, "unit", None)
    threshold = getattr(rule, "threshold", None)
    numeric = _as_float(value)
    if unit and threshold is not None and numeric is not None:
        return (
            f" {threshold} {unit} (= {round(numeric, 4)} internal/storage "
            f"units — auto-schedule uses raw values)"
        )
    return f" {_cell(value)}"


def _as_float(value: Any) -> float | None:
    """``float(value)`` for a number or a numeric string; ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _render_one_rule(
    L: list[str], rule_id: str, recs: list[CheckRecord], rule: Any,
    recipe: VerifyRecipe, by_element: dict[Any, Any], by_rule_bucket: dict[Any, Any],
    *, reuse_pass_from: str | None = None,
) -> None:
    counts = {k: 0 for k in _STATUS_LABEL}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # v1.5-R6 (1.2): an auto-fixed element re-queries as COMPLIANT in the
    # FINAL check_trace (the whole point of the fix), so it never shows up in
    # `fails` below — but the write itself is a material fact a reviewer
    # needs to see, or "0 outstanding" silently reads as "nothing happened
    # here" when N elements actually got auto-corrected this run.
    auto_fixed: list[tuple[CheckRecord, dict[str, Any]]] = []
    for r in recs:
        if r["status"] != "compliant":
            continue
        outcome = outcome_for(r, by_element, by_rule_bucket)
        if outcome is not None and outcome.get("path") == "B" and outcome.get("executed"):
            auto_fixed.append((r, outcome))

    L.append(f'<a id="{_rule_anchor(rule_id)}"></a>')
    L.append(f"### Rule `{rule_id}`")
    L.append("")
    desc = _first_sentence(getattr(rule, "description", None))
    if desc:
        L.append(f"> {desc}")
        L.append("")
    L.append(f"- **In plain language:** {_requirement_phrase(rule)}")
    L.append(
        f"- **Outcome:** {counts['compliant']} compliant · {counts['non_compliant']} "
        f"non-compliant · {counts['manual_review']} need human · {counts['missing_data']} missing"
        + (f" · **{len(auto_fixed)} auto-fixed**" if auto_fixed else "")
    )
    L.append("")

    # ── verify recipe ──
    L.append("**How to verify this yourself (native):**")
    L.append("")
    L.append(recipe.narrative)
    L.append("")
    sched = recipe.schedule
    field_cols = ", ".join(f"`{f}`" for f in sched.fields)
    L.append(f"- **Schedule recipe:** Category = `{sched.category}`; fields = {field_cols}.")
    if sched.filters:
        # v1.5-R6 (3.2): filters are now structured {field, operator, value}
        # dicts (the exact shape configure_schedule accepts) — render them as
        # a readable "field operator value" line, ANDed, instead of a raw
        # Python repr.
        filter_text = " AND ".join(
            f"`{f.get('field')}` {f.get('operator')}" + _filter_value_display(rule, f)
            for f in sched.filters
        )
        L.append(
            f"  - Filters (auto-created schedule only — the manual recipe above "
            f"stays unfiltered): {filter_text}"
        )
    if sched.group_or_sort:
        L.append(f"  - {sched.group_or_sort}")
    if recipe.view_filter is not None:
        vf = recipe.view_filter
        L.append(
            f"- **View filter + colour:** rule `{vf.parameter}` **{vf.rule_text}** "
            f"→ override **{vf.color}**."
        )
    else:
        L.append("- **View filter:** _not expressible as one native filter rule_ — "
                 f"{recipe.degraded_reason} Use the Select-by-ID list + the "
                 f"\"Needs attention\" table below.")

    # ── FAIL / needs-human set (computed before Select-by-ID: 1.4) ──
    fails = [r for r in recs if r["status"] != "compliant"]

    # v1.5-R6 (1.4): the paste-ready list is the FLAGGED set (what a reviewer
    # actually needs to select in Revit) — the full scope (incl. the PASS set)
    # is offered separately so a false-negative audit is still one click away,
    # but it's no longer what a reviewer pastes by default.
    L.append(f"- **Select by ID:** {_select_by_id_line(fails)}")
    if len(fails) != len(recs):
        L.append(
            f"  - Full scope (audit false negatives): {_select_by_id_line(recs)}"
        )
    L.append("")

    # ── PASS set ──
    passes = [r for r in recs if r["status"] == "compliant"]
    op_cols = recipe.operand_columns
    L.append(f"**PASS set ({len(passes)})** — listed so you can audit for false negatives:")
    L.append("")
    if reuse_pass_from is not None:
        L.append(
            f"_Identical to rule [`{reuse_pass_from}`](#{_rule_anchor(reuse_pass_from)})'s "
            f"PASS set above — not repeated here to keep the report readable; "
            f"the element ids are unchanged._"
        )
    elif passes:
        _render_record_table(L, passes, rule, op_cols, by_element, by_rule_bucket, pass_set=True)
    else:
        L.append("_No compliant elements for this rule._")
    L.append("")

    L.append(f"**Needs attention ({len(fails)}):**")
    L.append("")
    if fails:
        _render_record_table(L, fails, rule, op_cols, by_element, by_rule_bucket, pass_set=False)
    elif auto_fixed:
        sample_r, sample_out = auto_fixed[0]
        delta = f"{_cell(sample_out.get('before'))} → {_cell(sample_out.get('after'))}"
        if len(auto_fixed) == 1:
            L.append(f"_0 outstanding — 1 auto-fixed this run ({delta}) (see below)._")
        else:
            L.append(f"_0 outstanding — {len(auto_fixed)} auto-fixed this run (see below)._")
    else:
        L.append("_Nothing flagged for this rule._")
    L.append("")

    # ── auto-fixed set (1.2) ──
    if auto_fixed:
        L.append(f"**Auto-fixed ({len(auto_fixed)}):**")
        L.append("")
        L.append("| Element | ElementId | Old → New | Via |")
        L.append("|---------|-----------|-----------|-----|")
        for r, outcome in auto_fixed:
            name = r.get("element_name") or "(unnamed)"
            eid = r.get("revit_element_id")
            eid_cell = str(eid) if eid is not None else _cell(r["element_id"])
            before, after = outcome.get("before"), outcome.get("after")
            via = outcome.get("executed_via") or "revit write"
            L.append(
                f"| {_cell(name)} | {eid_cell} | {_cell(before)} → {_cell(after)} | {via} |"
            )
        L.append("")


def _render_record_table(
    L: list[str], records: list[CheckRecord], rule: Any, op_cols: list[str],
    by_element: dict[Any, Any], by_rule_bucket: dict[Any, Any], *, pass_set: bool,
) -> None:
    # Column set: identity + value (+ operand/threshold/expected) + verdict/outcome
    extra_hdr = []
    if "operand" in op_cols:
        extra_hdr.append("Required/related")
    if "threshold" in op_cols:
        extra_hdr.append("Threshold")
    if "suggested_value" in op_cols and not pass_set:
        extra_hdr.append("Canonical")
    if "pattern" in op_cols:
        extra_hdr.append("Pattern")
    last_col = "Verdict" if pass_set else "Status → outcome"
    header = ["Element", "ElementId", f"`{rule.parameter}`", *extra_hdr, last_col]
    L.append("| " + " | ".join(header) + " |")
    L.append("|" + "|".join(["---"] * len(header)) + "|")

    shown = records[:_MAX_TABLE_ROWS]
    for r in shown:
        name = r.get("element_name") or "(unnamed)"
        # F-02: mark the element itself, not just a summary line. Someone
        # reading one row must be able to see that this finding describes a
        # design ALTERNATIVE — a layout that may never be built — without
        # cross-referencing anything.
        if r.get("design_option"):
            name = f"{name} ⧉ option: {r['design_option']}"
        eid = r.get("revit_element_id")
        eid_cell = str(eid) if eid is not None else _cell(r["element_id"])
        value_cell = _cell(r.get("value_display", r.get("raw_value")))
        row = [_cell(name), eid_cell, value_cell]
        if "operand" in op_cols:
            row.append(_cell(r.get("operand")) + (" · exempt" if r.get("exempt") else ""))
        if "threshold" in op_cols:
            row.append(_cell(r.get("threshold")))
        if "suggested_value" in op_cols and not pass_set:
            row.append(_cell(r.get("suggested_value")))
        if "pattern" in op_cols:
            row.append(_cell(r.get("pattern")))
        if pass_set:
            outcome = outcome_for(r, by_element, by_rule_bucket)
            if _is_executed_auto_fix(outcome):
                before, after = outcome.get("before"), outcome.get("after")
                row.append(
                    f"✓ pass (auto-fixed this run: {_cell(before)} → {_cell(after)})"
                )
            elif r.get("exempt"):
                row.append("✓ exempt")
            else:
                row.append("✓ pass")
        else:
            outcome = outcome_for(r, by_element, by_rule_bucket)
            label = _STATUS_LABEL.get(r["status"], r["status"])
            row.append(f"{label} → {_outcome_cell(outcome)}")
        L.append("| " + " | ".join(row) + " |")
    if len(records) > _MAX_TABLE_ROWS:
        extra = len(records) - _MAX_TABLE_ROWS
        L.append(f"\n_… and {extra} more (full set in `report_trace.json`)._")


def _render_legend(L: list[str]) -> None:
    """v1.5-R6 (2.3): explain the fix-lifecycle vocabulary ONCE, up front, so
    every per-rule "Status → outcome" cell below reads without re-explaining
    itself."""
    L.append(
        "**Legend** (fix lifecycle — every outcome cell below uses one of these):"
    )
    L.append("")
    L.append(
        "- **QC** — the deterministic rules-engine pass that produced this "
        "record's verdict (compliant / non-compliant / needs human / missing data)."
    )
    L.append(
        "- **Revit write (auto-applied)** — a deterministic fix committed "
        "straight to the model; batched into one undo entry when the addin "
        "supports it, written element-by-element otherwise."
    )
    L.append(
        "- **awaiting approval (proposal issue)** — a computed fix parked as an "
        "ACC issue; applied only after a human sets it to *In progress*."
    )
    L.append(
        "- **human-only** — an LLM-suggested value for a life-safety parameter; "
        "raised as a manual ACC issue and NEVER auto-applied or approve-gated."
    )
    L.append("- **parked** — a fix that was computed but held back (e.g. by the issue budget).")
    L.append(
        "- **pending** — a fix was previewed but not yet committed (this run "
        "ended, or the write is gated)."
    )
    L.append("")


def _render_per_rule(
    L: list[str], grouped: OrderedDict[str, list[CheckRecord]],
    rules_by_id: dict[str, Any], by_element: dict[Any, Any], by_rule_bucket: dict[Any, Any],
) -> None:
    L.append('<a id="3-per-rule-verification"></a>')
    L.append("## 3. Per-rule verification")
    L.append("")
    if not grouped:
        L.append("_No parameter checks were recorded in this run._")
        L.append("")
        return
    _render_legend(L)
    # v1.5-R6 (2.5): don't repeat an IDENTICAL PASS table for a second rule of
    # the same category — a reviewer who just audited rule A's 200-row PASS
    # table for false negatives doesn't need the exact same 200 rows verbatim
    # under rule B two paragraphs later. Only suppressed when the compliant
    # ELEMENT SET is byte-for-byte the same (never when it merely overlaps),
    # and every suppressed rule still gets its OWN full "Needs attention" /
    # Auto-fixed data — this only touches the (already-audited) PASS table.
    seen_pass_sets: dict[str, tuple[str, frozenset[str]]] = {}
    for rule_id, recs in grouped.items():
        rule = rules_by_id.get(rule_id) or rule_view_from_record(recs[0])
        recipe = recipe_for(rule, recs)
        cat = getattr(rule, "category", None) or recs[0].get("category") or "?"
        pass_ids = frozenset(
            r["element_id"] for r in recs if r["status"] == "compliant"
        )
        reuse_from: str | None = None
        if pass_ids:
            prior = seen_pass_sets.get(cat)
            if prior is not None and prior[1] == pass_ids:
                reuse_from = prior[0]
            else:
                seen_pass_sets[cat] = (rule_id, pass_ids)
        _render_one_rule(
            L, rule_id, recs, rule, recipe, by_element, by_rule_bucket,
            reuse_pass_from=reuse_from,
        )


def _geometry_outcome(
    finding: dict[str, Any], by_rule_bucket: dict[Any, Any]
) -> dict[str, Any] | None:
    """Design/Result outcome for one geometry finding.

    v1.5-R6 (1.1e): geometry findings ride the SAME shared bucket DesignAgent
    folds into Path A groups (CLAUDE.md: "Geometry findings ride a shared
    bucket") — always grouped by ``(rule_id, status)``, never per-element — so
    the join is a straight ``by_rule_bucket`` lookup (no per-element/type_id
    fallback needed, unlike ``report_trace.outcome_for`` for parameter rules).
    """
    rule_id = finding.get("rule_id", "?")
    status = finding.get("status") or "non_compliant"
    fix = by_rule_bucket.get((rule_id, status))
    if fix is None:
        return None
    return normalize_outcome(fix)


def _render_geometry(
    L: list[str], geometry_findings: list[dict[str, Any]], by_rule_bucket: dict[Any, Any],
) -> None:
    if not geometry_findings:
        return
    L.append('<a id="3b-geometry--clearance-findings"></a>')
    L.append("## 3b. Geometry / clearance findings")
    L.append("")
    L.append(
        "These come from 3D clearance checks (not the parameter engine), so verify "
        "them with Revit **Interference Check** or by sectioning the model at the "
        "flagged elements rather than a schedule. Select-by-ID anchors them:"
    )
    L.append("")
    L.append("| Rule | Element | ElementId | Severity | Detail | Outcome |")
    L.append("|------|---------|-----------|----------|--------|---------|")
    for f in geometry_findings[:_MAX_TABLE_ROWS]:
        outcome = _geometry_outcome(f, by_rule_bucket)
        L.append(
            f"| `{f.get('rule_id','?')}` | {_cell(f.get('element_name') or f.get('element_id'))} "
            f"| {_cell(f.get('element_id'))} | {_sev(f.get('severity'))} "
            f"| {_cell(_first_sentence(f.get('message'), limit=120))} "
            f"| {_outcome_cell(outcome)} |"
        )
    if len(geometry_findings) > _MAX_TABLE_ROWS:
        L.append(f"\n_… and {len(geometry_findings) - _MAX_TABLE_ROWS} more._")
    L.append("")


def _render_appendix(
    L: list[str], check_trace: list[CheckRecord],
    by_element: dict[Any, Any], by_rule_bucket: dict[Any, Any],
) -> None:
    L.append('<a id="4-per-element-appendix-query--qc--design--result"></a>')
    L.append("## 4. Per-element appendix (Query → QC → Design → Result)")
    L.append("")
    if not check_trace:
        L.append("_No per-element trace recorded._")
        L.append("")
        return
    L.append(
        "Each row is one `(element, rule)` evaluation as the run recorded it — "
        "the value pulled, the verdict, and what was done. The ElementId is the "
        "anchor for Select-by-ID."
    )
    L.append("")

    # v1.5-R6 (2.5): a plain compliant row with NO action attached is already
    # in every per-rule PASS table above (§3) — repeating all of them here too
    # made this section scale with total elements instead of with what
    # actually needs a reviewer's attention. Keep every non-compliant row AND
    # every compliant row that DID have something happen to it (an auto-fix);
    # the rest collapse into one honest count.
    action_rows: list[CheckRecord] = []
    omitted = 0
    for rec in check_trace:
        if rec["status"] != "compliant":
            action_rows.append(rec)
            continue
        outcome = outcome_for(rec, by_element, by_rule_bucket)
        if outcome is not None:
            action_rows.append(rec)
        else:
            omitted += 1

    if omitted:
        L.append(
            f"_{omitted} compliant row(s) with no action omitted — full list in "
            f"`report_trace.json`._"
        )
        L.append("")

    if not action_rows:
        L.append("_Nothing outstanding — every row above was a plain, untouched PASS._")
        L.append("")
        return

    L.append("| Element | ElementId | Rule | Value | Operand | QC | Design/Result |")
    L.append("|---------|-----------|------|-------|---------|----|--------------| ")
    shown = action_rows[:_MAX_APPENDIX_ROWS]
    for rec in shown:
        name = rec.get("element_name") or "(unnamed)"
        eid = rec.get("revit_element_id")
        eid_s = str(eid) if eid is not None else rec["element_id"]
        pulled = _cell(rec.get("value_display", rec.get("raw_value")))
        operand = rec.get("operand")
        operand_cell = (
            f"{_cell(operand)} ({rec.get('operand_source') or 'related'})"
            if operand is not None or rec.get("operand_source") else "-"
        )
        verdict = _STATUS_LABEL.get(rec["status"], rec["status"])
        if rec.get("severity"):
            verdict += f" ({_sev(rec.get('severity'))})"
        if rec.get("exempt"):
            verdict += " · exempt"
        outcome = outcome_for(rec, by_element, by_rule_bucket)
        # v1.5-R6 (polish round 2, N6): a "Compliant" QC verdict here can mean
        # either of two very different things — always-fine, or fine only
        # because this run just wrote a new value. Say which.
        if rec["status"] == "compliant" and _is_executed_auto_fix(outcome):
            verdict += " (after auto-fix)"
        L.append(
            f"| {_cell(name)} | {eid_s} | `{rec['rule_id']}` | {pulled} "
            f"| {operand_cell} | {verdict} | {_outcome_cell(outcome)} |"
        )
    if len(action_rows) > _MAX_APPENDIX_ROWS:
        extra = len(action_rows) - _MAX_APPENDIX_ROWS
        L.append(f"\n_… and {extra} more (full trace in `report_trace.json`)._")
    L.append("")


def _render_not_touched(
    L: list[str], state: dict[str, Any], summary: dict[str, Any],
    proposed_fixes: list[dict[str, Any]], geometry_findings: list[dict[str, Any]],
) -> None:
    L.append('<a id="5-what-we-did-not-touch-and-why"></a>')
    L.append("## 5. What we did NOT touch, and why")
    L.append("")
    L.append(
        "A success-only report destroys trust. These are the things the run "
        "deliberately left for a human — by design, not by omission:"
    )
    L.append("")
    md = summary.get("missing_data", 0)
    mr = summary.get("manual_review", 0)
    L.append(
        f"- **Missing data ({md}):** the parameter was blank, so compliance could "
        "not be computed — a data-quality gap, not a verdict. See "
        "[data_quality_report.md](data_quality_report.md)."
    )
    L.append(
        f"- **Needs human review ({mr}):** the rule is deterministic but the result "
        "needs judgement (e.g. a host rating not in the code table). See "
        "[review_queue.md](review_queue.md)."
    )
    parked = [
        f for f in proposed_fixes
        if not f.get("executed") and (f.get("preview") or {}).get("proposal_issue_id")
    ]
    if parked:
        # v1.5-R6 (polish round 2, N1): a rule's approve-gated fixes get
        # RE-PROPOSED (memo-hit, same proposal_issue_id stamped again) on a
        # later iteration triggered by SOME OTHER rule's auto-fix — the
        # duplicate ProposedFix objects both land in state["proposed_fixes"],
        # so counting len(parked) directly double-counts the same real-world
        # write. Dedup by write-set identity before reporting the count.
        parked_writes = {_write_set_key(f) for f in parked}
        L.append(
            f"- **Awaiting approval ({len(parked_writes)}):** computed Revit fixes that are "
            "safety-gated — they were proposed as ACC issues and will only be "
            "written after a human sets the issue to *In progress* (the Approvals flow)."
        )
    budget_parked = [
        f for f in proposed_fixes
        if not f.get("executed")
        and (f.get("preview") or {}).get("reason") == "issue budget reached"
    ]
    if budget_parked:
        budget_parked_writes = {_write_set_key(f) for f in budget_parked}
        L.append(
            f"- **Capped by issue budget ({len(budget_parked_writes)}):** more findings "
            "exist than `--limit` allowed as ACC issues this run; raise the limit or "
            "re-run to surface the rest. (They are NOT lost — listed in `findings.json`.)"
        )
    if geometry_findings:
        L.append(
            f"- **Geometry findings ({len(geometry_findings)}):** clearance/spatial "
            "checks reported in §3b — verify via Interference Check, not a schedule."
        )
    exempt = sum(1 for r in (state.get("check_trace") or []) if r.get("exempt"))
    if exempt:
        L.append(
            f"- **Exempt ({exempt}):** in scope but the rule imposes no requirement "
            "(e.g. a door in a non-rated wall) — counted compliant, listed in the PASS sets."
        )
    L.append("")
    _render_fix_interactions(L, state)


def _render_fix_interactions(L: list[str], state: dict[str, Any]) -> None:
    """v1.5-R7 (R1-Stage 1): render the fix-interaction list detected from
    ``state["fix_write_log"]`` — capture-time data recorded by DesignAgent
    during the run (renders-never-re-derives; no re-evaluation here). A
    parameter written more than once (by different rules, or across
    iterations) is a concrete critical pair per docs/260711_Autofix Loop.md."""
    interactions = detect_fix_interactions(state.get("fix_write_log"))
    L.append(f"**Fix interactions observed ({len(interactions)}):**")
    L.append("")
    if not interactions:
        L.append("_none observed_")
        L.append("")
        return
    L.append(
        "The same (element, parameter) slot was written more than once — by "
        "different rules and/or across iterations. The final value may depend "
        "on fix order; review these before trusting the result is order-independent."
    )
    L.append("")
    L.append("| ElementId | Parameter | Rules | Iterations | Value chain |")
    L.append("|-----------|-----------|-------|------------|-------------|")
    for i in interactions:
        chain = " → ".join(f"{_cell(o)}→{_cell(n)}" for o, n in i.get("values", []))
        L.append(
            f"| {_cell(i.get('write_eid'))} | {_cell(i.get('parameter'))} "
            f"| {', '.join(f'`{r}`' for r in i.get('rules', []))} "
            f"| {', '.join(str(x) for x in i.get('iterations', []))} | {chain} |"
        )
    L.append("")


def _render_audit_trail(L: list[str], trace_filename: str) -> None:
    L.append('<a id="6-audit-trail"></a>')
    L.append("## 6. Audit trail")
    L.append("")
    L.append(f"- Reasoning trace (raw events): [{trace_filename}]({trace_filename})")
    L.append("- Structured (element, rule) trace: [report_trace.json](report_trace.json)")
    L.append("- Findings (machine-readable): [findings.json](findings.json)")
    L.append("- Outcomes (all 4 buckets): [outcomes.json](outcomes.json)")
    L.append("- Run metadata: [metadata.json](metadata.json)")
    L.append(
        "- ACC audit chain: for any issue above, run Forma `meta_verify_audit_chain` "
        "to confirm its tamper-evident dry-run → approval → execute entries."
    )
    L.append("")
    L.append("---")
    L.append(
        "_Generated by bim-orchestrator. This report RENDERS the run's recorded "
        "artifacts; it does not re-run checks. Verify every claim natively using "
        "the per-rule recipes above._"
    )
    # v1.5-R6 (2.6): the checksum itself can't be embedded here (hashing the
    # file requires the file to already be finished) — the sidecar is written
    # right after this Markdown is saved to disk; see orchestrator.py.
    L.append("")
    L.append("Integrity: SHA-256 in sidecar (`verification_report.sha256`).")
    # v1.5-R6 (polish round 2, N5): the sidecar names an algorithm but never
    # HOW to recompute it — spell out the one-liner instead of assuming a
    # reviewer knows Get-FileHash off the top of their head.
    L.append(f"  {_HASH_VERIFY_HINT}")


__all__ = ["render_audit_report"]
