from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from broker.live_log import install_memory_log_handler

DEFAULT_IB_PORTS = [7497, 4002, 7496, 4001]


def load_dotenv_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value is not None else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value is not None else default


def _get_time(name: str, default: str) -> time:
    raw = os.getenv(name, default)
    hour, minute = raw.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _get_int_list(name: str, default: str) -> list[int]:
    raw = os.getenv(name, default)
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _get_float_list(name: str, default: str) -> list[float]:
    raw = os.getenv(name, default)
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def _get_port_candidates(name: str, default: str) -> list[int]:
    raw = os.getenv(name, default).strip().upper()
    if raw == "AUTO":
        return list(DEFAULT_IB_PORTS)
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    ib_host: str
    ib_port: int
    ib_port_candidates: list[int]
    ib_client_id: int
    symbols: list[str]
    order_quantity: int
    max_position: int
    fast_sma: int
    slow_sma: int
    bar_size: str
    duration: str
    use_rth: bool
    poll_interval_seconds: int
    enable_news: bool
    news_provider_codes: list[str]
    news_max_items: int
    news_min_sentiment_to_buy: int
    enable_ai_analysis: bool
    ai_base_url: str
    ai_api_key: str | None
    ai_model: str
    ai_min_confidence: int
    enable_ai_stock_selection: bool
    stock_universe: list[str]
    max_selected_symbols: int
    ai_selection_min_confidence: int
    stop_loss_pct: float
    take_profit_pct: float
    max_drawdown_pct: float
    max_daily_loss_pct: float
    trade_start_time: time
    trade_end_time: time
    data_timezone: str
    market_timezone: str
    backtest_cash: float
    train_fast_windows: list[int]
    train_slow_windows: list[int]
    train_stop_loss_pcts: list[float]
    train_take_profit_pcts: list[float]
    session_retry_attempts: int
    session_retry_delay_seconds: int
    session_probe_duration: str
    session_probe_bar_size: str
    account: str | None
    log_level: str


