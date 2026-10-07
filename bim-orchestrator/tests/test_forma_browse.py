"""v1.7-R25 — the project picker's read side, in the MCP client.

Parsing the AECDM list tools' TEXT output used to live in
``streamlit_app/app.py``; the web UI's Setup card needed the same thing, so it
moved to ``mcp_clients/forma.py`` and Streamlit re-exports it. These tests pin
the two LIVE shapes (dates in the parser docstrings) plus the browse pair,
which is what the service route calls.

No Forma here: ``browse_*`` is exercised against a fake that answers the two
``list_*`` wrappers, which is exactly the seam the routes monkeypatch too.
"""

from __future__ import annotations

import asyncio

import pytest

from bim_orchestrator.mcp_clients.forma import (
    FormaMCPClient,
    FormaNamed,
    FormaProject,
    parse_aecdm_projects,
    parse_name_id_list,
)


class _T:
    """A stand-in for MCP's TextContent — the parsers only read ``.text``."""

    def __init__(self, text: str) -> None:
        self.text = text


# The 2026-06-11 shape: one line per entry, id in parentheses, emoji prefix.
_HUB_BLOB = (
    "Found 1 AEC hub(s):\n\n"
    "🏢 Ken's Hub  (ID: urn:adsk.ace:prod.scope:00000000-0000-0000-0000-0000000000aa)\n"
)

# The 2026-06-19 dual-id shape: a block per project.
_PROJECT_BLOB = (
    "Found 2 AEC project(s):\n\n"
    "• Some Office\n"
    "    AECDM id: urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000008\n"
    "    DM/Issues id: b.00000000-0000-0000-0000-000000000007\n"
    "• Sample ACC Project\n"
    "    AECDM id: urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000002\n"
    "    DM/Issues id: b.00000000-0000-0000-0000-000000000001\n"
)


class TestParseNameIdList:
    def test_reads_name_and_id_stripping_the_emoji(self) -> None:
        assert parse_name_id_list([_T(_HUB_BLOB)]) == [
            FormaNamed(
                id="urn:adsk.ace:prod.scope:00000000-0000-0000-0000-0000000000aa",
                name="Ken's Hub",
            )
        ]

    def test_accepts_a_bare_item_not_in_a_list(self) -> None:
        assert parse_name_id_list(_T(_HUB_BLOB))[0].name == "Ken's Hub"

    def test_a_nameless_line_falls_back_to_the_id(self) -> None:
        """An entry is never dropped for lacking a display name — a picker
        showing a truncated id still lets the user select it."""
        rows = parse_name_id_list([_T("  (ID: urn:adsk.workspace:prod.project:zzz)")])
        urn = "urn:adsk.workspace:prod.project:zzz"
        assert rows == [FormaNamed(id=urn, name=urn)]

    def test_lines_without_an_id_are_ignored(self) -> None:
        assert parse_name_id_list([_T("Found 0 AEC hub(s):\n\nnothing here\n")]) == []

    def test_a_bullet_prefix_is_not_part_of_the_name(self) -> None:
        """Live shape, 2026-09-05: hubs and element groups come back bulleted,
        not emoji-prefixed. U+2022 sits below the emoji blocks, so the class
        ported from Streamlit left it glued to the name and the picker showed
        "• Autodesk APAC TS"."""
        rows = parse_name_id_list([_T("• Autodesk APAC TS  (ID: urn:adsk.ace:prod.scope:aa)")])
        assert rows == [FormaNamed(id="urn:adsk.ace:prod.scope:aa", name="Autodesk APAC TS")]

    def test_an_emoji_in_presentation_form_leaves_no_stray_selector(self) -> None:
        """🏗️ is TWO codepoints (U+1F3D7 U+FE0F). The class ported from
        Streamlit matched only the first, so the variation selector stayed
        glued to the name and rendered as a stray character in the picker."""
        rows = parse_name_id_list([_T("🏗️ Snowdon Towers  (ID: urn:x)")])
        assert rows == [FormaNamed(id="urn:x", name="Snowdon Towers")]


