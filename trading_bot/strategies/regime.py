"""
Market Regime Detection Module.

Analyzes broad market data (SPY) to classify the current market regime
and provide parameter adjustments for the trading strategy.

Detection uses:
- 20/50-day EMA crossover for trend direction
- ATR percentile for volatility classification
- ADX-like directional movement metric for trending vs ranging

Regimes influence position sizing, stop placement, and volume thresholds.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd
import structlog

from trading_bot.utils.indicators import compute_atr, compute_ema

log = structlog.get_logger(__name__)


class MarketRegime(Enum):
    """Broad market regime classifications."""

    TRENDING_BULLISH = "trending_bullish"
    TRENDING_BEARISH = "trending_bearish"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"


class RegimeDetector:
    """
    Detects the current market regime from SPY/market bar data.

    Uses a combination of EMA crossover, ATR percentile, and a simplified
    ADX-like metric derived from directional movement to classify the
    market into one of five regimes.

    Each regime maps to a set of parameter adjustments that the strategy
    and risk modules can apply to adapt behavior.
    """

    # Minimum bars required for reliable regime detection.
    # 20-day EMA needs ~20 bars; 50-day EMA will be approximate with
    # fewer bars but still directionally useful.  The previous threshold
    # of 60 caused the detector to default on insufficient data too often
    # (e.g. when Polygon free-tier returns limited history and yfinance
    # is slow).  30 bars gives usable EMA-20/50 crossover signals.
    MIN_BARS = 30

    # ADX-like threshold: above this value the market is considered trending.
    ADX_TRENDING_THRESHOLD = 25.0

    # ATR percentile threshold: above this the market is high volatility.
    ATR_HIGH_VOL_PERCENTILE = 80.0

    # ATR percentile threshold: below this the market is low volatility.
    ATR_LOW_VOL_PERCENTILE = 20.0

    # Lookback window for ATR percentile calculation.
    ATR_PERCENTILE_WINDOW = 50

    # DI smoothing period (matches classic ADX calculation).
    DI_PERIOD = 14

    def detect(self, spy_bars: pd.DataFrame) -> MarketRegime:
        """
        Analyze SPY/market data to determine the current market regime.

        Args:
            spy_bars: DataFrame with OHLCV columns for SPY (or broad market
                      ETF). Must contain at least ``MIN_BARS`` rows.

        Returns:
            The detected ``MarketRegime``.
        """
        if spy_bars is None or spy_bars.empty or len(spy_bars) < self.MIN_BARS:
            log.warning(
                "regime.insufficient_data",
                bars=0 if spy_bars is None or spy_bars.empty else len(spy_bars),
                required=self.MIN_BARS,
                fallback="range_bound",
            )
            # RANGE_BOUND is the correct default when we can't determine the
            # regime — it applies moderate parameters without the stricter
            # volume confirmation multiplier that LOW_VOLATILITY imposes.
            return MarketRegime.RANGE_BOUND

        # --- Trend direction via EMA crossover ---
        ema_20 = compute_ema(spy_bars, length=20)
        ema_50 = compute_ema(spy_bars, length=50)

        latest_ema_20 = ema_20.iloc[-1]
        latest_ema_50 = ema_50.iloc[-1]

        bullish_cross = latest_ema_20 > latest_ema_50

        # --- Volatility classification via ATR percentile ---
        atr_series = compute_atr(spy_bars, length=14)
        is_high_vol = self._is_high_volatility(atr_series)
        is_low_vol = self._is_low_volatility(atr_series)

        # --- Trending vs ranging via ADX-like metric ---
        adx_value = self._compute_adx(spy_bars)
        is_trending = adx_value >= self.ADX_TRENDING_THRESHOLD

        # --- Classify regime ---
        regime = self._classify(bullish_cross, is_trending, is_high_vol, is_low_vol)

        log.info(
            "regime.detected",
            regime=regime.value,
            ema_20=round(float(latest_ema_20), 2),
            ema_50=round(float(latest_ema_50), 2),
            adx=round(float(adx_value), 2),
            is_trending=is_trending,
            is_high_vol=is_high_vol,
            is_low_vol=is_low_vol,
            bars_received=len(spy_bars),
        )

        return regime

    def get_regime_adjustments(self, regime: MarketRegime) -> dict:
        """
        Return parameter adjustments for the given market regime.

        The returned dictionary contains multipliers and overrides that
        strategy and risk modules should apply:

        - ``position_size_multiplier``: Scale factor for position size
          (1.0 = normal).
        - ``stop_multiplier``: Scale factor for stop distance (>1.0 =
          wider stops, <1.0 = tighter stops).
        - ``max_positions_override``: If not ``None``, overrides the
          configured maximum number of concurrent positions.
        - ``volume_confirmation_multiplier``: Scale factor for the
          volume confirmation threshold (>1.0 = require more volume).

        Args:
            regime: The current ``MarketRegime``.

        Returns:
            Dictionary of adjustment parameters.
        """
        adjustments = _REGIME_ADJUSTMENTS.get(regime)
        if adjustments is None:
            log.warning(
                "regime.unknown_regime",
                regime=regime,
            )
            return _REGIME_ADJUSTMENTS[MarketRegime.LOW_VOLATILITY].copy()

        log.debug(
            "regime.adjustments",
            regime=regime.value,
            adjustments=adjustments,
        )
        return adjustments.copy()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_high_volatility(self, atr_series: pd.Series) -> bool:
        """
        Determine if current ATR is above the 80th percentile of the
        recent ATR window.
        """
        if atr_series.empty or atr_series.isna().all():
            return False

        window = atr_series.iloc[-self.ATR_PERCENTILE_WINDOW:]
        window = window.dropna()

        if len(window) < 2:
            return False

        current_atr = window.iloc[-1]
        percentile_threshold = np.percentile(
            window.values, self.ATR_HIGH_VOL_PERCENTILE
        )

        return bool(current_atr > percentile_threshold)

    def _is_low_volatility(self, atr_series: pd.Series) -> bool:
        """
        Determine if current ATR is below the 20th percentile of the
        recent ATR window.
        """
        if atr_series.empty or atr_series.isna().all():
            return False

        window = atr_series.iloc[-self.ATR_PERCENTILE_WINDOW:]
        window = window.dropna()

        if len(window) < 2:
            return False

        current_atr = window.iloc[-1]
        percentile_threshold = np.percentile(
            window.values, self.ATR_LOW_VOL_PERCENTILE
        )

        return bool(current_atr < percentile_threshold)

    def _compute_adx(self, df: pd.DataFrame) -> float:
        """
        Compute a simplified ADX-like metric from directional movement.

        This follows the classic Wilder ADX calculation:
        1. Compute +DM and -DM from consecutive highs/lows.
        2. Smooth +DM, -DM, and TR using Wilder's smoothing (EMA).
        3. Compute +DI and -DI.
        4. Compute DX = |+DI - -DI| / (+DI + -DI) * 100.
        5. Smooth DX to get ADX.

        Returns the latest ADX value, or 0.0 on error.
        """
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]

        try:
            high = df["high"]
            low = df["low"]
            close = df["close"]

            # Directional movement
            up_move = high.diff()
            down_move = -low.diff()

            plus_dm = pd.Series(0.0, index=df.index)
            minus_dm = pd.Series(0.0, index=df.index)

            # +DM: up_move > down_move and up_move > 0
            plus_dm_mask = (up_move > down_move) & (up_move > 0)
            plus_dm = plus_dm.where(~plus_dm_mask, up_move)

            # -DM: down_move > up_move and down_move > 0
            minus_dm_mask = (down_move > up_move) & (down_move > 0)
            minus_dm = minus_dm.where(~minus_dm_mask, down_move)

            # True Range
            prev_close = close.shift(1)
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            # Wilder smoothing (equivalent to EMA with alpha = 1/period)
            period = self.DI_PERIOD
            smoothed_plus_dm = plus_dm.ewm(
                alpha=1.0 / period, adjust=False
            ).mean()
            smoothed_minus_dm = minus_dm.ewm(
                alpha=1.0 / period, adjust=False
            ).mean()
            smoothed_tr = true_range.ewm(
                alpha=1.0 / period, adjust=False
            ).mean()

            # Directional indicators
            plus_di = 100.0 * smoothed_plus_dm / smoothed_tr.replace(0, np.nan)
            minus_di = 100.0 * smoothed_minus_dm / smoothed_tr.replace(0, np.nan)

            # DX
            di_sum = plus_di + minus_di
            di_diff = (plus_di - minus_di).abs()
            dx = 100.0 * di_diff / di_sum.replace(0, np.nan)

            # ADX = smoothed DX
            adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()

            latest_adx = adx.iloc[-1]
            if pd.isna(latest_adx):
                return 0.0

            return float(latest_adx)

        except Exception:
            log.exception("regime.adx_computation_error")
            return 0.0

    @staticmethod
    def _classify(
        bullish_cross: bool, is_trending: bool, is_high_vol: bool,
        is_low_vol: bool = False,
    ) -> MarketRegime:
        """
        Map the signals to a single regime classification.

        Decision tree:
        1. If high volatility -> HIGH_VOLATILITY (takes priority)
        2. If trending and bullish cross -> TRENDING_BULLISH
        3. If trending and bearish cross -> TRENDING_BEARISH
        4. If not trending and low volatility -> LOW_VOLATILITY
        5. Otherwise -> RANGE_BOUND
        """
        if is_high_vol:
            return MarketRegime.HIGH_VOLATILITY

        if is_trending:
            if bullish_cross:
                return MarketRegime.TRENDING_BULLISH
            return MarketRegime.TRENDING_BEARISH

        # Not trending — distinguish low vol from range-bound
        if is_low_vol:
            return MarketRegime.LOW_VOLATILITY

        return MarketRegime.RANGE_BOUND


# ------------------------------------------------------------------
# Regime adjustment lookup table
# ------------------------------------------------------------------

_REGIME_ADJUSTMENTS: dict[MarketRegime, dict] = {
    MarketRegime.TRENDING_BULLISH: {
        "position_size_multiplier": 1.0,
        "stop_multiplier": 1.0,
        "max_positions_override": None,
        "volume_confirmation_multiplier": 1.0,
        "proximity_multiplier": 1.5,  # Wider for momentum — ride the trend
    },
    MarketRegime.TRENDING_BEARISH: {
        "position_size_multiplier": 0.5,
        "stop_multiplier": 0.8,
        "max_positions_override": 2,  # Limit exposure against trend
        "volume_confirmation_multiplier": 1.0,
        "proximity_multiplier": 0.8,  # Selective against trend
    },
    MarketRegime.RANGE_BOUND: {
        "position_size_multiplier": 0.75,
        "stop_multiplier": 1.0,
        "max_positions_override": 3,  # Slightly reduced in choppy markets
        "volume_confirmation_multiplier": 1.0,
        "proximity_multiplier": 2.0,  # Wide — catch range pullbacks
    },
    MarketRegime.HIGH_VOLATILITY: {
        "position_size_multiplier": 0.75,
        "stop_multiplier": 1.3,
        "max_positions_override": 3,  # Allow 3 positions (was 2 — too restrictive)
        "volume_confirmation_multiplier": 1.0,
        "proximity_multiplier": 2.0,  # Wider — gappers pull back further in high vol
    },
    MarketRegime.LOW_VOLATILITY: {
        "position_size_multiplier": 1.0,
        "stop_multiplier": 1.0,
        "max_positions_override": None,
        "volume_confirmation_multiplier": 1.5,
        "proximity_multiplier": 2.5,  # Widest — gappers sit far above VWAP
    },
}
