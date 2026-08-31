"""Tests for the lookup-table loader (policies/lookup_table.py) — IBC §716, N-key.

Exercises the real shipped ``config/lookup.ibc716.yaml`` (2-key: wall rating × wall
use) so the test doubles as a contract check on the table, plus the loader mechanics.
``match(params)`` returns ``(required, exempt)``.
"""

from __future__ import annotations

import yaml

from bim_orchestrator.policies import lookup_table as lt
from bim_orchestrator.policies.lookup_table import load_lookup


def _p(rating=None, use=None):
    params = {}
    if rating is not None:
        params["host.Fire Rating"] = rating
    if use is not None:
        params["host.Fire Function"] = use
    return params


def test_loads_real_ibc716_keys():
    lt.clear_cache()
    t = load_lookup("ibc716")
    assert t.name == "ibc716"
    assert [k.param for k in t.keys] == ["host.Fire Rating", "host.Fire Function"]
    assert t.host_params == ["Fire Rating", "Fire Function"]


def test_corridor_vs_barrier_same_rating():
    # The 2-key win: a 1-hr CORRIDOR needs a 20-min door; a 1-hr fire BARRIER 60 min.
    t = load_lookup("ibc716")
    assert t.match(_p("1 HR", "Corridor")) == ("20 min", False)
    assert t.match(_p("1 HR", "Fire Barrier")) == ("60 min", False)   # via "*"


def test_non_corridor_fire_partition_needs_45_not_20():
    # B-5 (review round 7): the "1 HR x Fire Partition" row shipped as 20 min —
    # that is the CORRIDOR allowance. A non-corridor 1-hr fire partition
    # (dwelling-unit separation) requires a 3/4-hr door per IBC 2018 Table 716.1(2).
    t = load_lookup("ibc716")
    assert t.match(_p("1 HR", "Fire Partition")) == ("45 min", False)


def test_heaviest_fire_wall_has_a_row():
    # B-5: "4 HR" previously fell through to manual review — the HEAVIEST wall
    # was the one the table couldn't answer. IBC: a 4-hr fire wall takes a 3-hr door.
    t = load_lookup("ibc716")
    assert t.match(_p("4 HR", "Fire Barrier")) == ("3 HR", False)
    assert t.match(_p("240 min", "Shaft")) == ("3 HR", False)  # minutes-form matches too


def test_missing_use_falls_to_barrier_default():
    # No Fire Function on the wall → None → only "*" rows match → stricter default.
    t = load_lookup("ibc716")
    assert t.match(_p("1 HR")) == ("60 min", False)


def test_high_rating_ignores_use():
    t = load_lookup("ibc716")
    assert t.match(_p("2 HR", "Corridor")) == ("90 min", False)
    assert t.match(_p("3 HR", "Fire Barrier")) == ("3 HR", False)


def test_minute_normalized_key():
    t = load_lookup("ibc716")
    # "120 min" == "2 HR" on the fire_rating key.
    assert t.match(_p("120 min", "Shaft")) == ("90 min", False)


def test_explicit_not_rated_sentinel_is_exempt():
    # Only a WRITTEN declaration of no-rating earns the exemption (B-4).
    t = load_lookup("ibc716")
    assert t.match(_p("NR", "Corridor")) == (None, True)
    assert t.match(_p("0 hr", "Corridor")) == (None, True)


def test_blank_or_absent_rating_is_manual_review_not_exempt():
    """B-4 (review round 7, 2026-08-16): blank ≠ "no requirement". Most real
    wall types ship with a blank Fire Rating; reading the blank as an
    exemption silently certified every door in them compliant — the same
    false-negative class M5 closed for junk values, one shape over. A wall
    with genuinely no requirement should SAY so ("NR")."""
    t = load_lookup("ibc716")
    assert t.match(_p(None)) == (None, False)
    assert t.match({}) == (None, False)
    assert t.match(_p("   ", "Corridor")) == (None, False)


def test_unitless_rating_is_manual_review_not_a_guess():
    # B-4: a wall typed "2" means 2 HOURS to its author; the old bare=minutes
    # parse read 2 MINUTES, which any door satisfies. Now unreadable → manual.
    t = load_lookup("ibc716")
    assert t.match(_p("2", "Corridor")) == (None, False)
    assert t.match(_p("0.5", "Corridor")) == (None, False)


