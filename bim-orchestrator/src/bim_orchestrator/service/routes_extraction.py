"""M2-C — PDF-extraction endpoint (Phase 3b): regulation/BEP PDF (or .txt) →
one executable RuleSet, via the ``rules_extractor`` sibling package (M13 /
C1.1 seam, ``llm.extraction_bridge``).

Service orchestrate-only (D6): the actual extraction + conversion is
``rules_extractor``'s (a separate package, installed only in dev — see
``rules_extractor_available``); this router adds the SAME re-validation +
grounding checks the Streamlit Setup tab runs (``_validate_extracted_yaml`` /
``_ruleset_grounding_warnings`` in ``streamlit_app/app.py``, ported here
rather than imported — the service must not depend on streamlit). A
schema-invalid extracted scenario is dropped with a warning, never trusted
on ``rules_extractor``'s own "executable" classification. Multiple scenarios
in one document are folded into ONE ruleset via the existing K6
``merge_rulesets`` (the response contract is a single ``ruleset``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from bim_orchestrator.llm.extraction_bridge import (
    ExtractionUnavailable,
    extraction_model,
    make_extraction_client,
    rules_extractor_available,
)
from bim_orchestrator.policies.rules_schema import RuleSet, merge_rulesets
from bim_orchestrator.service._common import max_upload_bytes, stream_upload_to_file
from bim_orchestrator.service.models import ExtractionResponse

log = structlog.get_logger(__name__)

_INSTALL_HINT = (
    "rules-extractor not installed — dev setup: "
    "uv pip install -e <path-to>/ExtractionAgents (then re-sync with "
    "uv sync --extra dev --inexact so uv doesn't remove it again)"
)


def _split_semantically_valid(
    raw_rules: Any, scenario: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep the rules the authoring validator accepts; warn about the rest (L2-19).

    Mirrors the converter's ``_split_by_status``: a rule that fails the semantic
    gate is DROPPED from what we hand back, not silently returned as executable.
    Delegates to the same ``rule_builder_core.validate_rule`` every other
    authoring surface uses — re-typing the checks here is exactly how those
    surfaces drifted apart before.

    Soft-fails to "keep everything" if the validator is unavailable: losing the
    check degrades to the previous behaviour rather than emptying a response.
    """
    rules = list(raw_rules or [])
    try:
        from bim_orchestrator.rule_builder_core import validate_rule
    except Exception:  # pragma: no cover — validator not importable
        return rules, []
    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        try:
            result = validate_rule(rule, is_geometry=False)
        except Exception as exc:  # pragma: no cover — validator misbehaving
            warnings.append(f"scenario '{scenario}': semantic validation failed ({exc})")
            kept.append(rule)
            continue
        if result.errors:
            detail = "; ".join(f"{i.field}: {i.message}" for i in result.errors)
            warnings.append(
                f"scenario '{scenario}': rule '{rule.get('id', '?')}' dropped — {detail}"
            )
            continue
        kept.append(rule)
    return kept, warnings


def _grounding_warnings(ruleset: RuleSet, config_dir: Path) -> list[str]:
    """Per-rule category/parameter grounding against the SAME catalogs the
    live run uses — mirrors ``streamlit_app/app.py:_ruleset_grounding_warnings``
    (copied, not imported: no streamlit dependency here). A rule whose
    category/parameter doesn't resolve runs 0 checks silently at run time;
    surfacing it here turns that into an honest warning instead."""
    from bim_orchestrator.policies.ost_catalog import OSTCatalog
    from bim_orchestrator.policies.param_catalog import load_param_catalog

    try:
        ost_catalog = OSTCatalog.load(config_dir / "ost_catalog.yaml")
    except Exception:  # noqa: BLE001 - catalog missing/invalid -> skip grounding
        return []
    try:
        pcat = load_param_catalog(config_dir=config_dir)
    except Exception:  # noqa: BLE001
        pcat = None

    target = ruleset.target_category
    default_categories = target if isinstance(target, list) else [target]

    warnings: list[str] = []
    for rule in ruleset.rules:
        categories = [rule.category] if rule.category else default_categories
        param_name = rule.bound_parameter or rule.parameter
        cat_resolved = False
        param_resolved = False
        checked_cats: list[str] = []
        for cat in categories:
            checked_cats.append(cat)
            ost = ost_catalog.resolve(cat, backend="revit")
            if ost is None:
                continue
            cat_resolved = True
            specs = pcat.params_for(ost) if pcat else []
            if not specs or any(s.name == param_name for s in specs):
                param_resolved = True
        if not cat_resolved:
            warnings.append(
                f"rule {rule.id}: category '{', '.join(checked_cats)}' không có trong "
                "OST catalog — rule sẽ chạy 0 check."
            )
        elif not param_resolved:
            warnings.append(
                f"rule {rule.id}: parameter '{param_name}' không có trong param_catalog "
                f"của category '{', '.join(checked_cats)}' — có thể chạy 0 check."
            )
    return warnings


