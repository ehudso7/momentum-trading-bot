"""
Tests for the circuit breaker's auto-recovery logic.

Covers the HALTED -> COOLDOWN -> WARNING transition flow,
error_type categorization, error summary reporting, and
re-halting from COOLDOWN when new triggers occur.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from trading_bot.config.settings import RiskConfig
from trading_bot.risk.circuit_breaker import CircuitBreaker, CircuitState
from trading_bot.utils.helpers import now_et


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def risk_config() -> RiskConfig:
    """Default risk configuration for circuit breaker tests."""
    return RiskConfig()


@pytest.fixture
def cb(risk_config: RiskConfig) -> CircuitBreaker:
    """A circuit breaker initialised with 10-minute cooldown for fast tests."""
    breaker = CircuitBreaker(risk_config, cooldown_minutes=10)
    breaker.reset_daily(25_000.0)
    return breaker


@pytest.fixture
def halted_cb(cb: CircuitBreaker) -> CircuitBreaker:
    """A circuit breaker already in HALTED state via consecutive losses."""
    # Use large equity so drawdown doesn't trigger; hit consecutive losses instead.
    cb.reset_daily(1_000_000.0)
    for _ in range(cb._config.max_consecutive_losses):
        cb.record_trade_result(-1.0)
    assert cb.state == CircuitState.HALTED
    return cb


# ---------------------------------------------------------------------------
# HALTED -> COOLDOWN transition
# ---------------------------------------------------------------------------

class TestHaltedToCooldown:
    """After cooldown_minutes in HALTED, check() should transition to COOLDOWN."""

    def test_stays_halted_before_cooldown_elapses(self, halted_cb: CircuitBreaker):
        """If not enough time has passed, state remains HALTED."""
        # Simulate time just before cooldown expires (9 minutes for a 10-min cooldown)
        future = halted_cb._halted_at + timedelta(minutes=9)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            state = halted_cb.check()
        assert state == CircuitState.HALTED

    def test_transitions_to_cooldown_after_cooldown_minutes(self, halted_cb: CircuitBreaker):
        """A cleared trigger may enter COOLDOWN at the configured time."""
        halted_cb._consecutive_losses = 0
        future = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            state = halted_cb.check()
        assert state == CircuitState.COOLDOWN

    def test_transitions_to_cooldown_well_past_cooldown(self, halted_cb: CircuitBreaker):
        """A cleared trigger can recover even well after the minimum wait."""
        halted_cb._consecutive_losses = 0
        future = halted_cb._halted_at + timedelta(minutes=60)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            state = halted_cb.check()
        assert state == CircuitState.COOLDOWN

    def test_cooldown_entered_at_is_set(self, halted_cb: CircuitBreaker):
        """_cooldown_entered_at is populated when entering COOLDOWN."""
        halted_cb._consecutive_losses = 0
        future = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            halted_cb.check()
        assert halted_cb._cooldown_entered_at is not None

    def test_trading_blocked_in_cooldown(self, halted_cb: CircuitBreaker):
        """COOLDOWN permits monitoring but never new entries."""
        halted_cb._consecutive_losses = 0
        future = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            halted_cb.check()
        assert halted_cb.is_trading_allowed is False

    def test_persistent_loss_trigger_stays_halted_after_wait(
        self, halted_cb: CircuitBreaker
    ):
        """Elapsed time alone cannot clear a still-active loss streak."""
        future = halted_cb._halted_at + timedelta(minutes=60)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            state = halted_cb.check()
        assert state == CircuitState.HALTED
        assert halted_cb._cooldown_entered_at is None
        assert "consecutive_losses" in halted_cb.get_status()["halt_reason"]

    def test_halted_at_not_none_required(self, risk_config: RiskConfig):
        """If _halted_at is None while HALTED, no recovery happens."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=1)
        cb.reset_daily(25_000.0)
        # Force HALTED without going through _halt() so _halted_at stays None
        cb._state = CircuitState.HALTED
        cb._halted_at = None

        future = now_et() + timedelta(minutes=5)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            state = cb.check()
        # Should remain HALTED because _halted_at is None
        assert state == CircuitState.HALTED


# ---------------------------------------------------------------------------
# COOLDOWN -> WARNING transition
# ---------------------------------------------------------------------------

