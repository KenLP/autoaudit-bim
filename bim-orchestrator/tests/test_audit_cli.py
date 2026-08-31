"""P3-1 — `--audit` orchestration + the reports' "Audit axes" section."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bim_orchestrator import orchestrator
from bim_orchestrator.audit_axes import AxesResult
from bim_orchestrator.audit_report import render_audit_report
from bim_orchestrator.reports import (
    load_axes_payload,
    render_axes_section,
    render_per_run_report,
)
from bim_orchestrator.state import Finding


def _axes_finding() -> Finding:
    return {
        "rule_id": "lod_min_300",
        "element_id": "447121",
        "parameter": "LOD",
        "severity_tag": "lod_violation",
        "severity": "severity_high",
        "message": "detected LOD 200 vs required 300",
        "suggested_value": None,
        "citation": None,
        "status": "non_compliant",
    }


def _write_profile(
    tmp_path: Path, *, mode: str = "check", rules: bool = True,
    propose_only: bool | None = None,
) -> Path:
    lines = ["name: t"]
    if rules:
        rules_file = tmp_path / "rules.t.yaml"
        rules_file.write_text("scenario: t\nrules: []\n", encoding="utf-8")
        lines += ["rules:", f"  - {rules_file.as_posix()}"]
    lines += ["run:", f"  mode: {mode}", "  max_issues: 7", "  max_elements: 42"]
    if propose_only is not None:
        lines += [f"  propose_only: {str(propose_only).lower()}"]
    p = tmp_path / "audit.t.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture()
def stub_axes(monkeypatch: pytest.MonkeyPatch):
    """Replace run_audit_axes with a stub that stages one artifact + returns
    one finding; returns the mutable AxesResult so tests can tweak it."""
    result = AxesResult(findings=[_axes_finding()], skipped=["spatial: unconfigured"])

    async def fake_run_audit_axes(profile, services, axes_dir: Path, **kw):
        axes_dir.mkdir(parents=True, exist_ok=True)
        (axes_dir / "lod.json").write_text(
            json.dumps({"summary": {"total": 1, "failed": 1}}), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        "bim_orchestrator.audit_axes.run_audit_axes", fake_run_audit_axes
    )
    return result


class TestAuditDispatch:
    async def test_check_mode_seeds_geometry_and_persists_axes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes, capsys
    ) -> None:
        captured: dict = {}

        async def fake_check(rules_path, autonomy_path, findings_out, *, geometry_seed=None, on_folder=None, **kw):
            captured["rules_path"] = rules_path
            captured["geometry_seed"] = geometry_seed
            # Simulate the run folder appearing → the hook must copy axes/.
            class _F:  # minimal RunFolder stand-in
                root = tmp_path / "run-deadbeef"
            _F.root.mkdir()
            on_folder(_F)
            return 0

        monkeypatch.setattr(orchestrator, "check", fake_check)
        rc = await orchestrator.audit(
            _write_profile(tmp_path, mode="check"),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=3,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0
        assert captured["geometry_seed"] == [_axes_finding()]
        # Axes artifacts persisted into the run folder by the hook
        assert (tmp_path / "run-deadbeef" / "axes" / "lod.json").exists()
        # Honest degrade printed to console
        out = capsys.readouterr().out
        assert "Audit axis skipped: spatial: unconfigured" in out
        assert "1 finding(s)" in out

    async def test_persists_profile_json_into_run_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes
    ) -> None:
        """W3 (SPEC_SCHEDULED_AUDIT_DELTA.md Q5): audit() stamps profile.json
        (name/mode/rules-by-basename/propose_only) alongside axes/ the moment
        the run folder exists, via the internal ``_persist`` hook."""

        async def fake_check(rules_path, autonomy_path, findings_out, *, geometry_seed=None, on_folder=None, **kw):
            class _F:
                root = tmp_path / "run-deadbeef"
            _F.root.mkdir()
            on_folder(_F)
            return 0

        monkeypatch.setattr(orchestrator, "check", fake_check)
        rc = await orchestrator.audit(
            _write_profile(tmp_path, mode="check", propose_only=True),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=3,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0
        payload = json.loads((tmp_path / "run-deadbeef" / "profile.json").read_text())
        assert payload["profile_name"] == "t"
        assert payload["mode"] == "check"
        assert payload["propose_only"] is True
        assert payload["rules"] == ["rules.t.yaml"]  # basename, not absolute path

    async def test_run_revit_mode_forwards_profile_caps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes
    ) -> None:
        captured: dict = {}

        async def fake_run_revit(rules_path, autonomy_path, findings_out, **kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(orchestrator, "run_revit", fake_run_revit)
        rc = await orchestrator.audit(
            _write_profile(tmp_path, mode="run_revit"),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=5,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0
        assert captured["limit"] == 7            # profile max_issues
        assert captured["max_elements"] == 42     # profile max_elements
        assert captured["max_iterations"] == 5
        assert captured["geometry_seed"] == [_axes_finding()]
        assert callable(captured["on_folder"])
        # SPEC_SCHEDULED_AUDIT_DELTA.md W1b/W2c: default profile → propose_only
        # forwarded as False, and a cross-run issue registry path is always
        # threaded (audit() is the only caller that sets one).
        assert captured["propose_only"] is False
        assert captured["issue_registry"] is not None

    async def test_run_revit_mode_forwards_propose_only_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes
    ) -> None:
        """A profile with ``run.propose_only: true`` must forward that flag
        verbatim to run_revit → DesignAgent (SPEC_SCHEDULED_AUDIT_DELTA.md W1b)."""
        captured: dict = {}

        async def fake_run_revit(rules_path, autonomy_path, findings_out, **kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(orchestrator, "run_revit", fake_run_revit)
        rc = await orchestrator.audit(
            _write_profile(tmp_path, mode="run_revit", propose_only=True),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=5,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0
        assert captured["propose_only"] is True

    async def test_demo_mode_dispatches_run_revit_with_mock_clients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes
    ) -> None:
        """AU demo package: mode "demo" rides the SAME run_revit orchestration
        but with the Demo Villa mock client factories — zero network."""
        captured: dict = {}

        async def fake_run_revit(rules_path, autonomy_path, findings_out, **kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(orchestrator, "run_revit", fake_run_revit)
        rc = await orchestrator.audit(
            _write_profile(tmp_path, mode="demo"),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=3,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0
        from bim_orchestrator.demo import DEMO_PROJECT_ID

        assert captured["revit_client_factory"] is not None
        assert captured["forma_client_factory"] is not None
        assert captured["project_id"] == DEMO_PROJECT_ID
        assert captured["published"] is False
        assert captured["limit"] == 7
        # Demo floor: >=4 iterations for route_node's fingerprint to settle
        # (documented on orchestrator.demo()).
        assert captured["max_iterations"] == 4
        assert captured["geometry_seed"] == [_axes_finding()]
        assert captured["propose_only"] is False
        assert captured["issue_registry"] is not None

    async def test_run_mode_dispatches_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes
    ) -> None:
        called = {"run": False}
        captured: dict = {}

        async def fake_run(*a, **kw):
            called["run"] = True
            captured.update(kw)
            return 0

        monkeypatch.setattr(orchestrator, "run", fake_run)
        rc = await orchestrator.audit(
            _write_profile(tmp_path, mode="run", propose_only=True),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=3,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 0 and called["run"]
        assert captured["propose_only"] is True
        assert captured["issue_registry"] is not None

    async def test_no_findings_seeds_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_axes
    ) -> None:
        stub_axes.findings = []
        captured: dict = {"geometry_seed": "sentinel"}

        async def fake_check(*a, **kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(orchestrator, "check", fake_check)
        await orchestrator.audit(
            _write_profile(tmp_path, mode="check"),
            Path("config/autonomy.yaml"),
            tmp_path / "findings.json",
            max_iterations=3,
            checkpoint_dir=tmp_path / "ckpt",
        )
        assert captured["geometry_seed"] is None

    async def test_invalid_profile_returns_2(self, tmp_path: Path, capsys) -> None:
        bad = tmp_path / "audit.bad.yaml"
        bad.write_text("name: bad\nrules: ['Z:/none.yaml']\n", encoding="utf-8")
        rc = await orchestrator.audit(
            bad, Path("config/autonomy.yaml"), tmp_path / "f.json",
            max_iterations=3, checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 2
        assert "rules file not found" in capsys.readouterr().err

    async def test_profile_without_rules_returns_2(
        self, tmp_path: Path, stub_axes, capsys
    ) -> None:
        rc = await orchestrator.audit(
            _write_profile(tmp_path, rules=False),
            Path("config/autonomy.yaml"), tmp_path / "f.json",
            max_iterations=3, checkpoint_dir=tmp_path / "ckpt",
        )
        assert rc == 2
        assert "no rules files" in capsys.readouterr().err


class TestAuditCLIFlag:
    def test_audit_is_mutually_exclusive_with_run_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["bim-orchestrator", "--audit", "x.yaml", "--check"]
        )
        with pytest.raises(SystemExit):
            orchestrator.main()

    def test_audit_flag_in_help(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr("sys.argv", ["bim-orchestrator", "--help"])
        with pytest.raises(SystemExit):
            orchestrator.main()
        assert "--audit" in capsys.readouterr().out


class TestAxesReportSection:
    def _payload(self) -> dict:
        return {
            "lod": {
                "required_lod": 300,
                "summary": {"total": 5, "passed": 3, "failed": 1, "undecided": 1},
                "results": [
                    {"guid": "g1", "tag": "447121", "name": "Door A",
                     "ifc_type": "IfcDoor", "detected_lod": 200, "passed": False},
                    {"guid": "g2", "tag": "447122", "name": "Door B",
                     "ifc_type": "IfcDoor", "detected_lod": None, "passed": None},
                ],
            },
            "spatial": {
                "summary": {"total": 2, "pass": 1, "fail": 1},
                "verdicts": [
                    {"guid": "s1", "name": "Corridor 1", "long_name": "L1 Corridor",
                     "rule": "width", "measured_m": 1.05, "required_m": 1.2,
                     "margin_m": -0.15, "status": "FAIL"},
                ],
            },
            "summary": {"skipped": ["spatial: room X errored — no geometry"]},
        }

    def test_load_axes_payload_roundtrip(self, tmp_path: Path) -> None:
        axes_dir = tmp_path / "axes"
        axes_dir.mkdir()
        payload = self._payload()
        (axes_dir / "lod.json").write_text(json.dumps(payload["lod"]), encoding="utf-8")
        (axes_dir / "spatial.json").write_text(
            json.dumps(payload["spatial"]), encoding="utf-8"
        )
        (axes_dir / "axes_summary.json").write_text(
            json.dumps(payload["summary"]), encoding="utf-8"
        )
        loaded = load_axes_payload(tmp_path)
        assert loaded is not None
        assert loaded["lod"]["summary"]["failed"] == 1
        assert loaded["spatial"]["summary"]["fail"] == 1

    def test_load_axes_payload_none_when_absent(self, tmp_path: Path) -> None:
        assert load_axes_payload(tmp_path) is None

    def test_render_axes_section_content(self) -> None:
        text = "\n".join(render_axes_section(self._payload()))
        assert "## Audit axes" in text
        assert "| 5 | 3 | 1 | 1 |" in text          # LOD summary row
        assert "447121" in text and "undecided" in text
        assert "| 2 | 1 | 1 |" in text               # spatial summary row
        assert "L1 Corridor" in text
        assert "lod_failures.bcfzip" in text
        assert "room X errored" in text               # skipped rendered

    def test_render_axes_section_empty_when_none(self) -> None:
        assert render_axes_section(None) == []

    def test_per_run_report_includes_section(self) -> None:
        md = render_per_run_report({}, run_id="run-x", axes=self._payload())
        assert "## Audit axes" in md
        assert md.index("## Audit axes") < md.index("## Audit trail")

    def test_verification_report_includes_section_and_toc(self) -> None:
        md = render_audit_report({}, run_id="run-x", axes=self._payload())
        assert "## 3c. Audit axes (IFC satellites)" in md
        assert "[Audit axes (IFC satellites)](#3c-audit-axes-ifc-satellites)" in md

    def test_verification_report_without_axes_unchanged_shape(self) -> None:
        md = render_audit_report({}, run_id="run-x")
        assert "3c. Audit axes" not in md


@pytest.mark.live_axes
@pytest.mark.skipif(
    os.environ.get("BIM_LIVE_AXES") != "1",
    reason="live satellite integration — set BIM_LIVE_AXES=1 with real venvs "
    "in config/audit_services.yaml (P3-1 acceptance #4, run by hand)",
)
class TestLiveAxes:
    async def test_spawn_real_satellites(self, tmp_path: Path) -> None:
        from bim_orchestrator.audit_axes import run_audit_axes
        from bim_orchestrator.policies.audit_profile import (
            AuditProfile,
            load_audit_services,
        )

        services = load_audit_services()
        ifc = os.environ.get("BIM_LIVE_AXES_IFC")
        assert ifc and Path(ifc).exists(), "set BIM_LIVE_AXES_IFC to a real IFC"
        profile = AuditProfile(
            name="live",
            axes={
                "lod": {"enabled": True, "ifc_path": ifc, "required_lod": 300},
                "spatial": {"enabled": True, "ifc_path": ifc},
            },
        )
        result = await run_audit_axes(profile, services, tmp_path / "axes")
        # Contract check, not outcome check: envelopes persisted + no crash.
        assert (tmp_path / "axes" / "axes_summary.json").exists()
        assert result.skipped == [] or all(":" in s for s in result.skipped)


class TestFetchDocumentIdentity:
    """SPEC_DOCUMENT_IDENTITY_STAMP: orchestrator._fetch_document_identity —
    best-effort capture of the open Revit document's identity, never fatal."""

    async def test_default_mock_client_normalizes_to_snake_case(self) -> None:
        from tests._mocks import MockRevitMCPClient

        client = MockRevitMCPClient()
        result = await orchestrator._fetch_document_identity(client)
        assert result is not None
        assert result["title"] == "MockDocument"
        assert result["path"] == "C:\\mock\\Mock.rvt"  # mapped from pathName
        assert result["revit_version_number"] == "2026"
        assert result["revit_version_name"] == "Autodesk Revit 2026"
        assert result["is_workshared"] is None  # missing key -> None, no crash
        assert "fetched_at" in result

    async def test_fetch_error_returns_none(self) -> None:
        from tests._mocks import MockRevitMCPClient

        client = MockRevitMCPClient(fail_on={"revit_get_document_info"})
        result = await orchestrator._fetch_document_identity(client)
        assert result is None

    async def test_empty_title_returns_none(self) -> None:
        """Home screen (no document open) -> addin returns an empty title;
        the helper must not stamp an empty identity."""
        from tests._mocks import MockRevitMCPClient

        client = MockRevitMCPClient(document_info={"title": ""})
        result = await orchestrator._fetch_document_identity(client)
        assert result is None

    async def test_every_wire_key_maps_to_its_snake_case_field(self) -> None:
        """L-06 review, L-12: the only assertion on the camelCase mapping was
        `is_workshared is None` when the mock OMITTED the key — which passes
        just as well if the code reads the WRONG key name (a typo also yields
        None). Four of the six document fields had never been asserted with
        the key present, so the whole provenance block could have been
        silently null in every run.

        `isWorkshared`/`isModified` are deliberately False here: they are the
        two fields that must NOT pick up an `or None` (False is a real
        answer), and a test seeded with True would not notice if one did.
        """
        from tests._mocks import MockRevitMCPClient

        client = MockRevitMCPClient(document_info={
            "title": "Tower.rvt",
            "pathName": r"C:\models\Tower.rvt",
            "isWorkshared": False,
            "isModified": False,
            "projectName": "Snowdon Towers",
            "projectNumber": "ST-2026",
        })
        result = await orchestrator._fetch_document_identity(client)

        assert result is not None
        assert result["title"] == "Tower.rvt"
        assert result["path"] == r"C:\models\Tower.rvt"
        assert result["is_workshared"] is False
        assert result["is_modified"] is False
        assert result["project_name"] == "Snowdon Towers"
        assert result["project_number"] == "ST-2026"
