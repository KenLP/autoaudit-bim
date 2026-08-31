"""M1 — run detail / outcomes / trend / profiles / report-export /
verification-views endpoints (Phase 3b).

Service orchestrate-only (D6): every handler below calls an existing engine
module (``run_recorder``, ``report_export``, ``verification_views``,
``policies.audit_profile``) — no new business logic lives here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from bim_orchestrator.run_recorder import diff_outcomes, list_runs
from bim_orchestrator.service._common import resolve_run_dir
from bim_orchestrator.service.models import (
    DiffLatest,
    ExportReportRequest,
    ExportReportResponse,
    ProfileAxes,
    ProfileEntry,
    ProfilesResponse,
    RunArtifacts,
    RunDetailResponse,
    ScheduleOutcome,
    TrendPoint,
    TrendResponse,
    VerificationViewsRequest,
    VerificationViewsResponse,
)
from bim_orchestrator.service.runner import AuditRunner

# Same convention as policies/audit_profile.py's _DEFAULT_CONFIG_DIR: this
# file lives at src/bim_orchestrator/service/routes_runs.py, same depth as
# policies/audit_profile.py, so parents[3] resolves to bim-orchestrator/.
_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

# metadata.json sentinel statuses from run_recorder.list_runs() — a folder
# with no/corrupt metadata carries no outcomes_summary, so it would render
# as a fake all-zero trend point; exclude it rather than mislead the chart.
_SENTINEL_STATUSES = frozenset({"metadata_missing", "metadata_corrupt"})


def _load_outcomes(runs_root: Path, run_id: str) -> dict[str, Any] | None:
    p = runs_root / run_id / "outcomes.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _compliance_pct(summary: dict[str, Any]) -> float:
    total = sum(
        summary.get(k, 0) or 0
        for k in ("compliant", "non_compliant", "manual_review", "missing_data")
    )
    if total <= 0:
        return 0.0
    return round((summary.get("compliant", 0) or 0) / total * 100, 1)


def build_runs_router(runs_root: Path, runner: AuditRunner) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.get("/runs/{run_id}", response_model=RunDetailResponse)
    async def run_detail(run_id: str) -> RunDetailResponse:
        run_dir = resolve_run_dir(runs_root, run_id)
        meta_path = run_dir / "metadata.json"
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {"run_id": run_id, "status": "metadata_corrupt"}
        else:
            metadata = {"run_id": run_id, "status": "metadata_missing"}
        artifacts = RunArtifacts(
            report=(run_dir / "report.md").exists(),
            verification_report=(run_dir / "verification_report.md").exists(),
            trace=(run_dir / "trace.md").exists(),
            axes=(run_dir / "axes").is_dir(),
            report_docx=(run_dir / "verification_report.docx").exists(),
            report_pdf=(run_dir / "verification_report.pdf").exists(),
            delta=(run_dir / "delta.md").exists(),
        )
        return RunDetailResponse(metadata=metadata, artifacts=artifacts)

    @router.get("/runs/{run_id}/outcomes")
    async def run_outcomes(run_id: str) -> JSONResponse:
        run_dir = resolve_run_dir(runs_root, run_id)
        p = run_dir / "outcomes.json"
        if not p.exists():
            raise HTTPException(status_code=404, detail="outcomes.json not found")
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500, detail=f"outcomes.json unreadable: {exc}"
            ) from exc
        return JSONResponse(content=payload)

    @router.get("/trend", response_model=TrendResponse)
    async def trend() -> TrendResponse:
        rows = list_runs(runs_root)
        points: list[TrendPoint] = []
        # Newest-first (list_runs' own order) — the 2 most recent runs WITH a
        # readable outcomes.json are what diff_outcomes compares below.
        recent_outcomes: list[dict[str, Any]] = []
        for r in rows:
            if r.get("status") in _SENTINEL_STATUSES:
                continue
            run_id = r.get("run_id")
            summary = r.get("outcomes_summary") or {}
            points.append(
                TrendPoint(
                    run_id=run_id,
                    started_at=r.get("started_at"),
                    mode=r.get("mode"),
                    compliant=summary.get("compliant", 0) or 0,
                    non_compliant=summary.get("non_compliant", 0) or 0,
                    manual_review=summary.get("manual_review", 0) or 0,
                    missing_data=summary.get("missing_data", 0) or 0,
                    compliance_pct=_compliance_pct(summary),
                )
            )
            if run_id and len(recent_outcomes) < 2:
                oc = _load_outcomes(runs_root, run_id)
                if oc is not None:
                    recent_outcomes.append(oc)
        diff_latest: DiffLatest | None = None
        if len(recent_outcomes) >= 2:
            diff = diff_outcomes(recent_outcomes[1], recent_outcomes[0])
            diff_latest = DiffLatest(
                resolved=len(diff["resolved"]),
                new=len(diff["newly_introduced"]),
                persistent=len(diff["persistent"]),
            )
        return TrendResponse(points=points, diff_latest=diff_latest)

    @router.get("/profiles", response_model=ProfilesResponse)
    async def profiles() -> ProfilesResponse:
        from bim_orchestrator.policies.audit_profile import load_audit_profile

        entries: list[ProfileEntry] = []
        for p in sorted(_CONFIG_DIR.glob("audit.*.yaml")):
            try:
                profile = load_audit_profile(p)
            except ValueError as exc:
                entries.append(ProfileEntry(path=str(p), error=str(exc)))
                continue
            entries.append(
                ProfileEntry(
                    path=str(p),
                    name=profile.name,
                    rules=list(profile.rules),
                    axes=ProfileAxes(
                        lod=profile.axes.lod.enabled,
                        spatial=profile.axes.spatial.enabled,
                    ),
                    mode=profile.run.mode,
                )
            )
        return ProfilesResponse(profiles=entries)

    @router.post("/runs/{run_id}/export-report", response_model=ExportReportResponse)
    async def export_report_endpoint(
        run_id: str, req: ExportReportRequest
    ) -> ExportReportResponse:
        import anyio

        from bim_orchestrator.report_export import export_report

        run_dir = resolve_run_dir(runs_root, run_id)
        md = run_dir / "verification_report.md"
        if not md.exists():
            raise HTTPException(
                status_code=404,
                detail=f"verification_report.md not found for run {run_id}",
            )
        # export_report shells out to pandoc with a 120s ceiling — run it in a
        # worker thread so the event loop (SSE progress, /health) stays live
        # for the duration (2026-07 review, SVC-1).
        out, msg = await anyio.to_thread.run_sync(export_report, md, req.format)
        if out is None:
            # Covers both "pandoc not found" (guidance) and a real pandoc
            # failure — either way the export request itself is well-formed
            # but couldn't be honoured, so 422 with the raw message.
            raise HTTPException(status_code=422, detail=msg)
        return ExportReportResponse(ok=True, artifact=out.name)

    @router.post(
        "/runs/{run_id}/verification-views", response_model=VerificationViewsResponse
    )
    async def verification_views_endpoint(
        run_id: str, req: VerificationViewsRequest
    ) -> VerificationViewsResponse:
        # Lazy + late-bound (called, not captured, at request time) so this
        # endpoint honours the SAME monkeypatch surface as GET /health
        # (`app_module.probe_revit`) — a plain closure-captured reference
        # would freeze whatever probe_revit was AT create_app() time and
        # ignore a test's later monkeypatch.
        from bim_orchestrator.mcp_clients.revit import make_revit_client
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        from bim_orchestrator.service import app as app_module
        from bim_orchestrator.verification_views import (
            create_verification_schedules,
            manifest_dict,
            render_manifest_markdown,
        )

        run_dir = resolve_run_dir(runs_root, run_id)
        trace_path = run_dir / "report_trace.json"
        if not trace_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"report_trace.json not found for run {run_id}",
            )
        if not await app_module.probe_revit():
            raise HTTPException(status_code=503, detail="Revit addin not reachable")
        # D7: view creation also writes into the Revit document — shares the
        # single-run lock with an in-progress audit (and with a concurrent
        # verification-views call).
        if not runner.lock.acquire():
            raise HTTPException(
                status_code=409,
                detail="another audit or view-creation is in progress (single-run lock, D7)",
            )
        try:
            check_trace = json.loads(trace_path.read_text(encoding="utf-8"))
            catalog = OSTCatalog.load()
            async with make_revit_client() as client:
                results = await create_verification_schedules(
                    client, check_trace, catalog=catalog, dry_run=req.dry_run
                )
        finally:
            runner.lock.release()

        (run_dir / "verification_views.json").write_text(
            json.dumps(manifest_dict(results), indent=2, default=str),
            encoding="utf-8",
        )
        (run_dir / "verification_views.md").write_text(
            render_manifest_markdown(results), encoding="utf-8"
        )

        def _outcome(r: Any) -> ScheduleOutcome:
            return ScheduleOutcome(
                rule_id=r.rule_id,
                status=r.status,
                category=r.category_ost,
                schedule_id=r.schedule_id,
                schedule_name=r.schedule_name,
                detail=r.detail,
            )

        created = [
            _outcome(r)
            for r in results
            if r.status in ("created", "created_renamed", "existing")
        ]
        skipped = [_outcome(r) for r in results if r.status in ("degraded", "error")]
        return VerificationViewsResponse(
            ok=not any(r.status == "error" for r in results),
            created=created,
            skipped=skipped,
        )

    return router
