"""Circuit alerts are emitted on transitions, not on every polling tick."""

from __future__ import annotations

import unittest
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading_bot.main import TradingBot
from trading_bot.portfolio.manager import PortfolioSafetyError
from trading_bot.risk.circuit_breaker import CircuitState


class TestCircuitAlertTransition(unittest.TestCase):
    def _loop_bot(self) -> TradingBot:
        bot = TradingBot.__new__(TradingBot)
        bot._broker = Mock()
        bot._broker.get_account_equity.return_value = 71_664.97
        bot._broker.close_all_positions.return_value = True
        bot._broker.get_positions.return_value = []
        bot._broker.get_last_close_fills.return_value = []
        bot._config = SimpleNamespace(starting_capital=71_664.97)
        bot._circuit = Mock()
        bot._circuit.is_trading_allowed = True
        bot._sizer = Mock()
        bot._portfolio = Mock()
        bot._portfolio.close_all.return_value = []
        bot._portfolio.finalize_verified_broker_flat.return_value = []
        bot._portfolio.get_open_positions.return_value = []
        bot._portfolio.get_daily_pnl.return_value = 0.0
        bot._health = Mock()
        bot._notify = Mock()
        bot._update_dashboard = Mock()
        bot._generate_daily_summary = Mock()
        bot._generate_daily_alpha_report = Mock()
        bot._get_adaptive_scan_interval = Mock(return_value=0)
        bot._shutdown_event = threading.Event()
        bot._last_circuit_alert_state = None
        bot._last_trading_date = None
        bot._rejected_signals = []
        return bot

    def test_startup_reconciliation_failure_aborts_before_first_tick(self):
        bot = self._loop_bot()
        bot._portfolio.reconcile_positions.side_effect = PortfolioSafetyError(
            "startup broker state could not be flattened"
        )
        bot._tick = Mock()

        with self.assertRaises(PortfolioSafetyError):
            bot._run_live_loop()

        bot._tick.assert_not_called()
        bot._notify.notify_error.assert_called_once()

    def test_startup_equity_failure_aborts_before_first_tick(self):
        bot = self._loop_bot()
        bot._broker.get_account_equity.side_effect = RuntimeError("account API down")
        bot._tick = Mock()

        with self.assertRaisesRegex(
            PortfolioSafetyError, "account equity could not be verified"
        ):
            bot._run_live_loop()

        bot._tick.assert_not_called()
        bot._circuit.reset_daily.assert_not_called()
        self.assertEqual(bot._broker.close_all_positions.call_count, 2)
        self.assertEqual(bot._broker.get_positions.call_count, 2)

    def test_startup_rejects_non_finite_or_non_positive_equity(self):
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.subTest(equity=invalid):
                bot = self._loop_bot()
                bot._broker.get_account_equity.return_value = invalid
                with self.assertRaisesRegex(
                    PortfolioSafetyError, "invalid account equity"
                ):
                    bot._strict_account_equity("startup")

    def test_stop_during_startup_is_not_cleared_or_overwritten(self):
        bot = self._loop_bot()
        bot._tick = Mock()
        bot._portfolio.reconcile_positions.side_effect = bot.stop

        bot._run_live_loop()

        bot._broker.get_account_equity.assert_not_called()
        bot._tick.assert_not_called()
        self.assertFalse(bot._running)
        self.assertTrue(bot._shutdown_event.is_set())

    def test_shutdown_gate_is_immediately_before_entry_submission(self):
        bot = self._loop_bot()
        bot._running = True
        bot._shutdown_event.set()
        signal = object()
        risk_result = object()

        result = bot._open_position_if_running(signal, risk_result)

        self.assertIsNone(result)
        bot._portfolio.open_position.assert_not_called()

    def test_runtime_portfolio_safety_error_stops_without_api_retry(self):
        bot = self._loop_bot()
        bot._tick = Mock(
            side_effect=PortfolioSafetyError("position protection lost")
        )

        bot._run_live_loop()

        bot._tick.assert_called_once_with()
        bot._circuit.record_api_error.assert_not_called()
        bot._notify.notify_error.assert_called_once_with(
            error_type="portfolio_safety",
            message="Portfolio safety halt: position protection lost",
        )
        self.assertFalse(bot._running)
        self.assertTrue(bot._shutdown_event.is_set())

    def test_shutdown_flatten_error_is_not_masked_by_empty_entries(self):
        bot = self._loop_bot()
        bot._shutdown_event.set()
        bot._flatten_all_and_verify = Mock(
            side_effect=PortfolioSafetyError("exact close fill missing")
        )

        with self.assertRaisesRegex(
            PortfolioSafetyError, "exact close fill missing"
        ):
            bot._run_live_loop()

        bot._flatten_all_and_verify.assert_called_once_with("shutdown")
        bot._generate_daily_summary.assert_called_once_with()
        bot._generate_daily_alpha_report.assert_called_once_with()
        self.assertFalse(bot._running)
        self.assertTrue(bot._shutdown_event.is_set())

    def test_safety_dashboard_failure_cannot_skip_final_cleanup(self):
        bot = self._loop_bot()
        bot._tick = Mock(
            side_effect=PortfolioSafetyError("position protection lost")
        )
        # Initial dashboard succeeds; safety reporting fails; final reporting
        # succeeds.  Broker cleanup must still execute from finally.
        bot._update_dashboard.side_effect = [
            None,
            RuntimeError("dashboard unavailable"),
            None,
        ]

        bot._run_live_loop()

        self.assertEqual(bot._broker.close_all_positions.call_count, 2)
        self.assertEqual(bot._broker.get_positions.call_count, 2)
        self.assertFalse(bot._running)
        self.assertTrue(bot._shutdown_event.is_set())

    def test_daily_reset_reconciles_then_reads_equity_then_commits(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-10"
        bot._starting_equity = 70_000.0
        order = []
        bot._portfolio.reconcile_positions.side_effect = lambda: order.append(
            "reconcile"
        )
        bot._broker.get_account_equity.side_effect = lambda: (
            order.append("equity") or 71_000.0
        )
        bot._circuit.reset_daily.side_effect = lambda equity: order.append(
            "circuit"
        )
        bot._sizer.reset_daily.side_effect = lambda: order.append("sizer")
        bot._portfolio.reset_daily.side_effect = lambda: order.append("portfolio")

        with patch(
            "trading_bot.main.now_et",
            return_value=datetime(2026, 8, 11, 0, 1),
        ):
            bot._check_daily_reset()

        self.assertEqual(
            order,
            ["reconcile", "equity", "circuit", "sizer", "portfolio"],
        )
        self.assertEqual(bot._starting_equity, 71_000.0)
        self.assertEqual(bot._last_trading_date, "2026-08-11")

    def test_daily_reset_equity_failure_does_not_commit_new_day(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-10"
        bot._starting_equity = 70_000.0
        bot._broker.get_account_equity.side_effect = RuntimeError("account API down")

        with patch(
            "trading_bot.main.now_et",
            return_value=datetime(2026, 8, 11, 0, 1),
        ):
            with self.assertRaisesRegex(
                PortfolioSafetyError, "account equity could not be verified"
            ):
                bot._check_daily_reset()

        bot._portfolio.reconcile_positions.assert_called_once_with()
        bot._circuit.reset_daily.assert_not_called()
        bot._sizer.reset_daily.assert_not_called()
        bot._portfolio.reset_daily.assert_not_called()
        self.assertEqual(bot._starting_equity, 70_000.0)
        self.assertEqual(bot._last_trading_date, "2026-08-10")

    def test_broker_close_all_false_is_unverified_flatten(self):
        bot = self._loop_bot()
        bot._broker.close_all_positions.return_value = False

        with self.assertRaisesRegex(
            PortfolioSafetyError, "did not confirm close_all_positions"
        ):
            bot._flatten_all_and_verify("test")

        bot._broker.get_positions.assert_not_called()

    def test_broker_positions_remaining_is_unverified_flatten(self):
        bot = self._loop_bot()
        bot._broker.get_positions.return_value = [
            {"symbol": "JWEL", "qty": 10}
        ]

        with self.assertRaisesRegex(
            PortfolioSafetyError, "broker positions remain"
        ):
            bot._flatten_all_and_verify("test")

    def test_broker_wide_flatten_precedes_local_position_bookkeeping(self):
        bot = self._loop_bot()
        order = []
        bot._portfolio.get_open_positions.return_value = [object()]
        bot._broker.close_all_positions.side_effect = lambda: (
            order.append("broker_close") or True
        )
        bot._broker.get_positions.side_effect = lambda: (
            order.append("broker_verify") or []
        )
        bot._portfolio.finalize_verified_broker_flat.side_effect = (
            lambda _context: order.append("local_bookkeeping") or []
        )

        bot._flatten_all_and_verify("test")

        self.assertEqual(
            order,
            [
                "broker_close",
                "broker_verify",
                "local_bookkeeping",
                "broker_close",
                "broker_verify",
            ],
        )
        bot._portfolio.close_all.assert_not_called()
        bot._circuit.update_unrealized_pnl.assert_called_once_with(0.0)

    def test_bookkeeping_failure_propagates_after_second_broker_proof(self):
        bot = self._loop_bot()
        order = []
        bot._portfolio.get_open_positions.return_value = [object()]
        bot._broker.close_all_positions.side_effect = lambda: (
            order.append("broker_close") or True
        )
        bot._broker.get_positions.side_effect = lambda: (
            order.append("broker_verify") or []
        )
        def fail_bookkeeping(_context):
            order.append("local_bookkeeping")
            raise PortfolioSafetyError("exact close fill missing")

        bot._portfolio.finalize_verified_broker_flat.side_effect = (
            fail_bookkeeping
        )

        with self.assertRaisesRegex(
            PortfolioSafetyError, "exact close fill missing"
        ):
            bot._flatten_all_and_verify("shutdown")

        self.assertEqual(
            order,
            [
                "broker_close",
                "broker_verify",
                "local_bookkeeping",
                "broker_close",
                "broker_verify",
            ],
        )

    def test_post_bookkeeping_raced_liquidation_is_not_silently_unaccounted(self):
        bot = self._loop_bot()
        bot._portfolio.finalize_verified_broker_flat.return_value = []
        bot._broker.get_last_close_fills.return_value = [
            {
                "id": "raced-close",
                "symbol": "JWEL",
                "side": "sell",
                "qty": 10,
                "filled_qty": 10,
                "filled_avg_price": 3.50,
                "status": "filled",
            }
        ]

        with self.assertRaisesRegex(
            PortfolioSafetyError, "unexpected raced position"
        ):
            bot._flatten_all_and_verify("shutdown")

        self.assertEqual(bot._broker.close_all_positions.call_count, 2)
        self.assertEqual(bot._broker.get_positions.call_count, 2)
        bot._portfolio.finalize_verified_broker_flat.assert_called_once_with(
            "shutdown"
        )

    def test_untracked_broker_fill_cannot_bypass_bookkeeping(self):
        bot = self._loop_bot()
        order = []
        bot._portfolio.get_open_positions.return_value = []
        bot._broker.close_all_positions.side_effect = lambda: (
            order.append("broker_close") or True
        )
        bot._broker.get_positions.side_effect = lambda: (
            order.append("broker_verify") or []
        )

        def reject_unmatched_fill(_context):
            order.append("local_bookkeeping")
            raise PortfolioSafetyError("unmatched broker close fills")

        bot._portfolio.finalize_verified_broker_flat.side_effect = (
            reject_unmatched_fill
        )

        with self.assertRaisesRegex(
            PortfolioSafetyError, "unmatched broker close fills"
        ):
            bot._flatten_all_and_verify("hard_time_exit")

        self.assertEqual(
            order,
            [
                "broker_close",
                "broker_verify",
                "local_bookkeeping",
                "broker_close",
                "broker_verify",
            ],
        )

    def test_circuit_halt_does_not_continue_after_unverified_flatten(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-11"
        bot._portfolio.get_open_positions.return_value = [
            SimpleNamespace(pnl_unrealized=-100.0)
        ]
        bot._circuit.check.return_value = CircuitState.HALTED
        bot._circuit.is_trading_allowed = False
        bot._flatten_all_and_verify = Mock(
            side_effect=PortfolioSafetyError("broker positions remain")
        )

        with patch(
            "trading_bot.main.now_et",
            return_value=datetime(2026, 8, 11, 10, 30),
        ):
            with self.assertRaisesRegex(
                PortfolioSafetyError, "broker positions remain"
            ):
                bot._tick()

        bot._flatten_all_and_verify.assert_called_once_with(
            "circuit_breaker_halt"
        )

    def test_verified_circuit_halt_stops_process_before_timed_recovery(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-11"
        bot._portfolio.get_open_positions.return_value = [
            SimpleNamespace(pnl_unrealized=-100.0)
        ]
        bot._circuit.check.return_value = CircuitState.HALTED
        bot._circuit.is_trading_allowed = False
        bot._flatten_all_and_verify = Mock(return_value=[])
        bot._notify_circuit_state_change = Mock()

        with patch(
            "trading_bot.main.now_et",
            return_value=datetime(2026, 8, 11, 10, 30),
        ):
            bot._tick()

        bot._flatten_all_and_verify.assert_called_once_with(
            "circuit_breaker_halt"
        )
        bot._notify_circuit_state_change.assert_called_once_with(
            CircuitState.HALTED
        )
        self.assertFalse(bot._running)
        self.assertTrue(bot._shutdown_event.is_set())

    def test_loss_realized_during_position_update_flattens_in_same_tick(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-11"
        bot._daily_plan_generated = True
        bot._check_daily_reset = Mock()
        bot._market_data = Mock()
        bot._regime_detector = Mock()
        bot._regime_detector.detect.return_value = SimpleNamespace(
            value="range_bound"
        )
        bot._regime_detector.get_regime_adjustments.return_value = {}
        bot._strategy = Mock()
        bot._scanner = Mock()
        bot._portfolio.get_open_positions.side_effect = [
            [SimpleNamespace(pnl_unrealized=100.0)],
            [SimpleNamespace(pnl_unrealized=-400.0)],
        ]
        bot._circuit.check.side_effect = [
            CircuitState.NORMAL,
            CircuitState.HALTED,
        ]

        def realize_partial_loss(*_args, **_kwargs):
            bot._circuit.is_trading_allowed = False
            return []

        bot._portfolio.update_positions.side_effect = realize_partial_loss
        bot._flatten_all_and_verify = Mock(return_value=[])
        bot._notify_circuit_state_change = Mock()

        with (
            patch("trading_bot.main.is_near_close", return_value=False),
            patch("trading_bot.main.is_premarket", return_value=False),
            # The tick crossed the closing bell while positions were updating;
            # HALTED cleanup must still run before the closed-market return.
            patch("trading_bot.main.is_market_open", return_value=False),
        ):
            bot._tick()

        self.assertEqual(
            [call.args[0] for call in bot._circuit.update_unrealized_pnl.call_args_list],
            [100.0, -400.0],
        )
        bot._flatten_all_and_verify.assert_called_once_with(
            "circuit_breaker_halt"
        )
        bot._notify_circuit_state_change.assert_called_once_with(
            CircuitState.HALTED
        )
        bot._scanner.scan.assert_not_called()
        self.assertFalse(bot._running)
        self.assertTrue(bot._shutdown_event.is_set())

    def test_hard_time_exit_does_not_continue_after_unverified_flatten(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-11"
        bot._portfolio.get_open_positions.return_value = [
            SimpleNamespace(pnl_unrealized=0.0)
        ]
        bot._circuit.check.return_value = CircuitState.NORMAL
        bot._circuit.is_trading_allowed = True
        bot._flatten_all_and_verify = Mock(
            side_effect=PortfolioSafetyError("broker did not confirm close")
        )

        with patch(
            "trading_bot.main.now_et",
            return_value=datetime(2026, 8, 11, 15, 55),
        ), patch("trading_bot.main.is_near_close", return_value=True):
            with self.assertRaisesRegex(
                PortfolioSafetyError, "broker did not confirm close"
            ):
                bot._tick()

        bot._flatten_all_and_verify.assert_called_once_with("hard_time_exit")

    def test_hard_time_exit_flattens_broker_when_local_tracking_is_empty(self):
        bot = self._loop_bot()
        bot._running = True
        bot._last_trading_date = "2026-08-11"
        bot._portfolio.get_open_positions.return_value = []
        bot._circuit.check.return_value = CircuitState.NORMAL
        bot._circuit.is_trading_allowed = True
        bot._flatten_all_and_verify = Mock(return_value=[])

        with patch(
            "trading_bot.main.now_et",
            return_value=datetime(2026, 8, 11, 15, 55),
        ), patch("trading_bot.main.is_near_close", return_value=True):
            bot._tick()

        bot._flatten_all_and_verify.assert_called_once_with("hard_time_exit")

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

    def test_advisor_failure_after_alert_does_not_repeat_transition_alert(self):
        bot = TradingBot.__new__(TradingBot)
        bot._last_circuit_alert_state = None
        bot._circuit = Mock()
        bot._circuit.get_status.return_value = {
            "state": "halted",
            "halt_reason": "api_errors: 5 in 5 minutes >= 5",
            "daily_pnl": 0.0,
            "consecutive_losses": 0,
        }
        bot._notify = Mock()
        bot._advisor = Mock()
        bot._advisor.recommend_circuit_breaker_action.side_effect = RuntimeError(
            "advisor unavailable"
        )
        bot._portfolio = Mock()
        bot._portfolio.get_daily_journal_entries.return_value = []

        with self.assertRaisesRegex(RuntimeError, "advisor unavailable"):
            bot._notify_circuit_state_change(CircuitState.HALTED)
        bot._notify_circuit_state_change(CircuitState.HALTED)

        bot._notify.notify_circuit_breaker.assert_called_once()
        bot._advisor.recommend_circuit_breaker_action.assert_called_once()
        self.assertEqual(bot._last_circuit_alert_state, CircuitState.HALTED)


if __name__ == "__main__":
    unittest.main()
