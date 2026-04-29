"""
Back-compat shim — the canonical implementation lives at
``trading_bot.admin.billing_verification``.

Both invocations work identically:

    python -m scripts.billing_verification          # legacy
    python -m trading_bot.admin.billing_verification  # canonical
    trading-bot-billing-verify                       # console script

The trading_bot.admin module is shipped INSIDE the package and is
therefore always importable from any CWD inside the deployed Docker
image. The legacy ``scripts`` directory was outside the package and
broke ``python -m scripts.X`` invocations from any CWD other than the
project root. Keeping this shim means existing runbooks don't need to
change.
"""

# Explicit re-exports keep static analysers happy and document the
# public surface of the legacy import path.
from trading_bot.admin.billing_verification import (
    OPTIONAL_ENV_VARS,
    REQUIRED_ENV_VARS,
    format_json,
    format_text,
    has_failures,
    main,
    run_checks,
)

__all__ = [
    "OPTIONAL_ENV_VARS",
    "REQUIRED_ENV_VARS",
    "format_json",
    "format_text",
    "has_failures",
    "main",
    "run_checks",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
