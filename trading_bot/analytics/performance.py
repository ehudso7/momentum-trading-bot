"""
Honest live performance-analytics scorecard.

This module turns a list of closed trades (from ``data/journal.csv``) plus an
optional equity curve into a *conservative* performance scorecard.

Design principle (non-negotiable): this scorecard exists to tell the truth
about whether the strategy has an edge. It must never flatter the results and
must loudly signal when the sample is too small to trust. When there are zero
trades it returns a well-formed, zeroed struct — never fabricated or
extrapolated numbers.

The core statistical formulas (win_rate / profit_factor / expectancy / Sharpe /
Sortino / max drawdown) mirror the reference implementations in
``trading_bot.backtest.engine._compute_metrics`` and
``trading_bot.risk.advanced_analytics.AdvancedRiskAnalytics`` — but this module
is a pure, synchronous, dependency-light helper suitable for calling from the
dashboard request path.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

# A widely used rule-of-thumb minimum sample before per-trade statistics
# (win rate, expectancy, profit factor) carry any real signal. Below this we
# treat every metric as *indicative only*.
MIN_SAMPLE_FOR_CONFIDENCE = 30

# Trading days per year — annualisation factor for Sharpe/Sortino, matching the
# convention used in the backtest engine and advanced analytics module.
_TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Parsing helpers (journal rows read from CSV arrive as strings)
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> Optional[float]:
    """Best-effort float parse. Returns ``None`` for empty/invalid values."""
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _equity_value(point: Any) -> Optional[float]:
    """Extract the equity float from a (timestamp, equity) tuple or a dict."""
    if isinstance(point, dict):
        return _to_float(point.get("equity"))
    if isinstance(point, (tuple, list)) and len(point) >= 2:
        return _to_float(point[1])
    # A bare scalar is treated as the equity value itself.
    return _to_float(point)


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def _max_drawdown_pct(equity_values: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown of an equity series, as a positive %."""
    peak = -math.inf
    max_dd = 0.0
    for value in equity_values:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
    return round(max_dd * 100.0, 4)


# ---------------------------------------------------------------------------
# Core per-group statistics
# ---------------------------------------------------------------------------


def _core_stats(pnls: Sequence[float], rrs: Sequence[float]) -> dict[str, Any]:
    """Compute the honest core statistics for a set of trade PnLs.

    ``rrs`` is the subset of realised R-multiples (rr_ratio) that were present
    on the trades — it may be shorter than ``pnls`` when some rows omit it.
    """
    n = len(pnls)
    if n == 0:
        return {
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "total_pnl": 0.0,
            "expectancy_per_trade": 0.0,
            "expectancy_r": None,
            "avg_rr": None,
        }

    winning = [p for p in pnls if p > 0]
    losing = [p for p in pnls if p < 0]

    gross_profit = float(sum(winning))
    gross_loss = float(abs(sum(losing)))
    total_pnl = float(sum(pnls))

    # Profit factor is undefined without any losing trades — return None rather
    # than a flattering ``inf`` so the caller must handle the small-sample case.
    profit_factor: Optional[float]
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 4)
    else:
        profit_factor = None

    avg_rr = round(sum(rrs) / len(rrs), 4) if rrs else None

    return {
        "closed_trades": n,
        "wins": len(winning),
        "losses": len(losing),
        "win_rate": round(len(winning) / n, 4),
        "avg_win": round(gross_profit / len(winning), 4) if winning else 0.0,
        "avg_loss": round(sum(losing) / len(losing), 4) if losing else 0.0,
        "largest_win": round(max(winning), 4) if winning else 0.0,
        "largest_loss": round(min(losing), 4) if losing else 0.0,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": profit_factor,
        "total_pnl": round(total_pnl, 4),
        "expectancy_per_trade": round(total_pnl / n, 4),
        # Expectancy in R and average R are both derived from realised
        # rr_ratio; expressed per trade they coincide, but both are surfaced
        # because callers reason about them separately.
        "expectancy_r": avg_rr,
        "avg_rr": avg_rr,
    }


