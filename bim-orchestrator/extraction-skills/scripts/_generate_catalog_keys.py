"""Regenerate extraction-skills/ost_catalog_keys.txt — flat list for
Claude Desktop attachments. One line per entry: key | display | discipline.
Keep aliases separately in rule_schema.json (too noisy for the txt list).
"""

from pathlib import Path

from bim_orchestrator.policies.ost_catalog import OSTCatalog

SKILL_PACK_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    catalog = OSTCatalog.load()
    lines = [
        "# OST Catalog category keys (v1.3, live-verified against AECDM + Revit 2027)",
        "# Format: key | display | discipline",
        "# Use the `key` value (left column) when emitting category_key in extracted rules.",
        "",
    ]
    by_disc = {"architecture": [], "structure": [], "mep": []}
    for entry in catalog.entries:
        line = f"{entry.key:30s} | {entry.display:30s} | {entry.discipline}"
        by_disc[entry.discipline].append(line)
    for disc in ("architecture", "structure", "mep"):
        lines.append(f"\n## {disc.title()}\n")
        lines.extend(by_disc[disc])
    out_path = SKILL_PACK_ROOT / "ost_catalog_keys.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} — {len(catalog.entries)} entries")


if __name__ == "__main__":
    main()
