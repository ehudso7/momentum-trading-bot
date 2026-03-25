"""
Technical indicator calculations using pure numpy/pandas.

Provides VWAP, EMA, ATR, Parabolic SAR, and a convenience
function to enrich a DataFrame with all indicators at once.

No external TA library dependencies — all math is implemented directly.

All functions expect a DataFrame with columns: open, high, low, close, volume.
Column names are case-insensitive (will be lowercased internally).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure OHLCV columns are lowercase."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    return df


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Compute Volume Weighted Average Price.

    VWAP = cumulative(typical_price * volume) / cumulative(volume)
    where typical_price = (high + low + close) / 3

    Returns:
        pd.Series with VWAP values.
    """
    df = _normalize_columns(df)
    try:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        cumulative_tp_vol = (typical_price * df["volume"]).cumsum()
        cumulative_vol = df["volume"].cumsum()
        vwap = cumulative_tp_vol / cumulative_vol.replace(0, np.nan)
        return vwap
    except Exception:
        return pd.Series(dtype=float, index=df.index)


def compute_ema(df: pd.DataFrame, length: int = 9) -> pd.Series:
    """
    Compute Exponential Moving Average.

    Uses pandas ewm with span=length.

    Args:
        df: OHLCV DataFrame.
        length: EMA period (default 9).

    Returns:
        pd.Series with EMA values.
    """
    df = _normalize_columns(df)
    try:
        return df["close"].ewm(span=length, adjust=False).mean()
    except Exception:
        return pd.Series(dtype=float, index=df.index)


def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Compute Average True Range.

    TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
    ATR = EMA(TR, length)

    Args:
        df: OHLCV DataFrame.
        length: ATR period (default 14).

    Returns:
        pd.Series with ATR values.
    """
    df = _normalize_columns(df)
    try:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.ewm(span=length, adjust=False).mean()
        return atr
    except Exception:
        return pd.Series(dtype=float, index=df.index)


def compute_parabolic_sar(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute Parabolic SAR.

    Simplified implementation:
    - Uses acceleration factor starting at 0.02, max 0.20, step 0.02.

    Returns:
        DataFrame with columns: psar_long, psar_short.
        psar_long has values when trend is up (SAR below price).
        psar_short has values when trend is down (SAR above price).
    """
    df = _normalize_columns(df)
    try:
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        n = len(high)

        psar = np.full(n, np.nan)
        psar_long = np.full(n, np.nan)
        psar_short = np.full(n, np.nan)

        af_start = 0.02
        af_step = 0.02
        af_max = 0.20

        # Initialize
        is_long = True
        af = af_start
        ep = high[0]
        psar[0] = low[0]

        for i in range(1, n):
            prev_psar = psar[i - 1]

            if is_long:
                psar[i] = prev_psar + af * (ep - prev_psar)
                # Don't let SAR go above prior two lows
                if i >= 2:
                    psar[i] = min(psar[i], low[i - 1], low[i - 2])
                else:
                    psar[i] = min(psar[i], low[i - 1])

                if low[i] < psar[i]:
                    # Reverse to short
                    is_long = False
                    psar[i] = ep
                    ep = low[i]
                    af = af_start
                else:
                    if high[i] > ep:
                        ep = high[i]
                        af = min(af + af_step, af_max)
                    psar_long[i] = psar[i]
            else:
                psar[i] = prev_psar + af * (ep - prev_psar)
                # Don't let SAR go below prior two highs
                if i >= 2:
                    psar[i] = max(psar[i], high[i - 1], high[i - 2])
                else:
                    psar[i] = max(psar[i], high[i - 1])

                if high[i] > psar[i]:
                    # Reverse to long
                    is_long = True
                    psar[i] = ep
                    ep = high[i]
                    af = af_start
                else:
                    if low[i] < ep:
                        ep = low[i]
                        af = min(af + af_step, af_max)
                    psar_short[i] = psar[i]

        result = pd.DataFrame(index=df.index)
        result["psar_long"] = psar_long
        result["psar_short"] = psar_short
        return result

    except Exception:
        return pd.DataFrame(index=df.index)


