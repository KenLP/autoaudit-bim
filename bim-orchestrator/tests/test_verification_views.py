"""Tests for verification_views — auto-creating native Revit verification schedules.

Proves: one schedule per rule, category resolved to a BuiltInCategory, sort/group
configured (group for uniqueness), generality across requirement types, and
graceful degradation when the addin lacks the schedule tools. All against the
mock Revit client (1:1 protocol parity) — no live Revit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bim_orchestrator.agents.qc import QCAgent
from bim_orchestrator.agents.revit_query import RevitQueryAgent
from bim_orchestrator.policies.autonomy import AutonomyPolicy
from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill
from bim_orchestrator.report_trace import build_check_record
from bim_orchestrator.verification_views import (
    create_verification_schedules,
    manifest_dict,
    render_manifest_markdown,
)
from tests._mocks import MockRevitMCPClient

CONFIG = Path(__file__).resolve().parents[1] / "config"


def mk_rule(requirement: str, **kw) -> Rule:
    defaults = dict(
        id=kw.pop("id", f"test.{requirement}"),
        parameter=kw.pop("parameter", "X"),
        requirement=requirement,
        severity_tag="rule_violation",
        description="view test rule",
        autofill=RuleAutofill(strategy="none"),
    )
    defaults.update(kw)
    return Rule(**defaults)  # type: ignore[arg-type]


def _el(eid, name, category, **params):
    return {"id": eid, "name": name, "category": category, "params": params}


# ── end-to-end: real QC pipeline (fire-door) → schedule per rule ─────────────


def test_creates_one_schedule_for_firedoor_rule():
    async def run():
        autonomy = AutonomyPolicy.load(CONFIG / "autonomy.yaml")
        qc = QCAgent(rules_path=CONFIG / "rules.ibc716_door_rating.yaml",
                     autonomy=autonomy)
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        q = RevitQueryAgent(mcp=mock, rules=qc.rules, catalog=catalog)
        state = {"project_id": "p", "iteration": 0, "max_iterations": 1,
                 "elements": [], "findings": [], "proposed_fixes": [],
                 "status": "init", "error": None}
        state = await q.run(state)
        state = qc.run(state)
        results = await create_verification_schedules(
            mock, state["check_trace"], catalog=catalog
        )
        return mock, results

    mock, results = asyncio.run(run())
    assert len(results) == 1
    r = results[0]
    assert r.status == "created"
    assert r.category_ost == "OST_Doors"
    assert r.schedule_id is not None
    assert "Fire Rating" in r.fields
    # both addin calls were made through the client (MCP boundary)
    assert mock.calls_to("revit_create_schedule")
    assert mock.calls_to("revit_configure_schedule")
    # the cross-element (lookup) recipe is degraded → schedule still created, noted
    assert r.detail and "Select-by-ID" in r.detail


# ── generality across 3 requirement types ────────────────────────────────────


def _three_type_trace():
    r_present = mk_rule("present_and_nonempty", id="rooms.dept", parameter="Department",
                        category="Rooms")
    r_numeric = mk_rule("numeric_compare", id="doors.width", parameter="Width",
                        category="Doors", operator=">=", threshold=0.9)
    r_unique = mk_rule("unique_in_set", id="rooms.number", parameter="Number",
                       category="Rooms")
    return [
        build_check_record(r_present, _el("1", "Lobby", "Rooms", Department="Public"),
                           raw_value="Public", value="Public", passed=True, status="compliant"),
        build_check_record(r_numeric, _el("100", "Door C", "Doors", Width=0.8),
                           raw_value=0.8, value=0.8, passed=False, status="non_compliant"),
        build_check_record(r_unique, _el("2", "Room 2", "Rooms", Number="101"),
                           raw_value="101", value="101", passed=True, status="compliant"),
    ]


def test_generality_one_schedule_per_rule_type():
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        return await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )

    results = asyncio.run(run())
    assert len(results) == 3
    by_rule = {r.rule_id: r for r in results}
    assert by_rule["rooms.dept"].category_ost == "OST_Rooms"
    assert by_rule["doors.width"].category_ost == "OST_Doors"
    assert all(r.status == "created" for r in results)
    # 3 distinct schedule ids
    assert len({r.schedule_id for r in results}) == 3


def test_unique_rule_configures_group_by():
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        await create_verification_schedules(mock, _three_type_trace(), catalog=catalog)
        return mock

    mock = asyncio.run(run())
    # find the configure call whose sort field is the unique rule's parameter
    cfgs = mock.calls_to("revit_configure_schedule")
    group_flags = {}
    for c in cfgs:
        for sf in c.get("sortFields", []):
            group_flags[sf["field"]] = sf.get("groupBy")
    assert group_flags.get("Number") is True   # uniqueness → group
    assert group_flags.get("Width") is False   # numeric → sort only


# ── graceful degradation ─────────────────────────────────────────────────────


def test_degrades_when_addin_lacks_schedule_tool():
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient(unsupported_commands={"revit_create_schedule"})
        return await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )

    results = asyncio.run(run())
    assert all(r.status == "degraded" for r in results)
    assert all(r.schedule_id is None for r in results)
    assert all("unknown_command" in (r.detail or "") for r in results)


def test_dry_run_does_not_commit():
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        results = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog, dry_run=True
        )
        return mock, results

    mock, results = asyncio.run(run())
    assert all(r.status == "created" for r in results)
    # every create/configure call carried dryRun=True
    for tool in ("revit_create_schedule", "revit_configure_schedule"):
        assert all(c.get("dryRun") is True for c in mock.calls_to(tool))


# ── manifest helpers ─────────────────────────────────────────────────────────


def test_manifest_dict_and_markdown():
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        return await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )

    results = asyncio.run(run())
    md = manifest_dict(results)
    assert md["summary"]["created"] == 3
    assert len(md["schedules"]) == 3
    text = render_manifest_markdown(results)
    assert "build manifest" in text.lower()
    assert "rooms.dept" in text


def test_ost_fallback_when_label_unknown():
    """A category the catalog doesn't know still gets an OST_ attempt, not a crash."""
    async def run():
        rule = mk_rule("present_and_nonempty", id="x.weird", parameter="Foo",
                       category="Totally Made Up Category")
        rec = build_check_record(
            rule, _el("9", "W", "Totally Made Up Category", Foo="bar"),
            raw_value="bar", value="bar", passed=True, status="compliant")
        mock = MockRevitMCPClient()
        return await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())

    results = asyncio.run(run())
    assert results[0].category_ost == "OST_TotallyMadeUpCategory"


