"""v1 report module, Phase 2 — auto-create the native verification artifacts.

The verification report (Phase 1) tells a reviewer HOW to build, by hand, the
Revit schedule that reproduces each rule's finding set. This module BUILDS that
schedule for them — one click, still 100% native (a real Revit ViewSchedule the
reviewer reads + trusts), so the report isn't a black box.

It is driven entirely by the recorded ``check_trace`` (``report_trace.json``):
group by rule → derive the verify recipe from the rule's anatomy
(:func:`verify_recipes.recipe_for`) → create + configure a schedule per rule. No
RuleSet needed (the trace is self-contained), so this runs standalone AFTER any
finished run.

**Why schedules, not coloured view filters.** ``revit_configure_schedule``
filters support rich operators (``less`` / ``has_no_value`` / …), so a schedule
reproduces ANY requirement's predicate AND still lists the PASS set (the
false-negative defence). The addin's ``revit_apply_view_filter`` is equality-only
— it can't express ``< threshold`` / ``has no value`` — so the coloured-view step
stays a documented manual action (the add-in's filter is equality-only).
The schedule is created sorted/grouped by the checked parameter so the flagged
rows cluster.

MCP boundary respected: every Revit call goes through the injected client
(``mcp_clients/revit.py``); nothing here talks to Revit directly. Degrades
gracefully — if the addin lacks the schedule tools (``unknown_command``) the rule
is marked ``degraded`` and the run continues.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
from bim_orchestrator.report_trace import revit_parameter_of, rule_view_from_record
from bim_orchestrator.state import CheckRecord
from bim_orchestrator.verify_recipes import recipe_for

log = structlog.get_logger(__name__)

# Addin error codes that mean "this tool isn't available on this transport" —
# treat as a graceful degrade (the report's manual recipe still applies), not a
# hard error. Mirrors the batch per-element fallback in DesignAgent.
_DEGRADE_CODES = frozenset({"unknown_command", "command_not_found", "not_found", "404"})
# v1.5-R6 (3.1): addin error codes that mean "a schedule with this name
# already exists" — the FALLBACK idempotency signal for a transport whose
# get_views() doesn't (or can't) list schedules. Kept separate from
# _DEGRADE_CODES on purpose: "existing" and "degraded" mean different things
# to the reader of the manifest (one is success, the other is a real gap).
_EXISTS_CODES = frozenset({"duplicate_name", "name_conflict", "already_exists", "duplicate"})
# v1.7-R22 (D-4): re-configuring an EXISTING schedule can fail two ways that
# mean opposite things. A transport that simply has no configure_schedule
# ("unknown_command"/404) leaves a schedule that still exists and is still
# usable — degrade quietly and say so. But "not_found" means the id the probe
# just handed us is gone from the model, which is NOT a schedule we can claim
# exists — that one is a real error, not a shrug.
_RECONFIGURE_UNAVAILABLE = frozenset({"unknown_command", "command_not_found", "404"})


@dataclass
class ScheduleResult:
    """Outcome of trying to build one rule's verification schedule."""

    rule_id: str
    # v1.7-R22 adds "existing_reconfigured": the schedule was already there and
    # we re-applied its sort/filters rather than leaving a half-built one alone.
    status: str  # created | created_renamed | existing | existing_reconfigured | degraded | error
    category_ost: str | None
    schedule_id: int | None
    schedule_name: str | None
    fields: list[str] = field(default_factory=list)
    detail: str | None = None
    # LIVE 2026-08-01: `configure_schedule` reports per-filter/per-field
    # problems as `warnings` in an OTHERWISE-OK envelope (a filter Revit
    # refused is skipped, not an error). Ignoring them made "created" mean
    # "created AND configured" when it might only mean the former — the
    # assertion-without-evidence class. Carried onto the result so the
    # manifest can say what actually got applied.
    warnings: list[str] = field(default_factory=list)
    # LIVE PROBE 2026-07-12 (addin v0.8.13, F1/F3): the addin can silently
    # rename a schedule (forbidden characters / duplicate name) instead of
    # erroring. requested_name is always what WE asked for; schedule_name
    # (above) is what the addin actually applied — they differ only when
    # status == "created_renamed".
    requested_name: str | None = None


