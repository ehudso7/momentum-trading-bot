"""
VWAP Pullback + Breakout Continuation Strategy.

Primary setup: First strong pullback to VWAP or EMA9 with volume confirmation.
Secondary setup: Breakout continuation when momentum re-accelerates.

Long only (v1). Implements scale-out exits and trailing stops.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd
import structlog

from trading_bot.config.settings import AppConfig
from trading_bot.models.domain import (
    PositionInfo,
    ScanResult,
    SignalType,
    TradeSignal,
)
from trading_bot.strategies.base import Strategy
from trading_bot.utils.helpers import is_past_exit_time
from trading_bot.utils.indicators import enrich_dataframe

log = structlog.get_logger(__name__)


class PullbackVWAPStrategy(Strategy):
    """
    VWAP/EMA9 pullback with volume confirmation.

    Entry conditions (checked in order):
    1. VWAP pullback: price pulls back to within vwap_proximity_pct of VWAP,
       then reclaims with above-average volume.
    2. EMA9 pullback: same logic anchored to EMA9.
    3. Breakout continuation: price breaks above recent highs with volume spike.

    Exit conditions (priority order):
    1. Hard time exit (3:50 PM ET)
    2. Stop loss hit
    3. Scale-out at R:R targets
    4. Trailing stop hit
    5. Parabolic SAR flip
    """

    def __init__(self, config: AppConfig):
        self._entry_cfg = config.entry
        self._exit_cfg = config.exit
        self._risk_cfg = config.risk

    def evaluate(
        self, candidate: ScanResult, bars: pd.DataFrame
    ) -> Optional[TradeSignal]:
        """
        Evaluate a candidate for entry signal.

        Requires at least 20 bars of data for indicator computation.
        """
        if bars.empty or len(bars) < 20:
            log.debug(
                "strategy.insufficient_bars",
                symbol=candidate.symbol,
                bars=len(bars),
            )
            return None

        # Enrich with indicators
        df = enrich_dataframe(
            bars,
            ema_length=self._entry_cfg.ema_period,
            atr_length=14,
        )

        # Get latest values
        latest = df.iloc[-1]
        price = latest["close"]
        vwap = latest.get("vwap", price)
        ema = latest.get(f"ema_{self._entry_cfg.ema_period}", price)
        atr = latest.get("atr_14", 0.0)

        if pd.isna(atr) or atr <= 0:
            log.debug("strategy.no_atr", symbol=candidate.symbol)
            return None

        # Check each setup in priority order
        signal = self._check_vwap_pullback(candidate, df, price, vwap, ema, atr)
        if signal:
            return signal

        signal = self._check_ema_pullback(candidate, df, price, ema, atr)
        if signal:
            return signal

        signal = self._check_breakout(candidate, df, price, vwap, ema, atr)
        if signal:
            return signal

        return None

    def should_exit(
        self, position: PositionInfo, bars: pd.DataFrame, current_time_et=None
    ) -> tuple[bool, str]:
        """Check all exit conditions in priority order."""

        # 1. Hard time exit
        if is_past_exit_time(self._exit_cfg.hard_time_exit):
            return True, "hard_time_exit"

        # 2. Stop loss
        if position.current_price <= position.stop_price:
            return True, "stop_loss"

        # 3. Trailing stop
        if (
            position.trailing_stop_active
            and position.trailing_stop_price
            and position.current_price <= position.trailing_stop_price
        ):
            return True, "trailing_stop"

        # 4. Parabolic SAR flip (if enabled and we have bar data)
        if self._exit_cfg.use_parabolic_sar and not bars.empty and len(bars) >= 20:
            df = enrich_dataframe(bars, ema_length=self._entry_cfg.ema_period)
            latest = df.iloc[-1]
            psar_short = latest.get("psar_short")
            if psar_short is not None and not pd.isna(psar_short):
                # SAR is above price = bearish signal
                if psar_short <= position.current_price * 1.005:
                    return True, "psar_flip"

        return False, ""

    def compute_scale_out(
        self, position: PositionInfo, current_price: float
    ) -> Optional[tuple[int, str]]:
        """
        Determine if a scale-out is due based on R:R targets.

        Scale-out schedule:
          - scale_out 0: 1/3 shares at 1:1 R:R
          - scale_out 1: 1/3 shares at 2:1 R:R
          - scale_out 2: remainder on trailing stop or time exit
        """
        if position.scale_outs_completed >= len(self._exit_cfg.scale_out_rr_targets):
            return None

        risk_per_share = abs(position.entry_price - position.stop_price)
        if risk_per_share == 0:
            return None

        target_rr = self._exit_cfg.scale_out_rr_targets[position.scale_outs_completed]
        target_price = position.entry_price + (risk_per_share * target_rr)

        if current_price >= target_price:
            ratio = self._exit_cfg.scale_out_ratios[position.scale_outs_completed]
            shares_to_sell = max(1, math.floor(position.shares * ratio))
            # Don't sell more than remaining
            shares_to_sell = min(shares_to_sell, position.shares_remaining)

            reason = f"scale_out_{position.scale_outs_completed + 1}_at_{target_rr}R"
            return shares_to_sell, reason

        return None

    def get_trailing_stop(
        self, position: PositionInfo, bars: pd.DataFrame
    ) -> Optional[float]:
        """
        Compute trailing stop level.

        Activated after first scale-out. Uses ATR-based trailing.
        Only ratchets UP, never down.
        """
        # Only activate after at least one scale-out
        if position.scale_outs_completed < 1:
            return None

        if bars.empty or len(bars) < 20:
            return None

        df = enrich_dataframe(bars, ema_length=self._entry_cfg.ema_period)
        latest = df.iloc[-1]
        atr = latest.get("atr_14", 0.0)

        if pd.isna(atr) or atr <= 0:
            return None

        # ATR-based trailing stop
        new_stop = position.current_price - (
            atr * self._exit_cfg.trailing_stop_atr_multiplier
        )

        # Minimum: breakeven + buffer
        buffer = position.entry_price * (
            self._exit_cfg.trailing_stop_breakeven_buffer_pct / 100
        )
        breakeven_stop = position.entry_price + buffer
        new_stop = max(new_stop, breakeven_stop)

        # Only ratchet UP
        if position.trailing_stop_price and new_stop <= position.trailing_stop_price:
            return None

        return round(new_stop, 4)

    # --- Private helper methods ---

    def _check_vwap_pullback(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        vwap: float,
        ema: float,
        atr: float,
    ) -> Optional[TradeSignal]:
        """Check for VWAP pullback entry."""
        if pd.isna(vwap) or vwap <= 0:
            return None

        proximity_pct = abs(price - vwap) / vwap * 100

        # Price must be near VWAP (within proximity threshold)
        if proximity_pct > self._entry_cfg.vwap_proximity_pct:
            return None

        # Price must be reclaiming (current close > open = bullish bar)
        latest = df.iloc[-1]
        if latest["close"] <= latest["open"]:
            return None

        # Price must be above VWAP (reclaimed, not still below)
        if price < vwap:
            return None

        # Volume confirmation: current bar volume > multiplier * recent avg
        if not self._volume_confirms(df):
            return None

        # Build signal
        stop_price = self._compute_stop(df, price, atr)
        return self._build_signal(
            candidate, SignalType.VWAP_PULLBACK, price, stop_price, atr, vwap, ema
        )

    def _check_ema_pullback(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        ema: float,
        atr: float,
    ) -> Optional[TradeSignal]:
        """Check for EMA pullback entry (fallback if VWAP fails)."""
        ema_col = f"ema_{self._entry_cfg.ema_period}"
        if ema_col not in df.columns or pd.isna(ema) or ema <= 0:
            return None

        proximity_pct = abs(price - ema) / ema * 100

        if proximity_pct > self._entry_cfg.vwap_proximity_pct * 1.5:
            return None

        latest = df.iloc[-1]
        if latest["close"] <= latest["open"]:
            return None

        if price < ema:
            return None

        if not self._volume_confirms(df):
            return None

        vwap = df.iloc[-1].get("vwap", price)
        stop_price = self._compute_stop(df, price, atr)
        return self._build_signal(
            candidate, SignalType.EMA_PULLBACK, price, stop_price, atr, vwap, ema
        )

    def _check_breakout(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        vwap: float,
        ema: float,
        atr: float,
    ) -> Optional[TradeSignal]:
        """Check for breakout continuation entry."""
        lookback = self._entry_cfg.breakout_lookback_bars
        if len(df) < lookback + 1:
            return None

        # Check consolidation: recent bars should have tight range
        recent = df.iloc[-(lookback + 1) : -1]
        high_range = recent["high"].max() - recent["low"].min()

        # Consolidation means range is less than 2x ATR
        latest_atr = df.iloc[-1].get("atr_14", atr)
        if pd.isna(latest_atr) or latest_atr <= 0:
            return None

        if high_range > 2.0 * latest_atr:
            return None  # Not tight enough consolidation

        # Breakout: current close > highest high of lookback period
        recent_high = recent["high"].max()
        if price <= recent_high:
            return None

        # Volume confirmation
        if not self._volume_confirms(df):
            return None

        stop_price = self._compute_stop(df, price, atr)
        return self._build_signal(
            candidate,
            SignalType.BREAKOUT_CONTINUATION,
            price,
            stop_price,
            atr,
            vwap if not pd.isna(vwap) else price,
            ema if not pd.isna(ema) else price,
        )

    def _volume_confirms(self, df: pd.DataFrame) -> bool:
        """Check if current bar volume confirms the move."""
        if len(df) < 10:
            return False

        current_vol = df.iloc[-1]["volume"]
        avg_vol = df.iloc[-10:-1]["volume"].mean()

        if avg_vol <= 0:
            return False

        return current_vol >= avg_vol * self._entry_cfg.volume_confirmation_multiplier

    def _compute_stop(
        self, df: pd.DataFrame, entry_price: float, atr: float
    ) -> float:
        """
        Compute stop-loss price.

        Uses the most conservative (tightest) of:
        - Entry bar low
        - Swing low of last 10 bars
        - ATR-based: entry - (ATR * multiplier)

        But ensures minimum distance of min_atr_distance * ATR.
        """
        entry_bar_low = df.iloc[-1]["low"]
        swing_low = df.iloc[-10:]["low"].min()
        atr_stop = entry_price - (atr * self._risk_cfg.stop_loss_atr_multiplier)

        # Use highest (tightest) stop
        stop = max(entry_bar_low, swing_low, atr_stop)

        # Ensure minimum distance
        min_distance = atr * self._entry_cfg.min_atr_distance
        if entry_price - stop < min_distance:
            stop = entry_price - min_distance

        # Stop must be below entry
        if stop >= entry_price:
            stop = entry_price - min_distance

        return round(stop, 4)

    def _build_signal(
        self,
        candidate: ScanResult,
        signal_type: SignalType,
        entry_price: float,
        stop_price: float,
        atr: float,
        vwap: float,
        ema: float,
    ) -> Optional[TradeSignal]:
        """Build a TradeSignal with computed targets."""
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return None

        # Compute targets from R:R ratios
        targets = []
        for rr in self._exit_cfg.scale_out_rr_targets:
            targets.append(round(entry_price + (risk_per_share * rr), 4))

        # Confidence based on candidate score and setup quality
        confidence = min(candidate.score, 1.0)

        signal = TradeSignal(
            symbol=candidate.symbol,
            signal_type=signal_type,
            entry_price=round(entry_price, 4),
            stop_price=round(stop_price, 4),
            target_prices=targets,
            atr=round(atr, 4),
            vwap=round(vwap, 4) if not pd.isna(vwap) else entry_price,
            ema9=round(ema, 4) if not pd.isna(ema) else entry_price,
            confidence=round(confidence, 4),
        )

        log.info(
            "strategy.signal",
            symbol=signal.symbol,
            type=signal.signal_type.value,
            entry=signal.entry_price,
            stop=signal.stop_price,
            targets=signal.target_prices,
            rr=signal.reward_risk_ratio,
        )

        return signal
