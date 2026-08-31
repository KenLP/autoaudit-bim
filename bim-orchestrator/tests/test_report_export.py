"""Tests for report_export — docx/pdf export of the verification report.

The conversion shells to pandoc; these tests stub pandoc so they're deterministic
and don't require it installed. The key contract: Markdown is canonical, so a
missing pandoc is guidance (not a crash), while a missing report IS an error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bim_orchestrator.report_export import SUPPORTED_FORMATS, export_report


def _md(tmp_path: Path) -> Path:
    p = tmp_path / "verification_report.md"
    p.write_text("# Verification report\n", encoding="utf-8")
    return p


def test_missing_report_is_error(tmp_path):
    out, msg = export_report(tmp_path / "nope.md", "docx")
    assert out is None
    assert "not found" in msg


def test_unsupported_format(tmp_path):
    out, msg = export_report(_md(tmp_path), "rtf")
    assert out is None
    assert "unsupported format" in msg
    assert "docx" in SUPPORTED_FORMATS and "pdf" in SUPPORTED_FORMATS


def test_no_pandoc_returns_guidance_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bim_orchestrator.report_export.shutil.which", lambda name: None
    )
    out, msg = export_report(_md(tmp_path), "docx")
    assert out is None
    assert "pandoc not found" in msg
    assert "skills" in msg  # points at the doc skills fallback


def test_pandoc_success_writes_output(tmp_path, monkeypatch):
    md = _md(tmp_path)
    target = md.with_suffix(".docx")

    def fake_run(cmd, **kwargs):
        # emulate pandoc writing the -o target
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_text("(binary docx)", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, msg = export_report(md, "docx", pandoc="pandoc")
    assert out == target
    assert out.exists()
    assert "wrote" in msg


def test_pandoc_failure_reports_stderr(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="latex engine missing")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, msg = export_report(_md(tmp_path), "pdf", pandoc="pandoc")
    assert out is None
    assert "pandoc failed" in msg
    assert "latex engine missing" in msg


def test_custom_out_path(tmp_path, monkeypatch):
    md = _md(tmp_path)
    custom = tmp_path / "sub" / "report.docx"
    custom.parent.mkdir()

    def fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, _ = export_report(md, "docx", out_path=custom, pandoc="pandoc")
    assert out == custom and custom.exists()


# ── v1.5-R6 (3.3): --pdf-engine=xelatex, timeout, honest fallback ───────────


def test_pdf_conversion_pins_xelatex_engine(tmp_path, monkeypatch):
    md = _md(tmp_path)
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, msg = export_report(md, "pdf", pandoc="pandoc")
    assert out is not None
    assert "--pdf-engine=xelatex" in seen_cmds[0]


def test_docx_conversion_never_gets_pdf_engine_flag(tmp_path, monkeypatch):
    """3.3: 'giữ docx nguyên' — the docx command is untouched by the PDF
    engine change."""
    md = _md(tmp_path)
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    export_report(md, "docx", pandoc="pandoc")
    assert not any("--pdf-engine" in arg for arg in seen_cmds[0])


def test_pdf_run_carries_timeout(tmp_path, monkeypatch):
    md = _md(tmp_path)
    captured_kwargs = {}

    def fake_run(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        Path(cmd[cmd.index("-o") + 1]).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    export_report(md, "pdf", pandoc="pandoc")
    assert captured_kwargs.get("timeout") == 120


def test_pdf_timeout_expired_is_honest_not_a_crash(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=120)

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, msg = export_report(_md(tmp_path), "pdf", pandoc="pandoc")
    assert out is None
    assert "timed out" in msg


def test_missing_xelatex_falls_back_to_default_engine_and_succeeds(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--pdf-engine=xelatex" in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="pandoc: xelatex not found"
            )
        Path(cmd[cmd.index("-o") + 1]).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, msg = export_report(_md(tmp_path), "pdf", pandoc="pandoc")
    assert out is not None
    assert out.exists()
    assert "fallback" in msg
    assert len(calls) == 2  # pinned engine attempt, then the fallback retry
    assert "--pdf-engine=xelatex" not in calls[1]


def test_missing_xelatex_and_fallback_also_fails_reports_both_reasons(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        if "--pdf-engine=xelatex" in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="pandoc: xelatex not found"
            )
        raise subprocess.CalledProcessError(1, cmd, stderr="no pdf engine available")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    out, msg = export_report(_md(tmp_path), "pdf", pandoc="pandoc")
    assert out is None
    assert "xelatex" in msg
    assert "no pdf engine available" in msg


def test_pdf_engine_override_none_disables_the_flag(tmp_path, monkeypatch):
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bim_orchestrator.report_export.subprocess.run", fake_run)
    export_report(_md(tmp_path), "pdf", pandoc="pandoc", pdf_engine=None)
    assert not any("--pdf-engine" in arg for arg in seen_cmds[0])
