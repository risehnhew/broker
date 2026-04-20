"""Tests for NewsAnalyzer."""
from __future__ import annotations

import pytest

from broker.news import NewsAnalyzer
from tests.conftest import make_headline


@pytest.fixture
def analyzer():
    return NewsAnalyzer()


class TestNewsAnalyzer:
    def test_empty_headlines_returns_neutral(self, analyzer):
        result = analyzer.analyze([])
        assert result.score == 0
        assert result.sentiment == "NEUTRAL"
        assert result.headline_count == 0

    def test_positive_keyword_increments_score(self, analyzer):
        result = analyzer.analyze([make_headline("AAPL beats earnings expectations")])
        assert result.score > 0
        assert result.sentiment in {"POSITIVE", "NEUTRAL"}

    def test_negative_keyword_decrements_score(self, analyzer):
        result = analyzer.analyze([make_headline("AAPL misses earnings forecast")])
        assert result.score < 0

    def test_strong_positive_gives_positive_sentiment(self, analyzer):
        headlines = [
            make_headline("Company beats earnings record"),
            make_headline("Strong profit growth reported"),
        ]
        result = analyzer.analyze(headlines)
        assert result.sentiment == "POSITIVE"
        assert result.score >= 2

    def test_strong_negative_gives_negative_sentiment(self, analyzer):
        headlines = [
            make_headline("Company misses earnings forecast"),
            make_headline("Weak revenue decline reported"),
        ]
        result = analyzer.analyze(headlines)
        assert result.sentiment == "NEGATIVE"
        assert result.score <= -2

    def test_mixed_signals_neutral(self, analyzer):
        headlines = [
            make_headline("Record profit"),    # +2 (record + profit)
            make_headline("Decline in sales"), # -1
            make_headline("Fraud probe"),      # -2 (fraud + probe)
        ]
        result = analyzer.analyze(headlines)
        assert result.sentiment == "NEUTRAL"

    def test_headline_count_excludes_empty(self, analyzer):
        headlines = [
            make_headline("Good news"),
            make_headline("   "),  # whitespace-only
            make_headline(""),
        ]
        result = analyzer.analyze(headlines)
        assert result.headline_count == 1

    def test_case_insensitive_matching(self, analyzer):
        result_lower = analyzer.analyze([make_headline("company beats target")])
        result_upper = analyzer.analyze([make_headline("COMPANY BEATS TARGET")])
        assert result_lower.score == result_upper.score

    def test_both_keywords_in_headline_net_score(self, analyzer):
        # "strong" (+1) and "warning" (-1) in same headline → net 0
        result = analyzer.analyze([make_headline("Strong warning issued")])
        assert result.score == 0

    # ── Word-boundary edge cases ────────────────────────────────────────────
    def test_no_false_positive_nonprofit(self, analyzer):
        result = analyzer.analyze([make_headline("Nonprofit organization reports results")])
        # "profit" should NOT match inside "nonprofit"
        assert result.score == 0, (
            f"'nonprofit' falsely matched 'profit': score={result.score}"
        )

    def test_no_false_positive_mission(self, analyzer):
        result = analyzer.analyze([make_headline("Company mission statement updated")])
        # "miss" should NOT match inside "mission"
        assert result.score == 0, (
            f"'mission' falsely matched 'miss': score={result.score}"
        )

    def test_no_false_positive_cute_cut(self, analyzer):
        result = analyzer.analyze([make_headline("Acute shortage analysis")])
        # "cut" should NOT match inside "acute"
        assert result.score == 0, (
            f"'acute' falsely matched 'cut': score={result.score}"
        )

    def test_no_false_positive_fundraise(self, analyzer):
        result = analyzer.analyze([make_headline("Fundraise campaign launched")])
        # "raise" should NOT match inside "fundraise" unless standalone
        assert result.score == 0, (
            f"'fundraise' falsely matched 'raise': score={result.score}"
        )

    def test_standalone_keyword_still_matches(self, analyzer):
        # Real "profit" as a word should still match
        result = analyzer.analyze([make_headline("Record profit reported this quarter")])
        assert result.score > 0

    def test_headlines_included_in_output(self, analyzer):
        result = analyzer.analyze([make_headline("Good news", provider="TEST")])
        assert len(result.headlines) == 1
        assert "[TEST]" in result.headlines[0]
