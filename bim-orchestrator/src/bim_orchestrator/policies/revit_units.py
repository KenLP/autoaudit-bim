"""Revit storage units + unit conversion for v1.4 rules.

Background — why this exists
----------------------------
Revit stores most length/area/volume values in **imperial** internal
units (feet, square feet, cubic feet) regardless of project display
units. BEPs, codes, and standards almost always quote metric values
(metres, mm, m²). Without explicit unit handling, the rule
``threshold: 2.4`` against ``parameter: "Unbounded Height"`` silently
checks 2.4 *feet* against a value also in feet — every room passes
because it's taller than 0.73 m.

The pre-v1.4 workaround used "metric mirror" parameter names like
``"Unbounded Height (m)"`` that RevitQueryAgent computed at fetch time.
That worked but baked unit into the parameter name (awkward, fragile,
hard for Claude to remember). v1.4 introduces a first-class
``Rule.unit`` field instead: the parameter stays canonical
(``"Unbounded Height"``), the rule declares ``unit: "m"``, and this
module converts at compare-time.

How it plugs in
---------------
``QCAgent`` calls :func:`convert_to_rule_unit` on the raw value pulled
from ``element.params[rule.parameter]`` BEFORE handing off to the
evaluator. If ``rule.unit`` is None we skip conversion (raw value goes
through unchanged — back-compat with hand-authored YAML and with the
legacy metric-mirror parameter names).

Adding a parameter or a unit
----------------------------
Most projects only need a handful of length params (Height, Area,
Width, Length, Volume). Extend :data:`REVIT_STORAGE_UNITS` with new
canonical parameter names as they come up. For unit conversions not in
:data:`_CONVERSIONS`, add the factor and its inverse; the helper does
the rest.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Revit storage units — what raw element.params[name] is in BEFORE conversion
# ---------------------------------------------------------------------------

# Keys: canonical Revit parameter name (case-sensitive, matches Revit UI).
# Values: the storage unit string used in ``_CONVERSIONS`` below.
REVIT_STORAGE_UNITS: dict[str, str] = {
    # Length / height
    "Unbounded Height": "ft",
    "Height": "ft",
    "Width": "ft",
    "Length": "ft",
    "Perimeter": "ft",
    "Thickness": "ft",
    "Base Offset": "ft",
    "Limit Offset": "ft",
    # Stair Runs (OST_StairsRuns)
    "Actual Run Width": "ft",
    "Minimum Run Width": "ft",
    # MEP round sizing (QA F-aux: Diameter was length in param_catalog but absent
    # here → pipe/duct diameter rules silently skipped conversion).
    "Diameter": "ft",
    "Outside Diameter": "ft",
    "Inside Diameter": "ft",
    # Area
    "Area": "ft²",
    # Volume
    "Volume": "ft³",
}

# Internal storage unit (imperial feet family) per catalog DIMENSION. Used to make
# REVIT_STORAGE_UNITS a *view* of param_catalog so the two can't drift (QA F-aux).
_DIM_TO_STORAGE: dict[str, str] = {"length": "ft", "area": "ft²", "volume": "ft³"}
_catalog_units_cache: dict[str, str] | None = None


def _catalog_storage_units() -> dict[str, str]:
    """Param-name → storage unit derived from ``param_catalog`` dimensional params.

    Built once. Every length/area/volume built-in the catalog knows about becomes
    a storage-unit entry (ft/ft²/ft³), so the hand-kept dict above only needs
    overrides + params for categories not yet catalogued. Catalog absent → ``{}``
    (callers fall back to the explicit dict).
    """
    global _catalog_units_cache
    if _catalog_units_cache is not None:
        return _catalog_units_cache
    out: dict[str, str] = {}
    try:
        from bim_orchestrator.policies.param_catalog import load_param_catalog
        for cat in load_param_catalog().categories:
            for p in cat.params:
                unit = _DIM_TO_STORAGE.get(p.dimension)
                if unit is not None:
                    out.setdefault(p.name, unit)
    except Exception:                                       # noqa: BLE001
        pass
    _catalog_units_cache = out
    return out


def _storage_unit_for(param_name: str) -> str | None:
    """Storage unit for a param: explicit dict first, then the catalog view."""
    unit = REVIT_STORAGE_UNITS.get(param_name)
    return unit if unit is not None else _catalog_storage_units().get(param_name)

# ---------------------------------------------------------------------------
# Conversion factors — multiplied directly. Add both directions explicitly
# so the lookup is O(1) and there's no implicit inverse-rounding.
# ---------------------------------------------------------------------------

_FT_TO_M = 0.3048

_CONVERSIONS: dict[tuple[str, str], float] = {
    # length
    ("ft", "m"): _FT_TO_M,
    ("m", "ft"): 1.0 / _FT_TO_M,
    ("mm", "m"): 0.001,
    ("m", "mm"): 1000.0,
    ("ft", "mm"): _FT_TO_M * 1000.0,
    ("mm", "ft"): 1.0 / (_FT_TO_M * 1000.0),
    ("cm", "m"): 0.01,
    ("m", "cm"): 100.0,
    # area
    ("ft²", "m²"): _FT_TO_M * _FT_TO_M,
    ("m²", "ft²"): 1.0 / (_FT_TO_M * _FT_TO_M),
    # volume
    ("ft³", "m³"): _FT_TO_M ** 3,
    ("m³", "ft³"): 1.0 / (_FT_TO_M ** 3),
}


def rule_value_to_storage_unit(
    param_name: str | None, value: float, rule_unit: str | None
) -> tuple[float, str] | None:
    """Inverse of :func:`convert_to_rule_unit` — express a rule-unit VALUE
    (typically a rule's ``threshold``) back in the parameter's Revit STORAGE
    unit.

    v1.5-R6 (3.2): a Revit schedule filter (``configure_schedule``) compares
    against the model's internal storage value, not the rule's declared
    unit — a rule authored as ``threshold: 900, unit: mm`` needs its filter
    value expressed in feet (Width's storage unit) or the filter would
    silently compare 900 (mm) against a value in feet. Returns
    ``(converted_value, storage_unit)``, or ``None`` when the parameter's
    storage unit isn't known or no conversion factor is registered —
    callers should render NO filter rather than guess.
    """
    if param_name is None or rule_unit is None:
        return None
    raw_unit = _storage_unit_for(param_name)
    if raw_unit is None:
        return None
    if raw_unit == rule_unit:
        return value, raw_unit
    try:
        converted = convert(value, rule_unit, raw_unit)
    except ValueError:
        return None
    return converted, raw_unit


class UnitConversionError(ValueError):
    """A rule declared a unit but the raw value's KNOWN storage unit has no
    registered factor to it. Comparing anyway would be a silent wrong-unit
    pass/fail, so QC catches this and routes the element to manual_review (M-b)."""


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Return ``value`` converted from ``from_unit`` into ``to_unit``.

    Raises :class:`ValueError` when no conversion factor is registered.
    Same-unit conversions are a no-op.
    """
    if from_unit == to_unit:
        return value
    factor = _CONVERSIONS.get((from_unit, to_unit))
    if factor is None:
        raise ValueError(
            f"No conversion registered: {from_unit!r} -> {to_unit!r}. "
            f"Add the factor to policies/revit_units._CONVERSIONS."
        )
    return value * factor


def convert_to_rule_unit(
    value: Any, param_name: str | None, target_unit: str | None
) -> Any:
    """Convert a raw element value to the rule's declared unit.

    Returns the original ``value`` unchanged when:
      * ``target_unit`` is None (rule declares no unit → no conversion)
      * ``param_name`` is None or not in :data:`REVIT_STORAGE_UNITS`
        (storage unit unknown — assume value is already in target unit)
      * raw and target units are equal
      * ``value`` is None or non-numeric (let the evaluator reject it)

    Logs a warning and returns the raw value on conversion errors so a
    bad rule never crashes the QC pipeline.
    """
    if target_unit is None or param_name is None:
        return value
    raw_unit = _storage_unit_for(param_name)
    if raw_unit is None:
        # Unknown storage unit — assume the value is already in the
        # rule's target unit. Common for custom shared parameters and
        # for the legacy "(m)" / "areaMetric" mirrors that v1.3 produced.
        return value
    if raw_unit == target_unit:
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    try:
        return convert(numeric, raw_unit, target_unit)
    except ValueError as exc:
        # M-b: a KNOWN storage unit with no registered factor to the rule's unit.
        # Returning the raw number (the old behaviour) meant the evaluator then
        # compared a value in `raw_unit` against a threshold in `target_unit` —
        # a silent WRONG-UNIT pass/fail behind a single log line. Signal it so QC
        # routes the element to manual_review instead of guessing.
        raise UnitConversionError(
            f"cannot convert {param_name!r} from {raw_unit!r} to {target_unit!r}: {exc}"
        ) from exc


def format_in_unit(value: Any, unit: str | None) -> Any:
    """Render a converted value for HUMAN display: ``914.4`` + ``"mm"`` →
    ``"914.4 mm"``.

    L-02 (2026-07-25 live review): a rule reading *"Door width must be at
    least 900 mm"* produced the verdict in mm but reported the evidence as
    ``2.8333333333333335`` — Revit's raw feet, unlabelled. The judgement was
    right and unverifiable, which is precisely what a verification report
    exists to prevent. The converted number was already sitting in the trace
    next to the raw one; only the display picked the wrong field.

    Returns ``value`` UNCHANGED — the SAME object, so callers may test the
    result with ``is`` — when there is no unit to state or the value isn't
    numeric. A non-numeric value has no unit arithmetic behind it, and
    inventing a suffix would be a second kind of lie.

    Float noise from the conversion (``914.4000000000002``) is trimmed to 4
    decimals; the full-precision number stays in the trace's ``value`` field
    for anyone recomputing.
    """
    if unit is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    rounded = round(float(value), 4)
    text = f"{rounded:.4f}".rstrip("0").rstrip(".")
    return f"{text or '0'} {unit}"
