"""Tests for config validation hardening."""

from __future__ import annotations

import pytest

from trading_bot.config.settings import (
    AppConfig,
    BrokerConfig,
    ExitConfig,
    RiskConfig,
    RunMode,
    ScannerConfig,
)


class TestScaleOutValidation:
    """Test scale-out ratio validation."""

    def test_negative_ratio_rejected(self):
        """Scale-out ratios must all be positive."""
        with pytest.raises(ValueError, match="positive"):
            ExitConfig(
                scale_out_ratios=[-0.333, 1.333, 0.0],
                scale_out_rr_targets=[1.0, 2.0],
            )

    def test_zero_ratio_rejected(self):
        """Zero ratios are not allowed."""
        with pytest.raises(ValueError, match="positive"):
            ExitConfig(
                scale_out_ratios=[0.0, 0.5, 0.5],
                scale_out_rr_targets=[1.0, 2.0],
            )

    def test_unsorted_targets_rejected(self):
        """R:R targets must be in ascending order."""
        with pytest.raises(ValueError, match="ascending"):
            ExitConfig(
                scale_out_ratios=[0.333, 0.333, 0.334],
                scale_out_rr_targets=[2.0, 1.0],
            )

    def test_valid_config_passes(self):
        """Normal config passes all validation."""
        cfg = ExitConfig(
            scale_out_ratios=[0.333, 0.333, 0.334],
            scale_out_rr_targets=[1.0, 2.0],
        )
        assert len(cfg.scale_out_ratios) == 3

    def test_sum_not_one_rejected(self):
        """Ratios that don't sum to ~1.0 are rejected."""
        with pytest.raises(ValueError, match="sum to"):
            ExitConfig(
                scale_out_ratios=[0.5, 0.5, 0.5],
                scale_out_rr_targets=[1.0, 2.0],
            )


class TestAppConfigSafety:
    """Test cross-field safety validation."""

    def test_live_mode_requires_non_paper(self):
        """Live mode with paper=true is rejected."""
        with pytest.raises(ValueError, match="alpaca_paper"):
            AppConfig(
                run_mode=RunMode.LIVE,
                broker=BrokerConfig(alpaca_paper=True),
            )

    def test_live_mode_rejects_empty_key(self):
        """Live mode with empty API key is rejected."""
        with pytest.raises(ValueError, match="valid Alpaca API key"):
            AppConfig(
                run_mode=RunMode.LIVE,
                broker=BrokerConfig(
                    alpaca_paper=False,
                    alpaca_api_key="",
                    alpaca_api_secret="real_secret",
                ),
            )

    def test_live_mode_rejects_placeholder_key(self):
        """Live mode with placeholder API key is rejected."""
        with pytest.raises(ValueError, match="placeholder"):
            AppConfig(
                run_mode=RunMode.LIVE,
                broker=BrokerConfig(
                    alpaca_paper=False,
                    alpaca_api_key="your_alpaca_api_key_here",
                    alpaca_api_secret="real_secret",
                ),
            )

    def test_excessive_exposure_rejected(self):
        """Total risk exposure exceeding 2x daily limit is rejected."""
        with pytest.raises(ValueError, match="exposure"):
            AppConfig(
                risk=RiskConfig(
                    risk_per_trade_pct=3.0,
                    max_open_positions=10,
                    hard_daily_loss_limit_pct=5.0,
                ),
            )

    def test_invalid_price_range_rejected(self):
        """Scanner min_price >= max_price is rejected."""
        with pytest.raises(ValueError, match="min_price"):
            AppConfig(
                scanner=ScannerConfig(min_price=25.0, max_price=20.0),
            )

    def test_paper_mode_passes_without_keys(self):
        """Paper mode works without API keys."""
        cfg = AppConfig(run_mode=RunMode.PAPER)
        assert cfg.run_mode == RunMode.PAPER


class TestRiskBounds:
    """Test that risk parameter bounds are enforced."""

    def test_risk_per_trade_max_3pct(self):
        """risk_per_trade_pct upper bound is 3%."""
        with pytest.raises(Exception):
            RiskConfig(risk_per_trade_pct=5.0)

    def test_risk_per_trade_min_01pct(self):
        """risk_per_trade_pct lower bound is 0.1%."""
        with pytest.raises(Exception):
            RiskConfig(risk_per_trade_pct=0.01)

    def test_max_leverage_capped_at_4(self):
        """Max leverage cannot exceed 4x."""
        with pytest.raises(Exception):
            RiskConfig(max_leverage=6.0)
