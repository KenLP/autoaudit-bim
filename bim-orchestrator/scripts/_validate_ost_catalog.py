"""Quick validation of config/ost_catalog.yaml.

Not a real test (those land in Task #3) — just confirms the file parses,
keys/OSTs are unique, and reports the discipline split + AECDM-null count.
"""
from collections import Counter
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parents[1] / "config" / "ost_catalog.yaml"

data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
cats = data["categories"]

print(f"version: {data['version']}")
print(f"total entries: {len(cats)}")

by_disc = Counter(e["discipline"] for e in cats)
print(f"by discipline: {dict(by_disc)}")

aecdm_null = sum(1 for e in cats if e["aecdm_label"] is None)
print(f"aecdm_label null (skipped for Forma backend): {aecdm_null}")

keys = [e["key"] for e in cats]
key_dups = [k for k in set(keys) if keys.count(k) > 1]
print(f"duplicate keys: {key_dups or 'none'}")

osts = [e["ost"] for e in cats]
ost_dups = [o for o in set(osts) if osts.count(o) > 1]
print(f"duplicate OSTs: {ost_dups or 'none'}")

displays = [e["display"] for e in cats]
display_dups = [d for d in set(displays) if displays.count(d) > 1]
print(f"duplicate displays: {display_dups or 'none'}")

# Aliases that collide across entries — would make resolve() ambiguous
alias_owners: dict[str, list[str]] = {}
for e in cats:
    for a in e.get("aliases", []) or []:
        alias_owners.setdefault(a.lower(), []).append(e["key"])
alias_collisions = {a: owners for a, owners in alias_owners.items() if len(owners) > 1}
print(f"alias collisions: {alias_collisions or 'none'}")
