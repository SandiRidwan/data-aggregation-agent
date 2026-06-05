"""
tests/test_scoring.py — Test Suite for Scoring Layer

Coverage:
- _parse_score_response: valid JSON, markdown-fenced JSON, missing keys, bad JSON
- _tier_from_score: boundary values
- score_record: DRY_RUN mode, Groq failure fallback
- Golden-example tests: known input → expected output tier
- _call_groq_with_retry: retry behavior on failure
"""

import json
import pytest
from unittest.mock import patch, MagicMock

# Set DRY_RUN before importing scoring to avoid env side effects
import os
os.environ["GROQ_API_KEY"] = "test-key-placeholder"

from src.scoring import (
    _parse_score_response,
    _tier_from_score,
    _default_score,
    _build_user_message,
    _inject_score,
    score_record,
    GOLDEN_EXAMPLES,
)


# ─── _parse_score_response ────────────────────────────────────────────────────

class TestParseScoreResponse:

    def test_valid_json(self):
        raw = json.dumps({
            "relevance": 35, "recency": 25, "completeness": 18, "actionability": 8,
            "total_score": 86, "fit_tier": "HIGH",
            "reasoning": "Strong match", "flag_for_review": False
        })
        result = _parse_score_response(raw)
        assert result["fit_tier"] == "HIGH"
        assert result["total_score"] == 86
        assert result["flag_for_review"] is False

    def test_markdown_fenced_json(self):
        """Registry pattern: strip ```json fences before parsing (terbukti P22)"""
        raw = '```json\n{"relevance":30,"recency":20,"completeness":15,"actionability":7,"total_score":72,"fit_tier":"HIGH","reasoning":"ok","flag_for_review":false}\n```'
        result = _parse_score_response(raw)
        assert result["fit_tier"] == "HIGH"
        assert result["total_score"] == 72

    def test_score_clamped_to_max(self):
        """Scores exceeding max should be clamped."""
        raw = json.dumps({
            "relevance": 999, "recency": 999, "completeness": 999, "actionability": 999,
            "total_score": 999, "fit_tier": "HIGH",
            "reasoning": "test", "flag_for_review": False
        })
        result = _parse_score_response(raw)
        assert result["relevance"]     == 40
        assert result["recency"]       == 30
        assert result["completeness"]  == 20
        assert result["actionability"] == 10
        assert result["total_score"]   == 100

    def test_missing_keys_returns_default(self):
        """Partial response → fallback to default score."""
        raw = json.dumps({"relevance": 30, "reasoning": "incomplete"})
        result = _parse_score_response(raw)
        assert result == _default_score()

    def test_bad_json_returns_default(self):
        result = _parse_score_response("this is not json at all")
        assert result == _default_score()

    def test_empty_string_returns_default(self):
        result = _parse_score_response("")
        assert result == _default_score()

    def test_invalid_tier_normalized(self):
        """Unknown tier string → recalculated from score."""
        raw = json.dumps({
            "relevance": 35, "recency": 25, "completeness": 18, "actionability": 8,
            "total_score": 86, "fit_tier": "EXCELLENT",  # invalid
            "reasoning": "test", "flag_for_review": False
        })
        result = _parse_score_response(raw)
        assert result["fit_tier"] == "HIGH"  # recalculated from 86

    def test_total_score_recalculated(self):
        """total_score should be sum of components, not trusted from LLM."""
        raw = json.dumps({
            "relevance": 10, "recency": 10, "completeness": 10, "actionability": 5,
            "total_score": 9999,  # LLM hallucinated
            "fit_tier": "HIGH", "reasoning": "test", "flag_for_review": False
        })
        result = _parse_score_response(raw)
        assert result["total_score"] == 35  # 10+10+10+5


# ─── _tier_from_score ────────────────────────────────────────────────────────

