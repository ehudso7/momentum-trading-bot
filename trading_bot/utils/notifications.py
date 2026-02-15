"""
Webhook notification system for trade alerts and system events.

Sends non-blocking HTTP POST notifications with JSON payloads to a
configurable webhook URL. All notifications are fire-and-forget:
failures are logged but never interrupt trading logic.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

import requests
import structlog

from trading_bot.config.settings import NotificationConfig
from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)

# Timeout for webhook HTTP requests (connect, read) in seconds.
_WEBHOOK_TIMEOUT = (5, 10)


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
        """Send notification for system errors (API failures, exceptions, etc.)."""
        if not self._config.notify_on_error:
            return

        data: dict[str, Any] = {
            "error_type": error_type,
            "message": message,
        }
        if details:
            data["details"] = details

        payload = self._build_payload(event_type="error", data=data)
        self._send_async(payload)

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

    def _send_async(self, payload: dict[str, Any]) -> None:
        """
        Fire-and-forget: dispatch the webhook POST in a daemon thread.

        If notifications are disabled or the webhook URL is not configured,
        this is a no-op.
        """
        if not self._config.enabled:
            log.debug(
                "notification.disabled",
                event_type=payload.get("event_type"),
            )
            return

        if not self._config.webhook_url:
            log.debug(
                "notification.no_webhook_url",
                event_type=payload.get("event_type"),
            )
            return

        thread = threading.Thread(
            target=self._send_safe,
            args=(payload,),
            daemon=True,
        )
        thread.start()

    def _send_safe(self, payload: dict[str, Any]) -> None:
        """
        Wrapper that swallows all exceptions from _send.

        Used as the actual thread target so that failures never escape
        the daemon thread and never interrupt the main trading loop.
        """
        try:
            self._send(payload)
        except Exception as e:
            log.error(
                "notification.failed",
                event_type=payload.get("event_type"),
                error=str(e)[:200],
            )

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
