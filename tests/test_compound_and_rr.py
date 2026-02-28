"""Tests for compound mode and asymmetric R:R configuration."""
from __future__ import annotations
import pytest
from trading_bot.risk.compound import CompoundMode
from trading_bot.config.settings import ExitConfig


class TestCompoundMode:
    def test_disabled_returns_current_equity(self):
        cm = CompoundMode(enabled=False)
        cm.set_starting_equity(100000)
        assert cm.get_effective_equity(120000) == 120000

    def test_enabled_blends_equity(self):
        cm = CompoundMode(enabled=True, growth_smoothing=0.5)
        cm.set_starting_equity(100000)
        effective = cm.get_effective_equity(120000)
        # 100000 * 0.5 + 120000 * 0.5 = 110000
        assert effective == 110000.0

    def test_floor_prevents_undersizing(self):
        cm = CompoundMode(enabled=True, floor_multiplier=0.5, growth_smoothing=1.0)
        cm.set_starting_equity(100000)
        effective = cm.get_effective_equity(30000)  # Large drawdown
        assert effective == 50000.0  # Floor is 0.5 * 100k

    def test_ceiling_prevents_oversizing(self):
        cm = CompoundMode(enabled=True, ceiling_multiplier=2.0, growth_smoothing=1.0)
        cm.set_starting_equity(100000)
        effective = cm.get_effective_equity(300000)  # Big run
        assert effective == 200000.0  # Ceiling is 2.0 * 100k

    def test_no_starting_equity_returns_current(self):
        cm = CompoundMode(enabled=True)
        assert cm.get_effective_equity(50000) == 50000

    def test_multiplier_at_start(self):
        cm = CompoundMode(enabled=True, growth_smoothing=1.0)
        cm.set_starting_equity(100000)
        assert cm.get_multiplier(100000) == 1.0

    def test_multiplier_after_growth(self):
        cm = CompoundMode(enabled=True, growth_smoothing=1.0, ceiling_multiplier=3.0)
        cm.set_starting_equity(100000)
        assert cm.get_multiplier(150000) == 1.5

    def test_is_enabled_property(self):
        assert CompoundMode(enabled=True).is_enabled is True
        assert CompoundMode(enabled=False).is_enabled is False


class TestAsymmetricRR:
    def test_default_exit_config_has_3r_target(self):
        """Default ExitConfig should have 3:1 R:R as third target."""
        cfg = ExitConfig()
        assert 3.0 in cfg.scale_out_rr_targets

    def test_default_four_scale_out_ratios(self):
        """Default should have 4 scale-out portions."""
        cfg = ExitConfig()
        assert len(cfg.scale_out_ratios) == 4

    def test_ratios_sum_to_one(self):
        cfg = ExitConfig()
        total = sum(cfg.scale_out_ratios)
        assert 0.99 <= total <= 1.01

    def test_ratios_match_targets_plus_one(self):
        """scale_out_ratios should have one more element than targets."""
        cfg = ExitConfig()
        assert len(cfg.scale_out_ratios) == len(cfg.scale_out_rr_targets) + 1

    def test_targets_ascending(self):
        cfg = ExitConfig()
        assert cfg.scale_out_rr_targets == sorted(cfg.scale_out_rr_targets)