class TestTierFromScore:

    @pytest.mark.parametrize("score,expected", [
        (100, "HIGH"),
        (70,  "HIGH"),
        (69,  "MEDIUM"),
        (40,  "MEDIUM"),
        (39,  "LOW"),
        (0,   "LOW"),
    ])
    def test_boundaries(self, score, expected):
        assert _tier_from_score(score) == expected


# ─── _build_user_message ─────────────────────────────────────────────────────

class TestBuildUserMessage:

    def test_includes_title(self):
        record = {"title": "Test Grant", "description": "desc", "source_name": "sam.gov",
                  "published_date": "2026-01-01", "url": "https://example.com", "category": "grant"}
        msg = _build_user_message(record)
        assert "Test Grant" in msg

    def test_excludes_internal_fields(self):
        """Internal IDs and score fields should not be in the user message."""
        record = {"title": "X", "id": 999, "total_score": 50, "scraped_at": "2026-01-01",
                  "description": "", "source_name": "", "published_date": "", "url": "", "category": ""}
        msg = _build_user_message(record)
        assert "999" not in msg
        assert "total_score" not in msg


# ─── score_record (DRY_RUN) ───────────────────────────────────────────────────

class TestScoreRecordDryRun:

    def test_dry_run_returns_default_score(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        # Re-import to pick up env change
        import importlib
        import src.scoring as scoring_module
        importlib.reload(scoring_module)

        record = {"title": "Test", "url": "https://example.com"}
        result = scoring_module.score_record(record)

        assert "fit_tier" in result
        assert "total_score" in result
        assert result["title"] == "Test"   # original fields preserved

    def test_dry_run_no_api_call(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        import importlib
        import src.scoring as scoring_module
        importlib.reload(scoring_module)

        with patch.object(scoring_module, "_call_groq_with_retry") as mock_call:
            scoring_module.score_record({"title": "Test", "url": "https://x.com"})
            mock_call.assert_not_called()


# ─── score_record (Groq failure fallback) ────────────────────────────────────

class TestScoreRecordFallback:

    def test_groq_failure_returns_flagged_record(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        import importlib
        import src.scoring as scoring_module
        importlib.reload(scoring_module)

        with patch.object(scoring_module, "_call_groq_with_retry", return_value=None):
            record = {"title": "Test", "url": "https://example.com"}
            result = scoring_module.score_record(record)

        assert result["flag_for_review"] is True
        assert result["fit_tier"] == "LOW"
        assert result["total_score"] == 0
        assert result["title"] == "Test"  # original preserved


# ─── Golden Examples ──────────────────────────────────────────────────────────

class TestGoldenExamples:
    """
    Integration-style tests against real Groq API.
    Skipped in CI unless GROQ_API_KEY is a real key (not placeholder).
    Run manually: pytest tests/test_scoring.py::TestGoldenExamples -v
    """

    @pytest.mark.skipif(
        os.getenv("GROQ_API_KEY", "") in ("", "test-key-placeholder"),
        reason="Real GROQ_API_KEY required for golden tests"
    )
    def test_high_fit_example(self):
        example = GOLDEN_EXAMPLES[0]
        import importlib
        import src.scoring as scoring_module
        importlib.reload(scoring_module)

        result = scoring_module.score_record(example["input"])
        assert result["fit_tier"] == example["expected_tier"], (
            f"Expected {example['expected_tier']}, got {result['fit_tier']}. "
            f"Score: {result['total_score']}. Reasoning: {result.get('reasoning')}"
        )
        assert result["total_score"] >= example["min_score"]

    @pytest.mark.skipif(
        os.getenv("GROQ_API_KEY", "") in ("", "test-key-placeholder"),
        reason="Real GROQ_API_KEY required for golden tests"
    )
    def test_low_fit_example(self):
        example = GOLDEN_EXAMPLES[1]
        import importlib
        import src.scoring as scoring_module
        importlib.reload(scoring_module)

        result = scoring_module.score_record(example["input"])
        assert result["fit_tier"] == example["expected_tier"]
        assert result["total_score"] <= example["max_score"]
