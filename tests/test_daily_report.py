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
from typing import Iterable, Optional

import pytest

from trading_bot.reporting import daily_report as daily_report_mod
from trading_bot.reporting.daily_report import (
    DAILY_HEADER,
    GUARDRAIL_MIN_MATCHED_TRADES,
    GUARDRAIL_STATUS_CRITICAL,
    GUARDRAIL_STATUS_INSUFFICIENT,
    GUARDRAIL_STATUS_OK,
    GUARDRAIL_STATUS_WARNING,
    GUARDRAIL_WEBHOOK_ENV_VAR,
    DailyReportResult,
    build_guardrail_alert_payload,
    evaluate_guardrails,
    generate_daily_report,
    main,
    send_guardrail_alert,
    should_send_guardrail_alert,
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


# ===========================================================================
# Phase 3.4 — guardrail webhook alerts
# ===========================================================================


@pytest.fixture(autouse=True)
def _clear_webhook_env(monkeypatch):
    """Every Phase 3.4 test starts with a clean env."""
    monkeypatch.delenv(GUARDRAIL_WEBHOOK_ENV_VAR, raising=False)


@pytest.fixture
def guarded_report_factory(populated_data_dir):
    """
    Build a callable that forces a specific guardrail status on the
    next `generate_daily_report` invocation by monkey-patching the
    evaluator.
    """
    def _make(monkeypatch, status, reasons=None, action="do x"):
        def fake_evaluate(report):
            return {
                "status": status,
                "reasons": list(reasons or [f"reason for {status}"]),
                "recommended_action": action,
            }
        monkeypatch.setattr(daily_report_mod, "evaluate_guardrails", fake_evaluate)
        return populated_data_dir

    return _make


# ---------------------------------------------------------------------------
# should_send_guardrail_alert — pure predicate
# ---------------------------------------------------------------------------


class TestShouldSendGuardrailAlert:
    def test_warning_sends(self):
        assert should_send_guardrail_alert(GUARDRAIL_STATUS_WARNING) is True

    def test_critical_sends(self):
        assert should_send_guardrail_alert(GUARDRAIL_STATUS_CRITICAL) is True

    def test_ok_does_not_send(self):
        assert should_send_guardrail_alert(GUARDRAIL_STATUS_OK) is False

    def test_insufficient_data_does_not_send(self):
        assert should_send_guardrail_alert(GUARDRAIL_STATUS_INSUFFICIENT) is False

    @pytest.mark.parametrize("value", [None, "", "   ", "unknown", "foo"])
    def test_unknown_or_missing_does_not_send(self, value):
        assert should_send_guardrail_alert(value) is False

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("WARNING", True), ("Warning", True), ("  warning  ", True),
            ("CRITICAL", True), ("Critical", True),
            ("OK", False), ("Ok", False),
        ],
    )
    def test_case_and_whitespace_tolerant(self, value, expected):
        assert should_send_guardrail_alert(value) is expected


# ---------------------------------------------------------------------------
# build_guardrail_alert_payload — required fields
# ---------------------------------------------------------------------------


