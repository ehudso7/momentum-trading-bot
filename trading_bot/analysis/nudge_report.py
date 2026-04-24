"""
Phase 5.8 — Nudge Copy Performance Report.

Offline analytics that joins the Phase 5.5 upgrade-events JSONL
log and the Phase 4.9 conversion JSONL log on the opaque
``api_key_hash`` column to answer:

  * For each ``copy_variant_hash`` (i.e. each distinct piece of
    Phase 5.7 nudge copy that has ever rendered to a free-tier
    user), how many times did it fire?
  * How many distinct users saw it?
  * How many of those users later converted?
  * What is the per-variant conversion rate?
  * How long did it typically take to convert (median + p90)?

The report intentionally groups by ``copy_variant_hash`` only.
The raw copy text never enters the upgrade-events JSONL (Phase
5.8 hashes at write time), so it cannot enter the report — what
operators get is "variant 9b3f… converts at 12% in median 5.4d";
they correlate that hash back to a variant via their own
deployment notes.

Inputs (paths overridable on the CLI):

    data/api_upgrade_events.jsonl   (Phase 5.5/5.8)
    data/api_conversions.jsonl      (Phase 4.9)

Outputs:

    reports/nudge_report_<YYYY-MM-DD>.txt
    reports/nudge_report_<YYYY-MM-DD>.json

Privacy posture:

  * Both inputs already use ``SHA-256(api_key)[:32]`` — a raw API
    key cannot be loaded.
  * ``copy_variant_hash`` is itself a SHA-256 prefix of the
    operator-facing copy; the raw copy is **never** persisted by
    Phase 5.8 and consequently can never appear in the report.
  * A leak-guard test plants unique markers across every
    optional input field and asserts they don't appear on disk.
  * Per-variant rows omit ``api_key_hash`` so BI pipelines can't
    derive which users saw which variant from the report alone.

CLI::

    python -m trading_bot.analysis.nudge_report
    python -m trading_bot.analysis.nudge_report --date 2026-04-24
    python -m trading_bot.analysis.nudge_report \\
        --events data/api_upgrade_events.jsonl \\
        --conversions data/api_conversions.jsonl \\
        --reports-dir reports
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
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

DEFAULT_EVENTS_PATH = "data/api_upgrade_events.jsonl"
DEFAULT_CONVERSION_PATH = "data/api_conversions.jsonl"
DEFAULT_REPORTS_DIR = "reports"

REPORT_TYPE = "nudge_copy_performance"

# Minimum distinct users for a variant to be eligible for the
# "strongest" pick. Defends against one-user-one-conversion
# variants dominating the headline. Override on the CLI via
# ``--min-users-for-ranking``.
MIN_SUPPORT_FOR_RANKING = 2


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
    out: list[dict] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            parsed = json.loads(s)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def load_upgrade_events(
    path: Union[str, Path] = DEFAULT_EVENTS_PATH,
) -> list[dict]:
    return _read_jsonl(path)


def load_conversions(
    path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
) -> list[dict]:
    return _read_jsonl(path)


# ---------------------------------------------------------------------------
# Stat / time helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    """Nearest-rank percentile; empty → None."""
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


def _round_or_none(value: Optional[float], ndigits: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return None


def _seconds_to_days(seconds: Optional[float]) -> Optional[float]:
    if seconds is None:
        return None
    return round(float(seconds) / 86400.0, 4)


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _conversions_first_ts_by_hash(
    conversions: list[dict],
) -> dict[str, datetime]:
    """``api_key_hash → earliest parseable conversion timestamp``."""
    out: dict[str, datetime] = {}
    for rec in conversions:
        h = rec.get("api_key_hash")
        if not h:
            continue
        ts = _parse_iso_utc(rec.get("timestamp"))
        if ts is None:
            continue
        key = str(h)
        prev = out.get(key)
        if prev is None or ts < prev:
            out[key] = ts
    return out


def _has_variant_hash(rec: dict) -> bool:
    """Row carries a non-empty ``copy_variant_hash`` field."""
    v = rec.get("copy_variant_hash")
    return isinstance(v, str) and bool(v)


# ---------------------------------------------------------------------------
# Per-variant metrics
# ---------------------------------------------------------------------------


def compute_variant_metrics(
    events: list[dict],
    conversions: list[dict],
) -> list[dict[str, Any]]:
    """
    Build one row per ``copy_variant_hash``. Rows without a
    variant hash are excluded from the per-variant section but
    counted on the totals block.

    Each row:
      * ``copy_variant_hash``
      * ``event_count``
      * ``unique_users``
      * ``converted_users``
      * ``conversion_rate``
      * ``delta_samples``
      * ``median_time_to_convert_seconds`` / ``..._days``
      * ``p90_time_to_convert_seconds`` / ``..._days``
      * ``events`` — sorted list of event names this variant
        appeared on (lets BI confirm "variant X is only the
        banner copy" without joining back).

    Time delta = (earliest conversion ts) − (earliest event ts
    *for this variant hash*) per converted user. Negative deltas
    are dropped — premium users never reach the funnel, but a
    data oddity shouldn't poison the stats.

    Sort: ``conversion_rate`` desc, tie-break ``copy_variant_hash``
    alphabetically (deterministic).
    """
    by_hash_users: dict[str, set[str]] = defaultdict(set)
    by_hash_count: dict[str, int] = defaultdict(int)
    by_hash_events: dict[str, set[str]] = defaultdict(set)
    # earliest event ts per (variant_hash, user_hash).
    earliest_per_user: dict[tuple[str, str], datetime] = {}

    for rec in events:
        if not _has_variant_hash(rec):
            continue
        h = rec.get("api_key_hash")
        if not h:
            continue
        v = str(rec["copy_variant_hash"])
        u = str(h)
        by_hash_count[v] += 1
        by_hash_users[v].add(u)
        evt = rec.get("event")
        if isinstance(evt, str) and evt:
            by_hash_events[v].add(evt)
        ts = _parse_iso_utc(rec.get("timestamp"))
        if ts is None:
            continue
        key = (v, u)
        prev = earliest_per_user.get(key)
        if prev is None or ts < prev:
            earliest_per_user[key] = ts

    conv_ts_by_hash = _conversions_first_ts_by_hash(conversions)

    rows: list[dict[str, Any]] = []
    for variant_hash, users in by_hash_users.items():
        n_users = len(users)
        converted = users & set(conv_ts_by_hash.keys())
        n_conv = len(converted)
        rate: Optional[float] = None
        if n_users > 0:
            rate = n_conv / n_users

        deltas: list[float] = []
        for u in converted:
            evt_ts = earliest_per_user.get((variant_hash, u))
            conv_ts = conv_ts_by_hash.get(u)
            if evt_ts is None or conv_ts is None:
                continue
            delta = (conv_ts - evt_ts).total_seconds()
            if delta < 0:
                continue
            deltas.append(delta)

        median_s = _median(deltas)
        p90_s = _percentile(deltas, 0.90)

        rows.append({
            "copy_variant_hash": variant_hash,
            "event_count": by_hash_count[variant_hash],
            "unique_users": n_users,
            "converted_users": n_conv,
            "conversion_rate": _round_or_none(rate),
            "delta_samples": len(deltas),
            "median_time_to_convert_seconds": _round_or_none(median_s, 2),
            "p90_time_to_convert_seconds": _round_or_none(p90_s, 2),
            "median_time_to_convert_days": _seconds_to_days(median_s),
            "p90_time_to_convert_days": _seconds_to_days(p90_s),
            "events": sorted(by_hash_events[variant_hash]),
        })

    rows.sort(
        key=lambda r: (
            -(r["conversion_rate"]
              if r["conversion_rate"] is not None else -1.0),
            r["copy_variant_hash"],
        ),
    )
    return rows


# ---------------------------------------------------------------------------
# Top-pick
# ---------------------------------------------------------------------------


def pick_strongest_variant(
    rows: list[dict[str, Any]],
    *,
    min_users: int = MIN_SUPPORT_FOR_RANKING,
) -> Optional[dict[str, Any]]:
    """Return ``{"copy_variant_hash": ..., "value": rate}`` or None."""
    eligible = [
        r for r in rows
        if r.get("unique_users", 0) >= min_users
        and r.get("conversion_rate") is not None
    ]
    if not eligible:
        return None
    eligible.sort(
        key=lambda r: (-float(r["conversion_rate"]), r["copy_variant_hash"]),
    )
    winner = eligible[0]
    return {
        "copy_variant_hash": winner["copy_variant_hash"],
        "value": winner["conversion_rate"],
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_nudge_report(
    events_path: Union[str, Path] = DEFAULT_EVENTS_PATH,
    conversion_path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
    *,
    report_date: Optional[str] = None,
    min_users_for_ranking: int = MIN_SUPPORT_FOR_RANKING,
) -> dict:
    """
    Assemble the nudge-copy-performance report. Safe on missing /
    corrupt inputs.
    """
    events = load_upgrade_events(events_path)
    conversions = load_conversions(conversion_path)

    sources = {
        "upgrade_events_log": {
            "path": str(events_path),
            "exists": Path(events_path).exists(),
            "rows": len(events),
        },
        "conversion_log": {
            "path": str(conversion_path),
            "exists": Path(conversion_path).exists(),
            "rows": len(conversions),
        },
    }

    variants = compute_variant_metrics(events, conversions)
    strongest = pick_strongest_variant(
        variants, min_users=min_users_for_ranking,
    )

    events_with_variant = sum(1 for rec in events if _has_variant_hash(rec))
    events_without_variant = sum(
        1 for rec in events if not _has_variant_hash(rec)
    )

    totals = {
        "events_with_variant": events_with_variant,
        "events_without_variant": events_without_variant,
        "distinct_variants": len(variants),
        "unique_users": len({
            str(rec.get("api_key_hash"))
            for rec in events
            if rec.get("api_key_hash") and _has_variant_hash(rec)
        }),
        "converted_users": len({
            str(rec.get("api_key_hash"))
            for rec in conversions
            if rec.get("api_key_hash")
        }),
    }

    return {
        "report_type": REPORT_TYPE,
        "report_date": report_date or _today_utc_str(),
        "sources": sources,
        "min_users_for_ranking": int(min_users_for_ranking),
        "totals": totals,
        "variants": variants,
        "strongest_variant": strongest,
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


def _short_hash(h: str, n: int = 12) -> str:
    s = str(h or "")
    return s[:n] + ("…" if len(s) > n else "")


def format_text(report: dict) -> str:
    lines: list[str] = []
    bar = "=" * 86
    lines.append(bar)
    lines.append(
        f"NUDGE COPY PERFORMANCE REPORT — {report.get('report_date', '?')}"
    )
    lines.append(bar)

    sources = report.get("sources", {})
    lines.append("Sources:")
    for name, meta in sources.items():
        lines.append(
            f"  {name:<22} exists={meta.get('exists')!s:<5} "
            f"rows={meta.get('rows', 0):<6}"
        )

    totals = report.get("totals", {})
    lines.append("")
    lines.append("Totals:")
    lines.append(
        f"  events_with_variant      = "
        f"{totals.get('events_with_variant', 0)}"
    )
    lines.append(
        f"  events_without_variant   = "
        f"{totals.get('events_without_variant', 0)}"
    )
    lines.append(
        f"  distinct_variants        = "
        f"{totals.get('distinct_variants', 0)}"
    )
    lines.append(
        f"  unique_users             = "
        f"{totals.get('unique_users', 0)}"
    )
    lines.append(
        f"  converted_users          = "
        f"{totals.get('converted_users', 0)}"
    )
    lines.append(
        f"  min_users_for_ranking    = "
        f"{report.get('min_users_for_ranking', 0)}"
    )

    lines.append("")
    lines.append("By copy_variant_hash:")
    variants = report.get("variants", []) or []
    if not variants:
        lines.append("  (no variants recorded — no events carried a hash)")
    else:
        header = (
            f"  {'variant':<14} {'count':>6} {'users':>6} "
            f"{'conv':>5} {'rate':>8} {'med_d':>8} {'p90_d':>8} "
            f"{'events':>30}"
        )
        lines.append(header)
        lines.append("  " + ("-" * (len(header) - 2)))
        for r in variants:
            evts = ",".join(r.get("events") or [])[:30]
            lines.append(
                f"  {_short_hash(r.get('copy_variant_hash', '')):<14} "
                f"{r.get('event_count', 0):>6} "
                f"{r.get('unique_users', 0):>6} "
                f"{r.get('converted_users', 0):>5} "
                f"{_fmt(r.get('conversion_rate'), '.2%'):>8} "
                f"{_fmt(r.get('median_time_to_convert_days'), '.2f'):>8} "
                f"{_fmt(r.get('p90_time_to_convert_days'), '.2f'):>8} "
                f"{evts:>30}"
            )

    strongest = report.get("strongest_variant")
    lines.append("")
    if not strongest:
        lines.append("Strongest variant: (none — min-support not met)")
    else:
        lines.append(
            f"Strongest variant : {_short_hash(strongest.get('copy_variant_hash', ''))} "
            f" ({_fmt(strongest.get('value'), '.2%')})"
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
    date = str(report.get("report_date") or _today_utc_str())
    base = Path(reports_dir)
    base.mkdir(parents=True, exist_ok=True)
    txt_path = base / f"nudge_report_{date}.txt"
    json_path = base / f"nudge_report_{date}.json"
    txt_path.write_text(format_text(report) + "\n", encoding="utf-8")
    json_path.write_text(format_json(report) + "\n", encoding="utf-8")
    return txt_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.analysis.nudge_report",
        description=(
            "Generate a nudge-copy performance report by joining "
            "the upgrade-events JSONL and the conversion JSONL on "
            "api_key_hash, grouped by copy_variant_hash. Offline "
            "analytics only — never hits Core, the live API, or "
            "billing."
        ),
    )
    parser.add_argument(
        "--events", default=DEFAULT_EVENTS_PATH,
        help=(
            f"Path to the upgrade-events JSONL "
            f"(default: {DEFAULT_EVENTS_PATH})"
        ),
    )
    parser.add_argument(
        "--conversions", default=DEFAULT_CONVERSION_PATH,
        help=(
            f"Path to the conversion JSONL "
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
        "--min-users-for-ranking", type=int,
        default=MIN_SUPPORT_FOR_RANKING,
        help=(
            "Minimum unique users for a variant to qualify for the "
            "strongest-variant pick "
            f"(default: {MIN_SUPPORT_FOR_RANKING})"
        ),
    )
    parser.add_argument(
        "--json-only", action="store_true",
        help="Print JSON to stdout instead of writing files.",
    )
    parser.add_argument(
        "--text-only", action="store_true",
        help="Print the text report to stdout instead of writing files.",
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

    report = build_nudge_report(
        events_path=args.events,
        conversion_path=args.conversions,
        report_date=args.date,
        min_users_for_ranking=args.min_users_for_ranking,
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
    print(f"Nudge copy performance report written for {report['report_date']}:")
    print(f"  text: {txt_path}")
    print(f"  json: {json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
