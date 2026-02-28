# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a Python-based automated momentum day-trading bot. It has a single service: the trading bot process which includes a FastAPI dashboard (port 8080). No database or external containers are required. See `CLAUDE.md` for full architecture and `README.md` for setup/run/test commands.

### Running tests

```bash
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
- `AppConfig` includes a `strip_env_whitespace` model validator that strips trailing whitespace/tabs from environment variable values before Pydantic validation. This handles environments where secrets may be injected with extra whitespace.
