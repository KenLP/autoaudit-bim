#!/usr/bin/env python
"""Generate a parameter catalog by introspecting a live Revit model.

This is the reproducible source for ``config/param_catalog.<version>.yaml`` — the
PARAMETER-layer sibling of ``config/ost_catalog.yaml`` (the CATEGORY layer). It
does NOT hand-author the catalog; it dumps what Revit actually reports, so the map
can't rot relative to the model.

Method (tier: mcp-probe)
------------------------
For each requested built-in category it samples a few instances via
``revit_get_element_info`` (→ instance parameters) and each instance's type
(→ type parameters), then records per parameter:

  * name      — display name (Revit)            * binding   — instance | type
  * storage   — String/Integer/Double/...       * writable  — not isReadOnly
  * dimension — inferred from the valueString    * sample    — the valueString seen

The displayed unit (ft vs mm) is NOT recorded — only the DIMENSION (length/area/…),
which is stable across unit systems. A future ``tier: api-dump`` add-in can add the
BuiltInParameter enum + ForgeTypeId spec; the key stays the display name.

SCOPE: system-family categories only (their params are Revit-defined + stable).
Loadable component families (Doors/Windows/Furniture) carry family-authored params
that vary file-to-file — keep them on the ``bound_parameter``/``custom_param`` path.

Usage
-----
    # needs a live Revit addin reachable (HTTP-direct or MCP stdio); pick via env
    REVIT_MCP_USE_HTTP=true REVIT_MCP_VERSION=2027 \
        uv run python scripts/dump_param_catalog.py --out config/param_catalog.2027.yaml

    uv run python scripts/dump_param_catalog.py --category OST_Walls --samples 8 --all-params
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any

import yaml

from bim_orchestrator.mcp_clients.revit import make_revit_client

# System-family categories whose built-in params are stable enough to catalog.
DEFAULT_CATEGORIES: list[str] = [
    "OST_Walls",
    "OST_Floors",
    "OST_Ceilings",
    "OST_Roofs",
    "OST_Rooms",
    "OST_Stairs",
    "OST_StairsRuns",
    "OST_StairsLandings",
    "OST_Ramps",
]

# Curated mode drops graphics / IFC / analysis / phase noise from the dropdown.
# (Run with --all-params to keep everything — the faithful full dump.)
_DENY_EXACT = {
    "Category", "Design Option", "Image", "Type Image", "Structure",
    "Type Id", "Phase Id", "Phase", "Phase Created", "Phase Demolished",
    "Family and Type", "Family", "Type", "Has Association", "Related to Mass",
    "Hosted", "Base is Attached", "Top is Attached", "Cross-Section",
    "Coarse Scale Fill Color", "Coarse Scale Fill Pattern", "Model",
    "Manufacturer", "URL", "Description", "Cost", "Roughness", "Absorptance",
    "Assembly Description", "Computation Height",
}
_DENY_PREFIX = (
    "IFC", "Ifc", "Type IFC", "Type Ifc", "Export to IFC", "Export Type to IFC",
    "Thermal", "Heat", "Calculated", "Specified", "Actual Lighting",
    "Actual Power", "Base Lighting", "Base Power", "Lighting Load",
    "Power Load", "Sensible", "Latent", "Total Heat", "Plenum",
    "Number of People", "Area per Person", "Occupancy Unit",
)


def _is_numeric(text: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+(\.\d+)?", text.strip()))


def _looks_length(text: str) -> bool:
    # imperial feet-inches ("3' - 8\"") or metric mm/cm/m suffix
    if re.search(r"\d'\s*-", text) or re.search(r"\d+\"", text):
        return True
    return bool(re.search(r"\d+(\.\d+)?\s?(mm|cm|m)\b", text))


# Metric MEP shows several length params UNIT-LESS ("Length" = "102" mm), so a
# bare-number Double can't be classified by valueString alone. Fall back to the
# param NAME for these length-ish tokens. (Imperial models don't need this — they
# carry feet/inch marks — but it makes the dump metric-robust.)
_LENGTH_NAME_TOKENS = (
    "length", "elevation", "diameter", "width", "height", "depth",
    "thickness", "offset", "radius", "perimeter", "clearance", "headroom",
)


def infer_dimension(storage: str, value_string: str | None, name: str = "") -> str:
    """Best-effort dimension from storage type + a sample value + (fallback) name."""
    s = (value_string or "").strip()
    if storage == "String":
        return "text"
    if storage == "ElementId":
        return "reference"
    if storage == "None":
        return "none"
    if storage == "Integer":
        if s in ("Yes", "No"):
            return "yesno"
        return "number" if _is_numeric(s) else "option"
    if storage == "Double":
        if s.endswith("%"):
            return "percent"
        if "SF" in s or "m²" in s or s.endswith(" SM"):
            return "area"
        if "CF" in s or "m³" in s or s.endswith(" CM"):
            return "volume"
        if "°" in s:
            return "angle"
        if _looks_length(s):
            return "length"
        # Bare number (often metric, unit-less) → name-based length fallback.
        low = name.lower()
        if any(tok in low for tok in _LENGTH_NAME_TOKENS):
            return "length"
        return "number"
    return "other"


def _keep(name: str, *, all_params: bool) -> bool:
    if all_params:
        return True
    if name in _DENY_EXACT:
        return False
    return not name.startswith(_DENY_PREFIX)


def _param_record(raw: dict[str, Any], binding: str) -> dict[str, Any]:
    name = raw.get("name", "")
    storage = raw.get("storageType", "")
    sample = raw.get("valueString")
    rec: dict[str, Any] = {
        "name": name,
        "storage": str(storage).lower() if storage else "none",
        "binding": binding,
        "writable": not bool(raw.get("isReadOnly")),
        "dimension": infer_dimension(storage, sample, name),
    }
    if sample not in (None, ""):
        rec["sample"] = sample
    if name in ("Family Name", "Type Name"):
        rec["rename_only"] = True
    return rec


async def _collect_category(client: Any, ost: str, *, samples: int, all_params: bool) -> dict[str, Any]:
    elements = await client.list_elements(ost, limit=samples, only_instances=True)
    by_name: dict[str, dict[str, Any]] = {}
    type_ids: set[int] = set()
    has_type = False

    for el in elements:
        info = await client.get_element_info(int(el["id"]))
        for raw in info.get("parameters", []):
            nm = raw.get("name")
            if nm and nm not in by_name and _keep(nm, all_params=all_params):
                by_name[nm] = _param_record(raw, "instance")
        tid = info.get("typeId")
        if isinstance(tid, int) and tid > 0:
            type_ids.add(tid)
            has_type = True

    for tid in type_ids:
        info = await client.get_element_info(tid)
        for raw in info.get("parameters", []):
            nm = raw.get("name")
            if nm and nm not in by_name and _keep(nm, all_params=all_params):
                by_name[nm] = _param_record(raw, "type")

    key = ost.removeprefix("OST_").lower()
    return {
        "key": key,
        "ost": ost,
        "family_kind": "system",
        "has_type": has_type,
        "params": list(by_name.values()),
    }


async def _run(categories: list[str], *, samples: int, all_params: bool, out: str | None) -> int:
    async with make_revit_client() as client:  # type: ignore[attr-defined]
        doc = await client.get_document_info()
        ver = await client.get_version()
        revit_version = str(ver.get("version") or ver.get("versionNumber") or "")
        cats = []
        for ost in categories:
            try:
                cats.append(await _collect_category(client, ost, samples=samples, all_params=all_params))
            except Exception as exc:  # pragma: no cover - live-tool defensive
                print(f"!! {ost}: {exc}", file=sys.stderr)

    catalog = {
        "version": 1,
        "revit_version": revit_version,
        "provenance_tier": "mcp-probe",
        "source_model": doc.get("title", ""),
        "categories": cats,
    }
    text = yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True, width=120)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {out}  ({sum(len(c['params']) for c in cats)} params, {len(cats)} categories)")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dump a Revit parameter catalog (mcp-probe tier).")
    ap.add_argument("--category", action="append", dest="categories", help="OST_* (repeatable). Default: system families.")
    ap.add_argument("--samples", type=int, default=5, help="Instances to sample per category (union their params).")
    ap.add_argument("--all-params", action="store_true", help="Keep every param (skip the curation denylist).")
    ap.add_argument("--out", help="Write YAML here (default: stdout).")
    args = ap.parse_args(argv)
    cats = args.categories or DEFAULT_CATEGORIES
    return asyncio.run(_run(cats, samples=args.samples, all_params=args.all_params, out=args.out))


if __name__ == "__main__":
    raise SystemExit(main())
