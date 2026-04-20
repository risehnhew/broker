"""Tests for config helper functions and Settings validation."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from broker.config import (
    _get_bool, _get_int, _get_float, _get_time, _get_int_list, _get_float_list,
    load_settings,
)


# ── Helper function tests ────────────────────────────────────────────────────

class TestGetBool:
    def test_true_values(self):
        for val in ("1", "true", "True", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {"FLAG": val}):
                assert _get_bool("FLAG", False) is True

    def test_false_values(self):
        for val in ("0", "false", "no", "off", ""):
            with patch.dict(os.environ, {"FLAG": val}):
                assert _get_bool("FLAG", True) is False

    def test_missing_uses_default_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLAG", None)
            assert _get_bool("FLAG", True) is True

    def test_missing_uses_default_false(self):
        os.environ.pop("FLAG", None)
        assert _get_bool("FLAG", False) is False


class TestGetInt:
    def test_parses_integer(self):
        with patch.dict(os.environ, {"N": "42"}):
            assert _get_int("N", 0) == 42

    def test_missing_uses_default(self):
        os.environ.pop("N", None)
        assert _get_int("N", 99) == 99

    def test_negative_value(self):
        with patch.dict(os.environ, {"N": "-5"}):
            assert _get_int("N", 0) == -5


class TestGetFloat:
    def test_parses_float(self):
        with patch.dict(os.environ, {"F": "3.14"}):
            assert _get_float("F", 0.0) == pytest.approx(3.14)

    def test_missing_uses_default(self):
        os.environ.pop("F", None)
        assert _get_float("F", 1.5) == pytest.approx(1.5)


class TestGetTime:
    def test_parses_hhmm(self):
        with patch.dict(os.environ, {"T": "09:30"}):
            t = _get_time("T", "00:00")
            assert t.hour == 9
            assert t.minute == 30

    def test_default_value(self):
        os.environ.pop("T", None)
        t = _get_time("T", "16:00")
        assert t.hour == 16
        assert t.minute == 0


class TestGetIntList:
    def test_parses_comma_separated(self):
        with patch.dict(os.environ, {"L": "5,10,20"}):
            assert _get_int_list("L", "") == [5, 10, 20]

    def test_skips_empty_items(self):
        with patch.dict(os.environ, {"L": "5,,20"}):
            assert _get_int_list("L", "") == [5, 20]

    def test_uses_default_string(self):
        os.environ.pop("L", None)
        assert _get_int_list("L", "3,7") == [3, 7]


class TestGetFloatList:
    def test_parses_comma_separated(self):
        with patch.dict(os.environ, {"L": "0.02,0.05"}):
            result = _get_float_list("L", "")
            assert result == pytest.approx([0.02, 0.05])


# ── load_settings validation tests ──────────────────────────────────────────

_MINIMAL_ENV = {
    "SYMBOLS": "AAPL",
    "STOCK_UNIVERSE": "AAPL",
    "IB_PORT": "7497",
    "FAST_SMA": "5",
    "SLOW_SMA": "20",
    "STOP_LOSS_PCT": "0.03",
    "TAKE_PROFIT_PCT": "0.06",
    "MAX_DRAWDOWN_PCT": "0.15",
    "MAX_DAILY_LOSS_PCT": "0.03",
    "ORDER_QUANTITY": "10",
    "MAX_POSITION": "100",
    "NEWS_MAX_ITEMS": "10",
    "AI_MIN_CONFIDENCE": "60",
    "AI_SELECTION_MIN_CONFIDENCE": "65",
    "BACKTEST_CASH": "100000",
    "DATA_TIMEZONE": "UTC",
    "MARKET_TIMEZONE": "America/New_York",
    "TRAIN_FAST_WINDOWS": "5",
    "TRAIN_SLOW_WINDOWS": "20",
    "TRAIN_STOP_LOSS_PCTS": "0.03",
    "TRAIN_TAKE_PROFIT_PCTS": "0.06",
    "SESSION_RETRY_ATTEMPTS": "3",
    "SESSION_RETRY_DELAY_SECONDS": "5",
    "ENABLE_AI_ANALYSIS": "false",
    "ENABLE_AI_STOCK_SELECTION": "false",
    "MAX_SELECTED_SYMBOLS": "5",
    "LOG_LEVEL": "WARNING",
}


def _load(**overrides):
    env = {**_MINIMAL_ENV, **overrides}
    with patch.dict(os.environ, env, clear=True), \
         patch("broker.config.load_dotenv_file"), \
         patch("broker.config.install_memory_log_handler"):
        return load_settings()


class TestSettingsValidation:
    def test_valid_settings_load(self):
        s = _load()
        assert s.symbols == ["AAPL"]
        assert s.fast_sma == 5
        assert s.slow_sma == 20

    def test_fast_sma_must_be_less_than_slow_sma(self):
        with pytest.raises(ValueError, match="FAST_SMA"):
            _load(FAST_SMA="20", SLOW_SMA="20")

    def test_fast_sma_greater_than_slow_raises(self):
        with pytest.raises(ValueError, match="FAST_SMA"):
            _load(FAST_SMA="25", SLOW_SMA="20")

    def test_order_quantity_must_be_positive(self):
        with pytest.raises(ValueError, match="ORDER_QUANTITY"):
            _load(ORDER_QUANTITY="0")

    def test_negative_order_quantity_raises(self):
        with pytest.raises(ValueError, match="ORDER_QUANTITY"):
            _load(ORDER_QUANTITY="-1")

    def test_max_position_must_be_positive(self):
        with pytest.raises(ValueError, match="MAX_POSITION"):
            _load(MAX_POSITION="0")

    def test_stop_loss_must_be_between_0_and_1(self):
        with pytest.raises(ValueError, match="STOP_LOSS_PCT"):
            _load(STOP_LOSS_PCT="1.5")

    def test_stop_loss_zero_raises(self):
        with pytest.raises(ValueError, match="STOP_LOSS_PCT"):
            _load(STOP_LOSS_PCT="0.0")

    def test_take_profit_must_be_between_0_and_1(self):
        with pytest.raises(ValueError, match="TAKE_PROFIT_PCT"):
            _load(TAKE_PROFIT_PCT="0.0")

    def test_max_drawdown_must_be_between_0_and_1(self):
        with pytest.raises(ValueError, match="MAX_DRAWDOWN_PCT"):
            _load(MAX_DRAWDOWN_PCT="1.0")

    def test_backtest_cash_must_be_positive(self):
        with pytest.raises(ValueError, match="BACKTEST_CASH"):
            _load(BACKTEST_CASH="0")

    def test_ai_min_confidence_must_be_0_to_100(self):
        with pytest.raises(ValueError, match="AI_MIN_CONFIDENCE"):
            _load(AI_MIN_CONFIDENCE="101")

    def test_ai_min_confidence_negative_raises(self):
        with pytest.raises(ValueError, match="AI_MIN_CONFIDENCE"):
            _load(AI_MIN_CONFIDENCE="-1")

    def test_session_retry_attempts_must_be_positive(self):
        with pytest.raises(ValueError, match="SESSION_RETRY_ATTEMPTS"):
            _load(SESSION_RETRY_ATTEMPTS="0")

    def test_symbols_parsed_uppercase(self):
        s = _load(SYMBOLS="aapl,msft", STOCK_UNIVERSE="aapl,msft")
        assert s.symbols == ["AAPL", "MSFT"]

    def test_ib_port_candidates_auto(self):
        s = _load(IB_PORT="AUTO")
        assert len(s.ib_port_candidates) > 0

    def test_multiple_symbols(self):
        s = _load(SYMBOLS="AAPL,MSFT,GOOG", STOCK_UNIVERSE="AAPL,MSFT,GOOG")
        assert s.symbols == ["AAPL", "MSFT", "GOOG"]
        assert len(s.symbols) == 3
