"""M2-C — Settings endpoints (Phase 3b): env allowlist read/write, connection
smoke tests, and the ``doctor`` checklist as JSON.

Service orchestrate-only (D6): env files are read/written here directly (this
IS the settings surface — there's no lower-layer policy module for it), the
Forma/Revit smoke tests shell out to the SAME code paths the CLI/Streamlit
use (``--hello`` subprocess, addin ``/health``), and the doctor checklist
delegates entirely to ``orchestrator.doctor_checks()`` (single source of
truth with the CLI ``--doctor``, P3-5).

Secrets are NEVER returned in full — ``_mask`` shows at most the last 4
characters, and nothing at all below 8 (S-06: the old rule fell back to the
WHOLE value when it was shorter than four, which is the one case where a mask
has to hold). ``PUT /settings/env`` only accepts keys on the allowlist; a
request outside it comes back 403 before anything is written.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

from bim_orchestrator.llm.factory import llm_provider
from bim_orchestrator.policies.audit_profile import load_audit_services
from bim_orchestrator.service.models import (
    DoctorCheckItem,
    DoctorResponse,
    EnvItem,
    LlmStatus,
    PutEnvRequest,
    PutEnvResponse,
    ServicesStatus,
    SettingsResponse,
    TestConnectionResponse,
    TestRevitResponse,
)

# ── allowlist ────────────────────────────────────────────────────────────────

_ALLOW_EXACT = {"ANTHROPIC_API_KEY", "RULES_REMOTE_MANIFEST"}
_ALLOW_PREFIXES = ("FORMA_", "APS_", "BIM_")

# Always surfaced in GET /settings (even when unset, so the UI has fields to
# fill in) — the keys this app actually reads, per streamlit_app/app.py's
# credential wizard + Setup tab. Any OTHER allowlisted key already present in
# os.environ or an .env file is unioned in too (see get_settings below).
_KNOWN_KEYS = (
    "FORMA_MCP_SERVER_CWD",
    "APS_AUTH_MODE",
    "APS_CLIENT_ID",
    "APS_CLIENT_SECRET",
    "ANTHROPIC_API_KEY",
    "BIM_LLM_PROVIDER",
    "BIM_LLM_MODEL",
    "BIM_EXTRACTION_MODEL",
    "BIM_LLM_MAX_CALLS",
    "RULES_REMOTE_MANIFEST",
)


# Env keys are UPPER_SNAKE only. Checked BEFORE the allowlist: a key like
# "BIM_X\nEVIL=1" passes startswith("BIM_") but would smuggle a second,
# non-allowlisted line into the .env file (newline injection — 2026-07
# review, SVC-4). Values get the same no-newline check in put_env.
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_allowlisted(key: str) -> bool:
    return key in _ALLOW_EXACT or key.startswith(_ALLOW_PREFIXES)


# S-06: reveal the tail ONLY when the value is long enough that four characters
# are a fragment of it rather than most of it. Eight is the smallest length at
# which the hint is at most half the secret.
_MASK_TAIL_MIN_LEN = 8


def _mask(value: str) -> str:
    """A recognition hint, never a disclosure.

    The tail exists so an operator can tell *which* key is stored without the
    service handing it back. The old rule — last four characters, or the WHOLE
    value when it was shorter than four — inverted that for exactly the values
    where it matters most: a 3-character secret came back in full, and this
    module's own docstring claimed secrets are "NEVER returned in full".

    Short values are the interesting case, not an edge case. This endpoint
    covers an env allowlist a human types into, so a truncated paste, a
    placeholder, or a stray character IS what lands here — and a wrong value is
    exactly the one an operator will ask the API to show them.

    Below the threshold the answer is bullets alone. Nothing is lost: the
    response already carries ``set``, so "a value is stored" is answered without
    the mask. The bullet run is fixed-width on purpose — a mask that grew with
    the secret would leak its length.
    """
    if len(value) < _MASK_TAIL_MIN_LEN:
        return "••••••••"
    return f"••••{value[-4:]}"


# ── env file plumbing (mirrors streamlit_app/app.py's _write_env_values /
# _write_forma_env, copied — never imported, per the service/streamlit
# boundary) ──────────────────────────────────────────────────────────────────


def _root_env_path(config_dir: Path) -> Path:
    """``config_dir`` is normally ``<bim-orchestrator>/config`` (see
    ``service/app.py:create_app``'s default), so its parent is project root —
    same convention ``orchestrator.PROJECT_ROOT`` uses."""
    return config_dir.parent / ".env"


def _forma_env_path(config_dir: Path) -> Path:
    return config_dir.parent / "vendor" / "forma-mcp" / ".env"


def _target_env_path(key: str, config_dir: Path) -> Path:
    """Which file a key belongs in — ported from the two write helpers in
    streamlit_app/app.py: ``APS_*`` credentials (APS_CLIENT_ID/SECRET/
    AUTH_MODE) feed the forma-mcp SUBPROCESS via ``_write_forma_env``, so they
    live in ``vendor/forma-mcp/.env``. Everything else the allowlist covers —
    including ``FORMA_MCP_SERVER_CWD``, a path the ORCHESTRATOR process itself
    reads — goes through ``_write_env_values`` into the project root ``.env``.
    """
    if key.startswith("APS_"):
        return _forma_env_path(config_dir)
    return _root_env_path(config_dir)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        values[key.strip()] = val.strip()
    return values


def _upsert_env_file(path: Path, key: str, value: str) -> None:
    """Upsert one key=value pair, preserving comments/blank lines/other keys
    (same semantics as the streamlit helpers, generalised to one key at a
    time). Does NOT touch ``os.environ`` — the running process picks up the
    change on its next restart, same as editing the file by hand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written = False
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue
        line_key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if line_key == key:
            result.append(f"{key}={value}")
            written = True
        else:
            result.append(line)
    if not written:
        result.append(f"{key}={value}")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


# ── connection smoke tests (module-level so tests can monkeypatch them) ────


def _test_forma_connection() -> tuple[bool, str]:
    """Same concept as streamlit_app/app.py's ``_test_forma_connection``:
    spawn the CLI ``--hello`` smoke test rather than importing asyncio
    machinery into this long-lived service process."""
    proc = subprocess.run(
        [sys.executable, "-m", "bim_orchestrator.orchestrator", "--hello"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, log


async def _test_revit_connection() -> tuple[bool, str, str | None]:
    """Addin REST ``/health`` — same probe streamlit's ``_test_revit_connection``
    runs, ported to httpx's async client (this handler is async)."""
    import httpx

    from bim_orchestrator.mcp_clients.revit import _load_auth_token, _resolve_port

    version = os.environ.get("REVIT_MCP_VERSION", "2026")
    port = _resolve_port(version)
    token = _load_auth_token(version)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"http://127.0.0.1:{port}/health"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return False, f"No response at {url} — open Revit with RevitMCPServer addin loaded.", None
    except httpx.HTTPStatusError as exc:
        return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}", None
    except Exception as exc:  # noqa: BLE001 - Revit may simply be closed
        return False, str(exc), None

    svc = data.get("service", "revit-mcp-addin")
    ver = data.get("version")
    auth_on = data.get("authEnabled", True)
    return True, f"{svc} v{ver} · port {port} · auth {'on' if auth_on else 'off'}", ver


def build_settings_router(config_dir: Path) -> APIRouter:
    router = APIRouter(tags=["settings"])

    @router.get("/settings", response_model=SettingsResponse)
    async def get_settings() -> SettingsResponse:
        root_values = _parse_env_file(_root_env_path(config_dir))
        forma_values = _parse_env_file(_forma_env_path(config_dir))

        keys = set(_KNOWN_KEYS)
        for source in (os.environ, root_values, forma_values):
            keys.update(k for k in source if _is_allowlisted(k))

        items = []
        for key in sorted(keys):
            value = os.environ.get(key) or root_values.get(key) or forma_values.get(key) or ""
            items.append(EnvItem(key=key, set=bool(value), masked=_mask(value) if value else None))

        services = load_audit_services(config_dir / "audit_services.yaml")
        return SettingsResponse(
            env=items,
            services=ServicesStatus(
                lod=services.available("lod"),
                spatial=services.available("spatial"),
                revitcontrol=services.revitcontrol is not None and services.revitcontrol.exists(),
            ),
            llm=LlmStatus(provider=llm_provider()),
        )

    @router.put("/settings/env", response_model=PutEnvResponse)
    async def put_env(req: PutEnvRequest) -> PutEnvResponse:
        # Shape checks FIRST (see _KEY_RE) — an .env file is line-oriented, so
        # any \r/\n in key OR value writes extra lines the allowlist never saw.
        if not _KEY_RE.fullmatch(req.key):
            raise HTTPException(status_code=400, detail="invalid env key")
        if "\n" in req.value or "\r" in req.value:
            raise HTTPException(
                status_code=400, detail="env value must not contain newlines"
            )
        if not _is_allowlisted(req.key):
            raise HTTPException(
                status_code=403, detail=f"key '{req.key}' is not in the settings allowlist"
            )
        _upsert_env_file(_target_env_path(req.key, config_dir), req.key, req.value)
        return PutEnvResponse(ok=True)

    @router.post("/settings/test/forma", response_model=TestConnectionResponse)
    async def test_forma() -> TestConnectionResponse:
        import asyncio

        try:
            ok, message = await asyncio.to_thread(_test_forma_connection)
        except subprocess.TimeoutExpired:
            return TestConnectionResponse(ok=False, message="--hello timed out after 60s")
        return TestConnectionResponse(ok=ok, message=message)

    @router.post("/settings/test/revit", response_model=TestRevitResponse)
    async def test_revit() -> TestRevitResponse:
        ok, message, version = await _test_revit_connection()
        return TestRevitResponse(ok=ok, message=message, version=version)

    @router.get("/settings/doctor", response_model=DoctorResponse)
    async def doctor() -> DoctorResponse:
        from bim_orchestrator.orchestrator import doctor_checks

        checks = doctor_checks()
        return DoctorResponse(checks=[DoctorCheckItem(**c) for c in checks])

    return router
