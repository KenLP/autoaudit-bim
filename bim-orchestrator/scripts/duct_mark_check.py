r"""Live duct-Mark naming-convention check (v1.4-K3 spike).

Computes the expected Mark for every Duct from its spatial + system context
and compares against the actual Mark. Expected structure:

    "<SpaceName>-<LevelName>-<SystemName>-<NN>"

  • SpaceName  = the MEP Space (OST_MEPSpaces) whose bbox contains the duct
                 centroid. When several space bboxes overlap a centroid, the
                 SMALLEST-volume space wins (tightest fit — bboxes are
                 axis-aligned and large rooms like corridors over-cover).
  • LevelName  = the duct's "Reference Level" parameter.
  • SystemName = the duct's "System Name" parameter.
  • NN         = 01, 02, … sequence within each (Space, Level, System) group,
                 ordered by element id for determinism.

Data path (proves the P1 find_elements win + the space-first mapping the
domain expert asked for):
  1. revit_list_spaces                      → spaces (id, name, level)
  2. revit_get_element_geometry(space_id)   → space bbox      (parallel)
  3. revit_find_elements(OST_DuctCurves,
       fields=[Mark, System Name, Reference Level])           → 1 call
  4. revit_get_element_geometry(duct_id)    → duct centroid   (parallel)

Report-only by default — no model writes. Run against a live Revit with the
RevitMCPServer addin loaded (HTTP-direct):

    $env:REVIT_MCP_USE_HTTP = "true"
    $env:REVIT_MCP_VERSION  = "2027"
    $env:PYTHONIOENCODING   = "utf-8"
    uv run python scripts\duct_mark_check.py --max-ducts 400
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from typing import Any

from bim_orchestrator.mcp_clients.revit import make_revit_client

DUCT_CATEGORY = "OST_DuctCurves"
_FIELDS = ["Mark", "System Name", "Reference Level"]


def _bbox_contains(bbox: dict[str, Any], pt: dict[str, float], pad: float = 0.0) -> bool:
    lo, hi = bbox["min"], bbox["max"]
    return (
        lo["x"] - pad <= pt["x"] <= hi["x"] + pad
        and lo["y"] - pad <= pt["y"] <= hi["y"] + pad
        and lo["z"] - pad <= pt["z"] <= hi["z"] + pad
    )


async def _fetch_geometry(client: Any, eid: int, sem: asyncio.Semaphore) -> dict[str, Any] | None:
    async with sem:
        try:
            return await client.call_data("revit_get_element_geometry", {"id": int(eid)})
        except Exception:
            return None


async def run(max_ducts: int, space_limit: int, concurrency: int) -> int:
    client = make_revit_client()
    async with client:
        doc = await client.call_data("revit_get_document_info")
        print(f"Model: {doc.get('title')}  ({doc.get('displayUnitSystem')})")

        # 1. Spaces + their bounding boxes.
        spaces_raw = await client.call_data("revit_list_spaces", {"limit": space_limit})
        spaces = (spaces_raw or {}).get("spaces", [])
        print(f"Spaces: {len(spaces)}")

        sem = asyncio.Semaphore(concurrency)
        space_geos = await asyncio.gather(
            *(_fetch_geometry(client, s["id"], sem) for s in spaces)
        )
        space_boxes: list[dict[str, Any]] = []
        for s, geo in zip(spaces, space_geos):
            if not geo or "boundingBox" not in geo:
                continue
            space_boxes.append({
                "id": s["id"],
                "name": s.get("name") or f"Space {s['id']}",
                "volume": float(s.get("volume") or 0.0),
                "bbox": geo["boundingBox"],
            })
        # Smallest volume first → first containing match is the tightest fit.
        space_boxes.sort(key=lambda b: b["volume"])
        print(f"Spaces with usable bbox: {len(space_boxes)}")

        # 2. Ducts (bulk params in ONE find_elements call) + centroids.
        ducts = await client.find_elements(DUCT_CATEGORY, fields=_FIELDS, limit=max_ducts)
        print(f"Ducts fetched: {len(ducts)} (cap {max_ducts})")

        duct_geos = await asyncio.gather(
            *(_fetch_geometry(client, d["id"], sem) for d in ducts)
        )

        # 3. Map each duct → containing space; collect context.
        records: list[dict[str, Any]] = []
        unmapped = 0
        for d, geo in zip(ducts, duct_geos):
            flds = d.get("fields") or {}
            actual_mark = flds.get("Mark")
            level = flds.get("Reference Level_display") or flds.get("Reference Level")
            system = flds.get("System Name_display") or flds.get("System Name")
            centroid = (geo or {}).get("centroid")

            space_name = None
            if centroid:
                for box in space_boxes:  # smallest-first
                    if _bbox_contains(box["bbox"], centroid):
                        space_name = box["name"]
                        break
            if space_name is None:
                unmapped += 1
            records.append({
                "id": d["id"],
                "actual": actual_mark,
                "space": space_name,
                "level": level,
                "system": system,
            })

        # 4. Sequence numbering within (space, level, system).
        groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            if r["space"] and r["level"] and r["system"]:
                groups[(r["space"], r["level"], r["system"])].append(r)
        for key, members in groups.items():
            members.sort(key=lambda r: int(r["id"]))
            for i, r in enumerate(members, start=1):
                r["expected"] = f"{r['space']}-{r['level']}-{r['system']}-{i:02d}"

        # 5. Classify.
        ok = missing = mismatch = uncomputable = 0
        examples: list[dict[str, Any]] = []
        for r in records:
            expected = r.get("expected")
            if expected is None:
                r["status"] = "uncomputable"
                uncomputable += 1
            elif not r["actual"] or not str(r["actual"]).strip():
                r["status"] = "missing"
                missing += 1
            elif str(r["actual"]).strip() == expected:
                r["status"] = "ok"
                ok += 1
            else:
                r["status"] = "mismatch"
                mismatch += 1
            if r["status"] in ("missing", "mismatch") and len(examples) < 15:
                examples.append(r)

        # 6. Report.
        print("\n--- Duct Mark compliance ---")
        print(f"  total ducts checked : {len(records)}")
        print(f"  unmapped (no space) : {unmapped}")
        print(f"  COMPLIANT (ok)      : {ok}")
        print(f"  NON-COMPLIANT       : {missing + mismatch}  "
              f"(missing={missing}, mismatch={mismatch})")
        print(f"  uncomputable        : {uncomputable}  (missing space/level/system)")
        print(f"\n  Sequence groups (Space-Level-System): {len(groups)}")

        print("\n  Sample violations (expected Mark to write):")
        for r in examples:
            actual = r["actual"] if r["actual"] else "(none)"
            print(f"    #{r['id']}  [{r['status']:8}]  actual={actual!r}")
            print(f"            expected = {r['expected']!r}")

        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live duct-Mark naming-convention check.")
    ap.add_argument("--max-ducts", type=int, default=400, help="Cap on ducts checked.")
    ap.add_argument("--space-limit", type=int, default=200, help="Cap on spaces loaded.")
    ap.add_argument("--concurrency", type=int, default=8, help="Parallel geometry fetches.")
    args = ap.parse_args()
    return asyncio.run(run(args.max_ducts, args.space_limit, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
