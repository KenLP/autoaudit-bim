"""MCP client for the spatial-qc satellite (Phase 3, P3-1, D3/D9).

Spawns ``python -m spatial_qc.server`` (FastMCP, stdio) in the satellite's
own Python 3.10 venv (heavy deps: scipy/skimage/shapely/rtree/ifcopenshell).
Same posture as ``lod_validator.py`` — never import the library.

Envelope contract (server.py of that repo): ``check_building`` →
``{"summary": {"total","pass","fail"}, "verdicts": [{"guid", "name",
"long_name", "function", "level", "rule", "metric", "required_m",
"measured_m", "margin_m", "status": "PASS|FAIL|INFO|ERROR", "message",
"viz", "location": [x, y], "profile": {}}]}`` — ``guid`` is the
IfcSpace/IfcDoor GlobalId, ``viz`` a local PNG path on this machine.

Per-space-type thresholds are NOT supported at P3 (D9): ``check_building``
takes only ``required_width_m`` as override until the spatial-qc
``config_path`` handoff lands.
"""

from __future__ import annotations

from typing import Any

from bim_orchestrator.mcp_clients.lod_validator import (  # shared plumbing
    AuditAxisError,
    _StdioVenvClient,
)

__all__ = ["SpatialQCClient", "make_spatial_client", "AuditAxisError"]


class SpatialQCClient(_StdioVenvClient):
    axis = "spatial"
    server_module = "spatial_qc.server"

    async def check_building(
        self,
        ifc_path: str,
        required_width_m: float | None = None,
        rules: str | None = None,
        subtract_furniture: bool = False,
        doors_egress_only: bool = False,
    ) -> dict[str, Any]:
        """Full envelope: {summary, verdicts[...]}."""
        args: dict[str, Any] = {
            "ifc_path": ifc_path,
            "subtract_furniture": subtract_furniture,
            "doors_egress_only": doors_egress_only,
        }
        if required_width_m is not None:
            args["required_width_m"] = required_width_m
        if rules is not None:
            args["rules"] = rules
        return await self._call("check_building", args)

    async def emit_bcf(
        self,
        ifc_path: str,
        out_path: str,
        required_width_m: float = 1.10,
    ) -> dict[str, Any]:
        """→ {bcf, topics, fails} (.bcfzip written at out_path)."""
        return await self._call(
            "emit_bcf",
            {
                "ifc_path": ifc_path,
                "out_path": out_path,
                "required_width_m": required_width_m,
            },
        )


def make_spatial_client(services: Any) -> SpatialQCClient | None:
    """None when unconfigured/unavailable — the axis degrades to 'skipped'."""
    entry = getattr(services, "spatial_qc", None)
    if entry is None or not entry.exists():
        return None
    return SpatialQCClient(python_exe=entry.python, cwd=entry.cwd)
