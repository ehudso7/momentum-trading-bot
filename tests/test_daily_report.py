"""
Phase 3.2 tests: automated daily alpha validation report.

Covers:
- file creation (txt + json)
- correct date handling (explicit and default/today)
- missing CSV files handled gracefully
- report content includes readiness, simulation, and decile sections
- CLI execution (module + in-process main)
- function never raises, even on fatal write failure
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date as _date_type
from pathlib import Path
from typing import Iterable

import pytest

from trading_bot.reporting import daily_report as daily_report_mod
from trading_bot.reporting.daily_report import (
    DAILY_HEADER,
    GUARDRAIL_MIN_MATCHED_TRADES,
    GUARDRAIL_STATUS_CRITICAL,
    GUARDRAIL_STATUS_INSUFFICIENT,
    GUARDRAIL_STATUS_OK,
    GUARDRAIL_STATUS_WARNING,
    DailyReportResult,
    evaluate_guardrails,
    generate_daily_report,
    main,
)


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_csv(path: Path, headers: list[str], rows: Iterable[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


@pytest.fixture
def populated_data_dir(tmp_path: Path) -> tuple[Path, str]:
    """A minimal but realistic Core-dataset directory for a single day."""
    report_date = "2026-04-24"
    data = tmp_path / "data"
    _write_csv(
        data / f"alpha_scores_{report_date}.csv",
        ALPHA_HEADERS,
        [
            dict(timestamp="2026-04-24 09:45:00", symbol="AAA", score=0.92,
                 tier="A", action="buy", confidence=0.85,
                 regime="trending_bullish", gap_pct=12.0, relative_volume=15.0,
                 volatility=3.5, reasons="gap_sweet_spot|action_buy"),
            dict(timestamp="2026-04-24 10:00:00", symbol="BBB", score=0.20,
                 tier="F", action="skip", confidence=0.30,
                 regime="trending_bearish", gap_pct=2.0, relative_volume=1.2,
                 volatility=0.5, reasons="gap_weak|action_skip:strategy"),
        ],
    )
    _write_csv(
        data / f"decision_log_{report_date}.csv",
        DECISION_HEADERS,
        [
            dict(timestamp="2026-04-24 09:45:00", symbol="AAA", price=10.0,
                 gap_pct=12.0, relative_volume=15.0, volatility=3.5,
                 regime="trending_bullish", action="buy", confidence=0.85,
                 reason="executed"),
            dict(timestamp="2026-04-24 10:00:00", symbol="BBB", price=3.0,
                 gap_pct=2.0, relative_volume=1.2, volatility=0.5,
                 regime="trending_bearish", action="skip", confidence=0.30,
                 reason="strategy:no_valid_setup"),
        ],
    )
    _write_csv(
        data / "journal.csv",
        JOURNAL_HEADERS,
        [
            dict(date="2026-04-24", symbol="AAA", side="buy",
                 signal_type="vwap_pullback", entry_price=10.0, exit_price=12.0,
                 shares=100, pnl=200.0, rr_ratio=2.0, hold_time_minutes=30,
                 entry_time="09:45:00", exit_time="10:15:00",
                 exit_reason="target_2r", notes=""),
        ],
    )
    return data, report_date


# ---------------------------------------------------------------------------
# File creation (txt + json)
# ---------------------------------------------------------------------------


def test_generate_daily_report_creates_txt_and_json(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    reports_dir = tmp_path / "reports"

    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=reports_dir,
        min_required_outcomes=2,
    )

    assert isinstance(result, DailyReportResult)
    assert result.success is True
    assert result.error is None
    assert result.date == report_date
    assert result.txt_path.exists()
    assert result.json_path.exists()
    # Returned paths match the documented naming convention.
    assert result.txt_path.name == f"alpha_report_{report_date}.txt"
    assert result.json_path.name == f"alpha_report_{report_date}.json"


def test_generate_daily_report_creates_reports_dir_if_missing(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    # Deliberately nest into a non-existent path
    reports_dir = tmp_path / "not" / "yet" / "there" / "reports"
    assert not reports_dir.exists()

    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=reports_dir,
        min_required_outcomes=2,
    )
    assert result.success is True
    assert reports_dir.is_dir()


# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------


def test_explicit_date_used_verbatim(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    reports_dir = tmp_path / "reports"

    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=reports_dir,
        min_required_outcomes=2,
    )
    assert result.date == report_date
    assert f"alpha_report_{report_date}.txt" in result.txt_path.name


def test_default_date_is_today(tmp_path: Path, monkeypatch):
    """Passing `date=None` must fall back to today's date via _today_str."""
    monkeypatch.setattr(daily_report_mod, "_today_str", lambda: "2026-01-15")
    # No data files on disk — expects graceful report generation.
    reports_dir = tmp_path / "reports"

    result = generate_daily_report(
        date=None,
        data_dir=tmp_path / "data",
        reports_dir=reports_dir,
    )
    assert result.date == "2026-01-15"
    assert result.txt_path.name == "alpha_report_2026-01-15.txt"
    assert result.json_path.name == "alpha_report_2026-01-15.json"
    assert result.txt_path.exists()
    assert result.json_path.exists()


