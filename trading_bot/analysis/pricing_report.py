"""
Phase 5.0 — Pricing Intelligence Report.

Offline analytics that joins the Phase 4.6 per-key usage log and the
Phase 4.9 free→paid conversion log to answer:

  * How many total / free / premium users do we have?
  * What is the conversion rate?
  * How many conversions did we see per day?
  * Which price_ids are users converting to?
  * How long do users stay on free before converting?
    (average / median / p90 time-to-convert)
  * How intense is usage per tier, and who are the power users?

Inputs are both append-only JSONL files already produced by the
bot — no database, no external service. Outputs are two plain
files: a human-readable text report and a machine-readable JSON.

Privacy posture:
  * Neither input file contains raw API keys (both use
    ``SHA-256(api_key)[:32]``), so this module cannot leak a raw
    key even if it tried.
  * The report propagates ONLY the opaque hash — never a customer
    name, email, IP, or payment field.
  * A leak-guard test plants unique markers in both input files
    and asserts they do NOT appear in the report body.

CLI::

    python -m trading_bot.analysis.pricing_report
    python -m trading_bot.analysis.pricing_report --date 2026-04-24
    python -m trading_bot.analysis.pricing_report \\
        --usage data/api_usage.jsonl \\
        --conversions data/api_conversions.jsonl \\
        --reports-dir reports
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date as _date_type, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_USAGE_PATH = "data/api_usage.jsonl"
DEFAULT_CONVERSION_PATH = "data/api_conversions.jsonl"
DEFAULT_REPORTS_DIR = "reports"

REPORT_TYPE = "pricing_intelligence"

# Top-percentile brackets we emit. All values > 0, <= 1.
TOP_PERCENTILES: tuple[float, ...] = (0.05, 0.10)


# ---------------------------------------------------------------------------
# Loaders — never raise
# ---------------------------------------------------------------------------


def _read_jsonl(path: Union[str, Path]) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return []
    records: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except Exception:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def load_usage(path: Union[str, Path] = DEFAULT_USAGE_PATH) -> list[dict]:
    """Load the Phase 4.6 per-key usage JSONL file. Missing → []."""
    return _read_jsonl(path)


def load_conversions(
    path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
) -> list[dict]:
    """Load the Phase 4.9 conversion JSONL file. Missing → []."""
    return _read_jsonl(path)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    """
    Parse a timestamp that may or may not have a trailing ``Z``.
    Both the usage log and the conversion log emit
    ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` — we accept the canonical form
    plus plain ISO without the Z.
    """
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _today_utc_str() -> str:
    """UTC calendar date as YYYY-MM-DD. Factored out for test patching."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_bucket(ts: Optional[str]) -> Optional[str]:
    """Return the YYYY-MM-DD bucket from a full timestamp."""
    dt = _parse_iso_utc(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Stat helpers — pure, testable in isolation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    """
    Nearest-rank percentile for a sorted (or unsorted) list. ``percentile``
    is a fraction in [0, 1]. Empty list → None.
    """
    if not values:
        return None
    srt = sorted(values)
    n = len(srt)
    rank = max(1, math.ceil(percentile * n))
    rank = min(rank, n)
    return float(srt[rank - 1])


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.fmean(values))


def _round_or_none(value: Optional[float], ndigits: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), ndigits)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_user_counts(
    usage: list[dict],
    conversions: list[dict],
) -> dict[str, int]:
    """
    Derive user cohort counts from the usage and conversion logs.

    A user = one distinct ``key_hash`` in the usage log. We also
    report the count of users that appear in BOTH the free set and
    the premium set (typical "converted from free" cohort).
    """
    seen_free: set[str] = set()
    seen_premium: set[str] = set()
    all_hashes: set[str] = set()

    for row in usage:
        h = row.get("key_hash")
        if not h:
            continue
        hashed = str(h)
        all_hashes.add(hashed)
        tier = str(row.get("tier") or "").lower()
        if tier == "free":
            seen_free.add(hashed)
        elif tier == "premium":
            seen_premium.add(hashed)

    converted_hashes = {
        str(rec.get("api_key_hash"))
        for rec in conversions
        if rec.get("api_key_hash")
    }

    # Users that have appeared as BOTH free and premium in usage.
    converted_from_free = seen_free & seen_premium

    return {
        "total_unique": len(all_hashes),
        "free": len(seen_free),
        "premium": len(seen_premium),
        "converted_from_free_to_premium": len(converted_from_free),
        "conversion_events_recorded": len(converted_hashes),
    }


def compute_conversion_metrics(
    usage: list[dict],
    conversions: list[dict],
) -> dict[str, Any]:
    """Conversion rate, per-day counts, per-price_id counts."""
    free_users = {
        str(row.get("key_hash"))
        for row in usage
        if row.get("key_hash")
        and str(row.get("tier") or "").lower() == "free"
    }

    distinct_converted = {
        str(rec.get("api_key_hash"))
        for rec in conversions
        if rec.get("api_key_hash")
    }

    n_free = len(free_users)
    n_converted = len(distinct_converted)

    conversion_rate: Optional[float] = None
    if n_free > 0:
        conversion_rate = n_converted / n_free

    per_day_counter: Counter[str] = Counter()
    for rec in conversions:
        bucket = _date_bucket(rec.get("timestamp"))
        if bucket:
            per_day_counter[bucket] += 1

    per_day = [
        {"date": d, "count": per_day_counter[d]}
        for d in sorted(per_day_counter)
    ]

    by_price: Counter = Counter()
    for rec in conversions:
        price_id = rec.get("price_id")
        by_price[price_id if price_id is not None else "__null__"] += 1

    by_price_list = sorted(
        (
            {
                "price_id": (None if k == "__null__" else k),
                "count": v,
            }
            for k, v in by_price.items()
        ),
        key=lambda r: r["count"],
        reverse=True,
    )

    return {
        "conversions_total": n_converted,
        "free_users": n_free,
        "conversion_rate": _round_or_none(conversion_rate),
        "conversions_per_day": per_day,
        "conversions_by_price_id": by_price_list,
    }


def compute_time_to_convert(conversions: list[dict]) -> dict[str, Any]:
    """
    For each conversion with both ``timestamp`` and
    ``first_seen_timestamp`` parseable, compute the delta in
    seconds. Report sample size, average, median, and p90 — in
    both seconds AND days for human-friendly reading.
    """
    deltas_seconds: list[float] = []
    for rec in conversions:
        converted_dt = _parse_iso_utc(rec.get("timestamp"))
        first_seen_dt = _parse_iso_utc(rec.get("first_seen_timestamp"))
        if converted_dt is None or first_seen_dt is None:
            continue
        # Normalise both sides to UTC-aware for subtraction.
        if converted_dt.tzinfo is None:
            converted_dt = converted_dt.replace(tzinfo=timezone.utc)
        if first_seen_dt.tzinfo is None:
            first_seen_dt = first_seen_dt.replace(tzinfo=timezone.utc)
        delta = (converted_dt - first_seen_dt).total_seconds()
        # Negative deltas would imply first_seen_timestamp > converted —
        # that's a data oddity; skip rather than poison the stats.
        if delta < 0:
            continue
        deltas_seconds.append(delta)

    samples = len(deltas_seconds)
    avg = _mean(deltas_seconds)
    med = _median(deltas_seconds)
    p90 = _percentile(deltas_seconds, 0.90)

    def _to_days(s: Optional[float]) -> Optional[float]:
        if s is None:
            return None
        return round(s / 86400.0, 4)

    return {
        "samples": samples,
        "average_seconds": _round_or_none(avg, 2),
        "median_seconds": _round_or_none(med, 2),
        "p90_seconds": _round_or_none(p90, 2),
        "average_days": _to_days(avg),
        "median_days": _to_days(med),
        "p90_days": _to_days(p90),
    }


def _final_tier_for_hash(
    rows_by_hash: dict[str, list[dict]],
) -> dict[str, str]:
    """
    Decide each user's effective tier: 'premium' if any row records
    premium, else 'free' if any row records free, else 'unknown'.
    """
    out: dict[str, str] = {}
    for h, rows in rows_by_hash.items():
        has_premium = any(
            str(r.get("tier") or "").lower() == "premium" for r in rows
        )
        has_free = any(
            str(r.get("tier") or "").lower() == "free" for r in rows
        )
        if has_premium:
            out[h] = "premium"
        elif has_free:
            out[h] = "free"
        else:
            out[h] = "unknown"
    return out


def compute_usage_intensity(usage: list[dict]) -> dict[str, Any]:
    """
    Request volume per user, bucketed by tier, plus the top-5% /
    top-10% power users. Every hash emitted here is already the
    opaque SHA-256[:32] — no raw keys are ever surfaced.
    """
    rows_by_hash: dict[str, list[dict]] = defaultdict(list)
    for row in usage:
        h = row.get("key_hash")
        if not h:
            continue
        rows_by_hash[str(h)].append(row)

    tier_of = _final_tier_for_hash(rows_by_hash)
    requests_of = {h: len(rows) for h, rows in rows_by_hash.items()}

    free_users_req = [
        requests_of[h] for h in requests_of if tier_of[h] == "free"
    ]
    premium_users_req = [
        requests_of[h] for h in requests_of if tier_of[h] == "premium"
    ]

    free_total = sum(free_users_req)
    premium_total = sum(premium_users_req)

    free_avg = (
        free_total / len(free_users_req) if free_users_req else None
    )
    premium_avg = (
        premium_total / len(premium_users_req) if premium_users_req else None
    )

    # Top-percentile users by request count (across all tiers).
    sorted_hashes = sorted(
        requests_of.items(), key=lambda kv: kv[1], reverse=True,
    )
    total_users = len(sorted_hashes)

    top_brackets: dict[str, list[dict]] = {}
    for pct in TOP_PERCENTILES:
        if total_users == 0:
            top_brackets[f"top_{int(pct * 100)}_percent"] = []
            continue
        k = max(1, math.ceil(total_users * pct))
        bucket = []
        for h, req in sorted_hashes[:k]:
            bucket.append({
                "key_hash": h,
                "tier": tier_of.get(h, "unknown"),
                "requests": req,
            })
        top_brackets[f"top_{int(pct * 100)}_percent"] = bucket

    return {
        "free": {
            "total_requests": free_total,
            "user_count": len(free_users_req),
            "avg_requests_per_user": _round_or_none(free_avg, 3),
        },
        "premium": {
            "total_requests": premium_total,
            "user_count": len(premium_users_req),
            "avg_requests_per_user": _round_or_none(premium_avg, 3),
        },
        "top_percentile_users": top_brackets,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_pricing_report(
    usage_path: Union[str, Path] = DEFAULT_USAGE_PATH,
    conversion_path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
    *,
    report_date: Optional[str] = None,
) -> dict:
    """
    Assemble the full pricing-intelligence report dict. Safe on
    missing / corrupt input files (empty lists flow through all
    metric functions cleanly).
    """
    usage = load_usage(usage_path)
    conversions = load_conversions(conversion_path)

    sources = {
        "usage_log": {
            "path": str(usage_path),
            "exists": Path(usage_path).exists(),
            "rows": len(usage),
        },
        "conversion_log": {
            "path": str(conversion_path),
            "exists": Path(conversion_path).exists(),
            "rows": len(conversions),
        },
    }

    return {
        "report_type": REPORT_TYPE,
        "report_date": report_date or _today_utc_str(),
        "sources": sources,
        "user_counts": compute_user_counts(usage, conversions),
        "conversion_metrics": compute_conversion_metrics(usage, conversions),
        "time_to_convert": compute_time_to_convert(conversions),
        "usage_intensity": compute_usage_intensity(usage),
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt(value: Optional[float], spec: str = ".4f", na: str = "n/a") -> str:
    if value is None:
        return na
    try:
        if isinstance(value, float) and (value != value):  # NaN
            return na
    except Exception:
        return na
    try:
        return format(value, spec)
    except Exception:
        return str(value)


def format_text(report: dict) -> str:
    lines: list[str] = []
    bar = "=" * 78
    lines.append(bar)
    lines.append(f"PRICING INTELLIGENCE REPORT — {report.get('report_date','?')}")
    lines.append(bar)

    sources = report.get("sources", {})
    lines.append("Sources:")
    for name, meta in sources.items():
        lines.append(
            f"  {name:<16} exists={meta.get('exists')!s:<5} "
            f"rows={meta.get('rows', 0):<6}"
        )

    uc = report.get("user_counts", {})
    lines.append("")
    lines.append("User counts:")
    lines.append(f"  total_unique                    = {uc.get('total_unique', 0)}")
    lines.append(f"  free                            = {uc.get('free', 0)}")
    lines.append(f"  premium                         = {uc.get('premium', 0)}")
    lines.append(
        f"  converted_from_free_to_premium  = "
        f"{uc.get('converted_from_free_to_premium', 0)}"
    )
    lines.append(
        f"  conversion_events_recorded      = "
        f"{uc.get('conversion_events_recorded', 0)}"
    )

    cm = report.get("conversion_metrics", {})
    lines.append("")
    lines.append("Conversion metrics:")
    lines.append(f"  conversions_total   = {cm.get('conversions_total', 0)}")
    lines.append(f"  free_users          = {cm.get('free_users', 0)}")
    lines.append(
        f"  conversion_rate     = {_fmt(cm.get('conversion_rate'), '.2%')}"
    )

    per_day = cm.get("conversions_per_day") or []
    lines.append("  conversions_per_day:")
    if not per_day:
        lines.append("    (no conversions recorded)")
    else:
        for row in per_day:
            lines.append(f"    {row.get('date', '?')} : {row.get('count', 0)}")

    by_price = cm.get("conversions_by_price_id") or []
    lines.append("  conversions_by_price_id:")
    if not by_price:
        lines.append("    (none)")
    else:
        for row in by_price:
            pid = row.get("price_id") or "(null)"
            lines.append(f"    {pid:<36} : {row.get('count', 0)}")

    ttc = report.get("time_to_convert", {})
    lines.append("")
    lines.append("Time-to-convert:")
    lines.append(f"  samples             = {ttc.get('samples', 0)}")
    lines.append(
        f"  average             = {_fmt(ttc.get('average_days'), '.2f')} days "
        f"({_fmt(ttc.get('average_seconds'), '.0f')} s)"
    )
    lines.append(
        f"  median              = {_fmt(ttc.get('median_days'), '.2f')} days "
        f"({_fmt(ttc.get('median_seconds'), '.0f')} s)"
    )
    lines.append(
        f"  p90                 = {_fmt(ttc.get('p90_days'), '.2f')} days "
        f"({_fmt(ttc.get('p90_seconds'), '.0f')} s)"
    )

    ui = report.get("usage_intensity", {})
    free_ui = ui.get("free", {})
    premium_ui = ui.get("premium", {})
    lines.append("")
    lines.append("Usage intensity:")
    lines.append(
        f"  free    : users={free_ui.get('user_count', 0):<5} "
        f"total_req={free_ui.get('total_requests', 0):<6} "
        f"avg_req_per_user={_fmt(free_ui.get('avg_requests_per_user'), '.2f')}"
    )
    lines.append(
        f"  premium : users={premium_ui.get('user_count', 0):<5} "
        f"total_req={premium_ui.get('total_requests', 0):<6} "
        f"avg_req_per_user={_fmt(premium_ui.get('avg_requests_per_user'), '.2f')}"
    )

    top_brackets = ui.get("top_percentile_users", {})
    for label, rows in sorted(top_brackets.items()):
        lines.append(f"  {label}:")
        if not rows:
            lines.append("    (no users)")
            continue
        for row in rows:
            kh = row.get("key_hash", "") or ""
            short = kh[:12] + ("…" if len(kh) > 12 else "")
            lines.append(
                f"    {short:<15} tier={row.get('tier', '?'):<8} "
                f"requests={row.get('requests', 0)}"
            )

    lines.append(bar)
    return "\n".join(lines)


def format_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False, default=str)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_reports(
    report: dict,
    reports_dir: Union[str, Path] = DEFAULT_REPORTS_DIR,
) -> tuple[Path, Path]:
    """
    Write the text + JSON reports to disk. Returns the two paths.
    Creates parent directories as needed. Raises on unrecoverable
    I/O errors (CLI wraps and exits 2).
    """
    date = str(report.get("report_date") or _today_utc_str())
    base = Path(reports_dir)
    base.mkdir(parents=True, exist_ok=True)
    txt_path = base / f"pricing_report_{date}.txt"
    json_path = base / f"pricing_report_{date}.json"
    txt_path.write_text(format_text(report) + "\n", encoding="utf-8")
    json_path.write_text(format_json(report) + "\n", encoding="utf-8")
    return txt_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.analysis.pricing_report",
        description=(
            "Generate a free-vs-paid pricing intelligence report by "
            "joining the per-key usage log and the conversion log. "
            "Offline analytics only — never hits Core or billing."
        ),
    )
    parser.add_argument(
        "--usage", default=DEFAULT_USAGE_PATH,
        help=f"Path to the usage JSONL (default: {DEFAULT_USAGE_PATH})",
    )
    parser.add_argument(
        "--conversions", default=DEFAULT_CONVERSION_PATH,
        help=f"Path to the conversion JSONL (default: {DEFAULT_CONVERSION_PATH})",
    )
    parser.add_argument(
        "--reports-dir", default=DEFAULT_REPORTS_DIR,
        help=f"Directory for output (default: {DEFAULT_REPORTS_DIR})",
    )
    parser.add_argument(
        "--date", default=None,
        help="Report date YYYY-MM-DD (default: today UTC)",
    )
    parser.add_argument(
        "--json-only", action="store_true",
        help="Print only the JSON payload to stdout, do not write files",
    )
    parser.add_argument(
        "--text-only", action="store_true",
        help="Print only the plain-text report to stdout, do not write files",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    report = build_pricing_report(
        usage_path=args.usage,
        conversion_path=args.conversions,
        report_date=args.date,
    )

    if args.json_only and args.text_only:
        print(
            "error: --json-only and --text-only are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.json_only:
        print(format_json(report))
        return 0
    if args.text_only:
        print(format_text(report))
        return 0

    try:
        txt_path, json_path = write_reports(report, reports_dir=args.reports_dir)
    except Exception as exc:
        print(
            f"error: failed to write reports: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(f"Pricing intelligence report written for {report['report_date']}:")
    print(f"  text: {txt_path}")
    print(f"  json: {json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
