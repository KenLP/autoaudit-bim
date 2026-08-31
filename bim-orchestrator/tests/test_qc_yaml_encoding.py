"""Regression: QCAgent must load rules YAML as UTF-8, not the OS locale.

On Windows ``open()`` defaults to the cp1252 locale encoding, so any non-ASCII
rule content is mojibake'd at load time:

  * ``unit: "m²"`` (U+00B2) is read back as ``"mÂ²"``. ``revit_units`` then
    can't find the ``("ft²", "m²")`` conversion key, logs ``convert_failed``,
    and returns the RAW (feet²) value — so area/volume ``numeric_compare`` rules
    silently never fire.
  * Vietnamese descriptions / patterns are corrupted the same way.

The Streamlit writer already pins ``encoding="utf-8"``; the QC read path
(``agents/qc.py``) and ``AutonomyPolicy.load`` were the mismatched readers.

To make this guard bite on every platform — POSIX CI defaults to UTF-8 and
would otherwise read a reverted bare ``open()`` correctly and mask the bug —
``_simulate_windows_locale`` forces encoding-less ``open()`` calls to cp1252,
reproducing the Windows failure mode deterministically. The explicit
``encoding="utf-8"`` in the fixed code wins over that simulated default.
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager

import yaml

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy

# Hand-authored so the file genuinely contains the UTF-8 multibyte sequences
# (``yaml.safe_dump`` without ``allow_unicode=True`` would escape them to pure
# ASCII and the encoding path would never be exercised).
_RULES_YAML = (
    "scenario: encoding-regression\n"
    "target_category: Rooms\n"
    "rules:\n"
    "  - id: area.min.metric\n"
    "    parameter: Area\n"
    "    requirement: numeric_compare\n"
    '    operator: ">="\n'
    "    threshold: 10\n"
    '    unit: "m²"\n'
    "    category: Rooms\n"
    "    severity_tag: geometric_violation\n"
    "    severity_level: severity_medium\n"
    '    description: "Diện tích phòng tối thiểu 10 m²"\n'
    "    autofill:\n"
    "      strategy: none\n"
)


@contextmanager
def _simulate_windows_locale():
    """Force encoding-less ``open()`` to use cp1252, like a Windows box in a
    Western locale. Calls that pass ``encoding=`` are honoured verbatim."""
    real_open = builtins.open

    def patched(file, mode="r", *args, encoding=None, **kwargs):
        if encoding is None and "b" not in mode:
            encoding = "cp1252"
        return real_open(file, mode, *args, encoding=encoding, **kwargs)

    builtins.open = patched
    try:
        yield
    finally:
        builtins.open = real_open


def _autonomy(tmp_path) -> AutonomyPolicy:
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {"parameters": {"set_value": "approve"}},
                "severity_rules": {"geometric_violation": "severity_high"},
            }
        ),
        encoding="utf-8",
    )
    return AutonomyPolicy.load(cfg)


def _state(elements):
    return {
        "project_id": "enc",
        "iteration": 0,
        "max_iterations": 1,
        "elements": elements,
        "findings": [],
        "proposed_fixes": [],
        "status": "checking",
        "error": None,
    }


def test_superscript_unit_and_vietnamese_roundtrip(tmp_path):
    """The rule's ``unit`` and ``description`` survive load intact — no mojibake
    even when the ambient locale would default ``open()`` to cp1252."""
    p = tmp_path / "rules.area.yaml"
    p.write_text(_RULES_YAML, encoding="utf-8")

    with _simulate_windows_locale():
        qc = QCAgent(rules_path=p, autonomy=_autonomy(tmp_path))

    rule = qc.rules.rules[0]
    assert rule.unit == "m²"  # U+00B2 intact — NOT "mÂ²"
    assert rule.description == "Diện tích phòng tối thiểu 10 m²"


def test_area_numeric_compare_fires_after_metric_conversion(tmp_path):
    """End-to-end: with the unit read correctly, the ft²→m² conversion runs and
    the area rule fires on the under-size room. Under the bug the mojibake'd
    unit defeats the conversion (raw ft² compared) and BOTH rooms pass."""
    p = tmp_path / "rules.area.yaml"
    p.write_text(_RULES_YAML, encoding="utf-8")

    with _simulate_windows_locale():
        qc = QCAgent(rules_path=p, autonomy=_autonomy(tmp_path))

    out = qc.run(
        _state(
            [
                # Area stored in ft². 100 ft² = 9.29 m² < 10 → non-compliant.
                {"id": "R1", "category": "Rooms", "params": {"Area": 100.0}},
                # 120 ft² = 11.15 m² >= 10 → compliant. Proves the threshold is
                # applied in m² (raw ft² would pass BOTH and fire nothing).
                {"id": "R2", "category": "Rooms", "params": {"Area": 120.0}},
            ]
        )
    )

    s = out["outcomes_summary"]
    assert s["total"] == 2
    assert s["compliant"] == 1
    assert s["non_compliant"] == 1
    assert [f["element_id"] for f in out["findings"]] == ["R1"]


def test_simulator_reproduces_cp1252_mojibake(tmp_path):
    """Guard for the guard: prove ``_simulate_windows_locale`` actually forces
    the failure mode, so the tests above aren't vacuously green. A bare
    ``open()`` mojibakes; an explicit ``encoding="utf-8"`` does not."""
    p = tmp_path / "x.yaml"
    p.write_text('unit: "m²"\n', encoding="utf-8")

    with _simulate_windows_locale():
        with open(p) as f:  # the reverted bug
            assert "mÂ²" in f.read()
        with open(p, encoding="utf-8") as f:  # the fix
            assert "m²" in f.read()
