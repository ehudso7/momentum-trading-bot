# Momentum Day-Trading Bot

Automated momentum day-trading bot for US equities (NYSE/NASDAQ). Targets low-float momentum gappers using VWAP/EMA pullback entries, strict risk management, and Alpaca for execution.

---

**WARNING: This is high-risk software. Most day traders lose money. You can lose your entire account. Backtest results do NOT guarantee live performance. Start with paper trading only. Never trade money you cannot afford to lose.**

---

## Features

- **Momentum Scanner**: Finds low-float gappers with high relative volume and catalyst presence
- **VWAP Pullback Strategy**: Entries on pullback to VWAP/EMA9 with volume confirmation
- **Multiple Entry Setups**: VWAP pullback, EMA pullback, Opening Range Breakout (ORB), Red-to-Green, Breakout Continuation
- **Multi-Factor Confidence Scoring**: Time of day, gap fill risk, candle quality, momentum, EMA alignment, RSI, volume trend
- **Market Regime Detection**: SPY-based regime classifier (bullish, bearish, high-vol, range-bound, low-vol) auto-adjusts sizing/stops
- **AI Trading Advisor**: Rule-based expert system for entry/exit edge cases, daily planning, circuit breaker recommendations
- **Strict Risk Management**: 1% risk per trade, max 4x leverage, circuit breakers with auto-recovery
- **Correlation Checking**: Prevents concentrated sector/price risk across open positions (SIC codes + return correlation)
- **Scale-Out Exits**: 1/3 at 1:1 R:R, 1/3 at 2:1 R:R, trail remainder with ATR-based trailing stops
- **Hard Time Exit**: Flat all positions by 3:50 PM ET
- **Three Run Modes**: Backtest, Paper (default), Live
- **Trade Journal**: CSV logging of every trade with P&L tracking and daily summary reports
- **Webhook Notifications**: Slack/Discord alerts for trade opens/closes, circuit breaker events, daily summaries
- **System Health Monitoring**: Memory usage, tick rate, error rate, API health tracking
- **Resilient API Layer**: Retry with exponential backoff, rate limiting, error classification
- **Realistic Paper Broker**: Slippage model, margin simulation, stale order cleanup
- **Live Web Dashboard**: Real-time FastAPI dashboard with equity curve, positions, trades, health metrics

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

Edit `.env` with your API keys (see `.env.example` for detailed setup instructions):

```bash
# Required: Get a free key at https://polygon.io/dashboard/signup
POLYGON_API_KEY=your_key_here

# Required: Get free keys at https://app.alpaca.markets/signup
# Use Paper Trading keys first (switch to Paper Trading > API Keys)
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
```

> **Note:** Without API keys, the bot runs in local paper mode with simulated data. No real market connection is needed to explore the codebase or run tests.

### 4. Run

```bash
# Paper trading (default, recommended to start)
trading-bot --mode paper

# Backtest on historical data
trading-bot --mode backtest

# Live trading (requires explicit confirmation)
trading-bot --mode live

# Dashboard available at http://localhost:8080
# Disable dashboard: --dashboard-port 0
# Custom port: --dashboard-port 3000
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
Pre-market: Scanner (Polygon.io) → Watchlist
Market hours:
  Scanner → Regime Detection (SPY) → Strategy Evaluate (multi-setup)
    → AI Advisor (entry recommendation) → Correlation Check
      → Risk Check (position sizer: shares = risk$ / stop_distance)
        → Execution (Alpaca broker) → Portfolio Manager
          → Scale-outs, Trailing Stops, Journal, Notifications

Circuit Breaker monitors all activity (checked FIRST every tick).
Hard Time Exit checked SECOND (3:50 PM ET).
Health Monitor tracks system metrics continuously.
```

### Project Structure

```
trading_bot/
├── main.py                    # CLI entry point, orchestrator
├── dashboard/
│   ├── app.py                 # FastAPI dashboard server
│   ├── state.py               # Thread-safe state container
│   └── templates/index.html   # Dashboard HTML (Chart.js)
├── config/
│   ├── settings.py            # Pydantic config with risk bounds
│   ├── config.yaml            # Default values
│   └── .env.example           # API key template (with setup guide)
├── models/domain.py           # Shared data classes
├── data/
│   ├── polygon_client.py      # Polygon.io wrapper with rate limiting
│   ├── market_data.py         # Unified data interface
│   └── news_client.py         # Catalyst detection
├── scanners/
│   └── momentum_gappers.py    # Momentum scanner
├── strategies/
│   ├── base.py                # Strategy ABC
│   ├── pullback_vwap.py       # VWAP pullback + ORB + Red-to-Green strategy
│   ├── regime.py              # Market regime detection (SPY-based)
│   └── advisor.py             # Rule-based trading advisor (edge case mgmt)
├── risk/
│   ├── position_sizer.py      # Position sizing + risk checks
│   ├── circuit_breaker.py     # Safety circuit breaker with auto-recovery
│   └── correlation.py         # Sector/price correlation checker
├── execution/
│   ├── broker_base.py         # Broker ABC
│   ├── alpaca_broker.py       # Alpaca implementation
│   └── paper_broker.py        # In-memory paper broker with slippage model
├── portfolio/
│   └── manager.py             # Position lifecycle + journal
├── backtest/
│   └── engine.py              # Walk-forward backtesting
└── utils/
    ├── logger.py              # structlog setup
    ├── indicators.py          # VWAP, EMA, ATR, RSI, PSAR, Bollinger
    ├── helpers.py             # Market hours utilities
    ├── resilience.py          # Retry logic, rate limiting, error classification
    ├── health.py              # System health monitoring
    ├── notifications.py       # Webhook notifications (Slack, Discord, etc.)
    └── reports.py             # Daily summary report generation
```

## Dashboard

A live web dashboard starts automatically on port 8080 when running in paper or live mode.

```
http://localhost:8080       # Dashboard UI
http://localhost:8080/api/status         # JSON: full bot status
http://localhost:8080/api/positions      # JSON: open positions
http://localhost:8080/api/trades         # JSON: today's completed trades
http://localhost:8080/api/equity-history # JSON: equity curve data
http://localhost:8080/api/health         # JSON: system health metrics
http://localhost:8080/api/circuit-breaker # JSON: circuit breaker state
http://localhost:8080/api/docs          # Swagger API docs
```

The dashboard shows:
- Account equity, daily P&L, and daily return %
- Circuit breaker status with colored indicators
- Market regime badge
- Equity curve chart (Chart.js, auto-refreshes every 10s)
- Open positions table with R-multiple, trailing stops, scale-out progress
- Trade history table with P&L, R:R, hold time, exit reason
- System health metrics (uptime, memory, tick rate, API errors)
- Buying power and activity counts

To disable: `trading-bot --mode paper --dashboard-port 0`

## Testing

371 tests across 15 test modules covering all core modules:

```bash
pytest tests/ -v                          # All tests (371 tests)
pytest tests/test_risk.py -v              # Risk management (critical)
pytest tests/test_strategy.py -v          # Strategy signals
pytest tests/test_circuit_breaker_recovery.py -v  # Circuit breaker recovery
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT -- see [LICENSE](LICENSE) for details.
