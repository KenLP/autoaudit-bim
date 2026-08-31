"""Audit profile + satellite-services schema (Phase 3, P3-1).

An *audit profile* (``config/audit.<name>.yaml``) is ONE file = ONE audit
definition: the LOI rules files (same semantics as ``--rules``), the two
IFC-based axes (LOD via lod-validator, Spatial via spatial-qc), the run
options, and the unattended toggle. ``bim-orchestrator --audit <profile>``
consumes it.

``config/audit_services.yaml`` holds the MACHINE-LOCAL paths to the two
satellite venvs (each runs its own FastMCP stdio server on Python 3.10 —
see SPEC_PHASE3_AUDIT_APP.md D3) plus RevitControl. The real file is
gitignored; ``audit_services.yaml.example`` is committed. A missing file
or entry does NOT crash: the corresponding axis is simply *unavailable*
and the run degrades honestly ("skipped: unconfigured").

Layering: this is a ``policies/`` module — pure schema + YAML load, no
network/subprocess I/O (same posture as ``lookup_table`` / ``reference``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, Field, ValidationError

log = structlog.get_logger(__name__)

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

# "demo" (AU demo package, 2026-07-12): the audit dispatch delegates to the
# SAME run_revit() path with the mock Demo Villa client factories that
# `--demo` uses — zero network, deterministic. Added so the UI's Run drawer
# can start a stage-safe audit through POST /audits (an amendment to 3b B3,
# recorded in SPEC_AU_DEMO_PACKAGE.md).
AuditRunMode = Literal["check", "run", "run_revit", "demo"]


class LODAxis(BaseModel):
    """LOD/LOG geometry axis — lod-validator over an exported IFC (D4/D10)."""

    enabled: bool = False
    ifc_path: str | None = None
    # Model-wide required level (D10) — per-element BEP matrix is the
    # satellite repo's own deferred work.
    required_lod: int = Field(default=300, ge=100, le=500)
    # None = all signature classes the validator knows.
    classes: list[str] | None = None


class SpatialAxis(BaseModel):
    """Spatial QC axis — spatial-qc ``check_building`` over an IFC (D9)."""

    enabled: bool = False
    ifc_path: str | None = None
    # Comma-separated subset understood by spatial-qc: width,headroom,turning,door
    rules: str = "width,headroom,turning,door"
    # The ONLY threshold override the MCP surface accepts today (D9);
    # per-space-type config waits on the spatial-qc config_path handoff.
    required_width_m: float | None = None
    subtract_furniture: bool = False
    doors_egress_only: bool = False


class AuditAxes(BaseModel):
    lod: LODAxis = Field(default_factory=LODAxis)
    spatial: SpatialAxis = Field(default_factory=SpatialAxis)


class AuditRunOptions(BaseModel):
    """Maps 1:1 onto the existing run modes / CLI caps."""

    mode: AuditRunMode = "run"
    max_elements: int = Field(default=300, ge=1)
    max_issues: int = Field(default=10, ge=0)
    dry_run: bool = False
    # Scheduled/continuous audit: demote every would-be-auto Path B write to
    # an approve-gated proposal. ACC issue creation (Path A) is unaffected —
    # creating the issue IS the propose act. No effect on mode "check".
    propose_only: bool = False
    # Owner decision 2026-07-25 — opt-IN strictness for partial coverage.
    # A partial run (some target categories resolved, others dropped) IS a real
    # audit, so the default stays exit 0: flipping that default would turn every
    # existing scheduled run red on day one and just teach everyone to pass the
    # override. An unattended production audit wants the opposite — a silently
    # narrowed scope must page someone — so set this on the scheduled profile
    # and a dropped category becomes a non-zero exit. The report carries the
    # PARTIAL COVERAGE banner either way; this only changes what MACHINES see.
    fail_on_partial_coverage: bool = False


class UnattendedConfig(BaseModel):
    """P3-3 — RevitControl watchdog session around the run. Off by default."""

    enabled: bool = False
    revit_exe: str | None = None
    model_path: str | None = None
    revit_version: int | None = None


class AuditProfile(BaseModel):
    name: str
    rules: list[str] = Field(default_factory=list)
    axes: AuditAxes = Field(default_factory=AuditAxes)
    run: AuditRunOptions = Field(default_factory=AuditRunOptions)
    unattended: UnattendedConfig = Field(default_factory=UnattendedConfig)


def _resolve_relative(raw_path: str, base: Path) -> str:
    """Resolve a profile-declared path: absolute → as-is; relative → tried
    against the PROFILE's directory first (self-contained profiles behave the
    same from any cwd), falling back to cwd-relative (the `--rules
    config/...` CLI convention)."""
    p = Path(raw_path)
    if p.is_absolute():
        return raw_path
    profile_relative = base / p
    if profile_relative.exists():
        return str(profile_relative)
    return raw_path


def load_audit_profile(path: str | Path) -> AuditProfile:
    """Load + validate an audit profile. Fails BEFORE the run, not mid-run.

    Raises ``ValueError`` with an explicit list of every problem found:
    schema violations, rules files that don't exist, and — for each ENABLED
    axis — a missing/absent ``ifc_path`` (the user exports the IFC by hand
    at P3, D4; a dangling path would otherwise surface as a satellite error
    halfway through the audit). Relative paths are resolved profile-dir-first
    (see ``_resolve_relative``) and the RESOLVED paths are stored back on the
    returned profile, so downstream consumers never re-resolve.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"audit profile not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        profile = AuditProfile(**raw)
    except ValidationError as exc:
        raise ValueError(f"audit profile invalid ({p}): {exc}") from exc

    base = p.parent
    profile.rules = [_resolve_relative(r, base) for r in profile.rules]
    for axis in (profile.axes.lod, profile.axes.spatial):
        if axis.ifc_path:
            axis.ifc_path = _resolve_relative(axis.ifc_path, base)

    problems: list[str] = []
    for rules_file in profile.rules:
        if not Path(rules_file).exists():
            problems.append(f"rules file not found: {rules_file}")
    for axis_name, axis in (("lod", profile.axes.lod), ("spatial", profile.axes.spatial)):
        if not axis.enabled:
            continue
        if not axis.ifc_path:
            problems.append(f"axes.{axis_name}.enabled but ifc_path is empty")
        elif not Path(axis.ifc_path).exists():
            problems.append(f"axes.{axis_name}.ifc_path not found: {axis.ifc_path}")
    if profile.unattended.enabled:
        for key in ("revit_exe", "model_path", "revit_version"):
            if not getattr(profile.unattended, key):
                problems.append(f"unattended.enabled but {key} is empty")
    if problems:
        raise ValueError(
            f"audit profile {p} has {len(problems)} problem(s):\n  - "
            + "\n  - ".join(problems)
        )
    return profile


