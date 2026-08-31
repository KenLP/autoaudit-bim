"""v1 task CC: side-report renderers for the manual_review and missing_data buckets.

These complement the existing `findings.json` (which holds non-compliant items
routed to ACC Issues by DesignAgent). The two markdown reports give the BIM
Manager a focused view of:

* `review_queue.md`     -- Findings flagged for human judgment
                           (rule.requires_human=True + check failed)
* `data_quality_report.md` -- Elements missing parameter values needed to
                              evaluate compliance (data quality, not a design
                              violation -- the designer must fill these in)

The module is intentionally pure (no I/O): each renderer takes a list of
Findings and returns a Markdown string. Callers (orchestrator.py CLI modes,
Streamlit UI later in v1) handle file writes.

Both rendered outputs are valid Markdown even when the input list is empty --
this keeps the BM's workflow consistent (always 3 files alongside findings.json).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from bim_orchestrator.state import Finding


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short_citation(finding: Finding) -> str:
    """Render a one-line citation summary for a table cell."""
    cited = finding.get("citation")
    if cited:
        return cited
    if finding.get("citation_missing"):
        return "*missing (hard-mode rule)*"
    return ""


def render_review_queue(
    items: list[Finding],
    *,
    iteration: int | None = None,
    project_id: str | None = None,
) -> str:
    """Render the manual_review_items bucket as a Markdown report.

    Layout: header metadata, summary count, then a flat table sorted by
    severity (high first) then rule_id. Designed for a reviewer who needs
    to triage these one by one -- no grouping required since each item
    needs individual judgment.
    """
    lines: list[str] = []
    lines.append("# Manual Review Queue")
    lines.append("")
    lines.append(f"Generated: {_now_iso()}")
    if project_id:
        lines.append(f"Project: `{project_id}`")
    if iteration is not None:
        lines.append(f"Run iteration: {iteration}")
    lines.append("")
    lines.append(
        "These findings require human judgment. They were flagged because "
        "the rule has `requires_human: true` AND the value failed the "
        "deterministic check. Review each item and either resolve in the "
        "model, grant a derogation, or escalate."
    )
    lines.append("")
    lines.append(
        "> **Scope note.** QCAgent always evaluates *every* rule in the "
        "active YAML; the operator's `--rule` filter only scopes the "
        "subsequent DesignAgent (ACC issues + Path B writes). This queue "
        "reflects the full QC pass, so rows here may include rule IDs that "
        "aren't covered by the current `--rule` filter -- intentional, "
        "since manual-review items are not auto-routed anywhere."
    )
    lines.append("")

    if not items:
        lines.append("## Summary")
        lines.append("")
        lines.append("No manual-review items in this run.")
        lines.append("")
        return "\n".join(lines)

    severity_order = {
        "severity_high": 0,
        "severity_medium": 1,
        "severity_low": 2,
    }
    sorted_items = sorted(
        items,
        key=lambda f: (
            severity_order.get(f.get("severity", "severity_low"), 9),
            f.get("rule_id", ""),
            f.get("element_id", ""),
        ),
    )

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total items: {len(items)}")
    by_severity: dict[str, int] = defaultdict(int)
    for f in items:
        by_severity[f.get("severity", "severity_low")] += 1
    for sev in ("severity_high", "severity_medium", "severity_low"):
        count = by_severity.get(sev, 0)
        if count:
            lines.append(f"- {sev}: {count}")
    lines.append("")

    lines.append("## Items")
    lines.append("")
    lines.append("| # | Rule | Element | Parameter | Severity | Citation | Message |")
    lines.append("|---|------|---------|-----------|----------|----------|---------|")
    for idx, f in enumerate(sorted_items, start=1):
        rule_id = f.get("rule_id", "?")
        # QW-1: prefer name; fall back to element_id URN
        name = f.get("element_name")
        element_cell = name if name else f.get("element_id", "?")
        parameter = f.get("parameter", "?")
        severity = f.get("severity", "?")
        citation = _short_citation(f)
        message = (f.get("message") or "").replace("\n", " ").replace("|", "\\|")
        # Trim very long messages so the table stays readable
        if len(message) > 160:
            message = message[:157] + "..."
        lines.append(
            f"| {idx} | `{rule_id}` | {element_cell} | `{parameter}` | "
            f"{severity} | {citation} | {message} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_data_quality_report(
    items: list[Finding],
    *,
    iteration: int | None = None,
    project_id: str | None = None,
) -> str:
    """Render the missing_data_items bucket as a Markdown report.

    Layout: header, parameter-frequency summary, then items grouped by
    parameter. This grouping helps the BIM Manager prioritize -- often
    one missing parameter (e.g. Department) is missing on many elements,
    and a single bulk action resolves them all.
    """
    lines: list[str] = []
    lines.append("# Data Quality Report")
    lines.append("")
    lines.append(f"Generated: {_now_iso()}")
    if project_id:
        lines.append(f"Project: `{project_id}`")
    if iteration is not None:
        lines.append(f"Run iteration: {iteration}")
    lines.append("")
    lines.append(
        "These elements are missing parameter values needed to evaluate "
        "compliance. They are data quality issues, NOT design violations -- "
        "the designer needs to fill in the values before re-running the "
        "audit. Items are grouped by parameter so bulk fixes are easy."
    )
    lines.append("")
    lines.append(
        "> **Scope note.** QCAgent always evaluates *every* rule in the "
        "active YAML against *every* in-scope element, regardless of the "
        "operator's `--rule` filter. The `--rule` flag scopes only the "
        "subsequent DesignAgent step (ACC issue creation + Path B writes); "
        "this report reflects the full QC pass. If you expected fewer rows "
        "because you set `--rule foo`, that's working as designed -- the "
        "filter doesn't (and shouldn't) hide data-quality information."
    )
    lines.append("")

    if not items:
        lines.append("## Summary")
        lines.append("")
        lines.append("No missing-data items in this run.")
        lines.append("")
        return "\n".join(lines)

    # Group by parameter
    by_param: dict[str, list[Finding]] = defaultdict(list)
    for f in items:
        by_param[f.get("parameter", "?")].append(f)

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total items: {len(items)}")
    lines.append("")
    lines.append("Most common missing parameters:")
    by_param_count = sorted(
        ((p, len(fs)) for p, fs in by_param.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    for param, count in by_param_count:
        lines.append(f"- `{param}`: {count} element(s)")
    lines.append("")

    lines.append("## Items by parameter")
    lines.append("")
    for param, _ in by_param_count:
        param_items = by_param[param]
        lines.append(f"### Parameter: `{param}`")
        lines.append("")
        lines.append("| # | Element | Rule | Severity | Citation |")
        lines.append("|---|---------|------|----------|----------|")
        for idx, f in enumerate(param_items, start=1):
            # QW-1: prefer name; fall back to element_id URN
            name = f.get("element_name")
            element_cell = name if name else f.get("element_id", "?")
            rule_id = f.get("rule_id", "?")
            severity = f.get("severity", "?")
            citation = _short_citation(f)
            lines.append(
                f"| {idx} | {element_cell} | `{rule_id}` | {severity} | {citation} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_side_reports(
    state: dict[str, Any],
    findings_out_path: Any,
) -> tuple[Any, Any]:
    """Write review_queue.md and data_quality_report.md alongside findings.json.

    Returns the two paths written. Pure-function renderers above let us test
    the markdown without touching disk; this thin wrapper just glues them to
    the CLI's existing findings_out path convention.

    `findings_out_path` is a pathlib.Path; we use Path.with_name so the side
    reports always sit in the same directory as findings.json.
    """
    review_path = findings_out_path.with_name("review_queue.md")
    dataq_path = findings_out_path.with_name("data_quality_report.md")

    iteration = state.get("iteration")
    project_id = state.get("project_id")

    review_md = render_review_queue(
        state.get("manual_review_items", []) or [],
        iteration=iteration,
        project_id=project_id,
    )
    dataq_md = render_data_quality_report(
        state.get("missing_data_items", []) or [],
        iteration=iteration,
        project_id=project_id,
    )

    review_path.write_text(review_md, encoding="utf-8")
    dataq_path.write_text(dataq_md, encoding="utf-8")
    return review_path, dataq_path


# ── v1 task V: per-run report ───────────────────────────────────────────────


def _compliance_pct(summary: dict[str, Any] | None) -> str:
    """Return formatted "N%" or "(n/a)" when summary absent / total zero."""
    if not summary:
        return "(n/a)"
    total = summary.get("total") or 0
    if total == 0:
        return "(n/a)"
    return f"{(summary.get('compliant', 0) / total * 100):.1f}%"


def _top_findings(findings: list[Finding], *, limit: int = 10) -> list[Finding]:
    """Sort findings by severity (high first) and return top N for the report."""
    order = {"severity_high": 0, "severity_medium": 1, "severity_low": 2}
    return sorted(
        findings,
        key=lambda f: (
            order.get(f.get("severity", "severity_low"), 9),
            f.get("rule_id", ""),
        ),
    )[:limit]


def load_axes_payload(run_root: Path) -> dict[str, Any] | None:
    """P3-1: read the saved audit-axes envelopes from ``<run>/axes/``.

    Returns ``{"lod": dict|None, "spatial": dict|None, "summary": dict|None}``
    or None when the run had no axes at all. Pure file read — the renderers
    below consume THIS (never re-invoke a satellite), keeping the
    renders-never-re-derives invariant.
    """
    axes_dir = Path(run_root) / "axes"
    if not axes_dir.exists():
        return None
    payload: dict[str, Any] = {"lod": None, "spatial": None, "summary": None}
    for key, fname in (
        ("lod", "lod.json"),
        ("spatial", "spatial.json"),
        ("summary", "axes_summary.json"),
    ):
        p = axes_dir / fname
        if not p.exists():
            continue
        try:
            payload[key] = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload[key] = None
    if not any(payload.values()):
        return None
    return payload


_AXES_TOP_N = 10


def render_axes_section(axes: dict[str, Any] | None, *, heading: str = "## Audit axes") -> list[str]:
    """P3-1: markdown lines for the "Audit axes" section (LOD + Spatial).

    Renders FROM the saved envelopes only. Empty list when ``axes`` is None,
    so callers can unconditionally ``extend``.
    """
    if not axes:
        return []
    L: list[str] = [heading, ""]
    L.append(
        "_IFC-based satellite checks (lod-validator / spatial-qc), run once "
        "before the compliance loop. Raw envelopes: `axes/lod.json` / "
        "`axes/spatial.json`._"
    )
    L.append("")

    lod = axes.get("lod")
    if lod:
        s = lod.get("summary") or {}
        L.append(f"### LOD axis (required LOD {lod.get('required_lod', '?')})")
        L.append("")
        L.append("| Total | Passed | Failed | Undecided |")
        L.append("|---|---|---|---|")
        L.append(
            f"| {s.get('total', 0)} | {s.get('passed', 0)} "
            f"| {s.get('failed', 0)} | {s.get('undecided', 0)} |"
        )
        fails = [
            r for r in (lod.get("results") or []) if r.get("passed") is not True
        ]
        if fails:
            L.append("")
            L.append("| Element (tag) | Name | Type | Detected | Verdict |")
            L.append("|---|---|---|---|---|")
            for r in fails[:_AXES_TOP_N]:
                verdict = "undecided" if r.get("passed") is None else "FAIL"
                L.append(
                    f"| {r.get('tag') or r.get('guid')} | {r.get('name', '')} "
                    f"| {r.get('ifc_type', '')} | {r.get('detected_lod')} "
                    f"| {verdict} |"
                )
            if len(fails) > _AXES_TOP_N:
                L.append(f"\n_… and {len(fails) - _AXES_TOP_N} more in `axes/lod.json`._")
        L.append("")
        L.append("- BCF (open in any BCF viewer): [axes/lod_failures.bcfzip](axes/lod_failures.bcfzip)")
        L.append("")

    spatial = axes.get("spatial")
    if spatial:
        s = spatial.get("summary") or {}
        L.append("### Spatial axis")
        L.append("")
        L.append("| Total | Pass | Fail |")
        L.append("|---|---|---|")
        L.append(f"| {s.get('total', 0)} | {s.get('pass', 0)} | {s.get('fail', 0)} |")
        fails = [
            v for v in (spatial.get("verdicts") or []) if v.get("status") == "FAIL"
        ]
        if fails:
            L.append("")
            L.append("| Space/Door | Rule | Measured (m) | Required (m) | Margin (m) |")
            L.append("|---|---|---|---|---|")
            for v in fails[:_AXES_TOP_N]:
                name = v.get("long_name") or v.get("name") or v.get("guid")
                L.append(
                    f"| {name} | {v.get('rule')} | {v.get('measured_m')} "
                    f"| {v.get('required_m')} | {v.get('margin_m')} |"
                )
            if len(fails) > _AXES_TOP_N:
                L.append(
                    f"\n_… and {len(fails) - _AXES_TOP_N} more in `axes/spatial.json`._"
                )
        L.append("")
        L.append("- BCF: [axes/spatial_failures.bcfzip](axes/spatial_failures.bcfzip) · audit PNGs: `axes/viz/`")
        L.append("")

    summary = axes.get("summary")
    skipped = (summary or {}).get("skipped") or []
    if skipped:
        L.append("### Skipped / coverage gaps")
        L.append("")
        for s_item in skipped:
            L.append(f"- ⚠ {s_item}")
        L.append("")
    return L


def render_per_run_report(
    state: dict[str, Any],
    *,
    run_id: str | None = None,
    mode: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    rules_path: str | None = None,
    trace_filename: str = "trace.md",
    top_n: int = 10,
    axes: dict[str, Any] | None = None,
    document: dict[str, Any] | None = None,
) -> str:
    """v1 task V: per-run audit report rendered from a single run's OrchestratorState.

    Sections: Header (run metadata) → Summary (4-state + compliance pct) →
    Top non-compliant (sorted by severity) → Audit trail (rules path, trace
    link, side-report links). Output is ASCII-safe + relative-linked so it
    can be read straight from the runs/run-<id>/ folder.
    """
    findings = state.get("findings") or []
    manual_review = state.get("manual_review_items") or []
    missing_data = state.get("missing_data_items") or []
    summary = state.get("outcomes_summary")
    project_id = state.get("project_id")
    iteration = state.get("iteration")

    lines: list[str] = []
    lines.append(f"# Compliance audit report -- {run_id or '(no run id)'}")
    lines.append("")
    if mode:
        lines.append(f"**Mode:** `{mode}`")
    if project_id:
        lines.append(f"**Project:** `{project_id}`")
    if document:
        model_line = f"**Model:** `{document.get('title', '?')}`"
        if document.get("revit_version_name"):
            model_line += f" — {document['revit_version_name']}"
        lines.append(model_line)
        if document.get("path"):
            lines.append(f"**Model path:** `{document['path']}`")
        if document.get("is_modified"):
            lines.append(
                "⚠ **Model had unsaved changes at audit time** — "
                "results reflect the in-session state, not the saved file."
            )
    if started_at:
        lines.append(f"**Started:** {started_at}")
    if finished_at:
        lines.append(f"**Finished:** {finished_at}")
    if duration_seconds is not None:
        lines.append(f"**Duration:** {duration_seconds:.2f}s")
    if iteration is not None:
        lines.append(f"**Iterations:** {iteration}")
    if rules_path:
        lines.append(f"**Rules:** `{rules_path}`")
    lines.append("")
    if state.get("stop_reason") == "iteration_cap":
        # v1.5-R7 (cap-hit honesty): the loop hit max_iterations before the
        # fix set reached a stable fixpoint — say so plainly, don't let a
        # partial pass read as a finished one.
        lines.append(
            f"⚠ **Stopped at the iteration cap** (max_iterations="
            f"{state.get('max_iterations')}) — the fix set had NOT reached a "
            "stable fixpoint; results may depend on fix order. Re-run or "
            "raise the cap."
        )
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    if summary:
        lines.append(f"- Total checks: {summary.get('total', 0)}")
        lines.append(f"- Compliant:      {summary.get('compliant', 0)}  ({_compliance_pct(summary)})")
        lines.append(f"- Non-Compliant:  {summary.get('non_compliant', 0)}")
        lines.append(f"- Manual Review:  {summary.get('manual_review', 0)}")
        lines.append(f"- Missing Data:   {summary.get('missing_data', 0)}")
    else:
        lines.append("(no outcomes_summary present in state -- pre-BB run?)")
    lines.append("")

    lines.append("## Top non-compliant findings")
    lines.append("")
    top = _top_findings(findings, limit=top_n)
    if not top:
        lines.append("No non-compliant findings in this run.")
    else:
        lines.append("| # | Severity | Rule | Element | Parameter |")
        lines.append("|---|----------|------|---------|-----------|")
        for idx, f in enumerate(top, start=1):
            sev = f.get("severity", "?").replace("severity_", "").upper()
            rule = f.get("rule_id", "?")
            # QW-1: prefer human-readable name; fall back to truncated URN.
            name = f.get("element_name")
            elem = name if name else ((f.get("element_id") or "?")[:30])
            param = f.get("parameter", "?")
            lines.append(f"| {idx} | {sev} | `{rule}` | {elem} | `{param}` |")
        if len(findings) > top_n:
            lines.append(f"\n*(+{len(findings) - top_n} more in `findings.json`)*")
    lines.append("")

    lines.append("## Other buckets")
    lines.append("")
    lines.append(f"- Manual review items: {len(manual_review)}  ([review_queue.md](review_queue.md))")
    lines.append(f"- Missing data items: {len(missing_data)}  ([data_quality_report.md](data_quality_report.md))")
    lines.append("")

    # P3-1: IFC audit axes (rendered from the saved envelopes, never re-run).
    lines.extend(render_axes_section(axes))

    lines.append("## Audit trail")
    lines.append("")
    lines.append(f"- Reasoning trace: [{trace_filename}]({trace_filename})")
    lines.append("- Findings (machine-readable): [findings.json](findings.json)")
    lines.append("- Outcomes (all 4 buckets, structured): [outcomes.json](outcomes.json)")
    lines.append("- Run metadata: [metadata.json](metadata.json)")
    lines.append("")
    lines.append("---")
    lines.append("_Generated by bim-orchestrator. Trust pipeline: dry-run preview -> approval token -> execute -> audit chain._")

    return "\n".join(lines)


# ── v1 task V-3: cross-run trend report ─────────────────────────────────────


def _load_run(folder: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Read metadata.json + outcomes.json for one run folder.

    Returns ``(metadata, outcomes)`` or None when either file is missing /
    unparseable. Tolerant of partial folders -- the trend report should
    survive a half-written run (crash mid-finish) without raising.
    """
    meta_path = folder / "metadata.json"
    outcomes_path = folder / "outcomes.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    outcomes: dict[str, Any] = {}
    if outcomes_path.exists():
        try:
            outcomes = json.loads(outcomes_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            outcomes = {}
    return meta, outcomes


def render_trend_report(runs_root: Path, *, limit: int = 20) -> str:
    """v1 task V-3: aggregate the most recent N runs into a single Markdown.

    Sections: trend table (per-run NC/MR/MD counts + compliance %),
    convergence summary (delta from oldest to newest), persistent issues
    (open across ALL listed runs), newly_introduced (only in newest run),
    resolved_since_last (in prev run but not newest).

    Uses run_recorder.diff_outcomes for the latest-vs-previous diff; the
    "persistent across all" view is computed inline by intersecting every
    run's fingerprint set.
    """
    from bim_orchestrator.run_recorder import diff_outcomes, fingerprint

    lines: list[str] = []
    lines.append("# Compliance trend across recent runs")
    lines.append("")
    lines.append(f"Generated: {_now_iso()}")
    lines.append("")

    if not runs_root.exists():
        lines.append("_No runs/ folder yet -- this is normal on a fresh checkout._")
        return "\n".join(lines)

    folders = sorted(
        (f for f in runs_root.iterdir() if f.is_dir() and f.name.startswith("run-")),
        key=lambda f: f.name,
    )
    if not folders:
        lines.append("_No run folders found under_ `runs/`.")
        return "\n".join(lines)

    parsed: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for f in folders:
        rec = _load_run(f)
        if rec is None:
            continue
        meta, outcomes = rec
        parsed.append((f, meta, outcomes))

    if not parsed:
        lines.append("_Found run folders but none had readable metadata.json._")
        return "\n".join(lines)

    # Order by started_at desc (newest first), then trim to `limit`.
    parsed.sort(key=lambda triple: triple[1].get("started_at") or "", reverse=True)
    parsed = parsed[:limit]

    lines.append(f"Total runs scanned: **{len(parsed)}** (limit={limit})")
    lines.append("")

    # Trend table -- oldest at top so visual is L->R progress
    lines.append("## Trend table")
    lines.append("")
    lines.append("| Run | Started | Mode | Status | Total | Compliant | Non-Compliant | Manual Review | Missing Data | Compliance % |")
    lines.append("|-----|---------|------|--------|-------|-----------|---------------|---------------|--------------|--------------|")
    for _folder, meta, _outcomes in reversed(parsed):  # oldest first in table
        summary = meta.get("outcomes_summary") or {}
        total = summary.get("total") or 0
        compliant = summary.get("compliant") or 0
        pct = f"{(compliant / total * 100):.1f}%" if total else "(n/a)"
        lines.append(
            f"| `{meta.get('run_id','?')}` "
            f"| {(meta.get('started_at') or '?')} "
            f"| {meta.get('mode','?')} "
            f"| {meta.get('status','?')} "
            f"| {total} "
            f"| {compliant} "
            f"| {summary.get('non_compliant', 0)} "
            f"| {summary.get('manual_review', 0)} "
            f"| {summary.get('missing_data', 0)} "
            f"| {pct} |"
        )
    lines.append("")

    # Latest-vs-previous diff
    latest_folder, latest_meta, latest_outcomes = parsed[0]
    prev_outcomes = parsed[1][2] if len(parsed) > 1 else None
    diff = diff_outcomes(prev_outcomes, latest_outcomes)

    lines.append("## Latest run vs previous")
    lines.append("")
    lines.append(f"- Latest: `{latest_meta.get('run_id','?')}`  ({latest_meta.get('started_at')})")
    if prev_outcomes is None:
        lines.append("- Previous: (none -- this is the first run)")
    else:
        lines.append(f"- Previous: `{parsed[1][1].get('run_id','?')}`  ({parsed[1][1].get('started_at')})")
    lines.append(f"- Resolved since previous: **{len(diff['resolved'])}**")
    lines.append(f"- Newly introduced: **{len(diff['newly_introduced'])}**")
    lines.append(f"- Persistent (both runs): **{len(diff['persistent'])}**")
    lines.append("")

    if diff["newly_introduced"]:
        lines.append("### Newly introduced (latest only)")
        lines.append("")
        for f in diff["newly_introduced"][:15]:
            lines.append(
                f"- `{f.get('rule_id','?')}` on element "
                f"`{f.get('element_id','?')[:30]}` (param `{f.get('parameter','?')}`)"
            )
        if len(diff["newly_introduced"]) > 15:
            lines.append(f"  *(+{len(diff['newly_introduced']) - 15} more)*")
        lines.append("")

    if diff["resolved"]:
        lines.append("### Resolved since previous")
        lines.append("")
        for f in diff["resolved"][:15]:
            lines.append(
                f"- `{f.get('rule_id','?')}` on element "
                f"`{f.get('element_id','?')[:30]}` (param `{f.get('parameter','?')}`)"
            )
        if len(diff["resolved"]) > 15:
            lines.append(f"  *(+{len(diff['resolved']) - 15} more)*")
        lines.append("")

    # Persistent-across-ALL-runs section -- harder to fix, likely needs design
    if len(parsed) >= 3:
        per_run_fps: list[set[str]] = []
        for _folder, _meta, outcomes in parsed:
            run_fps: set[str] = set()
            for key in ("non_compliant", "manual_review_items", "missing_data_items"):
                for f in (outcomes.get(key) or []):
                    run_fps.add(fingerprint(f))
            per_run_fps.append(run_fps)
        persistent_all = set.intersection(*per_run_fps) if per_run_fps else set()
        if persistent_all:
            lines.append(f"## Persistent across ALL {len(parsed)} runs")
            lines.append("")
            lines.append(
                "_These findings have been open in every run scanned. "
                "Likely candidates for a design decision rather than a quick fix._"
            )
            lines.append("")
            # Show up to 20 with the rule:element:parameter triple
            for fp in sorted(persistent_all)[:20]:
                lines.append(f"- `{fp}`")
            if len(persistent_all) > 20:
                lines.append(f"  *(+{len(persistent_all) - 20} more)*")
            lines.append("")

    return "\n".join(lines)


def write_trend_report(runs_root: Path) -> Path:
    """Render and write the trend report to ``runs_root/trend.md``.

    Always overwrites -- the trend is recomputed on every call so it
    reflects the latest state of the runs/ folder.
    """
    runs_root.mkdir(parents=True, exist_ok=True)
    out = runs_root / "trend.md"
    out.write_text(render_trend_report(runs_root), encoding="utf-8")
    return out
