# CLAUDE.md - Agent Instructions for momentum-trading-bot

## Project Overview

Automated momentum day-trading bot for US equities (NYSE/NASDAQ).
Targets low-float momentum gappers with VWAP/EMA pullback entry strategy.
Built for small account growth ($500–$25k+) with strict risk management.

## Quick Start

```bash
pip install -e ".[dev]"
cp trading_bot/config/.env.example .env
# Fill in API keys in .env
trading-bot --mode paper          # Paper trading (default)
trading-bot --mode backtest       # Run backtest
pytest tests/ -v                  # Run tests
```

## Architecture

- **Sync polling loop** (60s default), not async
- **Pydantic BaseSettings** for all config validation with hard min/max bounds
- **Float data from yfinance** (Polygon lacks float field), cached 24h
- **Constructor dependency injection** — every component independently testable

### Data Flow

```
Scanner → Strategy.evaluate() → PositionSizer.calculate() → Broker.submit_order() → PortfolioManager
```

### Key Modules

| Module | Path | Responsibility |
|--------|------|---------------|
| Config | `trading_bot/config/settings.py` | Pydantic-validated config with risk bounds |
| Scanner | `trading_bot/scanners/momentum_gappers.py` | Find momentum gappers: price, gap, float, rvol, catalyst |
| Strategy | `trading_bot/strategies/pullback_vwap.py` | VWAP/EMA pullback entries, scale-out exits, trailing stops |
| Risk | `trading_bot/risk/position_sizer.py` | `shares = risk$ / stop_distance`, max positions, leverage, PDT |
| Circuit Breaker | `trading_bot/risk/circuit_breaker.py` | Halt on >5% drawdown, consecutive losses, API errors |
| Execution | `trading_bot/execution/alpaca_broker.py` | Alpaca-py TradingClient wrapper |
| Portfolio | `trading_bot/portfolio/manager.py` | Position lifecycle, scale-outs, trailing stops, CSV journal |
| Backtest | `trading_bot/backtest/engine.py` | pandas-based walk-forward simulation |

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
pytest tests/ -v --cov=trading_bot
pytest tests/test_risk.py -v        # Most critical tests
pytest tests/test_strategy.py -v    # Strategy signal tests
```

## Configuration

Config loads in layers: `config.yaml` → `.env` → environment variables.
Override any config value with env var prefix `TRADING_`, nested delimiter `__`.

Example: `TRADING_RISK__RISK_PER_TRADE_PCT=0.5`
