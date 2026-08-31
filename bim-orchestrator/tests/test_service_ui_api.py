"""Phase 3b M1 — AutoAudit UI backend endpoints (routes_runs/approvals/revit,
spa.py, RevitHTTPClient.select_elements/zoom_to_elements).

Everything mocked/offline, same posture as test_service_api.py: no Revit, no
Forma, no real audit. The ``client`` fixture MUST be used as a context
manager (see test_service_api.py's comment — a bare TestClient spins a fresh
event loop per request and the background audit task never progresses).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="service extras not installed")
from fastapi.testclient import TestClient  # noqa: E402

from bim_orchestrator.service import app as app_module  # noqa: E402
from bim_orchestrator.service import routes_extraction  # noqa: E402
from bim_orchestrator.service import routes_revit  # noqa: E402
from bim_orchestrator.service import routes_settings  # noqa: E402
from bim_orchestrator.service.app import create_app  # noqa: E402

# SPEC_AU_DEMO_PACKAGE.md item 2 — committed fixture records, schema-real
# (mirrors agents.design._build_record_fixes verbatim).
_DEMO_APPROVALS_DIR = Path(__file__).resolve().parents[2] / "references" / "demo_approvals"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def fake_probe_revit() -> bool:
        return False

    monkeypatch.setattr(app_module, "probe_revit", fake_probe_revit)
    monkeypatch.setattr(app_module, "probe_forma", lambda: True)
    monkeypatch.setattr(app_module, "probe_axes", lambda: (True, False))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(
        runs_root=tmp_path / "runs", approvals_dir=tmp_path / "approvals",
        config_dir=config_dir,
    )
    with TestClient(application) as c:
        yield c
        runner = application.state.runner
        if runner.current is not None and runner.current.status == "running":
            for _ in range(200):
                if runner.current.status != "running":
                    break
                time.sleep(0.01)


def _write_run(
    runs_root: Path,
    run_id: str,
    *,
    started_at: str,
    summary: dict | None = None,
    with_artifacts: bool = False,
) -> Path:
    d = runs_root / run_id
    d.mkdir(parents=True)
    summary = summary or {"compliant": 0, "non_compliant": 0, "manual_review": 0, "missing_data": 0}
    (d / "metadata.json").write_text(
        json.dumps({
            "run_id": run_id, "mode": "run", "status": "completed",
            "started_at": started_at, "outcomes_summary": summary,
        }),
        encoding="utf-8",
    )
    (d / "outcomes.json").write_text(
        json.dumps({
            "outcomes_summary": summary,
            "non_compliant": [
                {"rule_id": "r1", "element_id": "1", "parameter": "P"}
            ] * summary.get("non_compliant", 0),
            "manual_review_items": [],
            "missing_data_items": [],
            "proposed_fixes": [],
        }),
        encoding="utf-8",
    )
    if with_artifacts:
        (d / "report.md").write_text("# report", encoding="utf-8")
        (d / "verification_report.md").write_text("# verification", encoding="utf-8")
        (d / "trace.md").write_text("# trace", encoding="utf-8")
        (d / "axes").mkdir()
        (d / "axes" / "lod.json").write_text("{}", encoding="utf-8")
    return d


def _write_approval(
    approvals_dir: Path,
    filename: str,
    *,
    applied: bool = False,
    issue_status: str = "open",
) -> Path:
    approvals_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "issue_id": filename.removesuffix(".json"),
        "display_id": 1001,
        "project_id": "demo-villa-simulated",
        "created_at": "2026-07-07T11:53:46.463915+00:00",
        "applied": applied,
        "status": "applied" if applied else "pending_approval",
        "issue_status": issue_status,
        "fingerprint": "127cc181881a0385",
        "fixes": [
            {
                "finding_id": "demo.doors.fire_rating::703",
                "element_id": "9102",
                "flagged_instance": "703",
                "parameter": "Fire Rating",
                "old_value": "120 min",
                "new_value": "2 HR",
                "value_source": None,
                "evidence": None,
                "inherited_from": None,
                "action": "set_parameter",
            },
            {
                "finding_id": "demo.doors.fire_rating::701",
                "element_id": "9101",
                "flagged_instance": "701",
                "parameter": "Fire Rating",
                "old_value": "",
                "new_value": "2 HR",
                "value_source": None,
                "evidence": None,
                "inherited_from": "2 HR",
                "action": "set_parameter",
            },
        ],
    }
    p = approvals_dir / filename
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


class TestRunDetail:
    def test_shape_and_artifacts(self, client: TestClient, tmp_path: Path) -> None:
        _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00",
            summary={"compliant": 8, "non_compliant": 2, "manual_review": 0, "missing_data": 0},
            with_artifacts=True,
        )
        r = client.get("/api/runs/run-0000abcd")
        assert r.status_code == 200
        body = r.json()
        assert body["metadata"]["run_id"] == "run-0000abcd"
        assert body["artifacts"] == {
            "report": True, "verification_report": True, "trace": True,
            "axes": True, "report_docx": False, "report_pdf": False,
            "delta": False,
        }

    def test_artifacts_delta_true_when_present(self, client: TestClient, tmp_path: Path) -> None:
        run_dir = _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00",
        )
        (run_dir / "delta.md").write_text("# delta", encoding="utf-8")
        r = client.get("/api/runs/run-0000abcd")
        assert r.status_code == 200
        assert r.json()["artifacts"]["delta"] is True

    def test_unknown_run_404(self, client: TestClient) -> None:
        assert client.get("/api/runs/run-99999999").status_code == 404

    def test_bad_run_id_shape_400(self, client: TestClient) -> None:
        assert client.get("/api/runs/not-a-run-id").status_code == 400

    def test_path_traversal_in_run_id_rejected(self, client: TestClient) -> None:
        r = client.get("/api/runs/..%2F..%2F.env")
        assert r.status_code in (400, 404)


class TestOutcomes:
    def test_verbatim(self, client: TestClient, tmp_path: Path) -> None:
        _write_run(tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00")
        r = client.get("/api/runs/run-0000abcd/outcomes")
        assert r.status_code == 200
        assert "outcomes_summary" in r.json()

    def test_missing_outcomes_404(self, client: TestClient, tmp_path: Path) -> None:
        d = tmp_path / "runs" / "run-0000abcd"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("{}", encoding="utf-8")
        r = client.get("/api/runs/run-0000abcd/outcomes")
        assert r.status_code == 404


class TestTrend:
    def test_two_runs_diff(self, client: TestClient, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        _write_run(
            runs_root, "run-11111111", started_at="2026-07-10T00:00:00",
            summary={"compliant": 8, "non_compliant": 2, "manual_review": 0, "missing_data": 0},
        )
        (runs_root / "run-11111111" / "outcomes.json").write_text(
            json.dumps({
                "outcomes_summary": {"compliant": 8, "non_compliant": 2},
                "non_compliant": [{"rule_id": "r1", "element_id": "1", "parameter": "P"}],
                "manual_review_items": [], "missing_data_items": [], "proposed_fixes": [],
            }),
            encoding="utf-8",
        )
        _write_run(
            runs_root, "run-22222222", started_at="2026-07-11T00:00:00",
            summary={"compliant": 9, "non_compliant": 1, "manual_review": 0, "missing_data": 0},
        )
        (runs_root / "run-22222222" / "outcomes.json").write_text(
            json.dumps({
                "outcomes_summary": {"compliant": 9, "non_compliant": 1},
                "non_compliant": [{"rule_id": "r2", "element_id": "2", "parameter": "P"}],
                "manual_review_items": [], "missing_data_items": [], "proposed_fixes": [],
            }),
            encoding="utf-8",
        )
        r = client.get("/api/trend")
        assert r.status_code == 200
        body = r.json()
        assert len(body["points"]) == 2
        # newest first (list_runs' own order)
        assert body["points"][0]["run_id"] == "run-22222222"
        assert body["points"][0]["compliance_pct"] == 90.0
        assert body["diff_latest"] == {"resolved": 1, "new": 1, "persistent": 0}

    def test_single_run_no_diff(self, client: TestClient, tmp_path: Path) -> None:
        _write_run(tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00")
        body = client.get("/api/trend").json()
        assert len(body["points"]) == 1
        assert body["diff_latest"] is None

    def test_sentinel_rows_excluded(self, client: TestClient, tmp_path: Path) -> None:
        (tmp_path / "runs").mkdir(parents=True)
        (tmp_path / "runs" / "run-deadbeef").mkdir()  # no metadata.json -> sentinel
        body = client.get("/api/trend").json()
        assert body["points"] == []


class TestApprovals:
    def test_pending_shape(self, client: TestClient, tmp_path: Path) -> None:
        _write_approval(tmp_path / "approvals", "issue-mock-0001.json")
        r = client.get("/api/approvals")
        assert r.status_code == 200
        body = r.json()
        assert body["counts"] == {"pending": 1, "applied": 0, "ignored": 0}
        proposal = body["proposals"][0]
        assert proposal["file"] == "issue-mock-0001.json"
        assert proposal["status"] == "pending"
        assert proposal["rule_ids"] == ["demo.doors.fire_rating"]
        assert proposal["fixes"][1]["inherited_from"] == "2 HR"

    def test_applied_closed(self, client: TestClient, tmp_path: Path) -> None:
        _write_approval(
            tmp_path / "approvals", "issue-mock-0002.json",
            applied=True, issue_status="closed",
        )
        body = client.get("/api/approvals").json()
        assert body["proposals"][0]["status"] == "applied"
        assert body["counts"]["applied"] == 1

    def test_applied_issue_open(self, client: TestClient, tmp_path: Path) -> None:
        _write_approval(
            tmp_path / "approvals", "issue-mock-0003.json",
            applied=True, issue_status="applied_pending_close",
        )
        body = client.get("/api/approvals").json()
        assert body["proposals"][0]["status"] == "applied_issue_open"
        assert body["counts"]["applied"] == 1

    def test_ignored_listed_separately(self, client: TestClient, tmp_path: Path) -> None:
        p = _write_approval(tmp_path / "approvals", "issue-mock-0004.json")
        dest_dir = tmp_path / "approvals" / "_ignored"
        dest_dir.mkdir(parents=True)
        p.rename(dest_dir / p.name)
        body = client.get("/api/approvals").json()
        assert body["proposals"][0]["status"] == "ignored"
        assert body["counts"] == {"pending": 0, "applied": 0, "ignored": 1}

    def test_ignore_then_restore_moves_file(self, client: TestClient, tmp_path: Path) -> None:
        _write_approval(tmp_path / "approvals", "issue-mock-0005.json")
        r = client.post("/api/approvals/issue-mock-0005.json/ignore")
        assert r.status_code == 200
        assert not (tmp_path / "approvals" / "issue-mock-0005.json").exists()
        assert (tmp_path / "approvals" / "_ignored" / "issue-mock-0005.json").exists()

        r2 = client.post("/api/approvals/issue-mock-0005.json/restore")
        assert r2.status_code == 200
        assert (tmp_path / "approvals" / "issue-mock-0005.json").exists()
        assert not (tmp_path / "approvals" / "_ignored" / "issue-mock-0005.json").exists()

    def test_ignore_unknown_file_404(self, client: TestClient) -> None:
        assert client.post("/api/approvals/issue-nope.json/ignore").status_code == 404

    def test_ignore_applied_record_409(self, client: TestClient, tmp_path: Path) -> None:
        _write_approval(tmp_path / "approvals", "issue-mock-0006.json", applied=True)
        r = client.post("/api/approvals/issue-mock-0006.json/ignore")
        assert r.status_code == 409

    def test_traversal_filename_rejected(self, client: TestClient) -> None:
        r = client.post("/api/approvals/..%2F..%2Fsecret/ignore")
        assert r.status_code in (400, 404)
        r2 = client.post("/api/approvals/not-json/ignore")
        assert r2.status_code == 400

    def test_au_demo_fixtures_map_correctly(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """The committed AU-demo fixture records (SPEC_AU_DEMO_PACKAGE.md item
        2) must be genuine ApprovalWatcher records — round-trip them through
        the real endpoint rather than a synthetic dict."""
        assert _DEMO_APPROVALS_DIR.is_dir(), _DEMO_APPROVALS_DIR
        dest = tmp_path / "approvals"
        dest.mkdir(parents=True, exist_ok=True)
        for src in _DEMO_APPROVALS_DIR.glob("*.json"):
            (dest / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        body = client.get("/api/approvals").json()
        by_file = {p["file"]: p for p in body["proposals"]}
        assert by_file["au-demo-pending.json"]["status"] == "pending"
        assert by_file["au-demo-pending.json"]["fixes"][1]["inherited_from"] == "2 HR"
        assert by_file["au-demo-applied.json"]["status"] == "applied"
        assert body["counts"] == {"pending": 1, "applied": 1, "ignored": 0}


class TestExportReport:
    def test_missing_report_404(self, client: TestClient, tmp_path: Path) -> None:
        _write_run(tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00")
        r = client.post(
            "/api/runs/run-0000abcd/export-report", json={"format": "docx"}
        )
        assert r.status_code == 404

    def test_export_failure_is_422_with_raw_detail(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00",
            with_artifacts=True,
        )
        from bim_orchestrator import report_export

        def fake_export_report(md_path, fmt, **kw):
            return None, "pandoc not found on PATH. boom"

        monkeypatch.setattr(report_export, "export_report", fake_export_report)
        r = client.post(
            "/api/runs/run-0000abcd/export-report", json={"format": "pdf"}
        )
        assert r.status_code == 422
        assert "pandoc not found" in r.json()["detail"]

    def test_export_success(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00",
            with_artifacts=True,
        )
        from bim_orchestrator import report_export

        out_path = tmp_path / "runs" / "run-0000abcd" / "verification_report.docx"

        def fake_export_report(md_path, fmt, **kw):
            out_path.write_text("fake docx", encoding="utf-8")
            return out_path, f"wrote {out_path}"

        monkeypatch.setattr(report_export, "export_report", fake_export_report)
        r = client.post(
            "/api/runs/run-0000abcd/export-report", json={"format": "docx"}
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "artifact": "verification_report.docx"}


class TestVerificationViews:
    def test_missing_trace_404(self, client: TestClient, tmp_path: Path) -> None:
        _write_run(tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00")
        r = client.post(
            "/api/runs/run-0000abcd/verification-views", json={"dry_run": True}
        )
        assert r.status_code == 404

    def test_revit_down_503(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00"
        )
        (run_dir / "report_trace.json").write_text("[]", encoding="utf-8")
        # client fixture already stubs probe_revit -> False
        r = client.post(
            "/api/runs/run-0000abcd/verification-views", json={"dry_run": True}
        )
        assert r.status_code == 503

    def test_lock_held_409(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00"
        )
        (run_dir / "report_trace.json").write_text("[]", encoding="utf-8")

        async def fake_probe_revit_true() -> bool:
            return True

        monkeypatch.setattr(app_module, "probe_revit", fake_probe_revit_true)
        # Simulate an audit already holding the D7 lock.
        assert client.app.state.runner.lock.acquire() is True
        try:
            r = client.post(
                "/api/runs/run-0000abcd/verification-views", json={"dry_run": True}
            )
            assert r.status_code == 409
        finally:
            client.app.state.runner.lock.release()

    def test_success_creates_manifest(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = _write_run(
            tmp_path / "runs", "run-0000abcd", started_at="2026-07-11T00:00:00"
        )
        (run_dir / "report_trace.json").write_text("[]", encoding="utf-8")

        async def fake_probe_revit_true() -> bool:
            return True

        monkeypatch.setattr(app_module, "probe_revit", fake_probe_revit_true)

        from bim_orchestrator import verification_views as vv

        async def fake_create(client_, check_trace, *, catalog=None, dry_run=False):
            return [
                vv.ScheduleResult(
                    "demo.doors.fire_rating", "created", "OST_Doors", 900001,
                    "AutoAudit - demo.doors.fire_rating", ["Fire Rating"],
                )
            ]

        monkeypatch.setattr(vv, "create_verification_schedules", fake_create)

        r = client.post(
            "/api/runs/run-0000abcd/verification-views", json={"dry_run": True}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert len(body["created"]) == 1
        assert body["created"][0]["rule_id"] == "demo.doors.fire_rating"
        assert (run_dir / "verification_views.json").exists()
        assert (run_dir / "verification_views.md").exists()


class TestProfiles:
    def test_lists_committed_demo_profile(self, client: TestClient) -> None:
        r = client.get("/api/profiles")
        assert r.status_code == 200
        names = [p["name"] for p in r.json()["profiles"]]
        assert "demo" in names

    def test_invalid_profile_keeps_error_entry(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bim_orchestrator.service.routes_runs as routes_runs_module

        bad_dir = tmp_path / "config"
        bad_dir.mkdir(exist_ok=True)  # the `client` fixture already creates tmp_path/config
        (bad_dir / "audit.broken.yaml").write_text(
            "name: broken\nrules: ['Z:/no/such/rules.yaml']\n", encoding="utf-8"
        )
        monkeypatch.setattr(routes_runs_module, "_CONFIG_DIR", bad_dir)
        body = client.get("/api/profiles").json()
        assert len(body["profiles"]) == 1
        assert body["profiles"][0]["error"] is not None


class TestHighlight:
    """The endpoint is thin plumbing over ``highlight.highlight_elements``
    (the service holds zero business logic) — the walk's own behaviour is
    pinned in ``tests/test_highlight.py``. These tests pin the WIRE: request
    fields reach the orchestration, and its manifest reaches the response."""

    class _FakeRevit:
        """Two doors on two levels — the case the per-level walk exists for."""

        LEVELS = {447121: 593177, 447122: 593147}
        VIEWS = [
            {"id": 1350631, "name": "L2", "viewType": "FloorPlan",
             "levelId": 593177, "levelName": "L2", "isTemplate": False},
            {"id": 1350675, "name": "L3", "viewType": "FloorPlan",
             "levelId": 593147, "levelName": "L3", "isTemplate": False},
        ]

        def __init__(self, calls: list) -> None:
            self.calls = calls

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get_element_info(self, element_id):
            return {"id": element_id, "levelId": self.LEVELS[element_id]}

        async def get_views(self):
            return self.VIEWS

        async def open_view(self, view_id, **kw):
            self.calls.append(("open_view", view_id))
            return {"viewId": view_id}

        async def override_element_graphics(self, *, view_id, element_ids,
                                            color=None, transparency=None, reset=False):
            self.calls.append(("override", view_id, list(element_ids), color, reset))
            return {"ok": True}

        async def zoom_to_elements(self, ids):
            self.calls.append(("zoom", list(ids)))
            return {"zoomed": list(ids)}

        async def select_elements(self, ids):
            self.calls.append(("select", list(ids)))
            return {"selected": list(ids)}

    def test_walks_one_view_per_level_and_reports_them(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(
            routes_revit, "make_revit_client", lambda: self._FakeRevit(calls)
        )
        r = client.post("/api/revit/highlight", json={"element_ids": [447121, 447122]})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["selected"] == 2
        assert [(v["level_id"], v["view_name"], v["status"]) for v in body["views"]] == [
            (593177, "L2", "shown"),
            (593147, "L3", "shown"),
        ]
        assert calls == [
            ("open_view", 1350631), ("zoom", [447121]),
            ("open_view", 1350675), ("zoom", [447122]),
            ("select", [447121, 447122]),
        ]

    def test_colour_is_off_unless_requested(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Colour WRITES a graphic override into the model — a plain highlight
        # must stay navigation-only.
        calls: list[tuple] = []
        monkeypatch.setattr(
            routes_revit, "make_revit_client", lambda: self._FakeRevit(calls)
        )
        client.post("/api/revit/highlight", json={"element_ids": [447121]})
        assert not [c for c in calls if c[0] == "override"]

    def test_colour_request_reaches_the_addin(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(
            routes_revit, "make_revit_client", lambda: self._FakeRevit(calls)
        )
        r = client.post(
            "/api/revit/highlight",
            json={"element_ids": [447121], "color": {"r": 255, "g": 128, "b": 0}},
        )
        assert r.status_code == 200
        assert ("override", 1350631, [447121], {"r": 255, "g": 128, "b": 0}, False) in calls
        assert r.json()["views"][0]["colored"] is True

    def test_reset_clears_without_navigating(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(
            routes_revit, "make_revit_client", lambda: self._FakeRevit(calls)
        )
        r = client.post(
            "/api/revit/highlight", json={"element_ids": [447121], "reset": True}
        )
        assert r.status_code == 200
        assert calls == [("override", 1350631, [447121], None, True)]
        assert r.json()["selected"] == 0

    def test_per_level_false_restores_the_legacy_one_shot(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(
            routes_revit, "make_revit_client", lambda: self._FakeRevit(calls)
        )
        r = client.post(
            "/api/revit/highlight",
            json={"element_ids": [447121, 447122], "per_level": False},
        )
        assert r.status_code == 200
        assert calls == [
            ("select", [447121, 447122]),
            ("zoom", [447121, 447122]),
        ]
        assert r.json()["views"][0]["status"] == "degraded"

    def test_bad_colour_channel_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/revit/highlight",
            json={"element_ids": [1], "color": {"r": 300, "g": 0, "b": 0}},
        )
        assert r.status_code == 422

    def test_addin_unreachable_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BoomRevit:
            async def __aenter__(self):
                raise ConnectionError("addin not running")

            async def __aexit__(self, *exc):
                return None

        monkeypatch.setattr(routes_revit, "make_revit_client", lambda: _BoomRevit())
        r = client.post("/api/revit/highlight", json={"element_ids": [1]})
        assert r.status_code == 503

    def test_empty_ids_rejected(self, client: TestClient) -> None:
        r = client.post("/api/revit/highlight", json={"element_ids": []})
        assert r.status_code == 422

    def test_too_many_ids_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/revit/highlight", json={"element_ids": list(range(501))}
        )
        assert r.status_code == 422


class TestRevitDocument:
    def test_live_model_title(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeRevit:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_document_info(self):
                return {"title": "Snowdon Towers.rvt", "path": "C:/x.rvt"}

        monkeypatch.setattr(routes_revit, "make_revit_client", lambda: _FakeRevit())
        r = client.get("/api/revit/document")
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["title"] == "Snowdon Towers.rvt"

    def test_live_model_path_from_addin_pathname_field(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D7: the REAL addin wire contract returns "pathName", not "path"
        (GetDocumentInfoCommand.cs) — the route must read that key, not the
        bare "path" the old (wrong) code checked."""
        class _FakeRevit:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_document_info(self):
                return {"title": "X", "pathName": "C:/y.rvt"}

        monkeypatch.setattr(routes_revit, "make_revit_client", lambda: _FakeRevit())
        r = client.get("/api/revit/document")
        assert r.status_code == 200
        assert r.json()["path"] == "C:/y.rvt"

    def test_addin_down_degrades_not_errors(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BoomRevit:
            async def __aenter__(self):
                raise ConnectionError("addin not running")

            async def __aexit__(self, *exc):
                return None

        monkeypatch.setattr(routes_revit, "make_revit_client", lambda: _BoomRevit())
        r = client.get("/api/revit/document")
        # Never 5xx — the panel just falls back to run metadata.
        assert r.status_code == 200
        assert r.json()["connected"] is False

    def test_home_screen_no_title_is_disconnected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _HomeRevit:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_document_info(self):
                return {"title": ""}  # Home screen: no document

        monkeypatch.setattr(routes_revit, "make_revit_client", lambda: _HomeRevit())
        r = client.get("/api/revit/document")
        assert r.status_code == 200
        assert r.json()["connected"] is False


class TestSpaMount:
    def test_root_redirects_to_ui(self, client: TestClient) -> None:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/"

    def test_ui_not_built_returns_friendly_503(self, client: TestClient) -> None:
        # The test app is built without an autoaudit-ui/dist directory present
        # (repo hasn't run the frontend build) — this exercises the "not
        # built" branch of mount_spa directly against the real app.
        from bim_orchestrator.service.spa import DIST

        r = client.get("/ui")
        if DIST.is_dir():
            pytest.skip(
                "autoaudit-ui/dist exists on this machine -- not-built branch untestable here"
            )
        assert r.status_code == 503
        assert "not built" in r.text.lower()
        assert "<script" not in r.text  # no stack trace / app shell leaking through

    def test_ui_deep_link_when_not_built_still_503(self, client: TestClient) -> None:
        from bim_orchestrator.service.spa import DIST

        if DIST.is_dir():
            pytest.skip("autoaudit-ui/dist exists on this machine")
        r = client.get("/ui/runs/run-00000000")
        assert r.status_code == 503


class TestSpaMountBuilt:
    """Exercise the "dist exists" branch directly via mount_spa(), independent
    of whether autoaudit-ui/dist actually exists on this machine (frontend is
    a separate implementer's deliverable, B3)."""

    def _build_dist(self, tmp_path: Path) -> Path:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text(
            "<!doctype html><title>AutoAudit</title><body>app shell</body>",
            encoding="utf-8",
        )
        return dist

    def test_deep_link_falls_back_to_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI

        from bim_orchestrator.service import spa as spa_module

        dist = self._build_dist(tmp_path)
        monkeypatch.setattr(spa_module, "DIST", dist)
        app = FastAPI()
        spa_module.mount_spa(app)
        with TestClient(app) as c:
            r = c.get("/ui/runs/run-00000000", follow_redirects=True)
            assert r.status_code == 200
            assert "app shell" in r.text

    def test_api_404_detail_not_clobbered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI, HTTPException

        from bim_orchestrator.service import spa as spa_module

        dist = self._build_dist(tmp_path)
        monkeypatch.setattr(spa_module, "DIST", dist)
        app = FastAPI()

        @app.get("/api/thing/{id}")
        async def _thing(id: str):
            raise HTTPException(status_code=404, detail="thing not found: " + id)

        spa_module.mount_spa(app)
        with TestClient(app) as c:
            r = c.get("/api/thing/xyz")
            assert r.status_code == 404
            assert r.json()["detail"] == "thing not found: xyz"


# ── M2-A: rules / catalogs / builder (Phase 3b, SPEC_3B_M2_RULE_BUILDER_NOW.md) ──


def _write_ruleset_file(
    config_dir: Path, filename: str, *, scenario: str = "demo", category: str = "Doors"
) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    p = config_dir / filename
    p.write_text(
        f"scenario: {scenario}\n"
        f"target_category: {category}\n"
        "rules:\n"
        "- id: doors.mark.present\n"
        "  parameter: Mark\n"
        "  requirement: present_and_nonempty\n"
        "  severity_tag: missing_required_param\n"
        "  description: mark present\n"
        "  autofill:\n"
        "    strategy: none\n"
        "  remediation:\n"
        "    action: create_acc_issue\n",
        encoding="utf-8",
    )
    return p


class TestRulesLibrary:
    def test_list_rules(self, client: TestClient, tmp_path: Path) -> None:
        _write_ruleset_file(tmp_path / "config", "rules.demo.yaml")
        r = client.get("/api/rules")
        assert r.status_code == 200
        files = r.json()["files"]
        assert len(files) == 1
        assert files[0]["scenario"] == "demo"
        assert files[0]["rule_count"] == 1
        assert "Doors" in files[0]["categories"]
        assert files[0]["error"] is None

    def test_list_rules_reports_parse_error_entry(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "rules.broken.yaml").write_text("not: [valid, ruleset", encoding="utf-8")
        r = client.get("/api/rules")
        files = r.json()["files"]
        assert len(files) == 1
        assert files[0]["error"] is not None

    def test_get_rule_detail_flags_legacy_requirement(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "rules.legacy.yaml").write_text(
            "scenario: legacy\n"
            "target_category: Doors\n"
            "rules:\n"
            "- id: doors.width.min\n"
            "  parameter: Width\n"
            "  requirement: numeric_min\n"
            "  threshold: 800.0\n"
            "  severity_tag: value_out_of_range\n"
            "  description: width\n"
            "  autofill:\n"
            "    strategy: none\n"
            "  remediation:\n"
            "    action: create_acc_issue\n",
            encoding="utf-8",
        )
        r = client.get("/api/rules/rules.legacy.yaml")
        assert r.status_code == 200
        assert r.json()["legacy_rule_ids"] == ["doors.width.min"]

    def test_get_rule_404(self, client: TestClient) -> None:
        assert client.get("/api/rules/rules.nope.yaml").status_code == 404

    def test_get_rule_path_traversal_rejected(self, client: TestClient) -> None:
        r = client.get("/api/rules/..%2F..%2Fsecret.yaml")
        assert r.status_code in (400, 404)

    def test_put_creates_new_rule_file(self, client: TestClient, tmp_path: Path) -> None:
        ruleset = {
            "scenario": "new_scenario", "target_category": "Doors",
            "rules": [{
                "id": "doors.mark.present", "parameter": "Mark",
                "requirement": "present_and_nonempty", "severity_tag": "missing_required_param",
                "description": "mark", "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.put(
            "/api/rules/rules.new_scenario.yaml",
            json={"ruleset": ruleset, "overwrite": False},
        )
        assert r.status_code == 200
        assert (tmp_path / "config" / "rules.new_scenario.yaml").exists()

    def test_put_conflict_without_overwrite_409(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _write_ruleset_file(tmp_path / "config", "rules.demo.yaml")
        ruleset = {"scenario": "demo", "target_category": "Doors", "rules": []}
        r = client.put(
            "/api/rules/rules.demo.yaml", json={"ruleset": ruleset, "overwrite": False}
        )
        assert r.status_code == 409

    def test_put_overwrite_true_succeeds(self, client: TestClient, tmp_path: Path) -> None:
        _write_ruleset_file(tmp_path / "config", "rules.demo.yaml")
        ruleset = {"scenario": "demo", "target_category": "Doors", "rules": []}
        r = client.put(
            "/api/rules/rules.demo.yaml", json={"ruleset": ruleset, "overwrite": True}
        )
        assert r.status_code == 200
        assert client.get("/api/rules/rules.demo.yaml").json()["ruleset"]["rules"] == []

    def test_put_invalid_ruleset_422_with_error_list(self, client: TestClient) -> None:
        r = client.put(
            "/api/rules/rules.bad.yaml",
            json={"ruleset": {"scenario": "x"}, "overwrite": True},  # missing target_category
        )
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], list)
        assert r.json()["detail"]

    def test_put_rejects_numeric_compare_without_threshold(
        self, client: TestClient
    ) -> None:
        """PUT is the FINAL validation authority, not POST /builder/validate.

        The Builder's live validation is a UX affordance the client may
        debounce, race, or skip entirely; a scripted client never calls it at
        all. This endpoint ran Pydantic only — and `threshold` is legitimately
        optional per requirement — so a compare with no limit saved with a 200
        and then reported 100% compliance at run time.
        """
        ruleset = {
            "scenario": "x", "target_category": "Doors",
            "rules": [{
                "id": "doors.width.min", "parameter": "Width",
                "requirement": "numeric_compare", "operator": ">=",
                "severity_tag": "rule_violation", "description": "width",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.put(
            "/api/rules/rules.nothreshold.yaml",
            json={"ruleset": ruleset, "overwrite": True},
        )
        assert r.status_code == 422
        assert any("threshold" in item["field"] for item in r.json()["detail"])

    def test_put_rejects_string_zero_threshold(self, client: TestClient) -> None:
        # M6: the exact false-pass the audit found — a scripted client sends the
        # STRING "0"; before the coerce fix PUT returned 200 and saved width>=0.
        ruleset = {
            "scenario": "x", "target_category": "Doors",
            "rules": [{
                "id": "doors.width.min", "parameter": "Width",
                "requirement": "numeric_compare", "operator": ">=",
                "threshold": "0",
                "severity_tag": "rule_violation", "description": "width",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.put(
            "/api/rules/rules.strzero.yaml",
            json={"ruleset": ruleset, "overwrite": True},
        )
        assert r.status_code == 422
        assert any("threshold" in item["field"] for item in r.json()["detail"])

    def test_put_rejects_uncompilable_scope_filter_pattern(
        self, client: TestClient
    ) -> None:
        ruleset = {
            "scenario": "x", "target_category": "Doors",
            "rules": [{
                "id": "doors.mark.present", "parameter": "Mark",
                "requirement": "present_and_nonempty",
                "scope_filter": {"param": "Function", "pattern": "(unclosed"},
                "severity_tag": "missing_required_param", "description": "mark",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.put(
            "/api/rules/rules.badscope.yaml",
            json={"ruleset": ruleset, "overwrite": True},
        )
        assert r.status_code == 422
        assert any("scope_filter" in item["field"] for item in r.json()["detail"])

    def test_put_still_accepts_gt_zero_positive_check(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # Guard against over-blocking: `> 0` is the positive_number migration
        # and 4 shipped rules use it. It must keep saving.
        ruleset = {
            "scenario": "x", "target_category": "Rooms",
            "rules": [{
                "id": "rooms.area.positive", "parameter": "Area",
                "requirement": "numeric_compare", "operator": ">", "threshold": 0,
                "severity_tag": "rule_violation", "description": "area positive",
                "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.put(
            "/api/rules/rules.positive.yaml",
            json={"ruleset": ruleset, "overwrite": True},
        )
        assert r.status_code == 200
        assert (tmp_path / "config" / "rules.positive.yaml").exists()

    def test_put_enforces_unique_in_set_autofix_guard(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # QA F2 (rule_builder_core.enforce_unique_autofix): unique_in_set must be
        # forced to fixability=auto / target=instance even when the payload says
        # otherwise — the SAME guard the Streamlit save path runs.
        ruleset = {
            "scenario": "unique_demo", "target_category": "Rooms",
            "rules": [{
                "id": "rooms.number.unique", "parameter": "Number",
                "requirement": "unique_in_set", "severity_tag": "uniqueness_violation",
                "description": "unique", "fixability": "manual",
                "autofill": {"strategy": "none"}, "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.put(
            "/api/rules/rules.unique_demo.yaml", json={"ruleset": ruleset, "overwrite": True}
        )
        assert r.status_code == 200
        saved = client.get("/api/rules/rules.unique_demo.yaml").json()["ruleset"]["rules"][0]
        assert saved["fixability"] == "auto"
        assert saved["remediation"]["target"] == "instance"

    def test_delete_rule(self, client: TestClient, tmp_path: Path) -> None:
        _write_ruleset_file(tmp_path / "config", "rules.demo.yaml")
        r = client.delete("/api/rules/rules.demo.yaml")
        assert r.status_code == 200
        assert not (tmp_path / "config" / "rules.demo.yaml").exists()

    def test_delete_missing_404(self, client: TestClient) -> None:
        assert client.delete("/api/rules/rules.nope.yaml").status_code == 404

    def test_non_rules_config_files_unreachable(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # SVC-3 (2026-07 review): config/ also holds ost_catalog / autonomy /
        # lookup.* / reference.* — the rules CRUD surface is pinned to
        # rules.<scenario>.yaml so a UI bug or stray click can never read,
        # overwrite, or DELETE the engine's other config files.
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "ost_catalog.yaml").write_text("categories: {}\n", encoding="utf-8")
        assert client.get("/api/rules/ost_catalog.yaml").status_code == 400
        assert client.delete("/api/rules/ost_catalog.yaml").status_code == 400
        r = client.put(
            "/api/rules/ost_catalog.yaml",
            json={
                "ruleset": {"scenario": "x", "target_category": "Doors", "rules": []},
                "overwrite": True,
            },
        )
        assert r.status_code == 400
        # untouched — not overwritten by the PUT above
        assert (cfg / "ost_catalog.yaml").read_text(encoding="utf-8") == "categories: {}\n"
        # missing the rules. prefix / .yaml suffix → same rejection
        assert client.get("/api/rules/demo").status_code == 400
        assert client.delete("/api/rules/rules.demo").status_code == 400


class TestCatalogs:
    def test_categories_shape_and_notes(self, client: TestClient) -> None:
        r = client.get("/api/catalogs/categories")
        assert r.status_code == 200
        cats = r.json()["categories"]
        assert cats
        assert {"key", "label", "note"} <= cats[0].keys()
        stairs = next(c for c in cats if c["label"] == "Stairs")
        assert stairs["note"] is not None  # rule_builder_core.CATEGORY_NOTES

    def test_params_for_known_category(self, client: TestClient) -> None:
        r = client.get("/api/catalogs/params", params={"category": "Doors"})
        assert r.status_code == 200
        params = r.json()["params"]
        assert any(p["name"] == "Fire Rating" for p in params)

    def test_params_for_unknown_category_empty(self, client: TestClient) -> None:
        r = client.get("/api/catalogs/params", params={"category": "Not A Real Category"})
        assert r.status_code == 200
        assert r.json()["params"] == []

    def test_lookups_put_get_roundtrip(self, client: TestClient, tmp_path: Path) -> None:
        body = {
            "keys": [{"param": "host.Fire Rating", "dimension": "fire_rating"}],
            "rows": [{"when": ["1 HR"], "require": "20 min"}],
            "description": "test table", "overwrite": False,
        }
        r = client.put("/api/catalogs/lookups/uitest_m2", json=body)
        assert r.status_code == 200
        assert (tmp_path / "config" / "lookup.uitest_m2.yaml").exists()
        listed = client.get("/api/catalogs/lookups").json()["lookups"]
        assert any(t["name"] == "uitest_m2" and t["rows"] for t in listed)

    def test_lookups_conflict_without_overwrite_409(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        body = {"keys": [], "rows": [], "overwrite": False}
        assert client.put("/api/catalogs/lookups/dup_m2", json=body).status_code == 200
        r = client.put("/api/catalogs/lookups/dup_m2", json=body)
        assert r.status_code == 409

    def test_references_put_get_roundtrip(self, client: TestClient, tmp_path: Path) -> None:
        body = {
            "entries": [{"canonical": "Oak", "aliases": ["white oak"]}],
            "case_sensitive": False, "overwrite": False,
        }
        r = client.put("/api/catalogs/references/materials_m2", json=body)
        assert r.status_code == 200
        names = [x["name"] for x in client.get("/api/catalogs/references").json()["references"]]
        assert "materials_m2" in names

    def test_references_conflict_without_overwrite_409(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        body = {"entries": [{"canonical": "Oak", "aliases": []}], "overwrite": False}
        assert client.put("/api/catalogs/references/dup_ref_m2", json=body).status_code == 200
        r = client.put("/api/catalogs/references/dup_ref_m2", json=body)
        assert r.status_code == 409


class TestBuilder:
    def test_validate_ok(self, client: TestClient) -> None:
        r = client.post("/api/builder/validate", json={
            "rule": {
                "id": "doors.mark.present", "category": "Doors", "parameter": "Mark",
                "requirement": "present_and_nonempty", "fixability": "manual",
                "remediation": {"action": "create_acc_issue"},
            },
            "is_geometry": False,
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_validate_reports_field_errors(self, client: TestClient) -> None:
        r = client.post("/api/builder/validate", json={"rule": {}, "is_geometry": False})
        body = r.json()
        assert body["ok"] is False
        assert {"field": "id", "message": "Rule ID must not be empty"} in body["errors"]

    def test_draft_503_when_no_llm_key(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("BIM_LLM_PROVIDER", raising=False)
        r = client.post("/api/builder/draft", json={"text": "Doors must have a Mark value"})
        assert r.status_code == 503

    def test_draft_succeeds_with_stub_client(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SVC-5 (2026-07 review): draft_rule calls asyncio.run internally (its
        # Streamlit-era sync surface). Run directly on FastAPI's event-loop
        # thread, BOTH asyncio.run and its new-loop fallback raise "loop
        # already running" — every live-LLM draft died as a 422 while the only
        # service-level test covered the 503 no-key branch. The handler now
        # offloads to a worker thread; this pins the happy path end-to-end
        # with a stub client (no network).
        from bim_orchestrator.llm import factory as llm_factory

        rule = {
            "id": "doors.mark.present", "category": "Doors", "parameter": "Mark",
            "requirement": "present_and_nonempty", "fixability": "manual",
            "severity_tag": "missing_required_param", "description": "mark",
            "remediation": {"action": "create_acc_issue"},
        }

        class _StubLLM:
            async def complete_json(self, *, system: str, prompt: str) -> dict:
                assert "Doors must have a Mark value" in prompt
                return rule

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(llm_factory, "build_llm_client", lambda: _StubLLM())
        r = client.post("/api/builder/draft", json={"text": "Doors must have a Mark value"})
        assert r.status_code == 200
        body = r.json()
        assert body["rule"]["id"] == "doors.mark.present"
        assert body["warnings"] == []

    def test_preview_normalize_duration(self, client: TestClient) -> None:
        r = client.post("/api/builder/preview", json={
            "normalize_kind": "duration", "normalize_format": "{h}-hour", "sample": "180 MIN",
        })
        assert r.status_code == 200
        assert r.json() == {"output": "3-hour", "matches": None, "error": None}

    def test_preview_reference(self, client: TestClient, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "reference.materials_preview.yaml").write_text(
            "name: materials_preview\ncase_sensitive: false\nentries:\n"
            "- canonical: Oak\n  aliases: [white oak]\n",
            encoding="utf-8",
        )
        r = client.post("/api/builder/preview", json={
            "normalize_kind": "reference", "reference": "materials_preview", "sample": "white oak",
        })
        assert r.status_code == 200
        assert r.json()["output"] == "Oak"

    def test_ids_export_then_import_roundtrip(self, client: TestClient) -> None:
        ruleset = {
            "scenario": "ids_rt", "target_category": "Doors",
            "rules": [{
                "id": "doors.mark.present", "parameter": "Mark",
                "requirement": "present_and_nonempty", "severity_tag": "missing_required_param",
                "description": "mark present", "autofill": {"strategy": "none"},
                "remediation": {"action": "create_acc_issue"},
            }],
        }
        r = client.post("/api/builder/ids-export", json={"ruleset": ruleset})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/xml")

        r2 = client.post(
            "/api/builder/ids-import",
            files={"file": ("rules.ids", r.content, "application/xml")},
        )
        assert r2.status_code == 200
        assert r2.json()["rule_count"] == 1

    def test_ids_import_bad_file_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/builder/ids-import",
            files={"file": ("bad.ids", b"not xml at all", "application/xml")},
        )
        assert r.status_code == 422


class TestSettings:
    def test_get_masks_secret_and_reports_llm_services(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret12345678")
        monkeypatch.setenv("BIM_LLM_PROVIDER", "anthropic")
        r = client.get("/api/settings")
        assert r.status_code == 200
        assert "sk-ant-secret12345678" not in r.text  # full secret never round-trips
        body = r.json()
        items = {e["key"]: e for e in body["env"]}
        assert items["ANTHROPIC_API_KEY"]["set"] is True
        assert items["ANTHROPIC_API_KEY"]["masked"] == "••••5678"
        assert body["llm"]["provider"] == "anthropic"
        assert set(body["services"]) == {"lod", "spatial", "revitcontrol"}
        # empty tmp config_dir -> no audit_services.yaml -> all unavailable
        assert body["services"] == {"lod": False, "spatial": False, "revitcontrol": False}

    def test_get_unset_key_has_null_masked(self, client: TestClient) -> None:
        r = client.get("/api/settings")
        items = {e["key"]: e for e in r.json()["env"]}
        assert items["APS_CLIENT_ID"]["set"] is False
        assert items["APS_CLIENT_ID"]["masked"] is None

    @pytest.mark.parametrize("secret", ["x", "ab", "abc", "abcd", "sk-ant1"])
    def test_get_never_echoes_a_short_secret(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, secret: str
    ) -> None:
        """S-06: the old rule returned the WHOLE value below 4 characters, and
        most of it below 8 — inverting the mask exactly where it has to hold.

        Short values are not an edge case here: this endpoint fronts an env
        allowlist a human types into, so a truncated paste or a stray character
        IS what lands in it, and a wrong value is the one an operator will ask
        the API to show them.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        r = client.get("/api/settings")
        assert r.status_code == 200
        items = {e["key"]: e for e in r.json()["env"]}
        item = items["ANTHROPIC_API_KEY"]
        assert item["set"] is True, "the caller still learns a value is stored"
        assert item["masked"] == "••••••••"
        assert secret not in item["masked"]

    def test_the_mask_does_not_leak_the_length(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mask that grew with the secret would be a length oracle."""
        masks = []
        for secret in ("sk-ant-0123456789", "sk-ant-0123456789abcdefghijklmnop"):
            monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
            items = {e["key"]: e for e in client.get("/api/settings").json()["env"]}
            masks.append(items["ANTHROPIC_API_KEY"]["masked"])
        assert len({len(m) for m in masks}) == 1

    def test_a_long_secret_still_shows_its_tail(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hint exists so an operator can tell WHICH key is stored — the fix
        must not turn every row into an identical row of bullets."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret12345678")
        items = {e["key"]: e for e in client.get("/api/settings").json()["env"]}
        assert items["ANTHROPIC_API_KEY"]["masked"] == "••••5678"

    def test_put_rejects_key_outside_allowlist(self, client: TestClient) -> None:
        r = client.put("/api/settings/env", json={"key": "PATH", "value": "evil"})
        assert r.status_code == 403

    def test_put_aps_key_writes_forma_mcp_env_not_root(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        r = client.put("/api/settings/env", json={"key": "APS_CLIENT_ID", "value": "abc123"})
        assert r.status_code == 200
        forma_env = tmp_path / "vendor" / "forma-mcp" / ".env"
        assert forma_env.exists()
        assert "APS_CLIENT_ID=abc123" in forma_env.read_text(encoding="utf-8")
        root_env = tmp_path / ".env"
        assert not root_env.exists() or "APS_CLIENT_ID" not in root_env.read_text(encoding="utf-8")

    def test_put_forma_path_key_writes_root_env(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        r = client.put(
            "/api/settings/env", json={"key": "FORMA_MCP_SERVER_CWD", "value": "D:/vendor"}
        )
        assert r.status_code == 200
        root_env = tmp_path / ".env"
        assert root_env.exists()
        assert "FORMA_MCP_SERVER_CWD=D:/vendor" in root_env.read_text(encoding="utf-8")

    def test_put_upserts_existing_key_in_place(self, client: TestClient, tmp_path: Path) -> None:
        client.put("/api/settings/env", json={"key": "BIM_LLM_MODEL", "value": "first"})
        client.put("/api/settings/env", json={"key": "BIM_LLM_MODEL", "value": "second"})
        text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert text.count("BIM_LLM_MODEL=") == 1
        assert "BIM_LLM_MODEL=second" in text

    def test_put_rejects_newline_in_value(self, client: TestClient, tmp_path: Path) -> None:
        # SVC-4 (2026-07 review): .env is line-oriented — a value carrying a
        # newline would smuggle a second, NON-allowlisted key=value line past
        # the 403 gate (and vendor/forma-mcp/.env feeds a subprocess).
        r = client.put(
            "/api/settings/env",
            json={"key": "BIM_LLM_MODEL", "value": "x\nNODE_OPTIONS=--evil"},
        )
        assert r.status_code == 400
        assert not (tmp_path / ".env").exists()  # nothing written
        r2 = client.put(
            "/api/settings/env", json={"key": "BIM_LLM_MODEL", "value": "x\rEVIL=1"}
        )
        assert r2.status_code == 400

    def test_put_rejects_malformed_key_shapes(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # "BIM_X\nEVIL" passes startswith("BIM_") — the shape check must run
        # BEFORE the allowlist. Same for '=' smuggled into the key.
        for bad_key in ("BIM_X\nEVIL", "BIM_X=Y", "BIM_lower", "BIM_ SPACE"):
            r = client.put("/api/settings/env", json={"key": bad_key, "value": "1"})
            assert r.status_code == 400, bad_key
        assert not (tmp_path / ".env").exists()

    def test_test_forma_delegates_to_hello_smoke(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_settings, "_test_forma_connection", lambda: (True, "hello ok"))
        r = client.post("/api/settings/test/forma")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "message": "hello ok"}

    def test_test_revit_reports_addin_down(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_test_revit() -> tuple[bool, str, str | None]:
            return False, "No response at http://127.0.0.1:7891/health", None

        monkeypatch.setattr(routes_settings, "_test_revit_connection", fake_test_revit)
        r = client.post("/api/settings/test/revit")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["version"] is None

    def test_test_revit_reports_version_when_up(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_test_revit() -> tuple[bool, str, str | None]:
            return True, "revit-mcp-addin v1.2.3 · port 7891 · auth on", "1.2.3"

        monkeypatch.setattr(routes_settings, "_test_revit_connection", fake_test_revit)
        r = client.post("/api/settings/test/revit")
        body = r.json()
        assert body == {
            "ok": True, "message": "revit-mcp-addin v1.2.3 · port 7891 · auth on",
            "version": "1.2.3",
        }

    def test_doctor_structured(self, client: TestClient) -> None:
        r = client.get("/api/settings/doctor")
        assert r.status_code == 200
        checks = r.json()["checks"]
        assert checks
        for c in checks:
            assert c["status"] in {"pass", "warn", "fail"}
        assert "python >= 3.12" in {c["name"] for c in checks}


class TestExtraction:
    _VALID_RULES_YAML = """
scenario: extracted_demo
target_category: Doors
rules:
  - id: doors.mark.present
    parameter: Mark
    requirement: present_and_nonempty
    severity_tag: missing_required_param
    description: mark present
    autofill:
      strategy: none
    remediation:
      action: create_acc_issue
"""

    def test_pdf_503_when_rules_extractor_missing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_extraction, "rules_extractor_available", lambda: False)
        r = client.post(
            "/api/extraction/pdf", files={"file": ("spec.txt", b"hello", "text/plain")}
        )
        assert r.status_code == 503
        assert "rules-extractor" in r.json()["detail"]
        assert "uv pip install -e" in r.json()["detail"]

    def test_pdf_success_via_stubbed_extraction_bridge(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_extraction, "rules_extractor_available", lambda: True)
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )

        class _Scenario:
            scenario = "extracted_demo"
            rules_yaml = TestExtraction._VALID_RULES_YAML

        class _Result:
            scenarios = [_Scenario()]

        def fake_run_extraction(tmp_path, *, tables, client, max_sections=None):
            return _Result(), []  # (convert_result, coverage)

        monkeypatch.setattr(routes_extraction, "_run_extraction", fake_run_extraction)

        r = client.post(
            "/api/extraction/pdf",
            files={"file": ("spec.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ruleset"]["scenario"] == "extracted_demo"
        assert len(body["ruleset"]["rules"]) == 1
        assert body["ruleset"]["rules"][0]["id"] == "doors.mark.present"

    def test_pdf_422_when_extraction_yields_no_rules(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_extraction, "rules_extractor_available", lambda: True)
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )

        class _Result:
            scenarios = []

        monkeypatch.setattr(
            routes_extraction, "_run_extraction",
            lambda tmp_path, *, tables, client, max_sections=None: (_Result(), []),
        )
        r = client.post(
            "/api/extraction/pdf", files={"file": ("spec.txt", b"no rules here", "text/plain")}
        )
        assert r.status_code == 422

    def test_pdf_max_sections_threaded_and_validated(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 2026-07-15: a demo cap so a big PDF runs a snappy live segment
        # (~20-30s) instead of minutes; the uncapped run stays the golden.
        monkeypatch.setattr(routes_extraction, "rules_extractor_available", lambda: True)
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )
        seen: dict = {}

        class _Scenario:
            scenario = "capped"
            rules_yaml = TestExtraction._VALID_RULES_YAML

        class _Result:
            scenarios = [_Scenario()]

        def fake_run_extraction(tmp_path, *, tables, client, max_sections=None):
            seen["max_sections"] = max_sections
            return _Result(), []

        monkeypatch.setattr(routes_extraction, "_run_extraction", fake_run_extraction)

        r = client.post(
            "/api/extraction/pdf",
            files={"file": ("spec.pdf", b"%PDF-1.4", "application/pdf")},
            data={"max_sections": "12"},
        )
        assert r.status_code == 200
        assert seen["max_sections"] == 12

        # invalid cap rejected before any work
        r2 = client.post(
            "/api/extraction/pdf",
            files={"file": ("spec.pdf", b"%PDF-1.4", "application/pdf")},
            data={"max_sections": "0"},
        )
        assert r2.status_code == 422

    def test_pdf_502_when_every_section_llm_call_failed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 2026-07-15 smoke test caught this: when every section's LLM call fails
        # (billing / auth / rate-limit) the endpoint used to swallow it as a
        # misleading 422 "no executable rules". With errored coverage + no
        # scenarios it must surface the REAL upstream error as a 502.
        monkeypatch.setattr(routes_extraction, "rules_extractor_available", lambda: True)
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )

        class _Result:
            scenarios = []

        class _Cov:
            def __init__(self, status, error):
                self.status = status
                self.error = error

        coverage = [
            _Cov("error", "Error code: 400 - credit balance is too low"),
            _Cov("error", "Error code: 400 - credit balance is too low"),  # dup collapses
            _Cov("skipped", None),
        ]
        monkeypatch.setattr(
            routes_extraction, "_run_extraction",
            lambda tmp_path, *, tables, client, max_sections=None: (_Result(), coverage),
        )
        r = client.post(
            "/api/extraction/pdf", files={"file": ("spec.pdf", b"%PDF-1.4", "application/pdf")}
        )
        assert r.status_code == 502
        assert "credit balance is too low" in r.json()["detail"]
        assert "extraction LLM call failed" in r.json()["detail"]

    def test_pdf_422_when_extraction_raises(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_extraction, "rules_extractor_available", lambda: True)
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )

        def boom(tmp_path, *, tables, client, max_sections=None):
            raise RuntimeError("pdf parse failed")

        monkeypatch.setattr(routes_extraction, "_run_extraction", boom)
        r = client.post(
            "/api/extraction/pdf", files={"file": ("spec.pdf", b"garbage", "application/pdf")}
        )
        assert r.status_code == 422
        assert "pdf parse failed" in r.json()["detail"]


class TestUploadSizeCap:
    """S-03 (2026-07-25 live review) — both multipart routes did
    ``raw = await file.read()``: the WHOLE upload into RAM with no ceiling,
    so one oversized POST could exhaust the pilot machine's memory. Reads are
    now chunked and stop at a cap (413).
    """

    @staticmethod
    def _stub_extraction(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Wire extraction so a request that PASSES the cap would succeed —
        otherwise a 413 proves nothing (the route could be failing later)."""
        seen: list[str] = []
        monkeypatch.setattr(
            routes_extraction, "rules_extractor_available", lambda: True
        )
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )

        class _Scenario:
            scenario = "extracted_demo"
            rules_yaml = TestExtraction._VALID_RULES_YAML

        class _Result:
            scenarios = [_Scenario()]

        def fake_run_extraction(tmp_path, *, tables, client, max_sections=None):
            seen.append(tmp_path)
            return _Result(), []

        monkeypatch.setattr(routes_extraction, "_run_extraction", fake_run_extraction)
        return seen

    def test_oversized_document_is_413_and_never_reaches_extraction(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._stub_extraction(monkeypatch)
        monkeypatch.setenv("AUTOAUDIT_MAX_UPLOAD_MB", "1")
        r = client.post(
            "/api/extraction/pdf",
            files={"file": ("big.pdf", b"x" * (2 << 20), "application/pdf")},
        )
        assert r.status_code == 413
        assert "upload limit" in r.json()["detail"]
        assert not seen, "the oversized upload still reached the extractor"

    def test_document_under_the_cap_still_works(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cap must not become "uploads are broken" — pins the happy path
        # at a size that exercises several chunks.
        seen = self._stub_extraction(monkeypatch)
        monkeypatch.setenv("AUTOAUDIT_MAX_UPLOAD_MB", "4")
        r = client.post(
            "/api/extraction/pdf",
            files={"file": ("ok.pdf", b"%PDF-1.4" + b"x" * (3 << 20), "application/pdf")},
        )
        assert r.status_code == 200
        assert seen, "a legal upload was rejected"

    def test_streamed_document_lands_on_disk_intact(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Chunked streaming must not corrupt or truncate the payload — the
        # extractor reads the temp file by path, so a wrong byte count here
        # would be a silently mangled document, not an error.
        payload = bytes(range(256)) * 8192  # 2 MiB, every byte value
        captured: dict[str, bytes] = {}
        monkeypatch.setattr(
            routes_extraction, "rules_extractor_available", lambda: True
        )
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: object()
        )

        def fake_run_extraction(tmp_path, *, tables, client, max_sections=None):
            captured["bytes"] = Path(tmp_path).read_bytes()
            raise RuntimeError("stop here - we only wanted the file")

        monkeypatch.setattr(routes_extraction, "_run_extraction", fake_run_extraction)
        client.post(
            "/api/extraction/pdf",
            files={"file": ("bin.pdf", payload, "application/pdf")},
        )
        assert captured["bytes"] == payload

    def test_oversized_ids_import_is_413(self, client: TestClient) -> None:
        from bim_orchestrator.service._common import MAX_IDS_BYTES

        r = client.post(
            "/api/builder/ids-import",
            files={"file": ("huge.ids", b"<" * (MAX_IDS_BYTES + 1), "application/xml")},
        )
        assert r.status_code == 413

    def test_413_never_names_a_knob_that_does_not_govern_that_route(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Caught live: the shared helper hard-coded "AUTOAUDIT_MAX_UPLOAD_MB
        # raises it" into every 413, including the IDS route whose cap is
        # FIXED — sending the operator to a knob that changes nothing.
        from bim_orchestrator.service._common import MAX_IDS_BYTES

        ids = client.post(
            "/api/builder/ids-import",
            files={"file": ("huge.ids", b"<" * (MAX_IDS_BYTES + 1), "application/xml")},
        )
        assert "AUTOAUDIT_MAX_UPLOAD_MB" not in ids.json()["detail"]

        self._stub_extraction(monkeypatch)
        monkeypatch.setenv("AUTOAUDIT_MAX_UPLOAD_MB", "1")
        pdf = client.post(
            "/api/extraction/pdf",
            files={"file": ("big.pdf", b"x" * (2 << 20), "application/pdf")},
        )
        # ...but the route it DOES govern still tells the operator the knob.
        assert "AUTOAUDIT_MAX_UPLOAD_MB" in pdf.json()["detail"]

    def test_junk_env_override_falls_back_instead_of_disabling_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-closed: a broken env var must not remove the limit it was set
        # to tune. "0" and "-5" would otherwise mean no cap at all.
        from bim_orchestrator.service._common import (
            DEFAULT_MAX_UPLOAD_MB,
            max_upload_bytes,
        )

        for junk in ("", "abc", "0", "-5"):
            monkeypatch.setenv("AUTOAUDIT_MAX_UPLOAD_MB", junk)
            assert max_upload_bytes() == DEFAULT_MAX_UPLOAD_MB << 20
        monkeypatch.setenv("AUTOAUDIT_MAX_UPLOAD_MB", "7")
        assert max_upload_bytes() == 7 << 20


class TestExtractionCancellation:
    """S-04 (2026-07-25 live review) — the service ran extraction under
    ``asyncio.wait_for(asyncio.to_thread(...), 300)``. On timeout the AWAIT is
    abandoned but a Python thread cannot be killed, so rules_extractor kept
    fanning sections out and every one kept BILLING for a result nobody would
    read. The thread still can't be killed; the spending can be stopped at the
    one chokepoint every billed call passes through.
    """

    def test_seam_client_refuses_calls_after_cancel(self) -> None:
        from bim_orchestrator.llm.extraction_bridge import (
            ExtractionCancelled,
            ExtractionSeamClient,
        )

        calls: list[str] = []

        class _Inner:
            def emit_ruleset(self, *, system, user_content, tool, model):
                calls.append(model)
                return {"ok": True}

        seam = ExtractionSeamClient(_Inner())
        kw = {"system": "s", "user_content": "u", "tool": {}, "model": "m"}
        assert seam.emit_ruleset(**kw) == {"ok": True}
        seam.cancel()
        with pytest.raises(ExtractionCancelled):
            seam.emit_ruleset(**kw)
        # The refusal happened BEFORE the billed inner call.
        assert calls == ["m"], "a cancelled client still called the API"

    def test_cancel_is_idempotent_and_visible(self) -> None:
        from bim_orchestrator.llm.extraction_bridge import ExtractionSeamClient

        seam = ExtractionSeamClient(object())
        assert seam.cancelled is False
        seam.cancel()
        seam.cancel()
        assert seam.cancelled is True

    def test_cancelled_call_is_not_recorded_as_usage(self) -> None:
        # A refused call is not a call that happened — recording it would
        # inflate the cost report the operator reads.
        from bim_orchestrator.llm.extraction_bridge import (
            ExtractionCancelled,
            ExtractionSeamClient,
        )
        from bim_orchestrator.llm.usage import UsageRecorder

        recorder = UsageRecorder()
        seam = ExtractionSeamClient(object(), recorder=recorder)
        seam.cancel()
        with pytest.raises(ExtractionCancelled):
            seam.emit_ruleset(system="s", user_content="u", tool={}, model="m")
        assert recorder.total_calls == 0

    def test_route_cancels_the_client_on_timeout(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The wire, not just the mechanism (the L-01 lesson): a timeout must
        # actually call cancel(), otherwise the orphan keeps spending.
        import asyncio

        class _Seam:
            def __init__(self) -> None:
                self.cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

        seam = _Seam()
        monkeypatch.setattr(
            routes_extraction, "rules_extractor_available", lambda: True
        )
        monkeypatch.setattr(
            routes_extraction, "make_extraction_client", lambda recorder=None: seam
        )

        async def fake_wait_for(awaitable, timeout):
            # Close the coroutine we are not awaiting, so no "never awaited"
            # warning masks the assertion below.
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        r = client.post(
            "/api/extraction/pdf",
            files={"file": ("spec.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert r.status_code == 504
        assert seam.cancelled, "timeout did not cancel the extraction client"


class TestValidateEndpointCanSeeRulesetContext:
    """A-03 (2026-07-25 live review) — `POST /builder/validate` had no field
    for the enclosing ruleset's categories, so it could not reach the same
    verdict as `PUT /rules/{name}` even in principle: the client was unable to
    supply the context, not merely forgetting to.

    A rule that leaves `category` unset INHERITS `target_category`, and the
    read-only write-target guard resolves nothing without it. Result: the live
    validation says OK and the save gate answers 422 on the same rule — the UI
    and the gate disagreeing about the same input.
    """

    _RULE = {
        "id": "a03.readonly",
        "category": "",                    # inherits from the ruleset
        "parameter": "Area",               # read-only built-in
        "requirement": "numeric_compare",
        "operator": ">=",
        "threshold": 1.0,
        "severity_tag": "rule_violation",
        "description": "area minimum",
        "fixability": "auto",
        "autofill": {"strategy": "infer_from_name"},
        "remediation": {"action": "set_parameter", "target": "instance"},
    }

    def test_validate_agrees_with_the_save_gate_when_given_the_context(
        self, client: TestClient
    ) -> None:
        v = client.post("/api/builder/validate", json={
            "rule": self._RULE, "ruleset_categories": ["Doors"],
        })
        assert v.status_code == 200
        assert v.json()["ok"] is False, (
            "validate accepted a rule the save gate rejects"
        )

        put = client.put("/api/rules/rules.a03.yaml", json={
            "ruleset": {"scenario": "a03", "target_category": ["Doors"],
                        "rules": [self._RULE]},
            "overwrite": True,
        })
        assert put.status_code == 422      # both surfaces, same answer

    def test_the_field_is_optional_so_existing_clients_are_unaffected(
        self, client: TestClient
    ) -> None:
        # Every caller written before this field keeps working; the answer is
        # just less complete, exactly as it was.
        r = client.post("/api/builder/validate", json={"rule": self._RULE})
        assert r.status_code == 200
        assert "ok" in r.json()

    def test_a_scalar_target_category_is_accepted_too(
        self, client: TestClient
    ) -> None:
        # `target_category` is legitimately a str OR a list in the schema —
        # the request model must not be stricter than the thing it mirrors.
        r = client.post("/api/builder/validate", json={
            "rule": self._RULE, "ruleset_categories": "Doors",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
