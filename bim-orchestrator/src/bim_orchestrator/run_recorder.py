"""v1 tasks L + M: per-run folder + token-efficient reasoning trace.

Each --check / --apply / --run / --run-revit invocation creates one
runs/run-<id>/ folder containing:

    metadata.json            -- run shape (id, mode, started_at, duration, status)
    trace.md                 -- human-readable reasoning trace rendered from
                                structlog events captured during the run
    findings.json            -- non_compliant items (CC: routed to ACC issues)
    review_queue.md          -- manual_review items (CC bucket)
    data_quality_report.md   -- missing_data items (CC bucket)
    outcomes.json            -- the 4-state summary + all 3 finding buckets

Token efficiency (per HANDOFF discussion before L+M kicked off):
    TraceCollector captures structlog event_dict only -- never full LLM
    prompts/responses. Long strings are truncated to 500 chars defensively.
    Any future LLM-emitting agent should log token usage as event fields
    so cost can be audited without storing prompt text.
"""

from __future__ import annotations

import contextvars
import json
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Trace collector ─────────────────────────────────────────────────────────

# A single orchestrator invocation activates one collector. Async tasks in the
# graph all inherit the contextvar so the structlog processor finds it.
_collector: contextvars.ContextVar["TraceCollector | None"] = contextvars.ContextVar(
    "run_trace_collector", default=None
)

# Keys that are too large / sensitive to keep in the trace. Dropping these
# protects the trace.md token-budget and prevents accidental prompt leakage.
_DROP_KEYS = frozenset({
    "prompt",
    "response",
    "full_response",
    "context",
    "messages",
    "input",
    "output",
})

# Long string values get truncated to this many chars. Beyond this, the
# information density of a structlog event drops sharply and we'd rather
# see a thousand small events than dump a single MB string.
_STR_TRUNCATE_CHARS = 500


@dataclass
class TraceCollector:
    """In-memory ring of structlog events for one orchestrator invocation.

    The orchestrator wraps each run in `with collector.active(): ...` (or
    calls activate/deactivate manually). The structlog `trace_processor`
    below looks up the current collector via contextvar and appends each
    event dict. After the run, `RunFolder.write_trace(collector)` renders
    the buffer to Markdown.

    Note: events are stored as shallow-copied dicts. We intentionally do
    NOT clone deeply -- that would be expensive and the events are typed
    as already-rendered primitives by the agents emitting them.
    """

    run_id: str
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)

    def activate(self) -> contextvars.Token[TraceCollector | None]:
        return _collector.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token[TraceCollector | None]) -> None:
        _collector.reset(token)

    def record(self, event_dict: dict[str, Any]) -> None:
        cleaned: dict[str, Any] = {}
        for k, v in event_dict.items():
            if k in _DROP_KEYS:
                continue
            if isinstance(v, str) and len(v) > _STR_TRUNCATE_CHARS:
                cleaned[k] = v[: _STR_TRUNCATE_CHARS - 3] + "..."
            else:
                cleaned[k] = v
        self.events.append(cleaned)

    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at


