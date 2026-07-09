"""
Performance analytics for the momentum trading bot.

Exposes :func:`compute_performance` — an honest, conservative live
scorecard that never flatters results and loudly signals when the trade
sample is too small to be statistically meaningful.
"""

from __future__ import annotations

from trading_bot.analytics.performance import (
    MIN_SAMPLE_FOR_CONFIDENCE,
    compute_performance,
    rolling,
)

__all__ = [
    "compute_performance",
    "rolling",
    "MIN_SAMPLE_FOR_CONFIDENCE",
]
