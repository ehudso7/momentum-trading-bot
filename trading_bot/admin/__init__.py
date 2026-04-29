"""
Operator-only admin tooling that ships inside the package.

Modules here are NEVER imported by the live API request path. They
exist so operators (and CI smoke jobs) can invoke them via console
scripts or ``python -m trading_bot.admin.<name>`` from any CWD —
including from inside the deployed Docker image, where the legacy
``scripts/`` directory used to live outside the package.
"""
