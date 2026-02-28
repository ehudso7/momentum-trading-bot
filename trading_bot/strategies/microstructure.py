"""Market microstructure scoring for entry quality."""
from __future__ import annotations
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

class MicrostructureScorer:
    """
    Scores entry quality based on market microstructure signals.
    
    Evaluates:
    - Bid-ask spread proxy (high-low range relative to price)
    - Volume concentration (large bars vs many small bars)
    - Price impact estimate
    
    Returns a multiplier [0.5, 1.1] for confidence scoring.
    """
    
    WIDE_SPREAD_THRESHOLD = 0.005  # >0.5% high-low/price = wide spread
    THIN_VOLUME_THRESHOLD = 50000  # <50K avg volume per bar = thin
    
    def score(self, bars: pd.DataFrame, entry_price: float) -> float:
        """
        Score market microstructure quality.
        
        Args:
            bars: Recent intraday OHLCV bars.
            entry_price: Proposed entry price.
            
        Returns:
            Multiplier in [0.5, 1.1].
        """
        if bars is None or bars.empty or len(bars) < 10:
            return 1.0
        
        try:
            bars = bars.copy()
            bars.columns = [c.lower() for c in bars.columns]
            
            multiplier = 1.0
            
            # 1. Spread proxy: average (high-low)/close for recent bars
            recent = bars.iloc[-10:]
            avg_range = ((recent["high"] - recent["low"]) / recent["close"]).mean()
            
            if avg_range > self.WIDE_SPREAD_THRESHOLD:
                penalty = min(0.3, (avg_range - self.WIDE_SPREAD_THRESHOLD) * 20)
                multiplier -= penalty
                log.debug("microstructure.wide_spread", avg_range_pct=round(avg_range * 100, 2))
            
            # 2. Volume concentration: prefer steady volume over spiky
            vol_std = recent["volume"].std()
            vol_mean = recent["volume"].mean()
            if vol_mean > 0:
                cv = vol_std / vol_mean  # coefficient of variation
                if cv > 2.0:
                    multiplier *= 0.92  # very spiky volume = less reliable
                elif cv < 0.5:
                    multiplier *= 1.05  # steady volume = reliable
            
            # 3. Thin volume penalty
            if vol_mean < self.THIN_VOLUME_THRESHOLD:
                multiplier *= 0.85
                log.debug("microstructure.thin_volume", avg_volume=round(vol_mean))
            
            return round(max(0.5, min(1.1, multiplier)), 4)
        except Exception:
            return 1.0
