"""
Circuit breaker for trading system safety.

Monitors daily drawdown, consecutive losses, and API errors.
Halts all trading when safety thresholds are breached.
Supports auto-recovery through a COOLDOWN state after a configurable
cooldown period, transitioning back to WARNING (reduced trading) if
no new triggers occur.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog

from trading_bot.config.settings import RiskConfig

log = structlog.get_logger(__name__)


class CircuitState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    COOLDOWN = "cooldown"
    HALTED = "halted"


class CircuitBreaker:
    """
    Monitors system health and halts trading when thresholds are breached.

    Conditions that trigger halt:
    - Daily drawdown exceeds drawdown_circuit_breaker_pct
    - Consecutive losing trades exceeds max_consecutive_losses
    - API error count exceeds threshold within 5-minute window

    Recovery flow:
    - After cooldown_minutes in HALTED, auto-transitions to COOLDOWN.
    - After 5 minutes in COOLDOWN with no new triggers, transitions to WARNING.
    - WARNING allows reduced trading; full NORMAL requires daily reset
      or force_reset.
    """

    def __init__(self, config: RiskConfig, cooldown_minutes: int = 30):
        self._config = config
        self._cooldown_minutes = cooldown_minutes
        self._state: CircuitState = CircuitState.NORMAL
        self._consecutive_losses: int = 0
        self._daily_pnl: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._starting_equity: float = 0.0
        self._api_errors: deque[datetime] = deque()
        self._api_error_window = timedelta(minutes=5)
        self._halted_at: Optional[datetime] = None
        self._cooldown_entered_at: Optional[datetime] = None
        self._api_error_counts: dict[str, int] = defaultdict(int)

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_trading_allowed(self) -> bool:
        """
        Trading is allowed in NORMAL, WARNING, and COOLDOWN states,
        BUT only if drawdown hasn't exceeded the hard daily limit.

        COOLDOWN after a halt should not allow NEW trades if the daily
        drawdown is still above threshold — only position management.
        """
        if self._state == CircuitState.HALTED:
            return False
        if self._state == CircuitState.COOLDOWN:
            # COOLDOWN allows position management but blocks new entries
            # if drawdown still exceeds threshold
            if self._starting_equity > 0:
                total_pnl = self._daily_pnl + self._unrealized_pnl
                drawdown_pct = abs(min(0, total_pnl)) / self._starting_equity * 100
                if drawdown_pct >= self._config.drawdown_circuit_breaker_pct:
                    return False
        return self._state in (
            CircuitState.NORMAL,
            CircuitState.WARNING,
            CircuitState.COOLDOWN,
        )

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def check(self) -> CircuitState:
        """
        Evaluate all circuit breaker conditions.

        Returns current circuit state after evaluation.
        Includes auto-recovery logic: HALTED -> COOLDOWN -> WARNING.
        """
        now = datetime.utcnow()

        # --- Auto-recovery: HALTED -> COOLDOWN ---
        if self._state == CircuitState.HALTED and self._halted_at is not None:
            elapsed = now - self._halted_at
            if elapsed >= timedelta(minutes=self._cooldown_minutes):
                self._state = CircuitState.COOLDOWN
                self._cooldown_entered_at = now
                log.info(
                    "circuit.cooldown_entered",
                    halted_for_minutes=round(elapsed.total_seconds() / 60, 1),
                )
                return self._state

        # --- Auto-recovery: COOLDOWN -> WARNING ---
        if self._state == CircuitState.COOLDOWN and self._cooldown_entered_at is not None:
            elapsed = now - self._cooldown_entered_at
            if elapsed >= timedelta(minutes=5):
                self._state = CircuitState.WARNING
                self._cooldown_entered_at = None
                log.info(
                    "circuit.recovered_to_warning",
                    cooldown_minutes=round(elapsed.total_seconds() / 60, 1),
                )
                return self._state

        if self._starting_equity <= 0:
            return self._state

        # 1. Daily drawdown check (includes both realized and unrealized P&L)
        if self._starting_equity > 0:
            total_pnl = self._daily_pnl + self._unrealized_pnl
            drawdown_pct = abs(min(0, total_pnl)) / self._starting_equity * 100
            if drawdown_pct >= self._config.drawdown_circuit_breaker_pct:
                self._halt(
                    f"daily_drawdown: {drawdown_pct:.2f}% >= "
                    f"{self._config.drawdown_circuit_breaker_pct}% "
                    f"(realized={self._daily_pnl:.2f}, unrealized={self._unrealized_pnl:.2f})"
                )
                return self._state

            # Warning at 75% of threshold
            if drawdown_pct >= self._config.drawdown_circuit_breaker_pct * 0.75:
                self._warn(
                    f"drawdown_approaching: {drawdown_pct:.2f}% "
                    f"(halt at {self._config.drawdown_circuit_breaker_pct}%)"
                )

        # 2. Consecutive losses check
        if self._consecutive_losses >= self._config.max_consecutive_losses:
            self._halt(
                f"consecutive_losses: {self._consecutive_losses} >= "
                f"{self._config.max_consecutive_losses}"
            )
            return self._state

        # 3. API error check (within 5-minute window)
        self._prune_old_errors()
        error_count = len(self._api_errors)
        if error_count >= self._config.api_error_halt_threshold:
            self._halt(
                f"api_errors: {error_count} in 5 minutes >= "
                f"{self._config.api_error_halt_threshold}"
            )
            return self._state

        if error_count >= self._config.api_error_halt_threshold * 0.6:
            self._warn(f"api_errors_rising: {error_count} in 5 minutes")

        return self._state

    def update_unrealized_pnl(self, unrealized_pnl: float) -> None:
        """
        Update unrealized P&L from open positions.

        Called each tick so the circuit breaker can halt trading
        before a catastrophic open position grows into a fatal loss.
        Without this, the circuit breaker only sees realized P&L
        (after a trade closes), which is too late.
        """
        self._unrealized_pnl = unrealized_pnl

    def record_trade_result(self, pnl: float) -> None:
        """Update counters after a trade closes."""
        self._daily_pnl += pnl

        if pnl < 0:
            self._consecutive_losses += 1
            log.info(
                "circuit.loss_recorded",
                pnl=round(pnl, 2),
                consecutive=self._consecutive_losses,
            )
        else:
            self._consecutive_losses = 0

        # Re-evaluate state after each trade
        self.check()

    def record_api_error(self, error_type: Optional[str] = None) -> None:
        """Record an API error with timestamp and optional error type.

        Args:
            error_type: Optional category string for the error (e.g.,
                        "timeout", "rate_limit", "auth", "server_error").
                        When provided, the count for that type is incremented.
        """
        self._api_errors.append(datetime.utcnow())

        if error_type is not None:
            self._api_error_counts[error_type] += 1

        log.warning(
            "circuit.api_error",
            recent_count=len(self._api_errors),
            error_type=error_type,
        )
        self.check()

    def get_error_summary(self) -> dict[str, int]:
        """Return a summary of API error counts grouped by error type.

        Returns:
            Dictionary mapping error type strings to their occurrence counts.
            Only includes types that were explicitly recorded via
            ``record_api_error(error_type=...)``.
        """
        return dict(self._api_error_counts)

    def reset_daily(self, equity: float) -> None:
        """Reset for a new trading day."""
        self._starting_equity = equity
        self._daily_pnl = 0.0
        self._unrealized_pnl = 0.0
        self._consecutive_losses = 0
        self._api_errors.clear()
        self._api_error_counts.clear()
        self._state = CircuitState.NORMAL
        self._halted_at = None
        self._cooldown_entered_at = None
        log.info("circuit.daily_reset", equity=round(equity, 2))

    def force_reset(self) -> None:
        """Manual reset (for admin/testing purposes)."""
        self._state = CircuitState.NORMAL
        self._consecutive_losses = 0
        self._api_errors.clear()
        self._api_error_counts.clear()
        self._halted_at = None
        self._cooldown_entered_at = None
        log.warning("circuit.force_reset")

    def _halt(self, reason: str) -> None:
        """Transition to HALTED state."""
        if self._state != CircuitState.HALTED:
            self._state = CircuitState.HALTED
            self._halted_at = datetime.utcnow()
            self._cooldown_entered_at = None
            log.critical("circuit.HALTED", reason=reason)

    def _warn(self, reason: str) -> None:
        """Transition to WARNING state (only if not already HALTED or COOLDOWN)."""
        if self._state == CircuitState.NORMAL:
            self._state = CircuitState.WARNING
            log.warning("circuit.warning", reason=reason)

    def _prune_old_errors(self) -> None:
        """Remove API errors older than the 5-minute window."""
        cutoff = datetime.utcnow() - self._api_error_window
        while self._api_errors and self._api_errors[0] < cutoff:
            self._api_errors.popleft()

    def get_status(self) -> dict:
        """Get current circuit breaker status for monitoring."""
        return {
            "state": self._state.value,
            "daily_pnl": round(self._daily_pnl, 2),
            "starting_equity": round(self._starting_equity, 2),
            "consecutive_losses": self._consecutive_losses,
            "api_errors_5min": len(self._api_errors),
            "api_error_summary": self.get_error_summary(),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
            "drawdown_pct": (
                round(abs(min(0, self._daily_pnl + self._unrealized_pnl)) / self._starting_equity * 100, 2)
                if self._starting_equity > 0
                else 0.0
            ),
            "halted_at": (
                self._halted_at.isoformat() if self._halted_at is not None else None
            ),
            "cooldown_entered_at": (
                self._cooldown_entered_at.isoformat()
                if self._cooldown_entered_at is not None
                else None
            ),
            "cooldown_minutes": self._cooldown_minutes,
        }
