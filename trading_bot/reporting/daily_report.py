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

# Phase 3.3 — guardrail thresholds. All three are documented in
# docs/CORE_CONTROL.md so operators can reason about them without
# reading source.
GUARDRAIL_MIN_MATCHED_TRADES = 20  # below this → insufficient_data

GUARDRAIL_STATUS_OK = "ok"
GUARDRAIL_STATUS_WARNING = "warning"
GUARDRAIL_STATUS_CRITICAL = "critical"
GUARDRAIL_STATUS_INSUFFICIENT = "insufficient_data"


@dataclass
class DailyReportResult:
    """Return value of `generate_daily_report`."""

    date: str
    txt_path: Path
    json_path: Path
    success: bool
    error: Optional[str] = None
    guardrail_status: Optional[str] = None


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


# ---------------------------------------------------------------------------
# Phase 3.3 — alpha performance guardrails
# ---------------------------------------------------------------------------


def _coerce_int(value, default: int = 0) -> int:
    """Coerce an arbitrary value to int, returning `default` on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _coerce_float(value) -> Optional[float]:
    """Coerce an arbitrary value to float, returning None on failure."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def evaluate_guardrails(report: dict) -> dict:
    """
    Classify the day's alpha-filter performance as one of
    ``ok`` / ``warning`` / ``critical`` / ``insufficient_data``.

    Pure function: takes the dict produced by
    ``trading_bot.analysis.alpha_report.build_report`` and returns a
    decorator dict. Never mutates its input, never raises — a missing
    or malformed field degrades gracefully to insufficient_data.

    Logic (see docs/CORE_CONTROL.md):

    - insufficient_data if fewer than GUARDRAIL_MIN_MATCHED_TRADES (20)
      matched trades were observed.
    - critical if the A+B shadow-filter row shows
      `allowed_avg_r_multiple < blocked_avg_r_multiple`, i.e. the
      trades the filter would KEEP did WORSE than the ones it would
      REJECT.
    - critical if the same row shows
      `allowed_win_rate < blocked_win_rate`.
    - warning if `promotion_readiness.status == "weak"`.
    - warning if A/B tier outcome_count is below min_required_outcomes.
    - ok otherwise.
    """
    totals = (report or {}).get("totals") or {}
    readiness = (report or {}).get("promotion_readiness") or {}
    sim_rows = (report or {}).get("shadow_filter_simulation") or []

    matched_trades = _coerce_int(totals.get("matched_trades", 0))

    # --- insufficient_data short-circuit ---
    if matched_trades < GUARDRAIL_MIN_MATCHED_TRADES:
        return {
            "status": GUARDRAIL_STATUS_INSUFFICIENT,
            "reasons": [
                f"only {matched_trades} matched trades "
                f"(need >= {GUARDRAIL_MIN_MATCHED_TRADES})"
            ],
            "recommended_action": (
                "keep the paper alpha filter disabled and let more trades "
                "accumulate before judging performance"
            ),
        }

    # --- look up the A+B shadow-filter simulation row ---
    ab_row = next(
        (r for r in sim_rows if r.get("threshold") == "A+B"), None
    )

    critical_reasons: list[str] = []
    if ab_row:
        a_r = _coerce_float(ab_row.get("allowed_avg_r_multiple"))
        b_r = _coerce_float(ab_row.get("blocked_avg_r_multiple"))
        a_wr = _coerce_float(ab_row.get("allowed_win_rate"))
        b_wr = _coerce_float(ab_row.get("blocked_win_rate"))

        # Only compare when BOTH sides have realized outcomes.
        if a_r is not None and b_r is not None and a_r < b_r:
            critical_reasons.append(
                f"allowed avg R {a_r:.3f} < blocked avg R {b_r:.3f} "
                f"at the A+B threshold"
            )
        if a_wr is not None and b_wr is not None and a_wr < b_wr:
            critical_reasons.append(
                f"allowed win rate {a_wr:.2%} < blocked win rate "
                f"{b_wr:.2%} at the A+B threshold"
            )

    if critical_reasons:
        return {
            "status": GUARDRAIL_STATUS_CRITICAL,
            "reasons": critical_reasons,
            "recommended_action": (
                "disable TRADING_ALPHA_FILTER_ENABLED and re-run the "
                "analysis before re-enabling — the filter is rejecting "
                "better trades than it keeps"
            ),
        }

    # --- warning conditions ---
    warning_reasons: list[str] = []
    if str(readiness.get("status", "")).lower() == "weak":
        warning_reasons.append(
            "promotion_readiness.status is 'weak' — A/B tiers "
            "fail to outperform C/D/F on the current sample"
        )

    ab_block = readiness.get("ab") or {}
    ab_outcome = _coerce_int(ab_block.get("outcome_count", 0))
    min_req = _coerce_int(readiness.get("min_required_outcomes", 0))
    if min_req > 0 and ab_outcome < min_req:
        warning_reasons.append(
            f"A/B outcome_count={ab_outcome} is below "
            f"min_required_outcomes={min_req}"
        )

    if warning_reasons:
        return {
            "status": GUARDRAIL_STATUS_WARNING,
            "reasons": warning_reasons,
            "recommended_action": (
                "review the filter before promotion — keep "
                "TRADING_ALPHA_FILTER_ENABLED=false in any environment "
                "that matters until warnings clear"
            ),
        }

    # --- ok ---
    return {
        "status": GUARDRAIL_STATUS_OK,
        "reasons": [
            "allowed side matches or outperforms blocked on both "
            "win rate and avg R at the A+B threshold"
        ],
        "recommended_action": (
            "no action — continue to monitor; promotion to live still "
            "requires an explicit ticket per docs/CORE_CONTROL.md"
        ),
    }


