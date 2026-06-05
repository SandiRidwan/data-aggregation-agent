"""
storage.py — Database Layer
SQLAlchemy ORM models + CRUD operations.
PostgreSQL via psycopg2-binary. Alembic-ready.

Patterns from registry:
- Railway DATABASE_URL parsing (terbukti P23)
- numpy → psycopg2 type cast via _cast() (terbukti P23)
- UPSERT ON CONFLICT DO UPDATE (terbukti P23)
- os.makedirs before FileHandler (terbukti P23)
"""

import os
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    create_engine, Column, String, Integer, Float,
    Boolean, DateTime, Text, UniqueConstraint, Index
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)


# ─── DB Connection ────────────────────────────────────────────────────────────

def get_engine():
    """
    Build SQLAlchemy engine.
    Dual-mode: Railway (DATABASE_URL env var) or local .env config.
    Pattern: Railway DATABASE_URL parsing (terbukti P23)
    """
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        # Railway injects postgres:// — SQLAlchemy needs postgresql://
        database_url = database_url.replace("postgres://", "postgresql://", 1)
        logger.info("Using Railway DATABASE_URL")
    else:
        # Local dev fallback
        host     = os.getenv("DB_HOST",     "localhost")
        port     = os.getenv("DB_PORT",     "5432")
        name     = os.getenv("DB_NAME",     "aggregator")
        user     = os.getenv("DB_USER",     "postgres")
        password = os.getenv("DB_PASSWORD", "")
        database_url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
        logger.info(f"Using local DB: {host}:{port}/{name}")

    return create_engine(database_url, pool_pre_ping=True, echo=False)


# ─── Models ───────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Record(Base):
    """
    A scraped + scored record from any source.
    url is the dedup key — UPSERT on conflict.
    """
    __tablename__ = "records"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    url             = Column(String(2048), nullable=False)
    title           = Column(Text,    nullable=False)
    description     = Column(Text)
    source_name     = Column(String(128))
    published_date  = Column(String(32))   # ISO string, flexible format
    category        = Column(String(128))
    organization    = Column(String(256))
    budget          = Column(String(128))
    deadline        = Column(String(64))
    contact_email   = Column(String(256))
    pdf_text        = Column(Text)

    # Scoring fields
    relevance       = Column(Integer, default=0)
    recency         = Column(Integer, default=0)
    completeness    = Column(Integer, default=0)
    actionability   = Column(Integer, default=0)
    total_score     = Column(Integer, default=0)
    fit_tier        = Column(String(16), default="LOW")
    reasoning       = Column(Text)
    flag_for_review = Column(Boolean, default=False)

    # Routing
    routed_to       = Column(String(128))   # Airtable table name
    routed_at       = Column(DateTime)
    emailed         = Column(Boolean, default=False)

    # Metadata
    scraped_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("url", name="uq_records_url"),
        Index("ix_records_fit_tier",     "fit_tier"),
        Index("ix_records_total_score",  "total_score"),
        Index("ix_records_flag_review",  "flag_for_review"),
        Index("ix_records_scraped_at",   "scraped_at"),
    )

    def __repr__(self):
        return f"<Record id={self.id} tier={self.fit_tier} score={self.total_score} '{self.title[:40]}'>"


class RunLog(Base):
    """Audit trail — one row per pipeline run."""
    __tablename__ = "run_logs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(64), nullable=False)
    started_at      = Column(DateTime, default=datetime.utcnow)
    finished_at     = Column(DateTime)
    sources_hit     = Column(Integer, default=0)
    records_scraped = Column(Integer, default=0)
    records_new     = Column(Integer, default=0)
    records_updated = Column(Integer, default=0)
    records_high    = Column(Integer, default=0)
    records_medium  = Column(Integer, default=0)
    records_low     = Column(Integer, default=0)
    errors          = Column(Text)
    status          = Column(String(32), default="running")  # running | done | failed


# ─── DB Init ─────────────────────────────────────────────────────────────────

_engine       = None
_SessionLocal = None


