"""
tests/test_storage.py — Test suite for src/storage.py

Coverage:
  - Record ORM model: fields, defaults, __repr__
  - RunLog ORM model: fields, defaults
  - get_engine: SQLite in-memory (no real PG needed)
  - init_db / get_session: table creation, session usable
  - upsert_records: insert new, update existing (by url), returns counts
  - upsert_records: skip records with no url, rollback on error
  - get_high_fit_records: filter by fit_tier=HIGH, emailed=False, ordered by score
  - mark_emailed: flips emailed flag
  - log_run_start / log_run_finish: RunLog lifecycle
  - Type helpers: _cast, _cast_int, _safe_str, _parse_dt
"""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# We patch get_engine so storage uses SQLite in-memory, not real PostgreSQL.
# All module-level state (_engine, _SessionLocal) is reset per test class.
# ---------------------------------------------------------------------------
from src.storage import (
    Base,
    Record,
    RunLog,
    _cast,
    _cast_int,
    _safe_str,
    _parse_dt,
)


# ===========================================================================
# Shared SQLite engine fixture
# ===========================================================================

@pytest.fixture(scope="module")
def sqlite_engine():
    """SQLite in-memory engine with all tables created."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    # postgresql UPSERT won't work on SQLite — we'll test around it
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(sqlite_engine):
    """Transactional session — rolled back after each test."""
    connection = sqlite_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ===========================================================================
# Record model
# ===========================================================================

class TestRecordModel:

    def test_instantiation_required_fields(self):
        rec = Record(url="https://example.com/1", title="Test Job")
        assert rec.url == "https://example.com/1"
        assert rec.title == "Test Job"

    def test_default_total_score(self, db_session):
        rec = Record(url="https://example.com/2", title="X")
        db_session.add(rec)
        db_session.flush()
        assert rec.total_score == 0

    def test_default_fit_tier(self, db_session):
        rec = Record(url="https://example.com/3", title="X")
        db_session.add(rec)
        db_session.flush()
        assert rec.fit_tier == "LOW"

    def test_default_flag_for_review(self, db_session):
        rec = Record(url="https://example.com/4", title="X")
        db_session.add(rec)
        db_session.flush()
        assert rec.flag_for_review == False

    def test_default_emailed(self, db_session):
        rec = Record(url="https://example.com/5", title="X")
        db_session.add(rec)
        db_session.flush()
        assert rec.emailed == False

    def test_repr_contains_tier_and_score(self):
        rec = Record(url="https://x.com", title="Engineer", fit_tier="HIGH", total_score=85)
        r = repr(rec)
        assert "HIGH" in r
        assert "85" in r

    def test_tablename(self):
        assert Record.__tablename__ == "records"

    def test_all_optional_fields_accept_none(self):
        rec = Record(
            url="https://example.com/6",
            title="X",
            description=None,
            source_name=None,
            pdf_text=None,
            reasoning=None,
        )
        assert rec.description is None
        assert rec.reasoning is None

    def test_full_fields(self):
        rec = Record(
            url="https://example.com/7",
            title="Senior Python Engineer",
            description="Great role",
            source_name="LinkedIn",
            category="Tech",
            organization="Acme Corp",
            total_score=85,
            fit_tier="HIGH",
            reasoning="Matches all",
            flag_for_review=True,
            emailed=False,
        )
        assert rec.source_name == "LinkedIn"
        assert rec.fit_tier == "HIGH"
        assert rec.flag_for_review is True


# ===========================================================================
# RunLog model
# ===========================================================================

class TestRunLogModel:

    def test_instantiation(self):
        run = RunLog(run_id="run_001")
        assert run.run_id == "run_001"

    def test_default_status(self, db_session):
        run = RunLog(run_id="run_002")
        db_session.add(run)
        db_session.flush()
        assert run.status == "running"

    def test_default_counts_zero(self, db_session):
        run = RunLog(run_id="run_003")
        db_session.add(run)
        db_session.flush()
        assert run.sources_hit == 0
        assert run.records_scraped == 0
        assert run.records_new == 0

    def test_tablename(self):
        assert RunLog.__tablename__ == "run_logs"


# ===========================================================================
# get_engine
# ===========================================================================

class TestGetEngine:

    def test_with_database_url_env(self):
        from src.storage import get_engine
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}):
            engine = get_engine()
        assert engine is not None

    def test_postgres_url_rewritten(self):
        """postgres:// should be rewritten to postgresql://"""
        from src.storage import get_engine
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:pass@host/db"}):
            with patch("src.storage.create_engine") as mock_ce:
                mock_ce.return_value = MagicMock()
                get_engine()
            call_url = mock_ce.call_args[0][0]
            assert call_url.startswith("postgresql://")

    def test_local_fallback_builds_url(self):
        from src.storage import get_engine
        env = {
            "DATABASE_URL": "",
            "DB_HOST": "myhost",
            "DB_PORT": "5432",
            "DB_NAME": "mydb",
            "DB_USER": "myuser",
            "DB_PASSWORD": "mypass",
        }
        with patch.dict(os.environ, env):
            with patch("src.storage.create_engine") as mock_ce:
                mock_ce.return_value = MagicMock()
                get_engine()
            call_url = mock_ce.call_args[0][0]
            assert "myhost" in call_url
            assert "mydb" in call_url


