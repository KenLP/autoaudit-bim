"""v1.5-R7 (R1-Stage 2): tests for the static rules-lint.

Scenarios per docs/specs/SPEC_R1LITE_R3SEED.md W2 item 4:
  (a) 2 independent rules -> clean + order of 2
  (b) A writes X, B reads X via compose_template -> 1 critical-pair warning, order A before B
  (c) 2 rules write the same param/category -> write-write ERROR
  (d) A and B read/write each other -> cycle ERROR
  (e) normalize self-loop -> NOT an error (idempotent)
  (f) bound_parameter rule -> footprint uses the BOUND name
  (g) llm_propose rule -> unanalyzable warning
  (h) CLI: 3 exit-code branches (clean / warning-not-strict / error)
"""

from __future__ import annotations

import sys

from bim_orchestrator import orchestrator
from bim_orchestrator.policies.rules_lint import ParamRef, extract_footprint, lint
from bim_orchestrator.policies.rules_schema import Rule, RuleSet


def _rule(**overrides) -> Rule:
    base = {
        "id": "r",
        "parameter": "P",
        "requirement": "present_and_nonempty",
        "severity_tag": "missing_required_param",
        "description": "test",
        "fixability": "manual",
        "autofill": {"strategy": "none"},
    }
    base.update(overrides)
    return Rule.model_validate(base)


def _ruleset(*rules: Rule, target_category: str = "Rooms") -> RuleSet:
    return RuleSet(scenario="test", target_category=target_category, rules=list(rules))


# ---- (a) independent rules --------------------------------------------------


def test_two_independent_rules_are_clean_with_full_order():
    r1 = _rule(id="r1", parameter="P1")
    r2 = _rule(id="r2", parameter="P2")
    report = lint(_ruleset(r1, r2))
    assert report.errors == []
    assert report.warnings == []
    assert report.order is not None
    assert set(report.order) == {"r1", "r2"}


# ---- (b) one-directional critical pair --------------------------------------


def test_write_then_read_is_one_critical_pair_warning_ordered():
    rule_a = _rule(
        id="a", parameter="Mark", requirement="canonical_format", fixability="auto",
        autofill={"strategy": "normalize", "normalize_kind": "auto"},
        remediation={"action": "set_parameter"},
    )
    rule_b = _rule(
        id="b", parameter="Description", requirement="present_and_nonempty",
        fixability="auto",
        autofill={"strategy": "compose_template", "template": "{Mark}-suffix"},
        remediation={"action": "set_parameter"},
    )
    report = lint(_ruleset(rule_a, rule_b))
    assert report.errors == []
    assert len(report.warnings) == 1
    w = report.warnings[0]
    assert w["type"] == "critical_pair"
    assert set(w["rules"]) == {"a", "b"}
    assert "Mark" in w["params"]
    assert report.order is not None
    assert report.order.index("a") < report.order.index("b")


# ---- (c) write-write conflict -----------------------------------------------


def test_two_rules_writing_same_param_same_category_is_write_write_error():
    rule_a = _rule(
        id="a", parameter="Fire Rating", requirement="canonical_format", fixability="auto",
        autofill={"strategy": "normalize", "normalize_kind": "fire_rating"},
        remediation={"action": "set_parameter"},
    )
    rule_b = _rule(
        id="b", parameter="Fire Rating", requirement="canonical_format", fixability="auto",
        autofill={"strategy": "inherit_from_host"},
        remediation={"action": "set_parameter"},
    )
    report = lint(_ruleset(rule_a, rule_b))
    conflicts = [e for e in report.errors if e["type"] == "write_write_conflict"]
    assert len(conflicts) == 1
    assert set(conflicts[0]["rules"]) == {"a", "b"}
    assert conflicts[0]["params"] == ["Fire Rating"]


# ---- (d) mutual read/write cycle --------------------------------------------


def test_mutual_read_write_is_a_cycle_error():
    rule_a = _rule(
        id="a", parameter="P1", other_param="P2",
        requirement="relation_compare", compare_kind="numeric", operator=">=",
        fixability="auto",
        autofill={"strategy": "normalize", "normalize_kind": "auto"},
        remediation={"action": "set_parameter"},
    )
    rule_b = _rule(
        id="b", parameter="P2", other_param="P1",
        requirement="relation_compare", compare_kind="numeric", operator=">=",
        fixability="auto",
        autofill={"strategy": "normalize", "normalize_kind": "auto"},
        remediation={"action": "set_parameter"},
    )
    report = lint(_ruleset(rule_a, rule_b))
    cycles = [e for e in report.errors if e["type"] == "cycle"]
    assert len(cycles) == 1
    assert set(cycles[0]["rules"]) >= {"a", "b"}
    assert report.order is None
    # no write-write conflict here — different params (P1 vs P2)
    assert [e for e in report.errors if e["type"] == "write_write_conflict"] == []


