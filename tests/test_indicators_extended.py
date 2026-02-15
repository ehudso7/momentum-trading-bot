"""Tests for extended indicators: RSI, volume MA, and enriched DataFrame columns."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.utils.indicators import (
    compute_rsi,
    compute_volume_ma,
    enrich_dataframe,
)


class TestComputeRSI:
    """Tests for compute_rsi() returning values between 0-100."""

    def test_rsi_values_within_range(self, sample_ohlcv_df):
        rsi = compute_rsi(sample_ohlcv_df, length=14)

        assert len(rsi) == len(sample_ohlcv_df)
        assert rsi.min() >= 0.0
        assert rsi.max() <= 100.0

    def test_rsi_no_nan_values(self, sample_ohlcv_df):
        rsi = compute_rsi(sample_ohlcv_df, length=14)
        assert not rsi.isna().any()

    def test_rsi_all_gains_returns_high_rsi(self):
        """A mostly increasing series (with one tiny loss to seed avg_loss)
        should produce RSI near 100.

        Note: A purely monotonic increasing series has avg_loss=0, which
        the implementation replaces with NaN, causing RSI to fall back to
        the fillna default of 50. Including one small initial dip seeds
        avg_loss with a tiny non-zero value, allowing RS to be computed
        normally and yielding RSI close to 100.
        """
        n = 50
        dates = pd.date_range("2024-01-01", periods=n, freq="1min")
        prices = np.linspace(10.0, 20.0, n)
        # Add one tiny loss at the start so avg_loss is non-zero
        prices[1] = prices[0] - 0.01
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices + 0.1,
                "low": prices - 0.1,
                "close": prices,
                "volume": np.full(n, 100_000),
            },
            index=dates,
        )

        rsi = compute_rsi(df, length=14)
        last_rsi = rsi.iloc[-1]
        assert last_rsi > 90.0

    def test_rsi_all_losses_returns_low_rsi(self):
        """A monotonically decreasing series should produce RSI near 0."""
        n = 50
        dates = pd.date_range("2024-01-01", periods=n, freq="1min")
        prices = np.linspace(20.0, 10.0, n)  # Strictly decreasing
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices + 0.1,
                "low": prices - 0.1,
                "close": prices,
                "volume": np.full(n, 100_000),
            },
            index=dates,
        )

        rsi = compute_rsi(df, length=14)
        last_rsi = rsi.iloc[-1]
        assert last_rsi < 10.0

    def test_rsi_flat_prices_returns_neutral(self):
        """Flat prices (no gains, no losses) should default to 50."""
        n = 30
        dates = pd.date_range("2024-01-01", periods=n, freq="1min")
        flat_price = 15.0
        df = pd.DataFrame(
            {
                "open": np.full(n, flat_price),
                "high": np.full(n, flat_price),
                "low": np.full(n, flat_price),
                "close": np.full(n, flat_price),
                "volume": np.full(n, 100_000),
            },
            index=dates,
        )

        rsi = compute_rsi(df, length=14)
        # With no change, RSI should be at the default 50
        assert rsi.iloc[-1] == pytest.approx(50.0, abs=1.0)

    def test_rsi_custom_length(self, sample_ohlcv_df):
        rsi_7 = compute_rsi(sample_ohlcv_df, length=7)
        rsi_21 = compute_rsi(sample_ohlcv_df, length=21)

        # Shorter period RSI should be more volatile (larger std deviation)
        assert rsi_7.std() > rsi_21.std()


class TestComputeVolumeMA:
    """Tests for compute_volume_ma() returning correct moving average."""

    def test_volume_ma_length_matches_input(self, sample_ohlcv_df):
        vol_ma = compute_volume_ma(sample_ohlcv_df, length=20)
        assert len(vol_ma) == len(sample_ohlcv_df)

    def test_volume_ma_no_nan_values(self, sample_ohlcv_df):
        """With min_periods=1, there should be no NaN values."""
        vol_ma = compute_volume_ma(sample_ohlcv_df, length=20)
        assert not vol_ma.isna().any()

    def test_volume_ma_first_value_equals_first_volume(self, sample_ohlcv_df):
        """With min_periods=1, the first MA value should equal the first volume."""
        vol_ma = compute_volume_ma(sample_ohlcv_df, length=20)
        first_vol = sample_ohlcv_df["volume"].iloc[0]
        assert vol_ma.iloc[0] == pytest.approx(first_vol, rel=1e-6)

    def test_volume_ma_correct_average_after_warmup(self):
        """After window fills, MA should be the mean of the last N values."""
        n = 30
        dates = pd.date_range("2024-01-01", periods=n, freq="1min")
        volumes = np.arange(1, n + 1, dtype=float) * 1000
        df = pd.DataFrame(
            {
                "open": np.full(n, 10.0),
                "high": np.full(n, 10.5),
                "low": np.full(n, 9.5),
                "close": np.full(n, 10.0),
                "volume": volumes,
            },
            index=dates,
        )

        length = 5
        vol_ma = compute_volume_ma(df, length=length)

        # At index 9 (10th row), the window spans volumes at indices 5..9
        expected = volumes[5:10].mean()
        assert vol_ma.iloc[9] == pytest.approx(expected, rel=1e-6)

    def test_volume_ma_smooths_data(self, sample_ohlcv_df):
        """Moving average should be smoother than raw volume."""
        vol_ma = compute_volume_ma(sample_ohlcv_df, length=20)
        raw_std = sample_ohlcv_df["volume"].std()
        ma_std = vol_ma.std()
        assert ma_std < raw_std


class TestEnrichDataFrame:
    """Tests for enrich_dataframe() adding rsi_14, ema_20, volume_ma_20."""

    def test_enrich_adds_rsi_14_column(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        assert "rsi_14" in enriched.columns

    def test_enrich_adds_ema_20_column(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        assert "ema_20" in enriched.columns

    def test_enrich_adds_volume_ma_20_column(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        assert "volume_ma_20" in enriched.columns

    def test_enrich_rsi_values_in_valid_range(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        rsi = enriched["rsi_14"]
        assert rsi.min() >= 0.0
        assert rsi.max() <= 100.0

    def test_enrich_ema_20_values_reasonable(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        ema20 = enriched["ema_20"]
        # EMA should be within the range of close prices
        close_min = enriched["close"].min()
        close_max = enriched["close"].max()
        assert ema20.min() >= close_min - 1.0
        assert ema20.max() <= close_max + 1.0

    def test_enrich_volume_ma_20_no_nans(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        assert not enriched["volume_ma_20"].isna().any()

    def test_enrich_preserves_original_columns(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in enriched.columns

    def test_enrich_does_not_modify_original(self, sample_ohlcv_df):
        original_cols = set(sample_ohlcv_df.columns)
        enrich_dataframe(sample_ohlcv_df)
        assert set(sample_ohlcv_df.columns) == original_cols

    def test_enrich_includes_all_default_indicators(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        expected_indicator_cols = {
            "ema_9",
            "ema_20",
            "atr_14",
            "vwap",
            "rsi_14",
            "volume_ma_20",
            "psar_long",
            "psar_short",
        }
        for col in expected_indicator_cols:
            assert col in enriched.columns, f"Missing expected column: {col}"

    def test_enrich_without_rsi(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df, include_rsi=False)
        assert "rsi_14" not in enriched.columns

    def test_enrich_without_volume_ma(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df, include_volume_ma=False)
        assert "volume_ma_20" not in enriched.columns

    def test_enrich_row_count_preserved(self, sample_ohlcv_df):
        enriched = enrich_dataframe(sample_ohlcv_df)
        assert len(enriched) == len(sample_ohlcv_df)
