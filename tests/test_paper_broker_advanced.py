"""Tests for SlippageModel and advanced PaperBroker features."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from trading_bot.execution.paper_broker import PaperBroker, SlippageModel
from trading_bot.models.domain import OrderSide


class TestSlippageModelBuySide:
    """SlippageModel.compute_slippage() buy gives higher price."""

    def test_buy_slippage_increases_price(self):
        model = SlippageModel(base_bps=5.0, volume_impact_bps=2.0)
        base_price = 10.0

        fill_price = model.compute_slippage(
            side=OrderSide.BUY,
            base_price=base_price,
            qty=100,
            avg_daily_volume=1_000_000,
        )

        assert fill_price > base_price

    def test_buy_slippage_multiple_runs_always_higher(self):
        """Even with random noise, buy fills should consistently be above base."""
        model = SlippageModel(base_bps=10.0, volume_impact_bps=2.0)
        base_price = 50.0

        for _ in range(50):
            fill = model.compute_slippage(
                side=OrderSide.BUY,
                base_price=base_price,
                qty=100,
                avg_daily_volume=1_000_000,
            )
            assert fill >= base_price


class TestSlippageModelSellSide:
    """SlippageModel.compute_slippage() sell gives lower price."""

    def test_sell_slippage_decreases_price(self):
        model = SlippageModel(base_bps=5.0, volume_impact_bps=2.0)
        base_price = 10.0

        fill_price = model.compute_slippage(
            side=OrderSide.SELL,
            base_price=base_price,
            qty=100,
            avg_daily_volume=1_000_000,
        )

        assert fill_price < base_price

    def test_sell_slippage_multiple_runs_always_lower(self):
        """Even with random noise, sell fills should consistently be below base."""
        model = SlippageModel(base_bps=10.0, volume_impact_bps=2.0)
        base_price = 50.0

        for _ in range(50):
            fill = model.compute_slippage(
                side=OrderSide.SELL,
                base_price=base_price,
                qty=100,
                avg_daily_volume=1_000_000,
            )
            assert fill <= base_price


class TestSlippageModelVolumeImpact:
    """Volume impact increases slippage for larger orders."""

    def test_large_order_has_more_slippage_than_small(self):
        model = SlippageModel(base_bps=5.0, volume_impact_bps=5.0, volatility_multiplier=1.0)
        base_price = 10.0
        avg_vol = 1_000_000

        # Use a fixed seed to remove noise
        import random
        random.seed(42)
        small_fill = model.compute_slippage(
            side=OrderSide.BUY,
            base_price=base_price,
            qty=100,
            avg_daily_volume=avg_vol,
        )

        random.seed(42)
        large_fill = model.compute_slippage(
            side=OrderSide.BUY,
            base_price=base_price,
            qty=100_000,
            avg_daily_volume=avg_vol,
        )

        # Larger order should produce a higher buy fill (more slippage)
        assert large_fill > small_fill

    def test_volume_impact_sell_side(self):
        """Larger sell orders should get worse fills (lower price)."""
        model = SlippageModel(base_bps=5.0, volume_impact_bps=5.0, volatility_multiplier=1.0)
        base_price = 20.0
        avg_vol = 500_000

        import random
        random.seed(99)
        small_fill = model.compute_slippage(
            side=OrderSide.SELL,
            base_price=base_price,
            qty=50,
            avg_daily_volume=avg_vol,
        )

        random.seed(99)
        large_fill = model.compute_slippage(
            side=OrderSide.SELL,
            base_price=base_price,
            qty=50_000,
            avg_daily_volume=avg_vol,
        )

        # Larger sell order gets worse (lower) fill
        assert large_fill < small_fill

    def test_zero_volume_impact_when_adv_zero(self):
        """When avg_daily_volume is 0, volume impact is skipped."""
        model = SlippageModel(base_bps=5.0, volume_impact_bps=5.0)
        fill = model.compute_slippage(
            side=OrderSide.BUY,
            base_price=10.0,
            qty=1000,
            avg_daily_volume=0,
        )
        # Should still have base slippage applied, price should be >= base
        assert fill >= 10.0


class TestPaperBrokerSubmitStopOrder:
    """submit_stop_order() creates pending order."""

    def test_stop_order_returns_order_id(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)
        assert order_id is not None
        assert isinstance(order_id, str)
        assert len(order_id) > 0

    def test_stop_order_status_is_pending(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)
        status = broker.get_order_status(order_id)

        assert status["status"] == "pending"
        assert status["symbol"] == "TEST"
        assert status["type"] == "stop"
        assert status["qty"] == 100
        assert status["stop_price"] == 9.0

    def test_stop_order_added_to_pending_orders(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)

        assert order_id in broker._pending_orders
        assert order_id in broker._order_timestamps

    def test_multiple_stop_orders(self):
        broker = PaperBroker()
        id1 = broker.submit_stop_order("AAPL", 50, 150.0)
        id2 = broker.submit_stop_order("MSFT", 30, 300.0)

        assert id1 != id2
        assert len(broker._pending_orders) == 2


class TestPaperBrokerCleanupStaleOrders:
    """cleanup_stale_orders() cancels old orders and keeps fresh ones."""

    def test_cleanup_cancels_stale_orders(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)

        # Make the order timestamp old (10 minutes ago)
        from trading_bot.utils.helpers import now_et
        broker._order_timestamps[order_id] = now_et() - timedelta(minutes=10)

        stale_ids = broker.cleanup_stale_orders()

        assert order_id in stale_ids
        assert broker.get_order_status(order_id)["status"] == "cancelled_stale"
        assert order_id not in broker._pending_orders

    def test_cleanup_keeps_fresh_orders(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)

        # Order was just created -- should be fresh
        stale_ids = broker.cleanup_stale_orders()

        assert stale_ids == []
        assert order_id in broker._pending_orders
        assert broker.get_order_status(order_id)["status"] == "pending"

    def test_cleanup_mixed_stale_and_fresh(self):
        broker = PaperBroker()
        stale_id = broker.submit_stop_order("OLD", 50, 8.0)
        fresh_id = broker.submit_stop_order("NEW", 50, 12.0)

        # Make only the first order stale
        from trading_bot.utils.helpers import now_et
        broker._order_timestamps[stale_id] = now_et() - timedelta(minutes=10)

        stale_ids = broker.cleanup_stale_orders()

        assert stale_id in stale_ids
        assert fresh_id not in stale_ids
        assert fresh_id in broker._pending_orders
        assert broker.get_order_status(fresh_id)["status"] == "pending"

    def test_cleanup_returns_empty_when_no_orders(self):
        broker = PaperBroker()
        stale_ids = broker.cleanup_stale_orders()
        assert stale_ids == []


class TestPaperBrokerGetOrderStatus:
    """get_order_status() returns correct status."""

    def test_filled_order_status(self):
        broker = PaperBroker(initial_equity=25_000.0)
        broker.update_price("TEST", 10.0)
        order_id = broker.submit_market_order("TEST", 100, OrderSide.BUY)

        status = broker.get_order_status(order_id)
        assert status["status"] == "filled"
        assert status["filled_qty"] == 100
        assert status["filled_avg_price"] > 0

    def test_pending_stop_order_status(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)

        status = broker.get_order_status(order_id)
        assert status["status"] == "pending"
        assert status["id"] == order_id

    def test_cancelled_order_status(self):
        broker = PaperBroker()
        order_id = broker.submit_stop_order("TEST", 100, 9.0)
        broker.cancel_order(order_id)

        status = broker.get_order_status(order_id)
        assert status["status"] == "cancelled"

    def test_unknown_order_status(self):
        broker = PaperBroker()
        status = broker.get_order_status("nonexistent_id")

        assert status["status"] == "unknown"
        assert status["id"] == "nonexistent_id"
        assert status["filled_qty"] == 0
        assert status["filled_avg_price"] == 0.0

    def test_rejected_order_status(self):
        broker = PaperBroker()
        # Sell without a position triggers rejection
        order_id = broker.submit_market_order("TEST", 100, OrderSide.SELL)

        status = broker.get_order_status(order_id)
        assert status["status"] == "rejected"
        assert status["filled_qty"] == 0
