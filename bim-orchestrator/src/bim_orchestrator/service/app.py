"""AuditHub FastAPI app (P3-2) — localhost-only orchestration API.

Run: ``autoaudit-service`` (entry point) or
``uv run uvicorn bim_orchestrator.service.app:app`` — binds 127.0.0.1:8601,
NO auth at P3 (same-machine only; documented in the pilot README). Browser
cross-origin writes are rejected by the S-01 CSRF guard below — the bind
address never protected against those, they ride the user's own browser.

Endpoints (spec P3-2):
  GET  /health                       — probe the 4 capability axes
  POST /audits                       — start an audit (409 while one runs, D7)
  GET  /audits/{id}                  — job status (+ run summary when done)
  GET  /audits/{id}/events           — SSE progress stream
  GET  /runs                         — run_recorder.list_runs
  GET  /runs/{id}/report             — report.md (text/markdown)
  GET  /runs/{id}/verification-report— verification_report.md
  GET  /runs/{id}/artifacts/{path}   — any run artifact (traversal-guarded)
  POST /approvals/apply-once         — one ApprovalWatcher pass (K5 mirror)
"""

from __future__ import annotations

import json
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from bim_orchestrator.service.models import (
    ApplyOnceResponse,
    AuditCreated,
    AuditRequest,
    AuditStatus,
    HealthAxes,
    HealthResponse,
)
from bim_orchestrator.service.runner import AuditRunner

log = structlog.get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8601

