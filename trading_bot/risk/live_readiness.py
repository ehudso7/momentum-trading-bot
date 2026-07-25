"""Evidence-based gate for any future live-money activation.

The evaluator is deliberately pure: it consumes closed journal rows and returns
an explainable decision. It never enables live trading by itself and it never
waives a failed criterion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from trading_bot.analytics.performance import compute_performance


@dataclass(frozen=True)
class LiveReadinessCriteria:
    """Minimum evidence required before live-money mode may start."""

    minimum_closed_trades: int = 100
    minimum_trading_days: int = 20
    minimum_profit_factor: float = 1.25
    maximum_drawdown_pct: float = 5.0


@dataclass(frozen=True)
class LiveReadinessResult:
    """Serializable readiness decision with every failed reason retained."""

    ready: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...]
    metrics: dict[str, Any]
    criteria: LiveReadinessCriteria

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "criteria": asdict(self.criteria),
        }


def _distinct_trading_days(trades: Iterable[dict[str, Any]]) -> int:
    dates = {
        str(row.get("date", "")).strip()
        for row in trades
        if str(row.get("date", "")).strip()
    }
    return len(dates)


def evaluate_live_readiness(
    trades: Iterable[dict[str, Any]],
    *,
    starting_equity: float,
    criteria: LiveReadinessCriteria | None = None,
) -> LiveReadinessResult:
    """Evaluate the complete live-money evidence gate.

    Missing or malformed journal values fail closed through the existing honest
    performance parser. Profit factor ``None`` (for example, no losing trades in
    a tiny sample) does not pass the gate.
    """

    selected = criteria or LiveReadinessCriteria()
    rows = list(trades or [])
    performance = compute_performance(rows, starting_equity=starting_equity)
    closed_trades = int(performance.get("closed_trades") or 0)
    trading_days = _distinct_trading_days(rows)
    expectancy = float(performance.get("expectancy_per_trade") or 0.0)
    profit_factor_raw = performance.get("profit_factor")
    profit_factor = (
        float(profit_factor_raw) if profit_factor_raw is not None else None
    )
    drawdown = float(performance.get("max_drawdown_pct") or 0.0)

    checks = {
        "minimum_closed_trades": closed_trades >= selected.minimum_closed_trades,
        "minimum_trading_days": trading_days >= selected.minimum_trading_days,
        "positive_expectancy": expectancy > 0.0,
        "minimum_profit_factor": (
            profit_factor is not None
            and profit_factor >= selected.minimum_profit_factor
        ),
        "maximum_drawdown": drawdown <= selected.maximum_drawdown_pct,
    }

    reasons: list[str] = []
    if not checks["minimum_closed_trades"]:
        reasons.append(
            f"closed trades {closed_trades}/{selected.minimum_closed_trades}"
        )
    if not checks["minimum_trading_days"]:
        reasons.append(
            f"trading days {trading_days}/{selected.minimum_trading_days}"
        )
    if not checks["positive_expectancy"]:
        reasons.append(f"expectancy must be positive (current {expectancy:.2f})")
    if not checks["minimum_profit_factor"]:
        rendered = "undefined" if profit_factor is None else f"{profit_factor:.2f}"
        reasons.append(
            "profit factor "
            f"{rendered}/{selected.minimum_profit_factor:.2f} minimum"
        )
    if not checks["maximum_drawdown"]:
        reasons.append(
            f"drawdown {drawdown:.2f}%/{selected.maximum_drawdown_pct:.2f}% maximum"
        )

    metrics = {
        "closed_trades": closed_trades,
        "trading_days": trading_days,
        "expectancy_per_trade": expectancy,
        "profit_factor": profit_factor,
        "max_drawdown_pct": drawdown,
    }
    return LiveReadinessResult(
        ready=all(checks.values()),
        checks=checks,
        reasons=tuple(reasons),
        metrics=metrics,
        criteria=selected,
    )