# ── v1.5-R6 (3.1): idempotent --create-verification-views ───────────────────


def test_running_twice_on_same_mock_reports_existing_second_time():
    """The acceptance scenario from the spec: run create_verification_schedules
    TWICE against the SAME (stateful) mock client — the second run must never
    create a duplicate schedule.

    v1.7-R22 (D-4) narrowed the status: the second pass now reports
    "existing_reconfigured" because it RE-APPLIES the configuration rather than
    walking away. Plain "existing" is reserved for the cases where it could
    not (no schedule id, or no configure tool on the transport)."""
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        first = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        second = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        return mock, first, second

    mock, first, second = asyncio.run(run())
    assert all(r.status == "created" for r in first)
    assert len(second) == len(first)
    assert all(r.status == "existing_reconfigured" for r in second)
    # no new schedule was created on the second pass
    assert len(mock.calls_to("revit_create_schedule")) == len(first)
    # the existing result still names the ORIGINAL schedule id
    first_ids = {r.rule_id: r.schedule_id for r in first}
    for r in second:
        assert r.schedule_id == first_ids[r.rule_id]


def test_probe_unavailable_falls_back_to_create_time_duplicate_error():
    """3.1 fallback path: a client whose get_views() the addin doesn't support
    (unknown_command) still ends up "existing" on the second pass, via the
    create-time duplicate-name error instead of the primary probe."""
    class _NoProbeClient(MockRevitMCPClient):
        async def get_views(self):
            from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
            raise RevitEnvelopeError(
                tool="revit_get_views", code="unknown_command", message="nope"
            )

        async def create_schedule(self, category, *, name=None, fields=None, dry_run=False):
            # Simulate the addin rejecting a duplicate name outright (the
            # get_views() probe can't see it, so this is the only signal).
            if name in getattr(self, "_created_names", set()):
                from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
                raise RevitEnvelopeError(
                    tool="revit_create_schedule", code="duplicate_name",
                    message="already exists",
                )
            self._created_names = getattr(self, "_created_names", set()) | {name}
            return await super().create_schedule(
                category, name=name, fields=fields, dry_run=dry_run
            )

    async def run():
        catalog = OSTCatalog.load()
        mock = _NoProbeClient()
        first = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        second = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        return first, second

    first, second = asyncio.run(run())
    assert all(r.status == "created" for r in first)
    # This path learns "it already exists" from a create-time duplicate error,
    # which carries NO schedule id — so there is nothing to re-configure and
    # the honest status stays the weaker plain "existing" (v1.7-R22).
    assert all(r.status == "existing" for r in second)
    assert all(r.schedule_id is None for r in second)