def init_db():
    """Create all tables. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    global _engine, _SessionLocal
    _engine       = get_engine()
    _SessionLocal = sessionmaker(bind=_engine)
    Base.metadata.create_all(_engine)
    logger.info("✅ Database tables initialized")


def get_session() -> Session:
    if _SessionLocal is None:
        init_db()
    return _SessionLocal()


# ─── CRUD Operations ──────────────────────────────────────────────────────────

def upsert_records(scored_records: list[dict]) -> tuple[int, int]:
    """
    Upsert scored records into DB. Dedup by URL.
    Pattern: UPSERT ON CONFLICT DO UPDATE (terbukti P23)
    
    Returns:
        (new_count, updated_count)
    """
    if not scored_records:
        return 0, 0

    session    = get_session()
    new_count  = 0
    upd_count  = 0

    try:
        for rec in scored_records:
            url = rec.get("url", "").strip()
            if not url:
                logger.debug(f"Skipping record with no URL: {rec.get('title','')[:50]}")
                continue

            # Build values dict — only columns that exist in the model
            values = {
                "url":             url,
                "title":           _safe_str(rec.get("title"), 500),
                "description":     _safe_str(rec.get("description")),
                "source_name":     _safe_str(rec.get("source_name"), 128),
                "published_date":  _safe_str(rec.get("published_date"), 32),
                "category":        _safe_str(rec.get("category"), 128),
                "organization":    _safe_str(rec.get("organization"), 256),
                "budget":          _safe_str(rec.get("budget"), 128),
                "deadline":        _safe_str(rec.get("deadline"), 64),
                "contact_email":   _safe_str(rec.get("contact_email"), 256),
                "pdf_text":        rec.get("pdf_text"),
                "relevance":       _cast_int(rec.get("relevance", 0)),
                "recency":         _cast_int(rec.get("recency", 0)),
                "completeness":    _cast_int(rec.get("completeness", 0)),
                "actionability":   _cast_int(rec.get("actionability", 0)),
                "total_score":     _cast_int(rec.get("total_score", 0)),
                "fit_tier":        _safe_str(rec.get("fit_tier", "LOW"), 16),
                "reasoning":       rec.get("reasoning"),
                "flag_for_review": bool(rec.get("flag_for_review", False)),
                "scraped_at":      _parse_dt(rec.get("scraped_at")),
                "updated_at":      datetime.utcnow(),
            }

            # PostgreSQL UPSERT
            stmt = pg_insert(Record).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["url"],
                set_={
                    k: stmt.excluded[k]
                    for k in values
                    if k != "url"
                }
            )

            result = session.execute(stmt)
            # rowcount 1 = new, 0 = no change (identical), check via lastrowid trick
            if result.rowcount == 1:
                new_count += 1
            else:
                upd_count += 1

        session.commit()
        logger.info(f"Upserted {new_count} new + {upd_count} updated records")

    except Exception as e:
        session.rollback()
        logger.error(f"DB upsert failed: {e}", exc_info=True)
        raise
    finally:
        session.close()

    return new_count, upd_count


def get_high_fit_records(limit: int = 100) -> list[Record]:
    """Fetch HIGH tier records not yet emailed — for digest."""
    session = get_session()
    try:
        return (
            session.query(Record)
            .filter(Record.fit_tier == "HIGH", Record.emailed == False)
            .order_by(Record.total_score.desc())
            .limit(limit)
            .all()
        )
    finally:
        session.close()


def mark_emailed(record_ids: list[int]) -> None:
    """Mark records as included in digest."""
    session = get_session()
    try:
        session.query(Record).filter(Record.id.in_(record_ids)).update(
            {"emailed": True}, synchronize_session=False
        )
        session.commit()
    finally:
        session.close()


def log_run_start(run_id: str) -> RunLog:
    session = get_session()
    try:
        run = RunLog(run_id=run_id, status="running")
        session.add(run)
        session.commit()
        session.refresh(run)
        return run
    finally:
        session.close()


def log_run_finish(run_id: str, stats: dict) -> None:
    session = get_session()
    try:
        session.query(RunLog).filter_by(run_id=run_id).update({
            "finished_at":      datetime.utcnow(),
            "status":           stats.get("status", "done"),
            "sources_hit":      stats.get("sources_hit", 0),
            "records_scraped":  stats.get("records_scraped", 0),
            "records_new":      stats.get("records_new", 0),
            "records_updated":  stats.get("records_updated", 0),
            "records_high":     stats.get("records_high", 0),
            "records_medium":   stats.get("records_medium", 0),
            "records_low":      stats.get("records_low", 0),
            "errors":           stats.get("errors"),
        })
        session.commit()
    finally:
        session.close()


# ─── Type Helpers ─────────────────────────────────────────────────────────────

def _cast(val):
    """Convert numpy scalars to Python native types. (terbukti P23)"""
    return val.item() if hasattr(val, "item") else val


def _cast_int(val) -> int:
    try:
        return int(_cast(val))
    except (TypeError, ValueError):
        return 0


def _safe_str(val, max_len: Optional[int] = None) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if max_len and len(s) > max_len:
        s = s[:max_len]
    return s or None


def _parse_dt(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None
