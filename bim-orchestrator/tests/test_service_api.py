"""P3-2 — AuditHub service API (service/app.py + runner.py).

Everything mocked: no Revit, no Forma, no satellites, no real audit —
the suite stays offline (acceptance #1). `fastapi` ships in the dev extras.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="service extras not installed")
from fastapi.testclient import TestClient  # noqa: E402

from bim_orchestrator.service import app as app_module  # noqa: E402
from bim_orchestrator.service.app import create_app  # noqa: E402
from bim_orchestrator.service.runner import AuditJob, SingleRunLock  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """App wired to a tmp runs root, with all external probes stubbed.

    MUST be used as a context manager: that keeps ONE portal event loop
    alive across requests, so the audit background task actually progresses
    between polls (a bare TestClient spins a fresh loop per request and the
    job would stay 'running' forever — the exact hang this suite had).
    """

    async def fake_probe_revit() -> bool:
        return False

    monkeypatch.setattr(app_module, "probe_revit", fake_probe_revit)
    monkeypatch.setattr(app_module, "probe_forma", lambda: True)
    monkeypatch.setattr(app_module, "probe_axes", lambda: (True, False))
    application = create_app(
        runs_root=tmp_path / "runs", approvals_dir=tmp_path / "approvals"
    )
    with TestClient(application) as c:
        yield c
        # Don't tear the portal down under a still-running job task.
        runner = application.state.runner
        if runner.current is not None and runner.current.status == "running":
            for _ in range(200):
                if runner.current.status != "running":
                    break
                time.sleep(0.01)


def _fake_audit(monkeypatch: pytest.MonkeyPatch, *, rc: int = 0, delay: float = 0.05):
    """Replace orchestrator.audit with a stub that emits a folder + returns."""

    class _Folder:
        run_id = "run-cafebabe"

    async def fake_audit(profile_path, autonomy_path, findings_out, **kw):
        on_folder = kw.get("on_folder")
        await asyncio.sleep(delay)
        if on_folder is not None:
            on_folder(_Folder)
        await asyncio.sleep(delay)
        return rc

    monkeypatch.setattr("bim_orchestrator.orchestrator.audit", fake_audit)
    return fake_audit


class TestHealth:
    def test_shape(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["version"], str)
        assert body["axes"] == {
            "revit": False, "forma": True, "lod": True, "spatial": False,
        }


class TestAudits:
    def test_post_returns_202_then_status_done(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake_audit(monkeypatch)
        resp = client.post("/audits", json={"profile_path": "config/audit.demo.yaml"})
        assert resp.status_code == 202
        audit_id = resp.json()["audit_id"]
        assert audit_id.startswith("aud-")
        assert resp.json()["run_id"] is None  # run folder not born yet

        # The portal loop runs in its own thread — give the job real time.
        for _ in range(200):
            status = client.get(f"/audits/{audit_id}").json()
            if status["status"] != "running":
                break
            time.sleep(0.01)
        assert status["status"] == "done"
        assert status["run_id"] == "run-cafebabe"
        assert status["error"] is None

    def test_second_post_while_running_is_409(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_audit(monkeypatch, delay=0.5)
        first = client.post("/audits", json={"profile_path": "x.yaml"})
        assert first.status_code == 202
        second = client.post("/audits", json={"profile_path": "y.yaml"})
        assert second.status_code == 409
        assert "single-run lock" in second.json()["detail"]

    def test_failed_audit_reports_error(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_audit(monkeypatch, rc=2)
        audit_id = client.post(
            "/audits", json={"profile_path": "x.yaml"}
        ).json()["audit_id"]
        for _ in range(200):
            status = client.get(f"/audits/{audit_id}").json()
            if status["status"] != "running":
                break
            time.sleep(0.01)
        assert status["status"] == "failed"
        assert "exit code 2" in status["error"]

    def test_body_must_name_exactly_one_source(self, client: TestClient) -> None:
        assert client.post("/audits", json={}).status_code == 422
        assert (
            client.post(
                "/audits",
                json={"profile_path": "x.yaml", "profile": {"name": "t"}},
            ).status_code
            == 422
        )

    def test_unknown_audit_id_404(self, client: TestClient) -> None:
        assert client.get("/audits/aud-00000000").status_code == 404

    def test_sse_stream_delivers_started_and_finished(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_audit(monkeypatch, delay=0.01)
        audit_id = client.post(
            "/audits", json={"profile_path": "x.yaml"}
        ).json()["audit_id"]
        events: list[dict] = []
        with client.stream("GET", f"/audits/{audit_id}/events") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:"):].strip()))
                if len(events) >= 3:
                    break
        messages = [e["message"] for e in events]
        assert messages[0] == "started"
        assert any(m.startswith("run_started run-cafebabe") for m in messages)


class TestRunsEndpoints:
    def _make_run(self, runs_root: Path, run_id: str = "run-0000abcd") -> Path:
        d = runs_root / run_id
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            json.dumps({"run_id": run_id, "status": "completed",
                        "started_at": "2026-07-11T00:00:00"}),
            encoding="utf-8",
        )
        (d / "report.md").write_text("# report", encoding="utf-8")
        (d / "verification_report.md").write_text("# verification", encoding="utf-8")
        (d / "axes").mkdir()
        (d / "axes" / "lod.json").write_text("{}", encoding="utf-8")
        return d

    def test_list_runs(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs")
        rows = client.get("/runs").json()
        assert rows[0]["run_id"] == "run-0000abcd"

    def test_reports_served_as_markdown(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs")
        r = client.get("/runs/run-0000abcd/report")
        assert r.status_code == 200
        assert r.text == "# report"
        assert "text/markdown" in r.headers["content-type"]
        v = client.get("/runs/run-0000abcd/verification-report")
        assert v.text == "# verification"

    def test_artifact_served(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs")
        r = client.get("/runs/run-0000abcd/artifacts/axes/lod.json")
        assert r.status_code == 200

    def test_artifact_traversal_blocked(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs")
        secret = tmp_path / "secret.env"
        secret.write_text("KEY=x", encoding="utf-8")
        r = client.get("/runs/run-0000abcd/artifacts/../../secret.env")
        assert r.status_code in (400, 404)
        r2 = client.get("/runs/run-0000abcd/artifacts/..%2F..%2Fsecret.env")
        assert r2.status_code in (400, 404)

    def test_bad_run_id_shape_rejected_before_fs(self, client: TestClient) -> None:
        assert client.get("/runs/../report").status_code in (400, 404)
        assert client.get("/runs/run-zzzzzzzz/report").status_code == 400

    def test_unknown_run_404(self, client: TestClient) -> None:
        assert client.get("/runs/run-99999999/report").status_code == 404


class TestDeltaEndpoint:
    """SPEC_SCHEDULED_AUDIT_DELTA.md W5 — GET /runs/{id}/delta serves
    delta.md the same way /report serves report.md (_markdown pattern)."""

    def _make_run(self, runs_root: Path, run_id: str = "run-0000abcd",
                   *, with_delta: bool = True) -> Path:
        d = runs_root / run_id
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            json.dumps({"run_id": run_id, "status": "completed",
                        "started_at": "2026-07-11T00:00:00"}),
            encoding="utf-8",
        )
        if with_delta:
            (d / "delta.md").write_text("# delta report", encoding="utf-8")
        return d

    def test_served_as_markdown_when_present(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs")
        r = client.get("/runs/run-0000abcd/delta")
        assert r.status_code == 200
        assert r.text == "# delta report"
        assert "text/markdown" in r.headers["content-type"]

    def test_404_when_delta_not_written(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs", with_delta=False)
        r = client.get("/runs/run-0000abcd/delta")
        assert r.status_code == 404

    def test_unknown_run_404(self, client: TestClient) -> None:
        assert client.get("/runs/run-99999999/delta").status_code == 404

    def test_api_prefix_variant_also_works(self, client: TestClient, tmp_path: Path) -> None:
        self._make_run(tmp_path / "runs")
        r = client.get("/api/runs/run-0000abcd/delta")
        assert r.status_code == 200
        assert r.text == "# delta report"


class TestApprovalsApplyOnce:
    def test_returns_counts(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            app_module, "apply_approvals_once", lambda approvals_dir: (2, 1)
        )
        resp = client.post("/approvals/apply-once")
        assert resp.status_code == 200
        assert resp.json() == {"applied": 2, "held": 1}

    def test_backend_failure_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(approvals_dir):
            raise RuntimeError("forma unreachable")

        monkeypatch.setattr(app_module, "apply_approvals_once", boom)
        resp = client.post("/approvals/apply-once")
        assert resp.status_code == 502
        assert "forma unreachable" in resp.json()["detail"]

    def test_409_while_single_run_lock_held(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SVC-2 (2026-07 review): the watcher WRITES Revit parameters (Path B)
        # — it must share the ONE single-run lock with POST /audits and
        # verification-views, not run concurrently with an audit.
        monkeypatch.setattr(
            app_module, "apply_approvals_once", lambda approvals_dir: (0, 0)
        )
        runner = client.app.state.runner
        assert runner.lock.acquire() is True
        try:
            resp = client.post("/approvals/apply-once")
            assert resp.status_code == 409
        finally:
            runner.lock.release()
        # lock free again → the pass runs (and releases the lock afterwards)
        assert client.post("/approvals/apply-once").status_code == 200
        assert runner.lock.acquire() is True  # not leaked by the handler
        runner.lock.release()


class TestSingleRunLock:
    def test_acquire_release_cycle(self, tmp_path: Path) -> None:
        lock = SingleRunLock(tmp_path)
        assert lock.acquire() is True
        assert lock.lock_path.exists()
        assert lock.acquire() is False  # held by self
        lock.release()
        assert not lock.lock_path.exists()
        assert lock.acquire() is True

    def test_stale_lock_from_dead_pid_is_cleared(self, tmp_path: Path) -> None:
        pytest.importorskip("psutil")
        lock_file = tmp_path / ".service_lock"
        lock_file.write_text("999999999", encoding="utf-8")  # pid can't exist
        lock = SingleRunLock(tmp_path)
        assert lock.acquire() is True

    def test_live_foreign_pid_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock_file = tmp_path / ".service_lock"
        lock_file.write_text("12345", encoding="utf-8")
        lock = SingleRunLock(tmp_path)
        monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
        assert lock.acquire() is False


class TestAuditJobStream:
    async def test_stream_replays_history_then_closes(self) -> None:
        job = AuditJob("aud-x")
        job.emit("service", "started")
        job.emit("qc", "qc.done")
        job.status = "done"
        got = [ev async for ev in job.stream_events()]
        assert [e["message"] for e in got] == ["started", "qc.done"]


class TestCsrfGuard:
    """S-01 (2026-07-25 live review) — the bind address never protected
    against CSRF, because the attack rides the USER'S browser, which is
    already local. `POST /approvals/apply-once` needs no body and no custom
    header (CORS-simple), so any open web page could fire a real Revit write
    pass with fetch(..., {mode:'no-cors'}) — zero clicks. One middleware
    rejects unsafe-method requests carrying a non-local Origin; browsers
    attach Origin to every cross-origin POST, so that header IS the vector.
    """

    def test_cross_origin_post_never_reaches_the_write_path(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler_ran(approvals_dir):  # pragma: no cover — must not run
            raise AssertionError("CSRF guard let the request through")

        monkeypatch.setattr(app_module, "apply_approvals_once", handler_ran)
        resp = client.post(
            "/approvals/apply-once", headers={"Origin": "https://evil.example"}
        )
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]
        # Rejected BEFORE the handler: the single-run lock was never taken.
        runner = client.app.state.runner
        assert runner.lock.acquire() is True
        runner.lock.release()

    def test_api_prefix_is_guarded_too(self, client: TestClient) -> None:
        # The router is included twice (root + /api) — the guard is
        # middleware precisely so both spellings are covered.
        resp = client.post(
            "/api/approvals/apply-once",
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403

    def test_null_origin_is_rejected(self, client: TestClient) -> None:
        # Sandboxed iframes and data: URLs send the literal string "null" —
        # that is exactly the anonymous context the guard exists for.
        resp = client.post(
            "/approvals/apply-once", headers={"Origin": "null"}
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:8601",   # the served SPA itself
            "http://localhost:5173",   # vite dev server, any local port
            "http://[::1]:8601",       # IPv6 loopback
        ],
    )
    def test_local_origins_still_pass(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, origin: str
    ) -> None:
        monkeypatch.setattr(
            app_module, "apply_approvals_once", lambda approvals_dir: (0, 0)
        )
        resp = client.post(
            "/approvals/apply-once", headers={"Origin": origin}
        )
        assert resp.status_code == 200

    def test_origin_that_merely_contains_localhost_is_rejected(
        self, client: TestClient
    ) -> None:
        # The check must be on the PARSED hostname, not a substring — the
        # classic bypass is registering localhost.evil.example.
        resp = client.post(
            "/approvals/apply-once",
            headers={"Origin": "https://localhost.evil.example"},
        )
        assert resp.status_code == 403

    def test_no_origin_header_is_untouched(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # curl / httpx / the runbooks send no Origin — this is a browser-
        # vector guard, not auth. Same-machine callers stay trusted (P3).
        monkeypatch.setattr(
            app_module, "apply_approvals_once", lambda approvals_dir: (1, 0)
        )
        assert client.post("/approvals/apply-once").status_code == 200

    def test_reads_are_not_guarded(self, client: TestClient) -> None:
        # GETs stay open even with a foreign Origin: without CORS headers the
        # browser cannot READ the response, and guarding them would break
        # nothing for an attacker but plenty for local tooling. Pinned so
        # nobody "helpfully" broadens the guard and kills SSE/dev-tool reads.
        resp = client.get("/health", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200


class TestJobEviction:
    """S-05 (2026-07-25 live review) — `AuditRunner.jobs` never evicted, and
    every finished job drags its full SSE event log along, so a service left
    running for days grew without bound. Finished jobs beyond
    MAX_FINISHED_JOBS go, oldest first; the durable record was never in this
    dict (run folders keep serving GET /runs/...), and an evicted id 404s
    exactly like an unknown one.
    """

    @staticmethod
    def _runner(tmp_path: Path):
        from bim_orchestrator.service.runner import AuditRunner

        return AuditRunner(tmp_path / "runs")

    def test_oldest_finished_evicted_beyond_cap(self, tmp_path: Path) -> None:
        from bim_orchestrator.service.runner import MAX_FINISHED_JOBS

        runner = self._runner(tmp_path)
        for i in range(MAX_FINISHED_JOBS + 5):
            job = AuditJob(f"aud-{i:08x}")
            job.status = "done"
            runner.jobs[job.audit_id] = job
        runner._evict_finished()
        assert len(runner.jobs) == MAX_FINISHED_JOBS
        # Insertion order = age: the 5 oldest are gone, the newest survive.
        assert "aud-00000000" not in runner.jobs
        assert f"aud-{MAX_FINISHED_JOBS + 4:08x}" in runner.jobs

    def test_running_job_is_never_evicted(self, tmp_path: Path) -> None:
        from bim_orchestrator.service.runner import MAX_FINISHED_JOBS

        runner = self._runner(tmp_path)
        live = AuditJob("aud-live0000")  # oldest entry, but still running
        runner.jobs[live.audit_id] = live
        for i in range(MAX_FINISHED_JOBS + 5):
            job = AuditJob(f"aud-{i:08x}")
            job.status = "failed" if i % 2 else "done"
            runner.jobs[job.audit_id] = job
        runner._evict_finished()
        assert "aud-live0000" in runner.jobs
        assert len(runner.jobs) == MAX_FINISHED_JOBS + 1

    def test_start_runs_eviction(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The eviction point is start() — no background task to forget. Pin
        # the wire, not just the helper (the L-01 lesson: both ends correct,
        # nothing in between).
        _fake_audit(monkeypatch, delay=0.01)
        runner = client.app.state.runner
        calls: list[bool] = []
        real = runner._evict_finished
        monkeypatch.setattr(
            runner, "_evict_finished", lambda: calls.append(True) or real()
        )
        assert (
            client.post("/audits", json={"profile_path": "x.yaml"}).status_code
            == 202
        )
        assert calls, "start() no longer evicts finished jobs"


class TestJobCeiling:
    """M-03 (2026-08-01 review) — an audit had no wall-clock ceiling.

    A stdio server that spawns but never completes its handshake left the job
    awaiting forever. `current` stayed occupied and the PID lock stayed held,
    so every later POST /audits returned 409 — on an unattended box that is a
    nightly that fails in silence until someone notices the delta reports
    stopped appearing.
    """

    def test_a_hung_audit_fails_and_frees_the_lock(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def never_returns(profile_path, autonomy_path, findings_out, **kw):
            await asyncio.Event().wait()

        monkeypatch.setattr("bim_orchestrator.orchestrator.audit", never_returns)
        monkeypatch.setenv("AUTOAUDIT_JOB_TIMEOUT_S", "0.05")

        resp = client.post("/audits", json={"profile_path": "x.yaml"})
        assert resp.status_code == 202
        audit_id = resp.json()["audit_id"]

        # Poll through the API, like every other job test here — each GET is
        # pumped through the portal loop, so the ceiling timer provably gets
        # loop time regardless of how idle the TestClient portal is between
        # requests. The first version of this test polled
        # `runner.jobs[...].status` directly from the test thread with a
        # wall-clock deadline and flaked roughly one full-suite run in three,
        # with nothing to distinguish "ceiling never fired" from "the loop
        # was never given a turn". Monotonic clock for the same reason: the
        # deadline is a hang-stop, not a measurement, and wall clock can jump.
        deadline = time.monotonic() + 30.0
        status = "running"
        while time.monotonic() < deadline:
            status = client.get(f"/audits/{audit_id}").json()["status"]
            if status != "running":
                break
            time.sleep(0.02)

        runner = client.app.state.runner
        job = runner.jobs[audit_id]
        assert status == "failed", "the hung job never hit its ceiling"
        assert "ceiling" in (job.error or "")
        # The next scheduled run must get a clean start, not a permanent 409.
        assert runner.lock.acquire() is True
        runner.lock.release()

    def test_a_bad_timeout_env_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"No ceiling" is the failure mode this exists to remove, so garbage
        must not silently restore it."""
        from bim_orchestrator.service.runner import _DEFAULT_JOB_TIMEOUT_S, _job_timeout_s

        for bad in ("abc", "0", "-1", ""):
            monkeypatch.setenv("AUTOAUDIT_JOB_TIMEOUT_S", bad)
            assert _job_timeout_s() == _DEFAULT_JOB_TIMEOUT_S
        monkeypatch.setenv("AUTOAUDIT_JOB_TIMEOUT_S", "120")
        assert _job_timeout_s() == 120.0


