"""P3-3 — UnattendedSession (RevitControl watchdog wrapper).

All tests mock ``subprocess.Popen``/``subprocess.run`` — never spawn a real
process. Live verification (a real watchdog dismissing a real dialog) is
done by hand, see docs/specs/SPEC_PHASE3_REMAINDER.md acceptance #2.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog.testing

from bim_orchestrator import orchestrator
from bim_orchestrator.policies.audit_profile import AuditServices, RevitControlService
from bim_orchestrator.unattended import (
    DEFAULT_KNOWN_DIALOGS,
    UnattendedLaunchError,
    UnattendedSession,
    persist_unattended_dir,
)


def _services(tmp_path: Path, *, with_revitcontrol: bool = True) -> AuditServices:
    if not with_revitcontrol:
        return AuditServices()
    rc_dir = tmp_path / "RevitControl"
    rc_dir.mkdir(exist_ok=True)
    rc_python = rc_dir / "python.exe"
    rc_python.write_text("", encoding="utf-8")  # .exists() only checks presence
    return AuditServices(
        revitcontrol=RevitControlService(dir=str(rc_dir), python=str(rc_python))
    )


class _FakePopen:
    """Records the args it was constructed with; behaves like a live process
    just enough for terminate()/wait()/poll() to be exercised."""

    instances: list["_FakePopen"] = []
    # Class-level override consumed by the NEXT constructed instance: None
    # means "still running" (poll() -> None); an int simulates a watchdog
    # that has already exited by the time __aenter__ polls it.
    next_returncode: int | None = None

    def __init__(self, args, cwd=None, **kw):
        self.args = args
        self.cwd = cwd
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.returncode = _FakePopen.next_returncode
        _FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    _FakePopen.instances = []
    _FakePopen.next_returncode = None
    yield
    _FakePopen.instances = []
    _FakePopen.next_returncode = None


@pytest.fixture()
def call_sequence() -> list[str]:
    """Populated by ``mock_subprocess`` in call order — e.g.
    ``["run:launch", "popen", "run:wait"]`` on the happy path — so the
    launch -> watchdog -> wait ordering can be asserted directly."""
    return []


def _phase_of(argv: list[str]) -> str | None:
    return argv[argv.index("--phase") + 1] if "--phase" in argv else None


# How long the faked launch/wait helper "takes". Long enough that a blocking
# call is unmistakable next to a 10 ms ticker, short enough to keep the suite
# fast (two calls = 0.6s).
_HELPER_S = 0.3


@pytest.fixture()
def mock_subprocess(monkeypatch: pytest.MonkeyPatch, call_sequence: list[str]):
    """Replace Popen (watchdog spawn) and run (launch helper, both phases)
    with no-op fakes so no real process is ever started."""

    def fake_popen(args, cwd=None, **kw):
        call_sequence.append("popen")
        return _FakePopen(args, cwd=cwd, **kw)

    monkeypatch.setattr("bim_orchestrator.unattended.subprocess.Popen", fake_popen)

    def fake_run(args, cwd=None, **kw):
        call_sequence.append(f"run:{_phase_of(args)}")
        return subprocess.CompletedProcess(args=args, returncode=0)

    fake_run_mock = MagicMock(side_effect=fake_run)
    monkeypatch.setattr("bim_orchestrator.unattended.subprocess.run", fake_run_mock)
    monkeypatch.setattr(
        "bim_orchestrator.unattended._resolve_port", lambda version: 7891
    )
    monkeypatch.setattr(
        "bim_orchestrator.unattended._load_auth_token", lambda version: "tok-123"
    )
    # The real early-exit poll is a production-only delay; keep the suite fast.
    monkeypatch.setattr("bim_orchestrator.unattended._WATCHDOG_EARLY_EXIT_POLL_S", 0)
    return fake_run_mock


class TestWatchdogConfigTemplate:
    async def test_config_matches_dialogrule_schema(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="C:/models/sample.rvt",
            revit_version=2027,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass

        cfg_path = tmp_path / "staging" / "unattended" / "watchdog.config.json"
        assert cfg_path.exists()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

        # revit_version is a STRING in WatchdogConfig (RevitControl's
        # watchdog.py:56), even though the profile carries it as an int.
        assert cfg["revit_version"] == "2027"
        assert isinstance(cfg["revit_version"], str)
        assert cfg["revit_exe"] == "C:/Revit/Revit.exe"
        assert cfg["default_model"] == "C:/models/sample.rvt"
        # unrecognized dialog must halt — never blind-clicked.
        assert cfg["unknown_dialog_action"] == "halt"

        assert len(cfg["known_dialogs"]) == 9
        for rule in cfg["known_dialogs"]:
            assert set(rule.keys()) <= {
                "name", "title_contains", "text_contains", "action", "button"
            }
            assert "name" in rule and "action" in rule
        names = {r["name"] for r in cfg["known_dialogs"]}
        assert {"unsigned_addin", "license", "signin", "save_changes"} <= names


class TestWatchdogSpawn:
    async def test_popen_args_and_cwd(self, tmp_path: Path, mock_subprocess) -> None:
        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass

        assert len(_FakePopen.instances) == 1
        proc = _FakePopen.instances[0]
        assert proc.args[0] == services.revitcontrol.python
        assert proc.args[1] == "watchdog.py"
        cfg_path = Path(proc.args[2])
        assert cfg_path.name == "watchdog.config.json"
        assert proc.cwd == services.revitcontrol.dir

    async def test_launch_helper_invoked_with_resolved_port_and_token(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="C:/models/sample.rvt",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass

        # Two-phase helper: --phase launch (before the watchdog), then
        # --phase wait (after) — both carrying the same resolved port/token.
        assert mock_subprocess.call_count == 2
        phases = []
        for call_args in mock_subprocess.call_args_list:
            launch_args = call_args.args[0]
            assert launch_args[0] == services.revitcontrol.python
            assert launch_args[1].endswith("unattended_launch.py")
            assert "--port" in launch_args and "7891" in launch_args
            assert "--token" in launch_args and "tok-123" in launch_args
            assert "--version" in launch_args and "2026" in launch_args
            assert call_args.kwargs["cwd"] == services.revitcontrol.dir
            phases.append(_phase_of(launch_args))
        assert phases == ["launch", "wait"]


class TestAexitNeverKillsRevit:
    async def test_exit_terminates_watchdog_only(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass

        watchdog_proc = _FakePopen.instances[0]
        assert watchdog_proc.terminated is True
        # No call anywhere spawned/killed a Revit process directly — the only
        # Popen is the watchdog; the only subprocess.run is the launch helper
        # (which itself never kills Revit, see scripts/unattended_launch.py).
        assert len(_FakePopen.instances) == 1
        for call in mock_subprocess.call_args_list:
            argv = call.args[0]
            assert "kill" not in " ".join(str(a) for a in argv).lower()


class TestPhaseOrdering:
    async def test_launch_before_watchdog_before_wait(
        self, tmp_path: Path, mock_subprocess, call_sequence: list[str]
    ) -> None:
        """The launch decision must happen before the watchdog exists (else
        its own relaunch tick can race a cold-starting Revit); the watchdog
        must exist before the wait phase (it dismisses the startup modal
        that blocks addin load) — see unattended.py's module docstring."""
        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass

        assert call_sequence == ["run:launch", "popen", "run:wait"]


