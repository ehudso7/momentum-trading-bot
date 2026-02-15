"""
Circuit breaker for trading system safety.

Monitors daily drawdown, consecutive losses, and API errors.
Halts all trading when safety thresholds are breached.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from enum import Enum

import structlog

from trading_bot.config.settings import RiskConfig

log = structlog.get_logger(__name__)


class CircuitState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    HALTED = "halted"


class CircuitBreaker:
    """
    Monitors system health and halts trading when thresholds are breached.

    Conditions that trigger halt:
    - Daily drawdown exceeds drawdown_circuit_breaker_pct
    - Consecutive losing trades exceeds max_consecutive_losses
    - API error count exceeds threshold within 5-minute window
    """

    def __init__(self, config: RiskConfig):
        self._config = config
        self._state: CircuitState = CircuitState.NORMAL
        self._consecutive_losses: int = 0
        self._daily_pnl: float = 0.0
        self._starting_equity: float = 0.0
        self._api_errors: deque[datetime] = deque()
        self._api_error_window = timedelta(minutes=5)

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_trading_allowed(self) -> bool:
        return self._state != CircuitState.HALTED

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def check(self) -> CircuitState:
        """
        Evaluate all circuit breaker conditions.

        Returns current circuit state after evaluation.
        """
        if self._starting_equity <= 0:
            return self._state

        # 1. Daily drawdown check
        if self._starting_equity > 0:
            drawdown_pct = abs(min(0, self._daily_pnl)) / self._starting_equity * 100
            if drawdown_pct >= self._config.drawdown_circuit_breaker_pct:
                self._halt(
                    f"daily_drawdown: {drawdown_pct:.2f}% >= "
                    f"{self._config.drawdown_circuit_breaker_pct}%"
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

    def record_api_error(self) -> None:
        """Record an API error with timestamp."""
        self._api_errors.append(datetime.utcnow())
        log.warning(
            "circuit.api_error",
            recent_count=len(self._api_errors),
        )
        self.check()

    def reset_daily(self, equity: float) -> None:
        """Reset for a new trading day."""
        self._starting_equity = equity
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._api_errors.clear()
        self._state = CircuitState.NORMAL
        log.info("circuit.daily_reset", equity=round(equity, 2))

    def force_reset(self) -> None:
        """Manual reset (for admin/testing purposes)."""
        self._state = CircuitState.NORMAL
        self._consecutive_losses = 0
        self._api_errors.clear()
        log.warning("circuit.force_reset")

    def _halt(self, reason: str) -> None:
        """Transition to HALTED state."""
        if self._state != CircuitState.HALTED:
            self._state = CircuitState.HALTED
            log.critical("circuit.HALTED", reason=reason)

    def _warn(self, reason: str) -> None:
        """Transition to WARNING state (only if not already HALTED)."""
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
            "drawdown_pct": (
                round(abs(min(0, self._daily_pnl)) / self._starting_equity * 100, 2)
                if self._starting_equity > 0
                else 0.0
            ),
        }
