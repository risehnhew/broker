from __future__ import annotations

import logging

from broker.ai_analysis import AIAnalyzer
from broker.config import configure_logging
from broker.config import load_settings
from broker.ib_client import IBClient
from broker.models import SimulationConfig
from broker.simulation import SimulationEngine


class Backtester:
    def __init__(self) -> None:
        self.settings = load_settings()
        configure_logging(self.settings.log_level)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = IBClient()
        self.ai_analyzer = (
            AIAnalyzer(self.settings.ai_base_url, self.settings.ai_api_key, self.settings.ai_model)
            if self.settings.enable_ai_analysis and self.settings.ai_api_key
            else None
        )

    def run(self) -> None:
        self.client.connect_and_start_any(
            host=self.settings.ib_host,
            ports=self.settings.ib_port_candidates,
            client_id=self.settings.ib_client_id + 100,
        )
        try:
            for symbol in self.settings.symbols:
                self._run_symbol(symbol)
        finally:
            self.client.disconnect_and_stop()

    def _run_symbol(self, symbol: str) -> None:
        bars = self.client.get_historical_bars(
            symbol=symbol,
            duration=self.settings.duration,
            bar_size=self.settings.bar_size,
            use_rth=self.settings.use_rth,
            timeout=30.0,
        )
        result = SimulationEngine(
            self.settings,
            SimulationConfig(
                fast_sma=self.settings.fast_sma,
                slow_sma=self.settings.slow_sma,
                stop_loss_pct=self.settings.stop_loss_pct,
                take_profit_pct=self.settings.take_profit_pct,
            ),
        ).run(symbol=symbol, bars=bars)

        self.logger.info(
            "BACKTEST %s | trades=%s | round_trips=%s | win_rate=%.2f%% | net_profit=%.2f | final_equity=%.2f | max_drawdown=%.2f%% | open_position=%s",
            result.symbol,
            result.trades,
            result.round_trips,
            result.win_rate,
            result.net_profit,
            result.final_equity,
            result.max_drawdown * 100,
            result.open_position,
        )
        if self.ai_analyzer:
            summary = self.ai_analyzer.summarize_training([result])
            if summary:
                self.logger.info("AI 回测摘要 %s | %s", symbol, summary)


def main() -> None:
    Backtester().run()


if __name__ == "__main__":
    main()
