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


# ---------------------------------------------------------------------------
# Phase 10.5 — Optimization loop: recommendations
# ---------------------------------------------------------------------------

#: Stable recommendation identifiers. Operators / dashboards can
#: pin these strings for filtering or A/B comparison; renaming any
#: of them is a breaking change.
REC_AMPLIFY_TOP_TRIGGER = "amplify_top_trigger"
REC_FEATURE_TOP_INSIGHT = "feature_top_insight"
REC_DOUBLE_DOWN_SOURCE = "double_down_best_source"
REC_TIGHTEN_FREE_LIMIT = "tighten_free_limit"
REC_INSUFFICIENT_DATA = "insufficient_data"

VALID_RECOMMENDATION_IDS: frozenset[str] = frozenset({
    REC_AMPLIFY_TOP_TRIGGER,
    REC_FEATURE_TOP_INSIGHT,
    REC_DOUBLE_DOWN_SOURCE,
    REC_TIGHTEN_FREE_LIMIT,
    REC_INSUFFICIENT_DATA,
})

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
VALID_PRIORITIES: frozenset[str] = frozenset({
    PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW,
})

#: A trigger / insight / source whose conversion rate clears this
#: bar gets a HIGH-priority recommendation. Below it, MEDIUM.
_HIGH_PRIORITY_RATE = 0.25
_MEDIUM_PRIORITY_RATE = 0.10

#: Lift threshold: ``usage_limit`` only earns the
#: ``tighten_free_limit`` recommendation when its conversion rate
#: is at least 1.5x the overall conversion rate. Prevents recommending
#: a tighter limit when usage_limit is no better than the baseline.
_TIGHTEN_LIMIT_LIFT = 1.5

#: Floor on absolute conversion rate before "tighten the free limit"
#: ever fires. Even with a 100x lift, a trigger that converts at
#: 0.1% absolute isn't a strong-enough signal to act on.
_TIGHTEN_LIMIT_RATE_FLOOR = 0.10


def _priority_for_rate(rate: float) -> str:
    if rate >= _HIGH_PRIORITY_RATE:
        return PRIORITY_HIGH
    if rate >= _MEDIUM_PRIORITY_RATE:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


def _recommend_amplify_trigger(summary: dict) -> Optional[dict]:
    headlines = summary.get("headlines", {}) or {}
    trigger = headlines.get("top_converting_trigger")
    if not trigger or not isinstance(trigger, dict):
        return None
    rate = float(trigger.get("shown_to_completed", 0.0) or 0.0)
    return {
        "id": REC_AMPLIFY_TOP_TRIGGER,
        "priority": _priority_for_rate(rate),
        "title": (
            f"Amplify the top converting trigger: "
            f"'{trigger.get('value', '?')}'"
        ),
        "rationale": (
            f"reason='{trigger.get('value', '?')}' converts at "
            f"{_fmt_rate(rate)} (shown={trigger.get('shown', 0)}, "
            f"completed={trigger.get('completed', 0)}), the highest "
            f"of any reason on the upgrade-events log."
        ),
        "action": (
            f"Surface the '{trigger.get('value', '?')}' upgrade "
            f"prompt earlier and on more endpoints. Verify the "
            f"copy variant currently rendered for that reason and "
            f"hold it constant while you scale impressions."
        ),
    }


def _recommend_feature_insight(summary: dict) -> Optional[dict]:
    headlines = summary.get("headlines", {}) or {}
    insight = headlines.get("top_performing_insight")
    if not insight or not isinstance(insight, dict):
        return None
    rate = float(insight.get("shown_to_completed", 0.0) or 0.0)
    return {
        "id": REC_FEATURE_TOP_INSIGHT,
        "priority": _priority_for_rate(rate),
        "title": (
            f"Feature the top performing insight surface: "
            f"'{insight.get('value', '?')}'"
        ),
        "rationale": (
            f"endpoint='{insight.get('value', '?')}' converts at "
            f"{_fmt_rate(rate)} (shown={insight.get('shown', 0)}, "
            f"completed={insight.get('completed', 0)}), the highest "
            f"of any insight surface on the upgrade-events log."
        ),
        "action": (
            f"Promote '{insight.get('value', '?')}' in the "
            f"dashboard navigation and email-digest links. Audit "
            f"the Phase 9.x insights rendered there and keep the "
            f"top performer pinned."
        ),
    }


