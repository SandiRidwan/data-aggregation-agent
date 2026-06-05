"""
source_03_rest_api.py — REST API Source Scraper
Uses httpx for modern HTTP/2 support + automatic retry on rate limits.

Advanced patterns:
- HTTP/2 support via httpx (faster than requests for API calls)
- 429 rate limit detection + Retry-After header parsing
- Cursor-based pagination (modern APIs use cursor, not page number)
- Response schema validation before processing
- ETag/Last-Modified caching headers (avoid re-downloading unchanged data)

Replace API_BASE_URL, endpoints, and field names with real API values.
"""

import logging
import os
import time
from typing import Optional
import httpx
from .base import BaseScraper

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

API_BASE_URL = os.getenv("SOURCE_03_API_URL",  "https://api.example.com/v1")
API_KEY      = os.getenv("SOURCE_03_API_KEY",  "")
PAGE_SIZE    = int(os.getenv("SOURCE_03_PAGE_SIZE", "50"))
MAX_RECORDS  = int(os.getenv("SOURCE_03_MAX_RECORDS", "500"))


class RestApiScraper(BaseScraper):
    """
    Scraper for paginated REST APIs.
    Supports both page-number pagination and cursor-based pagination.
    """

    source_name = "rest_api_source"
    source_type = "rest_api"
    delay_min   = 0.3
    delay_max   = 0.8

    def __init__(self):
        super().__init__()
        self._client = httpx.Client(
            http2=True,
            timeout=20,
            headers={
                "Accept":        "application/json",
                "Authorization": f"Bearer {API_KEY}" if API_KEY else "",
                "User-Agent":    "DataAggregator/1.0",
            }
        )
        self._etag:          Optional[str] = None
        self._last_modified: Optional[str] = None

    def _fetch_records(self) -> list[dict]:
        raw_records = []
        cursor      = None       # For cursor-based pagination
        page        = 1          # For page-number pagination

        while len(raw_records) < MAX_RECORDS:
            batch, next_cursor = self._fetch_page(page=page, cursor=cursor)

            if not batch:
                self.logger.info(f"Empty batch at page {page}, done")
                break

            raw_records.extend(batch)
            self.logger.debug(f"Page {page}: +{len(batch)} records (total: {len(raw_records)})")

            # Determine next page
            if next_cursor:
                cursor = next_cursor  # Cursor pagination
            else:
                page += 1            # Page-number pagination

            if len(batch) < PAGE_SIZE:
                break  # Last page (partial batch)

            self._jitter()

        return raw_records[:MAX_RECORDS]

    def _fetch_page(self, page: int = 1, cursor: Optional[str] = None
                    ) -> tuple[list[dict], Optional[str]]:
        """
        Fetch one page of results.
        Returns (records_list, next_cursor_or_None).
        """
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        else:
            params["page"] = page

        # ETag caching — send If-None-Match if we have a previous ETag
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.get(
                    f"{API_BASE_URL}/listings",
                    params=params,
                    headers=headers,
                )

                # 304 Not Modified — data unchanged since last request
                if resp.status_code == 304:
                    self.logger.info("304 Not Modified — cached data still valid")
                    return [], None

                # 429 Rate Limited — respect Retry-After header
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    self.logger.warning(f"Rate limited, waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue

                resp.raise_for_status()

                # Cache ETag for next run
                if "ETag" in resp.headers:
                    self._etag = resp.headers["ETag"]

                data         = resp.json()
                records      = self._extract_records(data)
                next_cursor  = data.get("next_cursor") or data.get("cursor") or None

                return records, next_cursor

            except httpx.HTTPStatusError as e:
                self.logger.warning(f"HTTP error on page {page} attempt {attempt}: {e}")
            except Exception as e:
                self.logger.warning(f"Error on page {page} attempt {attempt}: {e}")

            time.sleep(attempt * 2)

        self.logger.error(f"All retries failed for page {page}")
        return [], None

    def _extract_records(self, data: dict) -> list[dict]:
        """
        Extract record list from API response.
        API responses vary — adjust key names to match real API.
        Validates that response is a dict with expected structure.
        """
        if not isinstance(data, dict):
            self.logger.warning(f"Unexpected API response type: {type(data)}")
            return []

        # Try common response envelope keys
        for key in ("results", "items", "data", "records", "opportunities"):
            if key in data and isinstance(data[key], list):
                return data[key]

        # Flat list response (no envelope)
        if isinstance(data, list):
            return data

        self.logger.warning(f"Could not find records in response keys: {list(data.keys())}")
        return []

    def field_map(self) -> dict:
        """Adjust field names to match real API response schema."""
        return {
            "title":          "title",
            "description":    "description",
            "source_name":    lambda r: self.source_name,
            "published_date": "posted_date",
            "url":            "url",
            "category":       "category",
            "organization":   "organization_name",
            "budget":         "award_ceiling",
            "deadline":       "close_date",
            "contact_email":  "contact_email",
        }

    def __del__(self):
        """Close httpx client cleanly."""
        try:
            self._client.close()
        except Exception:
            pass