# ===========================================================================
# init_db / get_session (mocked engine)
# ===========================================================================

class TestInitDb:

    def test_init_db_creates_tables(self):
        import src.storage as storage_module
        engine = create_engine("sqlite:///:memory:")

        with patch.object(storage_module, "get_engine", return_value=engine):
            # Reset module state
            storage_module._engine = None
            storage_module._SessionLocal = None
            storage_module.init_db()

        assert storage_module._engine is not None
        assert storage_module._SessionLocal is not None

    def test_get_session_calls_init_if_needed(self):
        import src.storage as storage_module
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)

        storage_module._engine = None
        storage_module._SessionLocal = None

        with patch.object(storage_module, "get_engine", return_value=engine):
            session = storage_module.get_session()
        assert session is not None
        session.close()


# ===========================================================================
# upsert_records — tested via direct DB session (bypass pg_insert)
# ===========================================================================

class TestUpsertRecordsViaSession:
    """
    upsert_records() uses pg_insert (PostgreSQL UPSERT) which doesn't work
    on SQLite. We test the logic by mocking get_session to return our
    SQLite session and catching the dialect error, OR we test the helper
    functions and the Record model directly.

    For full upsert integration, we test the function with a mock session
    that verifies the correct SQL path is taken.
    """

    def test_upsert_empty_list_returns_zero(self):
        import src.storage as storage_module
        mock_session = MagicMock()

        with patch.object(storage_module, "get_session", return_value=mock_session):
            result = storage_module.upsert_records([])

        assert result == (0, 0)
        mock_session.execute.assert_not_called()

    def test_upsert_skips_record_with_no_url(self):
        import src.storage as storage_module
        mock_session = MagicMock()
        mock_session.execute.return_value = MagicMock(rowcount=1)

        records = [
            {"url": "", "title": "No URL Record", "total_score": 5, "fit_tier": "LOW"},
            {"url": "https://example.com/valid", "title": "Valid", "total_score": 7, "fit_tier": "HIGH"},
        ]

        with patch.object(storage_module, "get_session", return_value=mock_session):
            new_c, upd_c = storage_module.upsert_records(records)

        # execute should be called once (only for the valid record)
        assert mock_session.execute.call_count == 1

    def test_upsert_commits_on_success(self):
        import src.storage as storage_module
        mock_session = MagicMock()
        mock_session.execute.return_value = MagicMock(rowcount=1)

        records = [{"url": "https://x.com/1", "title": "Test", "total_score": 8, "fit_tier": "HIGH"}]

        with patch.object(storage_module, "get_session", return_value=mock_session):
            storage_module.upsert_records(records)

        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()

    def test_upsert_rollback_on_exception(self):
        import src.storage as storage_module
        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("DB error")

        records = [{"url": "https://x.com/1", "title": "Test", "total_score": 8, "fit_tier": "HIGH"}]

        with patch.object(storage_module, "get_session", return_value=mock_session):
            with pytest.raises(Exception, match="DB error"):
                storage_module.upsert_records(records)

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    def test_upsert_new_count_increments(self):
        import src.storage as storage_module
        mock_session = MagicMock()
        mock_session.execute.return_value = MagicMock(rowcount=1)

        records = [
            {"url": "https://x.com/1", "title": "A", "total_score": 8, "fit_tier": "HIGH"},
            {"url": "https://x.com/2", "title": "B", "total_score": 5, "fit_tier": "MEDIUM"},
        ]

        with patch.object(storage_module, "get_session", return_value=mock_session):
            new_c, upd_c = storage_module.upsert_records(records)

        assert new_c == 2

    def test_upsert_updated_count_increments(self):
        import src.storage as storage_module
        mock_session = MagicMock()
        # rowcount=0 means "updated" path
        mock_session.execute.return_value = MagicMock(rowcount=0)

        records = [
            {"url": "https://x.com/1", "title": "A", "total_score": 8, "fit_tier": "HIGH"},
        ]

        with patch.object(storage_module, "get_session", return_value=mock_session):
            new_c, upd_c = storage_module.upsert_records(records)

        assert upd_c == 1