def _format_guardrails_section(guardrails: dict) -> str:
    """Render the guardrails dict as a self-contained text block."""
    lines = ["", "Alpha guardrails:"]
    lines.append(f"  status             = {guardrails.get('status', '')}")
    lines.append(
        f"  recommended_action = {guardrails.get('recommended_action', '')}"
    )
    reasons = guardrails.get("reasons") or []
    lines.append("  reasons:")
    if not reasons:
        lines.append("    - (none)")
    else:
        for reason in reasons:
            lines.append(f"    - {reason}")
    return "\n".join(lines)


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

        # Phase 3.3 — classify the day's alpha-filter performance.
        guardrails = evaluate_guardrails(report)
        report_with_guardrails = dict(report)
        report_with_guardrails["guardrails"] = guardrails

        txt_body = format_text(report)
        txt_body_with_guardrails = (
            txt_body.rstrip("\n")
            + "\n"
            + _format_guardrails_section(guardrails)
        )
        txt_payload = _wrap_text_with_daily_header(
            report_date, txt_body_with_guardrails
        )
        json_payload = _compose_json_payload(
            report_with_guardrails, report_date
        )
        json_text = json.dumps(
            json_payload, indent=2, sort_keys=False, default=str
        )

        # Ensure the output dir exists before writing either file.
        Path(reports_dir).mkdir(parents=True, exist_ok=True)
        txt_out.write_text(txt_payload + "\n")
        json_out.write_text(json_text + "\n")

        result.success = True
        result.guardrail_status = guardrails.get("status")
        log.info(
            "daily_report.generated",
            date=report_date,
            txt=str(txt_out),
            json=str(json_out),
            alpha_rows=report.get("totals", {}).get("alpha_rows", 0),
            matched_trades=report.get("totals", {}).get("matched_trades", 0),
            guardrail_status=result.guardrail_status,
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
        print(f"  guardrail status: {result.guardrail_status}")
        return 0

    print(
        f"error: daily report for {result.date} failed: {result.error}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
