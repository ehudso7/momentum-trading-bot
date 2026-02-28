"""Per-setup win rate tracking for adaptive confidence weighting."""
from __future__ import annotations
from collections import defaultdict
from typing import Optional
import structlog

log = structlog.get_logger(__name__)

class SetupTracker:
    """
    Tracks win rates per signal type (setup) and market regime.
    
    After sufficient data (min_trades), the tracker provides a
    confidence multiplier based on historical performance of each
    setup in the current regime.
    """
    
    MIN_TRADES = 20  # Minimum trades before adjusting weights
    
    def __init__(self, min_trades: int = 20):
        self._min_trades = min_trades
        # (setup, regime) -> list of P&L values
        self._results: dict[tuple[str, str], list[float]] = defaultdict(list)
        # setup -> list of P&L values (regime-agnostic)
        self._global_results: dict[str, list[float]] = defaultdict(list)
    
    def record_trade(self, setup: str, regime: str, pnl: float) -> None:
        """Record a completed trade result."""
        self._results[(setup, regime)].append(pnl)
        self._global_results[setup].append(pnl)
    
    def get_confidence_multiplier(self, setup: str, regime: str) -> float:
        """
        Get confidence multiplier based on historical win rate.
        
        Returns multiplier in [0.7, 1.3]:
        - Win rate > 60%: boost (up to 1.3)
        - Win rate 45-60%: neutral (1.0)
        - Win rate < 45%: penalty (down to 0.7)
        
        Falls back to global (all-regime) stats if regime-specific
        data is insufficient.
        """
        # Try regime-specific first
        key = (setup, regime)
        trades = self._results.get(key, [])
        
        if len(trades) >= self._min_trades:
            return self._compute_multiplier(trades)
        
        # Fall back to global stats
        global_trades = self._global_results.get(setup, [])
        if len(global_trades) >= self._min_trades:
            return self._compute_multiplier(global_trades)
        
        return 1.0  # Not enough data
    
    def get_win_rate(self, setup: str, regime: Optional[str] = None) -> Optional[float]:
        """Get win rate for a setup, optionally filtered by regime."""
        if regime:
            trades = self._results.get((setup, regime), [])
        else:
            trades = self._global_results.get(setup, [])
        
        if not trades:
            return None
        
        wins = sum(1 for pnl in trades if pnl > 0)
        return round(wins / len(trades) * 100, 1)
    
    def get_stats(self) -> dict:
        """Return summary stats for all setups."""
        stats = {}
        for setup, trades in self._global_results.items():
            if trades:
                wins = sum(1 for pnl in trades if pnl > 0)
                stats[setup] = {
                    "total_trades": len(trades),
                    "win_rate": round(wins / len(trades) * 100, 1),
                    "avg_pnl": round(sum(trades) / len(trades), 2),
                    "total_pnl": round(sum(trades), 2),
                }
        return stats
    
    def _compute_multiplier(self, trades: list[float]) -> float:
        """Compute confidence multiplier from trade history."""
        if not trades:
            return 1.0
        wins = sum(1 for pnl in trades if pnl > 0)
        win_rate = wins / len(trades)
        
        if win_rate > 0.65:
            return min(1.3, 1.0 + (win_rate - 0.65) * 2)
        elif win_rate > 0.60:
            return 1.0 + (win_rate - 0.60) * 2
        elif win_rate >= 0.45:
            return 1.0
        else:
            return max(0.7, 1.0 - (0.45 - win_rate) * 2)
    
    def reset(self) -> None:
        """Clear all tracking data."""
        self._results.clear()
        self._global_results.clear()
