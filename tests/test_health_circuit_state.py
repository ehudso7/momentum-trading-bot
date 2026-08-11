"""A risk halt must not be emitted again as a health-check failure."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from enum import Enum


if importlib.util.find_spec("structlog") is None:
    structlog = types.ModuleType("structlog")

    class _Log:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    structlog.get_logger = lambda *_args, **_kwargs: _Log()
    sys.modules["structlog"] = structlog

from trading_bot.utils.health import HealthMonitor  # noqa: E402


class State(str, Enum):
    HALTED = "halted"


class TestHealthCircuitState(unittest.TestCase):
    def test_recently_ticking_halted_bot_is_operationally_healthy(self):
        monitor = HealthMonitor()
        monitor.record_tick()
        monitor.set_circuit_breaker(types.SimpleNamespace(state=State.HALTED))

        self.assertTrue(monitor.is_healthy())
        self.assertEqual(
            monitor.get_health_status()["circuit_breaker_state"],
            "halted",
        )


if __name__ == "__main__":
    unittest.main()
