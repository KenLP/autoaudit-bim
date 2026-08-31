"""Tests for the approval-resume loop (v1.4-K5).

Propose side: DesignAgent gathers approve-gated Path B fixes into ONE proposal
issue + writes an approvals record. Watcher side: on issue status `in_progress`,
ApprovalWatcher applies the parked writes, closes the issue, marks applied.
"""

from __future__ import annotations

import json

import pytest

from bim_orchestrator.agents.design import DesignAgent
from bim_orchestrator.approval_watcher import (
    LIVENESS_DEAD_PASS_LIMIT,
    ApprovalWatcher,
    _commit_writes,
    _reprove_stale,
)
from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
from tests._mocks import MockFormaMCPClient, MockRevitMCPClient
from tests.test_design_agent_path_b import (
    _auto_rule_inferred,
    _autonomy,
    _finding,
    _room,
    _ruleset,
    _state,
)


def _agent(tmp_path, forma, revit, approvals):
    # autonomy=approve gates the (non-deterministic) infer fill → proposal issue.
    return DesignAgent(
        mcp=forma,
        autonomy=_autonomy(tmp_path, set_value="approve"),
        project_id="p1",
        max_issues=10,
        rule_filter=None,
        revit_mcp=revit,
        rules=_ruleset(_auto_rule_inferred()),
        approvals_dir=approvals,
    )


def _dept_findings():
    return [_finding(
        rule_id="room.department.required", element_id="829712",
        parameter="Department", suggested="Residential",
    )]


