"""A console codepage must not be able to fail a run (found 2026-07-29).

Windows hands Python a cp1252 stdout unless `PYTHONIOENCODING` is set, and 19
`print` calls in `orchestrator.py` carry characters cp1252 cannot encode — the
arrow in "Elements → ACC Issues" among them. Printing one raised
`UnicodeEncodeError`, which propagated out of the post-run summary block, was
caught by `run_revit`'s failure handler, and recorded a run that had
**converged** — every artifact already written to disk — as `status: failed`
with exit 1.

Why nothing caught it for months: "set PYTHONIOENCODING=utf-8 in your shell" is
a documented gotcha in CLAUDE.md, so every session and every test run had it
set. A demo machine does not. `scripts/seed-au-demo.ps1` invokes this CLI from
PowerShell with nothing set, which means **the AU runbook's own step 2 failed
on a clean machine** — found while seeding for the backup recording, 33 days
before the internal deadline.

Two layers, because they fail differently:
  * `_make_console_utf8_safe` removes the cause (UTF-8 where the terminal takes
    it, `replace` where it doesn't);
  * `_console_safe` removes the class — a summary describes work that already
    finished, so nothing it does may decide whether that work counts.
"""

from __future__ import annotations

import io

import pytest

from bim_orchestrator.orchestrator import (
    _console_safe,
    _make_console_utf8_safe,
    _print_llm_status,
    _print_run_folder_notice,
)


class _Cp1252Stream(io.TextIOBase):
    """Stands in for a stock Windows console: it simply cannot encode "→"."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.reconfigured: dict[str, object] | None = None

    def write(self, s: str) -> int:
        s.encode("cp1252")  # raises UnicodeEncodeError, exactly like the console
        self.written.append(s)
        return len(s)


class _ReconfigurableStream(_Cp1252Stream):
    def reconfigure(self, *, encoding: str, errors: str) -> None:
        self.reconfigured = {"encoding": encoding, "errors": errors}


class TestConsoleIsMadeSafe:
    def test_stdout_and_stderr_are_both_reconfigured(self, monkeypatch):
        out, err = _ReconfigurableStream(), _ReconfigurableStream()
        monkeypatch.setattr("sys.stdout", out)
        monkeypatch.setattr("sys.stderr", err)
        _make_console_utf8_safe()
        for s in (out, err):
            assert s.reconfigured == {"encoding": "utf-8", "errors": "replace"}

    def test_errors_replace_not_strict(self, monkeypatch):
        """`replace` is the point: a console that cannot render an arrow should
        print '?' and carry on. Strict would only move the crash."""
        out = _ReconfigurableStream()
        monkeypatch.setattr("sys.stdout", out)
        monkeypatch.setattr("sys.stderr", _ReconfigurableStream())
        _make_console_utf8_safe()
        assert out.reconfigured is not None
        assert out.reconfigured["errors"] == "replace"

    def test_a_stream_without_reconfigure_is_left_alone(self, monkeypatch):
        """Pytest's capture objects and odd redirections have no reconfigure —
        setup must not explode on them."""
        monkeypatch.setattr("sys.stdout", _Cp1252Stream())
        monkeypatch.setattr("sys.stderr", _Cp1252Stream())
        _make_console_utf8_safe()  # must not raise

    def test_a_stream_that_refuses_reconfigure_is_survivable(self, monkeypatch):
        class _Stubborn(_Cp1252Stream):
            def reconfigure(self, **_kw):
                raise OSError("not a real tty")

        monkeypatch.setattr("sys.stdout", _Stubborn())
        monkeypatch.setattr("sys.stderr", _Stubborn())
        _make_console_utf8_safe()  # must not raise


class TestSummaryCannotChangeTheVerdict:
    def test_an_unprintable_summary_does_not_propagate(self, monkeypatch):
        """The actual 2026-07-29 failure: print raises, and pre-fix that
        exception reached run_revit's handler and rewrote a converged run."""
        monkeypatch.setattr("sys.stdout", _Cp1252Stream())

        class _Folder:
            root = "D:/runs/run-arrow→here"

        _print_run_folder_notice(_Folder())  # must not raise

    def test_the_failure_is_logged_not_swallowed(self):
        """Degrading silently would trade one invisible problem for another."""
        from structlog.testing import capture_logs

        @_console_safe
        def _boom() -> None:
            raise UnicodeEncodeError("cp1252", "→", 0, 1, "nope")

        with capture_logs() as logs:
            _boom()
        events = [entry["event"] for entry in logs]
        assert "console.summary_failed" in events

    def test_a_working_helper_still_prints(self, capsys):
        """The guard must not turn every summary into a silent no-op."""
        class _Folder:
            root = "D:/runs/run-1"

        _print_run_folder_notice(_Folder())
        assert "run-1" in capsys.readouterr().out

    def test_the_helper_keeps_its_identity(self):
        """functools.wraps — otherwise every helper logs as '_wrapped' and the
        warning above names nothing useful."""
        assert _print_run_folder_notice.__name__ == "_print_run_folder_notice"

    @pytest.mark.parametrize(
        "helper", [_print_run_folder_notice, _print_llm_status]
    )
    def test_helpers_are_guarded(self, helper):
        assert getattr(helper, "__wrapped__", None) is not None, (
            f"{helper.__name__} is not @_console_safe — a print inside it can "
            "still rewrite a finished run's status"
        )


def test_main_actually_calls_the_console_setup():
    """Pins the WIRING, not the function.

    The first mutation for this fix removed `_make_console_utf8_safe()` from
    `main()` and left the function defined — every test above still passed,
    because they call it directly. A setup routine nobody invokes protects
    nothing. Same shape as the renderer-nobody-calls lesson from v1.7-R9.
    """
    import inspect

    from bim_orchestrator import orchestrator

    src = inspect.getsource(orchestrator.main)
    assert "_make_console_utf8_safe()" in src, (
        "main() no longer sets up the console — a cp1252 terminal can fail a run again"
    )
    # ...and it must run BEFORE anything that prints or loads config.
    body = src.split("\n", 1)[1]
    assert body.index("_make_console_utf8_safe()") < body.index("load_dotenv()")
