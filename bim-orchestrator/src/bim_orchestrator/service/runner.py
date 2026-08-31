"""Audit job runner for the AuditHub service (P3-2).

One job at a time (D7): a module-level lock + a PID file under the runs root
(``runs/.service_lock``) enforce the single-run invariant — two concurrent
runs writing into the same Revit document is a real failure mode (review M9).

Progress events come from TWO sources merged into one ordered list:
  * the runner's own coarse marks (started / run_started / finished / failed)
  * the structlog tap (``logging_setup.set_service_tap``) active INSIDE the
    audit task — every orchestrator log event (query.* / qc.* / design.* /
    audit_axes.* / run_recorder.*) is forwarded, giving the SSE stream real
    phase granularity without touching the graph.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml

from bim_orchestrator.logging_setup import reset_service_tap, set_service_tap

log = structlog.get_logger(__name__)

# Wall-clock ceiling for ONE audit job. Deliberately far above any real run
# (a 300-element Revit audit is minutes, not hours) — this is a deadlock
# backstop, not a performance budget, and a false trip cancels a legitimate
# nightly. Override with AUTOAUDIT_JOB_TIMEOUT_S for a genuinely huge model.
_DEFAULT_JOB_TIMEOUT_S = 7200.0


def _job_timeout_s() -> float:
    """Read the ceiling at call time so a test (or an operator) can change it
    without re-importing the module. Garbage or non-positive → the default,
    because "no ceiling" is the failure mode this exists to remove."""
    raw = os.environ.get("AUTOAUDIT_JOB_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_JOB_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("service.bad_job_timeout", value=raw)
        return _DEFAULT_JOB_TIMEOUT_S
    return value if value > 0 else _DEFAULT_JOB_TIMEOUT_S


# Event names are dot-namespaced; the first segment maps to a coarse phase
# for the progress UI. Anything unlisted renders as phase "run".
_PHASE_BY_PREFIX = {
    "audit_axes": "axes",
    "lod_mcp": "axes",
    "spatial_mcp": "axes",
    "query": "query",
    "revit_query": "query",
    "geometry_query": "query",
    "qc": "qc",
    "grounding": "qc",
    "design": "design",
    "route": "design",
    "run_recorder": "record",
    "service": "service",
}


def _phase_for(event_name: str) -> str:
    prefix = event_name.split(".", 1)[0]
    return _PHASE_BY_PREFIX.get(prefix, "run")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditJob:
    """State + event log of one audit invocation."""

    def __init__(self, audit_id: str) -> None:
        self.audit_id = audit_id
        self.status: str = "running"
        self.phase: str = "service"
        self.run_id: str | None = None
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self._new_event = asyncio.Condition()
        self.task: asyncio.Task | None = None

    def emit(self, phase: str, message: str) -> None:
        self.phase = phase
        self.events.append({"ts": _now_iso(), "phase": phase, "message": message})
        # Wake SSE subscribers — only possible when a loop is running (the
        # audit task / endpoints); outside one there's nobody to wake.
        try:
            asyncio.get_running_loop().create_task(self._notify())
        except RuntimeError:
            # Swallows exactly: get_running_loop() outside a loop — i.e. a
            # sync caller (CLI path) emitting progress with no SSE listener
            # in existence. The event is still appended above; only the wake
            # is skipped.
            pass

    async def _notify(self) -> None:
        async with self._new_event:
            self._new_event.notify_all()

    async def stream_events(self):
        """Yield every event (past + live) until the job finishes."""
        idx = 0
        while True:
            while idx < len(self.events):
                yield self.events[idx]
                idx += 1
            if self.status != "running":
                return
            async with self._new_event:
                try:
                    await asyncio.wait_for(self._new_event.wait(), timeout=5.0)
                except TimeoutError:
                    pass  # periodic re-check so a finished job always closes


def _process_start_time(pid: int) -> float | None:
    """When `pid` started, or None if that cannot be determined.

    The PID alone is not an identity. Operating systems recycle PID numbers —
    Windows quickly — so an audit that died can have its number handed to
    something unrelated, and a liveness check on the number alone then reports
    the lock as held forever. Pairing the number with the process's start time
    makes it an identity: same number AND same start time is the same process.
    """
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:  # not installed, gone, or access denied
        return None


class SingleRunLock:
    """D7: one audit per service instance, with an on-disk owner marker so a
    human can see WHO holds it (and a restarted service can clear a stale one).

    SCOPE, stated plainly (L-07): this coordinates the paths that go through
    the SERVICE — POST /audits, approvals apply-once, verification views.
    Commands run straight from the CLI do not take it. So the invariant is
    "one audit at a time per service instance", not "per machine"; the CLI
    warns when it sees the lock held rather than blocking on it. See
    `orchestrator._warn_if_service_busy`.
    """

    def __init__(self, runs_root: Path) -> None:
        self._runs_root = runs_root
        self._lock_path = runs_root / ".service_lock"
        self._held = False

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def _pid_alive(self, pid: int) -> bool:
        try:
            import psutil  # optional — best-effort staleness check

            return psutil.pid_exists(pid)
        except ImportError:
            return True  # can't verify → treat as live (conservative)

    def _owner_still_running(self, raw: str) -> bool:
        """Is the process named in the lock file still the one that took it?

        Accepts both formats: the legacy bare PID, and `pid started_at`. A
        legacy file can only be checked by number (the pre-existing
        behaviour); with a start time, a recycled PID is correctly seen as a
        DIFFERENT process and the lock is released instead of jamming every
        later run behind a stranger's number.
        """
        parts = raw.split()
        if not parts:
            return False
        try:
            pid = int(parts[0])
        except ValueError:
            return False
        if pid == os.getpid():
            return False                      # our own leftover — reclaim it
        if not self._pid_alive(pid):
            return False
        if len(parts) < 2:
            return True                       # legacy file: number only
        try:
            recorded_start = float(parts[1])
        except ValueError:
            return True
        actual_start = _process_start_time(pid)
        if actual_start is None:
            return True                       # cannot tell → assume held
        if abs(actual_start - recorded_start) > 1.0:
            log.warning(
                "service.stale_lock_pid_recycled",
                pid=pid, recorded_start=recorded_start, actual_start=actual_start,
                note="the PID is alive but belongs to a different process — "
                "the audit that held this lock is gone",
            )
            return False
        return True

    def _owner_token(self) -> str:
        start = _process_start_time(os.getpid())
        return f"{os.getpid()} {start}" if start is not None else str(os.getpid())

    def acquire(self) -> bool:
        if self._held:
            return False
        self._runs_root.mkdir(parents=True, exist_ok=True)
        # O_CREAT|O_EXCL: create-or-fail in ONE syscall. The previous
        # exists()-then-write left a window where two processes both saw no
        # lock and both wrote one — the same race `approval_watcher.
        # _acquire_lock` already avoids this way.
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                raw = self._lock_path.read_text(encoding="utf-8").strip()
            except OSError:
                raw = ""
            if self._owner_still_running(raw):
                return False
            log.warning("service.stale_lock_cleared", path=str(self._lock_path))
            try:
                self._lock_path.unlink(missing_ok=True)
                fd = os.open(
                    str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except (OSError, FileExistsError):
                return False                  # someone else won the reclaim
        except OSError as exc:
            log.warning("service.lock_acquire_failed", error=str(exc))
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self._owner_token())
        self._held = True
        return True

    def release(self) -> None:
        self._held = False
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("service.lock_release_failed", error=str(exc))


# S-05 (2026-07-25 live review): `jobs` kept every finished AuditJob — and
# each one owns its full `events` list (thousands of tap entries per audit) —
# so a service left running for days grew without bound. Finished jobs beyond
# this many are evicted, oldest first; their durable output was never here
# (it lives in the run folder — GET /runs/{id}/... keeps working), only the
# in-memory status/SSE record dies, which for an evicted job returns the same
# 404 as an unknown id. The running job is never evicted.
MAX_FINISHED_JOBS = 20


class AuditRunner:
    """Owns the current job + the single-run lock."""

    def __init__(self, runs_root: Path) -> None:
        self.lock = SingleRunLock(runs_root)
        self.jobs: dict[str, AuditJob] = {}
        self.current: AuditJob | None = None

    def get(self, audit_id: str) -> AuditJob | None:
        return self.jobs.get(audit_id)

    def _evict_finished(self) -> None:
        # dict preserves insertion order → the front of this list is oldest.
        finished = [a for a, j in self.jobs.items() if j.status != "running"]
        for audit_id in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            del self.jobs[audit_id]
            log.info("service.job_evicted", audit_id=audit_id)

    def start(
        self,
        *,
        profile_path: str | None,
        profile_inline: dict[str, Any] | None,
        max_iterations: int = 3,
    ) -> AuditJob | None:
        """Spawn the audit task. None → another audit is running (409)."""
        if self.current is not None and self.current.status == "running":
            return None
        if not self.lock.acquire():
            return None
        self._evict_finished()
        job = AuditJob("aud-" + secrets.token_hex(4))
        self.jobs[job.audit_id] = job
        self.current = job
        job.task = asyncio.get_running_loop().create_task(
            self._execute(job, profile_path, profile_inline, max_iterations)
        )
        return job

    async def _execute(
        self,
        job: AuditJob,
        profile_path: str | None,
        profile_inline: dict[str, Any] | None,
        max_iterations: int,
    ) -> None:
        # Import here: the service package must stay importable without
        # dragging the whole orchestrator at module-import time.
        from bim_orchestrator import orchestrator

        # The audit prints unicode (→ ✓ banner emoji) on the SERVICE's
        # stdout; a console-spawned service on Windows inherits cp1252 and
        # the first such print kills the whole audit ('charmap' codec error,
        # caught live 2026-07-12). Idempotent, covers every entrypoint
        # (autoaudit-service AND bare uvicorn).
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except (ValueError, OSError):  # detached/closed stream (tests)
                    pass

        def tap(event_dict: dict[str, Any]) -> None:
            name = str(event_dict.get("event", ""))
            if not name:
                return
            extras = {
                k: v
                for k, v in event_dict.items()
                if k not in ("event", "timestamp", "level")
                and isinstance(v, (str, int, float, bool))
            }
            message = name if not extras else f"{name} {json.dumps(extras, default=str)}"
            job.emit(_phase_for(name), message)

        def on_folder(folder: Any) -> None:
            job.run_id = folder.run_id
            job.emit("service", f"run_started {folder.run_id}")

        inline_tmp: Path | None = None
        token = set_service_tap(tap)
        try:
            job.emit("service", "started")
            if profile_inline is not None:
                fd, name = tempfile.mkstemp(prefix="autoaudit-inline-", suffix=".yaml")
                inline_tmp = Path(name)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    yaml.safe_dump(profile_inline, fh, allow_unicode=True)
                profile_path = str(inline_tmp)
            assert profile_path is not None  # AuditRequest validated one source
            rc = await asyncio.wait_for(
                orchestrator.audit(
                    Path(profile_path),
                    orchestrator.DEFAULT_AUTONOMY_PATH,
                    orchestrator.DEFAULT_FINDINGS_OUT,
                    max_iterations=max_iterations,
                    checkpoint_dir=orchestrator.DEFAULT_CHECKPOINT_DIR,
                    on_folder=on_folder,
                ),
                timeout=_job_timeout_s(),
            )
            if rc == 0:
                job.status = "done"
                job.emit("service", "finished")
            else:
                job.status = "failed"
                job.error = f"exit code {rc}"
                job.emit("service", f"failed exit_code={rc}")
        except TimeoutError:
            # Without a ceiling, one hung audit holds `current` and the PID lock
            # for good: every later POST /audits 409s, and on an unattended box
            # nobody notices until the delta reports stop appearing. Recorded as
            # a failure so the artifact says what happened; `finally` releases
            # the lock, so the next scheduled run gets a clean start.
            secs = _job_timeout_s()
            job.status = "failed"
            job.error = f"audit exceeded the {secs:.0f}s job ceiling and was cancelled"
            job.emit("service", f"failed timeout after {secs:.0f}s")
            log.error(
                "service.audit_timeout", audit_id=job.audit_id, timeout_s=secs,
                hint="raise AUTOAUDIT_JOB_TIMEOUT_S if this model legitimately "
                "needs longer",
            )
        except Exception as exc:  # job must record, never propagate into loop
            job.status = "failed"
            job.error = str(exc)
            job.emit("service", f"failed {exc}")
            log.error("service.audit_failed", audit_id=job.audit_id, error=str(exc))
        finally:
            reset_service_tap(token)
            self.lock.release()
            if inline_tmp is not None:
                inline_tmp.unlink(missing_ok=True)
