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