def test_manifest_counts_existing_separately_from_created():
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        await create_verification_schedules(mock, _three_type_trace(), catalog=catalog)
        second = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        return second

    second = asyncio.run(run())
    md = manifest_dict(second)
    assert md["summary"]["existing_reconfigured"] == 3
    assert md["summary"]["existing"] == 0
    assert md["summary"]["created"] == 0
    text = render_manifest_markdown(second)
    assert "Existing" in text


# ── v1.5-R6 (3.2): real schedule filters ─────────────────────────────────────


def test_present_and_nonempty_filter_passed_to_configure_schedule():
    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept", parameter="Department",
                       category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        mock = MockRevitMCPClient()
        await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())
        return mock

    mock = asyncio.run(run())
    cfgs = mock.calls_to("revit_configure_schedule")
    assert len(cfgs) == 1
    filters = cfgs[0].get("filters")
    assert filters == [{"field": "Department", "operator": "has_no_value"}]


def test_numeric_compare_filter_uses_storage_unit_conversion():
    """Width threshold declared in mm must arrive at configure_schedule
    converted to feet (Width's Revit storage unit) — never the raw mm value."""
    async def run():
        rule = mk_rule("numeric_compare", id="doors.width", parameter="Width",
                       category="Doors", operator=">=", threshold=900.0, unit="mm")
        rec = build_check_record(
            rule, _el("100", "Door C", "Doors", Width=1.0),
            raw_value=1.0, value=1.0, passed=False, status="non_compliant")
        mock = MockRevitMCPClient()
        await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())
        return mock

    mock = asyncio.run(run())
    filters = mock.calls_to("revit_configure_schedule")[0].get("filters")
    assert filters
    f = filters[0]
    assert f["field"] == "Width"
    assert f["operator"] == "less"  # negation of ">="
    # Pin BOTH ends of the wire. The STRING form is a DECISION, not a bridge
    # constraint any more: since addin v0.8.23 the bridge takes string OR
    # number with byte-identical results, but on the older addins still
    # deployed (R2025 at 0.8.20) a number kills the whole configure command
    # while a string degrades to a surfaced warning — so string stays the
    # wire form (see verify_recipes._filter_value_text). If this assert
    # bothers you, read that docstring before "fixing" it to a float.
    # And the magnitude must be the storage unit: 900 mm -> ~2.953 ft.
    assert isinstance(f["value"], str)
    assert abs(float(f["value"]) - 2.9527559) < 1e-4


def test_mock_wire_contract_tracks_the_v0823_bridge():
    """The mock must reject exactly what the real bridge rejects — no more,
    no less. Its first version (R11) pinned the v0.8.21 string-only schema;
    after the addin's v0.8.23 fix (z.union([string, number]) + typed
    ScheduleFilter) that same strictness would FAIL the wire form the live
    addin now accepts — a mock stricter than reality misleads in the same
    way one looser than reality does, just in the opposite direction.

    bool is the trap worth pinning: Python `bool` is a subclass of `int`, so
    a naive (str, int, float) gate waves `True` through — and ScheduleFilter
    has no boolean form; the real bridge rejects it."""
    from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError

    async def configure(value):
        mock = MockRevitMCPClient()
        created = await mock.create_schedule(
            category="OST_Doors", name="probe", fields=["Mark", "Width"])
        return await mock.configure_schedule(
            created["scheduleId"],
            filters=[{"field": "Width", "operator": "less", "value": value}],
        )

    # Accepted since v0.8.23: string AND number forms.
    for ok_value in ("2.9527559055118114", 2.9527559055118114, 13):
        result = asyncio.run(configure(ok_value))
        assert result.get("ok", True), f"mock rejected accepted form {ok_value!r}"

    # Still rejected by the real bridge: bool and structured values.
    for bad_value in (True, [1], {"v": 1}):
        try:
            asyncio.run(configure(bad_value))
        except RevitEnvelopeError as exc:
            assert exc.code == "bad_envelope"
        else:
            raise AssertionError(
                f"mock accepted {bad_value!r}, which the real bridge rejects"
            )


