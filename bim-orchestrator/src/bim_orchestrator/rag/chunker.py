"""Paragraph-aware text chunking with character-based windowing + overlap.

We avoid pulling in langchain-text-splitters for one function. The chunker
prefers paragraph boundaries (\\n\\n) and falls back to sentence ends, then to
hard character splits when paragraphs are oversized.
"""

from __future__ import annotations

import re


def chunk_text(
    text: str,
    *,
    max_chars: int = 2000,
    overlap: int = 200,
) -> list[str]:
    """Split text into chunks of ≤max_chars with character-level overlap.

    Boundary preference order:
        1. Double newline (paragraph break)
        2. Sentence end (`. `, `! `, `? `, plus newline)
        3. Single newline
        4. Hard character split (last resort)
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be in [0, max_chars)")

    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 1. Split into paragraphs first (preserve original separators)
    paragraphs = _split_paragraphs(text)

    # 2. Greedily pack paragraphs into windows
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        # If a single paragraph exceeds the window, break it into smaller pieces.
        if len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(_hard_split(para, max_chars=max_chars))
            continue

        added_len = len(para) + (2 if current else 0)  # +2 for the join "\n\n"
        if current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += added_len

    if current:
        chunks.append("\n\n".join(current))

    # 3. Apply overlap by prepending the tail of the previous chunk
    if overlap > 0 and len(chunks) > 1:
        with_overlap: list[str] = [chunks[0]]
        for prev, curr in zip(chunks, chunks[1:]):
            tail = prev[-overlap:] if len(prev) >= overlap else prev
            with_overlap.append(tail + "\n" + curr)
        return with_overlap

    return chunks


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _hard_split(text: str, *, max_chars: int) -> list[str]:
    """Used when a single paragraph exceeds the window — split on sentence ends,
    then hard character split as last resort."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(s) > max_chars:
            # Sentence itself too long → hard character split
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(s), max_chars):
                chunks.append(s[i : i + max_chars])
            continue
        candidate = (current + " " + s).strip() if current else s
        if len(candidate) > max_chars:
            if current:
                chunks.append(current)
            current = s
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
