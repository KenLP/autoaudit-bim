"""Phase 2 — pure presentation helpers for surfacing the LLM agents in the UI.

Deliberately Streamlit-free and side-effect-free: every function takes plain
dicts and returns plain data, so the Streamlit app stays a thin caller and these
helpers are unit-testable offline. Living in the installed package (not under
``streamlit_app/``) keeps imports clean from both the app and the tests, and
matches the additive Phase-2 layout (new files, never merged to Phase 1's main).

The guiding rule is the same as the agents': **guarded, additive, invisible
when off.** Each helper is a no-op when the Phase-2 data is absent (no LLM run),
so a Phase-1 run renders pixel-identically — no empty columns, no badges.
"""

from __future__ import annotations

from typing import Any

# Column / cell labels kept here so the app and tests agree on one spelling.
DIAGNOSIS_COL = "💡 chẩn đoán"
SOURCE_COL = "nguồn"
LLM_SOURCE_BADGE = "🤖 AI đề xuất"
EVIDENCE_COL = "📎 căn cứ"


def has_any_diagnosis(findings: list[dict[str, Any]]) -> bool:
    """True iff at least one finding carries an advisory diagnosis."""
    return any(f.get("diagnosis") for f in findings)


def format_diagnosis(finding: dict[str, Any]) -> str:
    """One-cell rendering of a finding's diagnosis (empty when absent)."""
    diag = finding.get("diagnosis")
    if not diag:
        return ""
    summary = (diag.get("summary") or "").strip()
    action = (diag.get("suggested_action") or "").strip()
    conf = diag.get("confidence")
    parts: list[str] = []
    if summary:
        parts.append(summary)
    if action:
        parts.append(f"→ {action}")
    text = "  ".join(parts)
    if isinstance(conf, (int, float)) and text:
        text = f"{text}  ({float(conf):.0%})"
    return text


def attach_diagnosis_column(
    rows: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> bool:
    """Add a diagnosis column to ``rows`` IN PLACE — but only when there is
    something to show. ``rows`` and ``findings`` must be parallel (same order
    and length: one row per finding, as the Results tab builds them).

    Returns True if the column was added. When no finding has a diagnosis (the
    Phase-1 case, or LLM diagnostic flag off) this is a no-op and the table is
    unchanged — that is the whole point of the guard.
    """
    if not has_any_diagnosis(findings):
        return False
    if len(rows) != len(findings):
        # Defensive: never mangle the table if the caller's parallelism broke.
        return False
    for row, finding in zip(rows, findings):
        row[DIAGNOSIS_COL] = format_diagnosis(finding)
    return True


def source_cell(fix: dict[str, Any]) -> dict[str, str]:
    """A spreadable cell marking a fix whose value an LLM originated.

    Returns ``{SOURCE_COL: badge}`` for an LLM-proposed value, else ``{}`` — so
    spreading it into a row dict adds the "nguồn" column ONLY for LLM fixes. If
    no fix in a proposal is LLM-originated, no row gets the key and the rendered
    table has no extra column (Phase-1-identical).
    """
    if fix.get("value_source") == "llm":
        return {SOURCE_COL: LLM_SOURCE_BADGE}
    return {}


def evidence_cell(fix: dict[str, Any]) -> dict[str, str]:
    """A spreadable cell carrying the cite-the-clause evidence for a fix (the
    chosen classification code's definition). Returns ``{EVIDENCE_COL: text}``
    when present, else ``{}`` — so the "📎 căn cứ" column appears only when a fix
    actually has evidence, keeping non-classification / Phase-1 tables unchanged.
    """
    ev = fix.get("evidence")
    return {EVIDENCE_COL: str(ev)} if ev else {}
