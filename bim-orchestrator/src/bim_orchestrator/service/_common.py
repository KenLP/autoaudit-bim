"""Shared path/filename validators for the M1 routers (Phase 3b).

Small, dependency-free helpers so ``routes_runs.py`` / ``routes_approvals.py``
don't each re-derive the same traversal-guard regex. Mirrors the existing
``_run_dir`` helper inlined in ``service/app.py`` (P3-2) — kept here instead of
imported from there to avoid a circular import between the P3-2 routes and
the new M1 routers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import IO, Any

from fastapi import HTTPException

# Run ids are always "run-" + 8 lowercase hex chars (run_recorder.RunFolder.create).
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{8}$")

# Approval record filenames are "<issue_id>.json"; issue_id is ACC-assigned
# (alnum/dash/underscore/dot in practice) — allowlist rather than blocklist
# so a traversal payload (`../x`, `..%2Fx`) never reaches the filesystem.
APPROVAL_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")


def resolve_run_dir(runs_root: Path, run_id: str) -> Path:
    """Validate ``run_id`` shape BEFORE touching the filesystem, then resolve
    to ``runs_root/run_id``. 400 for a malformed id, 404 for a well-formed id
    with no matching folder."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")
    d = runs_root / run_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="unknown run id")
    return d


def validate_approval_filename(name: str) -> str:
    """Validate an approval record filename BEFORE touching the filesystem."""
    if not APPROVAL_FILENAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid approval file name")
    return name


# ── upload size caps (S-03, 2026-07-25 live review) ────────────────────────
#
# Both multipart routes did `raw = await file.read()` — the WHOLE upload into
# RAM with no ceiling, so one oversized (or hostile) POST could exhaust the
# pilot machine's memory. Reading in bounded chunks and stopping at a cap
# fixes both halves: the peak is the chunk, and the request is refused with a
# 413 instead of dying somewhere less legible.
UPLOAD_CHUNK = 1 << 20  # 1 MiB

# Regulation/BEP PDFs are genuinely big (a few hundred pages), so the default
# is generous; a pilot with a bigger standard raises it rather than patching.
DEFAULT_MAX_UPLOAD_MB = 64
# IDS is XML describing rules — orders of magnitude smaller. A separate,
# tighter cap: the loose PDF ceiling would be a silly limit for this route.
MAX_IDS_BYTES = 8 << 20  # 8 MiB


def max_upload_bytes() -> int:
    """PDF/document upload ceiling. ``AUTOAUDIT_MAX_UPLOAD_MB`` overrides;
    a junk or non-positive value falls back to the default rather than
    disabling the cap (fail-closed: a broken env var must not remove the
    limit it was set to tune)."""
    raw = os.environ.get("AUTOAUDIT_MAX_UPLOAD_MB", "").strip()
    try:
        mb = int(raw)
    except ValueError:
        mb = 0
    if mb <= 0:
        mb = DEFAULT_MAX_UPLOAD_MB
    return mb << 20


async def _iter_upload(file: Any, limit: int, what: str, hint: str | None):
    """Yield ``file``'s bytes in ``UPLOAD_CHUNK`` pieces, raising 413 as soon
    as the running total passes ``limit`` — so an oversized upload is refused
    at the cap, not after it has all been buffered.

    ``hint`` is appended to the 413 only when the caller's cap is actually
    tunable. Naming the env var on a route it does not govern (the IDS cap is
    fixed) sends the operator to a knob that changes nothing.
    """
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK)
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            detail = f"{what} exceeds the {limit >> 20} MiB upload limit"
            raise HTTPException(
                status_code=413, detail=detail if hint is None else f"{detail} ({hint})"
            )
        yield chunk


async def stream_upload_to_file(
    file: Any, dest: IO[bytes], *, limit: int, what: str, hint: str | None = None
) -> int:
    """Copy an upload to an already-open binary file, capped. Returns bytes
    written. Used where the payload goes to disk anyway (extraction) — the
    full document never sits in memory at all."""
    written = 0
    async for chunk in _iter_upload(file, limit, what, hint):
        dest.write(chunk)
        written += len(chunk)
    return written


async def read_upload_capped(
    file: Any, *, limit: int, what: str, hint: str | None = None
) -> bytes:
    """Read an upload fully into memory, but never more than ``limit``. For
    payloads that must be parsed whole (IDS XML); the cap is what keeps
    "in memory" bounded."""
    parts: list[bytes] = []
    async for chunk in _iter_upload(file, limit, what, hint):
        parts.append(chunk)
    return b"".join(parts)
