"""M2 — rules library CRUD endpoints (Phase 3b M2-A): the Rule Builder's save
target. Backs the ``/rules`` library page + the Builder's SAVE section.

Service orchestrate-only (D6): every handler reads/writes through
``policies.rules_schema.RuleSet`` (the SAME model ``QCAgent`` loads at run
time) and the two ``rule_builder_core`` save-time guards — no new business
logic. There is no dedicated ``rules_schema.load_ruleset`` helper in this
codebase (checked before writing this file); PUT validates by round-tripping
the candidate through YAML text and re-parsing it via ``RuleSet.model_validate``,
which is the same "does this survive the exact bytes we're about to write"
check the spec names.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from bim_orchestrator.policies.rules_schema import RuleSet
from bim_orchestrator.rule_builder_core import (
    enforce_reference_membership,
    enforce_unique_autofix,
    validate_rule,
)
from bim_orchestrator.service.models import (
    PutRuleSetRequest,
    PutRuleSetResponse,
    RuleDetailResponse,
    RuleFileEntry,
    RulesListResponse,
    ValidationErrorItem,
)

# Rules-file name comes straight off the URL path and becomes a filesystem
# component (config/<name>) — allowlist BEFORE it ever touches Path().
# Shape is pinned to rules.<scenario>.yaml: config/ also holds ost_catalog /
# autonomy / param_catalog / lookup.* / reference.* — a looser charset let
# DELETE/PUT reach ANY of them (2026-07 review, SVC-3). list_rules globs the
# same shape, so every name the UI can learn passes this regex.
_NAME_RE = re.compile(r"^rules\.[A-Za-z0-9._-]+\.ya?ml$")

# The 4 requirements the Rule Builder no longer OFFERS but the engine still
# evaluates (CLAUDE.md "Requirement set is consolidated; legacy still
# evaluates", v1.4-K22) — surfaced so the UI can show a read-only banner.
LEGACY_REQUIREMENTS = frozenset({
    "positive_number", "numeric_min", "numeric_min_conditional", "fire_rating_ge",
})


def _validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid rules file name")
    return name


def _categories_of(ruleset: RuleSet) -> list[str]:
    cats: set[str] = set()
    tc = ruleset.target_category
    if isinstance(tc, str):
        if tc:
            cats.add(tc)
    else:
        cats.update(c for c in tc if c)
    for r in ruleset.rules:
        if r.category:
            cats.add(r.category)
    return sorted(cats)


def _summarize_rule_file(path: Path) -> RuleFileEntry:
    # ISO string, not a raw epoch float — the UI (and every other timestamp
    # field in this API) expects an ISO date; a float gets read as epoch-ms
    # and renders as 1970.
    mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ruleset = RuleSet.model_validate(data)
    except Exception as exc:
        return RuleFileEntry(name=path.name, path=str(path), mtime=mtime, error=str(exc))
    return RuleFileEntry(
        name=path.name,
        path=str(path),
        scenario=ruleset.scenario,
        rule_count=len(ruleset.rules),
        categories=_categories_of(ruleset),
        mtime=mtime,
    )


def build_rules_router(config_dir: Path) -> APIRouter:
    router = APIRouter(tags=["rules"])

    @router.get("/rules", response_model=RulesListResponse)
    async def list_rules() -> RulesListResponse:
        files = [_summarize_rule_file(p) for p in sorted(config_dir.glob("rules.*.yaml"))]
        return RulesListResponse(files=files)

    @router.get("/rules/{name}", response_model=RuleDetailResponse)
    async def get_rule(name: str) -> RuleDetailResponse:
        _validate_name(name)
        path = config_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{name} not found")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            ruleset = RuleSet.model_validate(data)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"cannot parse {name}: {exc}"
            ) from exc
        legacy_ids = [r.id for r in ruleset.rules if r.requirement in LEGACY_REQUIREMENTS]
        return RuleDetailResponse(
            ruleset=ruleset.model_dump(exclude_none=True), legacy_rule_ids=legacy_ids,
        )

    @router.put("/rules/{name}", response_model=PutRuleSetResponse)
    async def put_rule(name: str, req: PutRuleSetRequest) -> PutRuleSetResponse:
        _validate_name(name)
        path = config_dir / name
        if path.exists() and not req.overwrite:
            raise HTTPException(
                status_code=409,
                detail=f"{name} already exists (set overwrite=true to replace)",
            )
        # v1.4-K22/QA F2/G7 save-time guards (rule_builder_core, B16) — run on
        # each parameter rule BEFORE schema validation, same order as the
        # Streamlit save path (_save_rule_to_yaml). geometry_rules pass through
        # untouched (the guards only understand parameter-rule shape).
        raw_rules: list[dict[str, Any]] = list(req.ruleset.get("rules") or [])
        guarded_rules = [
            enforce_reference_membership(enforce_unique_autofix(r)) for r in raw_rules
        ]
        candidate = dict(req.ruleset)
        candidate["rules"] = guarded_rules

        # PUT is the FINAL validation authority. `POST /builder/validate` is a
        # UX affordance the client may debounce, race, or never call at all —
        # this endpoint is the only thing standing between a draft and a rules
        # file that QC will execute. Cross-field checks (threshold present for a
        # `>=` compare, pattern present + compilable, scope regex compilable)
        # live in `validate_rule`; the schema below cannot express them, since
        # `threshold`/`pattern` are legitimately optional per requirement.
        # P1-05: pass the enclosing target_category down. A rule that leaves
        # `category` unset inherits the ruleset's targets, and without that the
        # catalog guard looked up an empty category and passed everything.
        ruleset_categories = req.ruleset.get("target_category")
        cross_field = [
            ValidationErrorItem(
                field=f"rules.{idx}.{issue.field}", message=issue.message
            )
            for idx, r in enumerate(guarded_rules)
            for issue in validate_rule(
                r, is_geometry=False, ruleset_categories=ruleset_categories
            ).errors
        ]
        cross_field += [
            ValidationErrorItem(
                field=f"geometry_rules.{idx}.{issue.field}", message=issue.message
            )
            for idx, g in enumerate(req.ruleset.get("geometry_rules") or [])
            for issue in validate_rule(g, is_geometry=True).errors
        ]
        if cross_field:
            raise HTTPException(
                status_code=422, detail=[e.model_dump() for e in cross_field]
            )

        try:
            ruleset = RuleSet.model_validate(candidate)
        except ValidationError as exc:
            errors = [
                ValidationErrorItem(field=".".join(str(p) for p in e["loc"]), message=e["msg"])
                for e in exc.errors()
            ]
            raise HTTPException(
                status_code=422, detail=[e.model_dump() for e in errors]
            ) from exc

        # "validate qua bản dump tạm" — round-trip through the exact YAML text
        # about to be written, so a value that validates in-memory but can't
        # survive a YAML round-trip (e.g. a non-YAML-safe type slipping through
        # a loose dict) is still caught before it reaches disk.
        yaml_text = yaml.safe_dump(
            ruleset.model_dump(exclude_none=True),
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        try:
            RuleSet.model_validate(yaml.safe_load(yaml_text))
        except ValidationError as exc:
            errors = [
                ValidationErrorItem(field=".".join(str(p) for p in e["loc"]), message=e["msg"])
                for e in exc.errors()
            ]
            raise HTTPException(
                status_code=422, detail=[e.model_dump() for e in errors]
            ) from exc

        config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml_text, encoding="utf-8")
        return PutRuleSetResponse(ok=True, path=str(path))

    @router.delete("/rules/{name}")
    async def delete_rule(name: str) -> dict:
        _validate_name(name)
        path = config_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{name} not found")
        path.unlink()
        return {"ok": True}

    return router
