"""
Phase 2.7 tests for optional daily rotation on the two Core datasets.

Covers both `DecisionLogger` (decision_log.csv) and `AlphaLogger`
(alpha_scores.csv):

- default path is unchanged when rotation is unset
- explicit `rotation="daily"` writes to `<stem>_YYYY-MM-DD<suffix>`
- env var `TRADING_DATA_ROTATION=daily` has the same effect
- unknown env values fall back to "none" (never silently lose data)
- header creation and append semantics still work for dated files
- write failures are still swallowed
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from trading_bot.core import alpha as alpha_mod
from trading_bot.core.alpha import (
    ROTATION_DAILY,
    ROTATION_NONE,
    AlphaLogger,
    RuleBasedAlphaScorer,
)
from trading_bot.models.domain import FeatureSnapshot, SignalDecision
from trading_bot.persistence import decision_log as decision_log_mod
from trading_bot.persistence.decision_log import DecisionLogger


TS = datetime(2026, 4, 24, 10, 30, 0)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _snap(symbol: str = "TEST") -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        timestamp=TS,
        price=10.0,
        gap_pct=12.0,
        relative_volume=8.0,
        volatility=3.0,
        regime="trending_bullish",
    )


def _dec(symbol: str = "TEST", action: str = "buy") -> SignalDecision:
    return SignalDecision(
        timestamp=TS,
        symbol=symbol,
        action=action,
        confidence=0.8,
        reason="executed" if action == "buy" else "strategy:no_valid_setup",
    )


@pytest.fixture(autouse=True)
def clear_rotation_env(monkeypatch):
    """
    Every rotation test must start from a known env state. Anything
    that wants the daily env-var mode sets it explicitly.
    """
    monkeypatch.delenv("TRADING_DATA_ROTATION", raising=False)


@pytest.fixture
def fixed_date(monkeypatch):
    """Pin `_today_str` on both modules to a known date so filenames are deterministic."""
    monkeypatch.setattr(decision_log_mod, "_today_str", lambda: "2026-04-24")
    monkeypatch.setattr(alpha_mod, "_today_str", lambda: "2026-04-24")
    return "2026-04-24"


# ---------------------------------------------------------------------------
# DecisionLogger
# ---------------------------------------------------------------------------


def test_decision_logger_default_rotation_uses_base_path(tmp_path: Path):
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target)
    assert logger.rotation == ROTATION_NONE
    assert logger.path == target
    assert logger.base_path == target
    assert target.exists()


def test_decision_logger_daily_rotation_creates_dated_file(
    tmp_path: Path, fixed_date
):
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target, rotation=ROTATION_DAILY)
    expected = tmp_path / f"decision_log_{fixed_date}.csv"

    assert logger.rotation == ROTATION_DAILY
    assert logger.path == expected
    assert expected.exists(), "daily rotation must pre-create today's file"
    # Canonical (un-dated) path must NOT be touched.
    assert not target.exists()
    # Header present in the dated file
    with open(expected) as f:
        header = f.readline().strip().split(",")
    assert header[0] == "timestamp"
    assert "action" in header and "reason" in header


def test_decision_logger_daily_env_var_enables_rotation(
    tmp_path: Path, monkeypatch, fixed_date
):
    monkeypatch.setenv("TRADING_DATA_ROTATION", "daily")
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target)
    assert logger.rotation == ROTATION_DAILY
    assert logger.path == tmp_path / f"decision_log_{fixed_date}.csv"


def test_decision_logger_unknown_env_value_falls_back_to_none(
    tmp_path: Path, monkeypatch
):
    """A typo in the env var must never silently route writes to an unexpected place."""
    monkeypatch.setenv("TRADING_DATA_ROTATION", "hourly")  # not supported
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target)
    assert logger.rotation == ROTATION_NONE
    assert logger.path == target


def test_decision_logger_explicit_rotation_overrides_env_var(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("TRADING_DATA_ROTATION", "daily")
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target, rotation=ROTATION_NONE)
    assert logger.rotation == ROTATION_NONE
    assert logger.path == target


def test_decision_logger_daily_appends_not_overwrites(
    tmp_path: Path, fixed_date
):
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target, rotation=ROTATION_DAILY)
    snap = _snap("AAA")
    logger.log(snap, _dec("AAA", "buy"))
    logger.log(snap, _dec("AAA", "skip"))

    with open(tmp_path / f"decision_log_{fixed_date}.csv") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAA"
    assert rows[0]["action"] == "buy"
    assert rows[1]["action"] == "skip"


def test_decision_logger_day_rollover_creates_new_file(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "decision_log.csv"

    # Day 1
    monkeypatch.setattr(decision_log_mod, "_today_str", lambda: "2026-04-24")
    logger = DecisionLogger(target, rotation=ROTATION_DAILY)
    logger.log(_snap("AAA"), _dec("AAA", "buy"))

    # Day 2 — rollover
    monkeypatch.setattr(decision_log_mod, "_today_str", lambda: "2026-04-25")
    logger.log(_snap("BBB"), _dec("BBB", "skip"))

    day1 = tmp_path / "decision_log_2026-04-24.csv"
    day2 = tmp_path / "decision_log_2026-04-25.csv"
    assert day1.exists() and day2.exists()

    with open(day1) as f:
        rows1 = list(csv.DictReader(f))
    with open(day2) as f:
        rows2 = list(csv.DictReader(f))
    assert [r["symbol"] for r in rows1] == ["AAA"]
    assert [r["symbol"] for r in rows2] == ["BBB"]


def test_decision_logger_write_errors_still_swallowed(
    tmp_path: Path, monkeypatch, fixed_date
):
    target = tmp_path / "decision_log.csv"
    logger = DecisionLogger(target, rotation=ROTATION_DAILY)
    expected = tmp_path / f"decision_log_{fixed_date}.csv"

    import builtins

    real_open = builtins.open

    def fail_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "")
        if str(path) == str(expected) and "a" in mode:
            raise OSError("simulated disk error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_open)
    # Must not raise
    logger.log(_snap(), _dec())


# ---------------------------------------------------------------------------
# AlphaLogger
# ---------------------------------------------------------------------------


def test_alpha_logger_default_rotation_uses_base_path(tmp_path: Path):
    target = tmp_path / "alpha_scores.csv"
    logger = AlphaLogger(target)
    assert logger.rotation == ROTATION_NONE
    assert logger.path == target
    assert target.exists()


def test_alpha_logger_daily_rotation_creates_dated_file(
    tmp_path: Path, fixed_date
):
    target = tmp_path / "alpha_scores.csv"
    logger = AlphaLogger(target, rotation=ROTATION_DAILY)
    expected = tmp_path / f"alpha_scores_{fixed_date}.csv"
    assert logger.path == expected
    assert expected.exists()
    assert not target.exists()


def test_alpha_logger_daily_env_var_enables_rotation(
    tmp_path: Path, monkeypatch, fixed_date
):
    monkeypatch.setenv("TRADING_DATA_ROTATION", "daily")
    target = tmp_path / "alpha_scores.csv"
    logger = AlphaLogger(target)
    assert logger.rotation == ROTATION_DAILY
    assert logger.path == tmp_path / f"alpha_scores_{fixed_date}.csv"


def test_alpha_logger_unknown_env_value_falls_back_to_none(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("TRADING_DATA_ROTATION", "yearly")
    target = tmp_path / "alpha_scores.csv"
    logger = AlphaLogger(target)
    assert logger.rotation == ROTATION_NONE


def test_alpha_logger_daily_append_and_rollover(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "alpha_scores.csv"

    monkeypatch.setattr(alpha_mod, "_today_str", lambda: "2026-04-24")
    logger = AlphaLogger(target, rotation=ROTATION_DAILY)
    scorer = RuleBasedAlphaScorer()
    snap = _snap("AAA")
    dec = _dec("AAA", "buy")
    logger.log(scorer.score(snap, dec), snap, dec)

    monkeypatch.setattr(alpha_mod, "_today_str", lambda: "2026-04-25")
    snap2 = _snap("BBB")
    dec2 = _dec("BBB", "skip")
    logger.log(scorer.score(snap2, dec2), snap2, dec2)

    day1 = tmp_path / "alpha_scores_2026-04-24.csv"
    day2 = tmp_path / "alpha_scores_2026-04-25.csv"
    assert day1.exists() and day2.exists()

    with open(day1) as f:
        rows1 = list(csv.DictReader(f))
    with open(day2) as f:
        rows2 = list(csv.DictReader(f))
    assert [r["symbol"] for r in rows1] == ["AAA"]
    assert [r["symbol"] for r in rows2] == ["BBB"]


def test_alpha_logger_write_errors_still_swallowed(
    tmp_path: Path, monkeypatch, fixed_date
):
    target = tmp_path / "alpha_scores.csv"
    logger = AlphaLogger(target, rotation=ROTATION_DAILY)
    expected = tmp_path / f"alpha_scores_{fixed_date}.csv"
    scorer = RuleBasedAlphaScorer()
    snap = _snap()
    dec = _dec()
    alpha = scorer.score(snap, dec)

    import builtins

    real_open = builtins.open

    def fail_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "")
        if str(path) == str(expected) and "a" in mode:
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_open)
    logger.log(alpha, snap, dec)  # must not raise


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


def test_both_loggers_share_env_var(tmp_path: Path, monkeypatch, fixed_date):
    """A single env var must flip BOTH loggers into rotation mode at once."""
    monkeypatch.setenv("TRADING_DATA_ROTATION", "daily")
    dl = DecisionLogger(tmp_path / "decision_log.csv")
    al = AlphaLogger(tmp_path / "alpha_scores.csv")
    assert dl.rotation == ROTATION_DAILY
    assert al.rotation == ROTATION_DAILY
    assert dl.path.name == f"decision_log_{fixed_date}.csv"
    assert al.path.name == f"alpha_scores_{fixed_date}.csv"


def test_default_both_loggers_untouched_when_env_var_unset(tmp_path: Path):
    """Legacy deployments (no env var, no constructor arg) see identical behavior."""
    dl = DecisionLogger(tmp_path / "decision_log.csv")
    al = AlphaLogger(tmp_path / "alpha_scores.csv")
    assert dl.rotation == ROTATION_NONE
    assert al.rotation == ROTATION_NONE
    assert dl.path == tmp_path / "decision_log.csv"
    assert al.path == tmp_path / "alpha_scores.csv"
