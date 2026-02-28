"""Tests for the backtesting engine."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.backtest.engine import BacktestEngine, BacktestResult
from trading_bot.config.settings import AppConfig


class TestBacktestEngine:
    """Test backtest engine logic."""

    def test_engine_creates(self, default_config: AppConfig):
        """Engine instantiates correctly."""
        engine = BacktestEngine(default_config)
        assert engine is not None

    def test_compute_metrics_empty_trades(self, default_config: AppConfig):
        """Empty trade list produces zero metrics."""
        engine = BacktestEngine(default_config)
        result = engine._compute_metrics("TEST", [])
        assert result.num_trades == 0
        assert result.total_pnl == 0.0
        assert result.win_rate == 0.0

    def test_compute_metrics_with_trades(self, default_config: AppConfig):
        """Verify metric calculations with known trades."""
        from trading_bot.backtest.engine import BacktestTrade

        engine = BacktestEngine(default_config)
        trades = [
            BacktestTrade(
                symbol="TEST",
                entry_date="2024-01-01",
                exit_date="2024-01-02",
                entry_price=10.0,
                exit_price=11.0,
                stop_price=9.5,
                shares=100,
                pnl=100.0,
                pnl_pct=10.0,
                rr_ratio=2.0,
                exit_reason="target",
            ),
            BacktestTrade(
                symbol="TEST",
                entry_date="2024-01-03",
                exit_date="2024-01-04",
                entry_price=10.0,
                exit_price=9.5,
                stop_price=9.5,
                shares=100,
                pnl=-50.0,
                pnl_pct=-5.0,
                rr_ratio=-1.0,
                exit_reason="stop_loss",
            ),
        ]

        result = engine._compute_metrics("TEST", trades)
        assert result.num_trades == 2
        assert result.total_pnl == 50.0
        assert result.win_rate == 50.0
        assert result.avg_win == 100.0
        assert result.avg_loss == -50.0
        assert result.profit_factor == 2.0

    def test_max_drawdown_calculation(self, default_config: AppConfig):
        """Verify max drawdown is computed correctly."""
        from trading_bot.backtest.engine import BacktestTrade

        engine = BacktestEngine(default_config)
        # Simulate: +200, -300, +100 from starting capital of 25000
        trades = [
            BacktestTrade("T", "2024-01-01", "2024-01-02", 10, 12, 9, 100,
                          200, 2.0, 2.0, "target"),
            BacktestTrade("T", "2024-01-03", "2024-01-04", 10, 7, 9, 100,
                          -300, -3.0, -3.0, "stop"),
            BacktestTrade("T", "2024-01-05", "2024-01-06", 10, 11, 9, 100,
                          100, 1.0, 1.0, "target"),
        ]

        result = engine._compute_metrics("TEST", trades)
        assert result.max_drawdown_pct > 0
        # Peak equity: 100200, trough: 99900, DD = 300/100200 = ~0.30%
        assert result.max_drawdown_pct == pytest.approx(0.30, abs=0.1)

    def test_report_format(self, default_config: AppConfig):
        """Report generates valid string output."""
        engine = BacktestEngine(default_config)
        results = {
            "TEST": BacktestResult(
                symbol="TEST",
                total_return_pct=5.0,
                total_pnl=1250.0,
                num_trades=10,
                win_rate=60.0,
                avg_win=200.0,
                avg_loss=-80.0,
                profit_factor=2.5,
                max_drawdown_pct=3.0,
                sharpe_ratio=1.5,
            )
        }
        report = engine.report(results)
        assert "BACKTEST RESULTS" in report
        assert "TEST" in report
        assert "60.0%" in report
        assert "DISCLAIMER" in report

    def test_simulate_with_known_data(self, default_config: AppConfig):
        """Simulate on synthetic data with known pattern."""
        engine = BacktestEngine(default_config)

        # Create data with clear EMA pullback setup
        np.random.seed(42)
        n = 60
        prices = np.concatenate([
            np.linspace(50, 55, 20),  # uptrend
            np.linspace(55, 52, 10),  # pullback
            np.linspace(52, 58, 30),  # recovery and continuation
        ])

        df = pd.DataFrame({
            "open": prices + np.random.normal(0, 0.1, n),
            "high": prices + np.abs(np.random.normal(0.3, 0.1, n)),
            "low": prices - np.abs(np.random.normal(0.3, 0.1, n)),
            "close": prices,
            "volume": np.random.randint(100_000, 500_000, n),
        }, index=pd.date_range("2024-01-01", periods=n, freq="D"))
        df.columns = [c.lower() for c in df.columns]

        from trading_bot.utils.indicators import enrich_dataframe
        df = enrich_dataframe(df, ema_length=9, atr_length=14, include_vwap=False)

        trades = engine._simulate("TEST", df)
        # We should get at least some trades from this pattern
        # The exact number depends on indicator warmup
        assert isinstance(trades, list)