def _resolve_ost(label: str, catalog: Any | None) -> str:
    """Map a category label ("Doors") → BuiltInCategory ("OST_Doors").

    Uses the OST catalog when available; falls back to the ``OST_<Label>``
    convention (spaces stripped) so a category the catalog doesn't know still
    gets a reasonable attempt rather than crashing.
    """
    if catalog is not None:
        ost = catalog.resolve(label, "revit")
        if ost:
            return str(ost)
    if label.startswith("OST_"):
        return label
    return "OST_" + label.replace(" ", "")


def _extract_schedule_id(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    raw = data.get("scheduleId", data.get("id"))
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _group_by_rule(check_trace: list[CheckRecord]) -> OrderedDict[str, list[CheckRecord]]:
    grouped: OrderedDict[str, list[CheckRecord]] = OrderedDict()
    for rec in check_trace:
        grouped.setdefault(rec["rule_id"], []).append(rec)
    return grouped


async def _probe_existing_schedules(client: Any) -> dict[str, int | None]:
    """v1.5-R6 (3.1): the PRIMARY idempotency probe — list whatever views the
    client already knows about and index by name, so a second
    ``--create-verification-views`` run on the same finished run recognises
    schedules it already built. A client that doesn't support ``get_views``
    (older addin, or a transport that just doesn't implement it) degrades to
    an empty map — the duplicate-name catch in the create loop below is the
    fallback probe for that case.
    """
    try:
        views = await client.get_views()
    except (RevitEnvelopeError, AttributeError, NotImplementedError) as exc:
        log.info("verification_views.probe_unavailable", error=str(exc))
        return {}
    existing: dict[str, int | None] = {}
    for v in views or []:
        name = v.get("name") if isinstance(v, dict) else None
        if name:
            vid = v.get("id", v.get("viewId"))
            try:
                existing[name] = int(vid) if vid is not None else None
            except (TypeError, ValueError):
                existing[name] = None
    return existing


def _sort_fields_for(rule: Any) -> list[dict[str, Any]]:
    """The sort/group instruction for one rule's schedule.

    v1.5-R6: the field must be the EFFECTIVE Revit name (``bound_parameter``
    wins) — ``rule.parameter`` alone is the canonical intent label and may not
    exist on the element at all for a bound rule. Shared by the create path and
    the D-4 re-configure path so the two can never drift.
    """
    return [{
        "field": revit_parameter_of(rule),
        "ascending": True,
        "groupBy": rule.requirement == "unique_in_set",
    }]


def _configuration_notes(recipe: Any, warnings: list[str]) -> str | None:
    """The honesty notes that belong on any result whose schedule got
    configured — degraded recipe first, then anything the addin refused."""
    parts: list[str] = []
    if recipe.degraded:
        # Honest: the schedule shows the operands/values, not a one-filter
        # verdict — pair with the Select-by-ID list from the report.
        parts.append(
            "degraded recipe — schedule shows the inputs; verify via "
            "Select-by-ID + the per-rule operands table in the report."
        )
    if warnings:
        # The schedule exists, but the addin refused part of what we asked for
        # (a field it couldn't add, a filter Revit rejected). Say so —
        # "created" must not imply "fully configured".
        parts.append(
            "addin warnings (part of the configuration was NOT applied): "
            + " · ".join(warnings)
        )
    return " ".join(parts) or None


async def create_verification_schedules(
    client: Any,
    check_trace: list[CheckRecord],
    *,
    catalog: Any | None = None,
    dry_run: bool = False,
) -> list[ScheduleResult]:
    """Create one verification schedule per rule from a recorded ``check_trace``.

    For each rule: resolve the category, ``create_schedule`` with the recipe's
    fields (+ the recipe's ``filters``, when expressible — 3.2), then
    ``configure_schedule`` to sort (and, for uniqueness rules, group) by the
    checked parameter so flagged rows cluster while the PASS set stays
    visible. Returns one :class:`ScheduleResult` per rule.

    v1.5-R6 (3.1, idempotent): a schedule named ``"AutoAudit - <rule_id>"``
    that already exists (per :func:`_probe_existing_schedules`, or — when
    that probe comes back empty — a duplicate-name error from
    ``create_schedule`` itself) is never recreated. Delete-and-recreate was
    considered and rejected: it needs a LIVE probe of the addin's
    rename/delete behaviour that this module can't safely assume.

    v1.7-R22 (D-4): "never recreated" no longer means "never touched". When we
    know the existing schedule's id, its sort/group/filter configuration is
    **re-applied** (``status="existing_reconfigured"``) instead of skipped.
    Without this, a schedule that was created and then failed mid-configure
    stayed half-built forever: every later run saw the name, reported
    ``"existing"``, and the manifest read like success while the schedule sat
    in the model unsorted and unfiltered. ``configure_schedule`` is
    declarative, so re-applying it is idempotent. ``status="existing"`` now
    means specifically "found, but could NOT re-apply" (no id, or no
    configure tool on this transport) — the honest weaker claim.

    Ownership caveat: a schedule under the ``AutoAudit - <rule_id>`` name is
    treated as tool-owned, so re-applying overwrites hand edits to its sort
    and filters. Rename a schedule to keep manual changes.

    Naming note (LIVE PROBE 2026-07-12, addin v0.8.13, F1): the name is
    ``"AutoAudit - <rule_id>"`` — NOT bracketed. Revit forbids ``[ ] { } | ;
    < > ? ~ :`` and backslash in a view name and the addin silently falls
    back to its own default (e.g. "Door Schedule 3") instead of erroring, so
    the old ``"[AutoAudit] <rule_id>"`` convention was NEVER actually applied
    on a real Revit session — see :func:`_check_name_applied` for the
    defence against this (and any other) silent rename.
    """
    grouped = _group_by_rule(check_trace)
    results: list[ScheduleResult] = []
    existing_schedules = await _probe_existing_schedules(client)
    for rule_id, recs in grouped.items():
        rule = rule_view_from_record(recs[0])
        recipe = recipe_for(rule, recs)
        ost = _resolve_ost(recipe.schedule.category, catalog)
        name = f"AutoAudit - {rule_id}"
        fields = list(recipe.schedule.fields)

        if name in existing_schedules:
            existing_sid = existing_schedules[name]
            if existing_sid is None:
                # The probe saw the NAME but the transport gave no id — there is
                # nothing to configure. Same report as before v1.7-R22.
                results.append(ScheduleResult(
                    rule_id, "existing", ost, None, name, fields,
                    "schedule already exists from a previous run — not recreated; "
                    "the transport reported no schedule id, so its configuration "
                    "could NOT be re-applied. Check its sort/filters by hand.",
                    requested_name=name,
                ))
                log.info(
                    "verification_views.existing", rule=rule_id,
                    schedule_id=None, category=ost, reconfigured=False,
                )
                continue
            # v1.7-R22 (D-4): re-apply the configuration instead of skipping.
            # The failure this fixes: take 1 creates the schedule and then fails
            # (or is interrupted) while configuring it; take 2 sees the name,
            # reports "existing", and leaves the half-built schedule unsorted and
            # unfiltered forever — while the manifest reads like success.
            # configure_schedule is declarative (sort/group/filters), so
            # re-applying it is idempotent. Creating is still never repeated.
            try:
                configured = await client.configure_schedule(
                    existing_sid, sort_fields=_sort_fields_for(rule),
                    filters=list(recipe.schedule.filters) or None,
                    # LIVE 2026-08-19: the addin ADDS sort fields and filters, it
                    # does not replace them. Without clearing first, re-applying
                    # appended our sort BEHIND whatever stale sort was already
                    # primary — the manifest said "re-applied" while the schedule
                    # on screen did not move. Re-configuring means REPLACE our
                    # configuration, so clear before writing. (The create path
                    # needs no clear: a fresh schedule has neither.)
                    clear_sort_fields=True, clear_filters=True,
                    dry_run=dry_run,
                )
                warnings = _configure_warnings(configured)
                detail = (
                    "schedule already existed from a previous run — not "
                    "recreated; its sort/filter configuration was re-applied."
                )
                notes = _configuration_notes(recipe, warnings)
                results.append(ScheduleResult(
                    rule_id, "existing_reconfigured", ost, existing_sid, name,
                    fields, f"{detail} {notes}" if notes else detail,
                    requested_name=name, warnings=warnings,
                ))
                log.info(
                    "verification_views.existing", rule=rule_id,
                    schedule_id=existing_sid, category=ost, reconfigured=True,
                    warnings=len(warnings), dry_run=dry_run,
                )
            except RevitEnvelopeError as exc:
                if exc.code in _RECONFIGURE_UNAVAILABLE:
                    # No configure tool on this transport. The schedule is
                    # genuinely there and usable — report it as existing, and
                    # say plainly that we could not re-assert its configuration.
                    results.append(ScheduleResult(
                        rule_id, "existing", ost, existing_sid, name, fields,
                        f"schedule already exists — not recreated; re-applying "
                        f"its configuration is unavailable on this transport "
                        f"({exc.code}). Check its sort/filters by hand.",
                        requested_name=name,
                    ))
                    log.info(
                        "verification_views.existing", rule=rule_id,
                        schedule_id=existing_sid, category=ost,
                        reconfigured=False, code=exc.code,
                    )
                else:
                    results.append(ScheduleResult(
                        rule_id, "error", ost, existing_sid, name, fields,
                        f"{exc.code}: {exc.message} (a schedule with this name "
                        f"was found — id {existing_sid} — but re-applying its "
                        f"configuration failed; it may be unsorted and "
                        f"unfiltered, or no longer in the model).",
                        requested_name=name,
                    ))
                    log.warning(
                        "verification_views.reconfigure_failed", rule=rule_id,
                        schedule_id=existing_sid, code=exc.code, category=ost,
                    )
            continue

        created_sid: int | None = None
        try:
            created = await client.create_schedule(
                ost, name=name, fields=fields, dry_run=dry_run
            )
            sid = _extract_schedule_id(created)
            if sid is None:
                results.append(ScheduleResult(
                    rule_id, "error", ost, None, name, fields,
                    "addin returned no scheduleId", requested_name=name,
                ))
                continue
            created_sid = sid
            # LIVE PROBE 2026-07-12 (addin v0.8.13, F1/F3): the addin can
            # silently rename the schedule (forbidden characters / duplicate
            # name) instead of erroring — trust what it says it did, not what
            # we asked for.
            actual_name, renamed = _check_name_applied(rule_id, name, created)
            # Sort by the checked parameter so fails cluster; group for
            # uniqueness. v1.5-R6 (3.2): pass the recipe's real schedule filters
            # through — empty for a recipe that can't express one.
            configured = await client.configure_schedule(
                sid, sort_fields=_sort_fields_for(rule),
                filters=list(recipe.schedule.filters) or None,
                dry_run=dry_run,
            )
            warnings = _configure_warnings(configured)
            detail = _configuration_notes(recipe, warnings)
            if renamed:
                rename_note = (
                    f"addin applied a different name than requested "
                    f"(requested {name!r}, got {actual_name!r}) — see "
                    f"the add-in renamed it silently."
                )
                detail = f"{detail} {rename_note}" if detail else rename_note
            results.append(ScheduleResult(
                rule_id, "created_renamed" if renamed else "created",
                ost, sid, actual_name, fields, detail, requested_name=name,
                warnings=warnings,
            ))
            log.info(
                "verification_views.created",
                rule=rule_id, schedule_id=sid, category=ost,
                degraded=recipe.degraded, dry_run=dry_run,
                filters=len(recipe.schedule.filters), renamed=renamed,
                warnings=len(warnings),
            )
        except RevitEnvelopeError as exc:
            if exc.code in _EXISTS_CODES:
                # Fallback idempotency probe (3.1): the primary get_views()
                # probe came back empty (or missed this one), but the addin
                # itself refused the duplicate name — same conclusion either way.
                results.append(ScheduleResult(
                    rule_id, "existing", ost, None, name, fields,
                    f"{exc.code}: addin reports this schedule name already exists.",
                    requested_name=name,
                ))
                log.info("verification_views.existing_via_create_error", rule=rule_id)
                continue
            status = "degraded" if exc.code in _DEGRADE_CODES else "error"
            log.warning(
                "verification_views.failed",
                rule=rule_id, code=exc.code, status=status,
                created_schedule_id=created_sid,
            )
            detail = f"{exc.code}: {exc.message}"
            if created_sid is not None:
                # LIVE 2026-08-01: create succeeded, configure failed — the
                # manifest reported schedule_id null and the half-built
                # schedule stayed in the model with nothing pointing at it.
                detail += (
                    f" (the schedule WAS created — id {created_sid} — but "
                    f"configuring it failed; it is in the model unsorted and "
                    f"unfiltered. Delete it or configure it by hand.)"
                )
            results.append(ScheduleResult(
                rule_id, status, ost, created_sid, name, fields,
                detail, requested_name=name,
            ))
    return results


def _configure_warnings(configured: Any) -> list[str]:
    """Pull ``configure_schedule``'s ``warnings`` out of an OK envelope's data.

    The addin adds a warning (and carries on) when it can't add a field to the
    schedule or when Revit refuses a filter — the envelope is still ``ok``, so
    only the ``warnings`` array distinguishes "configured" from "asked, and
    quietly ignored". Anything unparseable degrades to no warnings rather than
    inventing one.
    """
    if not isinstance(configured, dict):
        return []
    raw = configured.get("warnings")
    if not isinstance(raw, list):
        return []
    return [str(w) for w in raw if w is not None]


def _check_name_applied(
    rule_id: str, requested: str, created: Any
) -> tuple[str, bool]:
    """LIVE PROBE 2026-07-12 (addin v0.8.13, F1/F3) honesty-check: the addin
    can silently apply a DIFFERENT name than the one we asked for (forbidden
    characters, or a duplicate the primary probe didn't catch) instead of
    erroring. Returns ``(actual_name, renamed)`` — ``actual_name`` falls back
    to ``requested`` when the addin's response carries no name at all (older
    addin / degraded envelope), which is treated as "not renamed" rather than
    guessed at.
    """
    actual = created.get("name") if isinstance(created, dict) else None
    if not actual:
        return requested, False
    if actual == requested:
        return actual, False
    log.warning(
        "verification_views.name_not_applied",
        rule=rule_id, requested_name=requested, actual_name=actual,
    )
    return actual, True


_MANIFEST_STATUSES = (
    "created", "created_renamed", "existing", "existing_reconfigured",
    "degraded", "error",
)


def manifest_dict(results: list[ScheduleResult]) -> dict[str, Any]:
    """Serializable manifest of a schedule-build pass (written next to the run)."""
    counts: dict[str, int] = {s: 0 for s in _MANIFEST_STATUSES}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"summary": counts, "schedules": [asdict(r) for r in results]}


