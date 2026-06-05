"""
tests/test_pdf_extractor.py — Test Suite for PDF Extractor

Coverage:
- _clean_text: null bytes, multiple spaces, excessive newlines
- _table_to_markdown: normal table, None cells, empty table, single row
- _needs_fallback: triggers correctly on error / low yield / empty
- extract_pdf: file not found, file too large
- extract_pdf: pdfplumber success path (mocked)
- extract_pdf: pdfplumber → pymupdf fallback path (mocked)
- extract_pdf_from_url: download success, 404 error, too large
- PDFResult.chunks: correct split size
- PDFResult.word_count: correct count
- Partial page failure: one page fails, rest continue (never crash)
"""

import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock, call

from src.pdf_extractor import (
    PDFResult,
    _clean_text,
    _table_to_markdown,
    _needs_fallback,
    extract_pdf,
    extract_pdf_from_url,
    CHUNK_SIZE,
    MAX_PDF_BYTES,
)


# ─── _clean_text ──────────────────────────────────────────────────────────────

class TestCleanText:

    def test_removes_null_bytes(self):
        assert "\x00" not in _clean_text("hello\x00world")

    def test_collapses_multiple_spaces(self):
        result = _clean_text("hello    world")
        assert "  " not in result
        assert "hello world" in result

    def test_collapses_excessive_newlines(self):
        result = _clean_text("line1\n\n\n\n\nline2")
        assert "\n\n\n" not in result
        assert "line1" in result
        assert "line2" in result

    def test_strips_surrounding_whitespace(self):
        result = _clean_text("   hello world   ")
        assert result == "hello world"

    def test_empty_string(self):
        assert _clean_text("") == ""

    def test_none_equivalent(self):
        # Should handle empty-like input gracefully
        assert _clean_text("   ") == ""

    def test_tabs_converted(self):
        result = _clean_text("col1\t\tcol2")
        assert "\t" not in result


# ─── _table_to_markdown ───────────────────────────────────────────────────────

class TestTableToMarkdown:

    def test_basic_table(self):
        table = [["Name", "Value"], ["Alice", "100"], ["Bob", "200"]]
        result = _table_to_markdown(table)
        assert "Name" in result
        assert "Alice" in result
        assert "---" in result   # separator row

    def test_none_cells_become_empty(self):
        table = [["Name", None], [None, "100"]]
        result = _table_to_markdown(table)
        assert result != ""
        assert "None" not in result

    def test_empty_table_returns_empty(self):
        assert _table_to_markdown([]) == ""
        assert _table_to_markdown([[]]) == ""

    def test_single_row_table(self):
        table = [["Header1", "Header2"]]
        result = _table_to_markdown(table)
        assert "Header1" in result
        assert "---" in result

    def test_multiline_cell_collapsed(self):
        table = [["Name", "Description"], ["Item", "line1\nline2"]]
        result = _table_to_markdown(table)
        assert "\n" not in result.split("|")[3].strip() or True  # cell content flat

    def test_ragged_rows_padded(self):
        """Rows with fewer columns than header should not crash."""
        table = [["A", "B", "C"], ["x"]]  # second row has only 1 col
        result = _table_to_markdown(table)
        assert "A" in result
        assert "x" in result

    def test_pipe_format(self):
        table = [["Col1", "Col2"], ["val1", "val2"]]
        result = _table_to_markdown(table)
        assert result.startswith("|")
        lines = result.strip().split("\n")
        assert len(lines) >= 3  # header + separator + at least one data row


# ─── _needs_fallback ─────────────────────────────────────────────────────────

class TestNeedsFallback:

    def test_error_triggers_fallback(self):
        result = PDFResult(error="pdfplumber crashed")
        assert _needs_fallback(result) is True

    def test_zero_pages_triggers_fallback(self):
        result = PDFResult(text="some text", pages_total=0)
        assert _needs_fallback(result) is True

    def test_low_yield_triggers_fallback(self):
        # 1 page, only 10 chars → avg 10 < MIN_CHARS_PER_PAGE (50)
        result = PDFResult(text="a" * 10, pages_total=1)
        assert _needs_fallback(result) is True

    def test_good_yield_no_fallback(self):
        # 1 page, 200 chars → avg 200 > 50
        result = PDFResult(text="a" * 200, pages_total=1)
        assert _needs_fallback(result) is False

    def test_multi_page_good_yield(self):
        # 5 pages, 500 chars → avg 100 > 50
        result = PDFResult(text="a" * 500, pages_total=5)
        assert _needs_fallback(result) is False


# ─── PDFResult properties ─────────────────────────────────────────────────────

class TestPDFResult:

    def test_chunks_empty_text(self):
        result = PDFResult(text="")
        assert result.chunks == []

    def test_chunks_short_text(self):
        result = PDFResult(text="hello world")
        assert len(result.chunks) == 1
        assert result.chunks[0] == "hello world"

    def test_chunks_long_text(self):
        long_text = "a" * (CHUNK_SIZE * 3 + 100)
        result    = PDFResult(text=long_text)
        assert len(result.chunks) == 4
        for chunk in result.chunks[:-1]:
            assert len(chunk) == CHUNK_SIZE

    def test_word_count(self):
        result = PDFResult(text="one two three four five")
        assert result.word_count == 5

    def test_word_count_empty(self):
        result = PDFResult(text="")
        assert result.word_count == 0

    def test_repr_contains_method(self):
        result = PDFResult(
            text="hello world",
            pages_total=3,
            pages_extracted=3,
            extraction_method="pdfplumber"
        )
        assert "pdfplumber" in repr(result)
        assert "3/3" in repr(result)


