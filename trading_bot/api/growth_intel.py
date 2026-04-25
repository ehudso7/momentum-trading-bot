"""
Phase 10.4 — Growth Intelligence Layer.

Read-only analytics module that turns the existing JSONL telemetry
files (Phase 8.4 upgrade-events + Phase 10.3 share-events) into
operator-facing growth insights:

  * conversion funnel — ``upgrade_shown`` → ``upgrade_clicked`` →
    ``upgrade_completed`` aggregated overall and grouped by
    ``reason`` and ``endpoint``;
  * share funnel — ``share_generated`` → ``inbound_visit``
    aggregated overall and grouped by ``src`` (the inbound
    attribution token);
  * cross-funnel attribution — inbound visits joined to
    downstream ``upgrade_completed`` rows on the shared
    ``api_key_hash`` column, so an operator can see which
    referral source drove the most conversions.

Headlines surfaced by ``summarize`` / the CLI:

  * top converting trigger — the ``reason`` value with the
    highest shown→completed conversion rate (min-impressions
    threshold prevents single-row noise from winning);
  * top performing insight — the ``endpoint`` with the highest
    shown→completed conversion rate (each endpoint corresponds
    to a specific Phase 9.1/9.2/9.3 insight surface);
  * best source — the inbound ``src`` token whose visitors
    converted at the highest rate (attributed via
    ``api_key_hash``);
  * overall conversion rate — completed / shown across the whole
    upgrade funnel.

Boundary
--------

  * No persistence — this module only READS the existing JSONL
    files. It writes nothing.
  * No new HTTP route — invoked from the CLI or imported by an
    offline analytics notebook.
  * No raw API key surface — every grouping key is either an
    operator-supplied dimension (``reason``, ``endpoint``,
    ``src``) or the SHA-256[:32] hash already on disk. Raw keys
    are NEVER read because they are never written.
  * Pure stdlib + structlog (DEBUG-only). No FastAPI / Stripe /
    Core import — safely importable from a notebook or a CI job.

Env vars (read at call time, override-able per call):

    TRADING_API_UPGRADE_EVENTS_LOG_PATH   default data/api_upgrade_events.jsonl
    TRADING_API_SHARE_EVENTS_LOG_PATH     default data/api_share_events.jsonl

CLI::

    python -m trading_bot.api.growth_intel --summary
    python -m trading_bot.api.growth_intel --summary --json
    python -m trading_bot.api.growth_intel --summary \\
        --upgrade-path path/to/upgrade.jsonl \\
        --share-path  path/to/share.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional, Union

import structlog

from trading_bot.api.share_events import (
    DEFAULT_SHARE_EVENTS_LOG_PATH,
    EVENT_INBOUND_VISIT,
    EVENT_SHARE_GENERATED,
    SHARE_EVENTS_LOG_ENV_VAR,
)
from trading_bot.api.upgrade_events import (
    DEFAULT_UPGRADE_EVENTS_LOG_PATH,
    EVENT_UPGRADE_CLICKED,
    EVENT_UPGRADE_COMPLETED,
    EVENT_UPGRADE_SHOWN,
    UPGRADE_EVENTS_LOG_ENV_VAR,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Minimum number of ``upgrade_shown`` rows a dimension must have
#: before its conversion rate can win "top trigger" / "top insight".
#: Prevents a single-impression group from beating a high-volume
#: group on a 100 % accident.
DEFAULT_MIN_IMPRESSIONS = 5

#: Minimum number of ``inbound_visit`` rows a ``src`` must have
#: before it can win "best source".
DEFAULT_MIN_INBOUND = 3


# ---------------------------------------------------------------------------
# Loaders — read-only, fault-tolerant.
# ---------------------------------------------------------------------------


def _upgrade_log_path() -> Path:
    return Path(
        os.getenv(
            UPGRADE_EVENTS_LOG_ENV_VAR,
            DEFAULT_UPGRADE_EVENTS_LOG_PATH,
        )
    )


def _share_log_path() -> Path:
    return Path(
        os.getenv(
            SHARE_EVENTS_LOG_ENV_VAR,
            DEFAULT_SHARE_EVENTS_LOG_PATH,
        )
    )


def _read_jsonl(path: Path) -> list[dict]:
    """
    Load a JSONL file into a list of dict rows. Tolerant of:
      * missing files (returns []),
      * blank lines,
      * malformed JSON (drops the offending row),
      * non-dict payloads (drops the offending row).
    Never raises into the caller.
    """
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("growth_intel.read_error", path=str(path), error=str(exc))
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
    path: Union[str, Path, None] = None,
) -> list[dict]:
    """
    Load Phase 5.5 / 8.4 upgrade-events JSONL into a list of dicts.

    The default path resolves through
    ``$TRADING_API_UPGRADE_EVENTS_LOG_PATH`` (or
    ``data/api_upgrade_events.jsonl``). Pass an explicit path for
    offline analysis on rotated logs.

    Returns ``[]`` if the file is missing or unreadable. Malformed
    rows are dropped silently.
    """
    target = Path(path) if path is not None else _upgrade_log_path()
    return _read_jsonl(target)


def load_share_events(
    path: Union[str, Path, None] = None,
) -> list[dict]:
    """
    Load Phase 10.3 share-events JSONL into a list of dicts.

    The default path resolves through
    ``$TRADING_API_SHARE_EVENTS_LOG_PATH`` (or
    ``data/api_share_events.jsonl``). Pass an explicit path for
    offline analysis on rotated logs.

    Returns ``[]`` if the file is missing or unreadable.
    """
    target = Path(path) if path is not None else _share_log_path()
    return _read_jsonl(target)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _conversion_rate(numerator: int, denominator: int) -> float:
    """Safe ratio with the documented "0 / 0 → 0.0" semantics."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _by_dimension(
    rows: list[dict],
    *,
    dimension: str,
    funnel_events: tuple[str, str, str],
) -> dict[str, dict]:
    """
    Group ``rows`` by ``dimension`` (any field on the row), counting
    the three Phase 8.4 funnel events per group. Skips rows where
    the dimension value is missing / empty / non-string.

    Returns ``{value: {"shown": n, "clicked": n, "completed": n,
    "shown_to_completed": rate, "shown_to_clicked": rate,
    "clicked_to_completed": rate}}``.
    """
    shown_evt, clicked_evt, completed_evt = funnel_events
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for rec in rows:
        evt = rec.get("event")
        if evt not in (shown_evt, clicked_evt, completed_evt):
            continue
        value = rec.get(dimension)
        if not isinstance(value, str) or not value:
            continue
        counters[value][evt] += 1

    out: dict[str, dict] = {}
    for value, c in counters.items():
        shown = c[shown_evt]
        clicked = c[clicked_evt]
        completed = c[completed_evt]
        out[value] = {
            "shown": shown,
            "clicked": clicked,
            "completed": completed,
            "shown_to_clicked": _conversion_rate(clicked, shown),
            "clicked_to_completed": _conversion_rate(completed, clicked),
            "shown_to_completed": _conversion_rate(completed, shown),
        }
    return out