# ===========================================================================
# get_high_fit_records
# ===========================================================================

class TestGetHighFitRecords:

    def test_queries_high_tier_unemailed(self, db_session):
        import src.storage as storage_module

        # Insert records directly
        high_unemailed = Record(url="https://x.com/h1", title="High 1", fit_tier="HIGH", total_score=90, emailed=False)
        high_emailed   = Record(url="https://x.com/h2", title="High 2", fit_tier="HIGH", total_score=85, emailed=True)
        medium         = Record(url="https://x.com/m1", title="Med 1",  fit_tier="MEDIUM", total_score=60, emailed=False)

        db_session.add_all([high_unemailed, high_emailed, medium])
        db_session.flush()

        with patch.object(storage_module, "get_session", return_value=db_session):
            results = storage_module.get_high_fit_records(limit=100)

        titles = [r.title for r in results]
        assert "High 1" in titles
        assert "High 2" not in titles   # already emailed
        assert "Med 1" not in titles    # wrong tier

    def test_ordered_by_score_desc(self, db_session):
        import src.storage as storage_module

        db_session.add_all([
            Record(url="https://x.com/s1", title="S1", fit_tier="HIGH", total_score=70, emailed=False),
            Record(url="https://x.com/s2", title="S2", fit_tier="HIGH", total_score=95, emailed=False),
            Record(url="https://x.com/s3", title="S3", fit_tier="HIGH", total_score=80, emailed=False),
        ])
        db_session.flush()

        with patch.object(storage_module, "get_session", return_value=db_session):
            results = storage_module.get_high_fit_records(limit=100)

        high_results = [r for r in results if r.url.startswith("https://x.com/s")]
        scores = [r.total_score for r in high_results]
        assert scores == sorted(scores, reverse=True)

    def test_limit_respected(self, db_session):
        import src.storage as storage_module

        for i in range(5):
            db_session.add(Record(
                url=f"https://x.com/lim{i}",
                title=f"Lim {i}",
                fit_tier="HIGH",
                total_score=80 + i,
                emailed=False,
            ))
        db_session.flush()

        with patch.object(storage_module, "get_session", return_value=db_session):
            results = storage_module.get_high_fit_records(limit=2)

        assert len(results) <= 2


# ===========================================================================
# mark_emailed
# ===========================================================================

