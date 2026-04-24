"""
Phase 5.6 — Upgrade Funnel Report.

Offline analytics that joins four Phase 4.x / 5.x append-only
JSONL logs on the opaque ``SHA-256(api_key)[:32]`` column to
answer, for every upgrade-CTA trigger the bot has ever emitted:

  * How many free users saw this trigger?
  * How many of them later converted to paid?
  * What is the conversion rate for this trigger?
  * How long did it typically take between first seeing the
    trigger and converting (median + p90)?

and, for every ``ref_code`` that appeared on an upgrade event:

  * How many upgrade events did that channel produce?
  * How many distinct users do they represent?
  * How many of those users converted?
  * What is the per-channel conversion rate?

On top of the rows the report also names:

  * the strongest upgrade trigger — highest conversion rate,
    gated by a min-support guard so a one-user / one-conversion
    bucket does not outrank a real trigger;
  * the weakest upgrade trigger — lowest conversion rate with
    the same support guard (so we actually know which prompts
    are dead weight);
  * the strongest ref_code — highest conversion rate under the
    same support guard.

Inputs are four append-only JSONL files already produced by the
live server. No database, no external service, no dependency on
Core — this module's boundary test asserts it imports none of
the restricted surfaces.

    data/api_upgrade_events.jsonl   (Phase 5.5 — CTA telemetry)
    data/api_conversions.jsonl      (Phase 4.9 — free → paid)
    data/api_growth.jsonl           (Phase 5.1 — ?ref= events)
    data/api_usage.jsonl            (Phase 4.6 — per-key usage)

Outputs are two plain files next to the other reports:

    reports/upgrade_funnel_report_<YYYY-MM-DD>.txt
    reports/upgrade_funnel_report_<YYYY-MM-DD>.json

Privacy posture:

  * All four inputs already use ``SHA-256(api_key)[:32]`` — raw
    API keys cannot be loaded.
  * The report propagates ONLY aggregate counts and the opaque
    event / ref_code names. No paths, no request_ids, no
    per-user hashes are ever surfaced on the public surface.
  * A leak-guard test plants unique markers across every
    optional input field and asserts they never appear on disk.

CLI::

    python -m trading_bot.analysis.upgrade_funnel_report
    python -m trading_bot.analysis.upgrade_funnel_report --date 2026-04-24
    python -m trading_bot.analysis.upgrade_funnel_report \\
        --events data/api_upgrade_events.jsonl \\
        --conversions data/api_conversions.jsonl \\
        --growth data/api_growth.jsonl \\
        --usage data/api_usage.jsonl \\
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
DEFAULT_GROWTH_PATH = "data/api_growth.jsonl"
DEFAULT_USAGE_PATH = "data/api_usage.jsonl"
DEFAULT_REPORTS_DIR = "reports"

REPORT_TYPE = "upgrade_funnel"

# Minimum distinct users required for an event / ref_code to be
# eligible for the "strongest"/"weakest" rankings. Protects against
# a 1-user / 1-conversion bucket dominating a report built from a
# handful of rows.
MIN_SUPPORT_FOR_RANKING = 2


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
        s = line.strip()
        if not s:
            continue
        try:
            parsed = json.loads(s)
        except Exception:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def load_upgrade_events(
    path: Union[str, Path] = DEFAULT_EVENTS_PATH,
) -> list[dict]:
    return _read_jsonl(path)


def load_conversions(
    path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
) -> list[dict]:
    return _read_jsonl(path)


def load_growth(path: Union[str, Path] = DEFAULT_GROWTH_PATH) -> list[dict]:
    return _read_jsonl(path)


def load_usage(path: Union[str, Path] = DEFAULT_USAGE_PATH) -> list[dict]:
    return _read_jsonl(path)


# ---------------------------------------------------------------------------
# Stat + time helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    """Accept ISO with or without a trailing ``Z``."""
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
    """Nearest-rank percentile. Empty → None."""
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
# Index helpers — pure functions over already-loaded lists
# ---------------------------------------------------------------------------


def _conversions_first_timestamp_by_hash(
    conversions: list[dict],
) -> dict[str, datetime]:
    """
    Map ``api_key_hash -> earliest parseable conversion timestamp``.

    A user with multiple conversion rows (the conversion logger
    dedups, but a legitimate re-subscription is possible) gets the
    earliest one — that's the moment they became paid for the
    purpose of the funnel.
    """
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


def _events_by_type(
    events: list[dict],
) -> dict[str, list[tuple[str, Optional[datetime], Optional[str]]]]:
    """
    Bucket upgrade-event rows by ``event`` name.

    Returns ``{event_name: [(api_key_hash, timestamp, ref_code), ...]}``.
    Rows with no ``event`` or no ``api_key_hash`` are skipped.
    """
    out: dict[
        str, list[tuple[str, Optional[datetime], Optional[str]]]
    ] = defaultdict(list)
    for rec in events:
        evt = rec.get("event")
        h = rec.get("api_key_hash")
        if not evt or not h:
            continue
        ts = _parse_iso_utc(rec.get("timestamp"))
        ref = rec.get("ref_code") or None
        out[str(evt)].append((str(h), ts, ref))
    return out


# ---------------------------------------------------------------------------
# Per-event metrics
# ---------------------------------------------------------------------------


def compute_event_metrics(
    events: list[dict],
    conversions: list[dict],
) -> list[dict[str, Any]]:
    """
    Build one row per ``event`` name. Each row:

      * ``event_count`` — total rows with this event.
      * ``unique_users`` — distinct api_key_hash values for this event.
      * ``converted_users`` — of those, how many appear in conversions.
      * ``conversion_rate`` — converted_users / unique_users (or None).
      * ``median_time_from_event_to_conversion_seconds``
      * ``p90_time_from_event_to_conversion_seconds``
      * ``median_time_from_event_to_conversion_days`` (convenience)
      * ``p90_time_from_event_to_conversion_days`` (convenience)

    The time-delta is measured from the *earliest* time a converted
    user saw this event type to their conversion timestamp. Users
    with unparseable event or conversion timestamps, and users
    whose conversion pre-dates the event (shouldn't happen — a
    paid user can't hit the free-tier funnel — but defensive),
    are excluded from the delta stats.

    Rows are sorted by ``conversion_rate`` descending, tie-broken
    by ``event`` alphabetically, so output is deterministic.
    """
    buckets = _events_by_type(events)
    conv_ts_by_hash = _conversions_first_timestamp_by_hash(conversions)

    rows: list[dict[str, Any]] = []
    for event_name, entries in buckets.items():
        unique_hashes: set[str] = set()
        # earliest event time per (hash) for THIS event type.
        earliest_by_hash: dict[str, datetime] = {}
        for h, ts, _ref in entries:
            unique_hashes.add(h)
            if ts is None:
                continue
            prev = earliest_by_hash.get(h)
            if prev is None or ts < prev:
                earliest_by_hash[h] = ts

        converted_hashes = unique_hashes & set(conv_ts_by_hash.keys())

        deltas: list[float] = []
        for h in converted_hashes:
            evt_ts = earliest_by_hash.get(h)
            conv_ts = conv_ts_by_hash.get(h)
            if evt_ts is None or conv_ts is None:
                continue
            delta = (conv_ts - evt_ts).total_seconds()
            if delta < 0:
                continue
            deltas.append(delta)

        n_users = len(unique_hashes)
        n_converted = len(converted_hashes)
        rate: Optional[float] = None
        if n_users > 0:
            rate = n_converted / n_users

        median_s = _median(deltas)
        p90_s = _percentile(deltas, 0.90)

        rows.append({
            "event": event_name,
            "event_count": len(entries),
            "unique_users": n_users,
            "converted_users": n_converted,
            "conversion_rate": _round_or_none(rate),
            "delta_samples": len(deltas),
            "median_time_from_event_to_conversion_seconds":
                _round_or_none(median_s, 2),
            "p90_time_from_event_to_conversion_seconds":
                _round_or_none(p90_s, 2),
            "median_time_from_event_to_conversion_days":
                _seconds_to_days(median_s),
            "p90_time_from_event_to_conversion_days":
                _seconds_to_days(p90_s),
        })

    rows.sort(
        key=lambda r: (
            -(r["conversion_rate"] if r["conversion_rate"] is not None else -1.0),
            r["event"],
        ),
    )
    return rows


# ---------------------------------------------------------------------------
# Per-ref_code metrics
# ---------------------------------------------------------------------------


def compute_ref_code_metrics(
    events: list[dict],
    conversions: list[dict],
) -> list[dict[str, Any]]:
    """
    Build one row per ``ref_code`` that appears on at least one
    upgrade event. Each row:

      * ``ref_code``
      * ``upgrade_events`` — count of upgrade rows carrying this ref
      * ``unique_users`` — distinct api_key_hash values for those rows
      * ``conversions`` — how many of those users later converted
      * ``conversion_rate``

    Ref-code attribution is inclusive: a user who hit an upgrade
    prompt with ``?ref=twitter`` and later hit another prompt with
    ``?ref=hn`` counts toward both channels' conversion rate,
    mirroring the Phase 5.3 channel_report convention.

    Rows sorted by ``conversion_rate`` desc, tie-break by
    ``ref_code`` alphabetically.
    """
    by_ref_users: dict[str, set[str]] = defaultdict(set)
    by_ref_count: dict[str, int] = defaultdict(int)
    for rec in events:
        ref = rec.get("ref_code")
        h = rec.get("api_key_hash")
        if not ref or not h:
            continue
        by_ref_users[str(ref)].add(str(h))
        by_ref_count[str(ref)] += 1

    converted = set(_conversions_first_timestamp_by_hash(conversions).keys())

    rows: list[dict[str, Any]] = []
    for ref, users in by_ref_users.items():
        n_users = len(users)
        n_conv = len(users & converted)
        rate: Optional[float] = None
        if n_users > 0:
            rate = n_conv / n_users
        rows.append({
            "ref_code": ref,
            "upgrade_events": by_ref_count[ref],
            "unique_users": n_users,
            "conversions": n_conv,
            "conversion_rate": _round_or_none(rate),
        })

    rows.sort(
        key=lambda r: (
            -(r["conversion_rate"] if r["conversion_rate"] is not None else -1.0),
            r["ref_code"],
        ),
    )
    return rows


# ---------------------------------------------------------------------------
# Ranking: strongest / weakest
# ---------------------------------------------------------------------------


def _pick_strongest(
    rows: list[dict[str, Any]],
    *,
    key: str,
    name_field: str,
    min_users: int,
) -> Optional[dict[str, Any]]:
    eligible = [
        r for r in rows
        if r.get("unique_users", 0) >= min_users
        and r.get(key) is not None
    ]
    if not eligible:
        return None
    eligible.sort(
        key=lambda r: (-float(r[key]), str(r[name_field])),
    )
    winner = eligible[0]
    return {name_field: winner[name_field], "value": winner[key]}


def _pick_weakest(
    rows: list[dict[str, Any]],
    *,
    key: str,
    name_field: str,
    min_users: int,
) -> Optional[dict[str, Any]]:
    eligible = [
        r for r in rows
        if r.get("unique_users", 0) >= min_users
        and r.get(key) is not None
    ]
    if not eligible:
        return None
    # Ascending rate; tie-break alphabetically by name.
    eligible.sort(
        key=lambda r: (float(r[key]), str(r[name_field])),
    )
    winner = eligible[0]
    return {name_field: winner[name_field], "value": winner[key]}


def compute_top_picks(
    event_rows: list[dict[str, Any]],
    ref_rows: list[dict[str, Any]],
    *,
    min_users: int = MIN_SUPPORT_FOR_RANKING,
) -> dict[str, Optional[dict[str, Any]]]:
    """
    Return the headline picks. Each pick is either ``None`` (no
    eligible row) or ``{name_field: ..., "value": rate}``.
    """
    return {
        "strongest_upgrade_trigger": _pick_strongest(
            event_rows, key="conversion_rate",
            name_field="event", min_users=min_users,
        ),
        "weakest_upgrade_trigger": _pick_weakest(
            event_rows, key="conversion_rate",
            name_field="event", min_users=min_users,
        ),
        "strongest_ref_code": _pick_strongest(
            ref_rows, key="conversion_rate",
            name_field="ref_code", min_users=min_users,
        ),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_upgrade_funnel_report(
    events_path: Union[str, Path] = DEFAULT_EVENTS_PATH,
    conversion_path: Union[str, Path] = DEFAULT_CONVERSION_PATH,
    growth_path: Union[str, Path] = DEFAULT_GROWTH_PATH,
    usage_path: Union[str, Path] = DEFAULT_USAGE_PATH,
    *,
    report_date: Optional[str] = None,
    min_users_for_ranking: int = MIN_SUPPORT_FOR_RANKING,
) -> dict:
    """
    Assemble the funnel report dict. Safe on missing / corrupt
    inputs (each loader returns ``[]`` and the metrics functions
    produce empty rows + all-None picks).
    """
    events = load_upgrade_events(events_path)
    conversions = load_conversions(conversion_path)
    # Growth / usage are loaded for the source-manifest block
    # and for anyone who wants to extend the report later — the
    # headline metrics rely only on events + conversions.
    growth = load_growth(growth_path)
    usage = load_usage(usage_path)

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
    }

    event_rows = compute_event_metrics(events, conversions)
    ref_rows = compute_ref_code_metrics(events, conversions)
    picks = compute_top_picks(
        event_rows, ref_rows, min_users=min_users_for_ranking,
    )

    totals = {
        "events": sum(r["event_count"] for r in event_rows),
        "unique_users": len({
            str(rec.get("api_key_hash"))
            for rec in events
            if rec.get("api_key_hash")
        }),
        "converted_users": len({
            str(rec.get("api_key_hash"))
            for rec in conversions
            if rec.get("api_key_hash")
        }),
        "distinct_event_names": len(event_rows),
        "distinct_ref_codes": len(ref_rows),
    }

    return {
        "report_type": REPORT_TYPE,
        "report_date": report_date or _today_utc_str(),
        "sources": sources,
        "min_users_for_ranking": int(min_users_for_ranking),
        "totals": totals,
        "events": event_rows,
        "ref_codes": ref_rows,
        "top_picks": picks,
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


def _fmt_pick(pick: Optional[dict[str, Any]], name_field: str) -> str:
    if not pick:
        return "(none)"
    name = pick.get(name_field, "?")
    val = pick.get("value")
    return f"{name}  ({_fmt(val, '.2%')})"


def format_text(report: dict) -> str:
    lines: list[str] = []
    bar = "=" * 80
    lines.append(bar)
    lines.append(
        f"UPGRADE FUNNEL REPORT — {report.get('report_date', '?')}"
    )
    lines.append(bar)

    sources = report.get("sources", {})
    lines.append("Sources:")
    for name, meta in sources.items():
        lines.append(
            f"  {name:<20} exists={meta.get('exists')!s:<5} "
            f"rows={meta.get('rows', 0):<6}"
        )

    totals = report.get("totals", {})
    lines.append("")
    lines.append("Totals:")
    lines.append(f"  events                  = {totals.get('events', 0)}")
    lines.append(
        f"  unique_users            = {totals.get('unique_users', 0)}"
    )
    lines.append(
        f"  converted_users         = "
        f"{totals.get('converted_users', 0)}"
    )
    lines.append(
        f"  distinct_event_names    = "
        f"{totals.get('distinct_event_names', 0)}"
    )
    lines.append(
        f"  distinct_ref_codes      = "
        f"{totals.get('distinct_ref_codes', 0)}"
    )
    lines.append(
        f"  min_users_for_ranking   = "
        f"{report.get('min_users_for_ranking', 0)}"
    )

    lines.append("")
    lines.append("By upgrade event:")
    event_rows = report.get("events", []) or []
    if not event_rows:
        lines.append("  (no upgrade events recorded)")
    else:
        header = (
            f"  {'event':<26} {'count':>6} {'users':>6} {'conv':>5} "
            f"{'rate':>8} {'median_days':>12} {'p90_days':>10}"
        )
        lines.append(header)
        lines.append("  " + ("-" * (len(header) - 2)))
        for r in event_rows:
            name = str(r.get("event", ""))[:26]
            lines.append(
                f"  {name:<26} "
                f"{r.get('event_count', 0):>6} "
                f"{r.get('unique_users', 0):>6} "
                f"{r.get('converted_users', 0):>5} "
                f"{_fmt(r.get('conversion_rate'), '.2%'):>8} "
                f"{_fmt(r.get('median_time_from_event_to_conversion_days'), '.2f'):>12} "
                f"{_fmt(r.get('p90_time_from_event_to_conversion_days'), '.2f'):>10}"
            )

    lines.append("")
    lines.append("By ref_code:")
    ref_rows = report.get("ref_codes", []) or []
    if not ref_rows:
        lines.append("  (no ref_codes on upgrade events)")
    else:
        header = (
            f"  {'ref_code':<24} {'events':>7} {'users':>6} "
            f"{'conv':>5} {'rate':>8}"
        )
        lines.append(header)
        lines.append("  " + ("-" * (len(header) - 2)))
        for r in ref_rows:
            name = str(r.get("ref_code", ""))[:24]
            lines.append(
                f"  {name:<24} "
                f"{r.get('upgrade_events', 0):>7} "
                f"{r.get('unique_users', 0):>6} "
                f"{r.get('conversions', 0):>5} "
                f"{_fmt(r.get('conversion_rate'), '.2%'):>8}"
            )

    picks = report.get("top_picks", {}) or {}
    lines.append("")
    lines.append("Headline picks:")
    lines.append(
        f"  strongest_upgrade_trigger : "
        f"{_fmt_pick(picks.get('strongest_upgrade_trigger'), 'event')}"
    )
    lines.append(
        f"  weakest_upgrade_trigger   : "
        f"{_fmt_pick(picks.get('weakest_upgrade_trigger'), 'event')}"
    )
    lines.append(
        f"  strongest_ref_code        : "
        f"{_fmt_pick(picks.get('strongest_ref_code'), 'ref_code')}"
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
    """Write the text + JSON files; return their paths."""
    date = str(report.get("report_date") or _today_utc_str())
    base = Path(reports_dir)
    base.mkdir(parents=True, exist_ok=True)
    txt_path = base / f"upgrade_funnel_report_{date}.txt"
    json_path = base / f"upgrade_funnel_report_{date}.json"
    txt_path.write_text(format_text(report) + "\n", encoding="utf-8")
    json_path.write_text(format_json(report) + "\n", encoding="utf-8")
    return txt_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.analysis.upgrade_funnel_report",
        description=(
            "Generate an upgrade-funnel report by joining the Phase "
            "5.5 upgrade-event log, Phase 4.9 conversion log, Phase "
            "5.1 growth log, and Phase 4.6 usage log on the opaque "
            "api_key_hash column. Offline analytics only — never "
            "hits Core, the live API, or billing."
        ),
    )
    parser.add_argument(
        "--events", default=DEFAULT_EVENTS_PATH,
        help=f"Path to the upgrade-events JSONL (default: {DEFAULT_EVENTS_PATH})",
    )
    parser.add_argument(
        "--conversions", default=DEFAULT_CONVERSION_PATH,
        help=f"Path to the conversion JSONL (default: {DEFAULT_CONVERSION_PATH})",
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
            "Minimum unique users for an event / ref_code to qualify "
            f"for the strongest/weakest ranking "
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

    report = build_upgrade_funnel_report(
        events_path=args.events,
        conversion_path=args.conversions,
        growth_path=args.growth,
        usage_path=args.usage,
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
    print(f"Upgrade funnel report written for {report['report_date']}:")
    print(f"  text: {txt_path}")
    print(f"  json: {json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
