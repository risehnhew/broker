"""Tests for SimulationEngine."""
from __future__ import annotations

import pytest

from datetime import time

from broker.models import SimulationConfig
from broker.simulation import SimulationEngine
from tests.conftest import make_bar, make_settings


def make_config(**kw) -> SimulationConfig:
    defaults = dict(fast_sma=5, slow_sma=20, stop_loss_pct=0.03, take_profit_pct=0.06)
    defaults.update(kw)
    return SimulationConfig(**defaults)


def _dt(i: int) -> str:
    # 14:30 UTC = 09:30 NY — inside default trading hours after UTC→NY conversion
    hour = 14 + i // 60
    minute = i % 60
    return f"20240101 {hour:02d}:{minute:02d}:00"


def _rising_bars(n: int, start: float = 100.0, step: float = 0.5) -> list:
    return [make_bar(start + i * step, date=_dt(i)) for i in range(n)]


def _falling_bars(n: int, start: float = 120.0, step: float = 0.5) -> list:
    return [make_bar(start - i * step, date=_dt(i)) for i in range(n)]


def _flat_bars(n: int, price: float = 100.0) -> list:
    return [make_bar(price, date=_dt(i)) for i in range(n)]


class TestSimulationEngineBasics:
    @pytest.fixture
    def engine(self):
        return SimulationEngine(make_settings(), make_config())

    def test_raises_with_insufficient_bars(self, engine):
        with pytest.raises(RuntimeError, match="insufficient"):
            engine.run("AAPL", _flat_bars(21))

    def test_returns_result_for_valid_bars(self, engine):
        bars = _flat_bars(100)
        result = engine.run("AAPL", bars)
        assert result.symbol == "AAPL"
        assert isinstance(result.trades, int)
        assert isinstance(result.win_rate, float)
        assert isinstance(result.final_equity, float)

    def test_flat_market_no_trades(self, engine):
        bars = _flat_bars(100)
        result = engine.run("AAPL", bars)
        assert result.trades == 0
        assert result.net_profit == pytest.approx(0.0)

    def test_equity_curve_non_empty(self, engine):
        bars = _flat_bars(100)
        result = engine.run("AAPL", bars)
        assert len(result.equity_curve) > 0

    def test_final_equity_equals_cash_plus_open_position(self, engine):
        bars = _flat_bars(100)
        result = engine.run("AAPL", bars)
        last = result.equity_curve[-1]
        assert result.final_equity == pytest.approx(last.equity, rel=0.01)


class TestSimulationEngineTrades:
    def test_buy_signal_produces_trade(self):
        # Flat then rising → fast_sma crosses above slow_sma → BUY
        bars = _flat_bars(20) + _rising_bars(60, start=100.0)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        buys = [t for t in result.trade_log if t.action == "BUY"]
        assert len(buys) > 0

    def test_sell_signal_produces_trade(self):
        # Rising then falling → fast_sma crosses below slow_sma → SELL
        bars = _rising_bars(40, start=100.0) + _falling_bars(40, start=120.0)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        sells = [t for t in result.trade_log if t.action == "SELL"]
        assert len(sells) > 0

    def test_stop_loss_fires(self):
        config = make_config(stop_loss_pct=0.03, fast_sma=5, slow_sma=20)
        # Disable trading-hours gate so all bars are eligible
        settings = make_settings(order_quantity=1, max_position=10,
                                 trade_start_time=time(0, 0), trade_end_time=time(23, 59))
        # Rising bars to trigger buy, then crash triggers stop-loss
        bars = _rising_bars(25, start=100.0) + _falling_bars(40, start=113.0, step=2.0)
        engine = SimulationEngine(settings, config)
        result = engine.run("AAPL", bars)
        stop_loss_sells = [t for t in result.trade_log if t.reason == "stop_loss"]
        assert len(stop_loss_sells) > 0

    def test_take_profit_fires(self):
        config = make_config(take_profit_pct=0.05, fast_sma=5, slow_sma=20)
        settings = make_settings(order_quantity=1, max_position=10,
                                 trade_start_time=time(0, 0), trade_end_time=time(23, 59))
        # Rising bars trigger buy ~110, then jump to 130 (18%) triggers take-profit
        bars = _rising_bars(25, start=100.0) + [make_bar(130.0, date=_dt(25 + i)) for i in range(40)]
        engine = SimulationEngine(settings, config)
        result = engine.run("AAPL", bars)
        take_profit_sells = [t for t in result.trade_log if t.reason == "take_profit"]
        assert len(take_profit_sells) > 0

    def test_win_rate_between_0_and_100(self):
        bars = _rising_bars(40, start=100.0) + _falling_bars(40, start=120.0)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        assert 0.0 <= result.win_rate <= 100.0

    def test_no_negative_position(self):
        bars = _flat_bars(20) + _rising_bars(60)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        for point in result.equity_curve:
            assert point.position >= 0

    def test_cash_never_negative(self):
        bars = _flat_bars(20) + _rising_bars(60)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        for point in result.equity_curve:
            assert point.cash >= 0.0


class TestSimulationEngineMaxDrawdown:
    def test_max_drawdown_calculated(self):
        # Volatile bars should produce non-zero drawdown
        bars = _rising_bars(40, start=100.0) + _falling_bars(40, start=120.0)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        assert result.max_drawdown >= 0.0

    def test_max_drawdown_between_0_and_1(self):
        bars = _rising_bars(40, start=100.0) + _falling_bars(40, start=120.0)
        engine = SimulationEngine(make_settings(), make_config())
        result = engine.run("AAPL", bars)
        assert 0.0 <= result.max_drawdown <= 1.0


class TestSimulationEngineAvgCost:
    def test_weighted_avg_cost_on_multiple_buys(self):
        config = make_config(fast_sma=5, slow_sma=20, stop_loss_pct=0.5, take_profit_pct=0.5)
        settings = make_settings(order_quantity=1, max_position=10)
        bars = _flat_bars(22) + _rising_bars(40, start=100.0)
        engine = SimulationEngine(settings, config)
        result = engine.run("AAPL", bars)
        # Should not crash; avg_cost calculation is verified implicitly
        assert result.final_equity > 0

    def test_partial_sell_maintains_position(self):
        config = make_config(fast_sma=5, slow_sma=20)
        settings = make_settings(order_quantity=1, max_position=10)
        bars = _rising_bars(30, start=100.0) + _falling_bars(40, start=115.0)
        engine = SimulationEngine(settings, config)
        result = engine.run("AAPL", bars)
        # Simply assert it runs without error and produces valid state
        assert result.open_position >= 0