def test_empty_string_date_also_falls_back_to_today(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(daily_report_mod, "_today_str", lambda: "2026-01-15")
    result = generate_daily_report(
        date="   ",
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
    )
    assert result.date == "2026-01-15"


# ---------------------------------------------------------------------------
# Missing CSV files
# ---------------------------------------------------------------------------


def test_missing_csv_files_do_not_crash(tmp_path: Path):
    """No data on disk at all — function must still produce both files."""
    result = generate_daily_report(
        date="2026-04-24",
        data_dir=tmp_path / "empty_data",
        reports_dir=tmp_path / "reports",
    )
    assert result.success is True
    assert result.txt_path.exists()
    assert result.json_path.exists()

    # The text report records the missing-file note.
    txt = result.txt_path.read_text()
    assert "alpha_scores.csv not found" in txt
    payload = json.loads(result.json_path.read_text())
    assert payload["sources"]["alpha_scores"]["exists"] is False
    assert payload["sources"]["journal"]["exists"] is False


def test_missing_only_journal_still_succeeds(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    """Alpha + decision rows exist, journal is absent. Readiness should be
    `not_ready` but the report must still write cleanly."""
    data_dir, report_date = populated_data_dir
    (data_dir / "journal.csv").unlink()

    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=100,
    )
    assert result.success is True
    payload = json.loads(result.json_path.read_text())
    assert payload["sources"]["journal"]["exists"] is False
    # No realized outcomes → readiness defaults to not_ready.
    assert payload["promotion_readiness"]["status"] == "not_ready"


# ---------------------------------------------------------------------------
# Report content (readiness + simulation + decile)
# ---------------------------------------------------------------------------


def test_text_report_has_daily_header_and_all_sections(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=1,
    )
    txt = result.txt_path.read_text()

    # Daily header present and scoped to the correct date.
    assert DAILY_HEADER in txt
    assert report_date in txt

    # Required sections carried through from the underlying analysis.
    for heading in (
        "By tier",
        "By score decile",
        "Shadow filter simulation:",
        "Promotion readiness:",
    ):
        assert heading in txt, f"missing section: {heading}"


def test_json_payload_has_report_type_and_date_and_sections(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=1,
    )
    payload = json.loads(result.json_path.read_text())
    assert payload["report_type"] == "daily_alpha_validation"
    assert payload["report_date"] == report_date
    # All documented section keys present
    for key in (
        "sources", "totals", "tier_stats", "reason_stats", "regime_stats",
        "decile_stats", "promotion_readiness", "shadow_filter_simulation",
    ):
        assert key in payload, f"missing key: {key}"
    # Decile stats is always 10 rows
    assert len(payload["decile_stats"]) == 10
    # Shadow filter sim always contains three threshold rows
    sim_labels = [r["threshold"] for r in payload["shadow_filter_simulation"]]
    assert sim_labels == ["A only", "A+B", "A+B+C"]


def test_readiness_reflects_min_outcomes_argument(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    # Low threshold → matched single AAA buy counts as "enough" sample
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=1,
    )
    payload = json.loads(result.json_path.read_text())
    pr = payload["promotion_readiness"]
    assert pr["min_required_outcomes"] == 1
    assert pr["outcome_count"] == 1
    assert pr["status"] in {"ready_for_shadow_filter_test", "weak"}


# ---------------------------------------------------------------------------
# Does not raise on failure
# ---------------------------------------------------------------------------


def test_function_never_raises_on_write_failure(
    populated_data_dir: tuple[Path, str], tmp_path: Path, monkeypatch
):
    """Simulate a disk-failure on file writes. The function must
    return a failed result instead of propagating the exception."""
    data_dir, report_date = populated_data_dir
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    original_write_text = Path.write_text

    def fail_write_text(self, *args, **kwargs):
        if self.parent == reports_dir:
            raise OSError("simulated disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_write_text)

    # MUST NOT raise. Must return a result with success=False.
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=reports_dir,
        min_required_outcomes=1,
    )
    assert result.success is False
    assert result.error is not None
    assert "simulated disk full" in result.error


def test_function_never_raises_on_build_report_failure(
    populated_data_dir: tuple[Path, str], tmp_path: Path, monkeypatch
):
    """Even if the underlying analysis module blows up, we swallow."""
    data_dir, report_date = populated_data_dir

    def explode(*args, **kwargs):
        raise RuntimeError("analysis layer exploded")

    monkeypatch.setattr(daily_report_mod, "build_report", explode)
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
    )
    assert result.success is False
    assert "analysis layer exploded" in (result.error or "")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_function_returns_zero_on_success(
    populated_data_dir: tuple[Path, str], tmp_path: Path, capsys
):
    data_dir, report_date = populated_data_dir
    reports_dir = tmp_path / "reports"

    rc = main([
        "--date", report_date,
        "--data-dir", str(data_dir),
        "--reports-dir", str(reports_dir),
        "--min-outcomes", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Daily alpha validation report written" in out
    assert (reports_dir / f"alpha_report_{report_date}.txt").exists()
    assert (reports_dir / f"alpha_report_{report_date}.json").exists()


def test_main_function_returns_nonzero_on_failure(
    populated_data_dir: tuple[Path, str], tmp_path: Path, monkeypatch, capsys
):
    """When the underlying generate fails, main returns a non-zero code
    but STILL does not raise — appropriate for a CLI."""
    data_dir, report_date = populated_data_dir

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(daily_report_mod, "build_report", explode)
    rc = main([
        "--date", report_date,
        "--data-dir", str(data_dir),
        "--reports-dir", str(tmp_path / "reports"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed" in err.lower()


def test_cli_module_smoke(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    """`python -m trading_bot.reporting.daily_report` must exit 0 and write both files."""
    data_dir, report_date = populated_data_dir
    reports_dir = tmp_path / "reports"

    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.reporting.daily_report",
            "--date", report_date,
            "--data-dir", str(data_dir),
            "--reports-dir", str(reports_dir),
            "--min-outcomes", "1",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (reports_dir / f"alpha_report_{report_date}.txt").exists()
    assert (reports_dir / f"alpha_report_{report_date}.json").exists()


def test_cli_default_date_is_today(tmp_path: Path, monkeypatch):
    """Without --date the CLI must pick up today's date."""
    today = _date_type.today().strftime("%Y-%m-%d")
    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.reporting.daily_report",
            "--data-dir", str(tmp_path / "data"),  # non-existent → graceful
            "--reports-dir", str(tmp_path / "reports"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "reports" / f"alpha_report_{today}.txt").exists()
    assert (tmp_path / "reports" / f"alpha_report_{today}.json").exists()


# ---------------------------------------------------------------------------
# main.py integration — the shutdown call path exists and is guarded
# ---------------------------------------------------------------------------


class TestMainPyIntegration:
    """Source-level structural tests for the Phase 3.2 integration."""

    def _main_py_text(self) -> str:
        return (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "main.py"
        ).read_text()

    def test_main_imports_generate_daily_report(self):
        src = self._main_py_text()
        assert "from trading_bot.reporting.daily_report import generate_daily_report" in src

    def test_shutdown_path_calls_daily_alpha_report(self):
        src = self._main_py_text()
        assert "_generate_daily_alpha_report" in src
        # Called inside the shutdown block (right after the daily summary).
        shutdown_idx = src.find("_generate_daily_summary()")
        report_idx = src.find("_generate_daily_alpha_report()")
        assert shutdown_idx != -1 and report_idx != -1
        assert shutdown_idx < report_idx, (
            "alpha report must run AFTER the existing daily summary"
        )

    def test_integration_is_wrapped_in_try_except(self):
        """The helper itself must catch any failure so shutdown is unblocked."""
        src = self._main_py_text()
        # Grab the helper method body and assert a try/except is present.
        start = src.find("def _generate_daily_alpha_report(")
        assert start != -1, "_generate_daily_alpha_report method missing"
        # Look within a reasonable slice of the method.
        tail = src[start:start + 2000]
        assert "try:" in tail
        assert "except" in tail


# ===========================================================================
# Phase 3.3 — alpha performance guardrails
# ===========================================================================


# ---------------------------------------------------------------------------
# Synthetic report-dict fixtures — each pins the minimum fields the
# guardrail logic actually reads.
# ---------------------------------------------------------------------------


def _sim_row(
    threshold: str,
    allowed_wr: float | None = None,
    blocked_wr: float | None = None,
    allowed_r: float | None = None,
    blocked_r: float | None = None,
) -> dict:
    return {
        "threshold": threshold,
        "allowed_tiers": [],
        "allowed_buy_count": 0,
        "blocked_buy_count": 0,
        "allowed_outcome_count": 0,
        "allowed_win_rate": allowed_wr,
        "allowed_avg_pnl": None,
        "allowed_avg_r_multiple": allowed_r,
        "blocked_outcome_count": 0,
        "blocked_win_rate": blocked_wr,
        "blocked_avg_pnl": None,
        "blocked_avg_r_multiple": blocked_r,
    }


def _report(
    *,
    matched_trades: int,
    sim_rows: list[dict] | None = None,
    readiness_status: str = "ready_for_shadow_filter_test",
    ab_outcome_count: int = 200,
    min_required: int = 100,
) -> dict:
    return {
        "totals": {"matched_trades": matched_trades},
        "promotion_readiness": {
            "status": readiness_status,
            "min_required_outcomes": min_required,
            "ab": {"outcome_count": ab_outcome_count},
            "cdf": {"outcome_count": 0},
        },
        "shadow_filter_simulation": sim_rows or [],
    }


# ---------------------------------------------------------------------------
# insufficient_data
# ---------------------------------------------------------------------------


def test_guardrails_insufficient_data_when_matched_trades_below_threshold():
    report = _report(matched_trades=GUARDRAIL_MIN_MATCHED_TRADES - 1)
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_INSUFFICIENT
    assert isinstance(out["reasons"], list) and out["reasons"]
    assert "matched trades" in out["reasons"][0]
    assert "recommended_action" in out


def test_guardrails_insufficient_data_for_zero_matched_trades():
    assert evaluate_guardrails(_report(matched_trades=0))["status"] == (
        GUARDRAIL_STATUS_INSUFFICIENT
    )


def test_guardrails_insufficient_data_for_empty_report_dict():
    # Malformed / empty input must not crash and must yield
    # insufficient_data rather than guessing at status.
    assert evaluate_guardrails({})["status"] == GUARDRAIL_STATUS_INSUFFICIENT
    assert evaluate_guardrails(None)["status"] == GUARDRAIL_STATUS_INSUFFICIENT


# ---------------------------------------------------------------------------
# ok
# ---------------------------------------------------------------------------


def test_guardrails_ok_when_allowed_beats_blocked():
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row("A only"),
            _sim_row(
                "A+B",
                allowed_wr=0.65, blocked_wr=0.40,
                allowed_r=1.2, blocked_r=-0.3,
            ),
            _sim_row("A+B+C"),
        ],
        readiness_status="ready_for_shadow_filter_test",
        ab_outcome_count=150,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_OK
    # Reason still populated so operators have context, not empty.
    assert out["reasons"]


def test_guardrails_ok_treats_equal_metrics_as_not_critical():
    """A tie (allowed == blocked) must not flip to critical."""
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.5, blocked_wr=0.5,
                allowed_r=1.0, blocked_r=1.0,
            ),
        ],
        readiness_status="ready_for_shadow_filter_test",
        ab_outcome_count=150,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_OK


# ---------------------------------------------------------------------------
# warning
# ---------------------------------------------------------------------------


def test_guardrails_warning_when_readiness_is_weak():
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.6, blocked_wr=0.4,
                allowed_r=1.0, blocked_r=0.0,
            ),
        ],
        readiness_status="weak",
        ab_outcome_count=150,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_WARNING
    assert any("weak" in r.lower() for r in out["reasons"])


def test_guardrails_warning_when_ab_outcome_count_below_min():
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.6, blocked_wr=0.4,
                allowed_r=1.0, blocked_r=0.0,
            ),
        ],
        readiness_status="promising",
        ab_outcome_count=10,   # well below min_required=100
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_WARNING
    assert any("min_required_outcomes" in r for r in out["reasons"])


