"""Compound mode for adaptive position sizing as equity grows."""
from __future__ import annotations
import structlog

log = structlog.get_logger(__name__)

class CompoundMode:
    """
    Adjusts position sizing as equity grows or shrinks.
    
    When enabled, the effective risk per trade scales with current equity
    relative to starting equity, but is bounded by floor and ceiling
    multipliers to prevent runaway risk.
    
    Key principle: risk the same PERCENTAGE, but let the dollar amount
    grow naturally with equity. The floor prevents over-sizing after
    a drawdown, and the ceiling prevents reckless scaling after a run.
    """
    
    def __init__(
        self,
        enabled: bool = True,
        floor_multiplier: float = 0.5,
        ceiling_multiplier: float = 2.0,
        growth_smoothing: float = 0.5,
    ):
        """
        Args:
            enabled: Whether compound mode is active.
            floor_multiplier: Minimum equity multiplier (0.5 = half original).
            ceiling_multiplier: Maximum equity multiplier (2.0 = double).
            growth_smoothing: How much to smooth equity changes (0-1).
                0 = use starting equity always (no compounding).
                1 = use current equity fully.
                0.5 = blend 50/50 (default, conservative).
        """
        self._enabled = enabled
        self._floor = floor_multiplier
        self._ceiling = ceiling_multiplier
        self._smoothing = growth_smoothing
        self._starting_equity: float = 0.0
    
    @property
    def is_enabled(self) -> bool:
        return self._enabled
    
    def set_starting_equity(self, equity: float) -> None:
        """Set the baseline equity for compound calculations."""
        self._starting_equity = equity
    
    def get_effective_equity(self, current_equity: float) -> float:
        """
        Compute the effective equity for position sizing.
        
        Returns a blended equity value between starting and current,
        bounded by floor and ceiling multipliers.
        
        Args:
            current_equity: Current account equity.
            
        Returns:
            Effective equity for position sizing calculations.
        """
        if not self._enabled or self._starting_equity <= 0:
            return current_equity
        
        # Blend between starting and current equity
        blended = (
            self._starting_equity * (1 - self._smoothing)
            + current_equity * self._smoothing
        )
        
        # Apply floor and ceiling relative to starting equity
        floor_equity = self._starting_equity * self._floor
        ceiling_equity = self._starting_equity * self._ceiling
        
        effective = max(floor_equity, min(ceiling_equity, blended))
        
        if effective != current_equity:
            log.debug(
                "compound.adjusted",
                current=round(current_equity, 2),
                effective=round(effective, 2),
                starting=round(self._starting_equity, 2),
                multiplier=round(effective / self._starting_equity, 2),
            )
        
        return round(effective, 2)
    
    def get_multiplier(self, current_equity: float) -> float:
        """
        Get the compound multiplier relative to starting equity.
        
        Returns value in [floor, ceiling].
        """
        if not self._enabled or self._starting_equity <= 0:
            return 1.0
        
        effective = self.get_effective_equity(current_equity)
        return round(effective / self._starting_equity, 4)
