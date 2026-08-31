"""Cross-run Path A issue registry — one JSON file under runs/.

Records every EXECUTED grouped Path A issue keyed by its group identity so a
later run (scheduled nightly, or a re-run) can skip re-raising an issue that
is still open on ACC. Liveness is checked by the CALLER (design agent) via
forma.get_issue — this module never touches the network (keeps the MCP
boundary in agents/, and stays unit-testable with a tmp file).

Scope (Mức 1 continuous audit, SPEC_SCHEDULED_AUDIT_DELTA.md Q3): only
``audit()`` passes a registry path; the legacy CLI ``--run``/``--run-revit``
invocations pass ``None`` (see ``agents.design.DesignAgent.__init__``) and
behave exactly as before — no file is ever read or written for them.

No file-lock: the AuditHub service's single-run lock already serialises
scheduled audits; a hand-run CLI invocation racing a scheduled one is an
accepted, narrow exception (last-write-wins on ``record``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import structlog

from bim_orchestrator.approval_store import write_record

log = structlog.get_logger(__name__)

RESOLVED_ISSUE_STATUSES = frozenset({"closed", "void", "completed"})


def group_key(
    project_id: str, rule_id: str, bucket: str, element_ids: Iterable[Any]
) -> str:
    """Stable sha256 hex over ``project|rule|bucket|eid1,eid2,...``.

    Element ids are stringified + sorted so the same element SET produces
    the same key regardless of iteration/discovery order.
    """
    eids = ",".join(sorted(str(e) for e in element_ids))
    raw = f"{project_id}|{rule_id}|{bucket}|{eids}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IssueRegistry:
    """Read-modify-write JSON store at ``path`` (``runs/issue_registry.json``).

    Missing file → empty registry (never raises). Corrupt file → warn once
    per instance + treat as empty (never raises — a bad file must not break
    an otherwise-healthy scheduled run).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._warned_corrupt = False

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "groups": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if not self._warned_corrupt:
                log.warning(
                    "issue_registry.corrupt_file", path=str(self._path), error=str(exc)
                )
                self._warned_corrupt = True
            return {"version": 1, "groups": {}}
        if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
            if not self._warned_corrupt:
                log.warning("issue_registry.malformed_shape", path=str(self._path))
                self._warned_corrupt = True
            return {"version": 1, "groups": {}}
        return data

    def lookup(self, key: str) -> dict[str, Any] | None:
        return self._read().get("groups", {}).get(key)

    def record(self, key: str, entry: dict[str, Any]) -> None:
        data = self._read()
        data.setdefault("groups", {})[key] = entry
        # M-06: atomic, same reason as the approval records — a truncated
        # registry silently un-dedups every group it was holding.
        write_record(self._path, data)
