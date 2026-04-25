"""
Phase 10.1 — shareability layer.

Pure-stdlib helpers that decorate the existing Phase 9.1 insight
list and Phase 9.2 daily-hook with a small ``share`` field
designed to be copy-pasted into a tweet, Slack, email, or any
short-form social channel.

Share schema (every entry conforms when the helpers don't return
``None``):

    {
        "text": str,    # short, human-readable, copy-paste-ready
        "cta":  str,    # stable "Try it yourself"
        "url":  str,    # the public deployment URL
    }

Both helpers require the deployment's public URL (the same
``TRADING_PUBLIC_BASE_URL`` env var Phase 7.3 / Phase 8.3 already
use). When that env var is unset, the helpers return ``None`` and
the calling endpoint simply omits the ``share`` field — the
underlying insight / hook is unaffected.

Free vs premium: both tiers receive a ``share`` field. Premium
gets a richer one-liner that includes more context (e.g. the
percent-change figure or the dominance share); free gets the
plain-language version. Both shapes are identical so a frontend
doesn't need to branch.

This module imports nothing from FastAPI, structlog, or any
``trading_bot.*`` runtime — safely importable from CLIs, tests,
and offline analysis tools.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Stable CTA copy. Frontends can match on this string for A/B
#: testing without scraping markup.
DEFAULT_CTA = "Try it yourself"

#: Brand name surfaced in every share text. Lives in one place so a
#: rebrand only touches this module.
_BRAND_NAME = "Momentum Trading Bot"

#: Phase 9.1 insight IDs whose share copy is supported. Insights
#: with an unrecognised id never produce a share field — same
#: fail-closed posture the Phase 9.1 free-evidence allow-list uses.
_KNOWN_INSIGHT_IDS: frozenset[str] = frozenset({
    "trend.buy_delta",
    "promotion.readiness",
    "regime.dominant",
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


def _normalise_url(url: Optional[str]) -> Optional[str]:
    """Strip + drop trailing slashes; return ``None`` for empty input."""
    if not isinstance(url, str):
        return None
    s = url.strip().rstrip("/")
    return s or None


# ---------------------------------------------------------------------------
# Insight share builders — one branch per supported insight id
# ---------------------------------------------------------------------------


def _share_text_trend(evidence: dict, *, is_premium: bool) -> str:
    delta = _safe_int(evidence.get("delta"))
    direction = str(evidence.get("direction") or "")
    pct = evidence.get("percent_change")
    abs_delta = abs(delta)
    if direction == "up":
        verb = "up"
    elif direction == "down":
        verb = "down"
    else:
        verb = "unchanged"

    if not is_premium:
        if direction in ("up", "down"):
            return f"Buy signals {verb} {abs_delta} vs prior day on {_BRAND_NAME}."
        return f"Buy signals unchanged vs prior day on {_BRAND_NAME}."

    # Premium copy carries the percent-change context when available
    # so the share is a stronger social hook.
    pct_part = ""
    if isinstance(pct, (int, float)) and direction in ("up", "down"):
        sign = "+" if direction == "up" else "-"
        pct_part = f" ({sign}{abs(round(_safe_float(pct), 1))}%)"
    if direction in ("up", "down"):
        return (
            f"{_BRAND_NAME}: buy signals {verb} {abs_delta} vs prior day"
            f"{pct_part}."
        )
    return f"{_BRAND_NAME}: buy signals unchanged vs prior day."


def _share_text_readiness(evidence: dict, *, is_premium: bool) -> str:
    ready = bool(evidence.get("ready"))
    days = _safe_int(evidence.get("consecutive_passing_days"))
    if not is_premium:
        if ready:
            return (
                f"{_BRAND_NAME} promotion gate is GREEN after "
                f"{days} consecutive passing day(s)."
            )
        if days > 0:
            return (
                f"{_BRAND_NAME} on a {days}-day passing streak — "
                "promotion gate still pending."
            )
        return f"{_BRAND_NAME} promotion gate is currently blocked."
    # Premium adds the ready/blocked label in plain language.
    if ready:
        return (
            f"{_BRAND_NAME}: promotion gate READY after {days} "
            "consecutive passing day(s)."
        )
    if days > 0:
        return (
            f"{_BRAND_NAME}: building toward promotion — "
            f"{days} consecutive passing day(s)."
        )
    return (
        f"{_BRAND_NAME}: promotion gate BLOCKED — "
        "no passing streak yet."
    )


def _share_text_regime(evidence: dict, *, is_premium: bool) -> str:
    regime = str(evidence.get("regime") or "")
    hits = _safe_int(evidence.get("hits"))
    share = evidence.get("share")
    if not regime:
        return f"{_BRAND_NAME}: regime data unavailable."
    if not is_premium:
        return (
            f"{_BRAND_NAME}: '{regime}' regime led today's signals."
        )
    pct_part = ""
    if isinstance(share, (int, float)):
        pct_part = f" ({round(_safe_float(share) * 100, 1)}% share)"
    return (
        f"{_BRAND_NAME}: '{regime}' regime dominated with {hits} hits"
        f"{pct_part}."
    )


_INSIGHT_TEXT_BUILDERS = {
    "trend.buy_delta": _share_text_trend,
    "promotion.readiness": _share_text_readiness,
    "regime.dominant": _share_text_regime,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_insight_share(
    insight: Optional[dict],
    *,
    is_premium: bool,
    base_url: Optional[str],
) -> Optional[dict]:
    """
    Build a ``share`` payload for one Phase 9.1 insight.

    Returns ``None`` when:
      * ``insight`` isn't a dict
      * the insight's ``id`` is unrecognised
      * ``base_url`` is empty / unset (no public URL → no share)
    """
    if not isinstance(insight, dict):
        return None
    rule_id = insight.get("id")
    if rule_id not in _KNOWN_INSIGHT_IDS:
        return None
    builder = _INSIGHT_TEXT_BUILDERS.get(rule_id)
    if builder is None:
        return None
    url = _normalise_url(base_url)
    if not url:
        return None
    evidence = insight.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    text = builder(evidence, is_premium=bool(is_premium))
    return {"text": text, "cta": DEFAULT_CTA, "url": url}


def build_daily_hook_share(
    hook: Optional[dict],
    *,
    is_premium: bool,
    base_url: Optional[str],
) -> Optional[dict]:
    """
    Build a ``share`` payload for the Phase 9.2 daily hook.

    Returns ``None`` when ``hook`` isn't a dict or ``base_url`` is
    empty / unset.
    """
    if not isinstance(hook, dict):
        return None
    url = _normalise_url(base_url)
    if not url:
        return None
    change = str(hook.get("change") or "")
    magnitude = _safe_int(hook.get("magnitude"))
    if not is_premium:
        if change == "up":
            text = (
                f"Buy signals up {magnitude} vs prior day "
                f"on {_BRAND_NAME}."
            )
        elif change == "down":
            text = (
                f"Buy signals down {magnitude} vs prior day "
                f"on {_BRAND_NAME}."
            )
        else:
            text = (
                f"Buy signals unchanged vs prior day on {_BRAND_NAME}."
            )
    else:
        # Premium carries an explicit headline when the hook
        # already provides one, falling back to the generated copy.
        headline = str(hook.get("headline") or "")
        if change == "up":
            tail = "trending up"
        elif change == "down":
            tail = "trending down"
        else:
            tail = "holding flat"
        if headline:
            text = f"{_BRAND_NAME}: {headline} ({tail})."
        else:
            text = f"{_BRAND_NAME}: buy signals {tail}."
    return {"text": text, "cta": DEFAULT_CTA, "url": url}
