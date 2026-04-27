from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class FeatureSnapshot:
    """Immutable snapshot of market + symbol features at decision time."""
    symbol: str
    timestamp: datetime
    price: float
    gap_pct: float
    relative_volume: float
    volatility: float
    regime: str


@dataclass
class SignalDecision:
    """Final decision produced by the Core boundary."""
    symbol: str
    timestamp: datetime
    action: str  # buy | skip
    confidence: float
    reason: Optional[str] = None


@dataclass
class ExecutionIntent:
    """Represents a trade intent before hitting broker layer."""
    symbol: str
    action: str
    shares: int
    entry_price: float
    stop_price: float
