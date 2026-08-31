"""Declaration-sync: the public AU capability catalog may not drift from code.

``docs/RULE_CAPABILITY_CATALOG.md`` is shared OUTSIDE the team, and it has now
drifted after a policy change twice: it kept saying fire ratings collapse to
the "minimum" a week after the owner flipped the reducer to MAX (audit finding
F-04), and it promised "never a silent write" while nothing in the code
enforced it (F-02). Both fixes were prose edits — nothing stopped a third
drift. These tests do (the audit's "khóa máy" recommendation,
docs/audits/AUDIT_2026-08-01_rule-capability-catalog.md).

The point is NOT to freeze the wording. Every assertion here pins a sentence
to the code fact it advertises; rewording that keeps the fact is a one-line
test update made consciously, which is exactly the moment a human should be
looking at both ends.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from bim_orchestrator.policies.ost_catalog import OSTCatalog
from bim_orchestrator.policies.rules_schema import (
    AutofillStrategy,
    Requirement,
    Rule,
)

# tests/ -> bim-orchestrator/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_MD = _REPO_ROOT / "docs" / "RULE_CAPABILITY_CATALOG.md"
_OST_CATALOG_YAML = Path(__file__).resolve().parents[1] / "config" / "ost_catalog.yaml"


@pytest.fixture(scope="module")
def catalog_text() -> str:
    return _CATALOG_MD.read_text(encoding="utf-8")


class TestFireRatingReducerSentence:
    """F-04 — the sentence that shipped wrong in the dangerous direction.

    The reducer picks the MAXIMUM candidate (owner decision 2026-07-25:
    the parameter records the rating the code REQUIRES, so the minimum
    leaves higher-requirement elements declaring less than their own host
    demands). The catalog said "minimum" for a week after the flip.
    """

    def test_the_catalog_says_maximum(self, catalog_text: str) -> None:
        assert "collapses to the **maximum**" in catalog_text, (
            "the conflicting-hosts sentence no longer states the MAX policy — "
            "if it was reworded, re-pin the new wording here; if it says "
            "minimum again, that is the F-04 regression this test exists for"
        )

    def test_no_prescriptive_minimum_anywhere(self, catalog_text: str) -> None:
        """`minimum` may appear only when arguing AGAINST it. A prescriptive
        "collapse(s) to the minimum" is the exact regression."""
        assert not re.search(r"collapses? to the (\*\*)?minimum", catalog_text)

    def test_the_code_actually_picks_max(self) -> None:
        """Both ends: the sentence above is only honest while the reducer
        really is `max`. The full behavioural pin (including the rendered
        proposal body) lives in test_design_agent_path_b — this is the
        cheap tripwire that links the DOC test to the code fact."""
        import inspect

        from bim_orchestrator.agents.design import DesignAgent

        src = inspect.getsource(DesignAgent._collapse_to_one)
        assert "max(rateable" in src
        assert "min(rateable" not in src


class TestSupportedCategories:
    """FC2 — every category the catalog advertises must resolve through the
    REAL resolver (`OSTCatalog.find`), the same lookup a rule author's
    ``Rule.category`` goes through. Textual presence in the YAML is not
    enough: an entry could be renamed and a stale alias would still grep."""

    def test_every_advertised_category_resolves(self, catalog_text: str) -> None:
        m = re.search(
            r"\*\*Supported categories\*\*[^:]*:\s*(.+?)\.\s", catalog_text, re.S
        )
        assert m, "the 'Supported categories' block disappeared or was reshaped"
        names = [n.strip() for n in m.group(1).replace("\n", " ").split("·")]
        names = [re.sub(r"\s+", " ", n) for n in names if n.strip()]
        assert len(names) >= 10, f"suspiciously short category list: {names}"

        cat = OSTCatalog.load(_OST_CATALOG_YAML)
        unresolved = [n for n in names if cat.find(n) is None]
        assert not unresolved, (
            f"catalog advertises categories the OST catalog cannot resolve: "
            f"{unresolved}"
        )


# Use case -> the engine symbols that back it. THIS TABLE IS THE CONTRACT:
# each heading must exist in the doc, and each symbol must exist in the
# engine's own vocabulary — so removing either side breaks the build instead
# of leaving a public promise dangling. (kind, name) where kind selects the
# registry the name must appear in.
_USE_CASE_BACKING: dict[str, list[tuple[str, str]]] = {
    "A1 — Required data is present": [("requirement", "present_and_nonempty")],
    "A2 — Values are unique within the model": [
        ("requirement", "unique_in_set"),
        ("new_value_strategy", "next_available"),
    ],
    "A3 — Identifiers composed from other data": [
        ("autofill", "compose_template"),
    ],
    "B1 — Canonical unit format": [
        ("requirement", "canonical_format"),
        ("autofill", "normalize"),
    ],
    "B2 — Enumerated value mapping": [("requirement", "canonical_format")],
    "B3 — Pattern (regex) rules": [
        ("requirement", "matches_regex"),
        ("requirement", "not_matches_regex"),
        ("requirement", "matches_regex_if_present"),
    ],
    "B4 — Naming conventions": [("requirement", "canonical_format")],
    "C1 — Membership in an approved set": [("requirement", "canonical_format")],
    "C2 — Value required by a code table": [
        ("requirement", "relation_compare"),
        ("rule_field", "lookup"),
    ],
    "D1 — Numeric compare with units": [
        ("requirement", "numeric_compare"),
        ("rule_field", "unit"),
    ],
    "E1 — Consistency with a related element": [
        ("requirement", "relation_compare"),
        ("rule_field", "other_param"),
    ],
    "F1 — Inherit from host when empty": [("autofill", "inherit_from_host")],
    "F2 — Present AND canonical, inherit when empty (compound)": [
        ("autofill", "inherit_then_normalize"),
    ],
    "G1 — Conditional scope": [("rule_field", "scope_filter")],
}


class TestUseCasesAreBackedByTheEngine:
    def test_every_use_case_heading_still_exists(self, catalog_text: str) -> None:
        missing = [
            h for h in _USE_CASE_BACKING if f"### {h}" not in catalog_text
        ]
        assert not missing, (
            f"catalog headings renamed/removed without updating the contract "
            f"table: {missing}"
        )

    def test_every_backing_symbol_exists_in_the_engine(self) -> None:
        from bim_orchestrator.policies.rules_schema import NewValueStrategy

        registries = {
            "requirement": set(get_args(Requirement)),
            "autofill": set(get_args(AutofillStrategy)),
            "new_value_strategy": set(get_args(NewValueStrategy)),
            "rule_field": set(Rule.model_fields),
        }
        dangling = [
            (heading, kind, name)
            for heading, backing in _USE_CASE_BACKING.items()
            for kind, name in backing
            if name not in registries[kind]
        ]
        assert not dangling, (
            f"catalog use cases backed by symbols the engine no longer has: "
            f"{dangling}"
        )


class TestWritePromises:
    """F-02's class — a write-behaviour promise in a public doc must point at
    a real enforcement, not at hope."""

    def test_never_a_silent_write_is_enforced_not_just_written(
        self, catalog_text: str
    ) -> None:
        """Division of labour, chosen after a mutation result: a source-grep
        of the gate ("the demote line is present") stayed green with the
        demote wrapped in `if False` — the literal survives while the
        behaviour dies, so a string assert here is theatre. The BEHAVIOUR is
        pinned by test_auto_gate_value_source's policy-says-auto test (which
        that same mutation turns red — verified when it shipped). This test's
        job is the LINK: while the catalog makes the promise, the behavioural
        test that enforces it must exist, and the auto-grant list must stay
        exactly {compose_template} (a runtime value, not a grep)."""
        if "never a silent write" not in catalog_text:
            pytest.skip("the promise was removed from the catalog")

        from bim_orchestrator.agents.design import _AUTO_AUTOFILL_STRATEGIES

        assert _AUTO_AUTOFILL_STRATEGIES == frozenset({"compose_template"})

        gate_tests = (
            Path(__file__).parent / "test_auto_gate_value_source.py"
        ).read_text(encoding="utf-8")
        assert (
            "test_next_available_is_never_auto_even_when_POLICY_says_auto"
            in gate_tests
        ), (
            "the behavioural test enforcing 'never a silent write' is gone — "
            "either restore it or remove the promise from the public catalog"
        )

    def test_no_hardcoded_pass_count(self, catalog_text: str) -> None:
        """The "870 tests" lesson: a pass-count printed in a doc is stale the
        week after. The footer now points at the live QA-harness reports."""
        assert not re.search(r"\d+\s*/\s*\d+\s+passing", catalog_text)


class TestSkillPackCatalogIsInSync:
    """The extraction skill pack carries its OWN copy of the category list,
    and that copy is what the LLM is allowed to choose from.

    It had drifted: `ost_catalog_keys.txt` held 61 entries while the engine
    catalog had 63, missing `sheets` and `spaces`. That is not cosmetic —
    `extraction_prompt.md` tells the model to pick EXACTLY from its inline
    list, so a BEP clause about Sheets or Spaces came back tagged
    `needs_domain_mapping` ("category not in catalog") for two categories the
    catalog has had all along, and which shipped rule sets already target.
    Regenerate with `extraction-skills/scripts/_generate_catalog_keys.py`.
    """

    @staticmethod
    def _keys_file_entries() -> set[str]:
        path = (
            Path(__file__).resolve().parents[1]
            / "extraction-skills"
            / "ost_catalog_keys.txt"
        )
        keys = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            keys.add(line.split("|")[0].strip())
        return keys

    def test_keys_file_matches_the_engine_catalog(self) -> None:
        catalog_keys = {e.key for e in OSTCatalog.load(_OST_CATALOG_YAML).entries}
        file_keys = self._keys_file_entries()
        assert file_keys == catalog_keys, (
            "extraction-skills/ost_catalog_keys.txt is out of sync with "
            "config/ost_catalog.yaml — regenerate it with "
            "extraction-skills/scripts/_generate_catalog_keys.py. "
            f"missing from file={sorted(catalog_keys - file_keys)}, "
            f"stale in file={sorted(file_keys - catalog_keys)}"
        )

    def test_the_prompt_lists_every_category_the_engine_knows(self) -> None:
        """The prompt's inline list is the model's whole world — a category
        absent here cannot be extracted, whatever the catalog says."""
        prompt = (
            Path(__file__).resolve().parents[1]
            / "extraction-skills"
            / "extraction_prompt.md"
        ).read_text(encoding="utf-8")
        # The list is prose-wrapped, so "Duct Accessories" really appears as
        # "Duct\nAccessories". Collapse whitespace before looking, or the test
        # reports six categories missing that are plainly there (it did).
        flat = re.sub(r"\s+", " ", prompt)
        displays = {e.display for e in OSTCatalog.load(_OST_CATALOG_YAML).entries}
        missing = sorted(d for d in displays if d not in flat)
        assert not missing, (
            f"extraction_prompt.md does not offer these categories: {missing} "
            "— the extractor will refuse rules that target them"
        )
