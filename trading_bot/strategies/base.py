"""
Abstract base class for trading strategies.

All strategies must implement evaluate() for entry signals,
should_exit() for exit conditions, and get_trailing_stop()
for dynamic stop management.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from trading_bot.models.domain import PositionInfo, ScanResult, TradeSignal


class Strategy(ABC):
    """Abstract base for all trading strategies."""

    @abstractmethod
    def evaluate(
        self, candidate: ScanResult, bars: pd.DataFrame
    ) -> Optional[TradeSignal]:
        """
        Evaluate a scan candidate for entry.

        Args:
            candidate: Scan result with symbol, price, gap, volume info.
            bars: Enriched OHLCV DataFrame with indicators.

        Returns:
            TradeSignal if entry conditions are met, None otherwise.
        """
        ...

    @abstractmethod
    def should_exit(
        self, position: PositionInfo, bars: pd.DataFrame, current_time_et=None
    ) -> tuple[bool, str]:
        """
        Check if any exit condition is met.

        Args:
            position: Current position info.
            bars: Recent enriched OHLCV bars.
            current_time_et: Current time in ET (for time-based exits).

        Returns:
            Tuple of (should_exit: bool, reason: str).
        """
        ...

    @abstractmethod
    def compute_scale_out(
        self, position: PositionInfo, current_price: float
    ) -> Optional[tuple[int, str]]:
        """
        Determine if a partial scale-out is due.

        Args:
            position: Current position info.
            current_price: Latest price.

        Returns:
            Tuple of (shares_to_sell, reason) or None if no scale-out needed.
        """
        ...

    @abstractmethod
    def get_trailing_stop(
        self, position: PositionInfo, bars: pd.DataFrame
    ) -> Optional[float]:
        """
        Compute updated trailing stop level.

        Returns new stop price (only if higher than current),
        or None if no update needed.
        """
        ...
