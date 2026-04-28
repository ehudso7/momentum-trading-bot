"""Allow ``python -m trading_bot.saas ...`` to invoke the CLI."""

from __future__ import annotations

from trading_bot.saas.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
