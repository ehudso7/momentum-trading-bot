"""Volume exhaustion detection for momentum gappers."""
from __future__ import annotations
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

class VolumeExhaustionDetector:
    """
    Detects volume exhaustion on momentum gappers.
    
    Compares current intraday volume rate (shares/minute) to the 
    initial burst volume. When the rate drops below a threshold,
    the move is likely exhausted.
    """
    
    EXHAUSTION_RATIO = 0.10  # Current rate < 10% of initial = exhausted
    INITIAL_BARS = 15  # First 15 bars define "initial burst"
    
    def is_exhausted(self, bars: pd.DataFrame) -> bool:
        """
        Check if volume has dried up relative to the initial move.
        
        Args:
            bars: Intraday OHLCV bars (1-minute).
            
        Returns:
            True if current volume rate is below threshold.
        """
        if bars is None or bars.empty or len(bars) < self.INITIAL_BARS + 5:
            return False
        
        try:
            cols = [c.lower() for c in bars.columns]
            vol_col = "volume" if "volume" in cols else None
            if vol_col is None:
                return False
            
            bars = bars.copy()
            bars.columns = [c.lower() for c in bars.columns]
            
            initial_vol = bars.iloc[:self.INITIAL_BARS]["volume"].mean()
            if initial_vol <= 0:
                return False
            
            recent_vol = bars.iloc[-5:]["volume"].mean()
            ratio = recent_vol / initial_vol
            
            if ratio < self.EXHAUSTION_RATIO:
                log.info(
                    "volume.exhausted",
                    initial_avg=round(initial_vol),
                    recent_avg=round(recent_vol),
                    ratio=round(ratio, 4),
                )
                return True
            
            return False
        except Exception:
            return False
    
    def get_volume_ratio(self, bars: pd.DataFrame) -> float:
        """Return current/initial volume ratio for scoring."""
        if bars is None or bars.empty or len(bars) < self.INITIAL_BARS + 5:
            return 1.0
        
        try:
            bars = bars.copy()
            bars.columns = [c.lower() for c in bars.columns]
            initial_vol = bars.iloc[:self.INITIAL_BARS]["volume"].mean()
            if initial_vol <= 0:
                return 1.0
            recent_vol = bars.iloc[-5:]["volume"].mean()
            return round(recent_vol / initial_vol, 4)
        except Exception:
            return 1.0
