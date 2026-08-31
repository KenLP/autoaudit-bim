"""RevitHTTPClient batch routing (addin exposes POST /mcp/batch separately).

The addin's single-command /mcp endpoint never dispatched `batch` (returned
unknown_command → per-element fallback). It now has a dedicated /mcp/batch route;
the HTTP-direct client must POST there with {steps, dryRun} so the no-Node deploy
also gets ONE undo entry. No live server — httpx.MockTransport intercepts.
"""

from __future__ import annotations

import json

import httpx
import pytest

from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError, RevitHTTPClient


def _client(handler) -> RevitHTTPClient:
    client = RevitHTTPClient(port=7891)
    client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:7891",
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_batch_posts_to_mcp_batch_route():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "ok": True, "committed": True,
            "results": [{"ok": True}, {"ok": True}],
        })

    client = _client(handler)
    steps = [
        {"command": "set_parameter", "params": {"id": 1, "name": "Comments", "value": "OK"}},
        {"command": "rename_element", "params": {"id": 1, "name": "Door-NEW"}},
    ]
    env = await client.batch(steps, dry_run=False)
    await client._http.aclose()

    # the SEPARATE route, not /mcp
    assert seen["url"].endswith("/mcp/batch")
    assert seen["body"]["steps"] == steps
    assert seen["body"]["dryRun"] is False
    assert env["committed"] is True


@pytest.mark.asyncio
async def test_batch_not_ok_raises_envelope_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "ok": False, "error": {"code": "bad_step", "message": "nope"},
        })

    client = _client(handler)
    with pytest.raises(RevitEnvelopeError) as ei:
        await client.batch([{"command": "set_parameter", "params": {}}], dry_run=False)
    await client._http.aclose()
    assert ei.value.code == "bad_step"


@pytest.mark.asyncio
async def test_batch_normalizes_top_level_results_into_data():
    """H2: the addin's documented response shape is {ok, committed, results}
    with `results` at the TOP level (not nested under `data` like every other
    revit_* envelope). Every consumer reads `(env.get("data") or {}).get(
    "results")` — batch() must wrap top-level `results` into `data` so that
    read works, while leaving the top-level key in place for back-compat."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "committed": True,
            "results": [{"ok": True}, {"ok": True}],
        })

    client = _client(handler)
    env = await client.batch([{"command": "set_parameter", "params": {}}], dry_run=False)
    await client._http.aclose()

    assert env["results"] == [{"ok": True}, {"ok": True}]   # top-level kept
    assert env["data"]["results"] == [{"ok": True}, {"ok": True}]  # wrapped


@pytest.mark.asyncio
async def test_batch_reads_already_nested_results_shape():
    """H2 (other half): a response that already nests `results` under `data`
    (a future addin, or shape parity with the MCP-stdio transport) must pass
    through untouched — both shapes end up readable via `data.results`."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "committed": True,
            "data": {"results": [{"ok": True}]},
        })

    client = _client(handler)
    env = await client.batch([{"command": "set_parameter", "params": {}}], dry_run=False)
    await client._http.aclose()

    assert env["data"]["results"] == [{"ok": True}]


@pytest.mark.asyncio
async def test_batch_404_non_json_raises_unknown_command():
    """M1: an older addin lacking the /mcp/batch route commonly 404s with an
    HTML/plain-text body, not JSON. Before this fix, the bare `except Exception:
    resp.raise_for_status()` propagated an httpx.HTTPStatusError, which none of
    the 3 per-element-fallback callers (DesignAgent._commit_revit_batch,
    ApprovalWatcher._commit_writes / _reprove_stale) catch — killing the
    fallback entirely. A 404 must now surface as the SAME unknown_command
    RevitEnvelopeError the not-ok-envelope branch raises."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html><body>Not Found</body></html>")

    client = _client(handler)
    with pytest.raises(RevitEnvelopeError) as ei:
        await client.batch([{"command": "set_parameter", "params": {}}], dry_run=False)
    await client._http.aclose()
    assert ei.value.code == "unknown_command"


@pytest.mark.asyncio
async def test_batch_non_404_non_json_still_raises_for_status():
    """M1 (guard rail): a non-404 non-JSON body (e.g. a 500 from a proxy/
    gateway in front of the addin) must NOT be swallowed into unknown_command
    — that would make the per-element fallback silently retry a transient
    server error as if the route were simply missing. Keep raise_for_status()
    for everything except 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.batch([{"command": "set_parameter", "params": {}}], dry_run=False)
    await client._http.aclose()


@pytest.mark.asyncio
async def test_get_element_rooms_uses_mcp_command():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"count": 0, "elements": []}})

    client = _client(handler)
    await client.get_element_rooms([184239, 184501])
    await client._http.aclose()
    assert seen["url"].endswith("/mcp")            # single-command route
    assert seen["body"]["command"] == "get_element_rooms"
    assert seen["body"]["params"]["ids"] == [184239, 184501]