def _recommend_double_down_source(summary: dict) -> Optional[dict]:
    headlines = summary.get("headlines", {}) or {}
    source = headlines.get("best_source")
    if not source or not isinstance(source, dict):
        return None
    rate = float(source.get("completion_rate", 0.0) or 0.0)
    return {
        "id": REC_DOUBLE_DOWN_SOURCE,
        "priority": _priority_for_rate(rate),
        "title": (
            f"Double down on the best inbound source: "
            f"'{source.get('src', '?')}'"
        ),
        "rationale": (
            f"src='{source.get('src', '?')}' delivered "
            f"{source.get('inbound_users', 0)} inbound visitor(s) "
            f"of whom {source.get('completed_users', 0)} converted "
            f"({_fmt_rate(rate)}), the highest completion rate of "
            f"any attributed source."
        ),
        "action": (
            f"Concentrate share-channel effort on "
            f"'{source.get('src', '?')}': add more share buttons "
            f"that pre-fill ?src={source.get('src', '?')}, and "
            f"deprioritise sources with weaker attribution."
        ),
    }


def _recommend_tighten_free_limit(summary: dict) -> Optional[dict]:
    """
    Fires when ``usage_limit`` is a strong-enough conversion driver
    that it makes sense to widen its impression footprint by
    LOWERING the free-tier daily request cap (more callers hit the
    cap, more upgrade prompts get shown, the funnel scales).

    Three guards keep this from firing on noise:

      1. ``usage_limit`` must appear in ``by_reason`` with at
         least the documented ``min_impressions`` floor (already
         enforced upstream when computing the headline);
      2. its absolute shown→completed rate must exceed
         ``_TIGHTEN_LIMIT_RATE_FLOOR``;
      3. its conversion rate must be at least
         ``_TIGHTEN_LIMIT_LIFT`` × the overall conversion rate.
    """
    by_reason = summary.get("by_reason", {}) or {}
    stats = by_reason.get("usage_limit")
    if not stats or not isinstance(stats, dict):
        return None
    rate = float(stats.get("shown_to_completed", 0.0) or 0.0)
    if rate < _TIGHTEN_LIMIT_RATE_FLOOR:
        return None

    funnel = summary.get("conversion_funnel", {}) or {}
    overall = float(funnel.get("shown_to_completed", 0.0) or 0.0)
    if overall <= 0.0:
        # No baseline to compare against — don't make a blind
        # recommendation to weaken the free tier.
        return None
    if rate < overall * _TIGHTEN_LIMIT_LIFT:
        return None

    return {
        "id": REC_TIGHTEN_FREE_LIMIT,
        "priority": PRIORITY_HIGH,
        "title": "Tighten the free-tier daily request limit",
        "rationale": (
            f"reason='usage_limit' converts at {_fmt_rate(rate)} "
            f"versus an overall {_fmt_rate(overall)} — a "
            f"{(rate / overall):.1f}x lift. Each daily-cap hit is "
            f"a high-leverage upgrade prompt."
        ),
        "action": (
            "Lower TRADING_FREE_DAILY_REQUEST_LIMIT (or "
            "TRADING_FREE_MAX_REQUESTS_PER_DAY) by 20-30% and "
            "watch the funnel for one week. The Phase 8.1 / 5.4 "
            "knobs are reversible — revert if click-through "
            "drops or churn spikes."
        ),
    }


_RECOMMENDATION_RULES = (
    _recommend_amplify_trigger,
    _recommend_feature_insight,
    _recommend_double_down_source,
    _recommend_tighten_free_limit,
)


