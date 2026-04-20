from __future__ import annotations

import logging

from broker.config import Settings
from broker.models import SimulationConfig
from broker.models import SimulationResult
from broker.session_guard import connect_with_session_guard
from broker.session_guard import is_session_conflict_error
from broker.selector import AISymbolSelector
from broker.simulation import SimulationEngine
from broker.trade_log import append_trades_from_result
from broker.ui import explain_error


def _score(result: SimulationResult) -> float:
    return result.net_profit - result.max_drawdown * 10000 + result.win_rate * 10


def _interpret_result(result: SimulationResult) -> str:
    if result.round_trips <= 1:
        return "样本很少，这次结果只能说明当前这段行情里有效，不能直接推到实盘。"
    if result.net_profit > 0 and result.max_drawdown < 0.05:
        return "这组参数当前表现偏稳，收益为正且回撤较小。"
    if result.net_profit > 0:
        return "这组参数当前盈利，但回撤不低，仍要结合更长时间窗口再看。"
    return "这组参数当前回放结果偏弱，需要继续训练或换参数。"


def _serialize_result(result: SimulationResult) -> dict:
    return {
        "symbol": result.symbol,
        "trades": result.trades,
        "round_trips": result.round_trips,
        "win_rate": round(result.win_rate, 2),
        "net_profit": round(result.net_profit, 2),
        "final_equity": round(result.final_equity, 2),
        "max_drawdown_pct": round(result.max_drawdown * 100, 2),
        "open_position": result.open_position,
        "fast_sma": result.config.fast_sma,
        "slow_sma": result.config.slow_sma,
        "stop_loss_pct": round(result.config.stop_loss_pct * 100, 2),
        "take_profit_pct": round(result.config.take_profit_pct * 100, 2),
        "interpretation": _interpret_result(result),
        "equity_curve": [
            {
                "timestamp": point.timestamp,
                "equity": round(point.equity, 2),
                "close_price": round(point.close_price, 4),
                "position": point.position,
                "cash": round(point.cash, 2),
            }
            for point in result.equity_curve
        ],
        "trade_log": [
            {
                "timestamp": trade.timestamp,
                "action": trade.action,
                "quantity": trade.quantity,
                "price": round(trade.price, 4),
                "reason": trade.reason,
                "realized_pnl": None if trade.realized_pnl is None else round(trade.realized_pnl, 2),
                "position_after": trade.position_after,
            }
            for trade in result.trade_log
        ],
    }


def _build_session_payload(settings: Settings) -> dict:
    return {
        "attempts": [],
        "retry_attempts": settings.session_retry_attempts,
        "retry_delay_seconds": settings.session_retry_delay_seconds,
        "status": "idle",
    }


def _serialize_selection(result) -> dict:
    return {
        "market_view": result.market_view,
        "selected_symbols": result.selected_symbols,
        "picks": [
            {
                "rank": item.rank,
                "symbol": item.symbol,
                "score": item.score,
                "action": item.action,
                "confidence": item.confidence,
                "reason": item.reason,
                "selected": item.selected,
                "base_action": item.base_action,
                "candle_bias": item.candle_bias,
                "candle_score": item.candle_score,
                "news_sentiment": item.news_sentiment,
                "news_score": item.news_score,
                "ai_action": item.ai_action,
                "ai_confidence": item.ai_confidence,
                "close_price": round(item.close_price, 4),
                "position": item.position,
            }
            for item in result.picks
        ],
    }


def _serialize_educational_reports(candidates: dict) -> list[dict]:
    """将每只股票的 EducationalReport 序列化为 dict 列表。"""
    reports = []
    selector = AISymbolSelector.__new__(AISymbolSelector)
    for symbol, candidate in candidates.items():
        try:
            report = selector.build_educational_report(candidate)
            reports.append({
                "symbol": report.symbol,
                "steps": [
                    {
                        "step": step.step,
                        "title": step.title,
                        "content": step.content,
                        "indicators": [
                            {
                                "name": ind.name,
                                "value": ind.value,
                                "interpretation": ind.interpretation,
                                "explanation": ind.explanation,
                            }
                            for ind in step.indicators
                        ],
                        "verdict": step.verdict,
                    }
                    for step in report.steps
                ],
                "final_action": report.final_action,
                "final_confidence": report.final_confidence,
                "summary_zh": report.summary_zh,
            })
        except Exception:  # noqa: BLE001
            continue
    return reports


