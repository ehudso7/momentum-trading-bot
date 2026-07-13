"""
Trading-mode safety invariants (live-money safeguards).

These tests assert the EXISTING safety controls that keep the bot in paper /
simulation mode unless a human explicitly and deliberately opts into live
trading. They are the executable contract behind the CI paper-mode guard and
the CLAUDE.md "Safety Rules (NON-NEGOTIABLE)" section.

None of these tests place an order, touch a brokerage endpoint, or require
credentials. The live-mode broker (AlpacaBroker) is never constructed here —
where a test needs to prove the live code path selects it, AlpacaBroker is
patched with a sentinel so no real SDK client is created.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.config.settings import (
    AppConfig,
    BrokerConfig,
    RiskConfig,
    RunMode,
)
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.domain import PositionInfo
from trading_bot.risk.position_sizer import PositionSizer


# ---------------------------------------------------------------------------
# Invariant 1: paper mode is the default everywhere.
# ---------------------------------------------------------------------------
class TestPaperModeIsDefault:
    def test_appconfig_defaults_to_paper(self, monkeypatch):
        """A bare AppConfig (no env overrides) is paper mode."""
        # Ensure no ambient TRADING_RUN_MODE leaks in from the environment.
        monkeypatch.delenv("TRADING_RUN_MODE", raising=False)
        cfg = AppConfig()
        assert cfg.run_mode == RunMode.PAPER

    def test_broker_config_defaults_to_paper(self):
        """BrokerConfig defaults to the Alpaca paper environment."""
        assert BrokerConfig().alpaca_paper is True

    def test_shipped_config_yaml_is_paper(self):
        """The committed config.yaml ships in paper mode."""
        cfg_path = Path("trading_bot/config/config.yaml")
        assert cfg_path.exists(), "config.yaml must exist"
        cfg = AppConfig.from_yaml(str(cfg_path))
        assert cfg.run_mode == RunMode.PAPER


# ---------------------------------------------------------------------------
# Invariant 2: enabling live requires explicit, non-default config.
# ---------------------------------------------------------------------------
class TestLiveRequiresExplicitOptIn:
    def test_live_with_paper_broker_fails_closed(self):
        """Live mode while alpaca_paper=True (the default) is rejected."""
        with pytest.raises(ValueError, match="alpaca_paper"):
            AppConfig(run_mode=RunMode.LIVE, broker=BrokerConfig(alpaca_paper=True))

    def test_live_requires_real_key_not_empty(self):
        """Live mode with an empty API key is rejected."""
        with pytest.raises(ValueError, match="valid Alpaca API key"):
            AppConfig(
                run_mode=RunMode.LIVE,
                broker=BrokerConfig(
                    alpaca_paper=False,
                    alpaca_api_key="",
                    alpaca_api_secret="something",
                ),
            )

    def test_live_requires_real_key_not_placeholder(self):
        """Live mode with the placeholder API key is rejected."""
        with pytest.raises(ValueError, match="placeholder"):
            AppConfig(
                run_mode=RunMode.LIVE,
                broker=BrokerConfig(
                    alpaca_paper=False,
                    alpaca_api_key="your_alpaca_api_key_here",
                    alpaca_api_secret="something",
                ),
            )

    def test_fully_explicit_live_config_is_accepted(self):
        """
        The ONLY way to a valid live config: explicit LIVE + alpaca_paper=False
        + a real (non-placeholder) key. This documents the exact opt-in surface
        so any future weakening (e.g. defaulting a key) breaks a test.

        NOTE: constructing a valid live *config* does NOT trade — no broker is
        built and no order is placed here.
        """
        cfg = AppConfig(
            run_mode=RunMode.LIVE,
            broker=BrokerConfig(
                alpaca_paper=False,
                alpaca_api_key="unit-test-not-a-real-key",
                alpaca_api_secret="unit-test-not-a-real-secret",
            ),
        )
        assert cfg.run_mode == RunMode.LIVE
        assert cfg.broker.alpaca_paper is False


# ---------------------------------------------------------------------------
# Invariant 3: a test / CI environment cannot silently activate live.
# ---------------------------------------------------------------------------
class TestEnvCannotSilentlyGoLive:
    def test_setting_only_run_mode_env_to_live_fails_closed(self, monkeypatch):
        """
        Flipping ONLY TRADING_RUN_MODE=live via env (as a misconfigured CI or
        test shell might) does not activate live trading — it raises, because
        alpaca_paper is still True by default. Fail-closed.
        """
        monkeypatch.setenv("TRADING_RUN_MODE", "live")
        monkeypatch.delenv("TRADING_BROKER__ALPACA_PAPER", raising=False)
        with pytest.raises(ValueError, match="alpaca_paper"):
            AppConfig()

    def test_paper_env_stays_paper(self, monkeypatch):
        """The CI-exported paper env resolves to paper mode."""
        monkeypatch.setenv("TRADING_RUN_MODE", "paper")
        monkeypatch.setenv("TRADING_BROKER__ALPACA_PAPER", "true")
        cfg = AppConfig()
        assert cfg.run_mode == RunMode.PAPER
        assert cfg.broker.alpaca_paper is True


# ---------------------------------------------------------------------------
# Invariant 4: the order-placement path is gated by run mode.
#   Paper / backtest  -> PaperBroker (in-memory simulation; no endpoint).
#   Live              -> AlpacaBroker (the ONLY brokerage-touching broker).
# ---------------------------------------------------------------------------
class TestOrderPathIsGatedByMode:
    def _paper_config(self, tmp_path) -> AppConfig:
        return AppConfig(
            run_mode=RunMode.PAPER,
            broker=BrokerConfig(),
            journal_csv_path=str(tmp_path / "journal.csv"),
        )

    def test_paper_mode_uses_paper_broker_never_alpaca(self, tmp_path, monkeypatch):
        """
        Constructing the bot in paper mode selects the in-memory PaperBroker
        and NEVER constructs AlpacaBroker. AlpacaBroker is patched with a
        sentinel that fails the test if it is ever called.
        """
        import trading_bot.main as main_mod

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("AlpacaBroker must not be constructed in paper mode")

        monkeypatch.setattr(main_mod, "AlpacaBroker", _boom)

        bot = main_mod.TradingBot(self._paper_config(tmp_path))
        assert isinstance(bot._broker, PaperBroker)

    def test_backtest_mode_uses_paper_broker(self, tmp_path, monkeypatch):
        """Backtest mode also uses PaperBroker, never AlpacaBroker."""
        import trading_bot.main as main_mod

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("AlpacaBroker must not be constructed in backtest mode")

        monkeypatch.setattr(main_mod, "AlpacaBroker", _boom)

        cfg = AppConfig(
            run_mode=RunMode.BACKTEST,
            broker=BrokerConfig(),
            journal_csv_path=str(tmp_path / "journal.csv"),
        )
        bot = main_mod.TradingBot(cfg)
        assert isinstance(bot._broker, PaperBroker)

    def test_live_mode_selects_alpaca_broker(self, tmp_path, monkeypatch):
        """
        Live mode is the ONLY path that selects AlpacaBroker. AlpacaBroker is
        patched so this test proves the branch selection WITHOUT ever creating a
        real SDK client or touching a brokerage endpoint.
        """
        import trading_bot.main as main_mod

        constructed = {"called": 0}
        sentinel = object()

        def _fake_alpaca(broker_cfg):
            constructed["called"] += 1
            constructed["paper_flag"] = broker_cfg.alpaca_paper
            return sentinel

        monkeypatch.setattr(main_mod, "AlpacaBroker", _fake_alpaca)

        cfg = AppConfig(
            run_mode=RunMode.LIVE,
            broker=BrokerConfig(
                alpaca_paper=False,
                alpaca_api_key="unit-test-not-a-real-key",
                alpaca_api_secret="unit-test-not-a-real-secret",
            ),
            journal_csv_path=str(tmp_path / "journal.csv"),
        )
        bot = main_mod.TradingBot(cfg)
        assert constructed["called"] == 1
        # Live broker must not be in Alpaca's paper environment.
        assert constructed["paper_flag"] is False
        assert bot._broker is sentinel


# ---------------------------------------------------------------------------
# Invariant 5: risk limits fail closed (reject on violation / bad input).
# ---------------------------------------------------------------------------
class TestRiskLimitsFailClosed:
    def _sizer(self) -> PositionSizer:
        return PositionSizer(RiskConfig())

    def test_zero_stop_distance_rejected(self):
        """entry == stop => not approved (no position)."""
        result = self._sizer().calculate(
            equity=100_000.0,
            entry_price=10.0,
            stop_price=10.0,
            current_positions=[],
            buying_power=400_000.0,
        )
        assert result.approved is False
        assert result.shares == 0

    def test_nonpositive_equity_produces_no_shares(self):
        """Garbage/zero equity must never size a position."""
        result = self._sizer().calculate(
            equity=0.0,
            entry_price=10.0,
            stop_price=9.5,
            current_positions=[],
            buying_power=0.0,
        )
        assert result.approved is False
        assert result.shares == 0

    def test_max_open_positions_blocks_new_entry(self, sample_position):
        """At the max_open_positions cap, further entries are rejected."""
        cfg = RiskConfig()
        full_book = [sample_position] * cfg.max_open_positions
        result = PositionSizer(cfg).calculate(
            equity=100_000.0,
            entry_price=10.0,
            stop_price=9.5,
            current_positions=full_book,
            buying_power=400_000.0,
        )
        assert result.approved is False
        assert "max_positions" in result.reason

    def test_default_risk_config_within_documented_bounds(self):
        """
        The default risk posture stays inside the CLAUDE.md non-negotiable
        bounds (risk_per_trade <= 3%, leverage <= 4x). Guards against a silent
        loosening of the shipped defaults.
        """
        cfg = RiskConfig()
        assert 0.1 <= cfg.risk_per_trade_pct <= 3.0
        assert 1.0 <= cfg.max_leverage <= 4.0