class TestBuildGuardrailAlertPayload:
    def test_payload_has_all_required_fields(self, tmp_path: Path):
        payload = build_guardrail_alert_payload(
            date="2026-04-24",
            status="critical",
            recommended_action="disable TRADING_ALPHA_FILTER_ENABLED",
            reasons=["allowed avg R 0.1 < blocked 1.0", "win rate flipped"],
            txt_path=tmp_path / "alpha_report_2026-04-24.txt",
            json_path=tmp_path / "alpha_report_2026-04-24.json",
        )
        # Structured fields
        assert payload["report_date"] == "2026-04-24"
        assert payload["status"] == "critical"
        assert payload["recommended_action"] == "disable TRADING_ALPHA_FILTER_ENABLED"
        assert payload["reasons"] == [
            "allowed avg R 0.1 < blocked 1.0",
            "win rate flipped",
        ]
        assert "alpha_report_2026-04-24.txt" in payload["text_report_path"]
        assert "alpha_report_2026-04-24.json" in payload["json_report_path"]
        # Slack + Discord compatible message bodies
        assert "text" in payload and "content" in payload
        assert payload["text"] == payload["content"]
        assert "2026-04-24" in payload["text"]
        assert "CRITICAL" in payload["text"]
        assert "allowed avg R" in payload["text"]

    def test_payload_is_json_serializable(self, tmp_path: Path):
        payload = build_guardrail_alert_payload(
            date="2026-04-24",
            status="warning",
            recommended_action="review",
            reasons=[],
            txt_path=tmp_path / "x.txt",
            json_path=tmp_path / "x.json",
        )
        # Must round-trip through json without custom encoders
        dumped = json.dumps(payload)
        assert "warning" in dumped
        parsed = json.loads(dumped)
        assert parsed["status"] == "warning"

    def test_payload_handles_empty_reasons(self, tmp_path: Path):
        payload = build_guardrail_alert_payload(
            date="2026-04-24",
            status="warning",
            recommended_action="review",
            reasons=[],
            txt_path=tmp_path / "a.txt",
            json_path=tmp_path / "a.json",
        )
        assert payload["reasons"] == []
        assert "(none)" in payload["text"]

    def test_payload_warning_emoji_differs_from_critical(self, tmp_path: Path):
        """Just make sure the two look different so they're visually distinguishable."""
        p_warn = build_guardrail_alert_payload(
            date="2026-04-24", status="warning",
            recommended_action="x", reasons=["y"],
            txt_path=tmp_path / "a.txt", json_path=tmp_path / "a.json",
        )
        p_crit = build_guardrail_alert_payload(
            date="2026-04-24", status="critical",
            recommended_action="x", reasons=["y"],
            txt_path=tmp_path / "a.txt", json_path=tmp_path / "a.json",
        )
        # They share the message template but the leading emoji differs.
        assert p_warn["text"] != p_crit["text"]


