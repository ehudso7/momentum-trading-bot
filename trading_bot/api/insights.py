"""
Phase 9.1 — actionable, deterministic insight builder.

Pure-stdlib helper that takes a sanitised daily report (and the
previous day's report when available) and returns an ordered list
of small "insight" dicts suitable for surfacing in API responses
and the dashboard. Every output is a function of the inputs only
— no I/O, no Stripe, no time-of-day tricks — so the same inputs
produce the same outputs across processes.

Insight shape (every entry conforms):

    {
        "id":         str,                   # short stable identifier
        "title":      str,                   # one-line headline
        "summary":    str,                   # 1-2 sentence explanation
        "confidence": float,                 # 0.0..1.0 inclusive
        "severity":   "info"|"warn"|"critical",
        "evidence":   dict,                  # supporting numbers
        "action":     str,                   # operator next-step hint
    }

The premium tier receives the full list; the free tier receives at
most ``FREE_INSIGHT_LIMIT`` insights with ``evidence`` projected
through a per-rule allow-list (so deep stats stay premium).

This module imports nothing from FastAPI, structlog, or any
``trading_bot.*`` runtime — it is safely importable from CLIs,
tests, and offline analysis tools.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

INSIGHT_TREND_BUY_DELTA = "trend.buy_delta"
INSIGHT_READINESS = "promotion.readiness"
INSIGHT_REGIME_DOMINANT = "regime.dominant"

#: Documented (id, severity)-set for callers / tests / docs.
KNOWN_INSIGHT_IDS: frozenset[str] = frozenset({
    INSIGHT_TREND_BUY_DELTA,
    INSIGHT_READINESS,
    INSIGHT_REGIME_DOMINANT,
})

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"
VALID_SEVERITIES: frozenset[str] = frozenset({
    SEVERITY_INFO, SEVERITY_WARN, SEVERITY_CRITICAL,
})

#: Free callers receive at most this many insights.
FREE_INSIGHT_LIMIT = 2

#: Per-insight allow-list of evidence keys that survive the free
#: projection. Anything outside the allow-list is dropped before
#: the insight is emitted to a free caller. Premium callers see
#: the full evidence dict.
_FREE_EVIDENCE_ALLOWLIST: dict[str, frozenset[str]] = {
    INSIGHT_TREND_BUY_DELTA: frozenset({"delta", "direction"}),
    INSIGHT_READINESS: frozenset({"ready"}),
    INSIGHT_REGIME_DOMINANT: frozenset({"regime"}),
}


# ---------------------------------------------------------------------------
# Pure-helper utilities (small enough to inline per-rule)
# ---------------------------------------------------------------------------


def _safe_int(value, default: int = 0) -> int:
    """Coerce to int; on any failure return ``default``."""
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
    """Clamp into [0.0, 1.0]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# ---------------------------------------------------------------------------
# Rule a) trend — day-over-day delta on totals.buy_rows
# ---------------------------------------------------------------------------