def test_numeric_rule_schedule_is_configured_not_bad_envelope():
    """LIVE REPRO 2026-08-01 (Snowdon, run-d34fd30a): `demo.doors.width_min`
    was the ONLY rule of five that failed --create-verification-views, with
    `bad_envelope` at revit_configure_schedule — because it is the only
    requirement that sends a filter `value`, and it sent a float where the
    bridge's tool schema demands a string. The mock now enforces that
    contract, so this test fails the same way the live model did if the wire
    type ever regresses."""
    async def run():
        rule = mk_rule("numeric_compare", id="demo.doors.width_min", parameter="Width",
                       category="Doors", operator=">=", threshold=900.0, unit="mm")
        rec = build_check_record(
            rule, _el("100", "Door C", "Doors", Width=0.8),
            raw_value=0.8, value=0.8, passed=False, status="non_compliant")
        mock = MockRevitMCPClient()
        return await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0].status == "created", results[0].detail
    assert results[0].schedule_id is not None


def test_configure_warnings_are_surfaced_not_swallowed():
    """The addin skips a filter/field it can't apply and says so in `warnings`
    while the envelope stays ok — "created" must not silently mean "created
    AND fully configured"."""
    async def run():
        rule = mk_rule("numeric_compare", id="doors.width", parameter="Width",
                       category="Doors", operator=">=", threshold=900.0, unit="mm")
        rec = build_check_record(
            rule, _el("100", "Door C", "Doors", Width=0.8),
            raw_value=0.8, value=0.8, passed=False, status="non_compliant")
        mock = MockRevitMCPClient(
            configure_warnings=["Could not add filter on 'Width': value type mismatch."]
        )
        return await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())

    results = asyncio.run(run())
    r = results[0]
    assert r.status == "created"
    assert r.warnings == ["Could not add filter on 'Width': value type mismatch."]
    assert r.detail and "Could not add filter on 'Width'" in r.detail
    assert "NOT applied" in r.detail
    md = render_manifest_markdown(results)
    assert "addin warnings" in md
    assert "warnings" in manifest_dict(results)["schedules"][0]


def test_configure_failure_keeps_the_created_schedule_id():
    """LIVE 2026-08-01: create succeeded, configure failed — the manifest said
    `schedule_id: null`, so the half-built schedule left in the model was
    untraceable. Report the id that WAS created, and say it needs cleanup."""
    class _ConfigureFails(MockRevitMCPClient):
        async def configure_schedule(self, schedule_id, **kw):
            from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
            raise RevitEnvelopeError(
                tool="revit_configure_schedule", code="bad_envelope",
                message="Bridge returned a result with no parseable JSON envelope.",
            )

    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept", parameter="Department",
                       category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        return await create_verification_schedules(
            _ConfigureFails(), [rec], catalog=OSTCatalog.load()
        )

    results = asyncio.run(run())
    r = results[0]
    assert r.status == "error"
    assert r.schedule_id is not None
    assert r.detail and "WAS created" in r.detail


# ── LIVE PROBE 2026-07-12 (addin v0.8.13, F1/F3): naming convention +
# honesty-check against the addin's silent rename/fallback ──────────────────


def test_schedule_name_uses_unbracketed_convention():
    """The naming convention is "AutoAudit - <rule_id>" — no brackets. The old
    "[AutoAudit] <rule_id>" form is never actually applied by a real addin
    (F1: Revit forbids `[` `]` in a view name and silently falls back to its
    own default instead)."""
    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept", parameter="Department",
                       category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        mock = MockRevitMCPClient()
        return await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())

    results = asyncio.run(run())
    assert results[0].status == "created"
    assert results[0].schedule_name == "AutoAudit - rooms.dept"
    assert results[0].requested_name == "AutoAudit - rooms.dept"
    assert "[" not in results[0].schedule_name
    assert "]" not in results[0].schedule_name


