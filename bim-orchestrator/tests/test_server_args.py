"""`*_MCP_SERVER_ARGS` splitting must keep a Windows path intact.

Regression guard for the 2026-10-05 finding: POSIX `shlex.split` ate every
backslash, so `FORMA_MCP_SERVER_ARGS=C:\\x\\dist\\index.js` became
`C:xdistindex.js` and `--doctor` / the service's Forma probe reported
"entrypoint not found". The paths below are literal backslash strings and the
OS mode is forced, so these tests mean the same thing on macOS as on Windows.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bim_orchestrator.mcp_clients import _server_args
from bim_orchestrator.mcp_clients import forma as forma_module
from bim_orchestrator.mcp_clients.forma import FormaMCPConfig
from bim_orchestrator.mcp_clients.revit import RevitMCPConfig

WIN_ENTRYPOINT = r"C:\Users\ken\acc-forma-mcp-server\dist\index.js"
WIN_SPACED = r"C:\Program Files\forma mcp\dist\index.js"


@pytest.fixture
def windows_mode(monkeypatch):
    monkeypatch.setattr(_server_args, "_WINDOWS", True)


@pytest.fixture
def posix_mode(monkeypatch):
    monkeypatch.setattr(_server_args, "_WINDOWS", False)


@pytest.fixture
def node_forma_env(monkeypatch):
    """Force the `node <entrypoint>` branch, never the vendored SEA exe."""
    monkeypatch.setattr(forma_module, "_vendor_exe", lambda _name: None)
    monkeypatch.setenv("FORMA_MCP_SERVER_CMD", "node")
    monkeypatch.setenv("FORMA_MCP_SERVER_CWD", r"C:\Users\ken\acc-forma-mcp-server")


class TestWindowsMode:
    def test_forma_config_keeps_backslash_path(self, windows_mode, node_forma_env, monkeypatch):
        monkeypatch.setenv("FORMA_MCP_SERVER_ARGS", WIN_ENTRYPOINT)
        assert FormaMCPConfig.from_env().args == [WIN_ENTRYPOINT]

    def test_revit_config_keeps_backslash_path(self, windows_mode, monkeypatch):
        monkeypatch.setenv("REVIT_MCP_SERVER_ARGS", WIN_ENTRYPOINT)
        monkeypatch.setenv("REVIT_MCP_SERVER_CWD", r"C:\Users\ken\revit-mcp")
        assert RevitMCPConfig.from_env().args == [WIN_ENTRYPOINT]

    def test_quoted_path_with_spaces_is_one_token_without_quotes(self, windows_mode):
        raw = f'"{WIN_SPACED}" --port 3000'
        assert _server_args.split_server_args(raw) == [WIN_SPACED, "--port", "3000"]

    def test_unc_path_keeps_leading_double_backslash(self, windows_mode):
        raw = r"\\fileserver\share\dist\index.js"
        assert _server_args.split_server_args(raw) == [raw]

    def test_trailing_backslash_before_quote_does_not_escape_it(self, windows_mode):
        # Under POSIX rules `\"` escapes the quote and the split raises
        # "No closing quotation"; a Windows directory arg may end in `\`.
        assert _server_args.split_server_args(r'"C:\dist\" --x') == [r"C:\dist" + "\\", "--x"]


class TestPosixModeUnchanged:
    """The macOS setup passes POSIX
    paths; its quoting AND backslash-escaping must behave exactly as before."""

    def test_absolute_posix_path(self, posix_mode):
        raw = "/Users/ken/acc-forma-mcp-server/dist/index.js"
        assert _server_args.split_server_args(raw) == [raw]

    def test_backslash_escaped_space(self, posix_mode):
        raw = r"/Users/ken/forma\ mcp/dist/index.js --port 3000"
        assert _server_args.split_server_args(raw) == [
            "/Users/ken/forma mcp/dist/index.js",
            "--port",
            "3000",
        ]

    def test_single_quoted_path(self, posix_mode):
        raw = "'/Users/ken/forma mcp/dist/index.js'"
        assert _server_args.split_server_args(raw) == ["/Users/ken/forma mcp/dist/index.js"]


@pytest.mark.skipif(os.name != "nt", reason="Windows path resolution")
def test_doubled_backslashes_from_the_quickstart_still_resolve(tmp_path):
    """docs/dogfood/WINDOWS_QUICKSTART.md told users to write `C:\\\\Users\\\\…`
    because the old split halved them. They now reach the OS doubled — which
    Windows collapses — so an existing `.env` keeps working."""
    entrypoint = tmp_path / "index.js"
    entrypoint.write_text("// server")
    doubled = str(entrypoint).replace("\\", "\\\\")
    (arg,) = _server_args.split_server_args(doubled)
    assert Path(arg).is_file()


@pytest.mark.parametrize("mode", [True, False])
def test_empty_is_no_args(monkeypatch, mode):
    monkeypatch.setattr(_server_args, "_WINDOWS", mode)
    assert _server_args.split_server_args("") == []
