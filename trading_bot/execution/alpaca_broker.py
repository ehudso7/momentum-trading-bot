"""
Alpaca broker implementation using alpaca-py SDK with retry logic.

Supports both paper and live trading via the same interface.
Paper trading uses Alpaca's paper trading environment.
"""

from __future__ import annotations

import structlog

from trading_bot.config.settings import BrokerConfig
from trading_bot.execution.broker_base import BrokerBase
from trading_bot.models.domain import OrderSide
from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)

# Alpaca paper trading base URL
_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class AlpacaBroker(BrokerBase):
    """
    Alpaca implementation using alpaca-py SDK.

    Requires alpaca_api_key and alpaca_api_secret in config.
    Set alpaca_paper=True for paper trading (default and recommended).
    """

    def __init__(self, config: BrokerConfig):
        from alpaca.trading.client import TradingClient

        self._client = TradingClient(
            api_key=config.alpaca_api_key.get_secret_value(),
            secret_key=config.alpaca_api_secret.get_secret_value(),
            paper=config.alpaca_paper,
        )
        self._paper = config.alpaca_paper
        log.info("alpaca.connected", paper=self._paper)

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

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def submit_market_order(self, symbol: str, qty: int, side: OrderSide) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self._client.submit_order(request)
        log.info(
            "alpaca.market_order",
            order_id=str(order.id),
            symbol=symbol,
            side=side.value,
            qty=qty,
        )
        return str(order.id)

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def submit_limit_order(
        self, symbol: str, qty: int, side: OrderSide, limit_price: float
    ) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
        )
        order = self._client.submit_order(request)
        log.info(
            "alpaca.limit_order",
            order_id=str(order.id),
            symbol=symbol,
            side=side.value,
            qty=qty,
            price=limit_price,
        )
        return str(order.id)

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
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

        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        )
        order = self._client.submit_order(request)

        # Extract leg order IDs
        stop_id = ""
        tp_id = ""
        if order.legs:
            for leg in order.legs:
                if hasattr(leg, "stop_price") and leg.stop_price:
                    stop_id = str(leg.id)
                elif hasattr(leg, "limit_price") and leg.limit_price:
                    tp_id = str(leg.id)

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

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def submit_stop_order(self, symbol: str, qty: int, stop_price: float) -> str:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        request = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_price,
        )
        order = self._client.submit_order(request)
        log.info(
            "alpaca.stop_order",
            order_id=str(order.id),
            symbol=symbol,
            qty=qty,
            stop=stop_price,
        )
        return str(order.id)

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def replace_stop_order(
        self, order_id: str, qty: int, new_stop_price: float
    ) -> str:
        from alpaca.trading.requests import ReplaceOrderRequest

        request = ReplaceOrderRequest(
            qty=qty,
            stop_price=round(new_stop_price, 2),
        )
        try:
            new_order = self._client.replace_order_by_id(order_id, request)
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
            # Fallback: cancel old and submit new
            # Look up the symbol from the original order
            symbol = ""
            try:
                old_order = self._client.get_order_by_id(order_id)
                symbol = old_order.symbol or ""
            except Exception:
                pass
            self.cancel_order(order_id)
            if not symbol:
                log.error("alpaca.replace_stop_no_symbol", order_id=order_id)
                return ""
            return self.submit_stop_order(
                symbol=symbol,
                qty=qty,
                stop_price=new_stop_price,
            )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            log.info("alpaca.order_cancelled", order_id=order_id)
            return True
        except Exception as e:
            log.error("alpaca.cancel_error", order_id=order_id, error=str(e))
            return False

    def close_position(self, symbol: str) -> bool:
        try:
            self._client.close_position(symbol)
            log.info("alpaca.position_closed", symbol=symbol)
            return True
        except Exception as e:
            log.error("alpaca.close_error", symbol=symbol, error=str(e))
            return False

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
            order = self._client.get_order_by_id(order_id)
            return {
                "id": str(order.id),
                "status": str(order.status),
                "filled_qty": int(order.filled_qty) if order.filled_qty else 0,
                "filled_avg_price": (
                    float(order.filled_avg_price)
                    if order.filled_avg_price
                    else 0.0
                ),
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

        api_key = self._client._api_key
        secret_key = self._client._secret_key

        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
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
