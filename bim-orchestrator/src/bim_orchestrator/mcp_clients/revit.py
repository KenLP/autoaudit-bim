"""MCP and HTTP client wrappers for RevitMCPServer.

Two transport back-ends share the same convenience API via ``_RevitClientMixin``:

• ``RevitMCPClient``  — spawns the Node stdio bridge (``revit-mcp-server``),
  which forwards to the in-Revit HTTP addin. Requires Node.js + bridge installed.

• ``RevitHTTPClient`` — calls the addin REST API at ``http://127.0.0.1:{port}/mcp``
  directly. No Node bridge required; works as long as RevitMCPServer addin is loaded.

Use ``make_revit_client()`` to choose automatically:
  HTTP when ``REVIT_MCP_USE_HTTP=true`` or when no Node bridge is configured
  (no ``vendor/revit-mcp`` and no ``REVIT_MCP_SERVER_CWD``);
  MCP stdio bridge otherwise.

Bounding-box geometry returns coordinates in **feet** (Revit internal units).
Helpers ``feet_to_meters`` / ``meters_to_feet`` are provided.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import httpx
import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = structlog.get_logger(__name__)

# Ceiling on the stdio handshake — see `mcp_clients/forma.py`. Only the stdio
# bridge needs it; RevitHTTPClient already carries per-request httpx timeouts.
_HANDSHAKE_TIMEOUT_S = 60.0

FEET_TO_METERS = 0.3048
SQFT_TO_SQM = FEET_TO_METERS * FEET_TO_METERS  # 0.09290304
_PASSTHROUGH_ENV_KEYS = (
    "PATH",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "SYSTEMROOT",
    "REVIT_MCP_HOST",
    "REVIT_MCP_PORT",
    "REVIT_MCP_AUTH_TOKEN",
    "REVIT_MCP_VERSION",
    "REVIT_MCP_TIMEOUT_MS",
)

# bim-orchestrator/ root — used for vendor/ discovery.
_APP_ROOT = Path(__file__).parent.parent.parent.parent


def _vendor_cwd(server_name: str) -> str | None:
    """Return vendor/<server_name> path if its dist/index.js exists there."""
    candidate = _APP_ROOT / "vendor" / server_name
    return str(candidate) if (candidate / "dist" / "index.js").exists() else None


def feet_to_meters(feet: float) -> float:
    return feet * FEET_TO_METERS


def meters_to_feet(meters: float) -> float:
    return meters / FEET_TO_METERS


def sqft_to_sqm(sqft: float) -> float:
    return sqft * SQFT_TO_SQM


# HTTP-direct assumes the addin command is the MCP tool name minus "revit_".
# That holds for every forwarding tool but one: the Node MCP server maps
# `revit_get_version` to the addin's `get_revit_version` (index.ts:147), so the
# stripped name reaches an addin that has no such command and the call fails
# with `unknown_command` — which killed `scripts/dump_param_catalog.py` on a
# cosmetic version string alone. Audited 2026-08-25 across all 36 fwd()-based
# tools in index.ts: this is the ONLY mismatch (the other 57 tools use custom
# handlers, which don't take this path). Add an entry here if that ever changes.
_COMMAND_OVERRIDES = {"revit_get_version": "get_revit_version"}


def _resolve_port(version: str) -> int:
    """Compute addin port from version string. Override with REVIT_MCP_PORT."""
    if raw := os.environ.get("REVIT_MCP_PORT"):
        return int(raw)
    try:
        return 7891 + max(0, int(version) - 2026)
    except (ValueError, TypeError):
        return 7891


def _load_auth_token(version: str) -> str | None:
    """Read the auth token for the given Revit version.

    Priority: REVIT_MCP_AUTH_TOKEN env var → token file in %APPDATA%.
    The addin always requires a bearer token (RevitMCPServer >= 0.8.17), so
    there is no way to opt out of auth; None here means the token is missing
    and every call will 401.
    """
    if token := os.environ.get("REVIT_MCP_AUTH_TOKEN", "").strip():
        return token
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    token_path = (
        Path(appdata) / "Autodesk" / "Revit" / "Addins" / version / "revit-mcp-token.txt"
    )
    try:
        return token_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ── Config dataclass (for MCP stdio transport) ───────────────────────────────


@dataclass
class RevitMCPConfig:
    command: str
    args: list[str]
    cwd: str | None
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Self:
        command = os.environ.get("REVIT_MCP_SERVER_CMD", "node")
        cwd = os.environ.get("REVIT_MCP_SERVER_CWD") or _vendor_cwd("revit-mcp") or None
        raw_args = os.environ.get("REVIT_MCP_SERVER_ARGS", "")
        if not raw_args and cwd:
            raw_args = "dist/index.js"  # vendor default entrypoint
        args = shlex.split(raw_args) if raw_args else []
        env = {k: os.environ[k] for k in _PASSTHROUGH_ENV_KEYS if k in os.environ}
        if cwd:
            log.debug(
                "revit_mcp.cwd_resolved",
                cwd=cwd,
                source="env" if os.environ.get("REVIT_MCP_SERVER_CWD") else "vendor",
            )
        return cls(command=command, args=args, cwd=cwd, env=env)


# ── Error ─────────────────────────────────────────────────────────────────────


class RevitEnvelopeError(RuntimeError):
    """Raised when the addin (or bridge) returns ``ok: false``."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool}: [{code}] {message}")
        self.tool = tool
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StepOutcome:
    """Whether ONE step of a live batch is confirmed committed by Revit."""

    ok: bool
    reason: str | None = None


