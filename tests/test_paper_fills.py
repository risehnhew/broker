"""Tests for PaperTrader virtual fill math (no IBKR connection)."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from broker.paper_trader import PaperTrader, PaperState
from tests.conftest import make_settings


def _make_trader(cash: float = 10_000.0, **settings_kw) -> PaperTrader:
    s = make_settings(**settings_kw)
    with patch("broker.paper_trader.IBClient"), \
         patch("broker.paper_trader.DecisionEngine"), \
         patch("broker.paper_trader.RiskManager") as MockRM, \
         patch("broker.paper_trader.AISymbolSelector"):
        MockRM.return_value.initial_state.return_value = MagicMock()
        trader = PaperTrader(s, starting_cash=cash)
    return trader


class TestVirtualBuy:
    def test_buy_deducts_cash(self):
        trader = _make_trader(cash=10_000.0)
        trader._virtual_buy("AAPL", price=100.0, reason="test", ai_result=MagicMock(action="BUY", confidence=80))
        assert trader._cash == pytest.approx(10_000.0 - 100.0 * 10)

    def test_buy_updates_position(self):
        trader = _make_trader(cash=10_000.0)
        trader._virtual_buy("AAPL", price=100.0, reason="test", ai_result=MagicMock(action="BUY", confidence=80))
        assert trader._positions["AAPL"]["quantity"] == 10

    def test_buy_sets_avg_cost(self):
        trader = _make_trader(cash=10_000.0)
        trader._virtual_buy("AAPL", price=100.0, reason="test", ai_result=MagicMock(action="BUY", confidence=80))
        assert trader._positions["AAPL"]["avg_cost"] == pytest.approx(100.0)

    def test_buy_weighted_avg_cost_on_add(self):
        trader = _make_trader(cash=50_000.0)
        ai = MagicMock(action="BUY", confidence=80)
        trader._virtual_buy("AAPL", price=100.0, reason="test", ai_result=ai)  # 10 @ 100
        trader._virtual_buy("AAPL", price=120.0, reason="test", ai_result=ai)  # 10 @ 120
        expected_avg = (100.0 * 10 + 120.0 * 10) / 20
        assert trader._positions["AAPL"]["avg_cost"] == pytest.approx(expected_avg)
        assert trader._positions["AAPL"]["quantity"] == 20

    def test_buy_does_nothing_if_insufficient_cash(self):
        trader = _make_trader(cash=50.0)  # only $50
        trader._virtual_buy("AAPL", price=100.0, reason="test", ai_result=MagicMock(action="BUY", confidence=80))
        assert "AAPL" not in trader._positions or trader._positions.get("AAPL", {}).get("quantity", 0) == 0

    def test_buy_capped_by_max_position(self):
        trader = _make_trader(cash=100_000.0, max_position=5)
        ai = MagicMock(action="BUY", confidence=80)
        trader._virtual_buy("AAPL", price=10.0, reason="test", ai_result=ai)
        assert trader._positions["AAPL"]["quantity"] == 5

    def test_buy_records_trade(self):
        trader = _make_trader(cash=10_000.0)
        trader._virtual_buy("AAPL", price=100.0, reason="test", ai_result=MagicMock(action="BUY", confidence=80))
        assert len(trader._trades) == 1
        assert trader._trades[0]["action"] == "BUY"
        assert trader._trades[0]["symbol"] == "AAPL"


class TestVirtualSell:
    def _setup_with_position(self, cash: float = 50_000.0, qty: int = 20, price: float = 100.0) -> PaperTrader:
        trader = _make_trader(cash=cash)
        trader._positions["AAPL"] = {"quantity": qty, "avg_cost": price, "realized_pnl": 0.0}
        return trader

    def test_sell_adds_cash(self):
        trader = self._setup_with_position(cash=5_000.0, qty=20, price=100.0)
        trader._virtual_sell("AAPL", price=110.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        assert trader._cash == pytest.approx(5_000.0 + 110.0 * 10)

    def test_sell_reduces_position(self):
        trader = self._setup_with_position(qty=20)
        trader._virtual_sell("AAPL", price=110.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        assert trader._positions["AAPL"]["quantity"] == 10

    def test_sell_calculates_realized_pnl(self):
        trader = self._setup_with_position(qty=10, price=100.0)
        trader._virtual_sell("AAPL", price=110.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        # (110 - 100) * 10 = 100
        assert trader._positions["AAPL"]["realized_pnl"] == pytest.approx(100.0)

    def test_sell_loss_recorded_correctly(self):
        trader = self._setup_with_position(qty=10, price=100.0)
        trader._virtual_sell("AAPL", price=90.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        # (90 - 100) * 10 = -100
        assert trader._positions["AAPL"]["realized_pnl"] == pytest.approx(-100.0)

    def test_sell_full_position_zeroes_avg_cost(self):
        trader = self._setup_with_position(qty=10, price=100.0)
        trader._virtual_sell("AAPL", price=110.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        assert trader._positions["AAPL"]["quantity"] == 0
        assert trader._positions["AAPL"]["avg_cost"] == 0.0

    def test_sell_no_position_does_nothing(self):
        trader = _make_trader(cash=10_000.0)
        before_cash = trader._cash
        trader._virtual_sell("AAPL", price=100.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        assert trader._cash == before_cash
        assert len(trader._trades) == 0

    def test_sell_records_trade(self):
        trader = self._setup_with_position(qty=10)
        trader._virtual_sell("AAPL", price=110.0, reason="test", ai_result=MagicMock(action="SELL", confidence=70))
        assert len(trader._trades) == 1
        assert trader._trades[0]["action"] == "SELL"


class TestEquityAndState:
    def test_record_equity_captures_cash_plus_positions(self):
        trader = _make_trader(cash=5_000.0)
        trader._positions["AAPL"] = {"quantity": 10, "avg_cost": 100.0, "realized_pnl": 0.0}
        trader._last_prices["AAPL"] = 110.0
        trader._record_equity()
        last = trader._equity_curve[-1]
        assert last["equity"] == pytest.approx(5_000.0 + 10 * 110.0)

    def test_equity_curve_capped_at_500(self):
        trader = _make_trader()
        for _ in range(510):
            trader._record_equity()
        assert len(trader._equity_curve) <= 500

    def test_get_state_returns_paper_state(self):
        trader = _make_trader(cash=10_000.0)
        state = trader.get_state(running=True)
        assert isinstance(state, PaperState)
        assert state.running is True
        assert state.cash == pytest.approx(10_000.0)

    def test_return_pct_positive_on_profit(self):
        trader = _make_trader(cash=11_000.0)
        trader._starting_cash = 10_000.0
        state = trader.get_state()
        assert state.return_pct == pytest.approx(10.0)

    def test_return_pct_negative_on_loss(self):
        trader = _make_trader(cash=9_000.0)
        trader._starting_cash = 10_000.0
        state = trader.get_state()
        assert state.return_pct == pytest.approx(-10.0)

    def test_get_state_includes_positions(self):
        trader = _make_trader(cash=5_000.0)
        trader._positions["AAPL"] = {"quantity": 10, "avg_cost": 100.0, "realized_pnl": 50.0}
        trader._last_prices["AAPL"] = 105.0
        state = trader.get_state()
        assert "AAPL" in state.positions
        pos = state.positions["AAPL"]
        assert pos.quantity == 10
        assert pos.avg_cost == pytest.approx(100.0)
        assert pos.unrealized_pnl == pytest.approx((105.0 - 100.0) * 10)

    def test_trades_limited_to_50_in_state(self):
        trader = _make_trader()
        for i in range(60):
            trader._trades.append({"time": "t", "symbol": "X", "action": "BUY",
                                   "quantity": 1, "price": 100.0, "cash_after": 9900.0,
                                   "realized_pnl": None, "reason": "test"})
        state = trader.get_state()
        assert len(state.trades) == 50
