"""Tests for the GroundingAgent — uses fake-embed VectorStore."""

from __future__ import annotations

import pytest

from bim_orchestrator.agents.grounding import Citation, GroundingAgent, _build_query
from bim_orchestrator.rag.store import VectorStore
from bim_orchestrator.state import Finding, OrchestratorState


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Hash-based bag-of-words embedder for grounding tests.

    Bigger vocab coverage than the BIM-themed one in test_rag_store.py — uses
    feature hashing so any token contributes a deterministic dimension.
    Documents sharing tokens get higher cosine similarity.
    """
    import hashlib
    import math

    DIM = 64
    vectors: list[list[float]] = []
    for text in texts:
        vec = [0.0] * DIM
        for raw in text.lower().split():
            token = "".join(c for c in raw if c.isalnum())
            if not token:
                continue
            h = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
            vec[h] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        vectors.append([v / norm for v in vec])
    return vectors


# ---- Fixtures --------------------------------------------------------------


@pytest.fixture
def populated_store(tmp_path):
    """A store with realistic BEP + IBC chunks."""
    store = VectorStore(
        persist_dir=tmp_path / "chroma", collection="grounding-test", embed_fn=fake_embed
    )
    # BEP §4.2 — Department + Occupancy
    store.ingest_text(
        "Every room shall declare a department for cost allocation. "
        "Occupancy type drives egress and ventilation calculations.",
        source="BEP.pdf", section="§4.2", page=12,
    )
    # IBC §711 — Fire ratings on corridor walls
    store.ingest_text(
        "Fire rated corridor walls require 2-hour rated assemblies "
        "with smoke and stair enclosure separation per §711.",
        source="IBC.pdf", section="§711.2", page=45,
    )
    # BEP §6 — Door hardware (off-topic relative to Department)
    store.ingest_text(
        "Door hardware specifications include closer, lockset, and panic hardware.",
        source="BEP.pdf", section="§6", page=22,
    )
    return store


def _state_with_findings(findings: list[Finding]) -> OrchestratorState:
    return {
        "project_id": "test",
        "iteration": 0,
        "max_iterations": 1,
        "elements": [],
        "findings": findings,
        "proposed_fixes": [],
        "status": "checking",
        "error": None,
    }


def _finding(
    *,
    rule_id: str = "room.department.required",
    parameter: str = "Department",
    severity: str = "severity_medium",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        element_id="elem-1",
        parameter=parameter,
        severity_tag="missing_required_param",
        severity=severity,  # type: ignore[arg-type]
        message=f"{rule_id} failed",
        suggested_value=None,
        citation=None,
    )


# ---- Query construction ----------------------------------------------------


class TestBuildQuery:
    def test_combines_parameter_and_rule_tokens(self):
        f = _finding(rule_id="room.department.required", parameter="Department")
        q = _build_query(f)
        assert "Department" in q
        assert "department" in q  # rule_id token
        assert "required" in q

    def test_strips_dots_from_rule_id(self):
        q = _build_query(_finding(rule_id="wall.fire_rating.required"))
        assert "." not in q

    def test_handles_missing_fields(self):
        f = Finding(
            rule_id="", element_id="e", parameter="", severity_tag="x",
            severity="severity_low", message="m", suggested_value=None, citation=None,
        )
        assert _build_query(f) == ""


# ---- GroundingAgent.run ---------------------------------------------------


def test_populates_citation_on_relevant_finding(populated_store):
    agent = GroundingAgent(store=populated_store, top_k=2, min_score=0.0)
    findings = [_finding(parameter="Department", rule_id="room.department.required")]
    result = agent.run(_state_with_findings(findings))

    assert result["findings"][0]["citation"] is not None
    citation = result["findings"][0]["citation"]
    # Should include source + section + page
    assert "BEP.pdf" in citation
    assert "§4.2" in citation
    assert "p.12" in citation


def test_citation_refs_populated_alongside_string(populated_store):
    """Day 5: structured citation_refs match the formatted citation string."""
    agent = GroundingAgent(store=populated_store, top_k=2, min_score=0.0)
    findings = [_finding(parameter="Department", rule_id="room.department.required")]
    result = agent.run(_state_with_findings(findings))

    f = result["findings"][0]
    assert "citation_refs" in f
    refs = f["citation_refs"]
    assert isinstance(refs, list) and len(refs) >= 1
    # Each ref is a dict with the documented shape
    for ref in refs:
        assert set(ref.keys()) >= {"source", "section", "page", "snippet", "score"}
        assert isinstance(ref["source"], str)
        assert isinstance(ref["snippet"], str) and len(ref["snippet"]) > 0
        assert 0 <= ref["score"] <= 1.0
    # The top ref should be BEP.pdf §4.2 p.12
    top = refs[0]
    assert top["source"] == "BEP.pdf"
    assert top["section"] == "§4.2"
    assert top["page"] == 12


def test_citation_refs_absent_when_no_match(populated_store):
    """When min_score filters out everything, citation_refs should NOT be set."""
    agent = GroundingAgent(store=populated_store, top_k=2, min_score=0.99)
    findings = [_finding(parameter="Department")]
    result = agent.run(_state_with_findings(findings))

    f = result["findings"][0]
    assert f["citation"] is None
    assert "citation_refs" not in f


def test_multi_citation_separator(populated_store):
    """Top-k > 1 → citation string lists multiple refs separated by ' · '."""
    agent = GroundingAgent(store=populated_store, top_k=3, min_score=0.0)
    findings = [_finding(parameter="fire", rule_id="wall.fire_rating.required")]
    result = agent.run(_state_with_findings(findings))

    citation = result["findings"][0]["citation"]
    assert citation is not None
    assert " · " in citation
    # IBC §711 should rank above the off-topic BEP §6 entry for "fire" query
    assert "IBC.pdf" in citation


def test_low_score_chunks_filtered(populated_store):
    """min_score=0.99 → no chunk matches → citation is None."""
    agent = GroundingAgent(store=populated_store, top_k=3, min_score=0.99)
    findings = [_finding(parameter="Department")]
    result = agent.run(_state_with_findings(findings))
    assert result["findings"][0]["citation"] is None


def test_empty_store_skips_gracefully(tmp_path):
    empty = VectorStore(persist_dir=tmp_path / "empty", embed_fn=fake_embed)
    agent = GroundingAgent(store=empty)
    findings = [_finding()]
    result = agent.run(_state_with_findings(findings))
    # Citation untouched (still None as the original Finding had)
    assert result["findings"][0]["citation"] is None


def test_no_findings_handled(populated_store):
    agent = GroundingAgent(store=populated_store)
    result = agent.run(_state_with_findings([]))
    assert result["findings"] == []


def test_does_not_mutate_other_finding_fields(populated_store):
    agent = GroundingAgent(store=populated_store)
    original = _finding()
    snapshot = dict(original)
    result = agent.run(_state_with_findings([original]))
    out = result["findings"][0]
    # All non-citation fields untouched
    for key in ("rule_id", "element_id", "parameter", "severity_tag", "severity",
                "message", "suggested_value"):
        assert out[key] == snapshot[key]


# ---- Structured Citation -------------------------------------------------


def test_cite_returns_structured_citations(populated_store):
    """The `cite()` helper returns rich Citation dataclasses for UI consumers."""
    agent = GroundingAgent(store=populated_store, top_k=2, min_score=0.0)
    citations = agent.cite(_finding(parameter="Department"))
    assert len(citations) >= 1
    c = citations[0]
    assert isinstance(c, Citation)
    assert c.source == "BEP.pdf"
    assert c.section == "§4.2"
    assert c.page == 12
    assert 0 <= c.score <= 1.0
    assert c.snippet  # non-empty


def test_citation_snippet_respects_char_limit(populated_store):
    agent = GroundingAgent(store=populated_store, top_k=1, min_score=0.0, snippet_chars=40)
    citations = agent.cite(_finding(parameter="Department"))
    assert len(citations[0].snippet) <= 40


def test_citation_dataclass_frozen():
    c = Citation(source="x", section=None, page=None, snippet="s", score=0.5)
    with pytest.raises(Exception):
        c.source = "y"  # type: ignore[misc]


# ---- top_k respected -----------------------------------------------------


def test_top_k_limits_citations(populated_store):
    agent = GroundingAgent(store=populated_store, top_k=1, min_score=0.0)
    citations = agent.cite(_finding(parameter="Department"))
    assert len(citations) == 1


def test_top_k_zero_yields_no_citation(populated_store):
    agent = GroundingAgent(store=populated_store, top_k=0, min_score=0.0)
    findings = [_finding()]
    result = agent.run(_state_with_findings(findings))
    assert result["findings"][0]["citation"] is None
