"""
Position sizing and pre-trade risk validation.

The MOST safety-critical module in the entire system.
Implements: shares = risk_dollars / stop_distance
with guards for max positions, daily risk, leverage, and PDT.
"""

from __future__ import annotations

import math

import structlog

from trading_bot.config.settings import RiskConfig
from trading_bot.models.domain import PositionInfo, RiskCheckResult

log = structlog.get_logger(__name__)


class PositionSizer:
    """Calculates position size and performs pre-trade risk checks."""

    def __init__(self, config: RiskConfig):
        self._config = config
        self._daily_risk_used: float = 0.0
        self._day_trades_today: int = 0

    def calculate(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        current_positions: list[PositionInfo],
        buying_power: float,
    ) -> RiskCheckResult:
        """
        Full pre-trade risk check pipeline.

        Checks (in order):
        1. Stop distance validity
        2. Position size calculation
        3. Max open positions
        4. Daily risk budget
        5. Leverage limit
        6. PDT warning

        Returns RiskCheckResult with approved/rejected status.
        """
        warnings: list[str] = []

        # 1. Validate stop distance
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return RiskCheckResult(
                approved=False,
                shares=0,
                risk_dollars=0.0,
                reason="invalid_stop_distance: entry equals stop",
                leverage_used=0.0,
                positions_count=len(current_positions),
            )

        if entry_price <= 0:
            return RiskCheckResult(
                approved=False,
                shares=0,
                risk_dollars=0.0,
                reason="invalid_entry_price",
                leverage_used=0.0,
                positions_count=len(current_positions),
            )

        # 1b. Enforce minimum stop distance floor (prevents micro-stops
        # that create outsized positions via shares = risk$ / tiny_stop).
        min_stop_distance = entry_price * (self._config.min_stop_distance_pct / 100.0)
        if stop_distance < min_stop_distance:
            log.warning(
                "risk.stop_distance_floored",
                symbol="unknown",
                original_distance=round(stop_distance, 4),
                min_distance=round(min_stop_distance, 4),
                entry_price=entry_price,
                stop_price=stop_price,
            )
            stop_distance = min_stop_distance

        # 2. Calculate position size
        risk_dollars = equity * (self._config.risk_per_trade_pct / 100.0)
        shares = math.floor(risk_dollars / stop_distance)

        if shares <= 0:
            return RiskCheckResult(
                approved=False,
                shares=0,
                risk_dollars=risk_dollars,
                reason="position_too_small: risk amount insufficient for stop distance",
                leverage_used=0.0,
                positions_count=len(current_positions),
            )

        position_cost = shares * entry_price

        # 2b. Cap single-position value to max_position_value_pct of equity.
        # This prevents a tight stop from creating a position that is a
        # disproportionate share of the account, even within leverage limits.
        max_position_value = equity * (self._config.max_position_value_pct / 100.0)
        if position_cost > max_position_value:
            capped_shares = math.floor(max_position_value / entry_price)
            if capped_shares <= 0:
                return RiskCheckResult(
                    approved=False,
                    shares=0,
                    risk_dollars=risk_dollars,
                    reason="position_value_cap_reduces_to_zero_shares",
                    leverage_used=0.0,
                    positions_count=len(current_positions),
                )
            log.info(
                "risk.position_value_capped",
                original_shares=shares,
                capped_shares=capped_shares,
                position_value=round(capped_shares * entry_price, 2),
                max_pct=self._config.max_position_value_pct,
                equity=round(equity, 2),
            )
            shares = capped_shares
            position_cost = shares * entry_price
            warnings.append(
                f"shares_capped_for_concentration: {shares} shares "
                f"({self._config.max_position_value_pct}% equity cap)"
            )

        # 3. Max open positions check
        if len(current_positions) >= self._config.max_open_positions:
            return RiskCheckResult(
                approved=False,
                shares=0,
                risk_dollars=risk_dollars,
                reason=f"max_positions_reached: {len(current_positions)}/{self._config.max_open_positions}",
                leverage_used=0.0,
                positions_count=len(current_positions),
            )

        # 4. Daily risk budget check (includes unrealized losses from open positions)
        unrealized_loss = sum(
            min(0.0, p.pnl_unrealized) for p in current_positions
        )
        total_daily_risk = self._daily_risk_used + risk_dollars + abs(unrealized_loss)
        max_daily_risk = equity * (self._config.max_daily_risk_pct / 100.0)
        if total_daily_risk > max_daily_risk:
            return RiskCheckResult(
                approved=False,
                shares=0,
                risk_dollars=risk_dollars,
                reason=f"daily_risk_exceeded: ${total_daily_risk:.2f} > ${max_daily_risk:.2f} (unrealized: ${abs(unrealized_loss):.2f})",
                leverage_used=0.0,
                positions_count=len(current_positions),
            )

        # 5. Leverage check
        total_position_value = sum(
            p.shares_remaining * p.current_price for p in current_positions
        )
        new_total = total_position_value + position_cost
        leverage_used = new_total / equity if equity > 0 else 0.0

        if leverage_used > self._config.max_leverage:
            # Reduce shares to fit within leverage limit
            max_position_cost = (self._config.max_leverage * equity) - total_position_value
            if max_position_cost <= 0:
                return RiskCheckResult(
                    approved=False,
                    shares=0,
                    risk_dollars=risk_dollars,
                    reason=f"leverage_exceeded: {leverage_used:.1f}x > {self._config.max_leverage:.1f}x",
                    leverage_used=leverage_used,
                    positions_count=len(current_positions),
                )
            shares = math.floor(max_position_cost / entry_price)
            if shares <= 0:
                return RiskCheckResult(
                    approved=False,
                    shares=0,
                    risk_dollars=risk_dollars,
                    reason="leverage_limit_reduces_to_zero_shares",
                    leverage_used=leverage_used,
                    positions_count=len(current_positions),
                )
            position_cost = shares * entry_price
            leverage_used = (total_position_value + position_cost) / equity
            warnings.append(
                f"shares_reduced_for_leverage: {shares} shares at {leverage_used:.1f}x"
            )

        # 6. Buying power check
        if position_cost > buying_power:
            shares = math.floor(buying_power / entry_price)
            if shares <= 0:
                return RiskCheckResult(
                    approved=False,
                    shares=0,
                    risk_dollars=risk_dollars,
                    reason="insufficient_buying_power",
                    leverage_used=leverage_used,
                    positions_count=len(current_positions),
                )
            position_cost = shares * entry_price
            warnings.append(f"shares_reduced_for_buying_power: {shares}")

        # 7. PDT check (warning only, does not reject)
        pdt_warning = self._check_pdt_status(equity)
        if pdt_warning:
            warnings.append(pdt_warning)

        # Approved
        actual_risk = shares * stop_distance
        log.info(
            "risk.approved",
            shares=shares,
            risk_dollars=round(actual_risk, 2),
            leverage=round(leverage_used, 2),
            warnings=warnings,
        )

        return RiskCheckResult(
            approved=True,
            shares=shares,
            risk_dollars=round(actual_risk, 2),
            reason="approved",
            leverage_used=round(leverage_used, 2),
            positions_count=len(current_positions) + 1,
            warnings=warnings,
        )

    def record_trade_risk(self, risk_dollars: float) -> None:
        """Record risk used for daily budget tracking."""
        self._daily_risk_used += risk_dollars

    def record_day_trade(self) -> None:
        """Increment day trade counter."""
        self._day_trades_today += 1

    def reset_daily(self) -> None:
        """Reset daily counters (called at start of each trading day)."""
        self._daily_risk_used = 0.0
        self._day_trades_today = 0

    def _check_pdt_status(self, equity: float) -> str | None:
        """
        Check Pattern Day Trader rule status.

        PDT rule: accounts under $25k are limited to 3 day trades
        per 5 rolling business days.
        """
        if equity >= self._config.pdt_equity_threshold:
            return None

        if self._day_trades_today >= 3:
            return (
                f"PDT_WARNING: Account equity ${equity:,.0f} is below "
                f"${self._config.pdt_equity_threshold:,.0f}. "
                f"Day trades today: {self._day_trades_today}/3. "
                f"Risk of PDT flag."
            )

        if self._day_trades_today >= 2:
            return (
                f"PDT_CAUTION: {self._day_trades_today} day trades used. "
                f"1 remaining before PDT risk."
            )

        return None