# ---- (e) idempotent self-loop -----------------------------------------------


def test_normalize_self_loop_is_not_an_error():
    rule_e = _rule(
        id="e", parameter="Mark", requirement="canonical_format", fixability="auto",
        autofill={"strategy": "normalize", "normalize_kind": "auto"},
        remediation={"action": "set_parameter"},
    )
    report = lint(_ruleset(rule_e))
    assert report.errors == []
    assert report.order == ["e"]


def test_inherit_self_loop_is_condition_eliminating_not_an_error():
    """2026-07-12 refinement (supersedes the initial conservative stance):
    inherit_* SELF-loops are exempt because the fix is CONDITION-ELIMINATING —
    K9 + the M3 fullmatch guard validate every suggested value through the
    SAME requirement that flagged it, so a landed fix makes its own rule's
    condition false and the self-edge fires at most once per element (AWH
    'discharge the SCC with a well-founded measure'). The REAL inherit hazard
    (host value must be final first) is a CROSS-rule edge and stays flagged —
    see test below."""
    rule = _rule(
        id="inh", parameter="Fire Rating", requirement="canonical_format",
        fixability="auto",
        autofill={"strategy": "inherit_from_host"},
        remediation={"action": "set_parameter"},
    )
    report = lint(_ruleset(rule))
    assert [e for e in report.errors if e["type"] == "cycle"] == []
    assert report.order == ["inh"]


def test_inherit_cross_rule_host_dependency_still_flagged():
    """The exemption above must NOT hide the ordering hazard: a rule that
    WRITES the param another rule reads via ``host.<p>`` is still a
    critical-pair / ordering edge."""
    wall_rule = _rule(
        id="wall_fr", parameter="Fire Rating", requirement="canonical_format",
        fixability="auto", category="Walls",
        autofill={"strategy": "normalize", "normalize_kind": "fire_rating"},
        remediation={"action": "set_parameter"},
    )
    door_rule = _rule(
        id="door_inh", parameter="Fire Rating", requirement="present_and_nonempty",
        fixability="auto", category="Doors",
        autofill={"strategy": "inherit_from_host"},
        remediation={"action": "set_parameter"},
    )
    report = lint(_ruleset(wall_rule, door_rule))
    # wall_fr writes (Walls, Fire Rating); door_inh reads host.Fire Rating
    # (wildcard category) → dependency edge → order wall before door, and/or
    # a critical-pair warning. Either signal is acceptable; silence is not.
    flagged = [w for w in report.warnings if "wall_fr" in str(w) and "door_inh" in str(w)]
    ordered = report.order is not None and report.order.index("wall_fr") < report.order.index("door_inh")
    assert flagged or ordered


# ---- (f) bound_parameter pins fetch_name ------------------------------------


def test_bound_parameter_footprint_uses_bound_name():
    rule = _rule(
        id="f", parameter="Fire Rating", bound_parameter="FR_Custom",
        requirement="canonical_format", fixability="auto",
        autofill={"strategy": "normalize", "normalize_kind": "fire_rating"},
        remediation={"action": "set_parameter"},
    )
    fp = extract_footprint(rule, _ruleset(rule))
    read_params = {r.param for r in fp.reads}
    write_params = {w.param for w in fp.writes}
    assert "FR_Custom" in read_params
    assert "FR_Custom" in write_params
    assert "Fire Rating" not in read_params
    assert "Fire Rating" not in write_params


# ---- (g) llm_propose is unanalyzable ----------------------------------------


def test_llm_propose_rule_is_unanalyzable_warning():
    rule = _rule(
        id="g", parameter="Naming", requirement="matches_regex", pattern="^ADSK_",
        fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "llm_propose"},
    )
    fp = extract_footprint(rule, _ruleset(rule))
    assert fp.analyzable is False

    report = lint(_ruleset(rule))
    unanalyzable = [w for w in report.warnings if w["type"] == "unanalyzable"]
    assert len(unanalyzable) == 1
    assert unanalyzable[0]["rule"] == "g"


# ---- B-1 (review round 7): value strategies that bypass autofill still WRITE


def test_next_available_rule_writes_its_target():
    """B-1: `next_available` originates the value in remediation, with
    `autofill.strategy: none` — keying has_fix on the autofill alone gave this
    rule an EMPTY writes set while staying analyzable=True (the reviewed
    probe: `demo.rooms.number_unique → writes: RỖNG, analyzable: True`)."""
    rule = _rule(
        id="nx", parameter="Number", requirement="unique_in_set", fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "next_available"},
    )
    fp = extract_footprint(rule, _ruleset(rule))
    assert fp.analyzable is True
    assert ParamRef("Rooms", "Number") in fp.writes


