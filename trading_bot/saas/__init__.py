"""
SaaS-launch product layer.

This package is intentionally isolated from the live trading core. It
produces signal *recommendations* (no execution) for the public API
surface in trading_bot.api.server. The trading core (scanner, broker,
risk, portfolio) is never imported from here.

Modules:
    strategy        — momentum_breakout_v1 pure-function rules
    market_data     — provider selector (polygon/alpaca/yfinance/demo)
    report_engine   — assemble + persist SaaS-shape signal reports
    cli             — operator entry point (`python -m trading_bot.saas`)

Schema: see ``REPORT_SCHEMA_VERSION`` and the ``SignalReport``
constructor in ``report_engine.py``.

Risk posture: every report carries the disclaimer
``"Not financial advice."`` and a ``mode`` field that is one of
``paper``, ``live``, or ``demo``. Demo reports use deterministic
fixtures and label themselves accordingly.
"""

from __future__ import annotations

REPORT_SCHEMA_VERSION = "saas-v1"
