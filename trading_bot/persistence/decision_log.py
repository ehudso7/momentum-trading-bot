"""
Append-only CSV writer for Phase 1.5 Core conversion instrumentation.

Captures a `FeatureSnapshot` + `SignalDecision` pair for every candidate
evaluated by the main loop. Output is written to `data/decision_log.csv`
as a flat, self-describing row — this is a NEW dataset and is kept
separate from the existing trade journal (`data/journal.csv`) and
rejected-signal shadow log (`data/rejected_signals.csv`).

Failures to write never raise to the caller — logging is best-effort
so it cannot break the trading loop.

Phase 2.7 adds optional daily rotation for long-running shadow
deployments. Set `TRADING_DATA_ROTATION=daily` (or pass
`rotation="daily"` to the constructor) to write to
`data/decision_log_YYYY-MM-DD.csv` instead of the canonical path.
Date is re-evaluated per write so midnight rollover works without a
bot restart. Default mode is `none` — canonical path, backward
compatible.
"""

from __future__ import annotations

import csv
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional, Union

import structlog

from trading_bot.models.domain import FeatureSnapshot, SignalDecision

log = structlog.get_logger(__name__)

_DEFAULT_CSV_PATH = "data/decision_log.csv"
ROTATION_ENV_VAR = "TRADING_DATA_ROTATION"
ROTATION_NONE = "none"
ROTATION_DAILY = "daily"
_VALID_ROTATIONS = (ROTATION_NONE, ROTATION_DAILY)

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


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD. Factored out for easy patching in tests."""
    return date.today().strftime("%Y-%m-%d")


def _resolve_rotation(rotation: Optional[str]) -> str:
    """
    Resolve the rotation mode from an explicit argument or env var.

    Explicit argument wins. Unknown values silently fall back to
    "none" — a typo must never turn into a lost dataset.
    """
    raw = rotation if rotation is not None else os.getenv(ROTATION_ENV_VAR, ROTATION_NONE)
    mode = (raw or ROTATION_NONE).strip().lower()
    if mode not in _VALID_ROTATIONS:
        return ROTATION_NONE
    return mode


class DecisionLogger:
    """Thread-safe append-only writer for FeatureSnapshot + SignalDecision rows."""

    def __init__(
        self,
        csv_path: Union[str, Path] = _DEFAULT_CSV_PATH,
        rotation: Optional[str] = None,
    ):
        self._base_path = Path(csv_path)
        self._rotation = _resolve_rotation(rotation)
        self._lock = threading.Lock()
        try:
            self._base_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.error(
                "decision_log.mkdir_error",
                path=str(self._base_path.parent),
                error=str(e),
            )
        # Pre-create today's file so CLI readers never see a missing path
        # when the bot has started but not yet processed a candidate.
        self._ensure_header(self._current_path())

    @property
    def path(self) -> Path:
        """The current destination path — dated when rotation is daily."""
        return self._current_path()

    @property
    def base_path(self) -> Path:
        """The canonical un-rotated path (stable across days)."""
        return self._base_path

    @property
    def rotation(self) -> str:
        return self._rotation

    def _current_path(self) -> Path:
        """Compute the active path. Re-evaluated per call to handle midnight rollover."""
        if self._rotation == ROTATION_DAILY:
            today = _today_str()
            return self._base_path.with_name(
                f"{self._base_path.stem}_{today}{self._base_path.suffix}"
            )
        return self._base_path

    def _ensure_header(self, path: Path) -> None:
        """Create the CSV file with a header row if it does not already exist."""
        if path.exists():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        except Exception as e:
            log.error("decision_log.header_error", path=str(path), error=str(e))

    def log(self, snapshot: FeatureSnapshot, decision: SignalDecision) -> None:
        """
        Append a single row combining the feature snapshot and signal decision.

        Best-effort: exceptions are caught and logged so the trading loop
        is never interrupted by disk or permission errors.
        """
        row = self._merge(snapshot, decision)
        current = self._current_path()
        with self._lock:
            try:
                # Re-ensure header in case the file was deleted between init
                # and now, OR the day just rolled over in rotation mode.
                if not current.exists():
                    self._ensure_header(current)
                with open(current, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                    writer.writerow(row)
            except Exception as e:
                log.debug(
                    "decision_log.write_error",
                    path=str(current),
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