# ---------------------------------------------------------------------------
# send_guardrail_alert — never raises, reports error cleanly
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class TestSendGuardrailAlert:
    def test_missing_url_reports_error_without_posting(self, monkeypatch):
        called = {"n": 0}

        def fake_post(*args, **kwargs):  # pragma: no cover
            called["n"] += 1
            return _FakeResponse()

        monkeypatch.setattr(daily_report_mod.requests, "post", fake_post)
        sent, err = send_guardrail_alert("", {"status": "warning"})
        assert sent is False
        assert err == "missing_webhook_url"
        assert called["n"] == 0

    def test_success_2xx(self, monkeypatch):
        monkeypatch.setattr(
            daily_report_mod.requests, "post",
            lambda *a, **kw: _FakeResponse(200, "ok"),
        )
        sent, err = send_guardrail_alert("https://example/webhook", {})
        assert sent is True
        assert err is None

    def test_non_2xx_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            daily_report_mod.requests, "post",
            lambda *a, **kw: _FakeResponse(500, "internal"),
        )
        sent, err = send_guardrail_alert("https://example/webhook", {})
        assert sent is False
        assert err is not None
        assert "500" in err

    def test_request_exception_caught(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("dns fail")
        monkeypatch.setattr(daily_report_mod.requests, "post", boom)
        sent, err = send_guardrail_alert("https://example/webhook", {})
        assert sent is False
        assert "ConnectionError" in err
        assert "dns fail" in err

    def test_timeout_propagated_as_error_not_raise(self, monkeypatch):
        import socket

        def timeout(*args, **kwargs):
            raise socket.timeout("took too long")
        monkeypatch.setattr(daily_report_mod.requests, "post", timeout)
        sent, err = send_guardrail_alert("https://example/webhook", {})
        assert sent is False
        assert err is not None

    def test_passes_json_body_not_data(self, monkeypatch):
        captured = {}

        def capture(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["timeout"] = kwargs.get("timeout")
            return _FakeResponse(204, "")

        monkeypatch.setattr(daily_report_mod.requests, "post", capture)
        payload = {"text": "hi", "status": "warning"}
        send_guardrail_alert("https://example/webhook", payload, timeout=3.0)
        assert captured["url"] == "https://example/webhook"
        assert captured["json"] == payload
        assert captured["timeout"] == 3.0


# ---------------------------------------------------------------------------
# generate_daily_report — end-to-end webhook behavior
# ---------------------------------------------------------------------------


def _install_fake_post(monkeypatch, *, response: _FakeResponse = None,
                      recorder: Optional[list] = None, raises: Exception = None):
    """Helper: stub requests.post and optionally record calls."""
    rec = recorder if recorder is not None else []
    resp = response or _FakeResponse(204, "")

    def fake_post(url, *args, **kwargs):
        if raises is not None:
            raise raises
        rec.append({
            "url": url,
            "json": kwargs.get("json"),
            "timeout": kwargs.get("timeout"),
        })
        return resp

    monkeypatch.setattr(daily_report_mod.requests, "post", fake_post)
    return rec


class TestGenerateDailyReportAlertIntegration:
    # ---- No env var → no alert ----

    def test_no_env_var_no_network_call(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        calls = _install_fake_post(monkeypatch)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.success is True
        assert result.alert_sent is False
        assert result.alert_error is None  # didn't attempt
        assert calls == []

    # ---- Env var set but status does NOT trigger alert ----

    def test_ok_status_does_not_alert(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(monkeypatch, GUARDRAIL_STATUS_OK)
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://example/webhook")
        calls = _install_fake_post(monkeypatch)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.alert_sent is False
        assert calls == [], "no POST should happen for ok"

    def test_insufficient_data_does_not_alert(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_INSUFFICIENT
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://example/webhook")
        calls = _install_fake_post(monkeypatch)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.alert_sent is False
        assert calls == []

    # ---- Env var set AND status triggers alert ----

    def test_warning_sends_alert(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_WARNING,
            reasons=["sample size small"],
            action="keep filter disabled",
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://example/webhook")
        calls = _install_fake_post(monkeypatch)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.alert_sent is True
        assert result.alert_error is None
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://example/webhook"
        # Payload sanity
        body = call["json"]
        assert body["status"] == GUARDRAIL_STATUS_WARNING
        assert body["report_date"] == report_date
        assert body["recommended_action"] == "keep filter disabled"
        assert body["reasons"] == ["sample size small"]
        assert "text_report_path" in body and "json_report_path" in body

    def test_critical_sends_alert(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://example/webhook")
        calls = _install_fake_post(monkeypatch)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.alert_sent is True
        assert len(calls) == 1
        assert calls[0]["json"]["status"] == GUARDRAIL_STATUS_CRITICAL

    # ---- Failure paths must not fail the report ----

    def test_webhook_500_does_not_fail_report(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://example/webhook")
        _install_fake_post(monkeypatch, response=_FakeResponse(500, "oops"))

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        # Report itself still succeeded.
        assert result.success is True
        # But alert was NOT sent and the error is recorded.
        assert result.alert_sent is False
        assert result.alert_error is not None
        assert "500" in result.alert_error
        # Files still exist.
        assert result.txt_path.exists()
        assert result.json_path.exists()

    def test_webhook_connection_error_does_not_fail_report(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://example/webhook")
        _install_fake_post(
            monkeypatch, raises=ConnectionError("network down"),
        )

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.success is True
        assert result.alert_sent is False
        assert result.alert_error is not None
        assert "ConnectionError" in result.alert_error

    # ---- Explicit argument wins over env var ----

    def test_explicit_webhook_url_wins_over_env(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://env/webhook")
        calls = _install_fake_post(monkeypatch)

        generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
            webhook_url="https://explicit/webhook",
        )
        assert len(calls) == 1
        assert calls[0]["url"] == "https://explicit/webhook"

    def test_empty_explicit_webhook_does_not_alert(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        """Passing empty string explicitly must NOT fall back to env."""
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        monkeypatch.setenv(GUARDRAIL_WEBHOOK_ENV_VAR, "https://env/webhook")
        calls = _install_fake_post(monkeypatch)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
            webhook_url="",
        )
        assert calls == []
        assert result.alert_sent is False

    # ---- CLI still works ----

    def test_cli_still_works_when_env_unset(
        self, populated_data_dir, tmp_path: Path, capsys
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

    def test_cli_subprocess_with_env_var_and_warning(
        self, guarded_report_factory, tmp_path: Path, monkeypatch
    ):
        """CLI still exits 0 even when the webhook fails — by design."""
        data_dir, report_date = guarded_report_factory(
            monkeypatch, GUARDRAIL_STATUS_CRITICAL
        )
        # A URL that cannot resolve → subprocess will try to POST,
        # requests will raise, daily_report will swallow, exit 0.
        # We still want the CLI path to be exercised in a subprocess.
        env = os.environ.copy()
        env[GUARDRAIL_WEBHOOK_ENV_VAR] = "http://127.0.0.1:1/no-such-endpoint"
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
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert "guardrail status" in result.stdout.lower()


# Need this import for the subprocess CLI test above.
import os  # noqa: E402


# ===========================================================================
# Phase 3.5 — scorer fingerprint + drift guard
# ===========================================================================


from trading_bot.core.alpha import get_alpha_scorer_fingerprint  # noqa: E402
from trading_bot.reporting.daily_report import (  # noqa: E402
    _collect_alpha_fingerprints,
)


class TestJsonReportIncludesScorerFingerprint:
    def test_top_level_field_present(
        self, populated_data_dir: tuple[Path, str], tmp_path: Path
    ):
        data_dir, report_date = populated_data_dir
        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        payload = json.loads(result.json_path.read_text())
        assert "scorer_fingerprint" in payload
        assert payload["scorer_fingerprint"] == get_alpha_scorer_fingerprint()
        assert len(payload["scorer_fingerprint"]) == 64

    def test_also_reports_fingerprints_seen_in_source_data(
        self, populated_data_dir: tuple[Path, str], tmp_path: Path
    ):
        data_dir, report_date = populated_data_dir
        # Overwrite the existing alpha file with a fingerprinted version
        fp = "f" * 64
        (data_dir / f"alpha_scores_{report_date}.csv").write_text(
            ",".join([
                "timestamp", "symbol", "score", "tier", "action",
                "confidence", "regime", "gap_pct", "relative_volume",
                "volatility", "reasons", "scorer_fingerprint",
            ]) + "\n"
            + f"2026-04-24 09:45:00,AAA,0.92,A,buy,0.85,"
              f"trending_bullish,12.0,15.0,3.5,r,{fp}\n"
        )
        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        payload = json.loads(result.json_path.read_text())
        assert payload["scorer_fingerprints_in_data"] == [fp]


class TestTextReportIncludesFingerprint:
    def test_fingerprint_line_in_text_report(
        self, populated_data_dir: tuple[Path, str], tmp_path: Path
    ):
        data_dir, report_date = populated_data_dir
        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        txt = result.txt_path.read_text()
        assert "Scorer fingerprint:" in txt
        assert get_alpha_scorer_fingerprint() in txt


# ---------------------------------------------------------------------------
# _collect_alpha_fingerprints
# ---------------------------------------------------------------------------


class TestCollectAlphaFingerprints:
    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        assert _collect_alpha_fingerprints(tmp_path / "absent.csv") == []

    def test_file_without_column_returns_empty_list(self, tmp_path: Path):
        p = tmp_path / "alpha.csv"
        p.write_text(
            "timestamp,symbol,score,tier,action,confidence,regime,"
            "gap_pct,relative_volume,volatility,reasons\n"
            "2026-04-24 09:45:00,AAA,0.9,A,buy,0.8,bull,10,10,3,r\n"
        )
        assert _collect_alpha_fingerprints(p) == []

    def test_single_fingerprint(self, tmp_path: Path):
        fp = "a" * 64
        p = tmp_path / "alpha.csv"
        p.write_text(
            "timestamp,symbol,score,tier,action,confidence,regime,"
            "gap_pct,relative_volume,volatility,reasons,scorer_fingerprint\n"
            f"2026-04-24 09:45:00,AAA,0.9,A,buy,0.8,bull,10,10,3,r,{fp}\n"
            f"2026-04-24 10:00:00,BBB,0.7,B,buy,0.7,bull,8,8,2,r,{fp}\n"
        )
        assert _collect_alpha_fingerprints(p) == [fp]

    def test_multiple_fingerprints_sorted_unique(self, tmp_path: Path):
        fp_a = "a" * 64
        fp_b = "b" * 64
        p = tmp_path / "alpha.csv"
        p.write_text(
            "timestamp,symbol,score,tier,action,confidence,regime,"
            "gap_pct,relative_volume,volatility,reasons,scorer_fingerprint\n"
            f"2026-04-24 09:45:00,AAA,0.9,A,buy,0.8,bull,10,10,3,r,{fp_b}\n"
            f"2026-04-24 10:00:00,BBB,0.7,B,buy,0.7,bull,8,8,2,r,{fp_a}\n"
            f"2026-04-24 10:05:00,CCC,0.9,A,buy,0.8,bull,10,10,3,r,{fp_a}\n"
        )
        got = _collect_alpha_fingerprints(p)
        assert got == [fp_a, fp_b]  # sorted

    def test_accepts_glob_pattern(self, tmp_path: Path):
        fp_a = "a" * 64
        fp_b = "b" * 64
        for date_str, fp in [("2026-04-24", fp_a), ("2026-04-25", fp_b)]:
            p = tmp_path / f"alpha_scores_{date_str}.csv"
            p.write_text(
                "timestamp,symbol,score,tier,action,confidence,regime,"
                "gap_pct,relative_volume,volatility,reasons,scorer_fingerprint\n"
                f"{date_str} 09:45:00,X,0.9,A,buy,0.8,bull,10,10,3,r,{fp}\n"
            )
        got = _collect_alpha_fingerprints(str(tmp_path / "alpha_scores_*.csv"))
        assert got == sorted([fp_a, fp_b])


# ---------------------------------------------------------------------------
# Guardrail drift warnings
# ---------------------------------------------------------------------------


class TestGuardrailFingerprintDrift:
    def _base_report(self) -> dict:
        """Enough matched trades + healthy metrics so status defaults to ok."""
        return {
            "totals": {"matched_trades": 50},
            "promotion_readiness": {
                "status": "ready_for_shadow_filter_test",
                "min_required_outcomes": 25,
                "ab": {"outcome_count": 40},
                "cdf": {"outcome_count": 10},
            },
            "shadow_filter_simulation": [
                {"threshold": "A+B",
                 "allowed_win_rate": 0.7, "blocked_win_rate": 0.3,
                 "allowed_avg_r_multiple": 1.0, "blocked_avg_r_multiple": 0.0},
            ],
        }

    def test_single_fingerprint_no_warning(self):
        report = self._base_report()
        report["scorer_fingerprints"] = ["a" * 64]
        out = evaluate_guardrails(report)
        assert out["status"] == GUARDRAIL_STATUS_OK
        assert not any("fingerprint" in r for r in out["reasons"])

    def test_multiple_fingerprints_emit_warning(self):
        report = self._base_report()
        report["scorer_fingerprints"] = ["a" * 64, "b" * 64]
        out = evaluate_guardrails(report)
        assert out["status"] == GUARDRAIL_STATUS_WARNING
        assert any(
            "multiple alpha scorer fingerprints" in r.lower()
            for r in out["reasons"]
        )

    def test_missing_fingerprints_emit_warning(self):
        report = self._base_report()
        report["scorer_fingerprints"] = []    # explicitly empty
        out = evaluate_guardrails(report)
        assert out["status"] == GUARDRAIL_STATUS_WARNING
        assert any(
            "fingerprint unavailable" in r.lower() for r in out["reasons"]
        )

    def test_fingerprint_drift_never_escalates_to_critical(self):
        """Drift is WARNING only — even when the A+B row shows bad perf
        the fingerprint warning must be attached, not replaced."""
        report = self._base_report()
        # Tip the A+B row so critical would otherwise fire
        report["shadow_filter_simulation"] = [
            {"threshold": "A+B",
             "allowed_win_rate": 0.2, "blocked_win_rate": 0.8,
             "allowed_avg_r_multiple": 0.1, "blocked_avg_r_multiple": 1.5},
        ]
        report["scorer_fingerprints"] = ["a" * 64, "b" * 64]
        out = evaluate_guardrails(report)
        # Critical still wins because the data IS actually bad —
        # drift alone never forces critical, but it cannot mask it
        # either. The critical reasons must still be present.
        assert out["status"] == GUARDRAIL_STATUS_CRITICAL
        # (fingerprint warning is not added when status is critical
        # because critical short-circuits before the warning block.)

    def test_drift_warning_only_fires_when_scorer_fingerprints_key_present(self):
        """A caller that doesn't supply the field sees no fingerprint logic."""
        report = self._base_report()
        # Key omitted entirely
        out = evaluate_guardrails(report)
        assert out["status"] == GUARDRAIL_STATUS_OK
        assert not any("fingerprint" in r for r in out["reasons"])

    def test_insufficient_data_short_circuits_before_fingerprint_check(self):
        """Insufficient_data keeps its precedence — don't stack fingerprint
        noise on top of an already-conclusive insufficient status."""
        report = self._base_report()
        report["totals"]["matched_trades"] = 1
        report["scorer_fingerprints"] = []
        out = evaluate_guardrails(report)
        assert out["status"] == GUARDRAIL_STATUS_INSUFFICIENT
        assert not any("fingerprint" in r for r in out["reasons"])


# ---------------------------------------------------------------------------
# End-to-end drift detection in generate_daily_report
# ---------------------------------------------------------------------------


class TestGenerateDailyReportDriftDetection:
    """End-to-end: a rotated daily run with two distinct fingerprints
    produces a warning guardrail AND lists the distinct fingerprints
    in the JSON."""

    @staticmethod
    def _write_fixture(
        data_dir: Path,
        report_date: str,
        fingerprints: list[str],
        trades_per_fp: int = 15,
    ) -> None:
        """Write alpha + decision + journal with 2 * trades_per_fp entries."""
        alpha_header = ",".join([
            "timestamp", "symbol", "score", "tier", "action",
            "confidence", "regime", "gap_pct", "relative_volume",
            "volatility", "reasons", "scorer_fingerprint",
        ])
        dec_header = ",".join([
            "timestamp", "symbol", "price", "gap_pct", "relative_volume",
            "volatility", "regime", "action", "confidence", "reason",
        ])
        j_header = ",".join([
            "date", "symbol", "side", "signal_type", "entry_price",
            "exit_price", "shares", "pnl", "rr_ratio", "hold_time_minutes",
            "entry_time", "exit_time", "exit_reason", "notes",
        ])
        alpha_rows = [alpha_header]
        dec_rows = [dec_header]
        j_rows = [j_header]

        i = 0
        for fp in fingerprints:
            for k in range(trades_per_fp):
                sym = f"S{i:03d}"
                hh, mm = 9, 30 + i
                # Keep times inside the hour
                while mm >= 60:
                    hh += 1
                    mm -= 60
                ts = f"{report_date} {hh:02d}:{mm:02d}:00"
                entry_time = f"{hh:02d}:{mm:02d}:00"
                exit_time = f"{(hh + 1) % 24:02d}:{mm:02d}:00"
                # Alternate winners/losers so stats don't skew
                pnl = 100.0 if k % 2 == 0 else -50.0
                rr = 1.0 if k % 2 == 0 else -0.5
                alpha_rows.append(
                    f"{ts},{sym},0.9,A,buy,0.85,"
                    f"trending_bullish,12.0,15.0,3.5,r,{fp}"
                )
                dec_rows.append(
                    f"{ts},{sym},10.0,12.0,15.0,3.5,"
                    f"trending_bullish,buy,0.85,executed"
                )
                j_rows.append(
                    f"{report_date},{sym},buy,vwap_pullback,"
                    f"10.0,{10.0 + pnl / 100:.2f},100,{pnl},{rr},30,"
                    f"{entry_time},{exit_time},target,"
                )
                i += 1

        (data_dir / f"alpha_scores_{report_date}.csv").write_text(
            "\n".join(alpha_rows) + "\n"
        )
        (data_dir / f"decision_log_{report_date}.csv").write_text(
            "\n".join(dec_rows) + "\n"
        )
        (data_dir / "journal.csv").write_text("\n".join(j_rows) + "\n")

    def test_mixed_fingerprints_produce_warning_guardrail(
        self, tmp_path: Path
    ):
        fp_a, fp_b = "a" * 64, "b" * 64
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        report_date = "2026-04-24"
        self._write_fixture(data_dir, report_date, [fp_a, fp_b], trades_per_fp=15)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )

        payload = json.loads(result.json_path.read_text())
        gr = payload["guardrails"]
        # Status is warning (drift triggers warning when otherwise ok)
        assert gr["status"] == GUARDRAIL_STATUS_WARNING
        assert any(
            "multiple alpha scorer fingerprints" in r.lower()
            for r in gr["reasons"]
        )
        # The JSON also lists the distinct data-side fingerprints
        assert sorted(payload["scorer_fingerprints_in_data"]) == sorted([fp_a, fp_b])

    def test_single_fingerprint_does_not_trigger_drift_warning(
        self, tmp_path: Path
    ):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        report_date = "2026-04-24"
        self._write_fixture(data_dir, report_date, ["a" * 64], trades_per_fp=30)

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        payload = json.loads(result.json_path.read_text())
        gr = payload["guardrails"]
        assert not any(
            "multiple alpha scorer fingerprints" in r.lower()
            for r in gr["reasons"]
        )
        assert payload["scorer_fingerprints_in_data"] == ["a" * 64]
