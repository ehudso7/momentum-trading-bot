"""
Operator-friendly entry point for the SaaS analytics API.

This module is the documented Railway / Heroku / Render / Fly
deploy target for ``trading_bot.api.server:app``. It honours the
PaaS ``$PORT`` convention, binds ``0.0.0.0`` by default, and is a
thin wrapper around uvicorn so the operator gets a stable
``startCommand`` that doesn't bake assumptions into a shell line.

Usage
-----

    # Run with PaaS-injected $PORT (Railway, Heroku, Render, Fly):
    trading-bot-api

    # Same but with explicit overrides:
    trading-bot-api --port 9000 --host 127.0.0.1 --log-level info

    # Underlying module-style invocation (CI / dev):
    python -m trading_bot.api.serve

CLI argument resolution order (highest priority first):
    1. Explicit CLI flag (``--port``, ``--host``, ``--log-level``).
    2. PaaS env var (``PORT``, ``HOST``).
    3. Hard-coded default (``8080`` / ``0.0.0.0`` / ``info``).

Env vars consumed:
    PORT        — port to bind (PaaS convention).
    HOST        — host to bind (defaults to 0.0.0.0).
    SENTRY_DSN  — optional; enables Sentry error tracking (strict
                  no-op when unset, see trading_bot.utils.sentry).

Env vars NOT consumed by this entry point:
    Anything Stripe / billing / auth — those are read at request
    time by ``trading_bot.api.server`` itself. Booting the server
    requires no Stripe configuration; an unconfigured deploy
    simply rejects checkout requests with the documented 503 /
    400 responses, which is the correct fail-closed behaviour.

This module imports ``trading_bot.api.server`` (and therefore the
SaaS API surface) but NOT any Core trading module — the SaaS
boundary is preserved by construction.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_LOG_LEVEL = "info"
VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"critical", "error", "warning", "info", "debug", "trace"},
)


# ---------------------------------------------------------------------------
# Pure resolvers — testable without launching uvicorn
# ---------------------------------------------------------------------------


def _resolve_port(cli_value: Optional[int]) -> int:
    """
    Port resolution: explicit CLI > ``$PORT`` env > 8080 default.

    Fail-soft on any malformed value: an operator typo in
    ``$PORT`` must NOT brick the deploy. We log + fall through to
    the default rather than raising.
    """
    if cli_value is not None:
        if 1 <= cli_value <= 65535:
            return cli_value
        return DEFAULT_PORT
    raw = os.environ.get("PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    if not (1 <= port <= 65535):
        return DEFAULT_PORT
    return port


def _resolve_host(cli_value: Optional[str]) -> str:
    """Host resolution: explicit CLI > ``$HOST`` env > 0.0.0.0."""
    if cli_value:
        return cli_value
    raw = os.environ.get("HOST")
    if raw and raw.strip():
        return raw.strip()
    return DEFAULT_HOST


def _resolve_log_level(cli_value: Optional[str]) -> str:
    """uvicorn log level. Fails soft to ``info`` on unknown values."""
    candidate = (cli_value or "").strip().lower()
    if candidate in VALID_LOG_LEVELS:
        return candidate
    return DEFAULT_LOG_LEVEL


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-bot-api",
        description=(
            "Run the read-only SaaS analytics API "
            "(trading_bot.api.server:app) on the configured host + "
            "port. Honours the PaaS $PORT convention by default."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help=(
            "Port to bind. Falls back to the PORT env var, then to "
            f"{DEFAULT_PORT}. Out-of-range values fall back to the "
            "default (fail-soft)."
        ),
    )
    parser.add_argument(
        "--host", default=None,
        help=(
            "Host to bind. Falls back to the HOST env var, then to "
            f"{DEFAULT_HOST!r} (PaaS-friendly)."
        ),
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=sorted(VALID_LOG_LEVELS),
        help=(
            f"uvicorn log level (default: {DEFAULT_LOG_LEVEL!r}). "
            "Unknown values fall back to the default."
        ),
    )
    parser.add_argument(
        "--reload", action="store_true",
        help=(
            "Enable uvicorn auto-reload. DEV ONLY — never use this "
            "in production deployments."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Print the resolved bind address and exit 0 without "
            "actually starting uvicorn. Useful for CI / smoke "
            "checks."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    host = _resolve_host(args.host)
    port = _resolve_port(args.port)
    log_level = _resolve_log_level(args.log_level)

    print(
        f"trading-bot-api: binding {host}:{port} "
        f"(log_level={log_level}, reload={bool(args.reload)})",
        file=sys.stderr,
    )

    if args.dry_run:
        return 0

    # Optional Sentry error tracking. Strict no-op when SENTRY_DSN
    # is unset (sentry_sdk is not even imported). Initialised before
    # the app import so FastAPI is auto-instrumented by the SDK's
    # default integrations. Placed after --dry-run so smoke checks
    # stay side-effect free.
    from trading_bot.utils.sentry import init_sentry

    init_sentry()

    # Lazy-import uvicorn so the dry-run / argparse paths don't
    # require it (helps tests in environments without uvicorn).
    import uvicorn

    # Reference the app module by string so uvicorn imports it in
    # its own worker process when --reload is on. For the
    # production case (no reload) we pass the imported app object
    # directly to skip an extra import round-trip.
    if args.reload:
        uvicorn.run(
            "trading_bot.api.server:app",
            host=host, port=port,
            log_level=log_level,
            reload=True,
        )
    else:
        from trading_bot.api.server import app
        uvicorn.run(
            app,
            host=host, port=port,
            log_level=log_level,
            access_log=True,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