def batch_commit_outcomes(
    envelope: Any, *, expected: int
) -> list[StepOutcome]:
    """Per-step commit confirmation for a LIVE ``batch()`` envelope (P1-TRUST-01).

    Returns exactly ``expected`` outcomes, index-aligned with the steps that
    were sent. A caller may only record a write as applied when its outcome is
    ``ok``.

    The rule this replaces: three call sites each decided for themselves, and
    all three treated "the envelope didn't say otherwise" as success. Missing
    ``results`` in particular was read as *trust the commit* — so an addin that
    returned ``ok: true`` and nothing else got every write marked applied, the
    approval record closed, and the ACC issue closed with it, while the model
    could still hold the old values. Execution reported a negative and the
    consumer recorded a positive.

    Two signals, treated differently ON PURPOSE:

    * ``committed is False`` — an EXPLICIT negative from the addin. Nothing
      landed; every step is unconfirmed. Never overridden by per-step results.
    * ``committed`` absent — NOT a negative. The stdio transport and older
      addins never sent the field, and inventing a failure from silence would
      strand every write on those paths. Fall through to the per-step results,
      which is the stronger evidence anyway.

    Missing or short ``results`` is unconfirmed (owner decision 2026-07-26,
    option (a)): a step nobody confirmed is not a step that succeeded. That is
    also what covers the silent-envelope case above — no ``committed``, no
    ``results``, no confirmation.
    """
    if not isinstance(envelope, Mapping):
        return [StepOutcome(False, "malformed_envelope")] * expected

    committed = envelope.get("committed")
    if committed is False:
        return [StepOutcome(False, "not_committed")] * expected

    results = (envelope.get("data") or {}).get("results")
    if results is None:
        results = envelope.get("results")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        return [StepOutcome(False, "no_step_results")] * expected

    out: list[StepOutcome] = []
    for idx in range(expected):
        if idx >= len(results):
            out.append(StepOutcome(False, "missing_step_result"))
            continue
        res = results[idx]
        if not isinstance(res, Mapping):
            out.append(StepOutcome(False, "malformed_step_result"))
        elif res.get("ok") is False or "error" in res:
            detail = res.get("error") or "step reported not ok"
            out.append(StepOutcome(False, str(detail)))
        else:
            out.append(StepOutcome(True))
    return out


# ── Shared convenience API (mixin) ────────────────────────────────────────────


