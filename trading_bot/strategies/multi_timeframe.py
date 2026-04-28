"""Multi-timeframe confirmation for entry signals."""
from __future__ import annotations
import pandas as pd
import structlog
from trading_bot.utils.indicators import compute_ema

log = structlog.get_logger(__name__)

class MultiTimeframeConfirm:
    """
    Confirms entry signals against 5-minute and daily timeframe context.
    
    Rules:
    - 5-min EMA9 must be above EMA20 (short-term uptrend)
    - Daily close must be above daily EMA20 (medium-term trend support)
    - Returns a multiplier [0.5, 1.2] applied to confidence score
    """
    
    def confirm(self, bars_5min: pd.DataFrame, bars_daily: pd.DataFrame) -> float:
        """
        Return confidence multiplier based on higher timeframe alignment.
        
        Args:
            bars_5min: 5-minute OHLCV bars (at least 25 bars)
            bars_daily: Daily OHLCV bars (at least 25 bars)
        
        Returns:
            Multiplier in [0.5, 1.2]. 1.0 = neutral, >1.0 = aligned, <1.0 = conflicting.
        """
        multiplier = 1.0
        
        # 5-minute timeframe check
        if bars_5min is not None and not bars_5min.empty and len(bars_5min) >= 25:
            try:
                ema9 = compute_ema(bars_5min, length=9)
                ema20 = compute_ema(bars_5min, length=20)
                if not ema9.empty and not ema20.empty:
                    latest_ema9 = float(ema9.iloc[-1])
                    latest_ema20 = float(ema20.iloc[-1])
                    if latest_ema9 > latest_ema20:
                        multiplier *= 1.08  # 5-min trend aligned
                    else:
                        multiplier *= 0.85  # fighting 5-min downtrend
            except Exception:
                pass
        
        # Daily timeframe check
        if bars_daily is not None and not bars_daily.empty and len(bars_daily) >= 25:
            try:
                ema20_daily = compute_ema(bars_daily, length=20)
                if not ema20_daily.empty:
                    latest_close = float(bars_daily.iloc[-1]["close"] if "close" in bars_daily.columns else bars_daily.iloc[-1].get("Close", 0))
                    latest_ema20 = float(ema20_daily.iloc[-1])
                    if latest_close > latest_ema20:
                        multiplier *= 1.05  # daily trend support
                    else:
                        multiplier *= 0.80  # against daily trend
            except Exception:
                pass
        
        return round(max(0.5, min(1.2, multiplier)), 4)
