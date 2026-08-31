"""P3-3 — Unattended Revit sessions via RevitControl's watchdog.

Wraps the DISPATCH phase of an `--audit` run (``check``/``run``/``run_revit``)
so the whole audit can proceed without a human at the keyboard: a
``watchdog.py`` process (RevitControl repo, run under ITS OWN python — this
process never imports ``rigcore``, same MCP-boundary posture as the
lod-validator/spatial-qc satellites, D3/D8) supervises Revit's window for
known dialogs (dismiss) while an UNRECOGNIZED dialog HALTS — never
blind-clicked, RevitControl's own discipline
(``unknown_dialog_action: "halt"``, kept as-is here).

Revit itself is launched (if not already running) and waited-ready via
``scripts/unattended_launch.py``, a small helper that this module spawns as
a subprocess using RevitControl's python (it CAN import ``rigcore``). It runs
in two blocking phases straddling the watchdog spawn — ``--phase launch``
(is-running guard + launch, no wait) BEFORE the watchdog exists, then
``--phase wait`` (wait_addin_ready) AFTER — so the watchdog's own
no-revit.exe-yet relaunch tick can never race the initial launch decision
against a still-cold-starting Revit, while still being alive to dismiss the
Unsigned Add-In modal during the wait. A nonzero wait phase aborts the
session (``UnattendedLaunchError``) rather than letting the audit limp on
into a doomed ``run``/``run_revit``.

P3 limitation: this session does NOT write a ``status.json`` heartbeat for
the watchdog's hang/crash-loop logic — that full orchestrator<->rig
contract (current_test / heartbeat_at / expected_timeout_s) is deferred
future work. The watchdog still handles modal dialogs and a Revit crash on
its own; it just never learns an "expected timeout" from this session. A
crash-loop halt (the watchdog's PAUSED flag) is logged at ``__aexit__``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Self

import structlog

from bim_orchestrator.mcp_clients.revit import _load_auth_token, _resolve_port

if TYPE_CHECKING:
    from bim_orchestrator.policies.audit_profile import AuditServices

log = structlog.get_logger(__name__)


class UnattendedLaunchError(RuntimeError):
    """Raised when the launch helper's ``--phase wait`` step fails to bring
    the RevitMCP addin up within timeout.

    A nightly box has no human to dismiss a stuck modal — failing fast here
    (aborting the ``--audit`` dispatch) beats limping into ``run``/``run_revit``
    and failing later with a confusing MCP-connection error deep in the graph.
    """


# Mirrors RevitControl's watchdog.py DEFAULT_DIALOG_RULES / watchdog.config.json
# (2026-07-30 snapshot). Copied, not imported — this repo cannot depend on
# RevitControl's package (D3/D8 boundary: satellite tools stay out-of-process).
# Keep in sync by hand if that list changes upstream.
#
# ⚠ VERSION COUPLING: ``action: "ignore"`` below requires a RevitControl whose
# ``handle_modal`` knows that verb (added 2026-07-30 alongside U-02). An older
# watchdog treats every action that is not halt/kill as dismiss — i.e. it would
# CLICK the model-upgrade progress dialog, and the only button there cancels the
# upgrade. Do not backport this entry to a machine running an older rig.
DEFAULT_KNOWN_DIALOGS: list[dict[str, str]] = [
    {"name": "unsigned_addin", "title_contains": "Unsigned Add-In",
     "action": "dismiss", "button": "Always Load"},
    {"name": "journal_recover", "text_contains": "recover",
     "action": "dismiss", "button": "No"},
    {"name": "unclean_shutdown", "text_contains": "did not shut down",
     "action": "dismiss", "button": "No"},
    {"name": "close_unclean", "text_contains": "didn't close",
     "action": "dismiss", "button": "No"},
    {"name": "save_changes", "text_contains": "save changes",
     "action": "dismiss", "button": "No"},
    # U-02 (live test 2026-07-30): Revit's own upgrade PROGRESS window, shown
    # when opening a model authored in an older release. Unmatched, it took the
    # unknown-dialog action — halt — which writes PAUSED and stops the watchdog
    # supervising the REST of the session, on a run that was otherwise healthy
    # (the dialog closes itself; the model opened; the audit converged). Every
    # nightly pointed at a model older than its host would raise that halt.
    # `ignore`, not `dismiss`: the click ladder would press Cancel and abort the
    # upgrade. `ignore`, not `halt`: a progress window is not a question. If one
    # ever genuinely blocks, `wait_addin_ready` still times out and the run
    # fails honestly (UnattendedLaunchError) rather than limping.
    {"name": "model_upgrade", "title_contains": "Model Upgrade",
     "action": "ignore"},
    {"name": "license", "text_contains": "license", "action": "halt"},
    {"name": "signin", "text_contains": "sign in", "action": "halt"},
    {"name": "subscription", "text_contains": "subscription", "action": "halt"},
]

# scripts/unattended_launch.py lives at the repo's top level, alongside
# fetch-forma-mcp.ps1 — this file resolves it relative to itself so it works
# regardless of cwd.
_LAUNCH_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "unattended_launch.py"

_WATCHDOG_STOP_TIMEOUT_S = 10.0

# After spawning the watchdog, give it a brief moment to exit on its own
# before proceeding — RevitControl's watchdog.py is gaining a singleton guard
# (exit code 3) so two audits on the same box don't double-supervise. Tests
# monkeypatch this to 0 so the suite doesn't pay the real delay.
_WATCHDOG_EARLY_EXIT_POLL_S = 0.5


class UnattendedSession:
    """Async context manager: spawn the RevitControl watchdog for the
    duration of the wrapped block, launching Revit first if it isn't
    already running.

    ``__aexit__`` terminates ONLY the watchdog process — it never kills
    Revit itself (the user decides when to close their own session)."""

    def __init__(
        self,
        *,
        services: "AuditServices",
        revit_exe: str,
        model_path: str,
        revit_version: int,
        staging_dir: Path,
    ) -> None:
        if services.revitcontrol is None:
            raise ValueError("UnattendedSession requires services.revitcontrol")
        self._rc = services.revitcontrol
        self._revit_exe = revit_exe
        self._model_path = model_path
        self._revit_version = revit_version
        self._staging = Path(staging_dir)
        self._unattended_dir = self._staging / "unattended"
        self._watchdog_proc: subprocess.Popen | None = None
        # U-01 (live test 2026-07-30): set by the caller's ``on_folder`` hook
        # once the run folder exists, so ``__aexit__`` can copy the FINAL
        # watchdog state in. See the comment there for why the hook's own copy
        # is not enough. ``None`` = no run folder was ever created (the run
        # aborted early, or a mode that writes none) — nothing to copy into.
        self.run_root: Path | None = None

    async def __aenter__(self) -> Self:
        self._unattended_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = self._write_watchdog_config()

        port = _resolve_port(str(self._revit_version))
        token = _load_auth_token(str(self._revit_version)) or ""

        # The launch DECISION (is Revit already running? do we need to start
        # it?) must happen BEFORE the watchdog exists — else the watchdog's
        # own "no revit.exe -> relaunch" tick can race a still-cold-starting
        # Revit and kill/relaunch it (the "launch storm"). Order is therefore:
        # launch phase (blocking, no watchdog yet) -> spawn watchdog (it must
        # exist to dismiss the Unsigned Add-In modal that blocks addin load)
        # -> wait phase (blocking, watchdog now covering the wait).
        # `asyncio.to_thread` on every blocking helper call below: this session
        # wraps `audit()`, which under the AuditHub service runs as a task on
        # the SERVICE's event loop. A bare `subprocess.run` there freezes the
        # whole service for as long as Revit takes to cold-start (minutes) —
        # /health times out and the SSE progress stream goes silent, so the UI
        # reads a working nightly as a dead one.
        launch_result = await asyncio.to_thread(
            subprocess.run,
            self._helper_argv("launch", port=port, token=token),
            cwd=self._rc.dir,
        )
        if launch_result.returncode != 0:
            # Not fatal by itself — the wait phase below will fail honestly
            # (and abort) if Revit genuinely never came up.
            log.warning(
                "unattended.launch_phase_failed",
                returncode=launch_result.returncode,
            )

        self._watchdog_proc = subprocess.Popen(
            [self._rc.python, "watchdog.py", str(cfg_path)],
            cwd=self._rc.dir,
        )
        log.info(
            "unattended.watchdog_started",
            pid=self._watchdog_proc.pid,
            cfg=str(cfg_path),
        )

        # Everything from here on must leave no watchdog behind. The wait phase
        # blocks for as long as Revit takes to come up, which is exactly when a
        # human hits Ctrl+C (KeyboardInterrupt) or the service cancels the job
        # (CancelledError) — neither is an `Exception`, and neither ran the
        # cleanup below before this try block existed. `__aexit__` does not
        # cover it either: a context manager whose `__aenter__` raises never
        # gets an `__aexit__`. The orphan then keeps its relaunch-Revit
        # authority indefinitely — the 2026-07-16 relaunch-loop class of bug,
        # this time from our side.
        try:
            # A watchdog that exits immediately means another instance is already
            # supervising this machine (RevitControl's singleton guard, exit code
            # 3) — not an error, just don't double-terminate someone else's
            # process on the way out.
            await asyncio.sleep(_WATCHDOG_EARLY_EXIT_POLL_S)
            if self._watchdog_proc.poll() is not None:
                log.info(
                    "unattended.watchdog_already_running",
                    exit_code=self._watchdog_proc.returncode,
                )
                self._watchdog_proc = None

            wait_result = await asyncio.to_thread(
                subprocess.run,
                self._helper_argv("wait", port=port, token=token),
                cwd=self._rc.dir,
            )
            if wait_result.returncode != 0:
                # A nightly box has no human to dismiss a stuck modal — fail fast
                # instead of letting the audit limp into run_revit and fail later
                # with a confusing MCP error.
                raise UnattendedLaunchError(
                    "unattended launch: RevitMCP addin did not become ready "
                    f"(wait phase exit code {wait_result.returncode}, "
                    f"port={port}) — aborting the unattended audit."
                )
        except BaseException:
            # Same terminate-only cleanup path as __aexit__ (never touches Revit).
            await self._stop_watchdog()
            raise
        return self

    def _helper_argv(self, phase: str, *, port: int, token: str) -> list[str]:
        return [
            self._rc.python, str(_LAUNCH_HELPER),
            "--phase", phase,
            "--rc-dir", self._rc.dir,
            "--exe", self._revit_exe,
            "--model", self._model_path or "",
            "--version", str(self._revit_version),
            "--port", str(port),
            "--token", token,
        ]

    async def _stop_watchdog(self) -> None:
        proc = self._watchdog_proc
        if proc is None:
            return
        proc.terminate()

        def _reap() -> None:
            try:
                proc.wait(timeout=_WATCHDOG_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_WATCHDOG_STOP_TIMEOUT_S)

        # Off the event loop: up to 2x10s of waiting, and this also runs from
        # `__aexit__` on the service's loop.
        await asyncio.to_thread(_reap)
        log.info("unattended.watchdog_stopped", pid=proc.pid)
        self._watchdog_proc = None
        # Deliberately does NOT touch Revit — see class docstring.

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._stop_watchdog()
        # Surface a watchdog-side crash-loop/halt (it writes PAUSED and stops
        # supervising) — otherwise it's invisible mid-run.
        paused = self._unattended_dir / "PAUSED"
        if paused.exists():
            reason = paused.read_text(encoding="utf-8").strip()
            log.error("unattended.watchdog_paused", reason=reason)
        # U-01 (live test 2026-07-30). This used to say the PAUSED file "already
        # rides the existing unattended/ copytree in persist_unattended_dir, so
        # this is a log line, not a new artifact". That was wrong, and the first
        # live run proved it: `persist_unattended_dir` is called from the
        # ``on_folder`` hook, which fires when the run folder is CREATED — near
        # the START of the run. Everything the watchdog writes afterwards (the
        # rest of events.jsonl, later screenshots, and PAUSED itself) missed the
        # copy, and the staging dir is a tempdir that is then removed. The halt
        # existed only in stdout — the one place an unattended box has no reader.
        # Copy again now: the watchdog is stopped, so this state is final.
        if self.run_root is not None:
            persist_unattended_dir(self._staging, self.run_root)

    def _write_watchdog_config(self) -> Path:
        cfg = {
            "revit_version": str(self._revit_version),
            "revit_exe": self._revit_exe,
            "default_model": self._model_path or "",
            "status_path": str(self._unattended_dir / "status.json"),
            "events_path": str(self._unattended_dir / "events.jsonl"),
            "screenshot_dir": str(self._unattended_dir / "shots"),
            "pause_flag": str(self._unattended_dir / "PAUSED"),
            # Phase-1 discipline preserved: an unrecognized dialog must NOT be
            # blind-clicked — halt for a human instead.
            "unknown_dialog_action": "halt",
            "known_dialogs": DEFAULT_KNOWN_DIALOGS,
        }
        cfg_path = self._unattended_dir / "watchdog.config.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return cfg_path


def persist_unattended_dir(staging: Path, run_root: Path) -> None:
    """Copy the staged ``unattended/`` artifacts (watchdog config + whatever
    it wrote to status/events/shots) into the run folder — mirrors
    ``audit_axes.persist_axes_dir`` (the run folder only exists once the run
    mode starts). Copy failure is a warning, never a crash."""
    src = Path(staging) / "unattended"
    if not src.exists():
        return
    dst = Path(run_root) / "unattended"
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except OSError as exc:
        log.warning(
            "unattended.persist_failed", staging=str(src), error=str(exc)
        )
