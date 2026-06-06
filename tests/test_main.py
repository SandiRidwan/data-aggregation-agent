"""
tests/test_main.py — Test suite for main.py

Coverage:
  - PipelineStats: init, to_storage_dict, tier counting
  - step_scrape: all sources called, errors caught per-source, stats updated
  - step_score: scored records returned, tier counts, scoring failure fallback
  - step_store: upsert called, dry_run skips, exception caught
  - step_airtable: AirtableRecord conversion, sync called, dry_run, error caught
  - step_digest: DigestRecord conversion, send called, dry_run, failure logged
  - run_pipeline: full integration (all steps mocked), stats returned,
                  crash caught, log_run_start/finish called
  - DRY_RUN / RUN_ONCE modes
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers — build a fake scored record dict
# ---------------------------------------------------------------------------

def _rec(
    url="https://example.com/1",
    title="Test Job",
    total_score=80,
    fit_tier="HIGH",
    source_name="LinkedIn",
    description="Great role",
    reasoning="All match",
    scraped_at="2026-06-05T08:00:00",
) -> dict:
    return dict(
        url=url,
        title=title,
        total_score=total_score,
        fit_tier=fit_tier,
        source_name=source_name,
        description=description,
        reasoning=reasoning,
        scraped_at=scraped_at,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stats():
    from main import PipelineStats
    return PipelineStats(run_id="test_run_001")


@pytest.fixture
def three_records():
    return [
        _rec(url="https://x.com/1", total_score=85, fit_tier="HIGH"),
        _rec(url="https://x.com/2", total_score=55, fit_tier="MEDIUM"),
        _rec(url="https://x.com/3", total_score=20, fit_tier="LOW"),
    ]


# ===========================================================================
# PipelineStats
# ===========================================================================

class TestPipelineStats:

    def test_initial_state(self, stats):
        assert stats.run_id == "test_run_001"
        assert stats.status == "running"
        assert stats.records_scraped == 0
        assert stats.errors == []

    def test_to_storage_dict_keys(self, stats):
        d = stats.to_storage_dict()
        for key in ["status", "sources_hit", "records_scraped", "records_new",
                    "records_updated", "records_high", "records_medium", "records_low"]:
            assert key in d

    def test_to_storage_dict_no_errors_returns_none(self, stats):
        assert stats.to_storage_dict()["errors"] is None

    def test_to_storage_dict_with_errors(self, stats):
        stats.errors = ["error 1", "error 2"]
        d = stats.to_storage_dict()
        assert "error 1" in d["errors"]
        assert "error 2" in d["errors"]

    def test_status_reflects_assignment(self, stats):
        stats.status = "done"
        assert stats.to_storage_dict()["status"] == "done"


# ===========================================================================
# step_scrape
# ===========================================================================

class TestStepScrape:

    def _make_mock_scraper(self, name, records):
        mock = MagicMock()
        mock.__class__.__name__ = name
        mock.scrape.return_value = records
        return mock

    def test_aggregates_records_from_all_sources(self, stats):
        from main import step_scrape

        scrapers = [
            self._make_mock_scraper("Source01", [_rec(url="https://x.com/1")]),
            self._make_mock_scraper("Source02", [_rec(url="https://x.com/2"), _rec(url="https://x.com/3")]),
        ]

        with patch("main.StaticHtmlSourceScraper", return_value=scrapers[0]), \
             patch("main.AuthenticatedSourceScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.PlaywrightJsScraper", return_value=scrapers[1]), \
             patch("main.RestApiScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.RssFeedScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.SitemapScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.PdfSourceScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.AdvancedAuthScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))):
            records = step_scrape(stats)

        assert len(records) == 3

    def test_failed_scraper_doesnt_crash_pipeline(self, stats):
        from main import step_scrape

        bad_scraper = MagicMock()
        bad_scraper.__class__.__name__ = "BadSource"
        bad_scraper.scrape.side_effect = RuntimeError("connection refused")

        good_scraper = MagicMock()
        good_scraper.__class__.__name__ = "GoodSource"
        good_scraper.scrape.return_value = [_rec()]

        with patch("main.StaticHtmlSourceScraper", return_value=bad_scraper), \
             patch("main.AuthenticatedSourceScraper", return_value=good_scraper), \
             patch("main.PlaywrightJsScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.RestApiScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.RssFeedScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.SitemapScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.PdfSourceScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.AdvancedAuthScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))):
            records = step_scrape(stats)

        assert len(stats.errors) == 1
        assert "connection refused" in stats.errors[0]
        assert len(records) == 1  # good scraper still returned its record

    def test_stats_sources_hit_increments(self, stats):
        from main import step_scrape

        mock_scraper = MagicMock()
        mock_scraper.__class__.__name__ = "S"
        mock_scraper.scrape.return_value = []

        with patch("main.StaticHtmlSourceScraper", return_value=mock_scraper), \
             patch("main.AuthenticatedSourceScraper", return_value=mock_scraper), \
             patch("main.PlaywrightJsScraper", return_value=mock_scraper), \
             patch("main.RestApiScraper", return_value=mock_scraper), \
             patch("main.RssFeedScraper", return_value=mock_scraper), \
             patch("main.SitemapScraper", return_value=mock_scraper), \
             patch("main.PdfSourceScraper", return_value=mock_scraper), \
             patch("main.AdvancedAuthScraper", return_value=mock_scraper):
            step_scrape(stats)

        assert stats.sources_hit == 8

    def test_stats_records_scraped_set(self, stats):
        from main import step_scrape

        mock_scraper = MagicMock()
        mock_scraper.__class__.__name__ = "S"
        mock_scraper.scrape.return_value = [_rec(), _rec(url="https://x.com/2")]

        with patch("main.StaticHtmlSourceScraper", return_value=mock_scraper), \
             patch("main.AuthenticatedSourceScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.PlaywrightJsScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.RestApiScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.RssFeedScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.SitemapScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.PdfSourceScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))), \
             patch("main.AdvancedAuthScraper", return_value=MagicMock(scrape=MagicMock(return_value=[]))):
            step_scrape(stats)

        assert stats.records_scraped == 2


# ===========================================================================
# step_score
# ===========================================================================

class TestStepScore:

    def test_returns_scored_records(self, stats, three_records):
        from main import step_score

        with patch("main.score_batch", return_value=three_records) as mock_score:
            result = step_score(three_records, stats)

        assert result == three_records
        mock_score.assert_called_once()

    def test_tier_counts_updated(self, stats, three_records):
        from main import step_score

        with patch("main.score_batch", return_value=three_records):
            step_score(three_records, stats)

        assert stats.records_high == 1
        assert stats.records_medium == 1
        assert stats.records_low == 1

    def test_empty_records_returns_empty(self, stats):
        from main import step_score

        with patch("main.score_batch") as mock_score:
            result = step_score([], stats)

        assert result == []
        mock_score.assert_not_called()

    def test_scoring_failure_returns_original_records(self, stats, three_records):
        from main import step_score

        with patch("main.score_batch", side_effect=RuntimeError("Groq down")):
            result = step_score(three_records, stats)

        assert result == three_records
        assert len(stats.errors) == 1
        assert "Groq down" in stats.errors[0]

    def test_dry_run_passed_to_score_records(self, stats, three_records):
        from main import step_score

        with patch("main.score_batch", return_value=three_records) as mock_score, \
             patch("main.DRY_RUN", True):
            step_score(three_records, stats)

        mock_score.assert_called_once()


# ===========================================================================
# step_store
# ===========================================================================

class TestStepStore:

    def test_calls_upsert_records(self, stats, three_records):
        from main import step_store

        with patch("main.upsert_records", return_value=(2, 1)) as mock_up, \
             patch("main.DRY_RUN", False):
            step_store(three_records, stats)

        mock_up.assert_called_once_with(three_records)
        assert stats.records_new == 2
        assert stats.records_updated == 1

    def test_dry_run_skips_upsert(self, stats, three_records):
        from main import step_store

        with patch("main.upsert_records") as mock_up, \
             patch("main.DRY_RUN", True):
            step_store(three_records, stats)

        mock_up.assert_not_called()

    def test_empty_records_skips(self, stats):
        from main import step_store

        with patch("main.upsert_records") as mock_up, \
             patch("main.DRY_RUN", False):
            step_store([], stats)

        mock_up.assert_not_called()

    def test_exception_caught_added_to_errors(self, stats, three_records):
        from main import step_store

        with patch("main.upsert_records", side_effect=Exception("DB down")), \
             patch("main.DRY_RUN", False):
            step_store(three_records, stats)

        assert any("DB down" in e for e in stats.errors)


# ===========================================================================
# step_airtable
# ===========================================================================

class TestStepAirtable:

    def test_sync_called_with_airtable_records(self, stats, three_records):
        from main import step_airtable
        from src.airtable_sync import SyncResult

        mock_result = SyncResult(created=2, updated=1, errors=0)

        with patch("main.sync_to_airtable", return_value=mock_result) as mock_sync, \
             patch("main.build_routing_engine_from_env", return_value=MagicMock()), \
             patch("main.DRY_RUN", False):
            step_airtable(three_records, stats)

        mock_sync.assert_called_once()
        at_records = mock_sync.call_args[0][0]
        assert len(at_records) == 3

    def test_airtable_records_have_correct_fields(self, stats):
        from main import step_airtable
        from src.airtable_sync import AirtableRecord, SyncResult

        records = [_rec(url="https://x.com/1", title="Job A", total_score=85)]
        mock_result = SyncResult(created=1)
        captured = []

        def capture_sync(recs, **kwargs):
            captured.extend(recs)
            return mock_result

        with patch("main.sync_to_airtable", side_effect=capture_sync), \
             patch("main.build_routing_engine_from_env", return_value=MagicMock()), \
             patch("main.DRY_RUN", False):
            step_airtable(records, stats)

        assert len(captured) == 1
        assert isinstance(captured[0], AirtableRecord)
        assert captured[0].title == "Job A"
        assert captured[0].score == 85.0
        assert captured[0].url == "https://x.com/1"

    def test_dry_run_passed_through(self, stats, three_records):
        from main import step_airtable
        from src.airtable_sync import SyncResult

        with patch("main.sync_to_airtable", return_value=SyncResult()) as mock_sync, \
             patch("main.build_routing_engine_from_env", return_value=MagicMock()), \
             patch("main.DRY_RUN", True):
            step_airtable(three_records, stats)

        call_kwargs = mock_sync.call_args[1]
        assert call_kwargs.get("dry_run") is True

    def test_empty_records_skips(self, stats):
        from main import step_airtable

        with patch("main.sync_to_airtable") as mock_sync:
            step_airtable([], stats)

        mock_sync.assert_not_called()

    def test_exception_caught(self, stats, three_records):
        from main import step_airtable

        with patch("main.sync_to_airtable", side_effect=RuntimeError("Airtable down")), \
             patch("main.build_routing_engine_from_env", return_value=MagicMock()), \
             patch("main.DRY_RUN", False):
            step_airtable(three_records, stats)

        assert any("Airtable down" in e for e in stats.errors)

    def test_stats_updated(self, stats, three_records):
        from main import step_airtable
        from src.airtable_sync import SyncResult

        mock_result = SyncResult(created=3, updated=0, errors=0)

        with patch("main.sync_to_airtable", return_value=mock_result), \
             patch("main.build_routing_engine_from_env", return_value=MagicMock()), \
             patch("main.DRY_RUN", False):
            step_airtable(three_records, stats)

        assert stats.airtable_created == 3
        assert stats.airtable_updated == 0


# ===========================================================================
# step_digest
# ===========================================================================

class TestStepDigest:

    def test_send_digest_called(self, stats, three_records):
        from main import step_digest

        with patch("main.send_digest", return_value=True) as mock_send, \
             patch("main.DRY_RUN", False):
            step_digest(three_records, stats)

        mock_send.assert_called_once()
        assert stats.digest_sent is True

    def test_digest_records_converted_correctly(self, stats):
        from main import step_digest
        from src.digest import DigestPayload
        captured = []

        def capture(payload, **kwargs):
            captured.append(payload)
            return True

        records = [_rec(fit_tier="HIGH", title="Job A", total_score=85)]

        with patch("main.send_digest", side_effect=capture), \
             patch("main.DRY_RUN", False):
            step_digest(records, stats)

        assert len(captured) == 1
        assert isinstance(captured[0], DigestPayload)
        assert captured[0].high()[0].title == "Job A"

    def test_tier_mapping_high(self, stats):
        from main import step_digest
        from src.digest import DigestPayload
        captured = []

        def capture(payload, **kwargs):
            captured.append(payload)
            return True

        with patch("main.send_digest", side_effect=capture), \
             patch("main.DRY_RUN", False):
            step_digest([_rec(fit_tier="HIGH")], stats)

        assert captured[0].high()[0].tier == "High Fit"

    def test_tier_mapping_medium(self, stats):
        from main import step_digest
        from src.digest import DigestPayload
        captured = []

        def capture(payload, **kwargs):
            captured.append(payload)
            return True

        with patch("main.send_digest", side_effect=capture), \
             patch("main.DRY_RUN", False):
            step_digest([_rec(fit_tier="MEDIUM", total_score=55)], stats)

        assert captured[0].medium()[0].tier == "Medium Fit"

    def test_tier_mapping_low(self, stats):
        from main import step_digest
        from src.digest import DigestPayload
        captured = []

        def capture(payload, **kwargs):
            captured.append(payload)
            return True

        with patch("main.send_digest", side_effect=capture), \
             patch("main.DRY_RUN", False):
            step_digest([_rec(fit_tier="LOW", total_score=20)], stats)

        assert captured[0].low()[0].tier == "Low Fit"

    def test_empty_records_skips(self, stats):
        from main import step_digest

        with patch("main.send_digest") as mock_send:
            step_digest([], stats)

        mock_send.assert_not_called()

    def test_send_returns_false_logged(self, stats, three_records):
        from main import step_digest

        with patch("main.send_digest", return_value=False), \
             patch("main.DRY_RUN", False):
            step_digest(three_records, stats)

        assert stats.digest_sent is False
        assert any("Digest" in e for e in stats.errors)

    def test_exception_caught(self, stats, three_records):
        from main import step_digest

        with patch("main.send_digest", side_effect=RuntimeError("SMTP down")), \
             patch("main.DRY_RUN", False):
            step_digest(three_records, stats)

        assert any("SMTP down" in e for e in stats.errors)

    def test_dry_run_passed_through(self, stats, three_records):
        from main import step_digest

        with patch("main.send_digest", return_value=True) as mock_send, \
             patch("main.DRY_RUN", True):
            step_digest(three_records, stats)

        call_kwargs = mock_send.call_args[1]
        assert call_kwargs.get("dry_run") is True


# ===========================================================================
# run_pipeline — full integration
# ===========================================================================

class TestRunPipeline:

    def _patch_all_steps(self, records=None):
        """Context manager that patches all 5 steps."""
        records = records or [_rec()]
        return {
            "step_scrape":   patch("main.step_scrape",   return_value=records),
            "step_score":    patch("main.step_score",    return_value=records),
            "step_store":    patch("main.step_store"),
            "step_airtable": patch("main.step_airtable"),
            "step_digest":   patch("main.step_digest"),
            "log_start":     patch("main.log_run_start", return_value=MagicMock()),
            "log_finish":    patch("main.log_run_finish"),
            "DRY_RUN":       patch("main.DRY_RUN", False),
        }

    def test_returns_pipeline_stats(self):
        from main import run_pipeline, PipelineStats
        with patch("main.step_scrape", return_value=[_rec()]), \
             patch("main.step_score", return_value=[_rec()]), \
             patch("main.step_store"), \
             patch("main.step_airtable"), \
             patch("main.step_digest"), \
             patch("main.log_run_start", return_value=MagicMock()), \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", False):
            stats = run_pipeline()
        assert isinstance(stats, PipelineStats)

    def test_status_done_on_success(self):
        from main import run_pipeline
        with patch("main.step_scrape", return_value=[_rec()]), \
             patch("main.step_score", return_value=[_rec()]), \
             patch("main.step_store"), \
             patch("main.step_airtable"), \
             patch("main.step_digest"), \
             patch("main.log_run_start", return_value=MagicMock()), \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", False):
            stats = run_pipeline()
        assert stats.status in ("done", "done_with_errors")

    def test_status_failed_on_crash(self):
        from main import run_pipeline
        with patch("main.step_scrape", side_effect=RuntimeError("total crash")), \
             patch("main.log_run_start", return_value=MagicMock()), \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", False):
            stats = run_pipeline()
        assert stats.status == "failed"
        assert any("total crash" in e for e in stats.errors)

    def test_log_run_start_called(self):
        from main import run_pipeline
        with patch("main.step_scrape", return_value=[]), \
             patch("main.step_score", return_value=[]), \
             patch("main.step_store"), \
             patch("main.step_airtable"), \
             patch("main.step_digest"), \
             patch("main.log_run_start", return_value=MagicMock()) as mock_start, \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", False):
            run_pipeline()
        mock_start.assert_called_once()

    def test_log_run_finish_called(self):
        from main import run_pipeline
        with patch("main.step_scrape", return_value=[]), \
             patch("main.step_score", return_value=[]), \
             patch("main.step_store"), \
             patch("main.step_airtable"), \
             patch("main.step_digest"), \
             patch("main.log_run_start", return_value=MagicMock()), \
             patch("main.log_run_finish") as mock_finish, \
             patch("main.DRY_RUN", False):
            run_pipeline()
        mock_finish.assert_called_once()

    def test_dry_run_skips_log_start(self):
        from main import run_pipeline
        with patch("main.step_scrape", return_value=[]), \
             patch("main.step_score", return_value=[]), \
             patch("main.step_store"), \
             patch("main.step_airtable"), \
             patch("main.step_digest"), \
             patch("main.log_run_start") as mock_start, \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", True):
            run_pipeline()
        mock_start.assert_not_called()

    def test_all_5_steps_called(self):
        from main import run_pipeline
        with patch("main.step_scrape",   return_value=[_rec()]) as ms, \
             patch("main.step_score",    return_value=[_rec()]) as mc, \
             patch("main.step_store")    as mst, \
             patch("main.step_airtable") as ma, \
             patch("main.step_digest")   as md, \
             patch("main.log_run_start", return_value=MagicMock()), \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", False):
            run_pipeline()
        ms.assert_called_once()
        mc.assert_called_once()
        mst.assert_called_once()
        ma.assert_called_once()
        md.assert_called_once()

    def test_log_start_failure_doesnt_crash_pipeline(self):
        from main import run_pipeline
        with patch("main.step_scrape", return_value=[]), \
             patch("main.step_score", return_value=[]), \
             patch("main.step_store"), \
             patch("main.step_airtable"), \
             patch("main.step_digest"), \
             patch("main.log_run_start", side_effect=Exception("DB down")), \
             patch("main.log_run_finish"), \
             patch("main.DRY_RUN", False):
            stats = run_pipeline()
        assert stats is not None