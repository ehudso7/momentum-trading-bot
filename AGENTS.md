# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a Python-based automated momentum day-trading bot. It has a single service: the trading bot process which includes a FastAPI dashboard (port 8080). No database or external containers are required. See `CLAUDE.md` for full architecture and `README.md` for setup/run/test commands.

### Running tests

Tests require the `tzdata` Python package (timezone data for `US/Eastern`). The update script installs it automatically.

**Important:** Several `TRADING_*` environment variables may be injected by the cloud environment with trailing whitespace/tab characters. These cause Pydantic validation errors in tests that create `AppConfig` instances. To run tests reliably, unset them:

```bash
env -u TRADING_RUN_MODE -u TRADING_LOG_JSON -u TRADING_LOG_LEVEL \
    -u TRADING_STARTING_CAPITAL -u TRADING_RISK__RISK_PER_TRADE_PCT \
    -u TRADING_RISK__MAX_OPEN_POSITIONS -u TRADING_RISK__MAX_DAILY_RISK_PCT \
    -u TRADING_RISK__DRAWDOWN_CIRCUIT_BREAKER_PCT \
    -u TRADING_SCANNER__SCAN_INTERVAL_SECONDS -u TRADING_SCANNER__MIN_GAP_PCT \
    -u TRADING_SCANNER__MIN_RELATIVE_VOLUME -u TRADING_SCANNER__MAX_FLOAT_SHARES \
    -u TRADING_SCANNER__MIN_PRICE -u TRADING_SCANNER__MAX_PRICE \
    pytest tests/ -v
```

All 442 tests should pass. No linter is configured for this project.

### Running the application

```bash
trading-bot --mode paper --dashboard-port 8080
```

The dashboard is at `http://localhost:8080`. API docs at `http://localhost:8080/api/docs`. The bot connects to Alpaca paper trading (via `ALPACA_API_KEY`/`ALPACA_API_SECRET` env vars). Without valid API keys, it falls back to the local `PaperBroker`.

### Gotchas

- The `.env` file must exist at the project root (copy from `trading_bot/config/.env.example`). The update script creates it if missing.
- The `trading-bot` CLI is installed to `~/.local/bin`; ensure `PATH` includes it.
- Market is closed on weekends; the bot will report "Weekend" status but still runs the polling loop and dashboard.
