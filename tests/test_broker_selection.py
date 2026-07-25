"""Tests for production paper-broker selection safety."""

from trading_bot.config.settings import AppConfig, BrokerConfig
from trading_bot.main import _has_valid_alpaca_credentials


def test_empty_credentials_use_local_fallback() -> None:
    assert _has_valid_alpaca_credentials(AppConfig()) is False


def test_placeholder_credentials_use_local_fallback() -> None:
    config = AppConfig(
        broker=BrokerConfig(
            alpaca_api_key="your_alpaca_api_key_here",
            alpaca_api_secret="your_alpaca_api_secret_here",
        )
    )
    assert _has_valid_alpaca_credentials(config) is False


def test_complete_paper_credentials_select_alpaca() -> None:
    config = AppConfig(
        broker=BrokerConfig(
            alpaca_api_key="paper-key",
            alpaca_api_secret="paper-secret",
            alpaca_paper=True,
        )
    )
    assert _has_valid_alpaca_credentials(config) is True
