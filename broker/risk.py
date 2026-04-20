from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from broker.config import Settings
from broker.models import RiskDecision


@dataclass
class RiskState:
    equity_peak: float
    day_start_equity: float
    current_day: str | None = None


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def initial_state(self, starting_equity: float) -> RiskState:
        return RiskState(
            equity_peak=starting_equity,
            day_start_equity=starting_equity,
            current_day=None,
        )

    def evaluate(
        self,
        state: RiskState,
        now: datetime | None,
        equity: float,
        position: int,
        last_price: float,
        avg_cost: float | None,
    ) -> RiskDecision:
        state.equity_peak = max(state.equity_peak, equity)
        self._roll_day(state, now, equity)

        if position > 0 and avg_cost is not None:
            pnl_pct = (last_price - avg_cost) / avg_cost
            if pnl_pct <= -self.settings.stop_loss_pct:
                return RiskDecision(action="FORCE_SELL", reason="stop_loss")
            if pnl_pct >= self.settings.take_profit_pct:
                return RiskDecision(action="FORCE_SELL", reason="take_profit")

        if state.equity_peak > 0:
            drawdown = (state.equity_peak - equity) / state.equity_peak
            if drawdown >= self.settings.max_drawdown_pct:
                return RiskDecision(action="BLOCK_BUY", reason="max_drawdown")

        if state.day_start_equity > 0:
            day_loss = (state.day_start_equity - equity) / state.day_start_equity
            if day_loss >= self.settings.max_daily_loss_pct:
                if position > 0:
                    return RiskDecision(action="FORCE_SELL", reason="max_daily_loss")
                return RiskDecision(action="BLOCK_BUY", reason="max_daily_loss")

        if now is not None:
            current_time = now.time()
            if current_time < self.settings.trade_start_time or current_time > self.settings.trade_end_time:
                return RiskDecision(action="BLOCK_NEW", reason="outside_trading_hours")

        return RiskDecision(action="ALLOW", reason="ok")

    def _roll_day(self, state: RiskState, now: datetime | None, equity: float) -> None:
        if now is None:
            return
        day_key = now.date().isoformat()
        if state.current_day != day_key:
            state.current_day = day_key
            state.day_start_equity = equity
