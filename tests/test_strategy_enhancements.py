"""Tests for strategy enhancement modules."""
from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.strategies.multi_timeframe import MultiTimeframeConfirm
from trading_bot.strategies.volume_profile import VolumeExhaustionDetector
from trading_bot.strategies.microstructure import MicrostructureScorer


class TestMultiTimeframeConfirm:
    def test_aligned_uptrend_boosts(self):
        """Both 5-min and daily aligned bullish should boost."""
        mtf = MultiTimeframeConfirm()
        # Create uptrending 5-min data
        bars_5min = pd.DataFrame({
            "open": np.linspace(10, 15, 30),
            "high": np.linspace(10.5, 15.5, 30),
            "low": np.linspace(9.5, 14.5, 30),
            "close": np.linspace(10, 15, 30),
            "volume": [100000] * 30,
        })
        bars_daily = pd.DataFrame({
            "open": np.linspace(8, 14, 30),
            "high": np.linspace(8.5, 14.5, 30),
            "low": np.linspace(7.5, 13.5, 30),
            "close": np.linspace(8, 14, 30),
            "volume": [1000000] * 30,
        })
        result = mtf.confirm(bars_5min, bars_daily)
        assert result > 1.0  # Should boost

    def test_empty_bars_returns_neutral(self):
        mtf = MultiTimeframeConfirm()
        result = mtf.confirm(pd.DataFrame(), pd.DataFrame())
        assert result == 1.0

    def test_none_bars_returns_neutral(self):
        mtf = MultiTimeframeConfirm()
        result = mtf.confirm(None, None)
        assert result == 1.0

    def test_downtrend_penalizes(self):
        mtf = MultiTimeframeConfirm()
        bars_5min = pd.DataFrame({
            "open": np.linspace(15, 10, 30),
            "high": np.linspace(15.5, 10.5, 30),
            "low": np.linspace(14.5, 9.5, 30),
            "close": np.linspace(15, 10, 30),
            "volume": [100000] * 30,
        })
        bars_daily = pd.DataFrame({
            "open": np.linspace(14, 8, 30),
            "high": np.linspace(14.5, 8.5, 30),
            "low": np.linspace(13.5, 7.5, 30),
            "close": np.linspace(14, 8, 30),
            "volume": [1000000] * 30,
        })
        result = mtf.confirm(bars_5min, bars_daily)
        assert result < 1.0

    def test_result_clamped(self):
        mtf = MultiTimeframeConfirm()
        bars = pd.DataFrame({
            "open": np.linspace(10, 15, 30),
            "high": np.linspace(10.5, 15.5, 30),
            "low": np.linspace(9.5, 14.5, 30),
            "close": np.linspace(10, 15, 30),
            "volume": [100000] * 30,
        })
        result = mtf.confirm(bars, bars)
        assert 0.5 <= result <= 1.2


class TestVolumeExhaustionDetector:
    def _make_bars(self, initial_vol, recent_vol, n_bars=30):
        return pd.DataFrame({
            "open": [10.0] * n_bars,
            "high": [10.5] * n_bars,
            "low": [9.5] * n_bars,
            "close": [10.0] * n_bars,
            "volume": [initial_vol] * 15 + [recent_vol] * (n_bars - 15),
        })

    def test_exhausted_when_volume_drops(self):
        detector = VolumeExhaustionDetector()
        bars = self._make_bars(1000000, 40000)  # 4% < 5% threshold
        assert detector.is_exhausted(bars) is True

    def test_not_exhausted_when_volume_steady(self):
        detector = VolumeExhaustionDetector()
        bars = self._make_bars(500000, 400000)
        assert detector.is_exhausted(bars) is False

    def test_not_exhausted_with_short_bars(self):
        detector = VolumeExhaustionDetector()
        bars = pd.DataFrame({
            "open": [10.0] * 10,
            "high": [10.5] * 10,
            "low": [9.5] * 10,
            "close": [10.0] * 10,
            "volume": [100000] * 10,
        })
        assert detector.is_exhausted(bars) is False

    def test_empty_bars(self):
        detector = VolumeExhaustionDetector()
        assert detector.is_exhausted(pd.DataFrame()) is False
        assert detector.is_exhausted(None) is False

    def test_volume_ratio(self):
        detector = VolumeExhaustionDetector()
        bars = self._make_bars(1000000, 500000)
        ratio = detector.get_volume_ratio(bars)
        assert 0.4 < ratio < 0.6


class TestMicrostructureScorer:
    def test_normal_market_near_neutral(self):
        scorer = MicrostructureScorer()
        bars = pd.DataFrame({
            "open": [10.0] * 15,
            "high": [10.03] * 15,
            "low": [9.97] * 15,
            "close": [10.0] * 15,
            "volume": [200000] * 15,
        })
        result = scorer.score(bars, 10.0)
        assert 0.9 <= result <= 1.1

    def test_wide_spread_penalizes(self):
        scorer = MicrostructureScorer()
        bars = pd.DataFrame({
            "open": [10.0] * 15,
            "high": [10.50] * 15,  # 5% range
            "low": [9.50] * 15,
            "close": [10.0] * 15,
            "volume": [200000] * 15,
        })
        result = scorer.score(bars, 10.0)
        assert result < 0.9

    def test_thin_volume_penalizes(self):
        scorer = MicrostructureScorer()
        bars = pd.DataFrame({
            "open": [10.0] * 15,
            "high": [10.03] * 15,
            "low": [9.97] * 15,
            "close": [10.0] * 15,
            "volume": [5000] * 15,  # Very thin
        })
        result = scorer.score(bars, 10.0)
        assert result < 0.9

    def test_empty_bars_returns_neutral(self):
        scorer = MicrostructureScorer()
        assert scorer.score(pd.DataFrame(), 10.0) == 1.0
        assert scorer.score(None, 10.0) == 1.0

    def test_result_clamped(self):
        scorer = MicrostructureScorer()
        bars = pd.DataFrame({
            "open": [10.0] * 15,
            "high": [10.03] * 15,
            "low": [9.97] * 15,
            "close": [10.0] * 15,
            "volume": [200000] * 15,
        })
        result = scorer.score(bars, 10.0)
        assert 0.5 <= result <= 1.1
