"""
Trading-mode safety invariants (live-money safeguards).

These tests assert the safety controls that keep the bot in paper /
simulation mode unless a human explicitly and deliberately opts into live
trading. They are the executable contract behind the CI paper-mode guard in
.github/workflows/ci.yml (backend job).

Scope note: config-validation opt-in rules (live requires alpaca_paper=false,
empty/placeholder key rejection, paper-passes-without-keys) and risk-bound
enforcement are already covered by tests/test_config_validation.py, and
credential-based broker fallback by tests/test_broker_selection.py. This file
only adds the invariants those suites do not cover: paper-by-default,
env-driven fail-closed behavior, and run-mode gating of the order path.

None of these tests place an order, touch a brokerage endpoint, or require
credentials. The live-mode broker (AlpacaBroker) is never really constructed —
where a test needs to prove the live code path selects it, AlpacaBroker is
patched with a sentinel so no real SDK client is created.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.config.settings import AppConfig, BrokerConfig, RunMode
from trading_bot.execution.paper_broker import PaperBroker


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
# Invariant 2: a test / CI environment cannot silently activate live.
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

    def test_live_kill_switch_defaults_off(self, monkeypatch):
        """
        Even a fully explicit live config (LIVE + alpaca_paper=False + real
        keys) is rejected unless TRADING_LIVE_TRADING_ENABLED=true is ALSO
        set. The kill-switch defaults to off.
        """
        monkeypatch.delenv("TRADING_LIVE_TRADING_ENABLED", raising=False)
        with pytest.raises(ValueError, match="TRADING_LIVE_TRADING_ENABLED"):
            AppConfig(
                run_mode=RunMode.LIVE,
                broker=BrokerConfig(
                    alpaca_paper=False,
                    alpaca_api_key="unit-test-not-a-real-key",
                    alpaca_api_secret="unit-test-not-a-real-secret",
                ),
            )

    def test_paper_env_stays_paper(self, monkeypatch):
        """The CI-exported paper env resolves to paper mode."""
        monkeypatch.setenv("TRADING_RUN_MODE", "paper")
        monkeypatch.setenv("TRADING_BROKER__ALPACA_PAPER", "true")
        cfg = AppConfig()
        assert cfg.run_mode == RunMode.PAPER
        assert cfg.broker.alpaca_paper is True


# ---------------------------------------------------------------------------
# Invariant 3: the order-placement path is gated by run mode.
#   Paper (no credentials) / backtest -> local PaperBroker (in-memory; no
#     endpoint). Paper with valid credentials uses Alpaca's paper environment,
#     covered by tests/test_broker_selection.py.
#   Live -> AlpacaBroker (the ONLY brokerage-touching path), and only after
#     the paper-evidence gate passes.
# ---------------------------------------------------------------------------
class TestOrderPathIsGatedByMode:
    @staticmethod
    def _config(run_mode: RunMode, tmp_path, broker: BrokerConfig | None = None) -> AppConfig:
        kwargs: dict[str, object] = {
            "run_mode": run_mode,
            "broker": broker or BrokerConfig(),
            "journal_csv_path": str(tmp_path / "journal.csv"),
        }
        if run_mode == RunMode.LIVE:
            # Explicitly flip the kill-switch so config validation passes and
            # the tests below can prove the RUNTIME gates. No broker is really
            # constructed in any of these tests.
            kwargs["live_trading_enabled"] = True
        return AppConfig(**kwargs)

    @staticmethod
    def _live_broker_config() -> BrokerConfig:
        return BrokerConfig(
            alpaca_paper=False,
            alpaca_api_key="unit-test-not-a-real-key",
            alpaca_api_secret="unit-test-not-a-real-secret",
        )

    def test_paper_mode_without_credentials_uses_local_paper_broker(
        self, tmp_path, monkeypatch
    ):
        """
        Constructing the bot in paper mode with no credentials selects the
        in-memory PaperBroker and never constructs AlpacaBroker. AlpacaBroker
        is patched with a sentinel that fails the test if it is ever called.
        """
        import trading_bot.main as main_mod

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("AlpacaBroker must not be constructed in keyless paper mode")

        monkeypatch.setattr(main_mod, "AlpacaBroker", _boom)

        bot = main_mod.TradingBot(self._config(RunMode.PAPER, tmp_path))
        assert isinstance(bot._broker, PaperBroker)
        assert bot._broker_provider == "local_paper"

    def test_backtest_mode_uses_paper_broker(self, tmp_path, monkeypatch):
        """Backtest mode also uses PaperBroker, never AlpacaBroker."""
        import trading_bot.main as main_mod

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("AlpacaBroker must not be constructed in backtest mode")

        monkeypatch.setattr(main_mod, "AlpacaBroker", _boom)

        bot = main_mod.TradingBot(self._config(RunMode.BACKTEST, tmp_path))
        assert isinstance(bot._broker, PaperBroker)
        assert bot._broker_provider == "backtest_paper"

    def test_live_mode_without_paper_evidence_fails_closed(self, tmp_path, monkeypatch):
        """
        Even a fully explicit live config cannot construct the live broker
        without passing the paper-evidence gate: TradingBot raises before
        AlpacaBroker is ever touched.
        """
        import trading_bot.main as main_mod

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("AlpacaBroker must not be constructed when the gate fails")

        monkeypatch.setattr(main_mod, "AlpacaBroker", _boom)

        cfg = self._config(RunMode.LIVE, tmp_path, broker=self._live_broker_config())
        with pytest.raises(RuntimeError, match="evidence gate"):
            main_mod.TradingBot(cfg)

    def test_live_mode_selects_alpaca_broker_after_evidence_gate(
        self, tmp_path, monkeypatch
    ):
        """
        Live mode is the ONLY run mode that selects AlpacaBroker with
        alpaca_paper=False. The evidence gate is stubbed to pass and
        AlpacaBroker is patched, so this proves the branch selection WITHOUT
        creating a real SDK client or touching a brokerage endpoint.
        """
        import trading_bot.main as main_mod

        monkeypatch.setattr(
            main_mod.TradingBot, "_assert_live_evidence_gate", lambda self: None
        )

        constructed = {"called": 0}
        sentinel = object()

        def _fake_alpaca(broker_cfg):
            constructed["called"] += 1
            constructed["paper_flag"] = broker_cfg.alpaca_paper
            return sentinel

        monkeypatch.setattr(main_mod, "AlpacaBroker", _fake_alpaca)

        cfg = self._config(RunMode.LIVE, tmp_path, broker=self._live_broker_config())
        bot = main_mod.TradingBot(cfg)
        assert constructed["called"] == 1
        # Live broker must not be in Alpaca's paper environment.
        assert constructed["paper_flag"] is False
        assert bot._broker is sentinel
        assert bot._broker_provider == "alpaca_live"
