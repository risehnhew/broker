# IBKR Auto Trading Example

Python project for Interactive Brokers `TWS / IB Gateway` API.

Current features:

- Connect to `TWS` or `IB Gateway`
- Pull historical bars
- Pull IBKR news headlines
- Analyze K-line patterns
- Run paper simulation and backtests
- Train parameter combinations on historical data
- Use `MiniMax-M2.7-highspeed` for structured analysis
- Local dashboard for readable simulation results

## Main commands

Install dependencies:

```bash
pip install -r requirements.txt
```

Run simulator:

```bash
python -m broker.simulator
```

Run backtest:

```bash
python -m broker.backtest
```

Run training:

```bash
python -m broker.train
```

Run dashboard:

```bash
python -m broker.dashboard
```

Then open:

```text
http://127.0.0.1:8765
```

## IBKR defaults

- `TWS Paper`: `7497`
- `TWS Live`: `7496`
- `IB Gateway Paper`: `4002`
- `IB Gateway Live`: `4001`

## Important note

This project is built for technical experimentation and paper trading first. Do not move to live trading before validating:

- strategy behavior
- risk controls
- market data permissions
- account session conflicts

## Dashboard

The dashboard is local only. It shows:

- connection state
- simulation result cards
- readable Chinese error messages
- one-click simulation trigger
