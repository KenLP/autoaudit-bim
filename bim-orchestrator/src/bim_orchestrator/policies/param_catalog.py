"""Parameter Catalog loader — the PARAMETER-layer sibling of ``ost_catalog.py``.

``ost_catalog`` maps a rule author's *category label* → a Revit ``BuiltInCategory``
(``"Walls"`` → ``"OST_Walls"``). THIS maps a ``(category, parameter)`` → a
:class:`ParamSpec` describing how that built-in parameter is stored and bound
(storage type, instance vs type, writable, dimension). The join key between the two
layers is the **OST string** — so a rule names a category label, ``OSTCatalog``
resolves it to an OST, and this resolves the OST + parameter to the facts the Rule
Builder / extractor need.

What it unlocks (all at AUTHORING time — this is not a runtime re-binding layer):

  * **read-only refusal** — ``writable=False`` ⇒ the param can be *checked* but never
    be a Path-B *write target* (e.g. wall ``Width``, ``Area``, ``Unbounded Height``).
  * **write-target pre-resolution** — ``binding`` (instance|type) seeds
    ``remediation.target`` before a run can fail with ``not_found``/``read_only``.
  * **unit inference** — ``dimension`` (length/area/…) tells the engine a value needs
    unit conversion; ``revit_units.REVIT_STORAGE_UNITS`` can become a view of this.
  * **valid dropdown / "param-known" warning** — a ``(category, param)`` absent from
    the catalog is flagged ("not a built-in of this category; set bound_parameter or
    confirm it's a shared/project param"), mirroring the OST-catalog gate.

Scope: SYSTEM-family categories + SYSTEM (built-in) parameters only — Revit-defined,
stable across files. Loadable families (Doors/Windows/Furniture) carry
family-authored params and keep the ``bound_parameter``/``custom_param`` escape
hatch. Provenance tier: ``mcp-probe`` (see the config header +
``scripts/dump_param_catalog.py``). One file per Revit version
(``param_catalog.<version>.yaml``) — built-in params differ across versions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

Binding = Literal["instance", "type"]
Storage = Literal["string", "integer", "double", "elementid", "none"]
Dimension = Literal[
    "length", "area", "volume", "angle", "number",
    "percent", "text", "option", "yesno", "reference", "none",
]
FamilyKind = Literal["system", "loadable"]

# Dimensions that carry a physical unit (→ need Rule.unit / revit_units conversion).
_DIMENSIONAL = frozenset({"length", "area", "volume", "angle"})

# Catalogs live at <repo-root>/config/param_catalog.<version>.yaml.
# parents[0]=policies, [1]=bim_orchestrator, [2]=src, [3]=<repo-root>
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
DEFAULT_VERSION = "2027"


class ParamSpec(BaseModel):
    """One built-in parameter's storage + binding facts.

    Strict (``extra='forbid'``) so a typo'd YAML field fails at load, not silently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    storage: Storage
    binding: Binding
    writable: bool
    dimension: Dimension
    sample: str | None = None
    # Changed via rename_element, not set_parameter (Family Name → rename family,
    # Type Name → rename type — the K19 path). Param itself is read-only.
    rename_only: bool = False

    @property
    def is_write_target(self) -> bool:
        """Can this parameter be a Path-B remediation write target?

        Writable params, plus the rename-only identity params (changed via
        ``rename_element``). Read-only geometry (``Width``, ``Area``) is False.
        """
        return self.writable or self.rename_only

    @property
    def needs_unit(self) -> bool:
        """True when the value carries a physical unit (length/area/volume/angle)."""
        return self.dimension in _DIMENSIONAL