def test_forbidden_char_in_rule_id_triggers_honesty_check():
    """F1: a rule id that (via the naming convention) puts a forbidden Revit
    character into the schedule name gets silently renamed by the (mocked)
    addin. verification_views must NOT trust the requested name — it reports
    status="created_renamed" and surfaces both names, never crashes."""
    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept[legacy]",
                       parameter="Department", category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        mock = MockRevitMCPClient()
        return await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())

    results = asyncio.run(run())
    assert len(results) == 1
    r = results[0]
    assert r.status == "created_renamed"
    assert r.requested_name == "AutoAudit - rooms.dept[legacy]"
    assert r.schedule_name == "Door Schedule 1"
    assert r.schedule_name != r.requested_name
    assert r.detail and "different name than requested" in r.detail


def test_created_renamed_counted_in_manifest():
    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept[legacy]",
                       parameter="Department", category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        mock = MockRevitMCPClient()
        return await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())

    results = asyncio.run(run())
    md = manifest_dict(results)
    assert md["summary"]["created_renamed"] == 1
    assert md["summary"]["created"] == 0
    text = render_manifest_markdown(results)
    assert "renamed by addin" in text
    assert "Renamed rules" in text


def test_duplicate_valid_name_without_probe_surfaces_as_created_renamed():
    """F3: a duplicate (but otherwise valid) name is ALSO silently renamed by
    the addin rather than erroring. When the primary get_views() probe is
    unavailable, verification_views can't know ahead of time — it issues the
    create call, the addin quietly hands back a different name, and the
    honesty-check catches it as "created_renamed" (never a false "existing",
    never a crash)."""
    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept", parameter="Department",
                       category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        mock = MockRevitMCPClient(unsupported_commands={"revit_get_views"})
        first = await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())
        second = await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())
        return first, second

    first, second = asyncio.run(run())
    assert first[0].status == "created"
    assert first[0].schedule_name == "AutoAudit - rooms.dept"
    # second pass: probe unavailable, so it re-attempts create_schedule with
    # the SAME name — the mock (mirroring the live addin) silently renames
    # rather than erroring, and verification_views reports that honestly.
    assert second[0].status == "created_renamed"
    assert second[0].requested_name == "AutoAudit - rooms.dept"
    assert second[0].schedule_name != "AutoAudit - rooms.dept"


def test_idempotent_second_run_reports_existing_with_new_naming_convention():
    """Acceptance scenario, re-verified against the new "AutoAudit - <rule_id>"
    convention: with a valid name and a working get_views() probe, a second
    --create-verification-views pass on the same rule finds it via the
    PRIMARY probe and reports "existing" — no duplicate schedule, no rename."""
    async def run():
        rule = mk_rule("present_and_nonempty", id="rooms.dept", parameter="Department",
                       category="Rooms")
        rec = build_check_record(
            rule, _el("1", "Lobby", "Rooms", Department="Public"),
            raw_value="Public", value="Public", passed=True, status="compliant")
        mock = MockRevitMCPClient()
        first = await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())
        second = await create_verification_schedules(mock, [rec], catalog=OSTCatalog.load())
        return mock, first, second

    mock, first, second = asyncio.run(run())
    assert first[0].status == "created"
    assert first[0].schedule_name == "AutoAudit - rooms.dept"
    # v1.7-R22: found via the PRIMARY probe, so we have its id and re-apply the
    # configuration rather than skipping it (D-4).
    assert second[0].status == "existing_reconfigured"
    assert second[0].schedule_id == first[0].schedule_id
    # only ONE create_schedule call ever reached the addin
    assert len(mock.calls_to("revit_create_schedule")) == 1


# ── v1.7-R22 (D-4): a half-built schedule gets repaired, not skipped ───────


