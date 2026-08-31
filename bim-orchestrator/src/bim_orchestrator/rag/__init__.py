"""RAG layer for the Grounding Agent (Phase 2).

Phase 2 Day 1: local ChromaDB vector store with PDF ingestion.
Phase 2 Week 4 Day 1: retrieval quality eval harness + IBC fixture.
Used by the Grounding Agent to retrieve BEP / IBC sections per finding.
"""

from bim_orchestrator.rag.chunker import chunk_text
from bim_orchestrator.rag.eval import EvalQuery, EvalReport, EvalResult, run_eval
from bim_orchestrator.rag.pdf_extractor import extract_pages
from bim_orchestrator.rag.store import Chunk, VectorStore

__all__ = [
    "Chunk",
    "EvalQuery",
    "EvalReport",
    "EvalResult",
    "VectorStore",
    "chunk_text",
    "extract_pages",
    "run_eval",
]
