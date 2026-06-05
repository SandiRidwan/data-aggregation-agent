"""
base.py — Common Scraper Abstraction
All source-specific scrapers inherit from BaseScraper.
Every scraper returns a normalized dict — scoring.py never knows
which source produced the record.

Design principle: swapping or adding a source = one new file, not a refactor.
"""

import logging
import time
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Normalized Record Schema ─────────────────────────────────────────────────
# Every scraper MUST return dicts that conform to this shape.
# Missing optional fields → None (never omit the key entirely).

REQUIRED_FIELDS = {
    "title",
    "description",
    "source_name",
    "published_date",   # ISO format: "2026-06-01" or "" if unknown
    "url",
    "category",
    "scraped_at",       # Auto-set by normalize()
    "raw_source",       # Which scraper class produced this
}

OPTIONAL_FIELDS = {
    "author",
    "organization",
    "location",
    "budget",
    "deadline",
    "contact_email",
    "tags",             # list[str]
    "pdf_text",         # Populated by pdf_extractor.py if source has PDF
    "extra",            # dict for any source-specific fields
}


# ─── Scrape Result Container ──────────────────────────────────────────────────

@dataclass
class ScrapeResult:
    records:    list[dict] = field(default_factory=list)
    source:     str = ""
    run_id:     str = ""
    success:    bool = True
    error:      Optional[str] = None
    pages_hit:  int = 0
    records_raw: int = 0   # Before dedup

    def __repr__(self):
        status = "✅" if self.success else "❌"
        return (f"ScrapeResult({status} source={self.source} | "
                f"records={len(self.records)} | pages={self.pages_hit})")


# ─── Base Scraper ─────────────────────────────────────────────────────────────

class BaseScraper(ABC):
    """
    Abstract base class for all source scrapers.

    Subclass must implement:
        - source_name: str  (e.g. "sam_gov", "grants_gov")
        - source_type: str  ("static_html" | "js_rendered" | "rest_api" | "authenticated")
        - _fetch_records() -> list[dict]

    Optional overrides:
        - _auth()       — for authenticated sources (called once before _fetch_records)
        - _post_process() — source-specific cleanup before normalization
    """

    source_name: str = "unknown"
    source_type: str = "static_html"

    # Timing defaults — override per-source if needed
    delay_min: float = 0.8
    delay_max: float = 2.0
    max_retries: int = 3

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.{self.source_name}")

    # ── Public API ────────────────────────────────────────────────────────────

    def scrape(self) -> ScrapeResult:
        """
        Main entry point. Called by the pipeline runner.
        Returns ScrapeResult with normalized records.
        """
        result = ScrapeResult(source=self.source_name)

        try:
            # Auth step (no-op for most scrapers)
            self._auth()

            # Fetch raw records
            raw = self._fetch_records()
            result.records_raw = len(raw)

            # Post-process (source-specific cleanup)
            cleaned = [self._post_process(r) for r in raw]

            # Normalize to common schema
            normalized = [self._normalize(r) for r in cleaned]
            normalized = [r for r in normalized if r is not None]  # drop None

            result.records = normalized
            result.success  = True
            self.logger.info(f"✅ {self.source_name}: {len(normalized)} records")

        except Exception as e:
            result.success = False
            result.error   = str(e)
            self.logger.error(f"❌ {self.source_name} failed: {e}", exc_info=True)

        return result

    # ── Must Override ─────────────────────────────────────────────────────────

    @abstractmethod
    def _fetch_records(self) -> list[dict]:
        """
        Fetch raw records from the source.
        Return a list of dicts — structure is source-specific.
        Do NOT normalize here; normalization happens in _normalize().
        """
        ...

    # ── Optional Override ─────────────────────────────────────────────────────

    def _auth(self) -> None:
        """
        Authenticate with the source if needed.
        Default: no-op.
        Authenticated sources override this to store session/token.
        """
        pass

    def _post_process(self, raw: dict) -> dict:
        """
        Source-specific cleanup before normalization.
        Default: pass-through.
        Override for field renaming, type coercion, etc.
        """
        return raw

    # ── Normalization (shared, not overridden) ────────────────────────────────

    def _normalize(self, raw: dict) -> Optional[dict]:
        """
        Convert source-specific dict to the common record schema.
        Subclasses provide a field_map() to define the mapping.
        Returns None if record should be dropped (e.g. missing title).
        """
        mapping = self.field_map()
        record  = {}

        for canonical_key, source_key in mapping.items():
            if callable(source_key):
                # Allow lambda transforms: {"title": lambda r: r["name"].strip()}
                try:
                    record[canonical_key] = source_key(raw)
                except Exception:
                    record[canonical_key] = None
            else:
                record[canonical_key] = raw.get(source_key)

        # Inject metadata
        record["scraped_at"]  = datetime.utcnow().isoformat()
        record["raw_source"]  = self.source_name

        # Fill optional fields with None if not set
        for opt in OPTIONAL_FIELDS:
            record.setdefault(opt, None)

        # Drop record if title is missing — nothing to score
        if not record.get("title"):
            self.logger.debug(f"Dropping record with no title: {raw}")
            return None

        return record

    @abstractmethod
    def field_map(self) -> dict:
        """
        Return mapping: {canonical_field: source_field_or_callable}

        Example:
            return {
                "title":          "grant_name",
                "description":    "abstract",
                "source_name":    lambda r: "Grants.gov",
                "published_date": "posted_date",
                "url":            "opportunity_url",
                "category":       lambda r: "federal_grant",
            }
        """
        ...

    # ── Shared Utilities ──────────────────────────────────────────────────────

    def _jitter(self) -> None:
        """Randomized delay between requests. Always call between pages."""
        delay = random.uniform(self.delay_min, self.delay_max)
        time.sleep(delay)

    def _retry_get(self, session, url: str, **kwargs) -> Optional[object]:
        """
        GET with retry + exponential backoff.
        Works with requests.Session or curl_cffi Session.
        Returns Response object or None after max_retries.
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                response = session.get(url, timeout=20, **kwargs)
                if response.status_code == 200:
                    return response
                self.logger.warning(
                    f"[{self.source_name}] HTTP {response.status_code} on attempt {attempt}: {url}"
                )
            except Exception as e:
                self.logger.warning(
                    f"[{self.source_name}] Request error attempt {attempt}: {e}"
                )
            wait = attempt * 2
            time.sleep(wait)

        self.logger.error(f"[{self.source_name}] All {self.max_retries} retries failed: {url}")
        return None