class _RevitClientMixin:
    """Convenience methods shared by both MCP-stdio and HTTP back-ends.

    Subclasses must implement ``call_envelope``.
    """

    async def call_envelope(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def call_data(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Same as ``call_envelope`` but unwraps the ``data`` field."""
        envelope = await self.call_envelope(tool, arguments)
        return envelope.get("data")

    # ── read-only helpers ──────────────────────────────────────────────────

    async def ping(self) -> dict[str, Any]:
        return await self.call_data("revit_ping")

    async def get_document_info(self) -> dict[str, Any]:
        return await self.call_data("revit_get_document_info")

    async def get_version(self) -> dict[str, Any]:
        return await self.call_data("revit_get_version")

    async def list_levels(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_list_levels")
        return list((data or {}).get("levels", []))

    async def list_rooms(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_list_rooms")
        return list((data or {}).get("rooms", []))

    async def list_sheets(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """All drawing sheets (ViewSheet) in the model. Each item carries (live-
        verified on R27) ``id`` / ``sheetNumber`` / ``name`` / ``viewportCount``;
        the field readers also tolerate ``number`` / ``sheetName`` / ``title`` for
        addin variants. Sheets are documentation, not model geometry, so they
        don't come back from ``list_elements`` — the RevitQueryAgent routes
        OST_Sheets here.

        The addin's ``revit_list_sheets`` takes NO arguments and returns every
        sheet (no pagination), so ``limit`` is applied client-side as a safety
        cap rather than sent to the addin."""
        data = await self.call_data("revit_list_sheets", None)
        sheets = data if isinstance(data, list) else list((data or {}).get("sheets", []))
        if limit is not None and limit > 0:
            return sheets[:limit]
        return sheets

    async def list_categories(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_list_categories")
        return list((data or {}).get("categories", []))

    async def list_families(
        self, category: str | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Loaded families [{id, name, category, ...}] — used to resolve a Family
        element id from its name for ``remediation.target == "family"`` renames."""
        args: dict[str, Any] = {}
        if category is not None:
            args["category"] = category
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_list_families", args or None)
        return list((data or {}).get("families", []))

    async def list_elements(
        self,
        category: str,
        *,
        limit: int | None = None,
        only_instances: bool = True,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {
            "category": category,
            "onlyInstances": bool(only_instances),
        }
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_list_elements", args)
        return list((data or {}).get("elements", []))

    async def get_element_info(self, element_id: int) -> dict[str, Any]:
        return await self.call_data("revit_get_element_info", {"id": int(element_id)})

    async def get_element_geometry(self, element_id: int) -> dict[str, Any]:
        return await self.call_data("revit_get_element_geometry", {"id": int(element_id)})

    async def get_linked_files(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_get_linked_files")
        if isinstance(data, list):
            return data
        return list((data or {}).get("links", []))

    async def check_clearance(
        self,
        *,
        set_a_category: str,
        set_b_category: str,
        axis: str,
        direction: str | None = None,
        clearance_mm: float,
        set_b_link_id: int | None = None,
        view_id: int | None = None,
        sample_count: int = 3,
        set_a_limit: int | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        """Call revit_check_clearance and return the clashes list.

        Uses the revit_check_clearance MCP tool format:
          setA/setB use ``source`` (str: "host"/"link") + ``categories`` (list).
          Response shape: {clashCount, clashes: [{elementA, elementB,
          clearanceActualMm, type}]}.

        ``axis`` is ``"Z"`` (vertical raycast — needs ``direction`` below/above
        + a 3D view) or ``"bbox"`` (omnidirectional AABB proximity — ``direction``
        and ``sampleCount`` are ignored by the addin and omitted here).

        ``set_a_limit`` caps how many setA elements the addin loads (the P0
        element budget); ``max_results`` caps returned clash pairs.

        setA is always the host model; setB may be a linked file (pass
        ``set_b_link_id``) or also the host model (omit it).
        """
        set_a: dict[str, Any] = {"source": "host", "categories": [set_a_category]}
        if set_a_limit is not None:
            set_a["limit"] = int(set_a_limit)
        if set_b_link_id is not None:
            set_b: dict[str, Any] = {
                "source": "link",
                "linkId": set_b_link_id,
                "categories": [set_b_category],
            }
        else:
            set_b = {"source": "host", "categories": [set_b_category]}
        args: dict[str, Any] = {
            "setA": set_a,
            "setB": set_b,
            "axis": axis,
            "clearanceMm": clearance_mm,
        }
        # direction + sampleCount are Z-raycast-only; bbox proximity ignores them.
        if axis == "Z":
            if direction is not None:
                args["direction"] = direction
            args["sampleCount"] = sample_count
        if view_id is not None:
            args["viewId"] = view_id
        if max_results is not None:
            args["maxResults"] = int(max_results)
        data = await self.call_data("revit_check_clearance", args)
        if isinstance(data, list):
            return data
        return list((data or {}).get("clashes", []))

    async def list_spaces(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """All placed MEP Spaces. Shape per item: {id, name, number, levelName,
        area, volume, ...}. Note: no bounding box — fetch via
        ``get_element_geometry(space_id)`` when spatial containment is needed."""
        args: dict[str, Any] = {}
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_list_spaces", args)
        if isinstance(data, list):
            return data
        return list((data or {}).get("spaces", []))

    async def get_active_view(self) -> dict[str, Any]:
        """Return the currently active view. Shape: {id, name, viewType, ...}."""
        data = await self.call_data("revit_get_active_view")
        return data or {}

    async def get_views(self) -> list[dict[str, Any]]:
        """Return all views in the model. Shape per item: {id, name, viewType}."""
        data = await self.call_data("revit_get_views")
        if isinstance(data, list):
            return data
        return list((data or {}).get("views", []))

    # ── highlight surface (parity with RevitHTTPClient, 2026-08-26) ────────
    #
    # ``highlight.highlight_elements`` needs select/zoom/open_view/override,
    # which existed ONLY on the HTTP client. On a machine where the Node
    # bridge is configured, ``make_revit_client`` returns THIS class, so the
    # service's "Highlight in Revit" died with `'RevitMCPClient' object has
    # no attribute 'select_elements'` — invisible until 2026-08-26 because a
    # separate UI bug kept the button permanently disabled (axes booleans
    # read as HealthStatus strings), so nobody had ever pressed it on this
    # transport. Both classes share the ``call_data("revit_<tool>", args)``
    # convention, so these mirror the HTTP versions verbatim; the fuller
    # behavioural docstrings (ShowElements view-switching, override writes
    # to the model) live on the HTTP twins below.

    async def select_elements(self, ids: list[int]) -> dict[str, Any]:
        """Select the given elements in the Revit UI (no camera change)."""
        return await self.call_data("revit_select_elements", {"ids": list(ids)})

    async def zoom_to_elements(self, ids: list[int]) -> dict[str, Any]:
        """Show the elements — can CHANGE the active view (ShowElements)."""
        return await self.call_data("revit_zoom_to_elements", {"ids": list(ids)})

    async def open_view(self, view_id: int, *, dry_run: bool = False) -> dict[str, Any]:
        """Activate a view (UI action, not a model edit)."""
        return await self.call_data(
            "revit_open_view", {"viewId": int(view_id), "dryRun": bool(dry_run)}
        )

    async def override_element_graphics(
        self,
        *,
        view_id: int,
        element_ids: list[int],
        color: dict[str, int] | None = None,
        transparency: int | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """Per-element colour override in ONE view — WRITES to the document."""
        args: dict[str, Any] = {
            "viewId": int(view_id),
            "elementIds": [int(e) for e in element_ids],
        }
        if reset:
            args["reset"] = True
        if color is not None:
            args["color"] = color
        if transparency is not None:
            args["transparency"] = int(transparency)
        return await self.call_data("revit_override_element_graphics", args)

    async def get_parameter(
        self, element_id: int, parameter_name: str
    ) -> dict[str, Any]:
        return await self.call_data(
            "revit_get_parameter",
            {"id": int(element_id), "parameterName": parameter_name},
        )

    async def find_elements(
        self,
        category: str,
        *,
        filters: list[dict[str, Any]] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"category": category}
        if filters is not None:
            args["filters"] = filters
        if fields is not None:
            args["fields"] = fields
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_find_elements", args)
        return list((data or {}).get("elements", []))

    # ── write helpers (dry-run aware) ──────────────────────────────────────

    async def set_parameter(
        self,
        element_id: int,
        parameter_name: str,
        value: Any,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Set one parameter. Returns the full envelope for before/after diff."""
        return await self.call_envelope(
            "revit_set_parameter",
            {
                "id": int(element_id),
                "parameterName": parameter_name,
                "value": value,
                "dryRun": bool(dry_run),
            },
        )

    async def set_parameter_batch(
        self,
        element_ids: list[int],
        parameter_name: str,
        value: Any,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await self.call_envelope(
            "revit_set_parameter_batch",
            {
                "ids": [int(i) for i in element_ids],
                "parameterName": parameter_name,
                "value": value,
                "dryRun": bool(dry_run),
            },
        )

    async def batch(
        self,
        steps: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Run multiple commands in ONE Revit transaction → a single undo entry.

        Each step is ``{"command": "set_parameter"|"rename_element"|…,
        "params": {...}}`` (the un-prefixed command name, matching the addin's
        dispatch). Use for many heterogeneous writes (e.g. unique per-element
        Marks) that must commit atomically as one user-undoable action.
        """
        return await self.call_envelope(
            "revit_batch",
            {"steps": steps, "dryRun": bool(dry_run), "stopOnError": bool(stop_on_error)},
        )

    async def rename_element(
        self, element_id: int, new_name: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        return await self.call_envelope(
            "revit_rename_element",
            {
                "id": int(element_id),
                "name": new_name,
                "dryRun": bool(dry_run),
            },
        )

    # ── view-authoring helpers (v1 report module, Phase 2) ──────────────────
    # Used to AUTO-CREATE the native verification artifacts a reviewer would
    # otherwise build by hand — a schedule reproducing the finding set, a view
    # filter + colour override highlighting the flagged set. Same MCP boundary:
    # these route through the addin's revit_* tools like every other call.

    async def create_schedule(
        self,
        category: str,
        *,
        name: str | None = None,
        fields: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a ViewSchedule for ``category`` (a BuiltInCategory, e.g.
        ``OST_Walls``), optionally with column ``fields``. Returns the data dict;
        the new schedule's id is under ``scheduleId`` (or ``id``)."""
        args: dict[str, Any] = {"category": category, "dryRun": bool(dry_run)}
        if name is not None:
            args["name"] = name
        if fields is not None:
            args["fields"] = list(fields)
        return await self.call_data("revit_create_schedule", args)

    async def configure_schedule(
        self,
        schedule_id: int,
        *,
        filters: list[dict[str, Any]] | None = None,
        sort_fields: list[dict[str, Any]] | None = None,
        clear_filters: bool = False,
        clear_sort_fields: bool = False,
        export_csv: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add filters / sort+group fields to an existing schedule (and optionally
        export it to CSV). ``filters`` items: ``{field, operator, value}`` where
        operator is one of equals/not_equals/greater/greater_equal/less/less_equal/
        contains/not_contains/begins_with/ends_with/has_value/has_no_value —
        rich enough to reproduce ANY requirement's predicate. ``sort_fields``
        items: ``{field, ascending, groupBy}``."""
        args: dict[str, Any] = {"scheduleId": int(schedule_id), "dryRun": bool(dry_run)}
        if filters is not None:
            args["filters"] = filters
        if sort_fields is not None:
            args["sortFields"] = sort_fields
        if clear_filters:
            args["clearFilters"] = True
        if clear_sort_fields:
            args["clearSortFields"] = True
        if export_csv:
            args["exportCsv"] = True
        return await self.call_data("revit_configure_schedule", args)

    async def apply_view_filter(
        self,
        *,
        filter_name: str,
        category: str,
        parameter_name: str,
        value: str,
        view_id: int | None = None,
        color_rgb: dict[str, int] | None = None,
        reuse_existing: bool = True,
        visible: bool | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a parameter filter rule + apply it to a view with an optional
        colour override. NOTE: the addin supports an EQUALITY match only
        (``value``) — it cannot express ``< threshold`` / ``has no value``. See
        the add-in's equality-only `apply_view_filter`."""
        args: dict[str, Any] = {
            "filterName": filter_name,
            "category": category,
            "parameterName": parameter_name,
            "value": value,
            "reuseExisting": bool(reuse_existing),
            "dryRun": bool(dry_run),
        }
        if view_id is not None:
            args["viewId"] = int(view_id)
        if color_rgb is not None:
            args["colorRGB"] = color_rgb
        if visible is not None:
            args["visible"] = bool(visible)
        return await self.call_data("revit_apply_view_filter", args)

    async def color_override_by_param(
        self,
        *,
        category: str,
        parameter_name: str,
        color_map: dict[str, dict[str, int]],
        view_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Colour-code elements in a view by a parameter VALUE.
        ``color_map``: ``{param_value: {r,g,b}}``."""
        args: dict[str, Any] = {
            "category": category,
            "parameterName": parameter_name,
            "colorMap": color_map,
            "dryRun": bool(dry_run),
        }
        if view_id is not None:
            args["viewId"] = int(view_id)
        return await self.call_data("revit_color_override_by_param", args)


# ── Transport A: MCP stdio bridge ─────────────────────────────────────────────


class RevitMCPClient(_RevitClientMixin):
    """Async context manager over an MCP stdio session to revit-mcp-server."""

    def __init__(self, config: RevitMCPConfig) -> None:
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
            # See FormaMCPClient.__aenter__ — a mute bridge must fail, not hang.
            await asyncio.wait_for(session.initialize(), timeout=_HANDSHAKE_TIMEOUT_S)
        except BaseException:
            # `__aexit__` never runs after `__aenter__` raises, so close the
            # stack here or leak the bridge process + its reader tasks.
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
            raise
        self._session = session
        log.info("revit_mcp.connected", command=self._config.command)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("RevitMCPClient used outside `async with` block")
        return self._session

    async def call_envelope(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a ``revit_*`` tool and return the parsed envelope dict.

        Raises ``RevitEnvelopeError`` when ``ok: false``.
        """
        result = await self.session.call_tool(tool, arguments or {})
        envelope = _parse_envelope(result.content)
        log.info(
            "revit_mcp.call",
            tool=tool,
            ok=envelope.get("ok"),
            committed=envelope.get("committed"),
            dry_run=envelope.get("dryRun"),
        )
        if not envelope.get("ok", False):
            err = envelope.get("error") or {}
            raise RevitEnvelopeError(
                tool=tool,
                code=str(err.get("code", "unknown")),
                message=str(err.get("message", "no message")),
            )
        return envelope


# ── Transport B: HTTP direct ──────────────────────────────────────────────────


class RevitHTTPClient(_RevitClientMixin):
    """Direct httpx client to the Revit addin HTTP server.

    Bypasses the Node bridge entirely. The addin exposes:
      POST /mcp    — single command: {command, params, dryRun (top-level)}
      GET  /health — auth-exempt probe: {ok, service, version, authEnabled}

    ``dryRun`` is extracted from the ``arguments`` dict (if present) and
    elevated to the request body top level, matching what the Node bridge
    was doing transparently.
    """

    def __init__(self, port: int = 7891, auth_token: str | None = None) -> None:
        self._port = port
        self._auth_token = auth_token
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        timeout = float(os.environ.get("REVIT_MCP_TIMEOUT_MS", "30000")) / 1000.0
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        self._http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self._port}",
            headers=headers,
            timeout=timeout,
        )
        log.info("revit_http.connected", port=self._port, auth=bool(self._auth_token))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None

    async def batch(
        self,
        steps: list[dict[str, Any]],
        *,
        dry_run: bool = True,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Commit N writes in ONE Revit transaction via the addin's dedicated
        ``POST /mcp/batch`` route (NOT ``/mcp``) — body ``{steps, dryRun}``,
        response ``{ok, committed, results}``.

        The single-command ``/mcp`` endpoint never dispatched ``batch`` (it
        returned ``unknown_command`` → DesignAgent fell back to per-element = N
        undo entries). The addin now exposes the batch route, so the HTTP-direct
        deploy gets ONE undo too. The per-element fallback stays as a safety net
        for an older addin (an `unknown_command`/404 still triggers it).

        ``dry_run`` defaults to ``True`` (preview-default convention, like every
        other write helper on this mixin) — callers that want a live commit
        must pass ``dry_run=False`` explicitly.
        """
        if self._http is None:
            raise RuntimeError("RevitHTTPClient used outside `async with` block")
        resp = await self._http.post(
            "/mcp/batch",
            json={
                "steps": steps,
                "dryRun": bool(dry_run),
                "stopOnError": bool(stop_on_error),
            },
        )
        try:
            envelope: dict[str, Any] = resp.json()
        except Exception:
            # M1: a 404 (route missing — an older addin that predates
            # /mcp/batch) commonly returns an HTML/plain-text body, not JSON.
            # Surface it as the SAME unknown_command RevitEnvelopeError the
            # not-ok-envelope branch below raises, so all three per-element
            # fallback callers (DesignAgent._commit_revit_batch,
            # ApprovalWatcher._commit_writes / _reprove_stale) trigger on one
            # code regardless of transport-vs-envelope failure shape.
            if resp.status_code == 404:
                raise RevitEnvelopeError(
                    tool="revit_batch",
                    code="unknown_command",
                    message="/mcp/batch route missing (older addin)",
                ) from None
            resp.raise_for_status()
            return {}
        log.info(
            "revit_http.batch",
            ok=envelope.get("ok"),
            committed=envelope.get("committed"),
            steps=len(steps),
        )
        if not envelope.get("ok", False):
            err = envelope.get("error") or {}
            raise RevitEnvelopeError(
                tool="revit_batch",
                code=str(err.get("code", "unknown")),
                message=str(err.get("message", "no message")),
            )
        # H2: normalize the envelope shape. The addin's documented response is
        # {ok, committed, results} — ``results`` at the TOP level, not nested
        # under ``data`` like every other revit_* tool envelope. Every consumer
        # (DesignAgent._commit_revit_batch, ApprovalWatcher._commit_writes /
        # _reprove_stale) reads ``(env.get("data") or {}).get("results")`` to
        # match the other tools' shape — so mirror top-level ``results`` into
        # ``data`` here rather than touching 3 call sites. Keep the top-level
        # key too (back-compat for anything reading it directly). A response
        # that already nests under ``data`` (future addin, or the MCP-stdio
        # transport's envelope) passes through unchanged.
        if "results" in envelope and not (envelope.get("data") or {}).get("results"):
            envelope.setdefault("data", {})
            envelope["data"]["results"] = envelope["results"]
        return envelope

    async def get_element_rooms(self, ids: list[int]) -> dict[str, Any]:
        """v1.4: native phase-aware room containment (`POST /mcp` command
        ``get_element_rooms``). Returns per element ``room`` (point-based) or
        ``fromRoom``/``toRoom`` (wall-hosted door/window). Available as a future
        replacement for the geometry-bbox `_enrich_containing_space` join."""
        return await self.call_data("revit_get_element_rooms", {"ids": list(ids)})

    async def select_elements(self, ids: list[int]) -> dict[str, Any]:
        """v1.6/3b (B8): select the given elements in the Revit UI
        (`POST /mcp` command ``select_elements``, body ``{ids}`` — same key as
        ``get_element_rooms``/``set_parameter_batch`` above). Used by the
        AutoAudit UI's "Highlight in Revit" action; paired with
        ``zoom_to_elements`` by the caller so the selection is also visible.

        Selecting does NOT move the camera or change the active view. The addin
        requires at least one id, so there is no API way to CLEAR a selection —
        the user presses Esc (live-probed 2026-08-17)."""
        return await self.call_data("revit_select_elements", {"ids": list(ids)})

    async def zoom_to_elements(self, ids: list[int]) -> dict[str, Any]:
        """v1.6/3b (B8): show the given elements (`POST /mcp` command
        ``zoom_to_elements``, body ``{ids}``).

        **This can CHANGE the active view** — the addin calls Revit's
        ``UIDocument.ShowElements``, which is "show me this element", not a
        plain zoom: when the elements aren't visible in the active view Revit
        finds a view that shows them and activates it. Live-probed on Snowdon
        (2026-08-17): active view L5 + a door on L2 → Revit switched to L2. The
        old docstring here said "zoom/frame the active view", which promised
        less than the tool does; the AutoAudit confirm dialog said the same.

        Two consequences for callers:
          * **Multi-level sets get ONE view.** With three doors on L3/L4/L5 it
            activated L3 and left the other two selected but off-screen — walk
            the levels yourself (``highlight.highlight_elements``) if you want
            each one framed.
          * **It can raise a modal dialog.** If no suitable view exists Revit
            asks "search through closed views?" and blocks until answered — an
            unattended run must not call this (the dialog watchdog would halt,
            by design).
        """
        return await self.call_data("revit_zoom_to_elements", {"ids": list(ids)})

    async def open_view(self, view_id: int, *, dry_run: bool = False) -> dict[str, Any]:
        """Activate a view (`POST /mcp` command ``open_view``, body ``{viewId}``).

        A UI action, not a model edit — used by ``highlight.highlight_elements``
        to frame each level's elements in that level's own plan instead of
        letting ``ShowElements`` pick one view for the whole set."""
        return await self.call_data(
            "revit_open_view", {"viewId": int(view_id), "dryRun": bool(dry_run)}
        )

    async def override_element_graphics(
        self,
        *,
        view_id: int,
        element_ids: list[int],
        color: dict[str, int] | None = None,
        transparency: int | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """Per-element colour/fill override in ONE view (`POST /mcp` command
        ``override_element_graphics``).

        Unlike ``apply_view_filter`` (equality-only — see
        the add-in's filter is equality-only) this takes an EXPLICIT
        element-id list, so it can paint any set we already resolved, whatever
        predicate produced it.

        **It WRITES to the model.** A graphic override is stored in the view, so
        the document becomes modified (a cloud model then needs Sync or a
        discard) — this is presentation, never evidence: the colour is OUR
        verdict painted on, and it goes stale the moment the model changes,
        whereas a schedule/view-filter re-evaluates natively. Callers keep it
        opt-in and always offer ``reset=True`` to clear.
        """
        args: dict[str, Any] = {
            "viewId": int(view_id),
            "elementIds": [int(e) for e in element_ids],
        }
        if reset:
            args["reset"] = True
        if color is not None:
            args["color"] = color
        if transparency is not None:
            args["transparency"] = int(transparency)
        return await self.call_data("revit_override_element_graphics", args)

    async def call_envelope(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call the addin directly. Elevates ``dryRun`` from params to body top level."""
        if self._http is None:
            raise RuntimeError("RevitHTTPClient used outside `async with` block")
        args = dict(arguments or {})
        dry_run = bool(args.pop("dryRun", False))
        command = _COMMAND_OVERRIDES.get(tool, tool.removeprefix("revit_"))

        resp = await self._http.post(
            "/mcp",
            json={"command": command, "params": args, "dryRun": dry_run},
        )
        # Addin v0.7+: errors return HTTP 4xx/5xx but still carry the
        # {ok, error: {code, message}} envelope in the body (StatusForResult mapping).
        # Parse the body first so RevitEnvelopeError gets structured code/message.
        # Fall back to raise_for_status() only when the body is not valid JSON.
        try:
            envelope: dict[str, Any] = resp.json()
        except Exception:
            resp.raise_for_status()
            return {}

        log.info(
            "revit_http.call",
            tool=tool,
            ok=envelope.get("ok"),
            committed=envelope.get("committed"),
            dry_run=envelope.get("dryRun"),
        )
        if not envelope.get("ok", False):
            err = envelope.get("error") or {}
            raise RevitEnvelopeError(
                tool=tool,
                code=str(err.get("code", "unknown")),
                message=str(err.get("message", "no message")),
            )
        return envelope

    async def health(self) -> dict[str, Any]:
        """GET /health — auth-exempt. Returns {ok, service, version, authEnabled}."""
        if self._http is None:
            raise RuntimeError("RevitHTTPClient used outside `async with` block")
        resp = await self._http.get("/health")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ── Public type alias ─────────────────────────────────────────────────────────

AnyRevitClient = RevitMCPClient | RevitHTTPClient


# ── Factory ───────────────────────────────────────────────────────────────────


def make_revit_client() -> AnyRevitClient:
    """Return the appropriate Revit client based on environment config.

    Picks HTTP direct when ``REVIT_MCP_USE_HTTP=true`` or when no Node bridge
    is configured (no ``vendor/revit-mcp`` folder and no ``REVIT_MCP_SERVER_CWD``).
    Falls back to MCP stdio when the bridge is available.
    """
    use_http = os.environ.get("REVIT_MCP_USE_HTTP", "").lower() == "true"
    config = RevitMCPConfig.from_env()
    if use_http or config.cwd is None:
        version = os.environ.get("REVIT_MCP_VERSION", "2026")
        client = RevitHTTPClient(
            port=_resolve_port(version),
            auth_token=_load_auth_token(version),
        )
        log.debug("revit_client.selected", transport="http", port=client._port)
        return client
    log.debug("revit_client.selected", transport="mcp_stdio", cwd=config.cwd)
    return RevitMCPClient(config)


# ── Envelope parsing (MCP stdio only) ────────────────────────────────────────


def _parse_envelope(content: Any) -> dict[str, Any]:
    """Pull the JSON envelope out of an MCP tool result.

    The TS bridge emits a single text block whose payload is the
    JSON-stringified ``{ok, data, error, ...}`` envelope.

    When nothing parses, the raw text is QUOTED into the error message. Every
    bridge code path emits JSON, so a non-JSON body means the failure happened
    ABOVE the bridge — typically MCP input validation (the SDK turns a schema
    mismatch into a plain-text tool result). LIVE 2026-08-01: a bare
    "Bridge returned a result with no parseable JSON envelope" cost a live
    Revit session to diagnose; the text names the offending argument outright.
    """
    blocks = content if isinstance(content, list) else [content]
    raw_texts: list[str] = []
    for block in blocks:
        text = _block_text(block)
        if text is None:
            continue
        raw_texts.append(text)
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    detail = " | ".join(t.strip() for t in raw_texts if t.strip())
    message = "Bridge returned a result with no parseable JSON envelope."
    if detail:
        message += f" Raw result: {detail[:500]}"
    return {
        "ok": False,
        "error": {"code": "bad_envelope", "message": message},
    }


def _block_text(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("text") if block.get("type") == "text" else None
    text = getattr(block, "text", None)
    return text if isinstance(text, str) else None
