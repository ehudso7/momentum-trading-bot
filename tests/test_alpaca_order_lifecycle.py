"""Regression tests for Alpaca's asynchronous order lifecycle.

These tests pin the production incident where enum statuses were not
normalized, bracket legs were absent from the immediate response, and a retry
could submit the same logical exit more than once.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from enum import Enum
from types import SimpleNamespace
from unittest.mock import patch


def _install_local_dependency_stubs() -> None:
    """Allow this focused suite to run in a stdlib-only incident workspace.

    CI installs the project's real dependencies, so none of these branches run
    there.  They keep the regression executable locally when only Python's
    standard library is available.
    """
    if importlib.util.find_spec("structlog") is None:
        structlog = types.ModuleType("structlog")

        class _Log:
            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

        structlog.get_logger = lambda *_args, **_kwargs: _Log()
        sys.modules["structlog"] = structlog

    if importlib.util.find_spec("pydantic_settings") is None:
        settings = types.ModuleType("trading_bot.config.settings")
        settings.BrokerConfig = object
        sys.modules["trading_bot.config.settings"] = settings

    if importlib.util.find_spec("alpaca") is not None:
        return

    alpaca = types.ModuleType("alpaca")
    trading = types.ModuleType("alpaca.trading")
    enums = types.ModuleType("alpaca.trading.enums")
    requests = types.ModuleType("alpaca.trading.requests")

    class _StringEnum(str, Enum):
        def __str__(self) -> str:
            return f"{type(self).__name__}.{self.name}"

    class AlpacaOrderSide(_StringEnum):
        BUY = "buy"
        SELL = "sell"

    class TimeInForce(_StringEnum):
        DAY = "day"

    class OrderClass(_StringEnum):
        BRACKET = "bracket"

    class PositionIntent(_StringEnum):
        BUY_TO_OPEN = "buy_to_open"
        SELL_TO_CLOSE = "sell_to_close"

    class QueryOrderStatus(_StringEnum):
        OPEN = "open"

    class _Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    for name in (
        "MarketOrderRequest",
        "LimitOrderRequest",
        "StopOrderRequest",
        "StopLossRequest",
        "TakeProfitRequest",
        "ReplaceOrderRequest",
        "GetOrderByIdRequest",
        "GetOrdersRequest",
    ):
        setattr(requests, name, type(name, (_Request,), {}))

    enums.OrderSide = AlpacaOrderSide
    enums.TimeInForce = TimeInForce
    enums.OrderClass = OrderClass
    enums.PositionIntent = PositionIntent
    enums.QueryOrderStatus = QueryOrderStatus

    sys.modules.update(
        {
            "alpaca": alpaca,
            "alpaca.trading": trading,
            "alpaca.trading.enums": enums,
            "alpaca.trading.requests": requests,
        }
    )


_install_local_dependency_stubs()

from alpaca.trading.enums import PositionIntent  # noqa: E402

from trading_bot.execution.alpaca_broker import AlpacaBroker  # noqa: E402
from trading_bot.models.domain import OrderSide  # noqa: E402


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    CANCELED = "canceled"

    def __str__(self) -> str:
        return f"OrderStatus.{self.name}"


def _order(
    order_id: str = "order-1",
    *,
    status: OrderStatus = OrderStatus.NEW,
    filled_qty: int = 0,
    filled_avg_price: float = 0.0,
    legs: list[object] | None = None,
    client_order_id: str = "client-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        legs=legs,
        client_order_id=client_order_id,
    )


def _broker(client: object) -> AlpacaBroker:
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = client
    broker._paper = True
    return broker


class TestAlpacaOrderLifecycle(unittest.TestCase):
    def test_order_status_requests_nested_bracket_and_normalizes_legs(self):
        stop = SimpleNamespace(
            id="stop-1",
            status=OrderStatus.NEW,
            type=SimpleNamespace(value="stop"),
            filled_qty=0,
            filled_avg_price=None,
            stop_price="3.49",
            limit_price=None,
        )
        target = SimpleNamespace(
            id="tp-1",
            status=OrderStatus.NEW,
            type=SimpleNamespace(value="limit"),
            filled_qty=0,
            filled_avg_price=None,
            stop_price=None,
            limit_price="3.67",
        )

        class Client:
            def get_order_by_id(self, order_id, filter):
                self.order_id = order_id
                self.filter = filter
                return _order(
                    status=OrderStatus.FILLED,
                    filled_qty=647,
                    filled_avg_price=3.55,
                    legs=[stop, target],
                )

        client = Client()
        status = _broker(client).get_order_status("order-1")

        self.assertTrue(client.filter.nested)
        self.assertEqual(status["status"], "filled")
        self.assertEqual(status["filled_qty"], 647)
        self.assertEqual([leg["id"] for leg in status["legs"]], ["stop-1", "tp-1"])
        self.assertEqual([leg["status"] for leg in status["legs"]], ["new", "new"])

    def test_sell_market_order_is_sell_to_close(self):
        class Client:
            def submit_order(self, request):
                self.request = request
                return _order(status=OrderStatus.NEW)

        client = Client()
        _broker(client).submit_market_order("BWMN", 40, OrderSide.SELL)

        self.assertEqual(client.request.position_intent, PositionIntent.SELL_TO_CLOSE)
        self.assertTrue(client.request.client_order_id.startswith("mtb-market-bwmn-"))

    def test_timeout_after_accept_recovers_existing_order_without_duplicate(self):
        existing = _order(order_id="accepted-order", status=OrderStatus.NEW)

        class Client:
            def __init__(self):
                self.submit_calls = 0
                self.lookup_ids = []

            def submit_order(self, _request):
                self.submit_calls += 1
                raise TimeoutError("timed out after acceptance")

            def get_order_by_client_id(self, client_order_id):
                self.lookup_ids.append(client_order_id)
                return existing

        client = Client()
        order_id = _broker(client).submit_market_order("JWEL", 647, OrderSide.BUY)

        self.assertEqual(order_id, "accepted-order")
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(len(set(client.lookup_ids)), 1)

    def test_retry_reuses_exact_client_order_id(self):
        class Client:
            def __init__(self):
                self.requests = []

            def submit_order(self, request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise TimeoutError("timeout before response")
                return _order(order_id="order-2", status=OrderStatus.NEW)

            def get_order_by_client_id(self, _client_order_id):
                raise LookupError("not visible yet")

        client = Client()
        with patch("trading_bot.utils.resilience.time.sleep", return_value=None):
            order_id = _broker(client).submit_market_order(
                "JWEL", 647, OrderSide.BUY
            )

        self.assertEqual(order_id, "order-2")
        self.assertEqual(len(client.requests), 2)
        self.assertIs(client.requests[0], client.requests[1])
        self.assertEqual(
            client.requests[0].client_order_id,
            client.requests[1].client_order_id,
        )

    def test_bracket_may_have_no_legs_in_immediate_response(self):
        class Client:
            def submit_order(self, request):
                self.request = request
                return _order(status=OrderStatus.NEW, legs=None)

        result = _broker(Client()).submit_bracket_order(
            "JWEL",
            647,
            OrderSide.BUY,
            stop_price=3.49,
            take_profit_price=3.67,
        )

        self.assertEqual(result["entry_order_id"], "order-1")
        self.assertEqual(result["stop_order_id"], "")
        self.assertEqual(result["tp_order_id"], "")

    def test_cancel_returns_only_after_broker_confirms_canceled(self):
        class Client:
            def __init__(self):
                self.statuses = [OrderStatus.NEW, OrderStatus.CANCELED]
                self.cancel_calls = 0

            def get_order_by_id(self, _order_id, filter):
                del filter
                status = self.statuses.pop(0) if self.statuses else OrderStatus.CANCELED
                return _order(status=status)

            def cancel_order_by_id(self, _order_id):
                self.cancel_calls += 1

        client = Client()
        with patch("trading_bot.execution.alpaca_broker.time.sleep", return_value=None):
            canceled = _broker(client).cancel_order("order-1")

        self.assertTrue(canceled)
        self.assertEqual(client.cancel_calls, 1)
        self.assertEqual(client.statuses, [])

    def test_ambiguous_stop_replace_never_cancel_resubmits(self):
        class Client:
            def __init__(self):
                self.cancel_calls = 0
                self.submit_calls = 0

            def replace_order_by_id(self, _order_id, _request):
                raise TimeoutError("replace timed out")

            def get_order_by_client_id(self, _client_order_id):
                raise LookupError("not visible")

            def get_order_by_id(self, _order_id, filter):
                del filter
                return _order(status=OrderStatus.NEW)

            def cancel_order_by_id(self, _order_id):
                self.cancel_calls += 1

            def submit_order(self, _request):
                self.submit_calls += 1

        client = Client()
        with patch("trading_bot.utils.resilience.time.sleep", return_value=None):
            result = _broker(client).replace_stop_order("stop-1", 40, 42.31)

        self.assertEqual(result, "stop-1")
        self.assertEqual(client.cancel_calls, 0)
        self.assertEqual(client.submit_calls, 0)


if __name__ == "__main__":
    unittest.main()