class TestLockIdentityAndAtomicity:
    """L-07 — the lock's two real holes, and the honest scope note.

    The finding was filed as a TOCTOU race, which is the narrowest of the
    three problems here. The one that actually bites is PID recycling: an
    audit dies, the OS hands its number to something unrelated, the liveness
    check sees the number alive and every later run is refused — 409 forever,
    until a human deletes the file.
    """

    def test_a_recycled_pid_does_not_hold_the_lock_forever(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bim_orchestrator.service import runner as runner_mod

        runs = tmp_path / "runs"
        runs.mkdir()
        # A lock written by a process that has since died, whose PID number
        # now belongs to something else (same number, different start time).
        (runs / ".service_lock").write_text("4242 1000.0", encoding="utf-8")
        monkeypatch.setattr(SingleRunLock, "_pid_alive", lambda self, pid: True)
        monkeypatch.setattr(runner_mod, "_process_start_time", lambda pid: 9999.0)

        lock = SingleRunLock(runs)
        assert lock.acquire() is True, (
            "a recycled PID kept the lock — every later audit would 409 until "
            "someone deleted the file by hand"
        )
        lock.release()

    def test_the_same_process_still_holds_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other end: matching PID *and* start time is genuinely the same
        process, so the lock must be respected."""
        from bim_orchestrator.service import runner as runner_mod

        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / ".service_lock").write_text("4242 1000.0", encoding="utf-8")
        monkeypatch.setattr(SingleRunLock, "_pid_alive", lambda self, pid: True)
        monkeypatch.setattr(runner_mod, "_process_start_time", lambda pid: 1000.0)

        assert SingleRunLock(runs).acquire() is False

    def test_a_legacy_pid_only_lock_is_still_understood(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locks written before this change carry a bare PID. They must keep
        working — a service upgraded mid-audit must not lose its own lock."""
        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / ".service_lock").write_text("4242", encoding="utf-8")
        monkeypatch.setattr(SingleRunLock, "_pid_alive", lambda self, pid: True)

        assert SingleRunLock(runs).acquire() is False

    def test_a_live_foreign_owner_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lock is cross-PROCESS state; two objects inside one process are
        already separated by the in-process flag. So the second acquirer has
        to look like a different process — otherwise the test proves nothing
        (the code deliberately lets a process reclaim its own leftover, which
        is how a restarted service recovers)."""
        from bim_orchestrator.service import runner as runner_mod

        runs = tmp_path / "runs"
        assert SingleRunLock(runs).acquire() is True   # "the service"

        monkeypatch.setattr(os, "getpid", lambda: 999_001)   # a different process
        monkeypatch.setattr(SingleRunLock, "_pid_alive", lambda self, pid: True)
        monkeypatch.setattr(
            runner_mod, "_process_start_time",
            lambda pid: 1000.0 if pid == 999_001 else None,
        )
        assert SingleRunLock(runs).acquire() is False

    def test_a_released_lock_is_free_again(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        lock = SingleRunLock(runs)
        assert lock.acquire() is True
        lock.release()
        assert not (runs / ".service_lock").exists()
        assert SingleRunLock(runs).acquire() is True

    def test_the_owner_token_records_pid_and_start_time(self, tmp_path: Path) -> None:
        """The start time is what turns a recyclable number into an identity."""
        runs = tmp_path / "runs"
        lock = SingleRunLock(runs)
        assert lock.acquire() is True
        raw = (runs / ".service_lock").read_text(encoding="utf-8").strip()
        parts = raw.split()
        assert parts[0] == str(os.getpid())
        assert len(parts) == 2, "no start time recorded — PID recycling is back"
        float(parts[1])                       # parses as a timestamp
        lock.release()


class TestCliWarnsInsteadOfBlocking:
    """L-07 mảnh 4 — the CLI shares the document but not the lock.

    Warn, never block: a CLI process that died without releasing a lock would
    jam the nightly audit behind a stale file with nobody there to see it.
    A visible warning in front of a human is worth more than a lock that can
    take the service down.
    """

    def test_it_warns_when_the_service_holds_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        import bim_orchestrator.orchestrator as orch

        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / ".service_lock").write_text("4242 1000.0", encoding="utf-8")
        monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", runs)

        orch._warn_if_service_busy("run-revit")

        err = capsys.readouterr().err
        assert "4242" in err
        assert "run-revit" in err
        assert "Continuing anyway" in err, "the warning must not read as a refusal"

    def test_it_is_silent_when_no_lock_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        import bim_orchestrator.orchestrator as orch

        monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", tmp_path / "runs")
        orch._warn_if_service_busy("audit")
        assert capsys.readouterr().err == ""

    def test_an_unreadable_marker_never_breaks_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A warning helper must never be why a command fails to start."""
        import bim_orchestrator.orchestrator as orch

        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / ".service_lock").mkdir()      # a directory, not a file
        monkeypatch.setattr(orch, "DEFAULT_RUNS_DIR", runs)

        orch._warn_if_service_busy("audit")   # must not raise
