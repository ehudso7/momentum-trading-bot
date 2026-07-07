"""
Optional Sentry error-tracking bootstrap.

Shared by the two process entry points:

* ``trading_bot.main`` — the bot CLI (initialised before the trading
  loop starts so circuit-breaker / broker / API errors are captured).
* ``trading_bot.api.serve`` — the SaaS analytics API (FastAPI is
  auto-instrumented by sentry-sdk's default integrations once the
  SDK is initialised in the process).

Design contract — STRICT no-op when unconfigured:

* When the ``SENTRY_DSN`` env var is unset/empty, ``init_sentry()``
  returns ``False`` without importing ``sentry_sdk`` at all. There
  is zero import cost and zero side effects for operators who don't
  use Sentry — including test runs and local dev.
* When ``SENTRY_DSN`` is set but ``sentry-sdk`` is not installed,
  we log a warning and continue: observability must never brick a
  deploy or the trading loop.

Environment variables consumed:

    SENTRY_DSN                  — enables Sentry when set (required).
    RAILWAY_ENVIRONMENT_NAME    — Sentry ``environment`` tag
                                  (defaults to ``local``).
    RAILWAY_GIT_COMMIT_SHA      — Sentry ``release`` (when Railway
                                  provides it; omitted otherwise).
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)

#: Fraction of transactions sampled for performance tracing.
#: Deliberately low — this is an always-on trading process.
TRACES_SAMPLE_RATE = 0.05


def init_sentry() -> bool:
    """
    Initialise the Sentry SDK if (and only if) ``SENTRY_DSN`` is set.

    Returns ``True`` when Sentry was initialised, ``False`` when the
    call was a no-op (DSN unset, SDK missing, or init failure). Never
    raises — error tracking is best-effort and must not affect the
    host process's ability to boot.
    """
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        # Strict no-op: do NOT import sentry_sdk when unconfigured.
        return False

    try:
        import sentry_sdk  # Deferred import — only paid for when enabled.
    except ImportError:
        log.warning(
            "sentry.sdk_missing",
            detail="SENTRY_DSN is set but sentry-sdk is not installed; "
            "run: pip install sentry-sdk",
        )
        return False

    environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local")
    release = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or None

    try:
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=TRACES_SAMPLE_RATE,
            environment=environment,
            release=release,
            # Default integrations auto-instrument FastAPI/Starlette,
            # stdlib logging, threading, and outgoing HTTP clients.
        )
    except Exception as exc:  # pragma: no cover — defense in depth
        log.warning("sentry.init_failed", error=str(exc))
        return False

    log.info(
        "sentry.initialized",
        environment=environment,
        release=release,
        traces_sample_rate=TRACES_SAMPLE_RATE,
    )
    return True
