"""Classification subsets — category → valid classification codes (Phase 2 GĐ2).

The deterministic half of "is this the right Uniclass object?". Given an
element's category, ``subset_for`` returns the codes allowed for it; the
remediation closed loop (``requirement: value_in_subset``) then rejects any
proposal outside that set by machine — converting a slice of *meaning* into a
*membership* check, the same trick that makes the rest of the closed loop work.

GĐ2 step 4 — grounding: each code may carry a short DEFINITION. Because the
subset already narrows the candidates to a handful per category, we don't need
vector retrieval — we hand the model the definitions of exactly those candidates
so it picks the semantically-right code, and we keep the chosen code's definition
as evidence ("cite the clause") for the human reviewer. (Full vector-RAG over the
whole Uniclass table — reusing ``rag/`` — is the path for the un-narrowed case,
deferred until a complete corpus exists.)

Data lives in a YAML table (``config/classification_subsets.sample.yaml``),
mirroring the OST catalog / normalize "data-not-code" philosophy: add a category
by editing the file, never by editing Python. Each category maps either to a
plain list of codes, or to a ``{code: definition}`` map. Category labels resolve
through the existing ``OSTCatalog`` so "door" / "Doors" / aliases all map to the
same canonical entry — reusing Phase 1 infrastructure rather than duplicating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from .ost_catalog import OSTCatalog

log = structlog.get_logger(__name__)

# src/bim_orchestrator/policies/classification.py → repo root is parents[3].
_DEFAULT_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "classification_subsets.sample.yaml"
)


@dataclass
class ClassificationCatalog:
    """Loaded category → codes (+ optional definitions), OSTCatalog-aware."""

    system: str
    subsets: dict[str, list[str]]
    definitions: dict[str, dict[str, str]] = field(default_factory=dict)
    _catalog: OSTCatalog | None = field(default=None, repr=False)
    _lower: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # lowercased category label → canonical key, for case-insensitive lookup
        self._lower = {k.lower(): k for k in self.subsets}

    @classmethod
    def load(
        cls, path: Path | str | None = None, *, use_ost: bool = True
    ) -> "ClassificationCatalog":
        p = Path(path) if path is not None else _DEFAULT_PATH
        data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        subsets: dict[str, list[str]] = {}
        definitions: dict[str, dict[str, str]] = {}
        for cat, val in (data.get("subsets") or {}).items():
            cat = str(cat)
            if isinstance(val, dict):  # {code: definition}
                subsets[cat] = [str(c) for c in val.keys()]
                definitions[cat] = {str(c): str(d) for c, d in val.items()}
            else:  # plain list of codes
                subsets[cat] = [str(c) for c in (val or [])]
        catalog: OSTCatalog | None = None
        if use_ost:
            try:
                catalog = OSTCatalog.load()
            except Exception as exc:  # catalog optional — degrade to direct match
                log.info("classification.ost_unavailable", error=str(exc))
        return cls(
            system=str(data.get("system", "")),
            subsets=subsets,
            definitions=definitions,
            _catalog=catalog,
        )

    def _resolve_key(self, category: str | None) -> str | None:
        """Canonical subset key for a category label (exact → case-insensitive →
        OSTCatalog display), or None."""
        if not category:
            return None
        if category in self.subsets:
            return category
        low = category.lower()
        if low in self._lower:
            return self._lower[low]
        if self._catalog is not None:
            entry = self._catalog.find(category)
            if entry is not None and entry.display.lower() in self._lower:
                return self._lower[entry.display.lower()]
        return None

    def subset_for(self, category: str | None) -> list[str]:
        """Allowed codes for ``category`` (empty list if unknown)."""
        key = self._resolve_key(category)
        return self.subsets.get(key, []) if key else []

    def definitions_for(self, category: str | None) -> dict[str, str]:
        """``{code: definition}`` for ``category`` (empty if none/unknown)."""
        key = self._resolve_key(category)
        return self.definitions.get(key, {}) if key else {}

    def define(self, category: str | None, code: str) -> str | None:
        """Definition of one code for a category (evidence for the reviewer)."""
        return self.definitions_for(category).get(str(code).strip())