def _append_session_guidance(payload: dict, settings: Settings) -> None:
    if any("different ip address" in item["raw"].lower() for item in payload["errors"]):
        payload["guidance"].append(
            {
                "title": "会话冲突处理指南",
                "kind": "warning",
                "steps": [
                    "关闭别处正在使用同一账户的 TWS / IB Gateway。",
                    "退出浏览器里的 Client Portal。",
                    "退出手机上的 IBKR Mobile。",
                    "如果开了 VPN 或代理，先关闭。",
                    f"程序已自动重试 {settings.session_retry_attempts} 轮；若仍失败，请完全退出当前机器上的 IB Gateway，等待 {settings.session_retry_delay_seconds} 秒后重登。",
                    "重新登录 Paper，然后再次运行。",
                ],
            }
        )


def _finalize_summary(payload: dict) -> None:
    result_count = len(payload["results"])
    error_count = len(payload["errors"])
    total_net_profit = round(sum(item["net_profit"] for item in payload["results"]), 2) if payload["results"] else 0.0
    avg_win_rate = round(sum(item["win_rate"] for item in payload["results"]) / result_count, 2) if result_count else 0.0
    payload["summary"] = {
        "result_count": result_count,
        "error_count": error_count,
        "status": "ok" if result_count and not error_count else "partial" if result_count else "error" if error_count else "idle",
        "total_net_profit": total_net_profit,
        "average_win_rate": avg_win_rate,
    }


def run_simulation_snapshot(settings: Settings) -> dict:
    logging.getLogger("IBClient").setLevel(logging.ERROR)
    payload: dict = {
        "mode": "simulate",
        "has_run": True,
        "connected": False,
        "host": settings.ib_host,
        "port": None,
        "results": [],
        "errors": [],
        "guidance": [],
        "session": _build_session_payload(settings),
    }

    guard = connect_with_session_guard(settings, settings.ib_client_id + 200)
    payload["session"]["attempts"] = [
        {
            "attempt": item.attempt,
            "status": item.status,
            "message": explain_error(item.message),
        }
        for item in guard.attempts
    ]

    if guard.client is not None:
        payload["connected"] = True
        payload["port"] = guard.port
        payload["session"]["status"] = "ok"
        engine = SimulationEngine(
            settings,
            SimulationConfig(
                fast_sma=settings.fast_sma,
                slow_sma=settings.slow_sma,
                stop_loss_pct=settings.stop_loss_pct,
                take_profit_pct=settings.take_profit_pct,
            ),
        )

        try:
            for symbol in settings.symbols:
                try:
                    bars = guard.client.get_historical_bars(
                        symbol=symbol,
                        duration=settings.duration,
                        bar_size=settings.bar_size,
                        use_rth=settings.use_rth,
                        timeout=30.0,
                    )
                    result = engine.run(symbol, bars)
                    append_trades_from_result(result, mode="sim")
                    payload["results"].append(_serialize_result(result))
                except Exception as exc:  # noqa: BLE001
                    payload["errors"].append(
                        {
                            "symbol": symbol,
                            "message": explain_error(str(exc)),
                            "raw": str(exc),
                        }
                    )
        finally:
            guard.client.disconnect_and_stop()
    elif guard.error:
        payload["errors"].append(
            {
                "symbol": "CONNECT",
                "message": explain_error(guard.error),
                "raw": guard.error,
            }
        )
        payload["session"]["status"] = "session_conflict" if is_session_conflict_error(guard.error) else "error"

    _append_session_guidance(payload, settings)
    _finalize_summary(payload)
    return payload


def run_backtest_snapshot(settings: Settings) -> dict:
    payload = run_simulation_snapshot(settings)
    payload["mode"] = "backtest"
    return payload


