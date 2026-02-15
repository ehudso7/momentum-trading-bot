"""
Abstract broker interface.

All broker implementations (Alpaca, paper) must implement this interface.
This enables testing with the paper broker and swapping to live without
changing any other module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from trading_bot.models.domain import OrderSide


class BrokerBase(ABC):
    """Abstract broker interface for order execution."""

    @abstractmethod
    def get_account_equity(self) -> float:
        """Get current account equity (cash + positions value)."""
        ...

    @abstractmethod
    def get_buying_power(self) -> float:
        """Get available buying power (includes margin)."""
        ...

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """
        Get all open positions.

        Returns list of dicts with keys:
        symbol, qty, avg_entry_price, current_price, unrealized_pl, market_value.
        """
        ...

    @abstractmethod
    def submit_market_order(self, symbol: str, qty: int, side: OrderSide) -> str:
        """
        Submit a market order.

        Returns order ID string.
        """
        ...

    @abstractmethod
    def submit_limit_order(
        self, symbol: str, qty: int, side: OrderSide, limit_price: float
    ) -> str:
        """
        Submit a limit order.

        Returns order ID string.
        """
        ...

    @abstractmethod
    def submit_stop_order(self, symbol: str, qty: int, stop_price: float) -> str:
        """
        Submit a stop (sell) order.

        Returns order ID string.
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        ...

    @abstractmethod
    def close_position(self, symbol: str) -> bool:
        """Close entire position for a symbol. Returns True if successful."""
        ...

    @abstractmethod
    def close_all_positions(self) -> bool:
        """Close all open positions. Returns True if successful."""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> dict:
        """
        Get order status.

        Returns dict with keys: id, status, filled_qty, filled_avg_price.
        """
        ...

    @abstractmethod
    def get_day_trade_count(self) -> int:
        """Get number of day trades in the current rolling 5-day window."""
        ...
