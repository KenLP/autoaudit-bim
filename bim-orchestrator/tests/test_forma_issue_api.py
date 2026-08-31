"""Tests for the Forma issue read/update wrappers (v1.4-K5 — approval watcher).

Covers list/get + the dry_run→approval_token guardrail on update/add_comment,
mirroring the live acc-forma-mcp-server contract.
"""

from __future__ import annotations

import pytest

from tests._mocks import MockFormaMCPClient


@pytest.mark.asyncio
class TestFormaIssueApi:
    async def test_list_and_get(self):
        c = MockFormaMCPClient(
            issues=[{"id": "i1", "displayId": 1, "status": "open", "title": "T"}]
        )
        async with c:
            lst = await c.list_issues("p1", limit=5)
            assert [i["id"] for i in lst["issues"]] == ["i1"]
            got = await c.get_issue("p1", "i1")
            assert got["issue"]["status"] == "open"
            assert "in_progress" in got["issue"]["permittedStatuses"]

    async def test_update_guardrail(self):
        c = MockFormaMCPClient(issues=[{"id": "i1", "status": "in_progress", "title": "T"}])
        async with c:
            preview = await c.update_issue("p1", "i1", status="closed", dry_run=True)
            assert preview["approval_token"]
            # dry-run did not mutate
            assert (await c.get_issue("p1", "i1"))["issue"]["status"] == "in_progress"
            await c.update_issue(
                "p1", "i1", status="closed",
                dry_run=False, approval_token=preview["approval_token"],
            )
            assert (await c.get_issue("p1", "i1"))["issue"]["status"] == "closed"

    async def test_update_execute_requires_token(self):
        c = MockFormaMCPClient(issues=[{"id": "i1", "status": "open"}])
        async with c:
            with pytest.raises(RuntimeError):
                await c.update_issue("p1", "i1", status="closed", dry_run=False)

    async def test_add_comment_guardrail(self):
        c = MockFormaMCPClient(issues=[{"id": "i1", "status": "open"}])
        async with c:
            preview = await c.add_issue_comment("p1", "i1", "applied", dry_run=True)
            assert preview["approval_token"]
            assert c._comments.get("i1") is None  # noqa: SLF001
            await c.add_issue_comment(
                "p1", "i1", "applied",
                dry_run=False, approval_token=preview["approval_token"],
            )
            assert c._comments["i1"] == ["applied"]  # noqa: SLF001
