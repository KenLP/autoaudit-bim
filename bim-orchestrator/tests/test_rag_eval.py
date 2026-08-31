"""Tests for the RAG retrieval quality eval harness."""

from __future__ import annotations

import pytest

from bim_orchestrator.rag.eval import (
    EvalQuery,
    EvalReport,
    EvalResult,
    run_eval,
)
from bim_orchestrator.rag.store import Chunk, VectorStore
from tests.test_grounding_agent import fake_embed


@pytest.fixture
def store(tmp_path):
    s = VectorStore(persist_dir=tmp_path / "chroma", collection="eval-test", embed_fn=fake_embed)
    s.ingest_text(
        "Fire walls are continuous from foundation through roof per §711.",
        source="IBC.txt", section="§711", page=1,
    )
    s.ingest_text(
        "Smoke barriers shall have a minimum 1-hour fire-resistance rating per §712.",
        source="IBC.txt", section="§712", page=4,
    )
    s.ingest_text(
        "Door hardware specifications include closer, lockset, panic hardware.",
        source="BEP.txt", section="§6", page=22,
    )
    return s


# ---- EvalReport metrics ---------------------------------------------------


class TestEvalReportMetrics:
    def test_empty_report_returns_zero_metrics(self):
        report = EvalReport(results=[], top_k=3)
        assert report.hit_rate_at_1 == 0.0
        assert report.hit_rate_at_3 == 0.0
        assert report.mrr == 0.0
        assert report.total == 0

    def test_all_hit_at_1_gives_100_percent(self):
        results = [_synthetic_result(matched_rank=1) for _ in range(4)]
        report = EvalReport(results=results, top_k=3)
        assert report.hit_rate_at_1 == 1.0
        assert report.hit_rate_at_3 == 1.0
        assert report.mrr == 1.0

    def test_mrr_averaging(self):
        # rank 1 → rr=1.0, rank 2 → rr=0.5, no hit → rr=0
        results = [
            _synthetic_result(matched_rank=1),
            _synthetic_result(matched_rank=2),
            _synthetic_result(matched_rank=None),
        ]
        report = EvalReport(results=results, top_k=3)
        assert report.mrr == pytest.approx((1.0 + 0.5 + 0.0) / 3)

    def test_hit_at_3_includes_ranks_1_2_3_only(self):
        results = [
            _synthetic_result(matched_rank=1),  # hit@1 and hit@3
            _synthetic_result(matched_rank=3),  # hit@3 only
            _synthetic_result(matched_rank=4),  # neither
            _synthetic_result(matched_rank=None),  # neither
        ]
        report = EvalReport(results=results, top_k=5)
        assert report.hit_rate_at_1 == 0.25
        assert report.hit_rate_at_3 == 0.5


# ---- run_eval -----------------------------------------------------------


def test_run_eval_top1_match(store):
    queries = [
        EvalQuery(
            query="fire walls foundation roof",
            expected_source="IBC.txt",
            expected_section="§711",
        ),
    ]
    report = run_eval(store, queries, top_k=3)
    assert report.total == 1
    r = report.results[0]
    assert r.hit_at_1 is True
    assert r.matched_rank == 1
    assert r.reciprocal_rank == 1.0


def test_run_eval_no_match_returns_miss(store):
    queries = [
        EvalQuery(
            query="completely off-topic plumbing query",
            expected_source="NONEXISTENT.txt",
            expected_section="§99",
        ),
    ]
    report = run_eval(store, queries, top_k=3)
    r = report.results[0]
    assert r.hit_at_1 is False
    assert r.hit_at_3 is False
    assert r.matched_rank is None
    assert r.reciprocal_rank == 0.0


def test_run_eval_source_only_match(store):
    """When expected_section is None, only source needs to match."""
    queries = [
        EvalQuery(
            query="smoke barrier rating",
            expected_source="IBC.txt",
            expected_section=None,
        ),
    ]
    report = run_eval(store, queries, top_k=3)
    assert report.results[0].hit_at_1 is True


def test_run_eval_empty_store(tmp_path):
    empty = VectorStore(persist_dir=tmp_path / "empty", embed_fn=fake_embed)
    queries = [EvalQuery(query="anything", expected_source="X.txt")]
    report = run_eval(empty, queries, top_k=3)
    assert report.hit_rate_at_1 == 0.0
    assert report.results[0].top_chunks == []


# ---- Format output -------------------------------------------------------


def test_report_format_includes_metrics(store):
    queries = [
        EvalQuery(query="fire walls", expected_source="IBC.txt", expected_section="§711"),
    ]
    report = run_eval(store, queries, top_k=3)
    text = report.format(verbose=False)
    assert "RAG Retrieval Eval" in text
    assert "hit@1" in text
    assert "hit@3" in text
    assert "MRR" in text


def test_report_format_verbose_includes_per_query(store):
    queries = [
        EvalQuery(
            query="fire walls",
            expected_source="IBC.txt",
            expected_section="§711",
            description="test description",
        ),
    ]
    report = run_eval(store, queries, top_k=3)
    text = report.format(verbose=True)
    assert "Per-query breakdown" in text
    assert "test description" in text


# ---- Helpers --------------------------------------------------------------


def _synthetic_result(*, matched_rank: int | None) -> EvalResult:
    """Build an EvalResult for metric-shape unit tests (no real chunks)."""
    q = EvalQuery(query="x", expected_source="X.txt", expected_section="§x")
    rr = 1.0 / matched_rank if matched_rank else 0.0
    return EvalResult(
        query=q,
        top_chunks=[],
        hit_at_1=(matched_rank == 1),
        hit_at_3=(matched_rank is not None and matched_rank <= 3),
        reciprocal_rank=rr,
        matched_rank=matched_rank,
    )


# ---- Fixture smoke test --------------------------------------------------


def test_ibc_fixture_ingests(tmp_path):
    """The synthetic IBC §7 fixture loads without errors."""
    from bim_orchestrator.rag.fixtures import (
        DEFAULT_IBC_QUERIES,
        IBC_CHAPTER_7,
        ingest_ibc_chapter_7,
    )

    assert len(IBC_CHAPTER_7) >= 8
    assert len(DEFAULT_IBC_QUERIES) >= 5
    # Every query expects IBC.txt source
    assert all(q.expected_source == "IBC.txt" for q in DEFAULT_IBC_QUERIES)
    # Every fixture entry has section + page + text
    for entry in IBC_CHAPTER_7:
        assert entry["section"].startswith("§")
        assert isinstance(entry["page"], int)
        assert len(entry["text"]) > 20

    s = VectorStore(persist_dir=tmp_path / "chroma", collection="ibc", embed_fn=fake_embed)
    chunks = ingest_ibc_chapter_7(s)
    assert chunks == len(IBC_CHAPTER_7)
    assert s.count == chunks
