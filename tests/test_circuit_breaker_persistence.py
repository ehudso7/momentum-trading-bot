"""Regression tests for persistent circuit-breaker triggers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


if importlib.util.find_spec("structlog") is None:
    structlog = types.ModuleType("structlog")

    class _Log:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    structlog.get_logger = lambda *_args, **_kwargs: _Log()
    sys.modules["structlog"] = structlog

if importlib.util.find_spec("pydantic_settings") is None:
    settings = types.ModuleType("trading_bot.config.settings")
    settings.RiskConfig = object
    sys.modules["trading_bot.config.settings"] = settings

try:
    helpers_missing = importlib.util.find_spec("trading_bot.utils.helpers") is None
except (ImportError, ModuleNotFoundError):
    helpers_missing = True
if helpers_missing:
    helpers = types.ModuleType("trading_bot.utils.helpers")
    helpers.now_et = lambda: datetime.now(timezone.utc)
    sys.modules["trading_bot.utils.helpers"] = helpers

from trading_bot.risk.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitState,
)


class Config:
    drawdown_circuit_breaker_pct = 5.0
    hard_daily_loss_limit_pct = 5.0
    max_consecutive_losses = 3
    api_error_halt_threshold = 5


class TestPersistentCircuitTriggers(unittest.TestCase):
    def _loss_halted(self) -> CircuitBreaker:
        breaker = CircuitBreaker(Config(), cooldown_minutes=10)
        breaker.reset_daily(1_000_000.0)
        for _ in range(Config.max_consecutive_losses):
            breaker.record_trade_result(-1.0)
        self.assertEqual(breaker.state, CircuitState.HALTED)
        return breaker

    def test_elapsed_time_does_not_clear_active_loss_streak(self):
        breaker = self._loss_halted()
        future = breaker._halted_at + timedelta(minutes=60)

        with patch(
            "trading_bot.risk.circuit_breaker.now_et", return_value=future
        ):
            state = breaker.check()

        self.assertEqual(state, CircuitState.HALTED)
        self.assertFalse(breaker.is_trading_allowed)
        self.assertIn("consecutive_losses", breaker.get_status()["halt_reason"])

    def test_cleared_trigger_enters_non_trading_cooldown(self):
        breaker = self._loss_halted()
        breaker._consecutive_losses = 0
        future = breaker._halted_at + timedelta(minutes=10)

        with patch(
            "trading_bot.risk.circuit_breaker.now_et", return_value=future
        ):
            state = breaker.check()

        self.assertEqual(state, CircuitState.COOLDOWN)
        self.assertFalse(breaker.is_trading_allowed)

    def test_reset_clears_operator_visible_halt_reason(self):
        breaker = self._loss_halted()
        self.assertIn("consecutive_losses", breaker.get_status()["halt_reason"])

        breaker.reset_daily(1_000_000.0)

        self.assertIsNone(breaker.get_status()["halt_reason"])


if __name__ == "__main__":
    unittest.main()
