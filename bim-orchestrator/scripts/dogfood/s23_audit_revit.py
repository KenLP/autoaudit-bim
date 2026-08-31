"""S2.3 -- Revit-side audit trail verification.

DOGFOOD_PLAN Stage 2 S2.3 originally asked for Forma's
``meta_verify_audit_chain``, but the Stage 2 helper uses ``--no-forma``
(Snowdon has no linked ACC project). The trust pipeline still produces
an audit trail though -- in the Revit Comments parameter, populated by
the rule's ``remediation.comments_template`` after each Path B write.

This script connects to the live Revit MCP bridge, walks every room,
and reports:

    * count of rooms whose Comments matches the agent's audit pattern
    * the per-Department breakdown of those rooms (cross-checks that
      the writes targeted what we expected)
    * any room that has Department populated but NO audit Comments
      (would indicate a remediation step skipped the comment_template)

Usage:
    uv run python scripts/dogfood/s23_audit_revit.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from bim_orchestrator.mcp_clients.revit import (  # noqa: E402
    RevitEnvelopeError,
    RevitMCPClient,
    RevitMCPConfig,
)


AUDIT_PATTERN = re.compile(r"^Auto-filled Department='([^']+)' -- BEP Sec\.1\.7$")


async def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    config = RevitMCPConfig.from_env()

    async with RevitMCPClient(config) as client:
        rooms = await client.list_rooms()
        total = len(rooms)
        print(f"Found {total} rooms in the active document.\n")

        audit_hits: list[tuple[int, str, str]] = []  # (id, room_name, department)
        dept_filled_no_comment: list[tuple[int, str, str]] = []
        dept_empty: list[tuple[int, str]] = []

        for room in rooms:
            rid = int(room["id"])
            rname = room.get("name") or "?"
            try:
                info = await client.get_element_info(rid)
            except RevitEnvelopeError as exc:
                print(f"  [skip {rid}] {exc.code}: {exc.message}")
                continue
            params = {p["name"]: p.get("value") for p in info.get("parameters", [])}
            dept = (params.get("Department") or "").strip()
            comments = params.get("Comments") or ""

            if not dept:
                dept_empty.append((rid, rname))
                continue

            m = AUDIT_PATTERN.match(comments)
            if m:
                # Sanity: agent's parsed Department should match the room's
                # actual Department field. Mismatch means someone overwrote
                # one of the two after the run.
                audit_dept = m.group(1)
                audit_hits.append((rid, rname, audit_dept))
                if audit_dept != dept:
                    print(
                        f"  [mismatch] room {rid} {rname!r}: "
                        f"audit says {audit_dept!r}, field says {dept!r}"
                    )
            else:
                dept_filled_no_comment.append((rid, rname, dept))

        print("=" * 72)
        print(f"Total rooms                    : {total}")
        print(f"Rooms with audit Comments      : {len(audit_hits)}")
        print(f"Rooms Department-filled w/o audit: {len(dept_filled_no_comment)}")
        print(f"Rooms with empty Department    : {len(dept_empty)}")
        print()

        if audit_hits:
            print("Per-Department breakdown of audited writes:")
            counts = Counter(d for _, _, d in audit_hits)
            for dept, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {dept:<14} {n:>3}")
            print()

        if dept_filled_no_comment:
            print("Rooms with Department populated but no audit Comments")
            print("(pre-existing values from before W6 D5 carry-over, etc.):")
            for rid, name, dept in dept_filled_no_comment[:10]:
                print(f"  {rid:<10} {name:<30} Department={dept!r}")
            if len(dept_filled_no_comment) > 10:
                print(f"  ... and {len(dept_filled_no_comment) - 10} more")
            print()

        if dept_empty:
            print("Rooms with empty Department (should be 0 after S2.2):")
            for rid, name in dept_empty:
                print(f"  {rid:<10} {name}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
