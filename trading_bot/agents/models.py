"""
Data types for the agent gate.

Pure data containers, no business logic, mirroring ``trading_bot/models/domain.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"
DECISION_REDUCE = "reduce"
VALID_DECISIONS = (DECISION_ALLOW, DECISION_BLOCK, DECISION_REDUCE)

SOURCE_VETO = "veto"
SOURCE_SCOUT = "scout"
SOURCE_ADVISOR = "advisor"
SOURCE_GATE = "gate"

# Closed vocabulary the scout must classify into. Anything else is "unknown".
CATALYST_CLASSES = (
    "earnings",
    "fda",
    "merger",
    "dilution",
    "offering",
    "pump",
    "rumor",
    "unknown",
)
TOXIC_CATALYSTS = frozenset({"pump", "dilution", "offering"})

# Scout status values carried in ``AgentDecision.raw["scout_status"]``.
SCOUT_STATUS_DISABLED = "llm_disabled"
SCOUT_STATUS_OK = "ok"
SCOUT_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class AgentDecision:
    """
    One gate outcome for one candidate entry.

    ``size_multiplier`` is 1.0 for allow, 0.0 for block, and strictly
    between 0 and 1 for reduce (enforced in ``__post_init__``; the gate
    additionally clamps its own reduce decisions to no lower than 0.25). It
    is only ever applied multiplicatively to a share count the risk engine
    already approved, so it can shrink but never grow a position.
    """

    decision: str  # "allow" | "block" | "reduce"
    source: str  # "veto" | "scout" | "advisor" | "gate"
    reasons: list[str]
    size_multiplier: float
    scout_notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in VALID_DECISIONS:
            raise ValueError(f"invalid decision {self.decision!r}")
        if not (0.0 <= self.size_multiplier <= 1.0):
            raise ValueError(
                f"size_multiplier must be within [0, 1], got {self.size_multiplier}"
            )
        if self.decision == DECISION_BLOCK and self.size_multiplier != 0.0:
            raise ValueError("block decisions must carry size_multiplier=0.0")
        if self.decision == DECISION_ALLOW and self.size_multiplier != 1.0:
            raise ValueError("allow decisions must carry size_multiplier=1.0")
        if self.decision == DECISION_REDUCE and not (
            0.0 < self.size_multiplier < 1.0
        ):
            raise ValueError("reduce decisions must carry 0 < size_multiplier < 1")

    @property
    def allows_entry(self) -> bool:
        return self.decision != DECISION_BLOCK

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "source": self.source,
            "reasons": list(self.reasons),
            "size_multiplier": round(self.size_multiplier, 4),
            "scout_notes": self.scout_notes,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class VetoContext:
    """
    Everything ``RuleVeto`` is allowed to look at.

    Every field except ``symbol`` is optional. ``None`` means "not known";
    the veto skips checks whose inputs are missing, except for the required
    core (symbol, advisor action/confidence, circuit state), whose absence
    blocks by default.

    The gate builds this from the signal, scan result, advisor
    recommendation, circuit status, positions, and config. The veto itself
    never touches any of those objects, which keeps it a pure function.
    """

    symbol: Optional[str] = None

    # Advisor
    advisor_action: Optional[str] = None  # "enter" | "skip" | "reduce_size"
    advisor_confidence: Optional[float] = None
    advisor_reasons: tuple[str, ...] = ()

    # Circuit breaker
    circuit_ok: Optional[bool] = None
    circuit_state: Optional[str] = None

    # Session
    market_open: Optional[bool] = None
    near_hard_exit: Optional[bool] = None

    # Portfolio
    open_positions: Optional[int] = None
    max_open_positions: Optional[int] = None
    held_symbols: frozenset[str] = frozenset()

    # Regime
    regime: Optional[str] = None

    # Scan bounds (from ScanResult + ScannerConfig when present)
    gap_pct: Optional[float] = None
    max_gap_pct: Optional[float] = None
    price: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    relative_volume: Optional[float] = None
    min_relative_volume: Optional[float] = None
    float_shares: Optional[int] = None
    max_float_shares: Optional[int] = None
    spread_pct: Optional[float] = None
    max_spread_pct: Optional[float] = None

    # PDT
    equity: Optional[float] = None
    pdt_equity_threshold: Optional[float] = None
    day_trade_count: Optional[int] = None
    pdt_max_day_trades: int = 3


@dataclass(frozen=True)
class ScoutResult:
    """Structured outcome of one scout evaluation (before gate policy)."""

    status: str  # "llm_disabled" | "ok" | "failed"
    catalyst: str = "unknown"
    confidence: float = 0.0
    risk_note: str = ""
    failure: str = ""

    @property
    def is_toxic(self) -> bool:
        return self.status == SCOUT_STATUS_OK and self.catalyst in TOXIC_CATALYSTS
