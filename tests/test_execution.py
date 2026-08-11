"""Tests for broker implementations."""

from __future__ import annotations

import copy

import pytest

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.domain import OrderSide


class TestPaperBroker:
    """Test the in-memory paper broker."""

    def test_initial_equity(self):
        broker = PaperBroker(initial_equity=25_000.0)
        assert broker.get_account_equity() == 25_000.0

    def test_buying_power_is_4x(self):
        broker = PaperBroker(initial_equity=25_000.0)
        assert broker.get_buying_power() == 100_000.0

    def test_buy_order(self):
        broker = PaperBroker(initial_equity=25_000.0)
        broker.update_price("TEST", 10.0)

        order_id = broker.submit_market_order("TEST", 100, OrderSide.BUY)
        assert order_id is not None

        status = broker.get_order_status(order_id)
        assert status["status"] == "filled"
        assert status["filled_qty"] == 100

        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "TEST"
        assert positions[0]["qty"] == 100

    def test_sell_order(self):
        broker = PaperBroker(initial_equity=25_000.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        broker.update_price("TEST", 11.0)
        order_id = broker.submit_market_order("TEST", 100, OrderSide.SELL)

        status = broker.get_order_status(order_id)
        assert status["status"] == "filled"

        positions = broker.get_positions()
        assert len(positions) == 0

    def test_sell_without_position_rejected(self):
        broker = PaperBroker()
        order_id = broker.submit_market_order("NOPOS", 100, OrderSide.SELL)
        status = broker.get_order_status(order_id)
        assert status["status"] == "rejected"

    def test_partial_sell(self):
        broker = PaperBroker(initial_equity=25_000.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        broker.submit_market_order("TEST", 33, OrderSide.SELL)
        positions = broker.get_positions()
        assert positions[0]["qty"] == 67

    def test_slippage_applied(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=10.0)
        broker.update_price("TEST", 10.0)

        order_id = broker.submit_market_order("TEST", 100, OrderSide.BUY)
        status = broker.get_order_status(order_id)
        # Buy slippage: price * 1.001 = 10.01
        assert status["filled_avg_price"] > 10.0

    def test_close_position(self):
        broker = PaperBroker(initial_equity=25_000.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        result = broker.close_position("TEST")
        assert result is True
        assert len(broker.get_positions()) == 0

    def test_close_position_cancels_all_symbol_exit_orders(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        bracket = broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )

        assert broker.close_position("TEST") is True
        assert (
            broker.get_order_status(bracket["stop_order_id"])["status"]
            == "cancelled"
        )
        assert broker.get_order_status(bracket["tp_order_id"])["status"] == "cancelled"

        # A later price move must not fire a stale exit after the account is flat.
        assert broker.update_price("TEST", 8.0) == []
        assert broker.get_positions() == []

    def test_close_all_positions(self):
        broker = PaperBroker(initial_equity=50_000.0)
        broker.update_price("AAA", 10.0)
        broker.update_price("BBB", 20.0)
        broker.submit_market_order("AAA", 100, OrderSide.BUY)
        broker.submit_market_order("BBB", 50, OrderSide.BUY)

        assert len(broker.get_positions()) == 2
        assert broker.close_all_positions() is True
        assert len(broker.get_positions()) == 0
        fills = broker.get_last_close_fills()
        assert {fill["symbol"] for fill in fills} == {"AAA", "BBB"}
        assert all(fill["status"] == "filled" for fill in fills)
        assert all(fill["side"] == "sell" for fill in fills)
        assert {fill["symbol"]: fill["qty"] for fill in fills} == {
            "AAA": 100,
            "BBB": 50,
        }
        assert all(fill["filled_qty"] == fill["qty"] for fill in fills)
        assert all(fill["id"] in broker._orders for fill in fills)
        assert all(fill["filled_avg_price"] > 0 for fill in fills)

    def test_close_all_positions_rejects_tampered_nonfinite_fill_proof(self):
        broker = PaperBroker(initial_equity=50_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)
        original_get_order_status = broker.get_order_status

        def tampered_get_order_status(order_id: str) -> dict:
            snapshot = original_get_order_status(order_id)
            if (
                snapshot.get("status") == "filled"
                and snapshot.get("side") == "sell"
            ):
                snapshot["filled_avg_price"] = float("nan")
            return snapshot

        broker.get_order_status = tampered_get_order_status

        # Empty position/order maps alone are not sufficient proof of a close.
        assert broker.close_all_positions() is False
        assert broker.get_positions() == []
        fills = broker.get_last_close_fills()
        assert len(fills) == 1
        assert fills[0]["filled_avg_price"] != fills[0]["filled_avg_price"]

    @pytest.mark.parametrize(
        "field,tampered_value",
        [
            ("status", "partially_filled"),
            ("symbol", "OTHER"),
            ("side", "buy"),
            ("qty", 99),
            ("filled_qty", 99.5),
        ],
    )
    def test_close_all_positions_rejects_mismatched_fill_identity(
        self, field, tampered_value
    ):
        broker = PaperBroker(initial_equity=50_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)
        original_get_order_status = broker.get_order_status

        def tampered_get_order_status(order_id: str) -> dict:
            snapshot = original_get_order_status(order_id)
            if (
                snapshot.get("status") == "filled"
                and snapshot.get("side") == "sell"
            ):
                snapshot[field] = tampered_value
            return snapshot

        broker.get_order_status = tampered_get_order_status

        assert broker.close_all_positions() is False
        assert broker.get_positions() == []

    def test_close_all_positions_rejects_unbacked_forged_fill(self):
        broker = PaperBroker(initial_equity=50_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        def forged_close(symbol: str) -> bool:
            qty = broker._positions[symbol]["qty"]
            del broker._positions[symbol]
            broker._last_close_fills.append(
                {
                    "id": "not-a-broker-order",
                    "status": "filled",
                    "symbol": symbol,
                    "side": "sell",
                    "qty": qty,
                    "filled_qty": qty,
                    "filled_avg_price": 10.0,
                }
            )
            return True

        broker.close_position = forged_close

        assert broker.close_all_positions() is False
        assert broker.get_positions() == []

    def test_close_all_positions_does_not_hide_signed_short_state(self):
        broker = PaperBroker(initial_equity=50_000.0, slippage_bps=0.0)
        broker._positions["SHORT"] = {
            "qty": -5,
            "avg_entry_price": 10.0,
            "current_price": 9.0,
            "opened_at": "2026-08-10T10:00:00-04:00",
        }

        # PaperBroker does not originate shorts; a corrupt/injected short must
        # remain visible internally and make the reset fail closed.
        assert broker.get_positions() == []
        assert broker.close_all_positions() is False
        assert broker._positions["SHORT"]["qty"] == -5
        assert broker.get_last_close_fills() == []

    def test_close_all_positions_cancels_orphan_orders(self):
        broker = PaperBroker(initial_equity=50_000.0)
        broker.update_price("ORPHAN", 10.0)
        orphan_id = broker.submit_stop_order("ORPHAN", 10, 9.0)

        assert broker.get_positions() == []
        assert broker.get_order_status(orphan_id)["status"] == "new"
        assert broker.close_all_positions() is True
        assert broker.get_order_status(orphan_id)["status"] == "cancelled"
        assert broker._pending_orders == {}
        assert broker.get_last_close_fills() == []
        assert broker.update_price("ORPHAN", 8.0) == []

    def test_bracket_parent_returns_fresh_nested_working_sell_legs(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        bracket = broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )

        parent = broker.get_order_status(bracket["entry_order_id"])
        assert parent["status"] == "filled"
        assert [leg["id"] for leg in parent["legs"]] == [
            bracket["stop_order_id"],
            bracket["tp_order_id"],
        ]
        assert all(leg["status"] == "new" for leg in parent["legs"])
        assert all(leg["side"] == "sell" for leg in parent["legs"])
        assert all(leg["qty"] == 100 for leg in parent["legs"])
        assert {leg["type"] for leg in parent["legs"]} == {"stop", "limit"}

        # Nested children must be rebuilt from current broker state, not a
        # frozen copy captured when the parent was submitted.
        assert broker.cancel_order(bracket["stop_order_id"]) is True
        refreshed = broker.get_order_status(bracket["entry_order_id"])
        assert refreshed["legs"][0]["status"] == "cancelled"
        assert parent["legs"][0]["status"] == "new"

    def test_equity_changes_with_pnl(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        # Equity should include position value
        broker.update_price("TEST", 12.0)
        equity = broker.get_account_equity()
        # Cash: 25000 - 1000 = 24000, Position: 100 * 12 = 1200
        assert equity == pytest.approx(25_200.0, abs=1.0)

    @pytest.mark.parametrize(
        "invalid_price",
        [float("nan"), float("inf"), float("-inf"), 0.0, -1.0],
    )
    def test_update_price_rejects_invalid_value_without_mutation(
        self, invalid_price
    ):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )
        before = {
            "cash": broker._cash,
            "last_prices": copy.deepcopy(broker._last_prices),
            "positions": copy.deepcopy(broker._positions),
            "orders": copy.deepcopy(broker._orders),
            "pending": copy.deepcopy(broker._pending_orders),
        }

        with pytest.raises(ValueError, match="finite and positive"):
            broker.update_price("TEST", invalid_price)

        assert broker._cash == before["cash"]
        assert broker._last_prices == before["last_prices"]
        assert broker._positions == before["positions"]
        assert broker._orders == before["orders"]
        assert broker._pending_orders == before["pending"]

    def test_market_fill_rejects_nonfinite_slippage_without_mutation(self):
        class NonFiniteSlippage:
            @staticmethod
            def compute_slippage(side, base_price, qty):
                return float("nan")

        broker = PaperBroker(
            initial_equity=25_000.0,
            slippage_model=NonFiniteSlippage(),
        )
        broker.update_price("TEST", 10.0)
        before_cash = broker._cash
        before_orders = copy.deepcopy(broker._orders)

        with pytest.raises(ValueError, match="Market fill price"):
            broker.submit_market_order("TEST", 100, OrderSide.BUY)

        assert broker._cash == before_cash
        assert broker._positions == {}
        assert broker._orders == before_orders

    @pytest.mark.parametrize("invalid_fill_price", [float("nan"), float("inf")])
    def test_triggered_fill_preflight_is_atomic_on_nonfinite_slippage(
        self, invalid_fill_price
    ):
        class NonFiniteSlippage:
            @staticmethod
            def compute_slippage(side, base_price, qty):
                return invalid_fill_price

        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )
        broker._slippage = NonFiniteSlippage()
        before = {
            "cash": broker._cash,
            "last_prices": copy.deepcopy(broker._last_prices),
            "positions": copy.deepcopy(broker._positions),
            "orders": copy.deepcopy(broker._orders),
            "pending": copy.deepcopy(broker._pending_orders),
            "timestamps": copy.deepcopy(broker._order_timestamps),
            "day_trades": broker._day_trades,
        }

        with pytest.raises(ValueError, match="Stop fill price"):
            broker.update_price("TEST", 8.5)

        assert broker._cash == before["cash"]
        assert broker._last_prices == before["last_prices"]
        assert broker._positions == before["positions"]
        assert broker._orders == before["orders"]
        assert broker._pending_orders == before["pending"]
        assert broker._order_timestamps == before["timestamps"]
        assert broker._day_trades == before["day_trades"]

    def test_triggered_fill_commits_exact_preflighted_price(self):
        class ExactSlippage:
            @staticmethod
            def compute_slippage(side, base_price, qty):
                return 8.75

        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        bracket = broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )
        broker._slippage = ExactSlippage()

        triggered = broker.update_price("TEST", 8.5)

        assert [fill["id"] for fill in triggered] == [
            bracket["stop_order_id"]
        ]
        assert triggered[0]["filled_avg_price"] == 8.75
        assert (
            broker.get_order_status(bracket["stop_order_id"])[
                "filled_avg_price"
            ]
            == 8.75
        )
        assert broker.get_positions() == []

    @pytest.mark.parametrize("invalid_qty", [1.5, True, float("nan")])
    def test_order_quantity_requires_exact_positive_integer(self, invalid_qty):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)

        with pytest.raises(ValueError, match="positive integer"):
            broker.submit_market_order("TEST", invalid_qty, OrderSide.BUY)

        assert broker._cash == 25_000.0
        assert broker._positions == {}
        assert broker._orders == {}

    def test_invalid_bracket_child_price_cannot_fill_parent(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)

        with pytest.raises(ValueError, match="Stop price"):
            broker.submit_bracket_order(
                "TEST",
                100,
                OrderSide.BUY,
                stop_price=float("nan"),
                take_profit_price=12.0,
            )

        assert broker._cash == 25_000.0
        assert broker._positions == {}
        assert broker._orders == {}

    def test_day_trade_count(self):
        broker = PaperBroker(initial_equity=25_000.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        assert broker.get_day_trade_count() == 0

        broker.submit_market_order("TEST", 100, OrderSide.SELL)
        assert broker.get_day_trade_count() == 1

    def test_cancel_order(self):
        broker = PaperBroker()
        broker.update_price("TEST", 10.0)
        order_id = broker.submit_stop_order("TEST", 100, 9.0)

        assert broker.cancel_order(order_id) is True
        status = broker.get_order_status(order_id)
        assert status["status"] == "cancelled"

    def test_cancel_nonexistent_order(self):
        broker = PaperBroker()
        assert broker.cancel_order("fake_id") is False

    def test_cancel_terminal_order_does_not_rewrite_fill_history(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        order_id = broker.submit_market_order("TEST", 10, OrderSide.BUY)

        assert broker.get_order_status(order_id)["status"] == "filled"
        assert broker.cancel_order(order_id) is False
        assert broker.get_order_status(order_id)["status"] == "filled"

    def test_stop_trigger_fills_once_and_cancels_oco_partner(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        bracket = broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )

        triggered = broker.update_price("TEST", 8.5)

        assert len(triggered) == 1
        assert triggered[0]["id"] == bracket["stop_order_id"]
        assert triggered[0]["status"] == "filled"
        assert triggered[0]["filled_qty"] == 100
        assert (
            broker.get_order_status(bracket["tp_order_id"])["status"]
            == "cancelled_oco"
        )
        assert broker.get_positions() == []
        assert broker._pending_orders == {}

    def test_replace_stop_requires_active_order_and_preserves_oco_links(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        bracket = broker.submit_bracket_order(
            "TEST",
            100,
            OrderSide.BUY,
            stop_price=9.0,
            take_profit_price=12.0,
        )

        replacement_id = broker.replace_stop_order(
            bracket["stop_order_id"], 100, 9.5
        )

        assert broker.get_order_status(bracket["stop_order_id"])["status"] == "cancelled"
        replacement = broker.get_order_status(replacement_id)
        target = broker.get_order_status(bracket["tp_order_id"])
        parent = broker.get_order_status(bracket["entry_order_id"])
        assert replacement["status"] == "new"
        assert replacement["stop_price"] == 9.5
        assert replacement["oco_partner"] == bracket["tp_order_id"]
        assert target["oco_partner"] == replacement_id
        assert [leg["id"] for leg in parent["legs"]] == [
            replacement_id,
            bracket["tp_order_id"],
        ]

        assert broker.cancel_order(replacement_id) is True
        with pytest.raises(RuntimeError, match="not active and replaceable"):
            broker.replace_stop_order(replacement_id, 100, 9.6)

    def test_marketable_buy_limit_payload_and_fill_respect_limit(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=100.0)
        broker.update_price("TEST", 10.0)

        order_id = broker.submit_limit_order(
            "TEST", 100, OrderSide.BUY, limit_price=10.0
        )
        status = broker.get_order_status(order_id)

        assert status["status"] == "filled"
        assert status["symbol"] == "TEST"
        assert status["side"] == "buy"
        assert status["type"] == "limit"
        assert status["qty"] == 100
        assert status["limit_price"] == 10.0
        assert status["stop_price"] == 0.0
        assert status["filled_avg_price"] <= 10.0

    def test_non_marketable_buy_limit_waits_then_fills_without_crossing_limit(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=100.0)
        broker.update_price("TEST", 10.0)
        order_id = broker.submit_limit_order(
            "TEST", 100, OrderSide.BUY, limit_price=9.5
        )

        pending = broker.get_order_status(order_id)
        assert pending["status"] == "new"
        assert pending["type"] == "limit"
        assert pending["limit_price"] == 9.5
        assert broker.get_positions() == []

        triggered = broker.update_price("TEST", 9.4)
        filled = broker.get_order_status(order_id)
        assert [order["id"] for order in triggered] == [order_id]
        assert filled["status"] == "filled"
        assert filled["filled_avg_price"] <= 9.5
        assert broker.get_positions()[0]["qty"] == 100
