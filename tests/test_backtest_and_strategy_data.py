"""Tests for backtesting and strategy data enhancements."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategies.premarket_profile import PremarketProfile
from trading_bot.strategies.setup_tracker import SetupTracker


class TestPremarketProfile:
    def _make_bars(self, prices, volumes):
        n = len(prices)
        return pd.DataFrame({
            "open": prices,
            "high": [p + 0.1 for p in prices],
            "low": [p - 0.1 for p in prices],
            "close": prices,
            "volume": volumes,
        })

    def test_tight_consolidation_boosts(self):
        profile = PremarketProfile()
        bars = self._make_bars([10.0, 10.1, 10.05, 10.08, 10.12], [100000]*5)
        score = profile.score(bars)
        assert score >= 1.0

    def test_volatile_premarket_penalizes(self):
        profile = PremarketProfile()
        bars = pd.DataFrame({
            "open": [10.0, 11.0, 9.0, 12.0, 8.0],
            "high": [11.5, 12.0, 10.5, 13.0, 9.5],
            "low": [9.0, 9.5, 8.0, 10.0, 7.0],
            "close": [11.0, 9.5, 10.0, 8.5, 9.0],
            "volume": [500000, 100000, 800000, 50000, 200000],
        })
        score = profile.score(bars)
        assert score < 1.0

    def test_empty_bars_neutral(self):
        profile = PremarketProfile()
        assert profile.score(pd.DataFrame()) == 1.0
        assert profile.score(None) == 1.0

    def test_result_clamped(self):
        profile = PremarketProfile()
        bars = self._make_bars([10.0]*5, [100000]*5)
        score = profile.score(bars)
        assert 0.7 <= score <= 1.3

    def test_vwap_distance(self):
        profile = PremarketProfile()
        bars = self._make_bars([10.0, 10.2, 10.4, 10.6, 10.8], [100000]*5)
        dist = profile.get_vwap_distance(bars)
        assert dist is not None


class TestSetupTracker:
    def test_no_data_returns_neutral(self):
        tracker = SetupTracker()
        assert tracker.get_confidence_multiplier("vwap_pullback", "bullish") == 1.0

    def test_high_win_rate_boosts(self):
        tracker = SetupTracker(min_trades=5)
        for _ in range(8):
            tracker.record_trade("vwap_pullback", "bullish", 100)
        for _ in range(2):
            tracker.record_trade("vwap_pullback", "bullish", -50)
        mult = tracker.get_confidence_multiplier("vwap_pullback", "bullish")
        assert mult > 1.0

    def test_low_win_rate_penalizes(self):
        tracker = SetupTracker(min_trades=5)
        for _ in range(2):
            tracker.record_trade("breakout", "bearish", 100)
        for _ in range(8):
            tracker.record_trade("breakout", "bearish", -50)
        mult = tracker.get_confidence_multiplier("breakout", "bearish")
        assert mult < 1.0

    def test_falls_back_to_global(self):
        tracker = SetupTracker(min_trades=5)
        for _ in range(4):
            tracker.record_trade("ema", "bullish", 100)
        # Not enough regime-specific, add global
        for _ in range(6):
            tracker.record_trade("ema", "bearish", 100)
        mult = tracker.get_confidence_multiplier("ema", "low_vol")
        assert mult > 1.0  # Falls back to global

    def test_win_rate_calculation(self):
        tracker = SetupTracker()
        tracker.record_trade("vwap", "bull", 100)
        tracker.record_trade("vwap", "bull", -50)
        assert tracker.get_win_rate("vwap") == 50.0

    def test_get_stats(self):
        tracker = SetupTracker()
        tracker.record_trade("vwap", "bull", 100)
        tracker.record_trade("vwap", "bull", -50)
        stats = tracker.get_stats()
        assert "vwap" in stats
        assert stats["vwap"]["total_trades"] == 2

    def test_reset(self):
        tracker = SetupTracker()
        tracker.record_trade("vwap", "bull", 100)
        tracker.reset()
        assert tracker.get_stats() == {}

    def test_result_clamped(self):
        tracker = SetupTracker(min_trades=3)
        for _ in range(10):
            tracker.record_trade("vwap", "bull", 1000)
        mult = tracker.get_confidence_multiplier("vwap", "bull")
        assert 0.7 <= mult <= 1.3


class TestBacktestSlippage:
    def test_engine_uses_slippage(self):
        """Verify BacktestEngine has slippage model."""
        from trading_bot.backtest.engine import BacktestEngine
        from trading_bot.config.settings import AppConfig
        config = AppConfig(starting_capital=100_000.0)
        engine = BacktestEngine(config)
        assert hasattr(engine, '_slippage')
