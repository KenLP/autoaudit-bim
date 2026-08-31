"""Seed and revert a DELIBERATE model change so the nightly delta has a story.

Why this exists
---------------
`delta.md` compares a run against the previous successful run of the same
profile. Three nights running it has read `0 resolved / 0 newly introduced /
327 persistent` — correct, because nobody touched the model. For the AU
recording that is the wrong shape of true: 25 identical nights read as a
system that never finds anything. The delta only tells a story when the model
actually CHANGES between two runs.

So, the night before recording, this makes two changes with a predictable,
countable effect on the NEXT nightly's delta:

  * 5 doors get a valid `Mark` (`D_9NN`). All 149 doors currently fail
    `demo.doors.mark_naming` and none already matches `^D_\\d{3}$`, so five
    findings disappear -> **5 resolved**.
  * 1 room takes another room's `Number`. All 67 room Numbers are currently
    unique, and `unique_in_set` fails EVERY member of a duplicate group
    (`occurrences <= 1`, evaluated per element), so one write creates two
    findings -> **2 newly introduced**.

Both parameters are INSTANCE-bound, which is the whole reason they were
chosen. `Fire Rating` is carried by the Type, so "change five doors" would
have written five Types and moved an unpredictable number of instances;
`Width` is geometry. Neither gives a number you can say out loud on stage.

Design rules this script follows
--------------------------------
1. **Nothing is hardcoded.** Element ids are selected live at seed time.
   Three weeks separate writing this from running it, and the model may be
   edited in between; a script that assumes yesterday's ids fails silently by
   writing to the wrong door.
2. **The journal is the only source of truth for the revert.** Seed records
   `(element_id, parameter, old_value, new_value)` before it writes. Revert
   restores from that record — it never re-derives what the old value "should"
   have been.
3. **Revert refuses to clobber.** If an element's live value is not what seed
   wrote, someone else changed it; that element is skipped and reported, not
   overwritten. Same doctrine as the ApprovalWatcher's stale re-preview: a
   value we did not put there is not ours to replace.
4. **Preview by default.** Both commands need `--apply` to touch anything.
5. **One `batch` = one Revit undo entry**, so Ctrl+Z in Revit is a second
   belt if this script is unavailable.

Usage
-----
    # look first — no writes, prints exactly what would change
    uv run python scripts/au_demo_delta.py seed
    uv run python scripts/au_demo_delta.py seed --apply

    # after recording
    uv run python scripts/au_demo_delta.py revert            # preview
    uv run python scripts/au_demo_delta.py revert --apply

    # what is currently seeded, and does the model still agree?
    uv run python scripts/au_demo_delta.py status

Requires the target model OPEN in Revit with the addin listening. Set
``REVIT_MCP_VERSION`` (2027 for the Snowdon sample) — the default is 2026 and
will fail to connect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from bim_orchestrator.mcp_clients.revit import make_revit_client  # noqa: E402

JOURNAL_DIR = _REPO / "runs" / "au_demo_delta"
MARK_PATTERN = re.compile(r"^D_\d{3}$")

# Marks the seed assigns. In the 900 block on purpose: the convention this
# model uses for real doors is level-based (S10, 102A, 109D), so a D_9NN can
# be recognised at a glance as something this script put there. Verified free
# across the whole door population before writing, never assumed.
SEED_MARKS = ["D_901", "D_902", "D_903", "D_904", "D_905"]

DOORS_CATEGORY = "OST_Doors"


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _latest_journal() -> Path | None:
    if not JOURNAL_DIR.exists():
        return None
    files = sorted(JOURNAL_DIR.glob("seed_*.json"))
    return files[-1] if files else None


def _write_journal(record: dict[str, Any]) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = JOURNAL_DIR / f"seed_{stamp}.json"
    # Same durability posture as approval_store: write beside the target, then
    # replace. A half-written journal is worse than no journal — it would make
    # the revert think it knew the old values.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# model reads
# ---------------------------------------------------------------------------


async def _doors_with_marks(client: Any) -> list[dict[str, Any]]:
    """Every door with its current Mark, as (id, name, mark)."""
    listing = await client.find_elements(DOORS_CATEGORY, fields=["Mark"], limit=1000)
    out = []
    for el in listing:
        eid = el.get("id")
        if eid is None:
            continue
        fields = el.get("fields") or {}
        mark = fields.get("Mark")
        out.append({
            "element_id": int(eid),
            "name": el.get("name") or "",
            "mark": "" if mark is None else str(mark),
        })
    return out


async def _rooms_with_numbers(client: Any) -> list[dict[str, Any]]:
    rooms = await client.list_rooms()
    out = []
    for r in rooms:
        eid = r.get("id")
        num = r.get("number")
        if num is None:
            num = (r.get("parameters") or {}).get("Number")
        if eid is None or num is None or not str(num).strip():
            continue
        out.append({
            "element_id": int(eid),
            "name": r.get("name") or "",
            "number": str(num),
        })
    return out


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def _select_changes(
    doors: list[dict[str, Any]], rooms: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Choose the edits. Returns (changes, problems).

    Deterministic: candidates are sorted by element id and taken from the
    front, so a dry run and the apply that follows it pick the same elements
    even though they are two separate connections.
    """
    problems: list[str] = []
    changes: list[dict[str, Any]] = []

    # --- 5 doors -> a valid Mark (each becomes compliant => "resolved") -----
    taken = {d["mark"] for d in doors if d["mark"]}
    collisions = [m for m in SEED_MARKS if m in taken]
    if collisions:
        problems.append(
            f"seed Marks already present in the model: {collisions} — pick a "
            "different block rather than overwriting a real door's Mark"
        )
    already_valid = [d for d in doors if MARK_PATTERN.fullmatch(d["mark"])]
    if already_valid:
        problems.append(
            f"{len(already_valid)} door(s) ALREADY match ^D_\\d{{3}}$ "
            "(e.g. "
            f"{already_valid[0]['element_id']}) — a previous seed may not have "
            "been reverted; run `status` before seeding again"
        )

    candidates = sorted(
        (d for d in doors if not MARK_PATTERN.fullmatch(d["mark"])),
        key=lambda d: d["element_id"],
    )
    if len(candidates) < len(SEED_MARKS):
        problems.append(
            f"only {len(candidates)} non-compliant door(s) available, need "
            f"{len(SEED_MARKS)}"
        )
    # strict=False is deliberate: `candidates` is the whole non-compliant
    # population and we take only as many as there are seed Marks.
    for door, new_mark in zip(candidates, SEED_MARKS, strict=False):
        changes.append({
            "kind": "resolve",
            "rule_id": "demo.doors.mark_naming",
            "element_id": door["element_id"],
            "element_name": door["name"],
            "parameter": "Mark",
            "old_value": door["mark"],
            "new_value": new_mark,
        })

    # --- 1 room -> a duplicate Number (2 elements fail => "newly introduced")
    by_number: dict[str, list[dict[str, Any]]] = {}
    for r in rooms:
        by_number.setdefault(r["number"], []).append(r)
    existing_dups = {n: v for n, v in by_number.items() if len(v) > 1}
    if existing_dups:
        problems.append(
            f"room Number(s) already duplicated: {sorted(existing_dups)[:5]} — "
            "the 'newly introduced' count will not be 2; investigate before "
            "seeding"
        )

    ordered = sorted(rooms, key=lambda r: r["element_id"])
    if len(ordered) < 2:
        problems.append("need at least two numbered rooms to create a duplicate")
    else:
        donor, target = ordered[0], ordered[1]
        changes.append({
            "kind": "introduce",
            "rule_id": "demo.rooms.number_unique",
            "element_id": target["element_id"],
            "element_name": target["name"],
            "parameter": "Number",
            "old_value": target["number"],
            "new_value": donor["number"],
            # Recorded for the report only — the donor is NOT written to. It
            # still becomes non-compliant, because unique_in_set fails every
            # member of a duplicate group, and that is where the second of the
            # two expected findings comes from.
            "duplicate_of": {
                "element_id": donor["element_id"],
                "element_name": donor["name"],
            },
        })

    return changes, problems


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def _apply(client: Any, writes: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    steps = [
        {
            "command": "set_parameter",
            "params": {
                "id": int(w["element_id"]),
                "parameterName": w["parameter"],
                "value": w["value"],
            },
        }
        for w in writes
    ]
    return await client.batch(steps, dry_run=dry_run, stop_on_error=True)


def _report_batch(env: dict[str, Any]) -> bool:
    """Print the addin's verdict. Returns True when the write committed.

    `committed` ABSENT is not a denial (stdio and older addins never sent the
    field) — the 2026-07-26 trust-envelope decision. Only an explicit False is.
    """
    committed = env.get("committed")
    results = env.get("results")
    print(f"  addin: ok={env.get('ok')} committed={committed}")
    if results is None:
        print("  ⚠️  no per-step results returned — treat as UNCONFIRMED, verify in Revit")
        return committed is not False
    failed = [r for r in results if r.get("ok") is False]
    for r in failed:
        print(f"  ❌ step failed: {r}")
    print(f"  steps: {len(results)} total, {len(failed)} failed")
    return committed is not False and not failed


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _persistence_warning(what: str) -> None:
    """Every write here lands in the OPEN Revit session, not in the file.

    Bit us twice — 2026-08-05 and again 2026-08-06. Both times `revert
    --apply` reported committed=true, `status` confirmed all six restored, and
    a blind full-model scan agreed; then Revit was closed without saving and
    the seed came back, because the restored values had never left the
    session. The verification was true. It just was not durable, and nothing
    in the output said so.

    Deliberately a WARNING and not a save/sync call: syncing someone's cloud
    model is an outward-facing act with a blast radius past this script, and
    the operator may have other unsaved work in the same session. State the
    fact, let the human decide.
    """
    print()
    print(f"  ⚠️  {what} EXIST ONLY IN THE OPEN REVIT SESSION.")
    print("      Revit writes to memory. What persists them depends on the")
    print("      model: a WORKSHARED cloud model needs Sync With Central; a")
    print("      non-workshared one (cloud or local — R27_Snowdon is exactly")
    print("      this: cloud, isWorkshared=false) needs Save, and Sync isn't")
    print("      even offered. Close Revit without doing that and this is")
    print("      silently undone — already happened TWICE here, each time")
    print("      after a verified result. SAVE (or Sync, if workshared) IN")
    print("      REVIT BEFORE CLOSING, then re-run `status` after reopening")
    print("      to confirm it stuck. isModified=false in get_document_info")
    print("      AFTER the writes is the independent check that a save landed.")
    print()


async def cmd_seed(apply: bool) -> int:
    async with make_revit_client() as client:
        doc = await client.get_document_info()
        title = (doc or {}).get("title") or (doc or {}).get("name") or "?"
        print(f"Model: {title}\n")

        doors = await _doors_with_marks(client)
        rooms = await _rooms_with_numbers(client)
        print(f"Read {len(doors)} doors, {len(rooms)} numbered rooms.\n")

        changes, problems = _select_changes(doors, rooms)
        for p in problems:
            print(f"⚠️  {p}")
        if problems:
            print()

        print("Planned changes:")
        for c in changes:
            tag = "resolve   " if c["kind"] == "resolve" else "introduce "
            print(
                f"  [{tag}] {c['element_id']:>9}  {c['parameter']:<7} "
                f"{c['old_value']!r} -> {c['new_value']!r}   ({c['element_name']})"
            )
            if c.get("duplicate_of"):
                d = c["duplicate_of"]
                print(
                    f"              ^ duplicates room {d['element_id']} "
                    f"({d['element_name']}), which also becomes non-compliant"
                )
        n_res = sum(1 for c in changes if c["kind"] == "resolve")
        n_new = sum(2 for c in changes if c["kind"] == "introduce")
        print(f"\nExpected in tomorrow's delta.md: {n_res} resolved, {n_new} newly introduced.")

        blocking = [p for p in problems if "already present" in p or "need" in p]
        if blocking:
            print("\n❌ refusing to seed — resolve the warnings above first.")
            return 2

        if not apply:
            print("\n(preview only — re-run with --apply to write)")
            return 0

        writes = [
            {"element_id": c["element_id"], "parameter": c["parameter"], "value": c["new_value"]}
            for c in changes
        ]

        # The journal is written BEFORE the batch, not after. Learned the hard
        # way on 2026-08-04: the first real `--apply` died on an httpx
        # ReadTimeout inside the batch call, so the journal write below it never
        # ran — and a timeout is the ABSENCE of an answer, not an answer of
        # "nothing happened". The model may or may not have carried six changed
        # values with no record anywhere of what they used to be, which is the
        # one state this script exists to make impossible.
        #
        # This is the L-05 anchor pattern, already in this repo for exactly this
        # failure (`_write_pending_anchor`: an ACC issue nobody recorded is
        # worse than a duplicate). Same shape here: a journal for a write that
        # did not happen is harmless — `revert` sees the live value is not the
        # one seed wrote and skips it — while a write with no journal is a
        # model nobody can put back.
        journal = _write_journal({
            "seeded_at": _now(),
            "model": title,
            "committed": None,
            "confirmed": False,
            "status": "pending",
            "changes": changes,
            "reverted_at": None,
        })
        print(f"\nJournal (written before the write): {journal}")

        print()
        print("  ⚠️  STAY AT THE KEYBOARD. On 2026-08-04 one run of this write")
        print("      timed out because a modal dialog was up in Revit, and a")
        print("      modal blocks Revit's main thread — every addin command,")
        print("      even ping, hangs until a human clicks it. Two later runs")
        print("      of the SAME write had no dialog at all, so the cause was")
        print("      never established; do not assume it will or will not")
        print("      happen. A TIMEOUT IS NOT A FAILURE: click the dialog, then")
        print("      run `status` — it says whether the write landed.")
        print()

        print("Writing (one batch = one undo entry)...")
        try:
            env = await _apply(client, writes, dry_run=False)
        except Exception as exc:
            print(f"\n❌ the batch call failed: {exc.__class__.__name__}: {exc}")
            print("   This does NOT mean the write was rejected — a transport")
            print("   error tells you nothing about what Revit did. The journal")
            print("   above is complete, so once the addin answers again:")
            print("     ... au_demo_delta.py status         # did it land?")
            print("     ... au_demo_delta.py revert --apply # if it did")
            record = json.loads(journal.read_text(encoding="utf-8"))
            record["status"] = "unconfirmed"
            record["transport_error"] = f"{exc.__class__.__name__}: {exc}"
            tmp = journal.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, journal)
            return 1

        ok = _report_batch(env)
        record = json.loads(journal.read_text(encoding="utf-8"))
        record["committed"] = env.get("committed")
        record["confirmed"] = ok
        record["status"] = "applied" if ok else "unconfirmed"
        tmp = journal.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, journal)

        if not ok:
            print("⚠️  the write was NOT confirmed — check Revit before assuming it landed.")
            return 1
        _persistence_warning("The seeded values")
        print("Next: let tonight's nightly run, then read the new run's delta.md.")
        return 0


