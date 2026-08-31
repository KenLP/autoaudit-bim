"""PDF → per-page text extraction via pypdf.

Phase 2 Day 1: text-only extraction. Tables go through pdfplumber later if
needed; most BEP / IBC content is paragraph text, so pypdf is sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class PdfPage:
    page_number: int  # 1-indexed
    text: str


def extract_pages(pdf_path: str | Path) -> list[PdfPage]:
    """Return per-page text from a PDF. Pages that fail extraction yield empty strings."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    reader = PdfReader(str(path))
    out: list[PdfPage] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        out.append(PdfPage(page_number=i, text=text.strip()))
    return out