def load_settings() -> Settings:
    load_dotenv_file()

    symbols = [symbol.strip().upper() for symbol in os.getenv("SYMBOLS", "AAPL").split(",") if symbol.strip()]
    if not symbols:
        raise ValueError("SYMBOLS cannot be empty")
    stock_universe = [
        symbol.strip().upper()
        for symbol in os.getenv("STOCK_UNIVERSE", ",".join(symbols)).split(",")
        if symbol.strip()
    ]
    if not stock_universe:
        raise ValueError("STOCK_UNIVERSE cannot be empty")

    ib_port_candidates = _get_port_candidates("IB_PORT", "7497")
    if not ib_port_candidates:
        raise ValueError("IB_PORT cannot be empty")

    settings = Settings(
        ib_host=os.getenv("IB_HOST", "127.0.0.1"),
        ib_port=ib_port_candidates[0],
        ib_port_candidates=ib_port_candidates,
        ib_client_id=_get_int("IB_CLIENT_ID", 1001),
        symbols=symbols,
        order_quantity=_get_int("ORDER_QUANTITY", 10),
        max_position=_get_int("MAX_POSITION", 100),
        fast_sma=_get_int("FAST_SMA", 5),
        slow_sma=_get_int("SLOW_SMA", 20),
        bar_size=os.getenv("BAR_SIZE", "5 mins"),
        duration=os.getenv("DURATION", "3 D"),
        use_rth=_get_bool("USE_RTH", True),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60),
        enable_news=_get_bool("ENABLE_NEWS", True),
        news_provider_codes=[
            item.strip().upper()
            for item in os.getenv("NEWS_PROVIDER_CODES", "BRFG").split(",")
            if item.strip()
        ],
        news_max_items=_get_int("NEWS_MAX_ITEMS", 10),
        news_min_sentiment_to_buy=_get_int("NEWS_MIN_SENTIMENT_TO_BUY", -1),
        enable_ai_analysis=_get_bool("ENABLE_AI_ANALYSIS", True),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.minimax.io/v1"),
        ai_api_key=os.getenv("AI_API_KEY") or None,
        ai_model=os.getenv("AI_MODEL", "MiniMax-M2.7-highspeed"),
        ai_min_confidence=_get_int("AI_MIN_CONFIDENCE", 60),
        enable_ai_stock_selection=_get_bool("ENABLE_AI_STOCK_SELECTION", False),
        stock_universe=stock_universe,
        max_selected_symbols=_get_int("MAX_SELECTED_SYMBOLS", min(5, len(stock_universe))),
        ai_selection_min_confidence=_get_int("AI_SELECTION_MIN_CONFIDENCE", 65),
        stop_loss_pct=_get_float("STOP_LOSS_PCT", 0.03),
        take_profit_pct=_get_float("TAKE_PROFIT_PCT", 0.06),
        max_drawdown_pct=_get_float("MAX_DRAWDOWN_PCT", 0.15),
        max_daily_loss_pct=_get_float("MAX_DAILY_LOSS_PCT", 0.03),
        trade_start_time=_get_time("TRADE_START_TIME", "09:30"),
        trade_end_time=_get_time("TRADE_END_TIME", "16:00"),
        data_timezone=os.getenv("DATA_TIMEZONE", "UTC"),
        market_timezone=os.getenv("MARKET_TIMEZONE", "America/New_York"),
        backtest_cash=_get_float("BACKTEST_CASH", 100000),
        train_fast_windows=_get_int_list("TRAIN_FAST_WINDOWS", "5,8,10"),
        train_slow_windows=_get_int_list("TRAIN_SLOW_WINDOWS", "20,30,50"),
        train_stop_loss_pcts=_get_float_list("TRAIN_STOP_LOSS_PCTS", "0.02,0.03"),
        train_take_profit_pcts=_get_float_list("TRAIN_TAKE_PROFIT_PCTS", "0.04,0.06,0.08"),
        session_retry_attempts=_get_int("SESSION_RETRY_ATTEMPTS", 3),
        session_retry_delay_seconds=_get_int("SESSION_RETRY_DELAY_SECONDS", 20),
        session_probe_duration=os.getenv("SESSION_PROBE_DURATION", "1 D"),
        session_probe_bar_size=os.getenv("SESSION_PROBE_BAR_SIZE", "1 hour"),
        account=os.getenv("ACCOUNT") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )

    if settings.fast_sma >= settings.slow_sma:
        raise ValueError("FAST_SMA must be less than SLOW_SMA")
    if settings.order_quantity <= 0:
        raise ValueError("ORDER_QUANTITY must be greater than 0")
    if settings.max_position <= 0:
        raise ValueError("MAX_POSITION must be greater than 0")
    if settings.news_max_items <= 0:
        raise ValueError("NEWS_MAX_ITEMS must be greater than 0")
    if settings.ai_min_confidence < 0 or settings.ai_min_confidence > 100:
        raise ValueError("AI_MIN_CONFIDENCE must be between 0 and 100")
    if settings.ai_selection_min_confidence < 0 or settings.ai_selection_min_confidence > 100:
        raise ValueError("AI_SELECTION_MIN_CONFIDENCE must be between 0 and 100")
    if settings.max_selected_symbols <= 0:
        raise ValueError("MAX_SELECTED_SYMBOLS must be greater than 0")
    if not 0 < settings.stop_loss_pct < 1:
        raise ValueError("STOP_LOSS_PCT must be between 0 and 1")
    if not 0 < settings.take_profit_pct < 1:
        raise ValueError("TAKE_PROFIT_PCT must be between 0 and 1")
    if not 0 < settings.max_drawdown_pct < 1:
        raise ValueError("MAX_DRAWDOWN_PCT must be between 0 and 1")
    if not 0 < settings.max_daily_loss_pct < 1:
        raise ValueError("MAX_DAILY_LOSS_PCT must be between 0 and 1")
    if settings.backtest_cash <= 0:
        raise ValueError("BACKTEST_CASH must be greater than 0")
    if not settings.data_timezone.strip():
        raise ValueError("DATA_TIMEZONE cannot be empty")
    if not settings.market_timezone.strip():
        raise ValueError("MARKET_TIMEZONE cannot be empty")
    if not settings.train_fast_windows or not settings.train_slow_windows:
        raise ValueError("Training SMA windows cannot be empty")
    if not settings.train_stop_loss_pcts or not settings.train_take_profit_pcts:
        raise ValueError("Training risk parameter sets cannot be empty")
    if settings.session_retry_attempts <= 0:
        raise ValueError("SESSION_RETRY_ATTEMPTS must be greater than 0")
    if settings.session_retry_delay_seconds < 0:
        raise ValueError("SESSION_RETRY_DELAY_SECONDS must be non-negative")

    return settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
    install_memory_log_handler(level)
    logging.getLogger("ibapi.client").setLevel(logging.WARNING)
    logging.getLogger("ibapi.wrapper").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
