"""
Phase 9.2 — daily-hook builder.

Pure-stdlib helper that turns the existing daily report + the
prior day's report + the Phase 9.1 insight list into a single
"what changed" hook. Designed to drive repeat usage: an
integrator's UI / the dashboard banner can render the hook
without any further computation.

Hook schema (every entry conforms when ``build_daily_hook`` does
not return ``None``):

    {
        "headline":   str,                   # human-readable tagline
        "change":     "up"|"down"|"flat",    # direction of change
        "magnitude":  int|float,             # absolute change (units depend on driver)
        "confidence": float,                 # clamped to [0.0, 1.0]
        "since":      str|None,              # ISO date of the prior report
        "driver":     str,                   # which signal drove the hook
        "cta":        str,                   # short next-step prompt
    }

Logic order:

  1. Prefer the Phase 9.1 ``trend.buy_delta`` insight — it already
     carries direction + confidence + delta in evidence.
  2. Fall back to a direct ``totals.buy_rows`` delta when both
     reports expose it. Confidence collapses to a fixed
     low-medium value because there's no insight-level scoring.
  3. Return ``None`` when there is no prior report or both
     fallbacks miss — the dashboard / API response then simply
     omits the hook field.

Tier-aware projection: ``truncate_for_free`` returns a slimmed
shape with only ``headline`` / ``change`` / ``magnitude`` / ``cta``
/ ``since`` so the free response stays curated. Premium callers
receive the full hook including ``confidence`` and ``driver``.

This module imports nothing from FastAPI, structlog, or any
``trading_bot.*`` runtime — it is safely importable from CLIs,
tests, and offline analysis tools.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CHANGE_UP = "up"
CHANGE_DOWN = "down"
CHANGE_FLAT = "flat"
VALID_CHANGES: frozenset[str] = frozenset({CHANGE_UP, CHANGE_DOWN, CHANGE_FLAT})

#: Stable ID match for the Phase 9.1 trend insight.
_INSIGHT_TREND_BUY_DELTA = "trend.buy_delta"

#: Driver constants — surface in the ``driver`` field so a
#: dashboard / integrator can branch on which signal produced the hook.
DRIVER_TREND_INSIGHT = "trend.buy_delta"
DRIVER_TOTALS_FALLBACK = "totals.buy_rows"

#: CTA copy stays in one place so dashboards / integrators can rely
#: on a stable constant for matching / A/B testing.
DEFAULT_CTA = "Open the dashboard for the full breakdown"

#: When falling back to the direct totals delta (no trend insight),
#: confidence collapses to this fixed value — operators can still
#: tell at a glance that the hook is less rigorously derived than
#: an insight-driven one.
_FALLBACK_CONFIDENCE = 0.4

#: Free callers receive only this subset of hook keys.
_FREE_HOOK_ALLOWLIST: frozenset[str] = frozenset({
    "headline", "change", "magnitude", "since", "cta",
})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_unit(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _direction_from_delta(delta: int) -> str:
    if delta > 0:
        return CHANGE_UP
    if delta < 0:
        return CHANGE_DOWN
    return CHANGE_FLAT


def _format_headline(direction: str, magnitude: int) -> str:
    """Stable human-readable tagline keyed on the direction +
    absolute magnitude. Pure function — no time-of-day or locale
    surprises."""
    if direction == CHANGE_UP:
        return f"Buys up {magnitude} vs prior day"
    if direction == CHANGE_DOWN:
        return f"Buys down {magnitude} vs prior day"
    return "Buys unchanged vs prior day"


# ---------------------------------------------------------------------------
# Internal builders — one per source signal
# ---------------------------------------------------------------------------


def _hook_from_trend_insight(
    insight: dict,
    prev_report: dict,
) -> Optional[dict]:
    """Source 1 — the Phase 9.1 trend insight."""
    evidence = insight.get("evidence")
    if not isinstance(evidence, dict):
        return None
    if "delta" not in evidence or "direction" not in evidence:
        return None

    delta = _safe_int(evidence.get("delta"))
    direction = str(evidence.get("direction"))
    if direction not in VALID_CHANGES:
        return None

    confidence = _clamp_unit(_safe_float(insight.get("confidence")))
    since = prev_report.get("report_date")
    since = str(since) if since else None
    return {
        "headline": _format_headline(direction, abs(delta)),
        "change": direction,
        "magnitude": abs(delta),
        "confidence": confidence,
        "since": since,
        "driver": DRIVER_TREND_INSIGHT,
        "cta": DEFAULT_CTA,
    }


def _hook_from_totals(
    report: dict, prev_report: dict,
) -> Optional[dict]:
    """Source 2 — direct ``totals.buy_rows`` delta. Used when the
    trend insight is unavailable but both reports have totals."""
    curr_totals = report.get("totals")
    prev_totals = prev_report.get("totals")
    if not isinstance(curr_totals, dict) or not isinstance(prev_totals, dict):
        return None
    if "buy_rows" not in curr_totals or "buy_rows" not in prev_totals:
        return None

    curr_n = _safe_int(curr_totals.get("buy_rows"))
    prev_n = _safe_int(prev_totals.get("buy_rows"))
    delta = curr_n - prev_n
    direction = _direction_from_delta(delta)
    since = prev_report.get("report_date")
    since = str(since) if since else None
    return {
        "headline": _format_headline(direction, abs(delta)),
        "change": direction,
        "magnitude": abs(delta),
        "confidence": _FALLBACK_CONFIDENCE,
        "since": since,
        "driver": DRIVER_TOTALS_FALLBACK,
        "cta": DEFAULT_CTA,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_daily_hook(
    report: Optional[dict],
    prev_report: Optional[dict],
    insights: Optional[list[dict]] = None,
) -> Optional[dict]:
    """
    Build the day-over-day "what changed" hook from the existing
    Phase 4.0 sanitised report, the prior-day report, and the
    Phase 9.1 insight list.

    Returns ``None`` when there is no prior report or when neither
    signal source has enough data — the caller should then simply
    omit the ``daily_hook`` field from its response.
    """
    if not isinstance(report, dict) or not isinstance(prev_report, dict):
        return None

    # Source 1 — preferred.
    if isinstance(insights, list):
        for entry in insights:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") != _INSIGHT_TREND_BUY_DELTA:
                continue
            hook = _hook_from_trend_insight(entry, prev_report)
            if hook is not None:
                return hook
            # Trend insight was malformed — fall through to source 2.
            break

    # Source 2 — fallback.
    return _hook_from_totals(report, prev_report)


def truncate_for_free(hook: Optional[dict]) -> Optional[dict]:
    """
    Project a hook through the free-tier allow-list. Drops
    ``confidence`` and ``driver`` so they stay premium. Returns
    ``None`` for ``None`` input so the caller can safely chain
    this on a possibly-missing hook.
    """
    if not isinstance(hook, dict):
        return None
    return {k: v for k, v in hook.items() if k in _FREE_HOOK_ALLOWLIST}
