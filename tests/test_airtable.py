"""
tests/test_airtable.py — Test suite for airtable_sync.py

Coverage:
  - AirtableRecord: field mapping, to_airtable_fields, extra JSON
  - RoutingEngine: threshold boundaries, all 3 buckets, edge cases
  - AirtableClient: list_records pagination, create, update, upsert (create + update paths)
  - sync_to_airtable: dry run, live (mocked), error handling, missing target_table
  - _make_dry_run_client: no real HTTP calls
  - build helpers: env-based construction
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch, call

import pytest
import requests

# ---------------------------------------------------------------------------
# Import under test
# ---------------------------------------------------------------------------
from src.airtable_sync import (
    AirtableClient,
    AirtableRecord,
    RoutingEngine,
    SyncResult,
    _make_dry_run_client,
    build_client_from_env,
    build_routing_engine_from_env,
    sync_to_airtable,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def routing_engine():
    return RoutingEngine(
        table_a="High Fit",
        table_b="Medium Fit",
        table_c="Low Fit",
        threshold_high=7.0,
        threshold_medium=4.0,
    )


@pytest.fixture
def high_record():
    return AirtableRecord(
        source_id="src_001",
        title="Senior Python Engineer",
        url="https://example.com/job/001",
        score=8.5,
        summary="Great fit",
        reasoning="Matches all requirements",
        source_name="LinkedIn",
        scraped_at="2026-06-05T08:00:00Z",
    )


@pytest.fixture
def medium_record():
    return AirtableRecord(
        source_id="src_002",
        title="Python Developer",
        url="https://example.com/job/002",
        score=5.5,
    )


@pytest.fixture
def low_record():
    return AirtableRecord(
        source_id="src_003",
        title="Junior Developer",
        url="https://example.com/job/003",
        score=2.0,
    )


def _make_mock_session(list_records_response=None, create_response=None, update_response=None):
    """Helper: build a mock requests.Session."""
    session = MagicMock(spec=requests.Session)
    session.headers = {}

    list_resp = MagicMock()
    list_resp.raise_for_status.return_value = None
    list_resp.json.return_value = list_records_response or {"records": []}
    session.get.return_value = list_resp

    create_resp = MagicMock()
    create_resp.raise_for_status.return_value = None
    create_resp.json.return_value = create_response or {"id": "recABC123", "fields": {}}
    session.post.return_value = create_resp

    update_resp = MagicMock()
    update_resp.raise_for_status.return_value = None
    update_resp.json.return_value = update_response or {"id": "recABC123", "fields": {}}
    session.patch.return_value = update_resp

    return session


def _make_client(session=None) -> AirtableClient:
    return AirtableClient(
        token="test_token",
        base_id="appTESTBASE",
        session=session or _make_mock_session(),
    )


# ===========================================================================
# AirtableRecord
# ===========================================================================

class TestAirtableRecord:

    def test_required_fields_present(self, high_record):
        fields = high_record.to_airtable_fields()
        assert fields["Source ID"] == "src_001"
        assert fields["Title"] == "Senior Python Engineer"
        assert fields["URL"] == "https://example.com/job/001"
        assert fields["Score"] == 8.5

    def test_optional_fields_included(self, high_record):
        fields = high_record.to_airtable_fields()
        assert fields["Summary"] == "Great fit"
        assert fields["Reasoning"] == "Matches all requirements"
        assert fields["Source"] == "LinkedIn"
        assert fields["Scraped At"] == "2026-06-05T08:00:00Z"

    def test_optional_fields_omitted_when_empty(self, medium_record):
        fields = medium_record.to_airtable_fields()
        assert "Summary" not in fields
        assert "Reasoning" not in fields
        assert "Source" not in fields
        assert "Scraped At" not in fields

    def test_extra_serialised_as_json(self):
        rec = AirtableRecord(
            source_id="src_004",
            title="Test",
            url="https://example.com",
            score=6.0,
            extra={"industry": "FinTech", "applicants": 42},
        )
        fields = rec.to_airtable_fields()
        assert "Extra" in fields
        parsed = json.loads(fields["Extra"])
        assert parsed["industry"] == "FinTech"
        assert parsed["applicants"] == 42

    def test_extra_omitted_when_empty(self, medium_record):
        fields = medium_record.to_airtable_fields()
        assert "Extra" not in fields

    def test_score_rounded_to_2dp(self):
        rec = AirtableRecord(
            source_id="src_005",
            title="X",
            url="https://x.com",
            score=7.1234567,
        )
        assert rec.to_airtable_fields()["Score"] == 7.12

    def test_default_target_table_empty(self, high_record):
        assert high_record.target_table == ""


# ===========================================================================
# RoutingEngine
# ===========================================================================

class TestRoutingEngine:

    def test_score_above_high_threshold(self, routing_engine, high_record):
        assert routing_engine.route(high_record) == "High Fit"

    def test_score_exactly_high_threshold(self, routing_engine):
        rec = AirtableRecord(source_id="x", title="X", url="u", score=7.0)
        assert routing_engine.route(rec) == "High Fit"

    def test_score_just_below_high_threshold(self, routing_engine):
        rec = AirtableRecord(source_id="x", title="X", url="u", score=6.99)
        assert routing_engine.route(rec) == "Medium Fit"

    def test_score_in_medium_range(self, routing_engine, medium_record):
        assert routing_engine.route(medium_record) == "Medium Fit"

    def test_score_exactly_medium_threshold(self, routing_engine):
        rec = AirtableRecord(source_id="x", title="X", url="u", score=4.0)
        assert routing_engine.route(rec) == "Medium Fit"

    def test_score_just_below_medium_threshold(self, routing_engine):
        rec = AirtableRecord(source_id="x", title="X", url="u", score=3.99)
        assert routing_engine.route(rec) == "Low Fit"

    def test_score_in_low_range(self, routing_engine, low_record):
        assert routing_engine.route(low_record) == "Low Fit"

    def test_score_zero(self, routing_engine):
        rec = AirtableRecord(source_id="x", title="X", url="u", score=0.0)
        assert routing_engine.route(rec) == "Low Fit"

    def test_score_ten(self, routing_engine):
        rec = AirtableRecord(source_id="x", title="X", url="u", score=10.0)
        assert routing_engine.route(rec) == "High Fit"

    def test_route_all_assigns_target_table(self, routing_engine, high_record, medium_record, low_record):
        records = [high_record, medium_record, low_record]
        routing_engine.route_all(records)
        assert high_record.target_table == "High Fit"
        assert medium_record.target_table == "Medium Fit"
        assert low_record.target_table == "Low Fit"

    def test_route_all_returns_same_list(self, routing_engine, high_record):
        records = [high_record]
        result = routing_engine.route_all(records)
        assert result is records

    def test_route_all_empty_list(self, routing_engine):
        result = routing_engine.route_all([])
        assert result == []

    def test_custom_table_names(self):
        engine = RoutingEngine(
            table_a="Tier 1",
            table_b="Tier 2",
            table_c="Tier 3",
            threshold_high=8.0,
            threshold_medium=5.0,
        )
        assert engine.route(AirtableRecord(source_id="x", title="X", url="u", score=9.0)) == "Tier 1"
        assert engine.route(AirtableRecord(source_id="x", title="X", url="u", score=6.0)) == "Tier 2"
        assert engine.route(AirtableRecord(source_id="x", title="X", url="u", score=3.0)) == "Tier 3"


# ===========================================================================
# AirtableClient
# ===========================================================================

class TestAirtableClient:

    def test_requires_token(self):
        with pytest.raises(ValueError, match="AIRTABLE_TOKEN"):
            AirtableClient(token="", base_id="appXXX")

    def test_requires_base_id(self):
        with pytest.raises(ValueError, match="AIRTABLE_BASE_ID"):
            AirtableClient(token="tok_xxx", base_id="")

    def test_list_records_single_page(self):
        session = _make_mock_session(
            list_records_response={"records": [{"id": "rec1"}, {"id": "rec2"}]}
        )
        client = _make_client(session)
        records = client.list_records("High Fit")
        assert len(records) == 2
        session.get.assert_called_once()

    def test_list_records_pagination(self):
        """Two pages — first response has offset, second does not."""
        page1 = MagicMock()
        page1.raise_for_status.return_value = None
        page1.json.return_value = {"records": [{"id": "rec1"}], "offset": "page2token"}

        page2 = MagicMock()
        page2.raise_for_status.return_value = None
        page2.json.return_value = {"records": [{"id": "rec2"}]}

        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = [page1, page2]

        client = _make_client(session)
        records = client.list_records("High Fit")
        assert len(records) == 2
        assert session.get.call_count == 2

    def test_list_records_with_filter(self):
        session = _make_mock_session()
        client = _make_client(session)
        client.list_records("High Fit", filter_formula="{Source ID}='src_001'")
        call_kwargs = session.get.call_args
        assert "filterByFormula" in call_kwargs.kwargs.get("params", call_kwargs[1].get("params", {})) or \
               "{Source ID}='src_001'" in str(call_kwargs)

    def test_create_record(self, high_record):
        session = _make_mock_session(create_response={"id": "recNEW001", "fields": {}})
        client = _make_client(session)
        result = client.create_record("High Fit", high_record.to_airtable_fields())
        assert result["id"] == "recNEW001"
        session.post.assert_called_once()

    def test_update_record(self, high_record):
        session = _make_mock_session(update_response={"id": "recEXIST001", "fields": {}})
        client = _make_client(session)
        result = client.update_record("High Fit", "recEXIST001", high_record.to_airtable_fields())
        assert result["id"] == "recEXIST001"
        session.patch.assert_called_once()

    def test_upsert_creates_when_not_exists(self, high_record):
        session = _make_mock_session(
            list_records_response={"records": []},
            create_response={"id": "recNEW001", "fields": {}},
        )
        client = _make_client(session)
        action, record_id = client.upsert_record("High Fit", high_record)
        assert action == "created"
        assert record_id == "recNEW001"
        session.post.assert_called_once()
        session.patch.assert_not_called()

    def test_upsert_updates_when_exists(self, high_record):
        session = _make_mock_session(
            list_records_response={"records": [{"id": "recEXIST001", "fields": {}}]},
            update_response={"id": "recEXIST001", "fields": {}},
        )
        client = _make_client(session)
        action, record_id = client.upsert_record("High Fit", high_record)
        assert action == "updated"
        assert record_id == "recEXIST001"
        session.patch.assert_called_once()
        session.post.assert_not_called()

    def test_http_error_propagates(self, high_record):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
        session.get.return_value = bad_resp

        client = _make_client(session)
        with pytest.raises(requests.HTTPError):
            client.list_records("High Fit")


# ===========================================================================
# sync_to_airtable
# ===========================================================================

class TestSyncToAirtable:

    def test_dry_run_no_http_calls(self, high_record, medium_record, low_record, routing_engine):
        result = sync_to_airtable(
            [high_record, medium_record, low_record],
            routing_engine=routing_engine,
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.errors == 0
        assert result.created == 3  # dry run counts all as "would create"

    def test_dry_run_str_representation(self, routing_engine, high_record):
        result = sync_to_airtable([high_record], routing_engine=routing_engine, dry_run=True)
        assert "[DRY RUN]" in str(result)

    def test_live_all_created(self, high_record, medium_record, low_record, routing_engine):
        session = _make_mock_session(
            list_records_response={"records": []},
            create_response={"id": "recNEW", "fields": {}},
        )
        client = _make_client(session)
        result = sync_to_airtable(
            [high_record, medium_record, low_record],
            client=client,
            routing_engine=routing_engine,
            dry_run=False,
        )
        assert result.created == 3
        assert result.updated == 0
        assert result.errors == 0

    def test_live_all_updated(self, high_record, medium_record, routing_engine):
        session = _make_mock_session(
            list_records_response={"records": [{"id": "recEXIST", "fields": {}}]},
            update_response={"id": "recEXIST", "fields": {}},
        )
        client = _make_client(session)
        result = sync_to_airtable(
            [high_record, medium_record],
            client=client,
            routing_engine=routing_engine,
            dry_run=False,
        )
        assert result.updated == 2
        assert result.created == 0
        assert result.errors == 0

    def test_http_error_counted_as_error(self, high_record, routing_engine):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = requests.HTTPError("500")
        session.get.return_value = bad_resp

        client = _make_client(session)
        result = sync_to_airtable(
            [high_record],
            client=client,
            routing_engine=routing_engine,
            dry_run=False,
        )
        assert result.errors == 1
        assert result.created == 0

    def test_unexpected_error_counted(self, high_record, routing_engine):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = RuntimeError("boom")
        session.get.return_value = bad_resp

        client = _make_client(session)
        result = sync_to_airtable(
            [high_record],
            client=client,
            routing_engine=routing_engine,
            dry_run=False,
        )
        assert result.errors == 1

    def test_record_without_target_table_skipped(self, routing_engine):
        # Force target_table to stay empty by not routing
        rec = AirtableRecord(source_id="x", title="X", url="u", score=8.0)
        rec.target_table = ""  # explicitly no table
        session = _make_mock_session()
        client = _make_client(session)

        # Use a routing engine that we override to NOT set target_table
        class NoOpEngine(RoutingEngine):
            def route_all(self, records):
                return records  # don't set target_table

        engine = NoOpEngine("A", "B", "C", 7.0, 4.0)
        result = sync_to_airtable([rec], client=client, routing_engine=engine, dry_run=False)
        assert result.skipped == 1
        assert result.created == 0

    def test_empty_records_list(self, routing_engine):
        result = sync_to_airtable([], routing_engine=routing_engine, dry_run=True)
        assert result.created == 0
        assert result.errors == 0

    def test_routing_applied_before_sync(self, high_record, routing_engine):
        """target_table should be set by sync_to_airtable even if initially empty."""
        assert high_record.target_table == ""
        session = _make_mock_session(
            list_records_response={"records": []},
            create_response={"id": "recNEW", "fields": {}},
        )
        client = _make_client(session)
        sync_to_airtable([high_record], client=client, routing_engine=routing_engine, dry_run=False)
        assert high_record.target_table == "High Fit"

    def test_mixed_create_update_error(self, routing_engine):
        """3 records: 1 create, 1 update, 1 error."""
        rec_create = AirtableRecord(source_id="c", title="C", url="u", score=8.0)
        rec_update = AirtableRecord(source_id="u", title="U", url="u", score=5.0)
        rec_error  = AirtableRecord(source_id="e", title="E", url="u", score=2.0)

        call_count = {"n": 0}

        def dynamic_get(*args, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            # rec_create → no existing, rec_update → existing, rec_error → HTTP error
            if call_count["n"] == 1:
                resp.json.return_value = {"records": []}
            elif call_count["n"] == 2:
                resp.json.return_value = {"records": [{"id": "recEXIST", "fields": {}}]}
            else:
                resp.raise_for_status.side_effect = requests.HTTPError("403")
            return resp

        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = dynamic_get

        create_resp = MagicMock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"id": "recNEW", "fields": {}}
        session.post.return_value = create_resp

        update_resp = MagicMock()
        update_resp.raise_for_status.return_value = None
        update_resp.json.return_value = {"id": "recEXIST", "fields": {}}
        session.patch.return_value = update_resp

        client = _make_client(session)
        result = sync_to_airtable(
            [rec_create, rec_update, rec_error],
            client=client,
            routing_engine=routing_engine,
            dry_run=False,
        )
        assert result.created == 1
        assert result.updated == 1
        assert result.errors == 1


# ===========================================================================
# _make_dry_run_client
# ===========================================================================

class TestDryRunClient:

    def test_no_real_http_calls(self, high_record):
        client = _make_dry_run_client("tok", "app")
        # These should not raise and not make real HTTP calls
        records = client.list_records("High Fit")
        assert records == []

        created = client.create_record("High Fit", high_record.to_airtable_fields())
        assert "id" in created

    def test_upsert_always_creates_in_dry_run(self, high_record):
        client = _make_dry_run_client("tok", "app")
        action, record_id = client.upsert_record("High Fit", high_record)
        assert action == "created"


# ===========================================================================
# Build helpers
# ===========================================================================

class TestBuildHelpers:

    def test_build_routing_engine_from_env_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            engine = build_routing_engine_from_env()
        assert engine.table_a == "High Fit"
        assert engine.table_b == "Medium Fit"
        assert engine.table_c == "Low Fit"
        assert engine.threshold_high == 7.0
        assert engine.threshold_medium == 4.0

    def test_build_routing_engine_from_env_custom(self):
        env = {
            "AIRTABLE_TABLE_A": "Tier A",
            "AIRTABLE_TABLE_B": "Tier B",
            "AIRTABLE_TABLE_C": "Tier C",
            "AIRTABLE_THRESHOLD_HIGH": "8.0",
            "AIRTABLE_THRESHOLD_MEDIUM": "5.0",
        }
        with patch.dict(os.environ, env):
            engine = build_routing_engine_from_env()
        assert engine.table_a == "Tier A"
        assert engine.threshold_high == 8.0
        assert engine.threshold_medium == 5.0

    def test_build_client_from_env_raises_without_token(self):
        with patch.dict(os.environ, {"AIRTABLE_TOKEN": "", "AIRTABLE_BASE_ID": "appXXX"}):
            with pytest.raises(ValueError):
                build_client_from_env()

    def test_build_client_from_env_raises_without_base_id(self):
        with patch.dict(os.environ, {"AIRTABLE_TOKEN": "tok_xxx", "AIRTABLE_BASE_ID": ""}):
            with pytest.raises(ValueError):
                build_client_from_env()


# ===========================================================================
# SyncResult
# ===========================================================================

class TestSyncResult:

    def test_str_live(self):
        r = SyncResult(created=2, updated=1, skipped=0, errors=0, dry_run=False)
        s = str(r)
        assert "created=2" in s
        assert "updated=1" in s
        assert "[DRY RUN]" not in s

    def test_str_dry_run(self):
        r = SyncResult(created=3, dry_run=True)
        assert "[DRY RUN]" in str(r)
        