def _sharpe_sortino(returns: Sequence[float]) -> tuple[float, float]:
    """Annualised Sharpe and Sortino ratios from per-trade returns.

    Guards std == 0 -> 0.0 (never divide-by-zero, never fabricate a ratio).
    Requires at least two returns to have a meaningful dispersion.
    """
    n = len(returns)
    if n < 2:
        return 0.0, 0.0

    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / n
    std = math.sqrt(variance)

    sharpe = (mean / std) * math.sqrt(_TRADING_DAYS) if std > 0 else 0.0

    downside = [r for r in returns if r < 0]
    if downside:
        d_var = sum(r ** 2 for r in downside) / n
        d_std = math.sqrt(d_var)
        sortino = (mean / d_std) * math.sqrt(_TRADING_DAYS) if d_std > 0 else 0.0
    else:
        # No losing trades in-sample: downside deviation is zero. Honest choice
        # is 0.0 rather than an infinite ratio that would flatter the strategy.
        sortino = 0.0

    return round(sharpe, 4), round(sortino, 4)


def _confidence_note(closed: int) -> str:
    """Human-readable honesty banner keyed off the closed-trade count."""
    if closed == 0:
        return "No closed trades yet — the strategy has not entered a position."
    if closed < MIN_SAMPLE_FOR_CONFIDENCE:
        return (
            f"{closed}/{MIN_SAMPLE_FOR_CONFIDENCE} trades — metrics are "
            "indicative only, not yet statistically meaningful."
        )
    return (
        f"{closed} closed trades meet the {MIN_SAMPLE_FOR_CONFIDENCE}-trade "
        "minimum — metrics are statistically meaningful, but keep monitoring "
        "for regime changes and sample drift."
    )