class TestWaitPhaseFailure:
    async def test_wait_failure_terminates_watchdog_and_raises(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        def fake_run(args, cwd=None, **kw):
            rc = 1 if _phase_of(args) == "wait" else 0
            return subprocess.CompletedProcess(args=args, returncode=rc)

        mock_subprocess.side_effect = fake_run

        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )

        entered_body = False
        with pytest.raises(UnattendedLaunchError):
            async with session:
                entered_body = True  # pragma: no cover — must never run

        assert entered_body is False
        # Same terminate-only path as __aexit__ — never a Revit kill.
        assert len(_FakePopen.instances) == 1
        assert _FakePopen.instances[0].terminated is True

    async def test_an_interrupt_during_the_wait_also_stops_the_watchdog(
        self, tmp_path: Path, mock_subprocess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The nonzero-exit path above was the only one that cleaned up.

        The wait phase blocks for as long as Revit takes to start — which is
        exactly when a human presses Ctrl+C, or the service cancels the job.
        Neither is an ``Exception``, and an ``__aenter__`` that raises never
        gets an ``__aexit__``, so the watchdog survived the audit that spawned
        it and kept its authority to relaunch Revit indefinitely (the
        2026-07-16 relaunch-loop class, from our side this time).
        """

        def run_interrupted(args, cwd=None, **kw):
            if _phase_of(args) == "wait":
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_subprocess.side_effect = run_interrupted

        session = UnattendedSession(
            services=_services(tmp_path),
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        with pytest.raises(KeyboardInterrupt):
            async with session:
                pass  # pragma: no cover — never reached

        assert len(_FakePopen.instances) == 1
        assert _FakePopen.instances[0].terminated is True


class TestTheEventLoopKeepsRunning:
    async def test_launch_and_wait_do_not_freeze_the_loop(
        self, tmp_path: Path, mock_subprocess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`__aenter__` must not block the event loop it runs on.

        Under the AuditHub service this session wraps an audit running as a
        task on the SERVICE's loop. A bare `subprocess.run` there freezes the
        whole service for the minutes Revit needs to cold-start: /health times
        out and the SSE progress stream goes silent, so an operator watching
        the UI sees a working nightly as a dead one.

        Pinned by behaviour rather than by "was to_thread called", and measured
        as the LONGEST STALL rather than a tick count: counting ticks passes as
        long as *some* part of `__aenter__` yielded, so it stayed green with one
        of the two calls still blocking (found by mutating this very test).
        """

        def slow_run(args, cwd=None, **kw):
            time.sleep(_HELPER_S)  # stands in for Revit's cold start
            return subprocess.CompletedProcess(args=args, returncode=0)

        mock_subprocess.side_effect = slow_run

        gaps: list[float] = []

        async def ticker() -> None:
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.get_running_loop().create_task(ticker())
        try:
            session = UnattendedSession(
                services=_services(tmp_path),
                revit_exe="C:/Revit/Revit.exe",
                model_path="",
                revit_version=2026,
                staging_dir=tmp_path / "staging",
            )
            async with session:
                pass
        finally:
            task.cancel()

        assert gaps, "the ticker never ran at all"
        # A responsive loop reschedules the ticker every ~10 ms; a loop frozen
        # by one blocking helper call shows a single gap of _HELPER_S. The
        # threshold sits between the two with room for scheduler jitter.
        assert max(gaps) < _HELPER_S / 2, (
            "event loop stalled inside __aenter__ for "
            f"{max(gaps):.3f}s (helper call is {_HELPER_S}s) — a blocking "
            "subprocess call is back"
        )
        assert _FakePopen.instances[0].killed is False


class TestPausedFlagSurfaced:
    async def test_paused_flag_logged_and_persisted(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        services = _services(tmp_path)
        staging_dir = tmp_path / "staging"
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=staging_dir,
        )
        reason = "crash-loop: revit.exe exited 3x within 5min"

        with structlog.testing.capture_logs() as cap_logs:
            async with session:
                # Simulate the watchdog writing PAUSED mid-run (crash-loop
                # halt) — a real watchdog would do this asynchronously while
                # the wrapped block runs.
                (staging_dir / "unattended" / "PAUSED").write_text(
                    reason, encoding="utf-8"
                )

        paused_events = [
            e for e in cap_logs if e.get("event") == "unattended.watchdog_paused"
        ]
        assert len(paused_events) == 1
        assert paused_events[0]["reason"] == reason
        assert paused_events[0]["log_level"] == "error"

        # No new artifact format — PAUSED already rides the existing
        # unattended/ copytree, so the halt is visible in the run folder too.
        run_root = tmp_path / "run-abc"
        run_root.mkdir()
        persist_unattended_dir(staging_dir, run_root)
        assert (run_root / "unattended" / "PAUSED").read_text(
            encoding="utf-8"
        ) == reason


class TestWatchdogAlreadyRunning:
    async def test_immediate_exit_skips_terminate_no_error(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        """Exit code 3 = RevitControl's watchdog singleton guard: another
        instance is already supervising. Must not be treated as an error,
        and __aexit__ must not try to terminate a process we didn't spawn."""
        _FakePopen.next_returncode = 3
        services = _services(tmp_path)
        session = UnattendedSession(
            services=services,
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            assert session._watchdog_proc is None

        assert len(_FakePopen.instances) == 1
        assert _FakePopen.instances[0].terminated is False
        assert _FakePopen.instances[0].killed is False


class TestPersistUnattendedDir:
    def test_copies_staged_artifacts_into_run_folder(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        (staging / "unattended").mkdir(parents=True)
        (staging / "unattended" / "watchdog.config.json").write_text("{}", encoding="utf-8")
        run_root = tmp_path / "run-abc"
        run_root.mkdir()

        persist_unattended_dir(staging, run_root)

        assert (run_root / "unattended" / "watchdog.config.json").exists()

    def test_missing_staging_dir_is_a_noop(self, tmp_path: Path) -> None:
        run_root = tmp_path / "run-abc"
        run_root.mkdir()
        persist_unattended_dir(tmp_path / "nope", run_root)  # must not raise
        assert not (run_root / "unattended").exists()


class TestDegradeWhenRevitControlUnconfigured:
    async def test_unattended_enabled_without_revitcontrol_falls_back_attended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """Mirrors the branch in orchestrator.audit(): enabled=True but
        services.revitcontrol is unconfigured -> attended run, no crash, no
        UnattendedSession constructed (assert via a Popen spy that must
        never fire)."""
        spy = MagicMock()
        monkeypatch.setattr("bim_orchestrator.unattended.subprocess.Popen", spy)

        rules_file = tmp_path / "rules.t.yaml"
        rules_file.write_text("scenario: t\nrules: []\n", encoding="utf-8")
        profile_path = tmp_path / "audit.t.yaml"
        profile_path.write_text(
            "\n".join(
                [
                    "name: t",
                    "rules:",
                    f"  - {rules_file.as_posix()}",
                    "run:",
                    "  mode: check",
                    "unattended:",
                    "  enabled: true",
                    "  revit_exe: C:/Revit/Revit.exe",
                    "  model_path: C:/models/sample.rvt",
                    "  revit_version: 2026",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        async def fake_check(rules_path, autonomy_path, findings_out, *, geometry_seed=None, on_folder=None, **kw):
            class _F:
                root = tmp_path / "run-xyz"
            _F.root.mkdir()
            if on_folder is not None:
                on_folder(_F)
            return 0

        monkeypatch.setattr(orchestrator, "check", fake_check)
        # No audit_services.yaml on disk -> AuditServices() with revitcontrol=None.
        # audit() does `from ...audit_profile import load_audit_services` LOCALLY
        # (fresh lookup each call) -> patch the source attribute, not orchestrator's.
        monkeypatch.setattr(
            "bim_orchestrator.policies.audit_profile.load_audit_services",
            lambda path=None: AuditServices(),
        )

        rc = await orchestrator.audit(
            profile_path,
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=3,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0
        spy.assert_not_called()
        err = capsys.readouterr().err
        assert "unattended requested but RevitControl unconfigured" in err


class TestModelUpgradeDialogIsIgnored:
    """U-02 (live 2026-07-30). Revit's upgrade PROGRESS window used to fall
    through to the unknown-dialog action — halt — which writes PAUSED and stops
    the watchdog supervising the rest of a healthy session."""

    def test_rule_exists_and_is_ignore_not_dismiss_or_halt(self) -> None:
        rule = next(
            (r for r in DEFAULT_KNOWN_DIALOGS if r["name"] == "model_upgrade"), None
        )
        assert rule is not None, "the model_upgrade rule was removed"
        # `dismiss` would send the click ladder (button -> No/Close/Cancel/OK ->
        # ESC) at a progress dialog whose only button CANCELS the upgrade;
        # `halt` is what the bug was. Neither is acceptable here.
        assert rule["action"] == "ignore"
        assert rule.get("button", "") == "", "an ignored dialog is never clicked"
        assert rule["title_contains"] == "Model Upgrade"

    def test_rule_precedes_every_text_matching_rule(self) -> None:
        """RevitControl's match_rule returns the FIRST rule that matches, and it
        tests title_contains before text_contains WITHIN a rule but walks rules
        in order. A later text rule cannot shadow this one only if this one is
        reached first."""
        names = [r["name"] for r in DEFAULT_KNOWN_DIALOGS]
        upgrade = names.index("model_upgrade")
        for halting in ("license", "signin", "subscription"):
            assert upgrade < names.index(halting)

    async def test_it_reaches_the_watchdog_config(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        """Pin the WIRE, not just the constant: the rule is only worth anything
        if it lands in the config the watchdog actually reads."""
        session = UnattendedSession(
            services=_services(tmp_path),
            revit_exe="C:/Revit/Revit.exe",
            model_path="C:/models/sample.rvt",
            revit_version=2027,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass
        cfg = json.loads(
            (tmp_path / "staging" / "unattended" / "watchdog.config.json")
            .read_text(encoding="utf-8")
        )
        rule = next(r for r in cfg["known_dialogs"] if r["name"] == "model_upgrade")
        assert rule["action"] == "ignore"


class TestFinalWatchdogStatePersists:
    """U-01 (live 2026-07-30). `persist_unattended_dir` runs from the on_folder
    hook — at run-folder CREATION. Anything the watchdog writes later, PAUSED
    included, missed the copy and died with the staging tempdir."""

    async def test_aexit_copies_state_written_after_the_run_folder_existed(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        staging = tmp_path / "staging"
        run_root = tmp_path / "runs" / "run-abc"
        run_root.mkdir(parents=True)

        session = UnattendedSession(
            services=_services(tmp_path),
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=staging,
        )
        async with session:
            # What the on_folder hook does: copy now, and hand over the folder.
            persist_unattended_dir(staging, run_root)
            session.run_root = run_root
            # ...and only THEN does the watchdog halt. This ordering is the bug.
            (staging / "unattended" / "PAUSED").write_text(
                "modal:Model Upgrade", encoding="utf-8"
            )
            (staging / "unattended" / "events.jsonl").write_text(
                '{"type": "halted"}\n', encoding="utf-8"
            )

        paused = run_root / "unattended" / "PAUSED"
        assert paused.exists(), "the halt never reached the run folder"
        assert paused.read_text(encoding="utf-8") == "modal:Model Upgrade"
        assert (run_root / "unattended" / "events.jsonl").read_text(
            encoding="utf-8"
        ) == '{"type": "halted"}\n'

    async def test_no_run_folder_is_not_an_error(
        self, tmp_path: Path, mock_subprocess
    ) -> None:
        """An aborted run creates no folder; exiting must still be clean."""
        session = UnattendedSession(
            services=_services(tmp_path),
            revit_exe="C:/Revit/Revit.exe",
            model_path="",
            revit_version=2026,
            staging_dir=tmp_path / "staging",
        )
        async with session:
            pass
        assert session.run_root is None

    def test_orchestrator_hands_the_run_folder_over(self) -> None:
        """Pin the WIRE. The re-copy is unreachable unless audit()'s on_folder
        hook sets `run_root` — and a hook that sets nothing breaks no test of
        the session itself (the lesson from L2-05/L2-12/#37, three times over).
        """
        import inspect

        src = inspect.getsource(orchestrator.audit)
        assert "session_cm.run_root = folder.root" in src
        assert "isinstance(session_cm, UnattendedSession)" in src
