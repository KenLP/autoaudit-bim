"""OST Catalog loader + label resolver (v1.3 foundation).

The catalog (``config/ost_catalog.yaml``) is the single source of truth that
maps a rule author's compliance label (e.g. ``"Walls"``, ``"Doors"``,
``"Ống gió"``) to the MCP-specific string each backend expects:

  * Revit MCP wants a Revit ``BuiltInCategory`` enum string
    (``"OST_Walls"``, ``"OST_DuctCurves"``, ...).
  * Forma/AECDM MCP wants a display label (``"Walls"``, ``"Ducts"``).
    Some categories aren't exposed by AECDM at all — those carry a
    null ``aecdm_label`` and the resolver returns ``None`` + warns so
    the caller can skip cleanly.

This module deliberately stays MCP-agnostic — it knows nothing about
how a category is queried, only how its name translates. The shared
``derive_specs()`` helper (Task #4) consumes ``OSTCatalog`` to turn a
``RuleSet`` into per-category specs for either backend.

Resolution priority (see ``resolve()`` / ``find()``):

  1. Exact match on ``key``      (case-sensitive — internal stable id)
  2. Exact match on ``display``  (case-sensitive — pretty name)
  3. Case-insensitive match against key / display / aliases
  4. Bounded Levenshtein fuzzy match (typo tolerance, length-gated)
  5. Unknown → log ``ost_catalog.unknown_label`` warn + return None

The fuzzy step is length-gated to avoid false positives on short
strings (``"Door"`` is distance 2 from ``"Roof"`` — nope). The gate:

  * len < 5  → no fuzzy match (returns None)
  * len 5–7  → max distance 1
  * len ≥ 8  → max distance 2

When a fuzzy match is ambiguous (two entries tied at the same minimum
distance), the resolver logs the candidates and returns None rather
than picking arbitrarily.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

Backend = Literal["revit", "aecdm"]
Discipline = Literal["architecture", "structure", "mep"]

# Catalog YAML lives at <repo-root>/config/ost_catalog.yaml.
# parents[0]=policies, [1]=bim_orchestrator, [2]=src, [3]=<repo-root>
_DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "ost_catalog.yaml"
)


class CatalogEntry(BaseModel):
    """One row of the OST catalog.

    Strict validation: unknown fields raise (``extra='forbid'``) — catches
    typos in the YAML at load time rather than silently dropping data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    display: str
    aecdm_label: str | None = None
    ost: str
    discipline: Discipline
    aliases: list[str] = Field(default_factory=list)


