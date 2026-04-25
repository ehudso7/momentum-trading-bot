"""
Phase 9.3 — stickiness loop: streak + missed-day nudge.

Two pure-stdlib helpers that derive retention signals from the
existing daily report and (optionally) the prior-day report.
Everything is computed per request — no persistence, no clock
reads outside of the supplied report dates.

Streak schema (when ``build_streak`` does not return ``None``):

    {
        "days":           int,    # consecutive passing days from
                                  # promotion_readiness
        "label":          str,    # "5-day passing streak"
        "milestone":      bool,   # True iff days hits a milestone
        "next_milestone": int,    # next milestone the streak is
                                  # heading toward (premium only)
    }

Nudge schema (when ``build_nudge`` does not return ``None``):

    {
        "kind":         "missed_day",  # only kind in 9.3
        "headline":     str,           # "You missed 2 day(s) of reports."
        "days_missed":  int,           # gap - 1
        "since":        str,           # ISO date of the prior report
        "cta":          str,           # short next-step prompt
    }

Both helpers return ``None`` when the input doesn't justify a
banner — the caller then simply omits the field from its
response.

This module imports nothing from FastAPI, structlog, or any
``trading_bot.*`` runtime — safely importable from CLIs, tests,
and offline analysis tools.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Optional


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

NUDGE_MISSED_DAY = "missed_day"
VALID_NUDGE_KINDS: frozenset[str] = frozenset({NUDGE_MISSED_DAY})

#: Stable milestone ladder. Operators / dashboards can rely on
#: these specific values for celebratory styling.
MILESTONE_DAYS: tuple[int, ...] = (3, 5, 7, 10, 14, 21, 30, 60, 90, 180, 365)

#: CTA copy lives in one place so dashboards / integrators can
#: rely on a stable constant for matching / A/B testing.
NUDGE_CTA = "Open the dashboard to see what changed while you were away"

#: Free-tier allow-list for streak fields. ``next_milestone`` is
#: a forward-looking signal that stays premium.
_FREE_STREAK_ALLOWLIST: frozenset[str] = frozenset({
    "days", "label", "milestone",
})

#: Free-tier allow-list for nudge fields. Everything is
#: user-facing — no premium-only data on this surface today.
_FREE_NUDGE_ALLOWLIST: frozenset[str] = frozenset({
    "kind", "headline", "days_missed", "since", "cta",
})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_date(value) -> Optional[_date]:
    """Parse a YYYY-MM-DD string. Returns ``None`` on any failure."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) < 10:
        return None
    try:
        return _date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


def _next_milestone_after(days: int) -> int:
    """Return the next milestone strictly greater than ``days``.
    When ``days`` already exceeds the highest documented milestone,
    extend by 30-day increments so the streak always has a target
    to chase."""
    for m in MILESTONE_DAYS:
        if days < m:
            return m
    # Past 365 — keep adding 30-day rungs.
    largest = MILESTONE_DAYS[-1]
    rungs_past = ((days - largest) // 30) + 1
    return largest + 30 * rungs_past


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_streak(
    report: Optional[dict],
    prev_report: Optional[dict] = None,  # noqa: ARG001 — accepted for symmetry
) -> Optional[dict]:
    """
    Build a streak record from ``report.promotion_readiness``.

    Returns ``None`` when:
      * the report is missing / not a dict
      * ``promotion_readiness`` is missing / not a dict
      * ``consecutive_passing_days`` is zero or negative

    The ``prev_report`` argument is accepted for signature
    symmetry with ``build_daily_hook`` and ``build_nudge`` but is
    not currently consulted — the report's own
    ``consecutive_passing_days`` field is the source of truth.
    """
    if not isinstance(report, dict):
        return None
    pr = report.get("promotion_readiness")
    if not isinstance(pr, dict):
        return None
    days = _safe_int(pr.get("consecutive_passing_days"))
    if days <= 0:
        return None

    is_milestone = days in MILESTONE_DAYS
    next_target = _next_milestone_after(days)
    label = (
        f"{days}-day passing streak"
        if days != 1 else "1-day passing streak"
    )
    return {
        "days": days,
        "label": label,
        "milestone": is_milestone,
        "next_milestone": next_target,
    }


def build_nudge(
    report: Optional[dict],
    prev_report: Optional[dict],
) -> Optional[dict]:
    """
    Build a missed-day nudge from the gap between the current and
    prior report dates.

    Returns ``None`` when:
      * either report is missing / not a dict
      * either ``report_date`` cannot be parsed as YYYY-MM-DD
      * the gap is exactly 1 day (the normal cadence)
      * the prior date is the same as / after the current date
        (out-of-order or duplicate; treat as no nudge)

    Otherwise produces a ``missed_day`` nudge whose
    ``days_missed`` is the number of intervening dates the user
    didn't see.
    """
    if not isinstance(report, dict) or not isinstance(prev_report, dict):
        return None
    curr_date = _parse_iso_date(report.get("report_date"))
    prev_date = _parse_iso_date(prev_report.get("report_date"))
    if curr_date is None or prev_date is None:
        return None
    delta_days = (curr_date - prev_date).days
    # Normal daily cadence (or out-of-order / duplicate dates) =>
    # no nudge. The missed-day rule fires only when there's a
    # genuine gap of two or more days.
    if delta_days <= 1:
        return None
    days_missed = delta_days - 1
    plural = "" if days_missed == 1 else "s"
    headline = (
        f"You missed {days_missed} day{plural} of reports."
    )
    return {
        "kind": NUDGE_MISSED_DAY,
        "headline": headline,
        "days_missed": days_missed,
        "since": prev_date.isoformat(),
        "cta": NUDGE_CTA,
    }


def truncate_streak_for_free(streak: Optional[dict]) -> Optional[dict]:
    """
    Project a streak through the free-tier allow-list. Drops
    ``next_milestone`` (the forward-looking premium signal). ``None``
    passes through unchanged so the caller can chain safely on a
    possibly-missing streak.
    """
    if not isinstance(streak, dict):
        return None
    return {k: v for k, v in streak.items() if k in _FREE_STREAK_ALLOWLIST}


def truncate_nudge_for_free(nudge: Optional[dict]) -> Optional[dict]:
    """
    Project a nudge through the free-tier allow-list. The current
    Phase 9.3 nudge schema is already entirely user-facing, so this
    helper is a defensive copy that future-proofs against new
    premium-only fields without churning the call sites.
    """
    if not isinstance(nudge, dict):
        return None
    return {k: v for k, v in nudge.items() if k in _FREE_NUDGE_ALLOWLIST}
