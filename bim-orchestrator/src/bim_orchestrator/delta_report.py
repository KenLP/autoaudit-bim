"""Scheduled/continuous audit — per-run delta report (SPEC_SCHEDULED_AUDIT_DELTA.md
W4, Q4/Q5).

Answers the question a BIM manager running an unattended nightly audit cares
about: which problems are NEW since the last comparable run, which are
RESOLVED, and which are still PERSISTENT?

Pure render-from-disk (renders-never-re-derives, same posture as
``audit_report.py``): reads ONLY ``metadata.json`` / ``outcomes.json`` /
``profile.json`` already written by ``run_recorder``/``audit()`` for the
current run and its baseline — never re-runs a check. The diff itself reuses
``run_recorder.diff_outcomes`` (v1 task V-2) verbatim; this module does not
implement its own diff algorithm.

Baseline selection (Q4): the newest earlier run with a successful terminal
status (``SUCCESSFUL_STATUSES``) and the SAME identity — same
``profile.json.profile_name`` when
the current run has a profile, or same ``metadata.mode`` AND no profile.json
on either side when it doesn't (a bare CLI ``--run``/``--run-revit`` run).
This avoids diffing against an unrelated ad-hoc run that happened to land
between two scheduled ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Statuses that mark a run whose outcomes.json is complete and trustworthy.
# ``--check``/``--apply`` record "completed"; the graph modes
# (``--run``/``--run-revit``, hence every ``--audit``) record the final state
# status "converged". Gating on "completed" alone made the delta feature dead
# in the scheduled-audit main path (v1.7-3bP1 post-ship fix). "failed" and a
# max-iterations non-converged exit stay excluded: their outcomes may be cut
# short, so a diff against them would misreport "resolved".
SUCCESSFUL_STATUSES = frozenset({"completed", "converged"})


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read + parse a JSON file. Missing file or corrupt JSON → None (never
    raises) — a sidecar being absent/broken must not crash the report."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def find_baseline(runs_root: Path, current_dir: Path) -> Path | None:
    """Newest earlier successful run with the SAME identity as ``current_dir``.

    See module docstring for the identity rule. Returns None when there is
    no comparable candidate (including when ``current_dir``'s own
    metadata.json can't be read).
    """
    current_meta = _read_json(current_dir / "metadata.json")
    if current_meta is None:
        return None
    current_started = current_meta.get("started_at") or ""
    current_profile = _read_json(current_dir / "profile.json")
    current_profile_name = (
        current_profile.get("profile_name") if current_profile is not None else None
    )
    current_mode = current_meta.get("mode")
    current_resolved = current_dir.resolve()

    if not runs_root.exists():
        return None

    candidates: list[tuple[str, Path]] = []
    for folder in runs_root.iterdir():
        if not folder.is_dir() or not folder.name.startswith("run-"):
            continue
        if folder.resolve() == current_resolved:
            continue
        meta = _read_json(folder / "metadata.json")
        if meta is None:
            continue
        if meta.get("status") not in SUCCESSFUL_STATUSES:
            continue
        started = meta.get("started_at") or ""
        if not started or not (started < current_started):
            continue
        if _read_json(folder / "outcomes.json") is None:
            continue

        cand_profile = _read_json(folder / "profile.json")
        if current_profile is not None:
            if cand_profile is None or cand_profile.get("profile_name") != current_profile_name:
                continue
        else:
            if cand_profile is not None:
                continue
            if meta.get("mode") != current_mode:
                continue
        candidates.append((started, folder))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _field(finding: dict[str, Any], key: str) -> str:
    value = finding.get(key)
    return str(value) if value not in (None, "") else "?"


def _render_group(title: str, findings: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title} ({len(findings)})", ""]
    if not findings:
        lines.append("_(none)_")
        lines.append("")
        return lines
    grouped: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        grouped.setdefault(_field(f, "rule_id"), []).append(f)
    for rule_id in sorted(grouped):
        items = grouped[rule_id]
        lines.append(f"### {rule_id} ({len(items)} element(s))")
        lines.append("")
        for f in items:
            lines.append(
                f"- `{_field(f, 'element_id')}` | param `{_field(f, 'parameter')}` "
                f"| {_field(f, 'status')} | {_field(f, 'severity')}"
            )
        lines.append("")
    return lines


def _diff_for(current_dir: Path, baseline_dir: Path | None) -> dict[str, list[dict[str, Any]]]:
    from bim_orchestrator.run_recorder import diff_outcomes

    current_outcomes = _read_json(current_dir / "outcomes.json") or {}
    baseline_outcomes = (
        _read_json(baseline_dir / "outcomes.json") if baseline_dir is not None else None
    )
    return diff_outcomes(baseline_outcomes, current_outcomes)


def render_delta_report(current_dir: Path, baseline_dir: Path | None) -> str:
    """Render the Markdown delta report for one run vs its baseline.

    English (matches trend.md/report.md); no listing caps — findings are
    grouped by rule_id to stay readable instead.
    """
    from datetime import datetime

    current_meta = _read_json(current_dir / "metadata.json") or {}
    current_profile = _read_json(current_dir / "profile.json")
    run_id = current_meta.get("run_id") or current_dir.name

    baseline_meta = (
        _read_json(baseline_dir / "metadata.json") or {} if baseline_dir is not None else {}
    )

    lines: list[str] = [f"# Delta report: {run_id}", ""]
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    current_line = (
        f"Current run: `{run_id}` "
        f"(started {current_meta.get('started_at', '?')}, "
        f"mode `{current_meta.get('mode', '?')}`"
    )
    if current_profile is not None:
        current_line += f", profile `{current_profile.get('profile_name', '?')}`"
    cur_doc = current_meta.get("document") or {}
    if cur_doc.get("title"):
        current_line += f", model `{cur_doc['title']}`"
    current_line += ")"
    lines.append(current_line)

    base_doc = baseline_meta.get("document") or {}
    if baseline_dir is None:
        lines.append("Baseline: (none — first comparable run)")
    else:
        baseline_line = (
            f"Baseline: `{baseline_meta.get('run_id', baseline_dir.name)}` "
            f"(started {baseline_meta.get('started_at', '?')}"
        )
        if base_doc.get("title"):
            baseline_line += f", model `{base_doc['title']}`"
        baseline_line += ")"
        lines.append(baseline_line)

    if cur_doc.get("title") and base_doc.get("title") and cur_doc["title"] != base_doc["title"]:
        lines.append(
            f"⚠ **Model differs from baseline** (current `{cur_doc['title']}`, "
            f"baseline `{base_doc['title']}`) — this delta may compare two "
            "different documents."
        )
    lines.append("")

    diff = _diff_for(current_dir, baseline_dir)
    lines.append("## Summary")
    lines.append("")
    lines.append("| Resolved | Newly introduced | Persistent |")
    lines.append("|---|---|---|")
    lines.append(
        f"| {len(diff['resolved'])} | {len(diff['newly_introduced'])} "
        f"| {len(diff['persistent'])} |"
    )
    lines.append("")

    lines.extend(_render_group("Newly introduced", diff["newly_introduced"]))
    lines.extend(_render_group("Resolved since baseline", diff["resolved"]))
    lines.extend(_render_group("Persistent", diff["persistent"]))

    return "\n".join(lines)


def write_delta_report(current_dir: Path) -> Path:
    """Find the baseline, render, and write ``delta.md`` + ``delta.json``
    into ``current_dir``. Returns the ``delta.md`` path.
    """
    runs_root = current_dir.parent
    baseline_dir = find_baseline(runs_root, current_dir)
    markdown = render_delta_report(current_dir, baseline_dir)
    (current_dir / "delta.md").write_text(markdown, encoding="utf-8")

    diff = _diff_for(current_dir, baseline_dir)
    current_meta = _read_json(current_dir / "metadata.json") or {}
    baseline_meta = _read_json(baseline_dir / "metadata.json") if baseline_dir else None
    payload = {
        "run_id": current_meta.get("run_id") or current_dir.name,
        "baseline_run_id": (
            (baseline_meta or {}).get("run_id") or baseline_dir.name
            if baseline_dir is not None
            else None
        ),
        "document": current_meta.get("document"),
        "baseline_document": (baseline_meta or {}).get("document") if baseline_dir else None,
        "counts": {
            "resolved": len(diff["resolved"]),
            "newly_introduced": len(diff["newly_introduced"]),
            "persistent": len(diff["persistent"]),
        },
        "resolved": diff["resolved"],
        "newly_introduced": diff["newly_introduced"],
        "persistent": diff["persistent"],
    }
    (current_dir / "delta.json").write_text(
        json.dumps(payload, default=str), encoding="utf-8"
    )
    return current_dir / "delta.md"