def render_manifest_markdown(results: list[ScheduleResult]) -> str:
    """Short human-readable summary of what was (or wasn't) created."""
    lines = ["# Verification views — build manifest", ""]
    counts: dict[str, int] = {s: 0 for s in _MANIFEST_STATUSES}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    lines.append(
        f"- Created: **{counts['created']}** "
        f"(renamed by addin: **{counts['created_renamed']}**) "
        f"· Existing, re-configured (v1.7-R22): "
        f"**{counts['existing_reconfigured']}** "
        f"· Existing, left as-is: **{counts['existing']}** "
        f"· Degraded: **{counts['degraded']}** · Error: **{counts['error']}**"
    )
    lines.append("")
    lines.append("| Rule | Status | Category | Schedule id | Name | Note |")
    lines.append("|------|--------|----------|-------------|------|------|")
    for r in results:
        lines.append(
            f"| `{r.rule_id}` | {r.status} | {r.category_ost or '?'} "
            f"| {r.schedule_id if r.schedule_id is not None else '-'} "
            f"| {r.schedule_name or '-'} | {(r.detail or '').replace('|', '/')} |"
        )
    lines.append("")
    if counts["degraded"]:
        lines.append(
            "> Degraded rules: the addin couldn't build the schedule (or the "
            "check is cross-element). Use the manual recipe + Select-by-ID list "
            "in `verification_report.md`."
        )
    if any(r.warnings for r in results):
        lines.append(
            "> Rules with addin warnings: the schedule exists but part of what "
            "was asked for (a field, a filter) was NOT applied — the `Note` "
            "column quotes the addin verbatim. Treat those schedules as "
            "unfiltered and fall back to the manual recipe in "
            "`verification_report.md`."
        )
    if counts["existing"]:
        lines.append(
            "> Existing, left as-is: the schedule is in the model but its "
            "configuration could NOT be re-applied (no schedule id from the "
            "transport, or no `configure_schedule` on it). It may be sorted and "
            "filtered from an earlier run — or not at all. Check it by hand."
        )
    if counts["created_renamed"]:
        lines.append(
            "> Renamed rules: the addin applied a DIFFERENT schedule name than "
            "requested (silent fallback — see "
            "the add-in renames silently). The `Name` "
            "column above is the ACTUAL name; look it up under that, not the "
            "`AutoAudit - <rule_id>` convention."
        )
    return "\n".join(lines)


__all__ = [
    "ScheduleResult",
    "create_verification_schedules",
    "manifest_dict",
    "render_manifest_markdown",
]
