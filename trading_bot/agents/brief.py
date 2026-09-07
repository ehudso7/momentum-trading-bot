"""
Agent brief: one human-readable log line and one structured record per gate
decision.

Persistence is an append-only CSV (``data/agent_decisions.csv`` by default,
next to the trade journal), deliberately separate from ``decision_log.csv``
which keeps its one-row-per-candidate invariant and is written by the main
loop's existing ``_log_decision`` path. The brief also keeps a bounded
in-memory ring of recent decisions for the dashboard.

Best-effort like the other journals: a disk error is logged and never
propagates into the trading loop.
"""

from __future__ import annotations

import collections
import csv
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from trading_bot.agents.models import AgentDecision
from trading_bot.utils.helpers import now_et

log = structlog.get_logger(__name__)

CSV_HEADERS: list[str] = [
    "timestamp",
    "symbol",
    "decision",
    "source",
    "size_multiplier",
    "advisor_action",
    "advisor_confidence",
    "scout_status",
    "scout_catalyst",
    "scout_confidence",
    "regime",
    "gap_pct",
    "relative_volume",
    "reasons",
    "scout_notes",
]


class AgentBrief:
    """Structured recorder for gate decisions. Thread-safe, never raises."""

    def __init__(
        self,
        csv_path: Optional[Path] = None,
        max_recent: int = 100,
        clock: Callable[[], Any] = now_et,
    ) -> None:
        self._csv_path = Path(csv_path) if csv_path is not None else None
        self._recent: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=max_recent
        )
        self._lock = threading.Lock()
        self._clock = clock
        if self._csv_path is not None:
            self._ensure_header()

    @property
    def csv_path(self) -> Optional[Path]:
        return self._csv_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, decision: AgentDecision) -> dict[str, Any]:
        """Log, persist, and remember one decision. Returns the flat record."""
        row = self._flatten(decision)

        log.info(
            "agent.decision",
            symbol=row["symbol"],
            decision=row["decision"],
            source=row["source"],
            size_multiplier=row["size_multiplier"],
            advisor_action=row["advisor_action"],
            scout_catalyst=row["scout_catalyst"],
            reasons=list(decision.reasons),
            line=self.format_line(decision),
        )

        with self._lock:
            self._recent.appendleft(row)
            if self._csv_path is not None:
                self._append_csv(row)
        return row

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent decisions first, as plain dicts safe for JSON."""
        with self._lock:
            return [dict(r) for r in list(self._recent)[:limit]]

    @staticmethod
    def format_line(decision: AgentDecision) -> str:
        raw = decision.raw
        return (
            f"[agent:{decision.source}] {raw.get('symbol', '?')} "
            f"{decision.decision.upper()} x{decision.size_multiplier:.2f} "
            f"advisor={raw.get('advisor_action', '?')} "
            f"scout={raw.get('scout_catalyst', 'unknown')}/{raw.get('scout_status', '?')} "
            f"reasons={'; '.join(decision.reasons)}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flatten(self, decision: AgentDecision) -> dict[str, Any]:
        raw = decision.raw
        timestamp = self._clock()
        return {
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": raw.get("symbol", ""),
            "decision": decision.decision,
            "source": decision.source,
            "size_multiplier": round(decision.size_multiplier, 4),
            "advisor_action": raw.get("advisor_action", ""),
            "advisor_confidence": _round_or_blank(raw.get("advisor_confidence")),
            "scout_status": raw.get("scout_status", ""),
            "scout_catalyst": raw.get("scout_catalyst", ""),
            "scout_confidence": _round_or_blank(raw.get("scout_confidence")),
            "regime": raw.get("regime", ""),
            "gap_pct": _round_or_blank(raw.get("gap_pct"), 1),
            "relative_volume": _round_or_blank(raw.get("relative_volume"), 1),
            "reasons": "; ".join(decision.reasons),
            "scout_notes": decision.scout_notes,
        }

    def _ensure_header(self) -> None:
        assert self._csv_path is not None
        try:
            self._csv_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._csv_path.exists():
                with open(self._csv_path, "w", newline="") as f:
                    csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        except Exception as exc:
            log.error(
                "agent.brief_header_error", path=str(self._csv_path), error=str(exc)
            )

    def _append_csv(self, row: dict[str, Any]) -> None:
        assert self._csv_path is not None
        try:
            if not self._csv_path.exists():
                self._ensure_header()
            with open(self._csv_path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
        except Exception as exc:
            log.debug(
                "agent.brief_write_error", path=str(self._csv_path), error=str(exc)
            )


def _round_or_blank(value: Any, digits: int = 4) -> Any:
    if value is None:
        return ""
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return ""
