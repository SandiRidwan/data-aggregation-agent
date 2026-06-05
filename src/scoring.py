"""
scoring.py — LLM Scoring Layer
Uses Groq API (llama-3.3-70b-versatile) to score aggregated records
against client-defined criteria.

Patterns from registry:
- Groq structured JSON enforcement (terbukti P22)
- 3-retry backoff for LLM (terbukti P22)
- Weighted explainable scoring (terbukti P22)
- Two-pass LLM processing (AI Pipeline Patterns)
- DRY_RUN mode (terbukti P22, P23)
"""

import json
import time
import logging
import os
from typing import Optional
from groq import Groq

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODEL         = "llama-3.3-70b-versatile"   # Active per Jun 2026 (terbukti P22)
TEMPERATURE   = 0.1                          # Avoid token loop (terbukti P22)
MAX_TOKENS    = 512
DRY_RUN       = os.getenv("DRY_RUN", "false").lower() == "true"

# ─── Static System Prompt (cache-friendly — never changes between records) ───
# Keep this IDENTICAL across all calls so Groq can cache it.
# Only the user message (the record) changes per call.

SYSTEM_PROMPT = """You are a strict JSON scoring engine. You evaluate records against acquisition criteria.

SCORING CRITERIA:
- relevance    (0–40): How closely does the record match the client's target domain/industry?
- recency      (0–30): How recent is the source data? Current = 30, 6mo ago = 15, older = 0.
- completeness (0–20): How complete are the key fields (title, description, source, date)?
- actionability(0–10): Does this record contain a clear next step or contact point?

OUTPUT FORMAT — Return ONLY valid JSON, no markdown, no preamble, no explanation outside JSON:
{
  "relevance": <int 0-40>,
  "recency": <int 0-30>,
  "completeness": <int 0-20>,
  "actionability": <int 0-10>,
  "total_score": <sum of above, int 0-100>,
  "fit_tier": "<HIGH|MEDIUM|LOW>",
  "reasoning": "<one sentence max>",
  "flag_for_review": <true|false>
}

TIER RULES:
- HIGH   : total_score >= 70
- MEDIUM : total_score 40–69
- LOW    : total_score < 40

flag_for_review = true if any required field is missing OR reasoning is uncertain."""


# ─── Groq Client (singleton) ─────────────────────────────────────────────────

_client: Optional[Groq] = None

def _get_client() -> Groq:
    global _client
    if _client is None:
        if not GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY not set in environment")
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


# ─── Core Scoring Function ────────────────────────────────────────────────────

def score_record(record: dict) -> dict:
    """
    Score a single record against criteria using Groq LLM.

    Args:
        record: dict with keys like title, description, source, date, url, etc.

    Returns:
        dict with original record + scoring fields injected.
        On failure after 3 retries, returns record with default LOW score.
    """
    if DRY_RUN:
        logger.debug(f"[DRY_RUN] Skipping Groq call for: {record.get('title', 'unknown')[:60]}")
        return _inject_score(record, _default_score())

    user_message = _build_user_message(record)
    raw_response = _call_groq_with_retry(user_message)

    if raw_response is None:
        logger.warning(f"Groq failed after 3 retries for record: {record.get('title', 'unknown')[:60]}")
        return _inject_score(record, _default_score())

    score_data = _parse_score_response(raw_response)
    return _inject_score(record, score_data)


def score_batch(records: list[dict]) -> list[dict]:
    """
    Score a list of records. Logs progress every 10 records.

    Args:
        records: list of dicts from scraper output

    Returns:
        list of dicts with scores injected
    """
    scored = []
    total  = len(records)

    for i, record in enumerate(records, 1):
        result = score_record(record)
        scored.append(result)

        if i % 10 == 0 or i == total:
            high   = sum(1 for r in scored if r.get("fit_tier") == "HIGH")
            medium = sum(1 for r in scored if r.get("fit_tier") == "MEDIUM")
            low    = sum(1 for r in scored if r.get("fit_tier") == "LOW")
            logger.info(f"Scored {i}/{total} | HIGH:{high} MEDIUM:{medium} LOW:{low}")

        # Groq free tier: ~30 RPM. Small jitter to stay safe.
        if not DRY_RUN:
            time.sleep(0.5)

    return scored


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_user_message(record: dict) -> str:
    """Serialize record to a clean string for the LLM user turn."""
    # Only send fields that are meaningful for scoring — don't leak internal IDs
    scoring_fields = {
        "title":       record.get("title", ""),
        "description": record.get("description", ""),
        "source":      record.get("source_name", ""),
        "date":        record.get("published_date", ""),
        "url":         record.get("url", ""),
        "category":    record.get("category", ""),
    }
    return f"Score this record:\n{json.dumps(scoring_fields, ensure_ascii=False, indent=2)}"


