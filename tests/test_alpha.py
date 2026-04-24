"""
Tests for Phase 2 Core conversion: alpha scoring layer.

Covers:
- AlphaScore serialization and tier mapping.
- RuleBasedAlphaScorer on high-quality and low-quality setups.
- AlphaLogger CSV append semantics.
- Shadow-mode integration: alpha scoring does not change decision behavior.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from trading_bot.core.alpha import (
    CSV_HEADERS,
    AlphaLogger,
    AlphaScore,
    RuleBasedAlphaScorer,
    score_to_tier,
)
from trading_bot.models.domain import FeatureSnapshot, SignalDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TS = datetime(2026, 4, 24, 10, 30, 0)


def _snap(
    *,
    symbol: str = "TEST",
    gap_pct: float = 10.0,
    rvol: float = 8.0,
    volatility: float = 3.0,
    regime: str = "trending_bullish",
    price: float = 10.0,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        timestamp=TS,
        price=price,
        gap_pct=gap_pct,
        relative_volume=rvol,
        volatility=volatility,
        regime=regime,
    )


def _dec(
    *,
    symbol: str = "TEST",
    action: str = "buy",
    confidence: float = 0.85,
    reason: str = "executed",
) -> SignalDecision:
    return SignalDecision(
        timestamp=TS,
        symbol=symbol,
        action=action,
        confidence=confidence,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# AlphaScore serialization + tier helper
# ---------------------------------------------------------------------------


def test_alpha_score_serialization():
    score = AlphaScore(
        symbol="AAA",
        timestamp=TS,
        score=0.8765,
        tier="A",
        reasons=["gap_sweet_spot(10.0%)", "rvol_strong(8.0x)"],
    )
    d = score.to_dict()
    assert d == {
        "timestamp": "2026-04-24 10:30:00",
        "symbol": "AAA",
        "score": 0.8765,
        "tier": "A",
        "reasons": "gap_sweet_spot(10.0%)|rvol_strong(8.0x)",
    }


def test_alpha_score_default_reasons_is_empty_list():
    score = AlphaScore(symbol="X", timestamp=TS, score=0.5, tier="C")
    assert score.reasons == []
    assert score.to_dict()["reasons"] == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1.00, "A"),
        (0.90, "A"),
        (0.80, "A"),
        (0.79, "B"),
        (0.65, "B"),
        (0.64, "C"),
        (0.50, "C"),
        (0.49, "D"),
        (0.35, "D"),
        (0.34, "F"),
        (0.00, "F"),
    ],
)
def test_score_to_tier_boundaries(raw, expected):
    assert score_to_tier(raw) == expected


# ---------------------------------------------------------------------------
# RuleBasedAlphaScorer — quality cases
# ---------------------------------------------------------------------------


def test_rule_based_scorer_high_quality_setup_is_tier_a():
    """A textbook low-float gapper in a trending bull market with a
    high-confidence executed signal must score as tier A (>= 0.80)."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap(
        gap_pct=12.0,
        rvol=15.0,
        volatility=3.5,
        regime="trending_bullish",
    )
    dec = _dec(action="buy", confidence=0.85, reason="executed")

    result = scorer.score(snap, dec)

    assert result.symbol == "TEST"
    assert 0.0 <= result.score <= 1.0
    assert result.score >= 0.80
    assert result.tier == "A"
    assert any("gap_sweet_spot" in r for r in result.reasons)
    assert any("rvol_exceptional" in r for r in result.reasons)
    assert any("vol_healthy" in r for r in result.reasons)
    assert any("regime_trending_bullish" in r for r in result.reasons)
    assert "action_buy" in result.reasons


def test_rule_based_scorer_low_quality_setup_is_tier_f():
    """A weak gap + weak rvol + bearish regime + low-confidence skip
    must score in the F tier."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap(
        gap_pct=2.0,
        rvol=1.2,
        volatility=0.5,
        regime="trending_bearish",
    )
    dec = _dec(
        action="skip",
        confidence=0.30,
        reason="strategy:no_valid_setup",
    )

    result = scorer.score(snap, dec)

    assert 0.0 <= result.score <= 1.0
    assert result.score < 0.35
    assert result.tier == "F"
    assert any("gap_weak" in r for r in result.reasons)
    assert any("rvol_weak" in r for r in result.reasons)
    assert any("regime_trending_bearish" in r for r in result.reasons)
    assert any("action_skip:strategy" in r for r in result.reasons)


def test_rule_based_scorer_midrange_setup_is_b_or_c():
    """A reasonable but not elite setup should land in B or C, not A or F."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap(
        gap_pct=6.0,
        rvol=3.0,
        volatility=1.5,
        regime="range_bound",
    )
    dec = _dec(action="buy", confidence=0.70, reason="executed")

    result = scorer.score(snap, dec)
    assert 0.0 <= result.score <= 1.0
    assert result.tier in {"B", "C"}


