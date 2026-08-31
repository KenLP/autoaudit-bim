"""Diagnostic: probe what AECDM returns for doors + replay the QC evaluator.

Reproduces what happens when S1.5's uploaded rule runs against the live
Pacific Continental project's doors. Prints, per door:

    * id (truncated), name
    * every property name + value (so we can spot the canonical "Width" /
      "Family and Type" property name and unit)
    * the `Width` value as seen by QC
    * the `Family and Type` value (the rule's `when_param`)
    * whether `when_pattern` matched
    * the evaluator decision and 4-state bucket

Run:
    uv run python scripts/diag_s15_doors.py --limit 8

This is a throwaway script -- not a unit test. Kept in scripts/ so a
future operator can re-run it if S1.5 surfaces the same suspicion.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig  # noqa: E402
from bim_orchestrator.policies.rules_engine import evaluate  # noqa: E402


RULES_PATH = REPO_ROOT / "config" / "rules.dogfood_s15.uploaded.yaml"


def _prop(props: list[dict[str, Any]], name: str) -> Any:
    for p in props:
        if p.get("name") == name:
            return p.get("value")
    return None


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


async def probe(limit: int) -> None:
    load_dotenv(REPO_ROOT / ".env")
    eg = os.environ.get("DEMO_ELEMENT_GROUP_ID")
    if not eg:
        print("FATAL: DEMO_ELEMENT_GROUP_ID not set in .env", file=sys.stderr)
        sys.exit(2)

    rules_doc = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    rules = rules_doc.get("rules", [])
    if not rules:
        print(f"FATAL: no rules in {RULES_PATH}", file=sys.stderr)
        sys.exit(2)
    rule = rules[0]
    print("=" * 78)
    print("Rule under test:")
    print(f"  id            = {rule['id']}")
    print(f"  parameter     = {rule['parameter']!r}")
    print(f"  requirement   = {rule['requirement']}")
    print(f"  threshold     = {rule.get('threshold')}")
    print(f"  when_param    = {rule.get('when_param')!r}")
    print(f"  when_pattern  = {rule.get('when_pattern')!r}")
    print("=" * 78)

    config = FormaMCPConfig.from_env()
    async with FormaMCPClient(config) as client:
        elements = await client.query_elements(eg, "Doors")
        print(f"\nAECDM returned {len(elements)} Door elements (showing first {limit})\n")

        # 1) Property name survey -- which property names are uniformly
        # present? Useful when the rule's `parameter` name doesn't match.
        all_prop_names: dict[str, int] = {}
        for el in elements:
            for p in el.get("properties", []) or []:
                pname = p.get("name")
                if pname:
                    all_prop_names[pname] = all_prop_names.get(pname, 0) + 1
        # The properties present on >= 90% of doors are likely canonical.
        threshold_count = int(0.9 * len(elements))
        canonical = sorted(
            (n for n, c in all_prop_names.items() if c >= threshold_count),
            key=str.lower,
        )
        print("Properties present on >= 90% of doors:")
        for n in canonical:
            print(f"  - {n!r}  ({all_prop_names[n]} / {len(elements)})")
        print()
        # Flag width-related & family-related properties specifically
        width_props = [n for n in all_prop_names if "width" in n.lower()]
        family_props = [n for n in all_prop_names if "family" in n.lower() or "type" in n.lower()]
        print("Width-flavored property names:", width_props)
        print("Family/Type-flavored property names:", family_props)
        print()

        # 2) Per-door evaluator replay
        print("-" * 78)
        print(f"Per-door evaluation against rule '{rule['id']}':")
        print("-" * 78)
        pat = re.compile(rule["when_pattern"]) if rule.get("when_pattern") else None
        for el in elements[:limit]:
            eid = (el.get("id") or "?")[:20] + "..."
            ename = el.get("name") or "?"
            props = el.get("properties", []) or []
            value = _prop(props, rule["parameter"])
            cond = _prop(props, rule["when_param"]) if rule.get("when_param") else None
            in_scope = bool(pat and isinstance(cond, str) and pat.search(cond))

            passed = evaluate(
                rule["requirement"],
                value,
                threshold=rule.get("threshold"),
                condition_value=cond,
                when_pattern=rule.get("when_pattern"),
            )
            if passed:
                bucket = "compliant"
            elif _is_missing(value):
                bucket = "missing_data"
            else:
                bucket = "non_compliant"

            print(
                f"  {eid:<24}  name={ename!r:<28}  "
                f"Width={value!r:<10}  cond={cond!r:<40}  "
                f"in_scope={in_scope}  -> {bucket}"
            )

        # 3) Aggregate the full 49 against the rule
        print()
        print("-" * 78)
        print(f"Aggregate over all {len(elements)} doors:")
        buckets = {"compliant": 0, "non_compliant": 0, "missing_data": 0}
        in_scope_count = 0
        for el in elements:
            props = el.get("properties", []) or []
            value = _prop(props, rule["parameter"])
            cond = _prop(props, rule["when_param"]) if rule.get("when_param") else None
            if pat and isinstance(cond, str) and pat.search(cond):
                in_scope_count += 1
            passed = evaluate(
                rule["requirement"],
                value,
                threshold=rule.get("threshold"),
                condition_value=cond,
                when_pattern=rule.get("when_pattern"),
            )
            if passed:
                buckets["compliant"] += 1
            elif _is_missing(value):
                buckets["missing_data"] += 1
            else:
                buckets["non_compliant"] += 1
        print(f"  In-scope (matching when_pattern): {in_scope_count}")
        for k, v in buckets.items():
            print(f"  {k:<14} {v}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe S1.5 doors + rule")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    try:
        asyncio.run(probe(args.limit))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
