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
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Protocol, Union

import structlog

from trading_bot.models.domain import FeatureSnapshot, SignalDecision

log = structlog.get_logger(__name__)

_DEFAULT_CSV_PATH = "data/alpha_scores.csv"

# Phase 2.7 — daily-rotation controls. Kept in sync with
# trading_bot.persistence.decision_log so an operator can flip a single
# env var and rotate both Core datasets at once.
ROTATION_ENV_VAR = "TRADING_DATA_ROTATION"
ROTATION_NONE = "none"
ROTATION_DAILY = "daily"
_VALID_ROTATIONS = (ROTATION_NONE, ROTATION_DAILY)

# Phase 3 — paper-only alpha filter gate.
# Off by default; only active in paper mode. Never touches live mode.
FILTER_ENABLED_ENV_VAR = "TRADING_ALPHA_FILTER_ENABLED"
FILTER_MIN_TIER_ENV_VAR = "TRADING_ALPHA_FILTER_MIN_TIER"
FILTER_DEFAULT_ENABLED = False
FILTER_DEFAULT_MIN_TIER = "B"
FILTER_VALID_MIN_TIERS: tuple[str, ...] = ("A", "B", "C")
# Tier weakness ordering — lower index = stronger. A trade with tier t
# passes a minimum of min iff _TIER_INDEX[t] <= _TIER_INDEX[min].
_TIER_INDEX: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}


def _today_str() -> str:
    """Return today's date as YYYY-MM-DD. Patchable from tests."""
    return date.today().strftime("%Y-%m-%d")


def _resolve_rotation(rotation: Optional[str]) -> str:
    """Resolve rotation mode; explicit arg wins, unknown values fall back to none."""
    raw = rotation if rotation is not None else os.getenv(ROTATION_ENV_VAR, ROTATION_NONE)
    mode = (raw or ROTATION_NONE).strip().lower()
    if mode not in _VALID_ROTATIONS:
        return ROTATION_NONE
    return mode

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
    # Phase 3.5 — fingerprint of the scorer configuration that
    # produced this row. Optional: old files created before Phase 3.5
    # will not have this column, and the analysis layer treats the
    # column as nullable so legacy data still parses.
    "scorer_fingerprint",
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
# Phase 3.5 — scorer fingerprint
# ---------------------------------------------------------------------------


def get_alpha_scorer_config() -> dict:
    """
    Return the scorer configuration that defines an alpha tier.

    The returned dict covers every constant that, if changed,
    invalidates previously collected calibration data:
      - RuleBasedAlphaScorer feature weights (must sum to 1.0).
      - Tier cutoffs A / B / C / D.
      - Regime → score map.

    Re-computed on every call so monkey-patching class attributes is
    observable by `get_alpha_scorer_fingerprint()` without module
    reloads.
    """
    return {
        "scorer": "RuleBasedAlphaScorer",
        "weights": {
            "gap": RuleBasedAlphaScorer.GAP_WEIGHT,
            "rvol": RuleBasedAlphaScorer.RVOL_WEIGHT,
            "vol": RuleBasedAlphaScorer.VOL_WEIGHT,
            "regime": RuleBasedAlphaScorer.REGIME_WEIGHT,
            "confidence": RuleBasedAlphaScorer.CONF_WEIGHT,
            "reason": RuleBasedAlphaScorer.REASON_WEIGHT,
        },
        "tier_thresholds": {
            "A": TIER_A_MIN,
            "B": TIER_B_MIN,
            "C": TIER_C_MIN,
            "D": TIER_D_MIN,
        },
        "regime_scores": dict(RuleBasedAlphaScorer._REGIME_SCORES),
    }


def get_alpha_scorer_fingerprint() -> str:
    """
    Deterministic SHA-256 fingerprint of the scorer configuration.

    Stable across runs as long as weights, tier cutoffs, and regime
    scores are unchanged. Serialization uses sorted keys and tight
    separators so formatting whitespace cannot flip the hash.
    """
    payload = json.dumps(
        get_alpha_scorer_config(),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CSV logger
# ---------------------------------------------------------------------------


class AlphaLogger:
    """Thread-safe append-only writer for AlphaScore rows."""

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
                "alpha_log.mkdir_error",
                path=str(self._base_path.parent),
                error=str(e),
            )
        self._ensure_header(self._current_path())

    @property
    def path(self) -> Path:
        """Active destination path — dated when rotation is daily."""
        return self._current_path()

    @property
    def base_path(self) -> Path:
        return self._base_path

    @property
    def rotation(self) -> str:
        return self._rotation

    def _current_path(self) -> Path:
        if self._rotation == ROTATION_DAILY:
            today = _today_str()
            return self._base_path.with_name(
                f"{self._base_path.stem}_{today}{self._base_path.suffix}"
            )
        return self._base_path

    def _ensure_header(self, path: Path) -> None:
        if path.exists():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        except Exception as e:
            log.error("alpha_log.header_error", path=str(path), error=str(e))

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
            # Phase 3.5 — stamped per-row so multi-day analysis can
            # detect mid-dataset weight changes even when the file
            # has been appended to across versions.
            "scorer_fingerprint": get_alpha_scorer_fingerprint(),
        }
        current = self._current_path()
        with self._lock:
            try:
                if not current.exists():
                    self._ensure_header(current)
                with open(current, "a", newline="") as f:
                    csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
            except Exception as e:
                log.debug(
                    "alpha_log.write_error", path=str(current), error=str(e)
                )


# ---------------------------------------------------------------------------
# Phase 3 — paper-only alpha filter gate.
# ---------------------------------------------------------------------------


