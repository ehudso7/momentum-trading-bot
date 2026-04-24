"""
Phase 3.2 — automated daily alpha validation report.

After each trading session ends, the bot writes a text and a JSON
report that combine every piece of Core-conversion analysis for the
just-finished day:

- sources / totals (how many rows were captured today)
- tier, reason, and regime stats
- score-decile calibration         (Phase 2.6)
- promotion readiness              (Phase 2.6)
- shadow-mode tier-filter sim      (Phase 2.9)

The module is **post-run reporting only**. It has no side effects on
the trading loop: the hot path never imports it, and the integration
point in ``main.py`` wraps the call in ``try/except`` so no failure
here can block shutdown.

All real analysis logic lives in ``trading_bot.analysis.alpha_report``
— this module is a thin "which files, for which day, where to save"
wrapper around it.

Usage:
    python -m trading_bot.reporting.daily_report
    python -m trading_bot.reporting.daily_report --date 2026-04-24
    python -m trading_bot.reporting.daily_report \\
        --data-dir data --reports-dir reports --min-outcomes 100
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date as _date_type
from pathlib import Path
from typing import Optional, Union

import structlog

from trading_bot.analysis.alpha_report import (
    DEFAULT_MIN_REQUIRED_OUTCOMES,
    build_report,
    format_json,
    format_text,
)

log = structlog.get_logger(__name__)


DAILY_HEADER = "DAILY ALPHA VALIDATION REPORT"
DEFAULT_DATA_DIR = "data"
DEFAULT_REPORTS_DIR = "reports"


@dataclass
class DailyReportResult:
    """Return value of `generate_daily_report`."""

    date: str
    txt_path: Path
    json_path: Path
    success: bool
    error: Optional[str] = None


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD. Factored out for test patching."""
    return _date_type.today().strftime("%Y-%m-%d")


def _resolve_date(date: Optional[str]) -> str:
    """Default to today's date when none was provided."""
    if date is None or str(date).strip() == "":
        return _today_str()
    return str(date).strip()


def _build_paths(
    report_date: str,
    data_dir: Union[str, Path],
    reports_dir: Union[str, Path],
) -> tuple[Path, Path, Path, Path, Path]:
    """
    Compute all input + output paths for a given date.

    Inputs match the Phase 2.7 rotation scheme:
      data/alpha_scores_<DATE>.csv
      data/decision_log_<DATE>.csv
      data/journal.csv            (journal is NOT rotated)
    """
    data = Path(data_dir)
    reports = Path(reports_dir)
    alpha_in = data / f"alpha_scores_{report_date}.csv"
    decision_in = data / f"decision_log_{report_date}.csv"
    journal_in = data / "journal.csv"
    txt_out = reports / f"alpha_report_{report_date}.txt"
    json_out = reports / f"alpha_report_{report_date}.json"
    return alpha_in, decision_in, journal_in, txt_out, json_out


def _wrap_text_with_daily_header(report_date: str, body: str) -> str:
    """Prepend the daily header so the plain-text report is self-identifying."""
    bar = "=" * 78
    return "\n".join([bar, f"{DAILY_HEADER} — {report_date}", bar, body])


def _compose_json_payload(report: dict, report_date: str) -> dict:
    """Return a copy of the analysis report decorated with daily metadata."""
    payload = {
        "report_type": "daily_alpha_validation",
        "report_date": report_date,
    }
    payload.update(report)
    return payload


def generate_daily_report(
    date: Optional[str] = None,
    data_dir: Union[str, Path] = DEFAULT_DATA_DIR,
    reports_dir: Union[str, Path] = DEFAULT_REPORTS_DIR,
    min_required_outcomes: int = DEFAULT_MIN_REQUIRED_OUTCOMES,
) -> DailyReportResult:
    """
    Build and persist the daily alpha validation report for `date`.

    `date` defaults to today in the bot's local date semantics (same
    as Phase 2.7 rotation). The function is deliberately best-effort:
    it never raises — any failure is logged and reflected in the
    returned `DailyReportResult.success` / `.error` fields so the
    caller (usually `TradingBot._run_live_loop` at shutdown) can
    tolerate it without blocking.
    """
    report_date = _resolve_date(date)
    alpha_in, decision_in, journal_in, txt_out, json_out = _build_paths(
        report_date, data_dir, reports_dir
    )

    # `success=False` by default — flipped True only after both files written.
    result = DailyReportResult(
        date=report_date,
        txt_path=txt_out,
        json_path=json_out,
        success=False,
    )

    try:
        report = build_report(
            alpha_path=alpha_in,
            decision_path=decision_in,
            journal_path=journal_in,
            min_required_outcomes=min_required_outcomes,
        )

        txt_body = format_text(report)
        txt_payload = _wrap_text_with_daily_header(report_date, txt_body)
        json_payload = _compose_json_payload(report, report_date)
        json_text = json.dumps(json_payload, indent=2, sort_keys=False, default=str)

        # Ensure the output dir exists before writing either file.
        Path(reports_dir).mkdir(parents=True, exist_ok=True)
        txt_out.write_text(txt_payload + "\n")
        json_out.write_text(json_text + "\n")

        result.success = True
        log.info(
            "daily_report.generated",
            date=report_date,
            txt=str(txt_out),
            json=str(json_out),
            alpha_rows=report.get("totals", {}).get("alpha_rows", 0),
            matched_trades=report.get("totals", {}).get("matched_trades", 0),
        )
    except Exception as exc:
        # Swallow — this report is best-effort and must never block shutdown.
        result.error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "daily_report.failed",
            date=report_date,
            error=result.error,
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.reporting.daily_report",
        description=(
            "Generate a daily alpha validation report (Phase 3.2). "
            "Wraps trading_bot.analysis.alpha_report with sensible "
            "date-aware default paths and writes both text and JSON."
        ),
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Report date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"Directory holding Core CSV datasets (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--reports-dir",
        default=DEFAULT_REPORTS_DIR,
        help=f"Directory to write reports into (default: {DEFAULT_REPORTS_DIR})",
    )
    parser.add_argument(
        "--min-outcomes",
        dest="min_outcomes",
        type=int,
        default=DEFAULT_MIN_REQUIRED_OUTCOMES,
        help=(
            "Promotion-readiness sample threshold passed through to the "
            f"analysis layer (default: {DEFAULT_MIN_REQUIRED_OUTCOMES})"
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    result = generate_daily_report(
        date=args.date,
        data_dir=args.data_dir,
        reports_dir=args.reports_dir,
        min_required_outcomes=args.min_outcomes,
    )

    if result.success:
        print(f"Daily alpha validation report written for {result.date}:")
        print(f"  text: {result.txt_path}")
        print(f"  json: {result.json_path}")
        return 0

    print(
        f"error: daily report for {result.date} failed: {result.error}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
