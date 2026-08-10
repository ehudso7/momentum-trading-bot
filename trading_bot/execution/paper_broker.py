"""
In-memory paper broker for testing and development.

Simulates order fills with configurable, realistic slippage model.
Requires no API keys or external connections.
"""

from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timedelta

import structlog

from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import OrderSide
from trading_bot.utils.helpers import now_et

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
        initial_equity: float = 100_000.0,
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
                    "opened_at": now_et().isoformat(),
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
            "filled_at": now_et().isoformat(),
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

    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        side: OrderSide,
        stop_price: float,
        take_profit_price: float,
    ) -> dict[str, str]:
        """
        Simulate a bracket order: entry fills immediately, stop and take-profit
        become pending orders that are checked on each price update.
        """
        # Fill the entry
        entry_order_id = self.submit_market_order(symbol, qty, side)
        entry_status = self.get_order_status(entry_order_id)
        if entry_status.get("status") != "filled":
            return {
                "entry_order_id": entry_order_id,
                "stop_order_id": "",
                "tp_order_id": "",
            }

        # Create stop-loss leg
        stop_order_id = str(uuid.uuid4())[:8]
        self._orders[stop_order_id] = {
            "id": stop_order_id,
            "status": "pending",
            "symbol": symbol,
            "type": "stop",
            "qty": qty,
            "stop_price": stop_price,
            "bracket_parent": entry_order_id,
        }
        self._pending_orders[stop_order_id] = self._orders[stop_order_id]
        self._order_timestamps[stop_order_id] = now_et()

        # Create take-profit leg
        tp_order_id = str(uuid.uuid4())[:8]
        self._orders[tp_order_id] = {
            "id": tp_order_id,
            "status": "pending",
            "symbol": symbol,
            "type": "take_profit",
            "qty": qty,
            "limit_price": take_profit_price,
            "bracket_parent": entry_order_id,
        }
        self._pending_orders[tp_order_id] = self._orders[tp_order_id]
        self._order_timestamps[tp_order_id] = now_et()

        # Link them as OCO pair
        self._orders[stop_order_id]["oco_partner"] = tp_order_id
        self._orders[tp_order_id]["oco_partner"] = stop_order_id

        log.info(
            "paper.bracket_order_created",
            symbol=symbol,
            entry_id=entry_order_id,
            stop_id=stop_order_id,
            tp_id=tp_order_id,
            stop_price=stop_price,
            tp_price=take_profit_price,
        )

        return {
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "tp_order_id": tp_order_id,
        }

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
        self._order_timestamps[order_id] = now_et()
        self._pending_orders[order_id] = self._orders[order_id]
        return order_id

    def replace_stop_order(
        self, order_id: str, qty: int, new_stop_price: float
    ) -> str:
        """Cancel old stop and create a new one at the updated price/qty."""
        old_order = self._orders.get(order_id, {})
        symbol = old_order.get("symbol", "")
        oco_partner = old_order.get("oco_partner")

        self.cancel_order(order_id)
        new_id = self.submit_stop_order(symbol, qty, new_stop_price)

        # Preserve OCO link if this was part of a bracket
        if oco_partner and oco_partner in self._orders:
            self._orders[new_id]["oco_partner"] = oco_partner
            self._orders[oco_partner]["oco_partner"] = new_id

        log.info(
            "paper.stop_replaced",
            old_id=order_id,
            new_id=new_id,
            symbol=symbol,
            new_stop=new_stop_price,
            new_qty=qty,
        )
        return new_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "cancelled"
            self._pending_orders.pop(order_id, None)
            self._order_timestamps.pop(order_id, None)
            return True
        return False

    def close_position(self, symbol: str) -> bool:
        # Cancel every protective/exit order before flattening.  Otherwise a
        # stale bracket leg can fire after the position is gone and mask the
        # exact orphan-exit failure that the paper broker is meant to catch.
        for order_id, order in list(self._pending_orders.items()):
            if order.get("symbol") == symbol:
                self.cancel_order(order_id)
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

    def update_price(self, symbol: str, price: float) -> list[dict]:
        """
        Update the last known price for a symbol and check pending orders.

        Returns list of order dicts that were triggered/filled this tick.
        """
        self._last_prices[symbol] = price
        if symbol in self._positions:
            self._positions[symbol]["current_price"] = price

        # Check pending stop and take-profit orders for this symbol
        triggered = []
        for order_id in list(self._pending_orders.keys()):
            order = self._pending_orders[order_id]
            if order.get("symbol") != symbol:
                continue

            filled = False
            if order.get("type") == "stop" and price <= order["stop_price"]:
                # Stop triggered — fill at stop price with slippage
                fill_price = self._slippage.compute_slippage(
                    OrderSide.SELL, order["stop_price"], order["qty"]
                )
                filled = True
            elif order.get("type") == "take_profit" and price >= order["limit_price"]:
                # Take-profit triggered — fill at limit price
                fill_price = order["limit_price"]
                filled = True

            if filled:
                qty = min(
                    order["qty"],
                    self._positions.get(symbol, {}).get("qty", 0),
                )
                if qty <= 0:
                    continue

                self._cash += qty * fill_price
                self._positions[symbol]["qty"] -= qty
                if self._positions[symbol]["qty"] == 0:
                    del self._positions[symbol]
                    self._day_trades += 1

                order["status"] = "filled"
                order["filled_qty"] = qty
                order["filled_avg_price"] = fill_price
                order["filled_at"] = now_et().isoformat()
                del self._pending_orders[order_id]
                self._order_timestamps.pop(order_id, None)
                triggered.append(order)

                log.info(
                    "paper.bracket_leg_filled",
                    order_id=order_id,
                    type=order["type"],
                    symbol=symbol,
                    price=fill_price,
                    qty=qty,
                )

                # Cancel OCO partner
                partner_id = order.get("oco_partner")
                if partner_id and partner_id in self._pending_orders:
                    self._orders[partner_id]["status"] = "cancelled_oco"
                    del self._pending_orders[partner_id]
                    self._order_timestamps.pop(partner_id, None)
                    log.info(
                        "paper.oco_cancelled",
                        cancelled_id=partner_id,
                        triggered_by=order_id,
                    )

        return triggered

    def cleanup_stale_orders(self) -> list[str]:
        """Cancel orders older than the timeout threshold. Returns cancelled IDs."""
        now = now_et()
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

        # Prune terminal-state orders older than 1 hour to prevent unbounded growth
        terminal_statuses = {"filled", "rejected", "cancelled", "cancelled_oco", "cancelled_stale", "expired"}
        cutoff = now - timedelta(hours=1)
        prune_ids = []
        for order_id, order in list(self._orders.items()):
            if order.get("status") in terminal_statuses:
                filled_at = order.get("filled_at")
                if filled_at:
                    try:
                        order_time = datetime.fromisoformat(filled_at)
                        if order_time < cutoff:
                            prune_ids.append(order_id)
                    except (TypeError, ValueError):
                        prune_ids.append(order_id)
                elif order_id not in self._order_timestamps and order_id not in stale_ids:
                    # No timestamp tracking and not just cancelled — safe to prune
                    prune_ids.append(order_id)

        for order_id in prune_ids:
            del self._orders[order_id]

        if prune_ids:
            log.debug("paper.terminal_orders_pruned", count=len(prune_ids))

        return stale_ids
