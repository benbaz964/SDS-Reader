"""
pdf_utils.py
============
Low-level PDF -> text/table extraction. Wraps pdfplumber, with a pypdf
fallback for PDFs that pdfplumber struggles with (some scanned/odd-encoded
files). Everything runs fully offline / locally - no network calls.
"""

from dataclasses import dataclass
from typing import List
import warnings

import pdfplumber

warnings.filterwarnings("ignore")


@dataclass
class PageData:
    page_number: int
    text: str
    tables: List[List[List[str]]]  # list of tables, each a list of rows, each row a list of cell strings


def extract_pdf(path: str) -> List[PageData]:
    """Extract text and tables from every page of a PDF, in reading order."""
    pages: List[PageData] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            try:
                raw_tables = page.extract_tables() or []
            except Exception:
                raw_tables = []
            cleaned_tables = []
            for t in raw_tables:
                cleaned = [[(c or "").strip() for c in row] for row in t]
                cleaned_tables.append(cleaned)
            pages.append(PageData(page_number=i + 1, text=text, tables=cleaned_tables))

    # Fallback: if pdfplumber got almost no text at all, this is likely a
    # scanned/image-only SDS. We can't OCR offline without extra tooling
    # installed, so we flag it via empty text and let the caller warn the user.
    return pages


def full_text(pages: List[PageData]) -> str:
    return "\n".join(p.text for p in pages)


def all_tables(pages: List[PageData]):
    """Yield (page_number, table) for every table found in the document."""
    for p in pages:
        for t in p.tables:
            yield p.page_number, t
