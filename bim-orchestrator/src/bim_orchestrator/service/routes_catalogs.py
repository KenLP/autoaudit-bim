"""M2 — catalogs endpoints (Phase 3b M2-A): categories / params / lookups /
references, backing the Rule Builder's SCOPE + ACTION sections.

Service orchestrate-only (D6): every handler reads through the existing
``policies/`` layer (``ost_catalog``, ``param_catalog``, ``lookup_table``,
``reference``) or ``rule_builder_core`` (``CATEGORY_NOTES`` / ``INTENT_ALIASES``)
— no new business logic lives here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query

from bim_orchestrator.rule_builder_core import CATEGORY_NOTES, INTENT_ALIASES
from bim_orchestrator.service.models import (
    CategoriesResponse,
    CategoryEntry,
    LookupEntry,
    LookupKeyOut,
    LookupRowOut,
    LookupsResponse,
    OkResponse,
    ParamEntry,
    ParamsResponse,
    PutLookupRequest,
    PutReferenceRequest,
    ReferenceEntryOut,
    ReferencesResponse,
)

# Same allowlist as routes_rules.py's rules-file name guard — a lookup/
# reference set name is user-authored (Rule Builder "create new table" flow)
# and becomes a filename component, so it's validated BEFORE touching disk.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid table name")
    return name


def build_catalogs_router(config_dir: Path) -> APIRouter:
    router = APIRouter(tags=["catalogs"])

    @router.get("/catalogs/categories", response_model=CategoriesResponse)
    async def categories() -> CategoriesResponse:
        from bim_orchestrator.policies.ost_catalog import OSTCatalog

        try:
            catalog = OSTCatalog.load()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"cannot load OST catalog: {exc}") from exc
        entries = [
            CategoryEntry(key=e.key, label=e.display, note=CATEGORY_NOTES.get(e.display))
            for e in catalog.entries
        ]
        entries.sort(key=lambda c: c.label)
        return CategoriesResponse(categories=entries)

    @router.get("/catalogs/params", response_model=ParamsResponse)
    async def params(category: str = Query(...)) -> ParamsResponse:
        from bim_orchestrator.policies.ost_catalog import OSTCatalog
        from bim_orchestrator.policies.param_catalog import load_param_catalog

        try:
            ost = OSTCatalog.load().resolve(category, backend="revit")
        except Exception:
            ost = None
        if ost is None:
            return ParamsResponse(params=[], aliases={})
        try:
            pcat = load_param_catalog()
        except Exception:
            return ParamsResponse(params=[], aliases={})
        specs = pcat.params_for(ost)
        param_entries = [
            ParamEntry(
                name=s.name, storage=s.storage, binding=s.binding,
                writable=s.is_write_target, dimension=s.dimension,
            )
            for s in specs
        ]
        return ParamsResponse(params=param_entries, aliases=dict(INTENT_ALIASES.get(ost, {})))

    @router.get("/catalogs/lookups", response_model=LookupsResponse)
    async def lookups() -> LookupsResponse:
        from bim_orchestrator.policies.lookup_table import load_lookup

        out: list[LookupEntry] = []
        for p in sorted(config_dir.glob("lookup.*.yaml")):
            name = p.stem[len("lookup."):]
            try:
                table = load_lookup(name, config_dir=config_dir)
            except Exception:
                continue  # corrupt/unreadable table — skip (mirrors GET /rules' error tolerance)
            out.append(LookupEntry(
                name=table.name,
                description=table.description,
                keys=[LookupKeyOut(param=k.param, dimension=k.dimension) for k in table.keys],
                rows=[LookupRowOut(when=r.when, require=r.require) for r in table.rows],
            ))
        return LookupsResponse(lookups=out)

    @router.put("/catalogs/lookups/{name}", response_model=OkResponse)
    async def put_lookup(name: str, req: PutLookupRequest) -> OkResponse:
        from bim_orchestrator.policies import lookup_table as lt

        _validate_name(name)
        path = config_dir / f"lookup.{name}.yaml"
        if path.exists() and not req.overwrite:
            raise HTTPException(
                status_code=409,
                detail=f"lookup.{name}.yaml already exists (set overwrite=true to replace)",
            )
        data: dict = {"name": name}
        if req.description:
            data["description"] = req.description
        data["keys"] = [k.model_dump() for k in req.keys]
        data["rows"] = [r.model_dump() for r in req.rows]
        try:
            lt.LookupTable.model_validate(data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        lt.clear_cache()
        return OkResponse(ok=True)

    @router.get("/catalogs/references", response_model=ReferencesResponse)
    async def references() -> ReferencesResponse:
        from bim_orchestrator.policies.reference import load_reference

        out: list[ReferenceEntryOut] = []
        for p in sorted(config_dir.glob("reference.*.yaml")):
            name = p.stem[len("reference."):]
            try:
                ref = load_reference(name, config_dir=config_dir)
            except Exception:
                continue
            out.append(ReferenceEntryOut(
                name=ref.name,
                entries=[e.model_dump() for e in ref.entries],
                case_sensitive=ref.case_sensitive,
            ))
        return ReferencesResponse(references=out)

    @router.put("/catalogs/references/{name}", response_model=OkResponse)
    async def put_reference(name: str, req: PutReferenceRequest) -> OkResponse:
        from bim_orchestrator.policies import reference as ref_mod

        _validate_name(name)
        path = config_dir / f"reference.{name}.yaml"
        if path.exists() and not req.overwrite:
            raise HTTPException(
                status_code=409,
                detail=f"reference.{name}.yaml already exists (set overwrite=true to replace)",
            )
        data = {"name": name, "case_sensitive": req.case_sensitive, "entries": req.entries}
        try:
            ref_mod.ReferenceSet.model_validate(data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        ref_mod.clear_cache()
        return OkResponse(ok=True)

    return router