def test_half_built_schedule_is_reconfigured_on_the_next_run():
    """THE D-4 FAILURE, pinned end to end.

    Take 1 creates the schedule and then dies while configuring it. Before
    v1.7-R22 the schedule sat in the model unsorted and unfiltered forever:
    every later run saw the name, said "existing", and the manifest read like
    success. Now take 2 re-applies the configuration.
    """
    from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError

    class _ConfigureFailsOnce(MockRevitMCPClient):
        configure_should_fail = True
        configure_calls: list[int] = []

        async def configure_schedule(self, schedule_id, **kw):
            if self.configure_should_fail:
                raise RevitEnvelopeError(
                    tool="revit_configure_schedule", code="internal_error",
                    message="Revit blew up mid-transaction",
                )
            self.configure_calls.append(schedule_id)
            return await super().configure_schedule(schedule_id, **kw)

    async def run():
        catalog = OSTCatalog.load()
        mock = _ConfigureFailsOnce()
        mock.configure_calls = []
        first = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        # the interruption is over; the schedules are in the model, half built
        mock.configure_should_fail = False
        second = await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )
        return mock, first, second

    mock, first, second = asyncio.run(run())

    # take 1: created, but configuring failed — reported honestly, with the id
    assert all(r.status == "error" for r in first)
    assert all(r.schedule_id is not None for r in first)

    # take 2: NOT recreated — and NOT skipped either
    assert len(mock.calls_to("revit_create_schedule")) == len(first)
    assert all(r.status == "existing_reconfigured" for r in second)
    # the repair actually reached the addin, on the SAME schedules
    assert sorted(mock.configure_calls) == sorted(r.schedule_id for r in first)
    # LIVE 2026-08-19: and it must REPLACE, not append. The addin ADDS sort
    # fields and filters; without clearing first, our sort landed BEHIND a
    # stale one and the schedule on screen never moved while the manifest
    # said "re-applied". The mock cannot model add-vs-replace, so pin the
    # wire contract that makes it a replace — this assertion is the only
    # thing standing between us and that silent no-op coming back.
    wire = mock.calls_to("revit_configure_schedule")[-len(second):]
    assert all(a.get("clearSortFields") is True for a in wire)
    assert all(a.get("clearFilters") is True for a in wire)


def test_reconfigure_unavailable_transport_stays_existing_not_error():
    """A transport with no configure_schedule at all: the schedule genuinely
    exists and is usable, so this degrades to plain "existing" with a note —
    it must not be inflated into an error."""
    from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError

    class _NoConfigureTool(MockRevitMCPClient):
        fail_configure = False

        async def configure_schedule(self, schedule_id, **kw):
            if self.fail_configure:
                raise RevitEnvelopeError(
                    tool="revit_configure_schedule", code="unknown_command",
                    message="not supported",
                )
            return await super().configure_schedule(schedule_id, **kw)

    async def run():
        catalog = OSTCatalog.load()
        mock = _NoConfigureTool()
        await create_verification_schedules(mock, _three_type_trace(), catalog=catalog)
        mock.fail_configure = True
        return await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )

    second = asyncio.run(run())
    assert all(r.status == "existing" for r in second)
    assert all("unavailable on this transport" in (r.detail or "") for r in second)
    # it still points at the real schedule so a human can go look at it
    assert all(r.schedule_id is not None for r in second)


def test_reconfigure_hard_failure_is_reported_as_error_with_the_id():
    """If re-applying the configuration fails for a REAL reason (not a missing
    tool), saying "existing" would be the assertion-without-evidence class —
    the schedule may be unsorted, unfiltered, or gone. Report an error, and
    keep the id so the human can find it."""
    from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError

    class _ConfigureBreaksLater(MockRevitMCPClient):
        fail_configure = False

        async def configure_schedule(self, schedule_id, **kw):
            if self.fail_configure:
                raise RevitEnvelopeError(
                    tool="revit_configure_schedule", code="not_found",
                    message="no such view",
                )
            return await super().configure_schedule(schedule_id, **kw)

    async def run():
        catalog = OSTCatalog.load()
        mock = _ConfigureBreaksLater()
        await create_verification_schedules(mock, _three_type_trace(), catalog=catalog)
        mock.fail_configure = True
        return await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )

    second = asyncio.run(run())
    assert all(r.status == "error" for r in second)
    assert all(r.schedule_id is not None for r in second)
    assert all("not_found" in (r.detail or "") for r in second)


def test_manifest_and_cli_do_not_silently_drop_the_new_status():
    """A new status that no renderer counts is a regression that hides itself —
    pin that the manifest names it and the summary line shows it."""
    async def run():
        catalog = OSTCatalog.load()
        mock = MockRevitMCPClient()
        await create_verification_schedules(mock, _three_type_trace(), catalog=catalog)
        return await create_verification_schedules(
            mock, _three_type_trace(), catalog=catalog
        )

    second = asyncio.run(run())
    md = manifest_dict(second)
    assert md["summary"]["existing_reconfigured"] == len(second)
    # every status a result can carry must have a slot in the manifest summary
    assert set(r.status for r in second) <= set(md["summary"])
    text = render_manifest_markdown(second)
    assert "re-configured" in text
    assert "existing_reconfigured" in text