class TestCooldownToWarning:
    """After 5 minutes in COOLDOWN with no new triggers, check() transitions to WARNING."""

    def test_stays_cooldown_before_5_minutes(self, halted_cb: CircuitBreaker):
        """If less than 5 minutes have passed in COOLDOWN, state stays COOLDOWN."""
        # First: transition to COOLDOWN
        halted_cb._consecutive_losses = 0
        cooldown_entry = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=cooldown_entry):
            halted_cb.check()
        assert halted_cb.state == CircuitState.COOLDOWN

        # Now check 4 minutes into COOLDOWN -- should still be COOLDOWN
        four_min_later = cooldown_entry + timedelta(minutes=4)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=four_min_later):
            state = halted_cb.check()
        assert state == CircuitState.COOLDOWN

    def test_transitions_to_warning_after_5_minutes(self, halted_cb: CircuitBreaker):
        """After 5 minutes in COOLDOWN, check() transitions to WARNING."""
        # Transition to COOLDOWN
        halted_cb._consecutive_losses = 0
        cooldown_entry = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=cooldown_entry):
            halted_cb.check()
        assert halted_cb.state == CircuitState.COOLDOWN

        # Now 5 minutes later
        five_min_later = cooldown_entry + timedelta(minutes=5)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=five_min_later):
            state = halted_cb.check()
        assert state == CircuitState.WARNING

    def test_warning_clears_cooldown_entered_at(self, halted_cb: CircuitBreaker):
        """Transitioning to WARNING clears _cooldown_entered_at."""
        # Transition HALTED -> COOLDOWN -> WARNING
        halted_cb._consecutive_losses = 0
        cooldown_entry = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=cooldown_entry):
            halted_cb.check()

        warning_time = cooldown_entry + timedelta(minutes=5)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=warning_time):
            halted_cb.check()

        assert halted_cb.state == CircuitState.WARNING
        assert halted_cb._cooldown_entered_at is None

    def test_trading_allowed_in_warning(self, halted_cb: CircuitBreaker):
        """WARNING allows trading."""
        # Full recovery: HALTED -> COOLDOWN -> WARNING
        halted_cb._consecutive_losses = 0
        cooldown_entry = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=cooldown_entry):
            halted_cb.check()

        warning_time = cooldown_entry + timedelta(minutes=5)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=warning_time):
            halted_cb.check()

        assert halted_cb.is_trading_allowed is True

    def test_cooldown_entered_at_none_prevents_transition(self, risk_config: RiskConfig):
        """If _cooldown_entered_at is None while in COOLDOWN, no WARNING transition."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=1)
        cb.reset_daily(25_000.0)
        cb._state = CircuitState.COOLDOWN
        cb._cooldown_entered_at = None

        future = now_et() + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            state = cb.check()
        # Remains COOLDOWN because _cooldown_entered_at is None
        assert state == CircuitState.COOLDOWN


# ---------------------------------------------------------------------------
# Full HALTED -> COOLDOWN -> WARNING cycle
# ---------------------------------------------------------------------------

class TestFullRecoveryCycle:
    """End-to-end auto-recovery from HALTED through COOLDOWN to WARNING."""

    def test_full_halted_cooldown_warning_cycle(self, risk_config: RiskConfig):
        """HALTED -> (wait) -> COOLDOWN -> (wait) -> WARNING in sequence."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=30)
        cb.reset_daily(1_000_000.0)

        # Trigger halt via consecutive losses
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        assert cb.state == CircuitState.HALTED
        assert cb.is_trading_allowed is False

        halted_at = cb._halted_at

        # 29 minutes later: still HALTED
        t1 = halted_at + timedelta(minutes=29)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t1):
            assert cb.check() == CircuitState.HALTED

        # Clear the original trigger before recovery is considered.
        cb._consecutive_losses = 0

        # 30 minutes later: COOLDOWN (new entries remain blocked)
        t2 = halted_at + timedelta(minutes=30)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t2):
            assert cb.check() == CircuitState.COOLDOWN
        assert cb.is_trading_allowed is False

        # 4 minutes into COOLDOWN: still COOLDOWN
        t3 = t2 + timedelta(minutes=4)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t3):
            assert cb.check() == CircuitState.COOLDOWN

        # 5 minutes into COOLDOWN: WARNING
        t4 = t2 + timedelta(minutes=5)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t4):
            assert cb.check() == CircuitState.WARNING
        assert cb.is_trading_allowed is True


# ---------------------------------------------------------------------------
# record_api_error with error_type categorization
# ---------------------------------------------------------------------------

