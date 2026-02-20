# CLAUDE.md - Agent Instructions for momentum-trading-bot

## Project Overview

Automated momentum day-trading bot for US equities (NYSE/NASDAQ).
Targets low-float momentum gappers with VWAP/EMA pullback entry strategy.
Built for small account growth ($500–$25k+) with strict risk management.
Deployed on Railway with live dashboard.

## Quick Start

```bash
pip install -e ".[dev]"
cp trading_bot/config/.env.example .env
# Fill in API keys in .env
trading-bot --mode paper          # Paper trading (default)
trading-bot --mode backtest       # Run backtest
pytest tests/ -v                  # Run tests (438 tests)
```

## Architecture

- **Adaptive sync polling loop** (10s–60s based on time of day), not async
- **Pydantic BaseSettings** for all config validation with hard min/max bounds
- **Multi-source data merging**: Polygon + Alpaca movers + Alpaca most-active + Yahoo Finance
- **Float data from yfinance** (Polygon lacks float field), cached 24h
- **Regime-adaptive strategy**: entry parameters adjust based on SPY market regime
- **Constructor dependency injection** — every component independently testable

### Data Flow

```
Scanner (4 data sources merged) → Filter Pipeline (price, gap, float, rvol)
  → Persistence Tracking → Regime Detection (SPY)
    → Strategy.evaluate() (5 setups × 3-bar lookback)
      → Advisor.recommend_entry() → CorrelationChecker
        → PositionSizer.calculate() → Broker.submit_order()
          → PortfolioManager (scale-outs, trailing stops, journal)
```

### Key Modules

| Module | Path | Responsibility |
|--------|------|---------------|
| Config | `trading_bot/config/settings.py` | Pydantic-validated config with risk bounds |
| Scanner | `trading_bot/scanners/momentum_gappers.py` | Multi-source scanner with persistence tracking |
| Alpaca Screener | `trading_bot/data/alpaca_screener.py` | Movers + most-active + batch snapshot enrichment |
| Yahoo Screener | `trading_bot/data/yahoo_screener.py` | Yahoo Finance day-gainers (no API key needed) |
| Market Data | `trading_bot/data/market_data.py` | Unified data interface (Polygon + yfinance fallback) |
| Strategy | `trading_bot/strategies/pullback_vwap.py` | 5 entry setups, regime-adaptive proximity, multi-bar lookback |
| Regime | `trading_bot/strategies/regime.py` | SPY-based regime detection + parameter adjustment tables |
| Advisor | `trading_bot/strategies/advisor.py` | Rule-based entry/exit/sizing recommendations |
| Risk | `trading_bot/risk/position_sizer.py` | `shares = risk$ / stop_distance`, max positions, leverage, PDT |
| Circuit Breaker | `trading_bot/risk/circuit_breaker.py` | Halt on >5% drawdown, consecutive losses, API errors |
| Correlation | `trading_bot/risk/correlation.py` | Sector/price correlation checker |
| Execution | `trading_bot/execution/alpaca_broker.py` | Alpaca-py TradingClient wrapper |
| Paper Broker | `trading_bot/execution/paper_broker.py` | Realistic paper broker with slippage |
| Portfolio | `trading_bot/portfolio/manager.py` | Position lifecycle, scale-outs, trailing stops, CSV journal |
| Dashboard | `trading_bot/dashboard/app.py` | FastAPI live dashboard (port 8080) |
| Backtest | `trading_bot/backtest/engine.py` | pandas-based walk-forward simulation |
| Indicators | `trading_bot/utils/indicators.py` | VWAP, EMA, ATR, RSI, PSAR, Bollinger |
| Resilience | `trading_bot/utils/resilience.py` | Retry with backoff, rate limiting, error classification |
| Health | `trading_bot/utils/health.py` | System health monitoring |
| Notifications | `trading_bot/utils/notifications.py` | Slack/Discord webhook alerts |

## Safety Rules (NON-NEGOTIABLE)

- **NEVER** remove or weaken risk limits (position sizing, stop losses, circuit breakers)
- **NEVER** default to live mode — paper mode must always be default
- **NEVER** commit API keys or secrets to the repository
- **NEVER** increase `risk_per_trade_pct` upper bound beyond 3%
- **NEVER** remove the live mode confirmation prompt
- **ALWAYS** test changes against `tests/test_risk.py` before merging
- Circuit breaker is checked FIRST in every tick, before any other logic
- Hard time exit (3:50 PM ET) is checked SECOND

## Testing

```bash
pytest tests/ -v --cov=trading_bot        # Full suite (438 tests)
pytest tests/test_risk.py -v              # Most critical tests
pytest tests/test_strategy.py -v          # Strategy signal tests
pytest tests/test_scanner.py -v           # Scanner pipeline
pytest tests/test_alpaca_screener.py -v   # Multi-source screener
pytest tests/test_regime.py -v            # Regime detection
```

## Configuration

Config loads in layers: `config.yaml` → `.env` → environment variables.
Override any config value with env var prefix `TRADING_`, nested delimiter `__`.

Example: `TRADING_RISK__RISK_PER_TRADE_PCT=0.5`

### Railway Environment Variables

Required:
- `POLYGON_API_KEY` — Polygon.io API key
- `ALPACA_API_KEY` — Alpaca paper trading API key
- `ALPACA_API_SECRET` — Alpaca paper trading API secret

Recommended:
- `TRADING_LOG_JSON=true` — JSON logs for Railway log viewer
- `TRADING_RUN_MODE=paper` — Explicit paper mode (default anyway)
