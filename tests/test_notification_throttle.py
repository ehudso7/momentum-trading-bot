"""Error-notification throttling.

``notify_error`` is called from inside the trading tick loop, which runs as
often as every 10 seconds. Without a throttle a persistent fault emits hundreds
of identical webhooks per hour and buries the alerts that matter. These tests
pin the throttle's contract: suppress duplicates per ``error_type``, never lose
the fact that a fault is ongoing, and never throttle the rare event types.
"""

from __future__ import annotations

import pytest

from trading_bot.config.settings import NotificationConfig
from trading_bot.utils import notifications as notifications_module
from trading_bot.utils.notifications import NotificationManager


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Capture payloads at the dispatch boundary — no network, no threads."""
    captured: list[dict] = []
    monkeypatch.setattr(
        NotificationManager,
        "_send_async",
        lambda self, payload: captured.append(payload),
    )
    return captured


def _manager(**overrides) -> NotificationManager:
    settings = {
        "enabled": True,
        "webhook_url": "https://hooks.slack.com/services/T/B/x",
        "notify_on_error": True,
        "notify_on_trade": True,
        "notify_on_circuit_breaker": True,
        "notify_on_daily_summary": True,
    }
    settings.update(overrides)
    return NotificationManager(NotificationConfig(**settings))


class TestErrorThrottle:
    def test_first_error_is_delivered(self, sent):
        _manager().notify_error(error_type="tick_error", message="boom")

        assert len(sent) == 1
        assert sent[0]["event_type"] == "error"
        assert sent[0]["data"]["error_type"] == "tick_error"
        # Nothing was suppressed before the first message.
        assert "suppressed_since_last_notification" not in sent[0]["data"]

    def test_repeat_within_interval_is_suppressed(self, sent):
        mgr = _manager()
        for _ in range(50):
            mgr.notify_error(error_type="tick_error", message="boom")

        assert len(sent) == 1, "a tight error loop must not fan out to the webhook"

    def test_distinct_error_types_do_not_throttle_each_other(self, sent):
        mgr = _manager()
        mgr.notify_error(error_type="tick_error", message="a")
        mgr.notify_error(error_type="reconciliation", message="b")

        assert [p["data"]["error_type"] for p in sent] == [
            "tick_error",
            "reconciliation",
        ]

    def test_delivery_resumes_after_interval_and_reports_suppressed_count(
        self, sent, monkeypatch
    ):
        clock = {"now": 1_000.0}
        monkeypatch.setattr(
            notifications_module.time, "monotonic", lambda: clock["now"]
        )
        mgr = _manager()

        mgr.notify_error(error_type="tick_error", message="boom")
        for _ in range(9):
            mgr.notify_error(error_type="tick_error", message="boom")

        clock["now"] += notifications_module._ERROR_NOTIFY_INTERVAL_SECONDS + 1
        mgr.notify_error(error_type="tick_error", message="boom")

        assert len(sent) == 2
        # The 9 suppressed occurrences are reported, so the throttle never hides
        # that the fault kept firing.
        assert sent[1]["data"]["suppressed_since_last_notification"] == 9

    def test_suppressed_counter_resets_after_delivery(self, sent, monkeypatch):
        clock = {"now": 0.0}
        monkeypatch.setattr(
            notifications_module.time, "monotonic", lambda: clock["now"]
        )
        mgr = _manager()
        step = notifications_module._ERROR_NOTIFY_INTERVAL_SECONDS + 1

        mgr.notify_error(error_type="tick_error", message="boom")  # sent[0]
        mgr.notify_error(error_type="tick_error", message="boom")  # suppressed
        clock["now"] += step
        mgr.notify_error(error_type="tick_error", message="boom")  # sent[1]
        clock["now"] += step
        mgr.notify_error(error_type="tick_error", message="boom")  # sent[2]

        assert len(sent) == 3
        assert sent[1]["data"]["suppressed_since_last_notification"] == 1
        # Counter reset: the quiet interval reports nothing suppressed.
        assert "suppressed_since_last_notification" not in sent[2]["data"]

    def test_disabled_error_notifications_send_nothing(self, sent):
        mgr = _manager(notify_on_error=False)
        mgr.notify_error(error_type="tick_error", message="boom")

        assert sent == []


class TestRareEventsAreNeverThrottled:
    """Circuit-breaker and daily-summary alerts are the ones that matter most."""

    def test_repeated_circuit_breaker_alerts_all_deliver(self, sent):
        mgr = _manager()
        for _ in range(5):
            mgr.notify_circuit_breaker(
                state="halted",
                reason="drawdown",
                daily_pnl=-5100.0,
                consecutive_losses=3,
            )

        assert len(sent) == 5

    def test_repeated_trade_alerts_all_deliver(self, sent):
        mgr = _manager()
        for _ in range(5):
            mgr.notify_trade_opened(
                symbol="TSLA",
                side="long",
                shares=10,
                entry_price=100.0,
                stop_price=99.0,
                risk_dollars=10.0,
            )

        assert len(sent) == 5
