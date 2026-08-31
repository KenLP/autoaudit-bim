# Scheduled audit + delta report (level-1 continuous audit)

Architecture: [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md).

Runs an audit **unattended, on a schedule**, and answers the question a BIM
manager actually cares about between two runs: which problems are NEW, which
are RESOLVED, and which are still PERSISTENT.

## Architecture

```
Windows Task Scheduler (daily 22:30 on this box)
   → scripts/scheduled_audit.ps1  -ProfilePath config\audit.nightly.yaml
        POST /audits  (AuditHub service, :8601)
        poll GET /audits/{id} until done/failed/timeout
   → orchestrator.audit()  — profile has  run.propose_only: true
        check + propose (ACC Issue / approve-gated proposal) — NEVER writes Revit
   → runs/<run_id>/delta.md + delta.json
        resolved / newly_introduced / persistent vs the last comparable run
```

**The scheduler is a thin PowerShell client OUTSIDE the service.** It never
talks to Revit/ACC/rules directly — it only calls `POST /audits`, so it goes
through the service's existing `SingleRunLock`. This means a scheduled run
can never collide with an audit a human starts from the UI at the same time
(one of them gets HTTP 409 and skips its turn — see the exit-code table
below). It also means the service keeps its "orchestrate-only, zero business
logic" invariant — no second scheduler lives inside the service process.

**`run.propose_only: true` is the non-negotiable part.** A scheduled run
must never write to the model unattended. Setting this on the profile demotes
every would-be-`auto` Path B decision (including the deterministic
`compose_template` fill that normally bypasses `autonomy.yaml` — an Opt
B) to `approve` — the fix still gets proposed as an approve-gated ACC issue
(Loop 2 / `ApprovalWatcher`), it just never commits without a human setting
the issue to "In progress". Path A (creating an ACC Issue for a manual
finding) is unaffected: creating the issue **is** the propose act, not a
model write.

## What the single-run lock does and does not cover

The service refuses a second audit while one is running (`409`, D7). The lock
is `runs/.service_lock`, holding `pid start_time` — the start time is there so
a PID the OS has recycled is recognised as a DIFFERENT process instead of
pinning the lock forever.

Its scope is **the service**. Three paths take it: `POST /audits`, approvals
apply-once, and verification-view creation. **Commands run from the CLI do
not.** `bim-orchestrator --run-revit` / `--audit` / `--watch-approvals` touch
the same Revit document, notice the lock, and print a warning — then continue.

That is deliberate, not an oversight. A CLI process that died without
releasing a lock would block the nightly audit night after night with nobody
there to see it; a warning in front of a human who can decide is worth more
than a lock that can take the schedule down. So: **do not run CLI commands
that touch Revit while the service is mid-audit** — the reading can be
inconsistent and writes can interleave. If you need the guarantee rather than
the warning, stop the service first.

## Cross-run issue dedup (Path A)

Without dedup, every scheduled run would re-raise a fresh ACC issue for every
problem still open from last night — the exact anti-pattern that makes a
"nightly audit" feature useless. `audit()` threads a cross-run
`runs/issue_registry.json` (see `issue_registry.py`) into `DesignAgent`: before
raising a Path A issue for a `(rule, status, element-set)` group, the agent
checks whether a PREVIOUS run already raised one and, if so, asks ACC whether
it's still open (`forma.get_issue`). Still open → skip (logged as
`design.issue_skipped_cross_run`); closed → the problem reappeared, so a
fresh issue is correctly raised. A failed ACC lookup (network blip, deleted
issue) fails OPEN — it creates the issue anyway rather than silently
swallowing a real warning.

This registry is **only** wired up by `audit()`. A bare `--run` / `--run-revit`
CLI invocation never touches it — legacy behaviour is unchanged.

## Delta report

