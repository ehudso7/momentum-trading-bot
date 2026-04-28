"""Per-symbol risk tracking with automatic blacklisting."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta
import structlog

log = structlog.get_logger(__name__)

class SymbolBlacklist:
    """
    Tracks per-symbol trade results and auto-blacklists symbols with
    consecutive losses in a rolling window.
    
    A symbol is blacklisted when it produces N consecutive losses
    within a rolling window of M days. Blacklisted symbols are
    skipped during candidate evaluation.
    """
    
    def __init__(
        self,
        max_consecutive_losses: int = 3,
        rolling_window_days: int = 5,
        blacklist_duration_hours: int = 24,
    ):
        self._max_consecutive = max_consecutive_losses
        self._window = timedelta(days=rolling_window_days)
        self._blacklist_duration = timedelta(hours=blacklist_duration_hours)
        # symbol -> list of (datetime, pnl) tuples
        self._trade_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        # symbol -> blacklisted_until datetime
        self._blacklist: dict[str, datetime] = {}
    
    def record_trade(self, symbol: str, pnl: float) -> None:
        """Record a completed trade result for a symbol."""
        now = datetime.utcnow()
        self._trade_history[symbol].append((now, pnl))
        self._prune_old_trades(symbol)
        
        if self._has_consecutive_losses(symbol):
            self._blacklist[symbol] = now + self._blacklist_duration
            log.warning(
                "blacklist.symbol_blacklisted",
                symbol=symbol,
                consecutive_losses=self._max_consecutive,
                until=self._blacklist[symbol].isoformat(),
            )
    
    def is_blacklisted(self, symbol: str) -> bool:
        """Check if a symbol is currently blacklisted."""
        if symbol not in self._blacklist:
            return False
        if datetime.utcnow() >= self._blacklist[symbol]:
            del self._blacklist[symbol]
            log.info("blacklist.symbol_unblacklisted", symbol=symbol)
            return False
        return True
    
    def get_blacklisted_symbols(self) -> list[str]:
        """Return list of currently blacklisted symbols."""
        now = datetime.utcnow()
        active = [s for s, until in self._blacklist.items() if now < until]
        return active
    
    def _has_consecutive_losses(self, symbol: str) -> bool:
        """Check if symbol has N consecutive losses in the rolling window."""
        trades = self._trade_history.get(symbol, [])
        if len(trades) < self._max_consecutive:
            return False
        recent = trades[-self._max_consecutive:]
        return all(pnl < 0 for _, pnl in recent)
    
    def _prune_old_trades(self, symbol: str) -> None:
        """Remove trades outside the rolling window."""
        cutoff = datetime.utcnow() - self._window
        self._trade_history[symbol] = [
            (ts, pnl) for ts, pnl in self._trade_history[symbol] if ts >= cutoff
        ]
    
    def reset(self) -> None:
        """Clear all tracking data."""
        self._trade_history.clear()
        self._blacklist.clear()
