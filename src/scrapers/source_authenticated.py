"""
source_authenticated.py — Template: Authenticated Source with Token Refresh
For the one source that requires login + JWT/session refresh.

Pattern: SessionManager detects 401/403 → auto re-login → retry.
The scraper never knows a refresh happened — it just gets a valid session.
"""

import logging
import os
import time
import requests
from datetime import datetime, timedelta
from typing import Optional
from .base import BaseScraper

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages auth token lifecycle for an authenticated source.
    
    - Stores token in memory (not disk — no credential leak risk)
    - Checks expiry before each request
    - Runs headless re-login when token expires or 401/403 received
    - Caller never needs to handle auth logic directly
    """

    TOKEN_EXPIRY_MINUTES = 55  # Refresh 5 min before actual expiry (usually 60 min)

    def __init__(self, login_url: str, username: str, password: str):
        self._login_url   = login_url
        self._username    = username
        self._password    = password
        self._token:      Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._session = requests.Session()

    def get_session(self) -> requests.Session:
        """Return a session with a valid auth token. Refreshes if needed."""
        if self._needs_refresh():
            self._login()
        return self._session

    def invalidate(self) -> None:
        """Force refresh on next get_session() call. Call on 401/403."""
        self._token      = None
        self._expires_at = None
        logger.info("Session invalidated — will re-login on next request")

    def _needs_refresh(self) -> bool:
        if self._token is None:
            return True
        if self._expires_at and datetime.utcnow() >= self._expires_at:
            logger.info("Token expired — refreshing")
            return True
        return False

    def _login(self, max_retries: int = 3) -> None:
        """
        POST credentials to login endpoint, extract token from response.
        Adjust field names to match the actual auth endpoint.
        """
        for attempt in range(1, max_retries + 1):
            try:
                resp = self._session.post(
                    self._login_url,
                    json={"username": self._username, "password": self._password},
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()

                # Adjust these field names to match actual API response
                self._token = data.get("access_token") or data.get("token")
                if not self._token:
                    raise ValueError(f"No token in login response: {list(data.keys())}")

                # Inject token into session headers for all future requests
                self._session.headers.update({"Authorization": f"Bearer {self._token}"})

                self._expires_at = (datetime.utcnow() +
                                    timedelta(minutes=self.TOKEN_EXPIRY_MINUTES))
                logger.info(f"✅ Auth token refreshed, valid until {self._expires_at.isoformat()}")
                return

            except Exception as e:
                wait = attempt * 3
                logger.warning(f"Login attempt {attempt}/{max_retries} failed: {e}. Retry in {wait}s")
                time.sleep(wait)

        raise RuntimeError(f"Authentication failed after {max_retries} attempts")


# ─── Authenticated Scraper ────────────────────────────────────────────────────

class AuthenticatedSourceScraper(BaseScraper):
    """
    Template for the source that requires login + token refresh.
    Replace env var names, BASE_URL, and selectors with real values.
    """

    source_name = "authenticated_source"
    source_type = "authenticated"
    delay_min   = 1.5
    delay_max   = 3.0

    # Replace with real values from client
    LOGIN_URL   = os.getenv("AUTH_SOURCE_LOGIN_URL", "https://example.com/api/auth/login")
    DATA_URL    = os.getenv("AUTH_SOURCE_DATA_URL",  "https://example.com/api/records")
    PAGE_SIZE   = 50

    def __init__(self):
        super().__init__()
        self._session_manager = SessionManager(
            login_url = self.LOGIN_URL,
            username  = os.getenv("AUTH_SOURCE_USERNAME", ""),
            password  = os.getenv("AUTH_SOURCE_PASSWORD", ""),
        )

    def _auth(self) -> None:
        """Trigger initial login before scraping starts."""
        session = self._session_manager.get_session()
        logger.info(f"Auth ready for {self.source_name}")

    def _fetch_records(self) -> list[dict]:
        raw_records = []
        page        = 1

        while True:
            records_page = self._fetch_page(page)
            if not records_page:
                break
            raw_records.extend(records_page)
            self.logger.debug(f"Page {page}: {len(records_page)} records")
            if len(records_page) < self.PAGE_SIZE:
                break  # Last page
            page += 1
            self._jitter()

        return raw_records

    def _fetch_page(self, page: int) -> list[dict]:
        """Fetch one page, auto-refreshing token on 401/403."""
        session = self._session_manager.get_session()
        params  = {"page": page, "page_size": self.PAGE_SIZE}

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = session.get(self.DATA_URL, params=params, timeout=20)

                if resp.status_code in (401, 403):
                    logger.warning(f"Auth error ({resp.status_code}) on page {page}, refreshing token")
                    self._session_manager.invalidate()
                    session = self._session_manager.get_session()
                    continue

                resp.raise_for_status()
                data = resp.json()

                # Adjust key name to match actual API response shape
                return data.get("results") or data.get("items") or data.get("data") or []

            except Exception as e:
                wait = attempt * 2
                logger.warning(f"Page {page} attempt {attempt} failed: {e}. Retry in {wait}s")
                time.sleep(wait)

        logger.error(f"Page {page} failed after {self.max_retries} retries")
        return []

    def field_map(self) -> dict:
        """Map authenticated source fields to canonical schema. Adjust per real API."""
        return {
            "title":          "name",
            "description":    "summary",
            "source_name":    lambda r: self.source_name,
            "published_date": "created_at",
            "url":            "permalink",
            "category":       "type",
            "organization":   "issuing_org",
            "deadline":       "close_date",
            "budget":         "award_amount",
        }
