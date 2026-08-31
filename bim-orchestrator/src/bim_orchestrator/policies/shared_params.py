"""Shared-parameter conventions loader — the openBIM companion to ``param_catalog``.

``param_catalog`` carries what a model actually exposes (live-probed Revit built-ins).
THIS carries the well-known *deliverable* parameters that are NOT reliable native
built-ins — COBie 2.4 fields, classification slots (Uniclass / OmniClass), and IFC
``Pset_*Common`` properties (e.g. ``ThermalTransmittance`` / U-value). They never
show up in a built-in probe, so without this layer the Rule Builder's LLM would have
to invent a parameter name when a user asks for a "COBie category" or "U-value" rule.

Pure data + lookups, no runtime re-binding. The conventions GROUND authoring:

  * **intent → agreed name** — a colloquial phrase ("u-value", "cobie category")
    maps to the standard shared-parameter name, fed to the extraction prompt.
  * **membership reference hint** — ``reference`` names the picklist a value rule
    should cite (the set may live in a private rule pack; this file only names it).
  * **binding / dimension** — seed ``remediation.target`` + unit handling, same as
    ``ParamSpec``.

This is a CONVENTION list, not a guarantee the model carries the parameter — the BIM
team adds it as a shared parameter per the project BEP. The Rule Builder still offers
the ✏️ Khác / ``bound_parameter`` escape for project-specific names not listed here.
One PUBLIC file (``config/shared_param_conventions.yaml``); the picklists it names may
be private (resolved by the engine's ``load_reference`` config/ fallback).
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

# parents[0]=policies, [1]=bim_orchestrator, [2]=src, [3]=<repo-root>
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_FILENAME = "shared_param_conventions.yaml"


class SharedParamConvention(BaseModel):
    """One well-known openBIM / deliverable parameter convention.

    Strict (``extra='forbid'``) so a typo'd YAML field fails at load, not silently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter: str
    standard: str = ""
    binding: str = "type"          # instance | type (free-form: COBie/IFC vary)
    dimension: str = "text"
    applies_to: tuple[str, ...] = Field(default_factory=tuple)
    intent: tuple[str, ...] = Field(default_factory=tuple)
    reference: str | None = None   # membership set name for a value-constraint rule
    note: str = ""

    def applies(self, category_label: str) -> bool:
        """True when this convention is relevant to a category label.

        Empty ``applies_to`` means 'any category' (e.g. a universal identifier).
        """
        return not self.applies_to or category_label in self.applies_to


class SharedParamConventions:
    """In-memory registry of :class:`SharedParamConvention`, loaded once per file."""

    SUPPORTED_VERSION = 1

    def __init__(self, conventions: list[SharedParamConvention]) -> None:
        self._conventions = list(conventions)
        self._by_param = {c.parameter.lower(): c for c in conventions}

    @classmethod
    def load(
        cls,
        *,
        config_dir: Path | str | None = None,
        path: Path | str | None = None,
    ) -> "SharedParamConventions":
        """Load + validate ``config/shared_param_conventions.yaml``."""
        if path is not None:
            conv_path = Path(path)
        else:
            base = Path(config_dir) if config_dir is not None else _DEFAULT_CONFIG_DIR
            conv_path = base / _FILENAME
        with open(conv_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Shared-param conventions must be a mapping: {conv_path}")
        ver = data.get("version")
        if ver != cls.SUPPORTED_VERSION:
            raise ValueError(
                f"Shared-param conventions version mismatch at {conv_path}: "
                f"expected {cls.SUPPORTED_VERSION}, got {ver!r}"
            )
        raw = data.get("conventions")
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"Shared-param conventions has no `conventions` list: {conv_path}")
        conventions = [SharedParamConvention.model_validate(c) for c in raw]
        log.info("shared_params.loaded", path=str(conv_path), conventions=len(conventions))
        return cls(conventions)

    # ---- introspection -------------------------------------------------

    @property
    def conventions(self) -> list[SharedParamConvention]:
        return list(self._conventions)

    def resolve(self, parameter: str) -> SharedParamConvention | None:
        """The convention for a parameter name (case-insensitive), or ``None``."""
        if not isinstance(parameter, str):
            return None
        return self._by_param.get(parameter.strip().lower())

    def for_category(self, category_label: str) -> list[SharedParamConvention]:
        """Conventions relevant to a category label (Rule Builder grounding)."""
        return [c for c in self._conventions if c.applies(category_label)]

    def match_intent(self, phrase: str, category_label: str | None = None) -> SharedParamConvention | None:
        """Map a colloquial phrase → a convention via its ``intent`` list.

        Longest matching intent phrase wins (so "u value" beats a shorter token).
        Scoped to ``category_label`` when given. Returns ``None`` on no match.
        """
        if not phrase:
            return None
        low = phrase.strip().lower()
        best: tuple[str, SharedParamConvention] | None = None
        for c in self._conventions:
            if category_label is not None and not c.applies(category_label):
                continue
            for token in c.intent:
                if token in low and (best is None or len(token) > len(best[0])):
                    best = (token, c)
        return best[1] if best else None


# Cache by resolved path so repeated loads across a session don't re-read.
_CACHE: dict[Path, SharedParamConventions] = {}


def load_shared_param_conventions(
    *,
    config_dir: Path | str | None = None,
    path: Path | str | None = None,
) -> SharedParamConventions:
    """Load + cache the shared-parameter conventions."""
    if path is not None:
        resolved = Path(path).resolve()
    else:
        base = Path(config_dir) if config_dir is not None else _DEFAULT_CONFIG_DIR
        resolved = (base / _FILENAME).resolve()
    cached = _CACHE.get(resolved)
    if cached is not None:
        return cached
    conv = SharedParamConventions.load(path=resolved)
    _CACHE[resolved] = conv
    return conv


def clear_cache() -> None:
    """Drop the load cache — for tests that rewrite the conventions file."""
    _CACHE.clear()
