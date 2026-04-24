"""
Phase 2 Core conversion: alpha scoring layer.

Scores every evaluated candidate using the `FeatureSnapshot` +
`SignalDecision` pair produced by Phase 1.5 instrumentation.

SHADOW MODE ONLY: the scorer does NOT block or approve trades. It is a
pure observer whose output is written to `data/alpha_scores.csv` for
offline analysis of signal quality. The live/paper loop is untouched.

Modules:
- `AlphaScore`  — result dataclass (symbol, timestamp, score, tier, reasons)
- `AlphaScorer` — Protocol interface any scorer must satisfy
- `RuleBasedAlphaScorer` — baseline heuristic scorer using the six
  documented features (gap_pct, relative_volume, volatility, regime,
  confidence, reason/action)
- `AlphaLogger` — thread-safe append-only CSV writer
"""

from __future__ import annotations

import csv
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, Union

import structlog

from trading_bot.models.domain import FeatureSnapshot, SignalDecision

log = structlog.get_logger(__name__)

_DEFAULT_CSV_PATH = "data/alpha_scores.csv"

CSV_HEADERS: list[str] = [
    "timestamp",
    "symbol",
    "score",
    "tier",
    "action",
    "confidence",
    "regime",
    "gap_pct",
    "relative_volume",
    "volatility",
    "reasons",
]

# Tier cutoffs (inclusive lower bound).
TIER_A_MIN = 0.80
TIER_B_MIN = 0.65
TIER_C_MIN = 0.50
TIER_D_MIN = 0.35


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class AlphaScore:
    """A single alpha score produced by an `AlphaScorer`."""

    symbol: str
    timestamp: datetime
    score: float  # 0.0 - 1.0
    tier: str  # "A" | "B" | "C" | "D" | "F"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": self.symbol,
            "score": round(self.score, 4),
            "tier": self.tier,
            "reasons": "|".join(self.reasons),
        }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class AlphaScorer(Protocol):
    """Any implementation that maps (snapshot, decision) to an AlphaScore."""

    def score(
        self, snapshot: FeatureSnapshot, decision: SignalDecision
    ) -> AlphaScore:  # pragma: no cover — interface only
        ...


# ---------------------------------------------------------------------------
# Tier helper
# ---------------------------------------------------------------------------


def score_to_tier(score: float) -> str:
    """Map a raw score in [0, 1] to a letter tier."""
    if score >= TIER_A_MIN:
        return "A"
    if score >= TIER_B_MIN:
        return "B"
    if score >= TIER_C_MIN:
        return "C"
    if score >= TIER_D_MIN:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Rule-based baseline scorer
# ---------------------------------------------------------------------------


