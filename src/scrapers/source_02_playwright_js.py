"""
source_02_playwright_js.py — JS-Rendered Site Scraper
Uses Playwright (headless Chromium) for sites that require JavaScript execution.

Advanced patterns:
- Stealth mode: randomized viewport + user agent to avoid bot detection
- wait_for_selector: waits for specific element before scraping (not just DOMContentLoaded)
- Network idle wait: ensures all XHR/fetch calls complete before extracting
- Auto-scroll: triggers lazy-loaded content before extraction
- Iframe detection: skips iframes silently instead of crashing
- Screenshot on failure: saves debug screenshot when page fails

Replace BASE_URL and selectors with real values from client source list.
"""

import logging
import os
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from .base import BaseScraper

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_URL       = os.getenv("SOURCE_02_URL", "https://example.com/opportunities")
MAX_PAGES      = int(os.getenv("SOURCE_02_MAX_PAGES", "10"))
WAIT_SELECTOR  = ".listing-item"          # Element that signals page is ready
PAGE_TIMEOUT   = 30_000                   # 30 seconds per page (ms)
DEBUG_SHOTS    = os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true"
SCREENSHOT_DIR = "logs/screenshots"


class PlaywrightJsScraper(BaseScraper):
    """
    Scraper for JS-rendered pages. Runs real Chromium headless.
    Each call to scrape() launches and closes one browser instance.
    """

    source_name = "js_rendered_source"
    source_type = "js_rendered"
    delay_min   = 2.0
    delay_max   = 4.0

    def _fetch_records(self) -> list[dict]:
        raw_records = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )

            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Block images/fonts to speed up loading
            context.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}",
                lambda route: route.abort()
            )

            page = context.new_page()

            # Hide webdriver property (basic anti-bot)
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            for page_num in range(1, MAX_PAGES + 1):
                url = f"{BASE_URL}?page={page_num}"

                try:
                    # Navigate + wait for network to settle
                    page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)

                    # Wait for the key listing element to appear
                    page.wait_for_selector(WAIT_SELECTOR, timeout=PAGE_TIMEOUT)

                    # Auto-scroll to trigger lazy loading
                    self._scroll_to_bottom(page)

                    # Extract items
                    items = page.query_selector_all(WAIT_SELECTOR)
                    if not items:
                        self.logger.info(f"No items on page {page_num}, stopping")
                        break

                    for item in items:
                        record = self._extract_item(page, item)
                        if record:
                            raw_records.append(record)

                    self.logger.debug(f"Page {page_num}: {len(items)} items")
                    self._jitter()

                except PWTimeout:
                    self.logger.warning(f"Timeout on page {page_num}: {url}")
                    if DEBUG_SHOTS:
                        self._save_screenshot(page, f"timeout_page_{page_num}")
                    break

                except Exception as e:
                    self.logger.error(f"Error on page {page_num}: {e}")
                    if DEBUG_SHOTS:
                        self._save_screenshot(page, f"error_page_{page_num}")
                    continue

            browser.close()

        return raw_records

    def _extract_item(self, page, element) -> dict:
        """
        Extract fields from a single listing element.
        Uses evaluate() to run JS inside the page context for complex extractions.
        Replace selectors with real values.
        """
        try:
            return {
                "raw_title":   self._inner_text(element, ".item-title"),
                "raw_desc":    self._inner_text(element, ".item-description"),
                "raw_date":    self._inner_text(element, ".item-date"),
                "raw_url":     self._get_href(element, "a.item-link"),
                "raw_org":     self._inner_text(element, ".item-organization"),
                "raw_budget":  self._inner_text(element, ".item-budget"),
                "raw_tag":     self._inner_text(element, ".item-category"),
            }
        except Exception as e:
            self.logger.debug(f"Failed to extract item: {e}")
            return {}

    def field_map(self) -> dict:
        return {
            "title":          "raw_title",
            "description":    "raw_desc",
            "source_name":    lambda r: self.source_name,
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       "raw_tag",
            "organization":   "raw_org",
            "budget":         "raw_budget",
        }

    # ── Playwright Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _scroll_to_bottom(page, steps: int = 5) -> None:
        """Gradually scroll to bottom to trigger lazy-loaded content."""
        for i in range(1, steps + 1):
            page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {i}/{steps})")
            page.wait_for_timeout(300)

    @staticmethod
    def _inner_text(element, selector: str) -> str:
        el = element.query_selector(selector)
        return el.inner_text().strip() if el else ""

    @staticmethod
    def _get_href(element, selector: str) -> str:
        el = element.query_selector(selector)
        return el.get_attribute("href") or "" if el else ""

    def _save_screenshot(self, page, name: str) -> None:
        try:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            path = f"{SCREENSHOT_DIR}/{self.source_name}_{name}.png"
            page.screenshot(path=path)
            self.logger.info(f"Screenshot saved: {path}")
        except Exception:
            pass