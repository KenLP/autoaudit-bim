"""P3-3 helper — launch Revit (if needed) and wait for the RevitMCP addin.

Runs under RevitControl's OWN python (its venv or a system py3.10), NOT
bim-orchestrator's venv — this process is spawned BY
``bim_orchestrator.unattended.UnattendedSession`` precisely so it can
``sys.path.insert`` RevitControl's ``rigcore`` package without this repo
depending on it (D3/D8 boundary: bim-orchestrator never imports a satellite
tool's library directly, only talks to it out-of-process).

``UnattendedSession`` invokes this helper TWICE, straddling the watchdog
spawn (see ``unattended.py`` module docstring for why): once with
``--phase launch`` (before the watchdog exists — the launch decision must not
race the watchdog's own relaunch tick), once with ``--phase wait`` (after,
so the watchdog is alive to dismiss the Unsigned Add-In modal while this
blocks on readiness). ``--phase all`` runs both in one process for any other
caller.

Usage:
    <revitcontrol_python> unattended_launch.py --rc-dir <dir> --exe <revit.exe>
        --model <path-or-empty> --version <int> --port <int> --token <token-or-empty>
        [--phase launch|wait|all]

Exit code 0 = the requested phase(s) succeeded (for ``wait``/``all``, the
addin answered /health within the timeout); non-zero = launch failed or the
addin never came up.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rc-dir", required=True, help="RevitControl repo directory")
    parser.add_argument("--exe", required=True, help="Path to Revit.exe")
    parser.add_argument("--model", default="", help="Optional .rvt to open")
    parser.add_argument("--version", required=True, help="Revit version, e.g. 2027")
    parser.add_argument("--port", type=int, required=True, help="RevitMCP addin HTTP port")
    parser.add_argument("--token", default="", help="RevitMCP addin auth token (blank if auth disabled)")
    parser.add_argument(
        "--phase",
        choices=["launch", "wait", "all"],
        default="all",
        help=(
            "launch: is_running guard + launch only, exit 0 without waiting. "
            "wait: construct RevitMCP + wait_addin_ready only (skips the "
            "launch decision). all: both, in this one process (default, "
            "back-compat with single-phase callers)."
        ),
    )
    args = parser.parse_args()

    sys.path.insert(0, args.rc_dir)
    from rigcore.mcp_client import RevitMCP
    from rigcore.revit_control import is_running, launch, wait_addin_ready

    if args.phase in ("launch", "all"):
        if not is_running():
            launch(args.exe, args.model)
        if args.phase == "launch":
            return 0

    mcp = RevitMCP(args.port, args.token or None)
    if not wait_addin_ready(mcp):
        print(
            f"unattended_launch: RevitMCP addin not ready within timeout "
            f"(port={args.port})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
