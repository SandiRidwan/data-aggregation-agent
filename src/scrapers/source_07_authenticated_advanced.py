"""
source_07_authenticated_advanced.py — Advanced Authenticated Scraper
Extends SessionManager pattern with CSRF token extraction + iframe handling.

Advanced patterns beyond source_authenticated.py:
- CSRF token extraction: reads token from login page HTML before POST
- Cookie jar persistence: session cookies maintained across requests
- Iframe content extraction: detects and fetches iframe src separately
- Form-based login: handles HTML form POST (not just JSON API auth)
- Post-login redirect handling: follows redirects to final authenticated page
- Session health check: lightweight ping before heavy scrape to catch expired sessions

Replace all URLs, field names, and selectors with real values.
"""

import logging
import os
import re
import time
from typing import Optional
import requests
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

LOGIN_PAGE_URL  = os.getenv("SOURCE_07_LOGIN_PAGE",  "https://example.com/login")
LOGIN_POST_URL  = os.getenv("SOURCE_07_LOGIN_POST",  "https://example.com/login")
DATA_URL        = os.getenv("SOURCE_07_DATA_URL",    "https://example.com/portal/listings")
HEALTH_URL      = os.getenv("SOURCE_07_HEALTH_URL",  "https://example.com/portal/dashboard")
USERNAME        = os.getenv("SOURCE_07_USERNAME",    "")
PASSWORD        = os.getenv("SOURCE_07_PASSWORD",    "")
MAX_PAGES       = int(os.getenv("SOURCE_07_MAX_PAGES", "10"))

# Field names for login form (inspect the real form with browser DevTools)
FORM_USERNAME_FIELD = "username"
FORM_PASSWORD_FIELD = "password"
CSRF_FIELD_NAME     = "_token"    # Common names: _token, csrf_token, authenticity_token


