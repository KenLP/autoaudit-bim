"""Retrieval quality eval harness.

Phase 2 Week 4 Day 1: measure how well the VectorStore retrieves the *right*
chunk for hand-built queries. Metrics:

    * hit@1   — top result matches expected (source, section)
    * hit@3   — any of top-3 matches expected
    * MRR     — mean reciprocal rank across all queries

This is the engineering substrate we'll re-run every time we tune chunk size,
embedder, or add a new corpus. A 5-line eval report after every change tells
us if RAG quality went up or down. Without this, "did we improve" is vibes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bim_orchestrator.rag.store import Chunk, VectorStore


@dataclass(frozen=True)
class EvalQuery:
    """A single eval query with the expected source / section to retrieve."""

    query: str
    expected_source: str
    expected_section: str | None = None  # None → only source must match
    description: str = ""


@dataclass(frozen=True)
class EvalResult:
    """Per-query outcome with retrieved chunks + computed metrics."""

    query: EvalQuery
    top_chunks: list[Chunk]
    hit_at_1: bool
    hit_at_3: bool
    reciprocal_rank: float       # 1/rank if hit anywhere in top-k, else 0.0
    matched_rank: int | None     # 1-indexed rank of first match, None if miss


@dataclass(frozen=True)
class EvalReport:
    results: list[EvalResult]
    top_k: int = 3

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hit_rate_at_1(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.hit_at_1) / len(self.results)

    @property
    def hit_rate_at_3(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.hit_at_3) / len(self.results)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.reciprocal_rank for r in self.results) / len(self.results)

    def format(self, *, verbose: bool = True) -> str:
        """Printable text report. verbose=True includes per-query breakdown."""
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append(f"  RAG Retrieval Eval  ·  {self.total} queries  ·  top_k={self.top_k}")
        lines.append("=" * 78)
        lines.append(f"  hit@1   {self.hit_rate_at_1 * 100:>5.1f} %   "
                     f"({sum(1 for r in self.results if r.hit_at_1)} / {self.total})")
        lines.append(f"  hit@3   {self.hit_rate_at_3 * 100:>5.1f} %   "
                     f"({sum(1 for r in self.results if r.hit_at_3)} / {self.total})")
        lines.append(f"  MRR     {self.mrr:>5.3f}")
        lines.append("=" * 78)
        if not verbose:
            return "\n".join(lines)

        lines.append("")
        lines.append("Per-query breakdown:")
        lines.append("-" * 78)
        for i, r in enumerate(self.results, start=1):
            status = _status_icon(r)
            target = r.query.expected_source
            if r.query.expected_section:
                target += f" {r.query.expected_section}"
            rank_str = f"rank={r.matched_rank}" if r.matched_rank else "rank=—"
            lines.append(
                f"  {status} {i:>2}. \"{r.query.query[:50]:<50}\""
                f"  target={target:<24}  {rank_str}"
            )
            if r.query.description:
                lines.append(f"        ↪ {r.query.description}")
            # Show top result for context
            if r.top_chunks:
                top = r.top_chunks[0]
                src = top.metadata.get("source", "?")
                sec = top.metadata.get("section", "")
                page = top.metadata.get("page", "")
                page_str = f" p.{page}" if page else ""
                lines.append(
                    f"        top1: {src} {sec}{page_str}  ({top.score:.2f})"
                )
        return "\n".join(lines)


def run_eval(
    store: VectorStore, queries: list[EvalQuery], *, top_k: int = 3
) -> EvalReport:
    """Execute each query against the store, score retrieval quality."""
    results: list[EvalResult] = []
    for q in queries:
        chunks = store.search(q.query, k=top_k)
        matched_rank = _find_match_rank(chunks, q)
        rr = 1.0 / matched_rank if matched_rank else 0.0
        results.append(
            EvalResult(
                query=q,
                top_chunks=chunks,
                hit_at_1=(matched_rank == 1),
                hit_at_3=(matched_rank is not None and matched_rank <= 3),
                reciprocal_rank=rr,
                matched_rank=matched_rank,
            )
        )
    return EvalReport(results=results, top_k=top_k)


def _find_match_rank(chunks: list[Chunk], query: EvalQuery) -> int | None:
    """Return 1-indexed rank of the first matching chunk, or None."""
    for i, chunk in enumerate(chunks, start=1):
        if _matches(chunk, query):
            return i
    return None


def _matches(chunk: Chunk, query: EvalQuery) -> bool:
    src = str(chunk.metadata.get("source", ""))
    if src != query.expected_source:
        return False
    if query.expected_section is None:
        return True
    sec = str(chunk.metadata.get("section") or "")
    return sec == query.expected_section


def _status_icon(result: EvalResult) -> str:
    if result.hit_at_1:
        return "✅"
    if result.hit_at_3:
        return "🟡"
    return "❌"
