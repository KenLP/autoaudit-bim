"""Tests for RevitMCPClient + MockRevitMCPClient (Phase 2 Week 6 Day 1).

Coverage:
  * RevitMCPConfig.from_env reads env vars, defaults, passes through.
  * _parse_envelope handles list/object content blocks + bad shapes.
  * RevitEnvelopeError raised on ok=false.
  * MockRevitMCPClient covers ping, list_rooms, get_element_info,
    get_element_geometry, set_parameter (dry vs commit), rename_element,
    batch, fail_on, calls_to.
  * feet_to_meters / meters_to_feet round-trip.

These tests use only the mock client — no Revit, no subprocess. The live
smoke test against a real Revit session is exercised via the CLI
``--list-revit-rooms`` flag (see test_smoke / manual run).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from bim_orchestrator.mcp_clients.revit import (
    FEET_TO_METERS,
    RevitEnvelopeError,
    RevitMCPConfig,
    _parse_envelope,
    feet_to_meters,
    meters_to_feet,
)
from tests._mocks import (
    SAMPLE_REVIT_ELEMENT_INFO,
    SAMPLE_REVIT_ROOMS,
    MockRevitMCPClient,
)


# ---- Unit converters -------------------------------------------------------


class TestUnits:
    def test_feet_to_meters_constant(self) -> None:
        assert FEET_TO_METERS == 0.3048

    def test_feet_to_meters_value(self) -> None:
        # 10' - 3" Unbounded Height in Studio 203 → ~3.124 m
        assert feet_to_meters(10.25) == pytest.approx(3.1242, rel=1e-3)

    def test_round_trip(self) -> None:
        assert meters_to_feet(feet_to_meters(7.5)) == pytest.approx(7.5, rel=1e-9)


# ---- RevitMCPConfig --------------------------------------------------------


class TestRevitMCPConfigFromEnv:
    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in (
            "REVIT_MCP_SERVER_CMD",
            "REVIT_MCP_SERVER_ARGS",
            "REVIT_MCP_SERVER_CWD",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = RevitMCPConfig.from_env()
        assert cfg.command == "node"
        assert cfg.args == []
        assert cfg.cwd is None

    def test_reads_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIT_MCP_SERVER_CMD", "node22")
        monkeypatch.setenv("REVIT_MCP_SERVER_ARGS", "dist/index.js --flag")
        monkeypatch.setenv("REVIT_MCP_SERVER_CWD", r"C:\Dev\RevitMCPServer\src\McpServer")
        cfg = RevitMCPConfig.from_env()
        assert cfg.command == "node22"
        assert cfg.args == ["dist/index.js", "--flag"]
        assert cfg.cwd == r"C:\Dev\RevitMCPServer\src\McpServer"

    def test_passes_through_revit_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIT_MCP_AUTH_TOKEN", "test-token-abc")
        monkeypatch.setenv("REVIT_MCP_PORT", "8123")
        monkeypatch.setenv("REVIT_MCP_VERSION", "2026")
        cfg = RevitMCPConfig.from_env()
        assert cfg.env.get("REVIT_MCP_AUTH_TOKEN") == "test-token-abc"
        assert cfg.env.get("REVIT_MCP_PORT") == "8123"
        assert cfg.env.get("REVIT_MCP_VERSION") == "2026"


# ---- _parse_envelope -------------------------------------------------------


class TestParseEnvelope:
    def test_parses_text_block_dict(self) -> None:
        envelope = {"ok": True, "data": {"pong": True}}
        content = [{"type": "text", "text": json.dumps(envelope)}]
        assert _parse_envelope(content) == envelope

    def test_parses_text_block_object(self) -> None:
        envelope = {"ok": True, "data": {"x": 1}}
        block = SimpleNamespace(type="text", text=json.dumps(envelope))
        assert _parse_envelope([block]) == envelope

    def test_skips_non_text_blocks(self) -> None:
        envelope = {"ok": True, "data": "v"}
        blocks: list[Any] = [
            {"type": "image", "data": "ignored"},
            {"type": "text", "text": json.dumps(envelope)},
        ]
        assert _parse_envelope(blocks) == envelope

    def test_returns_bad_envelope_when_empty(self) -> None:
        out = _parse_envelope([])
        assert out["ok"] is False
        assert out["error"]["code"] == "bad_envelope"

    def test_returns_bad_envelope_on_non_json(self) -> None:
        content = [{"type": "text", "text": "not-json"}]
        out = _parse_envelope(content)
        assert out["ok"] is False
        assert out["error"]["code"] == "bad_envelope"

    def test_handles_single_block_not_in_list(self) -> None:
        envelope = {"ok": True, "data": 42}
        block = {"type": "text", "text": json.dumps(envelope)}
        assert _parse_envelope(block) == envelope


# ---- RevitEnvelopeError ----------------------------------------------------


class TestEnvelopeError:
    def test_carries_code_and_message(self) -> None:
        err = RevitEnvelopeError(tool="revit_ping", code="unauthorized", message="x")
        assert err.tool == "revit_ping"
        assert err.code == "unauthorized"
        assert "unauthorized" in str(err)


# ---- MockRevitMCPClient ----------------------------------------------------


@pytest.mark.asyncio
class TestMockClientReads:
    async def test_ping(self) -> None:
        async with MockRevitMCPClient() as client:
            data = await client.ping()
        assert data["pong"] is True
        assert data["hasActiveDocument"] is True

    async def test_get_document_info(self) -> None:
        async with MockRevitMCPClient() as client:
            info = await client.get_document_info()
        assert info["title"] == "MockDocument"
        assert "displayUnitSystem" in info

    async def test_list_rooms_returns_full_fixture(self) -> None:
        async with MockRevitMCPClient() as client:
            rooms = await client.list_rooms()
        assert len(rooms) == len(SAMPLE_REVIT_ROOMS)
        assert {r["number"] for r in rooms} >= {"203", "P04", "201", "999"}

    async def test_get_element_info_known_id(self) -> None:
        async with MockRevitMCPClient() as client:
            info = await client.get_element_info(829712)
        assert info["name"] == "Studio Unit 203"
        names = [p["name"] for p in info["parameters"]]
        assert "Unbounded Height" in names
        assert "Department" in names

    async def test_get_element_info_unknown_id_raises(self) -> None:
        async with MockRevitMCPClient() as client:
            with pytest.raises(RevitEnvelopeError) as exc_info:
                await client.get_element_info(424242)
        assert exc_info.value.code == "not_found"

    async def test_get_element_geometry_derives_centroid(self) -> None:
        async with MockRevitMCPClient() as client:
            geom = await client.get_element_geometry(830966)
        bbox = geom["boundingBox"]
        cx = (bbox["min"]["x"] + bbox["max"]["x"]) / 2
        assert geom["centroid"]["x"] == pytest.approx(cx)
        assert geom["solidCount"] == 1


@pytest.mark.asyncio
class TestMockClientWrites:
    async def test_set_parameter_dry_run_does_not_mutate(self) -> None:
        async with MockRevitMCPClient() as client:
            envelope = await client.set_parameter(
                829712, "Comments", "BEP non-compliant", dry_run=True
            )
            after = await client.get_element_info(829712)
        assert envelope["ok"] is True
        assert envelope["dryRun"] is True
        assert envelope["committed"] is False
        comments = next(p for p in after["parameters"] if p["name"] == "Comments")
        assert comments["value"] is None  # unchanged

    async def test_set_parameter_commit_mutates(self) -> None:
        async with MockRevitMCPClient() as client:
            envelope = await client.set_parameter(
                829712, "Comments", "BEP §X.X non-compliant", dry_run=False
            )
            after = await client.get_element_info(829712)
        assert envelope["committed"] is True
        assert envelope["dryRun"] is False
        comments = next(p for p in after["parameters"] if p["name"] == "Comments")
        assert comments["value"] == "BEP §X.X non-compliant"

    async def test_set_parameter_returns_before_after_diff(self) -> None:
        async with MockRevitMCPClient() as client:
            envelope = await client.set_parameter(
                830966, "Comments", "Flagged", dry_run=False
            )
        changes = envelope["data"]["changes"]
        assert changes["before"] is None
        assert changes["after"] == "Flagged"

    async def test_rename_element_updates_name_and_param(self) -> None:
        async with MockRevitMCPClient() as client:
            envelope = await client.rename_element(999001, "Bedroom A", dry_run=False)
            info = await client.get_element_info(999001)
        assert envelope["data"]["changes"]["after"] == "Bedroom A"
        assert info["name"] == "Bedroom A"
        name_param = next(p for p in info["parameters"] if p["name"] == "Name")
        assert name_param["value"] == "Bedroom A"

    async def test_set_parameter_batch_counts_applied(self) -> None:
        async with MockRevitMCPClient() as client:
            envelope = await client.call_envelope(
                "revit_set_parameter_batch",
                {
                    "ids": [829712, 830966, 999001],
                    "parameterName": "Comments",
                    "value": "Batch flag",
                    "dryRun": False,
                },
            )
        assert envelope["data"]["count"] == 3
        assert envelope["committed"] is True


@pytest.mark.asyncio
class TestMockClientFailures:
    async def test_fail_on_raises_envelope_error(self) -> None:
        async with MockRevitMCPClient(fail_on={"revit_ping"}) as client:
            with pytest.raises(RevitEnvelopeError) as exc_info:
                await client.ping()
        assert exc_info.value.code == "simulated_failure"

    async def test_unknown_tool_raises(self) -> None:
        async with MockRevitMCPClient() as client:
            with pytest.raises(NotImplementedError):
                await client.call_envelope("revit_some_unknown_tool", {})


@pytest.mark.asyncio
class TestMockClientCallTracking:
    async def test_calls_to_filter(self) -> None:
        async with MockRevitMCPClient() as client:
            await client.ping()
            await client.list_rooms()
            await client.get_element_info(829712)
            await client.get_element_info(830966)
        assert len(client.calls_to("revit_get_element_info")) == 2
        assert len(client.calls_to("revit_ping")) == 1

    async def test_calls_recorded_in_order(self) -> None:
        async with MockRevitMCPClient() as client:
            await client.ping()
            await client.list_rooms()
        names = [name for name, _ in client.calls]
        assert names == ["revit_ping", "revit_list_rooms"]


# ---- Fixture self-checks (sanity) -------------------------------------------


class TestFixtureSelfChecks:
    """Lock in the violators we ship in SAMPLE_REVIT_ROOMS so rule tests can
    rely on them. Update these when the fixture changes."""

    def test_storage_p04_violates_area_min(self) -> None:
        p04 = next(r for r in SAMPLE_REVIT_ROOMS if r["number"] == "P04")
        assert p04["areaMetric"] < 9.0

    def test_bedroom_999_violates_bedroom_area_min(self) -> None:
        room = next(r for r in SAMPLE_REVIT_ROOMS if r["number"] == "999")
        assert "Bedroom" in room["name"]
        assert room["areaMetric"] < 10.0

    def test_number_203_has_duplicate(self) -> None:
        rooms_203 = [r for r in SAMPLE_REVIT_ROOMS if r["number"] == "203"]
        assert len(rooms_203) == 2

    def test_element_info_has_unbounded_height(self) -> None:
        for eid in (829712, 830966, 829648, 999001, 999002):
            params = SAMPLE_REVIT_ELEMENT_INFO[eid]["parameters"]
            heights = [p for p in params if p["name"] == "Unbounded Height"]
            assert heights and heights[0]["value"] is not None
