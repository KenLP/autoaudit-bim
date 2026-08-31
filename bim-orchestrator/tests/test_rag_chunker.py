"""Unit tests for the paragraph-aware chunker."""

from __future__ import annotations

import pytest

from bim_orchestrator.rag.chunker import chunk_text


class TestBasicBehavior:
    def test_empty_input_returns_empty_list(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_short_text_returns_single_chunk(self):
        text = "BEP §4.2 requires fire ratings on all corridor walls."
        assert chunk_text(text, max_chars=2000) == [text]

    def test_long_text_produces_multiple_chunks(self):
        text = "\n\n".join([f"Paragraph {i}. " + ("filler " * 30) for i in range(20)])
        chunks = chunk_text(text, max_chars=500, overlap=50)
        assert len(chunks) >= 4

    def test_all_chunks_within_max_chars_window(self):
        text = "\n\n".join([f"Para {i}. " + ("x" * 100) for i in range(30)])
        chunks = chunk_text(text, max_chars=400, overlap=40)
        # With overlap, each chunk = (max_chars body) + (≤overlap prepended). Soft limit.
        for c in chunks:
            assert len(c) <= 400 + 40 + 1  # +1 newline join


class TestParagraphBoundaries:
    def test_prefers_paragraph_boundary(self):
        # Two paragraphs that together fit, should stay together
        text = "Short paragraph one.\n\nShort paragraph two."
        chunks = chunk_text(text, max_chars=200, overlap=0)
        assert len(chunks) == 1
        assert "one" in chunks[0]
        assert "two" in chunks[0]

    def test_splits_at_paragraph_when_packed_too_full(self):
        para = "A" * 150  # 150 chars
        text = f"{para}\n\n{para}\n\n{para}"  # 3 paras of 150 chars
        chunks = chunk_text(text, max_chars=200, overlap=0)
        # Each para fits alone but two together exceed 200 → 3 chunks
        assert len(chunks) == 3

    def test_oversized_paragraph_hard_split(self):
        # Single paragraph longer than max_chars must be split internally
        para = ("Sentence ends here. " * 50)  # ~1000 chars in one paragraph
        chunks = chunk_text(para, max_chars=200, overlap=0)
        assert len(chunks) > 1
        assert all(len(c) <= 200 for c in chunks)


class TestOverlap:
    def test_overlap_zero_no_repetition(self):
        # Use distinct content per paragraph so we can detect actual overlap
        text = "\n\n".join([f"PARA{i}" + ("." * 100) for i in range(5)])
        chunks = chunk_text(text, max_chars=120, overlap=0)
        # With overlap=0, chunk N+1 should NOT start with the last bytes of chunk N
        for prev, curr in zip(chunks, chunks[1:]):
            tail = prev[-20:]
            # The next chunk starts with its own PARA marker, not the previous tail
            assert not curr.startswith(tail), f"Found repetition: tail={tail!r}, next={curr[:30]!r}"

    def test_overlap_prepends_tail_of_previous(self):
        text = "\n\n".join(["AAA" * 60 for _ in range(5)])
        chunks = chunk_text(text, max_chars=200, overlap=30)
        if len(chunks) > 1:
            tail = chunks[0][-30:]
            assert chunks[1].startswith(tail)


class TestInvalidInput:
    def test_zero_max_chars_raises(self):
        with pytest.raises(ValueError):
            chunk_text("x", max_chars=0)

    def test_negative_overlap_raises(self):
        with pytest.raises(ValueError):
            chunk_text("x", max_chars=100, overlap=-1)

    def test_overlap_exceeding_max_raises(self):
        with pytest.raises(ValueError):
            chunk_text("x", max_chars=100, overlap=100)
