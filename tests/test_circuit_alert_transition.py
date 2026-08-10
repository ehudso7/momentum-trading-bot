"""Circuit alerts are emitted on transitions, not on every polling tick."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from trading_bot.main import TradingBot
from trading_bot.risk.circuit_breaker import CircuitState


class TestCircuitAlertTransition(unittest.TestCase):
    def test_repeated_halted_ticks_send_one_alert_and_one_advisor_call(self):
        bot = TradingBot.__new__(TradingBot)
        bot._last_circuit_alert_state = None
        bot._circuit = Mock()
        bot._circuit.get_status.return_value = {
            "state": "halted",
            "halt_reason": "consecutive_losses: 3 >= 3",
            "daily_pnl": -134.34,
            "consecutive_losses": 3,
        }
        bot._notify = Mock()
        bot._advisor = Mock()
        bot._advisor.recommend_circuit_breaker_action.return_value = (
            SimpleNamespace(action="remain_halted", reasons=["loss streak"])
        )
        bot._portfolio = Mock()
        bot._portfolio.get_daily_journal_entries.return_value = []

        bot._notify_circuit_state_change(CircuitState.HALTED)
        bot._notify_circuit_state_change(CircuitState.HALTED)

        bot._notify.notify_circuit_breaker.assert_called_once_with(
            state="halted",
            reason="consecutive_losses: 3 >= 3",
            daily_pnl=-134.34,
            consecutive_losses=3,
        )
        bot._advisor.recommend_circuit_breaker_action.assert_called_once()

    def test_new_circuit_state_is_a_new_transition(self):
        bot = TradingBot.__new__(TradingBot)
        bot._last_circuit_alert_state = CircuitState.HALTED
        bot._circuit = Mock()
        bot._circuit.get_status.return_value = {
            "state": "cooldown",
            "halt_reason": "api_errors: 5 in 5 minutes >= 5",
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
        }
        bot._notify = Mock()
        bot._advisor = Mock()
        bot._advisor.recommend_circuit_breaker_action.return_value = (
            SimpleNamespace(action="monitor", reasons=[])
        )
        bot._portfolio = Mock()
        bot._portfolio.get_daily_journal_entries.return_value = []

        bot._notify_circuit_state_change(CircuitState.COOLDOWN)

        bot._notify.notify_circuit_breaker.assert_called_once()
        self.assertEqual(bot._last_circuit_alert_state, CircuitState.COOLDOWN)


if __name__ == "__main__":
    unittest.main()
