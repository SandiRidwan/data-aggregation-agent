"""
pdf_extractor.py — PDF Text & Table Extraction
Primary: pdfplumber (clean PDFs, tables)
Fallback: PyMuPDF/fitz (scanned PDFs, image-heavy, malformed)

Advanced patterns used:
- Two-pass extraction: try pdfplumber first, fallback to fitz if result empty/broken
- Table-aware extraction: tables converted to markdown before joining prose
- Scanned PDF detection: if text yield < MIN_CHARS_PER_PAGE → flag as image-based
- Chunk mode: split long PDFs into N-char chunks for LLM context window
- Graceful partial failure: if page N fails, continue pages N+1..end (never crash)

Usage:
    from src.pdf_extractor import extract_pdf, extract_pdf_from_url

    # From local file
    result = extract_pdf("/path/to/file.pdf")
    print(result.text)
    print(result.tables)   # list of markdown table strings
    print(result.is_scanned)

    # From URL (downloads to temp file first)
    result = extract_pdf_from_url("https://example.com/doc.pdf")
"""

import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

MIN_CHARS_PER_PAGE   = 50     # Below this → page probably scanned/image
MAX_PAGES            = 50     # Safety cap — don't process 500-page PDFs
CHUNK_SIZE           = 4000   # Chars per chunk for LLM context window
DOWNLOAD_TIMEOUT     = 30     # Seconds for PDF download
MAX_PDF_BYTES        = 20 * 1024 * 1024  # 20 MB hard limit


# ─── Result Container ─────────────────────────────────────────────────────────

@dataclass
class PDFResult:
    text:           str   = ""          # Full extracted text, cleaned
    tables:         list  = field(default_factory=list)  # list[str] markdown tables
    pages_total:    int   = 0
    pages_extracted:int   = 0
    is_scanned:     bool  = False       # True if mostly image-based
    extraction_method: str = "none"     # "pdfplumber" | "pymupdf" | "failed"
    error:          Optional[str] = None

    @property
    def chunks(self) -> list[str]:
        """Split text into CHUNK_SIZE pieces for LLM processing."""
        if not self.text:
            return []
        return [
            self.text[i:i + CHUNK_SIZE]
            for i in range(0, len(self.text), CHUNK_SIZE)
        ]

    @property
    def word_count(self) -> int:
        return len(self.text.split()) if self.text else 0

    def __repr__(self):
        status = "scanned" if self.is_scanned else self.extraction_method
        return (f"PDFResult({status} | {self.pages_extracted}/{self.pages_total} pages "
                f"| {self.word_count} words | {len(self.tables)} tables)")


# ─── Main Entry Points ────────────────────────────────────────────────────────

def extract_pdf(path: str) -> PDFResult:
    """
    Extract text and tables from a local PDF file.
    Two-pass: pdfplumber first, PyMuPDF fallback.

    Args:
        path: absolute or relative path to .pdf file

    Returns:
        PDFResult with text, tables, metadata
    """
    if not os.path.exists(path):
        logger.error(f"PDF not found: {path}")
        return PDFResult(error=f"File not found: {path}")

    file_size = os.path.getsize(path)
    if file_size > MAX_PDF_BYTES:
        logger.warning(f"PDF too large ({file_size/1024/1024:.1f} MB), skipping: {path}")
        return PDFResult(error=f"PDF exceeds size limit ({file_size/1024/1024:.1f} MB)")

    logger.info(f"Extracting PDF: {os.path.basename(path)} ({file_size/1024:.0f} KB)")

    # Pass 1: pdfplumber
    result = _extract_pdfplumber(path)

    # Pass 2: fallback to PyMuPDF if pdfplumber yielded too little
    if _needs_fallback(result):
        logger.info(f"pdfplumber yield low ({result.word_count} words), trying PyMuPDF fallback")
        fallback = _extract_pymupdf(path)
        if fallback.word_count > result.word_count:
            result = fallback

    # Final scanned detection
    if result.pages_total > 0:
        avg_chars = len(result.text) / result.pages_total
        result.is_scanned = avg_chars < MIN_CHARS_PER_PAGE

    if result.is_scanned:
        logger.warning(f"PDF appears to be scanned/image-based: {os.path.basename(path)}")

    return result


def extract_pdf_from_url(url: str, session: Optional[requests.Session] = None) -> PDFResult:
    """
    Download PDF from URL and extract.
    Uses temp file — no disk residue after function returns.

    Args:
        url:     Direct URL to a .pdf file
        session: Optional requests.Session (e.g. authenticated session from scraper)

    Returns:
        PDFResult
    """
    logger.info(f"Downloading PDF: {url}")
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    try:
        resp = sess.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()

        # Size check from Content-Length header before downloading
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length > MAX_PDF_BYTES:
            return PDFResult(error=f"PDF too large to download: {content_length/1024/1024:.1f} MB")

        # Stream to temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded > MAX_PDF_BYTES:
                    tmp_path = tmp.name
                    os.unlink(tmp_path)
                    return PDFResult(error="PDF exceeded size limit during download")
                tmp.write(chunk)
            tmp_path = tmp.name

        result = extract_pdf(tmp_path)

    except (requests.RequestException, Exception) as e:
        logger.error(f"Failed to download PDF from {url}: {e}")
        return PDFResult(error=f"Download failed: {e}")
    finally:
        # Always clean up temp file
        if "tmp_path" in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return result


# ─── Pass 1: pdfplumber ───────────────────────────────────────────────────────

