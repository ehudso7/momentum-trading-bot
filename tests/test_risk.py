"""
Tests for position sizing and risk management.

This is the MOST critical test file. Every edge case in position
sizing must be validated to prevent account blowup.
"""

from __future__ import annotations

import pytest

from trading_bot.config.settings import RiskConfig
from trading_bot.models.domain import (
    OrderSide,
    PositionInfo,
    PositionStatus,
    SignalType,
)
from trading_bot.risk.circuit_breaker import CircuitBreaker, CircuitState
from trading_bot.risk.position_sizer import PositionSizer


class TestPositionSizer:
    """Test position sizing calculations."""

    def test_basic_sizing(self, risk_config: RiskConfig):
        """equity=25000, risk=1%, entry=10, stop=9 -> risk$=250, shares=250."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=9.0,
            current_positions=[],
            buying_power=100_000.0,
        )

        assert result.approved is True
        assert result.shares == 250  # 250 / (10-9) = 250
        assert result.risk_dollars == 250.0

    def test_wider_stop_fewer_shares(self, risk_config: RiskConfig):
        """Wider stop distance means fewer shares."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=8.0,  # $2 stop distance
            current_positions=[],
            buying_power=100_000.0,
        )

        assert result.approved is True
        assert result.shares == 125  # 250 / 2 = 125

    def test_zero_stop_distance_rejected(self, risk_config: RiskConfig):
        """entry == stop -> rejected (division by zero prevented)."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=10.0,  # Zero distance
            current_positions=[],
            buying_power=100_000.0,
        )

        assert result.approved is False
        assert "invalid_stop_distance" in result.reason

    def test_negative_entry_rejected(self, risk_config: RiskConfig):
        """Negative entry price rejected."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            equity=25_000.0,
            entry_price=-1.0,
            stop_price=-2.0,
            current_positions=[],
            buying_power=100_000.0,
        )

        assert result.approved is False

    def test_max_positions_rejected(self, risk_config: RiskConfig):
        """With 4 open positions (max), new trade rejected."""
        sizer = PositionSizer(risk_config)

        # Create 4 fake open positions
        positions = [
            _make_position(f"SYM{i}", shares=50) for i in range(4)
        ]

        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=9.0,
            current_positions=positions,
            buying_power=100_000.0,
        )

        assert result.approved is False
        assert "max_positions_reached" in result.reason

    def test_daily_risk_limit(self, risk_config: RiskConfig):
        """Accumulated risk exceeds max_daily_risk_pct -> rejected."""
        sizer = PositionSizer(risk_config)

        # Use up most of daily risk budget (3% of 25000 = 750)
        sizer.record_trade_risk(600.0)

        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=9.0,  # Risk = 250, total would be 850 > 750
            current_positions=[],
            buying_power=100_000.0,
        )

        assert result.approved is False
        assert "daily_risk_exceeded" in result.reason

    def test_leverage_limit(self, risk_config: RiskConfig):
        """Position cost would exceed 4x leverage -> shares reduced."""
        sizer = PositionSizer(risk_config)

        # Already have large positions using most of the 4x leverage
        positions = [
            _make_position("BIG", shares=9000, price=10.0, current_price=10.0)
        ]

        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=9.5,  # Would want 500 shares = $5000
            current_positions=positions,
            buying_power=10_000.0,
        )

        # Should either be reduced or rejected
        if result.approved:
            total_value = (9000 * 10.0) + (result.shares * 10.0)
            assert total_value / 25_000.0 <= 4.0

    def test_pdt_warning(self, risk_config: RiskConfig):
        """Account under $25k with trades -> PDT warning."""
        sizer = PositionSizer(risk_config)
        sizer.record_day_trade()
        sizer.record_day_trade()

        result = sizer.calculate(
            equity=20_000.0,  # Under PDT threshold
            entry_price=10.0,
            stop_price=9.0,
            current_positions=[],
            buying_power=80_000.0,
        )

        assert result.approved is True
        assert any("PDT" in w for w in result.warnings)

    def test_small_account_sizing(self, risk_config: RiskConfig):
        """Small $500 account still gets valid sizing."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            equity=500.0,
            entry_price=5.0,
            stop_price=4.50,
            current_positions=[],
            buying_power=2_000.0,
        )

        assert result.approved is True
        assert result.shares == 10  # risk=5, distance=0.50, shares=10
        assert result.risk_dollars == 5.0

    def test_daily_reset(self, risk_config: RiskConfig):
        """Daily reset clears risk budget."""
        sizer = PositionSizer(risk_config)
        sizer.record_trade_risk(700.0)
        sizer.reset_daily()

        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=9.0,
            current_positions=[],
            buying_power=100_000.0,
        )

        assert result.approved is True

    def test_insufficient_buying_power(self, risk_config: RiskConfig):
        """Buying power too low -> shares reduced."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            equity=25_000.0,
            entry_price=10.0,
            stop_price=9.0,
            current_positions=[],
            buying_power=500.0,  # Can only afford 50 shares
        )

        if result.approved:
            assert result.shares <= 50


