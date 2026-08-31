"""Tests for VectorStore — uses fake-embed for determinism + speed."""

from __future__ import annotations

import pytest

from bim_orchestrator.rag.store import Chunk, VectorStore


# ---- Fake embedder ---------------------------------------------------------


# Tiny BIM-themed vocab. Bag-of-words → 10-dim vector → cosine similarity works.
_VOCAB = [
    "fire", "rating", "wall", "door", "egress",
    "corridor", "stair", "hour", "rated", "smoke",
]


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic bag-of-words embedding over a fixed BIM vocab.

    Vectors are L2-normalized so chroma's cosine distance works as expected.
    """
    import math

    vectors = []
    for text in texts:
        lower = text.lower()
        # Count each vocab word; substring match keeps it simple.
        raw = [float(lower.count(word)) for word in _VOCAB]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        vectors.append([v / norm for v in raw])
    return vectors


@pytest.fixture
def store(tmp_path):
    return VectorStore(
        persist_dir=tmp_path / "chroma", collection="test", embed_fn=fake_embed
    )


# ---- Ingestion -------------------------------------------------------------


def test_ingest_text_single_chunk(store):
    n = store.ingest_text(
        "Fire rated walls in corridors must be 2-hour rated.",
        source="bep.pdf", section="§4.2",
    )
    assert n == 1
    assert store.count == 1


def test_ingest_text_multi_chunk(store):
    # Text long enough to be split into ≥2 chunks at max_chars=200
    text = "\n\n".join([f"Paragraph {i}. " + ("rated " * 30) for i in range(5)])
    n = store.ingest_text(text, source="bep.pdf", max_chars=200, overlap=20)
    assert n >= 2
    assert store.count == n


def test_ingest_empty_text_returns_zero(store):
    assert store.ingest_text("", source="x.pdf") == 0
    assert store.ingest_text("   \n\n  ", source="x.pdf") == 0
    assert store.count == 0


# ---- Search ---------------------------------------------------------------


def test_search_returns_relevant_chunk_top1(store):
    store.ingest_text("Door hardware specifications go in §6.", source="bep", section="§6")
    store.ingest_text("Fire ratings on corridor walls per §4.2.", source="bep", section="§4.2")
    store.ingest_text("Egress stairs require 2-hour rated enclosures.", source="bep", section="§7")

    results = store.search("fire rating wall", k=2)
    assert len(results) == 2
    # The fire-rating chunk should rank first
    assert "fire" in results[0].text.lower()
    assert "wall" in results[0].text.lower()
    # Score should be > 0 and ≤ 1
    assert 0 < results[0].score <= 1.0


def test_search_returns_metadata(store):
    store.ingest_text("Fire rated corridor wall.", source="bep.pdf", section="§4.2", page=12)
    results = store.search("fire wall", k=1)
    assert len(results) == 1
    assert results[0].metadata["source"] == "bep.pdf"
    assert results[0].metadata["section"] == "§4.2"
    assert results[0].metadata["page"] == 12


def test_search_empty_store_returns_empty(store):
    assert store.search("anything") == []


def test_search_respects_k_limit(store):
    for i in range(7):
        store.ingest_text(f"Chunk {i}: fire wall rated door", source=f"s{i}.pdf")
    results = store.search("fire", k=3)
    assert len(results) == 3


def test_search_with_metadata_filter(store):
    store.ingest_text("Fire rated wall.", source="bep.pdf", section="§4.2")
    store.ingest_text("Fire rated door.", source="ibc.pdf", section="§711")
    results = store.search("fire", k=5, where={"source": "ibc.pdf"})
    assert len(results) == 1
    assert results[0].metadata["source"] == "ibc.pdf"


# ---- Persistence ----------------------------------------------------------


def test_persistence_survives_recreation(tmp_path):
    persist = tmp_path / "chroma"
    store1 = VectorStore(persist_dir=persist, collection="test", embed_fn=fake_embed)
    store1.ingest_text("Fire rated corridor wall.", source="bep.pdf", section="§4.2")
    assert store1.count == 1
    del store1

    # Recreate from same persist_dir
    store2 = VectorStore(persist_dir=persist, collection="test", embed_fn=fake_embed)
    assert store2.count == 1
    results = store2.search("fire wall", k=1)
    assert len(results) == 1
    assert results[0].metadata["section"] == "§4.2"


def test_upsert_no_duplication(store):
    """Re-ingesting the same text + same source should NOT duplicate."""
    store.ingest_text("Fire rated corridor wall.", source="bep.pdf", section="§4.2")
    initial = store.count
    store.ingest_text("Fire rated corridor wall.", source="bep.pdf", section="§4.2")
    assert store.count == initial  # upsert, not insert


def test_chunk_dataclass_immutable():
    c = Chunk(text="x", metadata={"a": 1}, score=0.5, chunk_id="id1")
    with pytest.raises(Exception):
        c.text = "y"  # type: ignore[misc]


def test_default_embedder_says_what_to_install_when_extra_is_absent(monkeypatch):
    """`sentence-transformers` is an optional extra (it owns torch, ~476 MB).

    Someone who reaches the default embedder without it must get a sentence
    they can act on, not a bare ImportError from inside the function.
    """
    import builtins

    from bim_orchestrator.rag import store

    monkeypatch.setattr(store, "_DEFAULT_MODEL", None)
    real_import = builtins.__import__

    def _no_sentence_transformers(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_sentence_transformers)

    with pytest.raises(ImportError) as excinfo:
        store._default_embed_fn()

    msg = str(excinfo.value)
    assert "--extra rag" in msg, "must name the install command"
    assert "embed_fn" in msg, "must name the way to avoid it entirely"
