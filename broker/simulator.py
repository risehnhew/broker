from __future__ import annotations

import logging

from broker.config import configure_logging
from broker.config import load_settings
from broker.simulation_service import run_simulation_snapshot
from broker.ui import render_connection
from broker.ui import render_friendly_error


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("Simulator")
    logging.getLogger("IBClient").setLevel(logging.ERROR)
    logger.setLevel(logging.ERROR)
    snapshot = run_simulation_snapshot(settings)

    if snapshot["connected"]:
        print(render_connection(settings.ib_host, snapshot["port"] or settings.ib_port))

    for item in snapshot["results"]:
        print(
            "\n".join(
                [
                    "",
                    "=" * 64,
                    f"Simulation {item['symbol']}",
                    "=" * 64,
                    f"Trades         {item['trades']}",
                    f"Round Trips    {item['round_trips']}",
                    f"Win Rate       {item['win_rate']:.2f}%",
                    f"Net Profit     {item['net_profit']:.2f}",
                    f"Final Equity   {item['final_equity']:.2f}",
                    f"Max Drawdown   {item['max_drawdown_pct']:.2f}%",
                    f"Open Position  {item['open_position']}",
                    f"Fast/Slow      {item['fast_sma']}/{item['slow_sma']}",
                ]
            )
        )

    for item in snapshot["errors"]:
        print(render_friendly_error(item["symbol"], item["raw"]))


if __name__ == "__main__":
    main()
