"""ApprovalWatcher (v1.4-K5) — Loop 2 of the approval-resume flow.

Out-of-band watcher: polls ACC issues created by DesignAgent for approve-gated
Path B fixes. When a proposal issue's status reaches the approve signal
(default ``in_progress``), it executes the parked Revit writes in ONE
``revit_batch`` transaction (single undo), comments + closes the issue, and
marks the local record applied (idempotent).

Reuses the trust pipeline: the approve gate simply moves from a CLI re-run to an
ACC status change. Records live as ``<approvals_dir>/<issue_id>.json`` written by
``DesignAgent._create_proposal_issue``.

The Revit document must be open when the watcher fires (writes target it).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from bim_orchestrator.approval_store import read_record, write_record
from bim_orchestrator.mcp_clients.revit import (
    RevitEnvelopeError,
    batch_commit_outcomes,
)
from bim_orchestrator.policies.approval_integrity import fingerprint, parse_fingerprint
from bim_orchestrator.policies.demo_identity import DEMO_PROJECT_ID

log = structlog.get_logger(__name__)

# ACC status that signals "approved — apply now" (verified in project
# permittedStatuses: draft|open|pending|in_progress|in_review|closed).
DEFAULT_APPLY_STATUS = "in_progress"

# Low10: consecutive `watch()` passes with every `get_issue` call failing
# before the loop gives up and raises (liveness escalation) — see `watch()`.
LIVENESS_DEAD_PASS_LIMIT = 5

# H3: a per-record lock guards against two appliers (e.g. `--watch-approvals`
# and the Streamlit "Apply now" button) racing on the same record and
# double-applying. A lock older than this is considered stale (a prior applier
# crashed) and may be stolen — safe because the per-fix `applied` flags below
# keep even a raced re-apply idempotent.
_STALE_LOCK_S = 600.0


def _acquire_lock(record_path: Path) -> Path | None:
    """Atomically create ``<record>.lock``. Returns the lock path on success, or
    ``None`` if another (live) applier already holds it. A stale lock is stolen."""
    lock = record_path.with_suffix(".lock")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return None
        if age <= _STALE_LOCK_S:
            return None
        try:                                    # steal a stale lock
            lock.unlink()
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (OSError, FileExistsError):
            return None
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
    finally:
        os.close(fd)
    return lock


def _release_lock(lock: Path | None) -> None:
    if lock is None:
        return
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        # Swallows exactly: Windows holding the file open, or a permission
        # hiccup. Release is best-effort by design — a leaked lock file is
        # reclaimed by `_acquire_lock`'s STALENESS check (mtime older than
        # _STALE_LOCK_S → steal), so failing loudly here would only turn a
        # self-healing case into a crash on the exit path. (F-4, 2026-08-16:
        # this comment previously claimed a "pid + start_time identity" check —
        # that mechanism belongs to the SERVICE's SingleRunLock, not this
        # per-record lock; the pid+timestamp written into the file is for a
        # human debugging, nothing reads it back.)
        pass


def _load_records(approvals_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    out: list[tuple[Path, dict[str, Any]]] = []
    if not approvals_dir.exists():
        return out
    for p in sorted(approvals_dir.glob("*.json")):
        rec = read_record(p)
        if rec is None:
            continue  # unreadable — read_record has already said so, loudly
        if rec.get("issue_id") and rec.get("fixes"):
            out.append((p, rec))
    return out


def _step_for(f: dict[str, Any]) -> dict[str, Any]:
    """Build the revit_batch step for one fix, dispatching by remediation action.

    v1.4-K17: a rename fix (Family/FamilySymbol Name) MUST use ``rename_element``
    (the Name property setter), NOT ``set_parameter`` — Name isn't a writable
    Parameter, so set_parameter fails and the rename silently no-ops. Default to
    set_parameter for older records that predate the ``action`` field.
    """
    eid = int(f["element_id"])
    if f.get("action") == "rename_element":
        return {"command": "rename_element", "params": {"id": eid, "name": f["new_value"]}}
    return {
        "command": "set_parameter",
        "params": {"id": eid, "parameterName": f["parameter"], "value": f["new_value"]},
    }


def _norm(v: Any) -> str:
    """Comparison key: None/blank collapse to '', everything else strips to str."""
    return "" if v is None else str(v).strip()


def _classify_stale(
    pending: list[dict[str, Any]], live_before: dict[int, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Shared classification: given a live ``before`` value per pending index,
    sort each fix into keep-for-write / already-applied / stale. Mutates the
    fix dicts in place (``stale`` flag, ``applied`` on already-satisfied) and
    returns ``(fixes_to_write, degraded)``.

    Low8: a live value matching EITHER the display ``old_value`` OR the raw
    ``old_value_raw`` (when the proposal stashed it) counts as "unchanged
    since propose" — the display string can be lossy (e.g. formatted units)
    while the raw value is what the parameter read API actually returns.

    ``degraded`` is True when at least one fix couldn't actually be judged
    (no live read, or no captured baseline) — those fixes still fail OPEN
    (kept for write, per the conservative design), but the caller should
    surface that the drift check didn't truly cover every fix.
    """
    to_write: list[dict[str, Any]] = []
    degraded = False
    for i, f in enumerate(pending):
        f["stale"] = False                          # refreshed each pass
        if i not in live_before:
            to_write.append(f)                      # couldn't read → fail-open
            degraded = True
            continue
        old = f.get("old_value")
        old_raw = f.get("old_value_raw")
        if old is None and old_raw is None:
            to_write.append(f)                      # no baseline → can't judge
            degraded = True
            continue
        live = live_before[i]
        if _norm(live) == _norm(old) or (old_raw is not None and _norm(live) == _norm(old_raw)):
            to_write.append(f)                      # unchanged → safe to write
        elif _norm(live) == _norm(f.get("new_value")):
            f["applied"] = True                     # already at target → satisfied
            log.info("approval_watcher.already_at_target",
                     element_id=f.get("element_id"), value=live)
        else:
            f["stale"] = True                       # drifted → hold back
            log.warning("approval_watcher.stale_skip", element_id=f.get("element_id"),
                        expected=old, live=live, proposed=f.get("new_value"))
    return to_write, degraded


