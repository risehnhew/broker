"""Tests for trade_log.py (CSV trade history)."""
from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from broker.models import SimulationResult, SimulationTrade, EquityPoint, SimulationConfig


@pytest.fixture(autouse=True)
def tmp_log_file(tmp_path, monkeypatch):
    """Redirect trade log to a temp file for each test."""
    import broker.trade_log as tl
    log_file = tmp_path / "trade_history.csv"
    monkeypatch.setattr(tl, "_TRADE_LOG_FILE", log_file)
    return log_file


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestAppendTrade:
    def test_creates_file_with_header(self, tmp_log_file):
        from broker.trade_log import append_trade
        append_trade("AAPL", "BUY", 10, 150.0, "signal_confirmed", None, 10, 1500.0, "live")
        assert tmp_log_file.exists()
        rows = _read_csv(tmp_log_file)
        assert len(rows) == 1

    def test_row_contains_correct_values(self, tmp_log_file):
        from broker.trade_log import append_trade
        append_trade("AAPL", "BUY", 10, 150.0, "test_reason", None, 10, 1500.0, "live")
        rows = _read_csv(tmp_log_file)
        r = rows[0]
        assert r["symbol"] == "AAPL"
        assert r["action"] == "BUY"
        assert int(r["quantity"]) == 10
        assert float(r["price"]) == pytest.approx(150.0)
        assert r["reason"] == "test_reason"
        assert r["realized_pnl"] == ""  # None → empty string
        assert r["mode"] == "live"

    def test_realized_pnl_written_when_provided(self, tmp_log_file):
        from broker.trade_log import append_trade
        append_trade("AAPL", "SELL", 10, 160.0, "exit_signal", 100.0, 0, 0.0, "live")
        rows = _read_csv(tmp_log_file)
        assert float(rows[0]["realized_pnl"]) == pytest.approx(100.0)

    def test_multiple_trades_appended(self, tmp_log_file):
        from broker.trade_log import append_trade
        append_trade("AAPL", "BUY", 10, 100.0, "buy1", None, 10, 1000.0)
        append_trade("MSFT", "BUY", 5, 200.0, "buy2", None, 5, 1000.0)
        rows = _read_csv(tmp_log_file)
        assert len(rows) == 2
        assert rows[0]["symbol"] == "AAPL"
        assert rows[1]["symbol"] == "MSFT"

    def test_price_rounded_to_4_decimals(self, tmp_log_file):
        from broker.trade_log import append_trade
        append_trade("AAPL", "BUY", 1, 123.456789, "r", None, 1, 123.456789)
        rows = _read_csv(tmp_log_file)
        price = float(rows[0]["price"])
        assert price == pytest.approx(123.4568, rel=1e-4)

    def test_header_not_duplicated_on_multiple_appends(self, tmp_log_file):
        from broker.trade_log import append_trade
        for _ in range(3):
            append_trade("AAPL", "BUY", 1, 100.0, "r", None, 1, 100.0)
        rows = _read_csv(tmp_log_file)
        assert len(rows) == 3

    def test_net_profit_cumulative_stored(self, tmp_log_file):
        from broker.trade_log import append_trade
        append_trade("AAPL", "BUY", 1, 100.0, "r", None, 1, 100.0, net_profit_cumulative=250.0)
        rows = _read_csv(tmp_log_file)
        assert float(rows[0]["net_profit_cumulative"]) == pytest.approx(250.0)


class TestAppendTradesFromResult:
    def _make_result(self, trades=None) -> SimulationResult:
        cfg = SimulationConfig(fast_sma=5, slow_sma=20, stop_loss_pct=0.03, take_profit_pct=0.06)
        if trades is None:
            trades = [
                SimulationTrade("AAPL", "20240101 09:30:00", "BUY", 10, 100.0, "buy", None, 10),
                SimulationTrade("AAPL", "20240101 10:00:00", "SELL", 10, 110.0, "sell", 100.0, 0),
            ]
        return SimulationResult(
            symbol="AAPL",
            trades=len(trades),
            round_trips=1,
            win_rate=100.0,
            net_profit=100.0,
            final_equity=100_100.0,
            max_drawdown=0.0,
            open_position=0,
            config=cfg,
            equity_curve=[],
            trade_log=trades,
        )

    def test_returns_count_of_written_trades(self, tmp_log_file):
        from broker.trade_log import append_trades_from_result
        result = self._make_result()
        count = append_trades_from_result(result, mode="sim")
        assert count == 2

    def test_cumulative_pnl_accumulated(self, tmp_log_file):
        from broker.trade_log import append_trades_from_result
        result = self._make_result()
        append_trades_from_result(result, mode="sim")
        rows = _read_csv(tmp_log_file)
        # First trade (BUY, realized_pnl=None → 0 cumulative)
        assert float(rows[0]["net_profit_cumulative"]) == pytest.approx(0.0)
        # Second trade (SELL, realized_pnl=100 → cumulative=100)
        assert float(rows[1]["net_profit_cumulative"]) == pytest.approx(100.0)

    def test_mode_written_correctly(self, tmp_log_file):
        from broker.trade_log import append_trades_from_result
        append_trades_from_result(self._make_result(), mode="backtest")
        rows = _read_csv(tmp_log_file)
        assert all(r["mode"] == "backtest" for r in rows)

    def test_buy_realized_pnl_empty(self, tmp_log_file):
        from broker.trade_log import append_trades_from_result
        append_trades_from_result(self._make_result())
        rows = _read_csv(tmp_log_file)
        assert rows[0]["realized_pnl"] == ""

    def test_empty_trade_log_returns_zero(self, tmp_log_file):
        from broker.trade_log import append_trades_from_result
        result = self._make_result(trades=[])
        count = append_trades_from_result(result)
        assert count == 0