class CategoryParams(BaseModel):
    """All catalogued built-in params for one built-in category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    ost: str
    family_kind: FamilyKind = "system"
    has_type: bool = True
    params: list[ParamSpec] = Field(default_factory=list)


class ParamCatalog:
    """In-memory ``(OST, parameter) → ParamSpec`` registry. Loaded once per version.

    Lookups accept either the OST string (the canonical join key, what
    ``OSTCatalog.resolve(label, backend='revit')`` returns) or the catalog ``key``,
    and are case-insensitive on the parameter name::

        cat = ParamCatalog.load("2027")
        spec = cat.resolve_param("OST_Walls", "Fire Rating")   # → ParamSpec(type, writable)
        cat.is_write_target("OST_Walls", "Width")              # → False (read-only)
        cat.is_write_target("OST_Walls", "Bogus Param")        # → None  (unknown)
        cat.params_for("OST_StairsRuns")                       # → [ParamSpec, ...] (dropdown)
    """

    SUPPORTED_VERSION = 1

    def __init__(
        self,
        categories: list[CategoryParams],
        *,
        revit_version: str = "",
        provenance_tier: str = "",
        source_model: str = "",
    ) -> None:
        self._categories = list(categories)
        self.revit_version = revit_version
        self.provenance_tier = provenance_tier
        self.source_model = source_model
        self._by_ost: dict[str, CategoryParams] = {c.ost: c for c in categories}
        self._by_key: dict[str, CategoryParams] = {c.key: c for c in categories}
        # Per-category param index, keyed by lowercased param name.
        self._param_idx: dict[str, dict[str, ParamSpec]] = {
            c.ost: {p.name.lower(): p for p in c.params} for c in categories
        }

    # ---- construction --------------------------------------------------

    @classmethod
    def load(
        cls,
        version: str | None = None,
        *,
        config_dir: Path | str | None = None,
        path: Path | str | None = None,
    ) -> "ParamCatalog":
        """Load + validate ``config/param_catalog.<version>.yaml``.

        ``version`` defaults to ``$REVIT_MCP_VERSION`` or :data:`DEFAULT_VERSION`.
        Pass ``path`` to load a specific file (tests).
        """
        if path is not None:
            catalog_path = Path(path)
        else:
            ver = version or os.environ.get("REVIT_MCP_VERSION") or DEFAULT_VERSION
            base = Path(config_dir) if config_dir is not None else _DEFAULT_CONFIG_DIR
            catalog_path = base / f"param_catalog.{ver}.yaml"
        with open(catalog_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Param catalog must be a mapping at top level: {catalog_path}")
        ver_field = data.get("version")
        if ver_field != cls.SUPPORTED_VERSION:
            raise ValueError(
                f"Param catalog version mismatch at {catalog_path}: "
                f"expected {cls.SUPPORTED_VERSION}, got {ver_field!r}"
            )
        raw = data.get("categories")
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"Param catalog has no `categories` list: {catalog_path}")
        categories = [CategoryParams.model_validate(c) for c in raw]
        log.info(
            "param_catalog.loaded",
            path=str(catalog_path),
            revit_version=data.get("revit_version", ""),
            categories=len(categories),
            params=sum(len(c.params) for c in categories),
        )
        return cls(
            categories,
            revit_version=str(data.get("revit_version", "")),
            provenance_tier=str(data.get("provenance_tier", "")),
            source_model=str(data.get("source_model", "")),
        )

    # ---- introspection -------------------------------------------------

    @property
    def categories(self) -> list[CategoryParams]:
        return list(self._categories)

    def category(self, category_or_ost: str) -> CategoryParams | None:
        """Resolve a category by OST string (preferred) or catalog key."""
        if not isinstance(category_or_ost, str):
            return None
        return self._by_ost.get(category_or_ost) or self._by_key.get(category_or_ost)

    def params_for(self, category_or_ost: str) -> list[ParamSpec]:
        """All catalogued params for a category — the Rule Builder dropdown source."""
        cat = self.category(category_or_ost)
        return list(cat.params) if cat is not None else []

    # ---- resolution ----------------------------------------------------

    def resolve_param(self, category_or_ost: str, param_name: str) -> ParamSpec | None:
        """The :class:`ParamSpec` for ``(category, param)``, or ``None`` if unknown.

        ``None`` means the param is not a catalogued built-in of that category —
        the caller decides whether to warn (Rule Builder) or fall back to the
        ``bound_parameter``/``custom_param`` path.
        """
        cat = self.category(category_or_ost)
        if cat is None or not isinstance(param_name, str):
            return None
        return self._param_idx[cat.ost].get(param_name.strip().lower())

    def is_known(self, category_or_ost: str, param_name: str) -> bool:
        """True iff ``param_name`` is a catalogued built-in of the category."""
        return self.resolve_param(category_or_ost, param_name) is not None

    def is_write_target(self, category_or_ost: str, param_name: str) -> bool | None:
        """Whether the param may be a Path-B write target.

        ``True``/``False`` for a known param (False ⇒ read-only — check-only);
        ``None`` when the param is unknown (caller decides).
        """
        spec = self.resolve_param(category_or_ost, param_name)
        return None if spec is None else spec.is_write_target

    def binding_of(self, category_or_ost: str, param_name: str) -> Binding | None:
        """``instance``/``type`` for a known param — seeds ``remediation.target``."""
        spec = self.resolve_param(category_or_ost, param_name)
        return None if spec is None else spec.binding

    def dimension_of(self, category_or_ost: str, param_name: str) -> Dimension | None:
        spec = self.resolve_param(category_or_ost, param_name)
        return None if spec is None else spec.dimension


# Cache by resolved path so repeated loads across a multi-rule run don't re-read.
_CACHE: dict[Path, ParamCatalog] = {}


def load_param_catalog(
    version: str | None = None,
    *,
    config_dir: Path | str | None = None,
    path: Path | str | None = None,
) -> ParamCatalog:
    """Load + cache the parameter catalog for a Revit version."""
    if path is not None:
        resolved = Path(path).resolve()
    else:
        ver = version or os.environ.get("REVIT_MCP_VERSION") or DEFAULT_VERSION
        base = Path(config_dir) if config_dir is not None else _DEFAULT_CONFIG_DIR
        resolved = (base / f"param_catalog.{ver}.yaml").resolve()
    cached = _CACHE.get(resolved)
    if cached is not None:
        return cached
    catalog = ParamCatalog.load(path=resolved)
    _CACHE[resolved] = catalog
    return catalog


def clear_cache() -> None:
    """Drop the load cache — for tests that rewrite a catalog file."""
    _CACHE.clear()