class RuleBasedAlphaScorer:
    """
    Deterministic, hand-tuned alpha scorer for the momentum strategy.

    Combines six documented features with fixed weights:

    - gap_pct            (20%) — 8-20% is the sweet spot for low-float gappers
    - relative_volume    (25%) — 5x+ preferred, 10x+ exceptional
    - volatility (ATR%)  (15%) — 2-5% is healthy, <1% stale, >8% erratic
    - market regime      (10%) — trending_bullish > range_bound > bearish
    - decision confidence (25%) — direct weight, already in [0, 1]
    - action/reason       (5%) — "buy" boosts, rejection reason shapes skip

    Output is clamped to [0, 1] and mapped to a letter tier by
    `score_to_tier`. Weights sum to 1.0 so the raw weighted blend is
    already bounded.
    """

    # Feature weights — must sum to 1.0.
    GAP_WEIGHT = 0.20
    RVOL_WEIGHT = 0.25
    VOL_WEIGHT = 0.15
    REGIME_WEIGHT = 0.10
    CONF_WEIGHT = 0.25
    REASON_WEIGHT = 0.05

    # Regime quality multipliers.
    _REGIME_SCORES = {
        "trending_bullish": 1.00,
        "range_bound": 0.60,
        "low_volatility": 0.50,
        "high_volatility": 0.40,
        "trending_bearish": 0.20,
    }

    def score(
        self, snapshot: FeatureSnapshot, decision: SignalDecision
    ) -> AlphaScore:
        reasons: list[str] = []

        gap_score = self._score_gap(snapshot.gap_pct, reasons)
        rvol_score = self._score_rvol(snapshot.relative_volume, reasons)
        vol_score = self._score_volatility(snapshot.volatility, reasons)
        regime_score = self._score_regime(snapshot.regime, reasons)
        conf_score = self._score_confidence(decision.confidence, reasons)
        reason_score = self._score_action_reason(decision, reasons)

        raw = (
            self.GAP_WEIGHT * gap_score
            + self.RVOL_WEIGHT * rvol_score
            + self.VOL_WEIGHT * vol_score
            + self.REGIME_WEIGHT * regime_score
            + self.CONF_WEIGHT * conf_score
            + self.REASON_WEIGHT * reason_score
        )
        score = max(0.0, min(1.0, raw))

        return AlphaScore(
            symbol=snapshot.symbol,
            timestamp=snapshot.timestamp,
            score=score,
            tier=score_to_tier(score),
            reasons=reasons,
        )

    # --- per-feature scoring ------------------------------------------------

    @staticmethod
    def _score_gap(gap_pct: float, reasons: list[str]) -> float:
        g = float(gap_pct)
        if 8.0 <= g <= 20.0:
            reasons.append(f"gap_sweet_spot({g:.1f}%)")
            return 1.0
        if 4.0 <= g < 8.0:
            reasons.append(f"gap_modest({g:.1f}%)")
            return 0.6
        if 20.0 < g <= 40.0:
            reasons.append(f"gap_extended({g:.1f}%)")
            return 0.7
        if g > 40.0:
            reasons.append(f"gap_extreme({g:.1f}%)")
            return 0.3
        reasons.append(f"gap_weak({g:.1f}%)")
        return 0.2

    @staticmethod
    def _score_rvol(rvol: float, reasons: list[str]) -> float:
        r = float(rvol)
        if r >= 10.0:
            reasons.append(f"rvol_exceptional({r:.1f}x)")
            return 1.0
        if r >= 5.0:
            reasons.append(f"rvol_strong({r:.1f}x)")
            return 0.8
        if r >= 2.0:
            reasons.append(f"rvol_adequate({r:.1f}x)")
            return 0.5
        reasons.append(f"rvol_weak({r:.1f}x)")
        return 0.2

    @staticmethod
    def _score_volatility(volatility: float, reasons: list[str]) -> float:
        """
        `volatility` is ATR as a percentage of price (see Phase 1.5
        FeatureSnapshot.volatility). Zero means unavailable (e.g., bars
        were too thin) — treated as insufficient rather than penalized
        as extreme.
        """
        v = float(volatility)
        if v <= 0.0:
            reasons.append("vol_unavailable")
            return 0.2
        if 2.0 <= v <= 5.0:
            reasons.append(f"vol_healthy({v:.2f}%)")
            return 1.0
        if 1.0 <= v < 2.0:
            reasons.append(f"vol_low({v:.2f}%)")
            return 0.5
        if 5.0 < v <= 8.0:
            reasons.append(f"vol_elevated({v:.2f}%)")
            return 0.6
        if v > 8.0:
            reasons.append(f"vol_erratic({v:.2f}%)")
            return 0.3
        reasons.append(f"vol_insufficient({v:.2f}%)")
        return 0.2

    @classmethod
    def _score_regime(cls, regime: str, reasons: list[str]) -> float:
        key = (regime or "").lower()
        score = cls._REGIME_SCORES.get(key, 0.5)
        reasons.append(f"regime_{key or 'unknown'}({score:.2f})")
        return score

    @staticmethod
    def _score_confidence(confidence: float, reasons: list[str]) -> float:
        c = max(0.0, min(1.0, float(confidence)))
        reasons.append(f"confidence({c:.2f})")
        return c

    @staticmethod
    def _score_action_reason(
        decision: SignalDecision, reasons: list[str]
    ) -> float:
        """
        Encode the downstream decision as a quality signal:
        - "buy" (executed) is the strongest positive.
        - "skip" is shaped by the rejection stage — advisor/correlation
          skips may still be high-alpha setups, while strategy skips
          and empty-bars skips are noise.
        """
        action = (decision.action or "").lower()
        reason = (decision.reason or "").lower()

        if action == "buy":
            reasons.append("action_buy")
            return 1.0

        if reason.startswith("advisor"):
            score = 0.60
        elif reason.startswith("correlation"):
            score = 0.55
        elif reason.startswith("risk"):
            score = 0.50
        elif reason.startswith("strategy"):
            score = 0.25
        elif reason == "already_held":
            score = 0.60
        elif reason == "broker_rejected":
            score = 0.40
        elif reason == "empty_bars":
            score = 0.10
        elif reason in ("equity_api_failed", "zero_equity"):
            score = 0.40
        else:
            score = 0.30

        reasons.append(f"action_skip:{reason or 'unknown'}")
        return score


# ---------------------------------------------------------------------------
# CSV logger
# ---------------------------------------------------------------------------


class AlphaLogger:
    """Thread-safe append-only writer for AlphaScore rows."""

    def __init__(self, csv_path: Union[str, Path] = _DEFAULT_CSV_PATH):
        self._path = Path(csv_path)
        self._lock = threading.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.error(
                "alpha_log.mkdir_error",
                path=str(self._path.parent),
                error=str(e),
            )
        self._ensure_header()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_header(self) -> None:
        if self._path.exists():
            return
        try:
            with open(self._path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        except Exception as e:
            log.error("alpha_log.header_error", path=str(self._path), error=str(e))

    def log(
        self,
        alpha: AlphaScore,
        snapshot: FeatureSnapshot,
        decision: SignalDecision,
    ) -> None:
        """Append a single alpha-score row. Best-effort — never raises."""
        row = {
            "timestamp": alpha.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": alpha.symbol,
            "score": round(alpha.score, 4),
            "tier": alpha.tier,
            "action": decision.action,
            "confidence": round(float(decision.confidence), 4),
            "regime": snapshot.regime,
            "gap_pct": round(float(snapshot.gap_pct), 3),
            "relative_volume": round(float(snapshot.relative_volume), 3),
            "volatility": round(float(snapshot.volatility), 4),
            "reasons": "|".join(alpha.reasons),
        }
        with self._lock:
            try:
                if not self._path.exists():
                    self._ensure_header()
                with open(self._path, "a", newline="") as f:
                    csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
            except Exception as e:
                log.debug(
                    "alpha_log.write_error", path=str(self._path), error=str(e)
                )
