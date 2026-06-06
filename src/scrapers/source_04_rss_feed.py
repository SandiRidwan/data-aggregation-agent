"""
source_04_rss_feed.py — RSS / Atom Feed Aggregator
Aggregates multiple RSS/Atom feeds into one normalized record list.

Advanced patterns:
- Multi-feed aggregation: one scraper handles N feeds (config-driven)
- Feed health check: marks dead feeds without crashing pipeline
- Dedup across feeds: same article from multiple feeds → kept once (by URL)
- Date normalization: feedparser returns 9-tuple → ISO string
- Summary HTML stripping: removes <p><b> tags from RSS summaries

Replace FEED_URLS with real feed URLs from client source list.
"""

import logging
import os
import re
import time
from datetime import datetime
from typing import Optional
import requests
import feedparser
from .base import BaseScraper

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
# Add real feed URLs here. One scraper handles all of them.

FEED_URLS = [
    os.getenv("SOURCE_04_FEED_1", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    os.getenv("SOURCE_04_FEED_2", "https://techcrunch.com/feed/"),
    os.getenv("SOURCE_04_FEED_3", "https://hnrss.org/frontpage"),
]

MAX_ENTRIES_PER_FEED = int(os.getenv("SOURCE_04_MAX_ENTRIES", "50"))
FEED_TIMEOUT         = 15  # Seconds per feed


class RssFeedScraper(BaseScraper):
    """
    Aggregates multiple RSS/Atom feeds.
    Dead feeds are logged and skipped — pipeline continues.
    """

    source_name = "rss_feed_source"
    source_type = "rss_feed"
    delay_min   = 0.5
    delay_max   = 1.0

    def _fetch_records(self) -> list[dict]:
        all_records = []
        seen_urls   = set()  # Dedup across feeds
        feed_urls   = [u for u in FEED_URLS if u]  # Filter empty env vars

        for feed_url in feed_urls:
            self.logger.info(f"Fetching feed: {feed_url}")
            entries = self._fetch_feed(feed_url)

            added = 0
            for entry in entries[:MAX_ENTRIES_PER_FEED]:
                url = entry.get("raw_url", "").strip()
                if url and url in seen_urls:
                    continue  # Dedup
                if url:
                    seen_urls.add(url)
                all_records.append(entry)
                added += 1

            self.logger.debug(f"Feed {feed_url}: +{added} entries")
            self._jitter()

        return all_records

    def _fetch_feed(self, feed_url: str) -> list[dict]:
        """
        Download and parse one RSS/Atom feed.
        Returns [] on any error — never raises.
        """
        try:
            # Download feed with timeout (feedparser.parse() has no timeout)
            resp = requests.get(feed_url, timeout=FEED_TIMEOUT, headers={
                "User-Agent": "DataAggregator/1.0 (RSS Reader)"
            })
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

        except requests.RequestException as e:
            self.logger.warning(f"Dead feed {feed_url}: {e}")
            return []
        except Exception as e:
            self.logger.warning(f"Feed parse error {feed_url}: {e}")
            return []

        if feed.bozo and not feed.entries:
            self.logger.warning(f"Malformed feed (bozo=True, 0 entries): {feed_url}")
            return []

        records = []
        for entry in feed.entries:
            try:
                records.append({
                    "raw_title":   getattr(entry, "title",   "").strip(),
                    "raw_desc":    self._strip_html(
                                       getattr(entry, "summary", "") or
                                       getattr(entry, "description", "")
                                   ),
                    "raw_url":     getattr(entry, "link",    "").strip(),
                    "raw_date":    self._parse_date(entry),
                    "raw_org":     getattr(feed.feed, "title", "").strip(),
                    "raw_feed":    feed_url,
                    "raw_tags":    self._extract_tags(entry),
                })
            except Exception as e:
                self.logger.debug(f"Skipping malformed entry: {e}")
                continue

        return records

    def field_map(self) -> dict:
        return {
            "title":          "raw_title",
            "description":    "raw_desc",
            "source_name":    lambda r: self.source_name,
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       "raw_tags",
            "organization":   "raw_org",
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(entry) -> str:
        """
        feedparser returns published_parsed as a time.struct_time 9-tuple.
        Convert to ISO date string.
        """
        for attr in ("published_parsed", "updated_parsed", "created_parsed"):
            parsed = getattr(entry, attr, None)
            if parsed:
                try:
                    return datetime(*parsed[:6]).strftime("%Y-%m-%d")
                except Exception:
                    continue
        return ""

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from RSS summary field."""
        if not text:
            return ""
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean[:2000]  # Cap description length

    @staticmethod
    def _extract_tags(entry) -> str:
        """Extract category/tag string from RSS entry tags list."""
        tags = getattr(entry, "tags", [])
        if not tags:
            return ""
        return ", ".join(
            t.get("term", "") for t in tags if t.get("term")
        )[:128]