def test_guardrails_warning_stacks_multiple_reasons():
    """Both conditions should yield warning with BOTH reasons listed."""
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.6, blocked_wr=0.4,
                allowed_r=1.0, blocked_r=0.0,
            ),
        ],
        readiness_status="weak",
        ab_outcome_count=10,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_WARNING
    assert len(out["reasons"]) >= 2


# ---------------------------------------------------------------------------
# critical
# ---------------------------------------------------------------------------


def test_guardrails_critical_by_avg_r_multiple():
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.55, blocked_wr=0.50,   # WR favours allowed
                allowed_r=0.4, blocked_r=1.1,       # avg R favours BLOCKED
            ),
        ],
        readiness_status="ready_for_shadow_filter_test",
        ab_outcome_count=150,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_CRITICAL
    assert any("avg R" in r for r in out["reasons"])
    assert "disable" in out["recommended_action"].lower()


def test_guardrails_critical_by_win_rate():
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.40, blocked_wr=0.70,   # WR favours blocked
                allowed_r=1.0, blocked_r=0.9,       # R favours allowed
            ),
        ],
        readiness_status="ready_for_shadow_filter_test",
        ab_outcome_count=150,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_CRITICAL
    assert any("win rate" in r.lower() for r in out["reasons"])


def test_guardrails_critical_requires_both_sides_to_have_outcomes():
    """If one side has no realized outcomes (None) the comparison is skipped."""
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.5, blocked_wr=None,   # no blocked wr
                allowed_r=1.0, blocked_r=None,
            ),
        ],
        readiness_status="ready_for_shadow_filter_test",
        ab_outcome_count=150,
        min_required=100,
    )
    out = evaluate_guardrails(report)
    # Neither comparison can fire so status is not critical.
    assert out["status"] != GUARDRAIL_STATUS_CRITICAL


