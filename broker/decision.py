from __future__ import annotations

from broker.config import Settings
from broker.models import AnalysisSnapshot
from broker.models import Decision


class DecisionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decide(self, snapshot: AnalysisSnapshot, position: int) -> Decision:
        if snapshot.base_action == "BUY":
            if snapshot.candle_bias == "BEARISH":
                return Decision(action="HOLD", reason="bearish_candle")
            if snapshot.news_score < self.settings.news_min_sentiment_to_buy:
                return Decision(action="HOLD", reason="weak_news")
            if snapshot.ai_used:
                if snapshot.ai_confidence < self.settings.ai_min_confidence:
                    return Decision(action="HOLD", reason="low_ai_confidence")
                if snapshot.ai_action != "BUY":
                    return Decision(action="HOLD", reason="ai_not_buy")
            return Decision(action="BUY", reason="signal_confirmed")

        if snapshot.base_action == "SELL" and position > 0:
            if snapshot.ai_used:
                if snapshot.ai_confidence >= self.settings.ai_min_confidence and snapshot.ai_action not in {"SELL", "HOLD"}:
                    return Decision(action="HOLD", reason="ai_blocks_sell")
            return Decision(action="SELL", reason="exit_signal")

        return Decision(action="HOLD", reason="no_signal")