def _conversion_funnel(rows: list[dict]) -> dict:
    """
    Overall ``upgrade_shown`` → ``upgrade_clicked`` →
    ``upgrade_completed`` counts from a Phase 5.5 / 8.4 events log.
    """
    counts: Counter[str] = Counter()
    for rec in rows:
        evt = rec.get("event")
        if evt in (
            EVENT_UPGRADE_SHOWN,
            EVENT_UPGRADE_CLICKED,
            EVENT_UPGRADE_COMPLETED,
        ):
            counts[evt] += 1
    shown = counts[EVENT_UPGRADE_SHOWN]
    clicked = counts[EVENT_UPGRADE_CLICKED]
    completed = counts[EVENT_UPGRADE_COMPLETED]
    return {
        "shown": shown,
        "clicked": clicked,
        "completed": completed,
        "shown_to_clicked": _conversion_rate(clicked, shown),
        "clicked_to_completed": _conversion_rate(completed, clicked),
        "shown_to_completed": _conversion_rate(completed, shown),
    }


def _share_funnel(rows: list[dict]) -> dict:
    """
    Overall ``share_generated`` → ``inbound_visit`` counts.

    The "click-through" rate is approximate: inbound visits cannot
    be 1:1 attributed to a specific share generation, but the
    aggregate ratio is the operator-facing metric the viral loop
    cares about (more shares ⇒ more inbound visits).
    """
    counts: Counter[str] = Counter()
    for rec in rows:
        evt = rec.get("event")
        if evt in (EVENT_SHARE_GENERATED, EVENT_INBOUND_VISIT):
            counts[evt] += 1
    generated = counts[EVENT_SHARE_GENERATED]
    inbound = counts[EVENT_INBOUND_VISIT]
    return {
        "generated": generated,
        "inbound": inbound,
        "inbound_per_share": _conversion_rate(inbound, generated),
    }


