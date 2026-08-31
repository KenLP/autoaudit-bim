"""M2 — Rule Builder endpoints (Phase 3b M2-A): draft / validate / preview /
IDS import-export. Extraction-PDF + settings are M2-C (out of scope here —
SPEC_3B_M2_RULE_BUILDER_NOW.md).

Service orchestrate-only (D6): every handler delegates to
``rule_builder_core`` (draft_rule/validate_rule, B16) or the existing
``policies`` layer (normalize/reference/ids_converter) — no new business
logic lives here.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from bim_orchestrator.policies.rules_schema import RuleSet
from bim_orchestrator.rule_builder_core import (
    LLMNotConfiguredError,
    RuleDraftError,
    draft_rule,
    validate_rule,
)
from bim_orchestrator.service._common import MAX_IDS_BYTES, read_upload_capped
from bim_orchestrator.service.models import (
    BuilderDraftRequest,
    BuilderDraftResponse,
    BuilderPreviewRequest,
    BuilderPreviewResponse,
    BuilderValidateRequest,
    BuilderValidateResponse,
    IdsExportRequest,
    IdsImportResponse,
    ValidationErrorItem,
)


def build_builder_router(config_dir: Path) -> APIRouter:
    router = APIRouter(tags=["builder"])

    @router.post("/builder/draft", response_model=BuilderDraftResponse)
    async def draft(req: BuilderDraftRequest) -> BuilderDraftResponse:
        import anyio

        try:
            # draft_rule calls asyncio.run internally (its Streamlit-era
            # sync surface) — on the event-loop thread BOTH asyncio.run and
            # its new-loop fallback raise "loop already running", so every
            # live-LLM draft died as a 422 (2026-07 review, SVC-5). A worker
            # thread has no ambient loop → asyncio.run works as designed.
            rule, warnings = await anyio.to_thread.run_sync(draft_rule, req.text)
        except LLMNotConfiguredError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc) or "LLM is not configured (missing ANTHROPIC_API_KEY)",
            ) from exc
        except RuleDraftError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return BuilderDraftResponse(rule=rule, warnings=warnings)

    @router.post("/builder/validate", response_model=BuilderValidateResponse)
    async def validate(req: BuilderValidateRequest) -> BuilderValidateResponse:
        # A-03: same signature the save gate uses. Without the ruleset's
        # categories this endpoint could not agree with PUT /rules/{name}
        # even in principle.
        result = validate_rule(
            req.rule, req.is_geometry, ruleset_categories=req.ruleset_categories
        )
        return BuilderValidateResponse(
            ok=result.ok,
            errors=[ValidationErrorItem(field=e.field, message=e.message) for e in result.errors],
            warnings=result.warnings,
        )

    @router.post("/builder/preview", response_model=BuilderPreviewResponse)
    async def preview(req: BuilderPreviewRequest) -> BuilderPreviewResponse:
        from bim_orchestrator.policies.normalize import auto_candidates, normalize_value

        if req.sample is None:
            return BuilderPreviewResponse(output=None, matches=None, error="sample is required")

        kind = req.normalize_kind
        try:
            if kind == "auto":
                if not req.pattern:
                    return BuilderPreviewResponse(
                        output=None, matches=None,
                        error="normalize_kind=auto needs a Pattern for the engine to pick a result",
                    )
                pick = next(
                    (c for c in auto_candidates(req.sample) if re.search(req.pattern, c)), None
                )
                return BuilderPreviewResponse(output=pick, matches=pick is not None)

            if kind == "reference":
                if not req.reference:
                    return BuilderPreviewResponse(
                        output=None, matches=None, error="missing reference set name"
                    )
                from bim_orchestrator.policies.reference import load_reference

                try:
                    ref = load_reference(req.reference, config_dir=config_dir)
                except Exception as exc:
                    return BuilderPreviewResponse(
                        output=None, matches=None,
                        error=f"could not load reference '{req.reference}': {exc}",
                    )
                out = ref.match(req.sample)
                matches = out is not None and out == req.sample
                return BuilderPreviewResponse(output=out, matches=matches)

            out = normalize_value(
                req.sample, kind or "", req.normalize_format,
                req.normalize_map, req.normalize_source,
            )
            matches: bool | None = None
            if req.pattern and out is not None:
                matches = re.fullmatch(req.pattern, out) is not None
            return BuilderPreviewResponse(output=out, matches=matches)
        except Exception as exc:
            return BuilderPreviewResponse(output=None, matches=None, error=str(exc))

    @router.post("/builder/ids-import", response_model=IdsImportResponse)
    async def ids_import(file: UploadFile = File(...)) -> IdsImportResponse:  # noqa: B008
        from bim_orchestrator.policies.ids_converter import ids_xml_to_ruleset

        # S-03: capped read — IDS is small XML; the cap is what keeps
        # "decode the whole thing" a bounded amount of memory.
        raw = await read_upload_capped(file, limit=MAX_IDS_BYTES, what="IDS file")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"file is not UTF-8: {exc}") from exc
        try:
            ruleset, _warnings = ids_xml_to_ruleset(text)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"could not parse IDS: {exc}") from exc
        return IdsImportResponse(
            ruleset=ruleset.model_dump(exclude_none=True), rule_count=len(ruleset.rules)
        )

    @router.post("/builder/ids-export")
    async def ids_export(req: IdsExportRequest) -> Response:
        from bim_orchestrator.policies.ids_converter import ruleset_to_ids_xml

        try:
            ruleset = RuleSet.model_validate(req.ruleset)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        xml_text, _warnings = ruleset_to_ids_xml(ruleset)
        return Response(content=xml_text, media_type="application/xml")

    return router