@pytest.mark.asyncio
class TestProposalIssue:
    async def test_approve_gated_path_b_creates_proposal_and_record(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        agent = _agent(tmp_path, forma, revit, approvals)
        state = await agent.run(
            _state(_dept_findings(), [_room("829712", "Studio 203", Department="")])
        )
        fix = state["proposed_fixes"][0]
        assert fix["autonomy"] == "approve" and fix["executed"] is False
        # No Revit write yet (gated)
        assert [c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is False] == []
        # One proposal record written
        recs = list(approvals.glob("*.json"))
        assert len(recs) == 1
        rec = json.loads(recs[0].read_text(encoding="utf-8"))
        assert rec["applied"] is False
        assert rec["status"] == "pending_approval"   # K14 lifecycle
        assert rec["issue_status"] == "open"
        f0 = rec["fixes"][0]
        assert f0["finding_id"] == fix["finding_id"]
        assert f0["element_id"] == "829712"   # instance-target rule → write target == instance
        assert f0["parameter"] == "Department"
        assert f0["new_value"] == "Residential"
        # Proposal issue exists in the store (status open)
        assert any(i["id"] == rec["issue_id"] for i in forma.issues)
        # v1.4-K5.1: description states the rule (once), the requirement, the
        # parameter, and old → new per element — not a bare repeated param list.
        desc = next(i for i in forma.issues if i["id"] == rec["issue_id"])["description"]
        assert "room.department.required" in desc       # rule id, stated once
        assert "Department required" in desc            # rule description
        assert "present and non-empty" in desc          # requirement spelled out
        assert "829712" in desc and "Residential" in desc  # element + proposed
        assert "_(empty)_" in desc                      # original value (blank)
        assert "→" in desc                              # old → new arrow

    async def test_per_rule_proposal_issues(self, tmp_path):
        """v1.4-K22: two Path-B rules → TWO proposal issues (one per rule), each
        listing only its own elements — NOT one combined issue mixing problems."""
        from bim_orchestrator.agents.qc import Rule

        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        rule2 = Rule.model_validate({
            "id": "room.comments.required", "parameter": "Comments",
            "requirement": "present_and_nonempty", "severity_tag": "missing_required_param",
            "description": "Comments required", "fixability": "auto",
            "remediation": {"action": "set_parameter", "target_parameter": "Comments",
                            "new_value_strategy": "inferred"},
            "autofill": {"strategy": "infer_from_room_name", "fallback": "General"},
        })
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="p1", max_issues=10, rule_filter=None, revit_mcp=revit,
            rules=_ruleset(_auto_rule_inferred(), rule2), approvals_dir=approvals,
        )
        findings = [
            _finding(rule_id="room.department.required", element_id="829712",
                     parameter="Department", suggested="Residential"),
            _finding(rule_id="room.comments.required", element_id="830966",
                     parameter="Comments", suggested="Note-1"),
        ]
        await agent.run(_state(findings, [
            _room("829712", "Studio 203", Department="", Comments=""),
            _room("830966", "Studio 204", Department="x", Comments=""),
        ]))
        records = [json.loads(p.read_text(encoding="utf-8"))
                   for p in approvals.glob("*.json")]
        # TWO records (one issue per rule); each covers exactly ONE rule
        assert len(records) == 2
        rules_per_record = [
            {f["finding_id"].split("::")[0] for f in rec["fixes"]} for rec in records
        ]
        assert all(len(rs) == 1 for rs in rules_per_record)
        assert {next(iter(rs)) for rs in rules_per_record} == {
            "room.department.required", "room.comments.required",
        }

    async def test_dry_run_only_skips_proposal(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        agent = DesignAgent(
            mcp=forma, autonomy=_autonomy(tmp_path, set_value="approve"),
            project_id="p1", max_issues=10, rule_filter=None, dry_run_only=True,
            revit_mcp=revit, rules=_ruleset(_auto_rule_inferred()), approvals_dir=approvals,
        )
        await agent.run(_state(_dept_findings(), [_room("829712", "S", Department="")]))
        assert not approvals.exists() or not list(approvals.glob("*.json"))


@pytest.mark.asyncio
class TestWatcher:
    async def _propose(self, tmp_path, forma, revit, approvals):
        agent = _agent(tmp_path, forma, revit, approvals)
        await agent.run(_state(_dept_findings(), [_room("829712", "S", Department="")]))
        rec_path = next(approvals.glob("*.json"))
        return rec_path, json.loads(rec_path.read_text(encoding="utf-8"))["issue_id"]

    async def test_in_progress_applies_and_closes(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        rec_path, issue_id = await self._propose(tmp_path, forma, revit, approvals)
        # Human approves: flip status
        next(i for i in forma.issues if i["id"] == issue_id)["status"] = "in_progress"

        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert len(applied) == 1
        # Write committed (batch path — mock supports it)
        steps = revit.calls_to("revit_batch")[0]["steps"]
        assert steps[0]["params"]["parameterName"] == "Department"
        assert steps[0]["params"]["value"] == "Residential"
        # Issue closed + record marked applied with explicit lifecycle (K14)
        assert next(i for i in forma.issues if i["id"] == issue_id)["status"] == "closed"
        rec_after = json.loads(rec_path.read_text(encoding="utf-8"))
        assert rec_after["applied"] is True
        assert rec_after["status"] == "applied"
        assert rec_after["issue_status"] == "closed"
        assert rec_after.get("applied_at")

    async def test_open_issue_is_skipped(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        rec_path, _ = await self._propose(tmp_path, forma, revit, approvals)
        # status stays "open" → not approved
        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert applied == []
        assert revit.calls_to("revit_batch") == []
        assert json.loads(rec_path.read_text(encoding="utf-8"))["applied"] is False

    async def test_already_applied_not_redone(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        rec_path, issue_id = await self._propose(tmp_path, forma, revit, approvals)
        next(i for i in forma.issues if i["id"] == issue_id)["status"] = "in_progress"
        w = ApprovalWatcher(approvals, forma, revit)
        await w.scan_once()
        batches_after_first = len(revit.calls_to("revit_batch"))
        # second pass: record applied → no new write
        applied2 = await w.scan_once()
        assert applied2 == []
        assert len(revit.calls_to("revit_batch")) == batches_after_first

    async def test_per_element_fallback_when_batch_unsupported(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma = MockFormaMCPClient(elements=[])
        revit = MockRevitMCPClient(unsupported_commands={"revit_batch"})
        rec_path, issue_id = await self._propose(tmp_path, forma, revit, approvals)
        next(i for i in forma.issues if i["id"] == issue_id)["status"] = "in_progress"
        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert len(applied) == 1
        commits = [c for c in revit.calls_to("revit_set_parameter")
                   if c["dryRun"] is False and c["parameterName"] == "Department"]
        assert len(commits) == 1


@pytest.mark.asyncio
class TestCommitWritesDispatch:
    """v1.4-K17 — a rename fix must dispatch rename_element, NOT set_parameter
    (the bug that left Family/FamilySymbol renames silently un-applied)."""

    _RENAME = {"element_id": "180235", "parameter": "Name",
               "new_value": "ADSK_Fur_Chair_Breuer", "action": "rename_element"}
    _SETP = {"element_id": "42", "parameter": "Department",
             "new_value": "Residential", "action": "set_parameter"}

    @staticmethod
    def _revit(**kw):
        return MockRevitMCPClient(element_info={
            180235: {"name": "M_Chair-Breuer", "parameters": []},
            42: {"name": "Room 1", "parameters": [{"name": "Department", "value": ""}]},
        }, **kw)

    async def test_batch_uses_rename_command(self, tmp_path):
        revit = self._revit()
        # copy: _commit_writes now stamps applied=True on the fix dicts (H3),
        # so pass fresh copies to keep the shared class-attr fixtures clean.
        n = await _commit_writes(revit, [dict(self._RENAME), dict(self._SETP)])
        assert n == 2
        steps = revit.calls_to("revit_batch")[0]["steps"]
        cmds = {s["command"] for s in steps}
        assert cmds == {"rename_element", "set_parameter"}
        rn = next(s for s in steps if s["command"] == "rename_element")
        assert rn["params"]["name"] == "ADSK_Fur_Chair_Breuer"
        assert "parameterName" not in rn["params"]   # NOT a set_parameter

    async def test_fallback_calls_rename_element(self, tmp_path):
        revit = self._revit(unsupported_commands={"revit_batch"})
        await _commit_writes(revit, [dict(self._RENAME)])
        renames = revit.calls_to("revit_rename_element")
        assert any(c.get("dryRun") is False and c.get("name") == "ADSK_Fur_Chair_Breuer"
                   for c in renames)
        # must NOT have tried to set a "Name" parameter
        assert not [c for c in revit.calls_to("revit_set_parameter")
                    if c.get("parameterName") == "Name"]

    async def test_missing_action_defaults_set_parameter(self, tmp_path):
        # old records (no action field) still set_parameter
        revit = self._revit()
        await _commit_writes(revit, [{"element_id": "42", "parameter": "Department",
                                      "new_value": "X"}])
        steps = revit.calls_to("revit_batch")[0]["steps"]
        assert steps[0]["command"] == "set_parameter"


# ---- H3: idempotency, partial-apply, project_id guard, lock -----------------

def _forma_in_progress(issue_id="iss-1"):
    f = MockFormaMCPClient(elements=[])
    f.issues.append({"id": issue_id, "status": "in_progress", "title": "t", "description": "d"})
    return f


def _revit_with(ids):
    return MockRevitMCPClient(element_info={
        i: {"name": f"Room {i}", "parameters": [{"name": "Department", "value": ""}]}
        for i in ids
    })


def _seed_record(approvals_dir, ids, *, issue_id="iss-1", project_id="p1"):
    approvals_dir.mkdir(parents=True, exist_ok=True)
    fixes = [{"finding_id": f"r::{i}", "element_id": str(i), "parameter": "Department",
              "new_value": "Residential", "action": "set_parameter"} for i in ids]
    rec = {"issue_id": issue_id, "project_id": project_id, "applied": False,
           "status": "pending_approval", "issue_status": "open", "fixes": fixes}
    p = approvals_dir / f"{issue_id}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


@pytest.mark.asyncio
class TestWatcherIdempotencyAndLock:
    async def test_partial_apply_retries_only_failed(self, tmp_path):
        # H3: one step fails → record stays partial, issue NOT closed; the next
        # poll retries ONLY the failed fix and never re-writes the applied one.
        approvals = tmp_path / "approvals"
        forma, revit = _forma_in_progress(), _revit_with([101, 102])
        revit.batch_fail_eids = {102}
        path = _seed_record(approvals, [101, 102])

        w = ApprovalWatcher(approvals, forma, revit)
        await w.scan_once()
        rec1 = json.loads(path.read_text(encoding="utf-8"))
        assert rec1["applied"] is False and rec1["status"] == "partial"
        assert rec1["fixes"][0]["applied"] is True
        assert rec1["fixes"][1].get("applied") is not True
        assert next(i for i in forma.issues if i["id"] == "iss-1")["status"] == "in_progress"

        revit.batch_fail_eids = set()          # transient failure clears
        await w.scan_once()
        second = revit.calls_to("revit_batch")[-1]["steps"]
        assert [s["params"]["id"] for s in second] == [102]   # 101 NOT re-written
        rec2 = json.loads(path.read_text(encoding="utf-8"))
        assert rec2["applied"] is True and rec2["status"] == "applied"
        assert next(i for i in forma.issues if i["id"] == "iss-1")["status"] == "closed"

    async def test_missing_project_id_does_not_kill_loop(self, tmp_path):
        # H3: a record missing project_id used to raise KeyError and kill the
        # watch loop. It must be skipped so a later valid record still applies.
        approvals = tmp_path / "approvals"
        forma, revit = _forma_in_progress("good"), _revit_with([201])
        approvals.mkdir(parents=True, exist_ok=True)
        (approvals / "bad.json").write_text(json.dumps({
            "issue_id": "bad", "applied": False,
            "fixes": [{"element_id": "999", "parameter": "Department", "new_value": "X"}],
        }), encoding="utf-8")
        _seed_record(approvals, [201], issue_id="good")

        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert [r["issue_id"] for r in applied] == ["good"]
        assert next(i for i in forma.issues if i["id"] == "good")["status"] == "closed"

    async def test_held_lock_skips_record(self, tmp_path):
        # H3: a per-record lock (held by another applier) makes the watcher skip
        # the record — no double-apply.
        approvals = tmp_path / "approvals"
        forma, revit = _forma_in_progress(), _revit_with([301])
        path = _seed_record(approvals, [301])
        (approvals / "iss-1.lock").write_text("other-pid", encoding="utf-8")  # held

        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert applied == []
        assert revit.calls_to("revit_batch") == []
        assert json.loads(path.read_text(encoding="utf-8"))["applied"] is False


# ---- Approval-security: fingerprint integrity gate --------------------------

from bim_orchestrator.policies.approval_integrity import fingerprint, parse_fingerprint


@pytest.mark.asyncio
class TestIntegrityGate:
    async def _propose(self, tmp_path, forma, revit, approvals):
        agent = _agent(tmp_path, forma, revit, approvals)
        await agent.run(_state(_dept_findings(), [_room("829712", "S", Department="")]))
        rec_path = next(approvals.glob("*.json"))
        return rec_path, json.loads(rec_path.read_text(encoding="utf-8"))["issue_id"]

    async def test_proposal_stamps_matching_fingerprint(self, tmp_path):
        # The record's fingerprint, the issue-body marker, and the fingerprint
        # recomputed from the record's fixes must all agree.
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        rec_path, issue_id = await self._propose(tmp_path, forma, revit, approvals)
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        desc = next(i for i in forma.issues if i["id"] == issue_id)["description"]
        assert rec["fingerprint"] == fingerprint(rec["fixes"])
        assert parse_fingerprint(desc) == rec["fingerprint"]

    async def test_tampered_record_is_refused(self, tmp_path):
        # An attacker rewrites the parked write AFTER approval → the watcher must
        # refuse (the writes no longer match the human-approved issue).
        approvals = tmp_path / "approvals"
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        rec_path, issue_id = await self._propose(tmp_path, forma, revit, approvals)
        next(i for i in forma.issues if i["id"] == issue_id)["status"] = "in_progress"
        # Tamper: swap the proposed value AND re-stamp the local fingerprint so
        # only the ACC-anchored one can catch it.
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        rec["fixes"][0]["new_value"] = "Malicious"
        rec["fingerprint"] = fingerprint(rec["fixes"])
        rec_path.write_text(json.dumps(rec), encoding="utf-8")

        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert applied == []                                   # not applied
        assert [c for c in revit.calls_to("revit_batch") if c["dryRun"] is False] == []
        assert next(i for i in forma.issues if i["id"] == issue_id)["status"] == "in_progress"
        after = json.loads(rec_path.read_text(encoding="utf-8"))
        assert after["applied"] is False
        assert after["status"] == "integrity_failed"

    async def test_legacy_record_without_fingerprint_still_applies(self, tmp_path):
        # A record + issue predating fingerprinting has no anchor → must NOT be
        # blocked (back-compat).
        approvals = tmp_path / "approvals"
        forma, revit = _forma_in_progress(), _revit_with([201])
        path = _seed_record(approvals, [201])          # no fingerprint, desc "d"
        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert [r["issue_id"] for r in applied] == ["iss-1"]
        assert json.loads(path.read_text(encoding="utf-8"))["applied"] is True


# ---- Approval-security: stale-value re-preview ------------------------------

def _seed_stale_record(approvals_dir, *, eid, old, new,
                       issue_id="iss-s", project_id="p1"):
    """Legacy-shaped record (no fingerprint → integrity passes) carrying an
    explicit old_value, so the stale-re-preview path can be exercised alone."""
    approvals_dir.mkdir(parents=True, exist_ok=True)
    fixes = [{"finding_id": "r::1", "element_id": str(eid), "parameter": "Department",
              "new_value": new, "old_value": old, "action": "set_parameter"}]
    rec = {"issue_id": issue_id, "project_id": project_id, "applied": False,
           "status": "pending_approval", "issue_status": "open", "fixes": fixes}
    p = approvals_dir / f"{issue_id}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def _revit_live(eid, value):
    return MockRevitMCPClient(element_info={
        eid: {"name": f"Room {eid}", "parameters": [{"name": "Department", "value": value}]}
    })


@pytest.mark.asyncio
class TestStaleRePreview:
    async def test_drifted_value_is_held_back(self, tmp_path):
        # old="Old" at propose, but the live model now reads a THIRD value a human
        # set → don't clobber it; hold the fix (issue stays open).
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-s")
        revit = _revit_live(555, "HumanEdit")
        path = _seed_stale_record(approvals, eid=555, old="Old", new="New")

        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert applied == []                                   # nothing landed
        assert [c for c in revit.calls_to("revit_batch") if c["dryRun"] is False] == []
        assert next(i for i in forma.issues if i["id"] == "iss-s")["status"] == "in_progress"
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["applied"] is False and rec["status"] == "partial"
        assert rec["fixes"][0]["stale"] is True
        assert rec["stale_writes"] == 1

    async def test_already_at_target_closes_without_rewrite(self, tmp_path):
        # A human already set the exact proposed value → mark satisfied (no write)
        # and let the issue close.
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-s")
        revit = _revit_live(555, "New")
        path = _seed_stale_record(approvals, eid=555, old="Old", new="New")

        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert [r["issue_id"] for r in applied] == ["iss-s"]
        assert [c for c in revit.calls_to("revit_batch") if c["dryRun"] is False] == []
        assert next(i for i in forma.issues if i["id"] == "iss-s")["status"] == "closed"
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["applied"] is True and rec["status"] == "applied"

    async def test_unchanged_value_applies_normally(self, tmp_path):
        # live == old (untouched since propose) → write proceeds.
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-s")
        revit = _revit_live(555, "Old")
        path = _seed_stale_record(approvals, eid=555, old="Old", new="New")

        await ApprovalWatcher(approvals, forma, revit).scan_once()
        committed = [c for c in revit.calls_to("revit_batch") if c["dryRun"] is False]
        assert committed and committed[0]["steps"][0]["params"]["value"] == "New"
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["applied"] is True and rec.get("stale_writes") == 0


# ---- M6: degraded stale re-preview (per-element fallback + fail-open flag) --

@pytest.mark.asyncio
class TestReproveStaleDegraded:
    async def test_batch_unsupported_falls_back_to_per_element_classification(self, tmp_path):
        # Addin lacks batch dry-run (older addin) but DOES support per-element
        # set_parameter dry-run — the re-preview must not silently fail open;
        # it should fall back and still classify unchanged/at-target/stale.
        revit = MockRevitMCPClient(
            unsupported_commands={"revit_batch"},
            element_info={555: {"name": "Room 555",
                                 "parameters": [{"name": "Department", "value": "Old"}]}},
        )
        pending = [{"element_id": "555", "parameter": "Department",
                    "new_value": "New", "old_value": "Old", "action": "set_parameter"}]
        to_write, degraded = await _reprove_stale(revit, pending)
        assert degraded is False
        assert len(to_write) == 1
        # per-element dry-run calls were made (not the unsupported batch)
        dry_calls = [c for c in revit.calls_to("revit_set_parameter") if c["dryRun"] is True]
        assert len(dry_calls) == 1

    async def test_batch_unsupported_per_element_detects_drift(self, tmp_path):
        # Per-element fallback must still catch genuine drift (not just
        # rubber-stamp everything as unchanged).
        revit = MockRevitMCPClient(
            unsupported_commands={"revit_batch"},
            element_info={555: {"name": "Room 555",
                                 "parameters": [{"name": "Department", "value": "HumanEdit"}]}},
        )
        pending = [{"element_id": "555", "parameter": "Department",
                    "new_value": "New", "old_value": "Old", "action": "set_parameter"}]
        to_write, degraded = await _reprove_stale(revit, pending)
        assert degraded is False
        assert to_write == []                 # held back
        assert pending[0]["stale"] is True

    async def test_both_batch_and_per_element_unavailable_fails_open_degraded(self, tmp_path):
        # Neither transport can re-preview at all → fail open (keep every
        # pending fix for write, per the conservative design) BUT flag
        # degraded=True so the caller can surface it (M6).
        class _DeadRevit:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                raise RevitEnvelopeError(tool="revit_batch", code="unknown_command", message="no")

            async def set_parameter(self, *a, **kw):
                raise RuntimeError("addin unreachable")

            async def rename_element(self, *a, **kw):
                raise RuntimeError("addin unreachable")

        pending = [{"element_id": "555", "parameter": "Department",
                    "new_value": "New", "old_value": "Old", "action": "set_parameter"}]
        to_write, degraded = await _reprove_stale(_DeadRevit(), pending)
        assert degraded is True
        assert to_write == pending             # fail-open: kept for write

    async def test_missing_old_value_fails_open_and_marks_degraded(self, tmp_path):
        # A fix with no captured baseline can't be judged either way — still
        # kept for write (fail-open, unchanged from before), but now also
        # flags the pass as degraded (a human should know the drift check
        # didn't truly cover this fix).
        revit = MockRevitMCPClient(
            element_info={555: {"name": "Room 555",
                                 "parameters": [{"name": "Department", "value": "Whatever"}]}},
        )
        pending = [{"element_id": "555", "parameter": "Department",
                    "new_value": "New", "action": "set_parameter"}]   # no old_value
        to_write, degraded = await _reprove_stale(revit, pending)
        assert degraded is True
        assert to_write == pending

    async def test_degraded_flag_persisted_on_record_and_close_comment(self, tmp_path):
        # End-to-end: when the ONLY judgeable path is fail-open-degraded, the
        # watcher must persist rec["degraded"] and append the note to the
        # close comment — not silently close as if everything were verified.
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-d")

        class _DeadRevit:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                if dry_run:
                    raise RevitEnvelopeError(
                        tool="revit_batch", code="unknown_command", message="no"
                    )
                # live commit still works (batch write path unaffected by the
                # dry-run-only re-preview outage)
                return {"ok": True, "committed": True,
                        "data": {"results": [{"ok": True}]}}

            async def set_parameter(self, *a, **kw):
                raise RuntimeError("addin unreachable")

            async def rename_element(self, *a, **kw):
                raise RuntimeError("addin unreachable")

        path = _seed_stale_record(approvals, eid=555, old="Old", new="New",
                                   issue_id="iss-d")
        applied = await ApprovalWatcher(approvals, forma, _DeadRevit()).scan_once()
        assert [r["issue_id"] for r in applied] == ["iss-d"]
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["degraded"] == "no_stale_check"
        comments = forma._comments.get("iss-d") or []
        assert any("degraded: no stale re-preview available" in c for c in comments)


# ---- Low8: stale re-preview reads old_value_raw defensively -----------------

@pytest.mark.asyncio
class TestLow8OldValueRaw:
    async def test_matches_raw_value_when_display_value_differs(self, tmp_path):
        # The proposal stashed a lossy display `old_value` (e.g. a formatted
        # string) alongside the raw parameter value `old_value_raw`. The live
        # model reads back the RAW form — must still classify as "unchanged".
        revit = MockRevitMCPClient(
            element_info={555: {"name": "Room 555",
                                 "parameters": [{"name": "Department", "value": "OLD_RAW"}]}},
        )
        pending = [{"element_id": "555", "parameter": "Department", "new_value": "New",
                    "old_value": "Old (display)", "old_value_raw": "OLD_RAW",
                    "action": "set_parameter"}]
        to_write, degraded = await _reprove_stale(revit, pending)
        assert degraded is False
        assert len(to_write) == 1
        assert pending[0]["stale"] is False

    async def test_still_flags_stale_when_neither_old_value_nor_raw_match(self, tmp_path):
        revit = MockRevitMCPClient(
            element_info={555: {"name": "Room 555",
                                 "parameters": [{"name": "Department", "value": "HumanEdit"}]}},
        )
        pending = [{"element_id": "555", "parameter": "Department", "new_value": "New",
                    "old_value": "Old (display)", "old_value_raw": "OLD_RAW",
                    "action": "set_parameter"}]
        to_write, degraded = await _reprove_stale(revit, pending)
        assert degraded is False
        assert to_write == []
        assert pending[0]["stale"] is True


# ---- M7: persist-before-close + retry close ---------------------------------

@pytest.mark.asyncio
class TestPersistBeforeCloseAndRetry:
    async def test_close_failure_leaves_record_applied_pending_close(self, tmp_path):
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-c")
        revit = _revit_with([701])
        path = _seed_record(approvals, [701], issue_id="iss-c")

        class _FailCloseForma:
            """Wraps MockFormaMCPClient; get_issue passes through, update_issue
            (the close call) always raises — models a transient ACC failure
            AFTER the Revit writes already landed."""
            def __init__(self, inner):
                self._inner = inner

            async def get_issue(self, *a, **kw):
                return await self._inner.get_issue(*a, **kw)

            async def update_issue(self, *a, **kw):
                raise RuntimeError("ACC transiently unavailable")

            async def add_issue_comment(self, *a, **kw):
                return await self._inner.add_issue_comment(*a, **kw)

        wrapped = _FailCloseForma(forma)
        applied = await ApprovalWatcher(approvals, wrapped, revit).scan_once()
        # Writes landed (progress made) even though the close call failed —
        # _apply returns True via `applied_now > 0`.
        assert len(applied) == 1
        rec = json.loads(path.read_text(encoding="utf-8"))
        # M7 (a): the record was persisted with the writes reflected BEFORE
        # the close attempt, and stays at applied_pending_close since close
        # failed — never silently reverted to "partial" or left un-persisted.
        assert rec["applied"] is True
        assert rec["status"] == "applied"
        assert rec["issue_status"] == "applied_pending_close"
        assert rec["fixes"][0]["applied"] is True
        # Issue itself was never closed in ACC.
        assert next(i for i in forma.issues if i["id"] == "iss-c")["status"] == "in_progress"

    async def test_scan_once_retries_close_only_without_rewriting(self, tmp_path):
        # First pass: writes land, close fails (transient). Second pass: close
        # succeeds — must NOT re-invoke _commit_writes (no new revit_batch call
        # with dryRun=False), only the close/comment calls.
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-c2")
        revit = _revit_with([702])
        path = _seed_record(approvals, [702], issue_id="iss-c2")

        state = {"fail_close": True}

        class _FlakyCloseForma:
            def __init__(self, inner):
                self._inner = inner

            async def get_issue(self, *a, **kw):
                return await self._inner.get_issue(*a, **kw)

            async def update_issue(self, *a, **kw):
                if state["fail_close"]:
                    raise RuntimeError("ACC transiently unavailable")
                return await self._inner.update_issue(*a, **kw)

            async def add_issue_comment(self, *a, **kw):
                return await self._inner.add_issue_comment(*a, **kw)

        wrapped = _FlakyCloseForma(forma)
        watcher = ApprovalWatcher(approvals, wrapped, revit)
        await watcher.scan_once()
        rec1 = json.loads(path.read_text(encoding="utf-8"))
        assert rec1["issue_status"] == "applied_pending_close"
        writes_after_first = len(revit.calls_to("revit_batch"))

        state["fail_close"] = False
        applied2 = await watcher.scan_once()
        assert [r["issue_id"] for r in applied2] == ["iss-c2"]
        # No NEW batch write call — only the close/comment retried.
        assert len(revit.calls_to("revit_batch")) == writes_after_first
        rec2 = json.loads(path.read_text(encoding="utf-8"))
        assert rec2["issue_status"] == "closed"
        assert next(i for i in forma.issues if i["id"] == "iss-c2")["status"] == "closed"


# ---- Low7: fingerprint anchor-downgraded warning ----------------------------

@pytest.mark.asyncio
class TestLow7AnchorDowngraded:
    async def test_record_only_fingerprint_still_passes_and_warns(self, tmp_path, caplog):
        # Description has NO fingerprint marker, but the record itself carries
        # one (e.g. an older ACC edit stripped the marker from the body). The
        # gate must still pass (record-fingerprint fallback) but log a warning
        # so an operator can see the anchor degraded to the weaker source.
        approvals = tmp_path / "approvals"
        forma = _forma_in_progress("iss-w")
        revit = _revit_with([801])
        path = _seed_record(approvals, [801], issue_id="iss-w")
        rec = json.loads(path.read_text(encoding="utf-8"))
        from bim_orchestrator.policies.approval_integrity import fingerprint
        rec["fingerprint"] = fingerprint(rec["fixes"])
        path.write_text(json.dumps(rec), encoding="utf-8")
        # forma issue's description has no marker (plain "d")
        assert "AutoAudit-Fingerprint" not in next(
            i for i in forma.issues if i["id"] == "iss-w")["description"]

        import structlog
        structlog.configure(
            processors=[structlog.processors.add_log_level, structlog.dev.ConsoleRenderer()]
        )
        applied = await ApprovalWatcher(approvals, forma, revit).scan_once()
        assert [r["issue_id"] for r in applied] == ["iss-w"]   # not blocked


# ---- Low10: watch() liveness escalation -------------------------------------

@pytest.mark.asyncio
class TestLow10LivenessEscalation:
    async def test_all_get_issue_failures_reset_by_one_success(self, tmp_path):
        approvals = tmp_path / "approvals"

        class _FlakyGetIssueForma:
            """get_issue fails for `iss-fail-1` every pass but succeeds for
            `iss-ok` every pass — `iss-ok` stays `open` (never applies/closes)
            so it keeps being polled pass after pass, proving a per-pass
            success resets the dead-pass counter even while another record
            never resolves."""
            def __init__(self):
                self.issues = [{"id": "iss-ok", "status": "open",
                                "title": "t", "description": "d"}]

            async def get_issue(self, project_id, issue_id):
                if issue_id != "iss-ok":
                    raise RuntimeError("simulated ACC failure")
                return {"issue": next(i for i in self.issues if i["id"] == issue_id)}

            async def update_issue(self, *a, dry_run=True, **kw):
                if dry_run:
                    return {"approval_token": "tok"}
                return {"issue": {}}

            async def add_issue_comment(self, *a, dry_run=True, **kw):
                if dry_run:
                    return {"approval_token": "tok"}
                return {"comment": {}}

        revit = _revit_with([901, 902])
        forma = _FlakyGetIssueForma()
        _seed_record(approvals, [901], issue_id="iss-fail-1", project_id="p1")
        _seed_record(approvals, [902], issue_id="iss-ok", project_id="p1")

        watcher = ApprovalWatcher(approvals, forma, revit)
        # Run enough passes that WOULD trip the liveness limit if the one
        # success per pass didn't reset the counter each time.
        await watcher.watch(interval_s=0, max_passes=LIVENESS_DEAD_PASS_LIMIT + 2)
        # No exception raised — success in every pass resets the counter.

    async def test_liveness_lost_raises_after_limit(self, tmp_path):
        approvals = tmp_path / "approvals"

        class _AlwaysDeadForma:
            async def get_issue(self, project_id, issue_id):
                raise RuntimeError("simulated ACC outage")

        revit = _revit_with([902])
        _seed_record(approvals, [902], issue_id="iss-dead", project_id="p1")
        watcher = ApprovalWatcher(approvals, _AlwaysDeadForma(), revit)
        with pytest.raises(RuntimeError, match="consecutive passes"):
            await watcher.watch(interval_s=0, max_passes=LIVENESS_DEAD_PASS_LIMIT + 5)

    async def test_no_records_due_is_neutral_not_dead(self, tmp_path):
        # An empty approvals dir → zero get_issue attempts per pass → must
        # NOT be treated as a liveness failure (nothing to prove one way or
        # the other).
        approvals = tmp_path / "approvals"
        approvals.mkdir(parents=True, exist_ok=True)
        forma, revit = MockFormaMCPClient(elements=[]), MockRevitMCPClient()
        watcher = ApprovalWatcher(approvals, forma, revit)
        await watcher.watch(interval_s=0, max_passes=LIVENESS_DEAD_PASS_LIMIT + 5)
        # No exception — passes with zero attempts don't count toward the limit.

    async def test_demo_records_never_polled_nor_counted_toward_liveness(self, tmp_path):
        """C-2 (review round 7, 2026-08-17): demo records (project_id
        "demo-villa-simulated" — CLI --demo leftovers or the seeded webUI-demo
        fixtures) point at a SIMULATED project, so `get_issue` against real
        ACC always throws. A dir whose only pending records were demo ones
        used to read "every attempt failing, pass after pass" → the watcher
        declared ACC unreachable and died while ACC was healthy. Demo records
        are now skipped before any attempt is made."""
        approvals = tmp_path / "approvals"

        class _WouldExplodeForma:
            calls = 0

            async def get_issue(self, project_id, issue_id):
                type(self).calls += 1
                raise RuntimeError("mock issue id polled against real ACC")

        revit = _revit_with([902])
        _seed_record(approvals, [902], issue_id="issue-mock-0001",
                     project_id="demo-villa-simulated")
        forma = _WouldExplodeForma()
        watcher = ApprovalWatcher(approvals, forma, revit)
        await watcher.watch(interval_s=0, max_passes=LIVENESS_DEAD_PASS_LIMIT + 5)
        # No liveness exception, and the simulated project was never polled.
        assert forma.calls == 0
        assert watcher.last_pass_get_issue_attempts == 0


def test_demo_project_constant_matches_the_demo_package():
    """C-2: the watcher guard duplicates the id so PRODUCTION code never
    imports the demo package — this pins the two constants equal, so a rename
    on either side fails loudly instead of silently disarming the guard."""
    from bim_orchestrator.approval_watcher import _DEMO_PROJECT_ID
    from bim_orchestrator.demo import DEMO_PROJECT_ID

    assert _DEMO_PROJECT_ID == DEMO_PROJECT_ID


class TestUnmeasuredReprovalIsNotDrift:
    """L-04 — a dry-run step that reports ok but carries no ``changes`` was
    never measured, and must not be reported as a drifted value.

    ``.get("before")`` on the missing dict yielded None, ``_norm`` turned that
    into "", and ``_classify_stale`` read it as a THIRD value — "a human
    edited this between propose and approve". The fix was then held back for
    good (the issue never closes, so every later pass repeats the same broken
    measurement) with ``degraded`` unset, because the index WAS in
    ``live_before``. The run asserted drift it had no evidence for — exactly
    the posture ``batch_commit_outcomes`` already forbids on the commit side
    ("missing results is unconfirmed, not success").
    """

    @staticmethod
    def _pending():
        return [{"element_id": "555", "parameter": "Department",
                 "new_value": "New", "old_value": "Old",
                 "action": "set_parameter"}]

    @pytest.mark.asyncio
    async def test_batch_step_without_changes_is_degraded_not_stale(self):
        class _MuteBatch:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                # ok, but no measurement — an older addin, or a step whose
                # preview reports nothing.
                return {"ok": True, "data": {"results": [{"ok": True, "id": 555}]}}

        pending = self._pending()
        to_write, degraded = await _reprove_stale(_MuteBatch(), pending)
        assert degraded is True, "unmeasured must set degraded (M6 fail-open)"
        assert to_write == pending
        assert pending[0]["stale"] is False, "not measured is not 'drifted'"

    @pytest.mark.asyncio
    async def test_a_step_carrying_an_error_is_also_unmeasured(self):
        class _ErrorStep:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                return {"ok": True, "data": {"results": [{"error": "boom"}]}}

        pending = self._pending()
        to_write, degraded = await _reprove_stale(_ErrorStep(), pending)
        assert degraded is True
        assert to_write == pending
        assert pending[0]["stale"] is False

    @pytest.mark.asyncio
    async def test_per_element_envelope_without_changes_is_degraded(self):
        class _MutePerElement:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                raise RevitEnvelopeError(
                    tool="revit_batch", code="unknown_command", message="no"
                )

            async def set_parameter(self, *a, **kw):
                return {"ok": True, "data": {}}

            async def rename_element(self, *a, **kw):
                return {"ok": True, "data": {}}

        pending = self._pending()
        to_write, degraded = await _reprove_stale(_MutePerElement(), pending)
        assert degraded is True
        assert to_write == pending
        assert pending[0]["stale"] is False

    @pytest.mark.asyncio
    async def test_a_real_empty_reading_is_still_a_reading(self):
        """The guard is on the CONTAINER, not the value: ``changes`` present
        with ``before: None`` IS a measurement (an empty parameter) and must
        keep matching an empty ``old_value`` — otherwise this fix would turn
        every blank parameter, the very class Path B exists to fill, into a
        degraded fail-open."""
        class _EmptyButMeasured:
            async def batch(self, steps, *, dry_run=True, stop_on_error=True):
                return {"ok": True, "data": {"results": [
                    {"ok": True, "changes": {"before": None, "after": "New"}}
                ]}}

        pending = [{"element_id": "555", "parameter": "Department",
                    "new_value": "New", "old_value": "",
                    "action": "set_parameter"}]
        to_write, degraded = await _reprove_stale(_EmptyButMeasured(), pending)
        assert degraded is False, "a measured empty value is not 'unmeasured'"
        assert to_write == pending
        assert pending[0]["stale"] is False
