"""
Health monitoring for the trading bot.

Tracks system health metrics including tick timing, scan activity,
trade activity, API errors, memory usage, and uptime. Provides a
single snapshot view of system health and a simple is_healthy check.
"""

from __future__ import annotations

import resource
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


class HealthMonitor:
    """
    Monitors trading bot health metrics.

    Tracks tick frequency, scan and trade activity, API errors,
    memory usage, uptime, and integrates with circuit breaker state
    and portfolio position counts.

    Usage:
        monitor = HealthMonitor()
        monitor.record_tick()
        monitor.record_scan(count=5)
        monitor.record_trade("AAPL")
        monitor.record_error("alpaca_api")
        status = monitor.get_health_status()
        if not monitor.is_healthy():
            log.warning("health.unhealthy", status=status)
    """

    def __init__(self) -> None:
        self._start_time: datetime = datetime.utcnow()
        self._tick_count: int = 0
        self._last_tick_at: Optional[datetime] = None
        self._last_scan_at: Optional[datetime] = None
        self._last_scan_candidates: int = 0
        self._last_trade_at: Optional[datetime] = None
        self._last_trade_symbol: Optional[str] = None
        self._total_api_errors: int = 0
        self._recent_errors: deque[datetime] = deque()
        self._error_window = timedelta(minutes=5)
        self._circuit_breaker: Optional[object] = None
        self._portfolio_manager: Optional[object] = None

    # --- Registration for external components ---

    def set_circuit_breaker(self, circuit_breaker: object) -> None:
        """Register the circuit breaker for state monitoring."""
        self._circuit_breaker = circuit_breaker

    def set_portfolio_manager(self, portfolio_manager: object) -> None:
        """Register the portfolio manager for position/P&L monitoring."""
        self._portfolio_manager = portfolio_manager

    # --- Recording methods ---

    def record_tick(self) -> None:
        """Record a polling tick. Updates tick counter and timestamp."""
        self._tick_count += 1
        self._last_tick_at = datetime.utcnow()
        log.debug(
            "health.tick_recorded",
            tick_count=self._tick_count,
        )

    def record_scan(self, count: int) -> None:
        """Record a scanner run with the number of candidates found.

        Args:
            count: Number of candidates returned by the scanner.
        """
        self._last_scan_at = datetime.utcnow()
        self._last_scan_candidates = count
        log.debug(
            "health.scan_recorded",
            candidates=count,
        )

    def record_trade(self, symbol: str) -> None:
        """Record that a trade was executed.

        Args:
            symbol: Ticker symbol that was traded.
        """
        self._last_trade_at = datetime.utcnow()
        self._last_trade_symbol = symbol
        log.info(
            "health.trade_recorded",
            symbol=symbol,
        )

    def record_error(self, source: str) -> None:
        """Record an API or system error.

        Args:
            source: Identifier for the error source (e.g., "alpaca_api",
                    "yfinance", "market_data").
        """
        now = datetime.utcnow()
        self._total_api_errors += 1
        self._recent_errors.append(now)
        self._prune_old_errors()
        log.warning(
            "health.error_recorded",
            source=source,
            total_errors=self._total_api_errors,
        )

    # --- Query methods ---

    def get_health_status(self) -> dict:
        """Return a full snapshot of system health metrics.

        Returns:
            Dictionary containing all tracked health metrics:
            - last_tick_at: ISO timestamp of most recent tick
            - tick_count_today: Total ticks recorded
            - last_scan_at: ISO timestamp of most recent scan
            - last_scan_candidates: Number of candidates from last scan
            - last_trade_at: ISO timestamp of most recent trade
            - last_trade_symbol: Symbol of most recent trade
            - api_error_count_total: Total API errors since start
            - api_error_count_5min: API errors in last 5 minutes
            - memory_usage_mb: Current RSS memory usage in megabytes
            - uptime_seconds: Seconds since monitor was started
            - circuit_breaker_state: Current circuit breaker state string
            - open_positions_count: Number of currently open positions
            - daily_pnl: Today's realized P&L
        """
        self._prune_old_errors()

        now = datetime.utcnow()
        uptime = (now - self._start_time).total_seconds()

        # Memory usage from resource module (RSS in bytes on Linux, KB on macOS)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is in KB on Linux, bytes on macOS; use as KB for consistency
        memory_mb = usage.ru_maxrss / 1024

        # Circuit breaker state
        circuit_state = None
        if self._circuit_breaker is not None:
            state_attr = getattr(self._circuit_breaker, "state", None)
            if state_attr is not None:
                circuit_state = (
                    state_attr.value
                    if hasattr(state_attr, "value")
                    else str(state_attr)
                )

        # Open positions count and daily P&L
        open_positions_count = 0
        daily_pnl = 0.0
        if self._portfolio_manager is not None:
            get_open = getattr(self._portfolio_manager, "get_open_positions", None)
            if get_open is not None:
                try:
                    open_positions_count = len(get_open())
                except Exception:
                    pass
            get_pnl = getattr(self._portfolio_manager, "get_daily_pnl", None)
            if get_pnl is not None:
                try:
                    daily_pnl = get_pnl()
                except Exception:
                    pass

        return {
            "last_tick_at": (
                self._last_tick_at.isoformat() if self._last_tick_at else None
            ),
            "tick_count_today": self._tick_count,
            "last_scan_at": (
                self._last_scan_at.isoformat() if self._last_scan_at else None
            ),
            "last_scan_candidates": self._last_scan_candidates,
            "last_trade_at": (
                self._last_trade_at.isoformat() if self._last_trade_at else None
            ),
            "last_trade_symbol": self._last_trade_symbol,
            "api_error_count_total": self._total_api_errors,
            "api_error_count_5min": len(self._recent_errors),
            "memory_usage_mb": round(memory_mb, 1),
            "uptime_seconds": round(uptime, 1),
            "circuit_breaker_state": circuit_state,
            "open_positions_count": open_positions_count,
            "daily_pnl": round(daily_pnl, 2),
        }

    def is_healthy(self) -> bool:
        """Check whether the system is in a healthy state.

        Returns False if:
        - No tick has been recorded in the last 5 minutes.

        Returns True otherwise (including when no tick has ever been
        recorded, as the system may still be starting up). A financial-risk
        halt is reported in ``circuit_breaker_state`` but is not an operational
        health failure; treating it as both caused duplicate error alerts.
        """
        # Check tick freshness
        if self._last_tick_at is not None:
            elapsed = datetime.utcnow() - self._last_tick_at
            if elapsed > timedelta(minutes=5):
                log.warning(
                    "health.stale_tick",
                    seconds_since_tick=round(elapsed.total_seconds(), 1),
                )
                return False

        return True

    # --- Internal helpers ---

    def _prune_old_errors(self) -> None:
        """Remove errors older than the 5-minute window."""
        cutoff = datetime.utcnow() - self._error_window
        while self._recent_errors and self._recent_errors[0] < cutoff:
            self._recent_errors.popleft()
