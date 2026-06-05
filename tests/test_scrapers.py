"""
tests/test_scrapers.py — Test Suite for BaseScraper

Coverage:
- field_map normalization: required fields, optional fields, None fallback
- _normalize: drops records with no title
- _normalize: scraped_at and raw_source auto-injected
- _retry_get: returns None after max retries
- Concrete scraper: verifies field_map contract
"""

import pytest
from unittest.mock import patch, MagicMock
from src.scrapers.base import BaseScraper, ScrapeResult, REQUIRED_FIELDS, OPTIONAL_FIELDS


# ─── Minimal concrete scraper for testing ────────────────────────────────────

class MinimalScraper(BaseScraper):
    source_name = "test_source"
    source_type = "static_html"

    def _fetch_records(self):
        return [
            {"raw_title": "Hello World", "raw_desc": "A description",
             "raw_url": "https://example.com/1", "raw_date": "2026-01-01"},
            {"raw_title": "",  "raw_desc": "No title",
             "raw_url": "https://example.com/2", "raw_date": ""},  # Should be dropped
        ]

    def field_map(self):
        return {
            "title":          "raw_title",
            "description":    "raw_desc",
            "source_name":    lambda r: "test_source",
            "published_date": "raw_date",
            "url":            "raw_url",
            "category":       lambda r: "test",
        }


# ─── Normalization ────────────────────────────────────────────────────────────

class TestNormalization:

    def setup_method(self):
        self.scraper = MinimalScraper()

    def test_required_fields_present(self):
        raw = {"raw_title": "Test", "raw_desc": "Desc",
               "raw_url": "https://x.com", "raw_date": "2026-01-01"}
        result = self.scraper._normalize(raw)
        for field in REQUIRED_FIELDS:
            assert field in result, f"Required field '{field}' missing from normalized record"

    def test_optional_fields_default_none(self):
        raw = {"raw_title": "Test", "raw_desc": "", "raw_url": "https://x.com", "raw_date": ""}
        result = self.scraper._normalize(raw)
        for field in OPTIONAL_FIELDS:
            assert field in result, f"Optional field '{field}' missing"
            # Optional fields not in field_map → should be None
            if field not in self.scraper.field_map():
                assert result[field] is None

    def test_scraped_at_auto_injected(self):
        raw = {"raw_title": "Test", "raw_desc": "", "raw_url": "https://x.com", "raw_date": ""}
        result = self.scraper._normalize(raw)
        assert result["scraped_at"] is not None
        assert "T" in result["scraped_at"]  # ISO format has T separator

    def test_raw_source_auto_injected(self):
        raw = {"raw_title": "Test", "raw_desc": "", "raw_url": "https://x.com", "raw_date": ""}
        result = self.scraper._normalize(raw)
        assert result["raw_source"] == "test_source"

    def test_missing_title_returns_none(self):
        raw = {"raw_title": "", "raw_desc": "desc", "raw_url": "https://x.com", "raw_date": ""}
        result = self.scraper._normalize(raw)
        assert result is None

    def test_lambda_field_map(self):
        raw = {"raw_title": "Test", "raw_desc": "", "raw_url": "https://x.com", "raw_date": ""}
        result = self.scraper._normalize(raw)
        assert result["source_name"] == "test_source"
        assert result["category"] == "test"


# ─── ScrapeResult ─────────────────────────────────────────────────────────────

class TestScrapeResult:

    def test_scrape_drops_no_title_records(self):
        scraper = MinimalScraper()
        result  = scraper.scrape()
        assert result.success is True
        # One record has empty title → should be dropped
        assert len(result.records) == 1
        assert result.records[0]["title"] == "Hello World"

    def test_scrape_result_repr(self):
        r = ScrapeResult(source="test", success=True)
        r.records = [{"title": "x"}]
        assert "test" in repr(r)
        assert "✅" in repr(r)

    def test_scrape_catches_exception(self):
        class BrokenScraper(BaseScraper):
            source_name = "broken"
            def _fetch_records(self):
                raise RuntimeError("Network down")
            def field_map(self):
                return {}

        result = BrokenScraper().scrape()
        assert result.success is False
        assert "Network down" in result.error


# ─── _retry_get ──────────────────────────────────────────────────────────────

class TestRetryGet:

    def test_returns_none_after_max_retries(self):
        scraper = MinimalScraper()
        scraper.max_retries = 2

        mock_session = MagicMock()
        mock_session.get.side_effect = ConnectionError("timeout")

        result = scraper._retry_get(mock_session, "https://example.com")
        assert result is None
        assert mock_session.get.call_count == 2

    def test_returns_response_on_success(self):
        scraper = MinimalScraper()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_session = MagicMock()
        mock_session.get.return_value = mock_response

        result = scraper._retry_get(mock_session, "https://example.com")
        assert result is mock_response

    def test_retries_on_non_200(self):
        scraper = MinimalScraper()
        scraper.max_retries = 3

        mock_response_fail    = MagicMock()
        mock_response_fail.status_code = 503

        mock_response_success = MagicMock()
        mock_response_success.status_code = 200

        mock_session = MagicMock()
        # Fail twice, succeed on third
        mock_session.get.side_effect = [
            mock_response_fail,
            mock_response_fail,
            mock_response_success,
        ]

        result = scraper._retry_get(mock_session, "https://example.com")
        assert result.status_code == 200
        assert mock_session.get.call_count == 3
