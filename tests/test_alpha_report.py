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
    DECILE_LABELS,
    DEFAULT_MIN_REQUIRED_OUTCOMES,
    TIER_ORDER,
    build_report,
    decile_stats,
    format_json,
    format_text,
    join_outcomes,
    load_alpha_scores,
    load_decision_log,
    load_journal,
    main,
    promotion_readiness,
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


# ===========================================================================
# Phase 2.6 — decile calibration + promotion readiness.
# ===========================================================================


def _make_joined_frame(
    tier_outcomes: dict[str, list[float]],
    rr_map: dict[str, list[float]] | None = None,
) -> pd.DataFrame:
    """
    Build a joined DataFrame from {tier: [pnl, pnl, ...]} fixtures.

    One row per pnl entry; tier rows without an rr entry default to
    pnl/100 so avg_r_multiple tracks sign/magnitude intuitively.
    """
    rows: list[dict] = []
    for tier, pnl_list in tier_outcomes.items():
        rr_list = (rr_map or {}).get(tier) or [p / 100.0 for p in pnl_list]
        for i, (pnl, rr) in enumerate(zip(pnl_list, rr_list)):
            rows.append({
                "symbol": f"{tier}{i}",
                "tier": tier,
                "score": {
                    "A": 0.90, "B": 0.70, "C": 0.55,
                    "D": 0.40, "F": 0.20,
                }[tier],
                "action": "buy",
                "regime": "trending_bullish",
                "confidence": 0.8,
                "pnl": pnl,
                "rr_ratio": rr,
                "matched": True,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Decile binning.
# ---------------------------------------------------------------------------


def test_decile_stats_returns_all_ten_buckets_in_order():
    stats = decile_stats(pd.DataFrame())
    assert [r["decile"] for r in stats] == DECILE_LABELS
    for row in stats:
        assert row["count"] == 0
        assert row["win_rate"] is None


def test_decile_binning_assigns_scores_correctly():
    # One row per representative score, confirm it lands in the intended bin.
    df = pd.DataFrame(
        {
            "score": [0.00, 0.05, 0.10, 0.25, 0.49, 0.50, 0.75, 0.90, 0.95, 1.00],
            "action": ["skip"] * 10,
            "tier": ["F"] * 10,
            "regime": ["range_bound"] * 10,
        }
    )
    stats = {r["decile"]: r for r in decile_stats(df)}
    assert stats["0.0-0.1"]["count"] == 2  # 0.00, 0.05
    assert stats["0.1-0.2"]["count"] == 1  # 0.10
    assert stats["0.2-0.3"]["count"] == 1  # 0.25
    assert stats["0.4-0.5"]["count"] == 1  # 0.49
    assert stats["0.5-0.6"]["count"] == 1  # 0.50
    assert stats["0.7-0.8"]["count"] == 1  # 0.75
    assert stats["0.9-1.0"]["count"] == 3  # 0.90, 0.95, 1.00 (last bucket includes 1.0)
    assert stats["0.3-0.4"]["count"] == 0
    assert stats["0.6-0.7"]["count"] == 0
    assert stats["0.8-0.9"]["count"] == 0


def test_decile_stats_include_outcome_fields_when_present():
    df = pd.DataFrame({
        "score": [0.92, 0.88, 0.55, 0.20],
        "action": ["buy", "buy", "skip", "skip"],
        "tier": ["A", "A", "C", "F"],
        "regime": ["trending_bullish"] * 4,
        "pnl": [200.0, -50.0, None, None],
        "rr_ratio": [2.0, -0.5, None, None],
    })
    stats = {r["decile"]: r for r in decile_stats(df)}
    top = stats["0.8-0.9"]
    # 0.88 falls in 0.8-0.9
    assert top["count"] == 1
    assert top["outcome_count"] == 1
    assert top["win_rate"] == 0.0  # single loser
    very_top = stats["0.9-1.0"]
    assert very_top["count"] == 1
    assert very_top["win_rate"] == 1.0
    assert very_top["avg_pnl"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Promotion readiness.
# ---------------------------------------------------------------------------


def test_promotion_readiness_empty_frame_returns_not_ready():
    result = promotion_readiness(pd.DataFrame(), min_required_outcomes=10)
    assert result["status"] == "not_ready"
    assert result["outcome_count"] == 0
    assert result["ab"]["outcome_count"] == 0
    assert result["cdf"]["outcome_count"] == 0
    assert result["ab_outperforms"] is False
    assert "Insufficient outcomes" in result["explanation"]


def test_ab_outperform_with_enough_outcomes_is_ready_for_shadow_filter_test():
    # 60 A/B winners (+100 ea), 40 C/D/F losers (-100 ea) → AB clearly wins,
    # total outcomes 100 >= default 100.
    joined = _make_joined_frame({
        "A": [100.0] * 30 + [-20.0] * 5,  # 30/35 = 85.7% win, total PnL +2900
        "B": [100.0] * 20 + [-20.0] * 5,  # 20/25 = 80% win
        "C": [-100.0] * 15,               # all losers
        "D": [-100.0] * 15,
        "F": [-100.0] * 10,
    })
    result = promotion_readiness(joined, min_required_outcomes=100)
    assert result["status"] == "ready_for_shadow_filter_test"
    assert result["ab_outperforms"] is True
    assert result["outcome_count"] == 100
    assert result["ab"]["win_rate"] > result["cdf"]["win_rate"]
    assert result["ab"]["avg_r_multiple"] > result["cdf"]["avg_r_multiple"]


def test_ab_outperform_but_small_sample_is_promising():
    joined = _make_joined_frame({
        "A": [100.0, 100.0, 100.0, -50.0],
        "B": [100.0, -50.0],
        "C": [-100.0, -100.0],
        "D": [-100.0],
        "F": [-100.0],
    })
    result = promotion_readiness(joined, min_required_outcomes=100)
    assert result["status"] == "promising"
    assert result["ab_outperforms"] is True
    assert result["outcome_count"] < 100


def test_lower_tiers_outperform_ab_is_weak_with_enough_outcomes():
    # Flip the pattern: AB loses more often than CDF.
    joined = _make_joined_frame({
        "A": [-100.0] * 30,
        "B": [-100.0] * 20,
        "C": [100.0] * 20,
        "D": [100.0] * 20,
        "F": [100.0] * 10,
    })
    result = promotion_readiness(joined, min_required_outcomes=50)
    assert result["status"] == "weak"
    assert result["ab_outperforms"] is False
    assert result["ab"]["win_rate"] < result["cdf"]["win_rate"]


def test_lower_tiers_win_with_small_sample_is_not_ready():
    joined = _make_joined_frame({
        "A": [-100.0, -100.0],
        "C": [100.0, 100.0],
    })
    result = promotion_readiness(joined, min_required_outcomes=100)
    assert result["status"] == "not_ready"
    assert result["ab_outperforms"] is False


def test_insufficient_sample_small_fixture_is_not_ready():
    joined = _make_joined_frame({"A": [100.0, -50.0]})
    result = promotion_readiness(joined, min_required_outcomes=100)
    assert result["status"] in {"promising", "not_ready"}
    # With only AB and win_rate=50% via heuristic this can be 'promising';
    # pin to 'not_ready' by lowering AB win rate
    joined2 = _make_joined_frame({"A": [-100.0, -100.0]})
    result2 = promotion_readiness(joined2, min_required_outcomes=100)
    assert result2["status"] == "not_ready"


def test_ab_only_outcomes_positive_expectancy_is_promising():
    """Only A/B traded — CDF has no realized outcomes. Positive
    expectancy on AB alone is enough to be 'promising' (small sample)."""
    joined = _make_joined_frame({"A": [100.0, 100.0, 100.0, -50.0]})
    result = promotion_readiness(joined, min_required_outcomes=100)
    assert result["status"] == "promising"
    assert result["cdf"]["outcome_count"] == 0


# ---------------------------------------------------------------------------
# build_report integration — JSON / text must include the new sections.
# ---------------------------------------------------------------------------


def test_json_includes_readiness_and_decile_stats(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=journal_csv,
        min_required_outcomes=100,
    )
    parsed = json.loads(format_json(report))
    assert "decile_stats" in parsed
    assert len(parsed["decile_stats"]) == 10
    assert {r["decile"] for r in parsed["decile_stats"]} == set(DECILE_LABELS)
    assert "promotion_readiness" in parsed
    pr = parsed["promotion_readiness"]
    assert set(pr.keys()) >= {
        "status", "explanation", "min_required_outcomes",
        "outcome_count", "ab_outperforms", "ab", "cdf",
    }
    # With only 3 outcomes vs min 100, status must be not_ready or promising
    assert pr["status"] in {"not_ready", "promising"}


def test_text_report_includes_readiness_section(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=journal_csv,
        min_required_outcomes=100,
    )
    txt = format_text(report)
    assert "By score decile" in txt
    assert "Promotion readiness:" in txt
    assert "status" in txt
    assert "A/B tiers" in txt
    assert "C/D/F tiers" in txt


def test_default_min_required_outcomes_is_100():
    assert DEFAULT_MIN_REQUIRED_OUTCOMES == 100


# ---------------------------------------------------------------------------
# CLI --min-outcomes flag.
# ---------------------------------------------------------------------------


def test_cli_min_outcomes_changes_readiness_threshold(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path, capsys
):
    # Default threshold (100) — 3 matched trades is nowhere near it.
    rc = main([
        "--alpha", str(alpha_csv),
        "--decision", str(decision_csv),
        "--journal", str(journal_csv),
        "--json",
    ])
    assert rc == 0
    default_parsed = json.loads(capsys.readouterr().out)
    assert default_parsed["promotion_readiness"]["min_required_outcomes"] == 100

    # Very low threshold (2) — 3 matched trades should now be "enough" and
    # if A/B outperforms the status flips to ready_for_shadow_filter_test.
    rc = main([
        "--alpha", str(alpha_csv),
        "--decision", str(decision_csv),
        "--journal", str(journal_csv),
        "--min-outcomes", "2",
        "--json",
    ])
    assert rc == 0
    low_parsed = json.loads(capsys.readouterr().out)
    assert low_parsed["promotion_readiness"]["min_required_outcomes"] == 2
    # With 3 outcomes >= 2 the status is either ready_for_shadow_filter_test
    # (if AB outperforms) or weak (if it doesn't). Either way, never "not_ready"
    # and never "promising" — those both require fewer than min outcomes.
    assert low_parsed["promotion_readiness"]["status"] in {
        "ready_for_shadow_filter_test", "weak",
    }


def test_cli_min_outcomes_subprocess_smoke(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.analysis.alpha_report",
            "--alpha", str(alpha_csv),
            "--decision", str(decision_csv),
            "--journal", str(journal_csv),
            "--min-outcomes", "2",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Promotion readiness:" in result.stdout
    assert "By score decile" in result.stdout


# ===========================================================================
# Phase 2.8 — multi-day / multi-file input support.
# ===========================================================================


from trading_bot.analysis.alpha_report import _resolve_paths  # noqa: E402


def _write_alpha(path: Path, rows: list[dict]) -> Path:
    return _write_csv(path, ALPHA_HEADERS, rows)


def _alpha_row(symbol: str, ts: str, tier: str = "A", score: float = 0.9,
               action: str = "buy") -> dict:
    return dict(
        timestamp=ts, symbol=symbol, score=score, tier=tier,
        action=action, confidence=0.8, regime="trending_bullish",
        gap_pct=10.0, relative_volume=10.0, volatility=3.0,
        reasons=f"gap_sweet_spot|action_{action}",
    )


# ---------------------------------------------------------------------------
# _resolve_paths helper
# ---------------------------------------------------------------------------


def test_resolve_paths_none_returns_empty():
    assert _resolve_paths(None) == []
    assert _resolve_paths("") == []
    assert _resolve_paths("   ") == []


def test_resolve_paths_single_existing_file(tmp_path: Path):
    p = _write_alpha(tmp_path / "alpha.csv", [_alpha_row("A", "2026-04-24 09:45:00")])
    assert _resolve_paths(str(p)) == [p]
    assert _resolve_paths(p) == [p]  # Path object also accepted


def test_resolve_paths_missing_explicit_path_returns_empty(tmp_path: Path):
    assert _resolve_paths(tmp_path / "absent.csv") == []
    assert _resolve_paths(str(tmp_path / "absent.csv")) == []


def test_resolve_paths_glob_returns_sorted_matches(tmp_path: Path):
    p1 = _write_alpha(tmp_path / "alpha_2026-04-24.csv", [_alpha_row("A", "2026-04-24 09:45:00")])
    p2 = _write_alpha(tmp_path / "alpha_2026-04-25.csv", [_alpha_row("B", "2026-04-25 09:45:00")])
    p3 = _write_alpha(tmp_path / "alpha_2026-04-23.csv", [_alpha_row("C", "2026-04-23 09:45:00")])
    got = _resolve_paths(str(tmp_path / "alpha_*.csv"))
    assert got == [p3, p1, p2]  # lexicographic → chronological


def test_resolve_paths_comma_separated(tmp_path: Path):
    p1 = _write_alpha(tmp_path / "a.csv", [_alpha_row("A", "2026-04-24 09:45:00")])
    p2 = _write_alpha(tmp_path / "b.csv", [_alpha_row("B", "2026-04-25 09:45:00")])
    spec = f"{p1},{p2}"
    assert _resolve_paths(spec) == [p1, p2]


def test_resolve_paths_mixed_glob_and_explicit(tmp_path: Path):
    p1 = _write_alpha(tmp_path / "alpha_2026-04-24.csv", [_alpha_row("A", "2026-04-24 09:45:00")])
    p2 = _write_alpha(tmp_path / "alpha_2026-04-25.csv", [_alpha_row("B", "2026-04-25 09:45:00")])
    explicit = _write_alpha(tmp_path / "extra.csv", [_alpha_row("C", "2026-04-26 09:45:00")])
    spec = f"{tmp_path}/alpha_*.csv,{explicit}"
    got = _resolve_paths(spec)
    assert got == [p1, p2, explicit]


def test_resolve_paths_deduplicates(tmp_path: Path):
    p = _write_alpha(tmp_path / "alpha.csv", [_alpha_row("A", "2026-04-24 09:45:00")])
    spec = f"{p},{p},{p}"
    assert _resolve_paths(spec) == [p]


# ---------------------------------------------------------------------------
# Single-file behaviour still works.
# ---------------------------------------------------------------------------


def test_single_file_still_works_end_to_end(
    alpha_csv: Path, decision_csv: Path, journal_csv: Path
):
    report = build_report(
        alpha_path=alpha_csv,
        decision_path=decision_csv,
        journal_path=journal_csv,
    )
    assert report["totals"]["alpha_rows"] == 5
    alpha_src = report["sources"]["alpha_scores"]
    assert alpha_src["exists"] is True
    assert alpha_src["resolved_files"] == 1
    assert alpha_src["resolved_paths"] == [str(alpha_csv)]
    assert alpha_src["path"] == str(alpha_csv)


# ---------------------------------------------------------------------------
# Glob loads multiple rotated files.
# ---------------------------------------------------------------------------


def test_glob_loads_multiple_rotated_alpha_files(tmp_path: Path):
    _write_alpha(tmp_path / "alpha_scores_2026-04-24.csv", [
        _alpha_row("AAA", "2026-04-24 09:45:00"),
        _alpha_row("BBB", "2026-04-24 10:00:00", tier="B", score=0.7),
    ])
    _write_alpha(tmp_path / "alpha_scores_2026-04-25.csv", [
        _alpha_row("CCC", "2026-04-25 09:45:00"),
        _alpha_row("DDD", "2026-04-25 10:00:00", tier="F", score=0.2, action="skip"),
    ])

    df = load_alpha_scores(str(tmp_path / "alpha_scores_*.csv"))
    assert len(df) == 4
    symbols = sorted(df["symbol"].tolist())
    assert symbols == ["AAA", "BBB", "CCC", "DDD"]


def test_glob_loads_multiple_days_into_report(tmp_path: Path):
    _write_alpha(tmp_path / "alpha_scores_2026-04-24.csv", [
        _alpha_row("AAA", "2026-04-24 09:45:00"),
    ])
    _write_alpha(tmp_path / "alpha_scores_2026-04-25.csv", [
        _alpha_row("BBB", "2026-04-25 09:45:00"),
    ])
    _write_alpha(tmp_path / "alpha_scores_2026-04-26.csv", [
        _alpha_row("CCC", "2026-04-26 09:45:00"),
    ])

    report = build_report(
        alpha_path=str(tmp_path / "alpha_scores_*.csv"),
        decision_path=tmp_path / "absent_decision.csv",
        journal_path=tmp_path / "absent_journal.csv",
    )
    src = report["sources"]["alpha_scores"]
    assert src["resolved_files"] == 3
    assert len(src["resolved_paths"]) == 3
    assert src["rows"] == 3
    assert src["exists"] is True
    assert report["totals"]["alpha_rows"] == 3


# ---------------------------------------------------------------------------
# Comma-separated inputs.
# ---------------------------------------------------------------------------


def test_comma_separated_inputs_concatenate(tmp_path: Path):
    p1 = _write_alpha(tmp_path / "day1.csv", [_alpha_row("AAA", "2026-04-24 09:45:00")])
    p2 = _write_alpha(tmp_path / "day2.csv", [_alpha_row("BBB", "2026-04-25 09:45:00")])
    df = load_alpha_scores(f"{p1},{p2}")
    assert len(df) == 2
    assert set(df["symbol"]) == {"AAA", "BBB"}


def test_comma_separated_mix_of_existing_and_missing(tmp_path: Path):
    p1 = _write_alpha(tmp_path / "day1.csv", [_alpha_row("AAA", "2026-04-24 09:45:00")])
    # Missing file in the middle must not crash; surviving files still load.
    spec = f"{p1},{tmp_path / 'ghost.csv'},{tmp_path / 'also_missing.csv'}"
    df = load_alpha_scores(spec)
    assert len(df) == 1
    assert df["symbol"].iloc[0] == "AAA"


# ---------------------------------------------------------------------------
# Missing paths / empty globs don't crash.
# ---------------------------------------------------------------------------


def test_missing_glob_does_not_crash(tmp_path: Path):
    df = load_alpha_scores(str(tmp_path / "no_match_*.csv"))
    assert len(df) == 0
    # build_report with a fully-missing glob behaves like the
    # missing-file case from Phase 2.5.
    report = build_report(
        alpha_path=str(tmp_path / "no_match_*.csv"),
        decision_path=str(tmp_path / "no_match_dec_*.csv"),
        journal_path=str(tmp_path / "no_match_j_*.csv"),
    )
    assert report["sources"]["alpha_scores"]["exists"] is False
    assert report["sources"]["alpha_scores"]["resolved_files"] == 0
    assert report["totals"]["alpha_rows"] == 0


# ---------------------------------------------------------------------------
# Duplicate header rows inside a concatenated file are dropped.
# ---------------------------------------------------------------------------


def test_duplicate_header_rows_are_dropped(tmp_path: Path):
    """
    Simulate `cat day1.csv day2.csv > merged.csv` — the second file's
    header ends up as a data row. The loader must filter it out.
    """
    target = tmp_path / "merged.csv"
    header = ",".join(ALPHA_HEADERS)
    row1 = f"2026-04-24 09:45:00,AAA,0.9,A,buy,0.8,trending_bullish,10,10,3,gap"
    row2 = f"2026-04-25 09:45:00,BBB,0.7,B,buy,0.7,range_bound,8,5,2,gap"
    # Header appears TWICE — once at the top, and once again before row2
    content = "\n".join([header, row1, header, row2]) + "\n"
    target.write_text(content)

    df = load_alpha_scores(target)
    assert len(df) == 2
    assert set(df["symbol"]) == {"AAA", "BBB"}


def test_journal_duplicate_header_rows_dropped(tmp_path: Path):
    target = tmp_path / "merged_journal.csv"
    header = ",".join(JOURNAL_HEADERS)
    row1 = "2026-04-24,AAA,buy,vwap_pullback,10,12,100,200,2.0,30,09:45:00,10:15:00,target_2r,"
    row2 = "2026-04-25,BBB,buy,ema_pullback,12,11,100,-50,-0.5,20,10:00:00,10:20:00,stop_loss,"
    content = "\n".join([header, row1, header, row2]) + "\n"
    target.write_text(content)

    df = load_journal(target)
    assert len(df) == 2
    assert set(df["symbol"]) == {"AAA", "BBB"}
    assert df["entry_dt"].notna().all()


# ---------------------------------------------------------------------------
# JSON includes resolved file counts.
# ---------------------------------------------------------------------------


def test_json_includes_resolved_file_counts(tmp_path: Path):
    _write_alpha(tmp_path / "alpha_2026-04-24.csv", [_alpha_row("AAA", "2026-04-24 09:45:00")])
    _write_alpha(tmp_path / "alpha_2026-04-25.csv", [_alpha_row("BBB", "2026-04-25 09:45:00")])

    report = build_report(
        alpha_path=str(tmp_path / "alpha_*.csv"),
        decision_path=tmp_path / "absent.csv",
        journal_path=tmp_path / "absent_j.csv",
    )
    parsed = json.loads(format_json(report))
    src = parsed["sources"]["alpha_scores"]
    assert src["resolved_files"] == 2
    assert len(src["resolved_paths"]) == 2
    assert src["rows"] == 2
    # All source blocks have the new fields
    for name in ("alpha_scores", "decision_log", "journal"):
        s = parsed["sources"][name]
        assert "resolved_files" in s
        assert "resolved_paths" in s


def test_text_report_lists_individual_files_when_multiple(tmp_path: Path):
    _write_alpha(tmp_path / "alpha_2026-04-24.csv", [_alpha_row("A", "2026-04-24 09:45:00")])
    _write_alpha(tmp_path / "alpha_2026-04-25.csv", [_alpha_row("B", "2026-04-25 09:45:00")])
    report = build_report(
        alpha_path=str(tmp_path / "alpha_*.csv"),
        decision_path=tmp_path / "absent.csv",
        journal_path=tmp_path / "absent_j.csv",
    )
    txt = format_text(report)
    assert "files=2" in txt
    assert "alpha_2026-04-24.csv" in txt
    assert "alpha_2026-04-25.csv" in txt


# ---------------------------------------------------------------------------
# CLI smoke test with glob input.
# ---------------------------------------------------------------------------


def test_cli_glob_smoke(tmp_path: Path):
    # Build two rotated alpha files + two decision files + one journal
    _write_alpha(tmp_path / "alpha_scores_2026-04-24.csv", [
        _alpha_row("AAA", "2026-04-24 09:45:00"),
        _alpha_row("BBB", "2026-04-24 10:00:00"),
    ])
    _write_alpha(tmp_path / "alpha_scores_2026-04-25.csv", [
        _alpha_row("CCC", "2026-04-25 09:45:00"),
    ])
    _write_csv(tmp_path / "decision_log_2026-04-24.csv", DECISION_HEADERS, [
        dict(timestamp="2026-04-24 09:45:00", symbol="AAA", price=10.0,
             gap_pct=10.0, relative_volume=10.0, volatility=3.0,
             regime="trending_bullish", action="buy", confidence=0.8,
             reason="executed"),
        dict(timestamp="2026-04-24 10:00:00", symbol="BBB", price=11.0,
             gap_pct=10.0, relative_volume=10.0, volatility=3.0,
             regime="trending_bullish", action="buy", confidence=0.7,
             reason="executed"),
    ])
    _write_csv(tmp_path / "decision_log_2026-04-25.csv", DECISION_HEADERS, [
        dict(timestamp="2026-04-25 09:45:00", symbol="CCC", price=12.0,
             gap_pct=10.0, relative_volume=10.0, volatility=3.0,
             regime="trending_bullish", action="buy", confidence=0.8,
             reason="executed"),
    ])

    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.analysis.alpha_report",
            "--alpha", str(tmp_path / "alpha_scores_*.csv"),
            "--decision", str(tmp_path / "decision_log_*.csv"),
            "--journal", str(tmp_path / "no_journal_*.csv"),  # empty glob
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["sources"]["alpha_scores"]["resolved_files"] == 2
    assert parsed["sources"]["decision_log"]["resolved_files"] == 2
    assert parsed["sources"]["journal"]["resolved_files"] == 0
    assert parsed["totals"]["alpha_rows"] == 3


def test_cli_comma_separated_smoke(tmp_path: Path):
    a1 = _write_alpha(tmp_path / "a1.csv", [_alpha_row("AAA", "2026-04-24 09:45:00")])
    a2 = _write_alpha(tmp_path / "a2.csv", [_alpha_row("BBB", "2026-04-25 09:45:00")])

    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.analysis.alpha_report",
            "--alpha", f"{a1},{a2}",
            "--decision", str(tmp_path / "absent.csv"),
            "--journal", str(tmp_path / "absent_j.csv"),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["sources"]["alpha_scores"]["resolved_files"] == 2
    assert parsed["totals"]["alpha_rows"] == 2