def _extract_pdfplumber(path: str) -> PDFResult:
    """
    Extract text and tables using pdfplumber.
    Best for: clean digital PDFs, PDFs with tables.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed, skipping pass 1")
        return PDFResult(error="pdfplumber not installed")

    text_parts = []
    tables     = []
    pages_ok   = 0

    try:
        with pdfplumber.open(path) as pdf:
            pages_total = len(pdf.pages)
            cap         = min(pages_total, MAX_PAGES)

            if pages_total > MAX_PAGES:
                logger.warning(f"PDF has {pages_total} pages, capping at {MAX_PAGES}")

            for i, page in enumerate(pdf.pages[:cap]):
                try:
                    # Extract tables first (before text, so we can subtract table areas)
                    page_tables = page.extract_tables()
                    for tbl in (page_tables or []):
                        md = _table_to_markdown(tbl)
                        if md:
                            tables.append(md)
                            text_parts.append(f"\n[TABLE]\n{md}\n[/TABLE]\n")

                    # Extract remaining text (outside table bounding boxes)
                    page_text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                    page_text = _clean_text(page_text)
                    if page_text:
                        text_parts.append(page_text)

                    pages_ok += 1

                except Exception as e:
                    # Partial failure — log and continue (never crash whole doc)
                    logger.debug(f"pdfplumber failed on page {i+1}: {e}")
                    continue

        full_text = "\n\n".join(text_parts)
        return PDFResult(
            text=full_text,
            tables=tables,
            pages_total=pages_total,
            pages_extracted=pages_ok,
            extraction_method="pdfplumber",
        )

    except Exception as e:
        logger.warning(f"pdfplumber failed entirely: {e}")
        return PDFResult(error=str(e))


# ─── Pass 2: PyMuPDF (fitz) ──────────────────────────────────────────────────

def _extract_pymupdf(path: str) -> PDFResult:
    """
    Extract text using PyMuPDF (fitz).
    Best for: scanned PDFs (with OCR layer), complex layouts, malformed PDFs.
    Note: Does not extract tables — text only.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (fitz) not installed, cannot use fallback")
        return PDFResult(error="PyMuPDF not installed")

    text_parts = []
    pages_ok   = 0

    try:
        doc = fitz.open(path)
        pages_total = len(doc)
        cap         = min(pages_total, MAX_PAGES)

        for i in range(cap):
            try:
                page      = doc[i]
                page_text = page.get_text("text")  # "text" mode = plain text, fast
                page_text = _clean_text(page_text)
                if page_text:
                    text_parts.append(page_text)
                pages_ok += 1
            except Exception as e:
                logger.debug(f"PyMuPDF failed on page {i+1}: {e}")
                continue

        doc.close()
        full_text = "\n\n".join(text_parts)
        return PDFResult(
            text=full_text,
            tables=[],   # PyMuPDF doesn't give structured tables
            pages_total=pages_total,
            pages_extracted=pages_ok,
            extraction_method="pymupdf",
        )

    except Exception as e:
        logger.warning(f"PyMuPDF failed entirely: {e}")
        return PDFResult(error=str(e))


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _needs_fallback(result: PDFResult) -> bool:
    """
    Decide if pdfplumber result is too poor to use.
    Triggers fallback if: error, or fewer than MIN_CHARS_PER_PAGE per page on average.
    """
    if result.error:
        return True
    if result.pages_total == 0:
        return True
    avg_chars = len(result.text) / max(result.pages_total, 1)
    return avg_chars < MIN_CHARS_PER_PAGE


def _clean_text(text: str) -> str:
    """
    Normalize extracted text:
    - Collapse 3+ newlines → 2
    - Remove null bytes
    - Strip trailing whitespace per line
    - Collapse multiple spaces
    """
    if not text:
        return ""
    text = text.replace("\x00", "")                    # Null bytes
    text = re.sub(r"[ \t]+", " ", text)                # Multiple spaces/tabs → single space
    text = re.sub(r" *\n *", "\n", text)               # Strip spaces around newlines
    text = re.sub(r"\n{3,}", "\n\n", text)             # 3+ newlines → 2
    return text.strip()


def _table_to_markdown(table: list) -> str:
    """
    Convert pdfplumber table (list of lists) to markdown table string.
    Handles None cells and multi-line cell content.

    Example input:  [["Name", "Value"], ["Alice", "100"], ["Bob", "200"]]
    Example output:
        | Name  | Value |
        |-------|-------|
        | Alice | 100   |
        | Bob   | 200   |
    """
    if not table or not any(table):
        return ""

    # Normalize cells: None → "", strip whitespace, collapse newlines
    def clean_cell(c) -> str:
        if c is None:
            return ""
        return str(c).replace("\n", " ").strip()

    rows = [[clean_cell(c) for c in row] for row in table if row]
    if not rows:
        return ""

    # Determine column count from widest row
    n_cols = max(len(row) for row in rows)

    # Pad rows to same width
    rows = [row + [""] * (n_cols - len(row)) for row in rows]

    # Column widths
    widths = [max(len(row[i]) for row in rows) for i in range(n_cols)]
    widths = [max(w, 3) for w in widths]  # Min 3 for separator

    def fmt_row(row):
        return "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"

    header    = fmt_row(rows[0])
    separator = "| " + " | ".join("-" * widths[i] for i in range(n_cols)) + " |"
    body      = "\n".join(fmt_row(row) for row in rows[1:])

    return f"{header}\n{separator}\n{body}" if body else f"{header}\n{separator}"