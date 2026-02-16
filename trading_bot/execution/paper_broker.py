"""
In-memory paper broker for testing and development.

Simulates order fills with configurable, realistic slippage model.
Requires no API keys or external connections.
"""

from __future__ import annotations

import math
import random
import uuid
from datetime import datetime

import structlog

from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import OrderSide

log = structlog.get_logger(__name__)


class SlippageModel:
    """Configurable slippage model for realistic paper fills."""

    def __init__(
        self,
        base_bps: float = 5.0,
        volume_impact_bps: float = 2.0,
        volatility_multiplier: float = 1.0,
    ):
        self._base_bps = base_bps
        self._volume_impact_bps = volume_impact_bps
        self._volatility_multiplier = volatility_multiplier

    def compute_slippage(
        self,
        side: OrderSide,
        base_price: float,
        qty: int,
        avg_daily_volume: int = 1_000_000,
    ) -> float:
        """
        Compute fill price after slippage.

        Slippage components:
        1. Base slippage (bid-ask spread proxy)
        2. Volume impact (larger orders get worse fills)
        3. Random noise (market microstructure)
        """
        # Base spread
        slippage_bps = self._base_bps

        # Volume impact: more slippage for larger orders relative to ADV
        if avg_daily_volume > 0:
            fill_ratio = qty / avg_daily_volume
            slippage_bps += self._volume_impact_bps * math.log1p(fill_ratio * 100)

        # Random noise (+/- 20% of base)
        noise = random.uniform(-0.2, 0.2) * self._base_bps
        slippage_bps += noise

        slippage_bps = max(slippage_bps, 0.5)  # Minimum 0.5 bps
        slippage_bps *= self._volatility_multiplier

        slippage_mult = slippage_bps / 10_000
        if side == OrderSide.BUY:
            return round(base_price * (1 + slippage_mult), 4)
        else:
            return round(base_price * (1 - slippage_mult), 4)


class PaperBroker(BrokerBase):
    """
    In-memory paper broker that simulates trading.

    Fills market orders immediately with configurable slippage model.
    Maintains position and equity tracking internally.
    """

    def __init__(
        self,
        initial_equity: float = 25_000.0,
        slippage_bps: float = 5.0,
        slippage_model: SlippageModel | None = None,
    ):
        self._initial_equity = initial_equity
        self._cash = initial_equity
        self._positions: dict[str, dict] = {}  # symbol -> position dict
        self._orders: dict[str, dict] = {}  # order_id -> order dict
        self._pending_orders: dict[str, dict] = {}  # order_id -> pending stop/limit
        self._slippage = slippage_model or SlippageModel(base_bps=slippage_bps)
        self._day_trades = 0
        self._last_prices: dict[str, float] = {}
        self._order_timestamps: dict[str, datetime] = {}
        self._stale_order_timeout_seconds = 300  # 5 minutes

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
        """Simulate immediate fill with realistic slippage model."""
        if qty <= 0:
            raise ValueError(f"Order qty must be positive, got {qty}")
        if not symbol or not symbol.isalpha():
            raise ValueError(f"Invalid symbol: {symbol!r}")

        order_id = str(uuid.uuid4())[:8]
        base_price = self._last_prices.get(symbol, 10.0)

        # Apply slippage
        fill_price = self._slippage.compute_slippage(side, base_price, qty)

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
        self._order_timestamps[order_id] = datetime.utcnow()
        self._pending_orders[order_id] = self._orders[order_id]
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "cancelled"
            self._pending_orders.pop(order_id, None)
            self._order_timestamps.pop(order_id, None)
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

    def cleanup_stale_orders(self) -> list[str]:
        """Cancel orders older than the timeout threshold. Returns cancelled IDs."""
        now = datetime.utcnow()
        stale_ids = []
        for order_id, created_at in list(self._order_timestamps.items()):
            age_seconds = (now - created_at).total_seconds()
            if age_seconds > self._stale_order_timeout_seconds:
                if order_id in self._pending_orders:
                    self._orders[order_id]["status"] = "cancelled_stale"
                    del self._pending_orders[order_id]
                    stale_ids.append(order_id)
                del self._order_timestamps[order_id]

        if stale_ids:
            log.info("paper.stale_orders_cancelled", count=len(stale_ids))

        return stale_ids