def generate_recommendations(summary: dict) -> list[dict]:
    """
    Phase 10.5 — turn a Phase 10.4 ``summarize`` dict into a stable,
    ordered list of operator-facing recommendations.

    Deterministic: same input ⇒ same output. The rules are pure
    functions of the summary shape; they don't read the clock,
    don't read disk, don't talk to the network.

    Each recommendation has the documented schema::

        {
          "id":        str,   # stable identifier (see VALID_RECOMMENDATION_IDS)
          "priority":  str,   # "high" | "medium" | "low"
          "title":     str,   # short headline
          "rationale": str,   # why this fires (cites concrete metrics)
          "action":    str,   # what to do next
        }

    When the summary is empty / supports no rule, returns a single
    ``insufficient_data`` recommendation so the CLI never prints a
    bare "(no recommendations)" footer.
    """
    if not isinstance(summary, dict):
        summary = {}

    out: list[dict] = []
    for rule in _RECOMMENDATION_RULES:
        try:
            rec = rule(summary)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.debug(
                "growth_intel.rule_error",
                rule=getattr(rule, "__name__", "?"),
                error=str(exc),
            )
            rec = None
        if rec is None:
            continue
        # Order recommendations by priority within the stable rule
        # order. ``priority_rank`` is a private sort key — the
        # rule order is what makes the same summary always render
        # the same numbered list.
        out.append(rec)

    if not out:
        out.append({
            "id": REC_INSUFFICIENT_DATA,
            "priority": PRIORITY_LOW,
            "title": "Gather more telemetry before optimising",
            "rationale": (
                "No reason / endpoint / src cleared the noise floor "
                "needed to make a confident recommendation."
            ),
            "action": (
                "Wait until the conversion funnel has at least 5 "
                "impressions per reason and 3 inbound visitors per "
                "src, then re-run --recommend."
            ),
        })

    # Stable sort: high → medium → low while preserving rule order
    # within each priority bucket so the numbering is repeatable.
    priority_rank = {
        PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2,
    }
    out.sort(key=lambda r: priority_rank.get(r.get("priority"), 9))
    return out


def format_recommendations_text(recommendations: list[dict]) -> str:
    """Render ``generate_recommendations`` output as a numbered list."""
    bar = "=" * 72
    lines: list[str] = []
    lines.append(bar)
    lines.append("GROWTH OPTIMIZATION RECOMMENDATIONS")
    lines.append(bar)
    if not recommendations:
        lines.append("(no recommendations)")
        lines.append(bar)
        return "\n".join(lines)
    for idx, rec in enumerate(recommendations, start=1):
        lines.append(
            f"{idx}. [{str(rec.get('priority', '?')).upper()}] "
            f"{rec.get('title', '?')}"
        )
        lines.append(f"   id        : {rec.get('id', '?')}")
        lines.append(f"   rationale : {rec.get('rationale', '')}")
        lines.append(f"   action    : {rec.get('action', '')}")
        lines.append("")
    # Trim the trailing blank line before the bar.
    if lines and lines[-1] == "":
        lines.pop()
    lines.append(bar)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pretty-printing (continued)
# ---------------------------------------------------------------------------


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
        "--recommend", action="store_true",
        help=(
            "Print Phase 10.5 numbered recommendations derived from "
            "the same summary. May be combined with --summary."
        ),
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

    if not (args.summary or args.recommend):
        parser.print_help()
        return 2

    summary = summarize(
        upgrade_path=args.upgrade_path,
        share_path=args.share_path,
        min_impressions=args.min_impressions,
        min_inbound=args.min_inbound,
    )

    if args.json:
        # JSON shape rules:
        #   --summary alone   → bare summary dict (Phase 10.4 contract).
        #   --recommend alone → bare list of recommendations.
        #   both              → envelope ``{summary, recommendations}``.
        if args.summary and args.recommend:
            payload: object = {
                "summary": summary,
                "recommendations": generate_recommendations(summary),
            }
        elif args.recommend:
            payload = generate_recommendations(summary)
        else:
            payload = summary
        print(json.dumps(payload, indent=2, sort_keys=False, default=str))
        return 0

    rendered: list[str] = []
    if args.summary:
        rendered.append(format_summary_text(summary))
    if args.recommend:
        rendered.append(
            format_recommendations_text(
                generate_recommendations(summary),
            )
        )
    print("\n\n".join(rendered))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
