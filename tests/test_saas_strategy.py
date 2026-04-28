"""
Tests for trading_bot.saas.strategy.momentum_breakout_v1.

Covers:
  * bullish/bearish/neutral classification
  * confidence is bounded 0..1 and 0 for neutral
  * insufficient bars produce a neutral signal with an error string
  * stop-loss / take-profit suggestions track the supplied RiskParams
  * volume-ratio threshold blocks low-conviction signals
"""

from __future__ import annotations

import math

import pytest

from trading_bot.saas.strategy import (
    DEFAULT_RISK_PARAMS,
    DIRECTION_BEARISH,
    DIRECTION_BULLISH,
    DIRECTION_NEUTRAL,
    MIN_BARS_REQUIRED,
    RiskParams,
    STRATEGY_ID,
    evaluate,
    sma,
    volume_ratio,
)


# ---------------------------------------------------------------------------
# Bar generators
# ---------------------------------------------------------------------------


def _bullish_bars(n: int = 80) -> list[dict]:
    """Strong uptrend with a volume spike on the last bar."""
    out: list[dict] = []
    for i in range(n):
        close = 100.0 + i * 1.0
        vol = 1_000_000.0
        if i == n - 1:
            vol = 5_000_000.0  # 5x trailing avg
        out.append({"close": close, "volume": vol, "open": close * 0.99,
                    "high": close * 1.01, "low": close * 0.98})
    return out


def _bearish_bars(n: int = 80) -> list[dict]:
    """Strong downtrend with a volume spike on the last bar."""
    out: list[dict] = []
    for i in range(n):
        close = 200.0 - i * 1.0
        vol = 1_000_000.0
        if i == n - 1:
            vol = 5_000_000.0
        out.append({"close": close, "volume": vol, "open": close * 1.01,
                    "high": close * 1.02, "low": close * 0.99})
    return out


def _flat_bars(n: int = 80) -> list[dict]:
    """Sideways, low volume — should classify neutral."""
    return [
        {"close": 100.0, "volume": 1_000_000.0, "open": 100.0,
         "high": 100.5, "low": 99.5}
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


class TestIndicators:
    def test_sma_basic(self):
        assert sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)

    def test_sma_insufficient_returns_none(self):
        assert sma([1.0, 2.0], 5) is None

    def test_sma_zero_window_returns_none(self):
        assert sma([1.0, 2.0, 3.0], 0) is None

    def test_volume_ratio_basic(self):
        # Trailing 5 average = 1, today = 3 → ratio 3.0
        ratio = volume_ratio([1.0, 1.0, 1.0, 1.0, 1.0, 3.0], window=5)
        assert ratio == pytest.approx(3.0)

    def test_volume_ratio_insufficient_returns_none(self):
        assert volume_ratio([1.0, 2.0], window=5) is None

    def test_volume_ratio_zero_avg_returns_none(self):
        assert volume_ratio([0.0] * 5 + [10.0], window=5) is None


# ---------------------------------------------------------------------------
# Direction classification
# ---------------------------------------------------------------------------


class TestDirection:
    def test_bullish_setup(self):
        signal = evaluate("AAPL", _bullish_bars())
        assert signal.direction == DIRECTION_BULLISH
        assert signal.strategy == STRATEGY_ID
        assert signal.confidence > 0.0
        assert signal.entry is not None
        assert signal.stop_loss is not None
        assert signal.take_profit is not None
        # bullish: stop below entry, target above
        assert signal.stop_loss < signal.entry < signal.take_profit

    def test_bearish_setup(self):
        signal = evaluate("XYZ", _bearish_bars())
        assert signal.direction == DIRECTION_BEARISH
        assert signal.confidence > 0.0
        # bearish: stop above entry, target below
        assert signal.stop_loss > signal.entry > signal.take_profit

    def test_neutral_when_volume_low(self):
        signal = evaluate("FLAT", _flat_bars())
        assert signal.direction == DIRECTION_NEUTRAL
        assert signal.confidence == 0.0
        assert signal.entry is None
        assert signal.stop_loss is None
        assert signal.take_profit is None

    def test_neutral_when_volume_just_below_threshold(self):
        bars = _bullish_bars()
        # Force last-bar volume ratio to ~1.19, just under 1.2 threshold.
        bars[-1] = {**bars[-1], "volume": 1_190_000.0}
        signal = evaluate("BORDER", bars)
        assert signal.direction == DIRECTION_NEUTRAL