def test_guardrails_critical_priority_over_warning():
    """Critical conditions pre-empt warning conditions in the same report."""
    report = _report(
        matched_trades=50,
        sim_rows=[
            _sim_row(
                "A+B",
                allowed_wr=0.3, blocked_wr=0.7,  # critical
                allowed_r=0.1, blocked_r=1.0,    # critical
            ),
        ],
        readiness_status="weak",      # would have warned
        ab_outcome_count=1,           # also would have warned
        min_required=100,
    )
    out = evaluate_guardrails(report)
    assert out["status"] == GUARDRAIL_STATUS_CRITICAL


# ---------------------------------------------------------------------------
# Report integration — JSON and text include guardrails
# ---------------------------------------------------------------------------


def test_json_report_includes_guardrails_block(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=1,
    )
    payload = json.loads(result.json_path.read_text())
    assert "guardrails" in payload
    gr = payload["guardrails"]
    for key in ("status", "reasons", "recommended_action"):
        assert key in gr, f"missing guardrails.{key}"
    # Status must be one of the documented values.
    assert gr["status"] in {
        GUARDRAIL_STATUS_OK,
        GUARDRAIL_STATUS_WARNING,
        GUARDRAIL_STATUS_CRITICAL,
        GUARDRAIL_STATUS_INSUFFICIENT,
    }


