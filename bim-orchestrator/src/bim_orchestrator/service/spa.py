"""AutoAudit UI SPA mount (Phase 3b, B5/B15 spec §A):

* ``GET /`` always 307-redirects to ``/ui/``.
* ``autoaudit-ui/dist`` present → mounted at ``/ui`` (StaticFiles, html=True);
  a 404 for anything under ``/ui/*`` falls back to ``index.html`` so
  client-side routing (react-router) owns deep links like ``/ui/runs/<id>``.
* ``autoaudit-ui/dist`` absent (not built yet) → ``GET /ui`` (and any
  ``/ui/*``) returns a one-line 503 HTML page pointing at the build script —
  never a stack trace.

DIST resolves relative to THIS file: service/ -> bim_orchestrator/ -> src/ ->
bim-orchestrator/ -> repo root (parents[4]) -> autoaudit-ui/dist. Frontend
build/scaffolding is owned by a separate implementer (B3) — this module only
serves whatever it finds there.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

DIST = Path(__file__).resolve().parents[4] / "autoaudit-ui" / "dist"

_NOT_BUILT_HTML = (
    "<!doctype html><html><head><title>AutoAudit UI</title></head>"
    "<body style=\"font-family:sans-serif;padding:2rem\">"
    "<h1>AutoAudit UI not built</h1>"
    "<p>Run <code>scripts/build-ui.ps1</code> (from <code>bim-orchestrator/</code>) "
    "to build the SPA, then reload this page.</p>"
    "</body></html>"
)


def mount_spa(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def _root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=307)

    if not DIST.is_dir():

        @app.get("/ui", include_in_schema=False)
        async def _ui_not_built() -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT_HTML, status_code=503)

        @app.get("/ui/{rest:path}", include_in_schema=False)
        async def _ui_not_built_sub(rest: str = "") -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT_HTML, status_code=503)

        return

    app.mount("/ui", StaticFiles(directory=DIST, html=True), name="ui")
    index = DIST / "index.html"

    # NOT a blanket ``@app.exception_handler(404)`` — that would intercept
    # every HTTPException(404) raised anywhere in the API (runs/approvals
    # "not found" responses included) and replace their real `detail` with a
    # generic one. Registering on the StarletteHTTPException CLASS instead and
    # delegating to FastAPI's own default handler for the non-SPA case keeps
    # every other 404's status/detail exactly as raised.
    @app.exception_handler(StarletteHTTPException)
    async def _spa_fallback(request: Request, exc: StarletteHTTPException):
        if (
            exc.status_code == 404
            and request.url.path.startswith("/ui/")
            and index.exists()
        ):
            return FileResponse(index)
        return await http_exception_handler(request, exc)


__all__ = ["DIST", "mount_spa"]
