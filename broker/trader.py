from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from broker.ai_analysis import AIAnalyzer
from broker.config import Settings
from broker.decision import DecisionEngine
from broker.ib_client import IBClient
from broker.models import AnalysisSnapshot
from broker.risk import RiskManager
from broker.selector import AISymbolSelector
from broker.selector import SymbolCandidate
from broker.time_utils import parse_bar_time
from broker.trade_log import append_trade

_TRADE_LOG_FILE = Path(__file__).parent / "trade_history.csv"


@dataclass
class PositionInfo:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class PortfolioSnapshot:
    positions: dict[str, PositionInfo]
    total_realized_pnl: float
    total_unrealized_pnl: float
    cash_balance: float


class Trader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = IBClient()
        self.decision_engine = DecisionEngine(settings)
        self.risk_manager = RiskManager(settings)
        self.risk_state = self.risk_manager.initial_state(1.0)
        self.ai_analyzer = (
            AIAnalyzer(settings.ai_base_url, settings.ai_api_key, settings.ai_model)
            if settings.enable_ai_analysis and settings.ai_api_key
            else None
        )
        self.symbol_selector = AISymbolSelector(settings, self.ai_analyzer)
        self._stop_event = threading.Event()
        self._portfolio_lock = threading.Lock()
        self._last_prices: dict[str, float] = {}
        self._portfolio: PortfolioSnapshot = PortfolioSnapshot(
            positions={},
            total_realized_pnl=0.0,
            total_unrealized_pnl=0.0,
            cash_balance=settings.backtest_cash,
        )

    def run_forever(self) -> None:
        self._stop_event.clear()
        self.client.connect_and_start_any(
            host=self.settings.ib_host,
            ports=self.settings.ib_port_candidates,
            client_id=self.settings.ib_client_id,
        )
        if self.settings.enable_news:
            try:
                providers = self.client.get_news_providers()
                provider_text = ", ".join(
                    f"{getattr(item, 'code', getattr(item, 'providerCode', '?'))}:{getattr(item, 'name', getattr(item, 'providerName', '?'))}"
                    for item in providers
                )
                self.logger.info("可用新闻源 %s", provider_text or "无")
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("新闻源查询失败: %s", exc)

        try:
            while not self._stop_event.is_set():
                self.run_once()
                self._update_portfolio()
                if self._stop_event.wait(self.settings.poll_interval_seconds):
                    break
        finally:
            self.client.disconnect_and_stop()

    def stop(self) -> None:
        self._stop_event.set()

    def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        with self._portfolio_lock:
            return PortfolioSnapshot(
                positions=dict(self._portfolio.positions),
                total_realized_pnl=self._portfolio.total_realized_pnl,
                total_unrealized_pnl=self._portfolio.total_unrealized_pnl,
                cash_balance=self._portfolio.cash_balance,
            )

    def _update_portfolio(self) -> None:
        """Update the internal portfolio state with current positions and P&L."""
        try:
            positions = self.client.get_positions(timeout=10.0)
            avg_costs = self.client.get_avg_costs()

            realized_pnl = self._read_realized_pnl()
            total_realized = sum(realized_pnl.values())

            positions_info: dict[str, PositionInfo] = {}
            total_unrealized = 0.0
            total_equity = self.settings.backtest_cash + total_realized

            for symbol, quantity in positions.items():
                if quantity == 0:
                    continue
                avg_cost = avg_costs.get(symbol, 0.0)
                current_price = self._last_prices.get(symbol, avg_cost)
                unrealized = (current_price - avg_cost) * quantity if avg_cost > 0 else 0.0
                total_unrealized += unrealized
                total_equity += current_price * quantity
                positions_info[symbol] = PositionInfo(
                    symbol=symbol,
                    quantity=int(quantity),
                    avg_cost=avg_cost,
                    current_price=current_price,
                    unrealized_pnl=unrealized,
                    realized_pnl=realized_pnl.get(symbol, 0.0),
                )

            cash_balance = total_equity - sum(
                pos.current_price * pos.quantity for pos in positions_info.values()
            )

            with self._portfolio_lock:
                self._portfolio.positions = positions_info
                self._portfolio.total_realized_pnl = total_realized
                self._portfolio.total_unrealized_pnl = total_unrealized
                self._portfolio.cash_balance = cash_balance

            self.logger.info(
                "持仓更新: %d 只股票，浮动盈亏 $%.2f，已实现 $%.2f，现金 $%.2f",
                len(positions_info),
                total_unrealized,
                total_realized,
                cash_balance,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("持仓更新失败: %s", exc)

    def _read_realized_pnl(self) -> dict[str, float]:
        """Read realized P&L per symbol from trade history CSV."""
        realized: dict[str, float] = {}
        if not _TRADE_LOG_FILE.exists():
            return realized
        try:
            with open(_TRADE_LOG_FILE, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("mode") != "live":
                        continue
                    pnl_str = row.get("realized_pnl", "").strip()
                    if not pnl_str or pnl_str.lower() == "none":
                        continue
                    try:
                        pnl = float(pnl_str)
                        symbol = row.get("symbol", "").strip().upper()
                        if symbol:
                            realized[symbol] = realized.get(symbol, 0.0) + pnl
                    except ValueError:
                        continue
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("读取交易记录失败: %s", exc)
        return realized

    def run_once(self) -> None:
        cycle_start = time.perf_counter()
        self.logger.info("交易轮次开始")
        positions = {symbol.upper(): int(value) for symbol, value in self.client.get_positions().items()}
        cached_candidates: dict[str, SymbolCandidate] = {}
        selected_symbols = set(self.settings.symbols)

        if self.settings.enable_ai_stock_selection:
            selection = self.symbol_selector.select(self.client, positions)
            cached_candidates.update(selection.candidates)
            selected_symbols = set(selection.selected_symbols)
            if selection.picks:
                preview = ", ".join(
                    f"{item.symbol}:{item.action}/{item.confidence}"
                    for item in selection.picks[: self.settings.max_selected_symbols]
                )
                self.logger.info(
                    "AI选股完成 market=%s picks=%s",
                    selection.market_view or "-",
                    preview,
                )
            if not selected_symbols:
                self.logger.info("AI选股本轮没有给出可开仓标的")
            for item in selection.errors:
                self.logger.warning("AI选股 %s 失败: %s", item["symbol"], item["message"])

        process_symbols = set(selected_symbols)
        process_symbols.update(symbol for symbol, quantity in positions.items() if quantity != 0)
        if not process_symbols:
            process_symbols.update(self.settings.symbols)

        for symbol in sorted(process_symbols):
            try:
                position = int(positions.get(symbol, 0))
                candidate = cached_candidates.get(symbol)
                if candidate is None:
                    candidate = self.symbol_selector.build_candidate(self.client, symbol, position)
                self._last_prices[symbol] = candidate.bars[-1].close
                allow_new_buy = (not self.settings.enable_ai_stock_selection) or (symbol in selected_symbols)
                self._process_candidate(candidate, allow_new_buy=allow_new_buy)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("处理 %s 失败: %s", symbol, exc)
        self.logger.info("交易轮次结束，用时 %.2fs", time.perf_counter() - cycle_start)

    def _process_candidate(self, candidate: SymbolCandidate, allow_new_buy: bool) -> None:
        symbol = candidate.symbol
        position = candidate.position
        bars = candidate.bars
        closes = [bar.close for bar in bars]
        signal = candidate.signal
        candle = candidate.candle
        news = candidate.news
        ai_result = candidate.ai

        snapshot = AnalysisSnapshot(
            base_action=signal.action,
            fast_sma=signal.fast_sma,
            slow_sma=signal.slow_sma,
            candle_bias=candle.bias,
            candle_score=candle.score,
            news_score=news.score,
            news_sentiment=news.sentiment,
            ai_action=ai_result.action,
            ai_confidence=ai_result.confidence,
            ai_summary=ai_result.summary,
            ai_used=ai_result.confidence > 0,
        )
        decision = self.decision_engine.decide(snapshot=snapshot, position=position)
        equity = max(1.0, bars[-1].close * max(position, 0) + 1.0)
        risk = self.risk_manager.evaluate(
            state=self.risk_state,
            now=self._parse_bar_time(bars[-1].date),
            equity=equity,
            position=position,
            last_price=bars[-1].close,
            avg_cost=None,
        )
        final_action = self._apply_risk_overlay(decision.action, risk.action, position)
        if final_action == "BUY" and not allow_new_buy:
            final_action = "HOLD"

        self.logger.info(
            "%s raw_signal=%s final_action=%s position=%s fast_sma=%.4f slow_sma=%.4f close=%.4f trend=%s k_bias=%s k_score=%s news=%s(%s) ai=%s(%s) decision=%s risk=%s",
            symbol,
            signal.action,
            final_action,
            position,
            signal.fast_sma,
            signal.slow_sma,
            closes[-1],
            candle.trend,
            candle.bias,
            candle.score,
            news.sentiment,
            news.score,
            ai_result.action,
            ai_result.confidence,
            decision.reason,
            risk.reason,
        )
        if candle.patterns:
            self.logger.info("%s K线形态: %s", symbol, ", ".join(candle.patterns))
        if news.headlines:
            self.logger.info("%s 最新新闻: %s", symbol, " | ".join(news.headlines[:3]))
        if ai_result.summary:
            self.logger.info("%s AI摘要: %s", symbol, ai_result.summary)
        if ai_result.risks:
            self.logger.info("%s AI风险: %s", symbol, " | ".join(ai_result.risks[:3]))
        if not allow_new_buy and position <= 0:
            self.logger.info("%s 未进入本轮 AI 选股名单，阻止新开仓", symbol)

        if final_action == "BUY":
            available = self.settings.max_position - position
            quantity = min(self.settings.order_quantity, available)
            if quantity > 0:
                self.client.place_market_order(symbol, "BUY", quantity)
                append_trade(
                    symbol=symbol,
                    action="BUY",
                    quantity=quantity,
                    price=closes[-1],
                    reason=f"{decision.reason} | ai={ai_result.action}/{ai_result.confidence}",
                    realized_pnl=None,
                    position_after=position + quantity,
                    equity=closes[-1] * (position + quantity),
                    mode="live",
                )
            return

        if final_action == "SELL":
            quantity = min(self.settings.order_quantity, position)
            if quantity > 0:
                self.client.place_market_order(symbol, "SELL", quantity)
                # Calculate realized P&L using avg_cost from current position
                avg_cost = self.client.get_avg_costs().get(symbol, closes[-1])
                sell_value = closes[-1] * quantity
                cost_basis = avg_cost * quantity
                realized = sell_value - cost_basis
                append_trade(
                    symbol=symbol,
                    action="SELL",
                    quantity=quantity,
                    price=closes[-1],
                    reason=f"{decision.reason} | ai={ai_result.action}/{ai_result.confidence}",
                    realized_pnl=realized,
                    position_after=position - quantity,
                    equity=closes[-1] * (position - quantity),
                    mode="live",
                )

    def _apply_risk_overlay(self, action: str, risk_action: str, position: int) -> str:
        if risk_action == "FORCE_SELL" and position > 0:
            return "SELL"
        if risk_action in {"BLOCK_BUY", "BLOCK_NEW"} and action == "BUY":
            return "HOLD"
        return action

    def _parse_bar_time(self, raw: str):
        return parse_bar_time(raw, self.settings.data_timezone, self.settings.market_timezone)
