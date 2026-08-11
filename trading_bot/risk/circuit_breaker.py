"""
Circuit breaker for trading system safety.

Monitors daily drawdown, consecutive losses, and API errors.
Halts all trading when safety thresholds are breached.
Supports auto-recovery through a COOLDOWN state after a configurable
cooldown period, transitioning back to WARNING (reduced trading) if
no new triggers occur.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog

from trading_bot.config.settings import RiskConfig
from trading_bot.utils.helpers import now_et

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
    - After the active trigger clears and cooldown_minutes elapse in HALTED,
      transitions to COOLDOWN.
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
        self._halt_reason: Optional[str] = None
        self._data_integrity_fault: Optional[str] = None
        self._api_error_counts: dict[str, int] = defaultdict(int)

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_losses(self) -> int:
        """Current consecutive loss count for graduated sizing."""
        return self._consecutive_losses

    def get_loss_streak_multiplier(self) -> float:
        """
        Graduated risk reduction based on consecutive losses.

        Instead of binary halt at max_consecutive_losses, gradually
        reduce position size as losses accumulate:
          0 losses: 1.0x (full size)
          1 loss:   0.75x
          2 losses: 0.50x
          3 losses: 0.25x
          4+ losses: halt (handled by circuit breaker check)

        Returns multiplier in (0.0, 1.0].
        """
        if self._consecutive_losses <= 0:
            return 1.0
        elif self._consecutive_losses == 1:
            return 0.75
        elif self._consecutive_losses == 2:
            return 0.50
        elif self._consecutive_losses == 3:
            return 0.25
        else:
            return 0.0  # Should be halted by circuit breaker

    @property
    def is_trading_allowed(self) -> bool:
        """Allow new entries only in NORMAL or WARNING.

        COOLDOWN is reserved for monitoring and position management.  Treating
        it as entry-eligible created a one-tick window in which the bot could
        trade again before a persistent loss-streak trigger re-halted it.
        """
        return self._state in (CircuitState.NORMAL, CircuitState.WARNING)

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def _active_halt_reason(self) -> Optional[str]:
        """Return the highest-priority trigger that is still active."""
        if self._data_integrity_fault is not None:
            return self._data_integrity_fault
        if self._starting_equity > 0:
            total_pnl = self._daily_pnl + self._unrealized_pnl
            drawdown_pct = (
                abs(min(0, total_pnl)) / self._starting_equity * 100
            )
            if drawdown_pct >= self._config.drawdown_circuit_breaker_pct:
                return (
                    f"daily_drawdown: {drawdown_pct:.2f}% >= "
                    f"{self._config.drawdown_circuit_breaker_pct}% "
                    f"(realized={self._daily_pnl:.2f}, "
                    f"unrealized={self._unrealized_pnl:.2f})"
                )

            realized_loss_pct = (
                abs(min(0, self._daily_pnl))
                / self._starting_equity
                * 100
            )
            if (
                realized_loss_pct
                >= self._config.hard_daily_loss_limit_pct
            ):
                return (
                    f"hard_daily_loss: realized {realized_loss_pct:.2f}% >= "
                    f"{self._config.hard_daily_loss_limit_pct}% "
                    f"(realized={self._daily_pnl:.2f})"
                )

        if (
            self._consecutive_losses
            >= self._config.max_consecutive_losses
        ):
            return (
                f"consecutive_losses: {self._consecutive_losses} >= "
                f"{self._config.max_consecutive_losses}"
            )

        self._prune_old_errors()
        error_count = len(self._api_errors)
        if error_count >= self._config.api_error_halt_threshold:
            return (
                f"api_errors: {error_count} in 5 minutes >= "
                f"{self._config.api_error_halt_threshold}"
            )
        return None

    def check(self) -> CircuitState:
        """Evaluate triggers before considering any recovery transition."""
        now = now_et()
        active_reason = self._active_halt_reason()
        if active_reason is not None:
            self._halt(active_reason)
            return self._state

        # Recovery is possible only after the trigger itself has cleared.
        if self._state == CircuitState.HALTED and self._halted_at is not None:
            elapsed = now - self._halted_at
            if elapsed >= timedelta(minutes=self._cooldown_minutes):
                self._state = CircuitState.COOLDOWN
                self._cooldown_entered_at = now
                log.info(
                    "circuit.cooldown_entered",
                    halted_for_minutes=round(
                        elapsed.total_seconds() / 60, 1
                    ),
                )
                return self._state

        if (
            self._state == CircuitState.COOLDOWN
            and self._cooldown_entered_at is not None
        ):
            elapsed = now - self._cooldown_entered_at
            if elapsed >= timedelta(minutes=5):
                self._state = CircuitState.WARNING
                self._cooldown_entered_at = None
                self._halt_reason = None
                log.info(
                    "circuit.recovered_to_warning",
                    cooldown_minutes=round(
                        elapsed.total_seconds() / 60, 1
                    ),
                )
                return self._state

        if self._starting_equity > 0:
            total_pnl = self._daily_pnl + self._unrealized_pnl
            drawdown_pct = (
                abs(min(0, total_pnl)) / self._starting_equity * 100
            )
            if (
                drawdown_pct
                >= self._config.drawdown_circuit_breaker_pct * 0.75
            ):
                self._warn(
                    f"drawdown_approaching: {drawdown_pct:.2f}% "
                    f"(halt at "
                    f"{self._config.drawdown_circuit_breaker_pct}%)"
                )

        error_count = len(self._api_errors)
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
        try:
            if isinstance(unrealized_pnl, bool):
                raise ValueError("boolean P&L is invalid")
            normalized = float(unrealized_pnl)
        except (TypeError, ValueError, OverflowError) as exc:
            reason = f"unrealized_pnl_unverified: {exc}"
            self._unrealized_pnl = 0.0
            self._data_integrity_fault = reason
            self._halt(reason)
            return
        if not math.isfinite(normalized):
            reason = f"unrealized_pnl_unverified: {normalized!r}"
            self._unrealized_pnl = 0.0
            self._data_integrity_fault = reason
            self._halt(reason)
            return
        self._unrealized_pnl = normalized

    def record_partial_realized_pnl(
        self, pnl: float, *, defer_check: bool = False
    ) -> None:
        """Expose a partial close immediately without counting a full trade.

        Partial realized P&L contributes to daily drawdown as soon as the
        broker confirms it, while consecutive-win/loss semantics remain one
        event per completed position.
        """
        if not math.isfinite(pnl):
            raise ValueError(
                f"Partial realized P&L must be finite, got {pnl!r}"
            )
        self._daily_pnl += pnl
        log.info(
            "circuit.partial_realized_recorded",
            pnl=round(pnl, 2),
            daily_pnl=round(self._daily_pnl, 2),
        )
        if not defer_check:
            self.check()

    def record_trade_result(
        self,
        pnl: float,
        realized_already_recorded: float = 0.0,
        *,
        defer_check: bool = False,
    ) -> None:
        """Update counters after a trade closes."""
        if not math.isfinite(pnl) or not math.isfinite(
            realized_already_recorded
        ):
            raise ValueError(
                "Trade P&L and previously recorded P&L must be finite"
            )
        self._daily_pnl += pnl - realized_already_recorded

        if pnl < 0:
            self._consecutive_losses += 1
            log.info(
                "circuit.loss_recorded",
                pnl=round(pnl, 2),
                consecutive=self._consecutive_losses,
            )
        else:
            self._consecutive_losses = 0

        # PortfolioManager defers this evaluation until the orchestrator has
        # replaced the pre-exit unrealized total with the exact remaining-book
        # total. Direct callers retain the historical immediate behavior.
        if not defer_check:
            self.check()

    def record_api_error(self, error_type: Optional[str] = None) -> None:
        """Record an API error with timestamp and optional error type.

        Args:
            error_type: Optional category string for the error (e.g.,
                        "timeout", "rate_limit", "auth", "server_error").
                        When provided, the count for that type is incremented.
        """
        self._api_errors.append(now_et())

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
        self._halt_reason = None
        self._data_integrity_fault = None
        log.info("circuit.daily_reset", equity=round(equity, 2))

    def force_reset(self) -> None:
        """Manual reset (for admin/testing purposes)."""
        self._state = CircuitState.NORMAL
        self._consecutive_losses = 0
        self._api_errors.clear()
        self._api_error_counts.clear()
        self._halted_at = None
        self._cooldown_entered_at = None
        self._halt_reason = None
        self._data_integrity_fault = None
        log.warning("circuit.force_reset")

    def _halt(self, reason: str) -> None:
        """Transition to HALTED state."""
        self._halt_reason = reason
        if self._state != CircuitState.HALTED:
            self._state = CircuitState.HALTED
            self._halted_at = now_et()
            self._cooldown_entered_at = None
            log.critical("circuit.HALTED", reason=reason)

    def _warn(self, reason: str) -> None:
        """Transition to WARNING state (only if not already HALTED or COOLDOWN)."""
        if self._state == CircuitState.NORMAL:
            self._state = CircuitState.WARNING
            log.warning("circuit.warning", reason=reason)

    def _prune_old_errors(self) -> None:
        """Remove API errors older than the 5-minute window."""
        cutoff = now_et() - self._api_error_window
        while self._api_errors and self._api_errors[0] < cutoff:
            self._api_errors.popleft()

    def get_status(self) -> dict:
        """Get current circuit breaker status for monitoring."""
        return {
            "state": self._state.value,
            "halt_reason": self._halt_reason,
            "daily_pnl": round(self._daily_pnl, 2),
            "starting_equity": round(self._starting_equity, 2),
            "consecutive_losses": self._consecutive_losses,
            "api_errors_5min": len(self._api_errors),
            "api_error_summary": self.get_error_summary(),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
            "drawdown_pct": (
                round(
                    abs(min(0, self._daily_pnl + self._unrealized_pnl))
                    / self._starting_equity
                    * 100,
                    2,
                )
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
