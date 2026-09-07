"""
Thread-safe state container shared between the trading bot and dashboard.

The bot writes snapshots on each tick; the dashboard reads them.
Uses threading.Lock for safety since the FastAPI server runs in a
background thread.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytz

ET = pytz.timezone("US/Eastern")


@dataclass
class DashboardSnapshot:
    """Point-in-time snapshot of all bot state for the dashboard."""

    # Account
    equity: float = 0.0
    starting_equity: float = 0.0
    daily_pnl: float = 0.0
    buying_power: float = 0.0

    # Positions
    open_positions: list[dict[str, Any]] = field(default_factory=list)

    # Trades
    journal_entries: list[dict[str, Any]] = field(default_factory=list)

    # Latest real-data scanner observations (not trade recommendations)
    scanner_candidates: list[dict[str, Any]] = field(default_factory=list)

    # Circuit breaker
    circuit_breaker: dict[str, Any] = field(default_factory=dict)

    # Health
    health: dict[str, Any] = field(default_factory=dict)

    # Market regime
    regime: str | None = None

    # Market status
    market_status: str = "unknown"  # open, closed, premarket, holiday
    market_status_detail: str = ""  # e.g. "Presidents' Day" or "Next open: Tue 9:30 AM"

    # Equity history (list of {timestamp, equity} for charting)
    equity_history: list[dict[str, Any]] = field(default_factory=list)

    # Rejected signals
    rejected_signals_count: int = 0
    rejected_by_stage: dict[str, int] = field(default_factory=dict)
    rejected_signals: list[dict[str, Any]] = field(default_factory=list)

    # Agent gate decisions (most recent first; empty when the gate is off)
    agent_decisions: list[dict[str, Any]] = field(default_factory=list)

    # Metadata
    run_mode: str = "paper"
    broker_provider: str = "unknown"
    last_updated: str | None = None
    bot_running: bool = False
    last_error: str | None = None


class DashboardState:
    """Thread-safe state container for the dashboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = DashboardSnapshot()
        self._equity_history: list[dict[str, Any]] = []

    def update(
        self,
        equity: float,
        starting_equity: float,
        daily_pnl: float,
        buying_power: float,
        open_positions: list[dict[str, Any]],
        journal_entries: list[dict[str, Any]],
        circuit_breaker: dict[str, Any],
        health: dict[str, Any],
        regime: str | None,
        run_mode: str,
        broker_provider: str = "unknown",
        scanner_candidates: list[dict[str, Any]] | None = None,
        market_status: str = "unknown",
        market_status_detail: str = "",
        last_error: str | None = None,
        rejected_signals_count: int = 0,
        rejected_by_stage: dict[str, int] | None = None,
        rejected_signals: list[dict[str, Any]] | None = None,
        agent_decisions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Called by the bot on each tick to push latest state."""
        now_eastern = datetime.now(ET)
        now_str = now_eastern.strftime("%I:%M:%S %p ET")

        with self._lock:
            # Append to equity history (keep last 500 points)
            self._equity_history.append(
                {
                    "timestamp": now_eastern.isoformat(),
                    "equity": round(equity, 2),
                }
            )
            if len(self._equity_history) > 500:
                self._equity_history = self._equity_history[-500:]

            self._snapshot = DashboardSnapshot(
                equity=equity,
                starting_equity=starting_equity,
                daily_pnl=daily_pnl,
                buying_power=buying_power,
                open_positions=open_positions,
                journal_entries=journal_entries,
                scanner_candidates=scanner_candidates or [],
                circuit_breaker=circuit_breaker,
                health=health,
                regime=regime,
                market_status=market_status,
                market_status_detail=market_status_detail,
                equity_history=list(self._equity_history),
                rejected_signals_count=rejected_signals_count,
                rejected_by_stage=rejected_by_stage or {},
                rejected_signals=rejected_signals or [],
                agent_decisions=agent_decisions or [],
                run_mode=run_mode,
                broker_provider=broker_provider,
                last_updated=now_str,
                bot_running=True,
                last_error=last_error,
            )

    def get_snapshot(self) -> DashboardSnapshot:
        """Return a deep copy of the current snapshot."""
        with self._lock:
            return deepcopy(self._snapshot)
