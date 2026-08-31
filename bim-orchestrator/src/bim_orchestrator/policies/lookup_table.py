"""Lookup-table loader — table-driven relational compliance (IBC §716).

A *lookup table* derives a REQUIRED value from one or more values of a related
element, so a rule can express "door rating ≥ TABLE(host wall use, host wall rating)"
instead of a flat comparison. This is the table-driven class of code checks — IBC
§716 opening protectives is the canonical example: the required fire-door rating is
NOT equal to the wall rating but a code-table function of the wall's USE *and*
rating (a 1-hr corridor needs a 20-min door; a 1-hr fire barrier needs 60 min). It
is the relational sibling of ``reference.py``:

  * ``reference`` (K21) = 1-D membership — is one param's value in an approved list?
  * ``lookup``          = derive a required value from N related-element values via
                          a table, then ``relation_compare`` against it.

Shape (generalised to **N keys**):

    keys:
      - { param: host.Fire Rating, dimension: fire_rating }  # matched by minutes
      - { param: host.Fire Function, dimension: string }     # matched case-insensitively
    rows:
      - { when: ["1 HR", "Corridor"], require: "20 min" }     # specific
      - { when: ["1 HR", "*"],        require: "60 min" }     # "*" = wildcard
      - { when: ["2 HR", "*"],        require: "90 min" }

``match(params)`` reads each key's ``param`` from the element's params, finds the
FIRST row whose ``when`` matches every key position (``"*"`` matches anything;
``fire_rating`` keys match by :func:`parse_to_minutes` so "2 HR" == "120 min";
``string`` keys match case-insensitively), and returns ``(required, exempt)``:

  * ``exempt=True``  — a ``fire_rating`` key carries an EXPLICIT not-rated sentinel
    ("NR", "-", "0 hr"): the code imposes no requirement → caller treats the
    element as compliant. Only a written declaration earns this — see ``match``.
  * ``required=None`` (not exempt) — no row matched, or the key is blank/unreadable:
    caller routes to manual review; we never guess.
  * ``required=<str>`` — the required value to ``relation_compare`` against.

Stores live at ``config/lookup.<name>.yaml`` and are loaded once + cached, exactly
like ``ost_catalog`` / ``reference`` — one table is referenced by many rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bim_orchestrator.policies.fire_rating_units import is_not_rated, parse_to_minutes

log = structlog.get_logger(__name__)

# Tables live at <repo-root>/config/lookup.<name>.yaml.
# parents[0]=policies, [1]=bim_orchestrator, [2]=src, [3]=<repo-root>
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

WILDCARD = "*"


class LookupKey(BaseModel):
    """One key column: the element param to read + how to match it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    param: str
    dimension: Literal["fire_rating", "string"] = "string"