async def _reprove_stale_per_element(
    revit: Any, pending: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool] | None:
    """M6 fallback: per-element dry-run re-preview when a batch dry-run isn't
    available (older addin lacking ``/mcp/batch``). Reads ``changes.before``
    off each individual ``set_parameter``/``rename_element`` dry-run response
    and runs it through the SAME classification as the batch path. Returns
    ``None`` (not an empty-ish tuple — a real "nothing to write" is a valid,
    non-degraded outcome) if even this per-element check fails, so the caller
    can fall back further and mark the pass degraded."""
    live_before: dict[int, Any] = {}
    try:
        for i, f in enumerate(pending):
            eid = int(f["element_id"])
            if f.get("action") == "rename_element":
                res = await revit.rename_element(eid, f["new_value"], dry_run=True)
            else:
                res = await revit.set_parameter(eid, f["parameter"], f["new_value"], dry_run=True)
            # set_parameter/rename_element return the FULL envelope (like
            # `_prepare_revit_fix` reads at propose time), not unwrapped data.
            # L-04: same rule as the batch path — an envelope with no
            # `changes` was not measured, so it must not masquerade as a
            # reading of None.
            changes = (res.get("data") or {}).get("changes")
            if not isinstance(changes, dict):
                changes = res.get("changes")
            if isinstance(changes, dict):
                live_before[i] = changes.get("before")
    except Exception as exc:
        log.warning("approval_watcher.reprove_per_element_failed", error=str(exc),
                    note="per-element stale re-preview also unavailable; failing open")
        return None
    return _classify_stale(pending, live_before)


