#!/bin/sh
# Railway entry point shared by both services in this project.
#
# SERVICE_ROLE selects what this container runs:
#   api (default) — SaaS analytics + billing API (trading_bot.api.serve)
#   bot           — the core trading loop with its live dashboard bound
#                   to $PORT (trading_bot.main)
#
# A Railway volume may be remounted root-owned. The script prepares that one
# directory and immediately drops to the unprivileged runtime account.
set -eu

run_as_bot() {
    if [ "$(id -u)" = "0" ]; then
        chown -R botuser:botuser /app/data
        chmod -R u=rwX,g=rX,o= /app/data
        exec gosu botuser "$@"
    fi
    exec "$@"
}

ROLE="${SERVICE_ROLE:-api}"

if [ "$ROLE" = "bot" ]; then
    # TRADING_RUN_MODE guards the mode (paper by default, enforced by
    # AppConfig). --mode only accepts backtest|paper|live; anything else
    # must fail loudly rather than fall back.
    MODE="${TRADING_RUN_MODE:-paper}"
    echo "railway_start: role=bot mode=${MODE} port=${PORT:-8080}"
    run_as_bot trading-bot --mode "$MODE"
fi

echo "railway_start: role=api port=${PORT:-8080}"
run_as_bot trading-bot-api --host 0.0.0.0
