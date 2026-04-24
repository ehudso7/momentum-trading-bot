"""
Tests for Phase 1.5 Core conversion instrumentation.

Covers:
- FeatureSnapshot and SignalDecision dataclass construction + serialization.
- DecisionLogger CSV append semantics (header creation, row shape, append).
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from trading_bot.models.domain import FeatureSnapshot, SignalDecision
from trading_bot.persistence.decision_log import CSV_HEADERS, DecisionLogger


# ---------------------------------------------------------------------------
# FeatureSnapshot
# ---------------------------------------------------------------------------


def test_feature_snapshot_creation():
    """FeatureSnapshot stores the required fields and round-trips via to_dict."""
    ts = datetime(2026, 4, 24, 10, 30, 15)
    snap = FeatureSnapshot(
        symbol="TEST",
        timestamp=ts,
        price=8.50,
        gap_pct=35.0,
        relative_volume=12.5,
        volatility=3.2,
        regime="trending_bullish",
    )

    assert snap.symbol == "TEST"
    assert snap.timestamp == ts
    assert snap.price == 8.50
    assert snap.gap_pct == 35.0
    assert snap.relative_volume == 12.5
    assert snap.volatility == 3.2
    assert snap.regime == "trending_bullish"

    d = snap.to_dict()
    assert d["symbol"] == "TEST"
    assert d["timestamp"] == "2026-04-24 10:30:15"
    assert d["price"] == 8.5
    assert d["gap_pct"] == 35.0
    assert d["relative_volume"] == 12.5
    assert d["volatility"] == 3.2
    assert d["regime"] == "trending_bullish"


def test_feature_snapshot_rounding():
    """Serialized fields are rounded for stable CSV output."""
    snap = FeatureSnapshot(
        symbol="ABC",
        timestamp=datetime(2026, 4, 24, 9, 45),
        price=8.123456789,
        gap_pct=4.56789,
        relative_volume=1.23456,
        volatility=0.987654,
        regime="range_bound",
    )
    d = snap.to_dict()
    assert d["price"] == round(8.123456789, 4)
    assert d["gap_pct"] == round(4.56789, 3)
    assert d["relative_volume"] == round(1.23456, 3)
    assert d["volatility"] == round(0.987654, 4)


# ---------------------------------------------------------------------------
# SignalDecision
# ---------------------------------------------------------------------------


def test_signal_decision_creation_buy():
    ts = datetime(2026, 4, 24, 10, 31, 0)
    dec = SignalDecision(
        timestamp=ts,
        symbol="TEST",
        action="buy",
        confidence=0.78,
        reason="executed",
    )
    assert dec.action == "buy"
    assert dec.confidence == 0.78
    assert dec.reason == "executed"
    assert dec.to_dict() == {
        "timestamp": "2026-04-24 10:31:00",
        "symbol": "TEST",
        "action": "buy",
        "confidence": 0.78,
        "reason": "executed",
    }


def test_signal_decision_creation_skip_with_default_confidence():
    """Skip decisions typically use default 0.5 when no strategy score exists."""
    dec = SignalDecision(
        timestamp=datetime(2026, 4, 24, 10, 31, 0),
        symbol="FAIL",
        action="skip",
        confidence=0.5,
        reason="strategy:no_valid_setup",
    )
    assert dec.action == "skip"
    assert dec.confidence == 0.5
    assert "no_valid_setup" in dec.reason


# ---------------------------------------------------------------------------
# DecisionLogger — CSV write path
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_csv(tmp_path: Path) -> Path:
    return tmp_path / "decision_log.csv"


def _make_pair(symbol: str, action: str, confidence: float, reason: str):
    ts = datetime(2026, 4, 24, 10, 30, 0)
    snap = FeatureSnapshot(
        symbol=symbol,
        timestamp=ts,
        price=10.0,
        gap_pct=12.0,
        relative_volume=5.0,
        volatility=2.5,
        regime="range_bound",
    )
    dec = SignalDecision(
        timestamp=ts,
        symbol=symbol,
        action=action,
        confidence=confidence,
        reason=reason,
    )
    return snap, dec


def test_decision_logger_creates_header(tmp_csv: Path):
    logger = DecisionLogger(tmp_csv)
    assert tmp_csv.exists()
    with open(tmp_csv) as f:
        first_line = f.readline().strip().split(",")
    assert first_line == CSV_HEADERS
    # Path accessor works
    assert logger.path == tmp_csv


def test_signal_decision_logging(tmp_csv: Path):
    """Full end-to-end: DecisionLogger appends one CSV row per log() call."""
    logger = DecisionLogger(tmp_csv)

    snap_a, dec_a = _make_pair("AAA", "buy", 0.82, "executed")
    snap_b, dec_b = _make_pair("BBB", "skip", 0.5, "strategy:no_valid_setup")

    logger.log(snap_a, dec_a)
    logger.log(snap_b, dec_b)

    with open(tmp_csv) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2

    # Row 1: buy
    r0 = rows[0]
    assert r0["symbol"] == "AAA"
    assert r0["action"] == "buy"
    assert float(r0["confidence"]) == pytest.approx(0.82)
    assert r0["reason"] == "executed"
    assert r0["regime"] == "range_bound"
    assert float(r0["price"]) == 10.0
    assert float(r0["gap_pct"]) == 12.0
    assert float(r0["relative_volume"]) == 5.0
    assert float(r0["volatility"]) == 2.5
    assert r0["timestamp"] == "2026-04-24 10:30:00"

    # Row 2: skip
    r1 = rows[1]
    assert r1["symbol"] == "BBB"
    assert r1["action"] == "skip"
    assert float(r1["confidence"]) == 0.5
    assert r1["reason"] == "strategy:no_valid_setup"


def test_decision_logger_appends_not_overwrites(tmp_csv: Path):
    """A new logger pointed at an existing file must append, not truncate."""
    logger1 = DecisionLogger(tmp_csv)
    snap, dec = _make_pair("AAA", "buy", 0.9, "executed")
    logger1.log(snap, dec)

    # Simulate process restart
    logger2 = DecisionLogger(tmp_csv)
    snap2, dec2 = _make_pair("BBB", "skip", 0.5, "risk:daily_limit")
    logger2.log(snap2, dec2)

    with open(tmp_csv) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAA"
    assert rows[1]["symbol"] == "BBB"


def test_decision_logger_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "deep" / "decision_log.csv"
    DecisionLogger(target)
    assert target.exists()
    assert target.parent.is_dir()


def test_decision_logger_write_errors_are_swallowed(tmp_csv: Path, monkeypatch):
    """A disk error during log() must not raise out of the logger."""
    logger = DecisionLogger(tmp_csv)
    snap, dec = _make_pair("AAA", "buy", 0.7, "executed")

    import builtins

    real_open = builtins.open

    def fail_open(path, *args, **kwargs):
        if str(path) == str(tmp_csv) and "a" in (args[0] if args else kwargs.get("mode", "")):
            raise OSError("simulated disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_open)
    # Must not raise
    logger.log(snap, dec)


def test_decision_logger_does_not_touch_existing_journal(tmp_path: Path):
    """Decision log is a NEW dataset; it must not overwrite journal.csv or similar."""
    journal = tmp_path / "journal.csv"
    journal.write_text("date,symbol,pnl\n2026-04-24,XYZ,100.00\n")

    decision_log = tmp_path / "decision_log.csv"
    DecisionLogger(decision_log)

    # Decision log was created as a separate file
    assert decision_log.exists()
    # Journal untouched
    assert journal.read_text() == "date,symbol,pnl\n2026-04-24,XYZ,100.00\n"


def test_logger_row_column_order_matches_header(tmp_csv: Path):
    """Each row must include exactly the header columns in the same order."""
    logger = DecisionLogger(tmp_csv)
    snap, dec = _make_pair("ZZZ", "skip", 0.5, "advisor:low_confidence")
    logger.log(snap, dec)

    with open(tmp_csv) as f:
        lines = f.read().strip().splitlines()
    assert lines[0].split(",") == CSV_HEADERS
    # Row has same number of columns as header
    assert len(lines[1].split(",")) == len(CSV_HEADERS)
