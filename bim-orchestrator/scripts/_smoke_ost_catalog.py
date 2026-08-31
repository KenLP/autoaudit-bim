"""Smoke check for OSTCatalog — one-shot validation; not a real test.

The actual test suite lands in Task #3 (tests/test_ost_catalog.py).
"""
from bim_orchestrator.policies.ost_catalog import OSTCatalog


def _show(label, entry):
    return entry.key if entry else None


def main() -> None:
    c = OSTCatalog.load()
    print(f"entries: {len(c.entries)}")
    print(
        "by_discipline: "
        f"arch={len(c.by_discipline('architecture'))} "
        f"struct={len(c.by_discipline('structure'))} "
        f"mep={len(c.by_discipline('mep'))}"
    )

    # Priority 1 — exact key (case-sensitive)
    print(f"[1] exact key 'walls'           -> {_show('walls', c.find('walls'))}")
    # Priority 2 — exact display (case-sensitive)
    print(f"[2] exact display 'Walls'       -> {_show('Walls', c.find('Walls'))}")
    # Priority 3 — case-insensitive
    print(f"[3] case-insensitive 'WALLS'    -> {_show('WALLS', c.find('WALLS'))}")
    print(f"[3] alias 'Tường'               -> {_show('Tường', c.find('Tường'))}")
    print(f"[3] alias 'Beam'                -> {_show('Beam', c.find('Beam'))}")
    print(f"[3] case-insens alias 'beam'    -> {_show('beam', c.find('beam'))}")
    # Priority 4 — fuzzy match
    print(f"[4] fuzzy 'Stairss' (typo)      -> {_show('Stairss', c.find('Stairss'))}")
    print(f"[4] fuzzy 'Ceilngs' (omit-i,7ch)-> {_show('Ceilngs', c.find('Ceilngs'))}")
    print(f"[4] fuzzy 'Door' (len<5, skip)  -> {_show('Door', c.find('Door'))}")
    # Priority 5 — unknown
    print(f"[5] unknown 'Banana'            -> {c.find('Banana')}")

    # Backend resolve
    print(f"resolve('Walls', revit)         -> {c.resolve('Walls', 'revit')}")
    print(f"resolve('Walls', aecdm)         -> {c.resolve('Walls', 'aecdm')}")
    print(f"resolve('Trusses', revit)       -> {c.resolve('Trusses', 'revit')}")
    print(
        f"resolve('Trusses', aecdm)       -> "
        f"{c.resolve('Trusses', 'aecdm')}   # entry has null aecdm_label"
    )

    # Cache: second identical call should hit cache (no second warn log)
    print(f"resolve('Banana', revit) #1     -> {c.resolve('Banana', 'revit')}")
    print(f"resolve('Banana', revit) #2     -> {c.resolve('Banana', 'revit')}  # cached")


if __name__ == "__main__":
    main()
