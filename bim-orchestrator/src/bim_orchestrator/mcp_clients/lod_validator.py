"""MCP client for the lod-validator satellite (Phase 3, P3-1, D3).

Spawns ``python -m lod_validator.server`` (FastMCP, stdio) using the
satellite's OWN Python 3.10 venv — paths come from
``config/audit_services.yaml`` (``policies/audit_profile.load_audit_services``).
Never import the library: the satellite pins 3.10 + ifcopenshell while this
orchestrator runs >=3.12, and the MCP boundary is absolute.

Envelope contract (validate.py / server.py of that repo):
``validate_lod`` → ``{"schema": "lod-validator/phase0", "required_lod": int,
"summary": {...}, "results": [{"guid", "ifc_type", "name", "tag", ...}]}``
where ``tag`` is the Revit ElementId (used as ``Finding.element_id``) and
``guid`` the IFC GlobalId (kept in details).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Self

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = structlog.get_logger(__name__)

# One generous per-call ceiling — large models take minutes, not seconds.
CALL_TIMEOUT = timedelta(seconds=600)

# D-1 (review round 7, 2026-08-16): same ceiling forma.py carries, for the same
# reason — a satellite venv that spawns but never answers `initialize()` (a
# broken 3.10 venv, a module that crashes while holding its pipes) would
# otherwise hang the nightly `--audit` FOREVER: the CLI has no outer timeout,
# so one mute satellite = silent dead nights until a human notices. Generous
# because a cold venv on a loaded machine imports ifcopenshell slowly; what
# this stops is the infinite wait, not slowness.
_HANDSHAKE_TIMEOUT_S = 60.0


class AuditAxisError(RuntimeError):
    """A satellite axis failed to spawn / transport / answer.

    Carries the axis name so the orchestration layer can degrade that ONE
    axis honestly ("lod: <message>") while the rest of the audit proceeds.
    """

    def __init__(self, axis: str, message: str) -> None:
        super().__init__(f"{axis}: {message}")
        self.axis = axis
        self.message = message


def structured_result(result: Any, *, axis: str, tool: str) -> dict[str, Any]:
    """Extract the dict payload from a FastMCP tool result.

    Prefers ``structuredContent``; falls back to parsing the first text
    content block as JSON (older FastMCP). Anything else → AuditAxisError.
    """
    if result.isError:
        raise AuditAxisError(axis, f"{tool} returned error: {result.content}")
    if getattr(result, "structuredContent", None):
        payload = result.structuredContent
        # FastMCP wraps non-object returns as {"result": ...}; our tools
        # return dicts, but unwrap defensively.
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        if isinstance(payload, dict):
            return payload
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise AuditAxisError(axis, f"{tool} returned no structured payload")


class _StdioVenvClient:
    """Shared async-context plumbing: spawn ``<venv python> -m <module>``."""

    axis = "satellite"
    server_module = ""

    def __init__(self, python_exe: str, cwd: str) -> None:
        self._python_exe = python_exe
        self._cwd = cwd
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self._python_exe,
            args=["-m", self.server_module],
            cwd=self._cwd,
        )
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            # D-1: a spawned-but-mute satellite leaves `initialize()` awaiting
            # forever — see `_HANDSHAKE_TIMEOUT_S`. TimeoutError is an
            # Exception, so it lands in the axis-scoped handler below and the
            # axis degrades honestly instead of the whole audit hanging.
            await asyncio.wait_for(session.initialize(), timeout=_HANDSHAKE_TIMEOUT_S)
        except Exception as exc:  # spawn/transport → axis-scoped, never raw
            # aclose() itself can raise while unwinding a half-open transport;
            # suppress so the ORIGINAL failure is what the caller sees.
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
            raise AuditAxisError(
                self.axis, f"failed to start {self.server_module} ({exc})"
            ) from exc
        except BaseException:
            # Cancellation (BaseException, not Exception) must ALSO reap the
            # spawned venv: `__aenter__` raising means `__aexit__` never runs,
            # so nothing else closes this stack — same shape as forma.py.
            # Re-raise raw: a CancelledError is not an axis failure.
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
            raise
        self._session = session
        log.info(f"{self.axis}_mcp.connected", python=self._python_exe)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError(f"{type(self).__name__} used outside `async with` block")
        try:
            result = await self._session.call_tool(
                tool, arguments, read_timeout_seconds=CALL_TIMEOUT
            )
        except AuditAxisError:
            raise
        except Exception as exc:
            raise AuditAxisError(self.axis, f"{tool} transport failed ({exc})") from exc
        payload = structured_result(result, axis=self.axis, tool=tool)
        log.info(f"{self.axis}_mcp.call", tool=tool)
        return payload


class LODValidatorClient(_StdioVenvClient):
    axis = "lod"
    server_module = "lod_validator.server"

    async def validate_lod(
        self,
        ifc_path: str,
        required_lod: int,
        classes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Full envelope: {schema, required_lod, summary, results[...]}."""
        args: dict[str, Any] = {"ifc_path": ifc_path, "required_lod": required_lod}
        if classes is not None:
            args["classes"] = classes
        return await self._call("validate_lod", args)

    async def emit_bcf(
        self,
        ifc_path: str,
        required_lod: int,
        out_path: str,
        only_failures: bool = True,
    ) -> dict[str, Any]:
        """→ {bcf_path, topics, failures} (.bcfzip written at out_path)."""
        return await self._call(
            "emit_bcf",
            {
                "ifc_path": ifc_path,
                "required_lod": required_lod,
                "out_path": out_path,
                "only_failures": only_failures,
            },
        )


def make_lod_client(services: Any) -> LODValidatorClient | None:
    """None when unconfigured/unavailable — the axis degrades to 'skipped'."""
    entry = getattr(services, "lod_validator", None)
    if entry is None or not entry.exists():
        return None
    return LODValidatorClient(python_exe=entry.python, cwd=entry.cwd)
