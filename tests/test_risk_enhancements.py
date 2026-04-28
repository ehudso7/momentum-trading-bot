"""Tests for risk management enhancements."""
from __future__ import annotations

from trading_bot.risk.symbol_blacklist import SymbolBlacklist
from trading_bot.risk.recovery import RecoveryMode


class TestSymbolBlacklist:
    def test_not_blacklisted_initially(self):
        bl = SymbolBlacklist()
        assert bl.is_blacklisted("AAPL") is False

    def test_blacklisted_after_consecutive_losses(self):
        bl = SymbolBlacklist(max_consecutive_losses=3)
        bl.record_trade("AAPL", -100)
        bl.record_trade("AAPL", -50)
        bl.record_trade("AAPL", -75)
        assert bl.is_blacklisted("AAPL") is True

    def test_win_resets_streak(self):
        bl = SymbolBlacklist(max_consecutive_losses=3)
        bl.record_trade("AAPL", -100)
        bl.record_trade("AAPL", -50)
        bl.record_trade("AAPL", 200)  # Win resets
        bl.record_trade("AAPL", -75)
        assert bl.is_blacklisted("AAPL") is False

    def test_blacklist_expires(self):
        bl = SymbolBlacklist(max_consecutive_losses=2, blacklist_duration_hours=0)
        bl.record_trade("TSLA", -100)
        bl.record_trade("TSLA", -50)
        # Duration is 0 hours, so it should expire immediately
        assert bl.is_blacklisted("TSLA") is False

    def test_different_symbols_independent(self):
        bl = SymbolBlacklist(max_consecutive_losses=2)
        bl.record_trade("AAPL", -100)
        bl.record_trade("AAPL", -50)
        bl.record_trade("TSLA", 200)
        assert bl.is_blacklisted("AAPL") is True
        assert bl.is_blacklisted("TSLA") is False

    def test_get_blacklisted_symbols(self):
        bl = SymbolBlacklist(max_consecutive_losses=2)
        bl.record_trade("AAPL", -100)
        bl.record_trade("AAPL", -50)
        assert "AAPL" in bl.get_blacklisted_symbols()

    def test_reset_clears_all(self):
        bl = SymbolBlacklist(max_consecutive_losses=2)
        bl.record_trade("AAPL", -100)
        bl.record_trade("AAPL", -50)
        bl.reset()
        assert bl.is_blacklisted("AAPL") is False
        assert bl.get_blacklisted_symbols() == []


class TestRecoveryMode:
    def test_not_active_initially(self):
        rm = RecoveryMode()
        assert rm.is_active is False
        assert rm.size_multiplier == 1.0

    def test_active_after_enter(self):
        rm = RecoveryMode(size_reduction=0.5)
        rm.enter_recovery()
        assert rm.is_active is True
        assert rm.size_multiplier == 0.5

    def test_exits_after_consecutive_wins(self):
        rm = RecoveryMode(consecutive_wins_to_exit=2)
        rm.enter_recovery()
        rm.record_trade(100)
        rm.record_trade(50)
        assert rm.is_active is False
        assert rm.size_multiplier == 1.0

    def test_loss_resets_win_streak(self):
        rm = RecoveryMode(consecutive_wins_to_exit=2, recovery_trades=10)
        rm.enter_recovery()
        rm.record_trade(100)
        rm.record_trade(-50)  # Resets streak
        rm.record_trade(100)
        assert rm.is_active is True  # Still active, streak reset

    def test_exits_after_max_trades(self):
        rm = RecoveryMode(recovery_trades=3, consecutive_wins_to_exit=2)
        rm.enter_recovery()
        rm.record_trade(-50)
        rm.record_trade(100)
        rm.record_trade(-50)
        rm.record_trade(100)
        rm.record_trade(-50)
        assert rm.is_active is False

    def test_no_op_when_not_active(self):
        rm = RecoveryMode()
        rm.record_trade(100)  # Should not error
        assert rm.is_active is False

    def test_reset(self):
        rm = RecoveryMode()
        rm.enter_recovery()
        rm.reset()
        assert rm.is_active is False
