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
        except (ImportError, ModuleNotFoundError, ValueError):
            return True

    minimal_workspace = missing("pydantic_settings")

    if missing("structlog"):
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

from trading_bot.execution.paper_broker import PaperBroker  # noqa: E402
from trading_bot.models.domain import (  # noqa: E402
    OrderSide,
    PositionInfo,
    PositionStatus,
    RiskCheckResult,
    SignalType,
    TradeSignal,
)
from trading_bot.portfolio.manager import (  # noqa: E402
    PortfolioManager,
    PortfolioSafetyError,
)


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


def _exit_leg_snapshot(
    order_id: str, status: str = "canceled", qty: int = 40
) -> dict:
    is_stop = order_id.startswith("stop")
    return {
        "id": order_id,
        "symbol": "BWMN",
        "side": "sell",
        "qty": qty,
        "type": "stop" if is_stop else "limit",
        "status": status,
        "filled_qty": 0,
        "filled_avg_price": 0.0,
    }


class TestPortfolioEntryLifecycle(unittest.TestCase):
    def test_wait_for_order_recovers_after_transient_status_error(self):
        class Broker:
            def __init__(self):
                self.calls = 0

            def get_order_status(self, order_id):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary status lookup timeout")
                return {
                    "id": order_id,
                    "status": "filled",
                    "filled_qty": 647,
                    "filled_avg_price": 3.56,
                }

        broker = Broker()
        with (
            patch(
                "trading_bot.portfolio.manager.time.monotonic",
                side_effect=(0.0, 0.0, 0.5),
            ),
            patch("trading_bot.portfolio.manager.time.sleep", return_value=None),
        ):
            status = _manager(broker)._wait_for_order(
                "entry-1", timeout_seconds=1.0
            )

        self.assertEqual(status["status"], "filled")
        self.assertEqual(status["filled_qty"], 647)
        self.assertEqual(broker.calls, 2)

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
                    "id": "entry-1",
                    "symbol": "JWEL",
                    "side": "buy",
                    "qty": "647.0",
                    "type": "market",
                    "status": "filled",
                    "filled_qty": "647.0",
                    "filled_avg_price": 3.56,
                    "legs": [
                        {
                            "id": "stop-1",
                            "symbol": "JWEL",
                            "status": "new",
                            "side": "sell",
                            "qty": "647.0",
                            "type": "stop",
                            "stop_price": 3.49,
                            "limit_price": 0,
                        },
                        {
                            "id": "tp-1",
                            "symbol": "JWEL",
                            "status": "new",
                            "side": "sell",
                            "qty": "647.0",
                            "type": "limit",
                            "stop_price": 0,
                            "limit_price": 3.67,
                        },
                    ],
                }

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1
                return "standalone-stop"

            def get_positions(self):
                return [{"symbol": "JWEL", "qty": "647.0"}]

        broker = Broker()
        position = _manager(broker).open_position(_signal(), _risk())

        self.assertEqual(position.status, PositionStatus.OPEN)
        self.assertEqual(position.entry_price, 3.56)
        self.assertEqual(position.shares, 647)
        self.assertEqual(position.broker_stop_order_id, "stop-1")
        self.assertEqual(position.broker_tp_order_id, "tp-1")
        self.assertEqual(broker.stop_submissions, 0)

    def test_entry_requires_exact_fresh_broker_position(self):
        cases = (
            0,
            646,
            647.5,
            True,
            float("nan"),
            float("inf"),
            -5,
            RuntimeError("position API unavailable"),
        )
        for broker_result in cases:
            with self.subTest(broker_result=broker_result):
                class Broker:
                    def __init__(self):
                        self.qty = broker_result
                        self.close_all_calls = 0

                    def submit_bracket_order(self, **_kwargs):
                        return {"entry_order_id": "entry-1"}

                    def get_order_status(self, order_id):
                        return {
                            "id": order_id,
                            "symbol": "JWEL",
                            "side": "buy",
                            "qty": 647,
                            "type": "market",
                            "status": "filled",
                            "filled_qty": 647,
                            "filled_avg_price": 3.56,
                            "legs": [
                                {
                                    "id": "stop-1",
                                    "symbol": "JWEL",
                                    "status": "new",
                                    "side": "sell",
                                    "qty": 647,
                                    "type": "stop",
                                    "stop_price": 3.49,
                                    "limit_price": 0.0,
                                },
                                {
                                    "id": "tp-1",
                                    "symbol": "JWEL",
                                    "status": "new",
                                    "side": "sell",
                                    "qty": 647,
                                    "type": "limit",
                                    "stop_price": 0.0,
                                    "limit_price": 3.67,
                                },
                            ],
                        }

                    def get_positions(self):
                        if isinstance(self.qty, Exception):
                            raise self.qty
                        return (
                            [{"symbol": "JWEL", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                    def cancel_order(self, _order_id):
                        return True

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                broker = Broker()
                manager = _manager(broker)
                with self.assertRaisesRegex(
                    PortfolioSafetyError,
                    "entry_position_unconfirmed:JWEL",
                ):
                    manager.open_position(_signal(), _risk())

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(manager.get_open_positions(), [])
                self.assertEqual(manager.get_daily_pnl(), 0.0)

    def test_entry_rejects_non_integral_or_nonfinite_order_quantities(self):
        exact_snapshot = {
            "id": "entry-1",
            "symbol": "JWEL",
            "side": "buy",
            "qty": "647.0",
            "status": "filled",
            "filled_qty": "647.0",
            "filled_avg_price": 3.56,
        }
        manager = _manager(object())
        self.assertEqual(
            manager._fill_validation_error(
                exact_snapshot,
                order_id="entry-1",
                symbol="JWEL",
                side="buy",
                qty=647,
                allowed_statuses={"filled"},
            ),
            "",
        )

        for field in ("qty", "filled_qty"):
            for bad_value in (647.5, True, float("nan"), float("inf")):
                with self.subTest(field=field, bad_value=bad_value):
                    snapshot = {**exact_snapshot, field: bad_value}
                    self.assertTrue(
                        manager._fill_validation_error(
                            snapshot,
                            order_id="entry-1",
                            symbol="JWEL",
                            side="buy",
                            qty=647,
                            allowed_statuses={"filled"},
                        )
                    )

        class Broker:
            def __init__(self):
                self.submissions = 0

            def submit_bracket_order(self, **_kwargs):
                self.submissions += 1
                raise AssertionError("invalid quantity reached the broker")

        for bad_shares in (647.5, True, float("nan"), float("inf"), 0, -1):
            with self.subTest(requested_shares=bad_shares):
                broker = Broker()
                with self.assertRaisesRegex(
                    PortfolioSafetyError, "entry_quantity_invalid:JWEL"
                ):
                    _manager(broker).open_position(
                        _signal(), _risk(shares=bad_shares)
                    )
                self.assertEqual(broker.submissions, 0)

    def test_entry_rejects_missing_or_nonfinite_fill_price(self):
        for invalid_price in (None, 0.0, float("nan"), float("inf")):
            with self.subTest(filled_avg_price=invalid_price):
                class Broker:
                    def __init__(self):
                        self.qty = 647
                        self.close_all_calls = 0

                    def submit_bracket_order(self, **_kwargs):
                        return {"entry_order_id": "entry-1"}

                    def get_order_status(self, order_id):
                        return {
                            "id": order_id,
                            "symbol": "JWEL",
                            "side": "buy",
                            "qty": 647,
                            "type": "market",
                            "status": "filled",
                            "filled_qty": 647,
                            "filled_avg_price": invalid_price,
                        }

                    def cancel_order(self, _order_id):
                        return True

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "JWEL", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                manager = _manager(broker)
                with self.assertRaisesRegex(
                    PortfolioSafetyError, "entry_fill_unconfirmed:JWEL"
                ):
                    manager.open_position(_signal(), _risk())

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(manager.get_open_positions(), [])
                self.assertEqual(manager.get_daily_pnl(), 0.0)

    def test_entry_rejects_wrong_child_symbol_or_effective_price(self):
        cases = (
            (0, "symbol", "OTHER"),
            (0, "stop_price", 3.48),
            (1, "limit_price", 3.68),
        )
        for leg_index, field, bad_value in cases:
            with self.subTest(field=field, bad_value=bad_value):
                class Broker:
                    def __init__(self):
                        self.qty = 647
                        self.close_all_calls = 0

                    def submit_bracket_order(self, **_kwargs):
                        return {"entry_order_id": "entry-1"}

                    def get_order_status(self, order_id):
                        legs = [
                            {
                                "id": "stop-1",
                                "symbol": "JWEL",
                                "status": "new",
                                "side": "sell",
                                "qty": 647,
                                "type": "stop",
                                "stop_price": 3.49,
                                "limit_price": 0.0,
                            },
                            {
                                "id": "tp-1",
                                "symbol": "JWEL",
                                "status": "new",
                                "side": "sell",
                                "qty": 647,
                                "type": "limit",
                                "stop_price": 0.0,
                                "limit_price": 3.67,
                            },
                        ]
                        legs[leg_index][field] = bad_value
                        return {
                            "id": order_id,
                            "symbol": "JWEL",
                            "side": "buy",
                            "qty": 647,
                            "type": "market",
                            "status": "filled",
                            "filled_qty": 647,
                            "filled_avg_price": 3.56,
                            "legs": legs,
                        }

                    def cancel_order(self, _order_id):
                        return True

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "JWEL", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                manager = _manager(broker)
                with (
                    patch(
                        "trading_bot.portfolio.manager._BRACKET_LEG_TIMEOUT_SECONDS",
                        0.0,
                    ),
                    self.assertRaisesRegex(
                        PortfolioSafetyError,
                        "entry_protection_unconfirmed:JWEL",
                    ),
                ):
                    manager.open_position(_signal(), _risk())

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(manager.get_open_positions(), [])
                self.assertEqual(manager.get_daily_pnl(), 0.0)

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

            def close_all_positions(self):
                self.qty = 0
                return True

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1

        broker = Broker()
        with (
            patch(
                "trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS",
                0.0,
            ),
            self.assertRaisesRegex(
                PortfolioSafetyError, "entry_fill_unconfirmed:JWEL"
            ),
        ):
            _manager(broker).open_position(_signal(), _risk())

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
                    "id": "entry-1",
                    "symbol": "JWEL",
                    "side": "buy",
                    "qty": 647,
                    "type": "market",
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

            def close_all_positions(self):
                self.qty = 0
                return True

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1

        broker = Broker()
        with (
            patch(
                "trading_bot.portfolio.manager._BRACKET_LEG_TIMEOUT_SECONDS",
                0.0,
            ),
            self.assertRaisesRegex(
                PortfolioSafetyError, "entry_protection_unconfirmed:JWEL"
            ),
        ):
            _manager(broker).open_position(_signal(), _risk())

        self.assertEqual(broker.qty, 0)
        self.assertEqual(broker.stop_submissions, 0)

    def test_ambiguous_submission_flattens_and_halts(self):
        class Broker:
            def __init__(self):
                self.qty = 0
                self.close_all_calls = 0

            def submit_bracket_order(self, **_kwargs):
                raise TimeoutError("submission response lost")

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

            def get_positions(self):
                return []

        broker = Broker()
        with self.assertRaisesRegex(
            PortfolioSafetyError, "entry_submission_ambiguous:JWEL"
        ):
            _manager(broker).open_position(_signal(), _risk())

        self.assertEqual(broker.close_all_calls, 1)

    def test_immediate_leg_ids_cannot_bypass_nested_active_pair_check(self):
        class Broker:
            def __init__(self):
                self.status_calls = 0

            def get_order_status(self, _order_id):
                self.status_calls += 1
                return {
                    "status": "filled",
                    "legs": [
                        {
                            "id": "stop-1",
                            "status": "held",
                            "side": "sell",
                            "qty": 647,
                            "type": "stop",
                            "stop_price": 3.49,
                        },
                        {
                            "id": "tp-1",
                            "status": "canceled",
                            "side": "sell",
                            "qty": 647,
                            "type": "limit",
                            "limit_price": 3.67,
                        },
                    ],
                }

        broker = Broker()
        with patch(
            "trading_bot.portfolio.manager._BRACKET_LEG_TIMEOUT_SECONDS", 0.0
        ):
            ids = _manager(broker)._wait_for_bracket_legs(
                "entry-1",
                expected_qty=647,
                initial_stop_id="stop-1",
                initial_tp_id="tp-1",
            )

        self.assertEqual(ids, ("", ""))
        self.assertEqual(broker.status_calls, 1)

    def test_active_children_are_rejected_when_parent_is_not_filled(self):
        class Broker:
            def get_order_status(self, _order_id):
                return {
                    "status": "error",
                    "legs": [
                        {
                            "id": "stop-1",
                            "status": "new",
                            "side": "sell",
                            "qty": 647,
                            "type": "stop",
                            "stop_price": 3.49,
                        },
                        {
                            "id": "tp-1",
                            "status": "new",
                            "side": "sell",
                            "qty": 647,
                            "type": "limit",
                            "limit_price": 3.67,
                        },
                    ],
                }

        with patch(
            "trading_bot.portfolio.manager._BRACKET_LEG_TIMEOUT_SECONDS", 0.0
        ):
            ids = _manager(Broker())._wait_for_bracket_legs(
                "entry-1", expected_qty=647
            )

        self.assertEqual(ids, ("", ""))

    def test_bracket_pair_accepts_new_or_held_stop_with_new_target(self):
        base_stop = {
            "id": "stop-1",
            "status": "new",
            "side": "sell",
            "qty": 647,
            "type": "stop",
            "stop_price": 3.49,
        }
        base_tp = {
            "id": "tp-1",
            "status": "new",
            "side": "sell",
            "qty": 647,
            "type": "limit",
            "limit_price": 3.67,
        }
        manager = _manager(object())

        self.assertEqual(
            manager._leg_ids({"legs": [base_stop, base_tp]}, 647),
            ("stop-1", "tp-1"),
        )
        self.assertEqual(
            manager._leg_ids(
                {"legs": [{**base_stop, "status": "held"}, base_tp]}, 647
            ),
            ("stop-1", "tp-1"),
        )
        self.assertEqual(
            manager._leg_ids(
                {
                    "legs": [
                        {**base_stop, "status": "held"},
                        {**base_tp, "status": "held"},
                    ]
                },
                647,
            ),
            ("", ""),
        )
        for rejected_status in (
            "accepted",
            "partially_filled",
            "pending_new",
            "canceled",
            "rejected",
            "expired",
            "filled",
        ):
            with self.subTest(status=rejected_status):
                rejected_stop = {**base_stop, "status": rejected_status}
                self.assertEqual(
                    manager._leg_ids({"legs": [rejected_stop, base_tp]}, 647),
                    ("", ""),
                )
        self.assertEqual(
            manager._leg_ids(
                {"legs": [{**base_stop, "qty": 646}, base_tp]}, 647
            ),
            ("", ""),
        )
        for invalid_qty in (647.5, True, float("nan"), float("inf")):
            with self.subTest(child_qty=invalid_qty):
                self.assertEqual(
                    manager._leg_ids(
                        {
                            "legs": [
                                {**base_stop, "qty": invalid_qty},
                                base_tp,
                            ]
                        },
                        647,
                    ),
                    ("", ""),
                )
        self.assertEqual(
            manager._leg_ids(
                {"legs": [base_stop, {**base_stop, "id": "stop-2"}, base_tp]},
                647,
            ),
            ("", ""),
        )

    def test_bracket_legs_from_different_snapshots_are_not_combined(self):
        stop = {
            "id": "stop-1",
            "status": "new",
            "side": "sell",
            "qty": 647,
            "type": "stop",
            "stop_price": 3.49,
        }
        target = {
            "id": "tp-1",
            "status": "new",
            "side": "sell",
            "qty": 647,
            "type": "limit",
            "limit_price": 3.67,
        }

        class Broker:
            def __init__(self):
                self.snapshots = iter(({"legs": [stop]}, {"legs": [target]}))
                self.calls = 0

            def get_order_status(self, _order_id):
                self.calls += 1
                return {"status": "filled", **next(self.snapshots)}

        broker = Broker()
        with (
            patch("trading_bot.portfolio.manager._BRACKET_LEG_TIMEOUT_SECONDS", 1.0),
            patch(
                "trading_bot.portfolio.manager.time.monotonic",
                side_effect=(0.0, 0.0, 1.0),
            ),
            patch("trading_bot.portfolio.manager.time.sleep", return_value=None),
        ):
            ids = _manager(broker)._wait_for_bracket_legs(
                "entry-1", expected_qty=647
            )

        self.assertEqual(ids, ("", ""))
        self.assertEqual(broker.calls, 2)


class TestPortfolioReconciliation(unittest.TestCase):
    def test_fresh_process_closes_all_and_never_adopts_signed_positions(self):
        for qty in (40, -12):
            with self.subTest(qty=qty):
                class Broker:
                    def __init__(self):
                        self.positions = [{"symbol": "BWMN", "qty": qty}]
                        self.close_all_calls = 0

                    def get_positions(self):
                        return list(self.positions)

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.positions = []
                        return True

                broker = Broker()
                manager = _manager(broker)
                manager.reconcile_positions()

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(manager.get_open_positions(), [])
                self.assertEqual(broker.get_positions(), [])

    def test_fresh_process_close_all_runs_even_when_positions_snapshot_is_empty(self):
        class Broker:
            def __init__(self):
                self.close_all_calls = 0

            def get_positions(self):
                return []

            def close_all_positions(self):
                self.close_all_calls += 1
                return True

        broker = Broker()
        _manager(broker).reconcile_positions()

        self.assertEqual(broker.close_all_calls, 1)

    def test_unverified_close_all_raises_hard_safety_error(self):
        class Broker:
            def get_positions(self):
                return [{"symbol": "BWMN", "qty": -12}]

            def close_all_positions(self):
                return True

        manager = _manager(Broker())

        with (
            patch("trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS", 0.0),
            self.assertRaises(PortfolioSafetyError),
        ):
            manager.reconcile_positions()

        self.assertEqual(manager.get_open_positions(), [])

    def test_missing_tracked_position_globally_cancels_and_halts(self):
        class Broker:
            def __init__(self):
                self.close_all_calls = 0

            def get_positions(self):
                return []

            def close_all_positions(self):
                self.close_all_calls += 1
                return True

        broker = Broker()
        position = _position()
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "reconcile_state_divergence"
        ):
            manager.reconcile_positions()

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.shares_remaining, 0)
        self.assertEqual(manager.get_open_positions(), [])

    def test_quantity_mismatch_globally_flattens_instead_of_adopting(self):
        class Broker:
            def __init__(self):
                self.qty = 35
                self.close_all_calls = 0

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

        broker = Broker()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "reconcile_state_divergence"
        ):
            manager.reconcile_positions()

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_open_positions(), [])

    def test_exact_reconciled_position_reproves_stop(self):
        class Broker:
            def __init__(self):
                self.close_all_calls = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, order_id):
                self.asserted_order_id = order_id
                return {
                    "id": "stop-1",
                    "status": "new",
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "stop",
                    "stop_price": 42.31,
                }

            def close_all_positions(self):
                self.close_all_calls += 1
                return True

        broker = Broker()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        manager.reconcile_positions()

        self.assertEqual(broker.asserted_order_id, "stop-1")
        self.assertEqual(broker.close_all_calls, 0)
        self.assertEqual(manager.get_open_positions(), [position])

    def test_verified_broker_flat_bookkeeping_journals_without_new_order(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("BWMN", 42.33)
        bracket = broker.submit_bracket_order(
            "BWMN",
            40,
            OrderSide.BUY,
            stop_price=42.31,
            take_profit_price=42.37,
        )
        position = _position(qty=40)
        position.broker_order_ids = [bracket["entry_order_id"]]
        position.broker_stop_order_id = bracket["stop_order_id"]
        position.broker_tp_order_id = bracket["tp_order_id"]
        entry_fill = broker.get_order_status(bracket["entry_order_id"])
        position.entry_price = entry_fill["filled_avg_price"]
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        self.assertTrue(broker.close_all_positions())
        self.assertEqual(broker.get_positions(), [])
        close_fills = broker.get_last_close_fills()
        self.assertEqual(len(close_fills), 1)
        actual_exit = close_fills[0]["filled_avg_price"]

        def unexpected_order(*_args, **_kwargs):
            raise AssertionError("bookkeeping must never submit an order")

        broker.submit_market_order = unexpected_order

        entries = manager.finalize_verified_broker_flat("shutdown")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].exit_reason, "shutdown")
        self.assertEqual(entries[0].exit_price, actual_exit)
        expected_pnl = 40 * (actual_exit - position.entry_price)
        self.assertEqual(entries[0].pnl, round(expected_pnl, 2))
        self.assertAlmostEqual(manager.get_daily_pnl(), expected_pnl)
        self.assertEqual(manager.get_open_positions(), [])
        self.assertEqual(manager.get_daily_journal_entries(), entries)
        self.assertEqual(broker._pending_orders, {})

    def test_verified_flat_bookkeeping_refuses_nonflat_broker(self):
        class Broker:
            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_last_close_fills(self):
                return []

        position = _position(qty=40)
        manager = _manager(Broker())
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "bookkeeping requires broker-flat state"
        ):
            manager.finalize_verified_broker_flat("shutdown")

        self.assertEqual(position.status, PositionStatus.OPEN)
        self.assertEqual(manager.get_open_positions(), [position])

    def test_verified_flat_bookkeeping_refuses_missing_fill(self):
        class Broker:
            def get_positions(self):
                return []

            def get_last_close_fills(self):
                return []

        position = _position(qty=40)
        manager = _manager(Broker())
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "expected one exact close fill"
        ):
            manager.finalize_verified_broker_flat("shutdown")

        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])
        self.assertEqual(position.status, PositionStatus.OPEN)

    def test_verified_flat_bookkeeping_rejects_nan_fill_without_mutation(self):
        class Broker:
            def get_positions(self):
                return []

            def get_last_close_fills(self):
                return [
                    {
                        "id": "close-1",
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "status": "filled",
                        "filled_qty": 40,
                        "filled_avg_price": float("nan"),
                    }
                ]

        position = _position(qty=40)
        manager = _manager(Broker())
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "unverified close fill"
        ):
            manager.finalize_verified_broker_flat("shutdown")

        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])
        self.assertEqual(position.status, PositionStatus.OPEN)

    def test_verified_flat_bookkeeping_rejects_duplicate_fill_ids_globally(self):
        class Broker:
            def get_positions(self):
                return []

            def get_last_close_fills(self):
                return [
                    {
                        "id": "reused-fill-id",
                        "symbol": symbol,
                        "side": "sell",
                        "qty": "40.0",
                        "status": "filled",
                        "filled_qty": "40.0",
                        "filled_avg_price": 42.30,
                    }
                    for symbol in ("BWMN", "JWEL")
                ]

        first = _position("BWMN", 40)
        second = _position("JWEL", 40)
        manager = _manager(Broker())
        manager._positions = {first.symbol: first, second.symbol: second}

        with self.assertRaisesRegex(
            PortfolioSafetyError, "duplicate close fill id"
        ):
            manager.finalize_verified_broker_flat("shutdown")

        self.assertEqual(manager.get_open_positions(), [first, second])
        self.assertEqual(first.status, PositionStatus.OPEN)
        self.assertEqual(second.status, PositionStatus.OPEN)
        self.assertEqual(manager.get_daily_pnl(), 0.0)