# ---------------------------------------------------------------------------
# Satellite services (machine-local paths — gitignored real file)
# ---------------------------------------------------------------------------


class ServiceVenv(BaseModel):
    """A satellite tool = its own venv's python.exe + repo cwd (D3)."""

    python: str
    cwd: str

    def exists(self) -> bool:
        return Path(self.python).exists() and Path(self.cwd).exists()


class RevitControlService(BaseModel):
    dir: str
    python: str

    def exists(self) -> bool:
        return Path(self.dir).exists() and Path(self.python).exists()


class AuditServices(BaseModel):
    """Parsed ``audit_services.yaml``. Any entry may be absent → that axis
    is *unavailable* (callers degrade to a skipped axis, never crash)."""

    lod_validator: ServiceVenv | None = None
    spatial_qc: ServiceVenv | None = None
    revitcontrol: RevitControlService | None = None

    def available(self, axis: Literal["lod", "spatial"]) -> bool:
        entry = self.lod_validator if axis == "lod" else self.spatial_qc
        return entry is not None and entry.exists()


def load_audit_services(
    path: str | Path | None = None,
) -> AuditServices:
    """Load ``config/audit_services.yaml``; missing file → empty services.

    A malformed ENTRY drops that entry with a warning (the other axes keep
    working); a malformed FILE returns empty services with a warning —
    "unavailable" is always the degrade, never an exception, because the
    LOI axis must still run on a machine with no satellites installed.
    """
    p = Path(path) if path is not None else _DEFAULT_CONFIG_DIR / "audit_services.yaml"
    if not p.exists():
        log.info("audit_services.missing", path=str(p))
        return AuditServices()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("audit_services.unparseable", path=str(p), error=str(exc))
        return AuditServices()
    if not isinstance(raw, dict):
        log.warning("audit_services.not_a_mapping", path=str(p))
        return AuditServices()

    kwargs: dict[str, object] = {}
    for key, model in (
        ("lod_validator", ServiceVenv),
        ("spatial_qc", ServiceVenv),
        ("revitcontrol", RevitControlService),
    ):
        entry = raw.get(key)
        if entry is None:
            continue
        try:
            kwargs[key] = model(**entry)
        except (ValidationError, TypeError) as exc:
            log.warning("audit_services.entry_invalid", entry=key, error=str(exc))
    return AuditServices(**kwargs)  # type: ignore[arg-type]
