from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisSnapshot:
    base_action: str
    fast_sma: float
    slow_sma: float
    candle_bias: str
    candle_score: int
    news_score: int
    news_sentiment: str
    ai_action: str
    ai_confidence: int
    ai_summary: str
    ai_used: bool = False  # True only when MiniMax was actually called


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    action: str
    reason: str


@dataclass(frozen=True)
class SimulationConfig:
    fast_sma: int
    slow_sma: int
    stop_loss_pct: float
    take_profit_pct: float


@dataclass(frozen=True)
class SimulationTrade:
    symbol: str
    timestamp: str
    action: str
    quantity: int
    price: float
    reason: str
    realized_pnl: float | None
    position_after: int


@dataclass(frozen=True)
class EquityPoint:
    timestamp: str
    equity: float
    close_price: float
    position: int
    cash: float


@dataclass(frozen=True)
class SimulationResult:
    symbol: str
    trades: int
    round_trips: int
    win_rate: float
    net_profit: float
    final_equity: float
    max_drawdown: float
    open_position: int
    config: SimulationConfig
    equity_curve: list[EquityPoint]
    trade_log: list[SimulationTrade]


@dataclass(frozen=True)
class SessionAttempt:
    attempt: int
    status: str
    message: str


@dataclass(frozen=True)
class IndicatorDetail:
    name: str
    value: str
    interpretation: str
    explanation: str


@dataclass(frozen=True)
class EducationalStep:
    step: int
    title: str
    content: str
    indicators: list[IndicatorDetail]
    verdict: str


@dataclass(frozen=True)
class EducationalReport:
    symbol: str
    steps: list[EducationalStep]
    final_action: str
    final_confidence: int
    summary_zh: str
