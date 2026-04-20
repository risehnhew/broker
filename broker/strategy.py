from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean


@dataclass(frozen=True)
class StrategySignal:
    action: str
    fast_sma: float
    slow_sma: float


class SmaCrossStrategy:
    def __init__(self, fast_window: int, slow_window: int) -> None:
        self.fast_window = fast_window
        self.slow_window = slow_window

    def evaluate(self, closes: list[float], position: int) -> StrategySignal:
        if len(closes) < self.slow_window:
            raise ValueError(f"行情数量不足，至少需要 {self.slow_window} 根 K 线")

        fast_sma = fmean(closes[-self.fast_window :])
        slow_sma = fmean(closes[-self.slow_window :])

        if fast_sma > slow_sma and position <= 0:
            return StrategySignal(action="BUY", fast_sma=fast_sma, slow_sma=slow_sma)
        if fast_sma < slow_sma and position > 0:
            return StrategySignal(action="SELL", fast_sma=fast_sma, slow_sma=slow_sma)
        return StrategySignal(action="HOLD", fast_sma=fast_sma, slow_sma=slow_sma)