async def _current_values(client: Any, changes: list[dict[str, Any]]) -> dict[int, str]:
    """Live value per seeded element, keyed by element id."""
    out: dict[int, str] = {}
    for c in changes:
        info = await client.get_element_info(int(c["element_id"]))
        params = (info or {}).get("parameters") or []
        found = ""
        for p in params:
            if p.get("name") == c["parameter"]:
                v = p.get("valueString")
                if v is None:
                    v = p.get("value")
                found = "" if v is None else str(v)
                break
        out[int(c["element_id"])] = found
    return out


async def cmd_revert(apply: bool, journal_path: Path | None) -> int:
    path = journal_path or _latest_journal()
    if path is None:
        print("No seed journal found — nothing to revert.")
        return 1
    record = json.loads(path.read_text(encoding="utf-8"))
    print(f"Journal: {path}")
    print(f"  seeded_at   : {record.get('seeded_at')}")
    print(f"  model       : {record.get('model')}")
    print(f"  reverted_at : {record.get('reverted_at')}\n")
    if record.get("reverted_at"):
        print("⚠️  this journal is already marked reverted — continuing would "
              "re-write values that are already restored. Check `status` first.")

    changes = record["changes"]
    async with make_revit_client() as client:
        live = await _current_values(client, changes)

        restorable, drifted = [], []
        for c in changes:
            eid = int(c["element_id"])
            now = live.get(eid, "")
            if now == str(c["new_value"]):
                restorable.append(c)
            else:
                drifted.append((c, now))

        for c in restorable:
            print(
                f"  [restore] {c['element_id']:>9}  {c['parameter']:<7} "
                f"{c['new_value']!r} -> {c['old_value']!r}"
            )
        for c, now in drifted:
            print(
                f"  [SKIP   ] {c['element_id']:>9}  {c['parameter']:<7} "
                f"live value is {now!r}, not the {c['new_value']!r} this script "
                f"wrote — someone else changed it, so it is not ours to replace"
            )

        if not restorable:
            print("\nNothing to restore.")
            return 0 if not drifted else 1
        if not apply:
            print("\n(preview only — re-run with --apply to write)")
            return 0

        writes = [
            {"element_id": c["element_id"], "parameter": c["parameter"], "value": c["old_value"]}
            for c in restorable
        ]
        print("\nRestoring (one batch = one undo entry)...")
        env = await _apply(client, writes, dry_run=False)
        ok = _report_batch(env)

        if ok:
            record["reverted_at"] = _now()
            record["reverted_count"] = len(restorable)
            record["skipped_drifted"] = [c["element_id"] for c, _ in drifted]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            print(f"\nJournal updated: reverted {len(restorable)}, skipped {len(drifted)}.")
            _persistence_warning("The restored values")
        else:
            print("\n⚠️  restore NOT confirmed — journal left unmarked so it can be retried.")
            return 1
        return 0 if not drifted else 1