def test_fixed_strategy_rule_writes_its_target():
    rule = _rule(
        id="fx", parameter="Department", requirement="present_and_nonempty",
        fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "fixed",
                     "new_value": "General"},
    )
    fp = extract_footprint(rule, _ruleset(rule))
    assert ParamRef("Rooms", "Department") in fp.writes


def test_two_next_available_rules_same_param_is_write_write_error():
    """The blind spot's payoff case: two rules renumbering the same
    Rooms.Number used to lint 'clean' — the exact write-write conflict the
    lint exists to catch."""
    r1 = _rule(
        id="n1", parameter="Number", requirement="unique_in_set", fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "next_available"},
    )
    r2 = _rule(
        id="n2", parameter="Number", requirement="matches_regex", pattern=r"^\d+$",
        fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "next_available"},
    )
    report = lint(_ruleset(r1, r2))
    ww = [e for e in report.errors if e["type"] == "write_write_conflict"]
    assert len(ww) == 1
    assert set(ww[0]["rules"]) == {"n1", "n2"}


def test_next_available_self_loop_is_condition_eliminating_not_a_cycle():
    """The write B-1 added creates a read→write self-edge (unique_in_set reads
    the very param next_available writes). That self-loop is DISCHARGED — a
    landed renumber makes its own condition false and cannot mint a new
    duplicate — so a lone renumber rule must lint clean with a full order,
    not explode into a cycle error the moment its write became visible."""
    rule = _rule(
        id="nx", parameter="Number", requirement="unique_in_set", fixability="auto",
        autofill={"strategy": "none"},
        remediation={"action": "set_parameter", "new_value_strategy": "next_available"},
    )
    report = lint(_ruleset(rule))
    assert report.errors == []
    assert report.order == ["nx"]


def test_shipped_demo_number_unique_footprint_has_the_write():
    """Pin the fix against the SHIPPED config the review probed, so the
    footprint can't silently regress to empty for the demo's flagship rule."""
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[1] / "config" / "rules.demo.yaml"
    ruleset = RuleSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    by_id = {r.id: r for r in ruleset.rules}
    rule = by_id["demo.rooms.number_unique"]
    fp = extract_footprint(rule, ruleset)
    assert fp.analyzable is True
    assert any(w.param == "Number" for w in fp.writes), (
        "demo.rooms.number_unique writes Rooms.Number via next_available — "
        "an empty writes set here is the exact B-1 blind spot"
    )


# ---- manual rule with autofill never counted as a write ---------------------


def test_manual_fixability_rule_produces_no_write():
    """A manual rule's autofill only fills Finding.suggested_value for
    DISPLAY — DesignAgent never writes it (fixability != auto), so it must
    not appear as a WRITE in the footprint (design.py:_partition)."""
    rule = _rule(
        id="m", parameter="Mark", requirement="canonical_format", fixability="manual",
        autofill={"strategy": "normalize", "normalize_kind": "auto"},
        remediation={"action": "set_parameter"},
    )
    fp = extract_footprint(rule, _ruleset(rule))
    assert fp.writes == set()
    assert ParamRef("Rooms", "Mark") in fp.reads


# ---- (h) CLI exit codes ------------------------------------------------------


