"""Tests for RSIAnalyzer, VolumeAnalyzer, KlineAnalyzer."""
from __future__ import annotations

import pytest

from broker.analysis import RSIAnalyzer, VolumeAnalyzer, SupportResistance, KlineAnalyzer
from tests.conftest import make_bar, make_bars


class TestRSIAnalyzer:
    @pytest.fixture
    def rsi(self):
        return RSIAnalyzer(period=14)

    def test_returns_50_when_insufficient_data(self, rsi):
        assert rsi.compute([100.0] * 10) == 50.0

    def test_returns_50_for_exactly_period_bars(self, rsi):
        # Need period+1 bars minimum
        assert rsi.compute([100.0] * 14) == 50.0

    def test_all_gains_returns_100(self, rsi):
        closes = list(range(1, 17))  # strictly increasing
        result = rsi.compute(closes)
        assert result == 100.0

    def test_all_losses_returns_near_zero(self, rsi):
        closes = list(range(16, 0, -1))  # strictly decreasing
        result = rsi.compute(closes)
        assert result < 5.0

    def test_flat_prices_returns_50(self, rsi):
        # BUG: was returning 100.0 — should return 50.0
        closes = [100.0] * 20
        result = rsi.compute(closes)
        assert result == pytest.approx(50.0), f"Expected 50.0 for flat prices, got {result}"

    def test_alternating_returns_near_50(self, rsi):
        closes = [100.0, 101.0] * 10
        result = rsi.compute(closes)
        assert 40.0 < result < 60.0

    def test_overbought_interpretation(self, rsi):
        label, _ = rsi.interpret(75.0)
        assert label == "超买"

    def test_oversold_interpretation(self, rsi):
        label, _ = rsi.interpret(25.0)
        assert label == "超卖"

    def test_neutral_interpretation(self, rsi):
        label, _ = rsi.interpret(50.0)
        assert label == "中性"

    def test_rsi_realistic_value(self, rsi):
        # 10 up days of +1, 4 down days of -1
        closes = [100.0]
        for _ in range(10):
            closes.append(closes[-1] + 1.0)
        for _ in range(4):
            closes.append(closes[-1] - 1.0)
        result = rsi.compute(closes)
        assert result > 50.0  # mostly up → RSI > 50


class TestVolumeAnalyzer:
    @pytest.fixture
    def vol(self):
        return VolumeAnalyzer()

    def test_returns_1_when_insufficient(self, vol):
        assert vol.compute_ratio([1000.0] * 10) == 1.0

    def test_expanding_volume_profile(self, vol):
        # Older volumes low, recent 20 volumes high
        volumes = [1000.0] * 20 + [2000.0] * 20
        assert vol.profile(volumes) == "expanding"

    def test_contracting_volume_profile(self, vol):
        volumes = [2000.0] * 20 + [500.0] * 20
        assert vol.profile(volumes) == "contracting"

    def test_normal_volume_profile(self, vol):
        volumes = [1000.0] * 40
        assert vol.profile(volumes) == "normal"

    def test_ratio_expanding(self, vol):
        volumes = [1000.0] * 20 + [2000.0] * 20
        ratio = vol.compute_ratio(volumes)
        assert ratio == pytest.approx(2.0)

    def test_zero_baseline_returns_1(self, vol):
        volumes = [0.0] * 30 + [1000.0] * 20
        result = vol.compute_ratio(volumes)
        assert result == 1.0


class TestSupportResistance:
    @pytest.fixture
    def sr(self):
        return SupportResistance()

    def test_detects_correct_support_resistance(self, sr):
        bars = [make_bar(100.0, high=105.0, low=95.0) for _ in range(20)]
        support, resistance = sr.detect(bars)
        assert support == pytest.approx(95.0)
        assert resistance == pytest.approx(105.0)

    def test_uses_lookback_bars(self, sr):
        early = [make_bar(50.0, high=60.0, low=40.0)] * 10
        recent = [make_bar(100.0, high=110.0, low=90.0)] * 10
        bars = early + recent
        support, resistance = sr.detect(bars, lookback=10)
        assert support == pytest.approx(90.0)
        assert resistance == pytest.approx(110.0)

    def test_fewer_bars_than_lookback_uses_all(self, sr):
        bars = [make_bar(100.0, high=110.0, low=90.0)] * 5
        support, resistance = sr.detect(bars, lookback=20)
        assert support == pytest.approx(90.0)