class TestRecordApiErrorWithType:
    """record_api_error should categorize errors when error_type is provided."""

    def test_error_type_none_does_not_create_category(self, cb: CircuitBreaker):
        """Calling without error_type doesn't populate _api_error_counts."""
        cb.record_api_error()
        assert cb.get_error_summary() == {}

    def test_single_error_type(self, cb: CircuitBreaker):
        """A single categorised error shows count of 1."""
        cb.record_api_error(error_type="timeout")
        summary = cb.get_error_summary()
        assert summary == {"timeout": 1}

    def test_multiple_same_type(self, cb: CircuitBreaker):
        """Multiple errors of the same type accumulate."""
        for _ in range(3):
            cb.record_api_error(error_type="rate_limit")
        summary = cb.get_error_summary()
        assert summary == {"rate_limit": 3}

    def test_multiple_different_types(self, cb: CircuitBreaker):
        """Different error types are counted independently."""
        cb.record_api_error(error_type="timeout")
        cb.record_api_error(error_type="timeout")
        cb.record_api_error(error_type="rate_limit")
        cb.record_api_error(error_type="auth")
        cb.record_api_error(error_type="server_error")

        summary = cb.get_error_summary()
        assert summary["timeout"] == 2
        assert summary["rate_limit"] == 1
        assert summary["auth"] == 1
        assert summary["server_error"] == 1

    def test_error_type_with_none_mixed(self, cb: CircuitBreaker):
        """Mixing typed and untyped errors: only typed ones appear in summary."""
        cb.record_api_error(error_type="timeout")
        cb.record_api_error()  # No type
        cb.record_api_error(error_type="timeout")

        summary = cb.get_error_summary()
        assert summary == {"timeout": 2}

    def test_api_errors_still_count_toward_halt(self, cb: CircuitBreaker):
        """Typed errors still increment the sliding window and can trigger halt."""
        threshold = cb._config.api_error_halt_threshold  # default 5
        for i in range(threshold):
            cb.record_api_error(error_type="server_error")

        assert cb.state == CircuitState.HALTED


# ---------------------------------------------------------------------------
# get_error_summary
# ---------------------------------------------------------------------------

class TestGetErrorSummary:
    """get_error_summary returns a categorized dict of error counts."""

    def test_empty_on_fresh_breaker(self, cb: CircuitBreaker):
        """No errors recorded -> empty dict."""
        assert cb.get_error_summary() == {}

    def test_returns_plain_dict_not_defaultdict(self, cb: CircuitBreaker):
        """The returned value is a regular dict, not a defaultdict."""
        cb.record_api_error(error_type="timeout")
        summary = cb.get_error_summary()
        assert type(summary) is dict

    def test_summary_reflects_all_recorded_types(self, cb: CircuitBreaker):
        """Every recorded type appears with correct count."""
        cb.record_api_error(error_type="timeout")
        cb.record_api_error(error_type="rate_limit")
        cb.record_api_error(error_type="rate_limit")
        cb.record_api_error(error_type="auth")

        summary = cb.get_error_summary()
        assert len(summary) == 3
        assert summary["timeout"] == 1
        assert summary["rate_limit"] == 2
        assert summary["auth"] == 1

    def test_summary_cleared_on_daily_reset(self, cb: CircuitBreaker):
        """Daily reset clears the error summary."""
        cb.record_api_error(error_type="timeout")
        cb.record_api_error(error_type="auth")
        cb.reset_daily(25_000.0)

        assert cb.get_error_summary() == {}

    def test_summary_cleared_on_force_reset(self, cb: CircuitBreaker):
        """Force reset also clears the error summary."""
        cb.record_api_error(error_type="server_error")
        cb.force_reset()

        assert cb.get_error_summary() == {}

    def test_summary_in_status_dict(self, cb: CircuitBreaker):
        """get_status() includes the error summary under 'api_error_summary'."""
        cb.record_api_error(error_type="rate_limit")
        status = cb.get_status()
        assert "api_error_summary" in status
        assert status["api_error_summary"]["rate_limit"] == 1


# ---------------------------------------------------------------------------
# Re-halting from COOLDOWN
# ---------------------------------------------------------------------------

