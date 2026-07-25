"""Shared test fixtures for the trading bot test suite."""

from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from trading_bot.config.settings import (
    AppConfig,
    BrokerConfig,
    DataConfig,
    EntryConfig,
    ExitConfig,
    RiskConfig,
    ScannerConfig,
)
from trading_bot.models.domain import (
    OrderSide,
    PositionInfo,
    PositionStatus,
    ScanResult,
    SignalType,
    TradeSignal,
)

# Legacy public-product route tests opt into the public surface explicitly.
# Runtime code is private by default when this future-launch gate is absent.
os.environ.setdefault("TRADING_PUBLIC_PRODUCT_ENABLED", "true")


@pytest.fixture
def default_config() -> AppConfig:
    """AppConfig with all defaults for testing (paper mode)."""
    return AppConfig(
        starting_capital=100_000.0,
        scanner=ScannerConfig(),
        risk=RiskConfig(),
        entry=EntryConfig(),
        exit=ExitConfig(),
        broker=BrokerConfig(),
        data=DataConfig(),
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    """Default risk configuration."""
    return RiskConfig()


@pytest.fixture
def scanner_config() -> ScannerConfig:
    """Default scanner configuration."""
    return ScannerConfig()


@pytest.fixture
def sample_scan_result() -> ScanResult:
    """A typical low-float momentum gapper scan result."""
    return ScanResult(
        symbol="TEST",
        price=8.50,
        gap_pct=35.0,
        relative_volume=12.5,
        float_shares=5_000_000,
        volume=5_000_000,
        prev_close=6.30,
        catalyst="FDA",
        score=0.85,
    )


@pytest.fixture
def sample_trade_signal() -> TradeSignal:
    """A validated trade signal for testing."""
    return TradeSignal(
        symbol="TEST",
        signal_type=SignalType.VWAP_PULLBACK,
        entry_price=10.00,
        stop_price=9.50,
        target_prices=[10.50, 11.00],
        atr=0.40,
        vwap=9.90,
        ema9=9.85,
        confidence=0.80,
    )


@pytest.fixture
def sample_position() -> PositionInfo:
    """An open position for testing exits and scale-outs."""
    return PositionInfo(
        symbol="TEST",
        side=OrderSide.BUY,
        entry_price=10.00,
        current_price=10.25,
        shares=100,
        shares_remaining=100,
        stop_price=9.50,
        target_prices=[10.50, 11.00],
        status=PositionStatus.OPEN,
        pnl_unrealized=25.0,
        pnl_realized=0.0,
        scale_outs_completed=0,
        entry_time=datetime(2024, 6, 15, 10, 30),
        signal_type=SignalType.VWAP_PULLBACK,
    )


@pytest.fixture
def sample_ohlcv_df() -> pd.DataFrame:
    """
    DataFrame with 100 rows of realistic 1-minute OHLCV data.

    Simulates a momentum stock that:
    - Opens at $8.00 (gap up from $6.30 prev close)
    - Spikes to $10.00 in first 20 bars
    - Pulls back to $8.80 (near VWAP)
    - Reclaims to $9.50 with volume
    - Trends up to $11.00

    This provides testable conditions for VWAP pullback strategy.
    """
    np.random.seed(42)
    n = 100

    # Create a realistic price path
    base_prices = np.concatenate(
        [
            np.linspace(8.0, 10.0, 20),  # Initial spike
            np.linspace(10.0, 8.8, 15),  # Pullback
            np.linspace(8.8, 9.5, 15),  # Recovery/reclaim
            np.linspace(9.5, 11.0, 50),  # Trend continuation
        ]
    )

    noise = np.random.normal(0, 0.05, n)
    close = base_prices + noise
    close = np.maximum(close, 2.0)  # Floor

    high = close + np.abs(np.random.normal(0.1, 0.05, n))
    low = close - np.abs(np.random.normal(0.1, 0.05, n))
    open_ = close + np.random.normal(0, 0.05, n)

    # Volume: higher during spike and reclaim, lower during pullback
    base_vol = np.concatenate(
        [
            np.full(20, 500_000),  # High volume spike
            np.full(15, 200_000),  # Low volume pullback
            np.full(15, 400_000),  # Volume on reclaim
            np.full(50, 300_000),  # Normal trend volume
        ]
    )
    volume = (base_vol + np.random.normal(0, 50_000, n)).astype(int)
    volume = np.maximum(volume, 10_000)

    dates = pd.date_range("2024-06-15 09:30", periods=n, freq="1min", tz="US/Eastern")

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=dates,
    )

    return df
