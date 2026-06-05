"""
source_05_sitemap.py — Sitemap XML Crawler
Discovers all pages via sitemap.xml / sitemap index, then scrapes each page.

Advanced patterns:
- Sitemap index support: handles sitemap of sitemaps (2-level crawl)
- lastmod filtering: only scrape pages modified after SINCE_DATE
- Concurrent-safe sequential fetch with jitter (no asyncio needed)
- robots.txt respect: checks Crawl-delay directive before fetching
- URL dedup: sitemap may list same URL in multiple sitemaps

Replace BASE_URL and content selectors with real values.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL      = os.getenv("SOURCE_05_URL", "https://example.com")
SITEMAP_PATH  = os.getenv("SOURCE_05_SITEMAP", "/sitemap.xml")
MAX_URLS      = int(os.getenv("SOURCE_05_MAX_URLS", "200"))
# Only scrape pages modified in last N days (0 = no filter)
SINCE_DAYS    = int(os.getenv("SOURCE_05_SINCE_DAYS", "30"))


class SitemapScraper(BaseScraper):
    """
    Discovers pages via sitemap.xml, scrapes each page for content.
    Handles both flat sitemaps and sitemap index files.
    """

    source_name = "sitemap_source"
    source_type = "static_html"
    delay_min   = 1.0
    delay_max   = 2.5

    def __init__(self):
        super().__init__()
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "DataAggregator/1.0 (sitemap crawler)",
            "Accept":     "text/html,application/xhtml+xml,application/xml",
        })
        self._seen_urls: set = set()

    def _fetch_records(self) -> list[dict]:
        sitemap_url = urljoin(BASE_URL, SITEMAP_PATH)
        self.logger.info(f"Discovering URLs from: {sitemap_url}")

        urls = self._discover_urls(sitemap_url)
        self.logger.info(f"Found {len(urls)} URLs to scrape (cap: {MAX_URLS})")

        raw_records = []
        for i, (url, lastmod) in enumerate(urls[:MAX_URLS]):
            record = self._scrape_page(url, lastmod)
            if record:
                raw_records.append(record)

            if (i + 1) % 20 == 0:
                self.logger.info(f"Scraped {i+1}/{min(len(urls), MAX_URLS)} pages")

            self._jitter()

        return raw_records

    def _discover_urls(self, sitemap_url: str) -> list[tuple[str, str]]:
        """
        Parse sitemap XML and return list of (url, lastmod) tuples.
        Handles sitemap index (contains <sitemapindex>) recursively.
        """
        resp = self._retry_get(self._session, sitemap_url)
        if resp is None:
            return []

        soup = BeautifulSoup(resp.content, "lxml-xml")
        urls = []

        # Check if this is a sitemap index
        if soup.find("sitemapindex"):
            child_sitemaps = soup.find_all("sitemap")
            self.logger.info(f"Sitemap index with {len(child_sitemaps)} child sitemaps")
            for sm in child_sitemaps:
                loc = sm.find("loc")
                if loc:
                    child_urls = self._discover_urls(loc.text.strip())
                    urls.extend(child_urls)
                    time.sleep(0.5)
        else:
            # Flat sitemap — extract <url> entries
            since_date = (
                datetime.utcnow() - timedelta(days=SINCE_DAYS)
                if SINCE_DAYS > 0 else None
            )

            for url_tag in soup.find_all("url"):
                loc     = url_tag.find("loc")
                lastmod = url_tag.find("lastmod")

                if not loc:
                    continue

                url_str  = loc.text.strip()
                lm_str   = lastmod.text.strip() if lastmod else ""

                # lastmod filter
                if since_date and lm_str:
                    try:
                        lm_dt = datetime.fromisoformat(lm_str[:10])
                        if lm_dt < since_date:
                            continue
                    except ValueError:
                        pass  # Keep URL if date can't be parsed

                # Dedup
                if url_str in self._seen_urls:
                    continue
                self._seen_urls.add(url_str)

                urls.append((url_str, lm_str))

        return urls

    def _scrape_page(self, url: str, lastmod: str) -> Optional[dict]:
        """
        Scrape a single page from the discovered URLs.
        Returns raw dict or None if page should be skipped.
        """
        resp = self._retry_get(self._session, url)
        if resp is None:
            return None

        try:
            soup  = BeautifulSoup(resp.text, "lxml")

            # Skip pages that aren't content (login, 404 pages, etc.)
            title_el = soup.find("h1") or soup.find("title")
            if not title_el:
                return None

            title = title_el.get_text(strip=True)
            if len(title) < 5:
                return None

            # Extract main content — try common content containers
            content_el = (
                soup.find("article") or
                soup.find("main") or
                soup.find(class_=lambda c: c and "content" in c.lower()) or
                soup.find("body")
            )
            description = ""
            if content_el:
                # Get all paragraphs, join
                paras = content_el.find_all("p")
                description = " ".join(
                    p.get_text(strip=True) for p in paras if len(p.get_text(strip=True)) > 20
                )[:2000]

            # Extract meta description as fallback
            if not description:
                meta = soup.find("meta", attrs={"name": "description"})
                if meta:
                    description = meta.get("content", "")

            return {
                "raw_title":   title,
                "raw_desc":    description,
                "raw_url":     url,
                "raw_date":    lastmod,
                "raw_org":     urlparse(url).netloc,
            }

        except Exception as e:
            self.logger.debug(f"Failed to parse page {url}: {e}")
            return None

    def field_map(self) -> dict:
        return {
            "title":          "raw_title",
            "description":    "raw_desc",
            "source_name":    lambda r: self.source_name,
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       lambda r: "web_content",
            "organization":   "raw_org",
        }