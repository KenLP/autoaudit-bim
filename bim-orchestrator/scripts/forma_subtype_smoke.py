"""Smoke: which project_id form does Forma's issues_list_types accept?

Diagnoses the Demo-2 404 by calling list_issue_subtypes against the ACC/DM
project (b.<guid>) vs the AECDM workspace urn. Run from bim-orchestrator/:

    $env:PYTHONIOENCODING="utf-8"
    uv run python scripts/forma_subtype_smoke.py

Needs vendor/forma-mcp creds + FORMA_MCP_SERVER_* (source) configured in .env.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig  # noqa: E402

_DM = os.environ.get("DEMO_PROJECT_ID", "")              # expected: b.<guid>
_AECDM = os.environ.get("DEMO_AECDM_PROJECT_ID", "")     # urn:adsk.workspace:...
_BARE = _DM[2:] if _DM.startswith("b.") else _DM

CANDIDATES = {
    "DEMO_PROJECT_ID (ACC/DM, b.<guid>)": _DM,
    "ACC/DM bare <guid>": _BARE,
    "DEMO_AECDM_PROJECT_ID (urn)": _AECDM,
}


async def main() -> None:
    cfg = FormaMCPConfig.from_env()
    print(f"Forma config: command={cfg.command!r} args={cfg.args!r} cwd={cfg.cwd!r}\n")
    async with FormaMCPClient(cfg) as c:
        for label, pid in CANDIDATES.items():
            if not pid:
                print(f"SKIP {label}: (not set)")
                continue
            try:
                subs = await c.list_issue_subtypes(pid)
                active = sum(1 for s in subs if s.get("is_active", True))
                print(f"OK   {label}\n       pid={pid}\n       -> {len(subs)} subtypes, {active} active")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).replace("\n", " ")[:160]
                print(f"FAIL {label}\n       pid={pid}\n       -> {type(exc).__name__}: {msg}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
