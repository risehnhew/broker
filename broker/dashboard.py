from __future__ import annotations

import atexit
import os
import signal
import socket
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import Body
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
import uvicorn

from broker.config import Settings
from broker.config import configure_logging
from broker.config import load_settings
from broker.live_log import clear_logs
from broker.live_log import get_recent_logs
from broker.runtime import PaperRuntime, TraderRuntime
from broker.simulation_service import run_backtest_snapshot
from broker.simulation_service import run_selection_snapshot
from broker.simulation_service import run_simulation_snapshot
from broker.simulation_service import run_training_snapshot

app = FastAPI(title="Broker Dashboard")
runtime = TraderRuntime()
paper_runtime = PaperRuntime()

_PAPER_DEFAULT_UNIVERSE = (
    "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AMD,QCOM,"
    "JPM,GS,V,MA,BAC,"
    "JNJ,LLY,UNH,"
    "HD,NKE,WMT,COST,"
    "XOM,CVX"
)


def _parse_symbol_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw).split(",")
    symbols = [str(item).strip().upper() for item in items if str(item).strip()]
    if not symbols:
        raise ValueError("股票列表不能为空。至少填写一个代码，例如 AAPL,MSFT。")
    return symbols


def _parse_int(raw: Any, name: str, minimum: int = 0) -> int:
    value = int(str(raw).strip())
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}。")
    return value


def _parse_float(raw: Any, name: str, minimum: float = 0.0) -> float:
    value = float(str(raw).strip())
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}。")
    return value


def _parse_percent(raw: Any, name: str) -> float:
    value = _parse_float(raw, name, 0.0) / 100.0
    if value <= 0 or value >= 1:
        raise ValueError(f"{name} 必须介于 0 和 100 之间。")
    return value


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_list(raw: Any, name: str) -> list[int]:
    items = [item.strip() for item in str(raw).split(",") if item.strip()]
    if not items:
        raise ValueError(f"{name} 不能为空。")
    values = [int(item) for item in items]
    if any(value <= 0 for value in values):
        raise ValueError(f"{name} 必须全部大于 0。")
    return values


def _parse_percent_list(raw: Any, name: str) -> list[float]:
    items = [item.strip() for item in str(raw).split(",") if item.strip()]
    if not items:
        raise ValueError(f"{name} 不能为空。")
    values = [float(item) / 100.0 for item in items]
    if any(value <= 0 or value >= 1 for value in values):
        raise ValueError(f"{name} 的每个值都必须介于 0 和 100 之间。")
    return values


def _format_decimal(value: float) -> str:
    text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".")


def _format_percent(value: float) -> str:
    return _format_decimal(value * 100)


def _serialize_settings(settings: Settings) -> dict[str, Any]:
    return {
        "ib_host": settings.ib_host,
        "ib_port": settings.ib_port,
        "ib_port_candidates": ",".join(str(port) for port in settings.ib_port_candidates),
        "account": settings.account or "",
        "symbols": ",".join(settings.symbols),
        "duration": settings.duration,
        "bar_size": settings.bar_size,
        "use_rth": settings.use_rth,
        "fast_sma": settings.fast_sma,
        "slow_sma": settings.slow_sma,
        "stop_loss_pct": _format_percent(settings.stop_loss_pct),
        "take_profit_pct": _format_percent(settings.take_profit_pct),
        "backtest_cash": _format_decimal(settings.backtest_cash),
        "order_quantity": settings.order_quantity,
        "max_position": settings.max_position,
        "enable_ai_stock_selection": settings.enable_ai_stock_selection,
        "stock_universe": ",".join(settings.stock_universe),
        "max_selected_symbols": settings.max_selected_symbols,
        "ai_selection_min_confidence": settings.ai_selection_min_confidence,
        "train_fast_windows": ",".join(str(value) for value in settings.train_fast_windows),
        "train_slow_windows": ",".join(str(value) for value in settings.train_slow_windows),
        "train_stop_loss_pcts": ",".join(_format_percent(value) for value in settings.train_stop_loss_pcts),
        "train_take_profit_pcts": ",".join(_format_percent(value) for value in settings.train_take_profit_pcts),
        "session_retry_attempts": settings.session_retry_attempts,
        "session_retry_delay_seconds": settings.session_retry_delay_seconds,
    }


def _serialize_runtime() -> dict[str, Any]:
    state = runtime.snapshot()
    return {
        "running": state.running,
        "started_at": state.started_at,
        "stopped_at": state.stopped_at,
        "last_error": state.last_error,
        "symbols": state.symbols or [],
        "stock_universe": state.stock_universe or [],
        "positions": {
            sym: {
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_cost": round(pos.avg_cost, 4),
                "current_price": round(pos.current_price, 4),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "unrealized_pnl_pct": round(pos.unrealized_pnl / (pos.avg_cost * pos.quantity) * 100, 2) if pos.avg_cost > 0 and pos.quantity > 0 else 0.0,
                "realized_pnl": round(pos.realized_pnl, 2),
            }
            for sym, pos in state.positions.items()
        },
        "total_realized_pnl": round(state.total_realized_pnl, 2),
        "total_unrealized_pnl": round(state.total_unrealized_pnl, 2),
        "total_pnl": round(state.total_realized_pnl + state.total_unrealized_pnl, 2),
        "cash_balance": round(state.cash_balance, 2),
    }


def _validate_settings(settings: Settings) -> None:
    if not settings.symbols:
        raise ValueError("股票列表不能为空。")
    if settings.fast_sma >= settings.slow_sma:
        raise ValueError("快均线必须小于慢均线。")
    if settings.order_quantity <= 0:
        raise ValueError("每次下单数量必须大于 0。")
    if settings.max_position <= 0:
        raise ValueError("最大持仓必须大于 0。")
    if settings.backtest_cash <= 0:
        raise ValueError("初始资金必须大于 0。")
    if not settings.stock_universe:
        raise ValueError("股票池不能为空。")
    if settings.max_selected_symbols <= 0:
        raise ValueError("AI 选股数量必须大于 0。")
    if not 0 <= settings.ai_selection_min_confidence <= 100:
        raise ValueError("AI 选股最低置信度必须介于 0 和 100 之间。")
    if not 0 < settings.stop_loss_pct < 1:
        raise ValueError("止损百分比必须介于 0 和 100 之间。")
    if not 0 < settings.take_profit_pct < 1:
        raise ValueError("止盈百分比必须介于 0 和 100 之间。")
    if settings.session_retry_attempts <= 0:
        raise ValueError("会话重试次数必须大于 0。")
    if settings.session_retry_delay_seconds < 0:
        raise ValueError("会话重试等待秒数不能小于 0。")
    if not settings.train_fast_windows or not settings.train_slow_windows:
        raise ValueError("训练均线参数不能为空。")
    if not settings.train_stop_loss_pcts or not settings.train_take_profit_pcts:
        raise ValueError("训练风控参数不能为空。")


def _merge_settings(base: Settings, overrides: dict[str, Any] | None) -> Settings:
    if not overrides:
        return base

    updates: dict[str, Any] = {}
    mapping = {
        "symbols": lambda value: _parse_symbol_list(value),
        "duration": lambda value: str(value).strip(),
        "bar_size": lambda value: str(value).strip(),
        "use_rth": _parse_bool,
        "fast_sma": lambda value: _parse_int(value, "快均线", 1),
        "slow_sma": lambda value: _parse_int(value, "慢均线", 2),
        "stop_loss_pct": lambda value: _parse_percent(value, "止损百分比"),
        "take_profit_pct": lambda value: _parse_percent(value, "止盈百分比"),
        "backtest_cash": lambda value: _parse_float(value, "初始资金", 1.0),
        "order_quantity": lambda value: _parse_int(value, "每次下单数量", 1),
        "max_position": lambda value: _parse_int(value, "最大持仓", 1),
        "enable_ai_stock_selection": _parse_bool,
        "enable_ai_analysis": _parse_bool,
        "stock_universe": lambda value: _parse_symbol_list(value),
        "max_selected_symbols": lambda value: _parse_int(value, "AI 选股数量", 1),
        "ai_selection_min_confidence": lambda value: _parse_int(value, "AI 选股最低置信度", 0),
        "train_fast_windows": lambda value: _parse_int_list(value, "训练快均线"),
        "train_slow_windows": lambda value: _parse_int_list(value, "训练慢均线"),
        "train_stop_loss_pcts": lambda value: _parse_percent_list(value, "训练止损百分比"),
        "train_take_profit_pcts": lambda value: _parse_percent_list(value, "训练止盈百分比"),
        "session_retry_attempts": lambda value: _parse_int(value, "会话重试次数", 1),
        "session_retry_delay_seconds": lambda value: _parse_int(value, "会话重试等待秒数", 0),
    }

    for key, parser in mapping.items():
        if key not in overrides:
            continue
        updates[key] = parser(overrides[key])

    settings = replace(base, **updates)
    _validate_settings(settings)
    return settings


def _probe_ib_port(host: str, ports: list[int], timeout: float = 0.3) -> int | None:
    """Return the first reachable port, or None. Fast TCP probe (no IBKR handshake)."""
    for port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((host, int(port))) == 0:
                    return int(port)
        except Exception:
            continue
    return None


def _empty_payload(settings: Settings, mode: str = "idle", has_run: bool = False) -> dict[str, Any]:
    reachable_port = _probe_ib_port(
        settings.ib_host,
        list(settings.ib_port_candidates) if settings.ib_port_candidates else [settings.ib_port],
    )
    return {
        "mode": mode,
        "has_run": has_run,
        "connected": reachable_port is not None,
        "host": settings.ib_host,
        "port": reachable_port if reachable_port is not None else (settings.ib_port_candidates[0] if settings.ib_port_candidates else settings.ib_port),
        "results": [],
        "errors": [],
        "guidance": [],
        "training": {"top_configs": []},
        "selection": {
            "market_view": "",
            "selected_symbols": [],
            "picks": [],
            "reports": [],
        },
        "session": {
            "attempts": [],
            "retry_attempts": settings.session_retry_attempts,
            "retry_delay_seconds": settings.session_retry_delay_seconds,
            "status": "idle",
        },
        "summary": {
            "result_count": 0,
            "error_count": 0,
            "status": "idle",
            "total_net_profit": 0.0,
            "average_win_rate": 0.0,
        },
        "runtime": _serialize_runtime(),
        "settings": _serialize_settings(settings),
    }


def _config_error_payload(settings: Settings, mode: str, message: str) -> dict[str, Any]:
    payload = _empty_payload(settings, mode=mode, has_run=True)
    payload["errors"] = [{"symbol": "CONFIG", "message": message, "raw": message}]
    payload["guidance"] = [
        {
            "title": "配置修正建议",
            "kind": "warning",
            "steps": [
                "先检查股票列表、股票池、均线参数、止损止盈和训练参数是否为空。",
                "快均线必须小于慢均线，例如 5 / 20。",
                "止损和止盈输入的是百分比，例如 3 表示 3%。",
                "如果要启用 AI 选股，请确认股票池不为空，且选股数量大于 0。",
                "如果你想恢复默认值，可以点击'恢复默认配置'。",
            ],
        }
    ]
    payload["summary"]["error_count"] = 1
    payload["summary"]["status"] = "error"
    return payload


