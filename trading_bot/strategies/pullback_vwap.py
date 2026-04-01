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
        self._current_regime: str = "range_bound"
        self._proximity_multiplier: float = 1.0

        # Last rejection diagnostics (populated when evaluate() returns None)
        self.last_rejection_details: dict[str, str] = {}

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
            "high_volatility": 1.5,    # High vol = wider, gappers pull back further
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

        # Dead zone hard filter: 11:30 AM - 1:00 PM ET
        # Research shows this window has lowest institutional participation,
        # wider spreads, and highest false signal rate. Block all but
        # exceptional setups (high RVOL) to avoid midday chop losses.
        try:
            current = now_et()
            is_dead_zone = (
                (current.hour == 11 and current.minute >= 30) or current.hour == 12
            )
            if is_dead_zone and candidate.relative_volume < 5.0:
                log.info(
                    "strategy.dead_zone_filtered",
                    symbol=candidate.symbol,
                    rvol=candidate.relative_volume,
                )
                return None
        except Exception:
            pass

        # Volume exhaustion filter
        if self._volume_exhaust.is_exhausted(bars):
            log.info("strategy.volume_exhausted", symbol=candidate.symbol)
            return None

        # Check each setup in priority order, with multi-bar lookback
        # Try current bar first, then check recent bars for missed signals
        #
        # Regime-restricted setups: in bearish or high-vol, only allow
        # VWAP pullback (highest conviction). Secondary setups (EMA, ORB,
        # red-to-green, breakout) have lower win rates in hostile regimes.
        all_setups = [
            ("vwap_pullback", self._check_vwap_pullback),
            ("ema_pullback", self._check_ema_pullback),
            ("orb", self._check_opening_range_breakout),
            ("red_to_green", self._check_red_to_green),
            ("breakout", self._check_breakout),
        ]
        if self._current_regime == "trending_bearish":
            # Bearish: VWAP pullback only — safest setup against trend
            setups = [s for s in all_setups if s[0] == "vwap_pullback"]
        elif self._current_regime == "high_volatility":
            # High-vol: allow VWAP + EMA pullbacks (both are pullback setups
            # with defined risk via stop placement, unlike breakouts/ORB)
            setups = [s for s in all_setups if s[0] in ("vwap_pullback", "ema_pullback")]
        else:
            setups = all_setups

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
                # Minimum R:R gate: reject signals where first target
                # doesn't offer at least 1.5:1 reward-to-risk
                if signal.reward_risk_ratio < 1.5:
                    log.info(
                        "strategy.rr_too_low",
                        symbol=candidate.symbol,
                        rr=round(signal.reward_risk_ratio, 2),
                        setup=setup_name,
                    )
                    continue
                return signal

        # If no signal on current bar, check last 2 bars for missed entries
        for lookback_offset in [2, 3]:
            if len(df) < lookback_offset + 20:
                continue
            lb_df = df.iloc[:-lookback_offset]
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
                    price_move = price - lb_price
                    signal.entry_price = round(price, 4)
                    # Scale stop proportionally so R:R stays valid
                    if price_move > 0:
                        signal.stop_price = round(signal.stop_price + price_move * 0.8, 4)
                    # Revalidate stop distance with current price
                    if signal.stop_price >= price:
                        signal.stop_price = round(price - (atr * self._entry_cfg.min_atr_distance), 4)
                    # R:R gate for lookback signals too
                    if signal.reward_risk_ratio < 1.5:
                        continue
                    log.info(
                        "strategy.lookback_signal",
                        symbol=candidate.symbol,
                        type=signal.signal_type.value,
                        lookback_bars=lookback_offset - 1,
                    )
                    return signal

        # --- Detailed diagnostics: why did every setup reject? ---
        rejection_details = self._diagnose_rejections(
            candidate, df, price, vwap, ema, ema20, atr, rsi,
        )
        self.last_rejection_details = rejection_details

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
            regime=self._current_regime,
            proximity_multiplier=self._proximity_multiplier,
            rejection_details=rejection_details,
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

            # 4b. MACD histogram flipping negative on runners (earlier exit than RSI)
            macd_hist = latest.get("macd_histogram")
            if macd_hist is not None and not pd.isna(macd_hist) and len(df) >= 2:
                prev_hist = df.iloc[-2].get("macd_histogram")
                if (
                    prev_hist is not None
                    and not pd.isna(prev_hist)
                    and prev_hist > 0
                    and macd_hist < 0
                    and position.r_multiple >= 1.5
                ):
                    return True, "macd_momentum_shift"

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

        Activated once position reaches 0.3R profit (breakeven protection)
        or after first scale-out. Uses ATR-based trailing.
        Only ratchets UP, never down.
        """
        # Activate breakeven protection early: once position is at +0.3R,
        # start trailing to lock in gains and prevent winners turning to losers.
        # 0.3R is aggressive but critical for momentum trades — protects capital
        # on moves that stall after initial push.
        r_multiple = position.r_multiple
        if position.scale_outs_completed < 1 and r_multiple < 0.3:
            return None

        if bars.empty or len(bars) < 20:
            return None

        df = enrich_dataframe(bars, ema_length=self._entry_cfg.ema_period)
        latest = df.iloc[-1]
        atr = latest.get("atr_14", 0.0)

        if pd.isna(atr) or atr <= 0:
            return None

        # Chandelier Exit: trail from high-water mark (peak price),
        # not current price, to protect gains from pullbacks.
        # Adaptive multiplier: tighten as profit grows to lock in more.
        reference_price = position.high_water_mark or position.current_price
        base_mult = self._exit_cfg.trailing_stop_atr_multiplier
        if r_multiple >= 3.0:
            atr_mult = base_mult * 0.7   # Tight at +3R
        elif r_multiple >= 2.0:
            atr_mult = base_mult * 0.85  # Moderately tight at +2R
        else:
            atr_mult = base_mult
        new_stop = reference_price - (atr * atr_mult)

        # VWAP-aware trailing: if VWAP is above the ATR-based stop and
        # price is above VWAP, use VWAP minus a small buffer as the stop.
        # VWAP acts as dynamic support — stocks bouncing off VWAP are strong,
        # and breaking below VWAP signals weakness.
        vwap = latest.get("vwap")
        if (
            vwap is not None
            and not pd.isna(vwap)
            and vwap > 0
            and position.current_price > vwap
        ):
            vwap_stop = vwap - (atr * 0.25)  # Small buffer below VWAP
            if vwap_stop > new_stop:
                new_stop = vwap_stop

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

    def _diagnose_rejections(
        self,
        candidate: ScanResult,
        df: pd.DataFrame,
        price: float,
        vwap: float,
        ema: float,
        ema20: float,
        atr: float,
        rsi: float,
    ) -> dict:
        """
        Diagnose why each setup rejected the candidate.

        Returns a dict of {setup_name: rejection_reason} for logging.
        This is called only when no signal was generated, so the slight
        overhead of re-checking conditions is acceptable for diagnostics.
        """
        reasons: dict[str, str] = {}
        latest = df.iloc[-1]
        is_bullish = latest["close"] > latest["open"]
        vol_ok = self._volume_confirms(df)

        # --- VWAP Pullback ---
        if pd.isna(vwap) or vwap <= 0:
            reasons["vwap_pullback"] = "no_vwap_data"
        else:
            prox = abs(price - vwap) / vwap * 100
            adaptive = self._entry_cfg.vwap_proximity_pct * self._proximity_multiplier
            if prox > adaptive:
                reasons["vwap_pullback"] = f"too_far_from_vwap({prox:.1f}%>{adaptive:.1f}%)"
            elif not is_bullish:
                reasons["vwap_pullback"] = "bearish_bar"
            elif price < vwap:
                reasons["vwap_pullback"] = "price_below_vwap"
            elif not vol_ok:
                reasons["vwap_pullback"] = "no_volume_confirmation"
            else:
                reasons["vwap_pullback"] = "build_signal_failed"

        # --- EMA Pullback ---
        ema_col = f"ema_{self._entry_cfg.ema_period}"
        if ema_col not in df.columns or pd.isna(ema) or ema <= 0:
            reasons["ema_pullback"] = "no_ema_data"
        else:
            prox = abs(price - ema) / ema * 100
            adaptive = self._entry_cfg.vwap_proximity_pct * 1.5 * self._proximity_multiplier
            if prox > adaptive:
                reasons["ema_pullback"] = f"too_far_from_ema({prox:.1f}%>{adaptive:.1f}%)"
            elif not is_bullish:
                reasons["ema_pullback"] = "bearish_bar"
            elif price < ema:
                reasons["ema_pullback"] = "price_below_ema"
            elif not vol_ok:
                reasons["ema_pullback"] = "no_volume_confirmation"
            else:
                reasons["ema_pullback"] = "build_signal_failed"

        # --- ORB ---
        reasons["orb"] = "orb_conditions_not_met"
        if not is_bullish:
            reasons["orb"] = "bearish_bar"
        elif not vol_ok:
            reasons["orb"] = "no_volume_confirmation"

        # --- Red to Green ---
        if candidate.prev_close <= 0:
            reasons["red_to_green"] = "no_prev_close"
        elif price <= candidate.prev_close:
            reasons["red_to_green"] = "price_below_prev_close"
        elif not is_bullish:
            reasons["red_to_green"] = "bearish_bar"
        elif not vol_ok:
            reasons["red_to_green"] = "no_volume_confirmation"
        else:
            reasons["red_to_green"] = "first_bar_not_red"

        # --- Breakout ---
        lookback = self._entry_cfg.breakout_lookback_bars
        if len(df) < lookback + 1:
            reasons["breakout"] = "insufficient_bars"
        elif not vol_ok:
            reasons["breakout"] = "no_volume_confirmation"
        else:
            recent = df.iloc[-(lookback + 1):-1]
            recent_high = recent["high"].max()
            if price <= recent_high:
                reasons["breakout"] = f"no_breakout(price={price:.2f}<=high={recent_high:.2f})"
            else:
                high_range = recent["high"].max() - recent["low"].min()
                latest_atr = df.iloc[-1].get("atr_14", atr)
                if not pd.isna(latest_atr) and latest_atr > 0 and high_range > 2.0 * latest_atr:
                    reasons["breakout"] = "not_consolidated"
                else:
                    reasons["breakout"] = "build_signal_failed"

        return reasons

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

        # ORB range must be reasonable (not already extended)
        orb_range = orb_high - orb_low
        if orb_range > 2.5 * atr:
            return None

        # Price must break above ORB high with confirmation margin
        latest = df.iloc[-1]
        if price <= orb_high + (atr * 0.25):
            return None

        # Must be a bullish bar
        if latest["close"] <= latest["open"]:
            return None

        # Volume confirmation
        if not self._volume_confirms(df):
            return None

        # ORB requires stricter RVOL (2.0x minimum)
        if candidate.relative_volume < 2.0:
            return None

        # Price above VWAP
        if not pd.isna(vwap) and price < vwap:
            return None

        # EMA alignment filter: EMA9 > EMA20 confirms uptrend
        ema20_val = df.iloc[-1].get("ema_20", price)
        if not pd.isna(ema) and not pd.isna(ema20_val) and ema < ema20_val:
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

        Uses time-of-day normalization: midday requires higher volume
        threshold since baseline volume is naturally lower, making
        average-looking volume potentially just noise.
        """
        if len(df) < 10:
            return False

        avg_vol = df.iloc[-10:-3]["volume"].mean()

        if avg_vol <= 0:
            return False

        # Time-of-day volume normalization
        base_mult = self._entry_cfg.volume_confirmation_multiplier
        try:
            current = now_et()
            if (current.hour == 11 and current.minute >= 30) or current.hour == 12:
                # Dead zone: require 1.3x normal threshold (~2.0x)
                threshold_mult = base_mult * 1.3
            elif 13 <= current.hour < 15:
                # Afternoon: slightly higher (~1.8x)
                threshold_mult = base_mult * 1.2
            else:
                # Morning power zone & power hour: normal
                threshold_mult = base_mult
        except Exception:
            threshold_mult = base_mult

        threshold = avg_vol * threshold_mult

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
        # Use 5-bar swing low (tighter than 10-bar = less risk per trade)
        swing_low_5 = df.iloc[-5:]["low"].min()
        # Place stop slightly below swing low (buffer prevents exact-level whipsaws)
        structure_stop = swing_low_5 - (atr * 0.15)
        atr_stop = entry_price - (atr * self._risk_cfg.stop_loss_atr_multiplier)

        # Use highest (tightest) valid stop
        stop = max(entry_bar_low, structure_stop, atr_stop)

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
                if gap_filled_pct > 60:
                    score *= max(0.75, 1.0 - (gap_filled_pct - 60) / 150)

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

        # --- 9b. ADX trend strength ---
        adx = df.iloc[-1].get("adx_14", 0.0) if "adx_14" in df.columns else 0.0
        if not pd.isna(adx):
            if adx < 20:
                score *= 0.90  # Choppy/no trend — mild penalty
            elif adx > 30:
                score *= 1.08  # Strong trend — boost

        # --- 10. Multi-timeframe confirmation (5-min resampled from 1-min bars) ---
        try:
            if len(df) >= 30:
                bars_5min = df.resample("5min").agg(
                    {"open": "first", "high": "max", "low": "min",
                     "close": "last", "volume": "sum"}
                ).dropna()
                if len(bars_5min) >= 25:
                    mtf_mult = self._mtf.confirm(bars_5min, None)
                    score *= mtf_mult
        except Exception:
            pass

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
