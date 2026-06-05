"""
source_06_pdf_source.py — Site with PDF Attachments
Scrapes a listing page, finds PDF links, downloads + extracts each PDF.
Integrates with pdf_extractor.py.

Advanced patterns:
- Two-phase scrape: first get listing page, then fetch each PDF
- PDF link detection: finds .pdf links via href pattern + Content-Type check
- Concurrent-safe sequential processing (no race conditions)
- Score-ready output: pdf_text field populated for LLM deep scoring
- Skip non-PDF links silently (images, zip files, etc.)

Replace BASE_URL and selectors with real values from client source list.
"""

import logging
import os
import re
from typing import Optional
import requests
from bs4 import BeautifulSoup
from .base import BaseScraper
from src.pdf_extractor import extract_pdf_from_url, PDFResult

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL     = os.getenv("SOURCE_06_URL", "https://example.com/publications")
MAX_PAGES    = int(os.getenv("SOURCE_06_MAX_PAGES", "5"))
MAX_PDFS     = int(os.getenv("SOURCE_06_MAX_PDFS",  "30"))

# Min word count for PDF to be worth scoring
MIN_PDF_WORDS = 50


class PdfSourceScraper(BaseScraper):
    """
    Scrapes pages that contain PDF download links.
    For each listing: scrapes metadata from HTML, then extracts full text from PDF.
    """

    source_name = "pdf_source"
    source_type = "static_html"
    delay_min   = 2.0
    delay_max   = 4.0

    def __init__(self):
        super().__init__()
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        })
        self._pdf_count = 0

    def _fetch_records(self) -> list[dict]:
        raw_records = []

        for page_num in range(1, MAX_PAGES + 1):
            if self._pdf_count >= MAX_PDFS:
                self.logger.info(f"Reached MAX_PDFS ({MAX_PDFS}), stopping")
                break

            url  = f"{BASE_URL}?page={page_num}"
            resp = self._retry_get(self._session, url)
            if resp is None:
                break

            soup     = BeautifulSoup(resp.text, "lxml")
            listings = soup.select(".listing-item")  # Replace with real selector

            if not listings:
                self.logger.info(f"No listings on page {page_num}, done")
                break

            for listing in listings:
                if self._pdf_count >= MAX_PDFS:
                    break

                record = self._process_listing(listing)
                if record:
                    raw_records.append(record)

            self.logger.info(f"Page {page_num}: {len(raw_records)} records so far")
            self._jitter()

        return raw_records

    def _process_listing(self, listing) -> Optional[dict]:
        """
        Extract metadata from listing HTML element,
        find PDF link, download and extract text.
        """
        # Extract metadata from HTML
        title = self._text(listing, ".listing-title")
        if not title:
            return None

        desc     = self._text(listing, ".listing-summary")
        date     = self._text(listing, ".listing-date")
        org      = self._text(listing, ".listing-org")
        page_url = self._href(listing, "a.listing-link")

        # Find PDF link — try direct PDF href first, then follow listing page
        pdf_url = self._find_pdf_link(listing)

        if not pdf_url and page_url:
            pdf_url = self._find_pdf_on_detail_page(page_url)

        # Extract PDF text
        pdf_text = ""
        if pdf_url:
            self.logger.debug(f"Extracting PDF: {pdf_url}")
            pdf_result = extract_pdf_from_url(pdf_url, session=self._session)

            if pdf_result.error:
                self.logger.warning(f"PDF extraction failed: {pdf_result.error}")
            elif pdf_result.word_count < MIN_PDF_WORDS:
                self.logger.debug(f"PDF too sparse ({pdf_result.word_count} words): {pdf_url}")
            else:
                pdf_text = pdf_result.text
                self._pdf_count += 1
                self.logger.debug(
                    f"PDF extracted: {pdf_result.word_count} words, "
                    f"method={pdf_result.extraction_method}"
                )

            self._jitter()

        return {
            "raw_title":    title,
            "raw_desc":     desc or (pdf_text[:500] if pdf_text else ""),
            "raw_url":      page_url or pdf_url or "",
            "raw_date":     date,
            "raw_org":      org,
            "raw_pdf_url":  pdf_url or "",
            "raw_pdf_text": pdf_text,
        }

    def _find_pdf_link(self, element) -> Optional[str]:
        """Find a PDF link directly in the element."""
        for a in element.find_all("a", href=True):
            href = a["href"]
            if self._is_pdf_link(href):
                return self._absolute_url(href)
        return None

    def _find_pdf_on_detail_page(self, page_url: str) -> Optional[str]:
        """Follow a listing URL and find PDF link on the detail page."""
        resp = self._retry_get(self._session, page_url)
        if resp is None:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            if self._is_pdf_link(a["href"]):
                return self._absolute_url(a["href"])
        return None

    @staticmethod
    def _is_pdf_link(href: str) -> bool:
        """True if href points to a PDF."""
        return bool(href and (
            href.lower().endswith(".pdf") or
            "download" in href.lower() and "pdf" in href.lower() or
            re.search(r"[?&]format=pdf", href, re.I)
        ))

    def _absolute_url(self, href: str) -> str:
        """Convert relative URLs to absolute."""
        if href.startswith("http"):
            return href
        from urllib.parse import urljoin
        return urljoin(BASE_URL, href)

    def field_map(self) -> dict:
        return {
            "title":          "raw_title",
            "description":    "raw_desc",
            "source_name":    lambda r: self.source_name,
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       lambda r: "document",
            "organization":   "raw_org",
            "pdf_text":       "raw_pdf_text",
        }

    # ── HTML Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _text(element, selector: str) -> str:
        el = element.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _href(element, selector: str) -> str:
        el = element. select_one(selector)
        return el.get("href", "") if el else ""