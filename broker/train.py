from __future__ import annotations

import logging

from broker.ai_analysis import AIAnalyzer
from broker.config import configure_logging
from broker.config import load_settings
from broker.ib_client import IBClient
from broker.models import SimulationConfig
from broker.models import SimulationResult
from broker.simulation import SimulationEngine


def _score(result: SimulationResult) -> float:
    return result.net_profit - result.max_drawdown * 10000 + result.win_rate * 10


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("Trainer")
    client = IBClient()
    ai_analyzer = (
        AIAnalyzer(settings.ai_base_url, settings.ai_api_key, settings.ai_model)
        if settings.enable_ai_analysis and settings.ai_api_key
        else None
    )

    client.connect_and_start_any(settings.ib_host, settings.ib_port_candidates, settings.ib_client_id + 300)
    try:
        all_results: list[SimulationResult] = []
        for symbol in settings.symbols:
            bars = client.get_historical_bars(
                symbol=symbol,
                duration=settings.duration,
                bar_size=settings.bar_size,
                use_rth=settings.use_rth,
                timeout=30.0,
            )
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
                            result = engine.run(symbol, bars)
                            all_results.append(result)

        ranked = sorted(all_results, key=_score, reverse=True)
        for item in ranked[:10]:
            logger.info(
                "TRAIN TOP %s | fast=%s slow=%s stop=%.3f take=%.3f | profit=%.2f | drawdown=%.2f%% | win_rate=%.2f%% | trades=%s",
                item.symbol,
                item.config.fast_sma,
                item.config.slow_sma,
                item.config.stop_loss_pct,
                item.config.take_profit_pct,
                item.net_profit,
                item.max_drawdown * 100,
                item.win_rate,
                item.trades,
            )

        if ai_analyzer and ranked:
            summary = ai_analyzer.summarize_training(ranked)
            if summary:
                logger.info("AI TRAIN SUMMARY | %s", summary)
    finally:
        client.disconnect_and_stop()


if __name__ == "__main__":
    main()
