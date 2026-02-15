"""
Shared domain data classes used across all modules.

These are pure data containers with no business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalType(str, Enum):
    VWAP_PULLBACK = "vwap_pullback"
    EMA_PULLBACK = "ema_pullback"
    BREAKOUT_CONTINUATION = "breakout_continuation"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_CLOSED = "partially_closed"
    CLOSED = "closed"


@dataclass
class ScanResult:
    """A candidate stock from the momentum scanner."""

    symbol: str
    price: float
    gap_pct: float
    relative_volume: float
    float_shares: Optional[int]
    volume: int
    prev_close: float
    catalyst: Optional[str]
    score: float  # composite ranking score [0, 1]
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"ScanResult({self.symbol} ${self.price:.2f} "
            f"gap={self.gap_pct:.1f}% rvol={self.relative_volume:.1f}x "
            f"score={self.score:.2f})"
        )


@dataclass
class TradeSignal:
    """A validated entry signal from the strategy."""

    symbol: str
    signal_type: SignalType
    entry_price: float
    stop_price: float
    target_prices: list[float]  # [1:1 target, 2:1 target]
    atr: float
    vwap: float
    ema9: float
    confidence: float  # 0.0 - 1.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def risk_per_share(self) -> float:
        """Dollar risk per share (entry - stop)."""
        return abs(self.entry_price - self.stop_price)

    @property
    def reward_risk_ratio(self) -> float:
        """R:R ratio to first target."""
        if not self.target_prices or self.risk_per_share == 0:
            return 0.0
        return abs(self.target_prices[0] - self.entry_price) / self.risk_per_share


@dataclass
class PositionInfo:
    """Tracks an open or recently closed position."""

    symbol: str
    side: OrderSide
    entry_price: float
    current_price: float
    shares: int
    shares_remaining: int
    stop_price: float
    target_prices: list[float]
    status: PositionStatus
    pnl_unrealized: float
    pnl_realized: float
    scale_outs_completed: int
    entry_time: datetime
    signal_type: SignalType = SignalType.VWAP_PULLBACK
    trailing_stop_active: bool = False
    trailing_stop_price: Optional[float] = None
    broker_order_ids: list[str] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return self.pnl_realized + self.pnl_unrealized

    @property
    def r_multiple(self) -> float:
        """Current P&L expressed as a multiple of initial risk."""
        risk_per_share = abs(self.entry_price - self.stop_price)
        if risk_per_share == 0:
            return 0.0
        pnl_per_share = self.current_price - self.entry_price
        return pnl_per_share / risk_per_share


@dataclass
class RiskCheckResult:
    """Result of pre-trade risk validation."""

    approved: bool
    shares: int
    risk_dollars: float
    reason: str  # "approved" or rejection reason
    leverage_used: float
    positions_count: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class JournalEntry:
    """A completed trade record for the CSV journal."""

    date: str
    symbol: str
    side: str
    signal_type: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    rr_ratio: float
    hold_time_minutes: float
    entry_time: str
    exit_time: str
    exit_reason: str
    notes: str = ""

    def to_dict(self) -> dict:
        """Convert to dict for CSV writing."""
        return {
            "date": self.date,
            "symbol": self.symbol,
            "side": self.side,
            "signal_type": self.signal_type,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "shares": self.shares,
            "pnl": round(self.pnl, 2),
            "rr_ratio": round(self.rr_ratio, 2),
            "hold_time_minutes": round(self.hold_time_minutes, 1),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "exit_reason": self.exit_reason,
            "notes": self.notes,
        }
