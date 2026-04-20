from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any

_TRADE_LOG_FILE = Path(__file__).parent / "trade_history.csv"

_TRADE_LOG_COLUMNS = [
    "timestamp",
    "mode",
    "symbol",
    "action",
    "quantity",
    "price",
    "reason",
    "realized_pnl",
    "position_after",
    "net_profit_cumulative",
    "equity",
]


def _ensure_header() -> None:
    if not _TRADE_LOG_FILE.exists():
        with open(_TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_TRADE_LOG_COLUMNS)
            writer.writeheader()


def append_trade(
    symbol: str,
    action: str,
    quantity: int,
    price: float,
    reason: str,
    realized_pnl: float | None,
    position_after: int,
    equity: float,
    mode: str = "unknown",
    net_profit_cumulative: float = 0.0,
) -> None:
    _ensure_header()
    with open(_TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_TRADE_LOG_COLUMNS)
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "price": round(price, 4),
            "reason": reason,
            "realized_pnl": "" if realized_pnl is None else round(realized_pnl, 2),
            "position_after": position_after,
            "net_profit_cumulative": round(net_profit_cumulative, 2),
            "equity": round(equity, 2),
        })


def append_trades_from_result(result: Any, mode: str = "sim") -> int:
    """从 SimulationResult 批量写入交易记录。返回写入条数。"""
    _ensure_header()
    count = 0
    cumulative = 0.0
    with open(_TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_TRADE_LOG_COLUMNS)
        for trade in result.trade_log:
            pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
            cumulative += pnl
            writer.writerow({
                "timestamp": trade.timestamp,
                "mode": mode,
                "symbol": trade.symbol,
                "action": trade.action,
                "quantity": trade.quantity,
                "price": round(trade.price, 4),
                "reason": trade.reason,
                "realized_pnl": "" if trade.realized_pnl is None else round(trade.realized_pnl, 2),
                "position_after": trade.position_after,
                "net_profit_cumulative": round(cumulative, 2),
                "equity": round(result.final_equity, 2),
            })
            count += 1
    return count
