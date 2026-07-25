# Momentum Day-Trading Bot

Automated momentum day-trading bot for US equities (NYSE/NASDAQ). Targets low-float momentum gappers using VWAP/EMA pullback entries with elite-level adaptive scanning, regime-aware strategy, and strict risk management. Executes via Alpaca.

---

**WARNING: This is high-risk software. Most day traders lose money. You can lose your entire account. Backtest results do NOT guarantee live performance. Start with paper trading only. Never trade money you cannot afford to lose.**

---

## Current release status: private paper launch

This project is deployed as a **single-owner, paper-trading** application. It is
not a public product, not an investment-advisory service, and not accepting
users. Public signup, billing, share links, growth projections, demo signals,
and mobile order routing are disabled.

- Authoritative contract: [`docs/PRIVATE_PAPER_LAUNCH.md`](docs/PRIVATE_PAPER_LAUNCH.md)
- Pre-deploy checklist: [`docs/LAUNCH_CHECKLIST.md`](docs/LAUNCH_CHECKLIST.md)
- Operator runbook: [`docs/LAUNCH_RUNBOOK.md`](docs/LAUNCH_RUNBOOK.md)

**Live-money trading is blocked in code**, not merely by configuration. Setting
`TRADING_RUN_MODE=live` is not sufficient: `_assert_live_evidence_gate` in
`trading_bot/main.py` raises before an `AlpacaBroker` is constructed unless the
paper journal shows ≥100 closed trades across ≥20 trading days with positive
expectancy, profit factor ≥1.25, and drawdown ≤5%.

The SaaS/billing layer described below is **built but not launched**, and stays
that way until the public-product gate in the contract passes legal review.

---

## Features

### Scanning & Data
- **Multi-Source Momentum Scanner**: Merges Polygon, Alpaca movers, Alpaca most-active, and Yahoo Finance for maximum coverage
- **Adaptive Scan Interval**: 10s during opening drive, 15s power zone, 30s active hours, 60s dead zone — mirrors how elite traders allocate attention
- **Candidate Persistence Tracking**: Symbols appearing in consecutive scans get score boosts (building momentum = higher conviction)
- **Catalyst Detection**: Scans news headlines for FDA, earnings, merger keywords via Polygon

### Strategy
- **5 Entry Setups**: VWAP pullback, EMA pullback, Opening Range Breakout (ORB), Red-to-Green, Breakout Continuation
- **Regime-Adaptive Entry Zones**: Proximity thresholds widen/tighten based on market regime (1.8x wider in low vol, 0.7x tighter in bearish)
- **Multi-Bar Signal Lookback**: Checks last 3 bars for valid entries — catches setups between scan ticks
- **Multi-Bar Volume Confirmation**: Any of the last 3 bars with above-average volume counts as confirmation
- **Multi-Factor Confidence Scoring**: Time of day, gap fill risk, candle quality, momentum, EMA alignment, RSI, volume trend, relative volume strength (9 factors)

### Risk Management
- **Position Sizing**: `shares = risk$ / stop_distance`, never risking more than configured % per trade
- **Circuit Breaker**: Halts trading on >5% drawdown, consecutive losses, or API errors — with auto-recovery
- **Market Regime Detection**: SPY-based classifier (bullish, bearish, high-vol, range-bound, low-vol) auto-adjusts sizing, stops, and entry parameters
- **Correlation Checking**: Prevents concentrated sector/price risk across open positions (SIC codes + return correlation)
- **AI Trading Advisor**: Rule-based expert system for entry/exit edge cases, daily planning, circuit breaker recommendations
- **Scale-Out Exits**: 1/3 at 1:1 R:R, 1/3 at 2:1 R:R, trail remainder with ATR-based trailing stops
- **Momentum Exhaustion**: RSI > 80 with extended run triggers protective exit
- **Hard Time Exit**: Flat all positions by 3:50 PM ET
- **PDT Protection**: Warns when approaching pattern day trader limits

### Infrastructure
- **Three Run Modes**: Backtest, Paper (default), Live
- **Live Web Dashboard**: Real-time FastAPI dashboard with equity curve, positions, trades, health metrics
- **Trade Journal**: CSV logging of every trade with P&L tracking and daily summary reports
- **Webhook Notifications**: Slack/Discord alerts for trade opens/closes, circuit breaker events, daily summaries
- **System Health Monitoring**: Memory usage, tick rate, error rate, API health tracking
- **Resilient API Layer**: Retry with exponential backoff, rate limiting, error classification (auth errors not retried)
- **Realistic Paper Broker**: Slippage model, margin simulation, stale order cleanup
- **Docker + Railway Ready**: Production Dockerfile with health checks, signal handling, non-root user