# S-01 (2026-07-25 live review): the service binds 127.0.0.1, but a CSRF
# request rides the USER'S browser, which is already on the right side of
# that bind. `POST /approvals/apply-once` needs no body and no custom header
# — a CORS-simple request — so any web page the user had open could trigger
# a real Revit write pass with fetch(..., {mode:'no-cors'}). Browsers attach
# `Origin` to every cross-origin POST, so an unsafe-method request carrying a
# non-local Origin can only be that attack; one guard closes S-01 and S-02
# (the multipart LLM-quota routes) for every router at once. Requests
# WITHOUT an Origin (curl, httpx, the runbooks, this suite) are untouched —
# this is a browser-vector guard, not auth; same-machine callers were always
# trusted (P3 posture, module docstring).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _origin_is_local(origin: str) -> bool:
    """True only for http(s)://<local host>[:port]. Anything unparseable —
    including the literal ``null`` a sandboxed iframe sends — is NOT local."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    # .hostname lowercases and strips [] from IPv6 literals.
    return parts.hostname in _LOCAL_HOSTNAMES


def _version() -> str:
    try:
        return importlib_metadata.version("bim-orchestrator")
    except importlib_metadata.PackageNotFoundError:
        return "dev"


# ── health probes (module-level so tests can monkeypatch) ──────────────────


async def probe_revit() -> bool:
    """Addin REST /health, 3s ceiling — Revit may simply not be open."""
    import os

    import httpx

    from bim_orchestrator.mcp_clients.revit import _resolve_port

    port = _resolve_port(os.environ.get("REVIT_MCP_VERSION", "2026"))
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/health")
            return resp.status_code == 200
    except Exception:
        return False


def probe_forma() -> bool:
    from bim_orchestrator.mcp_clients.forma import _vendor_exe

    return _vendor_exe("forma-mcp") is not None


def probe_axes() -> tuple[bool, bool]:
    from bim_orchestrator.policies.audit_profile import load_audit_services

    services = load_audit_services()
    return services.available("lod"), services.available("spatial")


def apply_approvals_once(approvals_dir: Path) -> tuple[int, int]:
    """One watcher pass → (applied, held). Held = records that were pending
    before the pass and still are after it (issue not yet 'In progress',
    fingerprint/stale gate, ACC unreachable, ...). Runs the SAME
    ApprovalWatcher path as --apply-approvals-once — never a direct apply."""
    import asyncio as _asyncio

    from bim_orchestrator.approval_watcher import ApprovalWatcher
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig
    from bim_orchestrator.mcp_clients.revit import make_revit_client

    def _pending_count() -> int:
        n = 0
        for p in Path(approvals_dir).glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not rec.get("applied"):
                n += 1
        return n

    async def _run() -> tuple[int, int]:
        before = _pending_count()
        forma_config = FormaMCPConfig.from_env()
        async with FormaMCPClient(forma_config) as forma:
            async with make_revit_client() as revit:
                watcher = ApprovalWatcher(approvals_dir, forma, revit)
                applied = await watcher.scan_once()
        return len(applied), max(0, before - len(applied))

    # Runs in a worker thread (no ambient loop) — asyncio.run owns a fresh one.
    return _asyncio.run(_run())


# ── app factory ─────────────────────────────────────────────────────────────


def create_app(
    runs_root: Path | None = None,
    approvals_dir: Path | None = None,
    config_dir: Path | None = None,
) -> FastAPI:
    from bim_orchestrator.orchestrator import DEFAULT_APPROVALS_DIR, DEFAULT_RUNS_DIR
    from bim_orchestrator.run_recorder import list_runs

    runs_root = runs_root or DEFAULT_RUNS_DIR
    approvals_dir = approvals_dir or DEFAULT_APPROVALS_DIR
    # M2-A: same depth convention as routes_runs.py's _CONFIG_DIR — this file
    # lives at src/bim_orchestrator/service/app.py, so parents[3] resolves to
    # bim-orchestrator/. Overridable (tests point it at a tmp_path config dir
    # so rules/lookup/reference CRUD never touches the real config/).
    config_dir = config_dir or (Path(__file__).resolve().parents[3] / "config")
    app = FastAPI(title="AutoAudit AuditHub", version=_version())
    runner = AuditRunner(runs_root)
    app.state.runner = runner  # exposed for tests

    @app.middleware("http")
    async def _reject_cross_origin_writes(request: Request, call_next):
        # S-01: see _origin_is_local. Middleware, not a dependency, so the
        # guard covers every router (both the root and /api includes) and
        # every route added later — a new POST endpoint is protected by
        # default instead of remembering to opt in.
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not _origin_is_local(origin):
                log.warning(
                    "service.cross_origin_write_rejected",
                    origin=origin,
                    method=request.method,
                    path=request.url.path,
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "cross-origin state-changing requests are "
                        "not allowed (CSRF guard, S-01)"
                    },
                )
        return await call_next(request)

    # B5 (SPEC_PHASE3B_AUTOAUDIT_UI.md): every P3-2 handler lives on ONE
    # APIRouter, included twice below — once unprefixed (keeps the original
    # P3-2 contract: /health, /audits, ...) and once under /api (the AutoAudit
    # UI's only prefix). Handlers/shapes are UNCHANGED — this is a mechanical
    # move from @app.get/@app.post to @p3_router.get/@p3_router.post.
    p3_router = APIRouter()

    @p3_router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        lod_ok, spatial_ok = probe_axes()
        return HealthResponse(
            version=_version(),
            axes=HealthAxes(
                revit=await probe_revit(),
                forma=probe_forma(),
                lod=lod_ok,
                spatial=spatial_ok,
            ),
        )

    @p3_router.post("/audits", response_model=AuditCreated, status_code=202)
    async def create_audit(req: AuditRequest) -> AuditCreated:
        job = runner.start(
            profile_path=req.profile_path, profile_inline=req.profile
        )
        if job is None:
            raise HTTPException(
                status_code=409,
                detail="another audit is already running (single-run lock, D7)",
            )
        return AuditCreated(audit_id=job.audit_id, run_id=job.run_id)

    @p3_router.get("/audits/{audit_id}", response_model=AuditStatus)
    async def audit_status(audit_id: str) -> AuditStatus:
        job = runner.get(audit_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown audit id")
        summary: dict[str, Any] | None = None
        if job.status != "running" and job.run_id:
            meta_path = runs_root / job.run_id / "metadata.json"
            if meta_path.exists():
                try:
                    summary = json.loads(meta_path.read_text(encoding="utf-8")).get(
                        "outcomes_summary"
                    )
                except (OSError, json.JSONDecodeError):
                    summary = None
        return AuditStatus(
            audit_id=job.audit_id,
            status=job.status,  # type: ignore[arg-type]
            phase=job.phase,
            run_id=job.run_id,
            error=job.error,
            summary=summary,
        )

    @p3_router.get("/audits/{audit_id}/events")
    async def audit_events(audit_id: str):
        job = runner.get(audit_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown audit id")

        async def gen():
            async for ev in job.stream_events():
                yield {"event": "progress", "data": json.dumps(ev)}

        return EventSourceResponse(gen())

    @p3_router.get("/runs")
    async def runs() -> list[dict[str, Any]]:
        return list_runs(runs_root)

    def _run_dir(run_id: str) -> Path:
        # run ids are run-<8 hex>; anything else (slashes, dots) is rejected
        # BEFORE touching the filesystem.
        import re

        if not re.fullmatch(r"run-[0-9a-f]{8}", run_id):
            raise HTTPException(status_code=400, detail="invalid run id")
        d = runs_root / run_id
        if not d.is_dir():
            raise HTTPException(status_code=404, detail="unknown run id")
        return d

    def _markdown(run_id: str, filename: str) -> PlainTextResponse:
        p = _run_dir(run_id) / filename
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"{filename} not found")
        return PlainTextResponse(
            p.read_text(encoding="utf-8"), media_type="text/markdown"
        )

    @p3_router.get("/runs/{run_id}/report")
    async def run_report(run_id: str) -> PlainTextResponse:
        return _markdown(run_id, "report.md")

    @p3_router.get("/runs/{run_id}/verification-report")
    async def run_verification_report(run_id: str) -> PlainTextResponse:
        return _markdown(run_id, "verification_report.md")

    @p3_router.get("/runs/{run_id}/delta")
    async def run_delta(run_id: str) -> PlainTextResponse:
        # SPEC_SCHEDULED_AUDIT_DELTA.md W5 — delta.md is written best-effort by
        # write_delta_report (orchestrator._finish_run_recording); _markdown
        # 404s cleanly for a run that predates this feature or had no baseline
        # write succeed. delta.json needs no dedicated endpoint — it's already
        # reachable via GET /runs/{id}/artifacts/delta.json.
        return _markdown(run_id, "delta.md")

    @p3_router.get("/runs/{run_id}/artifacts/{artifact_path:path}")
    async def run_artifact(run_id: str, artifact_path: str) -> FileResponse:
        run_dir = _run_dir(run_id)
        resolved = (run_dir / artifact_path).resolve()
        # Traversal guard: the resolved target must stay inside the run dir.
        if not resolved.is_relative_to(run_dir.resolve()):
            raise HTTPException(status_code=400, detail="path escapes run folder")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(resolved)

    @p3_router.post("/approvals/apply-once", response_model=ApplyOnceResponse)
    async def approvals_apply_once() -> ApplyOnceResponse:
        import anyio

        # D7: the watcher WRITES Revit parameters (Path B) — same document a
        # running audit reads. Shares the ONE single-run lock with POST /audits
        # and verification-views (2026-07 review, SVC-2); a second lock here
        # would split the "who is writing Revit" truth.
        if not runner.lock.acquire():
            raise HTTPException(
                status_code=409,
                detail="another audit or view-creation is in progress (single-run lock, D7)",
            )
        try:
            # apply_approvals_once spins its own loop → run in a worker thread.
            applied, held = await anyio.to_thread.run_sync(
                apply_approvals_once, approvals_dir
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            runner.lock.release()
        return ApplyOnceResponse(applied=applied, held=held)

    # B5: include the P3-2 router twice — root (unchanged contract) + /api
    # (the UI's prefix). M1 routers (routes_runs/approvals/revit) get the
    # same double-include so every new endpoint is reachable at both
    # `/foo` and `/api/foo`, matching the P3-2 endpoints they sit beside.
    # M2-A adds routes_rules/routes_catalogs/routes_builder on the same pattern.
    # M2-C adds routes_settings/routes_extraction, same pattern again.
    from bim_orchestrator.service.routes_approvals import build_approvals_router
    from bim_orchestrator.service.routes_builder import build_builder_router
    from bim_orchestrator.service.routes_catalogs import build_catalogs_router
    from bim_orchestrator.service.routes_extraction import build_extraction_router
    from bim_orchestrator.service.routes_revit import build_revit_router
    from bim_orchestrator.service.routes_rules import build_rules_router
    from bim_orchestrator.service.routes_runs import build_runs_router
    from bim_orchestrator.service.routes_settings import build_settings_router
    from bim_orchestrator.service.spa import mount_spa

    runs_router = build_runs_router(runs_root, runner)
    approvals_router = build_approvals_router(approvals_dir)
    revit_router = build_revit_router()
    rules_router = build_rules_router(config_dir)
    catalogs_router = build_catalogs_router(config_dir)
    builder_router = build_builder_router(config_dir)
    settings_router = build_settings_router(config_dir)
    extraction_router = build_extraction_router(config_dir)

    for r in (
        p3_router, runs_router, approvals_router, revit_router,
        rules_router, catalogs_router, builder_router,
        settings_router, extraction_router,
    ):
        app.include_router(r)
        app.include_router(r, prefix="/api")

    mount_spa(app)

    return app


app = create_app()


def main() -> int:
    """Entry point: ``autoaudit-service``."""
    import os

    import uvicorn
    from dotenv import load_dotenv

    from bim_orchestrator.logging_setup import configure_logging

    # Same as orchestrator.main(): pick up the nearest .env so the pilot's
    # ANTHROPIC_API_KEY / FORMA_* / BIM_* land in os.environ. Handlers read
    # os.environ lazily at request time, so loading here (before uvicorn.run)
    # is enough even though `app` was built at import. Without this, a service
    # launched via `uv run autoaudit-service` (which does NOT auto-load .env)
    # has no key → extraction/draft fail even when Settings shows it "set"
    # (Settings reads the .env FILE; the LLM client reads os.environ).
    load_dotenv()
    configure_logging()
    port = int(os.environ.get("AUTOAUDIT_PORT", DEFAULT_PORT))
    log.info("service.starting", host=DEFAULT_HOST, port=port)
    uvicorn.run(app, host=DEFAULT_HOST, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
