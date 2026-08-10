"""Tests for broker implementations."""

from __future__ import annotations

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
        assert broker.get_order_status(bracket["stop_order_id"])["status"] == "cancelled"
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
        broker.close_all_positions()
        assert len(broker.get_positions()) == 0

    def test_equity_changes_with_pnl(self):
        broker = PaperBroker(initial_equity=25_000.0, slippage_bps=0.0)
        broker.update_price("TEST", 10.0)
        broker.submit_market_order("TEST", 100, OrderSide.BUY)

        # Equity should include position value
        broker.update_price("TEST", 12.0)
        equity = broker.get_account_equity()
        # Cash: 25000 - 1000 = 24000, Position: 100 * 12 = 1200
        assert equity == pytest.approx(25_200.0, abs=1.0)

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