async def cmd_status(journal_path: Path | None) -> int:
    path = journal_path or _latest_journal()
    if path is None:
        print("No seed journal — the model carries no seeded change from this script.")
        return 0
    record = json.loads(path.read_text(encoding="utf-8"))
    print(f"Journal: {path}")
    print(f"  seeded_at   : {record.get('seeded_at')}")
    print(f"  reverted_at : {record.get('reverted_at') or '(not reverted)'}\n")
    async with make_revit_client() as client:
        live = await _current_values(client, record["changes"])
    for c in record["changes"]:
        eid = int(c["element_id"])
        now = live.get(eid, "")
        if now == str(c["new_value"]):
            state = "SEEDED   (still carrying the demo value)"
        elif now == str(c["old_value"]):
            state = "reverted (original value in place)"
        else:
            state = f"DRIFTED  (live {now!r} matches neither)"
        print(f"  {eid:>9}  {c['parameter']:<7} {state}")
    print("\n  (This reads the OPEN session. A value shown as reverted is not")
    print("   durable until Revit has been synced or saved — see the warning")
    print("   `seed` and `revert` print.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="make the deliberate change (preview unless --apply)")
    p_seed.add_argument("--apply", action="store_true", help="actually write to the model")

    p_rev = sub.add_parser("revert", help="restore the seeded values from the journal")
    p_rev.add_argument("--apply", action="store_true", help="actually write to the model")
    p_rev.add_argument("--journal", type=Path, default=None, help="journal file (default: newest)")

    p_st = sub.add_parser("status", help="is the seed currently in the model?")
    p_st.add_argument("--journal", type=Path, default=None, help="journal file (default: newest)")

    args = ap.parse_args()
    if args.command == "seed":
        return asyncio.run(cmd_seed(args.apply))
    if args.command == "revert":
        return asyncio.run(cmd_revert(args.apply, args.journal))
    return asyncio.run(cmd_status(args.journal))


if __name__ == "__main__":
    raise SystemExit(main())