def test_junk_unparseable_rating_is_not_exempt():
    """M5: a NON-BLANK rating that ``parse_to_minutes`` can't parse (garbage
    like a UL listing suffix) must NOT be classified exempt (compliant by
    default) — that was a false negative right in the §716 path (an
    under-spec door behind an unreadable-but-real rating silently passed).
    It must resolve unresolved (None, False) so QC routes it to manual_review,
    matching the existing "rated wall, rating not in table" contract."""
    t = load_lookup("ibc716")
    assert t.match(_p("2 HR (UL U419)", "Corridor")) == (None, False)
    assert t.match(_p("garbage value", "Fire Barrier")) == (None, False)


def test_unknown_rating_unresolved():
    t = load_lookup("ibc716")
    # rated wall, rating not in any row → (None, not-exempt) → caller: manual review.
    # (was "4 HR" until B-5 gave 4 HR its own row — 2.5 HR is not an IBC wall rating)
    assert t.match(_p("2.5 HR", "Fire Barrier")) == (None, False)


def test_cached_same_object():
    assert load_lookup("ibc716") is load_lookup("ibc716")


def test_falls_back_to_shared_config_dir(tmp_path):
    # A rule-pack folder WITHOUT the table → load falls back to the shared config/.
    lt.clear_cache()
    t = load_lookup("ibc716", tmp_path)        # not in tmp_path → config/ fallback
    assert t.name == "ibc716" and t.keys        # the real shared table loaded
    lt.clear_cache()


def test_pack_local_table_wins_over_config(tmp_path):
    # A table present in the pack folder is used (not the config/ fallback).
    lt.clear_cache()
    (tmp_path / "lookup.packonly.yaml").write_text(yaml.safe_dump({
        "name": "packonly",
        "keys": [{"param": "host.X", "dimension": "string"}],
        "rows": [{"when": ["a"], "require": "1"}],
    }), encoding="utf-8")
    t = load_lookup("packonly", tmp_path)
    assert t.match({"host.X": "a"}) == ("1", False)
    lt.clear_cache()


def test_scalar_when_for_single_key(tmp_path):
    # A 1-key table may write `when: "2 HR"` (scalar) — coerced to a 1-list.
    lt.clear_cache()
    p = tmp_path / "lookup.solo.yaml"
    p.write_text(yaml.safe_dump({
        "name": "solo",
        "keys": [{"param": "host.Fire Rating", "dimension": "fire_rating"}],
        "rows": [{"when": "2 HR", "require": "90 min"}],
    }), encoding="utf-8")
    t = load_lookup("solo", tmp_path)
    assert t.match({"host.Fire Rating": "2 HR"}) == ("90 min", False)
    lt.clear_cache()


# --- L-03: a falsy value is a VALUE, not a blank cell ------------------------


def _numeric_table() -> lt.LookupTable:
    """A code table keyed on a plain (non-fire-rating) cell, with a row for
    zero and a row for "no value recorded" — the shape that exposed L-03."""
    return lt.LookupTable(
        name="occupancy",
        keys=[lt.LookupKey(param="Occupant Count", dimension="string")],
        rows=[
            lt.LookupRow(when=["0"], require="ZERO-ROW"),
            lt.LookupRow(when=[""], require="BLANK-ROW"),
        ],
    )


def test_zero_matches_the_zero_row_not_the_blank_row():
    """`str(value or "")` folded 0 to "" — so an element that genuinely
    records zero resolved to the requirement written for elements that record
    NOTHING. Same "0 is a value, not a missing reading" rule the geometry path
    learned in `_clearance_actual_mm`."""
    t = _numeric_table()
    assert t.match({"Occupant Count": 0}) == ("ZERO-ROW", False)
    assert t.match({"Occupant Count": "0"}) == ("ZERO-ROW", False)


def test_only_a_real_absence_matches_the_blank_row():
    t = _numeric_table()
    assert t.match({"Occupant Count": None}) == ("BLANK-ROW", False)
    assert t.match({}) == ("BLANK-ROW", False)


def test_false_is_a_value_too():
    t = lt.LookupTable(
        name="flagged",
        keys=[lt.LookupKey(param="Is Rated", dimension="string")],
        rows=[
            lt.LookupRow(when=["False"], require="NOT-RATED-ROW"),
            lt.LookupRow(when=[""], require="BLANK-ROW"),
        ],
    )
    assert t.match({"Is Rated": False}) == ("NOT-RATED-ROW", False)


def test_an_unmatched_numeric_shape_is_manual_review_not_a_blank_match():
    """0.0 renders as "0.0", which no row declares — so the honest answer is
    "no row matched" (the caller routes it to manual review), NOT the blank
    row it used to silently fall into."""
    t = _numeric_table()
    assert t.match({"Occupant Count": 0.0}) == (None, False)
