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
    def missing(module_name: str) -> bool:
        try:
            return importlib.util.find_spec(module_name) is None
        except (ImportError, ModuleNotFoundError, ValueError):
            return True

    if missing("structlog"):
        structlog = types.ModuleType("structlog")

        class _Log:
            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

        structlog.get_logger = lambda *_args, **_kwargs: _Log()
        sys.modules["structlog"] = structlog

    if missing("pydantic_settings"):
        settings = types.ModuleType("trading_bot.config.settings")
        settings.BrokerConfig = object
        sys.modules["trading_bot.config.settings"] = settings

    if not missing("alpaca"):
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
    ACCEPTED = "accepted"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    HELD = "held"

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
    symbol: str = "JWEL",
    side: object | None = None,
    qty: int = 647,
    order_type: str = "market",
    stop_price: float | None = None,
    limit_price: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        symbol=symbol,
        side=side or SimpleNamespace(value="buy"),
        qty=qty,
        type=SimpleNamespace(value=order_type),
        stop_price=stop_price,
        limit_price=limit_price,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        legs=legs,
        client_order_id=client_order_id,
    )


def _leg(
    leg_id: str,
    order_type: str,
    *,
    status: object = OrderStatus.NEW,
    side: object = SimpleNamespace(value="sell"),
    qty: int = 647,
) -> SimpleNamespace:
    is_stop = order_type in {"stop", "stop_limit", "trailing_stop"}
    return SimpleNamespace(
        id=leg_id,
        symbol="JWEL",
        side=side,
        qty=qty,
        status=status,
        type=SimpleNamespace(value=order_type),
        filled_qty=0,
        filled_avg_price=None,
        stop_price="3.49" if is_stop else None,
        limit_price=None if is_stop else "3.67",
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

    def test_order_status_exposes_exact_root_stop_fields(self):
        class Client:
            def get_order_by_id(self, order_id, filter):
                self.order_id = order_id
                self.filter = filter
                return _order(
                    order_id="stop-1",
                    status=OrderStatus.NEW,
                    side=SimpleNamespace(value="sell"),
                    qty=40,
                    order_type="stop",
                    stop_price=42.31,
                )

        status = _broker(Client()).get_order_status("stop-1")

        self.assertEqual(status["symbol"], "JWEL")
        self.assertEqual(status["side"], "sell")
        self.assertEqual(status["qty"], 40)
        self.assertEqual(status["type"], "stop")
        self.assertEqual(status["stop_price"], 42.31)
        self.assertEqual(status["limit_price"], 0.0)

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

    def test_bracket_immediate_ids_are_ignored_until_both_legs_are_active(self):
        invalid_statuses = (
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FILLED,
            OrderStatus.HELD,
            SimpleNamespace(value="unaccepted"),
            "",
        )

        for invalid_status in invalid_statuses:
            with self.subTest(status=invalid_status):
                class Client:
                    def submit_order(self, request):
                        self.request = request
                        return _order(
                            status=OrderStatus.FILLED,
                            legs=[
                                _leg("stop-1", "stop"),
                                _leg(
                                    "tp-1",
                                    "limit",
                                    status=invalid_status,
                                ),
                            ],
                        )

                result = _broker(Client()).submit_bracket_order(
                    "JWEL",
                    647,
                    OrderSide.BUY,
                    stop_price=3.49,
                    take_profit_price=3.67,
                )

                self.assertEqual(result["stop_order_id"], "")
                self.assertEqual(result["tp_order_id"], "")

    def test_bracket_immediate_ids_require_an_active_protective_pair(self):
        class Client:
            def submit_order(self, request):
                self.request = request
                return _order(
                    status=OrderStatus.FILLED,
                    legs=[
                        _leg(
                            "stop-1",
                            "stop",
                            status=OrderStatus.NEW,
                        ),
                        _leg(
                            "tp-1",
                            "limit",
                            status=OrderStatus.NEW,
                        ),
                    ],
                )

        result = _broker(Client()).submit_bracket_order(
            "JWEL",
            647,
            OrderSide.BUY,
            stop_price=3.49,
            take_profit_price=3.67,
        )

        self.assertEqual(result["stop_order_id"], "stop-1")
        self.assertEqual(result["tp_order_id"], "tp-1")

    def test_filled_bracket_accepts_held_stop_with_new_take_profit(self):
        class Client:
            def submit_order(self, request):
                self.request = request
                return _order(
                    status=OrderStatus.FILLED,
                    legs=[
                        _leg("stop-1", "stop", status=OrderStatus.HELD),
                        _leg("tp-1", "limit", status=OrderStatus.NEW),
                    ],
                )

        result = _broker(Client()).submit_bracket_order(
            "JWEL",
            647,
            OrderSide.BUY,
            stop_price=3.49,
            take_profit_price=3.67,
        )

        self.assertEqual(result["stop_order_id"], "stop-1")
        self.assertEqual(result["tp_order_id"], "tp-1")

    def test_pending_parent_never_proves_protective_pair(self):
        class Client:
            def submit_order(self, request):
                self.request = request
                return _order(
                    status=OrderStatus.NEW,
                    legs=[
                        _leg("stop-1", "stop", status=OrderStatus.NEW),
                        _leg("tp-1", "limit", status=OrderStatus.NEW),
                    ],
                )

        result = _broker(Client()).submit_bracket_order(
            "JWEL",
            647,
            OrderSide.BUY,
            stop_price=3.49,
            take_profit_price=3.67,
        )

        self.assertEqual(result["stop_order_id"], "")
        self.assertEqual(result["tp_order_id"], "")

    def test_filled_bracket_rejects_both_legs_held(self):
        class Client:
            def submit_order(self, request):
                self.request = request
                return _order(
                    status=OrderStatus.FILLED,
                    legs=[
                        _leg("stop-1", "stop", status=OrderStatus.HELD),
                        _leg("tp-1", "limit", status=OrderStatus.HELD),
                    ],
                )

        result = _broker(Client()).submit_bracket_order(
            "JWEL",
            647,
            OrderSide.BUY,
            stop_price=3.49,
            take_profit_price=3.67,
        )

        self.assertEqual(result["stop_order_id"], "")
        self.assertEqual(result["tp_order_id"], "")

    def test_bracket_immediate_ids_require_sell_side_and_exact_quantity(self):
        invalid_legs = (
            [
                _leg("stop-1", "stop", side=SimpleNamespace(value="buy")),
                _leg("tp-1", "limit"),
            ],
            [
                _leg("stop-1", "stop", qty=646),
                _leg("tp-1", "limit"),
            ],
            [
                _leg("stop-1", "stop"),
                _leg("tp-1", "limit", qty=648),
            ],
            [
                _leg("stop-1", "stop", qty=647.5),
                _leg("tp-1", "limit"),
            ],
            [
                _leg("stop-1", "stop"),
                _leg("stop-2", "stop"),
                _leg("tp-1", "limit"),
            ],
        )

        for legs in invalid_legs:
            with self.subTest(legs=legs):
                class Client:
                    def submit_order(self, request):
                        self.request = request
                        return _order(status=OrderStatus.FILLED, legs=legs)

                result = _broker(Client()).submit_bracket_order(
                    "JWEL",
                    647,
                    OrderSide.BUY,
                    stop_price=3.49,
                    take_profit_price=3.67,
                )

                self.assertEqual(result["stop_order_id"], "")
                self.assertEqual(result["tp_order_id"], "")

    def test_order_status_never_truncates_fractional_quantities(self):
        class Client:
            def get_order_by_id(self, order_id, filter):
                del order_id, filter
                return _order(
                    qty=647.5,
                    filled_qty=647.5,
                    status=OrderStatus.FILLED,
                    legs=[
                        _leg("stop-1", "stop", qty=647.5),
                        _leg("tp-1", "limit", qty=647.5),
                    ],
                )

        status = _broker(Client()).get_order_status("order-1")

        self.assertIsNone(status["qty"])
        self.assertIsNone(status["filled_qty"])
        self.assertTrue(all(leg["qty"] is None for leg in status["legs"]))

    def test_positions_reject_fractional_broker_quantity(self):
        fractional = SimpleNamespace(
            symbol="JWEL",
            qty="647.5",
            avg_entry_price="3.55",
            current_price="3.50",
            unrealized_pl="-32.35",
            market_value="2264.50",
        )

        class Client:
            def get_all_positions(self):
                return [fractional]

        with self.assertRaises(RuntimeError):
            _broker(Client()).get_positions()

    def test_fractional_preclose_position_is_still_globally_flattened(self):
        fractional = SimpleNamespace(
            symbol="JWEL",
            qty="647.5",
            avg_entry_price="3.55",
            current_price="3.50",
            unrealized_pl="-32.35",
            market_value="2266.25",
        )

        class Client:
            def __init__(self):
                self.closed = False
                self.close_calls = 0
                self.position_reads = 0

            def get_all_positions(self):
                self.position_reads += 1
                return [] if self.closed else [fractional]

            def close_all_positions(self, cancel_orders):
                self.close_calls += 1
                self.cancel_orders = cancel_orders
                self.closed = True
                return [SimpleNamespace(order_id="close-1")]

            def get_orders(self, filter):
                self.order_filter = filter
                return []

            def get_order_by_id(self, _order_id, filter):
                raise AssertionError(
                    "an invalid pre-close snapshot cannot support accounting"
                )

        client = Client()
        broker = _broker(client)

        self.assertFalse(broker.close_all_positions())
        self.assertEqual(client.close_calls, 1)
        self.assertTrue(client.cancel_orders)
        self.assertGreaterEqual(client.position_reads, 2)
        self.assertEqual(client.get_all_positions(), [])
        self.assertEqual(broker.get_last_close_fills(), [])

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

    def test_cancel_rejects_snapshot_for_different_order_id(self):
        class Client:
            def __init__(self):
                self.cancel_calls = 0

            def get_order_by_id(self, _order_id, filter):
                del filter
                return _order(
                    order_id="different-order",
                    status=OrderStatus.CANCELED,
                )

            def cancel_order_by_id(self, _order_id):
                self.cancel_calls += 1

        client = Client()

        self.assertFalse(_broker(client).cancel_order("order-1"))
        self.assertEqual(client.cancel_calls, 0)

    def test_cancel_poll_rejects_snapshot_for_different_order_id(self):
        class Client:
            def __init__(self):
                self.reads = 0
                self.cancel_calls = 0

            def get_order_by_id(self, _order_id, filter):
                del filter
                self.reads += 1
                return _order(
                    order_id="order-1" if self.reads == 1 else "different-order",
                    status=(
                        OrderStatus.NEW
                        if self.reads == 1
                        else OrderStatus.CANCELED
                    ),
                )

            def cancel_order_by_id(self, _order_id):
                self.cancel_calls += 1

        client = Client()

        self.assertFalse(_broker(client).cancel_order("order-1"))
        self.assertEqual(client.cancel_calls, 1)

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

    def test_close_all_returns_only_after_positions_and_orders_are_empty(self):
        position = SimpleNamespace(
            symbol="JWEL",
            qty="647",
            avg_entry_price="3.55",
            current_price="3.50",
            unrealized_pl="-32.35",
            market_value="2264.50",
        )
        orphan_order = _order(order_id="orphan-stop")

        class Client:
            def __init__(self):
                self.close_calls = []
                self.position_reads = 0
                self.order_reads = 0

            def close_all_positions(self, cancel_orders):
                self.close_calls.append(cancel_orders)
                return [
                    SimpleNamespace(
                        order_id="close-1",
                        symbol="JWEL",
                        status=200,
                    )
                ]

            def get_all_positions(self):
                self.position_reads += 1
                return [position] if self.position_reads <= 2 else []

            def get_orders(self, filter):
                self.order_filter = filter
                self.order_reads += 1
                return [orphan_order] if self.order_reads == 1 else []

            def get_order_by_id(self, order_id, filter):
                self.close_order_filter = filter
                return _order(
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    filled_qty=647,
                    filled_avg_price=3.49,
                    symbol="JWEL",
                    side=SimpleNamespace(value="sell"),
                    qty=647,
                )

        client = Client()
        broker = _broker(client)
        with patch(
            "trading_bot.execution.alpaca_broker.time.sleep",
            return_value=None,
        ):
            closed = broker.close_all_positions()

        self.assertTrue(closed)
        self.assertEqual(client.close_calls, [True])
        self.assertGreaterEqual(client.position_reads, 3)
        self.assertGreaterEqual(client.order_reads, 2)
        self.assertTrue(client.order_filter.nested)
        self.assertTrue(client.close_order_filter.nested)
        self.assertEqual(broker.get_last_close_fills()[0]["id"], "close-1")
        self.assertEqual(
            broker.get_last_close_fills()[0]["filled_avg_price"],
            3.49,
        )

    def test_close_all_rejects_flat_account_without_exact_fill_response(self):
        position = SimpleNamespace(
            symbol="JWEL",
            qty="647",
            avg_entry_price="3.55",
            current_price="3.50",
            unrealized_pl="-32.35",
            market_value="2264.50",
        )

        class Client:
            def __init__(self):
                self.position_reads = 0

            def close_all_positions(self, cancel_orders):
                self.cancel_orders = cancel_orders
                return []

            def get_all_positions(self):
                self.position_reads += 1
                return [position] if self.position_reads == 1 else []

            def get_orders(self, filter):
                self.order_filter = filter
                return []

        client = Client()
        broker = _broker(client)
        self.assertFalse(broker.close_all_positions())
        self.assertTrue(client.cancel_orders)
        self.assertEqual(broker.get_last_close_fills(), [])

    def test_close_all_rejects_any_response_without_order_id(self):
        position = SimpleNamespace(
            symbol="JWEL",
            qty="647",
            avg_entry_price="3.55",
            current_price="3.50",
            unrealized_pl="-32.35",
            market_value="2264.50",
        )

        class Client:
            def __init__(self):
                self.position_reads = 0

            def close_all_positions(self, cancel_orders):
                self.cancel_orders = cancel_orders
                return [
                    SimpleNamespace(order_id="close-1"),
                    SimpleNamespace(order_id=None),
                ]

            def get_all_positions(self):
                self.position_reads += 1
                return [position] if self.position_reads == 1 else []

            def get_orders(self, filter):
                self.order_filter = filter
                return []

            def get_order_by_id(self, _order_id, filter):
                raise AssertionError(
                    "malformed response set must fail before fill lookup"
                )

        broker = _broker(Client())

        self.assertFalse(broker.close_all_positions())
        self.assertEqual(broker.get_last_close_fills(), [])

    def test_close_all_rejects_duplicate_preclose_symbols(self):
        positions = [
            SimpleNamespace(
                symbol="JWEL",
                qty="647",
                avg_entry_price="3.55",
                current_price="3.50",
                unrealized_pl="-32.35",
                market_value="2264.50",
            ),
            SimpleNamespace(
                symbol="JWEL",
                qty="647",
                avg_entry_price="3.55",
                current_price="3.50",
                unrealized_pl="-32.35",
                market_value="2264.50",
            ),
        ]

        class Client:
            def __init__(self):
                self.position_reads = 0

            def close_all_positions(self, cancel_orders):
                self.cancel_orders = cancel_orders
                return [
                    SimpleNamespace(order_id="close-1"),
                    SimpleNamespace(order_id="close-2"),
                ]

            def get_all_positions(self):
                self.position_reads += 1
                return positions if self.position_reads == 1 else []

            def get_orders(self, filter):
                self.order_filter = filter
                return []

            def get_order_by_id(self, order_id, filter):
                del filter
                return _order(
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    filled_qty=647,
                    filled_avg_price=3.49,
                    symbol="JWEL",
                    side=SimpleNamespace(value="sell"),
                    qty=647,
                )

        broker = _broker(Client())

        self.assertFalse(broker.close_all_positions())
        self.assertEqual(broker.get_last_close_fills(), [])

    def test_close_all_rejects_close_response_after_empty_position_snapshot(self):
        class Client:
            def close_all_positions(self, cancel_orders):
                self.cancel_orders = cancel_orders
                return [SimpleNamespace(order_id="unexpected-close")]

            def get_all_positions(self):
                return []

            def get_orders(self, filter):
                self.order_filter = filter
                return []

            def get_order_by_id(self, _order_id, filter):
                raise AssertionError(
                    "an untrusted race fill must not be normalized as exact"
                )

        client = Client()
        broker = _broker(client)

        self.assertFalse(broker.close_all_positions())
        self.assertTrue(client.cancel_orders)
        self.assertEqual(broker.get_last_close_fills(), [])

    def test_close_all_rejects_wrong_qty_nan_price_or_wrong_fill_id(self):
        cases = (
            (999, 647, 3.49, "close-1"),
            (647.5, 647, 3.49, "close-1"),
            (647, 647.5, 3.49, "close-1"),
            (647, 647, float("nan"), "close-1"),
            (647, 647, 3.49, "different-order"),
        )
        for order_qty, filled_qty, fill_price, observed_id in cases:
            with self.subTest(
                order_qty=order_qty,
                fill_price=fill_price,
                observed_id=observed_id,
            ):
                position = SimpleNamespace(
                    symbol="JWEL",
                    qty="647",
                    avg_entry_price="3.55",
                    current_price="3.50",
                    unrealized_pl="-32.35",
                    market_value="2264.50",
                )

                class Client:
                    def __init__(self):
                        self.position_reads = 0

                    def close_all_positions(self, cancel_orders):
                        self.cancel_orders = cancel_orders
                        return [SimpleNamespace(order_id="close-1")]

                    def get_all_positions(self):
                        self.position_reads += 1
                        return [position] if self.position_reads == 1 else []

                    def get_orders(self, filter):
                        self.order_filter = filter
                        return []

                    def get_order_by_id(self, _order_id, filter):
                        self.close_order_filter = filter
                        return _order(
                            order_id=observed_id,
                            status=OrderStatus.FILLED,
                            filled_qty=filled_qty,
                            filled_avg_price=fill_price,
                            symbol="JWEL",
                            side=SimpleNamespace(value="sell"),
                            qty=order_qty,
                        )

                broker = _broker(Client())
                self.assertFalse(broker.close_all_positions())
                self.assertEqual(broker.get_last_close_fills(), [])

    def test_close_all_fails_when_broker_state_cannot_be_proven_empty(self):
        position = SimpleNamespace(
            symbol="JWEL",
            qty="647",
            avg_entry_price="3.55",
            current_price="3.50",
            unrealized_pl="-32.35",
            market_value="2264.50",
        )

        class Client:
            def close_all_positions(self, cancel_orders):
                self.cancel_orders = cancel_orders

            def get_all_positions(self):
                return [position]

            def get_orders(self, filter):
                self.order_filter = filter
                return [_order(order_id="orphan-stop")]

        client = Client()
        with patch(
            "trading_bot.execution.alpaca_broker._ORDER_CONFIRM_TIMEOUT_SECONDS",
            0.0,
        ):
            closed = _broker(client).close_all_positions()

        self.assertFalse(closed)
        self.assertTrue(client.cancel_orders)
        self.assertTrue(client.order_filter.nested)


if __name__ == "__main__":
    unittest.main()
