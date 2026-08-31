"""One-shot audit axes orchestration (Phase 3, P3-1, D5).

Runs the two IFC-based satellite checks (LOD via lod-validator, Spatial via
spatial-qc) ONCE, BEFORE the LangGraph loop, and maps their verdicts into
``Finding`` dicts shaped exactly like GeometricQueryAgent's output — so they
ride the existing ``geometry_findings`` bucket (K7): DesignAgent folds them
into Path A on iteration 0 with per-(rule, status) grouping (K18), and the
reports' geometry section renders them. QC / route / DesignAgent are NOT
touched.

Lives at package top level (not ``policies/``) because it does real I/O:
spawns the satellite MCP subprocesses and writes artifacts (raw envelopes,
BCF zips, viz PNGs) into an ``axes/`` directory that the orchestrator later
persists into the run folder. Degrade posture: an unconfigured or failing
axis becomes an honest ``skipped`` entry ("lod: unconfigured" / the error
message) — the LOI axis must still run on a machine with no satellites.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from bim_orchestrator.mcp_clients.lod_validator import (
    AuditAxisError,
    make_lod_client,
)
from bim_orchestrator.mcp_clients.spatial_qc import make_spatial_client
from bim_orchestrator.policies.audit_profile import AuditProfile, AuditServices
from bim_orchestrator.state import Finding

log = structlog.get_logger(__name__)

# Below-threshold margin (in metres) at which a spatial FAIL escalates from
# medium to high severity — 10 cm short of a corridor width is a design
# problem, 1 cm may be a modelling artefact.
_SPATIAL_HIGH_MARGIN_M = -0.1


@dataclass
class AxesResult:
    findings: list[Finding] = field(default_factory=list)
    lod_raw: dict[str, Any] | None = None       # → axes/lod.json
    spatial_raw: dict[str, Any] | None = None   # → axes/spatial.json
    artifacts: list[str] = field(default_factory=list)  # bcf + viz copies
    skipped: list[str] = field(default_factory=list)    # honest degrade notes


def map_lod_results(envelope: dict[str, Any], required_lod: int) -> list[Finding]:
    """lod-validator results → Findings (same shape GeometricQueryAgent emits).

    ``passed is False`` → non_compliant / severity_high;
    ``passed is None`` (undecided) → manual_review / severity_low — appended
    (not silenced) so the report stays honest about what the tool could not
    decide. ``tag`` is the Revit ElementId → ``element_id`` (fallback to the
    IFC ``guid`` when the export carried no tag); the guid always rides along
    in the extra ``ifc_guid`` key.
    """
    findings: list[Finding] = []
    for r in envelope.get("results", []) or []:
        passed = r.get("passed")
        if passed is True:
            continue
        tag = str(r.get("tag") or "").strip()
        guid = str(r.get("guid") or "").strip()
        detected = r.get("detected_lod")
        missing = ", ".join(r.get("missing") or [])
        undecided = passed is None
        message = (
            f"detected LOD {detected} vs required {required_lod}"
            + (f"; missing: {missing}" if missing else "")
            + ("; validator undecided — verify by hand" if undecided else "")
        )
        f: Finding = {
            "rule_id": f"lod_min_{required_lod}",
            "element_id": tag or guid,
            "parameter": "LOD",
            "severity_tag": "lod_violation",
            "severity": "severity_low" if undecided else "severity_high",
            "message": message,
            "suggested_value": None,
            "citation": None,
            "status": "manual_review" if undecided else "non_compliant",
        }
        name = (r.get("name") or "").strip()
        if name:
            f["element_name"] = name
        f["ifc_guid"] = guid  # type: ignore[typeddict-unknown-key]
        f["category"] = r.get("ifc_type")  # type: ignore[typeddict-unknown-key]
        findings.append(f)
    return findings


def map_spatial_verdicts(envelope: dict[str, Any]) -> tuple[list[Finding], list[str]]:
    """spatial-qc verdicts → (Findings, skipped-notes).

    Only ``FAIL`` becomes a finding (severity by margin: more than 10 cm
    short → high, else medium). ``ERROR`` rows become skipped notes — a
    space the tool could not measure is a coverage gap, not a violation.
    ``PASS``/``INFO`` rows are neither.
    """
    findings: list[Finding] = []
    skipped: list[str] = []
    for v in envelope.get("verdicts", []) or []:
        status = v.get("status")
        if status == "ERROR":
            skipped.append(
                f"spatial: {v.get('rule')} on {v.get('guid')} errored — "
                f"{v.get('message')}"
            )
            continue
        if status != "FAIL":
            continue
        margin = v.get("margin_m")
        severe = margin is not None and float(margin) <= _SPATIAL_HIGH_MARGIN_M
        location = v.get("location")
        loc_txt = f" at {location}" if location else ""
        f: Finding = {
            "rule_id": f"spatial_{v.get('rule')}",
            "element_id": str(v.get("guid") or ""),
            "parameter": str(v.get("metric") or "spatial"),
            "severity_tag": "spatial_violation",
            "severity": "severity_high" if severe else "severity_medium",
            "message": (
                f"{v.get('message')} (measured {v.get('measured_m')} m, "
                f"required {v.get('required_m')} m{loc_txt})"
            ),
            "suggested_value": None,
            "citation": None,
            "status": "non_compliant",
        }
        name = (v.get("long_name") or v.get("name") or "").strip()
        if name:
            f["element_name"] = name
        f["category"] = v.get("function") or "space"  # type: ignore[typeddict-unknown-key]
        findings.append(f)
    return findings, skipped


def _copy_viz(envelope: dict[str, Any], viz_dir: Path) -> list[str]:
    """Copy spatial viz PNGs (temp paths on the satellite side) into the
    axes folder. Any copy failure is a warning, never a crash."""
    copied: list[str] = []
    for v in envelope.get("verdicts", []) or []:
        viz = v.get("viz")
        if not viz:
            continue
        src = Path(viz)
        try:
            if not src.exists():
                continue
            viz_dir.mkdir(parents=True, exist_ok=True)
            dst = viz_dir / src.name
            shutil.copyfile(src, dst)
            copied.append(str(dst))
        except OSError as exc:
            log.warning("audit_axes.viz_copy_failed", src=str(src), error=str(exc))
    return copied


async def run_audit_axes(
    profile: AuditProfile,
    services: AuditServices,
    axes_dir: Path,
    *,
    lod_client: Any | None = None,
    spatial_client: Any | None = None,
) -> AxesResult:
    """Run every ENABLED axis once; write raw envelopes + BCF + viz into
    ``axes_dir``; return mapped findings ready for the geometry bucket.

    ``lod_client`` / ``spatial_client`` are injectable test doubles (async
    context managers mirroring the real clients); production resolves them
    from ``services`` via the ``make_*_client`` factories.
    """
    axes_dir.mkdir(parents=True, exist_ok=True)
    result = AxesResult()

    # ── LOD axis ───────────────────────────────────────────────────────────
    lod_cfg = profile.axes.lod
    if lod_cfg.enabled:
        client = lod_client if lod_client is not None else make_lod_client(services)
        if client is None:
            result.skipped.append("lod: unconfigured (audit_services.yaml)")
        else:
            try:
                async with client as lod:
                    envelope = await lod.validate_lod(
                        ifc_path=lod_cfg.ifc_path,
                        required_lod=lod_cfg.required_lod,
                        classes=lod_cfg.classes,
                    )
                    result.lod_raw = envelope
                    (axes_dir / "lod.json").write_text(
                        json.dumps(envelope, default=str), encoding="utf-8"
                    )
                    result.findings.extend(
                        map_lod_results(envelope, lod_cfg.required_lod)
                    )
                    bcf_out = axes_dir / "lod_failures.bcfzip"
                    try:
                        await lod.emit_bcf(
                            ifc_path=lod_cfg.ifc_path,
                            required_lod=lod_cfg.required_lod,
                            out_path=str(bcf_out),
                            only_failures=True,
                        )
                        if bcf_out.exists():
                            result.artifacts.append(str(bcf_out))
                    except AuditAxisError as exc:
                        log.warning("audit_axes.lod_bcf_failed", error=str(exc))
            except AuditAxisError as exc:
                log.error("audit_axes.lod_failed", error=str(exc))
                result.skipped.append(f"lod: {exc.message}")

    # ── Spatial axis ───────────────────────────────────────────────────────
    spatial_cfg = profile.axes.spatial
    if spatial_cfg.enabled:
        client = (
            spatial_client if spatial_client is not None
            else make_spatial_client(services)
        )
        if client is None:
            result.skipped.append("spatial: unconfigured (audit_services.yaml)")
        else:
            try:
                async with client as spatial:
                    envelope = await spatial.check_building(
                        ifc_path=spatial_cfg.ifc_path,
                        required_width_m=spatial_cfg.required_width_m,
                        rules=spatial_cfg.rules,
                        subtract_furniture=spatial_cfg.subtract_furniture,
                        doors_egress_only=spatial_cfg.doors_egress_only,
                    )
                    result.spatial_raw = envelope
                    (axes_dir / "spatial.json").write_text(
                        json.dumps(envelope, default=str), encoding="utf-8"
                    )
                    findings, skipped = map_spatial_verdicts(envelope)
                    result.findings.extend(findings)
                    result.skipped.extend(skipped)
                    result.artifacts.extend(
                        _copy_viz(envelope, axes_dir / "viz")
                    )
                    bcf_out = axes_dir / "spatial_failures.bcfzip"
                    try:
                        await spatial.emit_bcf(
                            ifc_path=spatial_cfg.ifc_path,
                            out_path=str(bcf_out),
                            required_width_m=spatial_cfg.required_width_m or 1.10,
                        )
                        if bcf_out.exists():
                            result.artifacts.append(str(bcf_out))
                    except AuditAxisError as exc:
                        log.warning("audit_axes.spatial_bcf_failed", error=str(exc))
            except AuditAxisError as exc:
                log.error("audit_axes.spatial_failed", error=str(exc))
                result.skipped.append(f"spatial: {exc.message}")

    # Honest-degrade manifest — the report's axes section renders from this
    # (renders-never-re-derives; "no silent caps").
    (axes_dir / "axes_summary.json").write_text(
        json.dumps(
            {
                "findings": len(result.findings),
                "skipped": result.skipped,
                "artifacts": [Path(a).name for a in result.artifacts],
                "lod_enabled": lod_cfg.enabled,
                "spatial_enabled": spatial_cfg.enabled,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info(
        "audit_axes.done",
        findings=len(result.findings),
        skipped=len(result.skipped),
        artifacts=len(result.artifacts),
    )
    return result


def persist_axes_dir(staging: Path, run_root: Path) -> None:
    """Copy the staged ``axes/`` artifacts into the run folder (the run
    folder only exists once the run mode starts — the orchestrator calls
    this from the ``on_folder`` hook). Copy failure is a warning: losing a
    viz PNG must not fail the audit."""
    if not staging.exists():
        return
    dst = run_root / "axes"
    try:
        shutil.copytree(staging, dst, dirs_exist_ok=True)
    except OSError as exc:
        log.warning(
            "audit_axes.persist_failed", staging=str(staging), error=str(exc)
        )