def test_rule_based_scorer_clamps_to_unit_interval():
    """Even with pathological inputs the score stays in [0, 1]."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap(gap_pct=200.0, rvol=-5.0, volatility=999.0, regime="unknown")
    dec = _dec(action="skip", confidence=1.5, reason="")
    result = scorer.score(snap, dec)
    assert 0.0 <= result.score <= 1.0
    assert result.tier in {"A", "B", "C", "D", "F"}


def test_rule_based_scorer_unknown_regime_uses_neutral():
    scorer = RuleBasedAlphaScorer()
    snap = _snap(regime="mystery_regime")
    dec = _dec()
    result = scorer.score(snap, dec)
    assert any("regime_mystery_regime" in r for r in result.reasons)


def test_rule_based_scorer_volatility_zero_treated_as_unavailable():
    """volatility=0.0 from Phase 1.5 means "bars too thin" — score as
    insufficient but don't blow up."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap(volatility=0.0)
    result = scorer.score(snap, _dec())
    assert any("vol_unavailable" in r for r in result.reasons)


def test_rule_based_scorer_negative_confidence_clamped():
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    result = scorer.score(snap, _dec(confidence=-0.5))
    assert 0.0 <= result.score <= 1.0
    assert any("confidence(0.00)" in r for r in result.reasons)


def test_rule_based_scorer_skip_reasons_distinguished():
    """Different rejection stages should produce distinct reason tags."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    for stage in [
        "strategy:no_valid_setup",
        "risk:exceeds_daily_limit",
        "correlation:correlated_with_['AAA']",
        "advisor:low_confidence",
        "already_held",
        "empty_bars",
        "broker_rejected",
    ]:
        result = scorer.score(snap, _dec(action="skip", reason=stage))
        assert any(stage.split(":")[0] in r for r in result.reasons) or any(
            stage in r for r in result.reasons
        )


# ---------------------------------------------------------------------------
# AlphaLogger — CSV write path
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_csv(tmp_path: Path) -> Path:
    return tmp_path / "alpha_scores.csv"


def test_alpha_logger_creates_header(tmp_csv: Path):
    logger = AlphaLogger(tmp_csv)
    assert tmp_csv.exists()
    with open(tmp_csv) as f:
        header_line = f.readline().strip().split(",")
    assert header_line == CSV_HEADERS
    assert logger.path == tmp_csv


def test_csv_alpha_score_logging(tmp_csv: Path):
    """End-to-end: AlphaLogger appends one flat row per log() call."""
    logger = AlphaLogger(tmp_csv)
    scorer = RuleBasedAlphaScorer()

    snap_a = _snap(symbol="AAA", gap_pct=12.0, rvol=15.0, volatility=3.5,
                   regime="trending_bullish")
    dec_a = _dec(symbol="AAA", action="buy", confidence=0.85, reason="executed")
    logger.log(scorer.score(snap_a, dec_a), snap_a, dec_a)

    snap_b = _snap(symbol="BBB", gap_pct=2.0, rvol=1.2, volatility=0.5,
                   regime="trending_bearish")
    dec_b = _dec(
        symbol="BBB", action="skip", confidence=0.30,
        reason="strategy:no_valid_setup",
    )
    logger.log(scorer.score(snap_b, dec_b), snap_b, dec_b)

    with open(tmp_csv) as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2

    r0 = rows[0]
    assert r0["symbol"] == "AAA"
    assert r0["tier"] == "A"
    assert 0.80 <= float(r0["score"]) <= 1.0
    assert r0["action"] == "buy"
    assert r0["regime"] == "trending_bullish"
    assert float(r0["gap_pct"]) == 12.0
    assert float(r0["relative_volume"]) == 15.0
    assert r0["reasons"]  # non-empty

    r1 = rows[1]
    assert r1["symbol"] == "BBB"
    assert r1["tier"] == "F"
    assert float(r1["score"]) < 0.35


def test_alpha_logger_appends_across_instances(tmp_csv: Path):
    """A second logger pointed at the same file appends, not truncates."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    dec = _dec()
    AlphaLogger(tmp_csv).log(scorer.score(snap, dec), snap, dec)
    AlphaLogger(tmp_csv).log(scorer.score(snap, dec), snap, dec)

    with open(tmp_csv) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2


