"""
Phase 5.3 — Channel Performance Report.

Offline analytics that joins the three Phase 5.x append-only logs
on the opaque ``SHA-256(api_key)[:32]`` column to answer, for each
``ref_code`` that ever appeared in a ``?ref=`` query param:

  * How many distinct users arrived through this channel?
  * How many growth events did the channel produce?
  * How many API requests did those users make, total and premium?
  * How many of those users converted to a paid plan?
  * What is the conversion rate of the channel?
  * What is the average number of requests per user?

On top of those per-channel rows the report identifies the top
channel by:

  * unique users
  * conversions
  * conversion rate (with a minimum-support guard so a one-user
    bucket with 1 conversion does not outrank a real channel)
  * premium usage requests

Inputs are three append-only JSONL files already produced by the
bot — no database, no external service:

    data/api_growth.jsonl        (Phase 5.1 — ``?ref=`` events)
    data/api_usage.jsonl         (Phase 4.6 — per-key usage)
    data/api_conversions.jsonl   (Phase 4.9 — free → paid)

Outputs are two plain files next to the other reports:

    reports/channel_report_<YYYY-MM-DD>.txt
    reports/channel_report_<YYYY-MM-DD>.json

Privacy posture:
  * All three inputs already use ``SHA-256(api_key)[:32]`` — raw
    API keys cannot be loaded in the first place.
  * The report propagates ONLY aggregate counts and the opaque
    ``ref_code`` / hash; never a name, email, IP, path, or
    request id.
  * A leak-guard test plants unique markers (raw-looking API keys,
    IPs, request ids, paths) in each input and asserts they do
    NOT appear in the rendered report body.

CLI::

    python -m trading_bot.analysis.channel_report
    python -m trading_bot.analysis.channel_report --date 2026-04-24
    python -m trading_bot.analysis.channel_report \\
        --growth data/api_growth.jsonl \\
        --usage data/api_usage.jsonl \\
        --conversions data/api_conversions.jsonl \\
        --reports-dir reports
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_GROWTH_PATH = "data/api_growth.jsonl"
DEFAULT_USAGE_PATH = "data/api_usage.jsonl"
DEFAULT_CONVERSION_PATH = "data/api_conversions.jsonl"
DEFAULT_REPORTS_DIR = "reports"

REPORT_TYPE = "channel_performance"

# Minimum distinct users required for a channel to be eligible for
# "top channel by conversion_rate". Protects against a 1-user /
# 1-conversion bucket dominating the ranking — a rate we can't
# trust statistically.
MIN_SUPPORT_FOR_RATE_RANKING = 2


# ---------------------------------------------------------------------------
# Loaders — never raise
# ---------------------------------------------------------------------------


def _read_jsonl(path: Union[str, Path]) -> list[dict]:
    """Load a JSONL file into a list of dicts. Missing / corrupt → []."""
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


def load_growth(path: Union[str, Path] = DEFAULT_GROWTH_PATH) -> list[dict]:
    return _read_jsonl(path)


def load_usage(path: Union[str, Path] = DEFAULT_USAGE_PATH) -> list[dict]:
    return _read_jsonl(path)


def load_conversions(
    path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
) -> list[dict]:
    return _read_jsonl(path)


# ---------------------------------------------------------------------------
# Stat helpers
# ---------------------------------------------------------------------------


def _today_utc_str() -> str:
    """UTC calendar date as YYYY-MM-DD. Factored out for test patching."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _round_or_none(value: Optional[float], ndigits: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Joins — pure functions over already-loaded lists
# ---------------------------------------------------------------------------


def _index_usage_by_hash(usage: list[dict]) -> dict[str, dict[str, int]]:
    """
    Build ``{key_hash: {"total": n, "premium": m}}``. A user with no
    premium request still appears with ``premium = 0`` so downstream
    lookups are branchless.
    """
    out: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "premium": 0},
    )
    for row in usage:
        h = row.get("key_hash")
        if not h:
            continue
        key = str(h)
        out[key]["total"] += 1
        if str(row.get("tier") or "").lower() == "premium":
            out[key]["premium"] += 1
    return out


