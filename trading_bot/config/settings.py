"""
Configuration models with Pydantic validation.

Every risk-related parameter has hard min/max bounds enforced at the type level.
Config loads from YAML defaults -> .env secrets -> environment variable overrides.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


def _strip_whitespace(value: object) -> object:
    """Recursively strip whitespace from string values in config data."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {k: _strip_whitespace(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_whitespace(item) for item in value]
    return value


class RunMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class ScannerConfig(BaseModel):
    """Pre-market and intraday scanner filter thresholds."""

    min_gap_pct: float = Field(4.0, ge=2.0, le=100.0)
    max_gap_pct: float = Field(200.0, ge=50.0, le=500.0)
    max_float_shares: int = Field(50_000_000, ge=1_000_000)
    ideal_float_shares: int = Field(20_000_000, ge=500_000)
    min_relative_volume: float = Field(2.0, ge=1.0)
    min_price: float = Field(2.0, ge=0.50)
    max_price: float = Field(50.0, le=500.0)
    scan_interval_seconds: int = Field(60, ge=5, le=300)
    catalyst_keywords: list[str] = Field(
        default_factory=lambda: [
            "FDA",
            "earnings",
            "contract",
            "acquisition",
            "merger",
            "offering",
            "buyback",
            "upgrade",
            "partnership",
            "approval",
            "trial",
            "revenue",
            "guidance",
            "squeeze",
        ]
    )
    max_candidates: int = Field(20, ge=1, le=50)


class RiskConfig(BaseModel):
    """
    Position sizing and risk management parameters.

    NON-NEGOTIABLE: These bounds exist to prevent account blowup.
    Do NOT increase the upper limits without understanding the consequences.
    """

    risk_per_trade_pct: float = Field(1.0, ge=0.1, le=3.0)
    max_daily_risk_pct: float = Field(3.0, ge=1.0, le=5.0)
    max_open_positions: int = Field(4, ge=1, le=10)
    max_leverage: float = Field(4.0, ge=1.0, le=4.0)
    max_position_value_pct: float = Field(20.0, ge=5.0, le=50.0)
    min_stop_distance_pct: float = Field(1.0, ge=0.25, le=5.0)
    pdt_equity_threshold: float = Field(25_000.0)
    stop_loss_atr_multiplier: float = Field(1.25, ge=0.5, le=3.0)
    drawdown_circuit_breaker_pct: float = Field(5.0, ge=1.0, le=15.0)
    max_consecutive_losses: int = Field(5, ge=2, le=20)
    hard_daily_loss_limit_pct: float = Field(5.0, ge=1.0, le=15.0)
    api_error_halt_threshold: int = Field(5, ge=2, le=20)


class EntryConfig(BaseModel):
    """Entry signal parameters for VWAP pullback / breakout strategy."""

    vwap_proximity_pct: float = Field(1.5, ge=0.5, le=5.0)
    ema_period: int = Field(9, ge=5, le=21)
    volume_confirmation_multiplier: float = Field(1.5, ge=1.0, le=5.0)
    min_atr_distance: float = Field(0.5, ge=0.1, le=2.0)
    breakout_lookback_bars: int = Field(5, ge=3, le=20)
    min_consolidation_bars: int = Field(3, ge=2, le=10)


class ExitConfig(BaseModel):
    """Exit and profit-taking parameters."""

    scale_out_ratios: list[float] = Field(
        default_factory=lambda: [0.333, 0.333, 0.334]
    )
    scale_out_rr_targets: list[float] = Field(
        default_factory=lambda: [1.0, 2.0]
    )
    trailing_stop_atr_multiplier: float = Field(1.5, ge=0.5, le=4.0)
    trailing_stop_breakeven_buffer_pct: float = Field(0.5, ge=0.1, le=2.0)
    hard_time_exit: str = Field("15:50")  # 3:50 PM ET
    use_parabolic_sar: bool = Field(True)

    @model_validator(mode="after")
    def validate_scale_out(self) -> "ExitConfig":
        total = sum(self.scale_out_ratios)
        if not (0.99 <= total <= 1.01):
            raise ValueError(
                f"scale_out_ratios must sum to ~1.0, got {total:.3f}"
            )
        if any(r <= 0 for r in self.scale_out_ratios):
            raise ValueError("All scale_out_ratios must be positive")
        if len(self.scale_out_ratios) != len(self.scale_out_rr_targets) + 1:
            raise ValueError(
                "scale_out_ratios must have exactly one more element than "
                "scale_out_rr_targets (last portion is trailed)"
            )
        if self.scale_out_rr_targets != sorted(self.scale_out_rr_targets):
            raise ValueError("scale_out_rr_targets must be in ascending order")
        return self


class NotificationConfig(BaseModel):
    """Notification/webhook settings for trade alerts."""

    enabled: bool = Field(False)
    webhook_url: Optional[str] = Field(None)
    notify_on_trade: bool = Field(True)
    notify_on_circuit_breaker: bool = Field(True)
    notify_on_daily_summary: bool = Field(True)
    notify_on_error: bool = Field(True)


