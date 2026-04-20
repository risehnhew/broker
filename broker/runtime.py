from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from broker.config import Settings
from broker.paper_trader import PaperState, PaperTrader
from broker.trader import PortfolioSnapshot, PositionInfo, Trader


@dataclass
class RuntimeState:
    running: bool = False
    started_at: str = ""
    stopped_at: str = ""
    last_error: str = ""
    symbols: list[str] | None = None
    stock_universe: list[str] | None = None
    positions: dict[str, PositionInfo] = field(default_factory=dict)
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    cash_balance: float = 0.0


class TraderRuntime:
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._trader: Trader | None = None
        self._state = RuntimeState()

    def start(self, settings: Settings) -> RuntimeState:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._snapshot_locked()

            self._trader = Trader(settings)
            self._state = RuntimeState(
                running=True,
                started_at=datetime.now().isoformat(timespec="seconds"),
                last_error="",
                symbols=list(settings.symbols),
                stock_universe=list(settings.stock_universe),
            )
            self._thread = threading.Thread(target=self._run, name="broker-live-runtime", daemon=True)
            self._thread.start()
            self.logger.info("已从面板启动自动交易线程")
            return self._snapshot_locked()

    def stop(self) -> RuntimeState:
        with self._lock:
            trader = self._trader
            thread = self._thread
        if trader is not None:
            trader.stop()
        if thread and thread.is_alive():
            thread.join(timeout=5)

        with self._lock:
            self._state.running = False
            self._state.stopped_at = datetime.now().isoformat(timespec="seconds")
            self.logger.info("已从面板停止自动交易线程")
            return self._snapshot_locked()

    def snapshot(self) -> RuntimeState:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> RuntimeState:
        portfolio = self._trader.get_portfolio_snapshot() if self._trader else None
        return RuntimeState(
            running=self._state.running,
            started_at=self._state.started_at,
            stopped_at=self._state.stopped_at,
            last_error=self._state.last_error,
            symbols=list(self._state.symbols or []),
            stock_universe=list(self._state.stock_universe or []),
            positions=portfolio.positions if portfolio else {},
            total_realized_pnl=portfolio.total_realized_pnl if portfolio else 0.0,
            total_unrealized_pnl=portfolio.total_unrealized_pnl if portfolio else 0.0,
            cash_balance=portfolio.cash_balance if portfolio else 0.0,
        )

    def _run(self) -> None:
        trader: Trader | None
        with self._lock:
            trader = self._trader
        if trader is None:
            return

        try:
            trader.run_forever()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("自动交易线程异常退出: %s", exc)
            with self._lock:
                self._state.last_error = str(exc)
        finally:
            with self._lock:
                self._state.running = False
                self._state.stopped_at = datetime.now().isoformat(timespec="seconds")


class PaperRuntime:
    """Manages the PaperTrader background thread."""

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._trader: PaperTrader | None = None

    def start(self, settings: Settings, starting_cash: float = 10000.0) -> PaperState:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._snapshot_locked()
            self._trader = PaperTrader(settings, starting_cash)
            self._thread = threading.Thread(
                target=self._run, name="paper-trader", daemon=True
            )
            self._thread.start()
            self.logger.info("沙盘交易线程已启动，初始资金 $%.2f", starting_cash)
            return self._snapshot_locked()

    def stop(self) -> PaperState:
        with self._lock:
            trader = self._trader
            thread = self._thread
        if trader is not None:
            trader.stop()
        if thread and thread.is_alive():
            thread.join(timeout=5)
        with self._lock:
            return self._snapshot_locked(running=False)

    def reset(self) -> PaperState:
        self.stop()
        with self._lock:
            self._trader = None
            self._thread = None
        return PaperState()

    def snapshot(self) -> PaperState:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self, running: bool | None = None) -> PaperState:
        if self._trader is None:
            return PaperState()
        is_running = running if running is not None else (
            self._thread is not None and self._thread.is_alive()
        )
        return self._trader.get_state(running=is_running)

    def _run(self) -> None:
        with self._lock:
            trader = self._trader
        if trader is None:
            return
        try:
            trader.run_forever()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("沙盘交易线程异常退出: %s", exc)
