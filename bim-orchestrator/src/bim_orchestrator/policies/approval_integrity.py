"""Approval-integrity fingerprint (approval-security pass).

Binds the human-approved ACC proposal issue to the exact set of Revit writes
the :class:`ApprovalWatcher` will execute.

Threat: the watcher applies the parked writes from a LOCAL record
(``<approvals_dir>/<issue_id>.json``) when the ACC issue reaches the approve
status — but the human approves the *issue* they read in ACC, never the local
record. If that record's ``fixes`` are altered (or the wrong record is swapped
in) after the proposal was published, the watcher would silently execute writes
the human never signed off on.

Mitigation: :class:`~bim_orchestrator.agents.design.DesignAgent` stamps a
fingerprint over the canonical write-set into BOTH the proposal issue body (the
artifact that lives in ACC, out of reach of local-record tampering) and the
record. Before applying, the watcher recomputes the fingerprint from the local
record's fixes and compares it against the one carried in the fetched issue: a
mismatch means the writes no longer match what was approved, so it refuses.

The ACC issue is the trust anchor; a fingerprint stored on the record is only a
best-effort fallback for issues whose marker was stripped (an attacker who owns
the record file owns that copy too). Pure + I/O-free so it lives under
``policies/`` and both the propose and apply sides compute it identically.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Marker embedded in the proposal issue description. Machine-stable (exact
# prefix + lowercase hex) so it survives ACC reflowing the surrounding markdown.
FINGERPRINT_LABEL = "AutoAudit-Fingerprint"
_FINGERPRINT_RE = re.compile(rf"{FINGERPRINT_LABEL}:\s*([0-9a-f]{{8,}})")


def _canonical(fixes: list[dict[str, Any]]) -> list[list[str]]:
    """Reduce each fix to the fields that determine what gets WRITTEN.

    Only ``(write target, action, parameter, new value)`` land in the model, so
    only those bind the fingerprint. ``finding_id`` / ``old_value`` / display
    sugar are excluded — a cosmetic record edit shouldn't trip the gate, but any
    change to an actual write must. Sorted so element ordering is not
    significant.
    """
    rows = [
        [
            str(f.get("element_id")),
            str(f.get("action") or "set_parameter"),
            str(f.get("parameter") or ""),
            str(f.get("new_value")),
        ]
        for f in fixes
    ]
    rows.sort()
    return rows


def fingerprint(fixes: list[dict[str, Any]]) -> str:
    """Stable 16-hex digest over the canonical write-set of ``fixes``."""
    payload = json.dumps(_canonical(fixes), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint_line(fp: str) -> str:
    """The marker line to embed in the proposal issue body."""
    return f"{FINGERPRINT_LABEL}: {fp}"


def parse_fingerprint(text: str | None) -> str | None:
    """Extract the fingerprint from an issue description, or ``None`` if absent."""
    if not text:
        return None
    m = _FINGERPRINT_RE.search(text)
    return m.group(1) if m else None
