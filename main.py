"""
main.py — Pipeline Runner + APScheduler Cron

Orchestrates the full data aggregation pipeline:
  1. Run all scrapers (sources 01–07)
  2. Score records via Groq LLM (scoring.py)
  3. Store to PostgreSQL (storage.py)
  4. Sync to Airtable (airtable_sync.py)
  4b. Clean empty/ghost rows from Airtable tables
  5. Send email digest via SendGrid (digest.py)

Scheduling:
  - Default: daily at 08:00 UTC (configurable via CRON_HOUR / CRON_MINUTE)
  - DRY_RUN=true → run once immediately, skip real API calls
  - RUN_ONCE=true → run once then exit (for Railway one-off deploys)

Environment variables:
  CRON_HOUR        — hour for daily run (default: 8)
  CRON_MINUTE      — minute for daily run (default: 0)
  DRY_RUN          — skip real API/DB calls (default: false)
  RUN_ONCE         — run pipeline once then exit (default: false)
  SENTRY_DSN       — optional Sentry error tracking
  LOG_LEVEL        — DEBUG / INFO / WARNING (default: INFO)
  SENDGRID_API_KEY — SendGrid API key for email digest (Railway-compatible)
  SENDGRID_FROM    — verified sender email in SendGrid
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Logging — must be configured before importing modules that use it
# os.makedirs before any FileHandler (terbukti P23)
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "pipeline.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Optional Sentry setup (before anything that can raise)
# ---------------------------------------------------------------------------

_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=_SENTRY_DSN, traces_sample_rate=0.1)
        logger.info("Sentry initialised")
    except ImportError:
        logger.warning("sentry-sdk not installed — skipping Sentry init")

# ---------------------------------------------------------------------------
# Pipeline imports
# ---------------------------------------------------------------------------

from src.scoring import score_batch
from src.storage import init_db, upsert_records, log_run_start, log_run_finish
from src.airtable_sync import (
    AirtableRecord,
    build_routing_engine_from_env,
    sync_to_airtable,
    clean_empty_rows,              # ← NEW: ghost row cleaner
)
from src.digest import DigestPayload, DigestRecord, send_digest

# Scrapers
from src.scrapers.source_static_example import StaticHtmlSourceScraper
from src.scrapers.source_authenticated import AuthenticatedSourceScraper
from src.scrapers.source_02_playwright_js import PlaywrightJsScraper
from src.scrapers.source_03_rest_api import RestApiScraper
from src.scrapers.source_04_rss_feed import RssFeedScraper
from src.scrapers.source_05_sitemap import SitemapScraper
from src.scrapers.source_06_pdf_source import PdfSourceScraper
from src.scrapers.source_07_authenticated_advanced import AdvancedAuthScraper

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DRY_RUN  = os.getenv("DRY_RUN",  "false").lower() == "true"
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"
CRON_HOUR   = int(os.getenv("CRON_HOUR",   "8"))
CRON_MINUTE = int(os.getenv("CRON_MINUTE", "0"))

# ---------------------------------------------------------------------------
# Pipeline stats dataclass
# ---------------------------------------------------------------------------

class PipelineStats:
    def __init__(self, run_id: str):
        self.run_id          = run_id
        self.started_at      = datetime.now(timezone.utc)
        self.sources_hit     = 0
        self.records_scraped  = 0
        self.records_scored   = 0
        self.records_new      = 0
        self.records_updated  = 0
        self.records_high     = 0
        self.records_medium   = 0
        self.records_low      = 0
        self.airtable_created = 0
        self.airtable_updated = 0
        self.airtable_cleaned = 0          # ← NEW: ghost rows removed
        self.digest_sent      = False
        self.errors: list[str] = []
        self.status           = "running"

    def to_storage_dict(self) -> dict:
        return {
            "status":           self.status,
            "sources_hit":      self.sources_hit,
            "records_scraped":  self.records_scraped,
            "records_new":      self.records_new,
            "records_updated":  self.records_updated,
            "records_high":     self.records_high,
            "records_medium":   self.records_medium,
            "records_low":      self.records_low,
            "errors":           "\n".join(self.errors) if self.errors else None,
        }


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def step_scrape(stats: PipelineStats) -> list[dict]:
    """Run all scrapers. Returns flat list of normalised record dicts."""
    scrapers = [
        StaticHtmlSourceScraper(),
        AuthenticatedSourceScraper(),
        PlaywrightJsScraper(),
        RestApiScraper(),
        RssFeedScraper(),
        SitemapScraper(),
        PdfSourceScraper(),
        AdvancedAuthScraper(),
    ]

    all_records: list[dict] = []

    for scraper in scrapers:
        name = scraper.__class__.__name__
        try:
            result = scraper.scrape()
            # scraper.scrape() returns ScrapeResult object — extract .records
            if hasattr(result, "records"):
                records = result.records
            elif isinstance(result, list):
                records = result
            else:
                records = list(result)
            all_records.extend(records)
            stats.sources_hit += 1
            logger.info("✅ %s → %d records", name, len(records))
        except Exception as exc:  # noqa: BLE001
            msg = f"{name}: {exc}"
            logger.error("❌ Scraper failed — %s", msg)
            stats.errors.append(msg)
            if _SENTRY_DSN:
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception(exc)
                except ImportError:
                    pass

    stats.records_scraped = len(all_records)
    logger.info("Scraped total: %d records from %d sources", len(all_records), stats.sources_hit)
    return all_records


def step_score(records: list[dict], stats: PipelineStats) -> list[dict]:
    """Score records via Groq LLM. Returns records with score fields added."""
    if not records:
        logger.info("No records to score — skipping")
        return []

    try:
        scored = score_batch(records)
        stats.records_scored = len(scored)

        for rec in scored:
            tier = rec.get("fit_tier", "LOW")
            if tier == "HIGH":
                stats.records_high += 1
            elif tier == "MEDIUM":
                stats.records_medium += 1
            else:
                stats.records_low += 1

        logger.info(
            "Scored %d records — HIGH: %d MEDIUM: %d LOW: %d",
            len(scored), stats.records_high, stats.records_medium, stats.records_low,
        )
        return scored
    except Exception as exc:  # noqa: BLE001
        msg = f"Scoring failed: {exc}"
        logger.error("❌ %s", msg)
        stats.errors.append(msg)
        return records  # pass through unscored rather than losing data


def step_store(scored: list[dict], stats: PipelineStats) -> None:
    """Upsert records into PostgreSQL."""
    if not scored:
        return
    if DRY_RUN:
        logger.info("[DRY RUN] Would upsert %d records to DB", len(scored))
        return
    try:
        new_c, upd_c = upsert_records(scored)
        stats.records_new     = new_c
        stats.records_updated = upd_c
        logger.info("DB upsert: %d new, %d updated", new_c, upd_c)
    except Exception as exc:  # noqa: BLE001
        msg = f"DB upsert failed: {exc}"
        logger.error("❌ %s", msg)
        stats.errors.append(msg)


def step_airtable(scored: list[dict], stats: PipelineStats) -> None:
    """Route and sync records to Airtable."""
    if not scored:
        return

    at_records: list[AirtableRecord] = []
    for rec in scored:
        source_id = rec.get("url") or rec.get("source_id") or str(uuid.uuid4())
        at_records.append(AirtableRecord(
            source_id=source_id,
            title=rec.get("title", ""),
            url=rec.get("url", ""),
            score=float(rec.get("total_score", rec.get("score", 0))),
            summary=rec.get("description", ""),
            reasoning=rec.get("reasoning", ""),
            source_name=rec.get("source_name", ""),
            scraped_at=str(rec.get("scraped_at", "")),
        ))

    routing_engine = build_routing_engine_from_env()

    try:
        result = sync_to_airtable(at_records, routing_engine=routing_engine, dry_run=DRY_RUN)
        stats.airtable_created = result.created
        stats.airtable_updated = result.updated
        logger.info(
            "Airtable sync: %d created, %d updated, %d errors",
            result.created, result.updated, result.errors,
        )
        if result.errors > 0:
            stats.errors.append(f"Airtable: {result.errors} records failed to sync")
    except Exception as exc:  # noqa: BLE001
        msg = f"Airtable sync failed: {exc}"
        logger.error("❌ %s", msg)
        stats.errors.append(msg)


def step_clean_airtable(stats: PipelineStats) -> None:
    """
    Remove ghost empty rows from all Airtable tables after each sync.

    Ghost rows come from:
    - Airtable's 3 default empty rows on table creation
    - Records with no Source ID from failed scoring runs

    Non-critical — failure is logged as warning, not added to stats.errors.
    """
    if DRY_RUN:
        logger.info("[DRY RUN] Would clean empty rows from Airtable tables")
        return
    try:
        results = clean_empty_rows()
        total   = sum(results.values())
        stats.airtable_cleaned = total
        if total > 0:
            logger.info("🧹 Airtable cleanup: removed %d empty rows %s", total, results)
        else:
            logger.info("✅ Airtable tables clean — no empty rows found")
    except Exception as exc:
        # Non-critical — don't fail the pipeline over cleanup
        logger.warning("⚠️  Airtable cleanup skipped: %s", exc)


def step_digest(scored: list[dict], stats: PipelineStats) -> None:
    """Build and send email digest via SendGrid (primary) or SMTP (fallback)."""
    if not scored:
        logger.info("No records — skipping digest")
        return

    tier_map = {"HIGH": "High Fit", "MEDIUM": "Medium Fit", "LOW": "Low Fit"}
    digest_records = [
        DigestRecord(
            title=rec.get("title", ""),
            url=rec.get("url", ""),
            score=float(rec.get("total_score", rec.get("score", 0))),
            source_name=rec.get("source_name", ""),
            summary=rec.get("description", ""),
            reasoning=rec.get("reasoning", ""),
            tier=tier_map.get(rec.get("fit_tier", "LOW"), "Low Fit"),
        )
        for rec in scored
    ]

    run_label = (
        f"Data Aggregation Digest — "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    )
    payload = DigestPayload(records=digest_records, run_label=run_label)

    try:
        sent = send_digest(payload, dry_run=DRY_RUN)
        stats.digest_sent = sent
        if sent:
            logger.info("✅ Digest sent")
        else:
            logger.warning("⚠️  Digest send returned False")
            stats.errors.append("Digest: send_digest returned False")
    except Exception as exc:  # noqa: BLE001
        msg = f"Digest failed: {exc}"
        logger.error("❌ %s", msg)
        stats.errors.append(msg)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> PipelineStats:
    """Execute one full pipeline run. Returns stats."""
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    stats  = PipelineStats(run_id=run_id)

    logger.info("=" * 60)
    logger.info("Pipeline start — run_id=%s  DRY_RUN=%s", run_id, DRY_RUN)
    logger.info("=" * 60)

    if not DRY_RUN:
        try:
            log_run_start(run_id)
        except Exception as exc:
            logger.warning("Could not log run start: %s", exc)

    try:
        # Step 1 — Scrape
        raw_records = step_scrape(stats)

        # Step 2 — Score
        scored = step_score(raw_records, stats)

        # Step 3 — Store
        step_store(scored, stats)

        # Step 4 — Airtable sync
        step_airtable(scored, stats)

        # Step 4b — Clean ghost empty rows from Airtable
        step_clean_airtable(stats)

        # Step 5 — Email digest
        step_digest(scored, stats)

        stats.status = "done" if not stats.errors else "done_with_errors"

    except Exception as exc:  # noqa: BLE001
        stats.status = "failed"
        stats.errors.append(f"Pipeline crash: {exc}")
        logger.error("❌ Pipeline crashed: %s", exc, exc_info=True)
        if _SENTRY_DSN:
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(exc)
            except ImportError:
                pass

    finally:
        elapsed = (datetime.now(timezone.utc) - stats.started_at).total_seconds()
        logger.info(
            "Pipeline %s in %.1fs — scraped=%d scored=%d high=%d medium=%d low=%d "
            "airtable_cleaned=%d errors=%d",
            stats.status, elapsed,
            stats.records_scraped, stats.records_scored,
            stats.records_high, stats.records_medium, stats.records_low,
            stats.airtable_cleaned,
            len(stats.errors),
        )

        if not DRY_RUN:
            try:
                log_run_finish(run_id, stats.to_storage_dict())
            except Exception as exc:
                logger.warning("Could not log run finish: %s", exc)

    return stats


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    """Start APScheduler with a daily cron trigger."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error("apscheduler not installed — pip install apscheduler")
        sys.exit(1)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_pipeline,
        trigger=CronTrigger(hour=CRON_HOUR, minute=CRON_MINUTE),
        id="daily_pipeline",
        name=f"Daily pipeline @ {CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC",
        misfire_grace_time=300,
    )

    logger.info(
        "Scheduler started — daily pipeline @ %02d:%02d UTC",
        CRON_HOUR, CRON_MINUTE,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if DRY_RUN:
        logger.info("DRY_RUN mode — running pipeline once immediately")
        stats = run_pipeline()
        sys.exit(0 if stats.status != "failed" else 1)

    elif RUN_ONCE:
        logger.info("RUN_ONCE mode — running pipeline once then exiting")
        stats = run_pipeline()
        sys.exit(0 if stats.status != "failed" else 1)

    else:
        # Normal mode: initialise DB then start scheduler
        logger.info("Initialising database...")
        try:
            init_db()
        except Exception as exc:
            logger.error("DB init failed: %s", exc)
            sys.exit(1)

        start_scheduler()