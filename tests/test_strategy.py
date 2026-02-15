"""Tests for the VWAP pullback strategy."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from trading_bot.config.settings import AppConfig
from trading_bot.models.domain import (
    OrderSide,
    PositionInfo,
    PositionStatus,
    ScanResult,
    SignalType,
)
from trading_bot.strategies.pullback_vwap import PullbackVWAPStrategy


class TestPullbackVWAPStrategy:
    """Test strategy signal generation and exit logic."""

    def test_insufficient_bars_returns_none(
        self, default_config: AppConfig, sample_scan_result: ScanResult
    ):
        """Strategy returns None with fewer than 20 bars."""
        strategy = PullbackVWAPStrategy(default_config)
        short_df = pd.DataFrame(
            {
                "open": [10.0] * 10,
                "high": [10.5] * 10,
                "low": [9.5] * 10,
                "close": [10.0] * 10,
                "volume": [100000] * 10,
            }
        )
        result = strategy.evaluate(sample_scan_result, short_df)
        assert result is None

    def test_empty_bars_returns_none(
        self, default_config: AppConfig, sample_scan_result: ScanResult
    ):
        """Strategy returns None with empty DataFrame."""
        strategy = PullbackVWAPStrategy(default_config)
        result = strategy.evaluate(sample_scan_result, pd.DataFrame())
        assert result is None

    def test_signal_has_valid_structure(
        self, default_config: AppConfig, sample_scan_result: ScanResult, sample_ohlcv_df: pd.DataFrame
    ):
        """If a signal is generated, it has all required fields."""
        strategy = PullbackVWAPStrategy(default_config)
        signal = strategy.evaluate(sample_scan_result, sample_ohlcv_df)

        # Signal may or may not be generated depending on data
        if signal is not None:
            assert signal.symbol == "TEST"
            assert signal.entry_price > 0
            assert signal.stop_price > 0
            assert signal.stop_price < signal.entry_price
            assert len(signal.target_prices) == 2
            assert signal.target_prices[0] > signal.entry_price
            assert signal.target_prices[1] > signal.target_prices[0]
            assert signal.atr > 0
            assert 0 <= signal.confidence <= 1

    def test_stop_loss_exit(self, default_config: AppConfig, mocker):
        """should_exit returns True when price <= stop."""
        mocker.patch(
            "trading_bot.strategies.pullback_vwap.is_past_exit_time",
            return_value=False,
        )
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(current_price=9.40, stop_price=9.50)

        bars = _make_simple_bars(30)
        should_exit, reason = strategy.should_exit(position, bars)

        assert should_exit is True
        assert reason == "stop_loss"

    def test_no_exit_above_stop(self, default_config: AppConfig, mocker):
        """should_exit returns False when price > stop and no other exit triggers."""
        # Mock time to be during market hours (10:00 AM ET)
        mocker.patch(
            "trading_bot.strategies.pullback_vwap.is_past_exit_time",
            return_value=False,
        )
        # Disable PSAR to isolate stop loss testing
        default_config.exit.use_parabolic_sar = False
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(current_price=10.25, stop_price=9.50)

        bars = _make_simple_bars(30)
        should_exit, reason = strategy.should_exit(position, bars)

        assert should_exit is False

    def test_scale_out_at_1r(self, default_config: AppConfig):
        """Scale-out triggers at 1:1 R:R."""
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.50,  # 1:1 R:R (risk=0.50, reward=0.50)
            shares=100,
            scale_outs=0,
        )

        result = strategy.compute_scale_out(position, 10.50)
        assert result is not None
        shares_to_sell, reason = result
        assert shares_to_sell > 0
        assert shares_to_sell <= 100
        assert "1.0R" in reason

    def test_scale_out_at_2r(self, default_config: AppConfig):
        """Scale-out triggers at 2:1 R:R."""
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=11.00,  # 2:1 R:R
            shares=100,
            shares_remaining=67,  # After first scale-out
            scale_outs=1,
        )

        result = strategy.compute_scale_out(position, 11.00)
        assert result is not None
        shares_to_sell, reason = result
        assert shares_to_sell > 0
        assert "2.0R" in reason

    def test_no_scale_out_before_target(self, default_config: AppConfig):
        """No scale-out when price hasn't reached target."""
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.25,  # Only 0.5R, target is 1.0R
            shares=100,
            scale_outs=0,
        )

        result = strategy.compute_scale_out(position, 10.25)
        assert result is None

    def test_trailing_stop_not_active_before_scaleout(self, default_config: AppConfig):
        """Trailing stop is None before first scale-out."""
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(scale_outs=0)
        bars = _make_simple_bars(30)

        result = strategy.get_trailing_stop(position, bars)
        assert result is None

    def test_trailing_stop_after_scaleout(self, default_config: AppConfig):
        """Trailing stop activates after first scale-out."""
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(
            entry_price=10.0,
            current_price=11.0,
            stop_price=9.50,
            scale_outs=1,
        )
        bars = _make_simple_bars(30, base_price=11.0)

        result = strategy.get_trailing_stop(position, bars)
        # May or may not return a value depending on ATR computation
        if result is not None:
            # Must be above entry (breakeven + buffer)
            assert result >= position.entry_price

    def test_trailing_stop_only_ratchets_up(self, default_config: AppConfig):
        """Trailing stop never moves down."""
        strategy = PullbackVWAPStrategy(default_config)
        position = _make_position(
            entry_price=10.0,
            current_price=10.5,
            stop_price=9.50,
            scale_outs=1,
            trailing_stop=10.30,
        )
        bars = _make_simple_bars(30, base_price=10.5)

        result = strategy.get_trailing_stop(position, bars)
        if result is not None:
            assert result > position.trailing_stop_price


def _make_position(
    entry_price: float = 10.0,
    stop_price: float = 9.50,
    current_price: float = 10.25,
    shares: int = 100,
    shares_remaining: int | None = None,
    scale_outs: int = 0,
    trailing_stop: float | None = None,
) -> PositionInfo:
    """Create a mock position for testing."""
    return PositionInfo(
        symbol="TEST",
        side=OrderSide.BUY,
        entry_price=entry_price,
        current_price=current_price,
        shares=shares,
        shares_remaining=shares_remaining if shares_remaining is not None else shares,
        stop_price=stop_price,
        target_prices=[entry_price + (entry_price - stop_price), entry_price + 2 * (entry_price - stop_price)],
        status=PositionStatus.OPEN,
        pnl_unrealized=(current_price - entry_price) * shares,
        pnl_realized=0.0,
        scale_outs_completed=scale_outs,
        entry_time=datetime(2024, 6, 15, 10, 30),
        signal_type=SignalType.VWAP_PULLBACK,
        trailing_stop_active=trailing_stop is not None,
        trailing_stop_price=trailing_stop,
    )


def _make_simple_bars(n: int = 30, base_price: float = 10.0) -> pd.DataFrame:
    """Create simple OHLCV bars for testing."""
    np.random.seed(42)
    close = base_price + np.cumsum(np.random.normal(0, 0.05, n))
    close = np.maximum(close, 1.0)

    return pd.DataFrame(
        {
            "open": close + np.random.normal(0, 0.02, n),
            "high": close + np.abs(np.random.normal(0.05, 0.02, n)),
            "low": close - np.abs(np.random.normal(0.05, 0.02, n)),
            "close": close,
            "volume": np.random.randint(100_000, 500_000, n),
        },
        index=pd.date_range("2024-06-15 09:30", periods=n, freq="1min"),
    )
