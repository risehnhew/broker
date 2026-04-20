"""Shared fixtures for the broker test suite."""
from __future__ import annotations

from datetime import time

import pytest

from broker.config import Settings
from broker.ib_client import HistoricalBar, NewsHeadline


def make_settings(**overrides) -> Settings:
    defaults = dict(
        ib_host="127.0.0.1",
        ib_port=7497,
        ib_port_candidates=[7497],
        ib_client_id=1001,
        symbols=["AAPL"],
        order_quantity=10,
        max_position=100,
        fast_sma=5,
        slow_sma=20,
        bar_size="5 mins",
        duration="3 D",
        use_rth=True,
        poll_interval_seconds=60,
        enable_news=True,
        news_provider_codes=["BRFG"],
        news_max_items=10,
        news_min_sentiment_to_buy=-1,
        enable_ai_analysis=False,
        ai_base_url="https://api.example.com",
        ai_api_key=None,
        ai_model="test-model",
        ai_min_confidence=60,
        enable_ai_stock_selection=False,
        stock_universe=["AAPL"],
        max_selected_symbols=5,
        ai_selection_min_confidence=65,
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
        max_drawdown_pct=0.15,
        max_daily_loss_pct=0.03,
        trade_start_time=time(9, 30),
        trade_end_time=time(16, 0),
        data_timezone="UTC",
        market_timezone="America/New_York",
        backtest_cash=100_000.0,
        train_fast_windows=[5, 8],
        train_slow_windows=[20, 30],
        train_stop_loss_pcts=[0.02, 0.03],
        train_take_profit_pcts=[0.04, 0.06],
        session_retry_attempts=3,
        session_retry_delay_seconds=5,
        session_probe_duration="1 D",
        session_probe_bar_size="1 hour",
        account=None,
        log_level="WARNING",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings():
    return make_settings()


def make_bar(
    close: float,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000,
    date: str = "20240101 09:30:00",
) -> HistoricalBar:
    if open_ is None:
        open_ = close
    if high is None:
        high = close * 1.005
    if low is None:
        low = close * 0.995
    return HistoricalBar(date=date, open=open_, high=high, low=low, close=close, volume=volume)


def make_bars(closes: list[float], volume: float = 1_000_000) -> list[HistoricalBar]:
    # Use 14:30 UTC = 09:30 NY so bars fall inside default trading hours after tz conversion
    return [make_bar(c, volume=volume, date=f"20240101 {14 + i // 12}:{(i * 5) % 60:02d}:00") for i, c in enumerate(closes)]


def make_headline(text: str, provider: str = "BRFG") -> NewsHeadline:
    return NewsHeadline(provider_code=provider, article_id="1", headline=text, timestamp="20240101 09:00:00")
