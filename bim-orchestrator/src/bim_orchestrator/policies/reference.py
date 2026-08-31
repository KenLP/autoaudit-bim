"""Reference-data loader + deterministic matcher (v1.4-K21, tiers 1–2).

A *reference set* is an authoritative list of allowed values for a parameter —
an approved material palette, a classification catalog, a fixed list of valid
type names. A rule cites the set by name; QC checks membership and (for off-form
but recognisable values) snaps to the one true ``canonical`` entry. This reuses
the v1.4-K12 ``canonical_format`` contract — *compliant iff
``canonicalize(value) == value``; the fix is ``canonicalize(value)``* — where
``canonicalize`` here resolves **membership in the set**.

Matching is layered (see ``ReferenceSet.match``):

  1. **Exact** — the value already equals a ``canonical`` entry → compliant.
  2. **Alias / deterministic** — the value matches a declared ``alias``, OR
     ``_slug``/case-normalises onto a canonical or alias → return the canonical
     (a safe, deterministic auto-fix).
  3. **Fuzzy / semantic** — DEFERRED to Phase 2. A value that is close but not
     deterministically mappable returns ``None`` → the finding routes to a
     Path A ACC Issue (a human picks the right entry). We never guess.

Stores live at ``config/reference.<name>.yaml`` and are loaded once + cached,
exactly like ``ost_catalog.yaml`` — one set is referenced by many rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from bim_orchestrator.policies.normalize import _slug

log = structlog.get_logger(__name__)

# Reference YAMLs live at <repo-root>/config/reference.<name>.yaml.
# parents[0]=policies, [1]=bim_orchestrator, [2]=src, [3]=<repo-root>
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


class ReferenceEntry(BaseModel):
    """One allowed value + its known variants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical: str
    aliases: list[str] = Field(default_factory=list)


class ReferenceSet(BaseModel):
    """An authoritative list of allowed values for a parameter.

    ``match`` is the deterministic (tier 1–2) canonicaliser. Lookups are O(1):
    the constructor builds case/slug-folded indexes once.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    case_sensitive: bool = False
    entries: list[ReferenceEntry]

    # Built indexes (excluded from the model schema).
    _by_key: dict[str, str] = {}
    _by_slug: dict[str, str] = {}

    def model_post_init(self, __context: Any) -> None:
        by_key: dict[str, str] = {}
        by_slug: dict[str, str] = {}
        # First-write wins so a canonical claims its key before any alias does;
        # entries are iterated in declaration order.
        for entry in self.entries:
            for text in (entry.canonical, *entry.aliases):
                by_key.setdefault(self._key(text), entry.canonical)
                by_slug.setdefault(self._slugkey(text), entry.canonical)
        object.__setattr__(self, "_by_key", by_key)
        object.__setattr__(self, "_by_slug", by_slug)

    # ---- key normalisation (respects case_sensitive) -------------------

    def _key(self, text: str) -> str:
        s = text.strip()
        return s if self.case_sensitive else s.casefold()

    def _slugkey(self, text: str) -> str:
        s = _slug(text)
        return s if self.case_sensitive else s.casefold()

    # ---- matching ------------------------------------------------------

    def match(self, value: Any) -> str | None:
        """Tier 1–2 canonicaliser: the matched ``canonical`` entry, or ``None``.

        ``None`` means "not a deterministic member" → the rule treats it as a
        non-compliant, *unfixable* value (Path A). Tier 3 (fuzzy) is Phase 2.
        """
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        # Tier 1 (exact) + tier 2 (alias) share one cased index.
        hit = self._by_key.get(self._key(s))
        if hit is not None:
            return hit
        # Tier 2 (separator/case canonicalisation via slug).
        return self._by_slug.get(self._slugkey(s))


# Cache by resolved path so repeated lookups across a multi-rule RuleSet (and
# across rules sharing one set) don't re-read the file.
_CACHE: dict[Path, ReferenceSet] = {}


def reference_path(name: str, config_dir: Path | str | None = None) -> Path:
    base = Path(config_dir) if config_dir is not None else _DEFAULT_CONFIG_DIR
    return base / f"reference.{name}.yaml"


def load_reference(
    name: str, config_dir: Path | str | None = None
) -> ReferenceSet:
    """Load + cache the reference set ``<config_dir>/reference.<name>.yaml``.

    ``config_dir`` is normally the active rules file's folder (so a self-contained
    rule pack carries its own reference data). When the set isn't there, fall back
    to the shared default ``config/`` dir — so a private pack can reuse the common
    reference sets without copying them.
    """
    path = reference_path(name, config_dir).resolve()
    if config_dir is not None and not path.exists():
        alt = reference_path(name, None).resolve()
        if alt.exists():
            path = alt
    cached = _CACHE.get(path)
    if cached is not None:
        return cached
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Reference set must be a mapping at top level: {path}")
    data.setdefault("name", name)
    ref = ReferenceSet.model_validate(data)
    _CACHE[path] = ref
    log.info("reference.loaded", name=ref.name, entries=len(ref.entries), path=str(path))
    return ref


def clear_cache() -> None:
    """Drop the load cache — for tests that rewrite a reference file."""
    _CACHE.clear()


def normalize_reference(value: Any, ref: ReferenceSet) -> str | None:
    """Deterministic (tier 1–2) canonicalisation against a reference set."""
    return ref.match(value)