class LookupRow(BaseModel):
    """One row: ``when`` (one value per key, ``"*"`` = any) → ``require``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    when: list[str]
    require: str

    @field_validator("when", mode="before")
    @classmethod
    def _scalar_to_list(cls, v: Any) -> Any:
        # Allow a single-key table to write `when: "2 HR"` (scalar) for brevity.
        return [v] if isinstance(v, str) else v


class LookupTable(BaseModel):
    """An N-key table deriving a required value from a related element's values."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    keys: list[LookupKey] = Field(default_factory=list)
    rows: list[LookupRow] = Field(default_factory=list)

    @property
    def host_params(self) -> list[str]:
        """Key params that read from the host (``host.<name>``), prefix stripped —
        so the query layer knows what to hydrate on the host hop."""
        return [k.param[len("host."):] for k in self.keys if k.param.startswith("host.")]

    @property
    def own_params(self) -> list[str]:
        """Key params read from the element ITSELF (no ``host.`` prefix).

        The complement of :attr:`host_params`, and just as load-bearing: the
        query layer fetches only the params the rules ask for, so a table
        keyed on the element's own param (a room's ``Name``, a duct's
        ``System Type``) needs that name added to the spec or the key arrives
        as ``None`` and NO row can ever match. Missed until 2026-08-18
        because every shipped table keyed on the host (IBC §716) — the first
        own-key table (IRC room minimums) sent all 30 rooms to manual_review
        with an empty operand."""
        return [k.param for k in self.keys if not k.param.startswith("host.")]

    def _cell_matches(self, when: str, key: LookupKey, value: Any) -> bool:
        if when == WILDCARD:
            return True
        if key.dimension == "fire_rating":
            return parse_to_minutes(when) == parse_to_minutes(value)
        # L-03: `str(value or "")` folds every FALSY value to "" — so a real
        # `0` / `0.0` / `False` in the model matched a row written for a blank
        # cell, and missed the row that actually says `when: "0"`. A code table
        # keyed on an occupancy count or a boolean flag would silently resolve
        # to the wrong requirement. Same "0 is a value, not a missing reading"
        # rule the geometry path already learned (`_clearance_actual_mm`).
        text = "" if value is None else str(value)
        return text.strip().casefold() == when.strip().casefold()

    def match(self, params: dict[str, Any]) -> tuple[str | None, bool]:
        """Resolve the required value for an element. Returns ``(required, exempt)``.

        ``exempt`` is True ONLY when a ``fire_rating`` key carries an explicit
        not-rated sentinel. ``required`` is None (not exempt) when no row
        matches, or the key is blank/unreadable → caller: manual review.

        The ``fire_rating`` key has THREE distinct value shapes (M5), and B-4
        (review round 7, 2026-08-16) moved the blank shape OUT of exempt:
          * blank/None (no value present) → ``(None, False)`` → manual_review.
            "Not recorded" is NOT "no requirement": most real wall types ship
            with a blank Fire Rating, and reading that blank as an exemption
            silently certified every door in them compliant — the exact
            false-negative class the M5 junk-value fix closed for unparseable
            values, one shape over. If a wall truly has no requirement, the
            model should SAY so ("NR") — that written declaration is cheap and
            auditable; an inferred one is neither.
          * an explicit not-rated sentinel ("NR", "-", …, ``parse_to_minutes``
            == 0) → EXEMPT — the code imposes no requirement on an unrated host.
          * a NON-BLANK value that ``parse_to_minutes`` can't parse (junk like
            "2 HR (UL U419)", or a unit-less "2" — ambiguous between hours and
            minutes since B-4) → ``(None, False)`` → manual_review. This host
            may well carry a rating; we just can't read it without guessing.
        """
        values = [params.get(k.param) for k in self.keys]
        for key, value in zip(self.keys, values):
            if key.dimension != "fire_rating":
                continue
            blank = value is None or (isinstance(value, str) and not value.strip())
            if blank:
                return None, False  # B-4: not recorded ≠ no requirement → manual_review
            # L-03: ONE definition of the sentinel, shared with the QC
            # canonical_format path (which used to flag "NR" as a format
            # violation). is_not_rated() is False for blank AND unparseable,
            # so only a written declaration reaches the exempt branch.
            if is_not_rated(value):
                return None, True  # explicit not-rated sentinel ("NR", "-", ...)
            if parse_to_minutes(value) is None:
                return None, False  # non-blank but unparseable → manual_review
        for row in self.rows:
            if len(row.when) != len(self.keys):
                continue
            if all(
                self._cell_matches(w, k, v)
                for w, k, v in zip(row.when, self.keys, values)
            ):
                return row.require, False
        return None, False


# Cache by resolved path — repeated lookups across a multi-rule run don't re-read.
_CACHE: dict[Path, LookupTable] = {}


def lookup_path(name: str, config_dir: Path | str | None = None) -> Path:
    base = Path(config_dir) if config_dir is not None else _DEFAULT_CONFIG_DIR
    return base / f"lookup.{name}.yaml"


def load_lookup(name: str, config_dir: Path | str | None = None) -> LookupTable:
    """Load + cache the table ``<config_dir>/lookup.<name>.yaml``.

    Like :func:`reference.load_reference`: ``config_dir`` is the active rules file's
    folder (a self-contained rule pack can carry its own tables); when the table
    isn't there, fall back to the shared default ``config/`` dir so a private pack
    can cite the common code tables (IBC §716, etc.) without copying them.
    """
    path = lookup_path(name, config_dir).resolve()
    if config_dir is not None and not path.exists():
        alt = lookup_path(name, None).resolve()
        if alt.exists():
            path = alt
    cached = _CACHE.get(path)
    if cached is not None:
        return cached
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Lookup table must be a mapping at top level: {path}")
    data.setdefault("name", name)
    table = LookupTable.model_validate(data)
    _CACHE[path] = table
    log.info("lookup_table.loaded", name=table.name, keys=len(table.keys),
             rows=len(table.rows), path=str(path))
    return table


def clear_cache() -> None:
    """Drop the load cache — for tests that rewrite a table file."""
    _CACHE.clear()
