"""
source_static_example.py — Template: Static HTML Scraper
Replace this with the real source once client provides the URL list.

Demonstrates:
- How to extend BaseScraper for a static HTML source
- BeautifulSoup parsing pattern
- field_map() usage
- Pagination handling
"""

import logging
import requests
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)


class StaticHtmlSourceScraper(BaseScraper):
    """
    Template for a static HTML source.
    Replace BASE_URL and selectors with real values from client's source list.
    """

    source_name = "static_example"
    source_type = "static_html"
    delay_min   = 1.0
    delay_max   = 2.5

    BASE_URL    = "https://example.com/listings"  # Replace with real URL
    MAX_PAGES   = 10                               # Adjust per source

    def __init__(self):
        super().__init__()
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _fetch_records(self) -> list[dict]:
        raw_records = []

        for page in range(1, self.MAX_PAGES + 1):
            url      = f"{self.BASE_URL}?page={page}"
            response = self._retry_get(self._session, url)

            if response is None:
                self.logger.warning(f"Page {page} failed, stopping pagination")
                break

            soup    = BeautifulSoup(response.text, "lxml")
            items   = soup.select(".listing-item")  # Replace with real selector

            if not items:
                self.logger.info(f"No items on page {page}, end of results")
                break

            for item in items:
                raw_records.append(self._parse_item(item))

            self.logger.debug(f"Page {page}: {len(items)} items")
            self._jitter()

        return raw_records

    def _parse_item(self, item) -> dict:
        """Extract fields from a single listing element."""
        return {
            "raw_title":       self._text(item, ".listing-title"),
            "raw_description": self._text(item, ".listing-description"),
            "raw_date":        self._text(item, ".listing-date"),
            "raw_url":         self._attr(item, "a.listing-link", "href"),
            "raw_org":         self._text(item, ".listing-org"),
        }

    def field_map(self) -> dict:
        """Map source fields to canonical schema."""
        return {
            "title":          "raw_title",
            "description":    "raw_description",
            "source_name":    lambda r: self.source_name,
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       lambda r: "opportunity",
            "organization":   "raw_org",
        }

    # ── HTML Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _text(soup_el, selector: str) -> str:
        el = soup_el.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _attr(soup_el, selector: str, attr: str) -> str:
        el = soup_el.select_one(selector)
        return el.get(attr, "") if el else ""
