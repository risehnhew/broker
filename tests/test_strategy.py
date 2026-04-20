"""Tests for SmaCrossStrategy."""
from __future__ import annotations

import pytest

from broker.strategy import SmaCrossStrategy


@pytest.fixture
def strategy():
    return SmaCrossStrategy(fast_window=5, slow_window=20)


def _closes(n: int, base: float = 100.0) -> list[float]:
    return [base] * n


class TestSmaCrossStrategy:
    def test_buy_signal_fast_above_slow_no_position(self, strategy):
        # Last 5 bars higher than average of 20 → fast_sma > slow_sma
        closes = [100.0] * 15 + [110.0] * 5
        sig = strategy.evaluate(closes, position=0)
        assert sig.action == "BUY"
        assert sig.fast_sma > sig.slow_sma

    def test_sell_signal_fast_below_slow_with_position(self, strategy):
        closes = [110.0] * 15 + [90.0] * 5
        sig = strategy.evaluate(closes, position=5)
        assert sig.action == "SELL"
        assert sig.fast_sma < sig.slow_sma

    def test_hold_when_fast_above_slow_but_already_long(self, strategy):
        closes = [100.0] * 15 + [110.0] * 5
        sig = strategy.evaluate(closes, position=10)
        assert sig.action == "HOLD"

    def test_hold_when_fast_below_slow_but_no_position(self, strategy):
        closes = [110.0] * 15 + [90.0] * 5
        sig = strategy.evaluate(closes, position=0)
        assert sig.action == "HOLD"

    def test_hold_when_smas_equal(self, strategy):
        closes = [100.0] * 20
        sig = strategy.evaluate(closes, position=5)
        assert sig.action == "HOLD"
        assert sig.fast_sma == sig.slow_sma

    def test_raises_if_insufficient_data(self, strategy):
        with pytest.raises(ValueError, match="至少需要"):
            strategy.evaluate([100.0] * 19, position=0)

    def test_exactly_minimum_bars(self, strategy):
        closes = [100.0] * 20
        sig = strategy.evaluate(closes, position=0)
        assert sig.action == "HOLD"

    def test_sma_values_computed_correctly(self):
        strat = SmaCrossStrategy(fast_window=3, slow_window=5)
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        sig = strat.evaluate(closes, position=0)
        assert sig.fast_sma == pytest.approx(4.0)   # avg(3,4,5)
        assert sig.slow_sma == pytest.approx(3.0)   # avg(1,2,3,4,5)

    def test_negative_position_treated_as_no_position(self, strategy):
        closes = [100.0] * 15 + [110.0] * 5
        sig = strategy.evaluate(closes, position=-1)
        assert sig.action == "BUY"

    def test_large_fast_window_matches_slow(self):
        strat = SmaCrossStrategy(fast_window=19, slow_window=20)
        closes = list(range(1, 21))
        sig = strat.evaluate(closes, position=0)
        # fast_sma = avg(2..20) = 11.0, slow_sma = avg(1..20) = 10.5
        assert sig.fast_sma > sig.slow_sma
        assert sig.action == "BUY"
