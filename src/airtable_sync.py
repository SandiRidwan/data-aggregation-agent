"""
airtable_sync.py — Airtable sync with score-based routing + ghost row cleaner

Routing logic (default — update when client confirms):
  score >= 7.0  → TABLE_A (high fit)
  score 4.0–6.9 → TABLE_B (medium fit)
  score < 4.0   → TABLE_C (low fit)

Tables configured via env vars:
  AIRTABLE_TOKEN       — personal access token
  AIRTABLE_BASE_ID     — base ID from URL
  AIRTABLE_TABLE_A     — table name for high-fit records (default: "High Fit")
  AIRTABLE_TABLE_B     — table name for medium-fit records (default: "Medium Fit")
  AIRTABLE_TABLE_C     — table name for low-fit records (default: "Low Fit")
  DRY_RUN              — if "true", skip real API calls (log only)

Score thresholds configurable via env vars:
  AIRTABLE_THRESHOLD_HIGH   — minimum score for Table A (default: 7.0)
  AIRTABLE_THRESHOLD_MEDIUM — minimum score for Table B (default: 4.0)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import MagicMock

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AIRTABLE_API_BASE = "https://api.airtable.com/v0"
_DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def _get_threshold_high() -> float:
    return float(os.getenv("AIRTABLE_THRESHOLD_HIGH", "7.0"))


def _get_threshold_medium() -> float:
    return float(os.getenv("AIRTABLE_THRESHOLD_MEDIUM", "4.0"))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AirtableRecord:
    """Normalised record ready to be synced to Airtable."""

    # Required
    source_id: str          # unique ID from scraper (used for dedup)
    title: str
    url: str
    score: float

    # Optional enrichment
    summary: str = ""
    reasoning: str = ""
    source_name: str = ""
    scraped_at: str = ""
    extra: dict = field(default_factory=dict)

    # Populated after routing
    target_table: str = ""

    def to_airtable_fields(self) -> dict[str, Any]:
        """Convert to Airtable fields dict."""
        fields: dict[str, Any] = {
            "Source ID": self.source_id,
            "Title": self.title,
            "URL": self.url,
            "Score": round(self.score, 2),
        }
        if self.summary:
            fields["Summary"] = self.summary
        if self.reasoning:
            fields["Reasoning"] = self.reasoning
        if self.source_name:
            fields["Source"] = self.source_name
        if self.scraped_at:
            fields["Scraped At"] = self.scraped_at
        if self.extra:
            import json
            fields["Extra"] = json.dumps(self.extra)
        return fields


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class RoutingEngine:
    """Routes a record to the correct Airtable table based on score."""

    def __init__(
        self,
        table_a: str,
        table_b: str,
        table_c: str,
        threshold_high: float,
        threshold_medium: float,
    ):
        self.table_a = table_a
        self.table_b = table_b
        self.table_c = table_c
        self.threshold_high = threshold_high
        self.threshold_medium = threshold_medium

    def route(self, record: AirtableRecord) -> str:
        """Return the target table name for the given record."""
        if record.score >= self.threshold_high:
            return self.table_a
        elif record.score >= self.threshold_medium:
            return self.table_b
        else:
            return self.table_c

    def route_all(self, records: list[AirtableRecord]) -> list[AirtableRecord]:
        """Assign target_table to every record. Returns same list."""
        for rec in records:
            rec.target_table = self.route(rec)
        return records


# ---------------------------------------------------------------------------
# Airtable HTTP client
# ---------------------------------------------------------------------------

class AirtableClient:
    """
    Thin wrapper around Airtable REST API.
    Supports list, create, update, delete (upsert by Source ID).
    """

    def __init__(self, token: str, base_id: str, session: Optional[requests.Session] = None):
        if not token:
            raise ValueError("AIRTABLE_TOKEN is required")
        if not base_id:
            raise ValueError("AIRTABLE_BASE_ID is required")
        self.token = token
        self.base_id = base_id
        self._session = session or requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _table_url(self, table_name: str) -> str:
        import urllib.parse
        return f"{AIRTABLE_API_BASE}/{self.base_id}/{urllib.parse.quote(table_name)}"

    def list_records(self, table_name: str, filter_formula: str = "") -> list[dict]:
        """Fetch all records from a table (handles pagination)."""
        url    = self._table_url(table_name)
        params: dict[str, str] = {}
        if filter_formula:
            params["filterByFormula"] = filter_formula

        records: list[dict] = []
        offset: Optional[str] = None

        while True:
            if offset:
                params["offset"] = offset
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break

        logger.debug("list_records(%s): fetched %d records", table_name, len(records))
        return records

    def create_record(self, table_name: str, fields: dict) -> dict:
        """Create a single record. Returns created record dict."""
        url  = self._table_url(table_name)
        resp = self._session.post(url, json={"fields": fields}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def update_record(self, table_name: str, record_id: str, fields: dict) -> dict:
        """Update a single record by Airtable record ID."""
        url  = f"{self._table_url(table_name)}/{record_id}"
        resp = self._session.patch(url, json={"fields": fields}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def delete_record(self, table_name: str, record_id: str) -> bool:
        """Delete a single record by Airtable record ID. Returns True on success."""
        url  = f"{self._table_url(table_name)}/{record_id}"
        resp = self._session.delete(url, timeout=15)
        resp.raise_for_status()
        return resp.json().get("deleted", False)

    def upsert_record(self, table_name: str, record: AirtableRecord) -> tuple[str, str]:
        """
        Upsert a record by Source ID.
        Returns (action, airtable_record_id) where action is 'created' or 'updated'.
        """
        formula  = f"{{Source ID}}='{record.source_id}'"
        existing = self.list_records(table_name, filter_formula=formula)
        fields   = record.to_airtable_fields()

        if existing:
            airtable_id = existing[0]["id"]
            self.update_record(table_name, airtable_id, fields)
            logger.debug("upsert: updated %s in %s", record.source_id, table_name)
            return "updated", airtable_id
        else:
            created     = self.create_record(table_name, fields)
            airtable_id = created["id"]
            logger.debug("upsert: created %s in %s", record.source_id, table_name)
            return "created", airtable_id


# ---------------------------------------------------------------------------
# Sync result
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors:  int = 0
    dry_run: bool = False

    def __str__(self) -> str:
        mode = " [DRY RUN]" if self.dry_run else ""
        return (
            f"SyncResult{mode}: "
            f"created={self.created} updated={self.updated} "
            f"skipped={self.skipped} errors={self.errors}"
        )


# ---------------------------------------------------------------------------
# Main sync function
# ---------------------------------------------------------------------------

def sync_to_airtable(
    records: list[AirtableRecord],
    client: Optional[AirtableClient] = None,
    routing_engine: Optional[RoutingEngine] = None,
    dry_run: Optional[bool] = None,
) -> SyncResult:
    """
    Route and sync a list of AirtableRecords.

    Args:
        records: List of records to sync (score must be set).
        client: AirtableClient instance. If None, built from env vars.
        routing_engine: RoutingEngine instance. If None, built from env vars.
        dry_run: Override DRY_RUN env var. If None, uses env var.

    Returns:
        SyncResult with counts.
    """
    is_dry_run = dry_run if dry_run is not None else _DRY_RUN

    if client is None:
        token   = os.getenv("AIRTABLE_TOKEN", "")
        base_id = os.getenv("AIRTABLE_BASE_ID", "")
        if is_dry_run:
            client = _make_dry_run_client(token or "dry_run", base_id or "dry_run")
        else:
            client = AirtableClient(token=token, base_id=base_id)

    if routing_engine is None:
        routing_engine = RoutingEngine(
            table_a=os.getenv("AIRTABLE_TABLE_A", "High Fit"),
            table_b=os.getenv("AIRTABLE_TABLE_B", "Medium Fit"),
            table_c=os.getenv("AIRTABLE_TABLE_C", "Low Fit"),
            threshold_high=_get_threshold_high(),
            threshold_medium=_get_threshold_medium(),
        )

    routing_engine.route_all(records)
    result = SyncResult(dry_run=is_dry_run)

    for rec in records:
        if not rec.target_table:
            logger.warning("Record %s has no target_table — skipped", rec.source_id)
            result.skipped += 1
            continue

        if is_dry_run:
            logger.info(
                "[DRY RUN] Would upsert '%s' (score=%.2f) → %s",
                rec.title, rec.score, rec.target_table,
            )
            result.created += 1
            continue

        try:
            action, _ = client.upsert_record(rec.target_table, rec)
            if action == "created":
                result.created += 1
            else:
                result.updated += 1
        except requests.HTTPError as exc:
            logger.error("HTTP error syncing %s to %s: %s", rec.source_id, rec.target_table, exc)
            result.errors += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error syncing %s: %s", rec.source_id, exc)
            result.errors += 1

    logger.info("Sync complete: %s", result)
    return result


# ---------------------------------------------------------------------------
# Ghost row cleaner
# ---------------------------------------------------------------------------

def clean_empty_rows(
    client: Optional[AirtableClient] = None,
    table_names: Optional[list[str]] = None,
    dry_run: Optional[bool] = None,
) -> dict[str, int]:
    """
    Delete empty rows (rows without Source ID) from all Airtable tables.

    Ghost rows appear because:
    - Airtable creates 3 empty default rows on every new table
    - Previous pipeline runs that had scoring failures left rows with no Source ID

    Args:
        client: AirtableClient instance. If None, built from env vars.
        table_names: Tables to clean. If None, uses all 3 tables from env vars.
        dry_run: Override DRY_RUN env var.

    Returns:
        Dict mapping table_name → number of rows deleted.
    """
    is_dry_run = dry_run if dry_run is not None else (
        os.getenv("DRY_RUN", "false").lower() == "true"
    )

    if client is None:
        token   = os.getenv("AIRTABLE_TOKEN", "")
        base_id = os.getenv("AIRTABLE_BASE_ID", "")
        client  = AirtableClient(token=token, base_id=base_id)

    if table_names is None:
        table_names = [
            os.getenv("AIRTABLE_TABLE_A", "High Fit"),
            os.getenv("AIRTABLE_TABLE_B", "Medium Fit"),
            os.getenv("AIRTABLE_TABLE_C", "Low Fit"),
        ]

    results: dict[str, int] = {}

    for table_name in table_names:
        try:
            all_records = client.list_records(table_name)

            # Empty row = row with no Source ID value
            empty_records = [
                rec for rec in all_records
                if not str(rec.get("fields", {}).get("Source ID", "")).strip()
            ]

            if not empty_records:
                logger.info("✅ %s: no empty rows to clean", table_name)
                results[table_name] = 0
                continue

            if is_dry_run:
                logger.info(
                    "[DRY RUN] Would delete %d empty rows from %s",
                    len(empty_records), table_name,
                )
                results[table_name] = len(empty_records)
                continue

            deleted = 0
            for rec in empty_records:
                try:
                    client.delete_record(table_name, rec["id"])
                    deleted += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to delete row %s from %s: %s",
                        rec.get("id"), table_name, exc,
                    )

            logger.info("🧹 %s: deleted %d empty rows", table_name, deleted)
            results[table_name] = deleted

        except Exception as exc:
            logger.error("Error cleaning table %s: %s", table_name, exc)
            results[table_name] = 0

    total = sum(results.values())
    if total > 0:
        logger.info("🧹 Airtable clean complete: %d total empty rows removed", total)
    else:
        logger.info("✅ Airtable clean complete: all tables already clean")

    return results


# ---------------------------------------------------------------------------
# Dry run helper
# ---------------------------------------------------------------------------

def _make_dry_run_client(token: str, base_id: str) -> AirtableClient:
    """Returns a real AirtableClient whose HTTP session is mocked."""
    mock_session = MagicMock(spec=requests.Session)
    mock_session.headers = {}

    mock_response = MagicMock()
    mock_response.json.return_value = {"records": []}
    mock_response.raise_for_status.return_value = None
    mock_session.get.return_value = mock_response

    create_response = MagicMock()
    create_response.json.return_value = {"id": "rec_dry_run_000", "fields": {}}
    create_response.raise_for_status.return_value = None
    mock_session.post.return_value = create_response

    return AirtableClient(token=token, base_id=base_id, session=mock_session)


# ---------------------------------------------------------------------------
# Convenience: build from env
# ---------------------------------------------------------------------------

def build_client_from_env() -> AirtableClient:
    return AirtableClient(
        token=os.getenv("AIRTABLE_TOKEN", ""),
        base_id=os.getenv("AIRTABLE_BASE_ID", ""),
    )


def build_routing_engine_from_env() -> RoutingEngine:
    return RoutingEngine(
        table_a=os.getenv("AIRTABLE_TABLE_A", "High Fit"),
        table_b=os.getenv("AIRTABLE_TABLE_B", "Medium Fit"),
        table_c=os.getenv("AIRTABLE_TABLE_C", "Low Fit"),
        threshold_high=_get_threshold_high(),
        threshold_medium=_get_threshold_medium(),
    )