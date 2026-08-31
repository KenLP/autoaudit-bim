"""Tests for the synthetic BEP §1 Room Requirements fixture (Phase 2 W6 D2).

Asserts the fixture is well-formed and retrievable. Uses the same
hash-based bag-of-words embedder as test_grounding_agent so CI is offline
and deterministic.
"""

from __future__ import annotations

import pytest

from bim_orchestrator.rag.eval import run_eval
from bim_orchestrator.rag.fixtures import (
    BEP_ROOM_REQUIREMENTS,
    DEFAULT_BEP_QUERIES,
    ingest_bep_room_requirements,
)
from bim_orchestrator.rag.store import VectorStore
from tests.test_grounding_agent import fake_embed


# ---- Fixture shape --------------------------------------------------------


class TestFixtureShape:
    def test_seven_sections(self) -> None:
        assert len(BEP_ROOM_REQUIREMENTS) == 7

    def test_sections_are_unique(self) -> None:
        sections = [e["section"] for e in BEP_ROOM_REQUIREMENTS]
        assert len(set(sections)) == len(sections)

    def test_each_entry_has_required_fields(self) -> None:
        for entry in BEP_ROOM_REQUIREMENTS:
            assert {"section", "page", "text"} <= entry.keys()
            assert entry["text"].strip()
            assert isinstance(entry["page"], int) and entry["page"] >= 1
            assert entry["section"].startswith("§1.")

    def test_corpus_mentions_locked_thresholds(self) -> None:
        """The 6 numbers from the user-supplied BEP image must appear verbatim
        in at least one chunk so retrieval has the right anchor terms."""
        joined = " ".join(e["text"] for e in BEP_ROOM_REQUIREMENTS)
        for needle in ("10 m²", "9 m²", "2.4 m", "2.6 m"):
            assert needle in joined, f"missing threshold {needle!r}"

    def test_default_queries_align_with_sections(self) -> None:
        sections = {e["section"] for e in BEP_ROOM_REQUIREMENTS}
        for q in DEFAULT_BEP_QUERIES:
            assert q.expected_source == "BEP.txt"
            assert q.expected_section in sections


# ---- Ingest + retrieval ---------------------------------------------------


@pytest.fixture
def bep_store(tmp_path):
    store = VectorStore(
        persist_dir=tmp_path / "chroma",
        collection="bep-test",
        embed_fn=fake_embed,
    )
    ingest_bep_room_requirements(store)
    return store


class TestIngestAndRetrieval:
    def test_ingest_returns_chunk_count(self, tmp_path) -> None:
        store = VectorStore(
            persist_dir=tmp_path / "chroma",
            collection="bep-ingest",
            embed_fn=fake_embed,
        )
        added = ingest_bep_room_requirements(store)
        # Each entry is short → exactly one chunk per section
        assert added >= len(BEP_ROOM_REQUIREMENTS)

    def test_search_retrieves_at_least_one_chunk(self, bep_store) -> None:
        hits = bep_store.search("minimum floor area for bedroom", k=3)
        assert len(hits) >= 1
        assert all(h.metadata.get("source") == "BEP.txt" for h in hits)

    def test_run_eval_meets_quality_floor(self, bep_store) -> None:
        """End-to-end eval — top-3 hit rate must clear 60% on the synthetic
        corpus. Bag-of-words is intentionally weak; sentence-transformers in
        production scores higher. This is a regression floor, not a target."""
        report = run_eval(bep_store, DEFAULT_BEP_QUERIES, top_k=3)
        assert report.total == len(DEFAULT_BEP_QUERIES)
        assert report.hit_rate_at_3 >= 0.6, (
            f"BEP hit@3 dropped to {report.hit_rate_at_3:.2%}\n"
            + report.format(verbose=True)
        )
