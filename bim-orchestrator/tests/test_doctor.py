"""M2-C — ``doctor_checks()`` library extraction (P3-5 refactor): the CLI
``--doctor`` and the service's ``GET /api/settings/doctor`` now share ONE
checklist builder instead of the service re-deriving its own check logic.
"""

from __future__ import annotations

from bim_orchestrator.orchestrator import doctor, doctor_checks


def test_doctor_checks_shape() -> None:
    rows = doctor_checks()
    assert rows
    names = set()
    for row in rows:
        assert set(row) == {"name", "status", "detail"}
        assert row["status"] in {"pass", "warn", "fail"}
        names.add(row["name"])
    assert "python >= 3.12" in names
    assert "runs/ writable" in names


def test_doctor_checks_only_required_checks_can_fail() -> None:
    """Non-required checks degrade to warn, never fail -- a clean dev box
    with no forma-mcp.exe / no Revit / no satellites must still doctor()
    exit 0 (only "python >= 3.12" and "runs/ writable" are REQUIRED)."""
    required_names = {"python >= 3.12", "runs/ writable"}
    for row in doctor_checks():
        if row["status"] == "fail":
            assert row["name"] in required_names


def test_doctor_cli_prints_table_and_exits_0(capsys) -> None:
    code = doctor()
    out = capsys.readouterr().out
    assert code == 0
    assert "Check" in out and "Status" in out
    assert "PASS" in out  # uppercased status column, e.g. "python >= 3.12 ... PASS"
    assert "doctor: all required checks passed." in out