class TestPortfolioExitLifecycle(unittest.TestCase):
    def test_final_journal_uses_weighted_exit_after_partial_tranche(self):
        position = _position(qty=40)
        position.shares_remaining = 30
        position.status = PositionStatus.PARTIALLY_CLOSED
        position.pnl_realized = 10 * (41.00 - position.entry_price)
        manager = _manager(object())
        manager._daily_pnl = position.pnl_realized

        entry = manager._finalize_close(
            position,
            actual_exit_price=43.00,
            actual_shares=30,
            reason="manual_close",
        )

        self.assertAlmostEqual(entry.pnl, 6.80)
        self.assertAlmostEqual(entry.exit_price, 42.50)
        self.assertAlmostEqual(entry.rr_ratio, 8.50)
        self.assertAlmostEqual(
            entry.shares * (entry.exit_price - entry.entry_price),
            entry.pnl,
        )
        self.assertAlmostEqual(manager.get_daily_pnl(), 6.80)
        self.assertEqual(position.status, PositionStatus.CLOSED)

    def test_partial_protective_fill_flattens_before_accounting_mutation(self):
        class Broker:
            def __init__(self):
                self.qty = 30
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                if order_id == "stop-1":
                    return {
                        "id": order_id,
                        "status": "partially_filled",
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "type": "stop",
                        "filled_qty": 10,
                        "filled_avg_price": 42.31,
                    }
                return _exit_leg_snapshot(order_id, status="new")

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

        broker = Broker()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError,
            "bracket_partial_fill_untrackable:BWMN",
        ):
            manager._check_bracket_fills(position)

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.pnl_realized, 0.0)
        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])

    def test_bracket_overfill_globally_flattens_and_halts(self):
        class Broker:
            def __init__(self):
                self.qty = -10
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                if order_id == "stop-1":
                    return {
                        "id": order_id,
                        "status": "filled",
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "type": "stop",
                        "filled_qty": 50,
                        "filled_avg_price": 42.31,
                    }
                return {
                    "id": order_id,
                    "status": "cancelled_oco",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

        position = _position(qty=40)
        broker = Broker()
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "bracket_fill_invalid:BWMN"
        ):
            manager._check_bracket_fills(position)

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.shares_remaining, 0)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.pnl_realized, 0.0)
        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])

    def test_exact_full_bracket_fill_requires_broker_to_be_flat(self):
        class Broker:
            def __init__(self):
                self.qty = 10
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                if order_id == "stop-1":
                    return {
                        "id": order_id,
                        "status": "filled",
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "type": "stop",
                        "filled_qty": 40,
                        "filled_avg_price": 42.31,
                    }
                return {
                    "id": order_id,
                    "status": "cancelled_oco",
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "limit",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

        position = _position(qty=40)
        broker = Broker()
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "bracket_fill_broker_qty_divergence:BWMN"
        ):
            manager._check_bracket_fills(position)

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])

    def test_full_bracket_fill_requires_inactive_oco_sibling(self):
        class Broker:
            def __init__(self):
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                if order_id == "stop-1":
                    return {
                        "id": order_id,
                        "status": "filled",
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "type": "stop",
                        "filled_qty": 40,
                        "filled_avg_price": 42.31,
                    }
                return {
                    "id": order_id,
                    "status": "new",
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "limit",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }

            def get_positions(self):
                return []

            def cancel_order(self, _order_id):
                return False

            def close_all_positions(self):
                self.close_all_calls += 1
                return True

        position = _position(qty=40)
        broker = Broker()
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError,
            "bracket_sibling_cancel_unconfirmed:BWMN",
        ):
            manager._check_bracket_fills(position)

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])

    def test_unexplained_broker_flat_cancels_globally_and_halts(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                return _exit_leg_snapshot(order_id)

            def get_positions(self):
                return []

            def close_all_positions(self):
                self.close_all_calls += 1
                return True

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                raise AssertionError("must not sell an already-flat symbol")

        broker = Broker()
        position = _position()
        manager = _manager(broker)
        manager._positions[position.symbol] = position
        with self.assertRaisesRegex(
            PortfolioSafetyError, "close_qty_divergence:BWMN"
        ):
            manager._close_position(position, 42.36, "test")

        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(broker.close_all_calls, 1)

    def test_excess_broker_quantity_flattens_globally_and_halts(self):
        class Broker:
            def __init__(self):
                self.qty = 45
                self.close_all_calls = 0
                self.sell_calls = 0

            def get_order_status(self, order_id):
                return _exit_leg_snapshot(order_id)

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                raise AssertionError("divergent quantity requires global flatten")

        broker = Broker()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "close_qty_divergence:BWMN"
        ):
            manager._close_position(position, 42.36, "test")

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.shares_remaining, 0)
        self.assertEqual(manager.get_open_positions(), [])

    def test_smaller_or_signed_broker_quantity_flattens_and_halts(self):
        for broker_qty in (35, -3):
            with self.subTest(broker_qty=broker_qty):
                class Broker:
                    def __init__(self):
                        self.qty = broker_qty
                        self.close_all_calls = 0
                        self.sell_calls = 0

                    def get_order_status(self, order_id):
                        return _exit_leg_snapshot(order_id)

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def submit_market_order(self, *_args, **_kwargs):
                        self.sell_calls += 1
                        raise AssertionError(
                            "divergent quantity requires global flatten"
                        )

                broker = Broker()
                position = _position(qty=40)
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaisesRegex(
                    PortfolioSafetyError, "close_qty_divergence:BWMN"
                ):
                    manager._close_position(position, 42.36, "test")

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(broker.sell_calls, 0)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(manager.get_open_positions(), [])

    def test_post_cancel_quantity_growth_or_short_flattens_and_halts(self):
        for post_cancel_qty in (45, -2):
            with self.subTest(post_cancel_qty=post_cancel_qty):
                class Broker:
                    def __init__(self):
                        self.snapshots = iter((40, post_cancel_qty))
                        self.qty = post_cancel_qty
                        self.close_all_calls = 0
                        self.sell_calls = 0

                    def get_positions(self):
                        try:
                            self.qty = next(self.snapshots)
                        except StopIteration:
                            pass
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                    def get_order_status(self, order_id):
                        return _exit_leg_snapshot(order_id)

                    def cancel_order(self, _order_id):
                        return True

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def submit_market_order(self, *_args, **_kwargs):
                        self.sell_calls += 1
                        raise AssertionError(
                            "post-cancel divergence must not submit a sell"
                        )

                broker = Broker()
                position = _position(qty=40)
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaisesRegex(
                    PortfolioSafetyError,
                    "close_post_cancel_qty_divergence:BWMN",
                ):
                    manager._close_position(position, 42.36, "test")

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(broker.sell_calls, 0)
                self.assertEqual(position.status, PositionStatus.CLOSED)

    def test_unconfirmed_bracket_cancel_blocks_manual_sell(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, order_id):
                return _exit_leg_snapshot(order_id, status="new")

            def cancel_order(self, _order_id):
                return False

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                return "sell-1"

        broker = Broker()
        position = _position()
        manager = _manager(broker)
        manager._positions[position.symbol] = position
        broker.close_all_positions = lambda: setattr(
            broker, "positions_cleared", True
        ) or True
        original_get_positions = broker.get_positions
        broker.get_positions = lambda: (
            []
            if getattr(broker, "positions_cleared", False)
            else original_get_positions()
        )
        with self.assertRaisesRegex(
            PortfolioSafetyError, "close_exit_cancel_unconfirmed:BWMN"
        ):
            manager._close_position(position, 42.36, "test")

        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(broker.sell_calls, 0)

    def test_cancel_confirmation_requires_exact_tracked_leg_identity(self):
        class Broker:
            def __init__(self):
                self.qty = 40
                self.sell_calls = 0
                self.close_all_calls = 0

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def cancel_order(self, _order_id):
                return True

            def get_order_status(self, _order_id):
                return {
                    "id": "different-order",
                    "symbol": "OTHER",
                    "side": "buy",
                    "qty": 1,
                    "type": "market",
                    "status": "canceled",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                raise AssertionError("unverified exits must block a sell")

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

        broker = Broker()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "close_exit_cancel_unconfirmed:BWMN"
        ):
            manager._close_position(position, 42.36, "test")

        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_daily_pnl(), 0.0)

    def test_manual_close_waits_for_fill_and_uses_actual_price(self):
        class Broker:
            def __init__(self):
                self.sell_status_calls = 0

            def get_positions(self):
                return (
                    []
                    if self.sell_status_calls >= 2
                    else [{"symbol": "BWMN", "qty": 40}]
                )

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return _exit_leg_snapshot(order_id)
                self.sell_status_calls += 1
                if self.sell_status_calls == 1:
                    return {
                        "id": order_id,
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "type": "market",
                        "status": "accepted",
                        "filled_qty": 0,
                        "filled_avg_price": 0.0,
                    }
                return {
                    "id": order_id,
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "market",
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

    def test_manual_partial_fill_is_immediately_circuit_visible_and_not_doubled(self):
        class Circuit:
            def __init__(self):
                self.daily_pnl = 0.0
                self.partial_calls = []
                self.final_calls = []
                self.unrealized_pnl = 0.0

            def record_partial_realized_pnl(self, pnl, *, defer_check=False):
                self.partial_calls.append((pnl, defer_check))
                self.daily_pnl += pnl

            def update_unrealized_pnl(self, pnl):
                self.unrealized_pnl = pnl

            def record_trade_result(
                self,
                pnl,
                realized_already_recorded=0.0,
                *,
                defer_check=False,
            ):
                self.final_calls.append(
                    (pnl, realized_already_recorded, defer_check)
                )
                self.daily_pnl += pnl - realized_already_recorded

        class Broker:
            def __init__(self):
                self.position_snapshots = [40, 40, 30]
                self.sell_reads = 0

            def get_positions(self):
                qty = (
                    self.position_snapshots.pop(0)
                    if self.position_snapshots
                    else 30
                )
                return [{"symbol": "BWMN", "qty": qty}]

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return _exit_leg_snapshot(order_id)
                if order_id == "replacement-stop":
                    snapshot = _exit_leg_snapshot(
                        "replacement-stop", status="new", qty=30
                    )
                    snapshot["type"] = "stop"
                    snapshot["stop_price"] = 42.31
                    return snapshot
                self.sell_reads += 1
                return {
                    "id": order_id,
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "market",
                    "status": (
                        "partially_filled"
                        if self.sell_reads == 1
                        else "canceled"
                    ),
                    "filled_qty": 10,
                    "filled_avg_price": 41.00,
                }

            def cancel_order(self, _order_id):
                return True

            def submit_market_order(self, *_args, **_kwargs):
                return "sell-1"

            def submit_stop_order(self, **_kwargs):
                return "replacement-stop"

        broker = Broker()
        circuit = Circuit()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._circuit = circuit

        with patch(
            "trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS",
            0.0,
        ):
            result = manager._close_position(
                position, 42.36, "manual_partial"
            )

        partial_pnl = 10 * (41.00 - 42.33)
        self.assertIsNone(result)
        self.assertEqual(position.shares_remaining, 30)
        self.assertEqual(position.status, PositionStatus.PARTIALLY_CLOSED)
        self.assertAlmostEqual(position.pnl_realized, partial_pnl)
        self.assertAlmostEqual(manager.get_daily_pnl(), partial_pnl)
        self.assertEqual(circuit.partial_calls, [(partial_pnl, True)])
        self.assertAlmostEqual(circuit.daily_pnl, partial_pnl)
        self.assertAlmostEqual(
            position.pnl_unrealized,
            position.shares_remaining
            * (position.current_price - position.entry_price),
        )

        remaining_unrealized = 30 * (42.00 - 42.33)
        circuit.update_unrealized_pnl(remaining_unrealized)
        self.assertAlmostEqual(
            circuit.daily_pnl + circuit.unrealized_pnl,
            partial_pnl + remaining_unrealized,
        )

        entry = manager._finalize_close(
            position,
            actual_exit_price=43.00,
            actual_shares=30,
            reason="manual_close",
        )
        self.assertAlmostEqual(entry.pnl, 6.80)
        self.assertAlmostEqual(entry.exit_price, 42.50)
        self.assertAlmostEqual(manager.get_daily_pnl(), 6.80)
        self.assertAlmostEqual(circuit.daily_pnl, 6.80)
        self.assertEqual(len(circuit.final_calls), 1)
        self.assertAlmostEqual(circuit.final_calls[0][0], 6.80)
        self.assertAlmostEqual(circuit.final_calls[0][1], partial_pnl)
        self.assertTrue(circuit.final_calls[0][2])

    def test_manual_full_fill_requires_broker_to_be_flat(self):
        class Broker:
            def __init__(self):
                self.position_snapshots = [40, 40, 10]
                self.qty = 10
                self.close_all_calls = 0

            def get_positions(self):
                if self.position_snapshots:
                    self.qty = self.position_snapshots.pop(0)
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return _exit_leg_snapshot(order_id)
                return {
                    "id": order_id,
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "market",
                    "status": "filled",
                    "filled_qty": 40,
                    "filled_avg_price": 42.31,
                }

            def cancel_order(self, _order_id):
                return True

            def submit_market_order(self, *_args, **_kwargs):
                return "sell-1"

            def close_all_positions(self):
                self.close_all_calls += 1
                self.position_snapshots.clear()
                self.qty = 0
                return True

        broker = Broker()
        position = _position(qty=40)
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError,
            "close_full_fill_broker_qty_divergence:BWMN",
        ):
            manager._close_position(position, 42.36, "trailing_stop")

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_daily_pnl(), 0.0)
        self.assertEqual(manager.get_daily_journal_entries(), [])

    def test_manual_close_rejects_overfill_or_invalid_fill_price(self):
        cases = (
            (50, 42.31),
            (40, 0.0),
            (40, float("nan")),
            (40, float("inf")),
        )
        for reported_fill, reported_price in cases:
            with self.subTest(
                filled_qty=reported_fill,
                filled_avg_price=reported_price,
            ):
                class Broker:
                    def __init__(self):
                        self.qty = 40
                        self.close_all_calls = 0

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                    def get_order_status(self, order_id):
                        if order_id in {"stop-1", "tp-1"}:
                            return _exit_leg_snapshot(order_id)
                        return {
                            "id": order_id,
                            "symbol": "BWMN",
                            "side": "sell",
                            "qty": 40,
                            "type": "market",
                            "status": "filled",
                            "filled_qty": reported_fill,
                            "filled_avg_price": reported_price,
                        }

                    def cancel_order(self, _order_id):
                        return True

                    def submit_market_order(self, *_args, **_kwargs):
                        return "sell-1"

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                broker = Broker()
                position = _position(qty=40)
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaisesRegex(
                    PortfolioSafetyError,
                    "close_sell_snapshot_invalid:BWMN",
                ):
                    manager._close_position(
                        position, 42.36, "trailing_stop"
                    )

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(manager.get_daily_pnl(), 0.0)
                self.assertEqual(manager.get_daily_journal_entries(), [])

    def test_unconfirmed_exit_cancel_does_not_add_competing_stop(self):
        class Broker:
            def __init__(self):
                self.stop_submissions = 0

            def get_positions(self):
                return [{"symbol": "BWMN", "qty": 40}]

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return _exit_leg_snapshot(order_id)
                return {
                    "id": order_id,
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "market",
                    "status": "accepted",
                    "filled_qty": 0,
                    "filled_avg_price": 0.0,
                }

            def cancel_order(self, order_id):
                return order_id in {"stop-1", "tp-1"}

            def submit_market_order(self, *_args, **_kwargs):
                return "sell-1"

            def submit_stop_order(self, *_args, **_kwargs):
                self.stop_submissions += 1
                return "replacement-stop"

        broker = Broker()
        position = _position()
        manager = _manager(broker)
        manager._positions[position.symbol] = position
        broker.close_all_positions = lambda: setattr(
            broker, "positions_cleared", True
        ) or True
        original_get_positions = broker.get_positions
        broker.get_positions = lambda: (
            []
            if getattr(broker, "positions_cleared", False)
            else original_get_positions()
        )
        with (
            patch(
                "trading_bot.portfolio.manager._ORDER_FILL_TIMEOUT_SECONDS",
                0.0,
            ),
            self.assertRaisesRegex(
                PortfolioSafetyError, "close_sell_cancel_unconfirmed:BWMN"
            ),
        ):
            manager._close_position(position, 42.36, "test")

        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(broker.stop_submissions, 0)

    def test_cancel_losing_race_to_full_fill_finalizes_close(self):
        class Broker:
            def __init__(self):
                self.sell_status_calls = 0

            def get_positions(self):
                return (
                    []
                    if self.sell_status_calls >= 2
                    else [{"symbol": "BWMN", "qty": 40}]
                )

            def get_order_status(self, order_id):
                if order_id in {"stop-1", "tp-1"}:
                    return _exit_leg_snapshot(order_id)
                self.sell_status_calls += 1
                if self.sell_status_calls == 1:
                    return {
                        "id": order_id,
                        "symbol": "BWMN",
                        "side": "sell",
                        "qty": 40,
                        "type": "market",
                        "status": "accepted",
                        "filled_qty": 0,
                        "filled_avg_price": 0.0,
                    }
                return {
                    "id": order_id,
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "market",
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

    def test_restore_stop_requires_fresh_exact_broker_confirmation(self):
        class Broker:
            def __init__(self):
                self.close_all_calls = 0

            def submit_stop_order(self, symbol, qty, stop_price):
                self.submission = (symbol, qty, stop_price)
                return "replacement-stop"

            def get_order_status(self, order_id):
                return {
                    "id": order_id,
                    "status": "new",
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "stop",
                    "stop_price": 42.31,
                }

            def close_all_positions(self):
                self.close_all_calls += 1
                return True

        broker = Broker()
        position = _position()
        position.broker_stop_order_id = None
        position.broker_tp_order_id = None

        restored = _manager(broker)._restore_stop(position, 40)

        self.assertTrue(restored)
        self.assertEqual(position.broker_stop_order_id, "replacement-stop")
        self.assertEqual(broker.close_all_calls, 0)

    def test_rejected_restored_stop_triggers_verified_global_flatten(self):
        class Broker:
            def __init__(self):
                self.qty = 40
                self.close_all_calls = 0

            def submit_stop_order(self, **_kwargs):
                return "replacement-stop"

            def get_order_status(self, order_id):
                return {
                    "id": order_id,
                    "status": "rejected",
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "stop",
                    "stop_price": 42.31,
                }

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

            def get_positions(self):
                return ([{"symbol": "BWMN", "qty": self.qty}] if self.qty else [])

        broker = Broker()
        position = _position()
        position.broker_stop_order_id = None
        position.broker_tp_order_id = None
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "stop_restore_failed:BWMN"
        ):
            manager._restore_stop(position, 40)

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.shares_remaining, 0)
        self.assertEqual(manager.get_open_positions(), [])

    def test_restore_stop_rejects_wrong_type_or_effective_price(self):
        for field, bad_value in (("type", "limit"), ("stop_price", 42.30)):
            with self.subTest(field=field):
                class Broker:
                    def __init__(self):
                        self.qty = 40
                        self.close_all_calls = 0

                    def submit_stop_order(self, **_kwargs):
                        return "replacement-stop"

                    def get_order_status(self, order_id):
                        snapshot = {
                            "id": order_id,
                            "status": "new",
                            "symbol": "BWMN",
                            "side": "sell",
                            "qty": 40,
                            "type": "stop",
                            "stop_price": 42.31,
                        }
                        snapshot[field] = bad_value
                        return snapshot

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                position = _position()
                position.broker_stop_order_id = None
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaisesRegex(
                    PortfolioSafetyError, "stop_restore_failed:BWMN"
                ):
                    manager._restore_stop(position, 40)

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(manager.get_open_positions(), [])

    def test_scale_out_leaves_verified_bracket_untouched(self):
        class Broker:
            def __init__(self):
                self.cancel_calls = 0
                self.sell_calls = 0

            def cancel_order(self, _order_id):
                self.cancel_calls += 1
                raise AssertionError("protected bracket must remain active")

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                raise AssertionError("scale-out must be deferred")

            def get_order_status(self, order_id):
                self.asserted_order_id = order_id
                return {
                    "id": order_id,
                    "status": "new",
                    "symbol": "BWMN",
                    "side": "sell",
                    "qty": 40,
                    "type": "stop",
                    "stop_price": 42.31,
                }

        broker = Broker()
        position = _position()
        _manager(broker)._execute_scale_out(position, 20, 42.39, "target_1")

        self.assertEqual(broker.cancel_calls, 0)
        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(position.broker_stop_order_id, "stop-1")
        self.assertEqual(position.broker_tp_order_id, "tp-1")
        self.assertEqual(position.shares_remaining, 40)
        self.assertEqual(position.scale_outs_completed, 0)

    def test_stale_tracked_stop_cannot_count_as_scale_out_protection(self):
        for status in ("canceled", "rejected", "expired", "error", "unknown"):
            with self.subTest(status=status):
                class Broker:
                    def __init__(self):
                        self.qty = 40
                        self.sell_calls = 0
                        self.close_all_calls = 0

                    def get_order_status(self, order_id):
                        return {
                            "id": order_id,
                            "status": status,
                            "symbol": "BWMN",
                            "side": "sell",
                            "qty": 40,
                            "type": "stop",
                            "stop_price": 42.31,
                        }

                    def submit_market_order(self, *_args, **_kwargs):
                        self.sell_calls += 1
                        raise AssertionError("must not submit naked scale-out")

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                position = _position()
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaises(PortfolioSafetyError):
                    manager._execute_scale_out(
                        position, 20, 42.39, "target_1"
                    )

                self.assertEqual(broker.sell_calls, 0)
                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(manager.get_open_positions(), [])

    def test_scale_out_without_tracked_stop_is_hard_failure(self):
        class Broker:
            def __init__(self):
                self.sell_calls = 0

            def submit_market_order(self, *_args, **_kwargs):
                self.sell_calls += 1
                return "scale-sell"

            def close_all_positions(self):
                return True

            def get_positions(self):
                return []

        broker = Broker()
        for tp_order_id in (None, "tp-only"):
            with self.subTest(tp_order_id=tp_order_id):
                position = _position()
                position.broker_stop_order_id = None
                position.broker_tp_order_id = tp_order_id
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaises(PortfolioSafetyError):
                    manager._execute_scale_out(position, 20, 42.39, "target_1")

                self.assertEqual(position.shares_remaining, 0)
                self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(broker.sell_calls, 0)


