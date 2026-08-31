"""Headless smoke test for the Streamlit Extraction Review tab functions.

Streamlit can't be unit-tested directly, but the validation + save
helpers in app.py are framework-agnostic — we call them via the same
importlib-loaded json_to_yaml module path Streamlit uses at runtime.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import importlib.util

# Load app.py module (without running Streamlit). The Streamlit calls
# at module top-level will fail because there's no session — wrap the
# import in a way that intercepts those... actually we just bypass and
# import the json_to_yaml directly to test the underlying functions.
jty_path = REPO / "extraction-skills" / "scripts" / "json_to_yaml.py"
spec = importlib.util.spec_from_file_location("_jty", str(jty_path))
jty = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jty)

# Test 1: parse buggy CBI JSON, expect 1 warning (fragmentation)
cbi_path = REPO / "cbi_family_naming_rules.json"
if cbi_path.exists():
    payload = json.loads(cbi_path.read_text(encoding="utf-8"))
    rulesets = jty._envelope_to_rulesets(payload)
    for rs in rulesets:
        executable, review, invalid, warnings = jty._split_by_status(rs)
        print(f"[{rs.get('scenario')}]")
        print(f"  executable: {len(executable)}")
        print(f"  review:     {len(review)}")
        print(f"  invalid:    {len(invalid)}")
        print(f"  warnings:   {len(warnings)}")
        for w in warnings:
            print(f"    - {w}")
        # Assert fragmentation caught
        assert any(
            "over-fragmentation" in w.lower() for w in warnings
        ), "fragmentation heuristic should have fired"
    print("\n✓ Fragmentation detected as expected")
else:
    print(f"⚠ {cbi_path.name} not present; skipping CBI smoke")

# Test 2: round-trip a clean payload — should generate YAML
clean = {
    "scenario": "smoke_test_clean",
    "target_category": "Rooms",
    "rules": [
        {
            "id": "smoke.dept.required",
            "rule_type": "parameter_completeness",
            "category": "Rooms",
            "parameter": "Department",
            "requirement": "present_and_nonempty",
            "severity_tag": "missing_required_param",
            "description": "Dept required.",
            "fixability": "auto",
            "autofill": {"strategy": "none"},
            "extraction_meta": {
                "confidence": 0.95,
                "source_text": "Every room must have Department.",
                "source_location": "Test §1",
                "execution_status": "executable",
            },
        }
    ],
}
catalog = jty.OSTCatalog.load()
executable, _r, invalid, warnings = jty._split_by_status(clean)
assert len(executable) == 1
assert len(invalid) == 0
assert len(warnings) == 0
ruleset = jty._build_ruleset(clean, executable, catalog)
yaml_text = jty._ruleset_to_yaml(ruleset)
assert "smoke.dept.required" in yaml_text
assert "Department" in yaml_text
print("\n✓ Clean payload round-trips to YAML")
print(f"  YAML length: {len(yaml_text)} chars")
