"""Create ONE approve-gated proposal issue to eyeball its description (v1.4-K5.1).

This exercises the *real* DesignAgent code path (`_build_proposal_description`
→ `_create_proposal_issue` → Forma `issues_create` with the dry-run→token
guardrail) using a tiny hand-built set of parked Path B fixes — no Revit, no
model query, no watcher. The point is purely to verify the issue body now
states: the RULE (once), its requirement + expected format, the parameter, and
``old → new`` per element.

Run (from bim-orchestrator/, Forma creds in vendor/forma-mcp/.env):

    $env:PYTHONIOENCODING = "utf-8"
    $env:DEMO_PROJECT_ID  = "<acc project id>"     # same project as issue #88
    uv run python scripts/proposal_issue_smoke.py

It prints the rendered description locally first, then creates the issue and
prints its display id. Set status manually if you want to apply — this script
does NOT run the watcher.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.state import ProposedFix

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "rules.fire_rating_format.yaml"
AUTONOMY = ROOT / "config" / "autonomy.yaml"

# A small, representative slice of normalize fixes (Fire Rating is a TYPE param,
# so element_id == family type id; old → new is the format canonicalisation).
SAMPLE = [
    {"type_id": 2162724, "old": "180 MIN", "new": "3-hour"},
    {"type_id": 2176073, "old": "90 MIN", "new": "1.5-hour"},
    {"type_id": 2173105, "old": "2 HR", "new": "2-hour"},
]
RULE_ID = "doors.fire_rating.format"
PARAM = "Fire Rating"


def _fixes() -> list[ProposedFix]:
    out: list[ProposedFix] = []
    for s in SAMPLE:
        out.append(
            ProposedFix(
                finding_id=f"{RULE_ID}::{s['type_id']}",
                element_id=str(s["type_id"]),
                parameter=PARAM,
                new_value=s["new"],
                autonomy="approve",
                approval_token=None,
                preview={
                    "write_eid": s["type_id"],
                    "old_value": s["old"],
                    "rule_id": RULE_ID,
                },
                executed=False,
            )
        )
    return out


async def main() -> int:
    load_dotenv()  # pull DEMO_PROJECT_ID / DEMO_ISSUE_SUBTYPE_ID from .env
    project_id = os.environ.get("DEMO_PROJECT_ID")
    if not project_id:
        print("ERROR: set DEMO_PROJECT_ID (same ACC project as the demo).")
        return 2

    qc = QCAgent(rules_path=RULES, autonomy=AutonomyPolicy.load(AUTONOMY))
    fixes = _fixes()

    config = FormaMCPConfig.from_env()
    with tempfile.TemporaryDirectory() as tmp:
        async with FormaMCPClient(config) as client:
            agent = DesignAgent(
                mcp=client,
                autonomy=AutonomyPolicy.load(AUTONOMY),
                project_id=project_id,
                rules=qc.rules,
                approvals_dir=Path(tmp),
                issue_subtype_id=os.environ.get("DEMO_ISSUE_SUBTYPE_ID"),
            )

            # 1) Show the rendered body locally (no side effect).
            print("=" * 72)
            print("RENDERED DESCRIPTION (preview, not yet sent):\n")
            print(agent._build_proposal_description(fixes))
            print("=" * 72)

            # 2) Create the real proposal issue.
            await agent._create_proposal_issue(fixes)

            # 3) Report the created issue.
            recs = list(Path(tmp).glob("*.json"))
            if not recs:
                print("No issue created (check logs above).")
                return 1
            import json

            rec = json.loads(recs[0].read_text(encoding="utf-8"))
            print(
                f"\nCreated proposal issue: display={rec.get('display_id')} "
                f"id={rec.get('issue_id')} fixes={len(rec.get('fixes', []))}"
            )
            print("Flip its status to 'In progress' in ACC to apply (watcher not run).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
