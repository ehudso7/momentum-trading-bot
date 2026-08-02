<!-- EH-GOV:BEGIN GENERATED source=ehudso7/project-governance version=1.2.0 — do not edit inside this block; edits will be overwritten by `ehgov sync`. Project-owned content belongs in the PROJECT block. -->
# CLAUDE.md — Momentum Trading Bot

Governance: `ehudso7/project-governance` v1.2.0 · profile `regulated-system` · criticality `critical`

You are working in `momentum-trading-bot` (ai-agent-system). This project is governed: how you work here is defined by the pinned governance version above, not by ad-hoc judgment. `AGENTS.md` holds the cross-agent constitution; this file adds Claude Code specifics.

## Operating rules

1. **Inspect before changing.** Read the relevant code, tests, and docs before editing. Never edit a file you have not read.
2. **Classify the task first.** Every task is one of: `trivial`, `linear`, `multi-step`, `graph-required`, `high-risk-controlled`. State your classification before starting non-trivial work.
3. **Use graph workflows for non-trivial work.** For `multi-step` and above, identify the applicable workflow graph (see `GRAPH.md` and the table below), follow its nodes in order, and respect its gates. Do not collapse a graph-required workflow into one monolithic edit.
4. **Track state.** For graph-required work, create or update a task state record under `.governance/evidence/` (execution record per run). Distinguish `planned` → `implemented` → `tested` → `verified` explicitly; never report a later state than you have evidence for.
5. **Respect this repository's architecture.** Match existing patterns, module boundaries, and naming. Architecture changes go through the `architecture-review` graph.
6. **Validate before claiming completion.** Run the project's build/lint/test commands and report their real exit codes. Never claim success without command output backing it. A completion claim without verification evidence is a policy violation (`ai-agents.unverified-completion`).
7. **Produce evidence.** Non-trivial work produces an evidence bundle in `.governance/evidence/` (see `EVIDENCE.md`).
8. **Preserve unrelated work.** Never revert, reformat, or "clean up" code outside the task scope. Never discard uncommitted changes you did not author.
9. **Human approval is required for:** `production-deployment`, `destructive-migration`, `secrets`, `billing`, `authentication`, `authorization`. Request approval at those points and only those points — do not pad the session with unnecessary confirmations elsewhere.
10. **Stop on destructive or ambiguous high-risk operations.** If a step would drop data, rewrite history, touch secrets/billing/auth, or its intent is ambiguous, stop and ask.
11. **Full replacements when needed.** If an edit is too tangled for a patch, provide the complete replacement file rather than a partial diff that might corrupt it.
12. **Work checkpoint by checkpoint.** Finish and verify one graph node before moving to the next. Report blockers honestly instead of routing around them silently.

## Default workflow graphs

| Task kind | Graph |
|---|---|
| feature | `feature-development` |
| bug | `bug-resolution` |
| architecture | `architecture-review` |
| release | `production-release` |
| incident | `incident-response` |
| audit | `repository-audit` |
| security | `security-review` |
| diligence | `acquisition-diligence` |
| dependency | `dependency-upgrade` |

Graph definitions live in the governance source (`graphs/`); render one with `ehgov graph render <id>`.

## Sensitive paths

Changes under these paths trigger sensitive-path policies (extra review, approval, or denial):

- `.github/workflows/**`
- `**/migrations/**`
- `infra/**`
- `**/*auth*`
- `**/*billing*`
- `**/*permission*`
- `**/*secret*`

## Policies in force

Required policy sets: `base`, `security`, `delivery`, `ai-agents`. Machine-readable definitions live in the governance source (`policies/`); run `ehgov inspect` for the resolved list.
<!-- EH-GOV:END GENERATED -->

<!-- EH-GOV:BEGIN PROJECT -->
# CLAUDE.md - Agent Instructions for momentum-trading-bot

## Project Overview

Automated momentum day-trading bot for US equities (NYSE/NASDAQ).
Targets low-float momentum gappers with VWAP/EMA pullback entry strategy.
Built for account growth with strict risk management (default $100k starting capital).
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
<!-- EH-GOV:END PROJECT -->