def _run_extraction(
    tmp_path: str, *, tables: bool, client: Any, max_sections: int | None = None
) -> tuple[Any, list[Any]]:
    """The actual rules_extractor call — module-level so tests can monkeypatch
    it directly instead of installing/stubbing the real package.

    ``max_sections`` caps the breadth pass (the first N heading-aligned
    sections) — a big document runs ALL its sections by default (minutes on a
    few-hundred-page standard); a cap turns a live demo into a ~20-30s segment
    (the uncapped run stays the golden). Sections past the cap are recorded as
    ``skipped`` in coverage, not dropped silently.

    Returns ``(convert_result, coverage)``. Coverage is threaded back out so the
    handler can tell "the document genuinely had no executable rules" apart from
    "every section's LLM call failed" (billing / auth / rate-limit) — otherwise
    an upstream API failure is silently reported as an empty document."""
    from rules_extractor import convert_envelope, extract_sections, load_contract

    contract = load_contract()
    envelope, coverage = extract_sections(
        tmp_path, client=client, model=extraction_model(), tables=tables,
        contract=contract, max_sections=max_sections,
    )
    return convert_envelope(envelope, contract=contract), coverage


def _coverage_errors(coverage: list[Any]) -> list[str]:
    """Distinct per-section error messages (order-preserving), for the honest
    502 below. Empty when no section errored."""
    seen: set[str] = set()
    out: list[str] = []
    for c in coverage:
        err = (getattr(c, "error", None) or "").strip()
        if getattr(c, "status", None) == "error" and err and err not in seen:
            seen.add(err)
            out.append(err)
    return out


