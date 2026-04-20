from __future__ import annotations

from broker.models import SimulationResult


def explain_error(error_text: str) -> str:
    reason = error_text.strip()

    if "code=162" in reason and "different IP address" in reason:
        return (
            "历史数据被 IBKR 拒绝，因为同一交易会话正在另一 IP 上使用。"
            "通常是同账户在别处还开着 TWS/IB Gateway、Client Portal 或手机 App。"
            "先关闭别处会话，再重新登录当前机器的 Paper。"
        )

    if "code=502" in reason:
        return "本机没有连上 TWS/IB Gateway 的 API 端口。请确认程序已启动，且 API Socket 已开启。"

    return reason


def render_header(title: str) -> str:
    line = "=" * 64
    return f"\n{line}\n{title}\n{line}"


def render_kv(label: str, value: str) -> str:
    return f"{label:<14} {value}"


def render_connection(host: str, port: int) -> str:
    return "\n".join(
        [
            render_header("IBKR Connected"),
            render_kv("Host", host),
            render_kv("Port", str(port)),
        ]
    )


def render_simulation_result(result: SimulationResult) -> str:
    return "\n".join(
        [
            render_header(f"Simulation {result.symbol}"),
            render_kv("Trades", str(result.trades)),
            render_kv("Round Trips", str(result.round_trips)),
            render_kv("Win Rate", f"{result.win_rate:.2f}%"),
            render_kv("Net Profit", f"{result.net_profit:.2f}"),
            render_kv("Final Equity", f"{result.final_equity:.2f}"),
            render_kv("Max Drawdown", f"{result.max_drawdown * 100:.2f}%"),
            render_kv("Open Position", str(result.open_position)),
            render_kv("Fast/Slow", f"{result.config.fast_sma}/{result.config.slow_sma}"),
            render_kv("Stop/Take", f"{result.config.stop_loss_pct:.2%} / {result.config.take_profit_pct:.2%}"),
        ]
    )


def render_friendly_error(symbol: str, error_text: str) -> str:
    return "\n".join(
        [
            render_header(f"Simulation Error {symbol}"),
            explain_error(error_text),
        ]
    )