def compute_rsi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Compute Relative Strength Index.

    Uses Wilder's smoothing method (EMA with alpha=1/length).

    Args:
        df: OHLCV DataFrame.
        length: RSI period (default 14).

    Returns:
        pd.Series with RSI values (0-100).
    """
    df = _normalize_columns(df)
    try:
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.ewm(alpha=1.0 / length, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / length, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi.fillna(50.0)
    except Exception:
        return pd.Series(50.0, index=df.index)


def compute_volume_ma(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """
    Compute Volume Moving Average.

    Args:
        df: OHLCV DataFrame.
        length: MA period (default 20).

    Returns:
        pd.Series with volume MA values.
    """
    df = _normalize_columns(df)
    try:
        return df["volume"].rolling(window=length, min_periods=1).mean()
    except Exception:
        return pd.Series(dtype=float, index=df.index)


def compute_relative_volume(current_volume: float, avg_volume: float) -> float:
    """
    Compute relative volume ratio.

    Args:
        current_volume: Current session volume.
        avg_volume: Average volume (e.g., 20-day avg).

    Returns:
        Ratio of current to average volume. Returns 0.0 if avg is zero.
    """
    if avg_volume <= 0:
        return 0.0
    return current_volume / avg_volume


def compute_adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Compute Average Directional Index for trend strength filtering.

    ADX > 25 = trending, ADX < 20 = choppy. Used to filter out
    momentum entries in non-trending conditions.

    Args:
        df: OHLCV DataFrame.
        length: ADX period (default 14).

    Returns:
        pd.Series with ADX values (0-100).
    """
    df = _normalize_columns(df)
    try:
        high, low, close = df["high"], df["low"], df["close"]
        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)

        alpha = 1.0 / length
        smoothed_plus = plus_dm.ewm(alpha=alpha, adjust=False).mean()
        smoothed_minus = minus_dm.ewm(alpha=alpha, adjust=False).mean()
        smoothed_tr = tr.ewm(alpha=alpha, adjust=False).mean()

        plus_di = 100 * smoothed_plus / smoothed_tr.replace(0, np.nan)
        minus_di = 100 * smoothed_minus / smoothed_tr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=alpha, adjust=False).mean()
        return adx.fillna(0.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def compute_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """
    Compute MACD line, signal line, and histogram.

    MACD detects momentum shifts 2-3 bars before RSI — critical for
    early exits on fading runners.

    Args:
        df: OHLCV DataFrame.
        fast: Fast EMA period (default 12).
        slow: Slow EMA period (default 26).
        signal: Signal line EMA period (default 9).

    Returns:
        DataFrame with columns: macd, macd_signal, macd_histogram.
    """
    df = _normalize_columns(df)
    try:
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        result = pd.DataFrame(index=df.index)
        result["macd"] = macd_line
        result["macd_signal"] = signal_line
        result["macd_histogram"] = histogram
        return result
    except Exception:
        return pd.DataFrame(index=df.index)


def enrich_dataframe(
    df: pd.DataFrame,
    ema_length: int = 9,
    atr_length: int = 14,
    include_vwap: bool = True,
    include_psar: bool = True,
    include_rsi: bool = True,
    include_volume_ma: bool = True,
    include_macd: bool = True,
    include_adx: bool = True,
) -> pd.DataFrame:
    """
    Add all technical indicators to a DataFrame.

    Adds columns: ema_{length}, ema_20, atr_{atr_length}, rsi_14,
    volume_ma_20, and optionally vwap, psar_long, psar_short.

    Args:
        df: OHLCV DataFrame.
        ema_length: EMA period.
        atr_length: ATR period.
        include_vwap: Whether to compute VWAP (requires intraday data).
        include_psar: Whether to compute Parabolic SAR.
        include_rsi: Whether to compute RSI.
        include_volume_ma: Whether to compute volume moving average.

    Returns:
        New DataFrame with indicator columns added.
    """
    result = _normalize_columns(df)

    # EMA (fast)
    ema = compute_ema(result, length=ema_length)
    result[f"ema_{ema_length}"] = ema

    # EMA 20 (trend)
    if ema_length != 20:
        ema20 = compute_ema(result, length=20)
        result["ema_20"] = ema20

    # ATR
    atr = compute_atr(result, length=atr_length)
    result[f"atr_{atr_length}"] = atr

    # VWAP
    if include_vwap:
        vwap = compute_vwap(result)
        result["vwap"] = vwap

    # RSI
    if include_rsi:
        rsi = compute_rsi(result, length=14)
        result["rsi_14"] = rsi

    # Volume MA
    if include_volume_ma:
        vol_ma = compute_volume_ma(result, length=20)
        result["volume_ma_20"] = vol_ma

    # Parabolic SAR
    if include_psar:
        psar = compute_parabolic_sar(result)
        if not psar.empty:
            if "psar_long" in psar.columns:
                result["psar_long"] = psar["psar_long"]
            if "psar_short" in psar.columns:
                result["psar_short"] = psar["psar_short"]

    # ADX
    if include_adx:
        adx = compute_adx(result, length=14)
        result["adx_14"] = adx

    # MACD
    if include_macd:
        macd = compute_macd(result)
        if not macd.empty:
            for col in ("macd", "macd_signal", "macd_histogram"):
                if col in macd.columns:
                    result[col] = macd[col]

    return result
