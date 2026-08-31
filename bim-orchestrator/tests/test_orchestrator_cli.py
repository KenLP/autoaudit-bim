"""H1 — ``--apply-approved`` CLI-level regression.

The flag used to commit pending Path B Revit writes straight from
``outcomes.json``, entirely bypassing the approval trust pipeline (K5 /
110a27f: fingerprint gate + stale re-preview) and carrying two dispatch bugs
(flagged the instance id instead of the resolved write target; read
``action`` from the wrong dict key so rename fixes silently no-op'd as
``set_parameter("Name")``). It has been removed; this test pins that the CLI
now fails loudly with guidance instead of silently doing nothing (or, worse,
executing the legacy bypass again after a revert).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bim_orchestrator import orchestrator


class TestApplyApprovedRemoved:
    def test_apply_approved_exits_2_with_watch_approvals_guidance(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            sys, "argv", ["bim-orchestrator", "--apply-approved", "run-123"]
        )
        rc = orchestrator.main()
        assert rc == 2
        err = capsys.readouterr().err
        assert "watch-approvals" in err

    def test_apply_approved_function_no_longer_exported(self):
        # The legacy bypass function itself must be gone, not just unreachable
        # from the CLI — guards against a future re-wire finding it still there.
        assert not hasattr(orchestrator, "apply_approved")


class TestToolBuildId:
    """Gate 1: audit provenance stamps the exact build, not a static 0.1.0."""

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("BIM_ORCHESTRATOR_BUILD_ID", "release-2026.07.20-abc123")
        assert orchestrator._tool_build_id() == "release-2026.07.20-abc123"

    def test_returns_git_commit_in_a_source_checkout(self, monkeypatch):
        # This repo IS a git checkout, so the id must carry the commit — the
        # whole point of the fix (a bare package version identifies no code).
        monkeypatch.delenv("BIM_ORCHESTRATOR_BUILD_ID", raising=False)
        build_id = orchestrator._tool_build_id()
        assert build_id
        assert "+g" in build_id     # "<pkgver>+g<sha>[.dirty]"

    def test_never_raises_and_returns_nonempty(self, monkeypatch):
        # Degradation contract: even with git unreachable (PATH stripped), a
        # provenance build must never crash a run — it falls back to a version.
        monkeypatch.delenv("BIM_ORCHESTRATOR_BUILD_ID", raising=False)
        monkeypatch.setenv("PATH", "")
        build_id = orchestrator._tool_build_id()
        assert isinstance(build_id, str) and build_id


class TestDemoApprovalsDirSplit:
    """C-1 (review round 7, 2026-08-17): CLI ``--demo`` must not inherit the
    PRODUCTION approvals dir through the CLI default. The flag's default is
    the production dir, and ``_dispatch`` forwards ``args.approvals_dir``
    verbatim to every mode — so without the swap in the demo branch the
    simulated proposals landed among the real ones (the root of both the
    non-hermetic transcript snapshot and the watcher's liveness suicide).
    Same wire class as L-01: both ends were right, the middle wasn't."""

    @staticmethod
    def _spy(recorder):
        async def _stub(*args, **kwargs):
            recorder.update(kwargs)
            return 0
        return _stub

    def test_default_routes_to_the_demo_dir(self, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(orchestrator, "demo", self._spy(seen))
        monkeypatch.setattr(sys, "argv", ["bim-orchestrator", "--demo"])
        assert orchestrator.main() == 0
        assert seen.get("approvals_dir") == orchestrator.DEFAULT_DEMO_APPROVALS_DIR, (
            "--demo with no --approvals-dir must park proposals in the demo "
            "dir, never the production one"
        )

    def test_explicit_dir_is_still_honoured(self, monkeypatch, tmp_path):
        seen: dict = {}
        monkeypatch.setattr(orchestrator, "demo", self._spy(seen))
        monkeypatch.setattr(
            sys, "argv",
            ["bim-orchestrator", "--demo", "--approvals-dir", str(tmp_path)],
        )
        assert orchestrator.main() == 0
        assert seen.get("approvals_dir") == tmp_path


class TestPartialCoverageFlagReachesEveryRunMode:
    """L-01 — ``--fail-on-partial-coverage`` was inert on the two live paths.

    The flag was correct at BOTH ends and broken in the middle: argparse
    declared it, ``_exit_code_for`` honoured it, and ``test_exit_codes.py``
    pinned that consumer directly — but ``_dispatch`` only forwarded it to
    ``check()`` and ``apply()``. ``run()`` and ``run_revit()`` already took
    the keyword and already passed it to ``_exit_code_for``; nothing handed
    it over. So the exact invocation an unattended audit uses
    (``--run-revit --fail-on-partial-coverage``) silently exited 0 on a
    narrowed scope — the failure mode the flag exists to prevent.

    Testing the two ends is what let it ship, so these tests pin the WIRE.
    """

    _MODES = [
        ("--check", "check"),
        ("--apply", "apply"),
        ("--run", "run"),
        ("--run-revit", "run_revit"),
    ]

    @staticmethod
    def _spy(recorder):
        async def _stub(*args, **kwargs):
            recorder.update(kwargs)
            return 0
        return _stub

    @pytest.mark.parametrize("flag,func_name", _MODES)
    def test_flag_is_forwarded(self, monkeypatch, flag, func_name):
        seen: dict = {}
        monkeypatch.setattr(orchestrator, func_name, self._spy(seen))
        monkeypatch.setattr(
            sys, "argv",
            ["bim-orchestrator", flag, "--fail-on-partial-coverage"],
        )
        assert orchestrator.main() == 0
        assert seen.get("fail_on_partial_coverage") is True, (
            f"{func_name}() never received --fail-on-partial-coverage; "
            "the CLI flag is inert on this run mode"
        )

    @pytest.mark.parametrize("flag,func_name", _MODES)
    def test_default_stays_opt_in(self, monkeypatch, flag, func_name):
        # Ken's stance: partial coverage is exit 0 unless someone asks for
        # strictness. Flipping the default would redden every scheduled run
        # on day one and get the warning switched off for good.
        seen: dict = {}
        monkeypatch.setattr(orchestrator, func_name, self._spy(seen))
        monkeypatch.setattr(sys, "argv", ["bim-orchestrator", flag])
        assert orchestrator.main() == 0
        assert seen.get("fail_on_partial_coverage", False) is False

    def test_mode_table_covers_every_function_taking_the_flag(self):
        # The generalisation of the bug: a run mode that accepts the keyword
        # but is missing from _dispatch is invisible to the tests above,
        # because they only check the modes someone remembered to list.
        # This fails the day a new mode declares the parameter.
        import inspect

        declares = {
            name
            for name, obj in vars(orchestrator).items()
            if inspect.iscoroutinefunction(obj)
            and "fail_on_partial_coverage" in inspect.signature(obj).parameters
        }
        assert declares == {name for _, name in self._MODES}, (
            "a run mode accepts --fail-on-partial-coverage but is not covered "
            "here — add it to _MODES and make sure _dispatch forwards the flag"
        )


class TestGroundingFlagsReachEveryEligibleMode:
    """P1-CLI-01 — `--run --bep-fixture` parsed, exited 0, and ingested nothing.

    Same family as L-01, and it slipped through the very net L-01 installed:
    that table covered ONE flag (`--fail-on-partial-coverage`) across the run
    modes, not the flags that share the same wire. `run_revit` forwarded
    `bep_fixture`; `run` had no such parameter at all, so argparse accepted it,
    `_build_grounding` knew how to use it, and nothing carried it between them.

    This table is per (flag, mode) instead of per mode, and asserts the VALUE
    arrives rather than just the key — a `None` forwarded under the right name
    is the same silent no-op wearing a disguise.
    """

    _MODES = [("--run", "run"), ("--run-revit", "run_revit")]
    # (cli flag, argv tail, kwarg name, expected value)
    _FLAGS = [
        ("--bep-fixture", [], "use_bep_fixture", True),
        ("--bep-pdf", ["spec.pdf"], "bep_pdf", Path("spec.pdf")),
        ("--vector-store-dir", ["vs"], "vector_store_dir", Path("vs")),
        ("--fail-on-partial-coverage", [], "fail_on_partial_coverage", True),
    ]

    @staticmethod
    def _spy(recorder):
        async def _stub(*args, **kwargs):
            recorder.update(kwargs)
            return 0
        return _stub

    @pytest.mark.parametrize("mode_flag,func_name", _MODES)
    @pytest.mark.parametrize("flag,tail,kwarg,expected", _FLAGS)
    def test_flag_value_reaches_the_run_mode(
        self, monkeypatch, mode_flag, func_name, flag, tail, kwarg, expected
    ):
        seen: dict = {}
        monkeypatch.setattr(orchestrator, func_name, self._spy(seen))
        monkeypatch.setattr(
            sys, "argv", ["bim-orchestrator", mode_flag, flag, *tail]
        )
        assert orchestrator.main() == 0
        assert kwarg in seen, (
            f"{func_name}() never received {flag} — the flag is inert on this mode"
        )
        assert seen[kwarg] == expected, (
            f"{func_name}() got {seen[kwarg]!r} for {flag}, expected {expected!r}"
        )

    @pytest.mark.parametrize("mode_flag,func_name", _MODES)
    def test_grounding_flags_default_to_off(self, monkeypatch, mode_flag, func_name):
        # Guard: the defaults must stay off, or every run would start ingesting
        # a fixture nobody asked for.
        seen: dict = {}
        monkeypatch.setattr(orchestrator, func_name, self._spy(seen))
        monkeypatch.setattr(sys, "argv", ["bim-orchestrator", mode_flag])
        assert orchestrator.main() == 0
        assert seen.get("use_bep_fixture", False) is False
        assert seen.get("bep_pdf") is None

    def test_every_mode_taking_a_grounding_flag_is_in_the_table(self):
        # The generalisation of BOTH L-01 and P1-CLI-01: a run mode that
        # accepts one of these keywords but is missing here is invisible to the
        # tests above. Fails the day a new mode declares one.
        import inspect

        for _flag, _tail, kwarg, _expected in self._FLAGS:
            declares = {
                name
                for name, obj in vars(orchestrator).items()
                if inspect.iscoroutinefunction(obj)
                and kwarg in inspect.signature(obj).parameters
            }
            covered = {name for _, name in self._MODES}
            # `check`/`apply` legitimately take only the coverage flag.
            assert declares - covered <= {"check", "apply", "audit"}, (
                f"run mode(s) {declares - covered - {'check', 'apply', 'audit'}} "
                f"accept {kwarg} but are not covered here"
            )


class TestFormaExeIntegrity:
    """`--doctor` must answer "is this still the verified binary", not merely
    "is a file there". The exe is unsigned and will hold ACC credentials.

    A version probe is not an option: forma-mcp.exe exits with "Invalid
    environment configuration" unless APS credentials are set, and --doctor has
    to work on a machine that has none.
    """

    def _exe(self, tmp_path, body: bytes = b"MZ-not-really-an-exe"):
        exe = tmp_path / "forma-mcp.exe"
        exe.write_bytes(body)
        return exe

    def test_matching_sidecar_passes(self, tmp_path):
        import hashlib

        exe = self._exe(tmp_path)
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        (tmp_path / "forma-mcp.exe.sha256").write_text(digest, encoding="ascii")

        ok, detail = orchestrator._forma_exe_integrity(exe)
        assert ok is True
        assert digest[:16] in detail

    def test_missing_sidecar_does_not_pass_quietly(self, tmp_path):
        """A check that cannot tell must not read as a check that passed."""
        exe = self._exe(tmp_path)
        ok, detail = orchestrator._forma_exe_integrity(exe)
        assert ok is False
        assert "sidecar" in detail.lower()
        assert "fetch-forma-mcp" in detail

    def test_changed_binary_is_caught(self, tmp_path):
        import hashlib

        exe = self._exe(tmp_path)
        stale = hashlib.sha256(b"the file that was verified").hexdigest()
        (tmp_path / "forma-mcp.exe.sha256").write_text(stale, encoding="ascii")

        ok, detail = orchestrator._forma_exe_integrity(exe)
        assert ok is False
        assert "CHANGED" in detail
        assert "re-fetch" in detail

    def test_sidecar_whitespace_and_case_tolerated(self, tmp_path):
        """The sidecar is written by PowerShell; do not fail on a stray CRLF."""
        import hashlib

        exe = self._exe(tmp_path)
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        (tmp_path / "forma-mcp.exe.sha256").write_text(
            f"  {digest.upper()}\r\n", encoding="ascii"
        )

        ok, _ = orchestrator._forma_exe_integrity(exe)
        assert ok is True
