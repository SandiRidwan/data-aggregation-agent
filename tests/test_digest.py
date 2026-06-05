"""
tests/test_digest.py — Test suite for digest.py

Coverage:
  - DigestRecord: dataclass defaults
  - DigestPayload: tier grouping, auto timestamp, untiered
  - build_html: structure, tier sections, empty, escaping
  - build_plaintext: structure, tier sections, empty
  - build_email: subject, from, to, mime parts
  - SMTPConfig: from_env, missing fields, SSL detection
  - send_digest: dry run, live (mocked SMTP), auth error, smtp error,
                 missing recipients, env-based recipients
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from unittest.mock import MagicMock, patch, call

import pytest

from src.digest import (
    DigestPayload,
    DigestRecord,
    SMTPConfig,
    build_email,
    build_html,
    build_plaintext,
    send_digest,
    send_email,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def high_rec():
    return DigestRecord(
        title="Senior Python Engineer",
        url="https://example.com/job/1",
        score=8.5,
        source_name="LinkedIn",
        summary="Perfect match",
        reasoning="All requirements met",
        tier="High Fit",
    )


@pytest.fixture
def medium_rec():
    return DigestRecord(
        title="Python Developer",
        url="https://example.com/job/2",
        score=5.5,
        tier="Medium Fit",
    )


@pytest.fixture
def low_rec():
    return DigestRecord(
        title="Junior Dev",
        url="https://example.com/job/3",
        score=2.0,
        tier="Low Fit",
    )


@pytest.fixture
def full_payload(high_rec, medium_rec, low_rec):
    return DigestPayload(
        records=[high_rec, medium_rec, low_rec],
        run_label="Test Digest",
        generated_at="2026-06-05 08:00 UTC",
    )


@pytest.fixture
def empty_payload():
    return DigestPayload(records=[], run_label="Empty Run", generated_at="2026-06-05 08:00 UTC")


@pytest.fixture
def smtp_cfg():
    return SMTPConfig(host="smtp.gmail.com", port=587, user="test@gmail.com", password="apppass")


@pytest.fixture
def recipients():
    return ["client@example.com"]


# ===========================================================================
# DigestRecord
# ===========================================================================

class TestDigestRecord:

    def test_defaults(self):
        rec = DigestRecord(title="X", url="https://x.com", score=5.0)
        assert rec.source_name == ""
        assert rec.summary == ""
        assert rec.reasoning == ""
        assert rec.tier == ""

    def test_full_fields(self, high_rec):
        assert high_rec.title == "Senior Python Engineer"
        assert high_rec.score == 8.5
        assert high_rec.tier == "High Fit"


# ===========================================================================
# DigestPayload
# ===========================================================================

class TestDigestPayload:

    def test_high_filter(self, full_payload, high_rec):
        assert full_payload.high() == [high_rec]

    def test_medium_filter(self, full_payload, medium_rec):
        assert full_payload.medium() == [medium_rec]

    def test_low_filter(self, full_payload, low_rec):
        assert full_payload.low() == [low_rec]

    def test_untiered_empty_when_all_tiered(self, full_payload):
        assert full_payload.untiered() == []

    def test_untiered_catches_blank_tier(self):
        rec = DigestRecord(title="X", url="u", score=5.0, tier="")
        payload = DigestPayload(records=[rec])
        assert payload.untiered() == [rec]

    def test_auto_generated_at(self):
        payload = DigestPayload(records=[])
        assert "UTC" in payload.generated_at

    def test_explicit_generated_at(self):
        payload = DigestPayload(records=[], generated_at="2026-06-05 08:00 UTC")
        assert payload.generated_at == "2026-06-05 08:00 UTC"

    def test_empty_records(self, empty_payload):
        assert empty_payload.high() == []
        assert empty_payload.medium() == []
        assert empty_payload.low() == []


# ===========================================================================
# build_html
# ===========================================================================

class TestBuildHtml:

    def test_contains_run_label(self, full_payload):
        html = build_html(full_payload)
        assert "Test Digest" in html

    def test_contains_generated_at(self, full_payload):
        html = build_html(full_payload)
        assert "2026-06-05 08:00 UTC" in html

    def test_contains_record_title(self, full_payload):
        html = build_html(full_payload)
        assert "Senior Python Engineer" in html

    def test_contains_record_url(self, full_payload):
        html = build_html(full_payload)
        assert "https://example.com/job/1" in html

    def test_contains_score(self, full_payload):
        html = build_html(full_payload)
        assert "8.5" in html

    def test_contains_all_tier_headers(self, full_payload):
        html = build_html(full_payload)
        assert "High Fit" in html
        assert "Medium Fit" in html
        assert "Low Fit" in html

    def test_contains_summary(self, full_payload):
        html = build_html(full_payload)
        assert "Perfect match" in html

    def test_contains_reasoning(self, full_payload):
        html = build_html(full_payload)
        assert "All requirements met" in html

    def test_contains_source_name(self, full_payload):
        html = build_html(full_payload)
        assert "LinkedIn" in html

    def test_empty_payload_shows_no_records_message(self, empty_payload):
        html = build_html(empty_payload)
        assert "No records found" in html

    def test_missing_summary_no_empty_tag(self, medium_rec):
        payload = DigestPayload(records=[medium_rec], generated_at="2026-06-05")
        html = build_html(payload)
        # summary paragraph should not appear
        assert "Perfect match" not in html

    def test_record_count_in_header(self, full_payload):
        html = build_html(full_payload)
        assert "3 records" in html

    def test_single_record_no_plural(self, high_rec):
        payload = DigestPayload(records=[high_rec], generated_at="2026-06-05")
        html = build_html(payload)
        assert "1 record" in html
        assert "1 records" not in html

    def test_is_valid_html_structure(self, full_payload):
        html = build_html(full_payload)
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "<body" in html

    def test_tier_section_count(self, full_payload):
        html = build_html(full_payload)
        # High Fit (1) + Medium Fit (1) + Low Fit (1)
        assert html.count("https://example.com/job/") == 3


# ===========================================================================
# build_plaintext
# ===========================================================================

class TestBuildPlaintext:

    def test_contains_run_label(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "Test Digest" in txt

    def test_contains_generated_at(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "2026-06-05 08:00 UTC" in txt

    def test_contains_total_records(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "Total records: 3" in txt

    def test_contains_tier_headers(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "HIGH FIT" in txt
        assert "MEDIUM FIT" in txt
        assert "LOW FIT" in txt

    def test_contains_record_title(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "Senior Python Engineer" in txt

    def test_contains_url(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "https://example.com/job/1" in txt

    def test_contains_score(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "8.5" in txt

    def test_contains_summary(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "Perfect match" in txt

    def test_contains_source_name(self, full_payload):
        txt = build_plaintext(full_payload)
        assert "via LinkedIn" in txt

    def test_empty_shows_no_records(self, empty_payload):
        txt = build_plaintext(empty_payload)
        assert "No records found" in txt

    def test_empty_no_tier_sections(self, empty_payload):
        txt = build_plaintext(empty_payload)
        assert "HIGH FIT" not in txt


# ===========================================================================
# SMTPConfig
# ===========================================================================

class TestSMTPConfig:

    def test_from_env_success(self):
        env = {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "test@gmail.com",
            "SMTP_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env):
            cfg = SMTPConfig.from_env()
        assert cfg.host == "smtp.gmail.com"
        assert cfg.port == 587
        assert cfg.user == "test@gmail.com"
        assert cfg.use_ssl is False

    def test_from_env_ssl_port_465(self):
        env = {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "test@gmail.com",
            "SMTP_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env):
            cfg = SMTPConfig.from_env()
        assert cfg.use_ssl is True

    def test_from_env_missing_host(self):
        with patch.dict(os.environ, {"SMTP_HOST": "", "SMTP_USER": "x", "SMTP_PASSWORD": "y"}):
            with pytest.raises(ValueError, match="SMTP_HOST"):
                SMTPConfig.from_env()

    def test_from_env_missing_user(self):
        with patch.dict(os.environ, {"SMTP_HOST": "h", "SMTP_USER": "", "SMTP_PASSWORD": "y"}):
            with pytest.raises(ValueError, match="SMTP_USER"):
                SMTPConfig.from_env()

    def test_from_env_missing_password(self):
        with patch.dict(os.environ, {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": ""}):
            with pytest.raises(ValueError, match="SMTP_PASSWORD"):
                SMTPConfig.from_env()


# ===========================================================================
# build_email
# ===========================================================================

class TestBuildEmail:

    def test_subject_default(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients)
        assert "Test Digest" in msg["Subject"]
        assert "3 records" in msg["Subject"]

    def test_subject_override(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients, subject="Custom Subject")
        assert msg["Subject"] == "Custom Subject"

    def test_from_field(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients)
        assert smtp_cfg.user in msg["From"]

    def test_to_field_single(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients)
        assert "client@example.com" in msg["To"]

    def test_to_field_multiple(self, full_payload, smtp_cfg):
        msg = build_email(full_payload, smtp_cfg, ["a@x.com", "b@x.com"])
        assert "a@x.com" in msg["To"]
        assert "b@x.com" in msg["To"]

    def test_has_two_mime_parts(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients)
        parts = msg.get_payload()
        assert len(parts) == 2

    def test_first_part_is_plaintext(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients)
        assert msg.get_payload()[0].get_content_type() == "text/plain"

    def test_second_part_is_html(self, full_payload, smtp_cfg, recipients):
        msg = build_email(full_payload, smtp_cfg, recipients)
        assert msg.get_payload()[1].get_content_type() == "text/html"

    def test_no_recipients_raises(self, full_payload, smtp_cfg):
        with pytest.raises(ValueError, match="recipient"):
            build_email(full_payload, smtp_cfg, [])


# ===========================================================================
# send_digest — dry run
# ===========================================================================

class TestSendDigestDryRun:

    def test_dry_run_returns_true(self, full_payload, recipients):
        result = send_digest(full_payload, recipients=recipients, dry_run=True)
        assert result is True

    def test_dry_run_no_smtp_calls(self, full_payload, recipients):
        with patch("src.digest.send_email") as mock_send:
            send_digest(full_payload, recipients=recipients, dry_run=True)
            mock_send.assert_not_called()

    def test_dry_run_empty_payload(self, empty_payload, recipients):
        result = send_digest(empty_payload, recipients=recipients, dry_run=True)
        assert result is True

    def test_dry_run_no_recipients_raises(self, full_payload):
        with patch.dict(os.environ, {"DIGEST_TO": ""}):
            with pytest.raises(ValueError, match="DIGEST_TO"):
                send_digest(full_payload, recipients=[], dry_run=True)

    def test_dry_run_recipients_from_env(self, full_payload):
        with patch.dict(os.environ, {"DIGEST_TO": "a@x.com, b@x.com"}):
            result = send_digest(full_payload, dry_run=True)
        assert result is True


# ===========================================================================
# send_digest — live (mocked SMTP)
# ===========================================================================

class TestSendDigestLive:

    def _mock_smtp(self):
        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        return mock

    def test_sends_via_starttls(self, full_payload, smtp_cfg, recipients):
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            result = send_digest(full_payload, smtp_config=smtp_cfg, recipients=recipients, dry_run=False)
        assert result is True
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with(smtp_cfg.user, smtp_cfg.password)
        mock_smtp.sendmail.assert_called_once()

    def test_sends_via_ssl(self, full_payload, recipients):
        cfg = SMTPConfig(host="smtp.gmail.com", port=465, user="u@x.com", password="p", use_ssl=True)
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP_SSL", return_value=mock_smtp):
            result = send_digest(full_payload, smtp_config=cfg, recipients=recipients, dry_run=False)
        assert result is True
        mock_smtp.login.assert_called_once()
        mock_smtp.sendmail.assert_called_once()

    def test_auth_error_returns_false(self, full_payload, smtp_cfg, recipients):
        mock_smtp = self._mock_smtp()
        mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        with patch("smtplib.SMTP", return_value=mock_smtp):
            result = send_digest(full_payload, smtp_config=smtp_cfg, recipients=recipients, dry_run=False)
        assert result is False

    def test_smtp_exception_returns_false(self, full_payload, smtp_cfg, recipients):
        mock_smtp = self._mock_smtp()
        mock_smtp.sendmail.side_effect = smtplib.SMTPException("connection reset")
        with patch("smtplib.SMTP", return_value=mock_smtp):
            result = send_digest(full_payload, smtp_config=smtp_cfg, recipients=recipients, dry_run=False)
        assert result is False

    def test_unexpected_exception_returns_false(self, full_payload, smtp_cfg, recipients):
        with patch("smtplib.SMTP", side_effect=RuntimeError("boom")):
            result = send_digest(full_payload, smtp_config=smtp_cfg, recipients=recipients, dry_run=False)
        assert result is False

    def test_sendmail_called_with_correct_recipients(self, full_payload, smtp_cfg):
        recips = ["a@x.com", "b@x.com"]
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_digest(full_payload, smtp_config=smtp_cfg, recipients=recips, dry_run=False)
        call_args = mock_smtp.sendmail.call_args
        assert call_args[0][1] == recips
        