"""Phase 2 GĐ3-C1 — bridge rules-extractor through the phase2 seam.

Pins: the bridge satisfies rules_extractor's emit_ruleset protocol, records each
call on the shared UsageRecorder (tag "extraction"), and the provider gate is
honest (anthropic|inject only; fake/ollama can't extract). Offline: rules_extractor
FakeLLMClient — no key, no network. Skips cleanly if rules-extractor isn't installed
— UNLESS `BIM_REQUIRE_EXTRACTION=1` (M14, docs/reviews/REVIEW_MULTI_2026-07-04.md):
that's the CI-gate opt-in for an environment that is supposed to have the
extraction sibling installed (`uv pip install -e <ExtractionAgents>`), where a
silent skip would hide a real regression (e.g. `uv sync` re-removing the local
editable — see CLAUDE.md "Tooling + commands"). Default posture (env unset) is
unchanged: skip cleanly.
"""

from __future__ import annotations

import os

import pytest

from bim_orchestrator.llm.extraction_bridge import (
    ExtractionSeamClient,
    ExtractionUnavailable,
    extraction_model,
    make_extraction_client,
    rules_extractor_available,
)
from bim_orchestrator.llm.usage import UsageRecorder

if not rules_extractor_available():
    if os.environ.get("BIM_REQUIRE_EXTRACTION", "").strip() == "1":
        pytest.fail(
            "BIM_REQUIRE_EXTRACTION=1 but rules-extractor is not installed "
            "(uv pip install -e <path-to>/ExtractionAgents); "
            "did `uv sync` remove the local editable? Use `uv sync --extra dev "
            "--inexact` once installed."
        )
    pytestmark = pytest.mark.skip(reason="rules-extractor not installed")


_CANNED = {
    "scenario": "src",
    "target_category": "Doors",
    "rules": [
        {
            "id": "door.fire_rating.present",
            "parameter": "Fire Rating",
            "requirement": "present_and_nonempty",
            "severity_tag": "data_quality",
            "description": "Fire Rating must be present.",
            "extraction_meta": {
                "confidence": 0.9,
                "source_text": "Every door shall have a fire rating.",
                "source_location": "p.1",
            },
        },
        # Low P2 (docs/reviews/REVIEW_MULTI_2026-07-04.md — "K22.4 round-trip
        # test hẹp (1 rule)"): widen the canned envelope with a numeric_compare
        # rule (operator + threshold + unit)...
        {
            "id": "door.clear_width.min",
            "parameter": "Clear Opening Width",
            "requirement": "numeric_compare",
            "operator": ">=",
            "threshold": 813.0,
            "unit": "mm",
            "severity_tag": "life_safety",
            "description": "Accessible doors must have a clear opening width of at least 813 mm.",
            "extraction_meta": {
                "confidence": 0.85,
                "source_text": "Clear opening width shall be not less than 813 mm.",
                "source_location": "p.2",
            },
        },
        # ...and a rule carrying `scope_filter` (the universal "applies to"
        # gate), so both consolidated (v1.4-K10) fields round-trip through
        # rules_extractor's contract Rule -> RuleSet.model_validate here.
        {
            "id": "door.fire_rating.exterior_only",
            "parameter": "Fire Rating",
            "requirement": "present_and_nonempty",
            "scope_filter": {"param": "Function", "pattern": "Exterior"},
            "severity_tag": "data_quality",
            "description": "Exterior doors must declare a fire rating.",
            "extraction_meta": {
                "confidence": 0.85,
                "source_text": "Exterior doors shall have a fire rating declared.",
                "source_location": "p.3",
            },
        },
    ],
    "metadata": {"source": "test doc"},
}


def _fake_inner():
    from rules_extractor.llm import FakeLLMClient

    return FakeLLMClient(envelope=_CANNED)


# ---- bridge passes through + records ---------------------------------------


def test_bridge_records_call_on_recorder() -> None:
    rec = UsageRecorder()
    bridge = make_extraction_client(inner=_fake_inner(), recorder=rec)
    out = bridge.emit_ruleset(system="s", user_content="u", tool={"name": "t"}, model="m")
    assert out["rules"][0]["id"] == "door.fire_rating.present"
    assert rec.summary()["by_agent"] == {"extraction": 1}


def test_bridge_records_even_on_inner_error() -> None:
    class _Boom:
        def emit_ruleset(self, **kw):
            raise RuntimeError("model refused")

    rec = UsageRecorder()
    bridge = ExtractionSeamClient(_Boom(), recorder=rec)
    with pytest.raises(RuntimeError):
        bridge.emit_ruleset(system="s", user_content="u", tool={"name": "t"}, model="m")
    assert rec.total_calls == 1  # the failed attempt still counts


def test_bridge_works_without_recorder() -> None:
    bridge = ExtractionSeamClient(_fake_inner(), recorder=None)
    out = bridge.emit_ruleset(system="s", user_content="u", tool={"name": "t"}, model="m")
    assert out["scenario"] == "src"