def _resolve_filter_enabled(enabled: Optional[object]) -> bool:
    """
    Explicit constructor arg wins over the env var. Unrecognized
    values resolve to False — the filter is OFF by default and a
    typo must never turn it on.
    """
    if isinstance(enabled, bool):
        return enabled
    raw = enabled if enabled is not None else os.getenv(
        FILTER_ENABLED_ENV_VAR, str(FILTER_DEFAULT_ENABLED)
    )
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def _resolve_min_tier(min_tier: Optional[str]) -> str:
    """
    Explicit arg wins over env var. Unknown / typo values silently
    fall back to FILTER_DEFAULT_MIN_TIER — a typo must never relax
    the filter to "F" or similar.
    """
    raw = min_tier if min_tier is not None else os.getenv(
        FILTER_MIN_TIER_ENV_VAR, FILTER_DEFAULT_MIN_TIER
    )
    value = (str(raw) or "").strip().upper()
    if value not in FILTER_VALID_MIN_TIERS:
        return FILTER_DEFAULT_MIN_TIER
    return value


@dataclass
class FilterDecision:
    """
    Result of consulting the alpha filter for a single would-be trade.

    `blocked=True` means the candidate failed the tier threshold AND
    the filter is currently active (enabled + paper mode). In every
    other case the filter is a no-op and `blocked=False`.
    """

    blocked: bool
    reason: str  # "" when not blocked
    min_tier: str
    tier: Optional[str]  # the scored tier, when scoring ran; else None
    enabled: bool
    run_mode: str

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "reason": self.reason,
            "min_tier": self.min_tier,
            "tier": self.tier,
            "enabled": self.enabled,
            "run_mode": self.run_mode,
        }


class AlphaFilter:
    """
    Phase 3 alpha filter. Opt-in, paper-only, blocking-only.

    Safety invariants (also enforced by where ``check`` is called in
    ``TradingBot._tick``):

    - LIVE mode NEVER runs the filter. If the operator enables it in
      live, a warning is logged once at construction and `check()`
      returns `blocked=False` for every candidate.
    - The filter is OFF by default — an unset env var and no
      constructor arg yield byte-identical behaviour to the
      pre-Phase-3 bot.
    - The filter can ONLY block. It cannot approve a trade the risk
      engine rejected, cannot increase size, cannot skip the
      correlation/advisor checks. In the main loop it is invoked
      AFTER risk / correlation / advisor and purely decides whether
      the already-approved buy should still be issued.

    Env vars:
      - TRADING_ALPHA_FILTER_ENABLED  (default false)
      - TRADING_ALPHA_FILTER_MIN_TIER (default B; invalid → B)
    """

    LIVE_MODE_TOKEN = "live"
    PAPER_MODE_TOKEN = "paper"

    def __init__(
        self,
        scorer: "AlphaScorer",
        run_mode: str,
        enabled: Optional[object] = None,
        min_tier: Optional[str] = None,
    ) -> None:
        self._scorer = scorer
        self._run_mode = (str(run_mode) or self.PAPER_MODE_TOKEN).strip().lower()
        self._enabled = _resolve_filter_enabled(enabled)
        self._min_tier = _resolve_min_tier(min_tier)

        if self._enabled and self._run_mode == self.LIVE_MODE_TOKEN:
            log.warning(
                "alpha_filter_ignored_in_live_mode",
                min_tier=self._min_tier,
                detail=(
                    "TRADING_ALPHA_FILTER_ENABLED=true but run_mode is "
                    "live — the alpha filter is only honoured in paper "
                    "mode and will be ignored for this session."
                ),
            )

    # ------------------------------------------------------------------
    # Introspection (used by tests and status logging)
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def min_tier(self) -> str:
        return self._min_tier

    @property
    def run_mode(self) -> str:
        return self._run_mode

    @property
    def active(self) -> bool:
        """True iff the filter can actually block a trade this session."""
        return self._enabled and self._run_mode == self.PAPER_MODE_TOKEN

    # ------------------------------------------------------------------
    # Core decision
    # ------------------------------------------------------------------

    def check(
        self,
        snapshot: FeatureSnapshot,
        confidence: float = 0.5,
    ) -> FilterDecision:
        """
        Decide whether to allow or block a would-be buy.

        When the filter is inactive (live mode, disabled, or both)
        returns `blocked=False` immediately without computing a score.

        When active, scores the candidate with a tentative "buy"
        decision so the reason-component of the score reflects the
        would-be outcome rather than the blocked state.
        """
        if not self.active:
            return FilterDecision(
                blocked=False,
                reason="",
                min_tier=self._min_tier,
                tier=None,
                enabled=self._enabled,
                run_mode=self._run_mode,
            )

        tentative = SignalDecision(
            timestamp=snapshot.timestamp,
            symbol=snapshot.symbol,
            action="buy",
            confidence=float(confidence),
            reason="executed",
        )
        alpha = self._scorer.score(snapshot, tentative)

        if self._tier_meets_min(alpha.tier):
            return FilterDecision(
                blocked=False,
                reason="",
                min_tier=self._min_tier,
                tier=alpha.tier,
                enabled=True,
                run_mode=self._run_mode,
            )

        return FilterDecision(
            blocked=True,
            reason=f"alpha_filter_blocked:tier={alpha.tier}:min={self._min_tier}",
            min_tier=self._min_tier,
            tier=alpha.tier,
            enabled=True,
            run_mode=self._run_mode,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tier_index(tier: str) -> int:
        """Weakness index. Unknown tiers are treated as weakest possible."""
        return _TIER_INDEX.get((tier or "").upper(), 99)

    def _tier_meets_min(self, tier: str) -> bool:
        return self._tier_index(tier) <= self._tier_index(self._min_tier)