## SaaS quickstart — DEFERRED, not part of the current release

> **Not launched.** The private paper launch disables public signup and billing.
> Following this section would stand up a monetized public surface ahead of the
> legal review the contract requires. It is retained as reference for a future
> public launch only. See the release-status section above.

The repo also ships a public-facing trading-signal SaaS layer
(`trading_bot.saas` + `trading_bot.api.server`) that exposes
read-only signal recommendations behind an API-key + Stripe billing
boundary. The SaaS layer is isolated from the live trading core —
it never imports the scanner, the broker, or any execution module.

Should the public gate ever pass, the path from "deployed" to "real users":

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Sanity-check the billing env (never prints raw secrets)
trading-bot-billing-verify --strict

# 3. Generate the first signal report
TRADING_SAAS_DATA_MODE=demo python -m trading_bot.saas generate

# 4. Issue a free API key (operator only — raw key printed once)
python -m trading_bot.api.keys issue --tier free --label tester

# 5. Run the smoke test against the deployed surface
python -m trading_bot.api.smoke \
    --base-url https://<your-host> \
    --api-key  <issued-key>

# 6. Inspect the persistent webhook event log (no raw keys logged)
python -m trading_bot.api.keys webhook-events --limit 20
```

Useful URLs once deployed:

| Path                     | Purpose                                |
|--------------------------|----------------------------------------|
| `GET /health`            | Liveness probe — no auth               |
| `GET /launch`            | Public read-only product preview HTML  |
| `GET /signals/latest`    | Latest signal report (free or premium) |
| `GET /signals/history`   | Premium-only — list of report dates    |
| `GET /signals/{date}`    | Premium-only — full report for a date  |
| `POST /billing/checkout` | Authenticated free → premium upgrade   |
| `POST /webhook/stripe`   | Stripe webhook (server-to-server only) |

For the full operator runbook see [docs/LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md)
and [docs/stripe-zero-dollar-test.md](docs/stripe-zero-dollar-test.md).

### Trading-core quick start (paper-trading)

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

### 5. Deploy to Railway

```bash
# Push to GitHub, then connect repo in Railway dashboard
# Set these environment variables in Railway:
POLYGON_API_KEY=...
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
TRADING_LOG_JSON=true
```

Railway auto-detects the Dockerfile and deploys. The dashboard is accessible on the assigned Railway URL.

For the SaaS API surface (issuing API keys, mounting a persistent
manifest volume, the production env-var set), see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Before flipping real
traffic on, follow the **Launch Day Checklist** at the bottom of
that document — it walks through `railway.toml` lockdown, env-var
setup, manifest issuance, test-key cleanup, the
`python -m trading_bot.api.launch_check` verifier, the production
HTTP smoke test, and the Stripe wiring order.

## Configuration

All settings are in `trading_bot/config/config.yaml`. Override via environment variables with prefix `TRADING_`:

```bash
TRADING_RISK__RISK_PER_TRADE_PCT=0.5   # Risk 0.5% per trade
TRADING_SCANNER__MIN_GAP_PCT=5         # Minimum 5% gap
TRADING_LOG_LEVEL=DEBUG                # Verbose logging
```

### Key Risk Parameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `risk_per_trade_pct` | 1.0% | 0.1–3.0% | Risk per trade as % of equity |
| `max_daily_risk_pct` | 3.0% | 1.0–10.0% | Max cumulative daily risk |
| `max_open_positions` | 4 | 1–10 | Max concurrent positions |
| `max_leverage` | 4.0x | 1.0–4.0x | Max effective leverage |
| `drawdown_circuit_breaker_pct` | 5.0% | 2.0–10.0% | Halt trading at this daily drawdown |
| `hard_time_exit` | 15:50 | — | Close all positions (3:50 PM ET) |

### Scanner Filters

| Filter | Default | Description |
|--------|---------|-------------|
| `min_gap_pct` | 4% | Minimum gap-up percentage |
| `min_price` / `max_price` | $2 / $50 | Price range |
| `max_float_shares` | 50M | Maximum public float |
| `min_relative_volume` | 2x | Minimum relative volume vs 20-day avg |

### Adaptive Scan Intervals

The bot automatically adjusts scan frequency based on time of day:

| Window | Interval | Rationale |
|--------|----------|-----------|
| 9:30–10:00 AM | 10s | Opening drive — maximum opportunity |
| 10:00–10:30 AM | 15s | Power zone — high-probability setups |
| 10:30–11:30 AM | 30s | Active morning — moderate scanning |
| 11:30 AM–1:00 PM | 60s | Dead zone — low probability |
| 1:00–3:30 PM | 30s | Afternoon push — second wind |
| 3:30–4:00 PM | 60s | Close — manage only, no new entries |

## Architecture

```
Data Sources (merged):
  Polygon.io ─┐
  Alpaca Movers ─┤
  Alpaca Most-Active ─┼─→ Scanner (filter pipeline) → Candidates
  Yahoo Finance ─┘         ↓
                    Regime Detection (SPY) → Adaptive Parameters
                           ↓
                    Strategy Evaluate (5 setups × 3 bar lookback)
                           ↓
                    AI Advisor (entry recommendation)
                           ↓
                    Correlation Check → Risk Check (position sizer)
                           ↓
                    Execution (Alpaca broker)
                           ↓
                    Portfolio Manager → Scale-outs, Trailing Stops
                           ↓
                    Journal, Notifications, Dashboard

