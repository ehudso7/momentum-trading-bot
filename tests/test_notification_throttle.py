"""Notification delivery contract: error throttling and webhook renderability.

Two failure modes are pinned here, both of which make alerting look configured
while delivering nothing:

1. ``notify_error`` is called from inside the trading tick loop, which runs as
   often as every 10 seconds. Without a throttle a persistent fault emits
   hundreds of identical webhooks per hour and buries the alerts that matter.

2. Chat webhooks reject a POST with no renderable text field. Slack answers
   ``400 no_text``; the exception is then swallowed into a log line, so every
   notification fails silently. Every payload must carry a summary.
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


class TestPayloadIsDeliverable:
    """Every payload must be renderable by Slack and Discord.

    Slack rejects a body with no `text` with 400 no_text; Discord rejects an
    empty message. A rejected POST is retried twice and then swallowed, so this
    failure is invisible without these assertions.
    """

    def _emit_one_of_each(self, mgr) -> None:
        mgr.notify_trade_opened(
            symbol="TSLA",
            side="long",
            shares=10,
            entry_price=100.0,
            stop_price=99.0,
            risk_dollars=10.0,
        )
        mgr.notify_trade_closed(
            symbol="TSLA",
            side="long",
            shares=10,
            entry_price=100.0,
            exit_price=102.0,
            pnl=20.0,
            rr_ratio=2.0,
            hold_time_minutes=15.0,
            exit_reason="target",
        )
        mgr.notify_circuit_breaker(
            state="halted",
            reason="drawdown",
            daily_pnl=-5100.0,
            consecutive_losses=3,
        )
        mgr.notify_daily_summary(
            date="2026-07-27",
            total_trades=3,
            winning_trades=2,
            losing_trades=1,
            gross_pnl=260.0,
            net_pnl=250.0,
            win_rate=0.6667,
            largest_win=300.0,
            largest_loss=-50.0,
            ending_equity=71_929.46,
        )
        mgr.notify_error(error_type="tick_error", message="boom")

    def test_every_event_type_carries_nonempty_text_and_content(self, sent):
        self._emit_one_of_each(_manager())

        assert len(sent) == 5
        for payload in sent:
            label = payload["event_type"]
            assert payload.get("text"), f"{label} has no text — Slack returns 400"
            assert payload.get("content"), f"{label} has no content — Discord rejects"
            assert payload["text"] == payload["content"]
            # The structured body must survive alongside the summary.
            assert isinstance(payload["data"], dict)

    def test_summary_includes_the_facts_that_matter(self, sent):
        mgr = _manager()
        mgr.notify_circuit_breaker(
            state="halted",
            reason="drawdown",
            daily_pnl=-5100.0,
            consecutive_losses=3,
        )

        text = sent[0]["text"]
        assert "CIRCUIT BREAKER" in text
        assert "HALTED" in text
        assert "drawdown" in text
        assert "-$5,100.00" in text

    def test_error_summary_reports_suppressed_count(self, sent, monkeypatch):
        clock = {"now": 0.0}
        monkeypatch.setattr(
            notifications_module.time, "monotonic", lambda: clock["now"]
        )
        mgr = _manager()

        mgr.notify_error(error_type="tick_error", message="boom")
        for _ in range(4):
            mgr.notify_error(error_type="tick_error", message="boom")
        clock["now"] += notifications_module._ERROR_NOTIFY_INTERVAL_SECONDS + 1
        mgr.notify_error(error_type="tick_error", message="boom")

        assert "+4 suppressed" in sent[1]["text"]

    def test_malformed_data_still_renders_deliverable_text(self):
        # A partial or unexpected payload must never produce an empty summary,
        # because that turns into a rejected webhook and a silent operator.
        for event_type in (
            "trade_opened",
            "trade_closed",
            "circuit_breaker",
            "daily_summary",
            "error",
            "totally_unknown_event",
        ):
            summary = notifications_module._render_summary(event_type, {})
            assert summary, f"{event_type} rendered empty"
            assert isinstance(summary, str)

    def test_daily_summary_does_not_multiply_percentage_twice(self, sent):
        _manager().notify_daily_summary(
            date="2026-08-10",
            total_trades=1,
            winning_trades=1,
            losing_trades=0,
            gross_pnl=13.62,
            net_pnl=13.62,
            win_rate=100.0,
            largest_win=13.62,
            largest_loss=0.0,
            ending_equity=71_653.41,
        )

        assert "win rate 100.0%" in sent[0]["text"]
        assert "10000.0%" not in sent[0]["text"]


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
