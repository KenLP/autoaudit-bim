"""MCP client wrapper for acc-forma-mcp-server.

Spawns the server via stdio and exposes typed convenience methods for the
tools the orchestrator uses in Phase 1. Phase 2 will add Revit MCP alongside.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bim_orchestrator.mcp_clients._server_args import split_server_args

log = structlog.get_logger(__name__)

# Ceiling on the stdio handshake (spawn → `initialize()` reply). Generous: a
# cold SEA exe on a loaded machine is slow, and a false timeout here aborts a
# legitimate audit. What it exists to stop is the INFINITE wait — see the
# `__aenter__` comment.
_HANDSHAKE_TIMEOUT_S = 60.0

# bim-orchestrator/ root — used for vendor/ discovery.
_APP_ROOT = Path(__file__).parent.parent.parent.parent


def _vendor_exe(server_name: str) -> str | None:
    """Return path to vendor/<server_name>/forma-mcp.exe if it exists (SEA build)."""
    candidate = _APP_ROOT / "vendor" / server_name / "forma-mcp.exe"
    return str(candidate) if candidate.exists() else None


def launch_target(config: FormaMCPConfig | None = None) -> tuple[bool, str]:
    """Can a Forma MCP server actually be started? → ``(ok, detail)``.

    The single source of truth behind the UI's Forma dot
    (``service/app.probe_forma``) and ``--doctor``. Both used to ask whether
    ``vendor/forma-mcp/forma-mcp.exe`` exists, which is only ONE of the two
    ways this server runs: with no exe present the client falls back to
    ``node dist/index.js``. That made every non-Windows install report Forma
    missing while it was in fact connected (found 2026-09-13 on macOS).

    Asks :meth:`FormaMCPConfig.from_env` — the resolver the client itself
    uses — then checks the two things a subprocess needs: the command
    resolves, and its entrypoint exists. ``detail`` is a human line for the
    doctor table.

    "Launchable" deliberately does NOT mean "credentials are valid": that is
    what the Setup page's *Test Forma* answers.
    """
    import shutil

    if config is None:
        try:
            config = FormaMCPConfig.from_env()
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"config error: {exc}"

    command = Path(config.command)
    if command.is_absolute() or len(command.parts) > 1:
        if not command.is_file():
            return False, f"command not found: {config.command}"
        resolved_command = str(command)
    else:
        found = shutil.which(config.command)
        if found is None:
            return False, f"command not on PATH: {config.command}"
        resolved_command = found

    # No args → the command IS the server (the SEA exe), checked above.
    if not config.args:
        return True, f"SEA exe: {resolved_command}"

    # Otherwise args[0] is the entrypoint script; a relative one resolves
    # against cwd, exactly as the subprocess will resolve it.
    entrypoint = Path(config.args[0])
    if not entrypoint.is_absolute() and config.cwd:
        entrypoint = Path(config.cwd) / entrypoint
    if not entrypoint.is_file():
        return False, f"entrypoint not found: {entrypoint}"
    return True, f"{resolved_command} {entrypoint}"


def _vendor_cwd(server_name: str) -> str | None:
    """Return vendor/<server_name> path if its dist/index.js exists there.

    Portable layout: ship pre-built MCP servers under vendor/ so users don't
    need to clone + build them separately.
    """
    candidate = _APP_ROOT / "vendor" / server_name
    return str(candidate) if (candidate / "dist" / "index.js").exists() else None


# ── browse: parsing the AECDM list tools' TEXT output ───────────────────────
#
# The AECDM list tools (aecdm_list_hubs / aecdm_list_projects /
# aecdm_list_element_groups) return a list[TextContent] whose ``.text`` is a
# human-readable table, NOT JSON. Turning that back into data is this client's
# job, not a UI's — the two parsers below moved here from
# ``streamlit_app/app.py`` (v1.7-R25) so the Streamlit picker and the web UI's
# Setup card read ONE parser. Both shapes documented here were recorded live
# against the running server on the dates given.


@dataclass(frozen=True)
class FormaNamed:
    """An id/name pair — a hub, or an element group (what a user calls a
    "model")."""

    id: str
    name: str


@dataclass(frozen=True)
class FormaProject:
    """One ACC/Forma project and BOTH of its ids.

    ``aecdm_id`` (``urn:adsk.workspace:prod.project:<uuid>``) drives element
    queries; ``dm_id`` (``b.<uuid>``) drives ``issues_*`` / ``dm_*`` /
    ``reviews_*``. ``dm_id`` is ``""`` when a project has no linked DM/Issues
    container — callers keep whatever DM id they already had rather than
    blanking it.
    """

    name: str
    aecdm_id: str
    dm_id: str


_ID_RE = re.compile(r"\(ID:\s*([^)]+)\)")
# Leading decoration + whitespace ahead of the display name. Three characters
# beyond the emoji blocks the Streamlit class covered, each one measured
# against the LIVE server rather than guessed:
#   ︎ ️  the variation selectors. An emoji in presentation form
#          (🏗️ = U+1F3D7 U+FE0F) matched only its FIRST codepoint, leaving a
#          stray selector glued to the name.
#   •      the list bullet. U+2022 sits BELOW the U+2600 block, so a hub or
#          element group returned as "• Autodesk APAC TS  (ID: ...)" — the
#          actual live shape, seen 2026-09-05 — kept its bullet and the
#          picker read "• Autodesk APAC TS".
# All three are decoration; none is ever the first character of a real display
# name, so widening the class cannot swallow one.
_EMOJI_RE = re.compile(r"^[\U00010000-\U0010ffff\U00002600-\U000027ff︎️•\s\xa0]+")


def parse_name_id_list(raw: object) -> list[FormaNamed]:
    """Parse an AECDM list response into ``FormaNamed`` entries.

    Live format observed 2026-06-11::

        Found N AEC project(s):

        🏢 Project Name  (ID: urn:adsk.workspace:prod.project:<uuid>)

    The ``(ID: <value>)`` pattern is the anchor; whatever precedes it on the
    same line is the display name, emoji stripped. A line carrying an id but
    no name falls back to the first 40 characters of the id, so an entry is
    never dropped just for being unnamed.
    """
    out: list[FormaNamed] = []
    for item in raw if isinstance(raw, list) else [raw]:
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        for line in text.splitlines():
            m = _ID_RE.search(line)
            if not m:
                continue
            eid = m.group(1).strip()
            name = _EMOJI_RE.sub("", line[: m.start()]).strip()
            out.append(FormaNamed(id=eid, name=name or eid[:40]))
    return out


def parse_aecdm_projects(raw: object) -> list[FormaProject]:
    """Parse ``aecdm_list_projects`` into ``FormaProject`` entries.

    Forma's dual-id format (observed 2026-06-19) returns BOTH project ids per
    project, so ONE call resolves the AECDM URN *and* the DM/Issues id — no
    name-matching, no hard-coded id. Live shape::

        • <name>
            AECDM id: urn:adsk.workspace:prod.project:<uuid>
            DM/Issues id: b.<uuid>
    """
    out: list[FormaProject] = []
    name: str | None = None
    aecdm_id = ""
    dm_id = ""

    def _flush() -> None:
        nonlocal name, aecdm_id, dm_id
        if name and aecdm_id:
            out.append(FormaProject(name=name, aecdm_id=aecdm_id, dm_id=dm_id))
        name, aecdm_id, dm_id = None, "", ""

    for item in raw if isinstance(raw, list) else [raw]:
        text = getattr(item, "text", None)
        if text is None:
            text = str(item)
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("•"):  # a new project block starts
                _flush()
                name = s.lstrip("•").strip()
            elif s.startswith("AECDM id:"):
                aecdm_id = s.split(":", 1)[1].strip()
            elif s.startswith("DM/Issues id:"):
                dm_id = s.split(":", 1)[1].strip()
    _flush()
    return out


@dataclass
class FormaMCPConfig:
    command: str
    args: list[str]
    cwd: str | None
    env: dict[str, str]

    @classmethod
    def from_env(cls) -> Self:
        # SEA exe takes priority when no explicit CWD env var is set.
        # The exe's parent dir becomes cwd so dotenv/config finds vendor/forma-mcp/.env
        # (same dir the SSA credential wizard writes to).
        env_cwd = os.environ.get("FORMA_MCP_SERVER_CWD")
        if not env_cwd:
            exe = _vendor_exe("forma-mcp")
            if exe:
                cwd = str(Path(exe).parent)
                log.debug("forma_mcp.exe_detected", exe=exe)
                return cls(command=exe, args=[], cwd=cwd, env={})

        command = os.environ.get("FORMA_MCP_SERVER_CMD", "node")
        # cwd: explicit env var → vendor/ dist/index.js auto-detect → None
        cwd = env_cwd or _vendor_cwd("forma-mcp") or None
        raw_args = os.environ.get("FORMA_MCP_SERVER_ARGS", "")
        if not raw_args and cwd:
            raw_args = "dist/index.js"  # vendor default entrypoint
        args = split_server_args(raw_args)
        # We deliberately pass NO APS_* / FORMA_* secrets through. The subprocess
        # loads them from its own .env (located at cwd) via `dotenv/config`.
        # Only PATH-like vars needed to launch node are inherited automatically.
        env: dict[str, str] = {}
        if cwd:
            log.debug("forma_mcp.cwd_resolved", cwd=cwd, source="env" if env_cwd else "vendor")
        return cls(command=command, args=args, cwd=cwd, env=env)


class FormaMCPClient:
    """Async context manager over an MCP stdio session to acc-forma-mcp-server."""

    def __init__(self, config: FormaMCPConfig) -> None:
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        params_kwargs: dict[str, Any] = {
            "command": self._config.command,
            "args": self._config.args,
            "env": self._config.env,
        }
        if self._config.cwd is not None:
            params_kwargs["cwd"] = self._config.cwd
        params = StdioServerParameters(**params_kwargs)
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            # A spawned-but-mute server (a crashed exe still holding its pipes,
            # a bad .env) leaves `initialize()` awaiting forever. In the CLI that
            # is a visible hang; under the AuditHub service it is a job that
            # never finishes and never releases the single-run lock, so every
            # later POST /audits 409s — a nightly that fails silently for days.
            await asyncio.wait_for(session.initialize(), timeout=_HANDSHAKE_TIMEOUT_S)
        except BaseException:
            # An `__aenter__` that raises means `__aexit__` never runs, so
            # nothing else will close this stack: the child process and the
            # anyio reader tasks leak. Long-lived hosts (the service) accumulate
            # one per failed attempt. Same shape as `lod_validator._StdioVenvClient`.
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
            raise
        self._session = session
        log.info("forma_mcp.connected", command=self._config.command)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("FormaMCPClient used outside `async with` block")
        return self._session

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self.session.call_tool(tool, arguments or {})
        log.info("forma_mcp.call", tool=tool, is_error=result.isError)
        if result.isError:
            raise RuntimeError(f"MCP tool {tool} returned error: {result.content}")
        return result.content

    async def call_structured(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Same as `call` but returns the tool's structuredContent (always a dict)."""
        result = await self.session.call_tool(tool, arguments or {})
        log.info("forma_mcp.call", tool=tool, is_error=result.isError)
        if result.isError:
            raise RuntimeError(f"MCP tool {tool} returned error: {result.content}")
        return result.structuredContent or {}

    # --- Convenience wrappers for Phase 1 tools ---

    async def list_aecdm_hubs(self) -> Any:
        return await self.call("aecdm_list_hubs", {})

    async def list_aecdm_projects(self, hub_id: str) -> Any:
        return await self.call("aecdm_list_projects", {"hub_id": hub_id})

    async def list_element_groups(self, project_id: str) -> Any:
        return await self.call("aecdm_list_element_groups", {"project_id": project_id})

    # --- browse (the project picker's two reads) ---

    async def browse_projects(self) -> tuple[FormaNamed, list[FormaProject]]:
        """``(hub, projects)`` for a project picker.

        Resolves the AECDM hub URN first: the DM hub id kept in ``.env`` is a
        ``b.<uuid>``, which the AECDM API rejects. The FIRST hub wins — the
        same choice the Streamlit picker made, and callers surface the hub's
        name so the user can see which one answered. Choosing among several
        hubs is a deliberate follow-up, not an oversight.
        """
        hubs = parse_name_id_list(await self.list_aecdm_hubs())
        if not hubs:
            raise RuntimeError("No AECDM hubs found — check Forma MCP connection.")
        hub = hubs[0]
        return hub, parse_aecdm_projects(await self.list_aecdm_projects(hub.id))

    async def browse_element_groups(self, aecdm_project_id: str) -> list[FormaNamed]:
        """The models ("element groups") inside one AECDM project."""
        return parse_name_id_list(await self.list_element_groups(aecdm_project_id))

    async def query_elements(
        self,
        element_group_id: str,
        category: str,
    ) -> list[dict[str, Any]]:
        """Return elements as a list of dicts with id, name, properties[{name,value}]."""
        structured = await self.call_structured(
            "aecdm_query_elements",
            {
                "element_group_id": element_group_id,
                "category": category,
            },
        )
        return list(structured.get("elements", []))

    async def get_element_properties(
        self, element_group_id: str, element_id: str, category: str
    ) -> Any:
        return await self.call(
            "aecdm_get_element_properties",
            {
                "element_group_id": element_group_id,
                "element_id": element_id,
                "category": category,
            },
        )

    # v1 task B-3: candidate property names that might carry Forma Viewer dbId
    # in the AECDM `properties` array. Order matters -- first hit wins.
    #
    # Live probe on 2026-06-01 (Sample ACC Project project, 30-room model):
    # AECDM exposes "External ID" and "Revit Element ID" as flat property
    # names with spaces. Both are Revit-side identifiers (UniqueId GUID
    # and integer ElementId respectively); the "Revit Element ID" int OFTEN
    # matches Forma Viewer dbId for Revit-sourced models (the SVF2
    # translator typically preserves Revit's element index) but this is
    # heuristic, not guaranteed. ACC server-side will reject mismatches.
    #
    # If none of these appear we return empty; caller logs and proceeds
    # without the "View in Model" deep link (degraded UX, not a hard failure).
    _DB_ID_KEYS: tuple[str, ...] = (
        "Revit Element ID",   # most reliable for Revit-sourced models
        "RevitElementId",
        "objectId", "ObjectId", "dbId", "DbId",
        "Forma.objectId", "viewer.objectId",
    )

    # Same list, narrowed to UniqueId-like keys. Surfaced separately so the
    # issue body's Reference section can show the Revit UniqueId even when
    # we don't have a dbId for linked_documents.
    _UNIQUE_ID_KEYS: tuple[str, ...] = (
        "External ID",        # live AECDM property name (Revit UniqueId)
        "externalId", "ExternalId", "external_id",
        "UniqueId", "unique_id",
        "IfcGUID",
    )

    async def get_object_id_map(
        self, element_group_id: str, elements: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Return {element_id (AECDM URN): objectId (Forma Viewer dbId)}.

        v1 task B-3 (R1b in PHASE2_ROADMAP):
        ACC Issues `linked_documents[].details.objectId` is a Forma Viewer
        dbId, NOT a Revit UniqueId. We need to map. This helper scans each
        element's properties for any of the canonical externalId / dbId
        field names that might be exposed by the AECDM "properties" array.

        If the current acc-forma-mcp-server does NOT yet expose these on
        `aecdm_query_elements` output, the function returns an empty dict
        and the caller (DesignAgent) gracefully ships issues without
        objectId (the issue still lands in ACC, just without the native
        "View in Model" button). DO NOT crash here -- the v1 contract is
        "best effort", with full mapping landing once the MCP server
        either exposes externalId or adds a model derivative /properties
        tool (see PHASE2_ROADMAP Sec.9 R1b for the longer plan).
        """
        out: dict[str, int] = {}
        for el in elements:
            element_id = el.get("id")
            if not element_id:
                continue
            # First pass: inline params on the element (from query_elements).
            params: dict[str, Any] = el.get("params") or {}
            db_id = _first_int(params, self._DB_ID_KEYS)
            if db_id is not None:
                out[element_id] = db_id
        return out


    async def list_issue_subtypes(self, project_id: str) -> list[dict[str, Any]]:
        """Return flat list of {id, title, type_id, type_title, is_active} for all issue subtypes.

        `is_active` reflects APS's isActive flag — inactive subtypes are rejected
        by POST /issues, so callers should filter on this.
        """
        structured = await self.call_structured(
            "issues_list_types", {"project_id": project_id}
        )
        flat: list[dict[str, Any]] = []
        for t in structured.get("types", []):
            for s in t.get("subtypes", []):
                flat.append(
                    {
                        "id": s.get("id"),
                        "title": s.get("title"),
                        "type_id": t.get("id"),
                        "type_title": t.get("title"),
                        "is_active": bool(s.get("isActive", True)),
                    }
                )
        return flat

    async def create_issue(
        self,
        project_id: str,
        *,
        title: str,
        issue_subtype_id: str,
        description: str | None = None,
        assigned_to: str | None = None,
        assigned_to_type: str | None = None,
        published: bool = False,
        status: str = "open",
        dry_run: bool = True,
        approval_token: str | None = None,
        linked_documents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create an ACC Issue. Uses dry-run + approval token guardrail pipeline.

        Returns the structured response dict. When dry_run=True, contains
        approval_token, preview body, sideEffects. When dry_run=False, contains
        the created issue under key "issue".

        v1 task B (R1 resolved 2026-05-31 by acc-forma-mcp-server team): when
        ``linked_documents`` is provided, the ACC Issues UI renders a native
        "View in Model" button and the Revit Issues add-in highlights the
        element automatically. Schema (snake_case top-level, camelCase inner --
        APS-passthrough):

            [
              {
                "type": "ThreeDVectorPushpin",            # or TwoDVectorPushpin
                "urn":  "urn:adsk.wipprod:dm.lineage:<id>",
                "createdAtVersion": 7,
                "details": {
                  "viewable": {"guid": "<3d-view-guid>", "is3D": True},
                  "objectId": 4401618,                     # Forma Viewer dbId
                  "position": {"x": 1.2, "y": 3.4, "z": 0.0},  # optional
                  "viewerState": {...}                     # optional, passthrough
                }
              },
              ...
            ]

        Max array length 50. The `objectId` is a Forma Viewer dbId, NOT a
        Revit UniqueId -- callers must map first (see forma.get_object_id_map).
        """
        args: dict[str, Any] = {
            "project_id": project_id,
            "title": title,
            "issue_subtype_id": issue_subtype_id,
            "status": status,
            "published": published,
            "dry_run": dry_run,
        }
        if description is not None:
            args["description"] = description
        if assigned_to is not None:
            args["assigned_to"] = assigned_to
        if assigned_to_type is not None:
            args["assigned_to_type"] = assigned_to_type
        if approval_token is not None:
            args["approval_token"] = approval_token
        if linked_documents:
            # Skip when empty list -- send only when there's actually a link
            # to avoid noisy audit-chain hash entries for issues without one.
            args["linked_documents"] = linked_documents
        return await self.call_structured("issues_create", args)

    # --- v1.4-K5: issue read/update for the approval-resume watcher ---------

    async def list_issues(
        self,
        project_id: str,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List issues. Returns {issues:[...], pagination:{...}}."""
        args: dict[str, Any] = {
            "project_id": project_id,
            "limit": int(limit),
            "offset": int(offset),
        }
        if status is not None:
            args["status"] = status
        if assigned_to is not None:
            args["assigned_to"] = assigned_to
        return await self.call_structured("issues_list", args)

    async def get_issue(self, project_id: str, issue_id: str) -> dict[str, Any]:
        """Get one issue (incl. ``status`` + ``permittedStatuses``)."""
        return await self.call_structured(
            "issues_get", {"project_id": project_id, "issue_id": issue_id}
        )

    async def update_issue(
        self,
        project_id: str,
        issue_id: str,
        *,
        status: str | None = None,
        title: str | None = None,
        description: str | None = None,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        """Update an issue (sparse PATCH). Guardrailed: dry_run=True returns a
        preview + approval_token; call again dry_run=False + token to execute."""
        args: dict[str, Any] = {
            "project_id": project_id,
            "issue_id": issue_id,
            "dry_run": dry_run,
        }
        if status is not None:
            args["status"] = status
        if title is not None:
            args["title"] = title
        if description is not None:
            args["description"] = description
        if approval_token is not None:
            args["approval_token"] = approval_token
        return await self.call_structured("issues_update", args)

    async def add_issue_comment(
        self,
        project_id: str,
        issue_id: str,
        body: str,
        *,
        dry_run: bool = True,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        """Add a comment to an issue. Same dry_run→approval_token guardrail."""
        args: dict[str, Any] = {
            "project_id": project_id,
            "issue_id": issue_id,
            "body": body,
            "dry_run": dry_run,
        }
        if approval_token is not None:
            args["approval_token"] = approval_token
        return await self.call_structured("issues_add_comment", args)

    async def verify_audit_chain(self, *, since: str | None = None) -> Any:
        args: dict[str, Any] = {}
        if since is not None:
            args["since"] = since
        return await self.call("meta_verify_audit_chain", args)


# ── v1 task B-3: module-level helpers for linked_documents construction ─────


def _first_int(d: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """Return the first integer-coercible value among `keys` in `d`, else None."""
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return int(d[k])
            except (TypeError, ValueError):
                continue
    return None


def _first_str(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value among `keys` in `d`, else None."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k])
    return None


def extract_revit_unique_id(element: dict[str, Any]) -> str | None:
    """Return the Revit UniqueId from an AECDM element's flattened params.

    Live AECDM exposes this as the "External ID" property. Surfaces in the
    issue body's Reference section so reviewers can navigate manually in
    Revit even when linked_documents isn't populated (v1 gap until model
    derivative /properties endpoint lands).
    """
    return _first_str(element.get("params") or {}, FormaMCPClient._UNIQUE_ID_KEYS)


def extract_revit_element_id(element: dict[str, Any]) -> int | None:
    """Return the integer Revit ElementId from an AECDM element.

    Live AECDM exposes this as the "Revit Element ID" property. Same use
    case as extract_revit_unique_id but the integer is what populates
    objectId in linked_documents (best-effort -- see _DB_ID_KEYS docstring).
    """
    return _first_int(element.get("params") or {}, FormaMCPClient._DB_ID_KEYS)


def build_linked_document(
    *,
    document_lineage_urn: str,
    created_at_version: int,
    viewable_guid: str,
    object_id: int,
    is_3d: bool = True,
    position: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Construct one acc-forma-mcp-server `linked_documents` entry.

    Schema per R1 (resolved 2026-05-31): snake_case top-level, camelCase
    passthrough inner. ACC UI renders "View in Model" automatically when
    this dict is present and the document URN is browseable in the
    project's Docs module.

    Args:
        document_lineage_urn: format ``urn:adsk.wipprod:dm.lineage:<id>``.
            NOT the version URN (urn:adsk.wipprod:fs.file:vf...).
        created_at_version: version number the pin was placed against.
        viewable_guid: 3D viewable GUID from the model derivative manifest.
        object_id: Forma Viewer dbId (NOT a Revit UniqueId).
        is_3d: True for ThreeDVectorPushpin, False for TwoDVectorPushpin.
        position: optional ``{x, y, z}`` in viewer coords; ACC infers from
            element bbox when omitted.
    """
    pin_type = "ThreeDVectorPushpin" if is_3d else "TwoDVectorPushpin"
    details: dict[str, Any] = {
        "viewable": {"guid": viewable_guid, "is3D": is_3d},
        "objectId": object_id,
    }
    if position is not None:
        details["position"] = position
    return {
        "type": pin_type,
        "urn": document_lineage_urn,
        "createdAtVersion": created_at_version,
        "details": details,
    }
