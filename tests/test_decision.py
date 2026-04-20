"""Tests for DecisionEngine."""
from __future__ import annotations

import pytest

from broker.decision import DecisionEngine
from broker.models import AnalysisSnapshot
from tests.conftest import make_settings


def _snap(
    base_action="HOLD",
    candle_bias="NEUTRAL",
    news_score=0,
    ai_action="HOLD",
    ai_confidence=0,
    ai_used=False,
) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        base_action=base_action,
        fast_sma=5.0,
        slow_sma=4.0,
        candle_bias=candle_bias,
        candle_score=0,
        news_score=news_score,
        news_sentiment="NEUTRAL",
        ai_action=ai_action,
        ai_confidence=ai_confidence,
        ai_summary="",
        ai_used=ai_used,
    )


class TestDecisionEngineNoAI:
    """AI analysis disabled — only SMA + candle + news gates."""

    @pytest.fixture
    def engine(self):
        return DecisionEngine(make_settings(enable_ai_analysis=False, ai_api_key=None))

    def test_buy_signal_confirmed(self, engine):
        snap = _snap(base_action="BUY", candle_bias="NEUTRAL", news_score=0)
        d = engine.decide(snap, position=0)
        assert d.action == "BUY"
        assert d.reason == "signal_confirmed"

    def test_buy_blocked_bearish_candle(self, engine):
        snap = _snap(base_action="BUY", candle_bias="BEARISH")
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "bearish_candle"

    def test_buy_blocked_weak_news(self, engine):
        # Default news_min_sentiment_to_buy = -1, so score of -2 blocks
        s = make_settings(enable_ai_analysis=False, ai_api_key=None, news_min_sentiment_to_buy=-1)
        engine = DecisionEngine(s)
        snap = _snap(base_action="BUY", candle_bias="NEUTRAL", news_score=-2)
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "weak_news"

    def test_sell_with_position(self, engine):
        snap = _snap(base_action="SELL")
        d = engine.decide(snap, position=5)
        assert d.action == "SELL"
        assert d.reason == "exit_signal"

    def test_sell_signal_no_position_becomes_hold(self, engine):
        snap = _snap(base_action="SELL")
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "no_signal"

    def test_hold_base_signal_returns_hold(self, engine):
        snap = _snap(base_action="HOLD")
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"

    def test_news_score_exactly_at_threshold_allows_buy(self, engine):
        # news_min_sentiment_to_buy = -1, score = -1 → borderline, should HOLD
        snap = _snap(base_action="BUY", news_score=-1)
        d = engine.decide(snap, position=0)
        # -1 < -1 is False, so it passes through → BUY
        assert d.action == "BUY"

    def test_news_score_below_threshold_blocks_buy(self, engine):
        snap = _snap(base_action="BUY", news_score=-2)
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "weak_news"


class TestDecisionEngineWithAI:
    """AI analysis enabled — AI action and confidence gates."""

    @pytest.fixture
    def engine(self):
        return DecisionEngine(
            make_settings(enable_ai_analysis=True, ai_api_key="test-key", ai_min_confidence=60)
        )

    def test_ai_buy_high_confidence_confirms_buy(self, engine):
        snap = _snap(base_action="BUY", candle_bias="NEUTRAL", news_score=0,
                     ai_action="BUY", ai_confidence=70, ai_used=True)
        d = engine.decide(snap, position=0)
        assert d.action == "BUY"

    def test_ai_low_confidence_blocks_buy(self, engine):
        snap = _snap(base_action="BUY", ai_action="BUY", ai_confidence=50, ai_used=True)
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "low_ai_confidence"

    def test_ai_not_buy_blocks_buy(self, engine):
        snap = _snap(base_action="BUY", ai_action="HOLD", ai_confidence=80, ai_used=True)
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "ai_not_buy"

    def test_ai_sell_not_buy_blocks_buy(self, engine):
        snap = _snap(base_action="BUY", ai_action="SELL", ai_confidence=80, ai_used=True)
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "ai_not_buy"

    def test_ai_buy_high_confidence_blocks_sell(self, engine):
        snap = _snap(base_action="SELL", ai_action="BUY", ai_confidence=70, ai_used=True)
        d = engine.decide(snap, position=5)
        assert d.action == "HOLD"
        assert d.reason == "ai_blocks_sell"

    def test_ai_hold_allows_sell(self, engine):
        # AI HOLD with high confidence should NOT block SELL
        snap = _snap(base_action="SELL", ai_action="HOLD", ai_confidence=70, ai_used=True)
        d = engine.decide(snap, position=5)
        assert d.action == "SELL"

    def test_ai_sell_allows_sell(self, engine):
        snap = _snap(base_action="SELL", ai_action="SELL", ai_confidence=70, ai_used=True)
        d = engine.decide(snap, position=5)
        assert d.action == "SELL"

    def test_ai_buy_low_confidence_does_not_block_sell(self, engine):
        # AI says BUY but confidence below threshold → sell proceeds
        snap = _snap(base_action="SELL", ai_action="BUY", ai_confidence=40, ai_used=True)
        d = engine.decide(snap, position=5)
        assert d.action == "SELL"

    def test_bearish_candle_gates_before_ai_check(self, engine):
        snap = _snap(base_action="BUY", candle_bias="BEARISH",
                     ai_action="BUY", ai_confidence=90, ai_used=True)
        d = engine.decide(snap, position=0)
        assert d.action == "HOLD"
        assert d.reason == "bearish_candle"

    def test_no_ai_used_bypasses_ai_confidence_check(self, engine):
        # When ai_used=False, low AI confidence should NOT block BUY
        snap = _snap(base_action="BUY", candle_bias="NEUTRAL", news_score=0,
                     ai_action="BUY", ai_confidence=20, ai_used=False)
        d = engine.decide(snap, position=0)
        assert d.action == "BUY"
        assert d.reason == "signal_confirmed"

    def test_no_ai_used_bypasses_ai_action_check(self, engine):
        # When ai_used=False, ai_action=HOLD should NOT block BUY
        snap = _snap(base_action="BUY", candle_bias="NEUTRAL", news_score=0,
                     ai_action="HOLD", ai_confidence=80, ai_used=False)
        d = engine.decide(snap, position=0)
        assert d.action == "BUY"
        assert d.reason == "signal_confirmed"
