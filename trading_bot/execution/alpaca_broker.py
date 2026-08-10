"""
Alpaca broker implementation using alpaca-py SDK with retry logic.

Supports both paper and live trading via the same interface.
Paper trading uses Alpaca's paper trading environment.
"""

from __future__ import annotations

import time
import uuid

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


def _enum_value(value: object) -> str:
    """Return the wire value for alpaca-py string enums.

    ``str(OrderStatus.FILLED)`` is ``"OrderStatus.FILLED"`` on supported
    alpaca-py versions, not ``"filled"``.  Order lifecycle decisions must use
    the enum's value or the portfolio manager will miss fills and terminal
    states.
    """
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


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
            "status": _enum_value(getattr(leg, "status", "")),
            "type": _enum_value(getattr(leg, "type", "")),
            "filled_qty": int(getattr(leg, "filled_qty", 0) or 0),
            "filled_avg_price": float(
                getattr(leg, "filled_avg_price", 0.0) or 0.0
            ),
            "stop_price": float(getattr(leg, "stop_price", 0.0) or 0.0),
            "limit_price": float(getattr(leg, "limit_price", 0.0) or 0.0),
        }

    @classmethod
    def _bracket_leg_ids(cls, order: object) -> tuple[str, str]:
        stop_id = ""
        tp_id = ""
        for leg in getattr(order, "legs", None) or []:
            payload = cls._leg_payload(leg)
            order_type = payload["type"]
            if payload["stop_price"] > 0 or order_type in {
                "stop",
                "stop_limit",
                "trailing_stop",
            }:
                stop_id = payload["id"]
            elif payload["limit_price"] > 0 or order_type == "limit":
                tp_id = payload["id"]
        return stop_id, tp_id

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
        return [
            {
                "symbol": p.symbol,
                "qty": int(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "market_value": float(p.market_value),
            }
            for p in positions
        ]

    def submit_market_order(self, symbol: str, qty: int, side: OrderSide) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

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
        stop_id, tp_id = self._bracket_leg_ids(order)

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

    def cancel_open_orders_for_symbol(self, symbol: str) -> bool:
        """Cancel and confirm every open order for a symbol."""
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            request = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[symbol],
                nested=True,
            )
            orders = self._client.get_orders(filter=request)
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
                return int(position.get("qty", 0))
        return 0

    def close_all_positions(self) -> bool:
        try:
            self._client.close_all_positions(cancel_orders=True)
            log.info("alpaca.all_positions_closed")
            return True
        except Exception as e:
            log.error("alpaca.close_all_error", error=str(e))
            return False

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
                "status": _enum_value(order.status),
                "filled_qty": int(order.filled_qty) if order.filled_qty else 0,
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
