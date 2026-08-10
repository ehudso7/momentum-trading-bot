"""Daily summary win rate is already a percentage at the call site."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest


if importlib.util.find_spec("structlog") is None:
    structlog = types.ModuleType("structlog")

    class _Log:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    structlog.get_logger = lambda *_args, **_kwargs: _Log()
    sys.modules["structlog"] = structlog

if importlib.util.find_spec("pydantic_settings") is None:
    settings = types.ModuleType("trading_bot.config.settings")
    settings.NotificationConfig = object
    sys.modules["trading_bot.config.settings"] = settings

if importlib.util.find_spec("requests") is None:
    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    requests.post = lambda *_args, **_kwargs: None
    sys.modules["requests"] = requests

from trading_bot.utils.notifications import _render_summary  # noqa: E402


class TestDailySummaryPercent(unittest.TestCase):
    def test_100_percent_is_not_rendered_as_10000_percent(self):
        summary = _render_summary(
            "daily_summary",
            {
                "date": "2026-08-10",
                "total_trades": 1,
                "net_pnl": 13.62,
                "win_rate": 100.0,
                "ending_equity": 71_653.41,
            },
        )

        self.assertIn("win rate 100.0%", summary)
        self.assertNotIn("10000.0%", summary)


if __name__ == "__main__":
    unittest.main()
