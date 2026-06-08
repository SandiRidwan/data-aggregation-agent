"""
digest.py — Daily email digest via SendGrid API (primary) + SMTP (fallback)

Railway blocks ALL outbound SMTP ports (587 AND 465) at network level.
Fix: Use SendGrid HTTP API (port 443 — never blocked) as primary sender.
SMTP kept as fallback for local dev only.

Priority order:
  1. SendGrid API — if SENDGRID_API_KEY is set (Railway-compatible, port 443)
  2. SMTP fallback — if SENDGRID_API_KEY not set (local dev only)
  3. DRY_RUN — log only, no send

Environment variables:
  SENDGRID_API_KEY  — SendGrid API key (get free at sendgrid.com, 100/day free)
  SENDGRID_FROM     — verified sender email in SendGrid (must match verified sender)
  SMTP_HOST         — fallback only: e.g. smtp.gmail.com
  SMTP_PORT         — fallback only: 587 or 465
  SMTP_USER         — fallback only: sender email
  SMTP_PASSWORD     — fallback only: app password
  DIGEST_FROM       — override sender display name (optional)
  DIGEST_TO         — comma-separated recipient addresses
  DIGEST_SUBJECT    — override default subject (optional)
  DRY_RUN           — if "true", build email but do not send (log only)
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

_DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DigestRecord:
    """A single record to include in the digest."""
    title: str
    url: str
    score: float
    source_name: str = ""
    summary: str = ""
    reasoning: str = ""
    tier: str = ""          # "High Fit" / "Medium Fit" / "Low Fit"


@dataclass
class DigestPayload:
    """Everything needed to render and send one digest email."""
    records: list[DigestRecord] = field(default_factory=list)
    generated_at: str = ""          # ISO timestamp — auto-filled if empty
    run_label: str = "Daily Digest" # appears in subject + header

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )

    # Convenience groupings
    def high(self) -> list[DigestRecord]:
        return [r for r in self.records if r.tier == "High Fit"]

    def medium(self) -> list[DigestRecord]:
        return [r for r in self.records if r.tier == "Medium Fit"]

    def low(self) -> list[DigestRecord]:
        return [r for r in self.records if r.tier == "Low Fit"]

    def untiered(self) -> list[DigestRecord]:
        return [r for r in self.records if r.tier not in ("High Fit", "Medium Fit", "Low Fit")]


# ---------------------------------------------------------------------------
# SMTP config (fallback — local dev only)
# ---------------------------------------------------------------------------

@dataclass
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    use_ssl: bool = False       # True → SSL (port 465); False → STARTTLS (587)

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        host = os.getenv("SMTP_HOST", "")
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        password = os.getenv("SMTP_PASSWORD", "")
        use_ssl = port == 465
        if not host:
            raise ValueError("SMTP_HOST is required")
        if not user:
            raise ValueError("SMTP_USER is required")
        if not password:
            raise ValueError("SMTP_PASSWORD is required")
        return cls(host=host, port=port, user=user, password=password, use_ssl=use_ssl)


# ---------------------------------------------------------------------------
# Email builder (HTML + plaintext — shared by both senders)
# ---------------------------------------------------------------------------

def _tier_color(tier: str) -> str:
    return {"High Fit": "#16a34a", "Medium Fit": "#d97706", "Low Fit": "#dc2626"}.get(tier, "#6b7280")


def _tier_badge(tier: str) -> str:
    color = _tier_color(tier)
    label = tier or "Untiered"
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:4px;font-size:12px;font-weight:bold;">{label}</span>'
    )


def _record_html(rec: DigestRecord) -> str:
    summary_html = f"<p style='margin:4px 0;color:#374151;'>{rec.summary}</p>" if rec.summary else ""
    reasoning_html = (
        f"<p style='margin:4px 0;color:#6b7280;font-style:italic;font-size:13px;'>"
        f"Reasoning: {rec.reasoning}</p>"
    ) if rec.reasoning else ""
    source_html = (
        f"<span style='color:#9ca3af;font-size:12px;'>via {rec.source_name}</span> &nbsp;"
    ) if rec.source_name else ""
    return (
        f"<div style='border:1px solid #e5e7eb;border-radius:6px;padding:12px 16px;"
        f"margin-bottom:12px;background:#fff;'>"
        f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:6px;'>"
        f"<a href='{rec.url}' style='font-weight:600;color:#1d4ed8;text-decoration:none;"
        f"font-size:15px;'>{rec.title}</a>"
        f"</div>"
        f"<div style='margin-bottom:6px;'>{_tier_badge(rec.tier)} &nbsp;"
        f"<span style='color:#374151;font-size:13px;font-weight:600;'>Score: {rec.score:.1f}</span>"
        f" &nbsp; {source_html}</div>"
        f"{summary_html}{reasoning_html}"
        f"</div>"
    )


def _section_html(tier: str, records: list[DigestRecord]) -> str:
    if not records:
        return ""
    color = _tier_color(tier)
    items = "".join(_record_html(r) for r in records)
    return (
        f"<h2 style='color:{color};border-bottom:2px solid {color};"
        f"padding-bottom:4px;margin-top:24px;'>"
        f"{tier} ({len(records)})</h2>"
        f"{items}"
    )


def build_html(payload: DigestPayload) -> str:
    total = len(payload.records)
    sections = (
        _section_html("High Fit", payload.high())
        + _section_html("Medium Fit", payload.medium())
        + _section_html("Low Fit", payload.low())
    )
    if not sections:
        sections = "<p style='color:#6b7280;'>No records found in this run.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{payload.run_label}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#f9fafb;margin:0;padding:24px;">
<div style="max-width:680px;margin:0 auto;background:#f9fafb;">
  <div style="background:#1e40af;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h1 style="margin:0;font-size:20px;">{payload.run_label}</h1>
    <p style="margin:4px 0 0;opacity:.8;font-size:13px;">
      Generated {payload.generated_at} &middot; {total} record{'s' if total != 1 else ''}
    </p>
  </div>
  <div style="background:#fff;padding:20px 24px;border-radius:0 0 8px 8px;
    border:1px solid #e5e7eb;border-top:none;">
    {sections}
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 12px;">
    <p style="color:#9ca3af;font-size:12px;margin:0;">
      Data Aggregation Agent &middot; {payload.generated_at}
    </p>
  </div>
</div>
</body></html>"""


