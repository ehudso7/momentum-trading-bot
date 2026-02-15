"""
In-memory paper broker for testing and development.

Simulates order fills with configurable slippage.
Requires no API keys or external connections.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog

from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import OrderSide

log = structlog.get_logger(__name__)


class PaperBroker(BrokerBase):
    """
    In-memory paper broker that simulates trading.

    Fills market orders immediately at price + slippage.
    Maintains position and equity tracking internally.
    """

    def __init__(
        self,
        initial_equity: float = 25_000.0,
        slippage_bps: float = 5.0,
    ):
        self._initial_equity = initial_equity
        self._cash = initial_equity
        self._positions: dict[str, dict] = {}  # symbol -> position dict
        self._orders: dict[str, dict] = {}  # order_id -> order dict
        self._slippage_bps = slippage_bps
        self._day_trades = 0
        self._last_prices: dict[str, float] = {}

    def get_account_equity(self) -> float:
        """Cash + market value of all positions."""
        positions_value = sum(
            pos["qty"] * pos["current_price"] for pos in self._positions.values()
        )
        return self._cash + positions_value

    def get_buying_power(self) -> float:
        """For paper trading, buying power = 4x cash (margin simulation)."""
        return self._cash * 4.0

    def get_positions(self) -> list[dict]:
        return [
            {
                "symbol": sym,
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_entry_price"],
                "current_price": pos["current_price"],
                "unrealized_pl": pos["qty"]
                * (pos["current_price"] - pos["avg_entry_price"]),
                "market_value": pos["qty"] * pos["current_price"],
            }
            for sym, pos in self._positions.items()
            if pos["qty"] > 0
        ]

    def submit_market_order(self, symbol: str, qty: int, side: OrderSide) -> str:
        """Simulate immediate fill with slippage."""
        order_id = str(uuid.uuid4())[:8]
        base_price = self._last_prices.get(symbol, 10.0)

        # Apply slippage
        slippage_mult = 1 + (self._slippage_bps / 10_000)
        if side == OrderSide.BUY:
            fill_price = base_price * slippage_mult  # pay more
        else:
            fill_price = base_price / slippage_mult  # receive less

        fill_price = round(fill_price, 4)

        if side == OrderSide.BUY:
            cost = qty * fill_price
            if cost > self._cash * 4:  # 4x margin limit
                log.warning(
                    "paper.insufficient_funds",
                    cost=cost,
                    available=self._cash * 4,
                )
                self._orders[order_id] = {
                    "id": order_id,
                    "status": "rejected",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }
                return order_id

            self._cash -= cost
            if symbol in self._positions:
                pos = self._positions[symbol]
                total_qty = pos["qty"] + qty
                total_cost = (pos["avg_entry_price"] * pos["qty"]) + (
                    fill_price * qty
                )
                pos["avg_entry_price"] = total_cost / total_qty
                pos["qty"] = total_qty
                pos["current_price"] = fill_price
            else:
                self._positions[symbol] = {
                    "qty": qty,
                    "avg_entry_price": fill_price,
                    "current_price": fill_price,
                    "opened_at": datetime.utcnow().isoformat(),
                }

        else:  # SELL
            if symbol not in self._positions or self._positions[symbol]["qty"] < qty:
                log.warning("paper.no_position_to_sell", symbol=symbol)
                self._orders[order_id] = {
                    "id": order_id,
                    "status": "rejected",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }
                return order_id

            self._cash += qty * fill_price
            self._positions[symbol]["qty"] -= qty
            if self._positions[symbol]["qty"] == 0:
                del self._positions[symbol]
                self._day_trades += 1

        self._orders[order_id] = {
            "id": order_id,
            "status": "filled",
            "symbol": symbol,
            "side": side.value,
            "qty": qty,
            "filled_qty": qty,
            "filled_avg_price": fill_price,
            "filled_at": datetime.utcnow().isoformat(),
        }

        log.info(
            "paper.order_filled",
            order_id=order_id,
            symbol=symbol,
            side=side.value,
            qty=qty,
            price=fill_price,
        )

        return order_id

    def submit_limit_order(
        self, symbol: str, qty: int, side: OrderSide, limit_price: float
    ) -> str:
        """For paper trading, limit orders fill immediately at limit price."""
        self._last_prices[symbol] = limit_price
        return self.submit_market_order(symbol, qty, side)

    def submit_stop_order(self, symbol: str, qty: int, stop_price: float) -> str:
        """
        Record a stop order. In paper mode, stops are checked during
        position updates, not here. Returns order ID for tracking.
        """
        order_id = str(uuid.uuid4())[:8]
        self._orders[order_id] = {
            "id": order_id,
            "status": "pending",
            "symbol": symbol,
            "type": "stop",
            "qty": qty,
            "stop_price": stop_price,
        }
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "cancelled"
            return True
        return False

    def close_position(self, symbol: str) -> bool:
        if symbol in self._positions and self._positions[symbol]["qty"] > 0:
            qty = self._positions[symbol]["qty"]
            self.submit_market_order(symbol, qty, OrderSide.SELL)
            return True
        return False

    def close_all_positions(self) -> bool:
        symbols = list(self._positions.keys())
        for symbol in symbols:
            self.close_position(symbol)
        return True

    def get_order_status(self, order_id: str) -> dict:
        return self._orders.get(
            order_id,
            {"id": order_id, "status": "unknown", "filled_qty": 0, "filled_avg_price": 0.0},
        )

    def get_day_trade_count(self) -> int:
        return self._day_trades

    def update_price(self, symbol: str, price: float) -> None:
        """Update the last known price for a symbol (for simulation)."""
        self._last_prices[symbol] = price
        if symbol in self._positions:
            self._positions[symbol]["current_price"] = price