class TestLintRulesCLI:
    def _run(self, monkeypatch, argv, *, cwd):
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(orchestrator, "configure_logging", lambda **kwargs: None)
        monkeypatch.chdir(cwd)  # rules_lint.json is written to cwd — keep it out of the repo
        return orchestrator.main()

    def test_clean_ruleset_exits_0(self, tmp_path, monkeypatch, capsys):
        rules_path = tmp_path / "rules.yaml"
        rules_path.write_text(
            "scenario: t\ntarget_category: Rooms\nrules:\n"
            "  - id: r1\n    parameter: P1\n    requirement: present_and_nonempty\n"
            "    severity_tag: missing_required_param\n    description: d\n"
            "    autofill: {strategy: none}\n",
            encoding="utf-8",
        )
        rc = self._run(
            monkeypatch,
            ["bim-orchestrator", "--lint-rules", "--rules", str(rules_path)],
            cwd=tmp_path,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "ERRORS (0)" in out
        assert (tmp_path / "rules_lint.json").exists()

    def test_warning_only_exits_0_without_strict_1_with_strict(self, tmp_path, monkeypatch):
        rules_path = tmp_path / "rules.yaml"
        rules_path.write_text(
            "scenario: t\ntarget_category: Rooms\nrules:\n"
            "  - id: a\n    parameter: Mark\n    requirement: canonical_format\n"
            "    severity_tag: naming_violation\n    description: d\n"
            "    fixability: auto\n"
            "    autofill: {strategy: normalize, normalize_kind: auto}\n"
            "    remediation: {action: set_parameter}\n"
            "  - id: b\n    parameter: Description\n    requirement: present_and_nonempty\n"
            "    severity_tag: missing_required_param\n    description: d\n"
            "    fixability: auto\n"
            "    autofill: {strategy: compose_template, template: '{Mark}-suffix'}\n"
            "    remediation: {action: set_parameter}\n",
            encoding="utf-8",
        )
        rc = self._run(
            monkeypatch,
            ["bim-orchestrator", "--lint-rules", "--rules", str(rules_path)],
            cwd=tmp_path,
        )
        assert rc == 0
        rc_strict = self._run(
            monkeypatch,
            ["bim-orchestrator", "--lint-rules", "--strict", "--rules", str(rules_path)],
            cwd=tmp_path,
        )
        assert rc_strict == 1

    def test_error_ruleset_exits_1(self, tmp_path, monkeypatch):
        rules_path = tmp_path / "rules.yaml"
        rules_path.write_text(
            "scenario: t\ntarget_category: Rooms\nrules:\n"
            "  - id: a\n    parameter: Fire Rating\n    requirement: canonical_format\n"
            "    severity_tag: naming_violation\n    description: d\n"
            "    fixability: auto\n"
            "    autofill: {strategy: normalize, normalize_kind: fire_rating}\n"
            "    remediation: {action: set_parameter}\n"
            "  - id: b\n    parameter: Fire Rating\n    requirement: canonical_format\n"
            "    severity_tag: naming_violation\n    description: d\n"
            "    fixability: auto\n"
            "    autofill: {strategy: inherit_from_host}\n"
            "    remediation: {action: set_parameter}\n",
            encoding="utf-8",
        )
        rc = self._run(
            monkeypatch,
            ["bim-orchestrator", "--lint-rules", "--rules", str(rules_path)],
            cwd=tmp_path,
        )
        assert rc == 1


# ---- (h) exhaustiveness: every autofill strategy is accounted for -----------


def test_every_autofill_strategy_is_handled_by_extract_footprint():
    """L-13 — the blind spot CLAUDE.md warns about, turned into a failing test.

    `extract_footprint` mirrors the read/write semantics of the engine by
    hand. A strategy added to the `AutofillStrategy` Literal but not mirrored
    here does NOT fail loudly: the rule stays "analyzable" and simply reports
    an incomplete footprint, so rules-lint keeps declaring the rule set safe
    while silently missing whatever the new strategy reads. Rules-lint's whole
    job is to catch conflicts between rules — a missing read is exactly the
    conflict it would fail to see.

    So the strategies are enumerated here, split by whether they widen the
    READ set. Adding a member to the Literal breaks this test, which is the
    moment to decide which side it belongs on (and to mirror it in
    `extract_footprint` if it reads anything).
    """
    from typing import get_args

    from bim_orchestrator.policies.rules_schema import AutofillStrategy

    # Strategies whose value derives from ANOTHER parameter — extract_footprint
    # must add that parameter to `reads` or the lint is blind to the dependency.
    reads_other_params = {
        "inherit_from_host",       # reads host.<host_param or own param>
        "inherit_then_normalize",  # same, then normalises
        "compose_template",        # reads every {token} in the template
        "infer_from_room_name",    # reads the element's NAME (`__name__`)
    }
    # Strategies that derive the value from the element's OWN parameter (or a
    # fixed literal), so the rule's own read is already in the footprint.
    own_param_only = {
        "normalize",               # canonicalises the value in place
        "infer_from_adjacent",     # heuristic; no statically-known param
        "none",
    }

    declared = set(get_args(AutofillStrategy))
    accounted = reads_other_params | own_param_only
    assert declared == accounted, (
        "AutofillStrategy changed — decide which side the new member is on and "
        "mirror it in policies/rules_lint.extract_footprint if it reads another "
        f"parameter. declared-not-accounted={sorted(declared - accounted)}, "
        f"accounted-not-declared={sorted(accounted - declared)}"
    )

    # And the read-widening ones must genuinely widen it (both ends pinned).
    for strategy in sorted(reads_other_params):
        autofill: dict = {"strategy": strategy}
        expected = "Other Param"
        if strategy == "compose_template":
            autofill["template"] = "{Other Param}-{seq}"
        elif strategy == "infer_from_room_name":
            expected = "__name__"      # the element's name slot, not a param
        else:
            autofill["host_param"] = "Other Param"
        rule = _rule(id=f"r.{strategy}", parameter="P", autofill=autofill)
        fp = extract_footprint(rule, _ruleset(rule))
        assert any(ref.param == expected for ref in fp.reads), (
            f"{strategy} derives its value from another parameter, but "
            "extract_footprint did not record that read — rules-lint cannot "
            "see conflicts it does not know about"
        )
