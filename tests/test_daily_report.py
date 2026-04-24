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
    DailyReportResult,
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