def _share_by_src(rows: list[dict]) -> dict[str, dict]:
    """
    Per-``src`` share funnel. ``share_generated`` rows without a
    src use ``"(none)"`` so they're still visible.
    """
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for rec in rows:
        evt = rec.get("event")
        if evt not in (EVENT_SHARE_GENERATED, EVENT_INBOUND_VISIT):
            continue
        src = rec.get("src")
        bucket = src if isinstance(src, str) and src else "(none)"
        counters[bucket][evt] += 1

    out: dict[str, dict] = {}
    for src, c in counters.items():
        generated = c[EVENT_SHARE_GENERATED]
        inbound = c[EVENT_INBOUND_VISIT]
        out[src] = {
            "generated": generated,
            "inbound": inbound,
            "inbound_per_share": _conversion_rate(inbound, generated),
        }
    return out


def _attribution_by_src(
    upgrade_rows: list[dict],
    share_rows: list[dict],
) -> dict[str, dict]:
    """
    Cross-funnel attribution: for each inbound ``src`` token, how
    many distinct ``api_key_hash`` values landed via that src and
    later showed up in the conversion funnel?

    Join column is ``api_key_hash`` — the same SHA-256[:32] used by
    every Phase 4-10 hasher.
    """
    src_to_users: dict[str, set[str]] = defaultdict(set)
    for rec in share_rows:
        if rec.get("event") != EVENT_INBOUND_VISIT:
            continue
        src = rec.get("src")
        h = rec.get("api_key_hash")
        if not isinstance(src, str) or not src:
            continue
        if not isinstance(h, str) or not h:
            continue
        src_to_users[src].add(h)

    user_events: dict[str, set[str]] = defaultdict(set)
    for rec in upgrade_rows:
        evt = rec.get("event")
        if evt not in (
            EVENT_UPGRADE_SHOWN,
            EVENT_UPGRADE_CLICKED,
            EVENT_UPGRADE_COMPLETED,
        ):
            continue
        h = rec.get("api_key_hash")
        if not isinstance(h, str) or not h:
            continue
        user_events[h].add(str(evt))

    out: dict[str, dict] = {}
    for src, users in src_to_users.items():
        shown_users = sum(
            1 for u in users if EVENT_UPGRADE_SHOWN in user_events.get(u, ())
        )
        clicked_users = sum(
            1
            for u in users
            if EVENT_UPGRADE_CLICKED in user_events.get(u, ())
        )
        completed_users = sum(
            1
            for u in users
            if EVENT_UPGRADE_COMPLETED in user_events.get(u, ())
        )
        out[src] = {
            "inbound_users": len(users),
            "shown_users": shown_users,
            "clicked_users": clicked_users,
            "completed_users": completed_users,
            "completion_rate": _conversion_rate(
                completed_users, len(users),
            ),
        }
    return out


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------


def _top_by_completion(
    grouped: dict[str, dict],
    *,
    min_impressions: int,
) -> Optional[dict]:
    """
    Return the ``{value, ...stats}`` row with the highest
    ``shown_to_completed`` rate, ignoring groups with fewer than
    ``min_impressions`` shown rows. Ties broken by raw completed
    count then by alphabetic value to keep the output deterministic.
    """
    best: Optional[tuple[str, dict]] = None
    for value, stats in grouped.items():
        if stats.get("shown", 0) < min_impressions:
            continue
        if best is None:
            best = (value, stats)
            continue
        b_rate = best[1].get("shown_to_completed", 0.0)
        b_completed = best[1].get("completed", 0)
        c_rate = stats.get("shown_to_completed", 0.0)
        c_completed = stats.get("completed", 0)
        if (c_rate, c_completed, value) > (b_rate, b_completed, best[0]):
            best = (value, stats)
    if best is None:
        return None
    value, stats = best
    return {"value": value, **stats}


def _top_source(
    attribution: dict[str, dict],
    *,
    min_inbound: int,
) -> Optional[dict]:
    """Pick the src with the highest completion rate among sources
    that have at least ``min_inbound`` inbound visitors."""
    best: Optional[tuple[str, dict]] = None
    for src, stats in attribution.items():
        if stats.get("inbound_users", 0) < min_inbound:
            continue
        if best is None:
            best = (src, stats)
            continue
        b_rate = best[1].get("completion_rate", 0.0)
        b_completed = best[1].get("completed_users", 0)
        c_rate = stats.get("completion_rate", 0.0)
        c_completed = stats.get("completed_users", 0)
        if (c_rate, c_completed, src) > (b_rate, b_completed, best[0]):
            best = (src, stats)
    if best is None:
        return None
    src, stats = best
    return {"src": src, **stats}