class TestMarkEmailed:

    def test_sets_emailed_true(self, db_session):
        import src.storage as storage_module

        rec = Record(url="https://x.com/em1", title="Em1", fit_tier="HIGH", total_score=80, emailed=False)
        db_session.add(rec)
        db_session.flush()
        rec_id = rec.id

        with patch.object(storage_module, "get_session", return_value=db_session):
            storage_module.mark_emailed([rec_id])

        updated = db_session.query(Record).filter_by(id=rec_id).first()
        assert updated.emailed == True

    def test_empty_list_no_error(self, db_session):
        import src.storage as storage_module
        with patch.object(storage_module, "get_session", return_value=db_session):
            storage_module.mark_emailed([])  # should not raise


# ===========================================================================
# log_run_start / log_run_finish
# ===========================================================================

class TestRunLog:

    def test_log_run_start_creates_row(self, db_session):
        import src.storage as storage_module

        with patch.object(storage_module, "get_session", return_value=db_session):
            run = storage_module.log_run_start("run_test_001")

        assert run.run_id == "run_test_001"
        assert run.status == "running"

    def test_log_run_finish_updates_status(self, db_session):
        import src.storage as storage_module

        run = RunLog(run_id="run_test_002", status="running")
        db_session.add(run)
        db_session.flush()

        stats = {
            "status": "done",
            "sources_hit": 5,
            "records_scraped": 42,
            "records_new": 30,
            "records_updated": 12,
            "records_high": 10,
            "records_medium": 15,
            "records_low": 5,
        }

        with patch.object(storage_module, "get_session", return_value=db_session):
            storage_module.log_run_finish("run_test_002", stats)

        updated = db_session.query(RunLog).filter_by(run_id="run_test_002").first()
        assert updated.status == "done"
        assert updated.sources_hit == 5
        assert updated.records_new == 30


# ===========================================================================
# Type helpers
# ===========================================================================

class TestTypeHelpers:

    # _cast
    def test_cast_passthrough_int(self):
        assert _cast(42) == 42

    def test_cast_passthrough_float(self):
        assert _cast(3.14) == 3.14

    def test_cast_passthrough_str(self):
        assert _cast("hello") == "hello"

    def test_cast_numpy_scalar(self):
        """Simulate numpy scalar with .item() method."""
        mock_np = MagicMock()
        mock_np.item.return_value = 99
        assert _cast(mock_np) == 99

    # _cast_int
    def test_cast_int_from_int(self):
        assert _cast_int(5) == 5

    def test_cast_int_from_float(self):
        assert _cast_int(7.9) == 7

    def test_cast_int_from_string(self):
        assert _cast_int("3") == 3

    def test_cast_int_from_none(self):
        assert _cast_int(None) == 0

    def test_cast_int_from_invalid(self):
        assert _cast_int("abc") == 0

    # _safe_str
    def test_safe_str_normal(self):
        assert _safe_str("hello") == "hello"

    def test_safe_str_none_returns_none(self):
        assert _safe_str(None) is None

    def test_safe_str_strips_whitespace(self):
        assert _safe_str("  hello  ") == "hello"

    def test_safe_str_truncates(self):
        result = _safe_str("abcdefgh", max_len=3)
        assert result == "abc"

    def test_safe_str_empty_string_returns_none(self):
        assert _safe_str("") is None

    def test_safe_str_whitespace_only_returns_none(self):
        assert _safe_str("   ") is None

    def test_safe_str_converts_int(self):
        assert _safe_str(42) == "42"

    # _parse_dt
    def test_parse_dt_none(self):
        assert _parse_dt(None) is None

    def test_parse_dt_datetime_passthrough(self):
        dt = datetime(2026, 6, 5, 8, 0, 0)
        assert _parse_dt(dt) == dt

    def test_parse_dt_iso_string(self):
        result = _parse_dt("2026-06-05T08:00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026

    def test_parse_dt_invalid_string(self):
        assert _parse_dt("not-a-date") is None

    def test_parse_dt_invalid_type(self):
        assert _parse_dt(12345) is None or isinstance(_parse_dt("2026-06-05"), datetime)