"""Post-circuit-breaker recovery mode with reduced position sizing."""
from __future__ import annotations
import structlog

log = structlog.get_logger(__name__)

class RecoveryMode:
    """
    After a circuit breaker recovery, reduces position size for the
    first N trades. Returns to full size after M consecutive winners.
    
    This prevents the bot from immediately going full-size after a
    drawdown, which is a common cause of account blowups.
    """
    
    def __init__(
        self,
        recovery_trades: int = 3,
        size_reduction: float = 0.5,
        consecutive_wins_to_exit: int = 2,
    ):
        self._recovery_trades = recovery_trades
        self._size_reduction = size_reduction
        self._wins_to_exit = consecutive_wins_to_exit
        self._active = False
        self._trades_in_recovery: int = 0
        self._consecutive_wins: int = 0
    
    @property
    def is_active(self) -> bool:
        return self._active
    
    @property
    def size_multiplier(self) -> float:
        """Position size multiplier. 1.0 when not in recovery."""
        if not self._active:
            return 1.0
        return self._size_reduction
    
    def enter_recovery(self) -> None:
        """Activate recovery mode (called after circuit breaker recovery)."""
        self._active = True
        self._trades_in_recovery = 0
        self._consecutive_wins = 0
        log.warning(
            "recovery.entered",
            size_reduction=self._size_reduction,
            trades_required=self._recovery_trades,
        )
    
    def record_trade(self, pnl: float) -> None:
        """Record a trade result while in recovery mode."""
        if not self._active:
            return
        
        self._trades_in_recovery += 1
        
        if pnl > 0:
            self._consecutive_wins += 1
        else:
            self._consecutive_wins = 0
        
        # Exit recovery after consecutive wins
        if self._consecutive_wins >= self._wins_to_exit:
            self._exit_recovery("consecutive_wins")
            return
        
        # Exit recovery after enough trades regardless
        if self._trades_in_recovery >= self._recovery_trades + self._wins_to_exit:
            self._exit_recovery("max_trades_reached")
    
    def _exit_recovery(self, reason: str) -> None:
        """Deactivate recovery mode."""
        self._active = False
        log.info(
            "recovery.exited",
            reason=reason,
            trades_taken=self._trades_in_recovery,
            consecutive_wins=self._consecutive_wins,
        )
        self._trades_in_recovery = 0
        self._consecutive_wins = 0
    
    def reset(self) -> None:
        """Full reset."""
        self._active = False
        self._trades_in_recovery = 0
        self._consecutive_wins = 0
