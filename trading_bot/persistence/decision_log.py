"""
Append-only CSV writer for Phase 1.5 Core conversion instrumentation.

Captures a `FeatureSnapshot` + `SignalDecision` pair for every candidate
evaluated by the main loop. Output is written to `data/decision_log.csv`
as a flat, self-describing row — this is a NEW dataset and is kept
separate from the existing trade journal (`data/journal.csv`) and
rejected-signal shadow log (`data/rejected_signals.csv`).

Failures to write never raise to the caller — logging is best-effort
so it cannot break the trading loop.
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Union

import structlog

from trading_bot.models.domain import FeatureSnapshot, SignalDecision

log = structlog.get_logger(__name__)

_DEFAULT_CSV_PATH = "data/decision_log.csv"

CSV_HEADERS: list[str] = [
    "timestamp",
    "symbol",
    "price",
    "gap_pct",
    "relative_volume",
    "volatility",
    "regime",
    "action",
    "confidence",
    "reason",
]


class DecisionLogger:
    """Thread-safe append-only writer for FeatureSnapshot + SignalDecision rows."""

    def __init__(self, csv_path: Union[str, Path] = _DEFAULT_CSV_PATH):
        self._path = Path(csv_path)
        self._lock = threading.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.error("decision_log.mkdir_error", path=str(self._path.parent), error=str(e))
        self._ensure_header()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_header(self) -> None:
        """Create the CSV file with a header row if it does not already exist."""
        if self._path.exists():
            return
        try:
            with open(self._path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        except Exception as e:
            log.error("decision_log.header_error", path=str(self._path), error=str(e))

    def log(self, snapshot: FeatureSnapshot, decision: SignalDecision) -> None:
        """
        Append a single row combining the feature snapshot and signal decision.

        Best-effort: exceptions are caught and logged so the trading loop
        is never interrupted by disk or permission errors.
        """
        row = self._merge(snapshot, decision)
        with self._lock:
            try:
                # Re-ensure header in case the file was deleted between init and now.
                if not self._path.exists():
                    self._ensure_header()
                with open(self._path, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                    writer.writerow(row)
            except Exception as e:
                log.debug(
                    "decision_log.write_error",
                    path=str(self._path),
                    error=str(e),
                )

    @staticmethod
    def _merge(snapshot: FeatureSnapshot, decision: SignalDecision) -> dict:
        """Flatten a snapshot/decision pair into a single CSV row."""
        snap = snapshot.to_dict()
        dec = decision.to_dict()
        # Decision timestamp wins (it's the moment the decision was taken).
        return {
            "timestamp": dec.get("timestamp", snap.get("timestamp", "")),
            "symbol": snap["symbol"],
            "price": snap["price"],
            "gap_pct": snap["gap_pct"],
            "relative_volume": snap["relative_volume"],
            "volatility": snap["volatility"],
            "regime": snap["regime"],
            "action": dec["action"],
            "confidence": dec["confidence"],
            "reason": dec["reason"],
        }
