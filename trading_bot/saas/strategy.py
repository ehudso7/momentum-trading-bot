"""
momentum_breakout_v1 — transparent, testable signal rules.

Pure functions only. No I/O, no time, no global state. Every input is
explicit; every output is bounded. The strategy operates on daily OHLCV
bars (a Sequence of dicts or a pandas DataFrame with lowercase columns).

Direction rules:
    bullish  when close > sma_20 > sma_50 AND volume_ratio > 1.2
    bearish  when close < sma_20 < sma_50 AND volume_ratio > 1.2
    neutral  otherwise

Confidence (always in [0.0, 1.0]) blends three terms:
    momentum_pct    : abs(close / sma_50 - 1) capped at 10%
    sma_spread_pct  : abs(sma_20 / sma_50 - 1) capped at 5%
    volume_factor   : min(volume_ratio, 3.0) - 1.0, scaled
For neutral, confidence is always 0.0.

Stop-loss / take-profit suggestions are derived from the configurable
``RiskParams`` (see ``DEFAULT_RISK_PARAMS``). They are NOT financial
advice — they are mechanical levels driven by the most recent close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

DIRECTION_BULLISH = "bullish"
DIRECTION_BEARISH = "bearish"
DIRECTION_NEUTRAL = "neutral"
ALL_DIRECTIONS = (DIRECTION_BULLISH, DIRECTION_BEARISH, DIRECTION_NEUTRAL)

STRATEGY_ID = "momentum_breakout_v1"
DEFAULT_TIMEFRAME = "1d"
MIN_BARS_REQUIRED = 50
VOLUME_RATIO_THRESHOLD = 1.2

DISCLAIMER = "Not financial advice. For research and education only."


@dataclass(frozen=True)
class RiskParams:
    """Mechanical levels for stop-loss and take-profit suggestions."""

    stop_loss_pct: float = 0.04          # 4%
    take_profit_pct: float = 0.08        # 8% (R:R = 2:1)
    max_position_size_pct: float = 5.0   # of portfolio per signal
    max_daily_loss_pct: float = 2.0      # circuit-breaker hint


DEFAULT_RISK_PARAMS = RiskParams()


@dataclass(frozen=True)
class Signal:
    """One per-symbol signal record."""

    symbol: str
    direction: str
    strategy: str
    confidence: float
    timeframe: str
    indicators: dict
    rationale: list = field(default_factory=list)
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "strategy": self.strategy,
            "confidence": round(float(self.confidence), 4),
            "timeframe": self.timeframe,
            "rationale": list(self.rationale),
            "indicators": dict(self.indicators),
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Bar normalisation
# ---------------------------------------------------------------------------


def _bars_as_records(bars: Any) -> list[Mapping[str, Any]]:
    """
    Accept a pandas DataFrame or a sequence of mappings; return a list
    of plain dicts with lowercase keys ``open``/``high``/``low``/
    ``close``/``volume``. Order is preserved (oldest → newest).
    """
    if bars is None:
        return []
    # Try pandas first without importing it at module-load time.
    if hasattr(bars, "to_dict") and hasattr(bars, "columns"):
        try:
            renamed = bars.rename(columns={c: str(c).lower() for c in bars.columns})
            records = renamed.to_dict(orient="records")
            return list(records)
        except Exception:
            return []
    out: list[Mapping[str, Any]] = []
    for row in bars:
        if isinstance(row, Mapping):
            out.append({str(k).lower(): v for k, v in row.items()})
    return out


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


# ---------------------------------------------------------------------------
# Indicators (no numpy dependency, no pandas requirement)
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], window: int) -> Optional[float]:
    """Simple moving average over the trailing ``window`` values."""
    if window <= 0:
        return None
    if len(values) < window:
        return None
    tail = values[-window:]
    return sum(tail) / float(window)


def volume_ratio(volumes: Sequence[float], window: int = 20) -> Optional[float]:
    """
    Today's volume divided by the trailing ``window`` average.

    Returns None when there are insufficient bars or when the window
    average is zero (avoid division-by-zero).
    """
    if window <= 0 or len(volumes) < window + 1:
        return None
    today = float(volumes[-1])
    prior = volumes[-(window + 1):-1]
    avg = sum(prior) / float(window)
    if avg <= 0:
        return None
    return today / avg


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _scale_clamped(value: float, cap: float) -> float:
    """Clamp ``value / cap`` into [0, 1]."""
    if cap <= 0:
        return 0.0
    return max(0.0, min(1.0, value / cap))


def _compute_confidence(
    *,
    direction: str,
    momentum_pct: float,
    sma_spread_pct: float,
    vol_ratio: float,
) -> float:
    """
    Blend three normalised terms into a [0, 1] confidence score.

    Neutral signals are always 0.0. A bullish or bearish signal that
    just barely clears the rules (e.g. close = sma_20 + epsilon, vol =
    1.21x) lands near 0.0; one with strong momentum, wide SMA spread,
    and 3x volume lands near 1.0.
    """
    if direction == DIRECTION_NEUTRAL:
        return 0.0
    momentum_term = _scale_clamped(abs(momentum_pct), 0.10)
    spread_term = _scale_clamped(abs(sma_spread_pct), 0.05)
    volume_term = _scale_clamped(max(0.0, vol_ratio - 1.0), 2.0)
    blended = (momentum_term * 0.4) + (spread_term * 0.3) + (volume_term * 0.3)
    # Hard clamp — floating-point error must never produce 1.000001.
    return max(0.0, min(1.0, blended))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(
    symbol: str,
    bars: Any,
    *,
    risk: RiskParams = DEFAULT_RISK_PARAMS,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> Signal:
    """
    Run momentum_breakout_v1 on a single symbol.

    Returns a Signal record. If there are insufficient bars, returns a
    neutral signal with an ``error`` field — the caller can surface it
    as a market-data warning rather than crashing.
    """
    sym = (symbol or "").strip().upper() or "?"
    records = _bars_as_records(bars)
    if len(records) < MIN_BARS_REQUIRED:
        return Signal(
            symbol=sym,
            direction=DIRECTION_NEUTRAL,
            strategy=STRATEGY_ID,
            confidence=0.0,
            timeframe=timeframe,
            indicators={
                "close": None,
                "sma_20": None,
                "sma_50": None,
                "volume_ratio": None,
                "momentum_pct": None,
            },
            rationale=["insufficient market data"],
            error=f"insufficient_bars: have {len(records)}, need {MIN_BARS_REQUIRED}",
        )

    closes: list[float] = []
    volumes: list[float] = []
    for r in records:
        c = _safe_float(r.get("close"))
        v = _safe_float(r.get("volume"))
        if c is None or v is None:
            continue
        closes.append(c)
        volumes.append(v)
    if len(closes) < MIN_BARS_REQUIRED:
        return Signal(
            symbol=sym,
            direction=DIRECTION_NEUTRAL,
            strategy=STRATEGY_ID,
            confidence=0.0,
            timeframe=timeframe,
            indicators={
                "close": None,
                "sma_20": None,
                "sma_50": None,
                "volume_ratio": None,
                "momentum_pct": None,
            },
            rationale=["insufficient market data after cleaning"],
            error=f"insufficient_clean_bars: have {len(closes)}, need {MIN_BARS_REQUIRED}",
        )

    close = closes[-1]
    sma_20 = sma(closes, 20)
    sma_50 = sma(closes, 50)
    vol_ratio = volume_ratio(volumes, 20)
    if sma_20 is None or sma_50 is None or vol_ratio is None or sma_50 == 0:
        return Signal(
            symbol=sym,
            direction=DIRECTION_NEUTRAL,
            strategy=STRATEGY_ID,
            confidence=0.0,
            timeframe=timeframe,
            indicators={
                "close": round(close, 4),
                "sma_20": sma_20,
                "sma_50": sma_50,
                "volume_ratio": vol_ratio,
                "momentum_pct": None,
            },
            rationale=["indicator calculation produced None"],
            error="indicator_unavailable",
        )

    momentum_pct = (close / sma_50) - 1.0
    sma_spread_pct = (sma_20 / sma_50) - 1.0

    direction = DIRECTION_NEUTRAL
    rationale: list[str] = []
    if (
        close > sma_20 > sma_50
        and vol_ratio > VOLUME_RATIO_THRESHOLD
    ):
        direction = DIRECTION_BULLISH
        rationale = [
            f"close ${close:.2f} above SMA20 ${sma_20:.2f} above SMA50 ${sma_50:.2f}",
            f"volume {vol_ratio:.2f}x the 20-day average",
            f"momentum {momentum_pct * 100:.1f}% above 50-day SMA",
        ]
    elif (
        close < sma_20 < sma_50
        and vol_ratio > VOLUME_RATIO_THRESHOLD
    ):
        direction = DIRECTION_BEARISH
        rationale = [
            f"close ${close:.2f} below SMA20 ${sma_20:.2f} below SMA50 ${sma_50:.2f}",
            f"volume {vol_ratio:.2f}x the 20-day average",
            f"momentum {momentum_pct * 100:.1f}% below 50-day SMA",
        ]
    else:
        if vol_ratio <= VOLUME_RATIO_THRESHOLD:
            rationale.append(
                f"volume {vol_ratio:.2f}x below {VOLUME_RATIO_THRESHOLD:.2f}x threshold"
            )
        if not (close > sma_20 > sma_50) and not (close < sma_20 < sma_50):
            rationale.append("trend alignment not met")

    confidence = _compute_confidence(
        direction=direction,
        momentum_pct=momentum_pct,
        sma_spread_pct=sma_spread_pct,
        vol_ratio=vol_ratio,
    )

    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    if direction == DIRECTION_BULLISH:
        entry = round(close, 4)
        stop_loss = round(close * (1.0 - risk.stop_loss_pct), 4)
        take_profit = round(close * (1.0 + risk.take_profit_pct), 4)
    elif direction == DIRECTION_BEARISH:
        entry = round(close, 4)
        stop_loss = round(close * (1.0 + risk.stop_loss_pct), 4)
        take_profit = round(close * (1.0 - risk.take_profit_pct), 4)

    indicators = {
        "close": round(close, 4),
        "sma_20": round(sma_20, 4),
        "sma_50": round(sma_50, 4),
        "volume_ratio": round(vol_ratio, 4),
        "momentum_pct": round(momentum_pct, 4),
    }

    return Signal(
        symbol=sym,
        direction=direction,
        strategy=STRATEGY_ID,
        confidence=round(confidence, 4),
        timeframe=timeframe,
        indicators=indicators,
        rationale=rationale,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