def trace_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor: capture events into the active collector, passthrough.

    Installed by logging_setup.configure_logging(). When no collector is
    active (the default), this is a no-op that just returns the event_dict.
    Critically: this is the LAST processor in the chain (or near-last) so
    that everything renders the same as before.
    """
    col = _collector.get()
    if col is not None:
        col.record(event_dict)
    return event_dict


# ── Markdown rendering ──────────────────────────────────────────────────────

# Map structlog event prefix (everything before the first '.') to a
# human-friendly phase title. Unknown prefixes pass through verbatim.
_PHASE_FOR_PREFIX: dict[str, str] = {
    "query_agent": "Query",
    "revit_query_agent": "Query (Revit)",
    "qc_agent": "QC",
    "grounding_agent": "Grounding",
    "design_agent": "Design",
    "route": "Routing",
    "bump": "Iteration bump",
    "checkpoint": "Checkpoint",
    "hello": "Hello (smoke)",
    "check": "Check (CLI)",
    "apply": "Apply (CLI)",
    "run": "Run (CLI)",
}


def _classify(event_name: str) -> str:
    prefix = event_name.split(".", 1)[0] if "." in event_name else event_name
    return _PHASE_FOR_PREFIX.get(prefix, prefix)


def _format_details(ev: dict[str, Any]) -> str:
    skip = {"event", "level", "timestamp", "iteration"}
    parts: list[str] = []
    for k, v in ev.items():
        if k in skip:
            continue
        # Format value compactly. Booleans + numbers + short strings inline.
        if isinstance(v, str) and len(v) > 80:
            v_repr = repr(v[:77] + "...")
        else:
            v_repr = repr(v)
        parts.append(f"{k}={v_repr}")
    return ", ".join(parts)


def render_trace_markdown(
    collector: TraceCollector,
    *,
    run_id: str | None = None,
    project_id: str | None = None,
    mode: str | None = None,
) -> str:
    rid = run_id or collector.run_id
    lines: list[str] = []
    lines.append(f"# Run trace: {rid}")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if mode:
        lines.append(f"Mode: `{mode}`")
    if project_id:
        lines.append(f"Project: `{project_id}`")
    lines.append(f"Duration: {collector.elapsed_seconds():.2f}s")
    lines.append(f"Events captured: {len(collector.events)}")
    lines.append("")

    if not collector.events:
        lines.append("_No events captured._")
        lines.append("")
        return "\n".join(lines)

    # Group by (iteration, phase). Iteration is read from the event_dict
    # when present, otherwise treated as -1 (pre-loop / global events).
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    phase_first_seen: dict[tuple[int, str], int] = {}
    for idx, ev in enumerate(collector.events):
        iteration = ev.get("iteration", -1)
        if not isinstance(iteration, int):
            iteration = -1
        phase = _classify(ev.get("event", "?"))
        key = (iteration, phase)
        grouped[key].append(ev)
        phase_first_seen.setdefault(key, idx)

    # Sort: iteration ascending, then by first-appearance order of the phase
    sorted_keys = sorted(grouped.keys(), key=lambda k: (k[0], phase_first_seen[k]))

    current_iter: int | None = None
    for key in sorted_keys:
        iteration, phase = key
        if iteration != current_iter:
            current_iter = iteration
            if iteration == -1:
                lines.append("## Pre-loop / global")
            else:
                lines.append(f"## Iteration {iteration}")
            lines.append("")
        lines.append(f"### Phase: {phase}")
        lines.append("")
        for ev in grouped[key]:
            event_name = ev.get("event", "?")
            details = _format_details(ev)
            if details:
                lines.append(f"- `{event_name}` -- {details}")
            else:
                lines.append(f"- `{event_name}`")
        lines.append("")

    return "\n".join(lines)


# ── Run folder management ───────────────────────────────────────────────────


@dataclass
class RunFolder:
    """Manages the on-disk folder for a single orchestrator invocation."""

    run_id: str
    root: Path
    started_at: float
    mode: str

    @classmethod
    def create(cls, runs_root: Path, mode: str) -> RunFolder:
        # Short hex ID (8 chars from 4 random bytes) keeps paths readable
        # while collision odds remain astronomically low for our scale.
        run_id = "run-" + secrets.token_hex(4)
        root = runs_root / run_id
        root.mkdir(parents=True, exist_ok=True)
        return cls(run_id=run_id, root=root, started_at=time.time(), mode=mode)

    @property
    def findings_path(self) -> Path:
        return self.root / "findings.json"

    @property
    def metadata_path(self) -> Path:
        return self.root / "metadata.json"

    @property
    def trace_path(self) -> Path:
        return self.root / "trace.md"

    @property
    def outcomes_path(self) -> Path:
        return self.root / "outcomes.json"

    def write_metadata(self, *, status: str, state: dict[str, Any]) -> None:
        # M2 (2026-07 audit): a run whose ruleset resolved to NO query spec exits
        # non-zero, but its recorded status used to stay "converged"/"completed"
        # — a forensic reader of metadata.json saw a clean run. Record the
        # coverage state and downgrade the status to "no_audit" so the exit code,
        # the recorded status, and coverage never disagree. A "partial" audit
        # keeps its status (it IS a real audit) and carries the detail in
        # query_coverage. coverage_verdict imported lazily to keep run_recorder
        # (a low layer) free of an import-time dependency on state helpers.
        # P1-GEO-01: geometry gets the same treatment as query coverage. The
        # status downgrade already flows through `recorded_status`, but a
        # forensic reader of metadata.json also needs the DETAIL — which rules
        # never ran and why — without having to re-open the Markdown report.
        from bim_orchestrator.state import (
            coverage_verdict,
            geometry_verdict,
            recorded_status,
        )
        verdict = coverage_verdict(state)
        geo_verdict = geometry_verdict(state)
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "duration_seconds": round(time.time() - self.started_at, 2),
            "status": recorded_status(status, state),
            "coverage_status": verdict,
            "query_coverage": state.get("query_coverage"),
            "geometry_coverage_status": geo_verdict,
            "geometry_coverage": state.get("geometry_coverage"),
            "llm_usage": state.get("llm_usage"),
            "llm_status": state.get("llm_status"),
            "stop_reason": state.get("stop_reason"),
            # L2-08: which model ran, how many calls, how many the budget
            # refused. Absent on a Phase-1 run. Without this the artifact
            # could not say whether an AI took part at all, let alone which
            # checkpoint produced a value a human later approved.
            # L2-07: WHY the loop stopped. `converged` alone reads as "ran out
            # of work" — the report says so in those words — which is false
            # when a supervisor model asked to stop early with findings still
            # open. Only `iteration_cap` was ever surfaced before.
            "iterations": state.get("iteration"),
            "max_iterations": state.get("max_iterations"),
            "project_id": state.get("project_id"),
            "document": state.get("document_info"),
            "outcomes_summary": state.get("outcomes_summary"),
            "non_compliant_count": len(state.get("findings", []) or []),
            "manual_review_count": len(state.get("manual_review_items", []) or []),
            "missing_data_count": len(state.get("missing_data_items", []) or []),
            "proposed_fixes_count": len(state.get("proposed_fixes", []) or []),
            "executed_fixes_count": sum(
                1 for f in (state.get("proposed_fixes") or []) if f.get("executed")
            ),
        }
        self.metadata_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    def write_outcomes(self, state: dict[str, Any]) -> None:
        outcomes: dict[str, Any] = {
            "outcomes_summary": state.get("outcomes_summary"),
            "non_compliant": state.get("findings", []) or [],
            "manual_review_items": state.get("manual_review_items", []) or [],
            "missing_data_items": state.get("missing_data_items", []) or [],
            # proposed_fixes persisted so --apply-approved can resume them.
            # approval_token (Path A) and pending Revit writes (Path B) both live here.
            "proposed_fixes": state.get("proposed_fixes", []) or [],
        }
        self.outcomes_path.write_text(
            json.dumps(outcomes, indent=2, default=str), encoding="utf-8"
        )

    def write_trace(
        self,
        collector: TraceCollector,
        *,
        project_id: str | None = None,
    ) -> None:
        markdown = render_trace_markdown(
            collector,
            run_id=self.run_id,
            project_id=project_id,
            mode=self.mode,
        )
        self.trace_path.write_text(markdown, encoding="utf-8")


# ── list_runs helper ────────────────────────────────────────────────────────


def list_runs(runs_root: Path) -> list[dict[str, Any]]:
    """Return parsed metadata for every run folder, newest first.

    Folders missing metadata.json or with malformed JSON appear in the
    list with a sentinel status so the user can still see them and
    decide whether to clean up.
    """
    if not runs_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for folder in runs_root.iterdir():
        if not folder.is_dir() or not folder.name.startswith("run-"):
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.exists():
            rows.append({"run_id": folder.name, "status": "metadata_missing"})
            continue
        try:
            rows.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            rows.append({"run_id": folder.name, "status": "metadata_corrupt"})
    rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return rows


# ── v1 task V-2: fingerprint + cross-run diff helpers ──────────────────────


def fingerprint(finding: dict[str, Any]) -> str:
    """Deterministic identity of a finding across runs.

    A finding is "the same finding in a later run" iff (rule_id, element_id,
    parameter) match. We deliberately ignore severity, status, message, and
    citation -- those can change between runs without the underlying problem
    being a different one. fingerprint() drives diff_outcomes() below.

    Returns a stable string so callers can use it as a dict key, set member,
    or sort-key without re-hashing per call.
    """
    return "::".join((
        str(finding.get("rule_id", "")),
        str(finding.get("element_id", "")),
        str(finding.get("parameter", "")),
    ))


def diff_outcomes(
    prev_outcomes: dict[str, Any] | None,
    curr_outcomes: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Compute the run-over-run delta across all 4 finding buckets.

    Returns ``{resolved, newly_introduced, persistent}`` where each entry is
    a list of the *current* run's finding dicts (or a stub with rule_id /
    element_id when only the prev run had it). ``persistent`` is the
    intersection -- problems that existed in both runs.

    Buckets considered: non_compliant + manual_review_items + missing_data_items.
    Compliant elements aren't tracked individually (they're not findings).

    When ``prev_outcomes`` is None (first run), everything in curr is
    "newly_introduced" and resolved/persistent are empty.
    """
    def _flatten(outcomes: dict[str, Any]) -> dict[str, dict[str, Any]]:
        bag: dict[str, dict[str, Any]] = {}
        for key in ("non_compliant", "manual_review_items", "missing_data_items"):
            for f in (outcomes.get(key) or []):
                bag[fingerprint(f)] = f
        return bag

    curr_bag = _flatten(curr_outcomes)
    if prev_outcomes is None:
        return {
            "resolved": [],
            "newly_introduced": list(curr_bag.values()),
            "persistent": [],
        }
    prev_bag = _flatten(prev_outcomes)
    curr_fps = set(curr_bag)
    prev_fps = set(prev_bag)
    return {
        "resolved": [prev_bag[fp] for fp in (prev_fps - curr_fps)],
        "newly_introduced": [curr_bag[fp] for fp in (curr_fps - prev_fps)],
        "persistent": [curr_bag[fp] for fp in (curr_fps & prev_fps)],
    }


def format_runs_table(rows: list[dict[str, Any]]) -> str:
    """Pretty-print a list_runs() result as a fixed-width table."""
    if not rows:
        return "(no runs found)"
    headers = ["Run ID", "Mode", "Status", "Started", "Duration", "NC/MR/MD"]
    widths = [12, 10, 10, 19, 10, 12]
    lines: list[str] = []
    lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False)))
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        nc = r.get("non_compliant_count")
        mr = r.get("manual_review_count")
        md = r.get("missing_data_count")
        counts = (
            f"{nc}/{mr}/{md}"
            if all(v is not None for v in (nc, mr, md))
            else "-"
        )
        duration = r.get("duration_seconds")
        duration_str = f"{duration}s" if duration is not None else "-"
        cells = [
            str(r.get("run_id", "?"))[:12],
            str(r.get("mode", "?"))[:10],
            str(r.get("status", "?"))[:10],
            str(r.get("started_at", "?"))[:19],
            duration_str[:10],
            counts[:12],
        ]
        lines.append(
            "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=False))
        )
    return "\n".join(lines)
