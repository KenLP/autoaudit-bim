"""P3-1 — audit profile + audit services loaders (policies/audit_profile.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bim_orchestrator.policies.audit_profile import (
    AuditProfile,
    AuditServices,
    load_audit_profile,
    load_audit_services,
)


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def _valid_profile_yaml(tmp_path: Path, *, lod_enabled: bool = True) -> Path:
    ifc = _write(tmp_path / "model.ifc", "ISO-10303-21;")
    rules = _write(tmp_path / "rules.demo.yaml", "scenario: demo\nrules: []\n")
    return _write(
        tmp_path / "audit.demo.yaml",
        f"""
name: demo
rules:
  - {rules.as_posix()}
axes:
  lod:
    enabled: {str(lod_enabled).lower()}
    ifc_path: "{ifc.as_posix()}"
    required_lod: 300
  spatial:
    enabled: false
run:
  mode: check
  max_elements: 50
  max_issues: 5
  dry_run: true
""",
    )


class TestLoadAuditProfile:
    def test_valid_profile_loads(self, tmp_path: Path) -> None:
        profile = load_audit_profile(_valid_profile_yaml(tmp_path))
        assert isinstance(profile, AuditProfile)
        assert profile.name == "demo"
        assert profile.axes.lod.enabled is True
        assert profile.axes.lod.required_lod == 300
        assert profile.axes.spatial.enabled is False
        assert profile.run.mode == "check"
        assert profile.run.max_elements == 50
        assert profile.run.dry_run is True
        assert profile.unattended.enabled is False

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_audit_profile(tmp_path / "nope.yaml")

    def test_enabled_axis_with_missing_ifc_raises_listing_path(self, tmp_path: Path) -> None:
        rules = _write(tmp_path / "rules.demo.yaml", "scenario: demo\nrules: []\n")
        p = _write(
            tmp_path / "audit.bad.yaml",
            f"""
name: bad
rules: [{rules.as_posix()}]
axes:
  lod:
    enabled: true
    ifc_path: "{(tmp_path / 'missing.ifc').as_posix()}"
""",
        )
        with pytest.raises(ValueError) as exc:
            load_audit_profile(p)
        assert "missing.ifc" in str(exc.value)
        assert "axes.lod" in str(exc.value)

    def test_disabled_axis_ignores_missing_ifc(self, tmp_path: Path) -> None:
        rules = _write(tmp_path / "rules.demo.yaml", "scenario: demo\nrules: []\n")
        p = _write(
            tmp_path / "audit.ok.yaml",
            f"""
name: ok
rules: [{rules.as_posix()}]
axes:
  lod:
    enabled: false
    ifc_path: "Z:/definitely/not/there.ifc"
""",
        )
        profile = load_audit_profile(p)
        assert profile.axes.lod.enabled is False

    def test_enabled_axis_with_empty_ifc_path_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "audit.bad.yaml",
            "name: bad\naxes:\n  spatial:\n    enabled: true\n",
        )
        with pytest.raises(ValueError, match="axes.spatial"):
            load_audit_profile(p)

    def test_missing_rules_file_raises(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "audit.bad.yaml",
            "name: bad\nrules: ['Z:/no/rules.yaml']\n",
        )
        with pytest.raises(ValueError, match="rules file not found"):
            load_audit_profile(p)

    def test_required_lod_out_of_range_raises(self, tmp_path: Path) -> None:
        ifc = _write(tmp_path / "m.ifc", "x")
        p = _write(
            tmp_path / "audit.bad.yaml",
            f"name: bad\naxes:\n  lod:\n    enabled: true\n"
            f"    ifc_path: '{ifc.as_posix()}'\n    required_lod: 950\n",
        )
        with pytest.raises(ValueError, match="invalid"):
            load_audit_profile(p)

    def test_unattended_enabled_requires_fields(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "audit.bad.yaml",
            "name: bad\nunattended:\n  enabled: true\n",
        )
        with pytest.raises(ValueError, match="unattended"):
            load_audit_profile(p)

    def test_committed_demo_profile_loads(self) -> None:
        # The profile committed in config/ must always load (axes ship
        # DISABLED so the dangling demo ifc_path is never validated).
        repo_profile = (
            Path(__file__).resolve().parents[1] / "config" / "audit.demo.yaml"
        )
        profile = load_audit_profile(repo_profile)
        assert profile.name == "demo"
        assert profile.axes.lod.enabled is False
        assert profile.axes.spatial.enabled is False

    def test_committed_au_demo_profile_loads(self) -> None:
        # AU 2026 pilot-preview profile (SPEC_AU_DEMO_PACKAGE.md item 1) —
        # must always load cleanly on any machine (axes disabled, rules file
        # resolves profile-dir-first to config/rules.demo.yaml).
        repo_profile = (
            Path(__file__).resolve().parents[1] / "config" / "audit.au_demo.yaml"
        )
        profile = load_audit_profile(repo_profile)
        assert profile.name == "au_demo"
        assert profile.axes.lod.enabled is False
        assert profile.axes.spatial.enabled is False
        assert profile.rules and profile.rules[0].endswith("rules.demo.yaml")


class TestLoadAuditServices:
    def test_missing_file_returns_empty_services(self, tmp_path: Path) -> None:
        services = load_audit_services(tmp_path / "audit_services.yaml")
        assert isinstance(services, AuditServices)
        assert services.lod_validator is None
        assert services.available("lod") is False
        assert services.available("spatial") is False

    def test_entry_with_real_paths_is_available(self, tmp_path: Path) -> None:
        venv_py = _write(tmp_path / "python.exe", "")
        p = _write(
            tmp_path / "audit_services.yaml",
            f"lod_validator:\n  python: '{venv_py.as_posix()}'\n"
            f"  cwd: '{tmp_path.as_posix()}'\n",
        )
        services = load_audit_services(p)
        assert services.available("lod") is True
        assert services.available("spatial") is False

    def test_entry_with_dangling_python_is_unavailable(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path / "audit_services.yaml",
            f"spatial_qc:\n  python: 'Z:/nope/python.exe'\n"
            f"  cwd: '{tmp_path.as_posix()}'\n",
        )
        services = load_audit_services(p)
        assert services.spatial_qc is not None  # configured...
        assert services.available("spatial") is False  # ...but not usable

    def test_malformed_entry_dropped_others_kept(self, tmp_path: Path) -> None:
        venv_py = _write(tmp_path / "python.exe", "")
        p = _write(
            tmp_path / "audit_services.yaml",
            f"lod_validator:\n  python: '{venv_py.as_posix()}'\n"
            f"  cwd: '{tmp_path.as_posix()}'\n"
            "spatial_qc: 'just-a-string'\n",
        )
        services = load_audit_services(p)
        assert services.available("lod") is True
        assert services.spatial_qc is None

    def test_unparseable_file_returns_empty(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "audit_services.yaml", "lod_validator: [unclosed")
        services = load_audit_services(p)
        assert services.lod_validator is None

    def test_committed_example_parses(self) -> None:
        example = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "audit_services.yaml.example"
        )
        services = load_audit_services(example)
        # Entries parse; availability depends on the machine — just assert shape.
        assert services.lod_validator is not None
        assert services.spatial_qc is not None
        assert services.revitcontrol is not None
