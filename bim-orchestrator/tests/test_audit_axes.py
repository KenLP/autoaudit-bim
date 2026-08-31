"""P3-1 — audit axes orchestration + verdict→Finding mapping (audit_axes.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bim_orchestrator.audit_axes import (
    AxesResult,
    map_lod_results,
    map_spatial_verdicts,
    persist_axes_dir,
    run_audit_axes,
)
from bim_orchestrator.policies.audit_profile import AuditProfile, AuditServices

from tests._mocks import MockLODValidatorClient, MockSpatialQCClient


def _profile(tmp_path: Path, *, lod: bool = True, spatial: bool = True) -> AuditProfile:
    ifc = tmp_path / "model.ifc"
    ifc.write_text("ISO-10303-21;")
    return AuditProfile(
        name="t",
        rules=[],
        axes={
            "lod": {"enabled": lod, "ifc_path": str(ifc), "required_lod": 300},
            "spatial": {
                "enabled": spatial,
                "ifc_path": str(ifc),
                "required_width_m": 1.2,
            },
        },
    )


def _lod_result(**over) -> dict:
    base = {
        "guid": "2O2Fr$t4X7Zf8NOew3FLKI",
        "ifc_type": "IfcDoor",
        "name": "M_Single-Flush 0915",
        "tag": "447121",
        "required_lod": 300,
        "detected_lod": 200,
        "passed": False,
        "missing": ["swing geometry", "frame profile"],
        "satisfied": [],
        "evidence": {},
        "confidence": 0.9,
        "notes": [],
    }
    base.update(over)
    return base


def _spatial_verdict(**over) -> dict:
    base = {
        "guid": "1a2b3c4d5e6f7g8h9i0jkl",
        "name": "Corridor 1",
        "long_name": "Level 1 Corridor",
        "function": "corridor",
        "level": "Level 1",
        "rule": "width",
        "metric": "clear_width_m",
        "required_m": 1.2,
        "measured_m": 1.05,
        "margin_m": -0.15,
        "status": "FAIL",
        "message": "Corridor narrower than required",
        "viz": None,
        "location": [12.5, 3.2],
        "profile": {},
    }
    base.update(over)
    return base


class TestMapLOD:
    def test_fail_maps_to_non_compliant_high(self) -> None:
        env = {"results": [_lod_result()]}
        (f,) = map_lod_results(env, 300)
        assert f["rule_id"] == "lod_min_300"
        assert f["element_id"] == "447121"  # tag = Revit ElementId
        assert f["status"] == "non_compliant"
        assert f["severity"] == "severity_high"
        assert f["severity_tag"] == "lod_violation"
        assert f["parameter"] == "LOD"
        assert "detected LOD 200" in f["message"]
        assert "swing geometry" in f["message"]
        assert f["ifc_guid"] == "2O2Fr$t4X7Zf8NOew3FLKI"
        assert f["element_name"] == "M_Single-Flush 0915"

    def test_undecided_maps_to_manual_review_low(self) -> None:
        env = {"results": [_lod_result(passed=None, detected_lod=None)]}
        (f,) = map_lod_results(env, 300)
        assert f["status"] == "manual_review"
        assert f["severity"] == "severity_low"
        assert "undecided" in f["message"]

    def test_passed_produces_no_finding(self) -> None:
        env = {"results": [_lod_result(passed=True)]}
        assert map_lod_results(env, 300) == []

    def test_missing_tag_falls_back_to_guid(self) -> None:
        env = {"results": [_lod_result(tag="")]}
        (f,) = map_lod_results(env, 300)
        assert f["element_id"] == "2O2Fr$t4X7Zf8NOew3FLKI"


class TestMapSpatial:
    def test_fail_below_10cm_margin_is_high(self) -> None:
        findings, skipped = map_spatial_verdicts({"verdicts": [_spatial_verdict()]})
        assert skipped == []
        (f,) = findings
        assert f["rule_id"] == "spatial_width"
        assert f["element_id"] == "1a2b3c4d5e6f7g8h9i0jkl"
        assert f["severity"] == "severity_high"  # margin -0.15 <= -0.1
        assert f["severity_tag"] == "spatial_violation"
        assert f["parameter"] == "clear_width_m"
        assert "measured 1.05 m" in f["message"]
        assert "[12.5, 3.2]" in f["message"]

    def test_fail_small_margin_is_medium(self) -> None:
        findings, _ = map_spatial_verdicts(
            {"verdicts": [_spatial_verdict(margin_m=-0.02, measured_m=1.18)]}
        )
        assert findings[0]["severity"] == "severity_medium"

    def test_error_becomes_skipped_not_finding(self) -> None:
        findings, skipped = map_spatial_verdicts(
            {"verdicts": [_spatial_verdict(status="ERROR", message="no geometry")]}
        )
        assert findings == []
        assert len(skipped) == 1
        assert "no geometry" in skipped[0]

    def test_pass_and_info_ignored(self) -> None:
        findings, skipped = map_spatial_verdicts(
            {"verdicts": [
                _spatial_verdict(status="PASS"),
                _spatial_verdict(status="INFO"),
            ]}
        )
        assert findings == [] and skipped == []


class TestRunAuditAxes:
    @pytest.mark.asyncio
    async def test_both_axes_run_and_write_artifacts(self, tmp_path: Path) -> None:
        viz_src = tmp_path / "viz_src.png"
        viz_src.write_bytes(b"\x89PNG mock")
        lod = MockLODValidatorClient(envelope={
            "schema": "lod-validator/phase0",
            "required_lod": 300,
            "summary": {"total": 2, "passed": 1, "failed": 1, "undecided": 0,
                        "detected_lod_distribution": {}},
            "results": [_lod_result(), _lod_result(passed=True, tag="9")],
        })
        spatial = MockSpatialQCClient(envelope={
            "summary": {"total": 1, "pass": 0, "fail": 1},
            "verdicts": [_spatial_verdict(viz=str(viz_src))],
        })
        axes_dir = tmp_path / "axes"
        result = await run_audit_axes(
            _profile(tmp_path), AuditServices(), axes_dir,
            lod_client=lod, spatial_client=spatial,
        )
        assert isinstance(result, AxesResult)
        assert len(result.findings) == 2  # 1 lod fail + 1 spatial fail
        assert result.skipped == []
        # Raw envelopes persisted verbatim
        lod_json = json.loads((axes_dir / "lod.json").read_text(encoding="utf-8"))
        assert lod_json["summary"]["failed"] == 1
        assert (axes_dir / "spatial.json").exists()
        # BCFs written by the (mock) satellites + viz copied
        assert (axes_dir / "lod_failures.bcfzip").exists()
        assert (axes_dir / "spatial_failures.bcfzip").exists()
        assert (axes_dir / "viz" / "viz_src.png").exists()
        summary = json.loads(
            (axes_dir / "axes_summary.json").read_text(encoding="utf-8")
        )
        assert summary["findings"] == 2
        # Mock got the profile's knobs verbatim
        assert lod.calls[0]["required_lod"] == 300
        assert spatial.calls[0]["required_width_m"] == 1.2

    @pytest.mark.asyncio
    async def test_unconfigured_axes_skip_honestly(self, tmp_path: Path) -> None:
        result = await run_audit_axes(
            _profile(tmp_path), AuditServices(), tmp_path / "axes",
        )  # no clients injected + empty services → both unconfigured
        assert result.findings == []
        assert sorted(result.skipped) == [
            "lod: unconfigured (audit_services.yaml)",
            "spatial: unconfigured (audit_services.yaml)",
        ]

    @pytest.mark.asyncio
    async def test_disabled_axes_do_not_even_appear(self, tmp_path: Path) -> None:
        result = await run_audit_axes(
            _profile(tmp_path, lod=False, spatial=False),
            AuditServices(), tmp_path / "axes",
        )
        assert result.findings == [] and result.skipped == []

    @pytest.mark.asyncio
    async def test_axis_spawn_failure_degrades_other_axis_survives(
        self, tmp_path: Path
    ) -> None:
        lod = MockLODValidatorClient(fail_on_enter=True)
        spatial = MockSpatialQCClient(envelope={
            "summary": {"total": 1, "pass": 0, "fail": 1},
            "verdicts": [_spatial_verdict()],
        })
        result = await run_audit_axes(
            _profile(tmp_path), AuditServices(), tmp_path / "axes",
            lod_client=lod, spatial_client=spatial,
        )
        assert len(result.findings) == 1  # spatial still delivered
        assert any(s.startswith("lod: failed to start") for s in result.skipped)

    @pytest.mark.asyncio
    async def test_viz_copy_failure_does_not_crash(self, tmp_path: Path) -> None:
        spatial = MockSpatialQCClient(envelope={
            "summary": {"total": 1, "pass": 0, "fail": 1},
            "verdicts": [_spatial_verdict(viz="Z:/definitely/not/there.png")],
        })
        result = await run_audit_axes(
            _profile(tmp_path, lod=False), AuditServices(), tmp_path / "axes",
            spatial_client=spatial,
        )
        assert len(result.findings) == 1
        assert not any(a.endswith(".png") for a in result.artifacts)


class TestPersistAxesDir:
    def test_copies_into_run_folder(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        (staging / "viz").mkdir(parents=True)
        (staging / "lod.json").write_text("{}", encoding="utf-8")
        (staging / "viz" / "a.png").write_bytes(b"x")
        run_root = tmp_path / "run-abc"
        run_root.mkdir()
        persist_axes_dir(staging, run_root)
        assert (run_root / "axes" / "lod.json").exists()
        assert (run_root / "axes" / "viz" / "a.png").exists()

    def test_missing_staging_is_noop(self, tmp_path: Path) -> None:
        persist_axes_dir(tmp_path / "nope", tmp_path / "run")
        assert not (tmp_path / "run").exists()