# ---------------------------------------------------------------------------
# Public summary
# ---------------------------------------------------------------------------


def summarize(
    *,
    upgrade_path: Union[str, Path, None] = None,
    share_path: Union[str, Path, None] = None,
    min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
    min_inbound: int = DEFAULT_MIN_INBOUND,
) -> dict:
    """
    One-shot growth intelligence summary derived from the upgrade
    and share JSONL logs.

    Returns a stable, JSON-serialisable dict with the following
    top-level shape::

        {
          "conversion_funnel":    {shown, clicked, completed,
                                   shown_to_clicked,
                                   clicked_to_completed,
                                   shown_to_completed},
          "by_reason":            {reason: {<funnel stats>}},
          "by_insight":           {endpoint: {<funnel stats>}},
          "share_funnel":         {generated, inbound,
                                   inbound_per_share},
          "by_src":               {src: {<share funnel stats>}},
          "attribution_by_src":   {src: {<cross-funnel stats>}},
          "headlines": {
            "top_converting_trigger": {value, shown, clicked,
                                        completed, shown_to_completed,
                                        ...} | null,
            "top_performing_insight": {value, ...} | null,
            "best_source":            {src, inbound_users,
                                        completed_users,
                                        completion_rate} | null,
            "overall_conversion_rate": float (0..1)
          },
          "totals": {
            "upgrade_rows":  int,
            "share_rows":    int,
          }
        }
    """
    upgrade_rows = load_upgrade_events(upgrade_path)
    share_rows = load_share_events(share_path)

    overall = _conversion_funnel(upgrade_rows)
    by_reason = _by_dimension(
        upgrade_rows,
        dimension="reason",
        funnel_events=(
            EVENT_UPGRADE_SHOWN,
            EVENT_UPGRADE_CLICKED,
            EVENT_UPGRADE_COMPLETED,
        ),
    )
    # ``endpoint`` stands in as the operator-facing "insight id":
    # each Phase 9.x insight surface lives at a known endpoint
    # (/reports/latest hosts trend / readiness / regime insights,
    # /experiments/* hosts experiment-cap nudges, etc.). Grouping
    # the funnel by endpoint tells operators which insight surface
    # converts best, which is the actionable signal.
    by_insight = _by_dimension(
        upgrade_rows,
        dimension="endpoint",
        funnel_events=(
            EVENT_UPGRADE_SHOWN,
            EVENT_UPGRADE_CLICKED,
            EVENT_UPGRADE_COMPLETED,
        ),
    )
    share_overall = _share_funnel(share_rows)
    by_src = _share_by_src(share_rows)
    attribution = _attribution_by_src(upgrade_rows, share_rows)

    headlines = {
        "top_converting_trigger": _top_by_completion(
            by_reason, min_impressions=min_impressions,
        ),
        "top_performing_insight": _top_by_completion(
            by_insight, min_impressions=min_impressions,
        ),
        "best_source": _top_source(
            attribution, min_inbound=min_inbound,
        ),
        "overall_conversion_rate": overall["shown_to_completed"],
    }

    return {
        "conversion_funnel": overall,
        "by_reason": by_reason,
        "by_insight": by_insight,
        "share_funnel": share_overall,
        "by_src": by_src,
        "attribution_by_src": attribution,
        "headlines": headlines,
        "totals": {
            "upgrade_rows": len(upgrade_rows),
            "share_rows": len(share_rows),
        },
    }


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------


def _fmt_rate(value: float) -> str:
    return f"{(value * 100):.1f}%"


