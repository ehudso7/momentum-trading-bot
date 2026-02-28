"""Pre-market volume profile analysis for watchlist quality scoring."""
from __future__ import annotations
from typing import Optional
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

class PremarketProfile:
    """
    Analyzes pre-market volume profile to score watchlist candidates.
    
    Stocks that consolidate tightly near VWAP in pre-market (low range,
    steady volume) tend to produce better opening drive moves than
    those that are volatile pre-market.
    """
    
    def score(self, premarket_bars: pd.DataFrame) -> float:
        """
        Score pre-market quality based on volume profile.
        
        Args:
            premarket_bars: Pre-market OHLCV bars.
            
        Returns:
            Score multiplier in [0.7, 1.3].
            Higher = tighter consolidation = better setup.
        """
        if premarket_bars is None or premarket_bars.empty or len(premarket_bars) < 3:
            return 1.0
        
        try:
            bars = premarket_bars.copy()
            bars.columns = [c.lower() for c in bars.columns]
            
            multiplier = 1.0
            
            # 1. Range tightness: smaller range relative to price = better
            total_high = bars["high"].max()
            total_low = bars["low"].min()
            mid_price = (total_high + total_low) / 2
            if mid_price > 0:
                range_pct = (total_high - total_low) / mid_price * 100
                if range_pct < 3.0:
                    multiplier *= 1.15  # Tight consolidation
                elif range_pct < 5.0:
                    multiplier *= 1.05  # Moderate
                elif range_pct > 10.0:
                    multiplier *= 0.85  # Too volatile pre-market
            
            # 2. Volume consistency: steady volume = real interest
            vol_std = bars["volume"].std()
            vol_mean = bars["volume"].mean()
            if vol_mean > 0:
                cv = vol_std / vol_mean
                if cv < 0.5:
                    multiplier *= 1.10  # Very steady
                elif cv > 1.5:
                    multiplier *= 0.90  # Spiky, unreliable
            
            # 3. Closing near high = bullish pre-market
            last_close = bars.iloc[-1]["close"]
            if total_high > total_low:
                close_position = (last_close - total_low) / (total_high - total_low)
                if close_position > 0.7:
                    multiplier *= 1.08  # Closing near highs
                elif close_position < 0.3:
                    multiplier *= 0.88  # Fading pre-market
            
            return round(max(0.7, min(1.3, multiplier)), 4)
        except Exception:
            return 1.0
    
    def get_vwap_distance(self, premarket_bars: pd.DataFrame) -> Optional[float]:
        """Return distance from last price to pre-market VWAP in percent."""
        if premarket_bars is None or premarket_bars.empty:
            return None
        try:
            bars = premarket_bars.copy()
            bars.columns = [c.lower() for c in bars.columns]
            tp = (bars["high"] + bars["low"] + bars["close"]) / 3
            vwap = (tp * bars["volume"]).sum() / bars["volume"].sum()
            if vwap <= 0:
                return None
            last_price = bars.iloc[-1]["close"]
            return round((last_price - vwap) / vwap * 100, 2)
        except Exception:
            return None
