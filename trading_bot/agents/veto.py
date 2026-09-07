"""
Deterministic rule veto for the agent gate.

``RuleVeto.evaluate`` is a pure function of a ``VetoContext``: no network,
no clock, no broker, no side effects. It returns a single ``AgentDecision``
whose ``reasons`` list every rule that fired, so a block is always
explainable from the record alone.

Policy:

- **Block** (size_multiplier 0.0) when any hard rule fires, or when the
  required context (symbol, advisor action, advisor confidence, circuit
  state) is missing. Missing context fails closed.
- **Reduce** (size_multiplier 0.5) when the advisor said ``reduce_size`` or
  the regime is hostile.
- **Allow** (size_multiplier 1.0) otherwise.

Optional checks whose inputs are ``None`` are skipped rather than crashing;
the veto never invents a value for a field it was not given.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trading_bot.agents.models import (
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_REDUCE,
    SOURCE_VETO,
    AgentDecision,
    VetoContext,
)

ADVISOR_SKIP = "skip"
ADVISOR_REDUCE = "reduce_size"
ADVISOR_ENTER = "enter"
_KNOWN_ADVISOR_ACTIONS = frozenset({ADVISOR_ENTER, ADVISOR_SKIP, ADVISOR_REDUCE})

REDUCE_MULTIPLIER = 0.5

# Regimes the rule-based advisor already treats as hostile
# (``TradingAdvisor.recommend_entry``); the veto mirrors that judgement.
DEFAULT_HOSTILE_REGIMES: tuple[str, ...] = ("trending_bearish", "high_volatility")


@dataclass(frozen=True)
class RuleVeto:
    """Deterministic pre-trade veto. Construct once; ``evaluate`` is pure."""

    min_advisor_confidence: float = 0.55
    hostile_regimes: tuple[str, ...] = field(default=DEFAULT_HOSTILE_REGIMES)

    def __post_init__(self) -> None:
        if not (0.0 <= self.min_advisor_confidence <= 1.0):
            raise ValueError("min_advisor_confidence must be within [0, 1]")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, ctx: VetoContext) -> AgentDecision:
        block_reasons = self._required_context_gaps(ctx)
        if not block_reasons:
            block_reasons = self._hard_rules(ctx)

        symbol = ctx.symbol or ""
        raw = {"symbol": symbol, "advisor_action": ctx.advisor_action}

        if block_reasons:
            return AgentDecision(
                decision=DECISION_BLOCK,
                source=SOURCE_VETO,
                reasons=block_reasons,
                size_multiplier=0.0,
                raw=raw,
            )

        reduce_reasons = self._reduce_rules(ctx)
        if reduce_reasons:
            return AgentDecision(
                decision=DECISION_REDUCE,
                source=SOURCE_VETO,
                reasons=reduce_reasons,
                size_multiplier=REDUCE_MULTIPLIER,
                raw=raw,
            )

        return AgentDecision(
            decision=DECISION_ALLOW,
            source=SOURCE_VETO,
            reasons=["veto_passed"],
            size_multiplier=1.0,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    @staticmethod
    def _required_context_gaps(ctx: VetoContext) -> list[str]:
        """Fields without which no entry may be judged. Missing → block."""
        gaps: list[str] = []
        if not ctx.symbol:
            gaps.append("incomplete_context:symbol")
        if ctx.advisor_action is None:
            gaps.append("incomplete_context:advisor_action")
        elif ctx.advisor_action not in _KNOWN_ADVISOR_ACTIONS:
            gaps.append(f"unknown_advisor_action:{ctx.advisor_action}")
        if ctx.advisor_confidence is None:
            gaps.append("incomplete_context:advisor_confidence")
        if ctx.circuit_ok is None:
            gaps.append("incomplete_context:circuit_ok")
        return gaps

    def _hard_rules(self, ctx: VetoContext) -> list[str]:
        """Every hard rule is evaluated so the record explains the full block."""
        reasons: list[str] = []

        # Circuit breaker (already checked first in the tick; belt and braces).
        if ctx.circuit_ok is False:
            reasons.append(f"circuit_breaker:{ctx.circuit_state or 'not_ok'}")

        # Session validity.
        if ctx.market_open is False:
            reasons.append("session_closed")
        if ctx.near_hard_exit is True:
            reasons.append("near_hard_exit")

        # Advisor verdict and confidence.
        if ctx.advisor_action == ADVISOR_SKIP:
            reasons.append("advisor_skip")
        assert ctx.advisor_confidence is not None  # guaranteed by required gaps
        if ctx.advisor_confidence < self.min_advisor_confidence:
            reasons.append(
                f"advisor_confidence:{ctx.advisor_confidence:.2f}"
                f"<{self.min_advisor_confidence:.2f}"
            )

        # Portfolio capacity and duplicates.
        if (
            ctx.open_positions is not None
            and ctx.max_open_positions is not None
            and ctx.open_positions >= ctx.max_open_positions
        ):
            reasons.append(
                f"max_positions:{ctx.open_positions}/{ctx.max_open_positions}"
            )
        if ctx.symbol and ctx.symbol in ctx.held_symbols:
            reasons.append("already_held")

        # Gap beyond the scanner's absolute ceiling (the advisor only
        # reduces size above its extended-gap threshold; this is the hard cap).
        if (
            ctx.gap_pct is not None
            and ctx.max_gap_pct is not None
            and ctx.gap_pct > ctx.max_gap_pct
        ):
            reasons.append(f"gap_extreme:{ctx.gap_pct:.1f}>{ctx.max_gap_pct:.1f}")

        # Scanner bounds re-checked on the fields the scan result carries.
        if ctx.price is not None:
            if ctx.min_price is not None and ctx.price < ctx.min_price:
                reasons.append(f"price_below_min:{ctx.price:.2f}<{ctx.min_price:.2f}")
            if ctx.max_price is not None and ctx.price > ctx.max_price:
                reasons.append(f"price_above_max:{ctx.price:.2f}>{ctx.max_price:.2f}")
        if (
            ctx.relative_volume is not None
            and ctx.min_relative_volume is not None
            and ctx.relative_volume < ctx.min_relative_volume
        ):
            reasons.append(
                f"rvol_below_min:{ctx.relative_volume:.1f}<{ctx.min_relative_volume:.1f}"
            )
        if (
            ctx.float_shares is not None
            and ctx.max_float_shares is not None
            and ctx.float_shares > ctx.max_float_shares
        ):
            reasons.append(
                f"float_above_max:{ctx.float_shares}>{ctx.max_float_shares}"
            )
        if (
            ctx.spread_pct is not None
            and ctx.max_spread_pct is not None
            and ctx.spread_pct > ctx.max_spread_pct
        ):
            reasons.append(
                f"spread_above_max:{ctx.spread_pct:.2f}>{ctx.max_spread_pct:.2f}"
            )

        # PDT: accounts under the threshold get at most N day trades in the
        # rolling window. Only enforced when the broker reported a count.
        if (
            ctx.day_trade_count is not None
            and ctx.equity is not None
            and ctx.pdt_equity_threshold is not None
            and ctx.equity < ctx.pdt_equity_threshold
            and ctx.day_trade_count >= ctx.pdt_max_day_trades
        ):
            reasons.append(
                f"pdt_limit:{ctx.day_trade_count}/{ctx.pdt_max_day_trades}"
            )

        return reasons

    def _reduce_rules(self, ctx: VetoContext) -> list[str]:
        reasons: list[str] = []
        if ctx.advisor_action == ADVISOR_REDUCE:
            reasons.append("advisor_reduce_size")
        if ctx.regime is not None and ctx.regime in self.hostile_regimes:
            reasons.append(f"hostile_regime:{ctx.regime}")
        return reasons
