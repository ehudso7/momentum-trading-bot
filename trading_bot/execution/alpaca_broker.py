"""
Alpaca broker implementation using alpaca-py SDK with retry logic.

Supports both paper and live trading via the same interface.
Paper trading uses Alpaca's paper trading environment.
"""

from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal, InvalidOperation

import structlog

from trading_bot.config.settings import BrokerConfig
from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import OrderSide
from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)

# Alpaca paper trading base URL
_PAPER_BASE_URL = "https://paper-api.alpaca.markets"

_ORDER_CONFIRM_TIMEOUT_SECONDS = 10.0
_ORDER_CONFIRM_POLL_SECONDS = 0.25
_TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
    "done_for_day",
}

# After a filled long bracket parent, Alpaca normally reports the take-profit
# limit as ``new`` while its contingent stop remains ``held`` until the stop
# price triggers.  That ``held`` stop is the broker-managed protective half of
# the OCO pair, not an unsubmitted client-side stop.  Status acceptance is
# therefore leg-type specific; unknown and transitional statuses still fail
# closed.
_ACTIVE_TAKE_PROFIT_STATUS = "new"
_PROTECTIVE_STOP_STATUSES = {"new", "held"}


def _enum_value(value: object) -> str:
    """Return the wire value for alpaca-py string enums.

    ``str(OrderStatus.FILLED)`` is ``"OrderStatus.FILLED"`` on supported
    alpaca-py versions, not ``"filled"``.  Order lifecycle decisions must use
    the enum's value or the portfolio manager will miss fills and terminal
    states.
    """
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _strict_integral_qty(
    value: object,
    *,
    allow_zero: bool = True,
    allow_negative: bool = False,
) -> int | None:
    """Normalize an exact broker quantity without ever truncating it.

    Alpaca serializes quantities as strings, often with a ``.0`` suffix.  A
    plain ``int(float(value))`` silently turns a fractional broker quantity
    such as 647.5 into 647 and can make an oversized exit look exact.  Invalid,
    non-finite, fractional, boolean, and disallowed signed values therefore
    return ``None`` so lifecycle validation fails closed.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if (
        not decimal_value.is_finite()
        or decimal_value != decimal_value.to_integral_value()
    ):
        return None
    qty = int(decimal_value)
    if allow_negative:
        if not allow_zero and qty == 0:
            return None
    elif qty < 0 or (not allow_zero and qty == 0):
        return None
    return qty


def _require_positive_integral_qty(value: object) -> int:
    qty = _strict_integral_qty(value, allow_zero=False)
    if qty is None:
        raise ValueError(f"Quantity must be a positive whole number: {value!r}")
    return qty


class AlpacaBroker(BrokerBase):
    """
    Alpaca implementation using alpaca-py SDK.

    Requires alpaca_api_key and alpaca_api_secret in config.
    Set alpaca_paper=True for paper trading (default and recommended).
    """

    def __init__(self, config: BrokerConfig):
        from alpaca.trading.client import TradingClient

        self._api_key = config.alpaca_api_key.get_secret_value()
        self._api_secret = config.alpaca_api_secret.get_secret_value()
        self._client = TradingClient(
            api_key=self._api_key,
            secret_key=self._api_secret,
            paper=config.alpaca_paper,
        )
        self._paper = config.alpaca_paper
        self._last_close_fills: list[dict] = []
        log.info("alpaca.connected", paper=self._paper)

    @staticmethod
    def _client_order_id(purpose: str, symbol: str) -> str:
        """Create a stable, Alpaca-compatible ID for one logical submission."""
        safe_symbol = "".join(ch for ch in symbol.lower() if ch.isalnum())[:8]
        safe_purpose = "".join(ch for ch in purpose.lower() if ch.isalnum())[:8]
        return f"mtb-{safe_purpose}-{safe_symbol}-{uuid.uuid4().hex[:20]}"[:48]

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _submit_order_idempotent(self, request: object, client_order_id: str):
        """Submit once logically even when a timeout occurs after acceptance.

        The public method creates the client ID and request before entering the
        retry wrapper.  Every retry therefore reuses the same ID.  A lookup is
        attempted after any exception so a timeout-after-accept returns the
        already-created order instead of placing a duplicate.
        """
        try:
            return self._client.submit_order(request)
        except Exception:
            try:
                existing = self._client.get_order_by_client_id(client_order_id)
            except Exception:
                existing = None
            if existing is not None:
                log.warning(
                    "alpaca.order_recovered_by_client_id",
                    client_order_id=client_order_id,
                    order_id=str(existing.id),
                )
                return existing
            raise

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _replace_order_idempotent(
        self, order_id: str, request: object, client_order_id: str
    ):
        """Replace a stop without a cancel-and-resubmit duplicate window."""
        try:
            return self._client.replace_order_by_id(order_id, request)
        except Exception:
            try:
                existing = self._client.get_order_by_client_id(client_order_id)
            except Exception:
                existing = None
            if existing is not None:
                return existing
            raise

    @staticmethod
    def _position_intent(side: OrderSide):
        from alpaca.trading.enums import PositionIntent

        if side == OrderSide.SELL:
            return PositionIntent.SELL_TO_CLOSE
        return PositionIntent.BUY_TO_OPEN

    @staticmethod
    def _leg_payload(leg: object) -> dict:
        return {
            "id": str(getattr(leg, "id", "")),
            "symbol": str(getattr(leg, "symbol", "") or ""),
            "side": _enum_value(getattr(leg, "side", "")),
            "qty": _strict_integral_qty(
                getattr(leg, "qty", None), allow_zero=False
            ),
            "status": _enum_value(getattr(leg, "status", "")),
            "type": _enum_value(getattr(leg, "type", "")),
            "filled_qty": _strict_integral_qty(
                getattr(leg, "filled_qty", 0), allow_zero=True
            ),
            "filled_avg_price": float(
                getattr(leg, "filled_avg_price", 0.0) or 0.0
            ),
            "stop_price": float(getattr(leg, "stop_price", 0.0) or 0.0),
            "limit_price": float(getattr(leg, "limit_price", 0.0) or 0.0),
        }

    @classmethod
    def _bracket_leg_ids(
        cls, order: object, expected_qty: int | None = None
    ) -> tuple[str, str]:
        """Return one verified, working SELL stop/target pair.

        Alpaca can expose child IDs before the legs are usable.  Submission
        hints therefore obey the same side/quantity/status requirements as a
        later nested parent read; callers must never treat an arbitrary pair
        of child IDs as protection for the position.
        """
        # Child IDs can be present while the entry is still pending.  They are
        # evidence of live protection only on a freshly observed filled parent.
        if _enum_value(getattr(order, "status", "")) != "filled":
            return "", ""

        stop_ids: list[str] = []
        tp_ids: list[str] = []
        for leg in getattr(order, "legs", None) or []:
            payload = cls._leg_payload(leg)
            if (
                not payload["id"]
                or payload["side"] != "sell"
                or payload["qty"] is None
                or (
                    expected_qty is not None
                    and payload["qty"] != expected_qty
                )
            ):
                continue
            order_type = payload["type"]
            if payload["stop_price"] > 0 or order_type in {
                "stop",
                "stop_limit",
                "trailing_stop",
            }:
                if payload["status"] in _PROTECTIVE_STOP_STATUSES:
                    stop_ids.append(payload["id"])
            elif payload["limit_price"] > 0 or order_type == "limit":
                if payload["status"] == _ACTIVE_TAKE_PROFIT_STATUS:
                    tp_ids.append(payload["id"])
        # Bracket protection is atomic: one working leg without its OCO
        # partner (or an ambiguous duplicate pair) is not a ready bracket.
        if len(stop_ids) != 1 or len(tp_ids) != 1:
            return "", ""
        return stop_ids[0], tp_ids[0]

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def get_account_equity(self) -> float:
        account = self._client.get_account()
        return float(account.equity or 0)

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def get_buying_power(self) -> float:
        account = self._client.get_account()
        return float(account.buying_power or 0)

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def get_positions(self) -> list[dict]:
        positions = self._client.get_all_positions()
        normalized = []
        for position in positions:
            qty = _strict_integral_qty(
                getattr(position, "qty", None),
                allow_zero=False,
                allow_negative=True,
            )
            if qty is None:
                raise RuntimeError(
                    "Alpaca returned a non-integral or invalid position "
                    f"quantity for {getattr(position, 'symbol', '')!s}"
                )
            normalized.append(
                {
                    "symbol": position.symbol,
                    "qty": qty,
                    "avg_entry_price": float(position.avg_entry_price),
                    "current_price": float(position.current_price),
                    "unrealized_pl": float(position.unrealized_pl),
                    "market_value": float(position.market_value),
                }
            )
        return normalized

    def submit_market_order(self, symbol: str, qty: int, side: OrderSide) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        qty = _require_positive_integral_qty(qty)
        client_order_id = self._client_order_id("market", symbol)
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            position_intent=self._position_intent(side),
            client_order_id=client_order_id,
        )
        order = self._submit_order_idempotent(request, client_order_id)

        # Check for immediate rejection before returning
        status_str = _enum_value(order.status)
        if status_str in ("rejected", "canceled", "cancelled", "expired"):
            log.error(
                "alpaca.order_rejected",
                order_id=str(order.id),
                status=status_str,
                symbol=symbol,
                side=side.value,
                qty=qty,
            )
            raise RuntimeError(
                f"Order {order.id} rejected by Alpaca: status={status_str}"
            )

        log.info(
            "alpaca.market_order",
            order_id=str(order.id),
            symbol=symbol,
            side=side.value,
            qty=qty,
            client_order_id=client_order_id,
        )
        return str(order.id)

    def submit_limit_order(
        self, symbol: str, qty: int, side: OrderSide, limit_price: float
    ) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        qty = _require_positive_integral_qty(qty)
        client_order_id = self._client_order_id("limit", symbol)
        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            position_intent=self._position_intent(side),
            client_order_id=client_order_id,
        )
        order = self._submit_order_idempotent(request, client_order_id)
        log.info(
            "alpaca.limit_order",
            order_id=str(order.id),
            symbol=symbol,
            side=side.value,
            qty=qty,
            price=limit_price,
        )
        return str(order.id)

    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        side: OrderSide,
        stop_price: float,
        take_profit_price: float,
    ) -> dict[str, str]:
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        qty = _require_positive_integral_qty(qty)
        client_order_id = self._client_order_id("bracket", symbol)
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            position_intent=self._position_intent(side),
            client_order_id=client_order_id,
        )
        order = self._submit_order_idempotent(request, client_order_id)

        # Alpaca activates bracket legs only after the parent fills.  They may
        # legitimately be absent from the immediate submission response; the
        # portfolio manager retrieves the parent again with nested=True.
        stop_id, tp_id = self._bracket_leg_ids(order, expected_qty=qty)

        log.info(
            "alpaca.bracket_order",
            entry_id=str(order.id),
            stop_id=stop_id,
            tp_id=tp_id,
            symbol=symbol,
            side=side.value,
            qty=qty,
            stop=stop_price,
            target=take_profit_price,
        )
        return {
            "entry_order_id": str(order.id),
            "stop_order_id": stop_id,
            "tp_order_id": tp_id,
        }

    def submit_stop_order(self, symbol: str, qty: int, stop_price: float) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        qty = _require_positive_integral_qty(qty)
        client_order_id = self._client_order_id("stop", symbol)
        request = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_price,
            position_intent=self._position_intent(OrderSide.SELL),
            client_order_id=client_order_id,
        )
        order = self._submit_order_idempotent(request, client_order_id)
        log.info(
            "alpaca.stop_order",
            order_id=str(order.id),
            symbol=symbol,
            qty=qty,
            stop=stop_price,
        )
        return str(order.id)

    def replace_stop_order(
        self, order_id: str, qty: int, new_stop_price: float
    ) -> str:
        from alpaca.trading.requests import ReplaceOrderRequest

        qty = _require_positive_integral_qty(qty)
        client_order_id = self._client_order_id("replace", "stop")
        request = ReplaceOrderRequest(
            qty=qty,
            stop_price=round(new_stop_price, 2),
            client_order_id=client_order_id,
        )
        try:
            new_order = self._replace_order_idempotent(
                order_id, request, client_order_id
            )
            log.info(
                "alpaca.stop_replaced",
                old_id=order_id,
                new_id=str(new_order.id),
                new_stop=new_stop_price,
                qty=qty,
            )
            return str(new_order.id)
        except Exception as e:
            log.error(
                "alpaca.replace_stop_error",
                order_id=order_id,
                error=str(e),
            )
            # Never cancel-and-resubmit after an ambiguous replace failure.
            # The replacement may already exist; a blind fallback can leave two
            # sell orders live.  Preserve the known stop and let the next tick
            # reconcile it.
            current = self.get_order_status(order_id)
            if current.get("status") not in _TERMINAL_ORDER_STATUSES:
                log.warning(
                    "alpaca.replace_stop_preserving_original",
                    order_id=order_id,
                    status=current.get("status"),
                )
                return order_id
            raise

    def cancel_order(self, order_id: str) -> bool:
        try:
            current = self.get_order_status(order_id)
            if str(current.get("id", "") or "") != str(order_id):
                log.error(
                    "alpaca.cancel_identity_mismatch",
                    order_id=order_id,
                    observed_order_id=current.get("id"),
                )
                return False
            if current.get("status") in _TERMINAL_ORDER_STATUSES:
                return current.get("status") in {
                    "canceled",
                    "cancelled",
                    "expired",
                    "replaced",
                }
            self._client.cancel_order_by_id(order_id)
            deadline = time.monotonic() + _ORDER_CONFIRM_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                current = self.get_order_status(order_id)
                if str(current.get("id", "") or "") != str(order_id):
                    log.error(
                        "alpaca.cancel_identity_mismatch",
                        order_id=order_id,
                        observed_order_id=current.get("id"),
                    )
                    return False
                status = current.get("status")
                if status in {"canceled", "cancelled", "expired", "replaced"}:
                    log.info("alpaca.order_cancelled", order_id=order_id)
                    return True
                if status in {"filled", "rejected"}:
                    log.warning(
                        "alpaca.cancel_lost_race",
                        order_id=order_id,
                        status=status,
                    )
                    return False
                time.sleep(_ORDER_CONFIRM_POLL_SECONDS)
            log.error("alpaca.cancel_unconfirmed", order_id=order_id)
            return False
        except Exception as e:
            log.error("alpaca.cancel_error", order_id=order_id, error=str(e))
            return False

    def _get_open_orders(self, symbols: list[str] | None = None) -> list[object]:
        """Return every currently open order, including nested bracket legs."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request_kwargs: dict[str, object] = {
            "status": QueryOrderStatus.OPEN,
            "nested": True,
        }
        if symbols:
            request_kwargs["symbols"] = symbols
        request = GetOrdersRequest(**request_kwargs)
        orders = self._client.get_orders(filter=request)
        if not isinstance(orders, list):
            raise RuntimeError(
                "Alpaca open-order query returned an unrecognized payload"
            )
        return orders

    def cancel_open_orders_for_symbol(self, symbol: str) -> bool:
        """Cancel and confirm every open order for a symbol."""
        try:
            orders = self._get_open_orders([symbol])
        except Exception as e:
            log.error(
                "alpaca.open_orders_lookup_error", symbol=symbol, error=str(e)
            )
            return False

        results = [self.cancel_order(str(order.id)) for order in orders]
        return all(results) if results else True

    def close_position(self, symbol: str) -> bool:
        try:
            if not self.cancel_open_orders_for_symbol(symbol):
                log.error(
                    "alpaca.close_blocked_by_open_orders",
                    symbol=symbol,
                )
                return False
            if self._position_qty(symbol) == 0:
                return True
            order = self._client.close_position(symbol)
            order_id = str(getattr(order, "id", ""))
            if order_id:
                deadline = time.monotonic() + _ORDER_CONFIRM_TIMEOUT_SECONDS
                while time.monotonic() < deadline:
                    status = self.get_order_status(order_id).get("status")
                    if status == "filled":
                        break
                    if status in {
                        "canceled",
                        "cancelled",
                        "expired",
                        "rejected",
                    }:
                        return False
                    time.sleep(_ORDER_CONFIRM_POLL_SECONDS)
            closed = self._position_qty(symbol) == 0
            if closed:
                log.info("alpaca.position_closed", symbol=symbol)
            else:
                log.error("alpaca.position_close_unconfirmed", symbol=symbol)
            return closed
        except Exception as e:
            log.error("alpaca.close_error", symbol=symbol, error=str(e))
            return False

    def _position_qty(self, symbol: str) -> int:
        for position in self.get_positions():
            if position.get("symbol") == symbol:
                qty = position.get("qty")
                if not isinstance(qty, int) or isinstance(qty, bool):
                    raise RuntimeError(
                        f"Unnormalized broker quantity for {symbol}: {qty!r}"
                    )
                return qty
        return 0

    def close_all_positions(self) -> bool:
        """Cancel/flatten globally and retain exact close-fill snapshots."""
        self._last_close_fills = []
        try:
            snapshot_error: Exception | None = None
            try:
                positions_before = self.get_positions()
            except Exception as exc:
                # An invalid accounting snapshot (for example a fractional
                # holding created outside this bot) must not prevent the one
                # operation that makes the account safe.  Flatten first, prove
                # the live account empty, then return False because exact P&L
                # bookkeeping cannot be reconstructed from this snapshot.
                positions_before = []
                snapshot_error = exc
                log.critical(
                    "alpaca.close_all_preclose_snapshot_invalid",
                    error=str(exc),
                )
            responses = self._client.close_all_positions(cancel_orders=True)
            deadline = time.monotonic() + _ORDER_CONFIRM_TIMEOUT_SECONDS
            while True:
                positions_error: Exception | None = None
                orders_error: Exception | None = None
                try:
                    positions = self.get_positions()
                except Exception as exc:
                    positions = []
                    positions_error = exc
                try:
                    open_orders = self._get_open_orders()
                except Exception as exc:
                    open_orders = []
                    orders_error = exc
                if (
                    positions_error is None
                    and orders_error is None
                    and not positions
                    and not open_orders
                ):
                    break
                if time.monotonic() >= deadline:
                    log.critical(
                        "alpaca.close_all_unconfirmed",
                        positions=[p.get("symbol") for p in positions],
                        open_order_ids=[
                            str(getattr(order, "id", ""))
                            for order in open_orders
                        ],
                        positions_error=(
                            str(positions_error) if positions_error else None
                        ),
                        orders_error=(str(orders_error) if orders_error else None),
                    )
                    return False
                time.sleep(_ORDER_CONFIRM_POLL_SECONDS)

            if snapshot_error is not None:
                log.critical(
                    "alpaca.close_all_accounting_snapshot_unavailable",
                    error=str(snapshot_error),
                )
                return False

            if not positions_before:
                # A close response despite an empty pre-close snapshot means
                # broker state changed between reads (or the snapshot was
                # stale).  There is no trusted signed quantity to match the
                # fill against, so succeeding with an empty accounting set
                # would hide an untracked liquidation.  Halt fail-closed.
                if not isinstance(responses, list) or responses:
                    log.critical(
                        "alpaca.close_all_unexpected_flat_snapshot_responses",
                        response_type=type(responses).__name__,
                        response_count=(
                            len(responses)
                            if isinstance(responses, list)
                            else None
                        ),
                    )
                    return False
                log.info("alpaca.all_positions_closed")
                return True
            if not isinstance(responses, list):
                log.critical(
                    "alpaca.close_all_fill_responses_missing",
                    response_type=type(responses).__name__,
                )
                return False

            close_order_ids = []
            for response in responses:
                if isinstance(response, dict):
                    order_id = response.get("order_id")
                else:
                    order_id = getattr(response, "order_id", None)
                normalized_order_id = str(order_id or "").strip()
                if not normalized_order_id:
                    log.critical(
                        "alpaca.close_all_response_missing_order_id",
                        response=repr(response),
                    )
                    return False
                close_order_ids.append(normalized_order_id)
            if (
                len(close_order_ids) != len(responses)
                or len(close_order_ids) != len(positions_before)
                or len(close_order_ids) != len(set(close_order_ids))
            ):
                log.critical(
                    "alpaca.close_all_response_count_or_identity_mismatch",
                    responses=len(responses),
                    order_ids=len(close_order_ids),
                    positions=len(positions_before),
                )
                return False

            fills = [
                self.get_order_status(order_id)
                for order_id in close_order_ids
            ]
            for requested_order_id, fill in zip(close_order_ids, fills):
                if str(fill.get("id", "") or "") != requested_order_id:
                    log.critical(
                        "alpaca.close_all_fill_id_mismatch",
                        requested_order_id=requested_order_id,
                        observed_order_id=fill.get("id"),
                    )
                    return False
            expected_by_symbol: dict[str, int] = {}
            for position in positions_before:
                symbol = str(position.get("symbol", "") or "").upper()
                signed_qty = _strict_integral_qty(
                    position.get("qty"),
                    allow_zero=False,
                    allow_negative=True,
                )
                if not symbol or signed_qty is None:
                    log.critical(
                        "alpaca.close_all_invalid_position_snapshot",
                        position=position,
                    )
                    return False
                if symbol in expected_by_symbol:
                    log.critical(
                        "alpaca.close_all_duplicate_position_symbol",
                        symbol=symbol,
                    )
                    return False
                expected_by_symbol[symbol] = signed_qty
            fills_by_symbol: dict[str, list[dict]] = {}
            for fill in fills:
                symbol = str(fill.get("symbol", "") or "").upper()
                fills_by_symbol.setdefault(symbol, []).append(fill)

            validated_fills = []
            for symbol, signed_qty in expected_by_symbol.items():
                matching = fills_by_symbol.get(symbol, [])
                expected_side = "sell" if signed_qty > 0 else "buy"
                if len(matching) != 1:
                    log.critical(
                        "alpaca.close_all_fill_count_mismatch",
                        symbol=symbol,
                        count=len(matching),
                    )
                    return False
                fill = matching[0]
                fill_price = float(
                    fill.get("filled_avg_price", 0.0) or 0.0
                )
                order_qty = _strict_integral_qty(
                    fill.get("qty"), allow_zero=False
                )
                filled_qty = _strict_integral_qty(
                    fill.get("filled_qty"), allow_zero=False
                )
                if (
                    _enum_value(fill.get("status", "")) != "filled"
                    or _enum_value(fill.get("side", "")) != expected_side
                    or order_qty != abs(signed_qty)
                    or filled_qty != abs(signed_qty)
                    or not math.isfinite(fill_price)
                    or fill_price <= 0
                ):
                    log.critical(
                        "alpaca.close_all_fill_unconfirmed",
                        symbol=symbol,
                        fill=fill,
                    )
                    return False
                validated_fills.append(dict(fill))

            if len(validated_fills) != len(fills):
                log.critical(
                    "alpaca.close_all_unmatched_fill",
                    expected=len(validated_fills),
                    observed=len(fills),
                )
                return False
            self._last_close_fills = validated_fills
            log.info(
                "alpaca.all_positions_closed",
                fills=len(validated_fills),
            )
            return True
        except Exception as e:
            log.error("alpaca.close_all_error", error=str(e))
            return False

    def get_last_close_fills(self) -> list[dict]:
        """Return immutable snapshots of the last global-flatten fills."""
        return [dict(fill) for fill in getattr(self, "_last_close_fills", [])]

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def get_order_status(self, order_id: str) -> dict:
        try:
            from alpaca.trading.requests import GetOrderByIdRequest

            order = self._client.get_order_by_id(
                order_id,
                filter=GetOrderByIdRequest(nested=True),
            )
            return {
                "id": str(order.id),
                "symbol": str(getattr(order, "symbol", "") or ""),
                "side": _enum_value(getattr(order, "side", "")),
                "qty": _strict_integral_qty(
                    getattr(order, "qty", None), allow_zero=False
                ),
                "type": _enum_value(getattr(order, "type", "")),
                "stop_price": float(
                    getattr(order, "stop_price", 0.0) or 0.0
                ),
                "limit_price": float(
                    getattr(order, "limit_price", 0.0) or 0.0
                ),
                "status": _enum_value(order.status),
                "filled_qty": _strict_integral_qty(
                    getattr(order, "filled_qty", 0), allow_zero=True
                ),
                "filled_avg_price": (
                    float(order.filled_avg_price)
                    if order.filled_avg_price
                    else 0.0
                ),
                "client_order_id": str(
                    getattr(order, "client_order_id", "") or ""
                ),
                "legs": [
                    self._leg_payload(leg)
                    for leg in (getattr(order, "legs", None) or [])
                ],
            }
        except Exception as e:
            log.error("alpaca.order_status_error", order_id=order_id, error=str(e))
            return {
                "id": order_id,
                "status": "error",
                "filled_qty": 0,
                "filled_avg_price": 0.0,
            }

    def get_day_trade_count(self) -> int:
        try:
            account = self._client.get_account()
            return int(account.daytrade_count) if account.daytrade_count else 0
        except Exception:
            return 0

    def reset_paper_account(self) -> bool:
        """
        Reset the Alpaca paper trading account to default $100K balance.

        Uses the undocumented but functional DELETE /v2/account endpoint
        on the paper trading API. This is the same action that the Alpaca
        dashboard "Reset Account" button used before it was removed.

        Returns True if reset succeeded, False otherwise.
        """
        if not self._paper:
            log.error("alpaca.reset_refused", reason="Cannot reset a LIVE account")
            return False

        import requests

        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
        }

        # Close all positions and cancel orders first
        try:
            self._client.cancel_orders()
            log.info("alpaca.reset_orders_cancelled")
        except Exception as e:
            log.warning("alpaca.reset_cancel_orders_error", error=str(e))

        try:
            self._client.close_all_positions(cancel_orders=True)
            log.info("alpaca.reset_positions_closed")
        except Exception as e:
            log.warning("alpaca.reset_close_positions_error", error=str(e))

        # Reset via DELETE /v2/account
        url = f"{_PAPER_BASE_URL}/v2/account"
        try:
            resp = requests.delete(url, headers=headers, timeout=30)
            if resp.status_code in (200, 204):
                account = self._client.get_account()
                new_equity = float(account.equity or 0)
                log.info(
                    "alpaca.reset_success",
                    new_equity=new_equity,
                )
                print(f"  Paper account reset successful! New equity: ${new_equity:,.2f}")
                return True
            else:
                log.error(
                    "alpaca.reset_api_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                print(f"  API reset failed (HTTP {resp.status_code}). "
                      f"Try creating new paper API keys at https://app.alpaca.markets")
                return False
        except Exception as e:
            log.error("alpaca.reset_error", error=str(e))
            print(f"  Reset error: {e}")
            print("  Alternative: Create new paper trading API keys at https://app.alpaca.markets")
            return False