class TestCircuitBreaker:
    """Test circuit breaker safety mechanisms."""

    def test_initial_state_normal(self, risk_config: RiskConfig):
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(25_000.0)
        assert cb.state == CircuitState.NORMAL
        assert cb.is_trading_allowed is True

    def test_drawdown_halts_trading(self, risk_config: RiskConfig):
        """>5% daily drawdown halts trading."""
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(25_000.0)

        # Record losses totaling >5% of 25000 = >$1250
        cb.record_trade_result(-500.0)
        assert cb.is_trading_allowed is True

        cb.record_trade_result(-800.0)  # Total: -1300 > 1250
        assert cb.state == CircuitState.HALTED
        assert cb.is_trading_allowed is False

    def test_consecutive_losses_halt(self, risk_config: RiskConfig):
        """5 consecutive losses halts trading."""
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(100_000.0)  # Large equity so drawdown doesn't trigger

        for i in range(4):
            cb.record_trade_result(-10.0)
            assert cb.is_trading_allowed is True

        cb.record_trade_result(-10.0)  # 5th consecutive loss
        assert cb.state == CircuitState.HALTED

    def test_win_resets_consecutive_losses(self, risk_config: RiskConfig):
        """A winning trade resets the consecutive loss counter."""
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(100_000.0)

        for i in range(4):
            cb.record_trade_result(-10.0)

        cb.record_trade_result(50.0)  # Win resets counter
        assert cb.is_trading_allowed is True

        cb.record_trade_result(-10.0)
        assert cb.is_trading_allowed is True  # Only 1 loss, not 5

    def test_api_errors_halt(self, risk_config: RiskConfig):
        """5 API errors in 5 minutes halts trading."""
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(25_000.0)

        for i in range(4):
            cb.record_api_error()
            assert cb.is_trading_allowed is True

        cb.record_api_error()  # 5th error
        assert cb.state == CircuitState.HALTED

    def test_daily_reset_clears_state(self, risk_config: RiskConfig):
        """Daily reset restores normal trading."""
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(25_000.0)
        cb.record_trade_result(-2000.0)  # Trigger halt
        assert cb.state == CircuitState.HALTED

        cb.reset_daily(23_000.0)  # New day
        assert cb.state == CircuitState.NORMAL
        assert cb.is_trading_allowed is True

    def test_status_report(self, risk_config: RiskConfig):
        """Status dict contains expected keys."""
        cb = CircuitBreaker(risk_config)
        cb.reset_daily(25_000.0)
        cb.record_trade_result(-100.0)

        status = cb.get_status()
        assert "state" in status
        assert "daily_pnl" in status
        assert "consecutive_losses" in status
        assert status["daily_pnl"] == -100.0


def _make_position(
    symbol: str,
    shares: int = 100,
    price: float = 10.0,
    current_price: float = 10.0,
) -> PositionInfo:
    """Helper to create a mock position."""
    from datetime import datetime

    return PositionInfo(
        symbol=symbol,
        side=OrderSide.BUY,
        entry_price=price,
        current_price=current_price,
        shares=shares,
        shares_remaining=shares,
        stop_price=price * 0.95,
        target_prices=[price * 1.05, price * 1.10],
        status=PositionStatus.OPEN,
        pnl_unrealized=shares * (current_price - price),
        pnl_realized=0.0,
        scale_outs_completed=0,
        entry_time=datetime(2024, 6, 15, 10, 30),
        signal_type=SignalType.VWAP_PULLBACK,
    )
