from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from broker.analysis import KlineAnalyzer
from broker.config import Settings
from broker.decision import DecisionEngine
from broker.ib_client import HistoricalBar
from broker.models import AnalysisSnapshot
from broker.models import EquityPoint
from broker.models import SimulationConfig
from broker.models import SimulationResult
from broker.models import SimulationTrade
from broker.time_utils import parse_bar_time
from broker.risk import RiskManager
from broker.strategy import SmaCrossStrategy


class SimulationEngine:
    def __init__(self, settings: Settings, config: SimulationConfig) -> None:
        self.settings = replace(
            settings,
            fast_sma=config.fast_sma,
            slow_sma=config.slow_sma,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            enable_ai_analysis=False,
        )
        self.config = config
        self.strategy = SmaCrossStrategy(config.fast_sma, config.slow_sma)
        self.kline_analyzer = KlineAnalyzer()
        self.decision_engine = DecisionEngine(self.settings)
        self.risk_manager = RiskManager(self.settings)

    def run(self, symbol: str, bars: list[HistoricalBar]) -> SimulationResult:
        # Need at least slow_sma bars for SMA and 20 bars for candle analysis
        min_bars = max(self.config.slow_sma, 20)
        if len(bars) <= min_bars + 1:
            raise RuntimeError(f"{symbol} historical data is insufficient for simulation")

        cash = self.settings.backtest_cash
        position = 0
        avg_cost: float | None = None
        wins = 0
        losses = 0
        trade_count = 0
        risk_state = self.risk_manager.initial_state(cash)
        equity_curve: list[EquityPoint] = []
        trade_log: list[SimulationTrade] = []
        equity_peak = 0.0
        max_drawdown = 0.0

        for index in range(min_bars, len(bars)):
            window = bars[: index + 1]
            current_bar = window[-1]
            equity_before = cash + position * current_bar.close

            risk = self.risk_manager.evaluate(
                state=risk_state,
                now=self._parse_bar_time(current_bar.date),
                equity=equity_before,
                position=position,
                last_price=current_bar.close,
                avg_cost=avg_cost,
            )

            if risk.action == "FORCE_SELL" and position > 0:
                realized = (current_bar.close - (avg_cost or current_bar.close)) * position
                cash += position * current_bar.close
                trade_count += 1
                wins += int(realized >= 0)
                losses += int(realized < 0)
                trade_log.append(
                    SimulationTrade(
                        symbol=symbol,
                        timestamp=current_bar.date,
                        action="SELL",
                        quantity=position,
                        price=current_bar.close,
                        reason=risk.reason,
                        realized_pnl=realized,
                        position_after=0,
                    )
                )
                position = 0
                avg_cost = None
                self._append_equity_point(equity_curve, current_bar, cash, position)
                eq = equity_curve[-1].equity
                if eq > equity_peak:
                    equity_peak = eq
                if equity_peak > 0:
                    max_drawdown = max(max_drawdown, (equity_peak - eq) / equity_peak)
                continue

            signal = self.strategy.evaluate([bar.close for bar in window], position)
            candle = self.kline_analyzer.analyze(window[-20:])
            snapshot = AnalysisSnapshot(
                base_action=signal.action,
                fast_sma=signal.fast_sma,
                slow_sma=signal.slow_sma,
                candle_bias=candle.bias,
                candle_score=candle.score,
                news_score=0,
                news_sentiment="NEUTRAL",
                ai_action="HOLD",
                ai_confidence=0,
                ai_summary="simulation-no-ai",
                ai_used=False,
            )
            decision = self.decision_engine.decide(snapshot=snapshot, position=position)

            if risk.action in {"BLOCK_BUY", "BLOCK_NEW"} and decision.action == "BUY":
                self._append_equity_point(equity_curve, current_bar, cash, position)
                eq = equity_curve[-1].equity
                if eq > equity_peak:
                    equity_peak = eq
                if equity_peak > 0:
                    max_drawdown = max(max_drawdown, (equity_peak - eq) / equity_peak)
                continue

            if decision.action == "BUY":
                qty = min(
                    self.settings.order_quantity,
                    self.settings.max_position - position,
                    int(cash // current_bar.close),
                )
                if qty > 0:
                    cash -= qty * current_bar.close
                    total_cost = (avg_cost or 0.0) * position + qty * current_bar.close
                    position += qty
                    avg_cost = total_cost / position
                    trade_count += 1
                    trade_log.append(
                        SimulationTrade(
                            symbol=symbol,
                            timestamp=current_bar.date,
                            action="BUY",
                            quantity=qty,
                            price=current_bar.close,
                            reason=decision.reason,
                            realized_pnl=None,
                            position_after=position,
                        )
                    )

            elif decision.action == "SELL" and position > 0:
                qty = min(self.settings.order_quantity, position)
                realized = (current_bar.close - (avg_cost or current_bar.close)) * qty
                cash += qty * current_bar.close
                position -= qty
                trade_count += 1
                trade_log.append(
                    SimulationTrade(
                        symbol=symbol,
                        timestamp=current_bar.date,
                        action="SELL",
                        quantity=qty,
                        price=current_bar.close,
                        reason=decision.reason,
                        realized_pnl=realized,
                        position_after=position,
                    )
                )
                if position == 0:
                    avg_cost = None
                    wins += int(realized >= 0)
                    losses += int(realized < 0)

            self._append_equity_point(equity_curve, current_bar, cash, position)
            eq = equity_curve[-1].equity
            if eq > equity_peak:
                equity_peak = eq
            if equity_peak > 0:
                max_drawdown = max(max_drawdown, (equity_peak - eq) / equity_peak)

        final_equity = cash + position * bars[-1].close

        round_trips = wins + losses
        win_rate = wins / round_trips * 100 if round_trips else 0.0
        return SimulationResult(
            symbol=symbol,
            trades=trade_count,
            round_trips=round_trips,
            win_rate=win_rate,
            net_profit=final_equity - self.settings.backtest_cash,
            final_equity=final_equity,
            max_drawdown=max_drawdown,
            open_position=position,
            config=self.config,
            equity_curve=equity_curve,
            trade_log=trade_log,
        )

    def _append_equity_point(
        self,
        equity_curve: list[EquityPoint],
        current_bar: HistoricalBar,
        cash: float,
        position: int,
    ) -> None:
        equity_curve.append(
            EquityPoint(
                timestamp=current_bar.date,
                equity=cash + position * current_bar.close,
                close_price=current_bar.close,
                position=position,
                cash=cash,
            )
        )

    def _parse_bar_time(self, raw: str) -> datetime | None:
        return parse_bar_time(raw, self.settings.data_timezone, self.settings.market_timezone)