def _call_groq_with_retry(user_message: str, max_retries: int = 3) -> Optional[str]:
    """
    Call Groq API with retry + backoff.
    Returns raw text response, or None if all retries fail.
    Pattern: 3-retry backoff (terbukti P22)
    """
    client = _get_client()

    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_message},
                ],
            )
            return response.choices[0].message.content

        except Exception as e:
            wait = attempt * 2  # 2s, 4s, 6s
            logger.warning(f"Groq attempt {attempt}/{max_retries} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)

    return None


def _parse_score_response(raw: str) -> dict:
    """
    Parse Groq response to score dict.
    Strips markdown fences before parsing (terbukti P22).
    Falls back to default score on parse failure.
    """
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data  = json.loads(clean)

        # Validate required keys exist
        required = {"relevance", "recency", "completeness", "actionability",
                    "total_score", "fit_tier", "reasoning", "flag_for_review"}
        if not required.issubset(data.keys()):
            logger.warning(f"Groq response missing keys. Got: {list(data.keys())}")
            return _default_score()

        # Clamp numeric fields to valid range
        data["relevance"]     = max(0, min(40, int(data["relevance"])))
        data["recency"]       = max(0, min(30, int(data["recency"])))
        data["completeness"]  = max(0, min(20, int(data["completeness"])))
        data["actionability"] = max(0, min(10, int(data["actionability"])))
        data["total_score"]   = (data["relevance"] + data["recency"] +
                                 data["completeness"] + data["actionability"])

        # Normalize tier
        if data["fit_tier"] not in ("HIGH", "MEDIUM", "LOW"):
            data["fit_tier"] = _tier_from_score(data["total_score"])

        return data

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"Failed to parse Groq response: {e}. Raw: {raw[:200]}")
        return _default_score()


def _tier_from_score(score: int) -> str:
    if score >= 70:
        return "HIGH"
    elif score >= 40:
        return "MEDIUM"
    return "LOW"


def _default_score() -> dict:
    """Safe fallback when LLM fails — flagged for manual review."""
    return {
        "relevance":      0,
        "recency":        0,
        "completeness":   0,
        "actionability":  0,
        "total_score":    0,
        "fit_tier":       "LOW",
        "reasoning":      "Scoring failed — manual review required",
        "flag_for_review": True,
    }


def _inject_score(record: dict, score_data: dict) -> dict:
    """Merge score fields into record dict."""
    return {**record, **score_data}


# ─── Golden-Example Tests (inline, run with: python -m pytest tests/) ────────
# Full test suite is in tests/test_scoring.py
# These are the expected inputs → outputs for CI validation.

GOLDEN_EXAMPLES = [
    {
        "input": {
            "title": "RFP: Enterprise Data Integration Platform",
            "description": "Government agency seeking vendor for ETL pipeline, API integration, PostgreSQL backend. Budget $500K. Deadline June 2026.",
            "source_name": "SAM.gov",
            "published_date": "2026-06-01",
            "url": "https://sam.gov/opp/abc123",
            "category": "government_contract",
        },
        "expected_tier": "HIGH",
        "min_score": 70,
    },
    {
        "input": {
            "title": "Looking for Python tutor",
            "description": "Need someone to teach me Python basics.",
            "source_name": "Craigslist",
            "published_date": "2025-01-01",
            "url": "https://craigslist.org/xxx",
            "category": "education",
        },
        "expected_tier": "LOW",
        "max_score": 39,
    },
]
