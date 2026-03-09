"""
VWAP Pullback + Breakout Continuation + Opening Range Breakout Strategy.

Primary setup: First strong pullback to VWAP or EMA9 with volume confirmation.
Secondary setups: Breakout continuation, Opening Range Breakout (ORB),
Red-to-Green move, Pre-market High break.

Long only (v1). Implements scale-out exits, trailing stops, and multi-factor
confidence scoring modeled after elite momentum day-trading (Ross Cameron style).

Elite-level features:
- Multi-factor confidence scoring (time of day, volume profile, candle quality,
  momentum consistency, gap fill risk)
- Opening Range Breakout (first 15-min high/low as range)
- Red-to-Green move detection (opens red, reclaims prev close)
- Gap fill risk reduction (>50% gap filled = lower confidence)
- Time-of-day weighting (power zone 9:30-10:30, dead zone 11:30-1:00)
- Momentum exhaustion detection via RSI divergence
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
from trading_bot.strategies.multi_timeframe import MultiTimeframeConfirm
from trading_bot.strategies.volume_profile import VolumeExhaustionDetector
from trading_bot.strategies.microstructure import MicrostructureScorer
from trading_bot.utils.helpers import is_past_exit_time, now_et
from trading_bot.utils.indicators import enrich_dataframe

log = structlog.get_logger(__name__)


class PullbackVWAPStrategy(Strategy):
    """
    Elite-level VWAP/EMA9 pullback strategy with multi-factor confidence.

    Entry setups (checked in order):
    1. VWAP pullback: price pulls back to within vwap_proximity_pct of VWAP,
       then reclaims with above-average volume.
    2. EMA9 pullback: same logic anchored to EMA9.
    3. Opening Range Breakout: price breaks above first 15-min high with volume.
    4. Red-to-Green: opens below prev close, reclaims with momentum.
    5. Breakout continuation: price breaks above recent highs with volume spike.

    Exit conditions (priority order):
    1. Hard time exit (3:50 PM ET)
    2. Stop loss hit
    3. Scale-out at R:R targets
    4. Trailing stop hit
    5. Momentum exhaustion (RSI > 80 after extended run)
    6. Parabolic SAR flip

    Confidence scoring factors:
    - Base signal quality (proximity, volume, candle pattern)
    - Time of day (power zone 9:30-10:30 = boost, dead zone = penalty)
    - Gap fill risk (>50% filled = penalty)
    - Multi-candle momentum (consecutive green bars = boost)
    - Volume trend (increasing = boost, decreasing = penalty)
    - RSI level (overbought on entry = penalty)
    - EMA alignment (EMA9 > EMA20 > VWAP = boost)
    """

    # Time-of-day zones (hour, minute) in ET
    POWER_ZONE_END_HOUR = 10
    POWER_ZONE_END_MIN = 30
    DEAD_ZONE_START_HOUR = 11
    DEAD_ZONE_START_MIN = 30
    DEAD_ZONE_END_HOUR = 13

    # Opening range (first N minutes after open)
    ORB_MINUTES = 15

    # RSI thresholds
    RSI_OVERBOUGHT = 75

    def __init__(self, config: AppConfig):
        self._entry_cfg = config.entry
        self._exit_cfg = config.exit
        self._risk_cfg = config.risk

        # Regime-adaptive parameters (updated each tick by main loop)
        self._current_regime: str = "low_volatility"
        self._proximity_multiplier: float = 1.0

        self._mtf = MultiTimeframeConfirm()
        self._volume_exhaust = VolumeExhaustionDetector()
        self._microstructure = MicrostructureScorer()

    def set_regime(self, regime: str, adjustments: dict) -> None:
        """
        Update regime for adaptive entry parameters.

        Called each tick by the main loop after regime detection.
        Adjusts entry proximity thresholds to match market conditions:
        - Low vol / range-bound: widen zones (more setups qualify)
        - High vol: tighten zones (higher conviction required)
        - Trending bullish: normal zones (go with the flow)
        - Trending bearish: tighten significantly (be very selective)
        """
        self._current_regime = regime

        # Map regime to proximity multiplier
        # Higher = wider entry zone = more trades
        # Lower = tighter entry zone = fewer but higher conviction
        #
        # Key insight: momentum gappers often trade 3-5% above VWAP
        # after the gap. The multiplier must be wide enough to catch
        # the first pullback toward VWAP, not just stocks sitting on it.
        #
        # With base vwap_proximity_pct=1.5%:
        #   low_vol: 1.5% * 2.5 = 3.75% zone (catches shallow pullbacks)
        #   range:   1.5% * 2.0 = 3.0%  zone
        #   bullish: 1.5% * 1.5 = 2.25% zone
        #   high_vol: 1.5% * 1.0 = 1.5% zone (tight, high conviction)
        #   bearish: 1.5% * 0.8 = 1.2%  zone (very selective)
        _PROXIMITY_MAP = {
            "trending_bullish": 1.5,   # Bullish trend = wider for momentum
            "trending_bearish": 0.8,   # Bearish = selective
            "range_bound": 2.0,        # Range = wider to catch pullbacks
            "high_volatility": 1.0,    # High vol = standard, conviction
            "low_volatility": 2.5,     # Low vol = widest, catch shallow pullbacks
        }
        self._proximity_multiplier = _PROXIMITY_MAP.get(regime, 1.0)

        log.info(
            "strategy.regime_updated",
            regime=regime,
            proximity_multiplier=self._proximity_multiplier,
        )

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
        ema20 = latest.get("ema_20", price)
        atr = latest.get("atr_14", 0.0)
        rsi = latest.get("rsi_14", 50.0)

        if pd.isna(atr) or atr <= 0:
            log.debug("strategy.no_atr", symbol=candidate.symbol)
            return None

        # Volume exhaustion filter
        if self._volume_exhaust.is_exhausted(bars):
            log.info("strategy.volume_exhausted", symbol=candidate.symbol)
            return None

        # Check each setup in priority order, with multi-bar lookback
        # Try current bar first, then check recent bars for missed signals
        setups = [
            ("vwap_pullback", self._check_vwap_pullback),
            ("ema_pullback", self._check_ema_pullback),
            ("orb", self._check_opening_range_breakout),
            ("red_to_green", self._check_red_to_green),
            ("breakout", self._check_breakout),
        ]

        rejection_reasons = []

        for setup_name, check_fn in setups:
            if setup_name == "vwap_pullback":
                signal = check_fn(candidate, df, price, vwap, ema, ema20, atr, rsi)
            elif setup_name == "ema_pullback":
                signal = check_fn(candidate, df, price, ema, ema20, atr, rsi)
            elif setup_name in ("orb", "red_to_green"):
                signal = check_fn(candidate, df, price, vwap, ema, atr, rsi)
            else:
                signal = check_fn(candidate, df, price, vwap, ema, atr, rsi)

            if signal:
                return signal

        # If no signal on current bar, check last 2 bars for missed entries
        for lookback_offset in [2, 3]:
            if len(df) < lookback_offset + 20:
                continue
            lb_df = df.iloc[:-lookback_offset + 1]
            lb_latest = lb_df.iloc[-1]
            lb_price = lb_latest["close"]
            lb_vwap = lb_latest.get("vwap", lb_price)
            lb_ema = lb_latest.get(f"ema_{self._entry_cfg.ema_period}", lb_price)
            lb_ema20 = lb_latest.get("ema_20", lb_price)
            lb_atr = lb_latest.get("atr_14", 0.0)
            lb_rsi = lb_latest.get("rsi_14", 50.0)

            if pd.isna(lb_atr) or lb_atr <= 0:
                continue

            # Only try VWAP, EMA, and ORB for lookback (not breakout)
            for setup_name, check_fn in setups[:3]:
                if setup_name == "vwap_pullback":
                    signal = check_fn(candidate, lb_df, lb_price, lb_vwap, lb_ema, lb_ema20, lb_atr, lb_rsi)
                elif setup_name == "ema_pullback":
                    signal = check_fn(candidate, lb_df, lb_price, lb_ema, lb_ema20, lb_atr, lb_rsi)
                else:
                    signal = check_fn(candidate, lb_df, lb_price, lb_vwap, lb_ema, lb_atr, lb_rsi)

                if signal:
                    # Use current price for entry, not the lookback bar's price
                    signal.entry_price = round(price, 4)
                    # Revalidate stop distance with current price
                    if signal.stop_price >= price:
                        signal.stop_price = round(price - (atr * self._entry_cfg.min_atr_distance), 4)
                    log.info(
                        "strategy.lookback_signal",
                        symbol=candidate.symbol,
                        type=signal.signal_type.value,
                        lookback_bars=lookback_offset - 1,
                    )
                    return signal

        # Log why no signal was generated (diagnostic)
        log.info(
            "strategy.no_signal",
            symbol=candidate.symbol,
            price=round(price, 4),
            vwap=round(vwap, 4) if not pd.isna(vwap) else None,
            ema9=round(ema, 4) if not pd.isna(ema) else None,
            atr=round(atr, 4),
            rsi=round(rsi, 1) if not pd.isna(rsi) else None,
            vwap_dist_pct=round(abs(price - vwap) / vwap * 100, 2) if not pd.isna(vwap) and vwap > 0 else None,
            ema_dist_pct=round(abs(price - ema) / ema * 100, 2) if not pd.isna(ema) and ema > 0 else None,
            bar_bullish=df.iloc[-1]["close"] > df.iloc[-1]["open"],
            vol_confirms=self._volume_confirms(df),
            bars_available=len(df),
        )

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

        # 4. Momentum exhaustion (RSI-based exit for runners)
        if not bars.empty and len(bars) >= 20:
            df = enrich_dataframe(bars, ema_length=self._entry_cfg.ema_period)
            latest = df.iloc[-1]

            # RSI > 80 with position at +2R or more = momentum fading
            rsi = latest.get("rsi_14", 50.0)
            if not pd.isna(rsi) and rsi > 80.0 and position.r_multiple >= 2.0:
                # Confirm with bearish candle
                if latest["close"] < latest["open"]:
                    return True, "momentum_exhaustion"

            # 5. Parabolic SAR flip
            if self._exit_cfg.use_parabolic_sar:
                psar_short = latest.get("psar_short")
                if psar_short is not None and not pd.isna(psar_short):
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

        Activated once position reaches 0.5R profit (breakeven protection)
        or after first scale-out. Uses ATR-based trailing.
        Only ratchets UP, never down.
        """
        # Activate breakeven protection early: once position is at +0.5R,
        # start trailing to lock in gains and prevent winners turning to losers.
        r_multiple = position.r_multiple
        if position.scale_outs_completed < 1 and r_multiple < 0.5:
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
        ema20: float,
        atr: float,
        rsi: float,
    ) -> Optional[TradeSignal]:
        """Check for VWAP pullback entry with regime-adaptive proximity."""
        if pd.isna(vwap) or vwap <= 0:
            return None

        proximity_pct = abs(price - vwap) / vwap * 100

        # Regime-adaptive proximity threshold
        adaptive_proximity = self._entry_cfg.vwap_proximity_pct * self._proximity_multiplier

        # Price must be near VWAP (within adaptive proximity threshold)
        if proximity_pct > adaptive_proximity:
            return None

        # Price must be reclaiming (current close > open = bullish bar)
        latest = df.iloc[-1]
        if latest["close"] <= latest["open"]:
            return None

        # Price must be above VWAP (reclaimed, not still below)
        if price < vwap:
            return None

        # Volume confirmation: check last 3 bars, not just current
        if not self._volume_confirms(df):
            return None

        # Build signal with confidence scoring
        stop_price = self._compute_stop(df, price, atr, vwap=vwap)
        signal = self._build_signal(
            candidate, SignalType.VWAP_PULLBACK, price, stop_price, atr, vwap, ema
        )
        if signal:
            signal.confidence = self._compute_confidence(
                signal, candidate, df, rsi, ema, ema20, vwap
            )
        return signal

    def _check_ema_pullback(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        ema: float,
        ema20: float,
        atr: float,
        rsi: float,
    ) -> Optional[TradeSignal]:
        """Check for EMA pullback entry with regime-adaptive proximity."""
        ema_col = f"ema_{self._entry_cfg.ema_period}"
        if ema_col not in df.columns or pd.isna(ema) or ema <= 0:
            return None

        proximity_pct = abs(price - ema) / ema * 100

        # Regime-adaptive EMA proximity (wider than VWAP by 1.5x baseline)
        adaptive_proximity = self._entry_cfg.vwap_proximity_pct * 1.5 * self._proximity_multiplier

        if proximity_pct > adaptive_proximity:
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
        signal = self._build_signal(
            candidate, SignalType.EMA_PULLBACK, price, stop_price, atr, vwap, ema
        )
        if signal:
            signal.confidence = self._compute_confidence(
                signal, candidate, df, rsi, ema, ema20, vwap
            )
        return signal

    def _check_opening_range_breakout(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        vwap: float,
        ema: float,
        atr: float,
        rsi: float,
    ) -> Optional[TradeSignal]:
        """
        Check for Opening Range Breakout (ORB) entry.

        The opening range is the high/low of the first 15 minutes after
        market open (9:30 - 9:45 AM ET). A break above the ORB high
        with volume is a classic momentum entry.
        """
        if len(df) < 20:
            return None

        # Identify today's opening range (first 15 min after 9:30 AM ET).
        # Must handle both timezone-aware indexes (Polygon) and naive
        # indexes (yfinance fallback with multi-day data).
        orb_bars = pd.DataFrame()
        try:
            idx = df.index
            if hasattr(idx, 'tz') and idx.tz is not None:
                orb_bars = df.between_time("09:30", "09:45")
            elif hasattr(idx, 'date'):
                # Naive DatetimeIndex — filter to today's bars, take first 15
                today = idx[-1].date() if hasattr(idx[-1], 'date') else None
                if today is not None:
                    today_bars = df[idx.date == today]
                    orb_bars = today_bars.iloc[:self.ORB_MINUTES]
                else:
                    orb_bars = df.iloc[:self.ORB_MINUTES]
            else:
                orb_bars = df.iloc[:self.ORB_MINUTES]
        except Exception:
            orb_bars = df.iloc[:self.ORB_MINUTES]

        if orb_bars.empty or len(orb_bars) < 3:
            return None

        orb_high = orb_bars["high"].max()
        orb_low = orb_bars["low"].min()

        # Only valid if we have bars after the ORB window
        if len(df) <= len(orb_bars):
            return None

        # Price must break above ORB high
        latest = df.iloc[-1]
        if price <= orb_high:
            return None

        # Must be a bullish bar
        if latest["close"] <= latest["open"]:
            return None

        # Volume confirmation
        if not self._volume_confirms(df):
            return None

        # Price above VWAP
        if not pd.isna(vwap) and price < vwap:
            return None

        # Stop below ORB low or VWAP, whichever is higher (tighter risk)
        stop_price = orb_low
        if not pd.isna(vwap) and vwap > orb_low:
            stop_price = vwap - (atr * 0.25)

        # Ensure minimum stop distance
        min_distance = atr * self._entry_cfg.min_atr_distance
        if price - stop_price < min_distance:
            stop_price = price - min_distance

        stop_price = round(stop_price, 4)

        ema20 = df.iloc[-1].get("ema_20", price)
        signal = self._build_signal(
            candidate, SignalType.OPENING_RANGE_BREAKOUT,
            price, stop_price, atr, vwap, ema
        )
        if signal:
            signal.confidence = self._compute_confidence(
                signal, candidate, df, rsi, ema, ema20, vwap
            )
        return signal

    def _check_red_to_green(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        vwap: float,
        ema: float,
        atr: float,
        rsi: float,
    ) -> Optional[TradeSignal]:
        """
        Check for Red-to-Green move.

        The stock opens below the previous close (red) and then reclaims
        above it (green). This shows buyers stepping in and is a strong
        reversal signal for gappers.
        """
        if len(df) < 20:
            return None

        prev_close = candidate.prev_close
        if prev_close <= 0:
            return None

        # Current price must be above prev close (now green)
        if price <= prev_close:
            return None

        # Check that first bar opened below prev close (was red)
        first_bar_open = df.iloc[0]["open"]
        if first_bar_open >= prev_close:
            return None

        # Current bar must be bullish
        latest = df.iloc[-1]
        if latest["close"] <= latest["open"]:
            return None

        # Volume confirmation
        if not self._volume_confirms(df):
            return None

        # Stop below prev close or VWAP
        stop_price = prev_close - (atr * 0.5)
        if not pd.isna(vwap) and vwap < prev_close:
            stop_price = min(stop_price, vwap - (atr * 0.25))

        min_distance = atr * self._entry_cfg.min_atr_distance
        if price - stop_price < min_distance:
            stop_price = price - min_distance

        stop_price = round(stop_price, 4)

        ema20 = df.iloc[-1].get("ema_20", price)
        signal = self._build_signal(
            candidate, SignalType.RED_TO_GREEN,
            price, stop_price, atr, vwap, ema
        )
        if signal:
            signal.confidence = self._compute_confidence(
                signal, candidate, df, rsi, ema, ema20, vwap
            )
        return signal

    def _check_breakout(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        vwap: float,
        ema: float,
        atr: float,
        rsi: float,
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
        ema20 = df.iloc[-1].get("ema_20", price)
        signal = self._build_signal(
            candidate,
            SignalType.BREAKOUT_CONTINUATION,
            price,
            stop_price,
            atr,
            vwap if not pd.isna(vwap) else price,
            ema if not pd.isna(ema) else price,
        )
        if signal:
            signal.confidence = self._compute_confidence(
                signal, candidate, df, rsi, ema, ema20, vwap
            )
        return signal

    def _volume_confirms(self, df: pd.DataFrame) -> bool:
        """
        Check if any of the last 3 bars had confirming volume.

        Elite traders don't need the exact current candle to spike —
        they look for a recent volume surge in the area. Checking
        the last 3 bars catches volume spikes between scan ticks.
        """
        if len(df) < 10:
            return False

        avg_vol = df.iloc[-10:-3]["volume"].mean()

        if avg_vol <= 0:
            return False

        threshold = avg_vol * self._entry_cfg.volume_confirmation_multiplier

        # Check if ANY of the last 3 bars had confirming volume
        for i in range(-3, 0):
            if df.iloc[i]["volume"] >= threshold:
                return True

        return False

    def _compute_stop(
        self, df: pd.DataFrame, entry_price: float, atr: float,
        vwap: float | None = None,
    ) -> float:
        """
        Compute stop-loss price.

        Uses the most conservative (tightest) of:
        - Entry bar low
        - Swing low of last 10 bars
        - ATR-based: entry - (ATR * multiplier)
        - Below VWAP (if provided and applicable)

        But ensures minimum distance of min_atr_distance * ATR.
        """
        entry_bar_low = df.iloc[-1]["low"]
        swing_low = df.iloc[-10:]["low"].min()
        atr_stop = entry_price - (atr * self._risk_cfg.stop_loss_atr_multiplier)

        # Use highest (tightest) stop
        stop = max(entry_bar_low, swing_low, atr_stop)

        # For VWAP setups, also consider just below VWAP as stop
        if vwap is not None and not pd.isna(vwap):
            vwap_stop = vwap - (atr * 0.25)
            stop = max(stop, vwap_stop)

        # Ensure minimum distance
        min_distance = atr * self._entry_cfg.min_atr_distance
        if entry_price - stop < min_distance:
            stop = entry_price - min_distance

        # Stop must be below entry
        if stop >= entry_price:
            stop = entry_price - min_distance

        return round(stop, 4)

    def _compute_confidence(
        self,
        signal: TradeSignal,
        candidate: ScanResult,
        df: pd.DataFrame,
        rsi: float,
        ema: float,
        ema20: float,
        vwap: float,
    ) -> float:
        """
        Multi-factor confidence scoring for elite-level trade quality.

        Factors evaluated:
        1. Base signal quality from scanner score
        2. Time of day (power zone boost, dead zone penalty)
        3. Gap fill risk (>50% gap filled = penalty)
        4. Multi-candle momentum (consecutive green bars)
        5. Volume trend (increasing vs decreasing)
        6. RSI level (overbought penalty on entry)
        7. EMA alignment (EMA9 > EMA20 > VWAP = perfect alignment)
        8. Candle body quality (strong bodies vs dojis)
        9. Relative volume strength

        Returns confidence score in [0.0, 1.0].
        """
        score = min(candidate.score, 1.0)

        # --- 1. Time of day adjustment ---
        try:
            current = now_et()
            hour, minute = current.hour, current.minute

            # Power zone: 9:30 - 10:30 AM = +10% boost
            if (hour == 9 and minute >= 30) or hour == 10:
                score *= 1.10
            # Dead zone: 11:30 AM - 1:00 PM = -20% penalty
            elif (hour == 11 and minute >= 30) or hour == 12:
                score *= 0.80
            # Late day: after 3:00 PM = -15% penalty
            elif hour >= 15:
                score *= 0.85
        except Exception:
            pass

        # --- 2. Gap fill risk ---
        if candidate.prev_close > 0 and candidate.gap_pct > 0:
            gap_size = candidate.price - candidate.prev_close
            if gap_size > 0:
                current_gap = signal.entry_price - candidate.prev_close
                gap_filled_pct = (1.0 - current_gap / gap_size) * 100
                if gap_filled_pct > 50:
                    score *= max(0.6, 1.0 - (gap_filled_pct - 50) / 100)

        # --- 3. Multi-candle momentum ---
        if len(df) >= 5:
            consecutive_green = 0
            for i in range(-1, max(-6, -len(df)), -1):
                bar = df.iloc[i]
                if bar["close"] > bar["open"]:
                    consecutive_green += 1
                else:
                    break
            if consecutive_green >= 3:
                score *= 1.08
            elif consecutive_green >= 2:
                score *= 1.04
            elif consecutive_green == 0:
                score *= 0.90

        # --- 4. Volume trend ---
        if len(df) >= 10:
            recent_vol = df.iloc[-3:]["volume"].mean()
            earlier_vol = df.iloc[-10:-3]["volume"].mean()
            if earlier_vol > 0:
                vol_ratio = recent_vol / earlier_vol
                if vol_ratio >= 1.5:
                    score *= 1.08
                elif vol_ratio < 0.5:
                    score *= 0.85

        # --- 5. RSI level ---
        if not pd.isna(rsi):
            if rsi > self.RSI_OVERBOUGHT:
                score *= 0.85
            elif 40 <= rsi <= 60:
                score *= 1.05

        # --- 6. EMA alignment ---
        if not pd.isna(ema) and not pd.isna(ema20) and not pd.isna(vwap):
            price = signal.entry_price
            if price > ema > ema20 > vwap:
                score *= 1.10
            elif price > ema > vwap:
                score *= 1.05
            elif price < vwap:
                score *= 0.80

        # --- 7. Candle body quality ---
        latest = df.iloc[-1]
        body = abs(latest["close"] - latest["open"])
        total_range = latest["high"] - latest["low"]
        if total_range > 0:
            body_ratio = body / total_range
            if body_ratio > 0.7:
                score *= 1.05
            elif body_ratio < 0.3:
                score *= 0.90

        # --- 8. Relative volume strength ---
        if candidate.relative_volume >= 15.0:
            score *= 1.08
        elif candidate.relative_volume >= 10.0:
            score *= 1.04
        elif candidate.relative_volume < 3.0:
            score *= 0.85

        # --- 9. Microstructure quality ---
        score *= self._microstructure.score(df, signal.entry_price)

        # Clamp to [0, 1]
        return round(max(0.0, min(1.0, score)), 4)

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

        # Base confidence from candidate score (refined by _compute_confidence)
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
            confidence=signal.confidence,
        )

        return signal
