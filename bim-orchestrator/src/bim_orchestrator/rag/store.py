"""ChromaDB-backed vector store for BEP / IBC retrieval.

Phase 2 Day 1: persistent local store + paragraph-aware chunking + injectable
embedder. The default embedder lazily loads `sentence-transformers/all-MiniLM-L6-v2`
on first use (~90MB download, then cached at ~/.cache/huggingface). Tests pass
a deterministic `fake_embed` to avoid the download and stay fast.

Design notes:
    * Each chunk is stored with metadata: {source, page, section, chunk_idx}.
    * Persistence is ON by default — re-instantiating reuses the on-disk index.
    * Embedding is decoupled via `embed_fn` so unit tests can use bag-of-words
      and integration tests can use real sentence-transformers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import chromadb
import structlog

from bim_orchestrator.rag.chunker import chunk_text
from bim_orchestrator.rag.pdf_extractor import extract_pages

log = structlog.get_logger(__name__)

EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class Chunk:
    """A retrieval result: text + source metadata + similarity score."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    chunk_id: str = ""


class VectorStore:
    """ChromaDB-backed store with pluggable embedder.

    Usage:
        store = VectorStore(persist_dir="./data/chroma", collection="bep")
        store.ingest_text("...", source="bep.pdf", section="§4.2")
        results = store.search("fire rating", k=3)
    """

    def __init__(
        self,
        persist_dir: str | Path | None = None,
        *,
        collection: str = "bep",
        embed_fn: EmbedFn | None = None,
    ) -> None:
        self._embed_fn: EmbedFn | None = embed_fn
        self._collection_name = collection

        if persist_dir is not None:
            persist_path = Path(persist_dir)
            persist_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(persist_path))
        else:
            self._client = chromadb.EphemeralClient()

        # We embed externally and pass vectors directly → no chromadb-side embed fn.
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "vector_store.opened",
            collection=collection,
            persist_dir=str(persist_dir) if persist_dir else "<ephemeral>",
            count=self._collection.count(),
        )

    @property
    def count(self) -> int:
        return self._collection.count()

    # ---- ingestion ------------------------------------------------------

    def ingest_text(
        self,
        text: str,
        *,
        source: str,
        section: str | None = None,
        page: int | None = None,
        max_chars: int = 2000,
        overlap: int = 200,
        extra_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Chunk → embed → upsert. Returns number of chunks added."""
        chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
        if not chunks:
            return 0
        metadatas = []
        for idx, chunk in enumerate(chunks):
            meta = {
                "source": source,
                "chunk_idx": idx,
                **({"section": section} if section else {}),
                **({"page": page} if page is not None else {}),
                **(extra_metadata or {}),
            }
            metadatas.append(meta)
        return self._add_chunks(chunks, metadatas)

    def ingest_pdf(
        self,
        pdf_path: str | Path,
        *,
        source: str | None = None,
        max_chars: int = 2000,
        overlap: int = 200,
    ) -> int:
        """Extract pages → chunk → embed → upsert. Returns total chunks added."""
        path = Path(pdf_path)
        source = source or path.name
        pages = extract_pages(path)
        total = 0
        for page in pages:
            if not page.text:
                continue
            total += self.ingest_text(
                page.text,
                source=source,
                page=page.page_number,
                max_chars=max_chars,
                overlap=overlap,
            )
        log.info(
            "vector_store.pdf_ingested",
            source=source,
            pages=len(pages),
            chunks_added=total,
        )
        return total

    # ---- query ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """Return top-k chunks ordered by cosine similarity (higher is better)."""
        if self._collection.count() == 0:
            return []
        embed = self._embed([query])[0]
        result = self._collection.query(
            query_embeddings=[embed],
            n_results=min(k, self._collection.count()),
            where=where,
        )
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        chunks: list[Chunk] = []
        for cid, doc, meta, dist in zip(ids, docs, metas, distances):
            # Chroma returns cosine *distance*; convert to similarity in [0, 1].
            similarity = max(0.0, 1.0 - float(dist))
            chunks.append(
                Chunk(text=doc, metadata=dict(meta or {}), score=similarity, chunk_id=cid)
            )
        return chunks

    # ---- internals ------------------------------------------------------

    def _add_chunks(self, texts: list[str], metadatas: list[dict[str, Any]]) -> int:
        if not texts:
            return 0
        ids = [_chunk_id(meta, text) for meta, text in zip(metadatas, texts)]
        embeddings = self._embed(texts)
        # Upsert so re-ingesting the same source replaces, not duplicates.
        self._collection.upsert(
            ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings
        )
        return len(texts)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self._embed_fn is None:
            self._embed_fn = _default_embed_fn()
        return self._embed_fn(texts)


def _chunk_id(meta: dict[str, Any], text: str) -> str:
    """Deterministic id so re-ingesting same chunk updates rather than duplicates."""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    src = meta.get("source", "unknown")
    page = meta.get("page", "")
    idx = meta.get("chunk_idx", "")
    return f"{src}:{page}:{idx}:{h}"


# ---- default embedder (lazy sentence-transformers) ------------------------


_DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_MODEL: Any = None


_MISSING_EMBEDDER_HINT = (
    "The default RAG embedder needs `sentence-transformers`, which is an "
    "optional dependency because it pulls in torch (~476 MB) and nothing else "
    "in this project uses it.\n"
    "  Install it:   uv sync --extra dev --extra rag\n"
    "  Or avoid it:  pass your own `embed_fn=` to VectorStore -- the engine, "
    "the reports and `--demo` never touch this path."
)


def _default_embed_fn() -> EmbedFn:
    """Lazily load sentence-transformers on first use.

    Optional dependency (extra `rag`): raise something a human can act on
    rather than letting a bare ImportError surface from three frames down.
    """
    global _DEFAULT_MODEL
    if _DEFAULT_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised by test below
            raise ImportError(_MISSING_EMBEDDER_HINT) from exc

        log.info("vector_store.loading_default_model", model=_DEFAULT_MODEL_NAME)
        _DEFAULT_MODEL = SentenceTransformer(_DEFAULT_MODEL_NAME)

    def _encode(texts: list[str]) -> list[list[float]]:
        vectors = _DEFAULT_MODEL.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return vectors.tolist()

    return _encode
