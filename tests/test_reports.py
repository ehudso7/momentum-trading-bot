"""
Tests for DailySummaryReport.

Covers generate() with no trades, winning-only, losing-only, and mixed
trade sets; format_text() producing a multi-line string; and the
_max_consecutive_streaks() helper.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from trading_bot.models.domain import JournalEntry
from trading_bot.utils.reports import DailySummaryReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    symbol: str = "TEST",
    pnl: float = 0.0,
    rr_ratio: float = 1.0,
    hold_time_minutes: float = 15.0,
    entry_price: float = 10.0,
    exit_price: float = 10.5,
    shares: int = 100,
    exit_reason: str = "target",
) -> JournalEntry:
    """Create a JournalEntry with sensible defaults; override as needed."""
    return JournalEntry(
        date="2024-06-15",
        symbol=symbol,
        side="buy",
        signal_type="vwap_pullback",
        entry_price=entry_price,
        exit_price=exit_price,
        shares=shares,
        pnl=pnl,
        rr_ratio=rr_ratio,
        hold_time_minutes=hold_time_minutes,
        entry_time="09:35:00",
        exit_time="09:50:00",
        exit_reason=exit_reason,
    )


_DEFAULT_CB_STATUS = {
    "state": "normal",
    "daily_pnl": 0.0,
    "consecutive_losses": 0,
    "api_errors_5min": 0,
    "drawdown_pct": 0.0,
}


# ---------------------------------------------------------------------------
# generate() with no trades
# ---------------------------------------------------------------------------

class TestGenerateNoTrades:
    """When there are zero trades for the day."""

    def test_total_trades_zero(self):
        report = DailySummaryReport(
            trades=[],
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["total_trades"] == 0
        assert report["winners"] == 0
        assert report["losers"] == 0
        assert report["scratch"] == 0

    def test_pnl_fields_zero(self):
        report = DailySummaryReport(
            trades=[],
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["total_pnl"] == 0.0
        assert report["gross_profit"] == 0.0
        assert report["gross_loss"] == 0.0
        assert report["profit_factor"] == 0.0
        assert report["avg_winner"] == 0.0
        assert report["avg_loser"] == 0.0

    def test_streaks_zero(self):
        report = DailySummaryReport(
            trades=[],
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["max_consecutive_winners"] == 0
        assert report["max_consecutive_losers"] == 0

    def test_equity_and_return(self):
        report = DailySummaryReport(
            trades=[],
            starting_equity=20_000.0,
            ending_equity=20_500.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["starting_equity"] == 20_000.0
        assert report["ending_equity"] == 20_500.0
        assert report["daily_return_pct"] == 2.5

    def test_best_worst_symbol_na(self):
        report = DailySummaryReport(
            trades=[],
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["best_symbol"] == "N/A"
        assert report["worst_symbol"] == "N/A"

    def test_circuit_breaker_status_passthrough(self):
        cb = {"state": "halted", "daily_pnl": -500.0, "consecutive_losses": 5,
              "api_errors_5min": 0, "drawdown_pct": 2.0}
        report = DailySummaryReport(
            trades=[],
            starting_equity=25_000.0,
            ending_equity=24_500.0,
            circuit_breaker_status=cb,
        ).generate()

        assert report["circuit_breaker"]["state"] == "halted"


# ---------------------------------------------------------------------------
# generate() with winning trades only
# ---------------------------------------------------------------------------

class TestGenerateWinnersOnly:
    """All trades are profitable."""

    @pytest.fixture
    def winners_report(self) -> dict:
        trades = [
            _make_entry(symbol="AAPL", pnl=100.0, rr_ratio=2.0, hold_time_minutes=10.0),
            _make_entry(symbol="AAPL", pnl=200.0, rr_ratio=3.0, hold_time_minutes=20.0),
            _make_entry(symbol="TSLA", pnl=150.0, rr_ratio=2.5, hold_time_minutes=15.0),
        ]
        return DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_450.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

    def test_win_rate_100(self, winners_report: dict):
        assert winners_report["win_rate_pct"] == 100.0

    def test_no_losers(self, winners_report: dict):
        assert winners_report["losers"] == 0
        assert winners_report["gross_loss"] == 0.0
        assert winners_report["avg_loser"] == 0.0

    def test_profit_factor_infinite(self, winners_report: dict):
        assert winners_report["profit_factor"] == float("inf")

    def test_total_pnl(self, winners_report: dict):
        assert winners_report["total_pnl"] == 450.0

    def test_gross_profit(self, winners_report: dict):
        assert winners_report["gross_profit"] == 450.0

    def test_avg_winner(self, winners_report: dict):
        assert winners_report["avg_winner"] == 150.0  # 450 / 3

    def test_largest_winner(self, winners_report: dict):
        assert winners_report["largest_winner"] == 200.0

    def test_best_symbol(self, winners_report: dict):
        # AAPL has 300 total, TSLA has 150
        assert winners_report["best_symbol"] == "AAPL"
        assert winners_report["best_symbol_pnl"] == 300.0

    def test_worst_symbol(self, winners_report: dict):
        assert winners_report["worst_symbol"] == "TSLA"
        assert winners_report["worst_symbol_pnl"] == 150.0

    def test_avg_rr_ratio(self, winners_report: dict):
        expected = round((2.0 + 3.0 + 2.5) / 3, 2)
        assert winners_report["avg_rr_ratio"] == expected

    def test_avg_hold_time(self, winners_report: dict):
        expected = round((10.0 + 20.0 + 15.0) / 3, 1)
        assert winners_report["avg_hold_time_minutes"] == expected

    def test_max_consecutive_winners(self, winners_report: dict):
        assert winners_report["max_consecutive_winners"] == 3

    def test_max_consecutive_losers_zero(self, winners_report: dict):
        assert winners_report["max_consecutive_losers"] == 0


# ---------------------------------------------------------------------------
# generate() with losing trades only
# ---------------------------------------------------------------------------

class TestGenerateLosersOnly:
    """All trades are losses."""

    @pytest.fixture
    def losers_report(self) -> dict:
        trades = [
            _make_entry(symbol="GME", pnl=-50.0, rr_ratio=-0.5, hold_time_minutes=5.0),
            _make_entry(symbol="AMC", pnl=-80.0, rr_ratio=-0.8, hold_time_minutes=8.0),
            _make_entry(symbol="GME", pnl=-30.0, rr_ratio=-0.3, hold_time_minutes=4.0),
        ]
        return DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=24_840.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

    def test_win_rate_zero(self, losers_report: dict):
        assert losers_report["win_rate_pct"] == 0.0

    def test_no_winners(self, losers_report: dict):
        assert losers_report["winners"] == 0
        assert losers_report["gross_profit"] == 0.0
        assert losers_report["avg_winner"] == 0.0
        assert losers_report["largest_winner"] == 0.0

    def test_profit_factor_zero(self, losers_report: dict):
        # No gross profit -> profit_factor = 0.0
        assert losers_report["profit_factor"] == 0.0

    def test_total_pnl(self, losers_report: dict):
        assert losers_report["total_pnl"] == -160.0

    def test_gross_loss(self, losers_report: dict):
        assert losers_report["gross_loss"] == -160.0

    def test_avg_loser(self, losers_report: dict):
        expected = round(-160.0 / 3, 2)
        assert losers_report["avg_loser"] == expected

    def test_largest_loser(self, losers_report: dict):
        assert losers_report["largest_loser"] == -80.0

    def test_worst_symbol(self, losers_report: dict):
        # GME: -80 total, AMC: -80 total; min picks first
        assert losers_report["worst_symbol"] in ("GME", "AMC")

    def test_max_consecutive_losers(self, losers_report: dict):
        assert losers_report["max_consecutive_losers"] == 3

    def test_max_consecutive_winners_zero(self, losers_report: dict):
        assert losers_report["max_consecutive_winners"] == 0


# ---------------------------------------------------------------------------
# generate() with mixed trades
# ---------------------------------------------------------------------------

class TestGenerateMixed:
    """Mixed winning and losing trades."""

    @pytest.fixture
    def mixed_report(self) -> dict:
        trades = [
            _make_entry(symbol="AAPL", pnl=200.0, rr_ratio=2.0, hold_time_minutes=10.0),
            _make_entry(symbol="TSLA", pnl=-50.0, rr_ratio=-0.5, hold_time_minutes=5.0),
            _make_entry(symbol="AAPL", pnl=100.0, rr_ratio=1.0, hold_time_minutes=15.0),
            _make_entry(symbol="NVDA", pnl=-30.0, rr_ratio=-0.3, hold_time_minutes=3.0),
            _make_entry(symbol="NVDA", pnl=50.0, rr_ratio=0.5, hold_time_minutes=12.0),
        ]
        return DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_270.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

    def test_total_trades(self, mixed_report: dict):
        assert mixed_report["total_trades"] == 5

    def test_winners_and_losers_count(self, mixed_report: dict):
        assert mixed_report["winners"] == 3
        assert mixed_report["losers"] == 2

    def test_win_rate(self, mixed_report: dict):
        assert mixed_report["win_rate_pct"] == 60.0

    def test_total_pnl(self, mixed_report: dict):
        # 200 - 50 + 100 - 30 + 50 = 270
        assert mixed_report["total_pnl"] == 270.0

    def test_gross_profit_and_loss(self, mixed_report: dict):
        assert mixed_report["gross_profit"] == 350.0  # 200+100+50
        assert mixed_report["gross_loss"] == -80.0  # -50 + -30

    def test_profit_factor(self, mixed_report: dict):
        expected = round(350.0 / 80.0, 2)
        assert mixed_report["profit_factor"] == expected

    def test_avg_winner(self, mixed_report: dict):
        expected = round(350.0 / 3, 2)
        assert mixed_report["avg_winner"] == expected

    def test_avg_loser(self, mixed_report: dict):
        expected = round(-80.0 / 2, 2)
        assert mixed_report["avg_loser"] == expected

    def test_largest_winner_and_loser(self, mixed_report: dict):
        assert mixed_report["largest_winner"] == 200.0
        assert mixed_report["largest_loser"] == -50.0

    def test_best_and_worst_symbol(self, mixed_report: dict):
        # AAPL: 200+100=300, TSLA: -50, NVDA: -30+50=20
        assert mixed_report["best_symbol"] == "AAPL"
        assert mixed_report["best_symbol_pnl"] == 300.0
        assert mixed_report["worst_symbol"] == "TSLA"
        assert mixed_report["worst_symbol_pnl"] == -50.0

    def test_avg_rr_ratio(self, mixed_report: dict):
        total_rr = 2.0 + (-0.5) + 1.0 + (-0.3) + 0.5
        expected = round(total_rr / 5, 2)
        assert mixed_report["avg_rr_ratio"] == expected

    def test_daily_return_pct(self, mixed_report: dict):
        expected = round((25_270.0 - 25_000.0) / 25_000.0 * 100, 2)
        assert mixed_report["daily_return_pct"] == expected

    def test_scratch_trades(self):
        """Trades with pnl == 0 count as scratch."""
        trades = [
            _make_entry(pnl=100.0),
            _make_entry(pnl=0.0),
            _make_entry(pnl=-50.0),
        ]
        report = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_050.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["scratch"] == 1
        assert report["winners"] == 1
        assert report["losers"] == 1


# ---------------------------------------------------------------------------
# format_text() tests
# ---------------------------------------------------------------------------

class TestFormatText:
    """format_text() produces a multi-line string with expected content."""

    def test_no_trades_text(self):
        text = DailySummaryReport(
            trades=[],
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        assert isinstance(text, str)
        lines = text.split("\n")
        assert len(lines) > 5
        assert "DAILY TRADING SUMMARY" in text
        assert "No trades executed today." in text
        assert "CIRCUIT BREAKER STATUS" in text

    def test_with_trades_text(self):
        trades = [
            _make_entry(symbol="AAPL", pnl=100.0, rr_ratio=2.0, hold_time_minutes=10.0),
            _make_entry(symbol="TSLA", pnl=-50.0, rr_ratio=-0.5, hold_time_minutes=5.0),
        ]
        text = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_050.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        assert "DAILY TRADING SUMMARY" in text
        assert "TRADE STATISTICS" in text
        assert "P&L BREAKDOWN" in text
        assert "PERFORMANCE METRICS" in text
        assert "CIRCUIT BREAKER STATUS" in text

    def test_text_contains_equity_values(self):
        text = DailySummaryReport(
            trades=[],
            starting_equity=10_000.0,
            ending_equity=10_500.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        assert "10,000.00" in text
        assert "10,500.00" in text

    def test_text_contains_win_rate(self):
        trades = [
            _make_entry(pnl=100.0),
            _make_entry(pnl=-50.0),
            _make_entry(pnl=75.0),
        ]
        text = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_125.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        # Win rate should be 66.67%
        assert "66.67%" in text

    def test_profit_factor_inf_display(self):
        """When profit factor is inf, text should show 'INF'."""
        trades = [
            _make_entry(pnl=100.0),
            _make_entry(pnl=200.0),
        ]
        text = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_300.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        assert "INF" in text

    def test_text_is_multi_line(self):
        trades = [_make_entry(pnl=100.0)]
        text = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_100.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        lines = text.split("\n")
        assert len(lines) >= 30  # The template has many lines

    def test_text_contains_symbol_info(self):
        trades = [
            _make_entry(symbol="AAPL", pnl=200.0),
            _make_entry(symbol="TSLA", pnl=-50.0),
        ]
        text = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_150.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).format_text()

        assert "AAPL" in text
        assert "TSLA" in text


# ---------------------------------------------------------------------------
# _max_consecutive_streaks() tests
# ---------------------------------------------------------------------------

class TestMaxConsecutiveStreaks:
    """Direct tests for the _max_consecutive_streaks() method."""

    def _streaks(self, pnls: list[float]) -> tuple[int, int]:
        """Helper: build trades from a list of P&L values and get streaks."""
        trades = [_make_entry(pnl=p) for p in pnls]
        report = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        )
        return report._max_consecutive_streaks()

    def test_empty_trades(self):
        assert self._streaks([]) == (0, 0)

    def test_single_winner(self):
        assert self._streaks([100.0]) == (1, 0)

    def test_single_loser(self):
        assert self._streaks([-50.0]) == (0, 1)

    def test_single_scratch(self):
        assert self._streaks([0.0]) == (0, 0)

    def test_all_winners(self):
        assert self._streaks([10, 20, 30, 40]) == (4, 0)

    def test_all_losers(self):
        assert self._streaks([-10, -20, -30]) == (0, 3)

    def test_alternating_win_loss(self):
        assert self._streaks([10, -5, 10, -5, 10]) == (1, 1)

    def test_win_streak_then_loss_streak(self):
        assert self._streaks([10, 20, 30, -10, -20, -30]) == (3, 3)

    def test_loss_streak_then_win_streak(self):
        assert self._streaks([-10, -20, -30, 10, 20, 30, 40]) == (4, 3)

    def test_scratch_resets_both_streaks(self):
        # 2 wins, then scratch, then 3 wins
        pnls = [10, 20, 0.0, 10, 20, 30]
        wins, losses = self._streaks(pnls)
        assert wins == 3  # The 3 wins after the scratch
        assert losses == 0

    def test_scratch_resets_loss_streak(self):
        pnls = [-10, -20, 0.0, -10]
        wins, losses = self._streaks(pnls)
        assert wins == 0
        assert losses == 2  # Before the scratch

    def test_complex_pattern(self):
        # W W L L L W W W W L
        pnls = [10, 20, -5, -10, -15, 10, 20, 30, 40, -5]
        wins, losses = self._streaks(pnls)
        assert wins == 4   # The 4-win streak
        assert losses == 3  # The 3-loss streak


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for report generation."""

    def test_zero_starting_equity(self):
        """Zero starting equity should not cause division by zero."""
        report = DailySummaryReport(
            trades=[],
            starting_equity=0.0,
            ending_equity=0.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["daily_return_pct"] == 0.0

    def test_single_trade(self):
        """Report works with exactly one trade."""
        trades = [_make_entry(symbol="SOLO", pnl=50.0, rr_ratio=1.0,
                              hold_time_minutes=7.0)]
        report = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_050.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["total_trades"] == 1
        assert report["winners"] == 1
        assert report["win_rate_pct"] == 100.0
        assert report["best_symbol"] == "SOLO"
        assert report["worst_symbol"] == "SOLO"

    def test_all_scratch_trades(self):
        """All trades with pnl=0 yields 0 winners and 0 losers."""
        trades = [_make_entry(pnl=0.0) for _ in range(3)]
        report = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_000.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["total_trades"] == 3
        assert report["winners"] == 0
        assert report["losers"] == 0
        assert report["scratch"] == 3
        assert report["win_rate_pct"] == 0.0
        assert report["profit_factor"] == 0.0

    def test_negative_daily_return(self):
        """Negative daily return is correctly calculated."""
        report = DailySummaryReport(
            trades=[_make_entry(pnl=-100.0)],
            starting_equity=10_000.0,
            ending_equity=9_900.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["daily_return_pct"] == -1.0

    def test_many_symbols(self):
        """Report handles many different symbols."""
        symbols = ["AAPL", "TSLA", "NVDA", "AMZN", "GOOG"]
        trades = [
            _make_entry(symbol=s, pnl=(i + 1) * 10.0)
            for i, s in enumerate(symbols)
        ]
        report = DailySummaryReport(
            trades=trades,
            starting_equity=25_000.0,
            ending_equity=25_150.0,
            circuit_breaker_status=_DEFAULT_CB_STATUS,
        ).generate()

        assert report["best_symbol"] == "GOOG"  # 50
        assert report["worst_symbol"] == "AAPL"  # 10