def build_plaintext(payload: DigestPayload) -> str:
    lines = [
        f"{payload.run_label}",
        f"Generated: {payload.generated_at}",
        f"Total records: {len(payload.records)}",
        "",
    ]
    for tier, records in [
        ("High Fit", payload.high()),
        ("Medium Fit", payload.medium()),
        ("Low Fit", payload.low()),
    ]:
        if not records:
            continue
        lines.append(f"=== {tier.upper()} ({len(records)}) ===")
        for rec in records:
            lines.append(f"  [{rec.score:.1f}] {rec.title}")
            lines.append(f"  {rec.url}")
            if rec.summary:
                lines.append(f"  {rec.summary}")
            if rec.source_name:
                lines.append(f"  via {rec.source_name}")
            lines.append("")
    if not any([payload.high(), payload.medium(), payload.low()]):
        lines.append("No records found in this run.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email assembly (used by SMTP fallback)
# ---------------------------------------------------------------------------

def build_email(
    payload: DigestPayload,
    smtp_config: SMTPConfig,
    recipients: list[str],
    subject: Optional[str] = None,
) -> MIMEMultipart:
    """Assemble a MIMEMultipart email (html + plain text fallback)."""
    if not recipients:
        raise ValueError("At least one recipient is required")

    subject = subject or os.getenv(
        "DIGEST_SUBJECT",
        f"{payload.run_label} — {payload.generated_at} ({len(payload.records)} records)",
    )
    from_name = os.getenv("DIGEST_FROM", smtp_config.user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_name
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(build_plaintext(payload), "plain", "utf-8"))
    msg.attach(MIMEText(build_html(payload), "html", "utf-8"))
    return msg


# ---------------------------------------------------------------------------
# SendGrid sender — PRIMARY (Railway-compatible, port 443)
# ---------------------------------------------------------------------------

def _send_via_sendgrid(
    payload: DigestPayload,
    recipients: list[str],
    subject: str,
) -> bool:
    """
    Send digest via SendGrid HTTP API (port 443).
    Railway does NOT block port 443.
    Requires: SENDGRID_API_KEY + SENDGRID_FROM env vars.
    """
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Content
    except ImportError:
        raise ImportError(
            "sendgrid package not installed. "
            "Add 'sendgrid==6.11.0' to requirements.txt and redeploy."
        )

    api_key  = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("SENDGRID_FROM", os.getenv("SMTP_USER", ""))

    if not api_key:
        raise ValueError("SENDGRID_API_KEY is required")
    if not from_email:
        raise ValueError(
            "SENDGRID_FROM (or SMTP_USER) must be set to your verified SendGrid sender email"
        )

    message = Mail(
        from_email=from_email,
        to_emails=recipients,
        subject=subject,
    )
    message.add_content(Content("text/plain", build_plaintext(payload)))
    message.add_content(Content("text/html",  build_html(payload)))

    sg       = sendgrid.SendGridAPIClient(api_key=api_key)
    response = sg.send(message)

    if response.status_code in (200, 202):
        logger.info(
            "✅ Digest sent via SendGrid → %s (%d recipients) — HTTP %d",
            recipients, len(recipients), response.status_code,
        )
        return True
    else:
        logger.error(
            "SendGrid returned HTTP %d: %s",
            response.status_code, response.body,
        )
        return False


# ---------------------------------------------------------------------------
# SMTP sender — FALLBACK (local dev only — blocked on Railway)
# ---------------------------------------------------------------------------

def _send_via_smtp(
    payload: DigestPayload,
    recipients: list[str],
    subject: str,
    smtp_config: SMTPConfig,
) -> bool:
    """
    Send digest via SMTP.
    ⚠️  Railway blocks ALL SMTP ports (587 and 465).
    Only use for local development.
    """
    msg     = build_email(payload, smtp_config, recipients, subject)
    context = ssl.create_default_context()

    if smtp_config.use_ssl:
        with smtplib.SMTP_SSL(smtp_config.host, smtp_config.port, context=context) as server:
            server.login(smtp_config.user, smtp_config.password)
            server.sendmail(smtp_config.user, recipients, msg.as_string())
    else:
        with smtplib.SMTP(smtp_config.host, smtp_config.port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_config.user, smtp_config.password)
            server.sendmail(smtp_config.user, recipients, msg.as_string())

    logger.info("✅ Digest sent via SMTP → %s (%d recipients)", recipients, len(recipients))
    return True


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def send_digest(
    payload: DigestPayload,
    smtp_config: Optional[SMTPConfig] = None,
    recipients: Optional[list[str]] = None,
    subject: Optional[str] = None,
    dry_run: Optional[bool] = None,
) -> bool:
    """
    Build and send (or log in dry-run) a digest email.

    Sender priority:
      1. SendGrid API  — if SENDGRID_API_KEY is set (recommended, Railway-safe)
      2. SMTP fallback — if SENDGRID_API_KEY not set (local dev only)

    Returns True if sent/logged successfully, False on error.
    """
    is_dry_run = dry_run if dry_run is not None else _DRY_RUN

    # Resolve recipients
    if recipients is None:
        raw        = os.getenv("DIGEST_TO", "")
        recipients = [r.strip() for r in raw.split(",") if r.strip()]
    if not recipients:
        raise ValueError("DIGEST_TO env var or recipients argument is required")

    # Build subject
    resolved_subject = subject or os.getenv(
        "DIGEST_SUBJECT",
        f"{payload.run_label} — {payload.generated_at} ({len(payload.records)} records)",
    )

    # ── DRY RUN — log only ────────────────────────────────────────────────
    if is_dry_run:
        logger.info(
            "[DRY RUN] Would send digest to %s — subject: %s — %d records",
            recipients, resolved_subject, len(payload.records),
        )
        logger.info("[DRY RUN] HTML preview length: %d chars", len(build_html(payload)))
        return True

    # ── SENDGRID (primary) ────────────────────────────────────────────────
    sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
    if sendgrid_key:
        try:
            return _send_via_sendgrid(payload, recipients, resolved_subject)
        except ImportError as exc:
            logger.error("SendGrid import error: %s", exc)
            return False
        except Exception as exc:
            logger.error("SendGrid failed: %s — will NOT fall back to SMTP on Railway", exc)
            return False

    # ── SMTP FALLBACK (local dev only) ────────────────────────────────────
    logger.warning(
        "⚠️  SENDGRID_API_KEY not set — using SMTP fallback. "
        "NOTE: Railway blocks all SMTP ports. "
        "Set SENDGRID_API_KEY in Railway Variables to fix this."
    )
    try:
        if smtp_config is None:
            if is_dry_run:
                smtp_config = SMTPConfig(
                    host="localhost", port=587,
                    user="dry@run.local", password="dry_run",
                )
            else:
                smtp_config = SMTPConfig.from_env()
        return _send_via_smtp(payload, recipients, resolved_subject, smtp_config)
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed: %s", exc)
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP error: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error sending digest: %s", exc)
        return False