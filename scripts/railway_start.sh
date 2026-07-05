#!/bin/sh
# Railway entry point shared by both services in this project.
#
# SERVICE_ROLE selects what this container runs:
#   api (default) — SaaS analytics + billing API (trading_bot.api.serve)
#   bot           — the core trading loop with its live dashboard bound
#                   to $PORT (trading_bot.main)
#
# The chmod handles a Railway volume that may be remounted as root-owned
# at runtime, so runtime data under /app/data stays writable.
set -eu

chmod -R 777 /app/data 2>/dev/null || true

ROLE="${SERVICE_ROLE:-api}"

if [ "$ROLE" = "bot" ]; then
    # TRADING_RUN_MODE guards the mode (paper by default, enforced by
    # AppConfig). --mode only accepts backtest|paper|live; anything else
    # must fail loudly rather than fall back.
    MODE="${TRADING_RUN_MODE:-paper}"
    echo "railway_start: role=bot mode=${MODE} port=${PORT:-8080}"
    exec trading-bot --mode "$MODE"
fi

echo "railway_start: role=api port=${PORT:-8080}"
exec trading-bot-api --host 0.0.0.0
