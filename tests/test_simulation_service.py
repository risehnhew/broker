"""Tests for simulation_service helper functions."""
from __future__ import annotations

import pytest

from broker.models import (
    SimulationConfig, SimulationResult, SimulationTrade, EquityPoint,
)
from broker.simulation_service import (
    _score, _interpret_result, _serialize_result, _finalize_summary,
)


def _make_config(**kw) -> SimulationConfig:
    defaults = dict(fast_sma=5, slow_sma=20, stop_loss_pct=0.03, take_profit_pct=0.06)
    defaults.update(kw)
    return SimulationConfig(**defaults)


def _make_result(
    net_profit: float = 1000.0,
    max_drawdown: float = 0.05,
    win_rate: float = 60.0,
    round_trips: int = 5,
    trades: int = 10,
    final_equity: float = 101_000.0,
    open_position: int = 0,
) -> SimulationResult:
    return SimulationResult(
        symbol="AAPL",
        trades=trades,
        round_trips=round_trips,
        win_rate=win_rate,
        net_profit=net_profit,
        final_equity=final_equity,
        max_drawdown=max_drawdown,
        open_position=open_position,
        config=_make_config(),
        equity_curve=[
            EquityPoint("20240101 09:30:00", 100_000.0, 100.0, 0, 100_000.0),
            EquityPoint("20240101 10:00:00", 101_000.0, 101.0, 0, 101_000.0),
        ],
        trade_log=[
            SimulationTrade("AAPL", "20240101 09:30:00", "BUY", 10, 100.0, "buy", None, 10),
            SimulationTrade("AAPL", "20240101 10:00:00", "SELL", 10, 101.0, "sell", 100.0, 0),
        ],
    )


class TestScore:
    def test_higher_profit_gives_higher_score(self):
        r1 = _make_result(net_profit=1000.0, max_drawdown=0.05, win_rate=60.0)
        r2 = _make_result(net_profit=2000.0, max_drawdown=0.05, win_rate=60.0)
        assert _score(r2) > _score(r1)

    def test_lower_drawdown_gives_higher_score(self):
        r1 = _make_result(net_profit=1000.0, max_drawdown=0.15, win_rate=60.0)
        r2 = _make_result(net_profit=1000.0, max_drawdown=0.05, win_rate=60.0)
        assert _score(r2) > _score(r1)

    def test_higher_win_rate_gives_higher_score(self):
        r1 = _make_result(net_profit=1000.0, max_drawdown=0.05, win_rate=40.0)
        r2 = _make_result(net_profit=1000.0, max_drawdown=0.05, win_rate=80.0)
        assert _score(r2) > _score(r1)

    def test_score_returns_float(self):
        assert isinstance(_score(_make_result()), float)

    def test_negative_profit_reduces_score(self):
        positive = _make_result(net_profit=1000.0)
        negative = _make_result(net_profit=-1000.0)
        assert _score(positive) > _score(negative)


class TestInterpretResult:
    def test_few_round_trips_returns_warning(self):
        r = _make_result(round_trips=1)
        text = _interpret_result(r)
        assert "样本" in text or "少" in text

    def test_zero_round_trips_returns_warning(self):
        r = _make_result(round_trips=0)
        text = _interpret_result(r)
        assert text  # non-empty string

    def test_positive_profit_low_drawdown_positive_message(self):
        r = _make_result(net_profit=500.0, max_drawdown=0.03, round_trips=5)
        text = _interpret_result(r)
        assert "稳" in text or "正" in text or "盈利" in text

    def test_positive_profit_high_drawdown_caution_message(self):
        r = _make_result(net_profit=500.0, max_drawdown=0.10, round_trips=5)
        text = _interpret_result(r)
        assert text

    def test_negative_profit_weak_message(self):
        r = _make_result(net_profit=-500.0, round_trips=5)
        text = _interpret_result(r)
        assert "弱" in text or "差" in text or "偏" in text or "需要" in text

    def test_returns_string(self):
        assert isinstance(_interpret_result(_make_result()), str)


class TestSerializeResult:
    @pytest.fixture
    def serialized(self):
        return _serialize_result(_make_result())

    def test_contains_required_keys(self, serialized):
        required = {"symbol", "trades", "round_trips", "win_rate", "net_profit",
                    "final_equity", "max_drawdown_pct", "open_position",
                    "fast_sma", "slow_sma", "interpretation", "equity_curve", "trade_log"}
        assert required.issubset(serialized.keys())

    def test_symbol_correct(self, serialized):
        assert serialized["symbol"] == "AAPL"

    def test_max_drawdown_as_percentage(self, serialized):
        # max_drawdown=0.05 → max_drawdown_pct=5.0
        assert serialized["max_drawdown_pct"] == pytest.approx(5.0)

    def test_equity_curve_serialized(self, serialized):
        assert len(serialized["equity_curve"]) == 2
        point = serialized["equity_curve"][0]
        assert "timestamp" in point
        assert "equity" in point
        assert "position" in point
        assert "cash" in point

    def test_trade_log_serialized(self, serialized):
        assert len(serialized["trade_log"]) == 2
        trade = serialized["trade_log"][0]
        assert trade["action"] == "BUY"
        assert trade["quantity"] == 10
        assert trade["realized_pnl"] is None

    def test_sell_trade_pnl_serialized(self, serialized):
        sell = serialized["trade_log"][1]
        assert sell["realized_pnl"] == pytest.approx(100.0)

    def test_interpretation_is_string(self, serialized):
        assert isinstance(serialized["interpretation"], str)
        assert len(serialized["interpretation"]) > 0

    def test_net_profit_rounded(self):
        r = _make_result(net_profit=1234.5678)
        s = _serialize_result(r)
        assert s["net_profit"] == pytest.approx(1234.57, rel=1e-3)


class TestFinalizeSummary:
    def test_adds_summary_key(self):
        results = [
            _serialize_result(_make_result(net_profit=100.0, win_rate=60.0)),
            _serialize_result(_make_result(net_profit=200.0, win_rate=80.0)),
        ]
        payload = {"results": results, "errors": []}
        _finalize_summary(payload)
        assert "summary" in payload
        s = payload["summary"]
        assert "result_count" in s
        assert "error_count" in s
        assert "total_net_profit" in s
        assert "average_win_rate" in s

    def test_counts_results_and_errors(self):
        results = [_serialize_result(_make_result())]
        errors = [{"symbol": "FAIL", "message": "oops"}]
        payload = {"results": results, "errors": errors}
        _finalize_summary(payload)
        s = payload["summary"]
        assert s["result_count"] == 1
        assert s["error_count"] == 1

    def test_sums_profit(self):
        results = [
            _serialize_result(_make_result(net_profit=100.0)),
            _serialize_result(_make_result(net_profit=200.0)),
        ]
        payload = {"results": results, "errors": []}
        _finalize_summary(payload)
        assert payload["summary"]["total_net_profit"] == pytest.approx(300.0)

    def test_averages_win_rate(self):
        results = [
            _serialize_result(_make_result(win_rate=60.0)),
            _serialize_result(_make_result(win_rate=80.0)),
        ]
        payload = {"results": results, "errors": []}
        _finalize_summary(payload)
        assert payload["summary"]["average_win_rate"] == pytest.approx(70.0)

    def test_empty_results(self):
        payload = {"results": [], "errors": []}
        _finalize_summary(payload)
        s = payload["summary"]
        assert s["result_count"] == 0
        assert s["total_net_profit"] == 0.0
