"""Tests for RiskManager."""
from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from broker.risk import RiskManager, RiskState
from tests.conftest import make_settings


def _dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def manager():
    return RiskManager(
        make_settings(
            stop_loss_pct=0.03,
            take_profit_pct=0.06,
            max_drawdown_pct=0.15,
            max_daily_loss_pct=0.03,
            trade_start_time=time(9, 30),
            trade_end_time=time(16, 0),
        )
    )


class TestRiskManagerStopLoss:
    def test_stop_loss_triggers_force_sell(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(10), equity=97_000,
                                  position=10, last_price=97.0, avg_cost=100.0)
        assert result.action == "FORCE_SELL"
        assert result.reason == "stop_loss"

    def test_stop_loss_exactly_at_threshold(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(10), equity=97_000,
                                  position=10, last_price=97.0, avg_cost=100.0)
        # (97-100)/100 = -0.03 → exactly at threshold
        assert result.action == "FORCE_SELL"

    def test_just_below_stop_loss_allows(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(10), equity=98_000,
                                  position=10, last_price=97.1, avg_cost=100.0)
        # (97.1-100)/100 = -0.029 → below threshold
        assert result.action == "ALLOW"

    def test_no_stop_loss_without_avg_cost(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(10), equity=90_000,
                                  position=10, last_price=80.0, avg_cost=None)
        # No avg_cost → stop-loss skipped
        assert result.action != "FORCE_SELL" or result.reason != "stop_loss"

    def test_take_profit_triggers_force_sell(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(10), equity=106_000,
                                  position=10, last_price=106.0, avg_cost=100.0)
        assert result.action == "FORCE_SELL"
        assert result.reason == "take_profit"

    def test_no_stop_loss_when_no_position(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(10), equity=97_000,
                                  position=0, last_price=97.0, avg_cost=100.0)
        assert result.action == "ALLOW"


class TestRiskManagerDrawdown:
    def test_max_drawdown_blocks_buy(self, manager):
        state = RiskState(equity_peak=100_000, day_start_equity=100_000)
        result = manager.evaluate(state, now=_dt(10), equity=84_000,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "BLOCK_BUY"
        assert result.reason == "max_drawdown"

    def test_equity_peak_updates(self, manager):
        state = manager.initial_state(100_000)
        manager.evaluate(state, now=_dt(10), equity=110_000,
                         position=0, last_price=110.0, avg_cost=None)
        assert state.equity_peak == 110_000

    def test_drawdown_below_threshold_allows(self, manager):
        state = RiskState(equity_peak=100_000, day_start_equity=100_000)
        result = manager.evaluate(state, now=_dt(10), equity=86_000,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "ALLOW"


class TestRiskManagerDailyLoss:
    def test_max_daily_loss_with_position_force_sells(self, manager):
        state = RiskState(equity_peak=100_000, day_start_equity=100_000, current_day="2024-01-01")
        result = manager.evaluate(state, now=_dt(10), equity=96_000,
                                  position=5, last_price=95.0, avg_cost=100.0)
        # stop-loss hits first (pnl=-5%) → FORCE_SELL for stop_loss
        # test daily loss with no avg_cost
        state2 = RiskState(equity_peak=100_000, day_start_equity=100_000, current_day="2024-01-01")
        result2 = manager.evaluate(state2, now=_dt(10), equity=96_000,
                                   position=5, last_price=99.0, avg_cost=None)
        assert result2.action == "FORCE_SELL"
        assert result2.reason == "max_daily_loss"

    def test_max_daily_loss_no_position_blocks_buy(self, manager):
        state = RiskState(equity_peak=100_000, day_start_equity=100_000, current_day="2024-01-01")
        result = manager.evaluate(state, now=_dt(10), equity=96_500,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "BLOCK_BUY"
        assert result.reason == "max_daily_loss"

    def test_day_rollover_resets_day_start(self, manager):
        state = RiskState(equity_peak=100_000, day_start_equity=100_000, current_day="2024-01-01")
        manager.evaluate(state, now=_dt(10, day=2), equity=98_000,
                         position=0, last_price=100.0, avg_cost=None)
        assert state.current_day == "2024-01-02"
        assert state.day_start_equity == 98_000


class TestRiskManagerTradingHours:
    def test_outside_hours_blocks_new(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(8, 0), equity=100_000,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "BLOCK_NEW"
        assert result.reason == "outside_trading_hours"

    def test_after_close_blocks_new(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(16, 30), equity=100_000,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "BLOCK_NEW"

    def test_during_hours_allows(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=_dt(12, 0), equity=100_000,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "ALLOW"

    def test_none_time_skips_hours_check(self, manager):
        state = manager.initial_state(100_000)
        result = manager.evaluate(state, now=None, equity=100_000,
                                  position=0, last_price=100.0, avg_cost=None)
        assert result.action == "ALLOW"

    def test_stop_loss_takes_priority_over_hours(self, manager):
        state = manager.initial_state(100_000)
        # Outside hours, but stop-loss should fire first
        result = manager.evaluate(state, now=_dt(8, 0), equity=97_000,
                                  position=10, last_price=97.0, avg_cost=100.0)
        assert result.action == "FORCE_SELL"
        assert result.reason == "stop_loss"