class TestRehaltFromCooldown:
    """New halt triggers during COOLDOWN should return to HALTED."""

    def _enter_cooldown(self, cb: CircuitBreaker) -> CircuitBreaker:
        """Helper: drive cb from HALTED into COOLDOWN state."""
        halted_at = cb._halted_at
        cb._consecutive_losses = 0
        future = halted_at + timedelta(minutes=cb._cooldown_minutes)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            cb.check()
        assert cb.state == CircuitState.COOLDOWN
        return cb

    def test_consecutive_losses_during_cooldown_rehalts(self, risk_config: RiskConfig):
        """Hitting max consecutive losses during COOLDOWN re-triggers HALTED."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=10)
        cb.reset_daily(1_000_000.0)

        # Get to HALTED, then COOLDOWN
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        assert cb.state == CircuitState.HALTED
        self._enter_cooldown(cb)

        # Reset consecutive counter to simulate fresh trades in COOLDOWN
        cb._consecutive_losses = 0

        # Now record new consecutive losses in COOLDOWN
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)

        assert cb.state == CircuitState.HALTED

    def test_drawdown_during_cooldown_rehalts(self, risk_config: RiskConfig):
        """Exceeding drawdown during COOLDOWN re-triggers HALTED."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=10)
        cb.reset_daily(10_000.0)

        # Halt via consecutive losses (small losses, no drawdown trigger)
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        assert cb.state == CircuitState.HALTED
        self._enter_cooldown(cb)

        # Now record a big loss that triggers drawdown (>5% of 10000 = >500)
        # Current daily PnL is already -5.0 from the consecutive losses
        cb.record_trade_result(-600.0)

        assert cb.state == CircuitState.HALTED

    def test_api_errors_during_cooldown_rehalts(self, risk_config: RiskConfig):
        """Too many API errors during COOLDOWN re-triggers HALTED."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=10)
        cb.reset_daily(1_000_000.0)

        # Halt via consecutive losses, then move to COOLDOWN
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        assert cb.state == CircuitState.HALTED
        self._enter_cooldown(cb)

        # Reset consecutive losses so that trigger doesn't come from there
        cb._consecutive_losses = 0

        # Fire enough API errors to breach threshold
        for _ in range(risk_config.api_error_halt_threshold):
            cb.record_api_error(error_type="server_error")

        assert cb.state == CircuitState.HALTED

    def test_rehalt_sets_new_halted_at(self, risk_config: RiskConfig):
        """When re-halted from COOLDOWN, _halted_at is updated to the new time."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=10)
        cb.reset_daily(1_000_000.0)

        # Initial halt
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        original_halted_at = cb._halted_at

        # Move to COOLDOWN
        self._enter_cooldown(cb)
        cb._consecutive_losses = 0

        # Re-halt
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        assert cb.state == CircuitState.HALTED

        # _halted_at should have been updated to a newer timestamp
        assert cb._halted_at is not None
        assert cb._halted_at >= original_halted_at


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Miscellaneous edge cases in recovery logic."""

    def test_force_reset_returns_to_normal(self, halted_cb: CircuitBreaker):
        """force_reset() always returns to NORMAL regardless of state."""
        halted_cb.force_reset()
        assert halted_cb.state == CircuitState.NORMAL
        assert halted_cb._halted_at is None
        assert halted_cb._cooldown_entered_at is None

    def test_daily_reset_clears_recovery_timestamps(self, halted_cb: CircuitBreaker):
        """reset_daily() clears both halted_at and cooldown_entered_at."""
        halted_cb.reset_daily(30_000.0)
        assert halted_cb.state == CircuitState.NORMAL
        assert halted_cb._halted_at is None
        assert halted_cb._cooldown_entered_at is None

    def test_custom_cooldown_minutes(self, risk_config: RiskConfig):
        """Non-default cooldown_minutes is respected."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=60)
        cb.reset_daily(1_000_000.0)

        # Trigger halt
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)
        assert cb.state == CircuitState.HALTED

        # 59 minutes: still HALTED
        t1 = cb._halted_at + timedelta(minutes=59)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t1):
            assert cb.check() == CircuitState.HALTED

        # Clear the trigger; elapsed time alone is insufficient.
        cb._consecutive_losses = 0

        # 60 minutes: COOLDOWN
        t2 = cb._halted_at + timedelta(minutes=60)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t2):
            assert cb.check() == CircuitState.COOLDOWN

    def test_warning_state_does_not_auto_become_normal(self, risk_config: RiskConfig):
        """WARNING requires a daily or force reset to return to NORMAL."""
        cb = CircuitBreaker(risk_config, cooldown_minutes=1)
        cb.reset_daily(1_000_000.0)

        # HALTED -> COOLDOWN -> WARNING
        for _ in range(risk_config.max_consecutive_losses):
            cb.record_trade_result(-1.0)

        cb._consecutive_losses = 0
        t1 = cb._halted_at + timedelta(minutes=1)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t1):
            cb.check()
        assert cb.state == CircuitState.COOLDOWN

        # Clear the original halt trigger so the COOLDOWN->WARNING check
        # doesn't immediately re-halt from consecutive losses.
        cb._consecutive_losses = 0

        t2 = t1 + timedelta(minutes=5)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t2):
            cb.check()
        assert cb.state == CircuitState.WARNING

        # Many more check() calls later, still WARNING (not NORMAL)
        t3 = t2 + timedelta(hours=2)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=t3):
            cb.check()
        assert cb.state == CircuitState.WARNING

    def test_get_status_during_cooldown(self, halted_cb: CircuitBreaker):
        """get_status() reports COOLDOWN state and non-null cooldown_entered_at."""
        halted_cb._consecutive_losses = 0
        future = halted_cb._halted_at + timedelta(minutes=10)
        with patch("trading_bot.risk.circuit_breaker.now_et", return_value=future):
            halted_cb.check()

        status = halted_cb.get_status()
        assert status["state"] == "cooldown"
        assert status["cooldown_entered_at"] is not None
        assert status["cooldown_minutes"] == 10