def _run_mode(mode: str, runner, overrides: dict[str, Any] | None) -> JSONResponse:
    base_settings = load_settings()
    try:
        settings = _merge_settings(base_settings, overrides)
    except ValueError as exc:
        return JSONResponse(_config_error_payload(base_settings, mode, str(exc)), status_code=400)

    configure_logging(settings.log_level)
    payload = runner(settings)
    payload["runtime"] = _serialize_runtime()
    payload["settings"] = _serialize_settings(settings)
    return JSONResponse(payload)


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Broker Dashboard</title>
  <style>
    :root {
      --bg: #f3ecdf;
      --panel: #fffdf8;
      --ink: #1f2a33;
      --muted: #66707a;
      --line: #ddd4c5;
      --accent: #0b6d8b;
      --accent-soft: #e8f5fa;
      --good: #1d7a46;
      --bad: #a13d31;
      --warn: #9b6b10;
      --shadow: 0 18px 42px rgba(31, 42, 51, 0.08);
      --input-bg: #fff;
      --input-border: #d8cdbd;
      --stat-bg: #fff;
      --soft-bg: #f8f6f0;
    }
    html[data-theme="dark"] {
      --bg: #0f141a;
      --panel: #1a2028;
      --ink: #e8eef4;
      --muted: #8b97a6;
      --line: #2a3340;
      --accent: #4ec0e0;
      --accent-soft: #1a3340;
      --good: #4fd48a;
      --bad: #ff8a80;
      --warn: #ffcf70;
      --shadow: 0 18px 42px rgba(0, 0, 0, 0.4);
      --input-bg: #1a2028;
      --input-border: #2a3340;
      --stat-bg: #1a2028;
      --soft-bg: #151b22;
    }
    html[data-theme="dark"] body {
      background:
        radial-gradient(circle at top right, rgba(78,192,224,.08), transparent 28%),
        linear-gradient(180deg, #0f141a 0%, #0a0e13 100%) !important;
    }
    html[data-theme="dark"] .field input,
    html[data-theme="dark"] .field select {
      background: var(--input-bg); border-color: var(--input-border); color: var(--ink);
    }
    html[data-theme="dark"] .stat, html[data-theme="dark"] .overview-card,
    html[data-theme="dark"] .result-card, html[data-theme="dark"] .attempt-item,
    html[data-theme="dark"] .workflow-step, html[data-theme="dark"] .edu-card,
    html[data-theme="dark"] .table-wrap { background: var(--stat-bg) !important; }
    html[data-theme="dark"] .chart-box, html[data-theme="dark"] .result-note,
    html[data-theme="dark"] .selection-note, html[data-theme="dark"] .edu-indicator,
    html[data-theme="dark"] .edu-step-header { background: var(--soft-bg) !important; }
    html[data-theme="dark"] .field.checkbox { background: var(--stat-bg); border-color: var(--input-border); }
    html[data-theme="dark"] #paper-chart { background: var(--soft-bg) !important; }
    /* Connection top banner */
    .conn-banner {
      position: sticky; top: 0; z-index: 50;
      padding: 10px 18px; font-size: 13px; font-weight: 600;
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.08);
    }
    .conn-banner.bad { background: #fde8e6; color: #a13d31; border-bottom: 1px solid #f5c0bb; }
    .conn-banner.ok  { background: #e6f4ec; color: #1d7a46; border-bottom: 1px solid #b3ddc7; }
    html[data-theme="dark"] .conn-banner.bad { background: #3a1818; color: #ff8a80; border-bottom-color: #5a2c2c; }
    html[data-theme="dark"] .conn-banner.ok  { background: #143a24; color: #4fd48a; border-bottom-color: #1e5236; }
    /* Today-action card */
    .today-card {
      padding: 20px 24px; border-radius: 20px; margin-bottom: 20px;
      background: linear-gradient(135deg, var(--accent-soft) 0%, transparent 100%);
      border: 1px solid var(--line);
    }
    .today-card h3 { margin: 0 0 6px; font-size: 16px; color: var(--muted); font-weight: 600; }
    .today-card .today-msg { font-size: 22px; font-weight: 800; margin-bottom: 10px; letter-spacing: -0.01em; }
    .today-card .today-cta { display: flex; gap: 10px; flex-wrap: wrap; }
    /* Decisions filter pills */
    .filter-pills { display: inline-flex; gap: 4px; background: var(--accent-soft); padding: 3px; border-radius: 999px; }
    .filter-pill { padding: 4px 12px; border-radius: 999px; border: 0; background: transparent; font-size: 12px; font-weight: 600; cursor: pointer; color: var(--muted); }
    .filter-pill.active { background: var(--panel); color: var(--accent); box-shadow: 0 1px 3px rgba(0,0,0,.1); }
    /* Kbd shortcut hint */
    kbd {
      display: inline-block; padding: 1px 6px; font-size: 11px; font-family: Consolas, monospace;
      background: var(--soft-bg); border: 1px solid var(--line); border-radius: 4px;
      color: var(--muted); line-height: 1.4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(11,109,139,.12), transparent 28%),
        linear-gradient(180deg, #f8f3ea 0%, var(--bg) 100%);
    }
    .wrap {
      max-width: 1360px;
      margin: 0 auto;
      padding: 28px 18px 56px;
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr);
      gap: 18px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
    }
    .hero-main {
      padding: 28px;
    }
    .hero-main h1 {
      margin: 0 0 10px;
      font-size: 42px;
      line-height: 1;
      letter-spacing: -0.04em;
    }
    .hero-main p {
      margin: 0 0 20px;
      color: var(--muted);
      line-height: 1.7;
      max-width: 70ch;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
    }
    button {
      border: 0;
      cursor: pointer;
      border-radius: 999px;
      padding: 12px 18px;
      font-size: 14px;
      font-weight: 700;
    }
    button:disabled {
      opacity: .7;
      cursor: wait;
    }
    .notify-error { background: #fde8e6; color: var(--bad); border: 1px solid #f5c0bb; }
    .notify-ok { background: #e6f4ec; color: var(--good); border: 1px solid #b3ddc7; }
    .notify-info { background: var(--accent-soft); color: var(--accent); border: 1px solid #c5e4f0; }
    .primary { background: var(--accent); color: #fff; }
    .ghost { background: var(--accent-soft); color: var(--accent); }
    .tiny {
      padding: 8px 12px;
      font-size: 12px;
      border-radius: 14px;
    }
    .danger { background: #a13d31; color: #fff; }
    .tabs {
      display: flex;
      gap: 4px;
      padding: 6px;
      background: var(--accent-soft);
      border-radius: 999px;
      margin-bottom: 20px;
      overflow-x: auto;
      position: sticky;
      top: 8px;
      z-index: 10;
      backdrop-filter: blur(10px);
    }
    .tab-btn {
      flex: 1;
      min-width: fit-content;
      padding: 10px 18px;
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
      font-weight: 600;
      font-size: 14px;
      cursor: pointer;
      border: 0;
      white-space: nowrap;
      transition: all .15s;
    }
    .tab-btn:hover { color: var(--accent); }
    .tab-btn.active {
      background: var(--panel);
      color: var(--accent);
      box-shadow: 0 2px 8px rgba(0,0,0,.08);
    }
    [data-tab] { display: none; }
    [data-tab].active { display: block; animation: fade .2s ease; }
    @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

    /* Button spinner */
    .btn-spinner {
      display: inline-block;
      width: 12px;
      height: 12px;
      margin-right: 6px;
      border: 2px solid rgba(255,255,255,.4);
      border-top-color: currentColor;
      border-radius: 50%;
      animation: spin .8s linear infinite;
      vertical-align: -2px;
    }
    .ghost .btn-spinner { border-color: rgba(35,99,164,.3); border-top-color: var(--accent); }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Quick-start cards */
    .quickstart {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }
    .qs-card {
      padding: 22px;
      border-radius: 20px;
      background: var(--panel);
      border: 1px solid var(--line);
      cursor: pointer;
      transition: all .15s;
      text-align: left;
      font: inherit;
      color: inherit;
    }
    .qs-card:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,.08); border-color: var(--accent); }
    .qs-card .qs-emoji { font-size: 32px; display: block; }
    .qs-card h3 { margin: 10px 0 6px; font-size: 17px; }
    .qs-card p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
    .qs-card .qs-tag {
      display: inline-block;
      font-size: 11px;
      padding: 3px 9px;
      border-radius: 999px;
      margin-bottom: 6px;
      font-weight: 700;
    }
    .qs-card .qs-tag.rec { background: #e6f4ec; color: var(--good); }
    .qs-card .qs-tag.warn { background: #fde8e6; color: var(--bad); }
    .qs-card .qs-tag.lab { background: var(--accent-soft); color: var(--accent); }
    .warn-banner {
      padding: 14px 20px;
      background: #fde8e6;
      border: 1px solid #f5c0bb;
      border-radius: 14px;
      color: #a13d31;
      margin-bottom: 18px;
      font-size: 13.5px;
      line-height: 1.6;
    }
    .info-banner {
      padding: 14px 20px;
      background: var(--accent-soft);
      border: 1px solid #c5e4f0;
      border-radius: 14px;
      color: var(--accent);
      margin-bottom: 18px;
      font-size: 13.5px;
      line-height: 1.6;
    }
    .steps-list { line-height: 2; padding-left: 22px; margin: 8px 0; }
    .steps-list li { margin-bottom: 4px; }
    .side {
      padding: 20px;
      display: grid;
      gap: 14px;
      align-content: start;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px 16px;
      background: #fff;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 8px;
    }
    .value {
      font-size: 24px;
      font-weight: 800;
    }
    .value.small {
      font-size: 18px;
    }
    .section {
      padding: 22px;
      margin-bottom: 18px;
    }
    .section h2 {
      margin: 0 0 12px;
      font-size: 22px;
    }
    .sub {
      color: var(--muted);
      line-height: 1.7;
      margin-bottom: 16px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
    }
    .badge.ok { color: var(--good); background: #e7f6ed; }
    .badge.off { color: var(--bad); background: #fbe9e5; }
    .badge.info { color: var(--accent); background: var(--accent-soft); }
    .badge.warn { color: var(--warn); background: #fff1cc; }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .grid-2 {
      display: grid;
      grid-template-columns: 1.1fr .9fr;
      gap: 18px;
    }
    .controls {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }
    .field {
      display: grid;
      gap: 8px;
    }
    .field label {
      font-size: 13px;
      font-weight: 700;
    }
    .field input, .field select {
      width: 100%;
      border: 1px solid #d8cdbd;
      border-radius: 14px;
      background: #fff;
      color: var(--ink);
      padding: 11px 12px;
      font-size: 14px;
    }
    .field.checkbox {
      align-content: end;
    }
    .field.checkbox label {
      display: flex;
      align-items: center;
      gap: 10px;
      border: 1px solid #d8cdbd;
      border-radius: 14px;
      background: #fff;
      padding: 11px 12px;
      font-weight: 600;
    }
    .field.checkbox input {
      width: 18px;
      height: 18px;
      padding: 0;
    }
    .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }
    .control-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 16px;
    }
    .results {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
    }
    .result-card {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: #fff;
      padding: 16px;
    }
    .result-card h3 {
      margin: 0 0 10px;
      font-size: 20px;
    }
    .metric {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px dashed #e7e0d4;
      font-size: 14px;
    }
    .metric:last-child {
      border-bottom: 0;
    }
    .result-note, .selection-note {
      margin-top: 12px;
      padding: 12px;
      border-radius: 14px;
      background: #f8f6f0;
      color: var(--muted);
      line-height: 1.65;
      font-size: 14px;
    }
    .chart-box {
      margin-top: 14px;
      padding: 12px;
      border-radius: 14px;
      background: #f8f6f0;
      border: 1px solid #ebe3d7;
    }
    .chart-title {
      margin-bottom: 8px;
      font-size: 13px;
      color: var(--muted);
    }
    svg.sparkline {
      width: 100%;
      height: 120px;
      display: block;
    }
    .table-wrap {
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: #fff;
    }
    .trade-table, .train-table, .selection-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    .trade-table th, .trade-table td, .train-table th, .train-table td, .selection-table th, .selection-table td {
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid #ece4d9;
      vertical-align: top;
      white-space: nowrap;
    }
    .trade-table td.reason, .train-table td.note, .selection-table td.reason {
      white-space: normal;
      min-width: 240px;
    }
    .error-list, .guide-list, .attempt-list {
      display: grid;
      gap: 12px;
    }
    .error-item {
      border-left: 4px solid var(--bad);
      background: #fff6f4;
      border-radius: 14px;
      padding: 14px 16px;
    }
    .guide-item {
      border-left: 4px solid var(--warn);
      background: #fff9ea;
      border-radius: 14px;
      padding: 16px 18px;
    }
    .guide-item h3 {
      margin: 0 0 10px;
      font-size: 18px;
    }
    .guide-item ol {
      margin: 0;
      padding-left: 20px;
      line-height: 1.75;
    }
    .attempt-item {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px 16px;
      background: #fff;
    }
    .attempt-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      font-weight: 700;
    }
    .empty {
      color: var(--muted);
      font-style: italic;
      padding: 8px 0;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    .overview-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }
    .overview-card {
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: #fff;
    }
    .overview-value {
      font-size: 24px;
      font-weight: 800;
      margin-bottom: 8px;
    }
    .overview-note {
      color: var(--muted);
      line-height: 1.65;
      font-size: 14px;
    }
    .workflow {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .workflow-step {
      padding: 14px 16px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: #fff;
    }
    .workflow-step.active {
      border-color: var(--accent);
      background: #eef8fb;
    }
    .workflow-step.done {
      border-color: #c8e7d2;
      background: #eef9f2;
    }
    .workflow-kicker {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .workflow-title {
      font-weight: 800;
      margin-bottom: 6px;
    }
    .workflow-copy {
      color: var(--muted);
      line-height: 1.6;
      font-size: 13px;
    }
    .live-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-top: 18px;
      padding: 14px 16px;
      border-radius: 18px;
      background: #f7f3ea;
      border: 1px solid #e7dece;
    }
    .live-row label {
      display: inline-flex;
      gap: 10px;
      align-items: center;
      font-weight: 700;
    }
    .live-row input {
      width: 18px;
      height: 18px;
    }
    .mini-pill {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      background: #fff;
      border: 1px solid var(--line);
      font-size: 12px;
      font-weight: 700;
    }
    .log-panel {
      background: #1c252b;
      color: #edf3f6;
      border-radius: 18px;
      padding: 14px;
      min-height: 280px;
      max-height: 420px;
      overflow: auto;
      font-family: Consolas, "SFMono-Regular", monospace;
      font-size: 12px;
      line-height: 1.65;
    }
    .log-line {
      padding: 6px 0;
      border-bottom: 1px solid rgba(255,255,255,.08);
      white-space: pre-wrap;
      word-break: break-word;
    }
    .log-line:last-child {
      border-bottom: 0;
    }
    .log-meta {
      color: #90a4ae;
      margin-right: 8px;
    }
    .log-level-info { color: #90caf9; }
    .log-level-warning { color: #ffcc80; }
    .log-level-error { color: #ef9a9a; }
    .toolbar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 12px;
    }
    .toolbar-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    @media (max-width: 1180px) {
      .hero, .grid-2, .split, .stats, .controls, .overview-grid, .workflow {
        grid-template-columns: 1fr;
      }
      .live-row {
        flex-direction: column;
        align-items: flex-start;
      }
    }
    .edu-reports {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(480px, 1fr));
      gap: 18px;
    }
    .edu-card {
      border: 1px solid var(--line);
      border-radius: 20px;
      background: #fff;
      overflow: hidden;
    }
    .edu-card-header {
      padding: 14px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--line);
    }
    .edu-card-header h3 {
      margin: 0;
      font-size: 18px;
    }
    .edu-step {
      border-bottom: 1px solid #f0e8de;
    }
    .edu-step:last-child {
      border-bottom: 0;
    }
    .edu-step-header {
      padding: 12px 16px;
      display: flex;
      align-items: flex-start;
      gap: 12px;
      cursor: pointer;
      background: #faf8f4;
    }
    .edu-step-header:hover {
      background: #f5f0e8;
    }
    .edu-step-num {
      min-width: 26px;
      height: 26px;
      border-radius: 50%;
      background: var(--accent);
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .edu-step-title {
      flex: 1;
      font-weight: 700;
      font-size: 14px;
    }
    .edu-verdict-badge {
      font-size: 12px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 999px;
    }
    .edu-verdict-buy { background: #e7f6ed; color: var(--good); }
    .edu-verdict-sell { background: #fbe9e5; color: var(--bad); }
    .edu-verdict-hold { background: #f0f0f0; color: var(--muted); }
    .edu-step-body {
      padding: 0 16px 14px;
      display: none;
    }
    .edu-step-body.open {
      display: block;
    }
    .edu-step-content {
      font-size: 14px;
      line-height: 1.7;
      color: var(--ink);
      margin-bottom: 12px;
      padding-top: 4px;
    }
    .edu-indicators {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .edu-indicator {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 8px 12px;
      background: #faf8f4;
      font-size: 12px;
      cursor: help;
      max-width: 200px;
    }
    .edu-indicator-name {
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 2px;
    }
    .edu-indicator-value {
      font-size: 13px;
      font-weight: 600;
      color: var(--ink);
      margin-bottom: 4px;
    }
    .edu-indicator-interp {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }
    .edu-tooltip {
      display: none;
      position: absolute;
      background: var(--ink);
      color: #fff;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 12px;
      line-height: 1.6;
      max-width: 260px;
      z-index: 100;
      box-shadow: 0 8px 24px rgba(0,0,0,0.2);
    }
    .edu-indicator:hover .edu-tooltip {
      display: block;
    }
    .edu-card-summary {
      padding: 10px 16px;
      background: #f0f5f9;
      font-size: 13px;
      color: var(--muted);
      line-height: 1.65;
      border-top: 1px solid var(--line);
    }
  </style>
</head>
<body>
  <div id="conn-banner" class="conn-banner bad" style="display:none;">
    <span id="conn-banner-msg">⚠️ IBKR 未连接 —— 请先启动 IB Gateway 或 TWS（端口 4002/7497）</span>
    <button class="ghost tiny" id="conn-banner-diag" type="button" style="background:rgba(255,255,255,.5);">诊断</button>
  </div>
  <div class="wrap">
    <section class="hero">
      <div class="card hero-main">
        <h1>Broker Workbench</h1>
        <p>AI 辅助美股交易控制台。建议顺序:<b>沙盘观察</b> → <b>策略实验</b> → <b>实盘</b>。</p>
        <div id="notify-bar" style="display:none;margin-top:12px;padding:10px 16px;border-radius:12px;font-size:13px;line-height:1.5;"></div>
      </div>
      <div class="card side">
        <div class="stat">
          <div class="label">连接状态</div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <div class="value" id="conn-state">未检测</div>
            <div id="status-badge"></div>
          </div>
          <div class="value small" id="conn-host-port" style="margin-top:4px;color:var(--muted);"></div>
        </div>
        <div class="stat">
          <div class="label">账户</div>
          <div class="value small" id="account-state">-</div>
        </div>
        <div class="stat">
          <div class="label">模式 / 上次运行</div>
          <div class="value small" id="mode-state">—</div>
          <div style="font-size:12px;color:var(--muted);margin-top:2px;" id="run-state"></div>
        </div>
        <div class="stat">
          <div class="label">系统状态</div>
          <div class="value small" id="hero-status">—</div>
          <div style="font-size:12px;color:var(--muted);margin-top:2px;" id="hero-status-note"></div>
        </div>
        <div class="stat">
          <div class="label">推荐下一步</div>
          <div style="font-size:13px;font-weight:600;" id="next-action-title">先运行 AI 选股</div>
          <div style="font-size:12px;color:var(--muted);margin-top:2px;" id="next-action-note"></div>
        </div>
        <div class="stat" style="display:flex;align-items:center;justify-content:space-between;padding:10px 16px;">
          <label for="auto-refresh-toggle" style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;">
            <input id="auto-refresh-toggle" type="checkbox"> 自动刷新
          </label>
          <span class="mini-pill" id="last-update-pill">尚未刷新</span>
        </div>
        <div class="stat" id="diagnose-panel" style="display:none;">
          <div class="label">连接诊断</div>
          <div id="diagnose-results" style="font-size:12px;line-height:1.6;margin-top:4px;"></div>
        </div>
        <button class="ghost tiny" id="diagnose-btn" style="display:none;width:100%;">诊断连接</button>
      </div>
    </section>
    <!-- hidden JS anchors (referenced by renderOverview / renderSummary) -->
    <div style="display:none;">
      <span id="selected-symbols-count"></span>
      <span id="selected-symbols-note"></span>
      <span id="current-blocker"></span>
      <span id="current-blocker-note"></span>
      <div id="workflow-steps"></div>
      <span id="summary-results"></span>
      <span id="summary-errors"></span>
      <span id="summary-profit"></span>
      <span id="summary-winrate"></span>
    </div>

    <!-- ── Tab Navigation ─────────────────────────────────── -->
    <nav class="tabs" id="main-tabs">
      <button class="tab-btn" data-tab-target="home" title="快捷键 1">🏠 首页</button>
      <button class="tab-btn" data-tab-target="paper" title="快捷键 2">🎮 沙盘</button>
      <button class="tab-btn" data-tab-target="lab" title="快捷键 3">🧪 实验</button>
      <button class="tab-btn" data-tab-target="live" title="快捷键 4">💰 实盘</button>
      <button class="tab-btn" data-tab-target="settings" title="快捷键 5">⚙️ 参数</button>
      <button class="tab-btn" data-tab-target="logs" title="快捷键 6">📜 日志</button>
      <button class="tab-btn" id="theme-toggle" title="切换明暗主题" style="flex:0 0 auto;min-width:auto;padding:10px 14px;">🌙</button>
    </nav>

    <!-- ── Home Tab ───────────────────────────────────────── -->
    <div data-tab="home" class="today-card" id="today-card">
      <h3>📅 今天（<span id="today-date">—</span> · <span id="today-market">—</span>）</h3>
      <div class="today-msg" id="today-msg">加载中…</div>
      <div class="today-cta" id="today-cta"></div>
    </div>
    <section class="card section" data-tab="home" style="padding:24px;">
      <h2 style="margin:0 0 4px;">从哪里开始?</h2>
      <div class="sub" style="margin:0 0 18px;">选一个入口。不确定的话,从「沙盘」开始。</div>
      <div class="quickstart">
        <button class="qs-card" data-goto="paper" type="button">
          <span class="qs-tag rec">👍 新手推荐</span>
          <span class="qs-emoji">🎮</span>
          <h3>沙盘观察</h3>
          <p>给 AI 一个虚拟 $10,000,让它自动选股下单。用真实报价虚拟撮合,看策略真实表现,不影响任何真实账户。</p>
        </button>
        <button class="qs-card" data-goto="lab" type="button">
          <span class="qs-tag lab">🧪 调参</span>
          <span class="qs-emoji">🧪</span>
          <h3>策略实验</h3>
          <p>用历史数据跑 AI 选股、模拟、回测或训练。找到好参数再应用到沙盘或实盘。</p>
        </button>
        <button class="qs-card" data-goto="live" type="button">
          <span class="qs-tag warn">⚠️ 真钱</span>
          <span class="qs-emoji">💰</span>
          <h3>实盘自动交易</h3>
          <p>连接 IB Gateway 真实账户,AI 自动下单。沙盘跑出稳定正收益后再来。</p>
        </button>
      </div>
    </section>
    <section class="card section" data-tab="home">
      <h2>工作原理</h2>
      <ol class="steps-list">
        <li><b>股票池</b> — 你提供 10-25 只候选股票,覆盖多个行业。</li>
        <li><b>AI 选股</b> — MiniMax 分析 K 线形态、RSI、成交量、近期新闻,挑出今天值得关注的标的。</li>
        <li><b>下单决策</b> — SMA 交叉信号 + 止损/止盈规则 + AI 置信度,共同决定<b>买 / 卖 / 持有</b>。</li>
        <li><b>观察</b> — 沙盘账户用真实报价虚拟撮合,累计收益曲线、胜率、最大回撤实时更新。</li>
      </ol>
      <div class="info-banner">
        💡 建议流程:先在「沙盘」运行 1-3 个交易日,观察收益和胜率。策略表现稳定后,去「实验」调参验证,最后才考虑切到「实盘」。
      </div>
    </section>

    <!-- ── 沙盘账户 ───────────────────────────────────────── -->
    <section class="card section" id="paper-section" data-tab="paper">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:16px;">
        <div>
          <h2 style="margin:0 0 4px;">沙盘账户 · 虚拟 AI 交易</h2>
          <div class="sub" style="margin:0;">用虚拟本金让 AI 全自动交易，观察真实收益曲线。不影响真实账户。</div>
        </div>
        <div id="paper-status-badge" style="align-self:center;"></div>
      </div>

      <!-- Config row -->
      <div class="controls" style="grid-template-columns:140px 1fr auto auto auto;margin-bottom:18px;">
        <div class="field">
          <label for="paper-capital">初始本金 ($)</label>
          <input id="paper-capital" type="number" min="1000" step="1000" value="10000">
        </div>
        <div class="field">
          <label for="paper-universe">股票池（AI 自动挑选）</label>
          <input id="paper-universe" placeholder="AAPL,MSFT,NVDA,...">
          <div class="hint">留空则使用上方「AI 股票池」。建议 10-25 只，覆盖多个行业。</div>
        </div>
        <div class="field" style="justify-content:flex-end;">
          <label>&nbsp;</label>
          <button class="primary" id="paper-start-btn" style="background:#1d7a46;">启动沙盘</button>
        </div>
        <div class="field" style="justify-content:flex-end;">
          <label>&nbsp;</label>
          <button class="ghost" id="paper-stop-btn">暂停</button>
        </div>
        <div class="field" style="justify-content:flex-end;">
          <label>&nbsp;</label>
          <button class="ghost" id="paper-reset-btn">重置</button>
        </div>
      </div>

      <!-- Stats row -->
      <div class="stats" style="grid-template-columns:repeat(5,minmax(0,1fr));margin-bottom:16px;">
        <div class="stat"><div class="label">初始本金</div><div class="value" id="paper-starting">$10,000</div></div>
        <div class="stat"><div class="label">当前净值</div><div class="value" id="paper-equity" style="font-size:20px;">$—</div></div>
        <div class="stat"><div class="label">总收益</div><div class="value" id="paper-return">$—</div></div>
        <div class="stat"><div class="label">收益率</div><div class="value" id="paper-return-pct">—</div></div>
        <div class="stat"><div class="label">可用现金</div><div class="value" id="paper-cash">$—</div></div>
      </div>

      <!-- Equity sparkline -->
      <div id="paper-chart" style="background:#f8f3ea;border-radius:14px;padding:10px 16px;margin-bottom:16px;min-height:60px;"></div>

      <!-- Positions + Trades -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;">
        <div>
          <div style="font-weight:700;margin-bottom:8px;font-size:14px;">持仓</div>
          <table class="trade-table" style="width:100%;">
            <thead><tr><th>股票</th><th>数量</th><th>成本</th><th>现价</th><th>浮盈</th></tr></thead>
            <tbody id="paper-positions-body"></tbody>
          </table>
          <div class="empty" id="paper-positions-empty">暂无持仓 —— AI 还没买入任何股票</div>
        </div>
        <div>
          <div style="font-weight:700;margin-bottom:8px;font-size:14px;">最近交易</div>
          <table class="trade-table" style="width:100%;font-size:12px;">
            <thead><tr><th>时间</th><th>股票</th><th>操作</th><th>数量</th><th>价格</th><th>盈亏</th></tr></thead>
            <tbody id="paper-trades-body"></tbody>
          </table>
          <div class="empty" id="paper-trades-empty">暂无交易记录 —— 周末或盘前市场休市，周一开盘后 AI 才会出手</div>
        </div>
      </div>

      <!-- Status / progress bar -->
      <div id="paper-status-bar" style="margin-top:10px;padding:8px 14px;border-radius:10px;font-size:13px;display:none;"></div>
      <div id="paper-error" style="display:none;margin-top:6px;padding:8px 14px;border-radius:10px;font-size:13px;background:#fde8e6;color:#a13d31;border:1px solid #f5c0bb;"></div>

      <!-- Last cycle decisions -->
      <div id="paper-decisions-wrap" style="margin-top:14px;display:none;">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:8px;">
          <div style="font-weight:700;font-size:14px;">本轮分析结论 <span id="paper-decisions-count" class="tiny" style="font-weight:400;color:var(--muted);"></span></div>
          <div class="filter-pills" id="paper-decisions-filter">
            <button class="filter-pill" data-filter="actionable">🎯 只看买/卖</button>
            <button class="filter-pill active" data-filter="all">全部</button>
          </div>
        </div>
        <div id="paper-decisions-note" class="tiny" style="color:var(--muted);margin-bottom:6px;display:none;">
          💡 <b>low_ai_confidence</b> = AI 置信度 &lt; 决策阈值 30（周末数据弱，阈值已从 60 调低）
        </div>
        <table class="trade-table" style="width:100%;font-size:12px;">
          <thead><tr><th>股票</th><th>决定</th><th>持仓</th><th>现价</th><th>AI 看法</th><th>说明</th></tr></thead>
          <tbody id="paper-decisions-body"></tbody>
        </table>
        <div class="empty" id="paper-decisions-empty" style="display:none;">本轮没有买/卖动作 —— 全部 HOLD（点"全部"查看详情）</div>
      </div>
    </section>
    <!-- ── 沙盘账户 END ─────────────────────────────────── -->

    <!-- ── 实盘 ───────────────────────────────────────────── -->
    <section class="card section" data-tab="live">
      <div class="warn-banner">
        ⚠️ 这里会真实下单。强烈建议先在「沙盘」跑至少 1-3 个交易日,观察有稳定正收益后再来。
      </div>
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:12px;">
        <div>
          <h2 style="margin:0 0 4px;">实盘自动交易</h2>
          <div class="sub" style="margin:0;">连接 IB Gateway 真实账户。AI 每轮自动选股 → 下单 → 止损止盈。</div>
        </div>
      </div>
      <div class="actions">
        <button class="primary" id="auto-trade-btn" style="background:#1d7a46;">AI 全自动交易</button>
        <button class="ghost" id="start-live-btn">仅启动后台线程</button>
        <button class="ghost" id="stop-live-btn">停止自动交易</button>
        <button class="ghost" id="refresh-btn">刷新状态</button>
      </div>
    </section>

    <section class="card section" id="live-portfolio-section" data-tab="live" style="display:none;">
      <h2>实时持仓与盈亏</h2>
      <div class="sub">自动交易运行期间，实时展示持仓明细和累计盈亏。</div>
      <div class="overview-grid">
        <div class="overview-card">
          <div class="label">总盈亏</div>
          <div class="overview-value" id="live-total-pnl">$0.00</div>
          <div class="overview-note" id="live-total-pnl-note">已实现 + 浮动</div>
        </div>
        <div class="overview-card">
          <div class="label">已实现盈亏</div>
          <div class="overview-value" id="live-realized-pnl">$0.00</div>
          <div class="overview-note">来自已平仓交易</div>
        </div>
        <div class="overview-card">
          <div class="label">浮动盈亏</div>
          <div class="overview-value" id="live-unrealized-pnl">$0.00</div>
          <div class="overview-note">当前持仓未实现</div>
        </div>
        <div class="overview-card">
          <div class="label">现金余额</div>
          <div class="overview-value" id="live-cash-balance">$0.00</div>
          <div class="overview-note">当前可用资金</div>
        </div>
      </div>
      <div class="table-wrap" style="margin-top:16px;">
        <table class="trade-table">
          <thead>
            <tr>
              <th>股票</th>
              <th>持仓数量</th>
              <th>成本价</th>
              <th>现价</th>
              <th>浮动盈亏</th>
              <th>盈亏%</th>
              <th>已实现盈亏</th>
            </tr>
          </thead>
          <tbody id="live-positions-body"></tbody>
        </table>
      </div>
      <div class="empty" id="live-positions-empty">暂无持仓</div>
    </section>

    <section class="card section" data-tab="logs">
      <div class="toolbar">
        <div>
          <h2 style="margin:0 0 6px;">实时日志</h2>
          <div class="sub" style="margin:0;">后台轮次、AI 选股进度、自动交易动作都在这里,不用盯终端。</div>
        </div>
        <div class="toolbar-actions">
          <button class="ghost tiny" id="refresh-logs-btn">刷新日志</button>
          <button class="ghost tiny" id="clear-logs-btn">清空日志</button>
        </div>
      </div>
      <div class="log-panel" id="log-panel"></div>
    </section>

    <section class="card section" data-tab="settings">
        <h2>运行参数</h2>
        <div class="sub">在这里直接修改运行参数，无需改 <code>.env</code>。止损/止盈填百分比（如 3 表示 3%）。</div>
        <div class="controls">
          <div class="field" style="grid-column: span 2;">
            <label for="symbols">当前跟踪股票</label>
            <input id="symbols" placeholder="AAPL,MSFT,NVDA">
            <div class="hint">自动交易时，未持仓且不在 AI 选股名单里的股票不会新开仓。</div>
          </div>
          <div class="field" style="grid-column: span 2;">
            <label for="stock-universe">AI 股票池</label>
            <input id="stock-universe" placeholder="AAPL,MSFT,NVDA,AMZN,META,GOOGL">
            <div class="hint">MiniMax 会从这个股票池里挑出本轮优先处理的股票。</div>
          </div>
          <div class="field">
            <label for="duration">历史区间</label>
            <input id="duration" placeholder="3 D">
          </div>
          <div class="field">
            <label for="bar-size">K 线周期</label>
            <input id="bar-size" placeholder="5 mins">
          </div>
          <div class="field">
            <label for="fast-sma">快均线</label>
            <input id="fast-sma" type="number" min="1">
          </div>
          <div class="field">
            <label for="slow-sma">慢均线</label>
            <input id="slow-sma" type="number" min="2">
          </div>
          <div class="field">
            <label for="stop-loss">止损 %</label>
            <input id="stop-loss" type="number" min="0.01" step="0.01">
          </div>
          <div class="field">
            <label for="take-profit">止盈 %</label>
            <input id="take-profit" type="number" min="0.01" step="0.01">
          </div>
          <div class="field">
            <label for="backtest-cash">初始资金</label>
            <input id="backtest-cash" type="number" min="1" step="100">
          </div>
          <div class="field">
            <label for="order-quantity">每次下单数量</label>
            <input id="order-quantity" type="number" min="1">
          </div>
          <div class="field">
            <label for="max-position">最大持仓</label>
            <input id="max-position" type="number" min="1">
          </div>
          <div class="field checkbox">
            <label for="use-rth"><input id="use-rth" type="checkbox"> 仅常规交易时段</label>
          </div>
          <div class="field checkbox">
            <label for="enable-ai-stock-selection"><input id="enable-ai-stock-selection" type="checkbox"> 启用 AI 自动选股</label>
          </div>
          <div class="field">
            <label for="max-selected-symbols">每轮最多选股</label>
            <input id="max-selected-symbols" type="number" min="1">
          </div>
          <div class="field">
            <label for="ai-selection-confidence">选股最低置信度</label>
            <input id="ai-selection-confidence" type="number" min="0" max="100">
          </div>
        </div>
        <details style="margin-top:18px;">
          <summary style="cursor:pointer;font-size:14px;font-weight:700;user-select:none;color:var(--muted);padding:10px 0;">▸ 高级:训练参数 / 会话重试</summary>
          <div class="controls" style="margin-top:10px;">
            <div class="field" style="grid-column: span 2;">
              <label for="train-fast">训练快均线</label>
              <input id="train-fast" placeholder="5,8,10">
              <div class="hint">逗号分隔。训练会网格搜索所有组合。</div>
            </div>
            <div class="field" style="grid-column: span 2;">
              <label for="train-slow">训练慢均线</label>
              <input id="train-slow" placeholder="20,30,50">
            </div>
            <div class="field" style="grid-column: span 2;">
              <label for="train-stop">训练止损 %</label>
              <input id="train-stop" placeholder="2,3">
            </div>
            <div class="field" style="grid-column: span 2;">
              <label for="train-take">训练止盈 %</label>
              <input id="train-take" placeholder="4,6,8">
            </div>
            <div class="field">
              <label for="session-retry-attempts">会话重试次数</label>
              <input id="session-retry-attempts" type="number" min="1">
            </div>
            <div class="field">
              <label for="session-retry-delay">重试等待秒数</label>
              <input id="session-retry-delay" type="number" min="0">
            </div>
          </div>
        </details>
        <div class="control-actions">
          <button class="ghost tiny" id="use-selection-btn">用已选股票覆盖跟踪列表</button>
          <button class="ghost tiny" id="save-draft-btn">保存当前参数</button>
          <button class="ghost tiny" id="reset-config-btn">恢复默认配置</button>
        </div>
    </section>

    <!-- ── 实验 ───────────────────────────────────────────── -->
    <section class="card section" data-tab="lab">
      <h2 style="margin:0 0 4px;">策略实验</h2>
      <div class="sub" style="margin:0 0 14px;">用真实历史数据跑策略,看参数组合表现。都是只读操作,不下任何单。</div>
      <div class="actions">
        <button class="primary" id="select-btn">🔍 AI 选股</button>
        <button class="primary" id="preview-btn">📊 选股 + 模拟</button>
        <button class="ghost" id="simulate-btn">🧪 运行模拟</button>
        <button class="ghost" id="backtest-btn">📈 运行回测</button>
        <button class="ghost" id="train-btn">🎯 参数训练</button>
      </div>
      <div class="info-banner" style="margin-top:14px;margin-bottom:0;font-size:12.5px;">
        <b>选股</b>:AI 从股票池挑出优先标的 · <b>模拟/回测</b>:按 SMA 策略跑历史数据 · <b>训练</b>:网格搜索参数组合
      </div>
    </section>

    <section class="card section" data-tab="lab">
      <h2>AI 选股</h2>
      <div class="sub">这里会显示 MiniMax 从股票池里选出的优先标的、建议动作、置信度和理由。完成后可展开「AI 决策详解」学习每步推理依据。</div>
      <div class="selection-note" id="selection-summary">还没有运行 AI 选股。</div>
      <div class="table-wrap">
        <table class="selection-table">
          <thead>
            <tr>
              <th>排名</th>
              <th>股票</th>
              <th>是否入选</th>
              <th>建议动作</th>
              <th>置信度</th>
              <th>综合分</th>
              <th>基础信号</th>
              <th>K线</th>
              <th>新闻</th>
              <th>单股 AI</th>
              <th>价格</th>
              <th>理由</th>
            </tr>
          </thead>
          <tbody id="selection-body"></tbody>
        </table>
      </div>
      <div class="empty" id="selection-empty">还没有运行 AI 选股。</div>
    </section>

    <section class="card section" id="edu-section" data-tab="lab" style="display:none;">
      <div class="toolbar">
        <div>
          <h2 style="margin:0 0 6px;">AI 决策详解</h2>
          <div class="sub" style="margin:0;">每只股票的推理过程拆解，每步都有指标解释，方便你边看边学。</div>
        </div>
        <div class="toolbar-actions">
          <button class="ghost tiny" id="toggle-edu-btn">全部展开</button>
        </div>
      </div>
      <div class="edu-reports" id="edu-reports"></div>
      <div class="empty" id="edu-empty">还没有可展示的决策详解。选股完成后来这里学习。</div>
    </section>

    <section class="card section" data-tab="lab">
      <h2>模拟 / 回测结果</h2>
      <div class="sub">这里会展示每个股票的收益、回撤、参数摘要、权益曲线和简要解读。每张卡片可看到完整资金曲线。</div>
      <div class="results" id="results"></div>
      <div class="empty" id="results-empty">还没有运行模拟或回测。</div>
    </section>

    <section class="card section" data-tab="lab">
      <div class="toolbar">
        <div>
          <h2 style="margin:0 0 6px;">逐笔交易</h2>
          <div class="sub" style="margin:0;">每次买入、卖出、止损、止盈都会记录在这里，方便复盘。逐笔查看有助于理解策略在实际行情中的表现。</div>
        </div>
        <div class="toolbar-actions">
          <button class="ghost tiny" id="export-trades-btn">导出 CSV</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="trade-table">
          <thead>
            <tr>
              <th>股票</th>
              <th>时间</th>
              <th>动作</th>
              <th>数量</th>
              <th>价格</th>
              <th>已实现盈亏</th>
              <th>原因</th>
              <th>持仓后</th>
            </tr>
          </thead>
          <tbody id="trade-body"></tbody>
        </table>
      </div>
      <div class="empty" id="trade-empty">还没有可展示的交易明细。</div>
    </section>

    <section class="card section" data-tab="lab">
      <h2>训练结果</h2>
      <div class="sub">训练会批量搜索参数组合，并按收益、回撤和胜率做排序。可以直接把优胜参数套回上方表单。排名靠前的配置可直接套用。</div>
      <div class="table-wrap">
        <table class="train-table">
          <thead>
            <tr>
              <th>排名</th>
              <th>股票</th>
              <th>均线</th>
              <th>止损 / 止盈</th>
              <th>净利润</th>
              <th>胜率</th>
              <th>回撤</th>
              <th>交易数</th>
              <th>解读</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="training-body"></tbody>
        </table>
      </div>
      <div class="empty" id="training-empty">还没有运行训练。</div>
    </section>

    <section class="card section" id="errors-section" data-tab="logs">
      <h2>错误与提示</h2>
      <div class="sub">连接失败、权限冲突和参数错误等可读报错，优先处理这里的内容。</div>
      <div class="error-list" id="errors"></div>
      <div class="empty" id="errors-empty">目前没有错误。</div>
      <details id="session-details" style="margin-top:14px;">
        <summary style="cursor:pointer;font-size:13px;color:var(--muted);user-select:none;">▸ 会话守卫 / 处理建议</summary>
        <div style="margin-top:10px;display:grid;grid-template-columns:1fr 1fr;gap:14px;">
          <div>
            <div style="font-size:13px;font-weight:600;margin-bottom:6px;">会话守卫</div>
            <div class="attempt-list" id="session-attempts"></div>
            <div class="empty" id="session-empty" style="font-size:12px;">还没有运行会话探测。</div>
          </div>
          <div>
            <div style="font-size:13px;font-weight:600;margin-bottom:6px;">处理建议</div>
            <div class="guide-list" id="guidance"></div>
            <div class="empty" id="guidance-empty" style="font-size:12px;">当前没有额外处理建议。</div>
          </div>
        </div>
      </details>
    </section>
  </div>

  <script>
    const _notifyBar = document.getElementById('notify-bar');
    let _notifyTimer = null;
    function showNotify(msg, type) {
      if (!_notifyBar) return;
      _notifyBar.textContent = msg;
      _notifyBar.className = type === 'error' ? 'notify-error' : type === 'ok' ? 'notify-ok' : 'notify-info';
      _notifyBar.style.display = 'block';
      if (_notifyTimer) clearTimeout(_notifyTimer);
      const delay = type === 'error' ? 8000 : 4000;
      _notifyTimer = setTimeout(() => { _notifyBar.style.display = 'none'; }, delay);
    }

    // ── Tab switching ────────────────────────────────────────
    const TAB_KEY = 'broker-active-tab';
    function switchTab(target) {
      if (!target) return;
      document.querySelectorAll('[data-tab]').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
      document.querySelectorAll(`[data-tab="${target}"]`).forEach(el => el.classList.add('active'));
      const btn = document.querySelector(`.tab-btn[data-tab-target="${target}"]`);
      if (btn) btn.classList.add('active');
      localStorage.setItem(TAB_KEY, target);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tabTarget));
    });
    document.querySelectorAll('[data-goto]').forEach(card => {
      card.addEventListener('click', () => switchTab(card.dataset.goto));
    });
    // Restore last tab (default: home on first visit)
    switchTab(localStorage.getItem(TAB_KEY) || 'home');

    // ── Theme (dark/light) ──
    const THEME_KEY = 'broker-theme';
    function applyTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      const btn = document.getElementById('theme-toggle');
      if (btn) btn.textContent = t === 'dark' ? '☀️' : '🌙';
      localStorage.setItem(THEME_KEY, t);
    }
    applyTheme(localStorage.getItem(THEME_KEY) || 'light');
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });

    // ── Keyboard shortcuts ──
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const map = { '1': 'home', '2': 'paper', '3': 'lab', '4': 'live', '5': 'settings', '6': 'logs' };
      if (map[e.key]) { switchTab(map[e.key]); e.preventDefault(); }
      else if (e.key === 't' || e.key === 'T') {
        const cur = document.documentElement.getAttribute('data-theme') || 'light';
        applyTheme(cur === 'dark' ? 'light' : 'dark');
      }
    });

    // ── Connection top banner ──
    const connBanner = document.getElementById('conn-banner');
    const connBannerMsg = document.getElementById('conn-banner-msg');
    document.getElementById('conn-banner-diag')?.addEventListener('click', () => {
      document.getElementById('diagnose-btn')?.click();
    });
    function updateConnBanner(connected, hostPort) {
      if (!connBanner) return;
      if (connected) {
        connBanner.className = 'conn-banner ok';
        connBanner.style.display = 'none'; // hide when OK to save screen space
        connBannerMsg.textContent = `✅ IBKR 已连接（${hostPort || ''}）`;
      } else {
        connBanner.className = 'conn-banner bad';
        connBanner.style.display = 'flex';
        connBannerMsg.textContent = `⚠️ IBKR 未连接 —— 启动 IB Gateway / TWS 后点"刷新状态"；交易按钮将被禁用。`;
      }
      // Disable trade buttons when disconnected
      document.querySelectorAll('#auto-trade-btn, #start-live-btn, #paper-start-btn').forEach(btn => {
        if (!btn) return;
        if (!connected) {
          btn.disabled = true;
          btn.title = '请先启动 IB Gateway / TWS';
          btn.style.opacity = '.5';
        } else if (btn.title === '请先启动 IB Gateway / TWS') {
          btn.disabled = false;
          btn.title = '';
          btn.style.opacity = '';
        }
      });
    }

    // ── Today-action card ──
    function updateTodayCard(statusData) {
      const msgEl = document.getElementById('today-msg');
      const ctaEl = document.getElementById('today-cta');
      const dateEl = document.getElementById('today-date');
      const marketEl = document.getElementById('today-market');
      if (!msgEl || !ctaEl) return;
      const now = new Date();
      const dow = now.getDay(); // 0=Sun 6=Sat
      const etHour = (now.getUTCHours() - 4 + 24) % 24; // approximate ET (ignores DST precision)
      const isWeekend = dow === 0 || dow === 6;
      const isMarketHours = !isWeekend && etHour >= 9 && etHour < 16;
      const isPreMarket = !isWeekend && etHour >= 4 && etHour < 9;
      const isAfterHours = !isWeekend && etHour >= 16 && etHour < 20;
      if (dateEl) dateEl.textContent = now.toISOString().substring(0, 10);
      let marketLabel = '休市';
      if (isMarketHours) marketLabel = '🟢 盘中';
      else if (isPreMarket) marketLabel = '🟡 盘前';
      else if (isAfterHours) marketLabel = '🟡 盘后';
      else if (isWeekend) marketLabel = '🔴 周末休市';
      if (marketEl) marketEl.textContent = marketLabel;

      const connected = statusData?.runtime?.connected || (statusData?.connection?.connected);
      let msg, cta;
      if (!connected) {
        msg = '先启动 IB Gateway / TWS';
        cta = [{ label: '🔌 连接诊断', tab: null, action: () => document.getElementById('diagnose-btn')?.click() }];
      } else if (isWeekend) {
        msg = '市场休市 —— 用历史数据做模拟回测最合适';
        cta = [
          { label: '🧪 去实验标签', tab: 'lab' },
          { label: '🎮 沙盘依然能跑（用历史报价）', tab: 'paper' },
        ];
      } else if (isMarketHours) {
        msg = '盘中时段 —— 让 AI 自动选股交易';
        cta = [
          { label: '🎮 启动沙盘（推荐）', tab: 'paper' },
          { label: '💰 实盘（真钱，谨慎）', tab: 'live' },
        ];
      } else if (isPreMarket) {
        msg = '盘前准备 —— 先跑一轮 AI 选股';
        cta = [{ label: '🧪 AI 选股', tab: 'lab' }];
      } else {
        msg = '盘后时段 —— 回顾今日结果或调整参数';
        cta = [
          { label: '📜 看日志', tab: 'logs' },
          { label: '⚙️ 调参数', tab: 'settings' },
        ];
      }
      msgEl.textContent = msg;
      ctaEl.innerHTML = '';
      cta.forEach((c, i) => {
        const b = document.createElement('button');
        b.className = i === 0 ? 'primary' : 'ghost';
        b.textContent = c.label;
        b.addEventListener('click', () => {
          if (c.tab) switchTab(c.tab);
          if (c.action) c.action();
        });
        ctaEl.appendChild(b);
      });
    }

    // ── Button-busy helper (shows spinner while async op runs) ──
    async function withBusy(btn, labelWhileBusy, fn) {
      if (!btn) return fn();
      const original = btn.innerHTML;
      btn.disabled = true;
      btn.dataset._origLabel = original;
      btn.innerHTML = `<span class="btn-spinner"></span>${labelWhileBusy || '处理中...'}`;
      try {
        return await fn();
      } finally {
        btn.disabled = false;
        btn.innerHTML = btn.dataset._origLabel || original;
        delete btn.dataset._origLabel;
      }
    }

    // ── Fetch with timeout (keeps UI from freezing on a hung endpoint) ──
    async function fetchJSON(url, opts = {}, timeoutMs = 15000) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        const resp = await fetch(url, { ...opts, signal: ctrl.signal });
        const text = await resp.text();
        try { return JSON.parse(text); } catch { return { error: text || `HTTP ${resp.status}` }; }
      } finally {
        clearTimeout(timer);
      }
    }

    const selectBtn = document.getElementById('select-btn');
    const autoTradeBtn = document.getElementById('auto-trade-btn');
    const previewBtn = document.getElementById('preview-btn');
    const simulateBtn = document.getElementById('simulate-btn');
    const backtestBtn = document.getElementById('backtest-btn');
    const trainBtn = document.getElementById('train-btn');
    const startLiveBtn = document.getElementById('start-live-btn');
    const stopLiveBtn = document.getElementById('stop-live-btn');
    const refreshBtn = document.getElementById('refresh-btn');
    const saveDraftBtn = document.getElementById('save-draft-btn');
    const resetConfigBtn = document.getElementById('reset-config-btn');
    const useSelectionBtn = document.getElementById('use-selection-btn');
    const refreshLogsBtn = document.getElementById('refresh-logs-btn');
    const clearLogsBtn = document.getElementById('clear-logs-btn');

    const connState = document.getElementById('conn-state');
    const connHostPort = document.getElementById('conn-host-port');
    const accountState = document.getElementById('account-state');
    const modeState = document.getElementById('mode-state');
    const runState = document.getElementById('run-state');
    const statusBadge = document.getElementById('status-badge');

    const results = document.getElementById('results');
    const resultsEmpty = document.getElementById('results-empty');
    const tradeBody = document.getElementById('trade-body');
    const tradeEmpty = document.getElementById('trade-empty');
    const trainingBody = document.getElementById('training-body');
    const trainingEmpty = document.getElementById('training-empty');
    const selectionBody = document.getElementById('selection-body');
    const selectionEmpty = document.getElementById('selection-empty');
    const selectionSummary = document.getElementById('selection-summary');
    const errors = document.getElementById('errors');
    const errorsEmpty = document.getElementById('errors-empty');
    const guidance = document.getElementById('guidance');
    const guidanceEmpty = document.getElementById('guidance-empty');
    const sessionAttempts = document.getElementById('session-attempts');
    const sessionEmpty = document.getElementById('session-empty');
    const summaryResults = document.getElementById('summary-results');
    const summaryErrors = document.getElementById('summary-errors');
    const summaryProfit = document.getElementById('summary-profit');
    const summaryWinrate = document.getElementById('summary-winrate');
    const heroStatus = document.getElementById('hero-status');
    const heroStatusNote = document.getElementById('hero-status-note');
    const nextActionTitle = document.getElementById('next-action-title');
    const nextActionNote = document.getElementById('next-action-note');
    const selectedSymbolsCount = document.getElementById('selected-symbols-count');
    const selectedSymbolsNote = document.getElementById('selected-symbols-note');
    const currentBlocker = document.getElementById('current-blocker');
    const currentBlockerNote = document.getElementById('current-blocker-note');
    const workflowSteps = document.getElementById('workflow-steps');
    const autoRefreshToggle = document.getElementById('auto-refresh-toggle');
    const lastUpdatePill = document.getElementById('last-update-pill');
    const logPanel = document.getElementById('log-panel');
    const eduReports = document.getElementById('edu-reports');
    const eduEmpty = document.getElementById('edu-empty');
    const eduSection = document.getElementById('edu-section');
    const toggleEduBtn = document.getElementById('toggle-edu-btn');
    const diagnosePanel = document.getElementById('diagnose-panel');
    const diagnoseResults = document.getElementById('diagnose-results');
    const diagnoseBtn = document.getElementById('diagnose-btn');

    const form = {
      symbols: document.getElementById('symbols'),
      stock_universe: document.getElementById('stock-universe'),
      duration: document.getElementById('duration'),
      bar_size: document.getElementById('bar-size'),
      use_rth: document.getElementById('use-rth'),
      fast_sma: document.getElementById('fast-sma'),
      slow_sma: document.getElementById('slow-sma'),
      stop_loss_pct: document.getElementById('stop-loss'),
      take_profit_pct: document.getElementById('take-profit'),
      backtest_cash: document.getElementById('backtest-cash'),
      order_quantity: document.getElementById('order-quantity'),
      max_position: document.getElementById('max-position'),
      enable_ai_stock_selection: document.getElementById('enable-ai-stock-selection'),
      max_selected_symbols: document.getElementById('max-selected-symbols'),
      ai_selection_min_confidence: document.getElementById('ai-selection-confidence'),
      train_fast_windows: document.getElementById('train-fast'),
      train_slow_windows: document.getElementById('train-slow'),
      train_stop_loss_pcts: document.getElementById('train-stop'),
      train_take_profit_pcts: document.getElementById('train-take'),
      session_retry_attempts: document.getElementById('session-retry-attempts'),
      session_retry_delay_seconds: document.getElementById('session-retry-delay')
    };

    const draftKey = 'broker-dashboard-config';
    let lastSelection = null;
    let autoRefreshTimer = null;
    let livePortfolioTimer = null;

    const livePortfolioSection = document.getElementById('live-portfolio-section');
    const liveTotalPnl = document.getElementById('live-total-pnl');
    const liveRealizedPnl = document.getElementById('live-realized-pnl');
    const liveUnrealizedPnl = document.getElementById('live-unrealized-pnl');
    const liveCashBalance = document.getElementById('live-cash-balance');
    const livePositionsBody = document.getElementById('live-positions-body');
    const livePositionsEmpty = document.getElementById('live-positions-empty');

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function metric(label, value, cls = '') {
      return '<div class="metric"><span>' + escapeHtml(label) + '</span><strong class="' + cls + '">' + escapeHtml(value) + '</strong></div>';
    }

    function sparklineSvg(points) {
      if (!points || !points.length) return '';
      const width = 320;
      const height = 120;
      const values = points.map(point => point.equity);
      const min = Math.min(...values);
      const max = Math.max(...values);
      const span = Math.max(max - min, 1);
      const coords = points.map((point, index) => {
        const x = (index / Math.max(points.length - 1, 1)) * width;
        const y = height - ((point.equity - min) / span) * height;
        return x.toFixed(2) + ',' + y.toFixed(2);
      }).join(' ');
      return `
        <div class="chart-box">
          <div class="chart-title">权益曲线</div>
          <svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
            <polyline fill="none" stroke="#0b6d8b" stroke-width="3" points="${coords}" />
          </svg>
        </div>
      `;
    }

    function resetSections() {
      if (results) results.innerHTML = '';
      if (tradeBody) tradeBody.innerHTML = '';
      if (trainingBody) trainingBody.innerHTML = '';
      if (selectionBody) selectionBody.innerHTML = '';
      if (errors) errors.innerHTML = '';
      if (guidance) guidance.innerHTML = '';
      if (sessionAttempts) sessionAttempts.innerHTML = '';
    }

    function renderSummary(data) {
      summaryResults.textContent = data.summary?.result_count ?? 0;
      summaryErrors.textContent = data.summary?.error_count ?? 0;
      summaryProfit.textContent = Number(data.summary?.total_net_profit ?? 0).toFixed(2);
      summaryWinrate.textContent = Number(data.summary?.average_win_rate ?? 0).toFixed(2) + '%';
    }

    function blockerFromData(data) {
      if (!data.connected) {
        return {
          title: '未连接',
          note: '先确认 IB Gateway 或 TWS 已启动，并刷新状态。'
        };
      }
      if (data.errors?.length) {
        const first = data.errors[0];
        return {
          title: first.symbol || '有错误',
          note: first.message || '请先处理下方错误与提示。'
        };
      }
      if (data.mode === 'idle') {
        return {
          title: '尚未开始',
          note: '还没有跑过 AI 选股、模拟、回测或训练。'
        };
      }
      return {
        title: '无',
        note: '系统现在没有明显阻塞。'
      };
    }

    function nextActionFromData(data) {
      if (!data.connected) {
        return {
          title: '先连上 IBKR',
          note: '启动 IB Gateway/TWS 后，点击刷新状态。'
        };
      }
      if (data.errors?.length) {
        return {
          title: '先处理错误',
          note: '优先看页面底部的错误与提示，再继续后续操作。'
        };
      }
      if (!data.has_run) {
        return {
          title: '先运行 AI 选股',
          note: '先看模型挑了哪些股票，再决定要不要模拟或实盘。'
        };
      }
      if (data.mode === 'select') {
        if (data.selection?.selected_symbols?.length) {
          return {
            title: '用入选股票跑模拟',
            note: '先点"用已选股票覆盖跟踪列表"，再跑模拟更直观。'
          };
        }
        return {
          title: '调整股票池或阈值',
          note: '当前没有入选股票，建议放宽置信度或扩大股票池。'
        };
      }
      if (data.mode === 'simulate' || data.mode === 'backtest') {
        return {
          title: '看结果和交易明细',
          note: '先看收益曲线与逐笔交易，再决定是否训练或实盘。'
        };
      }
      if (data.mode === 'train' && data.training?.top_configs?.length) {
        return {
          title: '套用训练结果',
          note: '训练完成后，先套用参数，再跑一轮模拟验证。'
        };
      }
      return {
        title: '继续刷新观察',
        note: '如果数据在持续变化，建议打开自动刷新。'
      };
    }

    function renderWorkflow(data) {
      const selectedCount = data.selection?.selected_symbols?.length || 0;
      const hasSelection = (data.selection?.picks?.length || 0) > 0 || (lastSelection?.picks?.length || 0) > 0;
      const steps = [
        {
          kicker: 'Step 1',
          title: '配置参数',
          copy: '股票池、均线、止损止盈先在上面配好。',
          done: true,
          active: !data.has_run
        },
        {
          kicker: 'Step 2',
          title: 'AI 选股',
          copy: selectedCount ? ('本轮入选: ' + data.selection.selected_symbols.join(', ')) : '先跑一轮 AI 选股，明确优先标的。',
          done: hasSelection,
          active: data.mode === 'select'
        },
        {
          kicker: 'Step 3',
          title: '模拟 / 回测',
          copy: data.results?.length ? ('已有 ' + data.results.length + ' 个结果卡片可看。') : '先验证收益和回撤，再考虑自动交易。',
          done: Boolean(data.results?.length),
          active: data.mode === 'simulate' || data.mode === 'backtest'
        },
        {
          kicker: 'Step 4',
          title: '自动交易',
          copy: '最后再启动 broker.main，先小范围观察。',
          done: false,
          active: false
        }
      ];
      workflowSteps.innerHTML = '';
      for (const step of steps) {
        const cls = step.done ? 'workflow-step done' : step.active ? 'workflow-step active' : 'workflow-step';
        workflowSteps.insertAdjacentHTML('beforeend', `
          <div class="${cls}">
            <div class="workflow-kicker">${escapeHtml(step.kicker)}</div>
            <div class="workflow-title">${escapeHtml(step.title)}</div>
            <div class="workflow-copy">${escapeHtml(step.copy)}</div>
          </div>
        `);
      }
    }

    function renderOverview(data) {
      const blocker = blockerFromData(data);
      const nextAction = nextActionFromData(data);
      const selected = data.selection?.selected_symbols || lastSelection?.selected_symbols || [];
      const runtime = data.runtime || {};

      if (runtime.running) {
        heroStatus.textContent = '自动交易中';
        heroStatusNote.textContent = '后台线程正在持续轮询 IBKR 和 MiniMax。';
      } else if (!data.has_run) {
        heroStatus.textContent = '未开始';
        heroStatusNote.textContent = '你还没有执行任何动作。建议从 AI 选股开始。';
      } else if (data.errors?.length && !data.results?.length && !(data.training?.top_configs?.length)) {
        heroStatus.textContent = '有阻塞';
        heroStatusNote.textContent = '本轮没有产出结果，先处理错误。';
      } else if (data.mode === 'train' && data.training?.top_configs?.length) {
        heroStatus.textContent = '训练完成';
        heroStatusNote.textContent = '可以直接套用训练结果，再跑一轮模拟。';
      } else if (data.mode === 'select') {
        heroStatus.textContent = '选股完成';
        heroStatusNote.textContent = '现在最适合把入选股票带去做模拟。';
      } else if (data.results?.length) {
        heroStatus.textContent = '有结果';
        heroStatusNote.textContent = '收益、回撤和交易明细都已经可读。';
      } else {
        heroStatus.textContent = '运行中';
        heroStatusNote.textContent = '本轮已经启动，正在等待结果返回。';
      }

      nextActionTitle.textContent = nextAction.title;
      nextActionNote.textContent = nextAction.note;
      selectedSymbolsCount.textContent = String(selected.length);
      selectedSymbolsNote.textContent = selected.length
        ? selected.join(', ')
        : runtime.running && runtime.symbols?.length
          ? '自动交易当前监控: ' + runtime.symbols.join(', ')
          : '还没有 AI 选股结果。';
      currentBlocker.textContent = runtime.last_error ? '运行异常' : blocker.title;
      currentBlockerNote.textContent = runtime.last_error || blocker.note;
      renderWorkflow(data);
      lastUpdatePill.textContent = '最近刷新: ' + new Date().toLocaleTimeString();
      startLiveBtn.disabled = Boolean(runtime.running);
      stopLiveBtn.disabled = !runtime.running;
    }

    function configureAutoRefresh(enabled) {
      if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
      }
      if (enabled) {
        autoRefreshTimer = setInterval(async () => {
          await loadStatus();
          await loadLogs();
        }, 10000);
      }
      localStorage.setItem('broker-dashboard-auto-refresh', enabled ? '1' : '0');
    }

    function startLivePortfolioPolling() {
      if (livePortfolioTimer) return;
      livePortfolioTimer = setInterval(async () => {
        try {
          const resp = await fetch('/api/live/status');
          const data = await resp.json();
          renderLivePortfolio(data);
          await loadLogs();
        } catch (e) {
          console.warn('Live portfolio polling error:', e);
        }
      }, 5000);
    }

    function stopLivePortfolioPolling() {
      if (livePortfolioTimer) {
        clearInterval(livePortfolioTimer);
        livePortfolioTimer = null;
      }
    }

    function renderLivePortfolio(data) {
      // /api/live/status returns the runtime dict flat; /api/status wraps it in {runtime:...}
      const runtime = (data && data.runtime) ? data.runtime : (data || {});
      const positions = runtime.positions || {};
      const posArray = Object.values(positions);

      if (!runtime.running) {
        if (livePortfolioSection) livePortfolioSection.style.display = 'none';
        stopLivePortfolioPolling();
        return;
      }

      if (livePortfolioSection) livePortfolioSection.style.display = 'block';

      const totalPnl = runtime.total_pnl || 0;
      const realizedPnl = runtime.total_realized_pnl || 0;
      const unrealizedPnl = runtime.total_unrealized_pnl || 0;
      const cashBalance = runtime.cash_balance || 0;

      const fmtMoney = n => '$' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      if (liveTotalPnl) {
        liveTotalPnl.textContent = fmtMoney(totalPnl);
        liveTotalPnl.className = 'overview-value ' + (totalPnl >= 0 ? 'good' : 'bad');
      }
      if (liveRealizedPnl) {
        liveRealizedPnl.textContent = fmtMoney(realizedPnl);
        liveRealizedPnl.className = 'overview-value ' + (realizedPnl >= 0 ? 'good' : 'bad');
      }
      if (liveUnrealizedPnl) {
        liveUnrealizedPnl.textContent = fmtMoney(unrealizedPnl);
        liveUnrealizedPnl.className = 'overview-value ' + (unrealizedPnl >= 0 ? 'good' : 'bad');
      }
      if (liveCashBalance) {
        liveCashBalance.textContent = fmtMoney(cashBalance);
      }

      if (livePositionsBody) {
        livePositionsBody.innerHTML = '';
      }
      if (livePositionsEmpty) {
        livePositionsEmpty.style.display = posArray.length ? 'none' : 'block';
      }

      for (const pos of posArray) {
        const pnlClass = pos.unrealized_pnl >= 0 ? 'good' : 'bad';
        const pnlPct = pos.unrealized_pnl_pct || 0;
        const row = `
          <tr>
            <td>${escapeHtml(pos.symbol)}</td>
            <td>${pos.quantity}</td>
            <td>$${Number(pos.avg_cost).toFixed(2)}</td>
            <td>$${Number(pos.current_price).toFixed(2)}</td>
            <td class="${pnlClass}">$${Number(pos.unrealized_pnl).toFixed(2)}</td>
            <td class="${pnlClass}">${pnlPct.toFixed(2)}%</td>
            <td class="${pos.realized_pnl >= 0 ? 'good' : 'bad'}">$${Number(pos.realized_pnl || 0).toFixed(2)}</td>
          </tr>
        `;
        if (livePositionsBody) {
          livePositionsBody.insertAdjacentHTML('beforeend', row);
        }
      }
    }

    async function runAutoTrade() {
      // Show immediate visual feedback
      autoTradeBtn.textContent = '验证中...';
      autoTradeBtn.style.background = '#f59e0b';

      const payload = collectForm();

      const validationError = validateForm(payload);
      if (validationError) {
        showNotify('请先填写必要参数：' + validationError, 'error');
        resetButton();
        return;
      }
      autoTradeBtn.disabled = true;
      autoTradeBtn.textContent = '全自动交易启动中...';
      autoTradeBtn.style.background = '#1d7a46';

      try {
        // Step 1: Run AI stock selection
        const selectResp = await fetch('/api/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(collectForm())
        });
        const selectData = await selectResp.json();
        renderState(selectData);
        await loadLogs();

        if (!selectData.connected) {
          showNotify('IB Gateway 未连接，请先检查连接状态。', 'error');
          return;
        }

        // Step 2: Update symbols from selection
        if (selectData.selection?.selected_symbols?.length) {
          form.symbols.value = selectData.selection.selected_symbols.join(',');
          saveDraft();
        }

        // Step 3: Start live trading
        const liveResp = await fetch('/api/live/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(collectForm())
        });
        const liveData = await liveResp.json();
        renderState(liveData);
        await loadLogs();

        // Step 4: Start live portfolio polling
        startLivePortfolioPolling();

        showNotify('AI全自动交易已启动！系统将自动选股并执行交易。', 'ok');
      } catch (exc) {
        console.error('Auto trade error:', exc);
        showNotify('全自动交易启动失败：' + exc.message, 'error');
      } finally {
        resetButton();
      }
    }

    function resetButton() {
      autoTradeBtn.disabled = false;
      autoTradeBtn.textContent = 'AI 全自动交易';
      autoTradeBtn.style.background = '#1d7a46';
    }

    function renderLogs(items) {
      if (!logPanel) return;
      if (!items || !items.length) {
        logPanel.innerHTML = '<div class="log-line">还没有日志。</div>';
        return;
      }
      logPanel.innerHTML = items.map(item => {
        const levelClass = 'log-level-' + String(item.level || 'INFO').toLowerCase();
        return `
          <div class="log-line">
            <span class="log-meta">${escapeHtml(item.timestamp || '')}</span>
            <span class="${levelClass}">${escapeHtml(item.level || '')}</span>
            <span class="log-meta">${escapeHtml(item.logger || '')}</span>
            <span>${escapeHtml(item.message || '')}</span>
          </div>
        `;
      }).join('');
      logPanel.scrollTop = logPanel.scrollHeight;
    }

    function renderForm(settings) {
      if (!settings) return;
      form.symbols.value = settings.symbols ?? '';
      form.stock_universe.value = settings.stock_universe ?? '';
      form.duration.value = settings.duration ?? '';
      form.bar_size.value = settings.bar_size ?? '';
      form.use_rth.checked = Boolean(settings.use_rth);
      form.fast_sma.value = settings.fast_sma ?? '';
      form.slow_sma.value = settings.slow_sma ?? '';
      form.stop_loss_pct.value = settings.stop_loss_pct ?? '';
      form.take_profit_pct.value = settings.take_profit_pct ?? '';
      form.backtest_cash.value = settings.backtest_cash ?? '';
      form.order_quantity.value = settings.order_quantity ?? '';
      form.max_position.value = settings.max_position ?? '';
      form.enable_ai_stock_selection.checked = Boolean(settings.enable_ai_stock_selection);
      form.max_selected_symbols.value = settings.max_selected_symbols ?? '';
      form.ai_selection_min_confidence.value = settings.ai_selection_min_confidence ?? '';
      form.train_fast_windows.value = settings.train_fast_windows ?? '';
      form.train_slow_windows.value = settings.train_slow_windows ?? '';
      form.train_stop_loss_pcts.value = settings.train_stop_loss_pcts ?? '';
      form.train_take_profit_pcts.value = settings.train_take_profit_pcts ?? '';
      form.session_retry_attempts.value = settings.session_retry_attempts ?? '';
      form.session_retry_delay_seconds.value = settings.session_retry_delay_seconds ?? '';
      accountState.textContent = settings.account ? settings.account : '未设置';
    }

    function collectForm() {
      return {
        symbols: form.symbols.value,
        stock_universe: form.stock_universe.value,
        duration: form.duration.value,
        bar_size: form.bar_size.value,
        use_rth: form.use_rth.checked,
        fast_sma: form.fast_sma.value,
        slow_sma: form.slow_sma.value,
        stop_loss_pct: form.stop_loss_pct.value,
        take_profit_pct: form.take_profit_pct.value,
        backtest_cash: form.backtest_cash.value,
        order_quantity: form.order_quantity.value,
        max_position: form.max_position.value,
        enable_ai_stock_selection: form.enable_ai_stock_selection.checked,
        max_selected_symbols: form.max_selected_symbols.value,
        ai_selection_min_confidence: form.ai_selection_min_confidence.value,
        train_fast_windows: form.train_fast_windows.value,
        train_slow_windows: form.train_slow_windows.value,
        train_stop_loss_pcts: form.train_stop_loss_pcts.value,
        train_take_profit_pcts: form.train_take_profit_pcts.value,
        session_retry_attempts: form.session_retry_attempts.value,
        session_retry_delay_seconds: form.session_retry_delay_seconds.value
      };
    }

    function validateForm(payload) {
      if (!payload.symbols.trim()) {
        return '当前跟踪股票不能为空。';
      }
      if (!payload.stock_universe.trim()) {
        return 'AI 股票池不能为空。';
      }
      const fast = Number(payload.fast_sma);
      const slow = Number(payload.slow_sma);
      if (!Number.isFinite(fast) || !Number.isFinite(slow)) {
        return '快均线和慢均线必须填写数字。';
      }
      if (fast >= slow) {
        return '快均线必须小于慢均线。';
      }
      return '';
    }

    function saveDraft() {
      localStorage.setItem(draftKey, JSON.stringify(collectForm()));
    }

    function loadDraft() {
      const raw = localStorage.getItem(draftKey);
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch {
        return null;
      }
    }

    function applyDraft(draft) {
      if (!draft) return;
      for (const [key, element] of Object.entries(form)) {
        if (!(key in draft)) continue;
        if (element.type === 'checkbox') {
          element.checked = Boolean(draft[key]);
        } else {
          element.value = draft[key];
        }
      }
    }

    function renderSelection(data) {
      console.log('[renderSelection] data.selection:', JSON.stringify(data?.selection, null, 2).substring(0, 1500));
      const selection = data.selection ?? lastSelection;
      if (!selectionEmpty) return;
      if (!selection || !selection.picks || !selection.picks.length) {
        selectionSummary.textContent = data.mode === 'select'
          ? '本次没有选出可展示的结果。请先处理下方错误，或者检查股票池与 AI 置信度设置。'
          : (lastSelection ? selectionSummary.textContent : '还没有运行 AI 选股。');
        selectionEmpty.style.display = 'block';
        useSelectionBtn.disabled = !(lastSelection && lastSelection.selected_symbols && lastSelection.selected_symbols.length);
        return;
      }

      lastSelection = selection;
      selectionEmpty.style.display = 'none';
      const selected = selection.selected_symbols || [];
      selectionSummary.textContent = selected.length
        ? '本轮 AI 选中: ' + selected.join(', ') + (selection.market_view ? ' | 市场判断: ' + selection.market_view : '')
        : (selection.market_view || 'AI 已完成排序，但没有达到开仓条件的标的。');
      useSelectionBtn.disabled = selected.length === 0;

      for (const item of selection.picks) {
        const actionClass = item.action === 'BUY' ? 'good' : item.action === 'SELL' ? 'bad' : 'warn';
        selectionBody.insertAdjacentHTML('beforeend', `
          <tr>
            <td>${item.rank}</td>
            <td>${escapeHtml(item.symbol)}</td>
            <td>${item.selected ? '<span class="badge ok">Selected</span>' : '<span class="badge info">Watch</span>'}</td>
            <td class="${actionClass}">${escapeHtml(item.action)}</td>
            <td>${item.confidence}%</td>
            <td>${item.score}</td>
            <td>${escapeHtml(item.base_action)}</td>
            <td>${escapeHtml(item.candle_bias)} / ${item.candle_score}</td>
            <td>${escapeHtml(item.news_sentiment)} / ${item.news_score}</td>
            <td>${escapeHtml(item.ai_action)} / ${item.ai_confidence}</td>
            <td>${Number(item.close_price).toFixed(4)}</td>
            <td class="reason">${escapeHtml(item.reason)}</td>
          </tr>
        `);
      }
    }

    function renderResults(data) {
      if (data.results && data.results.length) {
        resultsEmpty.style.display = 'none';
        for (const item of data.results) {
          const profitClass = item.net_profit >= 0 ? 'good' : 'bad';
          results.insertAdjacentHTML('beforeend', `
            <article class="result-card">
              <h3>${escapeHtml(item.symbol)}</h3>
              ${metric('Trades', item.trades)}
              ${metric('Round Trips', item.round_trips)}
              ${metric('Win Rate', item.win_rate.toFixed(2) + '%', item.win_rate >= 50 ? 'good' : 'warn')}
              ${metric('Net Profit', item.net_profit.toFixed(2), profitClass)}
              ${metric('Final Equity', item.final_equity.toFixed(2))}
              ${metric('Max Drawdown', item.max_drawdown_pct.toFixed(2) + '%', 'warn')}
              ${metric('Fast / Slow', item.fast_sma + ' / ' + item.slow_sma)}
              ${metric('Stop / Take', item.stop_loss_pct.toFixed(2) + '% / ' + item.take_profit_pct.toFixed(2) + '%')}
              ${sparklineSvg(item.equity_curve)}
              <div class="result-note">${escapeHtml(item.interpretation)}</div>
            </article>
          `);

          for (const trade of item.trade_log) {
            const pnl = trade.realized_pnl === null ? '-' : trade.realized_pnl.toFixed(2);
            const pnlClass = trade.realized_pnl === null ? '' : trade.realized_pnl >= 0 ? 'good' : 'bad';
            tradeBody.insertAdjacentHTML('beforeend', `
              <tr>
                <td>${escapeHtml(item.symbol)}</td>
                <td>${escapeHtml(trade.timestamp)}</td>
                <td>${escapeHtml(trade.action)}</td>
                <td>${trade.quantity}</td>
                <td>${trade.price.toFixed(4)}</td>
                <td class="${pnlClass}">${escapeHtml(pnl)}</td>
                <td class="reason">${escapeHtml(trade.reason)}</td>
                <td>${trade.position_after}</td>
              </tr>
            `);
          }
        }
      } else {
        resultsEmpty.textContent = data.has_run
          ? '本次已运行，但没有生成可展示的收益结果。请先处理下方错误。'
          : '还没有运行模拟或回测。';
        resultsEmpty.style.display = 'block';
      }
      tradeEmpty.style.display = tradeBody.children.length ? 'none' : 'block';
    }

    function renderTraining(data) {
      const rows = data.training?.top_configs || [];
      if (rows.length) {
        trainingEmpty.style.display = 'none';
        for (const item of rows) {
          trainingBody.insertAdjacentHTML('beforeend', `
            <tr>
              <td>${item.rank}</td>
              <td>${escapeHtml(item.symbol)}</td>
              <td>${item.fast_sma} / ${item.slow_sma}</td>
              <td>${item.stop_loss_pct.toFixed(2)}% / ${item.take_profit_pct.toFixed(2)}%</td>
              <td class="${item.net_profit >= 0 ? 'good' : 'bad'}">${item.net_profit.toFixed(2)}</td>
              <td>${item.win_rate.toFixed(2)}%</td>
              <td>${item.max_drawdown_pct.toFixed(2)}%</td>
              <td>${item.trades}</td>
              <td class="note">${escapeHtml(item.interpretation)}</td>
              <td>
                <button
                  class="ghost tiny apply-training-btn"
                  data-fast="${item.fast_sma}"
                  data-slow="${item.slow_sma}"
                  data-stop="${item.stop_loss_pct.toFixed(2)}"
                  data-take="${item.take_profit_pct.toFixed(2)}"
                >
                  套用
                </button>
              </td>
            </tr>
          `);
        }
      } else {
        trainingEmpty.style.display = 'block';
      }
    }

    function renderErrors(data) {
      if (data.errors && data.errors.length) {
        errorsEmpty.style.display = 'none';
        for (const item of data.errors) {
          errors.insertAdjacentHTML('beforeend', `
            <div class="error-item">
              <strong>${escapeHtml(item.symbol)}</strong>
              <div style="margin-top:8px; line-height:1.7;">${escapeHtml(item.message)}</div>
            </div>
          `);
        }
      } else {
        errorsEmpty.style.display = 'block';
      }
    }

    function renderGuidance(data) {
      if (data.guidance && data.guidance.length) {
        guidanceEmpty.style.display = 'none';
        for (const item of data.guidance) {
          const steps = (item.steps || []).map(step => '<li>' + escapeHtml(step) + '</li>').join('');
          guidance.insertAdjacentHTML('beforeend', `
            <div class="guide-item">
              <h3>${escapeHtml(item.title)}</h3>
              <ol>${steps}</ol>
            </div>
          `);
        }
      } else {
        guidanceEmpty.style.display = 'block';
      }
    }

    function renderSession(data) {
      if (data.session && data.session.attempts.length) {
        sessionEmpty.style.display = 'none';
        for (const item of data.session.attempts) {
          const badge = item.status === 'ok' ? 'badge ok' : item.status === 'session_conflict' ? 'badge warn' : 'badge off';
          const label = item.status === 'ok' ? '成功' : item.status === 'session_conflict' ? '会话冲突' : '失败';
          sessionAttempts.insertAdjacentHTML('beforeend', `
            <div class="attempt-item">
              <div class="attempt-head">
                <span>第 ${item.attempt} 次探测</span>
                <span class="${badge}">${label}</span>
              </div>
              <div>${escapeHtml(item.message)}</div>
            </div>
          `);
        }
      } else {
        sessionEmpty.style.display = 'block';
      }
    }

    function verdictsForEdu(action) {
      if (action === 'BUY' || action === 'BULLISH') return { cls: 'edu-verdict-buy', label: '看多' };
      if (action === 'SELL' || action === 'BEARISH') return { cls: 'edu-verdict-sell', label: '看空' };
      return { cls: 'edu-verdict-hold', label: '中性' };
    }

    function renderEducationalReports(data) {
      if (!eduSection || !eduReports || !eduEmpty) return;
      const reports = data.selection?.reports || [];
      if (!reports.length) {
        eduSection.style.display = 'none';
        return;
      }
      eduSection.style.display = 'block';
      eduReports.innerHTML = '';
      eduEmpty.style.display = 'none';

      for (const report of reports) {
        const v = verdictsForEdu(report.final_action);
        const stepsHtml = report.steps.map(step => {
          const sv = verdictsForEdu(step.verdict);
          const indicatorsHtml = step.indicators.map(ind => `
            <div class="edu-indicator" style="position:relative;">
              <div class="edu-indicator-name">${escapeHtml(ind.name)}</div>
              <div class="edu-indicator-value">${escapeHtml(ind.value)}</div>
              <div class="edu-indicator-interp">${escapeHtml(ind.interpretation)}</div>
              <div class="edu-tooltip">${escapeHtml(ind.explanation)}</div>
            </div>
          `).join('');
          return `
            <div class="edu-step">
              <div class="edu-step-header" onclick="this.nextElementSibling.classList.toggle('open'); this.closest('.edu-card').classList.toggle('expanded');">
                <div class="edu-step-num">${step.step}</div>
                <div class="edu-step-title">${escapeHtml(step.title)}</div>
                <span class="edu-verdict-badge ${sv.cls}">${escapeHtml(step.verdict || sv.label)}</span>
              </div>
              <div class="edu-step-body">
                <div class="edu-step-content">${escapeHtml(step.content)}</div>
                <div class="edu-indicators">${indicatorsHtml}</div>
              </div>
            </div>
          `;
        }).join('');

        eduReports.insertAdjacentHTML('beforeend', `
          <div class="edu-card">
            <div class="edu-card-header">
              <h3>${escapeHtml(report.symbol)}</h3>
              <span class="edu-verdict-badge ${v.cls}">${escapeHtml(v.label)} | 置信度 ${report.final_confidence}%</span>
            </div>
            ${stepsHtml}
            <div class="edu-card-summary">${escapeHtml(report.summary_zh)}</div>
          </div>
        `);
      }
    }

    function renderState(data) {
      if (connState) connState.textContent = data.connected ? '已连接' : '未连接';
      if (connHostPort) connHostPort.textContent = data.connected ? `${data.host}:${data.port}` : `${data.host}:${data.port || '-'}`;
      updateConnBanner(data.connected, `${data.host}:${data.port || '-'}`);
      updateTodayCard({ connection: { connected: data.connected } });
      if (!data.connected) {
        if (diagnosePanel) diagnosePanel.style.display = 'block';
        if (diagnoseBtn) diagnoseBtn.style.display = 'block';
      } else {
        if (diagnosePanel) diagnosePanel.style.display = 'none';
        if (diagnoseBtn) diagnoseBtn.style.display = 'none';
      }
      if (modeState) modeState.textContent = data.mode || '未运行';
      if (!data.has_run) {
        if (runState) runState.textContent = '未运行';
      } else if (data.mode === 'select') {
        if (runState) runState.textContent = '选股完成';
      } else if (data.training?.top_configs?.length) {
        if (runState) runState.textContent = '训练完成';
      } else if (data.results?.length && !data.errors?.length) {
        if (runState) runState.textContent = '成功';
      } else if (data.results?.length && data.errors?.length) {
        if (runState) runState.textContent = '部分成功';
      } else {
        if (runState) runState.textContent = '失败';
      }

      if (statusBadge) {
        statusBadge.innerHTML = data.connected
          ? '<span class="badge ok">Connected</span>'
          : '<span class="badge off">Disconnected</span>';
      }

      resetSections();
      renderSummary(data);
      renderOverview(data);
      renderLivePortfolio(data);
      renderSelection(data);
      renderEducationalReports(data);
      renderResults(data);
      renderTraining(data);
      renderErrors(data);
      renderGuidance(data);
      renderSession(data);
      renderForm(data.settings);
    }

    async function postAction(path, button) {
      const payload = collectForm();
      const validationError = validateForm(payload);
      if (validationError) {
        showNotify(validationError, 'error');
        return;
      }
      saveDraft();
      await withBusy(button, '运行中...', async () => {
        try {
          const resp = await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          let data;
          try {
            data = await resp.json();
          } catch {
            const text = await resp.text();
            const errorDiv = document.getElementById('errors');
            if (errorDiv) {
              errorDiv.innerHTML = '<div class="error-item"><strong>服务器错误</strong><div style="margin-top:8px;">HTTP ' + resp.status + '：' + escapeHtml(text.substring(0, 500)) + '</div></div>';
              errorDiv.previousElementSibling.style.display = 'none';
            }
            throw new Error('非 JSON 响应');
          }
          if (!resp.ok) {
            const errorMsg = (data && data.errors && data.errors[0] && data.errors[0].message) || 'HTTP ' + resp.status;
            showNotify('请求失败：' + errorMsg, 'error');
            return;
          }
          renderState(data);
          await loadLogs();
        } catch (exc) {
          console.error('postAction error:', exc);
          showNotify('请求异常：' + exc.message, 'error');
        }
      });
    }

    async function loadConfig() {
      const resp = await fetch('/api/config');
      const data = await resp.json();
      renderForm(data.settings);
      const draft = loadDraft();
      if (draft) {
        applyDraft(draft);
      }
    }

    async function loadStatus() {
      const resp = await fetch('/api/status');
      const data = await resp.json();
      renderState(data);
      await loadLogs();
      const draft = loadDraft();
      if (draft) {
        applyDraft(draft);
      }
    }

    async function loadLogs() {
      const resp = await fetch('/api/logs');
      const data = await resp.json();
      renderLogs(data.logs || []);
    }

    async function controlLive(path, button) {
      const payload = collectForm();
      saveDraft();
      await withBusy(button, '处理中...', async () => {
        const resp = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        renderState(data);
        await loadLogs();
      });
    }

    async function runPreviewFlow() {
      previewBtn.disabled = true;
      const original = previewBtn.textContent;
      previewBtn.textContent = '预览中...';
      try {
        await postAction('/api/select', previewBtn);
        if (lastSelection?.selected_symbols?.length) {
          form.symbols.value = lastSelection.selected_symbols.join(',');
          saveDraft();
        }
        await postAction('/api/simulate', previewBtn);
        await loadLogs();
      } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = original;
      }
    }

    selectBtn.addEventListener('click', () => postAction('/api/select', selectBtn));
    autoTradeBtn.addEventListener('click', runAutoTrade);
    previewBtn.addEventListener('click', runPreviewFlow);
    simulateBtn.addEventListener('click', () => postAction('/api/simulate', simulateBtn));
    backtestBtn.addEventListener('click', () => postAction('/api/backtest', backtestBtn));
    trainBtn.addEventListener('click', () => postAction('/api/train', trainBtn));
    startLiveBtn.addEventListener('click', () => controlLive('/api/live/start', startLiveBtn));
    stopLiveBtn.addEventListener('click', () => controlLive('/api/live/stop', stopLiveBtn));
    refreshBtn.addEventListener('click', loadStatus);
    refreshLogsBtn.addEventListener('click', loadLogs);
    clearLogsBtn.addEventListener('click', async () => {
      await fetch('/api/logs/clear', { method: 'POST' });
      await loadLogs();
    });
    autoRefreshToggle.checked = localStorage.getItem('broker-dashboard-auto-refresh') === '1';
    configureAutoRefresh(autoRefreshToggle.checked);
    diagnoseBtn.addEventListener('click', async () => {
      diagnoseBtn.disabled = true;
      diagnoseBtn.textContent = '诊断中...';
      try {
        const resp = await fetch('/api/diagnose');
        const result = await resp.json();
        const lines = result.checks.map(c => {
          const cls = c.status === 'ok' ? 'good' : 'bad';
          return `<div class="${cls}">${escapeHtml(c.check)}: ${escapeHtml(c.message)}</div>`;
        });
        const steps = result.actionable_steps.map(s => `<li>${escapeHtml(s)}</li>`).join('');
        diagnoseResults.innerHTML = `
          ${lines.join('')}
          <div style="margin-top:8px;font-size:11px;color:var(--muted);">
            <strong>修复步骤：</strong>
            <ol style="margin:4px 0 0 16px;padding:0;">${steps}</ol>
          </div>
        `;
      } catch (exc) {
        diagnoseResults.innerHTML = '<div class="bad">诊断请求失败：' + escapeHtml(String(exc)) + '</div>';
      } finally {
        diagnoseBtn.disabled = false;
        diagnoseBtn.textContent = '诊断连接';
      }
    });
    document.getElementById('export-trades-btn').addEventListener('click', () => {
      window.open('/api/trades/export', '_blank');
    });
    autoRefreshToggle.addEventListener('change', () => configureAutoRefresh(autoRefreshToggle.checked));
    saveDraftBtn.addEventListener('click', () => {
      saveDraft();
      showNotify('当前参数已保存到浏览器本地。', 'ok');
    });
    useSelectionBtn.addEventListener('click', () => {
      if (!lastSelection || !lastSelection.selected_symbols || !lastSelection.selected_symbols.length) {
        showNotify('还没有可用的 AI 选股结果。', 'error');
        return;
      }
      form.symbols.value = lastSelection.selected_symbols.join(',');
      saveDraft();
      showNotify('已覆盖跟踪列表。', 'ok');
    });
    resetConfigBtn.addEventListener('click', async () => {
      localStorage.removeItem(draftKey);
      lastSelection = null;
      await loadConfig();
      await loadStatus();
    });
    trainingBody.addEventListener('click', event => {
      const button = event.target.closest('.apply-training-btn');
      if (!button) return;
      form.fast_sma.value = button.dataset.fast;
      form.slow_sma.value = button.dataset.slow;
      form.stop_loss_pct.value = button.dataset.stop;
      form.take_profit_pct.value = button.dataset.take;
      saveDraft();
    });
    toggleEduBtn.addEventListener('click', () => {
      const bodies = document.querySelectorAll('.edu-step-body');
      const allOpen = Array.from(bodies).every(b => b.classList.contains('open'));
      bodies.forEach(b => allOpen ? b.classList.remove('open') : b.classList.add('open'));
      toggleEduBtn.textContent = allOpen ? '全部展开' : '全部收起';
    });

    loadConfig().then(async () => {
      await loadStatus();
      await loadLogs();
    });

    setInterval(() => { loadLogs().catch(() => {}); }, 3000);

    // ── 沙盘账户 JS ────────────────────────────────────────
    const paperStartBtn   = document.getElementById('paper-start-btn');
    const paperStopBtn    = document.getElementById('paper-stop-btn');
    const paperResetBtn   = document.getElementById('paper-reset-btn');
    const paperStatusBadge = document.getElementById('paper-status-badge');
    const paperEquityEl   = document.getElementById('paper-equity');
    const paperReturnEl   = document.getElementById('paper-return');
    const paperReturnPct  = document.getElementById('paper-return-pct');
    const paperCashEl     = document.getElementById('paper-cash');
    const paperStartingEl = document.getElementById('paper-starting');
    const paperChartEl    = document.getElementById('paper-chart');
    const paperPosBody    = document.getElementById('paper-positions-body');
    const paperPosEmpty   = document.getElementById('paper-positions-empty');
    const paperTradesBody = document.getElementById('paper-trades-body');
    const paperDecisionsWrap = document.getElementById('paper-decisions-wrap');
    const paperDecisionsBody = document.getElementById('paper-decisions-body');
    const paperTradesEmpty = document.getElementById('paper-trades-empty');
    const paperErrorEl    = document.getElementById('paper-error');
    const paperStatusBar  = document.getElementById('paper-status-bar');

    let paperPollTimer = null;

    function paperFmt$(n) { return '$' + Number(n ?? 0).toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
    function paperFmtPct(n) {
      const v = Number(n ?? 0);
      const cls = v >= 0 ? 'good' : 'bad';
      return `<span class="${cls}">${v >= 0 ? '+' : ''}${v.toFixed(2)}%</span>`;
    }
    function paperFmtPnl(n) {
      const v = Number(n ?? 0);
      const cls = v >= 0 ? 'good' : 'bad';
      return `<span class="${cls}">${v >= 0 ? '+' : ''}${paperFmt$(v)}</span>`;
    }

    function renderPaperChart(curve, baseline) {
      if (!paperChartEl) return;
      if (!curve || curve.length < 2) {
        paperChartEl.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:10px 0;">等待第一笔数据…（沙盘运行 1 分钟后出图）</div>';
        return;
      }
      const values = curve.map(p => p.equity);
      const base = Number(baseline || values[0]);
      const min = Math.min(...values, base);
      const max = Math.max(...values, base);
      const span = Math.max(max - min, 1);
      const W = 860, H = 100, PAD = 4;
      const yOf = v => PAD + (1 - (v - min) / span) * (H - PAD * 2);
      const pts = values.map((v, i) => {
        const x = PAD + (i / Math.max(values.length - 1, 1)) * (W - PAD * 2);
        return `${x.toFixed(1)},${yOf(v).toFixed(1)}`;
      }).join(' ');
      const last = values[values.length - 1];
      const colour = last >= base ? '#1d7a46' : '#a13d31';
      const baseY = yOf(base).toFixed(1);
      const delta = last - base;
      const deltaPct = base > 0 ? (delta / base * 100) : 0;
      const deltaStr = `${delta >= 0 ? '+' : ''}$${Math.abs(delta).toLocaleString('en-US', {maximumFractionDigits:2})} (${delta >= 0 ? '+' : ''}${deltaPct.toFixed(2)}%)`;
      paperChartEl.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;display:block;">
          <line x1="${PAD}" y1="${baseY}" x2="${W-PAD}" y2="${baseY}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="4,4" opacity="0.5"/>
          <text x="${W-PAD-4}" y="${Math.max(Number(baseY)-4, 12)}" text-anchor="end" font-size="10" fill="var(--muted)">基准 $${base.toLocaleString('en-US', {maximumFractionDigits:0})}</text>
          <polyline points="${pts}" fill="none" stroke="${colour}" stroke-width="2" stroke-linejoin="round"/>
        </svg>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:2px;">
          <span>${escapeHtml(curve[0].time?.substring(0,16) || '')}</span>
          <span class="${delta >= 0 ? 'good' : 'bad'}" style="font-weight:700;">${deltaStr}</span>
          <span>${escapeHtml(curve[curve.length-1].time?.substring(0,16) || '')}</span>
        </div>`;
    }

    function renderPaperPositions(positions) {
      if (!paperPosBody) return;
      const entries = Object.values(positions || {}).filter(p => p.quantity > 0);
      if (entries.length === 0) {
        paperPosBody.innerHTML = '';
        if (paperPosEmpty) paperPosEmpty.style.display = '';
        return;
      }
      if (paperPosEmpty) paperPosEmpty.style.display = 'none';
      paperPosBody.innerHTML = entries.map(p => {
        const cls = p.unrealized_pnl >= 0 ? 'good' : 'bad';
        return `<tr>
          <td><strong>${escapeHtml(p.symbol)}</strong></td>
          <td>${p.quantity}</td>
          <td>$${Number(p.avg_cost).toFixed(2)}</td>
          <td>$${Number(p.current_price).toFixed(2)}</td>
          <td class="${cls}">${p.unrealized_pnl >= 0 ? '+' : ''}$${Number(p.unrealized_pnl).toFixed(2)}</td>
        </tr>`;
      }).join('');
    }

    function renderPaperTrades(trades) {
      if (!paperTradesBody) return;
      if (!trades || trades.length === 0) {
        paperTradesBody.innerHTML = '';
        if (paperTradesEmpty) paperTradesEmpty.style.display = '';
        return;
      }
      if (paperTradesEmpty) paperTradesEmpty.style.display = 'none';
      paperTradesBody.innerHTML = [...trades].reverse().slice(0, 30).map(t => {
        const isBuy = t.action === 'BUY';
        const pnlHtml = t.realized_pnl != null
          ? `<span class="${t.realized_pnl >= 0 ? 'good' : 'bad'}">${t.realized_pnl >= 0 ? '+' : ''}$${Number(t.realized_pnl).toFixed(2)}</span>`
          : '—';
        return `<tr>
          <td style="white-space:nowrap;">${escapeHtml((t.time || '').substring(0,16))}</td>
          <td><strong>${escapeHtml(t.symbol)}</strong></td>
          <td style="color:${isBuy ? 'var(--good)' : 'var(--bad)'};">${escapeHtml(t.action)}</td>
          <td>${t.quantity}</td>
          <td>$${Number(t.price).toFixed(2)}</td>
          <td>${pnlHtml}</td>
        </tr>`;
      }).join('');
    }

    let _paperDecisionsRaw = [];
    let _paperDecisionsFilter = 'all';
    function renderPaperDecisions(items) {
      if (!paperDecisionsBody || !paperDecisionsWrap) return;
      if (!Array.isArray(items) || items.length === 0) {
        paperDecisionsWrap.style.display = 'none';
        return;
      }
      _paperDecisionsRaw = items;
      const actions = items.filter(d => d.action === 'BUY' || d.action === 'SELL');
      // Auto-switch default: if any actionable, show actionable; else show all
      if (_paperDecisionsFilter === 'all' && actions.length > 0 && !document.querySelector('#paper-decisions-filter .filter-pill[data-filter="actionable"].active')) {
        // respect user's choice — don't auto-switch after first render
      }
      _renderPaperDecisionsView();
    }
    function _renderPaperDecisionsView() {
      if (!paperDecisionsBody || !paperDecisionsWrap) return;
      const items = _paperDecisionsRaw || [];
      if (items.length === 0) { paperDecisionsWrap.style.display = 'none'; return; }
      paperDecisionsWrap.style.display = 'block';
      const emptyEl = document.getElementById('paper-decisions-empty');
      const countEl = document.getElementById('paper-decisions-count');
      const actions = items.filter(d => d.action === 'BUY' || d.action === 'SELL');
      const shown = _paperDecisionsFilter === 'actionable' ? actions : items;
      const noteEl = document.getElementById('paper-decisions-note');
      if (noteEl) noteEl.style.display = items.some(d => d.reason === 'low_ai_confidence') ? 'block' : 'none';
      if (countEl) countEl.textContent = `· ${items.length} 只股票，其中 ${actions.length} 个买/卖信号`;
      const actColor = a => a === 'BUY' ? 'var(--good)' : (a === 'SELL' ? 'var(--bad)' : 'var(--muted)');
      if (shown.length === 0) {
        paperDecisionsBody.innerHTML = '';
        if (emptyEl) emptyEl.style.display = 'block';
        return;
      }
      if (emptyEl) emptyEl.style.display = 'none';
      paperDecisionsBody.innerHTML = shown.map(d => {
        const reason = d.decision_reason || d.reason || '';
        const riskNote = d.risk_action && d.risk_action !== 'PASS' ? ` <span class="bad">[风控:${d.risk_action}]</span>` : '';
        const blockNote = (!d.allow_new_buy && d.decision_action === 'BUY' && d.position === 0) ? ` <span class="warn">[未入选]</span>` : '';
        return `<tr>
        <td><strong>${escapeHtml(d.symbol)}</strong></td>
        <td style="color:${actColor(d.action)};font-weight:600;">${escapeHtml(d.action)}</td>
        <td>${d.position || 0}</td>
        <td>$${Number(d.price || 0).toFixed(2)}</td>
        <td style="color:${actColor(d.ai_action)};">${escapeHtml(d.ai_action || '-')} <span class="tiny">(${d.ai_confidence || 0})</span></td>
        <td class="tiny" style="max-width:320px;">${escapeHtml(reason)}${riskNote}${blockNote}</td>
      </tr>`;
      }).join('');
    }
    document.querySelectorAll('#paper-decisions-filter .filter-pill').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#paper-decisions-filter .filter-pill').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        _paperDecisionsFilter = btn.dataset.filter;
        _renderPaperDecisionsView();
      });
    });

    function renderPaperState(data) {
      if (!data) return;
      const running = data.running;
      if (paperStatusBadge) {
        const phase = data.market_phase || '';
        const phaseLabel = {'market': '🟢盘中', 'pre_market': '🟡盘前', 'after_hours': '🟡盘后', 'closed': '🔴休市'}[phase] || '';
        const phaseHtml = phaseLabel ? `<span class="mini-pill" style="margin-left:6px;">${phaseLabel}</span>` : '';
        paperStatusBadge.innerHTML = (running
          ? '<span class="badge ok">运行中</span>'
          : (data.started_at ? '<span class="badge off">已暂停</span>' : '<span class="badge off">未启动</span>'))
          + phaseHtml;
      }
      if (paperStartingEl) paperStartingEl.textContent = paperFmt$(data.starting_cash);
      if (paperEquityEl)   paperEquityEl.textContent   = paperFmt$(data.total_equity);
      if (paperCashEl)     paperCashEl.textContent      = paperFmt$(data.cash);

      const gain = (data.total_equity || 0) - (data.starting_cash || 0);
      if (paperReturnEl)  paperReturnEl.innerHTML  = paperFmtPnl(gain);
      if (paperReturnPct) paperReturnPct.innerHTML = paperFmtPct(data.return_pct);

      if (paperStatusBar) {
        const msg = data.status_msg || '';
        const cycles = data.cycle_count || 0;
        const lastRun = data.last_run_at ? '上次: ' + data.last_run_at.replace('T',' ') : '';
        if (running && msg) {
          const isActive = msg.includes('执行中') || msg.includes('连接');
          paperStatusBar.style.cssText = isActive
            ? 'display:block;background:#e8f4fd;color:#1d5a8a;border:1px solid #aed4f0;'
            : 'display:block;background:#f0f4f8;color:#555;border:1px solid #d0dae5;';
          paperStatusBar.innerHTML = '⚙ ' + escapeHtml(msg)
            + (cycles ? ' &nbsp;·&nbsp; 已完成 <b>' + cycles + '</b> 轮' : '')
            + (lastRun ? ' &nbsp;·&nbsp; ' + escapeHtml(lastRun) : '');
        } else if (!running && cycles) {
          paperStatusBar.style.cssText = 'display:block;background:#f0f4f8;color:#555;border:1px solid #d0dae5;';
          paperStatusBar.innerHTML = '已停止 &nbsp;·&nbsp; 共运行 <b>' + cycles + '</b> 轮'
            + (lastRun ? ' &nbsp;·&nbsp; ' + escapeHtml(lastRun) : '');
        } else {
          paperStatusBar.style.display = 'none';
        }
      }
      if (paperErrorEl) {
        if (data.last_error) {
          paperErrorEl.textContent = '错误: ' + data.last_error;
          paperErrorEl.style.display = 'block';
        } else {
          paperErrorEl.style.display = 'none';
        }
      }
      renderPaperChart(data.equity_curve, data.starting_cash);
      renderPaperPositions(data.positions);
      renderPaperTrades(data.trades);
      renderPaperDecisions(data.last_cycle_decisions);

      paperStartBtn.disabled = running;
      paperStopBtn.disabled  = !running;

      if (running && !paperPollTimer) {
        paperPollTimer = setInterval(refreshPaper, 10000);
      } else if (!running && paperPollTimer) {
        clearInterval(paperPollTimer);
        paperPollTimer = null;
      }
    }

    async function refreshPaper() {
      try {
        const resp = await fetch('/api/paper/status');
        const data = await resp.json();
        renderPaperState(data);
      } catch (e) { console.warn('[paper] status error', e); }
    }

    function collectPaperForm() {
      const universe = document.getElementById('paper-universe')?.value?.trim()
        || document.getElementById('stock-universe')?.value?.trim()
        || '';
      return {
        paper_capital: Number(document.getElementById('paper-capital')?.value || 10000),
        paper_universe: universe,
        // Forward current strategy params
        fast_sma: document.getElementById('fast-sma')?.value,
        slow_sma: document.getElementById('slow-sma')?.value,
        stop_loss_pct: document.getElementById('stop-loss')?.value,
        take_profit_pct: document.getElementById('take-profit')?.value,
        order_quantity: document.getElementById('order-quantity')?.value,
        max_position: document.getElementById('max-position')?.value,
        enable_ai_stock_selection: document.getElementById('enable-ai-stock-selection')?.checked ?? true,
        max_selected_symbols: document.getElementById('max-selected-symbols')?.value,
        ai_selection_min_confidence: document.getElementById('ai-selection-confidence')?.value,
        duration: document.getElementById('duration')?.value,
        bar_size: document.getElementById('bar-size')?.value,
        use_rth: document.getElementById('use-rth')?.checked ?? true,
      };
    }

    paperStartBtn.addEventListener('click', async () => {
      paperStartBtn.disabled = true;
      paperStartBtn.textContent = '启动中...';
      try {
        const resp = await fetch('/api/paper/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(collectPaperForm()),
        });
        const data = await resp.json();
        if (data.error) { showNotify('沙盘启动失败: ' + data.error, 'error'); return; }
        renderPaperState(data);
        showNotify('沙盘账户已启动！AI 将在后台自动交易。', 'ok');
      } catch (e) {
        showNotify('沙盘启动异常: ' + e.message, 'error');
      } finally {
        paperStartBtn.textContent = '启动沙盘';
      }
    });

    paperStopBtn.addEventListener('click', async () => {
      paperStopBtn.disabled = true;
      paperStopBtn.textContent = '暂停中...';
      try {
        const resp = await fetch('/api/paper/stop', { method: 'POST' });
        const data = await resp.json();
        renderPaperState(data);
        showNotify('沙盘已暂停。', 'info');
      } catch (e) {
        showNotify('暂停失败: ' + e.message, 'error');
      } finally {
        paperStopBtn.textContent = '暂停';
      }
    });

    paperResetBtn.addEventListener('click', async () => {
      if (!confirm('重置沙盘账户会清空所有虚拟持仓和收益记录，确定吗？')) return;
      try {
        const resp = await fetch('/api/paper/reset', { method: 'POST' });
        const data = await resp.json();
        renderPaperState(data);
        showNotify('沙盘账户已重置。', 'info');
      } catch (e) {
        showNotify('重置失败: ' + e.message, 'error');
      }
    });

    // Pre-fill paper universe with default pool
    (function() {
      const paperUniEl = document.getElementById('paper-universe');
      if (paperUniEl && !paperUniEl.value) {
        paperUniEl.value = 'AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,AMD,QCOM,JPM,GS,V,JNJ,LLY,HD,NKE,WMT,XOM,CVX';
      }
    })();

    refreshPaper();
    // ── 沙盘账户 JS END ────────────────────────────────────
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _dashboard_html()


@app.get("/api/config")
def config() -> JSONResponse:
    settings = load_settings()
    return JSONResponse({"settings": _serialize_settings(settings)})


@app.get("/api/status")
def status() -> JSONResponse:
    settings = load_settings()
    return JSONResponse(_empty_payload(settings))


@app.get("/api/logs")
def logs() -> JSONResponse:
    return JSONResponse({"logs": get_recent_logs()})


@app.post("/api/logs/clear")
def clear_logs_endpoint() -> JSONResponse:
    clear_logs()
    return JSONResponse({"ok": True})


@app.get("/api/diagnose")
def diagnose() -> JSONResponse:
    """对 IB Gateway 连接进行诊断，返回可用信息。"""
    settings = load_settings()
    host = settings.ib_host
    results: list[dict] = []

    # 1. 主机名解析检查
    try:
        ip = socket.gethostbyname(host)
        results.append({"check": "DNS 解析", "status": "ok", "message": f"{host} → {ip}"})
    except Exception as exc:
        results.append({"check": "DNS 解析", "status": "error", "message": f"无法解析 {host}: {exc}"})

    # 2. 逐个端口探测
    for port in settings.ib_port_candidates:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            conn_result = sock.connect_ex((host, port))
            sock.close()
            if conn_result == 0:
                port_name = {7497: "TWS Paper", 4002: "IB Gateway Paper", 7496: "TWS Live", 4001: "IB Gateway Live"}.get(port, f"端口 {port}")
                results.append({"check": f"端口 {port} ({port_name})", "status": "ok", "message": "端口开放，IB Gateway / TWS 可能在监听"})
            else:
                results.append({"check": f"端口 {port}", "status": "error", "message": f"端口关闭或被过滤（TWS/Live: {port}）"})
        except Exception as exc:
            results.append({"check": f"端口 {port}", "status": "error", "message": str(exc)})

    # 3. 综合建议
    open_ports = [r for r in results if r["status"] == "ok" and "端口" in r["check"]]
    actionable_steps: list[str] = []
    if not open_ports:
        actionable_steps = [
            "确认 IB Gateway / TWS 已启动并登录",
            "在 IB Gateway/TWS 里开启 Socket API：Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients",
            "确认 API 端口号与配置一致（TWS Paper: 7497，IB Gateway Paper: 4002）",
            "检查防火墙或安全软件没有阻止本机到本机的连接",
            "关闭同一账户在其他设备上的会话（手机 App、Client Portal 等）",
        ]
    else:
        actionable_steps = ["至少有一个端口开放，请在 Dashboard 点击「刷新状态」重试连接"]

    return JSONResponse({
        "host": host,
        "ports": settings.ib_port_candidates,
        "checks": results,
        "actionable_steps": actionable_steps,
    })


@app.get("/api/diagnose/minimax")
def diagnose_minimax() -> JSONResponse:
    """测试 MiniMax API 连接是否正常。"""
    settings = load_settings()
    if not settings.ai_api_key:
        return JSONResponse({
            "status": "error",
            "message": "AI_API_KEY 未配置，请在 .env 中填入你的 MiniMax API Key。",
        }, status_code=400)

    try:
        from openai import OpenAI
        client = OpenAI(base_url=settings.ai_base_url, api_key=settings.ai_api_key)
        response = client.chat.completions.create(
            model=settings.ai_model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
            temperature=0.1,
        )
        return JSONResponse({
            "status": "ok",
            "message": f"MiniMax API 连接成功！模型: {settings.ai_model}，返回: {response.choices[0].message.content}",
        })
    except Exception as exc:
        return JSONResponse({
            "status": "error",
            "message": f"MiniMax API 调用失败：{exc}",
            "hint": "常见原因：API Key 无效 / 额度用尽 / 网络无法访问 api.minimaxi.com / base_url 配置错误",
        }, status_code=502)


@app.get("/api/trades/export")
def export_trades() -> FileResponse:
    """下载交易历史 CSV 文件。"""
    from broker.trade_log import _TRADE_LOG_FILE
    if not _TRADE_LOG_FILE.exists():
        return JSONResponse({"error": "还没有交易记录"}, status_code=404)
    return FileResponse(
        path=str(_TRADE_LOG_FILE),
        media_type="text/csv",
        filename="trade_history.csv",
    )


@app.get("/api/debug/selection")
def debug_selection() -> JSONResponse:
    """调试端点：直接运行选股，返回完整结果（含错误详情）。"""
    settings = load_settings()
    configure_logging(settings.log_level)
    from broker.session_guard import connect_with_session_guard
    from broker.selector import AISymbolSelector
    from broker.ai_analysis import AIAnalyzer

    guard = connect_with_session_guard(settings, settings.ib_client_id + 999)
    if guard.client is None:
        return JSONResponse({
            "connected": False,
            "error": guard.error,
            "session_attempts": [
                {"attempt": a.attempt, "status": a.status, "message": a.message}
                for a in guard.attempts
            ],
        })

    try:
        ai_analyzer = None
        if settings.enable_ai_analysis and settings.ai_api_key:
            ai_analyzer = AIAnalyzer(settings.ai_base_url, settings.ai_api_key, settings.ai_model)
        selector = AISymbolSelector(settings, ai_analyzer)
        selection = selector.select(guard.client, {})
        return JSONResponse({
            "connected": True,
            "port": guard.port,
            "market_view": selection.market_view,
            "selected_symbols": selection.selected_symbols,
            "candidates_count": len(selection.candidates),
            "picks_count": len(selection.picks),
            "errors_count": len(selection.errors),
            "picks": [
                {
                    "rank": p.rank,
                    "symbol": p.symbol,
                    "selected": p.selected,
                    "action": p.action,
                    "confidence": p.confidence,
                    "score": p.score,
                }
                for p in selection.picks
            ],
            "errors": selection.errors,
            "candidates": {
                sym: {
                    "signal_action": c.signal.action,
                    "candle_bias": c.candle.bias,
                    "candle_score": c.candle.score,
                    "news_sentiment": c.news.sentiment,
                    "ai_action": c.ai.action,
                    "ai_confidence": c.ai.confidence,
                    "ai_summary": c.ai.summary,
                    "rsi": round(c.candle.rsi, 1),
                    "volume_profile": c.candle.volume_profile,
                    "volume_ratio": round(c.candle.volume_ratio, 2),
                }
                for sym, c in selection.candidates.items()
            },
        })
    finally:
        guard.client.disconnect_and_stop()


@app.post("/api/select")
def select(overrides: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    return _run_mode("select", run_selection_snapshot, overrides)


@app.post("/api/live/start")
def start_live(overrides: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    base_settings = load_settings()
    try:
        settings = _merge_settings(base_settings, overrides)
    except ValueError as exc:
        return JSONResponse(_config_error_payload(base_settings, "live", str(exc)), status_code=400)

    configure_logging(settings.log_level)
    runtime.start(settings)
    payload = _empty_payload(settings, mode="live", has_run=True)
    payload["runtime"] = _serialize_runtime()
    payload["guidance"] = [
        {
            "title": "自动交易已启动",
            "kind": "info",
            "steps": [
                "顶部总览会显示当前是否正在自动交易。",
                "实时日志区会持续输出轮次进度、选股和下单动作。",
                "如需停止，点击'停止自动交易'。",
            ],
        }
    ]
    return JSONResponse(payload)


@app.post("/api/live/stop")
def stop_live(overrides: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    settings = load_settings()
    configure_logging(settings.log_level)
    runtime.stop()
    payload = _empty_payload(settings, mode="live", has_run=True)
    payload["runtime"] = _serialize_runtime()
    return JSONResponse(payload)


@app.get("/api/live/status")
def live_status() -> JSONResponse:
    """Return current live trading portfolio status."""
    return JSONResponse(_serialize_runtime())


@app.post("/api/simulate")
def simulate(overrides: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    return _run_mode("simulate", run_simulation_snapshot, overrides)


@app.post("/api/backtest")
def backtest(overrides: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    return _run_mode("backtest", run_backtest_snapshot, overrides)


@app.post("/api/train")
def train(overrides: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    return _run_mode("train", run_training_snapshot, overrides)


def _serialize_paper(state=None) -> dict[str, Any]:
    if state is None:
        state = paper_runtime.snapshot()
    return {
        "running": state.running,
        "started_at": state.started_at,
        "stopped_at": state.stopped_at,
        "last_error": state.last_error,
        "starting_cash": state.starting_cash,
        "cash": state.cash,
        "realized_pnl": state.realized_pnl,
        "unrealized_pnl": state.unrealized_pnl,
        "total_equity": state.total_equity,
        "return_pct": state.return_pct,
        "equity_curve": state.equity_curve,
        "trades": state.trades,
        "symbols": state.symbols,
        "stock_universe": state.stock_universe,
        "cycle_count": state.cycle_count,
        "last_run_at": state.last_run_at,
        "status_msg": state.status_msg,
        "last_cycle_decisions": list(getattr(state, "last_cycle_decisions", []) or []),
        "market_phase": getattr(state, "market_phase", "") or "",
        "positions": {
            sym: {
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "realized_pnl": pos.realized_pnl,
            }
            for sym, pos in (state.positions or {}).items()
        },
    }


@app.post("/api/paper/start")
def paper_start(payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    base = load_settings()
    overrides: dict[str, Any] = {}
    starting_cash = 10000.0
    if payload:
        starting_cash = float(payload.get("paper_capital", 10000) or 10000)
        universe_raw = payload.get("paper_universe", "")
        if universe_raw:
            overrides["stock_universe"] = [s.strip().upper() for s in str(universe_raw).split(",") if s.strip()]
        # Forward strategy params from the main form if present
        for key in ("fast_sma", "slow_sma", "stop_loss_pct", "take_profit_pct",
                    "order_quantity", "max_position", "enable_ai_stock_selection",
                    "max_selected_symbols", "ai_selection_min_confidence",
                    "duration", "bar_size", "use_rth"):
            if key in payload and payload[key] not in (None, ""):
                overrides[key] = payload[key]
    try:
        settings = _merge_settings(base, overrides) if overrides else base
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    state = paper_runtime.start(settings, starting_cash)
    return JSONResponse(_serialize_paper(state))


@app.post("/api/paper/stop")
def paper_stop() -> JSONResponse:
    state = paper_runtime.stop()
    return JSONResponse(_serialize_paper(state))


@app.post("/api/paper/reset")
def paper_reset() -> JSONResponse:
    state = paper_runtime.reset()
    return JSONResponse(_serialize_paper(state))


@app.get("/api/paper/status")
def paper_status() -> JSONResponse:
    return JSONResponse(_serialize_paper())


def _find_available_port(host: str, start_port: int, attempts: int = 20) -> int:
    for port in range(start_port, start_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port found in range {start_port}-{start_port + attempts - 1}")


_LOCK_FILE = Path(__file__).parent / ".dashboard.lock"


def _is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def _get_pid_from_lock_file() -> int | None:
    if not _LOCK_FILE.exists():
        return None
    try:
        return int(_LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_process_running(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in result.stdout
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _acquire_lock() -> bool:
    """Try to acquire lock. If another instance is running, kill it."""
    existing_pid = _get_pid_from_lock_file()
    if existing_pid and _is_process_running(existing_pid):
        print(f"[Dashboard] Found old dashboard instance (PID {existing_pid}), killing it...")
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["taskkill", "/F", "/PID", str(existing_pid)], capture_output=True)
        else:
            os.kill(existing_pid, signal.SIGTERM)
        Path(_LOCK_FILE).unlink(missing_ok=True)

    # Write our PID
    _LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    Path(_LOCK_FILE).unlink(missing_ok=True)


def main() -> None:
    _acquire_lock()
    atexit.register(_release_lock)

    configure_logging(load_settings().log_level)

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    requested_port = int(os.getenv("DASHBOARD_PORT", "8765"))
    port = _find_available_port(host, requested_port)
    print(f"Dashboard URL: http://{host}:{port}")
    if port != requested_port:
        print(f"Port {requested_port} is busy, switched to {port}")
    uvicorn.run("broker.dashboard:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