def format_summary_text(summary: dict) -> str:
    """Render ``summarize()`` output as plain text for the CLI."""
    bar = "=" * 72
    lines: list[str] = []
    lines.append(bar)
    lines.append("GROWTH INTELLIGENCE SUMMARY")
    lines.append(bar)

    totals = summary.get("totals", {}) or {}
    lines.append(
        f"upgrade_rows : {totals.get('upgrade_rows', 0):<6} "
        f"share_rows   : {totals.get('share_rows', 0)}"
    )

    funnel = summary.get("conversion_funnel", {}) or {}
    lines.append("")
    lines.append("Conversion funnel:")
    lines.append(
        f"  shown     : {funnel.get('shown', 0):<6}"
        f"  clicked   : {funnel.get('clicked', 0):<6}"
        f"  completed : {funnel.get('completed', 0)}"
    )
    lines.append(
        f"  shown→clicked   : "
        f"{_fmt_rate(funnel.get('shown_to_clicked', 0.0))}"
    )
    lines.append(
        f"  clicked→completed: "
        f"{_fmt_rate(funnel.get('clicked_to_completed', 0.0))}"
    )
    lines.append(
        f"  shown→completed : "
        f"{_fmt_rate(funnel.get('shown_to_completed', 0.0))}"
    )

    share_funnel = summary.get("share_funnel", {}) or {}
    lines.append("")
    lines.append("Share funnel:")
    lines.append(
        f"  generated : {share_funnel.get('generated', 0):<6}"
        f"  inbound   : {share_funnel.get('inbound', 0):<6}"
        f"  inbound/share : "
        f"{_fmt_rate(share_funnel.get('inbound_per_share', 0.0))}"
    )

    headlines = summary.get("headlines", {}) or {}
    lines.append("")
    lines.append("Headlines:")
    trigger = headlines.get("top_converting_trigger")
    if trigger:
        lines.append(
            f"  top converting trigger : {trigger['value']} "
            f"(shown={trigger.get('shown', 0)}, "
            f"completed={trigger.get('completed', 0)}, "
            f"rate={_fmt_rate(trigger.get('shown_to_completed', 0.0))})"
        )
    else:
        lines.append(
            "  top converting trigger : (insufficient data)"
        )
    insight = headlines.get("top_performing_insight")
    if insight:
        lines.append(
            f"  top performing insight : {insight['value']} "
            f"(shown={insight.get('shown', 0)}, "
            f"completed={insight.get('completed', 0)}, "
            f"rate={_fmt_rate(insight.get('shown_to_completed', 0.0))})"
        )
    else:
        lines.append(
            "  top performing insight : (insufficient data)"
        )
    source = headlines.get("best_source")
    if source:
        lines.append(
            f"  best source            : {source['src']} "
            f"(inbound={source.get('inbound_users', 0)}, "
            f"completed={source.get('completed_users', 0)}, "
            f"rate={_fmt_rate(source.get('completion_rate', 0.0))})"
        )
    else:
        lines.append(
            "  best source            : (insufficient data)"
        )
    overall_rate = headlines.get("overall_conversion_rate", 0.0)
    lines.append(
        f"  overall conversion rate: {_fmt_rate(overall_rate)}"
    )

    lines.append(bar)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.growth_intel",
        description=(
            "Phase 10.4 growth intelligence — read the existing "
            "Phase 8.4 upgrade-events and Phase 10.3 share-events "
            "JSONL logs and surface conversion / share / "
            "attribution headlines. Read-only: writes nothing, "
            "imports nothing from Core."
        ),
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print the growth-intel summary.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of plain text.",
    )
    parser.add_argument(
        "--upgrade-path", default=None,
        help=(
            "Override the upgrade-events JSONL path "
            f"(default: ${UPGRADE_EVENTS_LOG_ENV_VAR} or "
            f"{DEFAULT_UPGRADE_EVENTS_LOG_PATH})."
        ),
    )
    parser.add_argument(
        "--share-path", default=None,
        help=(
            "Override the share-events JSONL path "
            f"(default: ${SHARE_EVENTS_LOG_ENV_VAR} or "
            f"{DEFAULT_SHARE_EVENTS_LOG_PATH})."
        ),
    )
    parser.add_argument(
        "--min-impressions", type=int,
        default=DEFAULT_MIN_IMPRESSIONS,
        help=(
            "Floor on shown-row count before a reason / endpoint "
            f"can win the headline (default: {DEFAULT_MIN_IMPRESSIONS})."
        ),
    )
    parser.add_argument(
        "--min-inbound", type=int, default=DEFAULT_MIN_INBOUND,
        help=(
            "Floor on inbound-visitor count before a src can win "
            f"the best-source headline (default: {DEFAULT_MIN_INBOUND})."
        ),
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.summary:
        parser.print_help()
        return 2

    summary = summarize(
        upgrade_path=args.upgrade_path,
        share_path=args.share_path,
        min_impressions=args.min_impressions,
        min_inbound=args.min_inbound,
    )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False, default=str))
    else:
        print(format_summary_text(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