def _converted_hashes(conversions: list[dict]) -> set[str]:
    """Set of distinct ``api_key_hash`` values that ever converted."""
    return {
        str(rec.get("api_key_hash"))
        for rec in conversions
        if rec.get("api_key_hash")
    }


def _group_growth_by_ref(
    growth: list[dict],
) -> dict[str, dict[str, Any]]:
    """
    Bucket growth events by ``ref_code``. Returns::

        {
          ref: {
            "events": int,
            "user_hashes": set[str],
          },
          ...
        }

    Rows without a ref_code or api_key_hash are ignored — the
    growth middleware sanitises both, but we defend anyway.
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"events": 0, "user_hashes": set()},
    )
    for rec in growth:
        ref = rec.get("ref_code")
        h = rec.get("api_key_hash")
        if not ref or not h:
            continue
        b = buckets[str(ref)]
        b["events"] += 1
        b["user_hashes"].add(str(h))
    return buckets


# ---------------------------------------------------------------------------
# Per-channel metrics
# ---------------------------------------------------------------------------


def compute_channel_rows(
    growth: list[dict],
    usage: list[dict],
    conversions: list[dict],
) -> list[dict[str, Any]]:
    """
    Build one row per ``ref_code``. Rows are sorted by
    ``unique_users`` descending, tie-broken by ``ref_code``
    alphabetically so the output is deterministic.
    """
    grouped = _group_growth_by_ref(growth)
    usage_index = _index_usage_by_hash(usage)
    converted = _converted_hashes(conversions)

    rows: list[dict[str, Any]] = []
    for ref, bucket in grouped.items():
        user_hashes: set[str] = bucket["user_hashes"]
        total_usage = 0
        premium_usage = 0
        for h in user_hashes:
            stats = usage_index.get(h)
            if stats is None:
                continue
            total_usage += stats["total"]
            premium_usage += stats["premium"]

        n_users = len(user_hashes)
        n_conv = len(user_hashes & converted)

        rate: Optional[float] = None
        if n_users > 0:
            rate = n_conv / n_users

        avg_req: Optional[float] = None
        if n_users > 0:
            avg_req = total_usage / n_users

        rows.append({
            "ref_code": ref,
            "unique_users": n_users,
            "total_growth_events": int(bucket["events"]),
            "total_usage_requests": total_usage,
            "premium_usage_requests": premium_usage,
            "conversions": n_conv,
            "conversion_rate": _round_or_none(rate),
            "avg_requests_per_user": _round_or_none(avg_req, 3),
        })

    rows.sort(key=lambda r: (-r["unique_users"], r["ref_code"]))
    return rows


# ---------------------------------------------------------------------------
# Top-channel selection
# ---------------------------------------------------------------------------


def _pick_top_rate(
    rows: list[dict[str, Any]],
    metric: str,
    min_users: int,
) -> Optional[dict[str, Any]]:
    """
    Top channel by rate with a deterministic tiebreaker on
    ``ref_code``. Prefers the larger rate; ties broken by smaller
    ``ref_code`` alphabetically; then by larger ``unique_users``.
    """
    eligible = [
        r for r in rows
        if r.get("unique_users", 0) >= min_users
        and r.get(metric) is not None
    ]
    if not eligible:
        return None
    # Primary: rate (descending). Secondary: ref_code (ascending).
    eligible.sort(
        key=lambda r: (-float(r[metric]), r["ref_code"]),
    )
    winner = eligible[0]
    return {"ref_code": winner["ref_code"], "value": winner[metric]}


def _pick_top_count(
    rows: list[dict[str, Any]],
    metric: str,
) -> Optional[dict[str, Any]]:
    """
    Top channel by an integer count metric. Ties broken by
    ``ref_code`` alphabetically.
    """
    eligible = [r for r in rows if r.get(metric) is not None]
    if not eligible:
        return None
    eligible.sort(key=lambda r: (-int(r[metric]), r["ref_code"]))
    winner = eligible[0]
    return {"ref_code": winner["ref_code"], "value": winner[metric]}


def compute_top_channels(
    rows: list[dict[str, Any]],
    *,
    min_users_for_rate: int = MIN_SUPPORT_FOR_RATE_RANKING,
) -> dict[str, Optional[dict[str, Any]]]:
    """Return the top channel by each headline metric."""
    return {
        "by_users": _pick_top_count(rows, "unique_users"),
        "by_conversions": _pick_top_count(rows, "conversions"),
        "by_conversion_rate": _pick_top_rate(
            rows, "conversion_rate", min_users=min_users_for_rate,
        ),
        "by_premium_usage_requests": _pick_top_count(
            rows, "premium_usage_requests",
        ),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_channel_report(
    growth_path: Union[str, Path] = DEFAULT_GROWTH_PATH,
    usage_path: Union[str, Path] = DEFAULT_USAGE_PATH,
    conversion_path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
    *,
    report_date: Optional[str] = None,
    min_users_for_rate: int = MIN_SUPPORT_FOR_RATE_RANKING,
) -> dict:
    """
    Assemble the channel-performance report dict. Safe on missing /
    corrupt inputs (each loader returns ``[]`` and the downstream
    metrics produce an empty channels list + all-None top picks).
    """
    growth = load_growth(growth_path)
    usage = load_usage(usage_path)
    conversions = load_conversions(conversion_path)

    sources = {
        "growth_log": {
            "path": str(growth_path),
            "exists": Path(growth_path).exists(),
            "rows": len(growth),
        },
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

    rows = compute_channel_rows(growth, usage, conversions)
    top = compute_top_channels(rows, min_users_for_rate=min_users_for_rate)

    totals = {
        "channels": len(rows),
        "unique_users": len({
            h
            for rec in growth
            for h in [rec.get("api_key_hash")]
            if h
        }),
        "growth_events": sum(r["total_growth_events"] for r in rows),
        "attributed_usage_requests": sum(
            r["total_usage_requests"] for r in rows
        ),
        "attributed_conversions": sum(r["conversions"] for r in rows),
    }

    return {
        "report_type": REPORT_TYPE,
        "report_date": report_date or _today_utc_str(),
        "sources": sources,
        "totals": totals,
        "min_users_for_rate_ranking": int(min_users_for_rate),
        "channels": rows,
        "top_channels": top,
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


def _fmt_top(pick: Optional[dict[str, Any]], value_spec: str = "") -> str:
    if not pick:
        return "(none)"
    ref = pick.get("ref_code", "?")
    val = pick.get("value")
    if value_spec:
        return f"{ref}  ({_fmt(val, value_spec)})"
    return f"{ref}  ({val})"


def format_text(report: dict) -> str:
    lines: list[str] = []
    bar = "=" * 78
    lines.append(bar)
    lines.append(
        f"CHANNEL PERFORMANCE REPORT — {report.get('report_date', '?')}"
    )
    lines.append(bar)

    sources = report.get("sources", {})
    lines.append("Sources:")
    for name, meta in sources.items():
        lines.append(
            f"  {name:<16} exists={meta.get('exists')!s:<5} "
            f"rows={meta.get('rows', 0):<6}"
        )

    totals = report.get("totals", {})
    lines.append("")
    lines.append("Totals:")
    lines.append(f"  channels                    = {totals.get('channels', 0)}")
    lines.append(
        f"  unique_users                = {totals.get('unique_users', 0)}"
    )
    lines.append(
        f"  growth_events               = {totals.get('growth_events', 0)}"
    )
    lines.append(
        f"  attributed_usage_requests   = "
        f"{totals.get('attributed_usage_requests', 0)}"
    )
    lines.append(
        f"  attributed_conversions      = "
        f"{totals.get('attributed_conversions', 0)}"
    )
    lines.append(
        f"  min_users_for_rate_ranking  = "
        f"{report.get('min_users_for_rate_ranking', 0)}"
    )

    lines.append("")
    lines.append("Channels:")
    channels = report.get("channels", []) or []
    if not channels:
        lines.append("  (no channels recorded)")
    else:
        header = (
            f"  {'ref_code':<24} {'users':>6} {'events':>7} "
            f"{'requests':>9} {'premium':>8} {'conv':>5} "
            f"{'rate':>8} {'avg_req':>9}"
        )
        lines.append(header)
        lines.append("  " + ("-" * (len(header) - 2)))
        for r in channels:
            ref = str(r.get("ref_code", ""))[:24]
            lines.append(
                f"  {ref:<24} "
                f"{r.get('unique_users', 0):>6} "
                f"{r.get('total_growth_events', 0):>7} "
                f"{r.get('total_usage_requests', 0):>9} "
                f"{r.get('premium_usage_requests', 0):>8} "
                f"{r.get('conversions', 0):>5} "
                f"{_fmt(r.get('conversion_rate'), '.2%'):>8} "
                f"{_fmt(r.get('avg_requests_per_user'), '.2f'):>9}"
            )

    top = report.get("top_channels", {}) or {}
    lines.append("")
    lines.append("Top channels:")
    lines.append(f"  by_users                    : {_fmt_top(top.get('by_users'))}")
    lines.append(
        f"  by_conversions              : {_fmt_top(top.get('by_conversions'))}"
    )
    lines.append(
        f"  by_conversion_rate          : "
        f"{_fmt_top(top.get('by_conversion_rate'), '.2%')}"
    )
    lines.append(
        f"  by_premium_usage_requests   : "
        f"{_fmt_top(top.get('by_premium_usage_requests'))}"
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
    Creates parent directories as needed.
    """
    date = str(report.get("report_date") or _today_utc_str())
    base = Path(reports_dir)
    base.mkdir(parents=True, exist_ok=True)
    txt_path = base / f"channel_report_{date}.txt"
    json_path = base / f"channel_report_{date}.json"
    txt_path.write_text(format_text(report) + "\n", encoding="utf-8")
    json_path.write_text(format_json(report) + "\n", encoding="utf-8")
    return txt_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.analysis.channel_report",
        description=(
            "Generate a channel-performance report by joining the "
            "growth log, usage log, and conversion log on the opaque "
            "api_key_hash column. Offline analytics only — never "
            "hits Core, the API, or billing."
        ),
    )
    parser.add_argument(
        "--growth", default=DEFAULT_GROWTH_PATH,
        help=f"Path to the growth JSONL (default: {DEFAULT_GROWTH_PATH})",
    )
    parser.add_argument(
        "--usage", default=DEFAULT_USAGE_PATH,
        help=f"Path to the usage JSONL (default: {DEFAULT_USAGE_PATH})",
    )
    parser.add_argument(
        "--conversions", default=DEFAULT_CONVERSION_PATH,
        help=(
            "Path to the conversion JSONL "
            f"(default: {DEFAULT_CONVERSION_PATH})"
        ),
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
        "--min-users-for-rate", type=int,
        default=MIN_SUPPORT_FOR_RATE_RANKING,
        help=(
            "Minimum unique users for a channel to qualify for the "
            "top-conversion-rate ranking "
            f"(default: {MIN_SUPPORT_FOR_RATE_RANKING})"
        ),
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

    if args.json_only and args.text_only:
        print(
            "error: --json-only and --text-only are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    report = build_channel_report(
        growth_path=args.growth,
        usage_path=args.usage,
        conversion_path=args.conversions,
        report_date=args.date,
        min_users_for_rate=args.min_users_for_rate,
    )

    if args.json_only:
        print(format_json(report))
        return 0
    if args.text_only:
        print(format_text(report))
        return 0

    try:
        txt_path, json_path = write_reports(
            report, reports_dir=args.reports_dir,
        )
    except Exception as exc:
        print(
            f"error: failed to write reports: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(
        f"Channel performance report written for {report['report_date']}:"
    )
    print(f"  text: {txt_path}")
    print(f"  json: {json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