Circuit Breaker monitors all activity (checked FIRST every tick).
Hard Time Exit checked SECOND (3:50 PM ET).
Health Monitor tracks system metrics continuously.
Scan interval adapts to time of day (10s–60s).
```

### Project Structure

```
trading_bot/
├── main.py                    # CLI entry point, orchestrator, adaptive loop
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
│   ├── alpaca_screener.py     # Alpaca movers + most-active + batch snapshots
│   ├── yahoo_screener.py      # Yahoo Finance day-gainers (no API key needed)
│   ├── market_data.py         # Unified data interface (live + backtest)
│   └── news_client.py         # Catalyst detection from news headlines
├── scanners/
│   └── momentum_gappers.py    # Multi-source scanner with persistence tracking
├── strategies/
│   ├── base.py                # Strategy ABC
│   ├── pullback_vwap.py       # VWAP/EMA/ORB/R2G/Breakout with regime adaptation
│   ├── regime.py              # Market regime detection + adjustment tables
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
│   └── manager.py             # Position lifecycle + CSV journal
├── backtest/
│   └── engine.py              # Walk-forward backtesting
└── utils/
    ├── logger.py              # structlog setup (JSON support)
    ├── indicators.py          # VWAP, EMA, ATR, RSI, PSAR, Bollinger
    ├── helpers.py             # Market hours, holidays, timezone utilities
    ├── resilience.py          # Retry logic, rate limiting, error classification
    ├── health.py              # System health monitoring
    ├── notifications.py       # Webhook notifications (Slack, Discord)
    └── reports.py             # Daily summary report generation
```

## Dashboard

A live web dashboard starts automatically on port 8080 when running in paper or live mode.

```
http://localhost:8080                    # Dashboard UI
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
- Market regime badge with volatility indicator
- Equity curve chart (Chart.js, auto-refreshes every 10s)
- Open positions table with R-multiple, trailing stops, scale-out progress
- Trade history table with P&L, R:R, hold time, exit reason
- Scanner stats (candidates found, last scan time)
- System health metrics (uptime, memory, tick rate, API errors)
- Buying power and activity counts

To disable: `trading-bot --mode paper --dashboard-port 0`

## Testing

438 tests across 20 test modules covering all core modules:

```bash
pytest tests/ -v                          # All tests (438 tests)
pytest tests/test_risk.py -v              # Risk management (critical)
pytest tests/test_strategy.py -v          # Strategy signals
pytest tests/test_scanner.py -v           # Scanner pipeline
pytest tests/test_regime.py -v            # Regime detection
pytest tests/test_alpaca_screener.py -v   # Multi-source screener
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
| Polygon.io | Real-time snapshots, gainers, news, aggregates | Free tier (5 API calls/min) |
| Alpaca | Movers, most-active, batch snapshots, execution | Free (paper + live) |
| Yahoo Finance | Day-gainers screener, float data, historical bars | Free (no API key) |
| yfinance | Float shares, avg volume, backtest data | Free |

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