def test_alpha_logger_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "deep" / "alpha_scores.csv"
    AlphaLogger(target)
    assert target.exists()
    assert target.parent.is_dir()


def test_alpha_logger_swallows_write_errors(tmp_csv: Path, monkeypatch):
    logger = AlphaLogger(tmp_csv)
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    dec = _dec()
    alpha = scorer.score(snap, dec)

    import builtins

    real_open = builtins.open

    def fail_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "")
        if str(path) == str(tmp_csv) and "a" in mode:
            raise OSError("simulated disk error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_open)
    # Must not raise
    logger.log(alpha, snap, dec)


def test_alpha_logger_row_has_all_header_columns(tmp_csv: Path):
    logger = AlphaLogger(tmp_csv)
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    dec = _dec()
    logger.log(scorer.score(snap, dec), snap, dec)

    with open(tmp_csv) as f:
        lines = f.read().strip().splitlines()
    assert lines[0].split(",") == CSV_HEADERS
    # Row field count matches header exactly (csv handles quoting).
    reader = csv.reader([lines[1]])
    assert len(next(reader)) == len(CSV_HEADERS)


# ---------------------------------------------------------------------------
# Shadow-mode integration — scoring does not change decision behavior
# ---------------------------------------------------------------------------


def test_shadow_mode_integration_does_not_change_decision_behavior(
    tmp_path: Path,
):
    """
    Mimic the main-loop integration: for each candidate, the
    DecisionLogger row MUST be identical whether or not alpha scoring
    runs alongside it. Alpha scoring is additive — a separate file.
    """
    from trading_bot.persistence.decision_log import DecisionLogger

    # --- Scenario A: decision log only ---
    dl_only = tmp_path / "scenario_a_decision.csv"
    logger_a = DecisionLogger(dl_only)

    # --- Scenario B: decision log + alpha shadow ---
    dl_with_alpha = tmp_path / "scenario_b_decision.csv"
    alpha_csv = tmp_path / "scenario_b_alpha.csv"
    logger_b_decision = DecisionLogger(dl_with_alpha)
    logger_b_alpha = AlphaLogger(alpha_csv)
    scorer = RuleBasedAlphaScorer()

    fixtures = [
        (
            _snap(symbol="AAA", gap_pct=12.0, rvol=15.0, volatility=3.5,
                  regime="trending_bullish"),
            _dec(symbol="AAA", action="buy", confidence=0.85, reason="executed"),
        ),
        (
            _snap(symbol="BBB", gap_pct=2.0, rvol=1.2, volatility=0.5,
                  regime="trending_bearish"),
            _dec(symbol="BBB", action="skip", confidence=0.30,
                 reason="strategy:no_valid_setup"),
        ),
        (
            _snap(symbol="CCC"),
            _dec(symbol="CCC", action="skip", confidence=0.5,
                 reason="already_held"),
        ),
    ]

    for snap, dec in fixtures:
        # Scenario A — decision only
        logger_a.log(snap, dec)
        # Scenario B — decision + alpha shadow
        logger_b_decision.log(snap, dec)
        alpha = scorer.score(snap, dec)
        logger_b_alpha.log(alpha, snap, dec)

    # The decision log contents must be identical byte-for-byte
    assert dl_only.read_text() == dl_with_alpha.read_text(), (
        "Alpha shadow scoring must not alter the decision log output"
    )

    # Alpha log is a separate file — created only when alpha runs
    assert alpha_csv.exists()
    with open(alpha_csv) as f:
        alpha_rows = list(csv.DictReader(f))
    assert len(alpha_rows) == len(fixtures)


def test_shadow_mode_scoring_is_read_only_on_inputs():
    """RuleBasedAlphaScorer must never mutate its inputs."""
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    dec = _dec()
    snap_copy = FeatureSnapshot(
        symbol=snap.symbol,
        timestamp=snap.timestamp,
        price=snap.price,
        gap_pct=snap.gap_pct,
        relative_volume=snap.relative_volume,
        volatility=snap.volatility,
        regime=snap.regime,
    )
    dec_copy = SignalDecision(
        timestamp=dec.timestamp,
        symbol=dec.symbol,
        action=dec.action,
        confidence=dec.confidence,
        reason=dec.reason,
    )
    scorer.score(snap, dec)
    assert snap == snap_copy
    assert dec == dec_copy
