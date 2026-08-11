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
from decimal import Decimal, InvalidOperation

import structlog

from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import OrderSide
from trading_bot.utils.helpers import now_et

log = structlog.get_logger(__name__)


def _strict_integral_qty(value: object) -> int:
    """Return an exact integer quantity, rejecting truncation and booleans."""
    if isinstance(value, bool):
        raise ValueError(f"Order qty must be a positive integer, got {value!r}")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"Order qty must be a positive integer, got {value!r}"
        ) from exc
    if (
        not numeric.is_finite()
        or numeric != numeric.to_integral_value()
        or numeric <= 0
    ):
        raise ValueError(f"Order qty must be a positive integer, got {value!r}")
    return int(numeric)


def _strict_signed_integral_qty(value: object) -> int:
    """Return an exact signed integer without silently truncating broker state."""
    if isinstance(value, bool):
        raise ValueError(f"Position qty must be an integer, got {value!r}")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Position qty must be an integer, got {value!r}") from exc
    if not numeric.is_finite() or numeric != numeric.to_integral_value():
        raise ValueError(f"Position qty must be an integer, got {value!r}")
    return int(numeric)


def _strict_positive_price(value: object, *, label: str) -> float:
    """Return a finite positive price before any broker state is changed."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite and positive, got {value!r}")
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{label} must be finite and positive, got {value!r}"
        ) from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"{label} must be finite and positive, got {value!r}")
    return price


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
        self._last_close_fills: list[dict] = []
        self._capture_close_fills = False

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
        qty = _strict_integral_qty(qty)
        if not symbol or not symbol.isalpha():
            raise ValueError(f"Invalid symbol: {symbol!r}")
        if not isinstance(side, OrderSide):
            raise ValueError(f"Invalid order side: {side!r}")

        order_id = str(uuid.uuid4())[:8]
        base_price = _strict_positive_price(
            self._last_prices.get(symbol, 10.0), label="Market price"
        )

        # Apply slippage
        fill_price = _strict_positive_price(
            self._slippage.compute_slippage(side, base_price, qty),
            label="Market fill price",
        )

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
        # Validate every child-order boundary before the immediately-filled
        # parent can mutate cash or positions.
        qty = _strict_integral_qty(qty)
        stop_price = _strict_positive_price(stop_price, label="Stop price")
        take_profit_price = _strict_positive_price(
            take_profit_price, label="Take-profit price"
        )

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
            # Match Alpaca's active child-leg representation.  The portfolio
            # manager intentionally accepts only a fresh nested SELL leg in
            # the working ``new`` state as broker-side protection.
            "status": "new",
            "symbol": symbol,
            "side": OrderSide.SELL.value,
            "type": "stop",
            "qty": qty,
            "filled_qty": 0,
            "filled_avg_price": 0.0,
            "stop_price": stop_price,
            "limit_price": 0.0,
            "bracket_parent": entry_order_id,
        }
        self._pending_orders[stop_order_id] = self._orders[stop_order_id]
        self._order_timestamps[stop_order_id] = now_et()

        # Create take-profit leg
        tp_order_id = str(uuid.uuid4())[:8]
        self._orders[tp_order_id] = {
            "id": tp_order_id,
            "status": "new",
            "symbol": symbol,
            "side": OrderSide.SELL.value,
            "type": "limit",
            "qty": qty,
            "filled_qty": 0,
            "filled_avg_price": 0.0,
            "stop_price": 0.0,
            "limit_price": take_profit_price,
            "bracket_parent": entry_order_id,
        }
        self._pending_orders[tp_order_id] = self._orders[tp_order_id]
        self._order_timestamps[tp_order_id] = now_et()

        # Link them as OCO pair
        self._orders[stop_order_id]["oco_partner"] = tp_order_id
        self._orders[tp_order_id]["oco_partner"] = stop_order_id
        self._orders[entry_order_id]["bracket_leg_ids"] = [
            stop_order_id,
            tp_order_id,
        ]

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
        """Submit a limit order without ever filling through its limit."""
        qty = _strict_integral_qty(qty)
        if not symbol or not symbol.isalpha():
            raise ValueError(f"Invalid symbol: {symbol!r}")
        if not isinstance(side, OrderSide):
            raise ValueError(f"Invalid order side: {side!r}")
        limit_price = _strict_positive_price(limit_price, label="Limit price")

        raw_current_price = self._last_prices.get(symbol)
        current_price = (
            _strict_positive_price(raw_current_price, label="Market price")
            if raw_current_price is not None
            else None
        )
        marketable = current_price is not None and (
            (side == OrderSide.BUY and current_price <= limit_price)
            or (side == OrderSide.SELL and current_price >= limit_price)
        )
        fill_price: float | None = None
        if marketable:
            slipped_price = _strict_positive_price(
                self._slippage.compute_slippage(side, current_price, qty),
                label="Limit fill price",
            )
            fill_price = (
                min(slipped_price, limit_price)
                if side == OrderSide.BUY
                else max(slipped_price, limit_price)
            )

        order_id = str(uuid.uuid4())[:8]
        order = {
            "id": order_id,
            "status": "new",
            "symbol": symbol,
            "side": side.value,
            "type": "limit",
            "qty": qty,
            "filled_qty": 0,
            "filled_avg_price": 0.0,
            "stop_price": 0.0,
            "limit_price": limit_price,
        }
        self._orders[order_id] = order

        if not marketable:
            self._pending_orders[order_id] = order
            self._order_timestamps[order_id] = now_et()
            return order_id

        assert fill_price is not None
        if not self._apply_account_fill(symbol, qty, side, fill_price):
            order["status"] = "rejected"
            return order_id

        order["status"] = "filled"
        order["filled_qty"] = qty
        order["filled_avg_price"] = fill_price
        order["filled_at"] = now_et().isoformat()
        log.info(
            "paper.limit_order_filled",
            order_id=order_id,
            symbol=symbol,
            side=side.value,
            qty=qty,
            price=fill_price,
            limit_price=limit_price,
        )
        return order_id

    def _apply_account_fill(
        self,
        symbol: str,
        qty: int,
        side: OrderSide,
        fill_price: float,
        *,
        allow_partial_sell: bool = False,
    ) -> int:
        """Apply a simulated fill and return the quantity actually filled."""
        qty = _strict_integral_qty(qty)
        fill_price = _strict_positive_price(fill_price, label="Fill price")
        if not isinstance(side, OrderSide):
            raise ValueError(f"Invalid order side: {side!r}")

        if side == OrderSide.BUY:
            cost = qty * fill_price
            if cost > self._cash * 4:
                log.warning(
                    "paper.insufficient_funds",
                    cost=cost,
                    available=self._cash * 4,
                )
                return 0

            self._cash -= cost
            if symbol in self._positions:
                pos = self._positions[symbol]
                total_qty = pos["qty"] + qty
                total_cost = (
                    pos["avg_entry_price"] * pos["qty"]
                    + fill_price * qty
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
            return qty

        available_qty = self._positions.get(symbol, {}).get("qty", 0)
        fill_qty = min(qty, available_qty) if allow_partial_sell else qty
        if fill_qty <= 0 or (not allow_partial_sell and available_qty < qty):
            log.warning("paper.no_position_to_sell", symbol=symbol)
            return 0

        self._cash += fill_qty * fill_price
        self._positions[symbol]["qty"] -= fill_qty
        if self._positions[symbol]["qty"] == 0:
            del self._positions[symbol]
            self._day_trades += 1
        return fill_qty

    def submit_stop_order(self, symbol: str, qty: int, stop_price: float) -> str:
        """
        Record a stop order. In paper mode, stops are checked during
        position updates, not here. Returns order ID for tracking.
        """
        qty = _strict_integral_qty(qty)
        if not symbol or not symbol.isalpha():
            raise ValueError(f"Invalid symbol: {symbol!r}")
        stop_price = _strict_positive_price(stop_price, label="Stop price")

        order_id = str(uuid.uuid4())[:8]
        self._orders[order_id] = {
            "id": order_id,
            "status": "new",
            "symbol": symbol,
            "side": OrderSide.SELL.value,
            "type": "stop",
            "qty": qty,
            "filled_qty": 0,
            "filled_avg_price": 0.0,
            "stop_price": stop_price,
            "limit_price": 0.0,
        }
        self._order_timestamps[order_id] = now_et()
        self._pending_orders[order_id] = self._orders[order_id]
        return order_id

    def replace_stop_order(
        self, order_id: str, qty: int, new_stop_price: float
    ) -> str:
        """Cancel old stop and create a new one at the updated price/qty."""
        # Never cancel live protection until its replacement payload is valid.
        qty = _strict_integral_qty(qty)
        new_stop_price = _strict_positive_price(
            new_stop_price, label="Stop price"
        )

        old_order = self._orders.get(order_id)
        if (
            old_order is None
            or order_id not in self._pending_orders
            or old_order.get("status") not in {"new", "held"}
            or old_order.get("type") not in {
                "stop",
                "stop_limit",
                "trailing_stop",
            }
        ):
            raise RuntimeError(
                f"Stop order {order_id!r} is not active and replaceable"
            )

        symbol = old_order.get("symbol", "")
        oco_partner = old_order.get("oco_partner")
        if oco_partner:
            partner = self._orders.get(oco_partner)
            if (
                partner is None
                or oco_partner not in self._pending_orders
                or partner.get("status") != "new"
            ):
                raise RuntimeError(
                    f"Stop order {order_id!r} has no active OCO partner"
                )

        if not self.cancel_order(order_id):
            raise RuntimeError(f"Could not cancel stop order {order_id!r}")
        new_id = self.submit_stop_order(symbol, qty, new_stop_price)

        # Preserve OCO link if this was part of a bracket
        if oco_partner:
            self._orders[new_id]["oco_partner"] = oco_partner
            bracket_parent = old_order.get("bracket_parent")
            if bracket_parent:
                self._orders[new_id]["bracket_parent"] = bracket_parent
                parent = self._orders.get(bracket_parent)
                if parent is not None:
                    parent["bracket_leg_ids"] = [
                        new_id if leg_id == order_id else leg_id
                        for leg_id in parent.get("bracket_leg_ids", [])
                    ]
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
        order = self._orders.get(order_id)
        if order is None:
            return False
        status = str(order.get("status", "") or "").lower()
        if status not in {"new", "accepted", "pending_new", "partially_filled"}:
            return False
        order["status"] = "cancelled"
        self._pending_orders.pop(order_id, None)
        self._order_timestamps.pop(order_id, None)
        return True

    @staticmethod
    def _is_exact_close_fill(fill: object, symbol: str, qty: int) -> bool:
        """Validate an immutable close-fill proof without coercive truncation."""
        if not isinstance(fill, dict):
            return False
        fill_id = fill.get("id")
        if not isinstance(fill_id, str) or not fill_id.strip():
            return False
        if str(fill.get("status", "") or "").lower() != "filled":
            return False
        if fill.get("symbol") != symbol:
            return False
        if str(fill.get("side", "") or "").lower() != OrderSide.SELL.value:
            return False
        try:
            requested_qty = _strict_integral_qty(fill.get("qty"))
            filled_qty = _strict_integral_qty(fill.get("filled_qty"))
            _strict_positive_price(
                fill.get("filled_avg_price"), label="Close fill price"
            )
        except ValueError:
            return False
        return requested_qty == qty and filled_qty == qty

    def _has_exact_close_fill(self, fill: object, symbol: str, qty: int) -> bool:
        """Require the captured proof and its broker record to agree exactly."""
        if not self._is_exact_close_fill(fill, symbol, qty):
            return False
        assert isinstance(fill, dict)  # Narrowed by the validation above.
        fill_id = fill["id"]
        broker_record = self._orders.get(fill_id)
        if not self._is_exact_close_fill(broker_record, symbol, qty):
            return False
        assert isinstance(broker_record, dict)
        return all(
            fill.get(field) == broker_record.get(field)
            for field in (
                "id",
                "status",
                "symbol",
                "side",
                "qty",
                "filled_qty",
                "filled_avg_price",
            )
        )

    def close_position(self, symbol: str) -> bool:
        # Cancel every protective/exit order before flattening.  Otherwise a
        # stale bracket leg can fire after the position is gone and mask the
        # exact orphan-exit failure that the paper broker is meant to catch.
        for order_id, order in list(self._pending_orders.items()):
            if order.get("symbol") == symbol:
                self.cancel_order(order_id)
        if symbol in self._positions:
            try:
                qty = _strict_signed_integral_qty(
                    self._positions[symbol].get("qty")
                )
            except ValueError:
                return False
            # PaperBroker does not originate shorts.  Never report a corrupted
            # or externally-injected non-long position as successfully closed.
            if qty <= 0:
                return False
            order_id = self.submit_market_order(symbol, qty, OrderSide.SELL)
            order = self.get_order_status(order_id)
            if self._capture_close_fills:
                self._last_close_fills.append(dict(order))
            return (
                self._has_exact_close_fill(order, symbol, qty)
                and symbol not in self._positions
            )
        return False

    def close_all_positions(self) -> bool:
        # A startup/shutdown reset must also remove orders for symbols that no
        # longer have a position.  Iterating positions alone leaves those
        # orphan exits armed and can make a later session fail its clean-state
        # invariant.
        # Snapshot the *signed* internal position state.  get_positions()
        # intentionally exposes longs only and could otherwise hide a corrupt
        # or externally-injected short while this method falsely reported flat.
        pre_close_positions: dict[str, int] = {}
        valid_pre_close_state = True
        for symbol, position in self._positions.items():
            if not isinstance(position, dict):
                valid_pre_close_state = False
                continue
            try:
                qty = _strict_signed_integral_qty(position.get("qty"))
            except ValueError:
                valid_pre_close_state = False
                continue
            if not isinstance(symbol, str) or not symbol or not symbol.isalpha():
                valid_pre_close_state = False
                continue
            if qty <= 0:
                # Shorts and zero-quantity residue are not valid PaperBroker
                # positions.  Leave them visible and fail closed rather than
                # silently ignoring them through the long-only public view.
                valid_pre_close_state = False
                continue
            pre_close_positions[symbol] = qty

        self._last_close_fills = []
        self._capture_close_fills = True
        close_results: dict[str, bool] = {}
        try:
            for order_id in list(self._pending_orders):
                self.cancel_order(order_id)

            for symbol in pre_close_positions:
                try:
                    close_results[symbol] = self.close_position(symbol)
                except Exception:
                    close_results[symbol] = False
                    log.exception("paper.close_position_failed", symbol=symbol)
        finally:
            self._capture_close_fills = False

        fills_by_symbol: dict[str, list[dict]] = {
            symbol: [] for symbol in pre_close_positions
        }
        unexpected_fill = False
        seen_fill_ids: set[str] = set()
        for fill in self._last_close_fills:
            if not isinstance(fill, dict):
                unexpected_fill = True
                continue
            fill_symbol = fill.get("symbol")
            fill_id = fill.get("id")
            if (
                fill_symbol not in fills_by_symbol
                or not isinstance(fill_id, str)
                or fill_id in seen_fill_ids
            ):
                unexpected_fill = True
                continue
            seen_fill_ids.add(fill_id)
            fills_by_symbol[fill_symbol].append(fill)

        exact_fill_proofs = not unexpected_fill
        for symbol, qty in pre_close_positions.items():
            symbol_fills = fills_by_symbol[symbol]
            exact_fill_proofs = (
                exact_fill_proofs
                and close_results.get(symbol) is True
                and len(symbol_fills) == 1
                and self._has_exact_close_fill(symbol_fills[0], symbol, qty)
            )

        closed = (
            valid_pre_close_state
            and exact_fill_proofs
            and len(self._last_close_fills) == len(pre_close_positions)
            and not self._positions
            and not self._pending_orders
        )
        if closed:
            log.info("paper.all_positions_closed")
        else:
            log.critical(
                "paper.close_all_unconfirmed",
                positions=list(self._positions),
                open_order_ids=list(self._pending_orders),
                exact_fill_proofs=exact_fill_proofs,
                valid_pre_close_state=valid_pre_close_state,
            )
        return closed

    def get_last_close_fills(self) -> list[dict]:
        """Return immutable snapshots of the last global-flatten fills."""
        return [dict(fill) for fill in self._last_close_fills]

    def get_order_status(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order is None:
            return {
                "id": order_id,
                "status": "unknown",
                "filled_qty": 0,
                "filled_avg_price": 0.0,
            }

        # Build nested bracket children from their current records every time.
        # Returning a copied snapshot prevents a caller from mutating broker
        # state and, importantly, ensures cancellation/fill status is fresh.
        snapshot = dict(order)
        leg_ids = order.get("bracket_leg_ids", [])
        if leg_ids:
            snapshot["legs"] = [
                dict(self._orders[leg_id])
                for leg_id in leg_ids
                if leg_id in self._orders
            ]
        return snapshot

    def get_day_trade_count(self) -> int:
        return self._day_trades

    def update_price(self, symbol: str, price: float) -> list[dict]:
        """
        Update the last known price for a symbol and check pending orders.

        Returns list of order dicts that were triggered/filled this tick.
        """
        if not symbol or not symbol.isalpha():
            raise ValueError(f"Invalid symbol: {symbol!r}")
        price = _strict_positive_price(price, label="Market price")

        # Phase 1: derive and validate every fill that this tick could trigger.
        # No broker state changes until all slippage outputs and stored order
        # prices/quantities have proved finite, positive, and integral.
        planned_fills: list[tuple[str, OrderSide, int, float]] = []
        for order_id in list(self._pending_orders):
            order = self._pending_orders.get(order_id)
            if order is None or order.get("symbol") != symbol:
                continue

            try:
                order_side = OrderSide(
                    order.get("side", OrderSide.SELL.value)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid pending-order side for {order_id!r}"
                ) from exc

            requested_qty: int | None = None
            fill_price: float | None = None
            if order.get("type") == "stop":
                if order_side != OrderSide.SELL:
                    raise ValueError(
                        f"Invalid stop-order side for {order_id!r}"
                    )
                stop_price = _strict_positive_price(
                    order.get("stop_price"), label="Stop price"
                )
                if price <= stop_price:
                    requested_qty = _strict_integral_qty(order.get("qty"))
                    fill_price = _strict_positive_price(
                        self._slippage.compute_slippage(
                            OrderSide.SELL, stop_price, requested_qty
                        ),
                        label="Stop fill price",
                    )
            elif order.get("type") == "limit":
                limit_price = _strict_positive_price(
                    order.get("limit_price"), label="Limit price"
                )
                marketable = (
                    order_side == OrderSide.BUY and price <= limit_price
                ) or (
                    order_side == OrderSide.SELL and price >= limit_price
                )
                if marketable:
                    requested_qty = _strict_integral_qty(order.get("qty"))
                    slipped_price = _strict_positive_price(
                        self._slippage.compute_slippage(
                            order_side, price, requested_qty
                        ),
                        label="Limit fill price",
                    )
                    fill_price = _strict_positive_price(
                        min(slipped_price, limit_price)
                        if order_side == OrderSide.BUY
                        else max(slipped_price, limit_price),
                        label="Limit fill price",
                    )

            if fill_price is not None:
                assert requested_qty is not None
                planned_fills.append(
                    (order_id, order_side, requested_qty, fill_price)
                )

        # Phase 2: commit the valid market mark, then apply the preflighted
        # fills in broker order.  A filled OCO leg may remove a later plan;
        # checking the live map preserves the original one-fill behavior.
        self._last_prices[symbol] = price
        if symbol in self._positions:
            self._positions[symbol]["current_price"] = price

        triggered = []
        for order_id, order_side, requested_qty, fill_price in planned_fills:
            order = self._pending_orders.get(order_id)
            if order is None:
                continue

            qty = self._apply_account_fill(
                symbol,
                requested_qty,
                order_side,
                fill_price,
                allow_partial_sell=(order_side == OrderSide.SELL),
            )
            if qty <= 0:
                order["status"] = "rejected"
                del self._pending_orders[order_id]
                self._order_timestamps.pop(order_id, None)
                continue

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
        terminal_statuses = {
            "filled",
            "rejected",
            "cancelled",
            "cancelled_oco",
            "cancelled_stale",
            "expired",
        }
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