def test_text_report_includes_alpha_guardrails_section(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=1,
    )
    txt = result.txt_path.read_text()
    assert "Alpha guardrails:" in txt
    assert "status" in txt
    assert "recommended_action" in txt
    assert "reasons:" in txt


def test_result_carries_guardrail_status(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    result = generate_daily_report(
        date=report_date,
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        min_required_outcomes=1,
    )
    assert result.guardrail_status is not None
    assert result.guardrail_status in {
        GUARDRAIL_STATUS_OK,
        GUARDRAIL_STATUS_WARNING,
        GUARDRAIL_STATUS_CRITICAL,
        GUARDRAIL_STATUS_INSUFFICIENT,
    }


def test_missing_csv_report_has_insufficient_data_guardrail(tmp_path: Path):
    """A totally-empty data dir yields an insufficient_data guardrail."""
    result = generate_daily_report(
        date="2026-04-24",
        data_dir=tmp_path / "empty",
        reports_dir=tmp_path / "reports",
    )
    assert result.success is True
    payload = json.loads(result.json_path.read_text())
    assert payload["guardrails"]["status"] == GUARDRAIL_STATUS_INSUFFICIENT
    assert result.guardrail_status == GUARDRAIL_STATUS_INSUFFICIENT


# ---------------------------------------------------------------------------
# CLI prints status
# ---------------------------------------------------------------------------


def test_cli_main_prints_guardrail_status(
    populated_data_dir: tuple[Path, str], tmp_path: Path, capsys
):
    data_dir, report_date = populated_data_dir
    rc = main([
        "--date", report_date,
        "--data-dir", str(data_dir),
        "--reports-dir", str(tmp_path / "reports"),
        "--min-outcomes", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "guardrail status" in out.lower()


def test_cli_subprocess_prints_guardrail_status(
    populated_data_dir: tuple[Path, str], tmp_path: Path
):
    data_dir, report_date = populated_data_dir
    result = subprocess.run(
        [
            sys.executable, "-m", "trading_bot.reporting.daily_report",
            "--date", report_date,
            "--data-dir", str(data_dir),
            "--reports-dir", str(tmp_path / "reports"),
            "--min-outcomes", "1",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "guardrail status" in result.stdout.lower()
    # One of the documented statuses must appear.
    assert any(
        status in result.stdout
        for status in (
            GUARDRAIL_STATUS_OK,
            GUARDRAIL_STATUS_WARNING,
            GUARDRAIL_STATUS_CRITICAL,
            GUARDRAIL_STATUS_INSUFFICIENT,
        )
    )


# ---------------------------------------------------------------------------
# Purity — evaluate_guardrails never mutates its input and never raises
# ---------------------------------------------------------------------------


def test_evaluate_guardrails_does_not_mutate_input():
    report = _report(
        matched_trades=50,
        sim_rows=[_sim_row("A+B", allowed_wr=0.5, blocked_wr=0.4,
                           allowed_r=1.0, blocked_r=0.5)],
    )
    before = json.dumps(report, sort_keys=True, default=str)
    evaluate_guardrails(report)
    after = json.dumps(report, sort_keys=True, default=str)
    assert before == after, "evaluate_guardrails mutated its input"


def test_evaluate_guardrails_handles_malformed_fields():
    """Strings where numbers are expected should not crash the evaluator."""
    report = {
        "totals": {"matched_trades": "not_a_number"},
        "promotion_readiness": {
            "status": "weak",
            "min_required_outcomes": "also_bogus",
            "ab": {"outcome_count": None},
        },
        "shadow_filter_simulation": [
            {"threshold": "A+B",
             "allowed_win_rate": "bad", "blocked_win_rate": 0.5,
             "allowed_avg_r_multiple": None, "blocked_avg_r_multiple": None},
        ],
    }
    # Must not raise. Either insufficient_data (matched_trades coerces
    # to 0) or another valid status.
    out = evaluate_guardrails(report)
    assert out["status"] in {
        GUARDRAIL_STATUS_OK,
        GUARDRAIL_STATUS_WARNING,
        GUARDRAIL_STATUS_CRITICAL,
        GUARDRAIL_STATUS_INSUFFICIENT,
    }