class TestParseAecdmProjects:
    def test_extracts_both_ids_per_project(self) -> None:
        assert parse_aecdm_projects([_T(_PROJECT_BLOB)]) == [
            FormaProject(
                name="Some Office",
                aecdm_id="urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000008",
                dm_id="b.00000000-0000-0000-0000-000000000007",
            ),
            FormaProject(
                name="Sample ACC Project",
                aecdm_id="urn:adsk.workspace:prod.project:00000000-0000-0000-0000-000000000002",
                dm_id="b.00000000-0000-0000-0000-000000000001",
            ),
        ]

    def test_project_without_dm_container_keeps_dm_id_empty(self) -> None:
        rows = parse_aecdm_projects(
            [_T("• No Container Project\n    AECDM id: urn:adsk.workspace:prod.project:abc\n")]
        )
        assert rows == [
            FormaProject(
                name="No Container Project",
                aecdm_id="urn:adsk.workspace:prod.project:abc",
                dm_id="",
            )
        ]

    def test_a_block_without_an_aecdm_id_is_dropped(self) -> None:
        """The AECDM id is what every downstream call needs; a block missing
        it is not a selectable project."""
        assert parse_aecdm_projects([_T("• Half A Project\n    DM/Issues id: b.123\n")]) == []


class _FakeClient(FormaMCPClient):
    """Answers the two list wrappers; never spawns the MCP subprocess."""

    def __init__(self, hubs_raw: object, projects_raw: object, groups_raw: object = None) -> None:
        self._hubs_raw = hubs_raw
        self._projects_raw = projects_raw
        self._groups_raw = groups_raw
        self.asked_hub: str | None = None
        self.asked_project: str | None = None

    async def list_aecdm_hubs(self):  # type: ignore[override]
        return self._hubs_raw

    async def list_aecdm_projects(self, hub_id: str):  # type: ignore[override]
        self.asked_hub = hub_id
        return self._projects_raw

    async def list_element_groups(self, project_id: str):  # type: ignore[override]
        self.asked_project = project_id
        return self._groups_raw


class TestBrowse:
    def test_browse_projects_uses_the_first_hub_urn(self) -> None:
        """The DM hub id in .env is b.<uuid>, which AECDM rejects — the hub
        URN has to come from list_aecdm_hubs, not from config."""
        client = _FakeClient([_T(_HUB_BLOB)], [_T(_PROJECT_BLOB)])
        hub, projects = asyncio.run(client.browse_projects())
        assert hub.name == "Ken's Hub"
        assert client.asked_hub == hub.id
        assert [p.name for p in projects] == ["Some Office", "Sample ACC Project"]

    def test_browse_projects_raises_when_no_hub_answers(self) -> None:
        client = _FakeClient([_T("Found 0 AEC hub(s):\n")], [])
        with pytest.raises(RuntimeError, match="No AECDM hubs"):
            asyncio.run(client.browse_projects())

    def test_browse_element_groups_passes_the_aecdm_project_id(self) -> None:
        blob = (
            "Found 1 element group(s):\n\n"
            "🏗️ Snowdon Towers  (ID: urn:adsk.wipprod:fs.file:vf.eg1)\n"
        )
        client = _FakeClient([], [], [_T(blob)])
        groups = asyncio.run(
            client.browse_element_groups("urn:adsk.workspace:prod.project:p1")
        )
        assert client.asked_project == "urn:adsk.workspace:prod.project:p1"
        assert groups == [FormaNamed(id="urn:adsk.wipprod:fs.file:vf.eg1", name="Snowdon Towers")]


# The Streamlit side keeps its legacy tuple shapes; those are pinned where the
# stubbed-streamlit import machinery already lives —
# tests/test_streamlit_argv.py::TestParseAecdmProjects (dual ids, empty dm_id)
# and ::TestReExportsClientParsers (the name/id wrapper).
