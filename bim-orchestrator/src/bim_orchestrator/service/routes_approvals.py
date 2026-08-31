"""M1 — Approvals inbox endpoints (Phase 3b).

Ports the lifecycle-mapping logic from ``streamlit_app/app.py``
(``_proposal_lifecycle``/``_proposal_rules``/``_ignore_proposal`` around
line 4837-4945) onto the service API. Read-only listing + ignore/restore only
— actually APPLYING an approval stays the ApprovalWatcher's job via the
existing ``POST /approvals/apply-once`` (B11); this router never writes a
Revit parameter or touches ACC.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from bim_orchestrator.service._common import validate_approval_filename
from bim_orchestrator.service.models import (
    ApprovalCounts,
    ApprovalFix,
    ApprovalRecord,
    ApprovalsResponse,
    OkResponse,
)


def _safe_load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lifecycle_status(rec: dict[str, Any], *, ignored: bool) -> str:
    """Mirrors ``streamlit_app/app.py:_proposal_lifecycle`` (app.py:4837-4849).

    Old records lack ``issue_status``; ``applied`` is the ground-truth flag
    the ApprovalWatcher sets (approval_watcher.py:471-476). The watcher closes
    the ACC issue on a full apply, so ``applied and issue_status=="closed"``
    is the common case; ``applied_pending_close``/``partial_pending`` (a
    write landed but the close call hasn't succeeded yet, or the apply was
    only partial) surfaces as "applied — issue still open".
    """
    if ignored:
        return "ignored"
    if rec.get("applied"):
        issue_status = rec.get("issue_status") or "closed"
        return "applied" if issue_status == "closed" else "applied_issue_open"
    return "pending"


def _rule_ids(rec: dict[str, Any]) -> list[str]:
    """Mirrors ``streamlit_app/app.py:_proposal_rules`` — parse each fix's
    ``finding_id`` ("rule::eid") for its rule component."""
    return sorted(
        {
            (f.get("finding_id") or "").split("::")[0]
            for f in rec.get("fixes", [])
        }
        - {""}
    )


def _to_record(filename: str, rec: dict[str, Any], *, ignored: bool) -> ApprovalRecord:
    fixes = [
        ApprovalFix(
            element_id=f.get("element_id"),
            parameter=f.get("parameter"),
            old_value=f.get("old_value"),
            new_value=f.get("new_value"),
            inherited_from=f.get("inherited_from"),
            action=f.get("action"),
            value_source=f.get("value_source"),   # L2-05: LLM vs deterministic
            evidence=f.get("evidence"),
        )
        for f in rec.get("fixes", [])
    ]
    return ApprovalRecord(
        file=filename,
        display_id=rec.get("display_id"),
        issue_id=rec.get("issue_id"),
        project_id=rec.get("project_id"),
        rule_ids=_rule_ids(rec),
        status=_lifecycle_status(rec, ignored=ignored),  # type: ignore[arg-type]
        created_at=rec.get("created_at"),
        fixes=fixes,
    )


def build_approvals_router(approvals_dir: Path) -> APIRouter:
    router = APIRouter(tags=["approvals"])
    ignored_dir = approvals_dir / "_ignored"

    @router.get("/approvals", response_model=ApprovalsResponse)
    async def list_approvals() -> ApprovalsResponse:
        proposals: list[ApprovalRecord] = []
        if approvals_dir.exists():
            for p in sorted(approvals_dir.glob("*.json")):
                rec = _safe_load(p)
                if rec is None:
                    continue
                proposals.append(_to_record(p.name, rec, ignored=False))
        if ignored_dir.exists():
            for p in sorted(ignored_dir.glob("*.json")):
                rec = _safe_load(p)
                if rec is None:
                    continue
                proposals.append(_to_record(p.name, rec, ignored=True))

        counts = ApprovalCounts(
            pending=sum(1 for pr in proposals if pr.status == "pending"),
            applied=sum(
                1 for pr in proposals if pr.status in ("applied", "applied_issue_open")
            ),
            ignored=sum(1 for pr in proposals if pr.status == "ignored"),
        )
        return ApprovalsResponse(proposals=proposals, counts=counts)

    @router.post("/approvals/{file}/ignore", response_model=OkResponse)
    async def ignore(file: str) -> OkResponse:
        name = validate_approval_filename(file)
        src = approvals_dir / name
        if not src.exists():
            raise HTTPException(status_code=404, detail="approval record not found")
        rec = _safe_load(src)
        if rec is not None and rec.get("applied"):
            raise HTTPException(
                status_code=409, detail="cannot ignore an already-applied proposal"
            )
        try:
            ignored_dir.mkdir(parents=True, exist_ok=True)
            src.rename(ignored_dir / name)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"failed to move approval file: {exc}"
            ) from exc
        return OkResponse()

    @router.post("/approvals/{file}/restore", response_model=OkResponse)
    async def restore(file: str) -> OkResponse:
        name = validate_approval_filename(file)
        src = ignored_dir / name
        if not src.exists():
            raise HTTPException(
                status_code=404, detail="ignored approval record not found"
            )
        dest = approvals_dir / name
        if dest.exists():
            raise HTTPException(
                status_code=409, detail="a pending record with this name already exists"
            )
        try:
            approvals_dir.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"failed to move approval file: {exc}"
            ) from exc
        return OkResponse()

    return router