class BrokerConfig(BaseModel):
    """Alpaca broker connection settings."""

    alpaca_api_key: SecretStr = Field(default=SecretStr(""))
    alpaca_api_secret: SecretStr = Field(default=SecretStr(""))
    alpaca_paper: bool = Field(True)
    alpaca_base_url: Optional[str] = Field(None)


class DataConfig(BaseModel):
    """Market data provider settings."""

    polygon_api_key: SecretStr = Field(default=SecretStr(""))
    finnhub_api_key: Optional[SecretStr] = Field(None)
    use_polygon_news: bool = Field(True)
    float_cache_hours: int = Field(24, ge=1, le=168)
    polygon_rate_limit_per_minute: int = Field(5, ge=1, le=100)


class AppConfig(BaseSettings):
    """
    Root application configuration.

    Loads from: config.yaml (defaults) -> .env (secrets) -> env vars (overrides).
    Environment variable prefix: TRADING_
    Nested delimiter: __ (e.g., TRADING_RISK__RISK_PER_TRADE_PCT=0.5)
    """

    run_mode: RunMode = Field(RunMode.PAPER)
    starting_capital: float = Field(100_000.0, ge=100.0)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    exit: ExitConfig = Field(default_factory=ExitConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    log_level: str = Field("INFO")
    log_json: bool = Field(False)
    journal_csv_path: str = Field("data/journal.csv")
    backtest_start_date: Optional[str] = Field(None)
    backtest_end_date: Optional[str] = Field(None)
    backtest_symbols: list[str] = Field(default_factory=list)

    model_config = {
        "env_prefix": "TRADING_",
        "env_nested_delimiter": "__",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @model_validator(mode="before")
    @classmethod
    def strip_env_whitespace(cls, data: dict) -> dict:
        """Strip trailing whitespace/tabs from env var values before validation."""
        if isinstance(data, dict):
            return {k: _strip_whitespace(v) for k, v in data.items()}
        return data

    @classmethod
    def from_yaml(cls, path: str = "trading_bot/config/config.yaml") -> "AppConfig":
        """Load config from YAML file, then overlay .env and env vars."""
        config_path = Path(path)
        yaml_data: dict = {}
        if config_path.exists():
            with open(config_path) as f:
                yaml_data = yaml.safe_load(f) or {}

        # Inject env vars for secrets if present (override YAML)
        env_overrides = {}
        if os.getenv("POLYGON_API_KEY"):
            env_overrides.setdefault("data", {})["polygon_api_key"] = os.getenv(
                "POLYGON_API_KEY"
            )
        if os.getenv("ALPACA_API_KEY"):
            env_overrides.setdefault("broker", {})["alpaca_api_key"] = os.getenv(
                "ALPACA_API_KEY"
            )
        if os.getenv("ALPACA_API_SECRET"):
            env_overrides.setdefault("broker", {})["alpaca_api_secret"] = os.getenv(
                "ALPACA_API_SECRET"
            )

        # Deep merge env_overrides into yaml_data
        for key, val in env_overrides.items():
            if isinstance(val, dict) and key in yaml_data:
                yaml_data[key].update(val)
            else:
                yaml_data[key] = val

        return cls(**yaml_data)

    @model_validator(mode="after")
    def validate_safety(self) -> "AppConfig":
        """Cross-field safety validation (NON-NEGOTIABLE)."""
        # Live mode requires explicit paper=false
        if self.run_mode == RunMode.LIVE and self.broker.alpaca_paper:
            raise ValueError(
                "Live mode requires broker.alpaca_paper=false. "
                "This is a safety check to prevent accidental live trading."
            )

        # Live mode requires real API keys (not empty or placeholder)
        if self.run_mode == RunMode.LIVE:
            key = self.broker.alpaca_api_key.get_secret_value()
            if not key or key in ("", "your_alpaca_api_key_here"):
                raise ValueError(
                    "Live mode requires a valid Alpaca API key, "
                    "not a placeholder."
                )

        # Max total exposure check: per-trade risk * max positions shouldn't
        # exceed daily risk limit on a single bad tick
        max_exposure = (
            self.risk.risk_per_trade_pct * self.risk.max_open_positions
        )
        if max_exposure > self.risk.hard_daily_loss_limit_pct * 2:
            raise ValueError(
                f"Total risk exposure ({max_exposure:.1f}%) too high for "
                f"daily loss limit ({self.risk.hard_daily_loss_limit_pct}%). "
                f"Reduce risk_per_trade_pct or max_open_positions."
            )

        # Scanner price range must be valid
        if self.scanner.min_price >= self.scanner.max_price:
            raise ValueError(
                f"scanner.min_price ({self.scanner.min_price}) must be less "
                f"than scanner.max_price ({self.scanner.max_price})"
            )

        return self
