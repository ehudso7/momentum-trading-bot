"""Tests for the fail-closed live-money evidence gate."""

from __future__ import annotations

from datetime import date, timedelta

from trading_bot.risk.live_readiness import (
    LiveReadinessCriteria,
    evaluate_live_readiness,
)


def _trades(count: int, *, pnl_pattern: tuple[float, ...]) -> list[dict]:
    start = date(2026, 1, 2)
    return [
        {
            "date": (start + timedelta(days=index // 5)).isoformat(),
            "pnl": pnl_pattern[index % len(pnl_pattern)],
            "rr_ratio": 1.5 if pnl_pattern[index % len(pnl_pattern)] > 0 else -1.0,
            "signal_type": "vwap_pullback",
        }
        for index in range(count)
    ]


def test_empty_journal_fails_closed() -> None:
    result = evaluate_live_readiness([], starting_equity=100_000)
    assert result.ready is False
    assert result.checks["minimum_closed_trades"] is False
    assert result.checks["positive_expectancy"] is False
    assert result.checks["minimum_profit_factor"] is False


def test_small_profitable_sample_still_fails() -> None:
    result = evaluate_live_readiness(
        _trades(10, pnl_pattern=(200.0, -100.0)),
        starting_equity=100_000,
    )
    assert result.ready is False
    assert result.checks["positive_expectancy"] is True
    assert result.checks["minimum_closed_trades"] is False
    assert result.checks["minimum_trading_days"] is False


def test_complete_positive_evidence_passes() -> None:
    result = evaluate_live_readiness(
        _trades(100, pnl_pattern=(200.0, 200.0, -100.0, -100.0)),
        starting_equity=100_000,
    )
    assert result.ready is True
    assert result.reasons == ()
    assert result.metrics["profit_factor"] == 2.0


def test_drawdown_limit_cannot_be_waived_by_other_metrics() -> None:
    criteria = LiveReadinessCriteria(maximum_drawdown_pct=0.01)
    result = evaluate_live_readiness(
        _trades(100, pnl_pattern=(-100.0, 300.0, 300.0, 300.0)),
        starting_equity=100_000,
        criteria=criteria,
    )
    assert result.ready is False
    assert result.checks["maximum_drawdown"] is False