class TestPortfolioStopLiveness(unittest.TestCase):
    @staticmethod
    def _active_snapshot(order_id: str, status: str = "new") -> dict:
        return {
            "id": order_id,
            "status": status,
            "symbol": "BWMN",
            "side": "sell",
            "qty": 40,
            "type": "stop" if order_id.startswith("stop") else "limit",
            "stop_price": 42.31 if order_id.startswith("stop") else 0.0,
            "limit_price": 42.37 if order_id.startswith("tp") else 0.0,
            "filled_qty": 0,
        }

    @staticmethod
    def _idle_strategy(trailing_stop=None):
        return types.SimpleNamespace(
            should_exit=lambda _position, _bars: (False, ""),
            compute_scale_out=lambda _position, _price: None,
            get_trailing_stop=lambda _position, _bars: trailing_stop,
        )

    @staticmethod
    def _market():
        return types.SimpleNamespace(
            get_current_price=lambda _symbol: 42.36,
            get_intraday_bars=lambda _symbol, lookback_bars: [],
        )

    def test_every_update_flattens_and_halts_on_dead_or_missing_stop(self):
        cases = ("canceled", "rejected", "expired", "error", "unknown", None)
        for stop_state in cases:
            with self.subTest(stop_state=stop_state):
                class Broker:
                    def __init__(self):
                        self.qty = 40
                        self.close_all_calls = 0

                    def get_order_status(self, order_id):
                        if order_id == "tp-1":
                            return TestPortfolioStopLiveness._active_snapshot(
                                order_id
                            )
                        return TestPortfolioStopLiveness._active_snapshot(
                            order_id, stop_state or "new"
                        )

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                position = _position()
                if stop_state is None:
                    position.broker_stop_order_id = None
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaises(PortfolioSafetyError):
                    manager.update_positions(
                        self._idle_strategy(), self._market()
                    )

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(position.shares_remaining, 0)
                self.assertEqual(manager.get_open_positions(), [])

    def test_every_update_flattens_and_halts_on_stop_lookup_exception(self):
        class Broker:
            def __init__(self):
                self.qty = 40
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                if order_id == "stop-1":
                    raise RuntimeError("status endpoint unavailable")
                return TestPortfolioStopLiveness._active_snapshot(order_id)

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

            def get_positions(self):
                return ([{"symbol": "BWMN", "qty": self.qty}] if self.qty else [])

        broker = Broker()
        position = _position()
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaises(PortfolioSafetyError):
            manager.update_positions(self._idle_strategy(), self._market())

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_open_positions(), [])

    def test_nonfinite_market_price_never_mutates_position_before_safety_halt(self):
        class Broker:
            def __init__(self):
                self.qty = 40
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                return TestPortfolioStopLiveness._active_snapshot(order_id)

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

            def get_positions(self):
                return ([{"symbol": "BWMN", "qty": self.qty}] if self.qty else [])

        for invalid_price in (float("nan"), float("inf"), -1.0):
            with self.subTest(price=invalid_price):
                broker = Broker()
                position = _position()
                original_price = position.current_price
                manager = _manager(broker)
                manager._positions[position.symbol] = position
                market = types.SimpleNamespace(
                    get_current_price=lambda _symbol: invalid_price,
                    get_intraday_bars=lambda _symbol, lookback_bars: [],
                )

                with self.assertRaisesRegex(
                    PortfolioSafetyError, "market_price_invalid"
                ):
                    manager.update_positions(self._idle_strategy(), market)

                self.assertEqual(position.current_price, original_price)
                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(manager.get_open_positions(), [])

    def test_held_stop_is_protective_only_with_fresh_exact_new_target(self):
        class Broker:
            def get_order_status(self, order_id):
                return TestPortfolioStopLiveness._active_snapshot(
                    order_id, "held" if order_id == "stop-1" else "new"
                )

        position = _position()
        manager = _manager(Broker())
        manager._positions[position.symbol] = position

        manager.update_positions(self._idle_strategy(), self._market())

        self.assertEqual(position.status, PositionStatus.OPEN)
        self.assertEqual(position.shares_remaining, 40)

    def test_held_stop_with_missing_or_dead_target_flattens_and_halts(self):
        for tp_state in (None, "canceled", "rejected", "expired", "held"):
            with self.subTest(tp_state=tp_state):
                class Broker:
                    def __init__(self):
                        self.qty = 40
                        self.close_all_calls = 0

                    def get_order_status(self, order_id):
                        return TestPortfolioStopLiveness._active_snapshot(
                            order_id,
                            "held" if order_id == "stop-1" else tp_state,
                        )

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                position = _position()
                if tp_state is None:
                    position.broker_tp_order_id = None
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaises(PortfolioSafetyError):
                    manager.update_positions(
                        self._idle_strategy(), self._market()
                    )

                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)
                self.assertEqual(manager.get_open_positions(), [])

    def test_held_stop_with_nonfinite_target_price_flattens_and_halts(self):
        class Broker:
            def __init__(self):
                self.qty = 40
                self.close_all_calls = 0

            def get_order_status(self, order_id):
                snapshot = TestPortfolioStopLiveness._active_snapshot(
                    order_id, "held" if order_id == "stop-1" else "new"
                )
                if order_id == "tp-1":
                    snapshot["limit_price"] = float("nan")
                return snapshot

            def close_all_positions(self):
                self.close_all_calls += 1
                self.qty = 0
                return True

            def get_positions(self):
                return (
                    [{"symbol": "BWMN", "qty": self.qty}]
                    if self.qty
                    else []
                )

        broker = Broker()
        position = _position()
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        with self.assertRaisesRegex(
            PortfolioSafetyError, "tp_limit_price=nan"
        ):
            manager.update_positions(self._idle_strategy(), self._market())

        self.assertEqual(broker.close_all_calls, 1)
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(manager.get_open_positions(), [])

    def test_rejected_or_unready_trailing_replacement_is_never_assigned(self):
        for replacement_status in ("rejected", "accepted", "pending_new"):
            with self.subTest(replacement_status=replacement_status):
                class Broker:
                    def __init__(self):
                        self.qty = 40
                        self.close_all_calls = 0

                    def get_order_status(self, order_id):
                        if order_id == "replacement-stop":
                            snapshot = TestPortfolioStopLiveness._active_snapshot(
                                order_id, replacement_status
                            )
                            snapshot["type"] = "stop"
                            snapshot["stop_price"] = 42.32
                            return snapshot
                        return TestPortfolioStopLiveness._active_snapshot(order_id)

                    def replace_stop_order(self, **_kwargs):
                        return "replacement-stop"

                    def close_all_positions(self):
                        self.close_all_calls += 1
                        self.qty = 0
                        return True

                    def get_positions(self):
                        return (
                            [{"symbol": "BWMN", "qty": self.qty}]
                            if self.qty
                            else []
                        )

                broker = Broker()
                position = _position()
                manager = _manager(broker)
                manager._positions[position.symbol] = position

                with self.assertRaises(PortfolioSafetyError):
                    manager.update_positions(
                        self._idle_strategy(trailing_stop=42.32),
                        self._market(),
                    )

                self.assertNotEqual(
                    position.broker_stop_order_id, "replacement-stop"
                )
                self.assertEqual(broker.close_all_calls, 1)
                self.assertEqual(position.status, PositionStatus.CLOSED)

    def test_exact_new_trailing_replacement_is_verified_before_assignment(self):
        class Broker:
            def get_order_status(self, order_id):
                snapshot = TestPortfolioStopLiveness._active_snapshot(order_id)
                if order_id == "replacement-stop":
                    snapshot["type"] = "stop"
                    snapshot["stop_price"] = 42.32
                return snapshot

            def replace_stop_order(self, **_kwargs):
                return "replacement-stop"

        position = _position()
        manager = _manager(Broker())
        manager._positions[position.symbol] = position

        manager.update_positions(
            self._idle_strategy(trailing_stop=42.32), self._market()
        )

        self.assertEqual(position.broker_stop_order_id, "replacement-stop")
        self.assertEqual(position.trailing_stop_price, 42.32)
        self.assertTrue(position.trailing_stop_active)

    def test_tp_fill_is_reconciled_before_scale_or_trailing_candidates(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("BWMN", 42.33)
        bracket = broker.submit_bracket_order(
            "BWMN",
            40,
            OrderSide.BUY,
            stop_price=42.31,
            take_profit_price=42.37,
        )
        position = _position()
        position.entry_price = broker.get_order_status(
            bracket["entry_order_id"]
        )["filled_avg_price"]
        position.broker_order_ids = [bracket["entry_order_id"]]
        position.broker_stop_order_id = bracket["stop_order_id"]
        position.broker_tp_order_id = bracket["tp_order_id"]

        strategy_calls = {"exit": 0, "scale": 0, "trailing": 0}

        def should_exit(_position, _bars):
            strategy_calls["exit"] += 1
            return False, ""

        def compute_scale_out(_position, _price):
            strategy_calls["scale"] += 1
            return 20, "target_1"

        def get_trailing_stop(_position, _bars):
            strategy_calls["trailing"] += 1
            return 42.32

        strategy = types.SimpleNamespace(
            should_exit=should_exit,
            compute_scale_out=compute_scale_out,
            get_trailing_stop=get_trailing_stop,
        )
        market = types.SimpleNamespace(
            get_current_price=lambda _symbol: 42.38,
            get_intraday_bars=lambda _symbol, lookback_bars: [],
        )
        manager = _manager(broker)
        manager._positions[position.symbol] = position

        entries = manager.update_positions(strategy, market)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].exit_reason, "broker_take_profit")
        self.assertEqual(position.status, PositionStatus.CLOSED)
        self.assertEqual(position.shares_remaining, 0)
        self.assertEqual(manager.get_open_positions(), [])
        self.assertEqual(broker.get_positions(), [])
        self.assertEqual(broker._pending_orders, {})
        self.assertEqual(
            broker.get_order_status(bracket["tp_order_id"])["status"],
            "filled",
        )
        self.assertEqual(
            broker.get_order_status(bracket["stop_order_id"])["status"],
            "cancelled_oco",
        )
        self.assertEqual(strategy_calls, {"exit": 0, "scale": 0, "trailing": 0})


if __name__ == "__main__":
    unittest.main()