# ---- end-to-end through rules_extractor.extract_sections -------------------


def test_extract_sections_through_bridge_meters() -> None:
    from rules_extractor import extract_sections

    rec = UsageRecorder()
    bridge = make_extraction_client(inner=_fake_inner(), recorder=rec)
    envelope, coverage = extract_sections(
        "Every door shall have a fire rating.", is_text=True, client=bridge,
        max_concurrency=1,
    )
    assert envelope["rulesets"] and envelope["rulesets"][0]["rules"]
    assert any(c.status.startswith("ok") for c in coverage)
    assert rec.summary()["by_agent"].get("extraction", 0) >= 1


# ---- provider gate ---------------------------------------------------------


def test_provider_fake_cannot_extract(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    with pytest.raises(ExtractionUnavailable):
        make_extraction_client()


def test_provider_ollama_builds_local_client(monkeypatch) -> None:
    # 2026-07-15: ollama IS now a supported extraction provider (local,
    # schema-constrained decoding) — no network at construction time.
    from rules_extractor.llm import OllamaClient

    monkeypatch.setenv("BIM_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("BIM_LLM_BASE_URL", "http://box:1234")
    monkeypatch.delenv("BIM_EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("BIM_LLM_MODEL", raising=False)
    client = make_extraction_client()
    assert isinstance(client, ExtractionSeamClient)
    inner = client._inner
    assert isinstance(inner, OllamaClient)
    assert inner._base_url == "http://box:1234"
    assert inner._model == "qwen3:14b"  # provider-aware default


def test_provider_unknown_cannot_extract(monkeypatch) -> None:
    monkeypatch.setenv("BIM_LLM_PROVIDER", "fake")
    with pytest.raises(ExtractionUnavailable):
        make_extraction_client()


# ---- contract compat: K22.4 output loads in the phase2 (superset) loader -----


def test_extracted_yaml_loads_in_phase2_loader() -> None:
    """The crux of 'keep K22.4': a rules_extractor-produced (K22.4) YAML must load
    in THIS phase2 bim-orchestrator, whose schema is a superset (value_in_subset /
    allowed_values / llm_safety_critical are additive).

    Low P2 widen: the canned envelope now also carries a `numeric_compare` rule
    (operator + threshold + unit) and a rule with a `scope_filter` — both
    v1.4-K10 consolidated fields — so the round-trip isn't pinned by a single
    present_and_nonempty rule alone.
    """
    import yaml
    from rules_extractor import convert_envelope, load_contract

    from bim_orchestrator.policies.rules_schema import RuleSet

    contract = load_contract()
    result = convert_envelope({"rulesets": [_CANNED]}, contract=contract)
    yamls = [s.rules_yaml for s in result.scenarios if s.rules_yaml]
    assert yamls, "expected at least one executable ruleset lowered to YAML"
    loaded: RuleSet | None = None
    for y in yamls:
        loaded = RuleSet.model_validate(yaml.safe_load(y))  # must not raise

    assert loaded is not None
    by_id = {r.id: r for r in loaded.rules}
    assert {
        "door.fire_rating.present",
        "door.clear_width.min",
        "door.fire_rating.exterior_only",
    } <= set(by_id)

    numeric_rule = by_id["door.clear_width.min"]
    assert numeric_rule.requirement == "numeric_compare"
    assert numeric_rule.operator == ">="
    assert numeric_rule.threshold == 813.0
    assert numeric_rule.unit == "mm"

    scoped_rule = by_id["door.fire_rating.exterior_only"]
    assert scoped_rule.scope_filter is not None
    assert scoped_rule.scope_filter.param == "Function"
    assert scoped_rule.scope_filter.pattern == "Exterior"


def test_extraction_model_precedence(monkeypatch) -> None:
    monkeypatch.delenv("BIM_LLM_PROVIDER", raising=False)  # -> anthropic default
    monkeypatch.delenv("BIM_EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("BIM_LLM_MODEL", raising=False)
    assert extraction_model() == "claude-sonnet-4-6"
    monkeypatch.setenv("BIM_LLM_MODEL", "claude-opus-4-8")
    assert extraction_model() == "claude-opus-4-8"
    monkeypatch.setenv("BIM_EXTRACTION_MODEL", "claude-sonnet-4-6")
    assert extraction_model() == "claude-sonnet-4-6"  # extraction override wins


def test_extraction_model_ollama_default(monkeypatch) -> None:
    # Provider-aware: the ollama path must NOT inherit the claude-sonnet default
    # (it would be posted verbatim as an Ollama model name and 404). Explicit
    # overrides still win.
    monkeypatch.setenv("BIM_LLM_PROVIDER", "ollama")
    monkeypatch.delenv("BIM_EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("BIM_LLM_MODEL", raising=False)
    assert extraction_model() == "qwen3:14b"
    monkeypatch.setenv("BIM_LLM_MODEL", "llama3.1:70b")
    assert extraction_model() == "llama3.1:70b"
