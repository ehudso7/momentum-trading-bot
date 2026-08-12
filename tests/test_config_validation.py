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



class TestYamlEnvironmentPrecedence:
    """Verify YAML remains the lowest-priority runtime configuration."""

    def test_environment_overrides_yaml_and_preserves_siblings(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "log_json: false\n"
            "risk:\n"
            "  risk_per_trade_pct: 0.5\n"
            "  max_daily_risk_pct: 1.5\n"
            "  max_open_positions: 3\n"
            "  max_leverage: 2.0\n"
            "  max_position_value_pct: 8.0\n"
            "  min_stop_distance_pct: 2.0\n"
            "  drawdown_circuit_breaker_pct: 2.0\n"
            "  max_consecutive_losses: 3\n"
            "  hard_daily_loss_limit_pct: 2.0\n"
            "  api_error_halt_threshold: 3\n"
        )

        monkeypatch.setenv(
            "TRADING_LOG_JSON",
            "true",
        )
        monkeypatch.setenv(
            "TRADING_RISK__RISK_PER_TRADE_PCT",
            "0.25",
        )
        monkeypatch.setenv(
            "TRADING_RISK__MAX_OPEN_POSITIONS",
            "2",
        )

        config = AppConfig.from_yaml(
            str(config_path)
        )

        assert config.log_json is True
        assert config.risk.risk_per_trade_pct == 0.25
        assert config.risk.max_open_positions == 2

        # An overridden nested field must not discard YAML siblings.
        assert config.risk.max_daily_risk_pct == 1.5
        assert config.risk.max_leverage == 2.0
        assert config.risk.hard_daily_loss_limit_pct == 2.0

    def test_dotenv_overrides_yaml(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "log_json: false\n"
        )

        (tmp_path / ".env").write_text(
            "TRADING_LOG_JSON=true\n"
        )

        monkeypatch.delenv(
            "TRADING_LOG_JSON",
            raising=False,
        )

        config = AppConfig.from_yaml(
            str(config_path)
        )

        assert config.log_json is True

    def test_direct_initializers_keep_normal_priority(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(
            "TRADING_LOG_JSON",
            "true",
        )

        config = AppConfig(
            log_json=False,
        )

        assert config.log_json is False

    def test_bare_provider_secret_aliases_remain_supported(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "broker:\n"
            "  alpaca_paper: true\n"
        )

        monkeypatch.delenv(
            "TRADING_BROKER__ALPACA_API_KEY",
            raising=False,
        )
        monkeypatch.delenv(
            "TRADING_BROKER__ALPACA_API_SECRET",
            raising=False,
        )
        monkeypatch.delenv(
            "TRADING_DATA__POLYGON_API_KEY",
            raising=False,
        )

        monkeypatch.setenv(
            "ALPACA_API_KEY",
            "bare-alpaca-key",
        )
        monkeypatch.setenv(
            "ALPACA_API_SECRET",
            "bare-alpaca-secret",
        )
        monkeypatch.setenv(
            "POLYGON_API_KEY",
            "bare-polygon-key",
        )

        config = AppConfig.from_yaml(
            str(config_path)
        )

        assert (
            config.broker.alpaca_api_key.get_secret_value()
            == "bare-alpaca-key"
        )
        assert (
            config.broker.alpaca_api_secret.get_secret_value()
            == "bare-alpaca-secret"
        )
        assert (
            config.data.polygon_api_key.get_secret_value()
            == "bare-polygon-key"
        )

    def test_canonical_environment_beats_compatibility_alias(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "broker:\n"
            "  alpaca_paper: true\n"
        )

        monkeypatch.setenv(
            "ALPACA_API_KEY",
            "bare-key",
        )
        monkeypatch.setenv(
            "ALPACA_API_SECRET",
            "bare-secret",
        )
        monkeypatch.setenv(
            "TRADING_BROKER__ALPACA_API_KEY",
            "canonical-key",
        )
        monkeypatch.setenv(
            "TRADING_BROKER__ALPACA_API_SECRET",
            "canonical-secret",
        )

        config = AppConfig.from_yaml(
            str(config_path)
        )

        assert (
            config.broker.alpaca_api_key.get_secret_value()
            == "canonical-key"
        )
        assert (
            config.broker.alpaca_api_secret.get_secret_value()
            == "canonical-secret"
        )