# ---------------------------------------------------------------------------
# Confidence bounds
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_confidence_in_range_for_bullish(self):
        signal = evaluate("AAPL", _bullish_bars())
        assert 0.0 <= signal.confidence <= 1.0

    def test_confidence_in_range_for_bearish(self):
        signal = evaluate("XYZ", _bearish_bars())
        assert 0.0 <= signal.confidence <= 1.0

    def test_confidence_zero_for_neutral(self):
        signal = evaluate("FLAT", _flat_bars())
        assert signal.confidence == 0.0

    def test_confidence_higher_for_stronger_setup(self):
        weak = _bullish_bars()
        # Stronger trend: triple the slope.
        strong: list[dict] = []
        for i in range(80):
            close = 100.0 + i * 3.0
            vol = 1_000_000.0
            if i == 79:
                vol = 5_000_000.0
            strong.append({"close": close, "volume": vol})
        weak_sig = evaluate("WEAK", weak)
        strong_sig = evaluate("STRONG", strong)
        assert strong_sig.confidence >= weak_sig.confidence


# ---------------------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------------------


class TestInsufficientData:
    def test_too_few_bars(self):
        signal = evaluate("AAPL", _bullish_bars(n=10))
        assert signal.direction == DIRECTION_NEUTRAL
        assert signal.confidence == 0.0
        assert signal.error is not None
        assert "insufficient" in signal.error.lower()

    def test_empty_bars(self):
        signal = evaluate("AAPL", [])
        assert signal.direction == DIRECTION_NEUTRAL
        assert signal.error is not None

    def test_none_bars(self):
        signal = evaluate("AAPL", None)
        assert signal.direction == DIRECTION_NEUTRAL
        assert signal.error is not None

    def test_bars_with_nan_close_drop_below_minimum(self):
        bars = _bullish_bars(n=MIN_BARS_REQUIRED)
        for b in bars[:5]:
            b["close"] = float("nan")
        signal = evaluate("AAPL", bars)
        # Either neutral (insufficient_clean_bars) or a normal signal —
        # but never a crash.
        assert signal.direction in (DIRECTION_BULLISH, DIRECTION_NEUTRAL)


# ---------------------------------------------------------------------------
# RiskParams
# ---------------------------------------------------------------------------


class TestRiskParamsApplied:
    def test_default_stop_and_target_pcts(self):
        signal = evaluate("AAPL", _bullish_bars())
        # Default stop is 4% below entry; target is 8% above.
        ratio_stop = signal.stop_loss / signal.entry
        ratio_tp = signal.take_profit / signal.entry
        assert math.isclose(ratio_stop, 1.0 - DEFAULT_RISK_PARAMS.stop_loss_pct, rel_tol=1e-3)
        assert math.isclose(ratio_tp, 1.0 + DEFAULT_RISK_PARAMS.take_profit_pct, rel_tol=1e-3)

    def test_custom_risk_params(self):
        risk = RiskParams(stop_loss_pct=0.10, take_profit_pct=0.20)
        signal = evaluate("AAPL", _bullish_bars(), risk=risk)
        ratio_stop = signal.stop_loss / signal.entry
        ratio_tp = signal.take_profit / signal.entry
        assert math.isclose(ratio_stop, 0.90, rel_tol=1e-3)
        assert math.isclose(ratio_tp, 1.20, rel_tol=1e-3)


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


class TestSignalSerialisation:
    def test_to_dict_has_all_documented_keys(self):
        signal = evaluate("AAPL", _bullish_bars())
        d = signal.to_dict()
        for k in (
            "symbol", "direction", "strategy", "confidence", "timeframe",
            "rationale", "indicators", "entry", "stop_loss", "take_profit",
        ):
            assert k in d

    def test_indicators_keys(self):
        signal = evaluate("AAPL", _bullish_bars())
        for k in ("close", "sma_20", "sma_50", "volume_ratio", "momentum_pct"):
            assert k in signal.indicators

    def test_rationale_is_list_of_strings_or_empty(self):
        signal = evaluate("AAPL", _bullish_bars())
        assert isinstance(signal.rationale, list)
        for r in signal.rationale:
            assert isinstance(r, str)