def build_extraction_router(config_dir: Path) -> APIRouter:
    router = APIRouter(tags=["extraction"])

    @router.post("/extraction/pdf", response_model=ExtractionResponse)
    async def extraction_pdf(
        file: UploadFile = File(...),  # noqa: B008
        max_sections: int | None = Form(None),  # noqa: B008
    ) -> ExtractionResponse:
        if not rules_extractor_available():
            raise HTTPException(status_code=503, detail=_INSTALL_HINT)
        if max_sections is not None and max_sections < 1:
            raise HTTPException(status_code=422, detail="max_sections must be >= 1")

        from bim_orchestrator.llm.usage import UsageRecorder

        recorder = UsageRecorder()
        try:
            client = make_extraction_client(recorder=recorder)
        except ExtractionUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        suffix = Path(file.filename or "upload").suffix.lower() or ".txt"
        # S-03: stream to the temp file in bounded chunks. The document is
        # going to disk anyway (rules_extractor takes a path), so buffering
        # it whole in RAM first bought nothing and had no ceiling.
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tmp_path = tf.name
            too_big: HTTPException | None = None
            try:
                await stream_upload_to_file(
                    file, tf, limit=max_upload_bytes(), what="document",
                    hint="AUTOAUDIT_MAX_UPLOAD_MB raises it",
                )
            except HTTPException as exc:
                too_big = exc
        if too_big is not None:
            # Unlink only AFTER the `with` closed the handle — on Windows
            # deleting a still-open file raises PermissionError, which would
            # replace the honest 413 with a 500.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise too_big

        try:
            # Extraction runs synchronous (rules_extractor-internal-threaded)
            # LLM calls that can take minutes on a big PDF — offload to a
            # worker thread so it doesn't block the service's single event
            # loop, and cap it at 300s (spec) rather than hang forever.
            result, coverage = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_extraction, tmp_path, tables=False, client=client,
                    max_sections=max_sections,
                ),
                timeout=300,
            )
        except TimeoutError as exc:
            # S-04: wait_for abandons the AWAIT, but the worker thread lives
            # on — and `rules_extractor` would keep fanning sections out,
            # billing every one for a result nobody will read. The thread
            # can't be killed; the SPENDING can be stopped. cancel() makes
            # the seam client refuse every further emit_ruleset, so the
            # orphan unwinds at its next call boundary instead of running the
            # document to the end.
            client.cancel()
            log.warning(
                "extraction.timed_out",
                detail="cancelled the seam client; the orphaned worker stops "
                       "at its next LLM call boundary",
            )
            raise HTTPException(
                status_code=504, detail="extraction timed out after 300s"
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"extraction failed: {exc}") from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError as unlink_exc:
                # On Windows the orphaned worker still holds the document open
                # after a timeout, so this legitimately fails — log it rather
                # than swallow, otherwise a leaking temp dir is invisible.
                log.warning(
                    "extraction.temp_cleanup_failed",
                    path=tmp_path, error=str(unlink_exc),
                )

        warnings: list[str] = []
        rulesets: list[RuleSet] = []
        for scenario in result.scenarios:
            if not scenario.rules_yaml:
                continue
            try:
                data = yaml.safe_load(scenario.rules_yaml) or {}
                ruleset = RuleSet.model_validate(data)
            except Exception as exc:  # noqa: BLE001 - never trust the extractor's own classification
                warnings.append(
                    f"scenario '{scenario.scenario}': schema invalid, skipped ({exc})"
                )
                continue
            # L2-19: the schema check above is not the authoring gate. P1-AUTHOR-01
            # put a SEMANTIC gate (rule_builder_core.validate_rule) on every
            # authoring surface — the Rule Builder, PUT /rules/{name}, the IDS
            # import and the converter — precisely so "valid rule" means one
            # thing everywhere. This route was missed, so a rule that pydantic
            # accepts but the validator rejects came back looking executable and
            # only failed later at the PUT. The cases that matter here are
            # exactly the ones an extraction model produces: a `matches_regex`
            # rule with no pattern, a pattern that will not compile, an empty
            # parameter name. Harmless in outcome — the PUT gate still refuses
            # them — and wrong in the place that matters: a document-import
            # surface telling the author their rules are fine.
            data_rules = data.get("rules") if isinstance(data, dict) else None
            kept, dropped = _split_semantically_valid(data_rules, scenario.scenario)
            warnings.extend(dropped)
            if not kept:
                continue
            if len(kept) != len(data_rules or []):
                ruleset = RuleSet.model_validate({**data, "rules": kept})
            warnings.extend(_grounding_warnings(ruleset, config_dir))
            rulesets.append(ruleset)

        if not rulesets:
            # Distinguish "the document genuinely had no executable rules" (422,
            # a document problem) from "every section's LLM call failed" (502, an
            # upstream problem — billing / auth / rate-limit). Surfacing the real
            # API message instead of a misleading "no rules" was found while
            # smoke-testing real spec PDFs, 2026-07-15.
            errors = _coverage_errors(coverage)
            if errors and not result.scenarios:
                detail = errors[0]
                if len(errors) > 1:
                    detail += f" (+{len(errors) - 1} more distinct errors)"
                raise HTTPException(
                    status_code=502, detail=f"extraction LLM call failed: {detail}"
                )
            raise HTTPException(
                status_code=422, detail="no executable rules extracted from this document"
            )

        merged = rulesets[0] if len(rulesets) == 1 else merge_rulesets(rulesets)
        return ExtractionResponse(ruleset=merged.model_dump(exclude_none=True), warnings=warnings)

    return router
