"""Regression tests for broker-confirmed portfolio state transitions."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


def _install_local_dependency_stubs() -> None:
    def missing(module_name: str) -> bool:
        try:
            return importlib.util.find_spec(module_name) is None
        except (ImportError, ModuleNotFoundError):
            return True

    minimal_workspace = importlib.util.find_spec("pydantic_settings") is None

    if importlib.util.find_spec("structlog") is None:
        structlog = types.ModuleType("structlog")

        class _Log:
            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

        structlog.get_logger = lambda *_args, **_kwargs: _Log()
        sys.modules["structlog"] = structlog

    stubs = {
        "trading_bot.config.settings": ("AppConfig", object),
        "trading_bot.data.market_data": ("MarketDataProvider", object),
        "trading_bot.risk.circuit_breaker": ("CircuitBreaker", object),
        "trading_bot.strategies.base": ("Strategy", object),
    }
    for module_name, (attribute, value) in stubs.items():
        if minimal_workspace or missing(module_name):
            parent_name = module_name.rsplit(".", 1)[0]
            if missing(parent_name):
                parent = types.ModuleType(parent_name)
                parent.__path__ = []
                sys.modules[parent_name] = parent
            module = types.ModuleType(module_name)
            setattr(module, attribute, value)
            sys.modules[module_name] = module

    if missing("trading_bot.utils.helpers"):
        helpers = types.ModuleType("trading_bot.utils.helpers")
        helpers.format_currency = lambda value: f"${value:,.2f}"
        helpers.now_et = lambda: datetime.now(timezone.utc)
        sys.modules["trading_bot.utils.helpers"] = helpers


_install_local_dependency_stubs()

from trading_bot.models.domain import (  # noqa: E402
    OrderSide,
    PositionInfo,
    PositionStatus,
    RiskCheckResult,
    SignalType,
    TradeSignal,
)
from trading_bot.portfolio.manager import PortfolioManager  # noqa: E402


def _manager(broker: object) -> PortfolioManager:
    manager = PortfolioManager.__new__(PortfolioManager)
    manager._broker = broker
    manager._config = types.SimpleNamespace(journal_csv_path="unused.csv")
    manager._circuit = None
    manager._market_data = None
    manager._positions = {}
    manager._daily_pnl = 0.0
    manager._journal_entries = []
    manager._log_to_journal = lambda _entry: None
    return manager


def _signal() -> TradeSignal:
    return TradeSignal(
        symbol="JWEL",
        signal_type=SignalType.VWAP_PULLBACK,
        entry_price=3.55,
        stop_price=3.49,
        target_prices=[3.67, 3.79],
        atr=0.10,
        vwap=3.50,
        ema9=3.52,
        confidence=0.9,
    )


def _risk(shares: int = 647) -> RiskCheckResult:
    return RiskCheckResult(
        approved=True,
        shares=shares,
        risk_dollars=38.82,
        reason="approved",
        leverage_used=0.0,
        positions_count=0,
    )


def _position(symbol: str = "BWMN", qty: int = 40) -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        side=OrderSide.BUY,
        entry_price=42.33,
        current_price=42.36,
        shares=qty,
        shares_remaining=qty,
        stop_price=42.31,
        target_prices=[42.37],
        status=PositionStatus.OPEN,
        pnl_unrealized=0.0,
        pnl_realized=0.0,
        scale_outs_completed=0,
        entry_time=datetime.now(timezone.utc),
        signal_type=SignalType.VWAP_PULLBACK,
        broker_order_ids=["entry-1"],
        broker_stop_order_id="stop-1",
        broker_tp_order_id="tp-1",
    )


class TestPortfolioEntryLifecycle(unittest.TestCase):
    def test_open_waits_for_nested_legs_and_uses_broker_fill_price(self):
        class Broker:
            def __init__(self):
                self.stop_submissions = 0

            def submit_bracket_order(self, **_kwargs):
                return {
                    "entry_order_id": "entry-1",
                    "stop_order_id": "",
                    "tp_order_id": "",
                }

            def get_order_status(self, _order_id):
                return {
                    "status": "filled",
                    "filled_qty": 647,
                    "filled_avg_price": 3.56,
                    "legs": [
                        {
                            "id": "stop-1",
                            "type": "stop",
                            "stop_price": 3.49,
                            "limit_price": 0,
                        },
                        {
                            "id": "tp-1",
                            "type": "limit",
                            "stop_price": 0,
                            "limit_price": 3.67,
                        },
                    ],
                }

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1
                return "standalone-stop"

        broker = Broker()
        position = _manager(broker).open_position(_signal(), _risk())

        self.assertEqual(position.status, PositionStatus.OPEN)
        self.assertEqual(position.entry_price, 3.56)
        self.assertEqual(position.shares, 647)
        self.assertEqual(position.broker_stop_order_id, "stop-1")
        self.assertEqual(position.broker_tp_order_id, "tp-1")
        self.assertEqual(broker.stop_submissions, 0)

    def test_partial_entry_is_canceled_and_flattened_not_tracked_open(self):
        class Broker:
            def __init__(self):
                self.qty = 200
                self.cancelled = []
                self.stop_submissions = 0

            def submit_bracket_order(self, **_kwargs):
                return {"entry_order_id": "entry-1"}

            def get_order_status(self, _order_id):
                return {
                    "status": "partially_filled",
                    "filled_qty": 200,
                    "filled_avg_price": 3.56,
                }

            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                return True

            def get_positions(self):
                return ([{"symbol": "JWEL", "qty": self.qty}] if self.qty else [])

            def close_position(self, _symbol):
                self.qty = 0
                return True

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1

        broker = Broker()
        with patch(
            "trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS", 0.0
        ):
            position = _manager(broker).open_position(_signal(), _risk())

        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.shares, 0)
        self.assertEqual(broker.qty, 0)
        self.assertEqual(broker.cancelled, ["entry-1"])
        self.assertEqual(broker.stop_submissions, 0)

    def test_missing_activated_legs_fails_closed_without_fallback_stop(self):
        class Broker:
            def __init__(self):
                self.qty = 647
                self.stop_submissions = 0

            def submit_bracket_order(self, **_kwargs):
                return {"entry_order_id": "entry-1"}

            def get_order_status(self, _order_id):
                return {
                    "status": "filled",
                    "filled_qty": 647,
                    "filled_avg_price": 3.56,
                    "legs": [],
                }

            def cancel_order(self, _order_id):
                return False

            def get_positions(self):
                return ([{"symbol": "JWEL", "qty": self.qty}] if self.qty else [])

            def close_position(self, _symbol):
                self.qty = 0
                return True

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1

        broker = Broker()
        with patch(
            "trading_bot.portfolio.manager._BRACKET_LEG_TIMEOUT_SECONDS", 0.0
        ):
            position = _manager(broker).open_position(_signal(), _risk())

        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(broker.qty, 0)
        self.assertEqual(broker.stop_submissions, 0)


class TestPortfolioExitLifecycle(unittest.TestCase):
    def test_internal_position_broker_flat_never_submits_sell(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0

            def get_order_status(self, _order_id):
                return {"status": "canceled", "filled_qty": 0}

            def get_positions(self):
                return []

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                raise AssertionError("must not sell an already-flat symbol")

        broker = Broker()
        position = _position()
        entry = _manager(broker)._close_position(position, 42.36, "test")

        self.assertIsNotNone(entry)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(broker.sell_calls, 0)

    def test_unconfirmed_bracket_cancel_blocks_manual_sell(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, _order_id):
                return {"status": "new", "filled_qty": 0}

            def cancel_order(self, _order_id):
                return False

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                return "sell-1"

        broker = Broker()
        position = _position()
        entry = _manager(broker)._close_position(position, 42.36, "test")

        self.assertIsNone(entry)
        self.assertEqual(position.status, PositionStatus.OPEN)
        self.assertEqual(broker.sell_calls, 0)

    def test_manual_close_waits_for_fill_and_uses_actual_price(self):
        class Broker:
            def __init__(self):
                self.sell_status_calls = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return {"status": "canceled", "filled_qty": 0}
                self.sell_status_calls += 1
                if self.sell_status_calls == 1:
                    return {"status": "accepted", "filled_qty": 0}
                return {
                    "status": "filled",
                    "filled_qty": 40,
                    "filled_avg_price": 42.31,
                }

            def cancel_order(self, _order_id):
                return True

            def submit_market_order(self, symbol, qty, side):
                self.submission = (symbol, qty, side)
                return "sell-1"

        broker = Broker()
        position = _position()
        with patch(
            "trading_bot.portfolio.manager.time.sleep", return_value=None
        ):
            entry = _manager(broker)._close_position(
                position, 42.36, "trailing_stop"
            )

        self.assertIsNotNone(entry)
        self.assertEqual(broker.submission, ("BWMN", 40, OrderSide.SELL))
        self.assertEqual(entry.exit_price, 42.31)
        self.assertEqual(entry.pnl, -0.8)
        self.assertEqual(position.status, PositionStatus.CLOSED)

    def test_unconfirmed_exit_cancel_does_not_add_competing_stop(self):
        class Broker:
            def __init__(self):
                self.stop_submissions = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return {"status": "canceled", "filled_qty": 0}
                return {"status": "accepted", "filled_qty": 0}

            def cancel_order(self, order_id):
                return order_id in {"stop-1", "tp-1"}

            def submit_market_order(self, *_args, **_kwargs):
                return "sell-1"

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1
                return "replacement-stop"

        broker = Broker()
        position = _position()
        with patch(
            "trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS", 0.0
        ):
            entry = _manager(broker)._close_position(position, 42.36, "test")

        self.assertIsNone(entry)
        self.assertEqual(position.status, PositionStatus.OPEN)
        self.assertEqual(broker.stop_submissions, 0)

    def test_cancel_losing_race_to_full_fill_finalizes_close(self):
        class Broker:
            def __init__(self):
                self.sell_status_calls = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return {"status": "canceled", "filled_qty": 0}
                self.sell_status_calls += 1
                if self.sell_status_calls == 1:
                    return {"status": "accepted", "filled_qty": 0}
                return {
                    "status": "filled",
                    "filled_qty": 40,
                    "filled_avg_price": 42.30,
                }

            def cancel_order(self, order_id):
                return order_id in {"stop-1", "tp-1"}

            def submit_market_order(self, *_args, **_kwargs):
                return "sell-1"

        position = _position()
        broker = Broker()
        with patch(
            "trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS", 0.0
        ):
            entry = _manager(broker)._close_position(
                position, 42.36, "trailing_stop"
            )

        self.assertIsNotNone(entry)
        self.assertEqual(entry.exit_price, 42.30)
        self.assertEqual(position.status, PositionStatus.CLOSED)

    def test_scale_out_cancels_bracket_before_sell_and_restores_one_stop(self):
        class Broker:
            def __init__(self):
                self.qty = 40
                self.events = []
                self.stop_submissions = 0

            def get_positions(self):
                return ([{"symbol": "BWMN", "qty": self.qty}] if self.qty else [])

            def get_order_status(self, order_id):
                if order_id == "scale-sell":
                    return {
                        "status": "filled",
                        "filled_qty": 20,
                        "filled_avg_price": 42.40,
                    }
                return {"status": "canceled", "filled_qty": 0}

            def cancel_order(self, order_id):
                self.events.append(("cancel", order_id))
                return True

            def submit_market_order(self, symbol, qty, side):
                self.events.append(("sell", symbol, qty, side))
                self.qty -= qty
                return "scale-sell"

            def submit_stop_order(self, symbol, qty, stop_price):
                self.events.append(("stop", symbol, qty, stop_price))
                self.stop_submissions += 1
                return "restored-stop"

        broker = Broker()
        position = _position()
        _manager(broker)._execute_scale_out(position, 20, 42.39, "target_1")

        self.assertEqual(
            broker.events[:3],
            [
                ("cancel", "stop-1"),
                ("cancel", "tp-1"),
                ("sell", "BWMN", 20, OrderSide.SELL),
            ],
        )
        self.assertEqual(broker.stop_submissions, 1)
        self.assertEqual(position.broker_stop_order_id, "restored-stop")
        self.assertIsNone(position.broker_tp_order_id)
        self.assertEqual(position.shares_remaining, 20)

    def test_scale_out_is_blocked_if_bracket_cancel_is_unconfirmed(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, _order_id):
                return {"status": "new", "filled_qty": 0}

            def cancel_order(self, _order_id):
                return False

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                return "scale-sell"

        broker = Broker()
        position = _position()
        _manager(broker)._execute_scale_out(position, 20, 42.39, "target_1")

        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(position.shares_remaining, 40)


if __name__ == "__main__":
    unittest.main()
