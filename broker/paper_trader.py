from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from broker.ai_analysis import AIAnalyzer
from broker.config import Settings
from broker.decision import DecisionEngine
from broker.ib_client import IBClient
from broker.models import AnalysisSnapshot
from broker.risk import RiskManager
from broker.selector import AISymbolSelector, SymbolCandidate
from broker.time_utils import parse_bar_time

_PAPER_CLIENT_ID = 1003


@dataclass
class PaperPosition:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class PaperState:
    running: bool = False
    started_at: str = ""
    stopped_at: str = ""
    last_error: str = ""
    starting_cash: float = 10000.0
    cash: float = 10000.0
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 10000.0
    return_pct: float = 0.0
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    stock_universe: list[str] = field(default_factory=list)
    cycle_count: int = 0
    last_run_at: str = ""
    status_msg: str = ""
    last_cycle_decisions: list[dict[str, Any]] = field(default_factory=list)
    market_phase: str = ""  # market / pre_market / after_hours / closed


class PaperTrader:
    """Paper-trading engine: same AI pipeline as Trader but virtual fills only."""

    def __init__(self, settings: Settings, starting_cash: float = 10000.0) -> None:
        self.settings = settings
        self.logger = logging.getLogger("PaperTrader")
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._starting_cash = starting_cash
        self._cash = starting_cash
        self._positions: dict[str, dict[str, Any]] = {}
        self._last_prices: dict[str, float] = {}
        self._equity_curve: deque[dict[str, Any]] = deque(
            [{"time": datetime.now().isoformat(timespec="seconds"), "equity": starting_cash}],
            maxlen=500,
        )
        self._trades: deque[dict[str, Any]] = deque(maxlen=50)
        self._started_at = ""
        self._stopped_at = ""
        self._last_error = ""
        self._cycle_count = 0
        self._last_run_at = ""
        self._status_msg = ""
        self._last_cycle_decisions: list[dict[str, Any]] = []
        self._current_cycle_decisions: list[dict[str, Any]] = []

        self.client = IBClient()
        self.decision_engine = DecisionEngine(settings)
        self.risk_manager = RiskManager(settings)
        self.risk_state = self.risk_manager.initial_state(1.0)
        ai = (
            AIAnalyzer(settings.ai_base_url, settings.ai_api_key, settings.ai_model)
            if settings.enable_ai_analysis and settings.ai_api_key
            else None
        )
        self.symbol_selector = AISymbolSelector(settings, ai)

    def stop(self) -> None:
        self._stop_event.set()

    def _get_market_phase(self) -> tuple[str, int]:
        """Returns (phase_name, poll_interval_seconds).

        Phase: market → 60s (盘中，每60s分析)
                pre_market → 300s (盘前，每5分钟)
                after_hours → 300s (盘后，每5分钟)
                closed → 600s (周末/休市，每10分钟)
        """
        try:
            et = datetime.now(ZoneInfo("America/New_York"))
            dow = et.weekday()
            hour, minute = et.hour, et.minute
            total_min = hour * 60 + minute
            # Market: Mon-Fri, 9:30-16:00 ET
            if dow < 5 and 9 * 60 + 30 <= total_min < 16 * 60:
                return ("market", 60)
            elif dow < 5 and 4 * 60 <= total_min < 9 * 60 + 30:
                return ("pre_market", 300)
            elif dow < 5 and 16 * 60 <= total_min < 20 * 60:
                return ("after_hours", 300)
            else:
                return ("closed", 600)
        except Exception:  # noqa: BLE001
            return ("market", 60)

    def run_forever(self) -> None:
        self._stop_event.clear()
        with self._lock:
            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._stopped_at = ""
            self._last_error = ""
            self._status_msg = "正在连接 IBKR..."

        try:
            self.client.connect_and_start_any(
                host=self.settings.ib_host,
                ports=self.settings.ib_port_candidates,
                client_id=_PAPER_CLIENT_ID,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[PaperTrader] IBKR连接失败: %s", exc)
            with self._lock:
                self._last_error = f"IBKR连接失败: {exc}"
                self._status_msg = "连接失败"
                self._stopped_at = datetime.now().isoformat(timespec="seconds")
            return

        with self._lock:
            self._status_msg = "已连接，首轮分析中..."
        self.logger.info("[PaperTrader] 沙盘交易启动，初始资金 $%.2f，股票池 %s",
                         self._starting_cash, ", ".join(self.settings.stock_universe))
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    self._status_msg = "交易轮次执行中..."
                phase, poll_interval = self._get_market_phase()
                ai_enabled = phase == "market"  # Only call MiniMax during market hours
                with self._lock:
                    self._market_phase = phase
                try:
                    self._run_once(ai_enabled=ai_enabled)
                    self._record_equity()
                    phase_label = {"market": "盘中", "pre_market": "盘前", "after_hours": "盘后", "closed": "休市"}.get(phase, phase)
                    with self._lock:
                        self._cycle_count += 1
                        self._last_run_at = datetime.now().isoformat(timespec="seconds")
                        interval_desc = f"{poll_interval // 60}分钟" if poll_interval >= 60 else f"{poll_interval}秒"
                        self._status_msg = f"【{phase_label}】等待下一轮（每 {interval_desc} 一次）"
                        self._last_error = ""
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("[PaperTrader] 本轮异常: %s", exc)
                    with self._lock:
                        self._last_error = str(exc)
                        self._status_msg = "本轮异常，等待重试"
                if self._stop_event.wait(poll_interval):
                    break
        finally:
            self.client.disconnect_and_stop()
            with self._lock:
                self._stopped_at = datetime.now().isoformat(timespec="seconds")
                self._status_msg = "已停止"
            self.logger.info("[PaperTrader] 沙盘交易已停止")

    def _set_status(self, msg: str) -> None:
        with self._lock:
            self._status_msg = msg

    def _run_once(self, ai_enabled: bool = True) -> None:
        self.logger.info("[PaperTrader] 开始交易轮次")
        self._current_cycle_decisions = []
        with self._lock:
            current_positions = {
                s: info["quantity"]
                for s, info in self._positions.items()
                if info["quantity"] > 0
            }

        cached_candidates: dict[str, SymbolCandidate] = {}
        selected_symbols = set(self.settings.symbols)
        universe = list(self.settings.stock_universe or self.settings.symbols)
        total = len(universe)

        if self.settings.enable_ai_stock_selection:
            if ai_enabled:
                self._set_status(f"AI 选股分析中，共 {total} 只股票…")
            else:
                self._set_status(f"数据收集中（非盘中，跳过 AI 分析）…")
            try:
                def _progress(idx: int, n: int, sym: str) -> None:
                    prefix = "AI 选股" if ai_enabled else "获取数据"
                    self._set_status(f"{prefix} {idx}/{n}: {sym}…")

                selection = self.symbol_selector.select(self.client, current_positions, on_progress=_progress, ai_enabled=ai_enabled)
                cached_candidates.update(selection.candidates)
                selected_symbols = set(selection.selected_symbols)
                if ai_enabled:
                    self._set_status(
                        f"AI 选股完成，入选 {len(selected_symbols)} 只: "
                        + (", ".join(sorted(selected_symbols)) or "无")
                    )
                else:
                    self._set_status(
                        f"数据收集完成（非盘中，跳过 AI）"
                    )
                self.logger.info(
                    "[PaperTrader] AI选股结果: %s",
                    ", ".join(sorted(selected_symbols)) or "无",
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[PaperTrader] AI选股失败: %s", exc)
                self._set_status(f"AI 选股失败: {exc}")

        process_symbols = set(selected_symbols)
        process_symbols.update(s for s, qty in current_positions.items() if qty > 0)
        if not process_symbols:
            process_symbols.update(self.settings.symbols)

        sorted_symbols = sorted(process_symbols)
        for idx, symbol in enumerate(sorted_symbols, 1):
            self._set_status(f"处理 {idx}/{len(sorted_symbols)}: {symbol}…")
            try:
                with self._lock:
                    position = self._positions.get(symbol, {}).get("quantity", 0)
                candidate = cached_candidates.get(symbol) or self.symbol_selector.build_candidate(
                    self.client, symbol, position
                )
                with self._lock:
                    self._last_prices[symbol] = candidate.bars[-1].close
                allow_buy = (not self.settings.enable_ai_stock_selection) or (symbol in selected_symbols)
                self._process_candidate(candidate, allow_buy)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[PaperTrader] 处理 %s 失败: %s", symbol, exc)

        with self._lock:
            self._last_cycle_decisions = list(self._current_cycle_decisions)
        self.logger.info("[PaperTrader] 交易轮次结束")

    def _process_candidate(self, candidate: SymbolCandidate, allow_new_buy: bool) -> None:
        symbol = candidate.symbol
        bars = candidate.bars
        price = bars[-1].close
        signal = candidate.signal
        candle = candidate.candle
        news = candidate.news
        ai_result = candidate.ai

        with self._lock:
            position = self._positions.get(symbol, {}).get("quantity", 0)

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
        risk = self.risk_manager.evaluate(
            state=self.risk_state,
            now=parse_bar_time(bars[-1].date, self.settings.data_timezone, self.settings.market_timezone),
            equity=max(price * max(position, 0) + self._cash, 1.0),
            position=position,
            last_price=price,
            avg_cost=self._positions.get(symbol, {}).get("avg_cost"),
        )

        final_action = decision.action
        if risk.action == "FORCE_SELL" and position > 0:
            final_action = "SELL"
        elif risk.action in {"BLOCK_BUY", "BLOCK_NEW"} and final_action == "BUY":
            final_action = "HOLD"
        if not allow_new_buy and position <= 0:
            final_action = "HOLD"

        self.logger.info(
            "[PaperTrader] %s final=%s signal=%s decision=%s pos=%d price=%.2f",
            symbol, final_action, signal.action, decision.reason, position, price,
        )
        self._current_cycle_decisions.append({
            "symbol": symbol,
            "action": final_action,
            "price": round(price, 2),
            "position": position,
            "reason": decision.reason,
            "ai_action": ai_result.action,
            "ai_confidence": ai_result.confidence,
            "ai_summary": (ai_result.summary or "")[:120],
        })

        if final_action == "BUY":
            self._virtual_buy(symbol, price, decision.reason, ai_result)
        elif final_action == "SELL":
            self._virtual_sell(symbol, price, decision.reason, ai_result)

    def _virtual_buy(self, symbol: str, price: float, reason: str, ai_result: Any) -> None:
        with self._lock:
            position = self._positions.get(symbol, {}).get("quantity", 0)
            available = self.settings.max_position - position
            qty = min(self.settings.order_quantity, available)
            if qty <= 0 or self._cash < price:
                return
            qty = min(qty, int(self._cash // price))
            if qty <= 0:
                return

            cost = price * qty
            pos = self._positions.setdefault(
                symbol, {"quantity": 0, "avg_cost": 0.0, "realized_pnl": 0.0}
            )
            old_qty = pos["quantity"]
            new_qty = old_qty + qty
            pos["avg_cost"] = (pos["avg_cost"] * old_qty + cost) / new_qty
            pos["quantity"] = new_qty
            self._cash -= cost
            self._trades.append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol,
                "action": "BUY",
                "quantity": qty,
                "price": round(price, 2),
                "cash_after": round(self._cash, 2),
                "realized_pnl": None,
                "reason": f"{reason} | ai={ai_result.action}/{ai_result.confidence}",
            })
        self.logger.info(
            "[PaperTrader] ✓ BUY %s x%d @ $%.2f  cash left $%.2f", symbol, qty, price, self._cash
        )

    def _virtual_sell(self, symbol: str, price: float, reason: str, ai_result: Any) -> None:
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos or pos["quantity"] <= 0:
                return
            qty = min(self.settings.order_quantity, pos["quantity"])
            realized = (price - pos["avg_cost"]) * qty
            pos["quantity"] -= qty
            pos["realized_pnl"] += realized
            if pos["quantity"] == 0:
                pos["avg_cost"] = 0.0
            self._cash += price * qty
            self._trades.append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol,
                "action": "SELL",
                "quantity": qty,
                "price": round(price, 2),
                "cash_after": round(self._cash, 2),
                "realized_pnl": round(realized, 2),
                "reason": f"{reason} | ai={ai_result.action}/{ai_result.confidence}",
            })
        self.logger.info(
            "[PaperTrader] ✓ SELL %s x%d @ $%.2f  realized $%.2f  cash $%.2f",
            symbol, qty, price, realized, self._cash,
        )

    def _record_equity(self) -> None:
        with self._lock:
            equity = self._cash + sum(
                self._last_prices.get(s, info.get("avg_cost", 0.0)) * info["quantity"]
                for s, info in self._positions.items()
                if info["quantity"] > 0
            )
            self._equity_curve.append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "equity": round(equity, 2),
            })

    def get_state(self, running: bool = False) -> PaperState:
        with self._lock:
            positions: dict[str, PaperPosition] = {}
            unrealized = 0.0
            realized_total = 0.0

            for symbol, info in self._positions.items():
                if info["quantity"] == 0 and info["realized_pnl"] == 0.0:
                    continue
                cur_price = self._last_prices.get(symbol, info.get("avg_cost", 0.0))
                unreal = (
                    (cur_price - info["avg_cost"]) * info["quantity"]
                    if info["quantity"] > 0 and info["avg_cost"] > 0
                    else 0.0
                )
                unrealized += unreal
                realized_total += info["realized_pnl"]
                positions[symbol] = PaperPosition(
                    symbol=symbol,
                    quantity=info["quantity"],
                    avg_cost=round(info["avg_cost"], 4),
                    current_price=round(cur_price, 4),
                    unrealized_pnl=round(unreal, 2),
                    realized_pnl=round(info["realized_pnl"], 2),
                )

            equity = self._cash + sum(
                self._last_prices.get(s, info.get("avg_cost", 0.0)) * info["quantity"]
                for s, info in self._positions.items()
                if info["quantity"] > 0
            )
            return_pct = (
                (equity - self._starting_cash) / self._starting_cash * 100
                if self._starting_cash > 0
                else 0.0
            )

            return PaperState(
                running=running,
                started_at=self._started_at,
                stopped_at=self._stopped_at,
                last_error=self._last_error,
                starting_cash=self._starting_cash,
                cash=round(self._cash, 2),
                positions=positions,
                realized_pnl=round(realized_total, 2),
                unrealized_pnl=round(unrealized, 2),
                total_equity=round(equity, 2),
                return_pct=round(return_pct, 2),
                equity_curve=list(self._equity_curve),
                trades=list(self._trades),
                symbols=list(self.settings.symbols),
                stock_universe=list(self.settings.stock_universe),
                cycle_count=self._cycle_count,
                last_run_at=self._last_run_at,
                status_msg=self._status_msg,
                last_cycle_decisions=list(self._last_cycle_decisions),
                market_phase=getattr(self, '_market_phase', ''),
            )
