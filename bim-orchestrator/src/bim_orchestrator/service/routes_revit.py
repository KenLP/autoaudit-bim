"""M1 — "Highlight in Revit" endpoint (Phase 3b, B8).

MCP boundary held: the only Revit access here goes through
``mcp_clients.revit.make_revit_client`` — never a direct addin call. The
per-level walk itself lives in ``bim_orchestrator.highlight``, not here: the
service holds zero business logic (P3 D6/D7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bim_orchestrator.highlight import highlight_elements
from bim_orchestrator.mcp_clients.revit import make_revit_client
from bim_orchestrator.service.models import (
    HighlightRequest,
    HighlightResponse,
    HighlightViewOutcome,
    RevitDocumentResponse,
)


def build_revit_router() -> APIRouter:
    router = APIRouter(tags=["revit"])

    @router.post("/revit/highlight", response_model=HighlightResponse)
    async def highlight(req: HighlightRequest) -> HighlightResponse:
        try:
            async with make_revit_client() as client:
                outcome = await highlight_elements(
                    client,
                    req.element_ids,
                    color=req.color.model_dump() if req.color else None,
                    reset=req.reset,
                    per_level=req.per_level,
                )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"Revit addin unreachable: {exc}"
            ) from exc
        return HighlightResponse(
            ok=True,
            selected=outcome.selected,
            views=[HighlightViewOutcome(**v.to_dict()) for v in outcome.views],
        )

    @router.get("/revit/document", response_model=RevitDocumentResponse)
    async def document() -> RevitDocumentResponse:
        """The live open Revit model — so the panel can show what you're
        actually auditing (vs. the last run's metadata). Never errors:
        addin down or no document open → ``connected=False`` so the UI just
        falls back to run metadata.
        """
        try:
            async with make_revit_client() as client:
                data = await client.get_document_info()
        except Exception:
            return RevitDocumentResponse(connected=False)
        # The addin returns {title, path, activeView, ...}; a document-less
        # Home screen returns an empty/absent title.
        title = (data or {}).get("title") or None
        return RevitDocumentResponse(
            connected=bool(title),
            title=title,
            path=(data or {}).get("pathName") or (data or {}).get("path") or None,
            active_view=(data or {}).get("activeView") or (data or {}).get("active_view"),
        )

    return router
