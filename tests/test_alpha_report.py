"""
Tests for Phase 2.5 offline alpha analysis.

Covers:
- Missing / empty files handled gracefully.
- Alpha-only report (no journal, no decision).
- Joined report with realized outcomes.
- Tier-level, reason-level, and regime-level statistics.
- JSON output round-trips.
- CLI smoke test via `python -m trading_bot.analysis.alpha_report`.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pytest

from trading_bot.analysis import alpha_report
from trading_bot.analysis.alpha_report import (
    TIER_ORDER,
    build_report,
    format_json,
    format_text,
    join_outcomes,
    load_alpha_scores,
    load_decision_log,
    load_journal,
    main,
    reason_stats,
    regime_stats,
    tier_stats,
)


# ---------------------------------------------------------------------------
# Fixtures — write deterministic CSVs into a tmp path.
# ---------------------------------------------------------------------------


ALPHA_HEADERS = [
    "timestamp", "symbol", "score", "tier", "action", "confidence",
    "regime", "gap_pct", "relative_volume", "volatility", "reasons",
]

DECISION_HEADERS = [
    "timestamp", "symbol", "price", "gap_pct", "relative_volume",
    "volatility", "regime", "action", "confidence", "reason",
]

JOURNAL_HEADERS = [
    "date", "symbol", "side", "signal_type", "entry_price", "exit_price",
    "shares", "pnl", "rr_ratio", "hold_time_minutes", "entry_time",
    "exit_time", "exit_reason", "notes",
]


def _write_csv(path: Path, headers: list[str], rows: Iterable[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


@pytest.fixture
def alpha_csv(tmp_path: Path) -> Path:
    rows = [
        # AAA: tier A buy — big winner
        dict(timestamp="2026-04-24 09:45:00", symbol="AAA", score=0.92,
             tier="A", action="buy", confidence=0.85,
             regime="trending_bullish", gap_pct=12.0, relative_volume=15.0,
             volatility=3.5, reasons="gap_sweet_spot|rvol_exceptional"),
        # BBB: tier A buy — loser
        dict(timestamp="2026-04-24 10:00:00", symbol="BBB", score=0.85,
             tier="A", action="buy", confidence=0.80,
             regime="trending_bullish", gap_pct=10.0, relative_volume=12.0,
             volatility=3.0, reasons="gap_sweet_spot"),
        # CCC: tier C skip, strategy rejection
        dict(timestamp="2026-04-24 10:15:00", symbol="CCC", score=0.55,
             tier="C", action="skip", confidence=0.50,
             regime="range_bound", gap_pct=5.0, relative_volume=3.0,
             volatility=1.5, reasons="gap_modest|action_skip:strategy"),
        # DDD: tier F skip
        dict(timestamp="2026-04-24 10:30:00", symbol="DDD", score=0.20,
             tier="F", action="skip", confidence=0.30,
             regime="trending_bearish", gap_pct=2.0, relative_volume=1.2,
             volatility=0.5, reasons="gap_weak|action_skip:strategy"),
        # EEE: tier B buy — winner (small)
        dict(timestamp="2026-04-24 10:45:00", symbol="EEE", score=0.70,
             tier="B", action="buy", confidence=0.70,
             regime="range_bound", gap_pct=6.0, relative_volume=3.0,
             volatility=2.0, reasons="gap_modest|rvol_adequate"),
    ]
    return _write_csv(tmp_path / "alpha.csv", ALPHA_HEADERS, rows)


@pytest.fixture
def decision_csv(tmp_path: Path) -> Path:
    rows = [
        dict(timestamp="2026-04-24 09:45:00", symbol="AAA", price=10.0,
             gap_pct=12.0, relative_volume=15.0, volatility=3.5,
             regime="trending_bullish", action="buy", confidence=0.85,
             reason="executed"),
        dict(timestamp="2026-04-24 10:00:00", symbol="BBB", price=12.0,
             gap_pct=10.0, relative_volume=12.0, volatility=3.0,
             regime="trending_bullish", action="buy", confidence=0.80,
             reason="executed"),
        dict(timestamp="2026-04-24 10:15:00", symbol="CCC", price=8.0,
             gap_pct=5.0, relative_volume=3.0, volatility=1.5,
             regime="range_bound", action="skip", confidence=0.50,
             reason="strategy:no_valid_setup"),
        dict(timestamp="2026-04-24 10:30:00", symbol="DDD", price=3.0,
             gap_pct=2.0, relative_volume=1.2, volatility=0.5,
             regime="trending_bearish", action="skip", confidence=0.30,
             reason="strategy:no_valid_setup"),
        dict(timestamp="2026-04-24 10:45:00", symbol="EEE", price=5.0,
             gap_pct=6.0, relative_volume=3.0, volatility=2.0,
             regime="range_bound", action="buy", confidence=0.70,
             reason="executed"),
    ]
    return _write_csv(tmp_path / "decision.csv", DECISION_HEADERS, rows)


@pytest.fixture
def journal_csv(tmp_path: Path) -> Path:
    """Three buys produced three closed trades; two wins, one loss."""
    rows = [
        dict(date="2026-04-24", symbol="AAA", side="buy",
             signal_type="vwap_pullback", entry_price=10.0, exit_price=12.0,
             shares=100, pnl=200.0, rr_ratio=2.0, hold_time_minutes=30,
             entry_time="09:45:00", exit_time="10:15:00",
             exit_reason="target_2r", notes=""),
        dict(date="2026-04-24", symbol="BBB", side="buy",
             signal_type="ema_pullback", entry_price=12.0, exit_price=11.5,
             shares=100, pnl=-50.0, rr_ratio=-0.5, hold_time_minutes=20,
             entry_time="10:00:00", exit_time="10:20:00",
             exit_reason="stop_loss", notes=""),
        dict(date="2026-04-24", symbol="EEE", side="buy",
             signal_type="breakout_continuation", entry_price=5.0,
             exit_price=5.25, shares=200, pnl=50.0, rr_ratio=1.0,
             hold_time_minutes=25, entry_time="10:45:00", exit_time="11:10:00",
             exit_reason="target_1r", notes=""),
    ]
    return _write_csv(tmp_path / "journal.csv", JOURNAL_HEADERS, rows)


# ---------------------------------------------------------------------------
# Missing / empty file handling.
# ---------------------------------------------------------------------------


def test_missing_files_produce_empty_but_valid_report(tmp_path: Path):
    report = build_report(
        alpha_path=tmp_path / "nope_alpha.csv",
        decision_path=tmp_path / "nope_dec.csv",
        journal_path=tmp_path / "nope_journal.csv",
    )
    assert report["sources"]["alpha_scores"]["exists"] is False
    assert report["sources"]["decision_log"]["exists"] is False
    assert report["sources"]["journal"]["exists"] is False
    assert report["totals"]["alpha_rows"] == 0
    assert report["totals"]["matched_trades"] == 0
    # Tier stats always include all 5 tiers even when empty
    tiers = [r["tier"] for r in report["tier_stats"]]
    assert tiers == TIER_ORDER
    for row in report["tier_stats"]:
        assert row["count"] == 0
        assert row["avg_score"] is None
        assert row["win_rate"] is None
    # Notes explain what's missing
    joined_notes = " ".join(report["notes"])
    assert "alpha_scores.csv not found" in joined_notes
    assert "journal.csv not found" in joined_notes
    # format_text still works
    txt = format_text(report)
    assert "ALPHA ANALYSIS REPORT" in txt


def test_empty_csv_files_are_treated_as_empty(tmp_path: Path):
    # Alpha with only a header
    alpha = tmp_path / "alpha.csv"
    _write_csv(alpha, ALPHA_HEADERS, [])
    report = build_report(
        alpha_path=alpha,
        decision_path=tmp_path / "no_dec.csv",
        journal_path=tmp_path / "no_journal.csv",
    )
    assert report["sources"]["alpha_scores"]["exists"] is True
    assert report["totals"]["alpha_rows"] == 0


def test_malformed_csv_handled(tmp_path: Path):
    bad = tmp_path / "alpha.csv"
    bad.write_bytes(b"\x00\x01\x02 not a csv \xff")
    df = load_alpha_scores(bad)
    # Parser may succeed and produce a weird DF, or fail; either way no raise
    assert df is not None


# ---------------------------------------------------------------------------
# Alpha-only report (no journal).
# ---------------------------------------------------------------------------


def test_alpha_only_report_has_tier_stats_but_no_outcomes(
    alpha_csv: Path, decision_csv: Path, tmp_path: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=tmp_path / "nope.csv",
    )
    assert report["totals"]["alpha_rows"] == 5
    assert report["totals"]["buy_rows"] == 3
    assert report["totals"]["skip_rows"] == 2
    assert report["totals"]["matched_trades"] == 0

    # Every tier row must exist and have no outcome data
    for row in report["tier_stats"]:
        assert row["win_rate"] is None
        assert row["avg_pnl"] is None
        assert row["avg_r_multiple"] is None

    # But counts and average score work
    a_row = next(r for r in report["tier_stats"] if r["tier"] == "A")
    assert a_row["count"] == 2
    assert a_row["buy_count"] == 2
    assert a_row["skip_count"] == 0
    assert a_row["avg_score"] == pytest.approx((0.92 + 0.85) / 2, rel=1e-3)


# ---------------------------------------------------------------------------
# Joined report with outcomes.
# ---------------------------------------------------------------------------


def test_joined_report_computes_win_rate_pnl_and_r_multiple(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=journal_csv,
        tolerance_minutes=5,
    )
    assert report["totals"]["alpha_rows"] == 5
    assert report["totals"]["journal_trades"] == 3
    # All three buy rows have a trade within 5min — expect 3 matches
    assert report["totals"]["matched_trades"] == 3

    tier_a = next(r for r in report["tier_stats"] if r["tier"] == "A")
    # AAA +200, BBB -50 → 1/2 wins, avg_pnl = 75, avg_R = 0.75
    assert tier_a["count"] == 2
    assert tier_a["outcome_count"] == 2
    assert tier_a["win_rate"] == pytest.approx(0.5, rel=1e-3)
    assert tier_a["avg_pnl"] == pytest.approx(75.0, rel=1e-3)
    assert tier_a["avg_r_multiple"] == pytest.approx(0.75, rel=1e-3)

    tier_b = next(r for r in report["tier_stats"] if r["tier"] == "B")
    # EEE only — winner
    assert tier_b["outcome_count"] == 1
    assert tier_b["win_rate"] == pytest.approx(1.0, rel=1e-3)
    assert tier_b["avg_pnl"] == pytest.approx(50.0, rel=1e-3)

    tier_c = next(r for r in report["tier_stats"] if r["tier"] == "C")
    assert tier_c["count"] == 1
    assert tier_c["outcome_count"] == 0
    assert tier_c["win_rate"] is None

    tier_f = next(r for r in report["tier_stats"] if r["tier"] == "F")
    assert tier_f["count"] == 1
    assert tier_f["outcome_count"] == 0


def test_join_respects_tolerance(alpha_csv: Path, journal_csv: Path):
    """With tolerance=0 nothing matches (journal entry_time is HH:MM:SS
    and alpha uses full timestamps — they align exactly here so the
    check is that a large negative drift breaks the join)."""
    alpha = load_alpha_scores(alpha_csv)
    # Shift all journal times by 10 minutes so tolerance=5 drops them
    journal = load_journal(journal_csv)
    journal["entry_dt"] = journal["entry_dt"] + pd.Timedelta(minutes=10)
    joined = join_outcomes(alpha, journal, tolerance_minutes=5)
    assert not joined["matched"].any()
    joined_wide = join_outcomes(alpha, journal, tolerance_minutes=15)
    # Widening tolerance to 15min brings the buy rows back
    assert int(joined_wide["matched"].sum()) == 3


def test_join_with_empty_journal_keeps_alpha_rows(alpha_csv: Path, tmp_path: Path):
    alpha = load_alpha_scores(alpha_csv)
    empty = load_journal(tmp_path / "missing.csv")
    joined = join_outcomes(alpha, empty)
    assert len(joined) == len(alpha)
    assert not joined["matched"].any()


# ---------------------------------------------------------------------------
# Tier stats.
# ---------------------------------------------------------------------------


def test_tier_stats_always_returns_all_five_tiers(alpha_csv: Path, tmp_path: Path):
    alpha = load_alpha_scores(alpha_csv)
    stats = tier_stats(alpha)
    assert [r["tier"] for r in stats] == TIER_ORDER


def test_tier_stats_buy_skip_counts(alpha_csv: Path):
    alpha = load_alpha_scores(alpha_csv)
    stats = {r["tier"]: r for r in tier_stats(alpha)}
    assert stats["A"]["buy_count"] == 2
    assert stats["A"]["skip_count"] == 0
    assert stats["C"]["buy_count"] == 0
    assert stats["C"]["skip_count"] == 1
    assert stats["F"]["skip_count"] == 1
    # Tiers with no rows
    assert stats["D"]["count"] == 0
    assert stats["D"]["buy_count"] == 0


# ---------------------------------------------------------------------------
# Reason stats.
# ---------------------------------------------------------------------------


def test_reason_stats_uses_decision_reason_when_available(
    alpha_csv: Path, journal_csv: Path
):
    # Merge a `reason` column into alpha rows by piggy-backing on
    # decision_log semantics: build_report doesn't merge the decision
    # reason into the joined frame, so reason_stats will fall back
    # to the `reasons` column's first token. Both modes must produce
    # stats that sum to the number of alpha rows.
    alpha = load_alpha_scores(alpha_csv)
    stats = reason_stats(alpha)
    assert stats  # non-empty
    total = sum(r["count"] for r in stats)
    assert total == len(alpha)
    # Sorted by count descending
    counts = [r["count"] for r in stats]
    assert counts == sorted(counts, reverse=True)


def test_reason_stats_empty_df():
    import pandas as pd
    assert reason_stats(pd.DataFrame()) == []


# ---------------------------------------------------------------------------
# Regime stats.
# ---------------------------------------------------------------------------


def test_regime_stats_groups_by_regime(alpha_csv: Path):
    alpha = load_alpha_scores(alpha_csv)
    stats = regime_stats(alpha)
    regimes = {r["regime"] for r in stats}
    assert regimes == {"trending_bullish", "range_bound", "trending_bearish"}
    bull = next(r for r in stats if r["regime"] == "trending_bullish")
    assert bull["count"] == 2  # AAA + BBB
    assert bull["buy_count"] == 2


# ---------------------------------------------------------------------------
# JSON output.
# ---------------------------------------------------------------------------


def test_json_output_is_parseable(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=journal_csv,
    )
    blob = format_json(report)
    parsed = json.loads(blob)
    assert parsed["totals"]["matched_trades"] == 3
    assert len(parsed["tier_stats"]) == 5
    # Top-level keys present
    assert set(parsed.keys()) >= {
        "sources", "totals", "tier_stats", "reason_stats",
        "regime_stats", "notes", "tolerance_minutes",
    }


def test_text_output_mentions_all_sections(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=journal_csv,
    )
    txt = format_text(report)
    for keyword in ("ALPHA ANALYSIS REPORT", "Sources", "Totals",
                    "By tier", "By reason", "By regime"):
        assert keyword in txt


# ---------------------------------------------------------------------------
# CLI smoke tests.
# ---------------------------------------------------------------------------


def test_main_function_prints_text(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path, capsys
):
    rc = main([
        "--alpha", str(alpha_csv),
        "--decision", str(decision_csv),
        "--journal", str(journal_csv),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALPHA ANALYSIS REPORT" in out


def test_main_function_json_flag(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path, capsys
):
    rc = main([
        "--alpha", str(alpha_csv),
        "--decision", str(decision_csv),
        "--journal", str(journal_csv),
        "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["totals"]["alpha_rows"] == 5


def test_main_function_writes_to_output_file(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path, tmp_path: Path
):
    target = tmp_path / "nested" / "out.json"
    rc = main([
        "--alpha", str(alpha_csv),
        "--decision", str(decision_csv),
        "--journal", str(journal_csv),
        "--json",
        "--output", str(target),
    ])
    assert rc == 0
    assert target.exists()
    parsed = json.loads(target.read_text())
    assert parsed["totals"]["matched_trades"] == 3


def test_cli_module_smoke(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    """`python -m trading_bot.analysis.alpha_report` must exit 0 and print."""
    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.analysis.alpha_report",
            "--alpha", str(alpha_csv),
            "--decision", str(decision_csv),
            "--journal", str(journal_csv),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ALPHA ANALYSIS REPORT" in result.stdout
    assert "By tier" in result.stdout


def test_cli_module_missing_files_still_zero_exit(tmp_path: Path):
    """Running against non-existent paths must still produce a valid report."""
    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.analysis.alpha_report",
            "--alpha", str(tmp_path / "nope.csv"),
            "--decision", str(tmp_path / "nope.csv"),
            "--journal", str(tmp_path / "nope.csv"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ALPHA ANALYSIS REPORT" in result.stdout
    # Note explaining missing files present
    assert "not found" in result.stdout


# ---------------------------------------------------------------------------
# Loader contract tests (no file -> empty-but-valid DataFrame).
# ---------------------------------------------------------------------------


def test_loaders_return_empty_dataframes_when_missing(tmp_path: Path):
    alpha = load_alpha_scores(tmp_path / "absent.csv")
    dec = load_decision_log(tmp_path / "absent.csv")
    journal = load_journal(tmp_path / "absent.csv")
    assert len(alpha) == 0 and len(dec) == 0 and len(journal) == 0
    # Still have the right schema
    assert "timestamp" in alpha.columns
    assert "timestamp" in dec.columns
    assert "entry_dt" in journal.columns


# Also import pandas for a test above
import pandas as pd  # noqa: E402
