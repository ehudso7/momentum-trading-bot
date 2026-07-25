"""
Webhook notification system for trade alerts and system events.

Sends non-blocking HTTP POST notifications with JSON payloads to a
configurable webhook URL. All notifications are fire-and-forget:
failures are logged but never interrupt trading logic.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
import structlog

from trading_bot.config.settings import NotificationConfig
from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)

# Timeout for webhook HTTP requests (connect, read) in seconds.
_WEBHOOK_TIMEOUT = (5, 10)

# Minimum seconds between error notifications sharing an error_type.
#
# notify_error is called from inside the trading tick loop, which runs as often
# as every 10 seconds. A persistent fault (API outage, repeated reconciliation
# failure) would otherwise emit ~360 identical webhooks per hour and bury the
# alerts that matter. Suppressed occurrences are counted and reported on the
# next message that gets through, so the throttle never hides that a fault is
# ongoing. Only error notifications are throttled: trade, circuit-breaker, and
# daily-summary events are already naturally rare and must never be dropped.
_ERROR_NOTIFY_INTERVAL_SECONDS = 300.0


class NotificationManager:
    """
    Thread-safe, non-blocking notification manager.

    Dispatches webhook notifications for trade events, circuit breaker
    triggers, daily summaries, and errors. Each notification is sent in
    a background daemon thread so the main trading loop is never blocked.

    Args:
        config: NotificationConfig with enabled flag, webhook URL, and
                per-event toggle switches.
    """

    def __init__(self, config: NotificationConfig) -> None:
        self._config = config
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._fallback_webhook_url: Optional[str] = getattr(config, 'fallback_webhook_url', None)
        # Error-notification throttle state, keyed by error_type.
        # Guarded by its own lock: notify_error may be called from the trading
        # loop and from background threads.
        self._error_throttle_lock = threading.Lock()
        self._error_last_sent: dict[str, float] = {}
        self._error_suppressed: dict[str, int] = {}
        self._worker = threading.Thread(target=self._process_queue, daemon=True)
        self._worker.start()

    # --- Public notification methods ---

    def notify_trade_opened(
        self,
        symbol: str,
        side: str,
        shares: int,
        entry_price: float,
        stop_price: float,
        risk_dollars: float,
        signal_type: str = "",
    ) -> None:
        """Send notification when a new trade is opened."""
        if not self._config.notify_on_trade:
            return

        payload = self._build_payload(
            event_type="trade_opened",
            data={
                "symbol": symbol,
                "side": side,
                "shares": shares,
                "entry_price": round(entry_price, 4),
                "stop_price": round(stop_price, 4),
                "risk_dollars": round(risk_dollars, 2),
                "signal_type": signal_type,
            },
        )
        self._send_async(payload)

    def notify_trade_closed(
        self,
        symbol: str,
        side: str,
        shares: int,
        entry_price: float,
        exit_price: float,
        pnl: float,
        rr_ratio: float,
        hold_time_minutes: float,
        exit_reason: str = "",
    ) -> None:
        """Send notification when a trade is closed (full or final scale-out)."""
        if not self._config.notify_on_trade:
            return

        payload = self._build_payload(
            event_type="trade_closed",
            data={
                "symbol": symbol,
                "side": side,
                "shares": shares,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "pnl": round(pnl, 2),
                "rr_ratio": round(rr_ratio, 2),
                "hold_time_minutes": round(hold_time_minutes, 1),
                "exit_reason": exit_reason,
            },
        )
        self._send_async(payload)

    def notify_circuit_breaker(
        self,
        state: str,
        reason: str,
        daily_pnl: float,
        consecutive_losses: int,
    ) -> None:
        """Send notification when circuit breaker changes state."""
        if not self._config.notify_on_circuit_breaker:
            return

        payload = self._build_payload(
            event_type="circuit_breaker",
            data={
                "state": state,
                "reason": reason,
                "daily_pnl": round(daily_pnl, 2),
                "consecutive_losses": consecutive_losses,
            },
        )
        self._send_async(payload)

    def notify_daily_summary(
        self,
        date: str,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        gross_pnl: float,
        net_pnl: float,
        win_rate: float,
        largest_win: float,
        largest_loss: float,
        ending_equity: float,
    ) -> None:
        """Send end-of-day performance summary."""
        if not self._config.notify_on_daily_summary:
            return

        payload = self._build_payload(
            event_type="daily_summary",
            data={
                "date": date,
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "gross_pnl": round(gross_pnl, 2),
                "net_pnl": round(net_pnl, 2),
                "win_rate": round(win_rate, 4),
                "largest_win": round(largest_win, 2),
                "largest_loss": round(largest_loss, 2),
                "ending_equity": round(ending_equity, 2),
            },
        )
        self._send_async(payload)

    def notify_error(
        self,
        error_type: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Send notification for system errors (API failures, exceptions, etc.).

        Throttled per ``error_type`` — see ``_ERROR_NOTIFY_INTERVAL_SECONDS``.
        A fault that keeps firing produces one webhook per interval rather than
        one per tick, and each delivered message reports how many occurrences
        were suppressed since the previous one.
        """
        if not self._config.notify_on_error:
            return

        suppressed = self._claim_error_slot(error_type)
        if suppressed is None:
            return

        data: dict[str, Any] = {
            "error_type": error_type,
            "message": message,
        }
        if details:
            data["details"] = details
        if suppressed:
            data["suppressed_since_last_notification"] = suppressed

        payload = self._build_payload(event_type="error", data=data)
        self._send_async(payload)

    def _claim_error_slot(self, error_type: str) -> Optional[int]:
        """Decide whether this error may notify now.

        Returns the number of occurrences suppressed since the last delivered
        notification for ``error_type`` (0 on the first one), or ``None`` when
        this occurrence is itself throttled and must not be sent.
        """
        now = time.monotonic()
        with self._error_throttle_lock:
            last = self._error_last_sent.get(error_type)
            if last is not None and (now - last) < _ERROR_NOTIFY_INTERVAL_SECONDS:
                self._error_suppressed[error_type] = (
                    self._error_suppressed.get(error_type, 0) + 1
                )
                return None
            self._error_last_sent[error_type] = now
            return self._error_suppressed.pop(error_type, 0)

    # --- Internal helpers ---

    def _build_payload(
        self, event_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a standardised notification payload with timestamp and event type."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "data": data,
        }

    def _process_queue(self) -> None:
        """Background worker that processes notification queue with retry."""
        while True:
            try:
                payload = self._queue.get(timeout=5)
                try:
                    self._send(payload)
                except Exception as e:
                    log.warning("notification.primary_failed", error=str(e)[:100])
                    # Try fallback webhook
                    if self._fallback_webhook_url:
                        try:
                            self._session.post(
                                self._fallback_webhook_url,
                                json=payload,
                                timeout=_WEBHOOK_TIMEOUT,
                            )
                            log.info("notification.fallback_sent")
                        except Exception:
                            log.error("notification.fallback_also_failed")
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception:
                continue

    def _send_async(self, payload: dict[str, Any]) -> None:
        if not self._config.enabled or not self._config.webhook_url:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            log.warning("notification.queue_full")

    @retry_with_backoff(
        max_retries=2,
        base_delay=1.0,
        max_delay=5.0,
        retryable_errors=(requests.RequestException,),
    )
    def _send(self, payload: dict[str, Any]) -> None:
        """
        POST the JSON payload to the configured webhook URL.

        Retried up to 2 times with exponential backoff on transient
        HTTP / network errors. After exhausting retries the exception
        propagates to _send_safe which logs and swallows it.
        """
        url = self._config.webhook_url
        response = self._session.post(
            url,  # type: ignore[arg-type]
            json=payload,
            timeout=_WEBHOOK_TIMEOUT,
        )
        response.raise_for_status()

        log.debug(
            "notification.sent",
            event_type=payload.get("event_type"),
            status_code=response.status_code,
        )
