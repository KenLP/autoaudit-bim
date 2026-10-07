"""v1.7-R25 — browse ACC/Forma projects by NAME, for the Setup project card.

Two reads, and neither one ever fails the request. A browse can time out, 403
(a rotated APS client whose robot has not been invited to the project yet), or
find no forma-mcp server at all — and in every one of those cases the UI's job
is to fall back to its "type the IDs in by hand" block, not to render an error
screen. So the handlers answer **200 with an ``error`` string** and empty
lists; a 5xx would take that choice away from the client.

MCP boundary held (D6): the only Forma access here is
``mcp_clients.forma.FormaMCPClient``, and the text-parsing lives inside that
client — this module holds no business logic, it maps client dataclasses onto
response models.

A client is spawned per request and dropped afterwards (``FormaMCPClient`` is
an async context manager over a stdio subprocess; holding one across requests
would mean owning its lifetime in a service that may sit idle for days). That
costs ~5-10s per browse, which is why the UI caches with react-query rather
than the server caching here.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter

from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig
from bim_orchestrator.service.models import (
    FormaElementGroupsResponse,
    FormaNamedItem,
    FormaProjectItem,
    FormaProjectsResponse,
)

log = structlog.get_logger(__name__)

# Same wall-clock ceiling the Streamlit picker measured against this server.
# The MCP handshake can hang indefinitely on bad credentials, and a browse
# that never returns is indistinguishable — to the person waiting — from a
# broken page.
_BROWSE_TIMEOUT_S = 20.0

_TIMEOUT_MSG = (
    "Forma MCP did not answer within {:.0f}s. Check the APS credentials and "
    "MCP server path below, or enter the IDs manually."
)


def _timeout_message() -> str:
    return _TIMEOUT_MSG.format(_BROWSE_TIMEOUT_S)


def build_forma_router() -> APIRouter:
    router = APIRouter(tags=["forma"])

    @router.get("/forma/projects", response_model=FormaProjectsResponse)
    async def forma_projects() -> FormaProjectsResponse:
        """Every AECDM project in the first hub, with BOTH ids per project."""

        async def _inner():
            async with FormaMCPClient(FormaMCPConfig.from_env()) as client:
                return await client.browse_projects()

        try:
            hub, projects = await asyncio.wait_for(_inner(), timeout=_BROWSE_TIMEOUT_S)
        except TimeoutError:
            log.warning("service.forma_browse_failed", what="projects", reason="timeout")
            return FormaProjectsResponse(error=_timeout_message())
        except Exception as exc:  # any browse failure is data, not a 5xx
            log.warning("service.forma_browse_failed", what="projects", error=str(exc))
            return FormaProjectsResponse(error=str(exc))

        return FormaProjectsResponse(
            hub=FormaNamedItem(id=hub.id, name=hub.name),
            projects=[
                FormaProjectItem(name=p.name, aecdm_id=p.aecdm_id, dm_id=p.dm_id)
                for p in projects
            ],
        )

    @router.get("/forma/element-groups", response_model=FormaElementGroupsResponse)
    async def forma_element_groups(project: str = "") -> FormaElementGroupsResponse:
        """The models inside one AECDM project. ``project`` is the AECDM URN —
        the DM ``b.<uuid>`` form is rejected by the AECDM API, so an empty or
        missing one is answered here rather than spawning a doomed subprocess.
        """
        if not project:
            return FormaElementGroupsResponse(error="no project selected")

        async def _inner():
            async with FormaMCPClient(FormaMCPConfig.from_env()) as client:
                return await client.browse_element_groups(project)

        try:
            groups = await asyncio.wait_for(_inner(), timeout=_BROWSE_TIMEOUT_S)
        except TimeoutError:
            log.warning("service.forma_browse_failed", what="element_groups", reason="timeout")
            return FormaElementGroupsResponse(error=_timeout_message())
        except Exception as exc:  # any browse failure is data, not a 5xx
            log.warning("service.forma_browse_failed", what="element_groups", error=str(exc))
            return FormaElementGroupsResponse(error=str(exc))

        return FormaElementGroupsResponse(
            groups=[FormaNamedItem(id=g.id, name=g.name) for g in groups]
        )

    return router