def run_training_snapshot(settings: Settings) -> dict:
    logging.getLogger("IBClient").setLevel(logging.ERROR)
    payload: dict = {
        "mode": "train",
        "has_run": True,
        "connected": False,
        "host": settings.ib_host,
        "port": None,
        "results": [],
        "errors": [],
        "guidance": [],
        "session": _build_session_payload(settings),
        "training": {
            "top_configs": [],
        },
    }

    guard = connect_with_session_guard(settings, settings.ib_client_id + 300)
    payload["session"]["attempts"] = [
        {
            "attempt": item.attempt,
            "status": item.status,
            "message": explain_error(item.message),
        }
        for item in guard.attempts
    ]

    if guard.client is not None:
        payload["connected"] = True
        payload["port"] = guard.port
        payload["session"]["status"] = "ok"

        try:
            all_results: list[SimulationResult] = []
            for symbol in settings.symbols:
                try:
                    bars = guard.client.get_historical_bars(
                        symbol=symbol,
                        duration=settings.duration,
                        bar_size=settings.bar_size,
                        use_rth=settings.use_rth,
                        timeout=30.0,
                    )
                except Exception as exc:  # noqa: BLE001
                    payload["errors"].append(
                        {
                            "symbol": symbol,
                            "message": explain_error(str(exc)),
                            "raw": str(exc),
                        }
                    )
                    continue

                for fast in settings.train_fast_windows:
                    for slow in settings.train_slow_windows:
                        if fast >= slow:
                            continue
                        for stop_loss in settings.train_stop_loss_pcts:
                            for take_profit in settings.train_take_profit_pcts:
                                engine = SimulationEngine(
                                    settings,
                                    SimulationConfig(
                                        fast_sma=fast,
                                        slow_sma=slow,
                                        stop_loss_pct=stop_loss,
                                        take_profit_pct=take_profit,
                                    ),
                                )
                                all_results.append(engine.run(symbol, bars))

            ranked = sorted(all_results, key=_score, reverse=True)
            payload["training"]["top_configs"] = [
                {
                    "rank": index + 1,
                    "symbol": item.symbol,
                    "fast_sma": item.config.fast_sma,
                    "slow_sma": item.config.slow_sma,
                    "stop_loss_pct": round(item.config.stop_loss_pct * 100, 2),
                    "take_profit_pct": round(item.config.take_profit_pct * 100, 2),
                    "net_profit": round(item.net_profit, 2),
                    "win_rate": round(item.win_rate, 2),
                    "max_drawdown_pct": round(item.max_drawdown * 100, 2),
                    "trades": item.trades,
                    "interpretation": _interpret_result(item),
                }
                for index, item in enumerate(ranked[:12])
            ]
        finally:
            guard.client.disconnect_and_stop()
    elif guard.error:
        payload["errors"].append(
            {
                "symbol": "CONNECT",
                "message": explain_error(guard.error),
                "raw": guard.error,
            }
        )
        payload["session"]["status"] = "session_conflict" if is_session_conflict_error(guard.error) else "error"

    _append_session_guidance(payload, settings)
    _finalize_summary(payload)
    return payload


def run_selection_snapshot(settings: Settings) -> dict:
    logging.getLogger("IBClient").setLevel(logging.ERROR)
    payload: dict = {
        "mode": "select",
        "has_run": True,
        "connected": False,
        "host": settings.ib_host,
        "port": None,
        "results": [],
        "errors": [],
        "guidance": [],
        "session": _build_session_payload(settings),
        "selection": {
            "market_view": "",
            "selected_symbols": [],
            "picks": [],
        },
    }

    guard = connect_with_session_guard(settings, settings.ib_client_id + 400)
    payload["session"]["attempts"] = [
        {
            "attempt": item.attempt,
            "status": item.status,
            "message": explain_error(item.message),
        }
        for item in guard.attempts
    ]

    if guard.client is not None:
        payload["connected"] = True
        payload["port"] = guard.port
        payload["session"]["status"] = "ok"
        selector = AISymbolSelector(settings, None)
        if settings.enable_ai_analysis and settings.ai_api_key:
            from broker.ai_analysis import AIAnalyzer

            selector = AISymbolSelector(settings, AIAnalyzer(settings.ai_base_url, settings.ai_api_key, settings.ai_model))

        try:
            selection = selector.select(guard.client, {})
            serialized = _serialize_selection(selection)
            serialized["reports"] = _serialize_educational_reports(selection.candidates)
            payload["selection"] = serialized
            for item in selection.errors:
                payload["errors"].append(
                    {
                        "symbol": item["symbol"],
                        "message": explain_error(item["message"]),
                        "raw": item["raw"],
                    }
                )
        finally:
            guard.client.disconnect_and_stop()
    elif guard.error:
        payload["errors"].append(
            {
                "symbol": "CONNECT",
                "message": explain_error(guard.error),
                "raw": guard.error,
            }
        )
        payload["session"]["status"] = "session_conflict" if is_session_conflict_error(guard.error) else "error"

    _append_session_guidance(payload, settings)
    _finalize_summary(payload)
    return payload