def _trend_insight(
    report: dict, prev_report: Optional[dict],
) -> Optional[dict]:
    """
    Compare today's buy_rows against yesterday's. Skipped when
    either side is missing the ``totals.buy_rows`` field — the
    insight only fires when there's an honest comparison to make.
    """
    if not isinstance(prev_report, dict):
        return None

    curr_totals = report.get("totals")
    prev_totals = prev_report.get("totals")
    if not isinstance(curr_totals, dict) or not isinstance(prev_totals, dict):
        return None
    if "buy_rows" not in curr_totals or "buy_rows" not in prev_totals:
        return None

    curr_n = _safe_int(curr_totals.get("buy_rows"))
    prev_n = _safe_int(prev_totals.get("buy_rows"))
    delta = curr_n - prev_n

    if delta > 0:
        direction = "up"
        severity = SEVERITY_INFO
        title = "Buy volume up vs prior day"
        summary = (
            f"Buys advanced {delta} → {curr_n} (was {prev_n})."
        )
        action = "monitor for sustained improvement"
    elif delta < 0:
        direction = "down"
        # A drop of more than half the prior day's buys warrants
        # operator attention; otherwise it's informational.
        if prev_n > 0 and abs(delta) >= max(1, prev_n // 2):
            severity = SEVERITY_WARN
        else:
            severity = SEVERITY_INFO
        title = "Buy volume down vs prior day"
        summary = (
            f"Buys retreated {abs(delta)} → {curr_n} (was {prev_n})."
        )
        action = "review filter regime — fewer buys today"
    else:
        direction = "flat"
        severity = SEVERITY_INFO
        title = "Buy volume unchanged vs prior day"
        summary = f"Buys held at {curr_n}."
        action = "no change required"

    # Confidence rises with the absolute delta relative to the
    # prior-day baseline, capped at 1.0. A near-zero baseline
    # collapses to a fixed 0.5 so the insight is still emitted but
    # marked as low-confidence.
    if prev_n <= 0:
        confidence = 0.5 if delta != 0 else 0.3
    else:
        confidence = _clamp_unit(abs(delta) / float(prev_n))

    percent_change = (
        round(100.0 * delta / prev_n, 2) if prev_n > 0 else None
    )

    evidence: dict = {
        "delta": delta,
        "direction": direction,
        "curr_buy_rows": curr_n,
        "prev_buy_rows": prev_n,
        "percent_change": percent_change,
    }
    return _make_insight(
        id_=INSIGHT_TREND_BUY_DELTA,
        title=title,
        summary=summary,
        confidence=confidence,
        severity=severity,
        evidence=evidence,
        action=action,
    )


# ---------------------------------------------------------------------------
# Rule b) readiness — promotion_readiness.ready / consecutive_passing_days
# ---------------------------------------------------------------------------


def _readiness_insight(report: dict) -> Optional[dict]:
    pr = report.get("promotion_readiness")
    if not isinstance(pr, dict):
        return None

    ready = bool(pr.get("ready"))
    consec = _safe_int(pr.get("consecutive_passing_days"))

    if ready:
        severity = SEVERITY_INFO
        title = "Promotion readiness: ready"
        summary = (
            f"Promotion gate green after "
            f"{consec} consecutive passing day(s)."
        )
        action = "ready for live deployment review"
        confidence = 1.0
    elif consec > 0:
        severity = SEVERITY_INFO
        title = "Promotion readiness: building"
        summary = (
            f"{consec} consecutive passing day(s) — promotion gate "
            f"still pending."
        )
        action = "continue accumulating consecutive passing days"
        # Confidence climbs as the streak grows, capped before "ready".
        confidence = _clamp_unit(0.4 + 0.05 * min(consec, 12))
    else:
        # Either explicitly blocked or no streak — both surface as
        # warn so the operator sees the negative state.
        severity = SEVERITY_WARN
        title = "Promotion readiness: blocked"
        summary = (
            "Promotion gate is blocked — no consecutive passing "
            "day streak."
        )
        action = "investigate guardrails before live promotion"
        confidence = 0.8

    evidence: dict = {
        "ready": ready,
        "consecutive_passing_days": consec,
    }
    return _make_insight(
        id_=INSIGHT_READINESS,
        title=title,
        summary=summary,
        confidence=confidence,
        severity=severity,
        evidence=evidence,
        action=action,
    )


# ---------------------------------------------------------------------------
# Rule c) regime — dominant regime by hits (when regime_stats present)
# ---------------------------------------------------------------------------


def _regime_insight(report: dict) -> Optional[dict]:
    regime_stats = report.get("regime_stats")
    if not isinstance(regime_stats, dict) or not regime_stats:
        return None

    # Pull (regime, hits) pairs. Stats can be either a flat int
    # ({"trending": 12}) or a nested dict ({"trending": {"hits": 12}}).
    pairs: list[tuple[str, int]] = []
    for name, value in regime_stats.items():
        if isinstance(value, dict):
            hits = _safe_int(value.get("hits"))
        else:
            hits = _safe_int(value)
        if hits > 0:
            pairs.append((str(name), hits))

    if not pairs:
        return None

    pairs.sort(key=lambda kv: (-kv[1], kv[0]))
    dominant_name, dominant_hits = pairs[0]
    runner_up = pairs[1] if len(pairs) > 1 else None
    total_hits = sum(h for _, h in pairs)

    share = dominant_hits / total_hits if total_hits else 0.0
    title = f"Dominant regime: {dominant_name}"
    if runner_up is None:
        summary = (
            f"Regime '{dominant_name}' was the only one with "
            f"qualifying activity ({dominant_hits} hits)."
        )
    else:
        summary = (
            f"Regime '{dominant_name}' led with {dominant_hits} "
            f"hits ({round(share * 100, 1)}% share); next was "
            f"'{runner_up[0]}' at {runner_up[1]}."
        )

    severity = SEVERITY_INFO
    action = (
        f"current regime is '{dominant_name}' — "
        "tune entries to match"
    )
    # Confidence = dominance share, clamped.
    confidence = _clamp_unit(share)

    evidence: dict = {
        "regime": dominant_name,
        "hits": dominant_hits,
        "share": round(share, 4) if total_hits else 0.0,
        "runner_up": (
            {"regime": runner_up[0], "hits": runner_up[1]}
            if runner_up else None
        ),
        "total_hits": total_hits,
    }
    return _make_insight(
        id_=INSIGHT_REGIME_DOMINANT,
        title=title,
        summary=summary,
        confidence=confidence,
        severity=severity,
        evidence=evidence,
        action=action,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _make_insight(
    *,
    id_: str,
    title: str,
    summary: str,
    confidence: float,
    severity: str,
    evidence: dict,
    action: str,
) -> dict:
    """Defensive factory — guarantees the documented schema."""
    if severity not in VALID_SEVERITIES:
        severity = SEVERITY_INFO
    return {
        "id": str(id_),
        "title": str(title),
        "summary": str(summary),
        "confidence": _clamp_unit(_safe_float(confidence)),
        "severity": severity,
        "evidence": dict(evidence),
        "action": str(action),
    }


def build_insights(
    report: Optional[dict],
    prev_report: Optional[dict] = None,
) -> list[dict]:
    """
    Build a deterministic, ordered list of insights from one (or
    two) sanitised daily reports.

    Order is fixed per call regardless of report contents:
      1. trend  (only when ``prev_report`` provides a comparable totals.buy_rows)
      2. readiness  (only when ``promotion_readiness`` is in the report)
      3. regime  (only when ``regime_stats`` is in the report)

    The premium consumer should display all returned insights; the
    free consumer should call ``truncate_for_free`` on the list
    before serialising.
    """
    if not isinstance(report, dict):
        return []

    insights: list[dict] = []
    rule_outputs = (
        _trend_insight(report, prev_report),
        _readiness_insight(report),
        _regime_insight(report),
    )
    for entry in rule_outputs:
        if entry is not None:
            insights.append(entry)
    return insights


def truncate_for_free(
    insights: list[dict],
    *,
    max_count: int = FREE_INSIGHT_LIMIT,
) -> list[dict]:
    """
    Return a defensive copy of ``insights`` with at most
    ``max_count`` entries; for each kept entry, project ``evidence``
    through the documented per-rule free-tier allow-list.

    Insights with an unrecognised ``id`` are dropped entirely (so a
    future insight rule cannot accidentally surface to free callers
    until its allow-list is added). Inputs are never mutated.
    """
    if max_count <= 0 or not isinstance(insights, list):
        return []
    kept: list[dict] = []
    for entry in insights:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("id")
        allow = _FREE_EVIDENCE_ALLOWLIST.get(rule_id)
        if allow is None:
            continue
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        new_evidence = {k: v for k, v in evidence.items() if k in allow}
        new_entry = dict(entry)
        new_entry["evidence"] = new_evidence
        kept.append(new_entry)
        if len(kept) >= max_count:
            break
    return kept
