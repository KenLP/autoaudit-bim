"""Catalog verification — probe Revit + AECDM, report gaps.

Compares :class:`OSTCatalog` entries against what each backend actually
exposes in a live session:

  * **Revit MCP**: calls ``revit_list_categories`` which returns the
    BuiltInCategory strings present in the active document. Any catalog
    entry whose ``ost`` isn't in that set is flagged ``revit_absent`` —
    might just mean the model has no instances of that category, not
    that the OST is wrong.

  * **AECDM**: for each catalog entry, probes
    ``aecdm_query_elements(category=entry.aecdm_label)``:
      - entries with ``aecdm_label=None`` are tagged ``unknown`` (we
        never asked).
      - a successful (non-error) call → ``aecdm_present`` regardless of
        whether elements came back. AECDM rejects unknown category
        labels with an error, so a clean call confirms the label is
        accepted.
      - any exception → ``aecdm_absent`` (label not exposed by AECDM
        for this project, or rate-limit / transport error).

The verifier returns a structured :class:`CatalogVerifyReport` with both
the per-entry verdicts and a textual ``recommendations`` list that
points at the 13 AECDM-null entries the catalog still has — when an
entry's ``aecdm_present=True`` but the catalog has ``aecdm_label=None``,
the recommendation includes the resolved label so the user can fill it
in without re-running the probe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog

from bim_orchestrator.policies.ost_catalog import CatalogEntry, OSTCatalog

log = structlog.get_logger(__name__)


RevitVerdict = Literal["present", "absent", "skipped"]
AecdmVerdict = Literal["present", "absent", "null_in_catalog", "skipped"]


@dataclass(frozen=True)
class CatalogEntryVerdict:
    """Per-entry result of a verification probe."""

    key: str
    display: str
    ost: str
    aecdm_label: str | None
    discipline: str
    revit_verdict: RevitVerdict
    revit_count: int = 0
    aecdm_verdict: AecdmVerdict = "skipped"
    aecdm_error: str | None = None


@dataclass(frozen=True)
class CatalogVerifyReport:
    """Bundle of per-entry verdicts + actionable recommendations."""

    verdicts: list[CatalogEntryVerdict]
    revit_skipped: bool
    aecdm_skipped: bool
    recommendations: list[str] = field(default_factory=list)

    # Convenience filters for human-readable output / tests.

    def revit_present(self) -> list[CatalogEntryVerdict]:
        return [v for v in self.verdicts if v.revit_verdict == "present"]

    def revit_absent(self) -> list[CatalogEntryVerdict]:
        return [v for v in self.verdicts if v.revit_verdict == "absent"]

    def aecdm_fillable(self) -> list[CatalogEntryVerdict]:
        """Entries with ``aecdm_label=None`` but probe succeeded under a
        plausible alternative — these are the ones the user should fill."""
        return [
            v for v in self.verdicts
            if v.aecdm_verdict == "present" and v.aecdm_label is None
        ]

    def aecdm_dead(self) -> list[CatalogEntryVerdict]:
        """Entries the catalog claims AECDM exposes, but the probe failed.
        Could be a renamed AECDM label or a project that doesn't expose
        the category — worth a human look."""
        return [
            v for v in self.verdicts
            if v.aecdm_verdict == "absent" and v.aecdm_label is not None
        ]

    def render_markdown(self) -> str:
        """Human-readable markdown summary (lands in runs/catalog_verify.md)."""
        lines: list[str] = ["# OST Catalog verification report", ""]
        lines.append(f"- Total entries: **{len(self.verdicts)}**")
        if not self.revit_skipped:
            lines.append(
                f"- Revit MCP probe: **{len(self.revit_present())}** present "
                f"in the active document, **{len(self.revit_absent())}** absent."
            )
        else:
            lines.append("- Revit MCP probe: **skipped** (no Revit client passed).")
        if not self.aecdm_skipped:
            fillable = len(self.aecdm_fillable())
            dead = len(self.aecdm_dead())
            lines.append(
                f"- AECDM probe: **{fillable}** fillable null entries detected, "
                f"**{dead}** non-null entries failed."
            )
        else:
            lines.append("- AECDM probe: **skipped** (no Forma client passed).")
        lines.append("")
        if self.recommendations:
            lines.append("## Recommendations")
            for rec in self.recommendations:
                lines.append(f"- {rec}")
            lines.append("")
        lines.append("## Per-entry verdicts")
        lines.append("")
        lines.append(
            "| key | display | discipline | OST | revit | AECDM label | AECDM |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for v in self.verdicts:
            aecdm_lbl = v.aecdm_label if v.aecdm_label is not None else "_(null)_"
            r_cell = (
                f"{v.revit_verdict} ({v.revit_count})"
                if v.revit_verdict == "present"
                else v.revit_verdict
            )
            a_cell = v.aecdm_verdict
            if v.aecdm_error:
                a_cell = f"{a_cell} ({v.aecdm_error[:40]}…)"
            lines.append(
                f"| {v.key} | {v.display} | {v.discipline} | `{v.ost}` | "
                f"{r_cell} | `{aecdm_lbl}` | {a_cell} |"
            )
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Protocols — narrow interface so we can mock without dragging in the full
# MCP client surface.
# ---------------------------------------------------------------------------


class RevitProbe(Protocol):
    async def list_categories(self) -> list[dict[str, Any]]: ...


class FormaProbe(Protocol):
    async def query_elements(
        self, element_group_id: str, category: str
    ) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def verify_catalog(
    catalog: OSTCatalog,
    *,
    revit_mcp: RevitProbe | None = None,
    forma_mcp: FormaProbe | None = None,
    element_group_id: str | None = None,
    aecdm_concurrency: int = 4,
) -> CatalogVerifyReport:
    """Probe each catalog entry against the supplied backends.

    Either or both backends may be ``None``; that side gets ``skipped``
    verdicts and the report's ``recommendations`` reflect what was
    actually probed.

    Args:
        catalog: Loaded catalog to verify.
        revit_mcp: Anything that quacks like ``RevitMCPClient`` —
            specifically ``list_categories()``. Omit to skip Revit probe.
        forma_mcp: Anything that quacks like ``FormaMCPClient`` —
            specifically ``query_elements(eg_id, category)``. Pair with
            ``element_group_id``. Omit to skip AECDM probe.
        element_group_id: Required when ``forma_mcp`` is set.
        aecdm_concurrency: Bound for parallel AECDM probes. Default 4 —
            AECDM is a paid API, don't hammer it.
    """
    revit_present_ost: set[str] = set()
    revit_counts: dict[str, int] = {}
    revit_skipped = revit_mcp is None
    if revit_mcp is not None:
        try:
            cats = await revit_mcp.list_categories()
            for c in cats:
                # Revit MCP list_categories returns
                # {id, name, builtInCategory, instanceCount} per the addin
                # source (RevitAddin/Commands/ListCategoriesCommand.cs).
                # The legacy/test shape was {enum, count} — read both so
                # mocks that haven't migrated still work.
                ost = c.get("builtInCategory") or c.get("enum")
                count = c.get("instanceCount") or c.get("count") or 0
                if isinstance(ost, str):
                    revit_present_ost.add(ost)
                    revit_counts[ost] = int(count)
            log.info(
                "verify_catalog.revit_probed",
                present_count=len(revit_present_ost),
            )
        except Exception as exc:
            log.warning("verify_catalog.revit_probe_failed", error=str(exc))
            revit_skipped = True

    aecdm_skipped = forma_mcp is None or not element_group_id
    aecdm_results: dict[str, tuple[AecdmVerdict, str | None]] = {}
    if not aecdm_skipped:
        assert forma_mcp is not None
        assert element_group_id is not None
        sem = asyncio.Semaphore(max(1, aecdm_concurrency))

        async def probe_one(entry: CatalogEntry) -> tuple[str, AecdmVerdict, str | None]:
            if entry.aecdm_label is None:
                return entry.key, "null_in_catalog", None
            async with sem:
                try:
                    await forma_mcp.query_elements(
                        element_group_id=element_group_id,
                        category=entry.aecdm_label,
                    )
                except Exception as exc:
                    return entry.key, "absent", str(exc)[:200]
            return entry.key, "present", None

        # For null_in_catalog entries we still want to *probe* with the
        # display label — many AECDM labels happen to equal the display.
        # We emit a "fillable" recommendation when that succeeds.
        async def probe_fallback(entry: CatalogEntry) -> tuple[str, AecdmVerdict, str | None]:
            async with sem:
                try:
                    await forma_mcp.query_elements(
                        element_group_id=element_group_id,
                        category=entry.display,
                    )
                except Exception as exc:
                    return entry.key, "null_in_catalog", str(exc)[:200]
            return entry.key, "present", None

        tasks = []
        for entry in catalog.entries:
            if entry.aecdm_label is None:
                tasks.append(probe_fallback(entry))
            else:
                tasks.append(probe_one(entry))
        results = await asyncio.gather(*tasks)
        for key, verdict, err in results:
            aecdm_results[key] = (verdict, err)
        log.info(
            "verify_catalog.aecdm_probed",
            total=len(catalog.entries),
            present=sum(1 for v, _ in aecdm_results.values() if v == "present"),
            absent=sum(1 for v, _ in aecdm_results.values() if v == "absent"),
            null=sum(1 for v, _ in aecdm_results.values() if v == "null_in_catalog"),
        )

    # Build verdicts
    verdicts: list[CatalogEntryVerdict] = []
    for entry in catalog.entries:
        revit_verdict: RevitVerdict
        revit_count = 0
        if revit_skipped:
            revit_verdict = "skipped"
        elif entry.ost in revit_present_ost:
            revit_verdict = "present"
            revit_count = revit_counts.get(entry.ost, 0)
        else:
            revit_verdict = "absent"
        aecdm_verdict: AecdmVerdict
        aecdm_error: str | None = None
        if aecdm_skipped:
            aecdm_verdict = "skipped"
        else:
            aecdm_verdict, aecdm_error = aecdm_results.get(
                entry.key, ("skipped", None)
            )
        verdicts.append(
            CatalogEntryVerdict(
                key=entry.key,
                display=entry.display,
                ost=entry.ost,
                aecdm_label=entry.aecdm_label,
                discipline=entry.discipline,
                revit_verdict=revit_verdict,
                revit_count=revit_count,
                aecdm_verdict=aecdm_verdict,
                aecdm_error=aecdm_error,
            )
        )

    # Recommendations
    recommendations: list[str] = []
    fillable = [v for v in verdicts if v.aecdm_verdict == "present" and v.aecdm_label is None]
    if fillable:
        recommendations.append(
            f"Fill {len(fillable)} AECDM-null entries — the probe succeeded "
            f"with the display label as the category name. Suggested edits:"
        )
        for v in fillable:
            recommendations.append(
                f"  - `{v.key}` → set `aecdm_label: {v.display}`"
            )
    dead = [v for v in verdicts if v.aecdm_verdict == "absent" and v.aecdm_label is not None]
    if dead:
        recommendations.append(
            f"Review {len(dead)} entries — AECDM rejected the label. "
            f"Could be renamed in this AECDM project, or not exposed:"
        )
        for v in dead[:10]:
            recommendations.append(
                f"  - `{v.key}` (current label `{v.aecdm_label}`) — error: "
                f"{(v.aecdm_error or '')[:80]}"
            )
        if len(dead) > 10:
            recommendations.append(f"  - …and {len(dead) - 10} more.")

    return CatalogVerifyReport(
        verdicts=verdicts,
        revit_skipped=revit_skipped,
        aecdm_skipped=aecdm_skipped,
        recommendations=recommendations,
    )