class AdvancedSessionManager:
    """
    Extended session manager that handles:
    - Form-based login (HTML form POST)
    - CSRF token extraction from login page
    - Cookie jar persistence
    - Session health check
    """

    def __init__(self):
        self._session   = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._logged_in = False

    def get_session(self) -> requests.Session:
        """Return authenticated session. Logs in if needed."""
        if not self._logged_in or not self._health_check():
            self._login()
        return self._session

    def invalidate(self) -> None:
        """Force re-login on next get_session() call."""
        self._logged_in = False
        logger.info("Session invalidated — will re-login")

    def _health_check(self) -> bool:
        """
        Lightweight check: hit a known authenticated page.
        Returns True if still logged in.
        """
        try:
            resp = self._session.get(HEALTH_URL, timeout=10, allow_redirects=False)
            # If redirected to login page → session expired
            if resp.status_code in (301, 302):
                location = resp.headers.get("Location", "")
                if "login" in location.lower():
                    logger.info("Session expired (redirect to login)")
                    return False
            return resp.status_code == 200
        except Exception:
            return False

    def _login(self, max_retries: int = 3) -> None:
        """
        Two-step form login:
        Step 1: GET login page → extract CSRF token
        Step 2: POST credentials + CSRF token → follow redirect to authenticated area
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Step 1: GET login page to extract CSRF token
                resp = self._session.get(LOGIN_PAGE_URL, timeout=20)
                resp.raise_for_status()

                csrf_token = self._extract_csrf(resp.text)
                if not csrf_token:
                    logger.warning("CSRF token not found on login page — proceeding without it")

                # Step 2: POST login form
                payload = {
                    FORM_USERNAME_FIELD: USERNAME,
                    FORM_PASSWORD_FIELD: PASSWORD,
                }
                if csrf_token:
                    payload[CSRF_FIELD_NAME] = csrf_token

                post_resp = self._session.post(
                    LOGIN_POST_URL,
                    data=payload,
                    timeout=20,
                    allow_redirects=True,
                )

                # Verify login succeeded
                if self._verify_login(post_resp):
                    self._logged_in = True
                    logger.info("✅ Form-based login successful")
                    return
                else:
                    logger.warning(f"Login attempt {attempt}: response suggests failure")

            except Exception as e:
                wait = attempt * 3
                logger.warning(f"Login attempt {attempt}/{max_retries} failed: {e}. Retry in {wait}s")
                time.sleep(wait)

        raise RuntimeError(f"Authentication failed after {max_retries} attempts")

    def _extract_csrf(self, html: str) -> Optional[str]:
        """
        Extract CSRF token from login page HTML.
        Checks multiple common patterns:
        - <input type="hidden" name="_token" value="...">
        - <meta name="csrf-token" content="...">
        """
        soup = BeautifulSoup(html, "lxml")

        # Pattern 1: hidden input field
        for field_name in (CSRF_FIELD_NAME, "csrf_token", "_csrf", "authenticity_token"):
            input_el = soup.find("input", attrs={"name": field_name})
            if input_el and input_el.get("value"):
                logger.debug(f"CSRF token found in input[name={field_name}]")
                return input_el["value"]

        # Pattern 2: meta tag
        meta = soup.find("meta", attrs={"name": re.compile("csrf", re.I)})
        if meta and meta.get("content"):
            logger.debug("CSRF token found in meta tag")
            return meta["content"]

        # Pattern 3: JavaScript variable
        match = re.search(r'csrf[_-]?token["\s]*[:=]\s*["\']([^"\']+)["\']', html, re.I)
        if match:
            logger.debug("CSRF token found in JavaScript")
            return match.group(1)

        return None

    def _verify_login(self, resp: requests.Response) -> bool:
        """
        Check if login POST succeeded.
        Adjust logic based on what the real site does after successful login.
        """
        # Redirect to non-login page = success
        if resp.url and "login" not in resp.url.lower():
            return True
        # Check for error message in response body
        if "invalid" in resp.text.lower() or "incorrect" in resp.text.lower():
            return False
        # 200 on the same login page = usually failure
        if resp.url and "login" in resp.url.lower():
            return False
        return True


# ─── Scraper ─────────────────────────────────────────────────────────────────

class AdvancedAuthScraper(BaseScraper):
    """
    Authenticated scraper with CSRF token handling, cookie persistence,
    iframe detection, and session health checks.
    """

    source_name = "authenticated_advanced"
    source_type = "authenticated"
    delay_min   = 2.0
    delay_max   = 4.0

    def __init__(self):
        super().__init__()
        self._session_mgr = AdvancedSessionManager()

    def _auth(self) -> None:
        self._session_mgr.get_session()

    def _fetch_records(self) -> list[dict]:
        raw_records = []

        for page_num in range(1, MAX_PAGES + 1):
            session = self._session_mgr.get_session()
            url     = f"{DATA_URL}?page={page_num}"

            resp = self._retry_get(session, url)
            if resp is None:
                break

            # Re-auth if redirected to login
            if "login" in resp.url.lower():
                logger.warning("Redirected to login — session expired, re-authenticating")
                self._session_mgr.invalidate()
                session = self._session_mgr.get_session()
                resp    = self._retry_get(session, url)
                if resp is None:
                    break

            soup   = BeautifulSoup(resp.text, "lxml")
            items  = soup.select(".listing-item")  # Replace selector

            if not items:
                logger.info(f"No items on page {page_num}, done")
                break

            for item in items:
                # Iframe detection: if content is in an iframe, fetch it separately
                iframe = item.find("iframe")
                if iframe and iframe.get("src"):
                    item = self._fetch_iframe_content(session, iframe["src"]) or item

                record = self._extract_item(item)
                if record:
                    raw_records.append(record)

            logger.debug(f"Page {page_num}: {len(items)} items")
            self._jitter()

        return raw_records

    def _extract_item(self, element) -> Optional[dict]:
        try:
            soup = element if hasattr(element, "select_one") else BeautifulSoup(str(element), "lxml")
            return {
                "raw_title":  self._text(soup, ".item-title"),
                "raw_desc":   self._text(soup, ".item-body"),
                "raw_url":    self._href(soup, "a.item-link"),
                "raw_date":   self._text(soup, ".item-date"),
                "raw_org":    self._text(soup, ".item-issuer"),
                "raw_budget": self._text(soup, ".item-value"),
            }
        except Exception as e:
            logger.debug(f"Item extraction failed: {e}")
            return None

    def _fetch_iframe_content(self, session, src: str) -> Optional[BeautifulSoup]:
        """Fetch iframe src and return its BeautifulSoup."""
        from urllib.parse import urljoin
        iframe_url = urljoin(DATA_URL, src)
        resp = self._retry_get(session, iframe_url)
        if resp:
            return BeautifulSoup(resp.text, "lxml")
        return None

    def field_map(self) -> dict:
        return {
            "title":          "raw_title",
            "description":    "raw_desc",
            "source_name":    lambda r: self.source_name,
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       lambda r: "portal_listing",
            "organization":   "raw_org",
            "budget":         "raw_budget",
        }

    @staticmethod
    def _text(soup, selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _href(soup, selector: str) -> str:
        el = soup.select_one(selector)
        return el.get("href", "") if el else ""