class TestKlineAnalyzer:
    @pytest.fixture
    def analyzer(self):
        return KlineAnalyzer()

    def test_raises_if_fewer_than_20_bars(self, analyzer):
        with pytest.raises(ValueError, match="至少需要 20"):
            analyzer.analyze(make_bars([100.0] * 19))

    def test_basic_analyze_returns_valid_result(self, analyzer):
        bars = make_bars([100.0] * 20)
        result = analyzer.analyze(bars)
        assert result.bias in {"BULLISH", "BEARISH", "NEUTRAL"}
        assert result.trend in {"UP", "DOWN", "SIDEWAYS"}
        assert isinstance(result.rsi, float)
        assert isinstance(result.patterns, list)

    def test_uptrend_detected(self, analyzer):
        closes = list(range(80, 100)) + [102.5]  # 20 bars, clear uptrend
        bars = make_bars(closes)
        result = analyzer.analyze(bars)
        assert result.trend == "UP"

    def test_downtrend_detected(self, analyzer):
        closes = list(range(120, 100, -1))  # 20 bars, clear downtrend
        bars = make_bars(closes)
        result = analyzer.analyze(bars)
        assert result.trend == "DOWN"

    def test_sideways_trend(self, analyzer):
        closes = [100.0, 101.0] * 10  # oscillating flat
        bars = make_bars(closes)
        result = analyzer.analyze(bars)
        assert result.trend == "SIDEWAYS"

    def test_hammer_pattern_detected(self, analyzer):
        # Need 19 base bars + 1 pattern bar = 20
        bars = make_bars([100.0] * 20)
        # body=0.5, lower_shadow=6.0 (> body*2=1.0), upper_shadow=0.3 (< body=0.5)
        hammer = make_bar(close=101.0, open_=101.5, high=101.8, low=95.0)
        result = analyzer.analyze(bars[:-1] + [hammer])
        assert "HAMMER" in result.patterns

    def test_shooting_star_pattern_detected(self, analyzer):
        bars = make_bars([100.0] * 20)
        # body=0.5, upper_shadow=7.5 (> body*2=1.0), lower_shadow=0.2 (< body=0.5)
        star = make_bar(close=100.5, open_=100.0, high=108.0, low=99.8)
        result = analyzer.analyze(bars[:-1] + [star])
        assert "SHOOTING_STAR" in result.patterns

    def test_bullish_engulfing(self, analyzer):
        bars = make_bars([100.0] * 18)
        prev = make_bar(close=98.0, open_=101.0, high=102.0, low=97.0)  # bearish candle
        curr = make_bar(close=102.5, open_=96.5, high=103.0, low=96.0)  # engulfs prev
        result = analyzer.analyze(bars + [prev, curr])
        assert "BULLISH_ENGULFING" in result.patterns

    def test_breakout_detected(self, analyzer):
        # Last bar closes above prior 19-bar high
        bars = make_bars([100.0] * 19)
        breakout = make_bar(close=110.0, open_=100.0, high=111.0, low=99.0)
        result = analyzer.analyze(bars + [breakout])
        assert "BREAKOUT" in result.patterns
        assert result.score > 0

    def test_score_bullish_bias_threshold(self, analyzer):
        # Multiple bullish signals → score >= 2 → BULLISH bias
        bars = make_bars(list(range(80, 100)) + [110.0])  # uptrend + breakout
        result = analyzer.analyze(bars)
        # uptrend (+1) + breakout (+1) = 2 → BULLISH
        assert result.bias == "BULLISH"