def _empty_scorecard(starting_equity: float) -> dict[str, Any]:
    """Well-formed, fully-zeroed struct for the zero-trade case."""
    return {
        "trade_count": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "profit_factor": None,
        "expectancy_per_trade": 0.0,
        "expectancy_r": None,
        "avg_rr": None,
        "total_pnl": 0.0,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "avg_hold_minutes": 0.0,
        "starting_equity": round(float(starting_equity), 4),
        "by_setup": {},
        "sample_size": 0,
        "min_sample_for_confidence": MIN_SAMPLE_FOR_CONFIDENCE,
        "is_statistically_significant": False,
        "confidence_note": _confidence_note(0),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_performance(
    trades: Iterable[dict[str, Any]],
    equity_curve: Optional[Sequence[Any]] = None,
    starting_equity: float = 100_000.0,
) -> dict[str, Any]:
    """Compute an honest performance scorecard from closed trades.

    Args:
        trades: Closed-trade rows (dicts) with at least a ``pnl`` field, plus
            optional ``rr_ratio``, ``hold_time_minutes`` and ``signal_type``.
            Values may be strings (as read from the CSV journal) or numbers.
        equity_curve: Optional ordered equity series used for drawdown. Each
            point may be a ``(timestamp, equity)`` tuple or a
            ``{"timestamp", "equity"}`` dict. When absent, drawdown is derived
            from the cumulative trade PnL curve seeded at ``starting_equity``.
        starting_equity: Account equity at the start of the measured period.

    Returns:
        A JSON-serialisable scorecard dict. Small samples are surfaced via
        ``is_statistically_significant`` and ``confidence_note``; numbers are
        never fabricated or extrapolated.
    """
    starting_equity = float(starting_equity)
    trade_list = list(trades) if trades is not None else []

    # A trade only counts as "closed" for statistics when it carries a numeric
    # pnl. Rows missing pnl are still counted in the raw ``trade_count`` for
    # transparency but excluded from the edge math.
    parsed: list[dict[str, Any]] = []
    for row in trade_list:
        pnl = _to_float(row.get("pnl"))
        if pnl is None:
            continue
        parsed.append(
            {
                "pnl": pnl,
                "rr_ratio": _to_float(row.get("rr_ratio")),
                "hold_time_minutes": _to_float(row.get("hold_time_minutes")),
                "signal_type": (str(row.get("signal_type")).strip()
                                if row.get("signal_type") not in (None, "")
                                else "unknown"),
            }
        )

    trade_count = len(trade_list)

    if not parsed:
        card = _empty_scorecard(starting_equity)
        card["trade_count"] = trade_count
        return card

    pnls = [t["pnl"] for t in parsed]
    rrs = [t["rr_ratio"] for t in parsed if t["rr_ratio"] is not None]
    holds = [t["hold_time_minutes"] for t in parsed if t["hold_time_minutes"] is not None]

    core = _core_stats(pnls, rrs)
    closed = core["closed_trades"]

    total_pnl = core["total_pnl"]
    total_return_pct = (
        round(total_pnl / starting_equity * 100.0, 4) if starting_equity > 0 else 0.0
    )

    # --- Drawdown: prefer the real equity curve, else cumulative-PnL proxy ---
    if equity_curve:
        eq_values = [v for v in (_equity_value(p) for p in equity_curve) if v is not None]
    else:
        eq_values = []
    if not eq_values:
        eq_values = [starting_equity]
        running = starting_equity
        for pnl in pnls:
            running += pnl
            eq_values.append(running)
    max_drawdown_pct = _max_drawdown_pct(eq_values)

    # --- Sharpe / Sortino from per-trade returns on starting equity ---
    if starting_equity > 0:
        returns = [p / starting_equity for p in pnls]
    else:
        returns = []
    sharpe, sortino = _sharpe_sortino(returns)

    avg_hold = round(sum(holds) / len(holds), 4) if holds else 0.0

    # --- by_setup breakdown (same core stats grouped by signal_type) ---
    by_setup: dict[str, dict[str, Any]] = {}
    setups: dict[str, list[dict[str, Any]]] = {}
    for t in parsed:
        setups.setdefault(t["signal_type"], []).append(t)
    for setup, rows in setups.items():
        s_pnls = [r["pnl"] for r in rows]
        s_rrs = [r["rr_ratio"] for r in rows if r["rr_ratio"] is not None]
        s_core = _core_stats(s_pnls, s_rrs)
        s_core["total_return_pct"] = (
            round(s_core["total_pnl"] / starting_equity * 100.0, 4)
            if starting_equity > 0
            else 0.0
        )
        by_setup[setup] = s_core

    is_significant = closed >= MIN_SAMPLE_FOR_CONFIDENCE

    return {
        "trade_count": trade_count,
        "closed_trades": closed,
        "wins": core["wins"],
        "losses": core["losses"],
        "win_rate": core["win_rate"],
        "avg_win": core["avg_win"],
        "avg_loss": core["avg_loss"],
        "largest_win": core["largest_win"],
        "largest_loss": core["largest_loss"],
        "gross_profit": core["gross_profit"],
        "gross_loss": core["gross_loss"],
        "profit_factor": core["profit_factor"],
        "expectancy_per_trade": core["expectancy_per_trade"],
        "expectancy_r": core["expectancy_r"],
        "avg_rr": core["avg_rr"],
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "avg_hold_minutes": avg_hold,
        "starting_equity": round(starting_equity, 4),
        "by_setup": by_setup,
        "sample_size": closed,
        "min_sample_for_confidence": MIN_SAMPLE_FOR_CONFIDENCE,
        "is_statistically_significant": is_significant,
        "confidence_note": _confidence_note(closed),
    }


def rolling(
    trades: Iterable[dict[str, Any]],
    window: int,
    starting_equity: float = 100_000.0,
) -> list[dict[str, Any]]:
    """Rolling win_rate / expectancy / equity series over closed trades.

    Produces one point per closed trade so an edge (or its absence) can be seen
    emerging over time. ``win_rate`` and ``expectancy`` are computed over the
    trailing ``window`` trades; ``equity`` is the cumulative curve seeded at
    ``starting_equity``.

    Args:
        trades: Closed-trade rows (see :func:`compute_performance`).
        window: Trailing window size (must be >= 1).
        starting_equity: Seed for the cumulative equity series.

    Returns:
        A list of dicts, each with ``trade_number``, ``window``, ``win_rate``,
        ``expectancy`` and ``equity``. Empty when there are no priced trades.
    """
    if window < 1:
        raise ValueError("window must be >= 1")

    starting_equity = float(starting_equity)

    pnls: list[float] = []
    for row in (trades or []):
        pnl = _to_float(row.get("pnl"))
        if pnl is not None:
            pnls.append(pnl)

    series: list[dict[str, Any]] = []
    equity = starting_equity
    for i, pnl in enumerate(pnls):
        equity += pnl
        win_start = max(0, i - window + 1)
        window_slice = pnls[win_start : i + 1]
        w_wins = sum(1 for p in window_slice if p > 0)
        w_n = len(window_slice)
        series.append(
            {
                "trade_number": i + 1,
                "window": w_n,
                "win_rate": round(w_wins / w_n, 4) if w_n else 0.0,
                "expectancy": round(sum(window_slice) / w_n, 4) if w_n else 0.0,
                "equity": round(equity, 4),
            }
        )
    return series
