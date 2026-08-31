"""Durable read/write for the approval records that carry Loop 2's state.

``<approvals_dir>/<issue_id>.json`` is the ONLY record that a proposal exists,
which writes it covers, and — after the watcher runs — which of them actually
landed. Nothing else can reconstruct it: the ACC issue body carries the
fingerprint but not the per-fix ``applied`` flags, and Revit carries the result
but no memory of who asked.

Two properties this module exists to guarantee (M-06, 2026-08-01 review):

* **A record is replaced atomically.** Every writer used to truncate the file
  and write in place. A crash mid-write — including the persist that happens
  right AFTER the writes have already gone into the model — left a truncated
  JSON file, so the run's proof of what it changed was the thing that got
  lost. Write to a temp file in the same directory, then ``os.replace``: a
  reader sees either the old record or the new one, never half of one.

* **An unreadable record is reported, never skipped in silence.** The loaders
  used to ``continue`` past a corrupt file, so a record that could no longer
  be parsed looked exactly like a record that had never existed — the issue
  stayed open on ACC forever and nothing anywhere said why.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def write_record(path: Path, record: dict[str, Any]) -> None:
    """Replace ``path`` with ``record`` atomically (tmp file + ``os.replace``).

    The temp file is created in the SAME directory so the replace stays on one
    filesystem (``os.replace`` is only atomic within a filesystem).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_record(path: Path) -> dict[str, Any] | None:
    """Parse one record, or ``None`` if it cannot be read — WITH a log line.

    Callers skip a ``None``; the difference from the old bare ``continue`` is
    that the operator now learns a record exists but is unreadable, which is
    the only signal that an issue will never be applied or closed.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error(
            "approval_store.record_unreadable",
            path=str(path),
            error=str(exc),
            hint="this proposal can no longer be applied or closed by the "
            "watcher — inspect the file; a truncated write predates the "
            "atomic-write fix (M-06)",
        )
        return None
