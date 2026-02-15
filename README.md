# Momentum Day-Trading Bot

Automated momentum day-trading bot for US equities (NYSE/NASDAQ). Targets low-float momentum gappers using VWAP/EMA pullback entries, strict risk management, and Alpaca for execution.

---

**WARNING: This is high-risk software. Most day traders lose money. You can lose your entire account. Backtest results do NOT guarantee live performance. Start with paper trading only. Never trade money you cannot afford to lose.**

---

## Features

- **Momentum Scanner**: Finds low-float gappers with high relative volume and catalyst presence
- **VWAP Pullback Strategy**: Entries on pullback to VWAP/EMA9 with volume confirmation
- **Strict Risk Management**: 1% risk per trade, max 4x leverage, circuit breakers
- **Scale-Out Exits**: 1/3 at 1:1 R:R, 1/3 at 2:1 R:R, trail remainder
- **Hard Time Exit**: Flat all positions by 3:50 PM ET
- **Three Run Modes**: Backtest, Paper (default), Live
- **Trade Journal**: CSV logging of every trade with P&L tracking

## Quick Start

### 1. Prerequisites

- Python 3.10+
- [Polygon.io API key](https://polygon.io/dashboard/signup) (free tier works)
- [Alpaca API keys](https://app.alpaca.markets/signup) (paper trading is free)

### 2. Install

```bash
git clone https://github.com/ehudso7/momentum-trading-bot.git
cd momentum-trading-bot
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
# .venv\Scripts\activate    # Windows
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp trading_bot/config/.env.example .env
```

Edit `.env` with your API keys:

```
POLYGON_API_KEY=your_key_here
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
```

### 4. Run

```bash
# Paper trading (default, recommended to start)
trading-bot --mode paper

# Backtest on historical data
trading-bot --mode backtest

# Live trading (requires explicit confirmation)
trading-bot --mode live
```

## Configuration

All settings are in `trading_bot/config/config.yaml`. Override via environment variables with prefix `TRADING_`:

```bash
TRADING_RISK__RISK_PER_TRADE_PCT=0.5   # Risk 0.5% per trade
TRADING_SCANNER__MIN_GAP_PCT=15         # Minimum 15% gap
TRADING_LOG_LEVEL=DEBUG                  # Verbose logging
```

### Key Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `risk_per_trade_pct` | 1.0% | Risk per trade as % of equity |
| `max_daily_risk_pct` | 3.0% | Max cumulative daily risk |
| `max_open_positions` | 4 | Max concurrent positions |
| `max_leverage` | 4.0x | Max effective leverage |
| `drawdown_circuit_breaker_pct` | 5.0% | Halt trading at this daily drawdown |
| `hard_time_exit` | 15:50 | Close all positions (3:50 PM ET) |

### Scanner Filters

| Filter | Default | Description |
|--------|---------|-------------|
| `min_gap_pct` | 10% | Minimum gap-up percentage |
| `min_price` / `max_price` | $2 / $20 | Price range |
| `max_float_shares` | 50M | Maximum public float |
| `min_relative_volume` | 5x | Minimum relative volume |

## Architecture

```
Scanner (Polygon.io)
  → Strategy (VWAP pullback evaluation)
    → Risk Check (position sizer: shares = risk$ / stop_distance)
      → Execution (Alpaca broker)
        → Portfolio Manager (scale-outs, trailing stops, journal)

Circuit Breaker monitors all activity and halts on safety breaches.
```

### Project Structure

```
trading_bot/
├── main.py                    # CLI entry point, orchestrator
├── config/
│   ├── settings.py            # Pydantic config with risk bounds
│   ├── config.yaml            # Default values
│   └── .env.example           # API key template
├── models/domain.py           # Shared data classes
├── data/
│   ├── polygon_client.py      # Polygon.io wrapper
│   ├── market_data.py         # Unified data interface
│   └── news_client.py         # Catalyst detection
├── scanners/
│   └── momentum_gappers.py    # Momentum scanner
├── strategies/
│   ├── base.py                # Strategy ABC
│   └── pullback_vwap.py       # VWAP pullback strategy
├── risk/
│   ├── position_sizer.py      # Position sizing + risk checks
│   └── circuit_breaker.py     # Safety circuit breaker
├── execution/
│   ├── broker_base.py         # Broker ABC
│   ├── alpaca_broker.py       # Alpaca implementation
│   └── paper_broker.py        # In-memory paper broker
├── portfolio/
│   └── manager.py             # Position lifecycle + journal
├── backtest/
│   └── engine.py              # Walk-forward backtesting
└── utils/
    ├── logger.py              # structlog setup
    ├── indicators.py          # VWAP, EMA, ATR, PSAR
    └── helpers.py             # Market hours utilities
```

## Testing

```bash
pytest tests/ -v                          # All tests
pytest tests/test_risk.py -v              # Risk management (critical)
pytest tests/ -v --cov=trading_bot        # With coverage
```

## Docker

```bash
docker compose build
docker compose up -d          # Run in background
docker compose logs -f        # View logs
docker compose down           # Stop
```

## Data Sources

| Source | Purpose | Cost |
|--------|---------|------|
| Polygon.io | Real-time snapshots, gainers, news | Free tier (5 API calls/min) |
| yfinance | Float data, historical bars (backtest) | Free |
| Alpaca | Order execution, account management | Free (paper + live) |

## Risk Disclaimer

This software is provided "as is" without warranty of any kind. Trading stocks, especially with leverage, involves substantial risk of loss. This bot is for educational and paper trading purposes. The authors are not responsible for any financial losses incurred through the use of this software. Always:

- Start with paper trading
- Understand all risk parameters before using real money
- Never trade with money you cannot afford to lose
- Past performance and backtests do not guarantee future results
- Consult a financial advisor before making investment decisions

## License

MIT
