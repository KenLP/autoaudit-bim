"""Synthetic RAG fixtures for eval / dev / demo without external PDF files."""

from bim_orchestrator.rag.fixtures.bep_room_requirements import (
    BEP_ROOM_REQUIREMENTS,
    DEFAULT_BEP_QUERIES,
    ingest_bep_room_requirements,
)
from bim_orchestrator.rag.fixtures.ibc_chapter_7 import (
    DEFAULT_IBC_QUERIES,
    IBC_CHAPTER_7,
    ingest_ibc_chapter_7,
)

__all__ = [
    "BEP_ROOM_REQUIREMENTS",
    "DEFAULT_BEP_QUERIES",
    "DEFAULT_IBC_QUERIES",
    "IBC_CHAPTER_7",
    "ingest_bep_room_requirements",
    "ingest_ibc_chapter_7",
]
