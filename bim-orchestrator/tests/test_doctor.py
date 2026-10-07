"""M2-C — ``doctor_checks()`` library extraction (P3-5 refactor): the CLI
``--doctor`` and the service's ``GET /api/settings/doctor`` now share ONE
checklist builder instead of the service re-deriving its own check logic.
"""

from __future__ import annotations

from bim_orchestrator.orchestrator import doctor, doctor_checks


def test_doctor_checks_shape() -> None:
    rows = doctor_checks()
    assert rows
    names = set()
    for row in rows:
        assert set(row) == {"name", "status", "detail"}
        assert row["status"] in {"pass", "warn", "fail"}
        names.add(row["name"])
    assert "python >= 3.12" in names
    assert "runs/ writable" in names


def test_doctor_checks_only_required_checks_can_fail() -> None:
    """Non-required checks degrade to warn, never fail -- a clean dev box
    with no forma-mcp.exe / no Revit / no satellites must still doctor()
    exit 0 (only "python >= 3.12" and "runs/ writable" are REQUIRED)."""
    required_names = {"python >= 3.12", "runs/ writable"}
    for row in doctor_checks():
        if row["status"] == "fail":
            assert row["name"] in required_names


def test_doctor_cli_prints_table_and_exits_0(capsys) -> None:
    code = doctor()
    out = capsys.readouterr().out
    assert code == 0
    assert "Check" in out and "Status" in out
    assert "PASS" in out  # uppercased status column, e.g. "python >= 3.12 ... PASS"
    assert "doctor: all required checks passed." in out


class TestFormaLaunchableRow:
    """The Forma row must answer "can this start", not "is there a .exe".

    Regression guard for the 2026-09-13 macOS finding: `--doctor` warned
    "forma-mcp.exe present — not found" on a box where Forma connected fine
    through the `node dist/index.js` fallback. The row now shares one resolver
    with the UI's Forma dot, so the two can never disagree.
    """

    @staticmethod
    def _row(rows, name):
        return next((r for r in rows if r["name"] == name), None)

    def _clear_forma_env(self, monkeypatch) -> None:
        for key in (
            "FORMA_MCP_SERVER_CMD",
            "FORMA_MCP_SERVER_ARGS",
            "FORMA_MCP_SERVER_CWD",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_node_fallback_passes_without_any_exe(self, tmp_path, monkeypatch) -> None:
        from bim_orchestrator.mcp_clients import forma as forma_module

        node = tmp_path / "node"
        node.write_text("#!/bin/sh\n")
        entrypoint = tmp_path / "dist" / "index.js"
        entrypoint.parent.mkdir()
        entrypoint.write_text("// server")

        self._clear_forma_env(monkeypatch)
        monkeypatch.setattr(forma_module, "_vendor_exe", lambda _name: None)
        monkeypatch.setenv("FORMA_MCP_SERVER_CMD", str(node))
        monkeypatch.setenv("FORMA_MCP_SERVER_ARGS", str(entrypoint))
        monkeypatch.setenv("FORMA_MCP_SERVER_CWD", str(tmp_path))

        row = self._row(doctor_checks(), "forma-mcp launchable")
        assert row is not None and row["status"] == "pass"
        assert str(entrypoint) in row["detail"]

    def test_unresolvable_command_warns(self, tmp_path, monkeypatch) -> None:
        from bim_orchestrator.mcp_clients import forma as forma_module

        self._clear_forma_env(monkeypatch)
        monkeypatch.setattr(forma_module, "_vendor_exe", lambda _name: None)
        monkeypatch.setenv("FORMA_MCP_SERVER_CMD", str(tmp_path / "no-such-node"))
        monkeypatch.setenv("FORMA_MCP_SERVER_ARGS", str(tmp_path / "index.js"))
        monkeypatch.setenv("FORMA_MCP_SERVER_CWD", str(tmp_path))

        row = self._row(doctor_checks(), "forma-mcp launchable")
        # warn, never fail: a box with no Forma set up must still exit 0.
        assert row is not None and row["status"] == "warn"
        assert "not found" in row["detail"]

    def test_integrity_row_stays_scoped_to_the_downloaded_exe(
        self, tmp_path, monkeypatch
    ) -> None:
        """A locally built dist/index.js has no sha256 sidecar — the supply
        chain check must not fire on it and must not invent a failure."""
        from bim_orchestrator.mcp_clients import forma as forma_module

        node = tmp_path / "node"
        node.write_text("#!/bin/sh\n")
        entrypoint = tmp_path / "index.js"
        entrypoint.write_text("// server")

        self._clear_forma_env(monkeypatch)
        monkeypatch.setattr(forma_module, "_vendor_exe", lambda _name: None)
        monkeypatch.setenv("FORMA_MCP_SERVER_CMD", str(node))
        monkeypatch.setenv("FORMA_MCP_SERVER_ARGS", str(entrypoint))
        monkeypatch.setenv("FORMA_MCP_SERVER_CWD", str(tmp_path))

        assert self._row(doctor_checks(), "forma-mcp.exe integrity") is None
