"""v1 task B-3: tests for the uniqueId → Forma Viewer dbId mapping helper
and the build_linked_document() schema constructor.

R1 was resolved by acc-forma-mcp-server on 2026-05-31 (linked_documents
field added to issues_create). R1b is the remaining mapping question:
where does objectId come from? This module locks the contract for the
mapping helper:

  * Best-effort scan of element properties for any externalId / dbId field
  * Returns empty dict gracefully when no candidates found (no crash)
  * Caller (DesignAgent) ships issues without "View in Model" button when
    mapping is empty -- not a fatal degradation.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.mcp_clients.forma import (
    _first_int,
    build_linked_document,
)


# ── _first_int helper ───────────────────────────────────────────────────────


def test_first_int_returns_first_matching_key():
    d = {"a": "1", "b": "2", "c": "3"}
    assert _first_int(d, ("b", "a", "c")) == 2


def test_first_int_returns_none_when_no_match():
    d = {"a": "1"}
    assert _first_int(d, ("x", "y", "z")) is None


def test_first_int_skips_unparseable_values_and_continues():
    d = {"a": "not-an-int", "b": "42"}
    assert _first_int(d, ("a", "b")) == 42


def test_first_int_skips_none_values():
    d = {"a": None, "b": 7}
    assert _first_int(d, ("a", "b")) == 7


def test_first_int_handles_int_passthrough():
    d = {"dbId": 1234}
    assert _first_int(d, ("dbId",)) == 1234


# ── get_object_id_map ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_object_id_map_finds_revit_element_id_in_params():
    """v1 B-3 live probe (2026-06-01): AECDM exposes 'Revit Element ID' as
    the integer-valued property that best matches Forma Viewer dbId for
    Revit-sourced models. Locked here so future AECDM property renames
    surface immediately."""
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient

    # We don't need a real connection -- only the helper logic.
    client = FormaMCPClient.__new__(FormaMCPClient)

    elements = [
        {"id": "urn:adsk:elem-1", "params": {"Revit Element ID": "555"}},
        {"id": "urn:adsk:elem-2", "params": {"dbId": 9999}},
        # No mapping field -- skipped silently
        {"id": "urn:adsk:elem-3", "params": {"OtherProp": "value"}},
    ]
    result = await client.get_object_id_map("eg-x", elements)
    assert result == {
        "urn:adsk:elem-1": 555,
        "urn:adsk:elem-2": 9999,
    }


def test_extract_revit_unique_id_finds_external_id():
    """AECDM 'External ID' property = Revit UniqueId (GUID string)."""
    from bim_orchestrator.mcp_clients.forma import extract_revit_unique_id
    el = {"id": "urn:x", "params": {"External ID": "abc-123-def-456"}}
    assert extract_revit_unique_id(el) == "abc-123-def-456"


def test_extract_revit_element_id_finds_integer():
    """AECDM 'Revit Element ID' property = Revit ElementId (int, best
    heuristic for Forma Viewer dbId for Revit-sourced models)."""
    from bim_orchestrator.mcp_clients.forma import extract_revit_element_id
    el = {"id": "urn:x", "params": {"Revit Element ID": "4401618"}}
    assert extract_revit_element_id(el) == 4401618


def test_extractors_return_none_when_property_absent():
    from bim_orchestrator.mcp_clients.forma import (
        extract_revit_element_id,
        extract_revit_unique_id,
    )
    el = {"id": "urn:x", "params": {"Department": "Sales"}}
    assert extract_revit_unique_id(el) is None
    assert extract_revit_element_id(el) is None


@pytest.mark.asyncio
async def test_get_object_id_map_returns_empty_when_no_candidates():
    """Graceful degradation: MCP server doesn't expose externalId yet.

    This is the v1-current state -- helper returns {} and the caller
    proceeds without linked_documents. Locked here so future server
    upgrades won't break the contract.
    """
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient

    client = FormaMCPClient.__new__(FormaMCPClient)
    elements = [
        {"id": "urn:adsk:elem-1", "params": {"Department": "Sales"}},
        {"id": "urn:adsk:elem-2", "params": {"Area": 12.5}},
    ]
    result = await client.get_object_id_map("eg-x", elements)
    assert result == {}


@pytest.mark.asyncio
async def test_get_object_id_map_skips_elements_without_id():
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient

    client = FormaMCPClient.__new__(FormaMCPClient)
    elements = [
        {"name": "no-id", "params": {"dbId": 1}},  # silently skipped
        {"id": "urn:adsk:elem-2", "params": {"dbId": 2}},
    ]
    result = await client.get_object_id_map("eg-x", elements)
    assert result == {"urn:adsk:elem-2": 2}


@pytest.mark.asyncio
async def test_get_object_id_map_handles_empty_elements_list():
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient

    client = FormaMCPClient.__new__(FormaMCPClient)
    assert await client.get_object_id_map("eg", []) == {}


# ── build_linked_document ───────────────────────────────────────────────────


def test_build_linked_document_minimal_3d_pin():
    out = build_linked_document(
        document_lineage_urn="urn:adsk.wipprod:dm.lineage:abc",
        created_at_version=7,
        viewable_guid="3d-guid-xyz",
        object_id=4401618,
    )
    assert out == {
        "type": "ThreeDVectorPushpin",
        "urn": "urn:adsk.wipprod:dm.lineage:abc",
        "createdAtVersion": 7,
        "details": {
            "viewable": {"guid": "3d-guid-xyz", "is3D": True},
            "objectId": 4401618,
        },
    }


def test_build_linked_document_2d_pin_when_is_3d_false():
    out = build_linked_document(
        document_lineage_urn="urn:adsk.wipprod:dm.lineage:abc",
        created_at_version=1,
        viewable_guid="sheet-guid",
        object_id=42,
        is_3d=False,
    )
    assert out["type"] == "TwoDVectorPushpin"
    assert out["details"]["viewable"]["is3D"] is False


def test_build_linked_document_includes_position_when_given():
    out = build_linked_document(
        document_lineage_urn="urn:adsk.wipprod:dm.lineage:abc",
        created_at_version=7,
        viewable_guid="g",
        object_id=1,
        position={"x": 1.0, "y": 2.0, "z": 3.0},
    )
    assert out["details"]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}


def test_build_linked_document_omits_position_when_none():
    out = build_linked_document(
        document_lineage_urn="urn:adsk.wipprod:dm.lineage:abc",
        created_at_version=7,
        viewable_guid="g",
        object_id=1,
    )
    assert "position" not in out["details"]


def test_build_linked_document_lineage_urn_format_doc():
    """Schema contract -- urn MUST be a lineage URN, not a version URN.
    The function does not validate (server-side rejects bad URNs) but the
    docstring warns. Lock the prefix so future refactors keep the doc accurate.
    """
    out = build_linked_document(
        document_lineage_urn="urn:adsk.wipprod:dm.lineage:abc",
        created_at_version=1, viewable_guid="g", object_id=1,
    )
    assert out["urn"].startswith("urn:adsk.wipprod:dm.lineage:")