async def _reprove_stale(
    revit: Any, pending: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Re-preview each pending write against the LIVE model and drop stale ones.

    A proposal captured ``old_value`` at propose time; between proposal and
    approval a human may have changed the model. Before committing, a dry-run
    batch reads each write target's current value (``changes.before``) and:

      * live == old_value (or ``old_value_raw`` — Low8) → unchanged since
        propose → keep (safe to write);
      * live == new_value  → someone already applied the exact fix → mark the
        fix ``applied`` (satisfied, so the issue can still close) and drop it;
      * live is a THIRD value → genuine drift → mark ``stale`` and drop it, so a
        newer value a human set is never clobbered (the issue stays open and the
        held-back fix is visible; a later poll re-evaluates it).

    M6: when the addin lacks batch dry-run (``RevitEnvelopeError``), this no
    longer fails open silently — it falls back to a PER-ELEMENT dry-run
    re-preview (``_reprove_stale_per_element``) and runs the same
    classification. Only if THAT also can't be done does the check truly fail
    open (conservative — the fingerprint gate remains the primary protection).

    Returns ``(fixes_to_write_this_pass, degraded)``. ``degraded=True`` means
    no stale-drift check could actually be performed (fail-open path) — the
    caller should surface this on the record/close-comment so a human knows
    the drift check was skipped, not silently satisfied.
    """
    steps = [_step_for(f) for f in pending]
    try:
        env = await revit.batch(steps, dry_run=True)
    except RevitEnvelopeError as exc:
        log.warning("approval_watcher.reprove_unavailable", code=exc.code,
                    note="batch dry-run unavailable; trying per-element re-preview")
        per_element = await _reprove_stale_per_element(revit, pending)
        if per_element is None:
            return pending, True                    # truly degraded — fail-open
        return per_element
    results = (env.get("data") or {}).get("results") if isinstance(env, dict) else None
    if not results:
        per_element = await _reprove_stale_per_element(revit, pending)
        if per_element is None:
            return pending, True
        return per_element
    live_before: dict[int, Any] = {}
    for i, res in enumerate(results):
        if i >= len(pending):
            break
        # L-04: only record a reading when the step actually CARRIES one. A
        # step that reports ok but no `changes` was not measured — and
        # `.get("before")` on the missing dict yields None, which `_norm`
        # turns into "" and `_classify_stale` then reads as a THIRD value:
        # "a human changed this between propose and approve". The fix was
        # held back for good (the issue never closes, so every later pass
        # re-runs the same broken measurement) and the `degraded` flag never
        # set, because `i in live_before`. So the run asserted drift it had
        # no evidence for — the exact posture `batch_commit_outcomes` already
        # forbids on the commit side ("missing results is unconfirmed, not
        # success"). Leaving `i` out of the dict routes it to the fail-open +
        # degraded branch that already exists.
        # `changes` present with `before: None` is a real reading (an empty
        # parameter) and must keep flowing through — the guard is on the
        # CONTAINER, not on the value.
        if not isinstance(res, dict) or res.get("ok") is False or "error" in res:
            continue
        changes = res.get("changes")
        if isinstance(changes, dict):
            live_before[i] = changes.get("before")
    return _classify_stale(pending, live_before)


async def _commit_writes(
    revit: Any, fixes: list[dict[str, Any]], *, degraded_out: list[bool] | None = None
) -> int:
    """Apply every not-yet-applied fix in ONE revit_batch (fall back to per-element
    when the addin lacks HTTP batch). Marks ``f['applied']=True`` on the fixes that
    succeed and returns the number applied in THIS call.

    H3: this is now idempotent + partial-safe. Fixes already carrying
    ``applied=True`` are skipped (a re-poll never re-writes them), and a step that
    FAILS leaves its fix pending (``applied`` unset) instead of aborting the whole
    batch or blanket-claiming success — so the next poll retries only the failures.
    Reads the batch's per-step ``results`` (like the DesignAgent commit) so a
    partial batch can't report writes that never landed. Raises only on a
    non-partial transport error (whole batch rejected) so the caller can retry.

    M6: ``degraded_out`` is an optional out-param (kept this way rather than
    widening the return type to a tuple, since ``_commit_writes`` is exercised
    directly by ``TestCommitWritesDispatch`` as a plain ``int``-returning
    call). When the stale re-preview couldn't be judged at all (batch dry-run
    AND per-element dry-run both unavailable), ``True`` is appended so the
    caller (``ApprovalWatcher._apply``) can mark the record/close-comment
    degraded."""
    pending = [f for f in fixes if not f.get("applied")]
    if not pending:
        return 0
    # M-sec2: re-preview against the live model and drop fixes whose value
    # drifted since propose (never clobber a newer human edit). May mark some
    # fixes applied (already-at-target) as a side effect — those still count
    # toward all_done in the caller.
    pending, degraded = await _reprove_stale(revit, pending)
    if degraded_out is not None:
        degraded_out.append(degraded)
    if not pending:
        return 0
    steps = [_step_for(f) for f in pending]     # exactly one step per fix
    try:
        env = await revit.batch(steps, dry_run=False)
    except RevitEnvelopeError as exc:
        if exc.code != "unknown_command":
            raise
        log.warning("approval_watcher.batch_unsupported", note="per-element fallback")
        applied = 0
        for f in pending:
            try:
                if f.get("action") == "rename_element":
                    await revit.rename_element(int(f["element_id"]), f["new_value"], dry_run=False)
                else:
                    await revit.set_parameter(
                        int(f["element_id"]), f["parameter"], f["new_value"], dry_run=False
                    )
            except RevitEnvelopeError as e:
                log.error("approval_watcher.commit_step_failed",
                          element_id=f.get("element_id"), code=e.code)
                continue
            f["applied"] = True
            applied += 1
        return applied
    # Batch path: one step per fix → outcomes index-aligned with `pending`.
    # P1-TRUST-01: this used to read `ok = (results is None) or ...` — an
    # envelope that confirmed NOTHING counted as every write succeeding, and
    # `committed` was never consulted at all. A record marked applied closes
    # the ACC issue, i.e. the BIM Coordinator's action queue, so an unconfirmed
    # write became a closed ticket over a model that may never have changed.
    outcomes = batch_commit_outcomes(env, expected=len(pending))
    applied = 0
    for f, outcome in zip(pending, outcomes):
        if outcome.ok:
            f["applied"] = True
            applied += 1
        else:
            log.error("approval_watcher.commit_step_failed",
                      element_id=f.get("element_id"), error=outcome.reason)
    return applied


# C-2 (review round 7, 2026-08-17): records written by --demo carry this
# project id (mirror of `demo.dataset.DEMO_PROJECT_ID` — duplicated here so
# production code never imports the demo package; a test pins the two equal).
# The watcher must never poll them: `get_issue` on a simulated project ALWAYS
# throws against real ACC, so a dir whose only pending records were demo ones
# read as "ACC unreachable, every attempt failing" pass after pass — a false
# liveness suicide while ACC was healthy. New demo records land in their own
# dir (C-1, orchestrator.DEFAULT_DEMO_APPROVALS_DIR); this guard covers the
# legacy mixed-dir case and any explicitly shared --approvals-dir.
_DEMO_PROJECT_ID = DEMO_PROJECT_ID  # re-exported: see policies/demo_identity.py


class ApprovalWatcher:
    """Polls ACC proposal issues and applies approved Path B fixes."""

    def __init__(
        self,
        approvals_dir: Path,
        forma_client: Any,
        revit_client: Any,
        *,
        apply_status: str = DEFAULT_APPLY_STATUS,
    ) -> None:
        self._dir = Path(approvals_dir)
        self._forma = forma_client
        self._revit = revit_client
        self._apply_status = apply_status
        # Low10: liveness bookkeeping for `watch()` — counts of `get_issue`
        # attempts/successes in the MOST RECENT `scan_once` pass, so a caller
        # polling in a loop can detect "ACC is unreachable" (every attempt
        # failing, pass after pass) versus "nothing to do this pass" (zero
        # attempts because no record is due — not a liveness problem).
        self.last_pass_get_issue_attempts = 0
        self.last_pass_get_issue_ok = 0

    async def scan_once(self) -> list[dict[str, Any]]:
        """One poll pass. Returns the records applied this pass."""
        applied: list[dict[str, Any]] = []
        self.last_pass_get_issue_attempts = 0
        self.last_pass_get_issue_ok = 0
        for path, rec in _load_records(self._dir):
            # C-2: a simulated (--demo) record is never applied against real
            # ACC/Revit and never counts toward liveness — see _DEMO_PROJECT_ID.
            if rec.get("project_id") == _DEMO_PROJECT_ID:
                log.debug("approval_watcher.demo_record_skipped", path=str(path))
                continue
            if rec.get("applied"):
                # M7 (c): all writes already landed, but a prior close/comment
                # attempt failed transiently — retry JUST the close, never the
                # writes (idempotency: _commit_writes is not re-entered).
                if rec.get("issue_status") == "applied_pending_close":
                    issue_id = rec.get("issue_id")
                    lock = _acquire_lock(path)
                    if lock is None:
                        log.info("approval_watcher.locked_skip", issue_id=issue_id,
                                 note="another applier holds the record lock")
                        continue
                    try:
                        if await self._close_issue(path, rec, rec.get("applied_writes", 0)):
                            applied.append(rec)
                    except Exception as exc:
                        log.error("approval_watcher.close_retry_failed",
                                  issue_id=issue_id, error=str(exc))
                    finally:
                        _release_lock(lock)
                continue
            issue_id = rec.get("issue_id")
            project_id = rec.get("project_id")
            # H3: a record missing project_id used to raise KeyError HERE (outside
            # the try) and kill the whole watch loop. Skip it defensively instead.
            if not project_id:
                log.warning("approval_watcher.record_missing_project_id",
                            path=str(path), issue_id=issue_id)
                continue
            self.last_pass_get_issue_attempts += 1
            try:
                got = await self._forma.get_issue(project_id, issue_id)
            except Exception as exc:
                log.warning("approval_watcher.get_issue_failed",
                            issue_id=issue_id, error=str(exc))
                continue
            self.last_pass_get_issue_ok += 1
            issue = got.get("issue") or got
            status = issue.get("status")
            if status != self._apply_status:
                log.debug("approval_watcher.pending", issue_id=issue_id, status=status)
                continue
            # H3: hold a per-record lock while applying so a second applier
            # (--watch-approvals vs Streamlit "Apply now") can't double-apply.
            lock = _acquire_lock(path)
            if lock is None:
                log.info("approval_watcher.locked_skip", issue_id=issue_id,
                         note="another applier holds the record lock")
                continue
            try:
                if await self._apply(path, rec, issue):
                    applied.append(rec)
            except Exception as exc:
                log.error("approval_watcher.apply_failed",
                          issue_id=issue_id, error=str(exc))
            finally:
                _release_lock(lock)
        return applied

    def _verify_integrity(self, rec: dict[str, Any], issue: dict[str, Any] | None) -> bool:
        """True if the record's write-set still matches what was approved.

        The fingerprint carried in the fetched ISSUE (the artifact the human
        approved in ACC) is the trust anchor; a fingerprint stored on the record
        is a fallback for issues whose marker was stripped. If neither exists the
        record predates fingerprinting → don't block it (back-compat)."""
        current = fingerprint(rec.get("fixes") or [])
        from_issue = parse_fingerprint((issue or {}).get("description"))
        # Low7: the ACC issue body is the trust anchor (it's what the human
        # actually approved); a fingerprint on the record alone is a WEAKER
        # fallback (the record lives on disk, outside ACC's audit trail) — flag
        # it so an operator can notice the anchor degraded, even though it
        # still passes (back-compat).
        if from_issue is None and rec.get("fingerprint"):
            log.warning("approval_watcher.anchor_downgraded", issue_id=rec.get("issue_id"),
                        note="issue description has no fingerprint marker; "
                             "falling back to the record's own fingerprint")
        anchor = from_issue or rec.get("fingerprint")
        if anchor is None:
            return True                             # legacy record → nothing to verify
        return current == anchor

    async def _apply(
        self, path: Path, rec: dict[str, Any], issue: dict[str, Any] | None = None
    ) -> bool:
        """Apply a record's approved writes. Returns True if the pass made
        progress (a write landed or the issue closed); False for a no-op — the
        integrity gate refused, or every fix was held back as stale."""
        issue_id = rec.get("issue_id")
        fixes = rec["fixes"]

        # Approval-security gate: refuse to write if the parked fixes no longer
        # match the fingerprint the human approved (record tampered / swapped).
        if not self._verify_integrity(rec, issue):
            log.error("approval_watcher.integrity_mismatch", issue_id=issue_id,
                      note="record fixes do not match approved proposal; refusing to apply")
            rec["status"] = "integrity_failed"
            rec["issue_status"] = "integrity_failed"
            rec["integrity_error"] = (
                "record write-set does not match the approved proposal fingerprint"
            )
            write_record(path, rec)
            return False

        degraded_out: list[bool] = []
        applied_now = await _commit_writes(self._revit, fixes, degraded_out=degraded_out)
        degraded = bool(degraded_out and degraded_out[0])
        total_applied = sum(1 for f in fixes if f.get("applied"))
        stale = sum(1 for f in fixes if f.get("stale"))
        all_done = bool(fixes) and all(f.get("applied") for f in fixes)
        log.info("approval_watcher.applied", issue_id=issue_id,
                 applied_now=applied_now, total=len(fixes), stale=stale,
                 all_done=all_done, degraded=degraded)

        if degraded:
            rec["degraded"] = "no_stale_check"

        # M7: persist the write outcome BEFORE attempting the ACC close/comment
        # calls. Previously the record was only written AFTER the close attempt
        # completed (success or failure) — a crash/kill between a successful
        # `_commit_writes` and the close call left NO record on disk reflecting
        # that the writes had already landed, so a restart could re-run
        # `_commit_writes` needlessly (harmless thanks to the per-fix `applied`
        # flags once persisted, but only once persisted).
        rec["applied"] = all_done
        rec["applied_writes"] = total_applied
        rec["stale_writes"] = stale
        rec["applied_at"] = datetime.now(timezone.utc).isoformat()
        rec["status"] = "applied" if all_done else "partial"
        rec["issue_status"] = "applied_pending_close" if all_done else "partial_pending"
        write_record(path, rec)

        if not all_done:
            # H3: partial apply — leave the issue OPEN so the next poll retries the
            # still-pending fixes (already-applied ones are skipped); never close.
            # M-sec2: `stale` fixes were deliberately held back (live value drifted).
            log.warning("approval_watcher.partial_apply", issue_id=issue_id,
                        applied=total_applied, stale=stale,
                        remaining=len(fixes) - total_applied)
            return applied_now > 0

        closed = await self._close_issue(path, rec, total_applied)
        # "applied this pass" = progress was made. A pass that only held stale
        # fixes back (0 writes, nothing closed) is a no-op → don't report it.
        return closed or applied_now > 0

    async def _close_issue(self, path: Path, rec: dict[str, Any], total_applied: int) -> bool:
        """Close the ACC issue (preview -> token -> execute) + audit comment for
        an already-fully-applied record, then persist the final lifecycle.

        Split out of ``_apply`` (M7) so ``scan_once`` can retry JUST this step
        — without touching Revit again — for a record stuck in
        ``applied_pending_close`` (all writes landed, but a prior close/comment
        call failed transiently). Returns True on success."""
        issue_id = rec.get("issue_id")
        project_id = rec.get("project_id")
        summary = f"AutoAudit applied {total_applied} Revit write(s) after approval."
        if rec.get("degraded") == "no_stale_check":
            summary += " (degraded: no stale re-preview available)"
        try:
            prev = await self._forma.update_issue(
                project_id, issue_id, status="closed", dry_run=True
            )
            await self._forma.update_issue(
                project_id, issue_id, status="closed",
                dry_run=False, approval_token=prev.get("approval_token"),
            )
            cprev = await self._forma.add_issue_comment(
                project_id, issue_id, summary, dry_run=True
            )
            await self._forma.add_issue_comment(
                project_id, issue_id, summary,
                dry_run=False, approval_token=cprev.get("approval_token"),
            )
        except Exception as exc:
            log.warning("approval_watcher.close_failed", issue_id=issue_id, error=str(exc))
            return False
        # M7 (b): persist the final status only after close actually succeeds.
        rec["issue_status"] = "closed"
        write_record(path, rec)
        return True

    async def watch(self, *, interval_s: float = 30.0, max_passes: int | None = None) -> None:
        """Poll forever (or ``max_passes`` times). Sleeps ``interval_s`` between.

        Low10: escalates when ACC looks unreachable rather than polling
        forever in silence. A pass where every ``get_issue`` attempt failed
        counts toward ``_consecutive_dead_passes``; a pass with at least one
        success resets it. ``LIVENESS_DEAD_PASS_LIMIT`` consecutive all-fail
        passes raises ``RuntimeError`` (non-zero exit) so an operator running
        ``--watch-approvals`` unattended notices instead of the loop quietly
        never applying anything. A pass with zero attempts (no record due)
        is neutral — neither counted as dead nor as a reset."""
        passes = 0
        consecutive_dead_passes = 0
        while True:
            applied = await self.scan_once()
            log.info("approval_watcher.pass", applied=len(applied))
            if self.last_pass_get_issue_attempts > 0:
                if self.last_pass_get_issue_ok == 0:
                    consecutive_dead_passes += 1
                    log.warning("approval_watcher.pass_all_get_issue_failed",
                                consecutive=consecutive_dead_passes,
                                attempts=self.last_pass_get_issue_attempts)
                    if consecutive_dead_passes >= LIVENESS_DEAD_PASS_LIMIT:
                        log.error("approval_watcher.liveness_lost",
                                  consecutive=consecutive_dead_passes)
                        raise RuntimeError(
                            f"ApprovalWatcher: {consecutive_dead_passes} consecutive passes "
                            "with every get_issue call failing — ACC appears unreachable."
                        )
                else:
                    consecutive_dead_passes = 0
            passes += 1
            if max_passes is not None and passes >= max_passes:
                return
            await asyncio.sleep(interval_s)