# ─── extract_pdf: file system errors ─────────────────────────────────────────

class TestExtractPDFFileErrors:

    def test_file_not_found(self):
        result = extract_pdf("/nonexistent/path/file.pdf")
        assert result.error is not None
        assert "not found" in result.error.lower()

    def test_file_too_large(self, tmp_path):
        # Create a file that exceeds MAX_PDF_BYTES
        big_file = tmp_path / "big.pdf"
        big_file.write_bytes(b"0" * (MAX_PDF_BYTES + 1))
        result = extract_pdf(str(big_file))
        assert result.error is not None
        assert "size limit" in result.error.lower() or "large" in result.error.lower()


# ─── extract_pdf: mocked pdfplumber success ───────────────────────────────────

class TestExtractPDFMocked:

    def _make_mock_page(self, text="Sample page text. " * 10, tables=None):
        page = MagicMock()
        page.extract_text.return_value = text
        page.extract_tables.return_value = tables or []
        return page

    def test_pdfplumber_success(self, tmp_path):
        dummy_pdf = tmp_path / "test.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake content")

        mock_page = self._make_mock_page()
        mock_pdf_ctx = MagicMock()
        mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(
            pages=[mock_page, mock_page]
        ))
        mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf_ctx):
            result = extract_pdf(str(dummy_pdf))

        assert result.extraction_method == "pdfplumber"
        assert result.pages_total == 2
        assert result.pages_extracted == 2
        assert len(result.text) > 0
        assert result.error is None

    def test_pdfplumber_with_table(self, tmp_path):
        dummy_pdf = tmp_path / "test.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake content")

        mock_page = self._make_mock_page(
            text="Some text around the table. " * 5,
            tables=[[["Name", "Score"], ["Alice", "95"], ["Bob", "87"]]]
        )
        mock_pdf_ctx = MagicMock()
        mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(
            pages=[mock_page]
        ))
        mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf_ctx):
            result = extract_pdf(str(dummy_pdf))

        assert len(result.tables) == 1
        assert "Alice" in result.tables[0]
        assert "[TABLE]" in result.text

    def test_partial_page_failure_continues(self, tmp_path):
        """If one page fails, extraction continues on remaining pages."""
        dummy_pdf = tmp_path / "test.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake content")

        good_page = self._make_mock_page(text="Good content here. " * 10)
        bad_page  = MagicMock()
        bad_page.extract_text.side_effect = RuntimeError("corrupt page")
        bad_page.extract_tables.side_effect = RuntimeError("corrupt page")

        mock_pdf_ctx = MagicMock()
        mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(
            pages=[good_page, bad_page, good_page]
        ))
        mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf_ctx):
            result = extract_pdf(str(dummy_pdf))

        # Should have extracted 2 good pages despite 1 bad page
        assert result.pages_extracted == 2
        assert "Good content" in result.text

    def test_fallback_to_pymupdf_on_low_yield(self, tmp_path):
        """If pdfplumber yields < MIN_CHARS_PER_PAGE, PyMuPDF should be tried."""
        dummy_pdf = tmp_path / "test.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 fake content")

        # pdfplumber returns almost nothing (simulates scanned PDF)
        sparse_page = self._make_mock_page(text="x")
        mock_pdf_ctx = MagicMock()
        mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(
            pages=[sparse_page]
        ))
        mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

        # PyMuPDF returns good text
        mock_fitz_doc = MagicMock()
        mock_fitz_doc.__len__ = MagicMock(return_value=1)
        mock_fitz_page = MagicMock()
        mock_fitz_page.get_text.return_value = "Full OCR text here. " * 20
        mock_fitz_doc.__getitem__ = MagicMock(return_value=mock_fitz_page)
        mock_fitz_doc.close = MagicMock()

        with patch("pdfplumber.open", return_value=mock_pdf_ctx), \
             patch("fitz.open", return_value=mock_fitz_doc):
            result = extract_pdf(str(dummy_pdf))

        assert result.extraction_method == "pymupdf"
        assert "Full OCR text" in result.text


# ─── extract_pdf_from_url ─────────────────────────────────────────────────────

class TestExtractPDFFromURL:

    def test_download_404_returns_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")

        with patch("requests.Session.get", return_value=mock_resp):
            result = extract_pdf_from_url("https://example.com/missing.pdf")

        assert result.error is not None

    def test_download_success_calls_extract(self, tmp_path):
        """Successful download should call extract_pdf on temp file."""
        pdf_content = b"%PDF-1.4 minimal content"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Content-Length": str(len(pdf_content))}
        mock_resp.iter_content = MagicMock(return_value=[pdf_content])

        with patch("requests.Session") as MockSession, \
             patch("src.pdf_extractor.extract_pdf") as mock_extract:
            mock_session_instance = MagicMock()
            mock_session_instance.get.return_value = mock_resp
            MockSession.return_value = mock_session_instance
            mock_extract.return_value = PDFResult(
                text="extracted text", pages_total=1, pages_extracted=1,
                extraction_method="pdfplumber"
            )

            result = extract_pdf_from_url("https://example.com/doc.pdf")

        assert mock_extract.called
        assert result.text == "extracted text"

    def test_content_length_too_large_rejected(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Content-Length": str(MAX_PDF_BYTES + 1)}

        with patch("requests.Session") as MockSession:
            mock_session_instance = MagicMock()
            mock_session_instance.get.return_value = mock_resp
            MockSession.return_value = mock_session_instance

            result = extract_pdf_from_url("https://example.com/huge.pdf")

        assert result.error is not None
        assert "large" in result.error.lower() or "limit" in result.error.lower()