class OSTCatalog:
    """In-memory dual-label registry. Loaded once at process startup.

    Resolution is deterministic + cached — repeated lookups during
    ``derive_specs()`` over a multi-rule RuleSet return instantly.

    Typical use::

        catalog = OSTCatalog.load()
        ost = catalog.resolve("Walls", backend="revit")     # → "OST_Walls"
        aecdm = catalog.resolve("Walls", backend="aecdm")   # → "Walls"
        none = catalog.resolve("Walls", backend="aecdm")    # → None (e.g. Rebar)
        entry = catalog.find("Tường")                       # → CatalogEntry(key="walls", ...)
    """

    DEFAULT_PATH = _DEFAULT_CATALOG_PATH
    SUPPORTED_VERSION = 1

    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries: list[CatalogEntry] = list(entries)
        # Exact-match indexes (case-sensitive). Key collisions are caught
        # by _validate_ost_catalog.py at edit time, so dict construction
        # losing a duplicate is acceptable here.
        self._by_key: dict[str, CatalogEntry] = {e.key: e for e in entries}
        self._by_display: dict[str, CatalogEntry] = {e.display: e for e in entries}
        # Case-insensitive index across key + display + aliases. First-write
        # wins so the canonical display claims the lowercase form before
        # any alias does (entries are iterated in catalog order).
        self._by_lower: dict[str, CatalogEntry] = {}
        for e in entries:
            for tag in (e.key, e.display, *e.aliases):
                self._by_lower.setdefault(tag.lower(), e)
        # resolve() cache — (label, backend) → str | None
        self._cache: dict[tuple[str, Backend], str | None] = {}

    # ---- construction --------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "OSTCatalog":
        """Load + validate the catalog YAML at ``path`` (default: repo-root)."""
        catalog_path = path or cls.DEFAULT_PATH
        with open(catalog_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"OST catalog must be a mapping at top level: {catalog_path}")
        version = data.get("version")
        if version != cls.SUPPORTED_VERSION:
            raise ValueError(
                f"OST catalog version mismatch at {catalog_path}: "
                f"expected {cls.SUPPORTED_VERSION}, got {version!r}"
            )
        raw_entries = data.get("categories")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError(f"OST catalog has no `categories` list: {catalog_path}")
        entries = [CatalogEntry.model_validate(e) for e in raw_entries]
        log.info(
            "ost_catalog.loaded",
            path=str(catalog_path),
            entries=len(entries),
            aecdm_null=sum(1 for e in entries if e.aecdm_label is None),
        )
        return cls(entries)

    # ---- introspection -------------------------------------------------

    @property
    def entries(self) -> list[CatalogEntry]:
        """Defensive copy — callers can iterate without mutating internal state."""
        return list(self._entries)

    def by_discipline(self, discipline: Discipline) -> list[CatalogEntry]:
        return [e for e in self._entries if e.discipline == discipline]

    # ---- resolution ----------------------------------------------------

    def find(self, label: str) -> CatalogEntry | None:
        """Backend-agnostic lookup. Returns the matching entry or None.

        Use ``resolve()`` when you want the backend-specific string;
        ``find()`` is for callers that need the full entry (discipline,
        display name for reporting, etc.).
        """
        if not isinstance(label, str) or not label:
            return None
        # 1: exact key (case-sensitive)
        if label in self._by_key:
            return self._by_key[label]
        # 2: exact display (case-sensitive)
        if label in self._by_display:
            return self._by_display[label]
        # 3: case-insensitive on key + display + aliases
        low = label.lower()
        if low in self._by_lower:
            return self._by_lower[low]
        # 4: fuzzy (length-gated, ambiguity-safe)
        match = self._fuzzy_match(low)
        if match is not None:
            log.info(
                "ost_catalog.fuzzy_match",
                input=label,
                matched_key=match.key,
                matched_display=match.display,
            )
            return match
        # 5: unknown
        log.warning("ost_catalog.unknown_label", label=label)
        return None

    def resolve(self, label: str, backend: Backend) -> str | None:
        """Return the backend-specific category string, or None on miss.

        ``backend='revit'`` → ``entry.ost`` (always present).
        ``backend='aecdm'`` → ``entry.aecdm_label`` (may be None — caller
        should skip the category on AECDM-backed runs).
        """
        cache_key = (label, backend)
        if cache_key in self._cache:
            return self._cache[cache_key]
        entry = self.find(label)
        if entry is None:
            self._cache[cache_key] = None
            return None
        if backend == "revit":
            result: str | None = entry.ost
        elif backend == "aecdm":
            result = entry.aecdm_label
            if result is None:
                log.warning(
                    "ost_catalog.aecdm_not_supported",
                    label=label,
                    matched_key=entry.key,
                    discipline=entry.discipline,
                    note="entry has no aecdm_label; AECDM-backed query will skip this category",
                )
        else:
            raise ValueError(f"Unknown backend: {backend!r}")
        self._cache[cache_key] = result
        return result

    # ---- internals -----------------------------------------------------

    def _fuzzy_match(self, lower_label: str) -> CatalogEntry | None:
        """Bounded Levenshtein over the lowercase index.

        Returns the unique best-match entry, or None if no candidate fits
        within the length-gated distance budget — or if multiple entries
        tie at the same minimum distance (ambiguous → bail).
        """
        n = len(lower_label)
        if n < 5:
            return None
        max_dist = 1 if n < 8 else 2

        # Collect candidates with distance ≤ max_dist.
        candidates: list[tuple[int, CatalogEntry]] = []
        seen_keys: set[str] = set()
        for tag, entry in self._by_lower.items():
            d = _levenshtein(lower_label, tag, max_dist)
            if d is None:
                continue
            # An entry can match via multiple tags (display + alias). Keep
            # only the best (lowest) distance per entry so the ambiguity
            # check below compares entries, not tag rows.
            if entry.key in seen_keys:
                continue
            seen_keys.add(entry.key)
            candidates.append((d, entry))

        if not candidates:
            return None
        candidates.sort(key=lambda kv: kv[0])
        # Ambiguous: two distinct entries tied at the lowest distance.
        if (
            len(candidates) > 1
            and candidates[0][0] == candidates[1][0]
            and candidates[0][1].key != candidates[1][1].key
        ):
            log.warning(
                "ost_catalog.fuzzy_ambiguous",
                input=lower_label,
                candidates=[(e.key, d) for d, e in candidates[:4]],
                note="returning None to avoid silent miscategorisation",
            )
            return None
        return candidates[0][1]


def _levenshtein(a: str, b: str, max_dist: int) -> int | None:
    """Bounded Levenshtein distance. Returns None if distance > ``max_dist``.

    Early-exits via the per-row minimum: once every cell in row ``i``
    exceeds ``max_dist``, no completion of the matrix can produce a
    distance ≤ max_dist. Length-difference shortcut runs first.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return None
    m, n = len(a), len(b)
    # Work along the shorter dimension for the inner row.
    if m < n:
        a, b = b, a
        m, n = n, m
    prev: list[int] = list(range(n + 1))
    for i in range(1, m + 1):
        cur: list[int] = [i] + [0] * n
        row_min = cur[0]
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return None
        prev = cur
    return prev[n]