After every `--audit` invocation that ends in a successful status
(`delta_report.SUCCESSFUL_STATUSES` = `"completed"` from `--check`/`--apply`,
`"converged"` from the graph modes `--run`/`--run-revit` — the scheduled
audit's main path),
`delta_report.write_delta_report` renders `runs/<run_id>/delta.md` +
`delta.json`. It is pure render-from-disk (reads only `metadata.json` /
`outcomes.json` / `profile.json` already on disk for the current run and its
baseline — it never re-runs a check), and reuses the existing
`run_recorder.diff_outcomes` — no new diff algorithm.

**Baseline selection** picks the newest *earlier* run with a successful
status (same `SUCCESSFUL_STATUSES` set) and the SAME identity as the current
run:
- same `profile.json` → `profile_name` (scheduled runs always have one — see
  below), or
- no `profile.json` on either side AND same `metadata.mode` (a bare CLI run).

This stops an unrelated ad-hoc run from polluting the nightly-vs-nightly diff.
A run's identity is stamped by `audit()` into `runs/<run_id>/profile.json`
(`profile_name`, `mode`, `rules` — by basename, not absolute path — and
`propose_only`) the moment the run folder is created.

Read it via:
- `runs/<run_id>/delta.md` / `delta.json` directly on disk, or
- `GET /runs/{run_id}/delta` (and `GET /api/runs/{run_id}/delta`) — plain
  Markdown, 404 if the run predates this feature or the render failed.
  `delta.json` needs no dedicated endpoint — it's already reachable via
  `GET /runs/{run_id}/artifacts/delta.json`.

## Setting it up

1. Copy the example profile and edit the machine-local paths:
   ```powershell
   Copy-Item config\audit.nightly.yaml.example config\audit.nightly.yaml
   ```
   Point `rules:` at your real rules file(s) and confirm `run.propose_only:
   true` is set (it is in the example — don't remove it for a scheduled run).
2. Register the AuditHub service to start at logon (once per machine):
   ```powershell
   Register-ScheduledTask -TaskName "AutoAudit Service" `
     -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
       -Argument "-NoProfile -Command uv run autoaudit-service") `
     -Trigger (New-ScheduledTaskTrigger -AtLogOn)
   ```
3. Register the nightly audit itself:
   ```powershell
   Register-ScheduledTask -TaskName "AutoAudit Nightly" `
     -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
       -Argument "-NoProfile -ExecutionPolicy Bypass -File D:\...\scripts\scheduled_audit.ps1 -ProfilePath D:\...\config\audit.nightly.yaml") `
     -Trigger (New-ScheduledTaskTrigger -Daily -At 22:30)
   ```
   ("Run whether user is logged on or not" needs the task configured with
   stored credentials — set that in Task Scheduler's UI or add
   `-User`/`-Password` to `Register-ScheduledTask`.)
4. Check `runs\scheduled_audit.log` the next morning, then open
   `runs/<run_id>/delta.md` (or `GET /runs/{run_id}/delta`).

## `scripts/scheduled_audit.ps1` exit codes

| Code | Meaning |
|---|---|
| 0 | Audit completed (`status: done`) |
| 2 | Audit failed (`status: failed`) — see the logged `error` |
| 3 | Busy — another audit was already running (`POST /audits` returned 409). The scheduled run **skips this slot**; it does not retry or queue. |
| 4 | Service unreachable (connection refused/timeout on `POST /audits`) |
| 5 | Timeout — no `done`/`failed` within `-TimeoutMinutes` (default 120) |
| 6 | Bad response — `POST /audits` returned 2xx but no `audit_id` |

## Known limitations

- **Revit must already be open with the right model** when `run.mode:
  run_revit` fires (the addin's HTTP endpoint needs a live document). Pair
  with `unattended.enabled: true` (P3-3, RevitControl watchdog) if you need
  Revit itself launched unattended too — see `docs/PRODUCTION_PACKAGING.md`.
- **A 409 (busy) is a skipped slot, not a queued retry.** If a human is
  running an audit from the UI right at 22:30, that night's scheduled run is
  simply skipped — it does not retry later in the same night.
- **Event-driven audits** (triggering on ACC model publish via the
  `dm.version.added` webhook) and **notifications** (Telegram/email when the
  delta looks bad) are levels 2 and 3 — out of scope here. `delta.json` is the
  pre-built hook a notifier would read.
