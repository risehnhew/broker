from __future__ import annotations

from broker.config import configure_logging, load_settings
from broker.trader import Trader


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    trader = Trader(settings)
    trader.run_forever()


if __name__ == "__main__":
    main()
