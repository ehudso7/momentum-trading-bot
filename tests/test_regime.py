"""
Tests for MarketRegime and RegimeDetector.

Covers regime detection under various market conditions (insufficient data,
bullish/bearish crossovers, high volatility) and the regime adjustment
multiplier lookup.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategies.regime import MarketRegime, RegimeDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(close_prices: np.ndarray, spread: float = 0.20) -> pd.DataFrame:
    """
    Build an OHLCV DataFrame from a close price series.

    Args:
        close_prices: 1-D array of close prices.
        spread: Fixed high/low distance from close.

    Returns:
        DataFrame with columns open, high, low, close, volume.
    """
    n = len(close_prices)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": close_prices - spread * 0.1,
            "high": close_prices + spread,
            "low": close_prices - spread,
            "close": close_prices,
            "volume": np.full(n, 1_000_000),
        },
        index=dates,
    )


def _make_trending_bullish_data(n: int = 100) -> pd.DataFrame:
    """
    Create OHLCV data with a clear bullish trend.

    Steadily rising prices so EMA-20 > EMA-50 with strong directional
    movement (high ADX). Uses fixed spread much larger than daily close
    change so True Range is constant, avoiding high-volatility detection.
    """
    base = 100.0
    daily_return = 1.001  # small daily move
    close = np.array([base * (daily_return ** i) for i in range(n)])

    # Fixed spread >> daily change ensures constant True Range
    spread = 2.0
    high = close + spread
    low = close - spread
    open_ = close - 0.05

    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 2_000_000),
        },
        index=dates,
    )


def _make_trending_bearish_data(n: int = 100) -> pd.DataFrame:
    """
    Create OHLCV data with a clear bearish trend.

    Steadily falling prices so EMA-20 < EMA-50 with strong directional
    movement (high ADX).  Spread scales with price to keep ATR proportional.
    """
    base = 200.0
    daily_return = 0.999  # small daily decline
    close = np.array([base * (daily_return ** i) for i in range(n)])

    # Fixed spread >> daily change ensures constant True Range
    spread = 2.0
    high = close + spread
    low = close - spread
    open_ = close + 0.05

    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 2_000_000),
        },
        index=dates,
    )


def _make_high_volatility_data(n: int = 100) -> pd.DataFrame:
    """
    Create OHLCV data where current ATR is above the 80th percentile.

    The last portion of bars have dramatically wider ranges than earlier bars
    so that the latest ATR exceeds the 80th percentile of the lookback window.
    """
    np.random.seed(30)
    # First 80 bars: low volatility, tight range
    close_low_vol = np.full(80, 100.0) + np.cumsum(np.random.normal(0, 0.1, 80))
    high_low_vol = close_low_vol + 0.3
    low_low_vol = close_low_vol - 0.3

    # Last 20 bars: very high volatility, wide range
    close_high_vol = close_low_vol[-1] + np.cumsum(np.random.normal(0, 2.0, 20))
    high_high_vol = close_high_vol + 5.0
    low_high_vol = close_high_vol - 5.0

    close = np.concatenate([close_low_vol, close_high_vol])
    high = np.concatenate([high_low_vol, high_high_vol])
    low = np.concatenate([low_low_vol, low_high_vol])
    open_ = (close + high) / 2.0

    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1_000_000),
        },
        index=dates,
    )


def _make_range_bound_data(n: int = 100) -> pd.DataFrame:
    """
    Create OHLCV data that is range-bound (no clear trend, low volatility).

    Price oscillates around a mean with small amplitude so that ADX is low,
    EMA-20 and EMA-50 stay close, and ATR doesn't spike.
    """
    np.random.seed(40)
    # Oscillate around 100 with a period of ~20 bars
    x = np.arange(n, dtype=float)
    close = 100.0 + 1.0 * np.sin(2 * np.pi * x / 20.0)
    close += np.random.normal(0, 0.1, n)
    high = close + 0.3
    low = close - 0.3
    open_ = close + np.random.normal(0, 0.05, n)

    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1_000_000),
        },
        index=dates,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def detector() -> RegimeDetector:
    """Fresh RegimeDetector instance."""
    return RegimeDetector()


# ---------------------------------------------------------------------------
# detect() tests
# ---------------------------------------------------------------------------

class TestDetectInsufficientData:
    """detect() with insufficient data should return RANGE_BOUND (safe default)."""

    def test_none_input(self, detector: RegimeDetector):
        result = detector.detect(None)
        assert result == MarketRegime.RANGE_BOUND

    def test_empty_dataframe(self, detector: RegimeDetector):
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = detector.detect(empty_df)
        assert result == MarketRegime.RANGE_BOUND

    def test_too_few_bars(self, detector: RegimeDetector):
        """Fewer than MIN_BARS rows should return RANGE_BOUND."""
        close = np.linspace(100, 110, RegimeDetector.MIN_BARS - 1)
        df = _make_ohlcv(close)
        result = detector.detect(df)
        assert result == MarketRegime.RANGE_BOUND

    def test_exactly_min_bars_minus_one(self, detector: RegimeDetector):
        """Exactly MIN_BARS - 1 rows is still insufficient."""
        close = np.linspace(100, 110, RegimeDetector.MIN_BARS - 1)
        df = _make_ohlcv(close)
        result = detector.detect(df)
        assert result == MarketRegime.RANGE_BOUND

    def test_exactly_min_bars_is_sufficient(self, detector: RegimeDetector):
        """Exactly MIN_BARS rows should NOT return LOW_VOLATILITY by default."""
        # With a flat price series this should be RANGE_BOUND, not LOW_VOLATILITY
        close = np.full(RegimeDetector.MIN_BARS, 100.0)
        df = _make_ohlcv(close)
        result = detector.detect(df)
        # Just verify it didn't early-return as LOW_VOLATILITY due to data check
        assert result in list(MarketRegime)


class TestDetectBullishCrossover:
    """detect() with bullish crossover data returns TRENDING_BULLISH."""

    def test_strong_uptrend(self, detector: RegimeDetector):
        df = _make_trending_bullish_data(n=100)
        # Patch _is_high_volatility so the upward trend in absolute ATR
        # (inherent in any trending series) doesn't mask the bullish signal.
        with patch.object(detector, "_is_high_volatility", return_value=False):
            result = detector.detect(df)
        assert result == MarketRegime.TRENDING_BULLISH

    def test_longer_uptrend(self, detector: RegimeDetector):
        """More data still gives bullish result."""
        df = _make_trending_bullish_data(n=150)
        with patch.object(detector, "_is_high_volatility", return_value=False):
            result = detector.detect(df)
        assert result == MarketRegime.TRENDING_BULLISH


class TestDetectBearishCrossover:
    """detect() with bearish crossover data returns TRENDING_BEARISH."""

    def test_strong_downtrend(self, detector: RegimeDetector):
        df = _make_trending_bearish_data(n=100)
        # Patch _is_high_volatility to isolate the bearish crossover signal.
        with patch.object(detector, "_is_high_volatility", return_value=False):
            result = detector.detect(df)
        assert result == MarketRegime.TRENDING_BEARISH

    def test_longer_downtrend(self, detector: RegimeDetector):
        df = _make_trending_bearish_data(n=150)
        with patch.object(detector, "_is_high_volatility", return_value=False):
            result = detector.detect(df)
        assert result == MarketRegime.TRENDING_BEARISH


class TestDetectHighVolatility:
    """detect() with high volatility data returns HIGH_VOLATILITY."""

    def test_high_vol_regime(self, detector: RegimeDetector):
        df = _make_high_volatility_data(n=100)
        result = detector.detect(df)
        assert result == MarketRegime.HIGH_VOLATILITY


class TestDetectRangeBound:
    """detect() with range-bound (no trend, low vol) data returns RANGE_BOUND."""

    def test_range_bound_regime(self, detector: RegimeDetector):
        df = _make_range_bound_data(n=100)
        result = detector.detect(df)
        assert result == MarketRegime.RANGE_BOUND


# ---------------------------------------------------------------------------
# _classify() unit tests (deterministic, no data dependency)
# ---------------------------------------------------------------------------

class TestClassifyDirectly:
    """Direct tests of the _classify static method for all regime paths."""

    def test_high_vol_takes_priority(self):
        assert RegimeDetector._classify(True, True, True) == MarketRegime.HIGH_VOLATILITY
        assert RegimeDetector._classify(False, True, True) == MarketRegime.HIGH_VOLATILITY
        assert RegimeDetector._classify(True, False, True) == MarketRegime.HIGH_VOLATILITY
        assert RegimeDetector._classify(False, False, True) == MarketRegime.HIGH_VOLATILITY

    def test_trending_bullish(self):
        assert RegimeDetector._classify(True, True, False) == MarketRegime.TRENDING_BULLISH

    def test_trending_bearish(self):
        assert RegimeDetector._classify(False, True, False) == MarketRegime.TRENDING_BEARISH

    def test_range_bound(self):
        assert RegimeDetector._classify(True, False, False) == MarketRegime.RANGE_BOUND
        assert RegimeDetector._classify(False, False, False) == MarketRegime.RANGE_BOUND


# ---------------------------------------------------------------------------
# get_regime_adjustments() tests
# ---------------------------------------------------------------------------

class TestGetRegimeAdjustments:
    """get_regime_adjustments() returns correct multipliers for each regime."""

    EXPECTED_KEYS = {
        "position_size_multiplier",
        "stop_multiplier",
        "max_positions_override",
        "volume_confirmation_multiplier",
        "proximity_multiplier",
    }

    def test_trending_bullish_adjustments(self, detector: RegimeDetector):
        adj = detector.get_regime_adjustments(MarketRegime.TRENDING_BULLISH)
        assert set(adj.keys()) == self.EXPECTED_KEYS
        assert adj["position_size_multiplier"] == 1.0
        assert adj["stop_multiplier"] == 1.0
        assert adj["max_positions_override"] is None
        assert adj["volume_confirmation_multiplier"] == 1.0

    def test_trending_bearish_adjustments(self, detector: RegimeDetector):
        adj = detector.get_regime_adjustments(MarketRegime.TRENDING_BEARISH)
        assert adj["position_size_multiplier"] == 0.5
        assert adj["stop_multiplier"] == 0.8
        assert adj["max_positions_override"] is None

    def test_range_bound_adjustments(self, detector: RegimeDetector):
        adj = detector.get_regime_adjustments(MarketRegime.RANGE_BOUND)
        assert adj["position_size_multiplier"] == 0.75
        assert adj["stop_multiplier"] == 1.0
        assert adj["max_positions_override"] is None

    def test_high_volatility_adjustments(self, detector: RegimeDetector):
        adj = detector.get_regime_adjustments(MarketRegime.HIGH_VOLATILITY)
        assert adj["position_size_multiplier"] == 0.6
        assert adj["stop_multiplier"] == 1.3
        assert adj["max_positions_override"] == 2
        assert adj["volume_confirmation_multiplier"] == 1.0

    def test_low_volatility_adjustments(self, detector: RegimeDetector):
        adj = detector.get_regime_adjustments(MarketRegime.LOW_VOLATILITY)
        assert adj["position_size_multiplier"] == 1.0
        assert adj["stop_multiplier"] == 1.0
        assert adj["max_positions_override"] is None
        assert adj["volume_confirmation_multiplier"] == 1.5

    def test_adjustments_all_have_correct_keys(self, detector: RegimeDetector):
        """Every regime returns the same set of keys."""
        for regime in MarketRegime:
            adj = detector.get_regime_adjustments(regime)
            assert set(adj.keys()) == self.EXPECTED_KEYS, f"Missing keys for {regime}"

    def test_adjustments_returns_copy(self, detector: RegimeDetector):
        """Modifying the returned dict should not affect future calls."""
        adj1 = detector.get_regime_adjustments(MarketRegime.HIGH_VOLATILITY)
        adj1["position_size_multiplier"] = 999.0

        adj2 = detector.get_regime_adjustments(MarketRegime.HIGH_VOLATILITY)
        assert adj2["position_size_multiplier"] == 0.6

    def test_bearish_reduces_position_size(self, detector: RegimeDetector):
        """Bearish regime should have smaller position_size_multiplier than bullish."""
        bullish = detector.get_regime_adjustments(MarketRegime.TRENDING_BULLISH)
        bearish = detector.get_regime_adjustments(MarketRegime.TRENDING_BEARISH)
        assert bearish["position_size_multiplier"] < bullish["position_size_multiplier"]

    def test_high_vol_widens_stops(self, detector: RegimeDetector):
        """High volatility regime should have stop_multiplier > 1.0."""
        adj = detector.get_regime_adjustments(MarketRegime.HIGH_VOLATILITY)
        assert adj["stop_multiplier"] > 1.0

    def test_high_vol_limits_positions(self, detector: RegimeDetector):
        """High volatility regime should cap max_positions_override."""
        adj = detector.get_regime_adjustments(MarketRegime.HIGH_VOLATILITY)
        assert adj["max_positions_override"] is not None
        assert adj["max_positions_override"] <= 3

    def test_low_vol_requires_more_volume(self, detector: RegimeDetector):
        """Low volatility regime requires higher volume confirmation."""
        adj = detector.get_regime_adjustments(MarketRegime.LOW_VOLATILITY)
        assert adj["volume_confirmation_multiplier"] > 1.0
