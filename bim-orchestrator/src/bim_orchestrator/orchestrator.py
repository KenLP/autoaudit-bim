"""Orchestrator — LangGraph state machine wiring Query, QC, and Design agents.

CLI subcommands:
    --hello     Smoke test MCP connection
    --check     Query → QC (read-only), dump findings.json
    --apply     Query → QC → Design once (single-pass, no loop)
    --run       Full cyclic graph (Day 4): Query → QC → [Design → bump → Query → …]
    (default)   Same as --run
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import pathlib
import sys
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import Any

import structlog
from dotenv import load_dotenv

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.grounding import GroundingAgent
from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.query import QueryAgent
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.graph import build_graph
from bim_orchestrator.llm.factory import (
    build_llm_run_context,
    llm_diagnostic_enabled,
    llm_flag_problems,
    llm_remediation_enabled,
    llm_supervisor_enabled,
    make_diagnostic_agent,
    make_remediation_agent,
    make_supervisor_agent,
)
from bim_orchestrator.logging_setup import configure_logging
from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig
from bim_orchestrator.mcp_clients.revit import make_revit_client
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.rules_lint import ParamRef, lint as lint_ruleset
from bim_orchestrator.rag.eval import run_eval
from bim_orchestrator.rag.fixtures import (
    DEFAULT_IBC_QUERIES,
    ingest_bep_room_requirements,
    ingest_ibc_chapter_7,
)
from bim_orchestrator.rag.store import VectorStore
from bim_orchestrator.audit_report import render_audit_report
from bim_orchestrator.report_trace import detect_fix_interactions
from bim_orchestrator.reports import (
    load_axes_payload,
    render_per_run_report,
    write_side_reports,
    write_trend_report,
)
from bim_orchestrator.run_recorder import (
    RunFolder,
    TraceCollector,
    format_runs_table,
    list_runs,
)
from bim_orchestrator.state import (
    Finding,
    OrchestratorState,
    coverage_verdict,
    geometry_verdict,
    recorded_status,
)

# Exit codes:
#   0   success
#   1   general / unexpected error
#   2   config error (env var missing, invalid CLI args)
#   3   MCP / connection error
#   4   ACC / APS API error

log = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "rules.parameter_completeness.yaml"
# --demo default rules file (only used when --demo is set AND --rules wasn't
# passed explicitly — see _resolve_rules_paths).
DEFAULT_DEMO_RULES_PATH = PROJECT_ROOT / "config" / "rules.demo.yaml"
DEFAULT_AUTONOMY_PATH = PROJECT_ROOT / "config" / "autonomy.yaml"
DEFAULT_FINDINGS_OUT = PROJECT_ROOT / "findings.json"
DEFAULT_CHECKPOINT_DIR = PROJECT_ROOT / "checkpoints"
DEFAULT_APPROVALS_DIR = PROJECT_ROOT / "runs" / "approvals"
# C-1 (review round 7, 2026-08-17): CLI --demo parks its simulated proposals
# in a SEPARATE dir. Mixing them into the production dir had two teeth: the
# demo's machine-local records leaked into the pinned --demo transcript (a
# fresh clone went RED), and the ApprovalWatcher counted them toward ACC
# liveness — get_issue on a simulated project always throws, so a dir holding
# only demo records read as "ACC unreachable" while ACC was fine (C-2). An
# explicit --approvals-dir still overrides. NOTE the service/audit `mode:
# demo` branch deliberately KEEPS the shared dir — the AU webUI arc shows its
# proposals in the Approvals view (see the comment at that call site).
DEFAULT_DEMO_APPROVALS_DIR = PROJECT_ROOT / "runs" / "approvals_demo"
# v1 task M: every --check / --apply / --run / --run-revit invocation gets
# its own runs/run-<id>/ folder with metadata.json + trace.md + outcomes.json
# alongside the legacy findings.json + side reports.
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"


def _start_run_recording(mode: str) -> tuple[RunFolder, TraceCollector, Any]:
    """v1 task L+M: open a run folder + activate the trace collector.

    Returns (folder, collector, contextvar_token). The token must be passed
    back to `_finish_run_recording` so the contextvar gets reset cleanly.
    """
    folder = RunFolder.create(DEFAULT_RUNS_DIR, mode=mode)
    collector = TraceCollector(run_id=folder.run_id)
    token = collector.activate()
    log.info("run_recorder.started", run_id=folder.run_id, mode=mode, folder=str(folder.root))
    return folder, collector, token


def _lint_ruleset_advisory(ruleset: Any) -> None:
    """v1.5-R7 (R1-Stage 2): light, non-blocking wire into every run — logs
    each rules-lint finding so a termination/confluence hazard is visible in
    the run's logs even when nobody ran ``--lint-rules`` by hand. NEVER fails
    the run (the CLI command is the only place ``--strict`` can fail it)."""
    try:
        report = lint_ruleset(ruleset)
    except Exception as exc:  # noqa: BLE001 — advisory only, must never break a run
        log.warning("rules_lint.advisory_failed", error=str(exc))
        return
    for finding in [*report.errors, *report.warnings]:
        log.warning("rules_lint.finding", **finding)


def _safe_getuser() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        # A headless / sandboxed process can lack a resolvable username
        # (no $USER, no pwd entry) — provenance is best-effort, never fatal.
        return "unknown"


def _warn_if_service_busy(mode: str) -> None:
    """Say so when the AuditHub service is mid-audit and this command is about
    to touch the same Revit document (L-07).

    WARN, never block. The service's single-run lock only coordinates the paths
    that go through the service; a CLI command is a context with a human in
    front of it, so the useful thing is to surface the collision and let them
    decide. Taking the lock here would be the technically tidier answer and the
    operationally worse one: a CLI process that dies without releasing it would
    jam the nightly audit behind a stale file, night after night, with nobody
    there to notice — trading a rare, passive risk (an inconsistent report) for
    an active one (no audit at all).

    Best-effort by construction: any error reading the marker is ignored. A
    warning helper must never be the reason a command fails to start.
    """
    try:
        lock_path = DEFAULT_RUNS_DIR / ".service_lock"
        if not lock_path.exists():
            return
        owner = lock_path.read_text(encoding="utf-8").strip().split()
        pid = owner[0] if owner else "?"
    except OSError:
        return
    log.warning(
        "cli.service_audit_in_progress",
        mode=mode, service_pid=pid, lock=str(lock_path),
        note="the AuditHub service is running an audit or applying approvals "
        "against the same Revit document; readings can be inconsistent and "
        "writes can interleave. Wait for it to finish, or stop the service.",
    )
    print(
        f"\n[!] AuditHub service appears busy (pid {pid}). It shares this "
        f"Revit document.\n    Running --{mode} now can read a half-updated "
        "model or interleave writes.\n    Continuing anyway — stop the service "
        "if you want a clean run.\n",
        file=sys.stderr,
    )


async def _fetch_document_identity(revit_client: Any) -> dict[str, Any] | None:
    """Chụp danh tính document Revit đang mở, một lần, đầu run.

    Best-effort: addin không chạy / lỗi transport / Home screen (title rỗng)
    → None + warning, KHÔNG BAO GIỜ fail run. Map wire camelCase → snake_case.
    """
    from datetime import datetime, timezone
    try:
        doc = await revit_client.get_document_info() or {}
        ver = await revit_client.get_version() or {}
    except Exception as exc:  # noqa: BLE001 — provenance is best-effort, never fatal
        log.warning("document_identity.unavailable", error=str(exc))
        return None
    title = doc.get("title") or None
    if not title:
        log.warning("document_identity.unavailable", error="no document open (empty title)")
        return None
    return {
        "title": title,
        "path": doc.get("pathName") or None,
        "is_workshared": doc.get("isWorkshared"),
        "is_modified": doc.get("isModified"),
        "project_name": doc.get("projectName") or None,
        "project_number": doc.get("projectNumber") or None,
        "revit_version_name": ver.get("versionName") or None,
        "revit_version_number": ver.get("versionNumber") or None,
        "fetched_at": _utc_iso_suffix(datetime.now(timezone.utc)),
    }


def _tool_build_id() -> str:
    """Exact build identifier stamped into an audit artifact's provenance.

    A static ``0.1.0`` (the package version, never bumped) told a forensic
    reader nothing about WHICH code produced a report. This prefers the real
    Git commit — ``<pkgver>+g<sha>`` (with ``.dirty`` when the tree has
    uncommitted changes) — so an audit can be tied back to an exact revision.

    Order: build-time-embedded id (env ``BIM_ORCHESTRATOR_BUILD_ID``, set by a
    packaging step where no ``.git`` ships) → live ``git rev-parse`` in the
    source tree → bare package version. Every branch degrades cleanly; git is
    invoked with a short timeout so a slow/hung VCS never stalls a run.
    """
    from importlib import metadata as importlib_metadata

    embedded = os.environ.get("BIM_ORCHESTRATOR_BUILD_ID")
    if embedded:
        return embedded

    try:
        pkg_version = importlib_metadata.version("bim-orchestrator")
    except importlib_metadata.PackageNotFoundError:
        pkg_version = "0+unknown"

    try:
        import subprocess

        here = Path(__file__).resolve().parent
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if not sha:
            return pkg_version
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=here, capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        return f"{pkg_version}+g{sha}{'.dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        # git absent (packaged deploy without .git) or timed out → version only.
        return pkg_version


def _build_provenance(
    state: OrchestratorState | dict[str, Any],
    rules_paths: Path | Sequence[Path] | None,
    folder: RunFolder,
) -> dict[str, Any]:
    """v1.5-R6 (2.2): capture-time provenance — who/what/when produced this
    run, so the renderer never has to call ``getpass``/``platform`` itself
    (a later re-render from a saved ``report_trace.json`` would then report
    the WRONG machine/user — renders-never-re-derives extends here too).
    """
    import hashlib
    import platform
    from datetime import datetime, timezone

    tool_version = _tool_build_id()

    paths: list[Path] = []
    if rules_paths is not None:
        paths = (
            [Path(rules_paths)] if isinstance(rules_paths, (str, Path))
            else [Path(p) for p in rules_paths]
        )
    rules_files: list[dict[str, Any]] = []
    for p in paths:
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as exc:
            log.warning("provenance.rules_file_hash_failed", path=str(p), error=str(exc))
            digest = None
        rules_files.append({"path": str(p), "sha256": digest})

    return {
        "tool_version": tool_version,
        "user": _safe_getuser(),
        "machine": platform.node() or "unknown",
        "captured_at": _utc_iso_suffix(datetime.now(timezone.utc)),
        "project_id": state.get("project_id"),
        "document": state.get("document_info"),
        "run_id": folder.run_id,
        "elements_fetched": len(state.get("elements") or []),
        "rules_files": rules_files,
    }


def _utc_iso_suffix(dt: Any) -> str:
    """v1.5-R6 (polish round 2, N4): one ISO-UTC format for every timestamp
    in the verification report's Provenance block, with an explicit
    "(UTC)" tag — the report used to mix a UTC ``captured_at`` (bare
    ``+00:00``) with a LOCAL naive ``Started``/``Finished`` (no offset at
    all), which reads as two clocks that disagree by the reviewer's UTC
    offset. Both now go through this one formatter. ``dt`` must already be
    timezone-aware (callers pass ``datetime.now(timezone.utc)`` or
    ``datetime.fromtimestamp(ts, tz=timezone.utc)``)."""
    from datetime import timezone as _tz
    return dt.astimezone(_tz.utc).isoformat(timespec="seconds") + " (UTC)"


def _finish_run_recording(
    folder: RunFolder,
    collector: TraceCollector,
    token: Any,
    state: OrchestratorState | dict[str, Any],
    *,
    status: str,
    rules: Any = None,
    rules_paths: Path | Sequence[Path] | None = None,
    max_elements: int | None = None,
    banner: str | None = None,
) -> None:
    """v1 task L+M: write metadata + outcomes + trace, then drop the contextvar.

    Also mirrors findings.json + the 2 side reports into the run folder so
    each run is fully self-contained (the legacy paths at PROJECT_ROOT remain
    for backward compat with existing scripts that grep findings.json).

    v1 task V: additionally writes the per-run audit report (report.md) and
    refreshes the cross-run trend (runs/trend.md). Both are pure render-from-
    state, no extra I/O contention with the trust pipeline.

    v1 report module: also writes the structured (element, rule) trace
    (report_trace.json) and the verification report (verification_report.md) —
    the human-readable "is this trustworthy + how do I check it myself?" report.
    Both are additive and best-effort (a render bug must never fail the run);
    ``rules`` (the RuleSet) is threaded in so the renderer can describe each rule
    + build native verify recipes.

    v1.5-R6: ``rules_paths`` (the actual file(s) on disk, for provenance
    hashing — ``rules.scenario`` alone is a name, not a path) / ``max_elements``
    (the ``--max-elements`` cap, for the Coverage line) / ``banner`` (2.4, e.g.
    --demo's notice) are all optional and additive — omitting them reproduces
    the pre-R6 report exactly.
    """
    import time
    finished_ts = time.time()
    try:
        # Mirror artifacts INTO the run folder so the folder is self-contained.
        folder.findings_path.write_text(
            json.dumps(state.get("findings", []) or [], indent=2, default=str),
            encoding="utf-8",
        )
        folder.write_outcomes(state)
        write_side_reports(state, folder.findings_path)
        folder.write_metadata(status=status, state=state)
        folder.write_trace(collector, project_id=state.get("project_id"))

        # P3-1: audit-axes envelopes were persisted into <run>/axes/ by the
        # on_folder hook (when --audit ran them); load once, render in both
        # reports below FROM the saved files — renders-never-re-derives.
        axes_payload = load_axes_payload(folder.root)

        # v1 task V-1: per-run audit report.
        from datetime import datetime, timezone
        report_md = render_per_run_report(
            state,
            run_id=folder.run_id,
            mode=folder.mode,
            started_at=datetime.fromtimestamp(folder.started_at).isoformat(timespec="seconds"),
            finished_at=datetime.fromtimestamp(finished_ts).isoformat(timespec="seconds"),
            duration_seconds=round(finished_ts - folder.started_at, 2),
            axes=axes_payload,
            document=state.get("document_info"),
        )
        (folder.root / "report.md").write_text(report_md, encoding="utf-8")

        # v1 report module: structured trace + verification report. Best-effort
        # (additive) -- a render bug must not fail the run's artifact write.
        try:
            (folder.root / "report_trace.json").write_text(
                # v1.5-R6 (3.4 hygiene): no indent — this file is machine-read
                # (re-render / verification_views), never hand-edited.
                json.dumps(state.get("check_trace") or [], default=str),
                encoding="utf-8",
            )
            # v1.5-R7 (R1-Stage 1): persist the accumulated write log + emit one
            # warning per detected fix interaction so it's visible in logs/CI,
            # not only in the rendered report.
            fix_write_log = state.get("fix_write_log") or []
            (folder.root / "fix_write_log.json").write_text(
                json.dumps(fix_write_log, default=str), encoding="utf-8",
            )
            for interaction in detect_fix_interactions(fix_write_log):
                log.warning("autofix.critical_pair", **interaction)
            provenance = _build_provenance(state, rules_paths, folder)
            # v1.5-R6 (polish round 2, N4): UTC + explicit "(UTC)" tag, same
            # formatter/timezone as provenance's `captured_at` — a mismatched
            # local-vs-UTC pair here used to read as two disagreeing clocks.
            verification_md = render_audit_report(
                state,
                rules=rules,
                run_id=folder.run_id,
                mode=folder.mode,
                started_at=_utc_iso_suffix(
                    datetime.fromtimestamp(folder.started_at, tz=timezone.utc)
                ),
                finished_at=_utc_iso_suffix(
                    datetime.fromtimestamp(finished_ts, tz=timezone.utc)
                ),
                duration_seconds=round(finished_ts - folder.started_at, 2),
                rules_path=getattr(rules, "scenario", None),
                banner=banner,
                provenance=provenance,
                max_elements=max_elements,
                axes=axes_payload,
                # P1-07: render the SAME status the run recorder writes to
                # metadata.json (incl. its "no_audit" downgrade), so the two
                # artifacts of one run can never contradict each other.
                run_status=recorded_status(status, state),
            )
            report_path = folder.root / "verification_report.md"
            report_path.write_text(verification_md, encoding="utf-8")
            # v1.5-R6 (2.6): a sidecar checksum of the WRITTEN bytes — the
            # report footer just says "Integrity: SHA-256 in sidecar" (no
            # chicken-and-egg: the hash is never embedded in the file it
            # hashes).
            import hashlib
            digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
            (folder.root / "verification_report.sha256").write_text(
                f"{digest}  verification_report.md\n", encoding="utf-8"
            )
        except Exception as exc:
            log.warning("run_recorder.verification_report_failed", error=str(exc))

        # Scheduled-audit delta: per-run resolved/new/persistent vs the previous
        # comparable run (same profile). Render-from-disk only; never fail the
        # run. Only for a successful run ("completed" from --check/--apply,
        # "converged" from the graph modes) — a failed run's outcomes are cut
        # short mid-write, so a diff against it would misreport "resolved".
        #
        # M-07 (2026-08-01 review): gate on `recorded_status`, the SAME string
        # metadata.json and the reports show — not on the raw graph status.
        # A run where every category failed to resolve still converges (zero
        # elements, zero findings) and `recorded_status` downgrades it to
        # "no_audit" for exactly that reason; gating on the raw status wrote a
        # delta anyway, and an empty outcome set diffed against a real baseline
        # reads as "every finding resolved since". That headline sat next to a
        # metadata.json saying no audit happened. Same P1-07 rule — one
        # terminal status for every artifact — extended to the delta, which
        # was added after it.
        try:
            from bim_orchestrator.delta_report import SUCCESSFUL_STATUSES, write_delta_report
            if recorded_status(status, state) in SUCCESSFUL_STATUSES:
                write_delta_report(folder.root)
        except Exception as exc:
            log.warning("run_recorder.delta_report_failed", error=str(exc))

        # v1 task V-3: refresh the cross-run trend (best-effort -- never raise).
        try:
            write_trend_report(DEFAULT_RUNS_DIR)
        except Exception as exc:
            log.warning("run_recorder.trend_refresh_failed", error=str(exc))

        log.info(
            "run_recorder.finished",
            run_id=folder.run_id,
            status=status,
            events=len(collector.events),
        )
    finally:
        TraceCollector.deactivate(token)


# ---- hello -----------------------------------------------------------------

def list_runs_cmd() -> int:
    """v1 task M: print a table of all runs/run-<id>/ folders, newest first.

    Reads metadata.json from each folder via `list_runs()` and formats via
    `format_runs_table()`. Both pure functions live in run_recorder so the
    Streamlit UI (later in v1) can reuse them.
    """
    rows = list_runs(DEFAULT_RUNS_DIR)
    print(format_runs_table(rows))
    print(f"\nTotal runs: {len(rows)} (folder: {DEFAULT_RUNS_DIR})")
    return 0


def trend_report_cmd() -> int:
    """v1 task V: regenerate runs/trend.md from the runs/ folder + print path.

    Useful for ad-hoc inspection without running a fresh audit; the
    --check / --apply / --run / --run-revit modes already refresh trend.md
    automatically at the end of each invocation.
    """
    path = write_trend_report(DEFAULT_RUNS_DIR)
    print(f"Trend report written: {path}")
    return 0


async def hello() -> int:
    """Smoke test: connect to MCP server, query Rooms from demo design, exit."""
    element_group_id = os.environ.get("DEMO_ELEMENT_GROUP_ID")
    if not element_group_id:
        log.error("hello.missing_env", missing="DEMO_ELEMENT_GROUP_ID")
        return 2

    config = FormaMCPConfig.from_env()
    async with FormaMCPClient(config) as client:
        log.info("hello.querying_rooms", element_group_id=element_group_id)
        elements = await client.query_elements(
            element_group_id=element_group_id,
            category="Rooms",
        )
        log.info("hello.ok", count=len(elements), first=elements[0] if elements else None)
    return 0


# ---- check (read-only) ----------------------------------------------------

async def check(
    rules_path: Path | Sequence[Path],
    autonomy_path: Path,
    findings_out: Path,
    *,
    geometry_seed: Sequence[Finding] | None = None,
    on_folder: Any | None = None,
    # Opt-IN strictness: a partial-coverage run stays exit 0 by default
    # (it IS a real audit); set this for unattended/scheduled audits.
    fail_on_partial_coverage: bool = False,
) -> int:
    """Run Query → QC sequentially, dump findings.json, print summary.

    ``geometry_seed`` (P3-1) — pre-computed findings (audit axes) placed on
    the ``geometry_findings`` bucket so the reports render them; check mode
    has no DesignAgent, so they become report content only. ``on_folder``
    mirrors ``run_revit``'s hook (used by ``--audit`` to persist the axes
    artifacts into the just-created run folder).
    """
    element_group_id = os.environ.get("DEMO_ELEMENT_GROUP_ID")
    if not element_group_id:
        log.error("check.missing_env", missing="DEMO_ELEMENT_GROUP_ID")
        return 2

    autonomy = AutonomyPolicy.load(autonomy_path)
    qc = QCAgent(rules_path=rules_path, autonomy=autonomy)
    catalog = OSTCatalog.load()

    config = FormaMCPConfig.from_env()
    state: OrchestratorState = {
        "project_id": os.environ.get("DEMO_AECDM_PROJECT_ID", ""),
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }

    if geometry_seed:
        state["geometry_findings"] = list(geometry_seed)

    folder, collector, token = _start_run_recording("check")
    if on_folder is not None:
        on_folder(folder)
    try:
        async with FormaMCPClient(config) as client:
            query = QueryAgent(
                mcp=client,
                element_group_id=element_group_id,
                rules=qc.rules,
                catalog=catalog,
                config_dir=qc._config_dir,  # resolve pack-local lookup host-hop
            )
            state = await query.run(state)
            if state["status"] == "failed":
                log.error("check.query_failed", error=state["error"])
                _finish_run_recording(
                    folder, collector, token, state, status="failed", rules=qc.rules,
                    rules_paths=rules_path,
                )
                return 1

        state = qc.run(state)

        # P3-1: surface seeded axes findings in findings.json + report tables
        # (mirrors run_revit's post-graph extend for geometry findings).
        if geometry_seed:
            state["findings"].extend(geometry_seed)

        findings_out.write_text(json.dumps(state["findings"], indent=2, default=str))
        review_path, dataq_path = write_side_reports(state, findings_out)
        _print_outcomes_summary(state)
        _print_summary(state["findings"], findings_out)
        # L2-18: `--check` has no DesignAgent and builds no LLM agent, so every
        # BIM_LLM_* flag is inert here. That is correct — check mode proposes
        # nothing — but an operator who set the flags got no sign of it, and a
        # ruleset's `llm_propose` rules produced findings with no fix and no
        # explanation. Disclose rather than wire: adding a model to a read-only
        # mode would be a bigger change than the complaint.
        _stamp_llm_status(state, rules=qc.rules)
        _print_llm_status(state)
        _print_side_report_notice(state, review_path, dataq_path)
        # L5 (audit): resolve the exit code BEFORE the recording is finished —
        # `_finish_run_recording` writes trace.md and then drops the structlog
        # tap, so coverage diagnostics logged after it reached the console only
        # and never the run's own captured trace.
        rc = _exit_code_for(state, fail_on_partial_coverage=fail_on_partial_coverage)
        _finish_run_recording(
            folder, collector, token, state, status="completed", rules=qc.rules,
            rules_paths=rules_path,
        )
        _print_run_folder_notice(folder)
        return rc
    except Exception:
        _finish_run_recording(
            folder, collector, token, state, status="failed", rules=qc.rules,
            rules_paths=rules_path,
        )
        raise


def _console_safe(fn: Callable[..., None]) -> Callable[..., None]:
    """Make a console-summary helper unable to change a run's verdict.

    Every ``_print_*`` below runs INSIDE the try block whose ``except`` records
    the run as ``failed``. So an exception while *describing* the run rewrote
    what the run *was*: on 2026-07-29 a cp1252 console could not encode the
    arrow in "Elements → ACC Issues", and an audit that had converged — with
    every artifact already on disk — was recorded ``status: failed``, exit 1.

    ``_make_console_utf8_safe`` removes that particular cause. This removes the
    CLASS: the summary is a description of work already finished, so nothing it
    does may decide whether that work counts. The failure is logged, never
    swallowed silently — but the verdict belongs to the audit, not to the
    printer.
    """

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            log.warning(
                "console.summary_failed", helper=fn.__name__, error=str(exc),
                note="the run itself is unaffected — see the run folder for artifacts",
            )

    return _wrapped


@_console_safe
def _print_run_folder_notice(folder: RunFolder) -> None:
    """v1 task L+M: tell the user where the run artifacts landed."""
    print(f"Run folder:    {folder.root}")


def _exit_code_for(
    final_state: OrchestratorState, *, fail_on_partial_coverage: bool = False
) -> int:
    """Map a graph final state to a process exit code.

    Closes the loop `graph.after_query` already promised in its docstring:
    it routes a query failure straight to END so the failure is preserved,
    and `route_node` returns status="failed" on the iteration cap — but both
    `run()` and `run_revit()` then returned 0 regardless. The run record and
    the console summary said "failed" while every MACHINE consumer (shell
    ``$?``, CI, the service runner's ``rc == 0 -> job.status='done'``) read a
    successful audit. A compliance tool must never report success for a run
    that could not read the model or never converged.

    The second gate covers the quieter twin: a ruleset that resolved to NO
    query spec (every target category unknown or unsupported on this backend)
    fetches nothing, finds nothing, and converges — "all clean" for a model
    that was never audited at all. A PARTIAL plan (some categories resolved)
    IS a real audit, so it only warns by default — the coverage record and the
    report's PARTIAL COVERAGE banner carry the detail either way. Pass
    ``fail_on_partial_coverage`` (CLI ``--fail-on-partial-coverage`` or the
    audit profile's ``run.fail_on_partial_coverage``) to make it fail instead:
    strictness is opt-IN so an unattended production audit can demand full
    scope without flipping the default under every existing scheduled run.
    """
    if final_state.get("status") == "failed":
        return 1

    coverage = final_state.get("query_coverage") or {}
    dropped = coverage.get("categories_dropped") or []
    verdict = coverage_verdict(final_state)
    if verdict == "no_audit":
        log.error("run.no_query_coverage", coverage=coverage)
        print(
            "\n❌ No audit was performed: every target category failed to "
            "resolve, so no element was ever fetched or checked."
        )
        for item in dropped:
            print(f"   - {item.get('category')}: {item.get('reason')}")
        return 1

    if verdict == "partial":
        log.warning(
            "run.partial_query_coverage",
            coverage=coverage,
            fail_on_partial=fail_on_partial_coverage,
        )
        print(
            "\n⚠️  Partial coverage — these target categories were dropped and "
            "NOT audited:"
        )
        for item in dropped:
            print(f"   - {item.get('category')}: {item.get('reason')}")
        if fail_on_partial_coverage:
            print(
                "   → failing the run (--fail-on-partial-coverage): an "
                "unattended audit must not narrow its own scope in silence."
            )
            return 1

    # P1-GEO-01/02: the geometry half decides the exit code too. A geometry
    # rule that never executed — the MCP call failed, or its reference link
    # never resolved — produced an empty findings list, which used to be
    # indistinguishable from "checked, no clashes" all the way out to exit 0.
    # Unconditional, NOT behind --fail-on-partial-coverage: that flag is for a
    # narrowed scope; this is a check that did not happen at all.
    geo_verdict = geometry_verdict(final_state)
    if geo_verdict == "no_audit":
        geo_cov = final_state.get("geometry_coverage") or {}
        log.error("run.geometry_no_audit", coverage=geo_cov)
        print(
            "\n❌ GEOMETRY AUDIT DID NOT RUN — every geometry rule failed to "
            "execute, so no clash/clearance check was performed:"
        )
        for item in geo_cov.get("rules_failed") or []:
            print(f"   - {item.get('rule_id')}: {item.get('reason')}")
        print("   This run is NOT evidence that the model is clash-free.")
        return 1

    if geo_verdict == "partial":
        geo_cov = final_state.get("geometry_coverage") or {}
        log.warning("run.geometry_partial", coverage=geo_cov)
        print("\n⚠️  Partial geometry coverage — these rules did NOT run:")
        for item in geo_cov.get("rules_failed") or []:
            print(f"   - {item.get('rule_id')}: {item.get('reason')}")

    return 0


@_console_safe
def _print_side_report_notice(
    state: OrchestratorState, review_path: Path, dataq_path: Path
) -> None:
    """v1 task CC: tell the user where the manual-review + data-quality reports landed."""
    review_count = len(state.get("manual_review_items", []) or [])
    dataq_count = len(state.get("missing_data_items", []) or [])
    print(f"Manual-review queue ({review_count}): {review_path}")
    print(f"Data-quality report ({dataq_count}): {dataq_path}")


@_console_safe
def _print_iteration_cap_warning(state: OrchestratorState) -> None:
    """v1.5-R7 (cap-hit honesty): a one-line console warning when the run
    stopped because it hit ``max_iterations``, not because it converged —
    the fix set may not have reached a stable fixpoint (see graph.py:
    route_node and report_trace.detect_fix_interactions)."""
    if state.get("stop_reason") != "iteration_cap":
        return
    print(
        f"⚠ Stopped at the iteration cap (max_iterations="
        f"{state.get('max_iterations')}) — results may depend on fix order "
        "(not a stable fixpoint). Re-run or raise the cap."
    )


@_console_safe
def _print_outcomes_summary(state: OrchestratorState) -> None:
    """v1 task BB: print the 4-state outcome table before the non_compliant detail.

    Reads `outcomes_summary` from state. No-op when absent (e.g. tests that
    pre-fabricate a state dict without invoking QC).
    """
    summary = state.get("outcomes_summary")
    if not summary:
        return
    total = summary["total"]
    print("\n=== Compliance outcomes ===")
    print(f"  Compliant:      {summary['compliant']:>6} / {total}")
    print(f"  Non-Compliant:  {summary['non_compliant']:>6}")
    print(f"  Manual Review:  {summary['manual_review']:>6}")
    print(f"  Missing Data:   {summary['missing_data']:>6}")


@_console_safe
def _print_summary(findings: list[dict], out_path: Path) -> None:
    total = len(findings)
    high = sum(1 for f in findings if f["severity"] == "severity_high")
    medium = sum(1 for f in findings if f["severity"] == "severity_medium")
    low = sum(1 for f in findings if f["severity"] == "severity_low")
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1

    print(f"\nFound {total} non-compliant findings ({high} high, {medium} medium, {low} low)")
    for rule_id, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  - {count} {rule_id}")
    print(f"Written to: {out_path}")


# ---- apply (single pass) --------------------------------------------------

async def apply(
    rules_path: Path | Sequence[Path],
    autonomy_path: Path,
    findings_out: Path,
    *,
    limit: int,
    rule_filter: str | None,
    dry_run_only: bool,
    published: bool,
    issue_subtype_id: str | None,
    # Opt-IN strictness: a partial-coverage run stays exit 0 by default
    # (it IS a real audit); set this for unattended/scheduled audits.
    fail_on_partial_coverage: bool = False,
) -> int:
    """Run Query → QC → Design once. Creates ACC Issues for the first N findings."""
    element_group_id = os.environ.get("DEMO_ELEMENT_GROUP_ID")
    project_id = os.environ.get("DEMO_PROJECT_ID")
    if not element_group_id:
        log.error("apply.missing_env", missing="DEMO_ELEMENT_GROUP_ID")
        return 2
    if not project_id:
        log.error("apply.missing_env", missing="DEMO_PROJECT_ID")
        return 2

    autonomy = AutonomyPolicy.load(autonomy_path)
    qc = QCAgent(rules_path=rules_path, autonomy=autonomy)
    catalog = OSTCatalog.load()

    config = FormaMCPConfig.from_env()
    state: OrchestratorState = {
        "project_id": project_id,
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }

    folder, collector, token = _start_run_recording("apply")
    try:
        async with FormaMCPClient(config) as client:
            query = QueryAgent(
                mcp=client,
                element_group_id=element_group_id,
                rules=qc.rules,
                catalog=catalog,
                config_dir=qc._config_dir,  # resolve pack-local lookup host-hop
            )
            state = await query.run(state)
            if state["status"] == "failed":
                log.error("apply.query_failed", error=state["error"])
                _finish_run_recording(
                    folder, collector, token, state, status="failed", rules=qc.rules,
                    rules_paths=rules_path,
                )
                return 1

            state = qc.run(state)
            findings_out.write_text(json.dumps(state["findings"], indent=2, default=str))
            review_path, dataq_path = write_side_reports(state, findings_out)

            design = DesignAgent(
                mcp=client,
                autonomy=autonomy,
                project_id=project_id,
                max_issues=limit,
                rule_filter=rule_filter,
                dry_run_only=dry_run_only,
                published=published,
                issue_subtype_id=issue_subtype_id or os.environ.get("DEMO_ISSUE_SUBTYPE_ID"),
                # v1 task B: wire rules so the Expected: clause in the issue
                # body renders per-requirement (e.g. "Value matches /\\d{3}/").
                # Without this, the body falls back to a generic label.
                rules=qc.rules,
            )
            state = await design.run(state)

        _print_apply_summary(state, dry_run_only=dry_run_only)
        # L2-18: `apply()` builds a DesignAgent but never passes `llm_agent`, so
        # an `llm_propose` rule degrades to Path A here no matter what the flags
        # say. Same disclosure as `check()` and `run()`.
        _stamp_llm_status(state, rules=qc.rules)
        _print_llm_status(state)
        _print_side_report_notice(state, review_path, dataq_path)
        rc = _exit_code_for(  # L5: log inside the trace tap
            state, fail_on_partial_coverage=fail_on_partial_coverage)
        _finish_run_recording(
            folder, collector, token, state, status="completed", rules=qc.rules,
            rules_paths=rules_path,
        )
        _print_run_folder_notice(folder)
        return rc
    except Exception:
        _finish_run_recording(
            folder, collector, token, state, status="failed", rules=qc.rules,
            rules_paths=rules_path,
        )
        raise


@_console_safe
def _print_apply_summary(state: OrchestratorState, *, dry_run_only: bool) -> None:
    _print_outcomes_summary(state)
    total_findings = len(state["findings"])
    fixes = state["proposed_fixes"]
    executed = [f for f in fixes if f["executed"]]
    pending = [f for f in fixes if not f["executed"]]

    mode = "DRY-RUN (preview only)" if dry_run_only else "EXECUTE"
    print(f"\n=== Apply summary [{mode}] ===")
    print(f"Non-compliant findings: {total_findings}")
    print(f"Proposed fixes:    {len(fixes)}")
    print(f"Executed:          {len(executed)}")
    print(f"Pending approval:  {len(pending)}")
    if executed:
        print("\nCreated ACC Issues:")
        for fix in executed:
            issue = (fix.get("preview") or {}).get("executed_issue") or {}
            issue_id = issue.get("id", "?")
            display = issue.get("displayId") or ""
            title = issue.get("title", "?")
            print(f"  - #{display} {title}  (id={issue_id})")
    if pending:
        print("\nPending fixes (preview only):")
        for fix in pending:
            print(
                f"  - {fix['finding_id']}  "
                f"→ would set {fix['parameter']}={fix['new_value']!r}  "
                f"(autonomy={fix['autonomy']})"
            )


# ---- run (full cyclic graph) ----------------------------------------------

async def run(
    rules_path: Path | Sequence[Path],
    autonomy_path: Path,
    findings_out: Path,
    *,
    limit: int,
    rule_filter: str | None,
    dry_run_only: bool,
    published: bool,
    issue_subtype_id: str | None,
    max_iterations: int,
    checkpoint_dir: Path,
    bep_pdf: Path | None = None,
    # P1-CLI-01: `run_revit` took this from the start; `run` did not, so
    # `--run --bep-fixture` parsed, exited 0, and ingested nothing.
    use_bep_fixture: bool = False,
    vector_store_dir: Path | None = None,
    geometry_seed: Sequence[Finding] | None = None,
    on_folder: Any | None = None,
    propose_only: bool = False,
    issue_registry: Path | None = None,
    # Opt-IN strictness: a partial-coverage run stays exit 0 by default
    # (it IS a real audit); set this for unattended/scheduled audits.
    fail_on_partial_coverage: bool = False,
) -> int:
    """Run the full LangGraph cyclic graph to convergence or max iterations.

    ``geometry_seed`` / ``on_folder`` (P3-1) — see ``run_revit``: axes
    findings seed the shared geometry bucket (D5) so DesignAgent folds them
    into Path A on iteration 0; the hook lets ``--audit`` persist the axes
    artifacts into the run folder as soon as it exists.

    ``propose_only`` / ``issue_registry`` (Mức 1 continuous audit) are
    threaded through to ``DesignAgent`` for consistency with ``run_revit`` —
    ``propose_only`` is a practical no-op here (this path has no Revit
    write-back to demote), and ``issue_registry`` still dedups Path A ACC
    issues across scheduled runs.
    """
    element_group_id = os.environ.get("DEMO_ELEMENT_GROUP_ID")
    project_id = os.environ.get("DEMO_PROJECT_ID")
    if not element_group_id:
        log.error("run.missing_env", missing="DEMO_ELEMENT_GROUP_ID")
        return 2
    if not project_id:
        log.error("run.missing_env", missing="DEMO_PROJECT_ID")
        return 2

    autonomy = AutonomyPolicy.load(autonomy_path)
    qc = QCAgent(rules_path=rules_path, autonomy=autonomy)
    _lint_ruleset_advisory(qc.rules)
    catalog = OSTCatalog.load()

    grounding = _build_grounding(
        bep_pdf, vector_store_dir, qc_rules=qc.rules,
        use_bep_fixture=use_bep_fixture,
    )

    config = FormaMCPConfig.from_env()

    initial_state: OrchestratorState = {
        "project_id": project_id,
        "iteration": 0,
        "max_iterations": max_iterations,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }

    if geometry_seed:
        initial_state["geometry_findings"] = list(geometry_seed)

    folder, collector, token = _start_run_recording("run")
    if on_folder is not None:
        on_folder(folder)
    final_state: OrchestratorState = initial_state
    try:
        async with FormaMCPClient(config) as client:
            query = QueryAgent(
                mcp=client,
                element_group_id=element_group_id,
                rules=qc.rules,
                catalog=catalog,
                config_dir=qc._config_dir,  # resolve pack-local lookup host-hop
            )
            design = DesignAgent(
                mcp=client,
                autonomy=autonomy,
                project_id=project_id,
                max_issues=limit,
                rule_filter=rule_filter,
                dry_run_only=dry_run_only,
                published=published,
                issue_subtype_id=issue_subtype_id or os.environ.get("DEMO_ISSUE_SUBTYPE_ID"),
                # v1 task B: same rationale as in apply() above.
                rules=qc.rules,
                propose_only=propose_only,
                issue_registry=issue_registry,
            )
            llm_ctx = build_llm_run_context()  # GĐ3-B1: shared client + usage budget
            diagnostic_agent = make_diagnostic_agent(rules=qc.rules, ctx=llm_ctx)
            supervisor_agent = make_supervisor_agent(rules=qc.rules, ctx=llm_ctx)
            app = build_graph(
                query, qc, design,
                grounding_agent=grounding,
                diagnostic_agent=diagnostic_agent,
                supervisor_agent=supervisor_agent,
                checkpoint_dir=checkpoint_dir,
            )
            final_state = await app.ainvoke(initial_state)

        # P3-1: axes findings were issued via the geometry bucket; surface
        # them in findings.json + report tables (same as run_revit does).
        if geometry_seed:
            final_state["findings"].extend(geometry_seed)

        findings_out.write_text(json.dumps(final_state["findings"], indent=2, default=str))
        review_path, dataq_path = write_side_reports(final_state, findings_out)
        _print_run_summary(final_state)
        _stamp_llm_usage(final_state, llm_ctx)   # L2-08
        # L2-12: `run()` builds no remediation agent at all, so an `llm_propose`
        # rule ALWAYS degrades to Path A on this path. That is by design; the
        # objection is that the run never said so.
        _stamp_llm_status(
            final_state, rules=qc.rules,
            diagnostic_agent=diagnostic_agent, supervisor_agent=supervisor_agent,
        )
        _print_llm_usage(llm_ctx)
        _print_llm_status(final_state)
        _print_side_report_notice(final_state, review_path, dataq_path)
        rc = _exit_code_for(  # L5: log inside the trace tap
            final_state, fail_on_partial_coverage=fail_on_partial_coverage)
        _finish_run_recording(
            folder, collector, token, final_state,
            status=final_state.get("status", "unknown"), rules=qc.rules,
            rules_paths=rules_path,
        )
        _print_run_folder_notice(folder)
        return rc
    except Exception:
        _finish_run_recording(
            folder, collector, token, final_state, status="failed", rules=qc.rules,
            rules_paths=rules_path,
        )
        raise


def _build_grounding(
    bep_pdf: Path | None,
    vector_store_dir: Path | None,
    *,
    qc_rules: Any = None,
    use_bep_fixture: bool = False,
) -> GroundingAgent | None:
    """Construct a GroundingAgent if a BEP source is provided or required.

    Sources, in priority order:
      1. ``bep_pdf`` (real PDF) — ingested via ``store.ingest_pdf``.
      2. ``use_bep_fixture`` (synthetic) — ingests the Phase 2 W6 fixture
         (``rag/fixtures/bep_room_requirements.py``) as a stand-in for the
         real PDF. Same chunk/section/page shape — swap later for free.
      3. Neither, but ``qc_rules`` declares hard citations → proceed with
         an empty store; every hard-rule finding gets flagged
         ``citation_missing=True`` (option B).

    Returns None when there's no PDF, no fixture flag, and no hard rule.
    """
    # Detect hard rules to decide whether we need an (even empty) store
    has_hard_rule = bool(
        qc_rules and any(r.citation.mode == "hard" for r in qc_rules.rules)
    )

    if bep_pdf is None and not use_bep_fixture and not has_hard_rule:
        return None

    if bep_pdf is not None and not bep_pdf.exists():
        log.error("run.bep_pdf_missing", path=str(bep_pdf))
        raise FileNotFoundError(f"BEP PDF not found: {bep_pdf}")

    store = VectorStore(persist_dir=vector_store_dir, collection="bep")
    if bep_pdf is not None:
        chunks_added = store.ingest_pdf(bep_pdf, source=bep_pdf.name)
        log.info("run.bep_ingested", source=bep_pdf.name, chunks=chunks_added)
    elif use_bep_fixture:
        chunks_added = ingest_bep_room_requirements(store)
        log.info("run.bep_fixture_ingested", chunks=chunks_added)
    elif has_hard_rule:
        log.warning(
            "run.hard_rules_without_source",
            note="rules.yaml declares hard citation but no --bep-pdf or "
            "--bep-fixture provided; findings will be flagged "
            "citation_missing=True (option B)",
        )
    return GroundingAgent(store=store, rules=qc_rules, top_k=2, min_score=0.05)


@_console_safe
def _print_run_summary(state: OrchestratorState) -> None:
    _print_outcomes_summary(state)
    fixes = state.get("proposed_fixes") or []
    executed = [f for f in fixes if f.get("executed")]
    findings_count = len(state.get("findings", []))

    print(f"\n=== Run summary ===")
    print(f"Final status:      {state['status']}")
    print(f"Iterations:        {state['iteration']}")
    print(f"Non-compliant findings: {findings_count}")
    print(f"Issues created:    {len(executed)}")
    if state.get("error"):
        print(f"Error:             {state['error']}")
    _print_iteration_cap_warning(state)
    if executed:
        print("\nCreated ACC Issues:")
        for fix in executed:
            issue = (fix.get("preview") or {}).get("executed_issue") or {}
            print(f"  - #{issue.get('displayId', '?')} {issue.get('title', '?')}")


def _stamp_llm_usage(state: OrchestratorState | dict[str, Any], llm_ctx: Any) -> None:
    """Put the LLM usage record on the STATE so it reaches metadata.json (L2-08).

    P2-02 pinned the cloud default to a dated snapshot and started recording
    which model actually ran, on the argument that pinning is impossible for
    every model so "RECORDING what ran turns a silent change into a visible
    one — two runs can be compared." That comparison was never possible:
    ``UsageRecorder.summary()`` had no caller anywhere in ``src/``, and its only
    exit was ``format_line()`` → a ``print`` that is gone by the time anyone
    opens the run folder. So the artifact could not say which model produced
    its LLM-assisted content, nor whether Phase 2 ran at all.

    Stamping the state — rather than threading ``llm_ctx`` into the recorder —
    is the route ``query_coverage`` / ``geometry_coverage`` already take: one
    place decides, every artifact reads the same value.

    A Phase-1 run has no context and gets no key, so its artifacts stay
    byte-identical.
    """
    if llm_ctx is None:
        return
    try:
        state["llm_usage"] = llm_ctx.recorder.summary()  # type: ignore[typeddict-unknown-key]
    except Exception as exc:  # accounting must never break a finished run
        log.warning("llm.usage_stamp_failed", error=str(exc))


def _stamp_llm_status(
    state: OrchestratorState | dict[str, Any],
    *,
    rules: Any,
    remediation_agent: Any = None,
    diagnostic_agent: Any = None,
    supervisor_agent: Any = None,
) -> None:
    """Say what Phase 2 was ASKED for versus what was actually wired (L2-12).

    Two silences met here. A mistyped flag (``BIM_LLM_REMEDIATION=y``) produced
    a run byte-identical to Phase 1 with nothing saying why — the plugin-missing
    path at least logged, the typo path did not. And nobody ever compared the
    two ends: a ruleset can declare ``new_value_strategy: llm_propose`` on N
    rules while no remediation agent exists, in which case every one of those
    rules degrades to a Path A issue. That degrade is correct behaviour and the
    right default — the objection is that the run never said it happened, so
    "the AI proposed nothing today" and "the AI was never asked" produced the
    same artifact.

    Recorded rather than enforced, deliberately: an unwired agent yields a
    complete, valid audit, so failing the run would be disproportionate. What it
    must not do is stay invisible.

    Emits nothing when there is nothing to say (no flags, no LLM rules, no bad
    values), so a plain Phase-1 run's artifacts are unchanged.
    """
    try:
        requested = {
            "remediation": llm_remediation_enabled(),
            "diagnostic": llm_diagnostic_enabled(),
            "supervisor": llm_supervisor_enabled(),
        }
        wired = {
            "remediation": remediation_agent is not None,
            "diagnostic": diagnostic_agent is not None,
            "supervisor": supervisor_agent is not None,
        }
        rule_list = getattr(rules, "rules", None) or []
        llm_rules = [
            r.id
            for r in rule_list
            if getattr(getattr(r, "remediation", None), "new_value_strategy", None)
            == "llm_propose"
        ]
        problems = llm_flag_problems()
        if not any(requested.values()) and not llm_rules and not problems:
            return
        status = {
            "requested": requested,
            "wired": wired,
            "flag_problems": problems,
            "rules_requesting_llm": llm_rules,
            # The gap that matters to a reader of the findings: rules asked for
            # a model-proposed value and no model was available to propose one.
            "llm_rules_degraded_to_path_a": bool(llm_rules and not wired["remediation"]),
        }
        state["llm_status"] = status  # type: ignore[typeddict-unknown-key]
        for p in problems:
            log.error(
                "llm.flag_not_understood", flag=p["flag"], value=p["value"],
                note="treated as OFF — this run is deterministic",
            )
        for name, want in requested.items():
            if want and not wired[name]:
                log.error(
                    "llm.agent_requested_but_not_wired", agent=name,
                    note="flag is on but no agent was built (plugin missing or "
                    "incompatible) — that half of Phase 2 did not run",
                )
        if status["llm_rules_degraded_to_path_a"]:
            log.warning(
                "llm.rules_degraded_to_path_a",
                rules=llm_rules, count=len(llm_rules),
                note="rules ask for an LLM-proposed value but no remediation "
                "agent is wired; their findings became Path A issues",
            )
    except Exception as exc:  # disclosure must never break a finished run
        log.warning("llm.status_stamp_failed", error=str(exc))


@_console_safe
def _print_llm_status(state: OrchestratorState | dict[str, Any]) -> None:
    """Put the same gap on the operator's screen — the log line is for the
    record, this is for the person who is about to read the findings."""
    status = (state or {}).get("llm_status") or {}
    if not status:
        return
    for p in status.get("flag_problems") or []:
        print(
            f"⚠️  {p['flag']}={p['value']!r} is not a value I understand — "
            "treated as OFF, so this run is fully deterministic."
        )
    for name, want in (status.get("requested") or {}).items():
        if want and not (status.get("wired") or {}).get(name):
            print(
                f"⚠️  LLM {name} agent was requested but could not be built — "
                "that half of Phase 2 did not run."
            )
    if status.get("llm_rules_degraded_to_path_a"):
        n = len(status.get("rules_requesting_llm") or [])
        print(
            f"Note:  {n} rule(s) ask for an AI-proposed value and no remediation "
            "agent is wired — their findings became Path A issues, not auto-fixes."
        )


@_console_safe
def _print_llm_usage(llm_ctx: Any) -> None:
    """GĐ3-B1: append the per-run LLM usage line (calls · time · per-agent), or
    nothing on a pure Phase-1 run (no context) / when no call was made."""
    if llm_ctx is None:
        return
    line = llm_ctx.recorder.format_line()
    if line:
        print(line)


# ---- run-revit (Phase 2 Week 6 Day 4 — live E2E with path B) -------------


async def run_revit(
    rules_path: Path | Sequence[Path],
    autonomy_path: Path,
    findings_out: Path,
    *,
    limit: int,
    rule_filter: str | None,
    dry_run_only: bool,
    published: bool,
    issue_subtype_id: str | None,
    max_iterations: int,
    checkpoint_dir: Path,
    bep_pdf: Path | None,
    use_bep_fixture: bool,
    vector_store_dir: Path | None,
    skip_forma: bool = False,
    fetch_concurrency: int = 4,
    max_elements: int = 300,
    bulk_fields: bool = False,
    approvals_dir: Path = DEFAULT_APPROVALS_DIR,
    revit_client_factory: Any | None = None,
    forma_client_factory: Any | None = None,
    project_id: str | None = None,
    on_folder: Any | None = None,
    banner: str | None = None,
    geometry_seed: Sequence[Finding] | None = None,
    propose_only: bool = False,
    issue_registry: Path | None = None,
    # Opt-IN strictness: a partial-coverage run stays exit 0 by default
    # (it IS a real audit); set this for unattended/scheduled audits.
    fail_on_partial_coverage: bool = False,
) -> int:
    """Live E2E run against Revit + Forma simultaneously.

    Routes by ``rule.fixability``:
      * manual → ACC Issue via Forma (Phase 1 path A)
      * auto   → Revit parameter write via Revit MCP (Phase 2 W6 path B)

    ``revit_client_factory`` / ``forma_client_factory`` — pass an already-
    constructed async-context-manager client (real OR mock; both
    ``RevitHTTPClient``/``MockRevitMCPClient`` and ``FormaMCPClient``/
    ``MockFormaMCPClient`` implement ``__aenter__``/``__aexit__``) to bypass
    the env-driven production construction (``make_revit_client()`` /
    ``FormaMCPConfig.from_env()``). Used by ``--demo`` to inject the mock
    clients without ever touching ``make_revit_client``/``FormaMCPConfig``
    (no network, no env vars). ``None`` (default) preserves the exact
    pre-existing behaviour for every other caller. ``project_id`` similarly
    overrides ``$DEMO_PROJECT_ID`` when set. ``on_folder`` (optional), when
    given, is called once with the ``RunFolder`` right after it's created —
    lets a caller (``--demo``) report the run folder path without adding a
    second return channel to this function. ``banner`` (v1.5-R6, 2.4) is
    forwarded verbatim to the verification report (e.g. --demo's "simulated
    model" notice); None for every other caller. ``propose_only`` /
    ``issue_registry`` (Mức 1 continuous audit) are threaded straight to
    ``DesignAgent`` — both default to the legacy no-op value, so a bare
    ``--run-revit`` invocation is unaffected; only ``--audit`` sets them.
    """
    project_id = project_id or os.environ.get("DEMO_PROJECT_ID")
    if not project_id:
        log.error("run_revit.missing_env", missing="DEMO_PROJECT_ID")
        return 2

    autonomy = AutonomyPolicy.load(autonomy_path)
    qc = QCAgent(rules_path=rules_path, autonomy=autonomy)
    _lint_ruleset_advisory(qc.rules)
    catalog = OSTCatalog.load()
    grounding = _build_grounding(
        bep_pdf, vector_store_dir,
        qc_rules=qc.rules,
        use_bep_fixture=use_bep_fixture,
    )

    # Only touch env/production construction when the caller didn't already
    # inject a client — --demo passes mocks here and neither of these lines
    # ever executes for it (no network, no env vars read).
    if revit_client_factory is None:
        revit_client_factory = make_revit_client()
    forma_config: FormaMCPConfig | None = None
    if forma_client_factory is None and not skip_forma:
        forma_config = FormaMCPConfig.from_env()

    initial_state: OrchestratorState = {
        "project_id": project_id,
        "iteration": 0,
        "max_iterations": max_iterations,
        "elements": [],
        "findings": [],
        "proposed_fixes": [],
        "status": "init",
        "error": None,
    }

    # GĐ3-B1: one shared LLM client + usage/budget recorder for the whole run,
    # captured by the closure below so Remediation / Diagnostic / Supervisor all
    # meter into it. None on a pure Phase-1 run (no flags) → zero overhead.
    llm_ctx = build_llm_run_context()
    # L2-12: the three agents are built inside the closure below; this carries
    # WHICH of them actually came back out, so the disclosure after the run can
    # compare "asked for" against "wired" instead of assuming they match.
    llm_agents: dict[str, Any] = {}

    # Note: we deliberately don't nest Revit + Forma in the same
    # AsyncExitStack — both spawn anyio task groups under the hood, and a
    # failure during Forma init leaves the outer cleanup unable to drain
    # Revit's cancel scope (RuntimeError: "Attempted to exit cancel scope
    # in a different task..."). Pass --no-forma when ACC isn't configured.
    async def _run_with_forma(forma_client: FormaMCPClient | None) -> OrchestratorState:
        async with revit_client_factory as revit_client:
            doc_identity = await _fetch_document_identity(revit_client)
            # v1.3: unified RevitQueryAgent derives categories + params +
            # follow_host directly from the RuleSet via the OST catalog.
            # The old _build_revit_query_agent + _OST_BY_LABEL dispatch is gone.
            query = RevitQueryAgent(
                mcp=revit_client,
                rules=qc.rules,
                catalog=catalog,
                fetch_concurrency=fetch_concurrency,
                max_elements_per_category=max_elements,
                bulk_fields=bulk_fields,
                config_dir=qc._config_dir,  # resolve pack-local lookup host-hop
            )
            # Phase 2 (feature-flagged): runtime remediation intelligence for
            # rules with `new_value_strategy: llm_propose`. None unless
            # BIM_LLM_REMEDIATION is set → Phase 1 behaviour is the default.
            design = DesignAgent(
                mcp=forma_client,
                autonomy=autonomy,
                project_id=project_id,
                max_issues=limit,
                rule_filter=rule_filter,
                dry_run_only=dry_run_only,
                published=published,
                issue_subtype_id=issue_subtype_id or os.environ.get("DEMO_ISSUE_SUBTYPE_ID"),
                revit_mcp=revit_client,
                rules=qc.rules,
                approvals_dir=approvals_dir,
                llm_agent=llm_agents.setdefault(
                    "remediation", make_remediation_agent(ctx=llm_ctx)
                ),
                propose_only=propose_only,
                issue_registry=issue_registry,
                max_elements=max_elements,
            )

            # v1.4-J: geometry rules — run before or instead of the graph.
            # P3-1: audit-axes findings (if any) pre-seed the same bucket —
            # EXTEND, never overwrite, so model-checked geometry rules and
            # IFC axes coexist in one run (D5).
            geo_findings: list[Finding] = list(geometry_seed or [])
            geo_coverage: dict[str, Any] = {}
            if qc.rules.geometry_rules:
                from bim_orchestrator.agents.geometry_query import GeometricQueryAgent
                geo_agent = GeometricQueryAgent(
                    mcp=revit_client,
                    geometry_rules=qc.rules.geometry_rules,
                    max_elements=max_elements,
                )
                geo_findings.extend(await geo_agent.run())
                # P1-GEO-01: carry the execution evidence, not just the
                # findings. Without it, "0 violations" and "nothing ran" are
                # the same state from here on.
                geo_coverage = geo_agent.coverage
                log.info(
                    "run_revit.geometry_check_done",
                    violations=len(geo_findings),
                    verdict=geo_coverage.get("verdict"),
                    executed=len(geo_coverage.get("rules_executed") or []),
                    failed=len(geo_coverage.get("rules_failed") or []),
                )

            if not qc.rules.rules:
                # Geometry-only YAML: bypass the LangGraph loop entirely.
                # Geometry findings go through the shared bucket so DesignAgent
                # groups them per element (v1.4-K7) into ACC Issues.
                pre_design: OrchestratorState = {
                    **initial_state,
                    "elements": [],
                    "findings": [],
                    "geometry_findings": geo_findings,  # type: ignore[typeddict-item]
                    "geometry_coverage": geo_coverage,  # type: ignore[typeddict-item]
                    "iteration": 0,
                    "status": "checking",
                }
                out = await design.run(pre_design)
                out["findings"] = geo_findings  # type: ignore[typeddict-item]
                out["geometry_coverage"] = geo_coverage  # type: ignore[typeddict-item]
                out["document_info"] = doc_identity
                return out

            # Parameter rules present: run the full graph, seeding the shared
            # geometry bucket so the design node (iteration 0) folds geometry
            # findings into the per-element Path A grouping — an element flagged
            # by both a parameter rule and a geometry rule gets ONE ACC Issue
            # (v1.4-K7 Tầng 3 coordination), not a separate second design pass.
            app = build_graph(
                query, qc, design,
                grounding_agent=grounding,
                diagnostic_agent=llm_agents.setdefault(
                    "diagnostic", make_diagnostic_agent(rules=qc.rules, ctx=llm_ctx)
                ),
                supervisor_agent=llm_agents.setdefault(
                    "supervisor", make_supervisor_agent(rules=qc.rules, ctx=llm_ctx)
                ),
                checkpoint_dir=checkpoint_dir,
            )
            final_state = await app.ainvoke(
                {**initial_state, "geometry_findings": geo_findings}
            )

            # Surface geometry findings in `findings` for reporting / findings_out
            # (they were issued via the bucket, not via the param findings list).
            if geo_findings:
                final_state["findings"].extend(geo_findings)  # type: ignore[arg-type]

            # P1-GEO-01: the mixed path needs this as much as the geometry-only
            # one — arguably more. Passing parameter rules must not let a failed
            # geometry half be summarised as "compliant".
            if geo_coverage:
                final_state["geometry_coverage"] = geo_coverage  # type: ignore[typeddict-item]
            final_state["document_info"] = doc_identity
            return final_state

    folder, collector, token = _start_run_recording("run-revit")
    if on_folder is not None:
        on_folder(folder)
    final_state: OrchestratorState = initial_state
    try:
        if skip_forma:
            log.info("run_revit.forma_skipped", reason="--no-forma flag")
            final_state = await _run_with_forma(None)
        elif forma_client_factory is not None:
            async with forma_client_factory as forma_client:
                final_state = await _run_with_forma(forma_client)
        else:
            async with FormaMCPClient(forma_config) as forma_client:
                final_state = await _run_with_forma(forma_client)

        findings_out.write_text(json.dumps(final_state["findings"], indent=2, default=str))
        review_path, dataq_path = write_side_reports(final_state, findings_out)
        _print_run_revit_summary(final_state, dry_run_only=dry_run_only)
        _stamp_llm_usage(final_state, llm_ctx)   # L2-08
        _stamp_llm_status(   # L2-12
            final_state, rules=qc.rules,
            remediation_agent=llm_agents.get("remediation"),
            diagnostic_agent=llm_agents.get("diagnostic"),
            supervisor_agent=llm_agents.get("supervisor"),
        )
        _print_llm_usage(llm_ctx)
        _print_llm_status(final_state)
        _print_side_report_notice(final_state, review_path, dataq_path)
        rc = _exit_code_for(  # L5: log inside the trace tap
            final_state, fail_on_partial_coverage=fail_on_partial_coverage)
        _finish_run_recording(
            folder, collector, token, final_state,
            status=final_state.get("status", "unknown"), rules=qc.rules,
            rules_paths=rules_path, max_elements=max_elements, banner=banner,
        )
        _print_run_folder_notice(folder)
        return rc
    except Exception:
        _finish_run_recording(
            folder, collector, token, final_state, status="failed", rules=qc.rules,
            rules_paths=rules_path, max_elements=max_elements, banner=banner,
        )
        raise


# ---- audit (Phase 3: LOI rules + IFC axes in ONE run) ----------------------

async def audit(
    profile_path: Path,
    autonomy_path: Path,
    findings_out: Path,
    *,
    max_iterations: int,
    checkpoint_dir: Path,
    approvals_dir: Path = DEFAULT_APPROVALS_DIR,
    services_path: Path | None = None,
    on_folder: Any | None = None,
) -> int:
    """``--audit <profile>``: run the audit axes ONCE, then the profile's run
    mode with the axes findings pre-seeded on the geometry bucket (D5).

    ``on_folder`` (P3-2) — optional extra hook composed AFTER the internal
    axes-persist hook; the AuditHub service uses it to learn the run_id the
    moment the run folder exists (POST /audits returns before the run does).

    Sequence: load profile (fail-fast validation) → run enabled satellites
    into a staging dir → dispatch ``check`` / ``run`` / ``run_revit`` per
    ``profile.run.mode``, passing the findings as ``geometry_seed`` and an
    ``on_folder`` hook that copies the staged ``axes/`` artifacts into the
    run folder the moment it exists — so ``_finish_run_recording`` can render
    the reports' "Audit axes" section FROM the saved envelopes.
    """
    import shutil
    import tempfile

    from bim_orchestrator.audit_axes import persist_axes_dir, run_audit_axes
    from bim_orchestrator.policies.audit_profile import (
        load_audit_profile,
        load_audit_services,
    )
    from bim_orchestrator.unattended import UnattendedSession, persist_unattended_dir

    try:
        profile = load_audit_profile(profile_path)
    except ValueError as exc:
        log.error("audit.profile_invalid", error=str(exc))
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    if not profile.rules:
        print(
            "❌ audit profile declares no rules files — the LOI axis is "
            "required at P3 (axes-only audits are not supported yet).",
            file=sys.stderr,
        )
        return 2

    services = load_audit_services(services_path)
    staging = Path(tempfile.mkdtemp(prefix="autoaudit-axes-"))
    try:
        axes = await run_audit_axes(profile, services, staging)
        for note in axes.skipped:
            # No silent caps: unconfigured/errored axes go to the console AND
            # the report (axes_summary.json → "Skipped / coverage gaps").
            print(f"⚠ Audit axis skipped: {note}")
        if axes.findings:
            print(f"Audit axes: {len(axes.findings)} finding(s) from IFC satellites")

        rules_paths = [Path(r) for r in profile.rules]
        seed = axes.findings or None

        def _persist(folder: RunFolder) -> None:
            # Q5: stamp the profile identity into the run folder — the ONLY
            # thing that makes ``delta_report.find_baseline`` able to compare
            # like-with-like across scheduled runs (see delta_report.py).
            # Rules are recorded by BASENAME — an absolute path differs
            # machine-to-machine.
            (folder.root / "profile.json").write_text(
                json.dumps({
                    "profile_name": profile.name,
                    "mode": profile.run.mode,
                    "rules": [Path(r).name for r in profile.rules],
                    "propose_only": profile.run.propose_only,
                }, indent=2),
                encoding="utf-8",
            )
            persist_axes_dir(staging, folder.root)
            persist_unattended_dir(staging, folder.root)
            # U-01: this copy happens at run-folder CREATION, so it cannot
            # contain anything the watchdog writes later — including the PAUSED
            # flag that says it stopped supervising. Hand the session the run
            # folder so its ``__aexit__`` can copy the final state, once the
            # watchdog is stopped. (``session_cm`` is bound below; this closure
            # only runs during the ``async with``, so it is always set by then.)
            if isinstance(session_cm, UnattendedSession):
                session_cm.run_root = folder.root
            if on_folder is not None:
                on_folder(folder)

        mode = profile.run.mode
        unattended = profile.unattended
        # P3-3: unattended.enabled requires a configured RevitControl satellite
        # (load_audit_profile already ensured revit_exe/model_path/revit_version
        # are non-empty when enabled). Missing/unreachable RevitControl degrades
        # honestly to an ATTENDED run rather than crashing the audit.
        use_unattended = (
            unattended.enabled
            and services.revitcontrol is not None
            and services.revitcontrol.exists()
        )
        if unattended.enabled and not use_unattended:
            print(
                "⚠ unattended requested but RevitControl unconfigured — "
                "running attended",
                file=sys.stderr,
            )
        session_cm = (
            UnattendedSession(
                services=services,
                revit_exe=unattended.revit_exe or "",
                model_path=unattended.model_path or "",
                revit_version=unattended.revit_version or 0,
                staging_dir=staging,
            )
            if use_unattended
            else nullcontext()
        )

        async with session_cm:
            if mode == "demo":
                # AU demo package: same orchestration as --demo (mock Demo
                # Villa clients, zero network) but reachable through the
                # audit profile / POST /audits path the UI uses. max_issues
                # doubles as the issue quota (`limit`) exactly like the
                # other branches; >=4 iterations for the same fingerprint
                # reason documented on demo().
                from bim_orchestrator.demo import DEMO_PROJECT_ID, build_demo_clients

                demo_revit, demo_forma = build_demo_clients()
                return await run_revit(
                    rules_paths, autonomy_path, findings_out,
                    limit=profile.run.max_issues,
                    rule_filter=None,
                    dry_run_only=profile.run.dry_run,
                    published=False,
                    issue_subtype_id=None,
                    max_iterations=max(max_iterations, 4),
                    checkpoint_dir=checkpoint_dir,
                    bep_pdf=None,
                    use_bep_fixture=False,
                    vector_store_dir=None,
                    max_elements=profile.run.max_elements,
                    # C-1 deliberately does NOT split this branch: the AU
                    # webUI arc (AU_DEMO_RUNBOOK beats 6-7) runs `mode: demo`
                    # THROUGH the service precisely so its proposals show up
                    # in the service's Approvals view — mixing is the point
                    # of that demo. The CLI `--demo` journey (no UI) is the
                    # one that gets the separate default dir; the watcher's
                    # demo-record guard (C-2) keeps these records from ever
                    # poisoning a real ApprovalWatcher pass.
                    approvals_dir=approvals_dir,
                    geometry_seed=seed,
                    on_folder=_persist,
                    revit_client_factory=demo_revit,
                    forma_client_factory=demo_forma,
                    project_id=DEMO_PROJECT_ID,
                    banner=_DEMO_REPORT_BANNER,
                    propose_only=profile.run.propose_only,
                    fail_on_partial_coverage=profile.run.fail_on_partial_coverage,
                    issue_registry=DEFAULT_RUNS_DIR / "issue_registry.json",
                )
            if mode == "check":
                return await check(
                    rules_paths, autonomy_path, findings_out,
                    geometry_seed=seed, on_folder=_persist,
                    fail_on_partial_coverage=profile.run.fail_on_partial_coverage,
                )
            if mode == "run":
                return await run(
                    rules_paths, autonomy_path, findings_out,
                    limit=profile.run.max_issues,
                    rule_filter=None,
                    dry_run_only=profile.run.dry_run,
                    published=True,
                    issue_subtype_id=None,
                    max_iterations=max_iterations,
                    checkpoint_dir=checkpoint_dir,
                    geometry_seed=seed,
                    on_folder=_persist,
                    propose_only=profile.run.propose_only,
                    fail_on_partial_coverage=profile.run.fail_on_partial_coverage,
                    issue_registry=DEFAULT_RUNS_DIR / "issue_registry.json",
                )
            # mode == "run_revit"
            return await run_revit(
                rules_paths, autonomy_path, findings_out,
                limit=profile.run.max_issues,
                rule_filter=None,
                dry_run_only=profile.run.dry_run,
                published=True,
                issue_subtype_id=None,
                max_iterations=max_iterations,
                checkpoint_dir=checkpoint_dir,
                bep_pdf=None,
                use_bep_fixture=False,
                vector_store_dir=None,
                max_elements=profile.run.max_elements,
                approvals_dir=approvals_dir,
                geometry_seed=seed,
                on_folder=_persist,
                propose_only=profile.run.propose_only,
                    fail_on_partial_coverage=profile.run.fail_on_partial_coverage,
                issue_registry=DEFAULT_RUNS_DIR / "issue_registry.json",
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@_console_safe
def _print_run_revit_summary(
    state: OrchestratorState, *, dry_run_only: bool
) -> None:
    _print_outcomes_summary(state)
    findings = state.get("findings") or []
    fixes = state.get("proposed_fixes") or []

    # Path classification from preview shape (see DesignAgent)
    path_b_executed: list[dict[str, Any]] = []
    path_a_executed: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    for fix in fixes:
        preview = fix.get("preview") or {}
        if not fix.get("executed"):
            parked.append(fix)
            continue
        # v1.4-K4: Path A issues carry "executed_issue"; Path B writes carry
        # "executed_via" (revit_batch or per_element).
        if "executed_issue" in preview:
            path_a_executed.append(fix)
        else:
            path_b_executed.append(fix)

    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule_id"]] = by_rule.get(f["rule_id"], 0) + 1

    # v1.4-K22: distinct approve-gated PROPOSAL issues (one per rule) live among
    # the parked fixes, tagged with their proposal_issue_id. Count them so the
    # summary can say "elements → issues" honestly (auto-fix proposals are NOT
    # "executed" — they wait for human approval in the Approvals tab).
    # An id appearing here does NOT mean an issue was raised this run: a
    # proposal parked by a PREVIOUS run is reused, not recreated. Counting
    # distinct ids reported three "created" on a run that created one.
    proposal_ids: dict[str, str] = {}
    for f in parked:
        preview = f.get("preview") or {}
        issue_id = preview.get("proposal_issue_id")
        if not issue_id:
            continue
        origin = preview.get("proposal_origin") or "created"
        # Within one run the same issue is seen once per fix; "created" wins
        # over the later "reused_this_run" stamps for the same id.
        if issue_id not in proposal_ids or origin == "created":
            proposal_ids[issue_id] = origin
    n_proposal_new = sum(1 for o_ in proposal_ids.values() if o_ == "created")
    n_proposal_reused = len(proposal_ids) - n_proposal_new
    outcomes = state.get("outcomes_summary") or {}
    n_nc = outcomes.get("non_compliant", 0)
    n_md = outcomes.get("missing_data", 0)
    n_proposal = len(proposal_ids)
    n_manual = len(path_a_executed)

    mode = "DRY-RUN (preview only)" if dry_run_only else "EXECUTE"
    print(f"\n=== Run-Revit summary [{mode}] ===")
    print(f"Final status:       {state['status']}")
    print(f"Iterations:         {state['iteration']}")
    _print_iteration_cap_warning(state)

    # The headline a BIM Manager actually wants: how many problem elements, and
    # how many ACC issues they turned into (each issue groups one rule).
    print("\n--- Elements → ACC Issues ---")
    print(f"  Detected:            {n_nc} non-compliant + {n_md} missing-data elements")
    n_total_issues = n_proposal + n_manual
    if n_proposal_reused:
        # Say what was raised versus what was already open, or a nightly run
        # reads as if it re-filed everything every night.
        print(
            f"  ACC Issues:          {n_total_issues} represented  "
            f"(1 issue per rule)"
        )
        print(
            f"    \u00b7 created this run:  {n_total_issues - n_proposal_reused}"
        )
        print(
            f"    \u00b7 reused (already open from an earlier run): "
            f"{n_proposal_reused}"
        )
    else:
        print(f"  ACC Issues created:  {n_total_issues}  (1 issue per rule)")
    print(f"    · {n_proposal:>3} auto-fix proposal(s)  → review/approve in the Approvals tab")
    print(f"    · {n_manual:>3} manual issue(s)        → Path A (someone fixes by hand)")
    if path_b_executed:
        print(f"  Revit auto-writes (no issue):  {len(path_b_executed)} element(s)")

    if path_b_executed:
        via = {(f.get("preview") or {}).get("executed_via") for f in path_b_executed}
        note = " (per-element — addin lacks HTTP batch)" if "per_element" in via else ""
        print(f"\nRevit parameter writes{note}:")
        for fix in path_b_executed:
            print(
                f"  - element {fix['element_id']}: "
                f"{fix['parameter']} → '{fix['new_value']}'"
            )
    if path_a_executed:
        print("\nManual ACC Issues (Path A) created:")
        for fix in path_a_executed:
            issue = ((fix.get("preview") or {}).get("executed_issue") or {})
            print(
                f"  - #{issue.get('displayId', '?')} {issue.get('title', '?')}"
            )
    if proposal_ids:
        print(f"\nAuto-fix proposal issues (approve-gated): {n_proposal} "
              "— open the Approvals tab to apply.")
    if state.get("error"):
        print(f"\nError: {state['error']}")


# ---- demo (SPEC_DEMO_MODE.md — no Revit, no ACC, no API key) --------------

_DEMO_BANNER = (
    "\n"
    "=== DEMO MODE — \"Demo Villa\" (simulated model) ===\n"
    "Reasoning is live (rules engine · QC · DesignAgent · trust pipeline "
    "· verification report).\n"
    "Data is staged (mock Revit + Forma clients — zero Revit, zero ACC, "
    "zero API key, zero network).\n"
)

# v1.5-R6 (2.4): same notice, one line, threaded into the verification report
# itself (the console banner above is only ever seen by the person who ran
# the command — a report opened later by someone else has no other signal
# that the elements/parameters are simulated, not a live model).
_DEMO_REPORT_BANNER = (
    "⚠ DEMO MODE — simulated model (mock Revit + ACC); reasoning & report "
    "pipeline are the real production path."
)


async def demo(
    rules_path: Path | Sequence[Path],
    *,
    autonomy_path: Path = DEFAULT_AUTONOMY_PATH,
    findings_out: Path = DEFAULT_FINDINGS_OUT,
    max_iterations: int = 4,
    checkpoint_dir: Path = DEFAULT_CHECKPOINT_DIR,
    # C-1: simulated proposals never share the production approvals dir.
    approvals_dir: Path = DEFAULT_DEMO_APPROVALS_DIR,
) -> int:
    """``--demo``: the full compliance loop over the mock "Demo Villa" model.

    Reuses ``run_revit()`` wholesale — Query -> QC -> Design -> loop until
    convergence -> verification report — with only the client factories and
    the rules default swapped (see ``run_revit``'s ``revit_client_factory`` /
    ``forma_client_factory`` / ``project_id`` params). This is deliberate:
    the demo path and the production path share every line of orchestration
    logic, so what a visitor sees in ``--demo`` is not a simplified stand-in
    — it's the real loop.

    ``max_iterations=4`` (vs. the CLI default of 3): mixing an immediately-
    executed auto-fix with a still-parked approve-gated fix in the same run
    changes the findings SET between iterations even though the approve-
    gated fix itself never resolves, so route_node's fingerprint check needs
    one extra iteration to notice nothing further is changing (see
    graph.py:route_node). ``limit=10`` (generous, hardcoded — not the CLI's
    tunable ``--limit``): the fixed 20-element dataset has exactly 5 rule
    groups; a small quota would silently drop one of them and hide part of
    the demo.
    """
    from bim_orchestrator.demo import DEMO_PROJECT_ID, build_demo_clients

    print(_DEMO_BANNER)
    revit_client, forma_client = build_demo_clients()

    captured_folder: RunFolder | None = None

    def _capture(folder: RunFolder) -> None:
        nonlocal captured_folder
        captured_folder = folder

    exit_code = await run_revit(
        rules_path,
        autonomy_path,
        findings_out,
        limit=10,
        rule_filter=None,
        dry_run_only=False,
        published=False,
        issue_subtype_id=None,
        max_iterations=max_iterations,
        checkpoint_dir=checkpoint_dir,
        bep_pdf=None,
        use_bep_fixture=False,
        vector_store_dir=None,
        skip_forma=False,
        approvals_dir=approvals_dir,
        revit_client_factory=revit_client,
        forma_client_factory=forma_client,
        project_id=DEMO_PROJECT_ID,
        on_folder=_capture,
        banner=_DEMO_REPORT_BANNER,
    )

    if captured_folder is not None:
        report_path = captured_folder.root / "verification_report.md"
        print("\n=== Next steps ===")
        print(f"1. Run folder:              {captured_folder.root}")
        print(f"2. Open the verification report: {report_path}")
        print(
            "3. Try it yourself: edit config/rules.demo.yaml (e.g. the door-"
            "width threshold) and re-run `bim-orchestrator --demo` to see "
            "the verdict change."
        )
        print(
            "4. Ready for the real thing? Point --run-revit at a live Revit "
            "session (see README — \"Try it in 5 minutes\")."
        )
    return exit_code


# ---- list-revit-rooms (Phase 2 Week 6 Day 1 — Revit MCP smoke) -----------


async def verify_catalog_cmd(
    *, skip_revit: bool = False, skip_forma: bool = False
) -> int:
    """v1.3 polish: probe OSTCatalog against the live Revit + AECDM session.

    Runs each catalog entry through whatever backends are available:
      * Revit MCP via ``revit_list_categories`` (returns OST_* present in
        the active document with instance counts).
      * AECDM via ``aecdm_query_elements`` per entry; null-aecdm-label
        entries get probed with their ``display`` label as a fallback
        guess — when that succeeds, the recommendations list the entry
        as fillable.

    Output: ``runs/catalog_verify_<timestamp>.md`` + stdout summary.
    """
    from datetime import datetime

    from bim_orchestrator.policies.ost_catalog_verify import verify_catalog

    catalog = OSTCatalog.load()

    revit_client_cm = None
    forma_client_cm = None
    revit_client = None
    forma_client = None
    element_group_id = os.environ.get("DEMO_ELEMENT_GROUP_ID")

    if not skip_revit:
        revit_client_cm = make_revit_client()
    if not skip_forma:
        if not element_group_id:
            log.warning(
                "verify_catalog.forma_env_missing",
                note="DEMO_ELEMENT_GROUP_ID not set; skipping AECDM probe",
            )
            skip_forma = True
        else:
            forma_config = FormaMCPConfig.from_env()
            forma_client_cm = FormaMCPClient(forma_config)

    try:
        if revit_client_cm is not None:
            revit_client = await revit_client_cm.__aenter__()
        if forma_client_cm is not None:
            forma_client = await forma_client_cm.__aenter__()

        report = await verify_catalog(
            catalog,
            revit_mcp=revit_client,
            forma_mcp=forma_client,
            element_group_id=element_group_id if forma_client else None,
        )
    finally:
        # Tear down in reverse order. Best-effort — partial failure during
        # init shouldn't mask the user's terminal output.
        if forma_client_cm is not None:
            try:
                await forma_client_cm.__aexit__(None, None, None)
            except Exception:
                log.warning("verify_catalog.forma_close_failed", exc_info=True)
        if revit_client_cm is not None:
            try:
                await revit_client_cm.__aexit__(None, None, None)
            except Exception:
                log.warning("verify_catalog.revit_close_failed", exc_info=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = DEFAULT_RUNS_DIR / f"catalog_verify_{ts}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report.render_markdown(), encoding="utf-8")

    # Stdout summary
    print("\n=== Catalog verify ===")
    print(f"Total entries:     {len(report.verdicts)}")
    if not report.revit_skipped:
        print(
            f"Revit present:     {len(report.revit_present())} / "
            f"{len(report.verdicts)} (others may just be absent from this model)"
        )
    else:
        print("Revit probe:       SKIPPED")
    if not report.aecdm_skipped:
        print(f"AECDM fillable:    {len(report.aecdm_fillable())} (null → label found)")
        print(f"AECDM dead:        {len(report.aecdm_dead())} (label rejected)")
    else:
        print("AECDM probe:       SKIPPED")
    print(f"Report written:    {out_path}")
    if report.recommendations:
        print("\nTop recommendations:")
        for line in report.recommendations[:8]:
            print(f"  {line}")
        if len(report.recommendations) > 8:
            print(f"  …and {len(report.recommendations) - 8} more in the report file.")
    return 0


async def list_revit_rooms() -> int:
    """Connect to the Revit MCP bridge, dump document info + rooms table.

    Requires Revit (2026 or 2027) running with the RevitMCPAddin loaded,
    plus env vars:
        REVIT_MCP_SERVER_CMD  (default: node)
        REVIT_MCP_SERVER_ARGS (e.g. "dist/index.js")
        REVIT_MCP_SERVER_CWD  (path to RevitMCPServer/src/McpServer)
        REVIT_MCP_VERSION     (2026 default; set to 2027 for Pacific demo —
                               drives port selection 7891 + version offset
                               + auth-token lookup path)
    """
    async with make_revit_client() as client:
        doc = await client.get_document_info()
        rooms = await client.list_rooms()

    print("\n=== Revit document ===")
    print(f"Title:      {doc.get('title')}")
    print(f"Path:       {doc.get('pathName')}")
    print(f"Project:    {doc.get('projectName')} ({doc.get('projectNumber')})")
    print(f"Units:      {doc.get('displayUnitSystem')}")
    print(f"Active view:{doc.get('activeViewName')}")

    print(f"\n=== Rooms ({len(rooms)}) ===")
    header = f"{'ID':>10}  {'Number':<8}  {'Name':<35}  {'Level':<25}  {'Area m²':>9}"
    print(header)
    print("-" * len(header))
    for r in rooms:
        name = (r.get("name") or "?")
        level = (r.get("levelName") or "?")
        print(
            f"{r.get('id', 0):>10}  "
            f"{(r.get('number') or '?'):<8}  "
            f"{name[:35]:<35}  "
            f"{level[:25]:<25}  "
            f"{r.get('areaMetric', 0):>9.2f}"
        )
    return 0


# ---- create-verification-views (v1 report module, Phase 2) ----------------


async def build_verification_views(run_id: str, *, dry_run: bool) -> int:
    """Auto-create native Revit verification schedules for a finished run.

    Reads ``runs/<run_id>/report_trace.json`` (the structured trace the
    verification report renders), then builds ONE ViewSchedule per rule — the
    native artifact a reviewer would otherwise create by hand to re-check the
    finding set. Writes a manifest (``verification_views.json`` + ``.md``) into
    the run folder. Revit must be open (uses the Revit MCP client).
    """
    from bim_orchestrator.verification_views import (
        create_verification_schedules,
        manifest_dict,
        render_manifest_markdown,
    )

    run_dir = DEFAULT_RUNS_DIR / run_id
    trace_path = run_dir / "report_trace.json"
    if not trace_path.exists():
        log.error("verification_views.trace_missing", run_id=run_id, path=str(trace_path))
        print(f"❌ report_trace.json not found for run '{run_id}'", file=sys.stderr)
        return 2
    check_trace = json.loads(trace_path.read_text(encoding="utf-8"))
    if not check_trace:
        print(f"No (element, rule) trace recorded for run '{run_id}' — nothing to build.")
        return 0

    catalog = OSTCatalog.load()
    async with make_revit_client() as revit:
        results = await create_verification_schedules(
            revit, check_trace, catalog=catalog, dry_run=dry_run
        )

    (run_dir / "verification_views.json").write_text(
        json.dumps(manifest_dict(results), indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "verification_views.md").write_text(
        render_manifest_markdown(results), encoding="utf-8"
    )

    created = [r for r in results if r.status == "created"]
    existing = [r for r in results if r.status == "existing"]
    reconfigured = [r for r in results if r.status == "existing_reconfigured"]
    degraded = [r for r in results if r.status == "degraded"]
    errored = [r for r in results if r.status == "error"]
    mode = "DRY-RUN (rolled back)" if dry_run else "EXECUTE"
    print(f"\n=== Verification views [{mode}] — {run_id} ===")
    print(
        f"Schedules created: {len(created)}  ·  existing, re-configured "
        f"(v1.7-R22): {len(reconfigured)}  ·  existing, left as-is: "
        f"{len(existing)}  ·  degraded: {len(degraded)}  "
        f"·  error: {len(errored)}"
    )
    for r in created:
        print(f"  ✅ {r.rule_id} → schedule {r.schedule_id} ({r.category_ost})")
    for r in reconfigured:
        print(
            f"  ↺ {r.rule_id} → schedule {r.schedule_id} already existed, "
            f"configuration re-applied ({r.category_ost})"
        )
    for r in existing:
        print(
            f"  ↺ {r.rule_id} → schedule {r.schedule_id} already exists; "
            f"configuration NOT re-applied ({r.category_ost})"
        )
    for r in degraded:
        print(f"  ⚠ {r.rule_id} → {r.detail}")
    for r in errored:
        print(f"  ❌ {r.rule_id} → {r.detail}")
    print(f"Manifest: {run_dir / 'verification_views.md'}")
    return 1 if errored else 0


def export_report_cmd(run_id: str, *, fmt: str) -> int:
    """Export a finished run's verification report to docx/pdf (Markdown stays
    canonical). Uses pandoc when present; otherwise prints guidance to use the
    document skills. See ``report_export.export_report``."""
    from bim_orchestrator.report_export import export_report

    run_dir = DEFAULT_RUNS_DIR / run_id
    md = run_dir / "verification_report.md"
    out, msg = export_report(md, fmt)
    if out is None:
        # Missing report → real error (2); "no pandoc" guidance → 0 (md is canonical).
        prefix = "❌ " if not md.exists() else "note: "
        print(prefix + msg, file=sys.stderr)
        return 2 if not md.exists() else 0
    print(f"✅ {msg}")
    return 0


# ---- eval-rag (Phase 2 Week 4 Day 1) --------------------------------------

def eval_rag(*, use_real_embed: bool, top_k: int) -> int:
    """Ingest the synthetic IBC §7 fixture and score retrieval quality.

    Fast feedback loop for RAG tuning. With `use_real_embed=False` (default)
    a deterministic bag-of-words embedder runs in milliseconds — useful for CI.
    `--use-real-embed` switches to sentence-transformers (~90MB first download).
    """
    if use_real_embed:
        embed_fn = None  # VectorStore lazily loads sentence-transformers
        log.info("eval_rag.embedder", choice="sentence-transformers (real)")
    else:
        embed_fn = _bag_of_words_embed
        log.info("eval_rag.embedder", choice="bag-of-words (fake, fast)")

    store = VectorStore(persist_dir=None, collection="ibc-eval", embed_fn=embed_fn)
    chunks_added = ingest_ibc_chapter_7(store)
    log.info("eval_rag.ingested", chunks=chunks_added, queries=len(DEFAULT_IBC_QUERIES))

    report = run_eval(store, DEFAULT_IBC_QUERIES, top_k=top_k)
    print(report.format(verbose=True))
    log.info(
        "eval_rag.done",
        hit_at_1=f"{report.hit_rate_at_1:.2%}",
        hit_at_3=f"{report.hit_rate_at_3:.2%}",
        mrr=f"{report.mrr:.3f}",
    )
    return 0


def _bag_of_words_embed(texts: list[str]) -> list[list[float]]:
    """Lightweight hash-based bag-of-words embedder — same shape as test fake."""
    import hashlib
    import math

    DIM = 128
    vectors: list[list[float]] = []
    for text in texts:
        vec = [0.0] * DIM
        for raw in text.lower().split():
            token = "".join(c for c in raw if c.isalnum())
            if not token:
                continue
            h = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
            vec[h] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        vectors.append([v / norm for v in vec])
    return vectors


# ---- CLI -------------------------------------------------------------------

def _classify_error(exc: BaseException) -> tuple[int, str]:
    """Map an exception to (exit_code, friendly_message)."""
    msg = str(exc)
    if isinstance(exc, FileNotFoundError):
        return 2, f"File not found: {exc.filename}"
    if "DEMO_" in msg and ("missing" in msg.lower() or "not set" in msg.lower()):
        return 2, f"Config error: {msg}"
    if "MCP" in msg or "stdio" in msg.lower():
        return 3, f"MCP connection error: {msg}"
    if "APS" in msg or "ISSUES_SERVICE" in msg or "BadRequest" in msg:
        return 4, f"ACC API error: {msg}"
    return 1, f"Unexpected error: {msg}"


def _forma_exe_integrity(exe: pathlib.Path) -> tuple[bool, str]:
    """Is the installed forma-mcp.exe still the file that was verified?

    `fetch-forma-mcp.ps1` verifies the download's SHA-256 before installing it
    and leaves the hash in a `.sha256` sidecar. Re-hashing here covers the
    window that script cannot: corruption or substitution after install.

    Deliberately NOT a version/smoke probe. The exe exits with "Invalid
    environment configuration" unless APS credentials are present, and
    `--doctor` must work on a machine that has none -- diagnosing an unconfigured
    machine is most of its job.

    An unverified install is reported as such rather than passing quietly: a
    check that cannot tell must not read as a check that passed. (Same rule the
    engine applies to a rule it cannot evaluate.)
    """
    import hashlib

    sidecar = exe.with_suffix(exe.suffix + ".sha256")
    if not sidecar.exists():
        return (
            False,
            "no .sha256 sidecar -- installed before verification existed, or with "
            "-SkipHashCheck. Re-run scripts/fetch-forma-mcp.ps1 to get a verified copy.",
        )
    try:
        expected = sidecar.read_text(encoding="ascii").strip().lower()
        digest = hashlib.sha256()
        with exe.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
    except OSError as exc:
        return (False, f"could not hash {exe}: {exc}")

    if actual != expected:
        return (
            False,
            f"SHA-256 CHANGED since install (expected {expected[:16]}..., "
            f"got {actual[:16]}...). Do not run it; re-fetch.",
        )
    return (True, f"sha256 {actual[:16]}... matches the sidecar")


def doctor_checks() -> list[dict[str, str]]:
    """P3-5 / M2-C: build the pre-flight checklist as data — one dict per
    check, ``{"name": str, "status": "pass"|"warn"|"fail", "detail": str}``.

    REQUIRED checks (the only ones that can come back ``fail``): python
    version, ``runs/`` writable. Everything else — .env keys, forma-mcp.exe,
    the live Revit addin (Revit may simply be closed), the two IFC
    satellites, RevitControl, the ``service`` extras, and the AuditHub
    single-run lock — is OPTIONAL (``warn``, never fails the whole check): a
    machine can run plain LOI audits with none of them installed, and this
    must stay true on a clean dev box.

    Single source of truth for BOTH the CLI ``doctor()`` (prints this list)
    and the service's ``GET /api/settings/doctor`` (returns it as JSON) —
    don't duplicate the check logic in the service layer.
    """
    import importlib.util

    from bim_orchestrator.mcp_clients.forma import _vendor_exe
    from bim_orchestrator.policies.audit_profile import load_audit_services

    rows: list[dict[str, str]] = []

    def _check(name: str, ok: bool, detail: str = "", *, required: bool = False) -> None:
        status = "pass" if ok else ("fail" if required else "warn")
        rows.append({"name": name, "status": status, "detail": detail})

    _check(
        "python >= 3.12",
        sys.version_info >= (3, 12),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        required=True,
    )

    env_path = PROJECT_ROOT / ".env"
    _check(".env present", env_path.exists(), str(env_path))

    exe = _vendor_exe("forma-mcp")
    _check("forma-mcp.exe present", exe is not None, exe or "not found under vendor/forma-mcp/")
    if exe is not None:
        ok, detail = _forma_exe_integrity(pathlib.Path(exe))
        _check("forma-mcp.exe integrity", ok, detail)

    try:
        import httpx

        from bim_orchestrator.mcp_clients.revit import _load_auth_token, _resolve_port

        version = os.environ.get("REVIT_MCP_VERSION", "2026")
        port = _resolve_port(version)
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        ok = resp.status_code == 200 and bool(resp.json().get("ok"))
        _check("Revit addin /health", ok, f"port {port}")
    except Exception as exc:  # noqa: BLE001 - Revit may simply be closed
        _check("Revit addin /health", False, f"unreachable: {exc}")

    services = load_audit_services()
    _check("lod-validator configured", services.available("lod"))
    _check("spatial-qc configured", services.available("spatial"))
    _check(
        "RevitControl configured",
        services.revitcontrol is not None and services.revitcontrol.exists(),
    )

    _check(
        "service extras installed (fastapi)",
        importlib.util.find_spec("fastapi") is not None,
    )

    lock_path = DEFAULT_RUNS_DIR / ".service_lock"
    if lock_path.exists():
        pid_txt = lock_path.read_text(encoding="utf-8").strip() if lock_path.exists() else ""
        _check(
            "AuditHub service lock",
            False,
            f"{lock_path} exists (pid {pid_txt}) — an audit may be running",
        )
    else:
        _check("AuditHub service lock", True, "no lock file")

    try:
        DEFAULT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        probe = DEFAULT_RUNS_DIR / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _check("runs/ writable", True, required=True)
    except OSError as exc:
        _check("runs/ writable", False, str(exc), required=True)

    return rows


def doctor() -> int:
    """P3-5: CLI ``--doctor`` — prints ``doctor_checks()`` as a PASS/FAIL/WARN
    table. Exit 0 iff no check came back ``fail`` (only the REQUIRED checks
    can — see ``doctor_checks``)."""
    rows = doctor_checks()

    width = max(len(r["name"]) for r in rows)
    print(f"{'Check':<{width}}  Status  Detail")
    for row in rows:
        print(f"{row['name']:<{width}}  {row['status'].upper():<6}  {row['detail']}")

    required_failed = any(r["status"] == "fail" for r in rows)
    if required_failed:
        print("\n❌ doctor: one or more REQUIRED checks failed.")
        return 1
    print("\n✅ doctor: all required checks passed.")
    return 0


def _lookup_key_ref(param: str) -> ParamRef:
    """Same host-prefix convention as ``rules_lint._host_aware_ref`` — a
    ``host.<p>`` lookup key reads a DIFFERENT (unknown-category) element."""
    if param.startswith("host."):
        return ParamRef(None, param.removeprefix("host."))
    return ParamRef(None, param)


def lint_rules_cmd(
    rules_paths: Sequence[Path], autonomy_path: Path, *, strict: bool
) -> int:
    """v1.5-R7 (R1-Stage 2): ``--lint-rules`` — static termination +
    confluence analysis (docs/260711_Autofix Loop.md). Prints a human-
    readable report, writes ``rules_lint.json`` to cwd. Exit 0 with no
    errors; ``--strict`` additionally fails on warnings; exit 1 on any error.
    """
    from bim_orchestrator.policies.lookup_table import load_lookup

    autonomy = AutonomyPolicy.load(autonomy_path)
    qc = QCAgent(rules_path=list(rules_paths), autonomy=autonomy)
    ruleset = qc.rules

    # Lookup-table key params are the one I/O-bearing read source
    # rules_lint.lint() can't resolve itself (it's pure) — pre-resolve here
    # and inject as extra_reads (see rules_lint.lint()'s docstring).
    extra_reads: dict[str, set[ParamRef]] = {}
    for rule in ruleset.rules:
        if not rule.lookup:
            continue
        try:
            table = load_lookup(rule.lookup, qc._config_dir)
        except (FileNotFoundError, OSError, ValueError) as exc:
            log.warning(
                "lint_rules.lookup_load_failed",
                rule=rule.id, name=rule.lookup, error=str(exc),
            )
            continue
        extra_reads[rule.id] = {_lookup_key_ref(k.param) for k in table.keys}

    report = lint_ruleset(ruleset, extra_reads=extra_reads or None)

    paths_s = ", ".join(str(p) for p in rules_paths)
    print(f"=== rules-lint: {paths_s} ===\n")
    print(f"ERRORS ({len(report.errors)}):")
    for e in report.errors:
        print(f"  - {e}")
    if not report.errors:
        print("  (none)")
    print(f"\nWARNINGS ({len(report.warnings)}):")
    for w in report.warnings:
        print(f"  - {w}")
    if not report.warnings:
        print("  (none)")
    unanalyzable = [w["rule"] for w in report.warnings if w["type"] == "unanalyzable"]
    print(f"\nUNANALYZABLE ({len(unanalyzable)}): {unanalyzable}")
    print(
        f"\nORDER: {report.order}"
        if report.order is not None
        else "\nORDER: (unavailable — a cycle was detected, see ERRORS)"
    )

    out_path = Path.cwd() / "rules_lint.json"
    out_path.write_text(
        json.dumps(
            {
                "errors": report.errors,
                "warnings": report.warnings,
                "order": report.order,
                "graph": report.graph,
            },
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nWritten: {out_path}")

    if report.errors:
        return 1
    if strict and report.warnings:
        return 1
    return 0


async def watch_approvals(
    *,
    approvals_dir: Path,
    once: bool,
    interval_s: float,
    max_passes: int | None = None,
) -> int:
    """v1.4-K5 Loop 2: poll ACC proposal issues; apply on status `in_progress`.

    Needs both Forma (read/update issues) and Revit (write fixes) connected.
    The Revit document must be open. Mirrors run_revit's nested-but-separate
    client entry to avoid the anyio cancel-scope teardown bug.
    """
    from bim_orchestrator.approval_watcher import ApprovalWatcher

    forma_config = FormaMCPConfig.from_env()
    async with FormaMCPClient(forma_config) as forma:
        async with make_revit_client() as revit:
            watcher = ApprovalWatcher(approvals_dir, forma, revit)
            if once:
                applied = await watcher.scan_once()
                print(f"Applied {len(applied)} approved proposal(s) from {approvals_dir}.")
            else:
                print(
                    f"Watching {approvals_dir} every {interval_s:.0f}s for issues "
                    f"set to 'In progress' — Ctrl+C to stop."
                )
                await watcher.watch(interval_s=interval_s, max_passes=max_passes)
    return 0


def _make_console_utf8_safe() -> None:
    """Stop the console's codepage from being able to fail a run.

    Windows hands Python a cp1252 stdout unless ``PYTHONIOENCODING`` is set,
    and 19 of this module's ``print`` calls carry characters cp1252 cannot
    encode — the arrow in "Elements → ACC Issues" among them. Printing one
    raised ``UnicodeEncodeError``, which propagated out of the summary block,
    was caught by ``run_revit``'s failure handler, and recorded a run that had
    **converged** as ``status: failed`` with exit 1.

    This was a documented gotcha ("set PYTHONIOENCODING=utf-8 in your shell"),
    which is exactly why every session and every test missed it: we all set it.
    A demo machine does not — ``scripts/seed-au-demo.ps1`` calls this CLI from
    PowerShell with nothing set, so the AU runbook's own step 2 failed on a
    clean machine (found 2026-07-29 while seeding for the backup recording).

    UTF-8 where the terminal accepts it, ``replace`` where it does not: a
    console that cannot render "→" should print "?" and carry on. Losing a
    glyph is a display problem. Losing the run is not.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # redirected to something exotic — leave it
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # output setup must never break startup
            pass


def main() -> int:
    _make_console_utf8_safe()
    load_dotenv()

    parser = argparse.ArgumentParser(prog="bim-orchestrator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--hello",
        action="store_true",
        help="Smoke test the MCP connection and exit",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="Run Query → QC once, dump findings.json, print summary",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Run Query → QC → Design once (single-pass, no loop).",
    )
    group.add_argument(
        "--run",
        action="store_true",
        help="Full cyclic graph: Query → QC → Design → loop until converged.",
    )
    group.add_argument(
        "--eval-rag",
        action="store_true",
        help="Phase 2 W4 D1: ingest synthetic IBC §7 fixture and score retrieval quality.",
    )
    group.add_argument(
        "--demo",
        action="store_true",
        help="Run the full compliance loop against a bundled mock model "
        "(\"Demo Villa\") — zero Revit, zero ACC, zero API key, zero "
        "network. Default rules: config/rules.demo.yaml (override with "
        "--rules). See README \"Try it in 5 minutes\".",
    )
    group.add_argument(
        "--list-runs",
        action="store_true",
        help="v1 task M: print a table of runs/run-<id>/ folders, newest first.",
    )
    group.add_argument(
        "--trend-report",
        action="store_true",
        help="v1 task V: regenerate runs/trend.md (cross-run diff + persistent issues).",
    )
    group.add_argument(
        "--list-revit-rooms",
        action="store_true",
        help="Phase 2 W6 D1: connect to Revit MCP bridge, dump rooms table. "
        "Requires Revit (2026 or 2027) running with RevitMCPAddin loaded; "
        "set REVIT_MCP_VERSION env to pick which one (default 2026).",
    )
    group.add_argument(
        "--verify-catalog",
        action="store_true",
        help="v1.3 polish: probe config/ost_catalog.yaml against the active "
        "Revit + AECDM session. Output: runs/catalog_verify_<ts>.md with "
        "per-entry verdicts + recommendations for filling AECDM-null entries. "
        "Pair with --no-revit or --no-forma to skip a backend.",
    )
    group.add_argument(
        "--run-revit",
        action="store_true",
        help="Phase 2 W6 D4: live E2E run against Revit + Forma. Routes "
        "findings by rule.fixability: manual -> ACC Issue, auto -> Revit "
        "parameter write. Pair with --bep-fixture or --bep-pdf for citations.",
    )
    group.add_argument(
        "--audit",
        type=Path,
        metavar="PROFILE_YAML",
        help="Phase 3: run an AutoAudit profile (config/audit.<name>.yaml) — "
        "LOI rules + enabled IFC axes (LOD via lod-validator, Spatial via "
        "spatial-qc) in ONE run folder + one verification report. Satellite "
        "paths come from config/audit_services.yaml; an unconfigured axis is "
        "skipped honestly, never fatal.",
    )
    group.add_argument(
        "--apply-approved",
        type=str,
        metavar="RUN_ID",
        help=argparse.SUPPRESS,  # H1: removed — kept only to fail with guidance below.
    )
    group.add_argument(
        "--watch-approvals",
        action="store_true",
        help="v1.4-K5 Loop 2: poll ACC proposal issues; when one is set to "
        "'In progress', apply its parked Revit writes (one revit_batch = one "
        "undo), comment + close the issue. Loops until Ctrl+C. Revit must be open.",
    )
    group.add_argument(
        "--apply-approvals-once",
        action="store_true",
        help="Single pass of --watch-approvals (apply any already-approved "
        "proposals, then exit). Useful for a Streamlit 'Apply now' button or cron.",
    )
    group.add_argument(
        "--create-verification-views",
        type=str,
        metavar="RUN_ID",
        help="v1 report Phase 2: build native Revit verification SCHEDULES from a "
        "finished run's runs/<RUN_ID>/report_trace.json (one schedule per rule, "
        "the artifact a reviewer would build by hand to re-check the findings). "
        "Writes verification_views.json/.md into the run folder. Revit must be "
        "open. Pair with --dry-run to preview (rolled back).",
    )
    group.add_argument(
        "--export-report",
        type=str,
        metavar="RUN_ID",
        help="v1 report Phase 2: export runs/<RUN_ID>/verification_report.md to "
        "docx/pdf (set --report-format; default docx). Markdown stays canonical; "
        "uses pandoc when present, else prints how to convert via the doc skills.",
    )
    group.add_argument(
        "--doctor",
        action="store_true",
        help="P3-5: pre-flight checklist (python version, .env, forma-mcp.exe, "
        "Revit addin health, audit satellites, service extras, runs/ writable). "
        "Prints a PASS/FAIL/WARN table; exit 0 iff every REQUIRED check passes.",
    )
    group.add_argument(
        "--lint-rules",
        action="store_true",
        help="v1.5-R7: static termination + confluence check over --rules "
        "(read/write footprint analysis, docs/260711_Autofix Loop.md). Prints "
        "ERRORS/WARNINGS/ORDER/UNANALYZABLE, writes rules_lint.json to cwd. "
        "Exit 1 on any error; pair with --strict to also fail on warnings.",
    )
    parser.add_argument(
        "--approvals-dir",
        type=Path,
        default=DEFAULT_APPROVALS_DIR,
        help=f"Directory of approval records (default: {DEFAULT_APPROVALS_DIR}).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="--watch-approvals poll interval in seconds (default: 30).",
    )
    parser.add_argument(
        "--report-format",
        choices=["docx", "pdf"],
        default="docx",
        help="--export-report only: target format (default: docx).",
    )
    parser.add_argument(
        "--use-real-embed",
        action="store_true",
        help="--eval-rag only: use sentence-transformers (real, ~90MB) instead of bag-of-words.",
    )
    parser.add_argument(
        "--eval-top-k",
        type=int,
        default=3,
        help="--eval-rag only: top-k for retrieval scoring (default: 3)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2,
        help="Max issues to create per iteration (default: 2)",
    )
    parser.add_argument(
        "--rule",
        type=str,
        default=None,
        help=(
            "rule_id to target. Default: None (all rules). Use 'none' / 'all' "
            "/ '' as explicit no-filter sentinels (matched by DesignAgent)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip issue execution (preview/audit dry-run entries only)",
    )
    parser.add_argument(
        "--fail-on-partial-coverage",
        action="store_true",
        help="Exit non-zero when some target categories could not be resolved "
             "(partial coverage). Default is exit 0 — a partial run is still a "
             "real audit and the report carries a PARTIAL COVERAGE banner — but "
             "an unattended/scheduled audit usually wants a silently narrowed "
             "scope to fail loudly. Audit profiles: run.fail_on_partial_coverage.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="--lint-rules only: also exit 1 when there are WARNINGS (not just ERRORS).",
    )
    parser.add_argument(
        "--unpublished",
        action="store_true",
        help="Create issues as drafts (visible to creator only)",
    )
    parser.add_argument(
        "--issue-subtype-id",
        type=str,
        default=None,
        help="Override Issue subtype ID. Falls back to $DEMO_ISSUE_SUBTYPE_ID.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum loop iterations for --run (default: 3; 4 for --demo — "
        "see _resolve_max_iterations)",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=DEFAULT_CHECKPOINT_DIR,
        help=f"Directory for JSON checkpoints (default: {DEFAULT_CHECKPOINT_DIR})",
    )
    parser.add_argument(
        "--bep-pdf",
        type=Path,
        default=None,
        help="Phase 2: path to BEP PDF. When set, the graph adds a Grounding step "
        "between QC and Route that attaches citations to findings via RAG.",
    )
    parser.add_argument(
        "--bep-fixture",
        action="store_true",
        help="Phase 2 W6: ingest the synthetic BEP §1 fixture (7 chunks) "
        "instead of a real PDF. Useful for demos when the real BEP isn't ready.",
    )
    parser.add_argument(
        "--fetch-concurrency",
        type=int,
        default=4,
        help="--run-revit only: number of parallel get_element_info calls the "
        "Revit query agent dispatches. Default 4 — Revit MCP bridge handles "
        "concurrency well; bump to 8/16 on large models if the per-spec log "
        "shows long elapsed_ms and high cache-miss counts. Set to 1 to debug.",
    )
    parser.add_argument(
        "--max-elements",
        type=int,
        default=300,
        help="--run-revit only: P0 element budget. Caps elements checked per "
        "category for parameter rules (list_elements limit) AND per geometry "
        "check (setA.limit). Default 300 — sized for demo/share builds. The "
        "cap is deterministic (first N in Revit order); a warning is logged "
        "when it bites. Raise for production-scale models.",
    )
    parser.add_argument(
        "--bulk-fields",
        action="store_true",
        help="--run-revit only: P1 perf opt-in. Fetch all instance-level "
        "params in ONE revit_find_elements call per category instead of N "
        "get_element_info calls — big win on element-dense categories (e.g. "
        "Ducts). Correctness preserved: the Type is still fetched + merged, so "
        "implicit Instance>Type fallback (fire-rating) still works. Rooms "
        "(metric enrichment) and host-hop scenarios (doors) keep the "
        "per-element path automatically. Verify on your model before relying "
        "on it for production.",
    )
    parser.add_argument(
        "--no-forma",
        action="store_true",
        help="--run-revit / --verify-catalog: skip the Forma MCP client. For "
        "--run-revit, manual (path A) findings get parked without creating "
        "an ACC Issue. For --verify-catalog, AECDM probes are skipped.",
    )
    parser.add_argument(
        "--no-revit",
        action="store_true",
        help="--verify-catalog only: skip Revit MCP probe (useful when no "
        "Revit instance is running but you still want the AECDM-side check).",
    )
    parser.add_argument(
        "--vector-store-dir",
        type=Path,
        default=None,
        help="Phase 2: persist the ChromaDB vector store at this path. "
        "Re-ingestion of the same PDF is idempotent. Default: ephemeral.",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        nargs="+",
        default=None,
        metavar="RULES_YAML",
        help="Path(s) to rules YAML. Pass several to merge scenarios into one "
        "run (e.g. --rules a.yaml b.yaml); rule ids dedup, first file wins. "
        f"Default: {DEFAULT_RULES_PATH} ({DEFAULT_DEMO_RULES_PATH} for --demo)",
    )
    parser.add_argument(
        "--autonomy",
        type=Path,
        default=DEFAULT_AUTONOMY_PATH,
        help=f"Path to autonomy YAML (default: {DEFAULT_AUTONOMY_PATH})",
    )
    parser.add_argument(
        "--findings-out",
        type=Path,
        default=DEFAULT_FINDINGS_OUT,
        help=f"Where to write findings JSON (default: {DEFAULT_FINDINGS_OUT})",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logs (DEBUG level). Overrides BIM_LOG_LEVEL.",
    )
    verbosity.add_argument(
        "--quiet",
        action="store_true",
        help="Quiet logs (WARNING level). Overrides BIM_LOG_LEVEL.",
    )
    parser.add_argument(
        "--log-format",
        choices=["plain", "json"],
        default=None,
        help="Override BIM_LOG_FORMAT (default: plain on TTY, json otherwise)",
    )
    args = parser.parse_args()

    # Configure logging early so all downstream logs respect it
    import logging as _logging
    level = None
    if args.verbose:
        level = _logging.DEBUG
    elif args.quiet:
        level = _logging.WARNING
    configure_logging(fmt=args.log_format, level=level)

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — top-level catch-all
        code, message = _classify_error(exc)
        log.error("orchestrator.failed", code=code, error=str(exc), exc_info=True)
        print(f"\n❌ {message}", file=sys.stderr)
        print(f"   Exit code: {code}. Run with --verbose for full traceback.", file=sys.stderr)
        return code


def _resolve_rules_paths(args: argparse.Namespace) -> list[Path]:
    """``--rules`` default depends on ``--demo`` — resolved once here so every
    dispatch branch below shares one answer instead of re-deriving it."""
    if args.rules is not None:
        return args.rules
    if getattr(args, "demo", False):
        return [DEFAULT_DEMO_RULES_PATH]
    return [DEFAULT_RULES_PATH]


def _resolve_max_iterations(args: argparse.Namespace) -> int:
    """``--max-iterations`` default depends on ``--demo`` (see ``demo()``'s
    docstring for why the mixed auto+approve demo dataset needs one more)."""
    if args.max_iterations is not None:
        return args.max_iterations
    return 4 if getattr(args, "demo", False) else 3


def _dispatch(args: argparse.Namespace) -> int:
    if args.doctor:
        return doctor()
    rules_paths = _resolve_rules_paths(args)
    max_iterations = _resolve_max_iterations(args)
    if args.lint_rules:
        return lint_rules_cmd(rules_paths, args.autonomy, strict=args.strict)
    if args.hello:
        return asyncio.run(hello())
    if args.list_runs:
        return list_runs_cmd()
    if args.apply_approved:
        # H1: --apply-approved bypassed the whole trust pipeline (no
        # fingerprint gate, no stale re-preview, wrote the flagged instance
        # id instead of the resolved write target) and is removed. Fail
        # loudly instead of silently no-op'ing so a stale script/muscle-memory
        # invocation gets redirected to the real approval-resume path.
        print(
            "❌ --apply-approved has been removed — it bypassed the approval "
            "trust pipeline (no fingerprint check, no stale re-preview).\n"
            "   Use --watch-approvals (loop) or --apply-approvals-once "
            "(single pass) instead — the ApprovalWatcher applies parked "
            "Path B writes once a proposal issue is set to 'In progress'.",
            file=sys.stderr,
        )
        return 2
    if args.create_verification_views:
        return asyncio.run(
            build_verification_views(
                args.create_verification_views, dry_run=args.dry_run
            )
        )
    if args.export_report:
        return export_report_cmd(args.export_report, fmt=args.report_format)
    if args.watch_approvals or args.apply_approvals_once:
        # L-07: the watcher WRITES the same document the service audits.
        _warn_if_service_busy(
            "watch-approvals" if args.watch_approvals else "apply-approvals-once"
        )
        return asyncio.run(
            watch_approvals(
                approvals_dir=args.approvals_dir,
                once=args.apply_approvals_once,
                interval_s=args.poll_interval,
            )
        )
    if args.trend_report:
        return trend_report_cmd()
    if args.list_revit_rooms:
        return asyncio.run(list_revit_rooms())
    if args.verify_catalog:
        return asyncio.run(
            verify_catalog_cmd(
                skip_revit=args.no_revit,
                skip_forma=args.no_forma,
            )
        )
    if args.demo:
        return asyncio.run(
            demo(
                rules_paths,
                autonomy_path=args.autonomy,
                findings_out=args.findings_out,
                max_iterations=max_iterations,
                checkpoint_dir=args.checkpoint_dir,
                # C-1: the CLI default points at the PRODUCTION dir; --demo
                # must not inherit it. Only an explicit --approvals-dir (i.e.
                # a value different from the CLI default) is forwarded.
                approvals_dir=(
                    args.approvals_dir
                    if args.approvals_dir != DEFAULT_APPROVALS_DIR
                    else DEFAULT_DEMO_APPROVALS_DIR
                ),
            )
        )
    if args.audit:
        _warn_if_service_busy("audit")
        return asyncio.run(
            audit(
                args.audit,
                args.autonomy,
                args.findings_out,
                max_iterations=max_iterations,
                checkpoint_dir=args.checkpoint_dir,
                approvals_dir=args.approvals_dir,
            )
        )
    if args.run_revit:
        _warn_if_service_busy("run-revit")
        return asyncio.run(
            run_revit(
                rules_paths,
                args.autonomy,
                args.findings_out,
                limit=args.limit,
                rule_filter=args.rule,
                dry_run_only=args.dry_run,
                published=not args.unpublished,
                issue_subtype_id=args.issue_subtype_id,
                max_iterations=max_iterations,
                checkpoint_dir=args.checkpoint_dir,
                bep_pdf=args.bep_pdf,
                use_bep_fixture=args.bep_fixture,
                vector_store_dir=args.vector_store_dir,
                skip_forma=args.no_forma,
                fetch_concurrency=args.fetch_concurrency,
                max_elements=args.max_elements,
                bulk_fields=args.bulk_fields,
                approvals_dir=args.approvals_dir,
                fail_on_partial_coverage=args.fail_on_partial_coverage,
            )
        )
    if args.eval_rag:
        return eval_rag(use_real_embed=args.use_real_embed, top_k=args.eval_top_k)
    if args.check:
        return asyncio.run(check(
            rules_paths, args.autonomy, args.findings_out,
            fail_on_partial_coverage=args.fail_on_partial_coverage,
        ))
    if args.apply:
        return asyncio.run(
            apply(
                rules_paths,
                args.autonomy,
                args.findings_out,
                limit=args.limit,
                rule_filter=args.rule,
                dry_run_only=args.dry_run,
                published=not args.unpublished,
                issue_subtype_id=args.issue_subtype_id,
                fail_on_partial_coverage=args.fail_on_partial_coverage,
            )
        )
    # --run or default (no flag)
    return asyncio.run(
        run(
            rules_paths,
            args.autonomy,
            args.findings_out,
            limit=args.limit,
            rule_filter=args.rule,
            dry_run_only=args.dry_run,
            published=not args.unpublished,
            issue_subtype_id=args.issue_subtype_id,
            max_iterations=max_iterations,
            checkpoint_dir=args.checkpoint_dir,
            bep_pdf=args.bep_pdf,
            use_bep_fixture=args.bep_fixture,   # P1-CLI-01
            vector_store_dir=args.vector_store_dir,
            fail_on_partial_coverage=args.fail_on_partial_coverage,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
