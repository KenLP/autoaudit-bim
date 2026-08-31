"""Phase 2 — UI surface helpers (pure, Streamlit-free) + provenance stash.

Pins the "guarded, additive, invisible when off" contract: the diagnosis column
and the LLM-source badge appear ONLY when the Phase-2 data is present, so a
Phase-1 run renders identically.
"""

from __future__ import annotations

from bim_orchestrator.ui_phase2 import (
    DIAGNOSIS_COL,
    EVIDENCE_COL,
    LLM_SOURCE_BADGE,
    SOURCE_COL,
    attach_diagnosis_column,
    evidence_cell,
    format_diagnosis,
    has_any_diagnosis,
    source_cell,
)


def _finding(eid, diag=None):
    f = {"rule_id": "r", "element_id": eid, "parameter": "P"}
    if diag is not None:
        f["diagnosis"] = diag
    return f


_DIAG = {
    "summary": "Blank Department.",
    "suggested_action": "Set from room program.",
    "confidence": 0.8,
    "source": "llm",
}


# ---- diagnosis column (guarded) -------------------------------------------


def test_no_column_when_no_diagnosis() -> None:
    findings = [_finding("a"), _finding("b")]
    rows = [{"element": "a"}, {"element": "b"}]
    added = attach_diagnosis_column(rows, findings)
    assert added is False
    assert all(DIAGNOSIS_COL not in r for r in rows)  # Phase-1 table untouched


def test_column_added_when_any_diagnosis() -> None:
    findings = [_finding("a", _DIAG), _finding("b")]
    rows = [{"element": "a"}, {"element": "b"}]
    added = attach_diagnosis_column(rows, findings)
    assert added is True
    assert DIAGNOSIS_COL in rows[0] and rows[0][DIAGNOSIS_COL]
    assert rows[1][DIAGNOSIS_COL] == ""  # finding without a diagnosis → blank cell


def test_format_diagnosis_compacts_summary_action_confidence() -> None:
    text = format_diagnosis(_finding("a", _DIAG))
    assert "Blank Department." in text
    assert "→ Set from room program." in text
    assert "80%" in text


def test_format_diagnosis_empty_when_absent() -> None:
    assert format_diagnosis(_finding("a")) == ""


def test_attach_is_noop_on_length_mismatch() -> None:
    findings = [_finding("a", _DIAG)]
    rows = [{"element": "a"}, {"element": "b"}]  # parallelism broken
    assert attach_diagnosis_column(rows, findings) is False
    assert all(DIAGNOSIS_COL not in r for r in rows)


def test_has_any_diagnosis() -> None:
    assert has_any_diagnosis([_finding("a", _DIAG)]) is True
    assert has_any_diagnosis([_finding("a")]) is False


# ---- source badge (guarded) -----------------------------------------------


def test_source_cell_badges_llm_only() -> None:
    assert source_cell({"value_source": "llm"}) == {SOURCE_COL: LLM_SOURCE_BADGE}
    assert source_cell({"value_source": None}) == {}
    assert source_cell({}) == {}  # deterministic fix → no column key


def test_evidence_cell_shows_only_when_present() -> None:
    ev = "Pr_30_59_24_14 = Fire-resisting doorsets (Uniclass2015)"
    assert evidence_cell({"evidence": ev}) == {EVIDENCE_COL: ev}
    assert evidence_cell({"evidence": None}) == {}
    assert evidence_cell({}) == {}  # no evidence → no column key (Phase-1 identical)
