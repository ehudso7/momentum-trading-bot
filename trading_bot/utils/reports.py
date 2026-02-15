"""
Daily summary report generation for the trading journal.

Computes performance statistics from completed trades and formats
them as either a structured dict or human-readable text report.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_module
from typing import Any

import structlog

from trading_bot.models.domain import JournalEntry

log = structlog.get_logger(__name__)


class DailySummaryReport:
    """
    Generates end-of-day performance reports from trade journal entries.

    Args:
        trades: List of JournalEntry records for the day.
        starting_equity: Account equity at start of day.
        ending_equity: Account equity at end of day.
        circuit_breaker_status: Status dict from CircuitBreaker.get_status().
    """

    def __init__(
        self,
        trades: list[JournalEntry],
        starting_equity: float,
        ending_equity: float,
        circuit_breaker_status: dict[str, Any],
    ) -> None:
        self._trades = trades
        self._starting_equity = starting_equity
        self._ending_equity = ending_equity
        self._circuit_breaker_status = circuit_breaker_status

    def generate(self) -> dict[str, Any]:
        """
        Compute all summary statistics and return as a dict.

        Returns:
            Dictionary containing all daily performance metrics.
        """
        total_trades = len(self._trades)

        if total_trades == 0:
            log.info("report.no_trades", message="No trades to summarize")
            return self._empty_report()

        winners = [t for t in self._trades if t.pnl > 0]
        losers = [t for t in self._trades if t.pnl < 0]
        scratch = [t for t in self._trades if t.pnl == 0]

        num_winners = len(winners)
        num_losers = len(losers)
        win_rate = (num_winners / total_trades) * 100

        total_pnl = sum(t.pnl for t in self._trades)
        gross_profit = sum(t.pnl for t in winners) if winners else 0.0
        gross_loss = sum(t.pnl for t in losers) if losers else 0.0

        # Profit factor: gross_profit / abs(gross_loss)
        # If no losers, profit factor is infinite — represent as 0.0 to avoid
        # division by zero; callers can check num_losers == 0 separately.
        if gross_loss != 0.0:
            profit_factor = gross_profit / abs(gross_loss)
        else:
            profit_factor = float("inf") if gross_profit > 0 else 0.0

        avg_winner = gross_profit / num_winners if num_winners > 0 else 0.0
        avg_loser = gross_loss / num_losers if num_losers > 0 else 0.0

        largest_winner = max(t.pnl for t in winners) if winners else 0.0
        largest_loser = min(t.pnl for t in losers) if losers else 0.0

        avg_hold_time = (
            sum(t.hold_time_minutes for t in self._trades) / total_trades
        )

        # Best and worst symbol by total P&L
        symbol_pnl: dict[str, float] = defaultdict(float)
        for t in self._trades:
            symbol_pnl[t.symbol] += t.pnl

        best_symbol = max(symbol_pnl, key=symbol_pnl.get)  # type: ignore[arg-type]
        worst_symbol = min(symbol_pnl, key=symbol_pnl.get)  # type: ignore[arg-type]

        # Average R:R ratio
        avg_rr_ratio = sum(t.rr_ratio for t in self._trades) / total_trades

        # Max consecutive winners / losers
        max_consec_wins, max_consec_losses = self._max_consecutive_streaks()

        # Daily return %
        if self._starting_equity > 0:
            daily_return_pct = (
                (self._ending_equity - self._starting_equity)
                / self._starting_equity
                * 100
            )
        else:
            daily_return_pct = 0.0

        report = {
            "date": date_module.today().isoformat(),
            "total_trades": total_trades,
            "winners": num_winners,
            "losers": num_losers,
            "scratch": len(scratch),
            "win_rate_pct": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else float("inf"),
            "avg_winner": round(avg_winner, 2),
            "avg_loser": round(avg_loser, 2),
            "largest_winner": round(largest_winner, 2),
            "largest_loser": round(largest_loser, 2),
            "avg_hold_time_minutes": round(avg_hold_time, 1),
            "best_symbol": best_symbol,
            "best_symbol_pnl": round(symbol_pnl[best_symbol], 2),
            "worst_symbol": worst_symbol,
            "worst_symbol_pnl": round(symbol_pnl[worst_symbol], 2),
            "avg_rr_ratio": round(avg_rr_ratio, 2),
            "max_consecutive_winners": max_consec_wins,
            "max_consecutive_losers": max_consec_losses,
            "starting_equity": round(self._starting_equity, 2),
            "ending_equity": round(self._ending_equity, 2),
            "daily_return_pct": round(daily_return_pct, 2),
            "circuit_breaker": self._circuit_breaker_status,
        }

        log.info(
            "report.generated",
            total_trades=total_trades,
            win_rate=round(win_rate, 2),
            total_pnl=round(total_pnl, 2),
            daily_return_pct=round(daily_return_pct, 2),
        )

        return report

    def format_text(self) -> str:
        """
        Generate a human-readable text report.

        Returns:
            Formatted multi-line string with all summary stats.
        """
        report = self.generate()

        if report["total_trades"] == 0:
            return self._format_empty_text(report)

        # Format profit factor display
        pf_display = (
            "INF" if report["profit_factor"] == float("inf")
            else f"{report['profit_factor']:.2f}"
        )

        lines = [
            "=" * 60,
            "DAILY TRADING SUMMARY",
            "=" * 60,
            "",
            f"  Starting Equity:    ${report['starting_equity']:>12,.2f}",
            f"  Ending Equity:      ${report['ending_equity']:>12,.2f}",
            f"  Daily Return:       {report['daily_return_pct']:>+12.2f}%",
            "",
            "-" * 60,
            "TRADE STATISTICS",
            "-" * 60,
            "",
            f"  Total Trades:       {report['total_trades']:>12d}",
            f"  Winners:            {report['winners']:>12d}",
            f"  Losers:             {report['losers']:>12d}",
            f"  Scratch:            {report['scratch']:>12d}",
            f"  Win Rate:           {report['win_rate_pct']:>11.2f}%",
            "",
            "-" * 60,
            "P&L BREAKDOWN",
            "-" * 60,
            "",
            f"  Total P&L:          ${report['total_pnl']:>12,.2f}",
            f"  Gross Profit:       ${report['gross_profit']:>12,.2f}",
            f"  Gross Loss:         ${report['gross_loss']:>12,.2f}",
            f"  Profit Factor:      {pf_display:>13s}",
            "",
            f"  Avg Winner:         ${report['avg_winner']:>12,.2f}",
            f"  Avg Loser:          ${report['avg_loser']:>12,.2f}",
            f"  Largest Winner:     ${report['largest_winner']:>12,.2f}",
            f"  Largest Loser:      ${report['largest_loser']:>12,.2f}",
            "",
            "-" * 60,
            "PERFORMANCE METRICS",
            "-" * 60,
            "",
            f"  Avg R:R Ratio:      {report['avg_rr_ratio']:>13.2f}",
            f"  Avg Hold Time:      {report['avg_hold_time_minutes']:>10.1f} min",
            f"  Max Consec Wins:    {report['max_consecutive_winners']:>12d}",
            f"  Max Consec Losses:  {report['max_consecutive_losers']:>12d}",
            "",
            f"  Best Symbol:        {report['best_symbol']:>8s}  (${report['best_symbol_pnl']:>+,.2f})",
            f"  Worst Symbol:       {report['worst_symbol']:>8s}  (${report['worst_symbol_pnl']:>+,.2f})",
            "",
            "-" * 60,
            "CIRCUIT BREAKER STATUS",
            "-" * 60,
            "",
            f"  State:              {report['circuit_breaker'].get('state', 'unknown'):>13s}",
            f"  Drawdown:           {report['circuit_breaker'].get('drawdown_pct', 0.0):>12.2f}%",
            f"  Consec Losses:      {report['circuit_breaker'].get('consecutive_losses', 0):>12d}",
            f"  API Errors (5m):    {report['circuit_breaker'].get('api_errors_5min', 0):>12d}",
            "",
            "=" * 60,
        ]

        return "\n".join(lines)

    def _max_consecutive_streaks(self) -> tuple[int, int]:
        """
        Calculate max consecutive winners and losers for the day.

        Returns:
            Tuple of (max_consecutive_winners, max_consecutive_losers).
        """
        if not self._trades:
            return 0, 0

        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0

        for trade in self._trades:
            if trade.pnl > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            elif trade.pnl < 0:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
            else:
                # Scratch trade (pnl == 0) resets both streaks
                current_wins = 0
                current_losses = 0

        return max_wins, max_losses

    def _empty_report(self) -> dict[str, Any]:
        """Return a report dict when there are no trades."""
        if self._starting_equity > 0:
            daily_return_pct = (
                (self._ending_equity - self._starting_equity)
                / self._starting_equity
                * 100
            )
        else:
            daily_return_pct = 0.0

        return {
            "date": date_module.today().isoformat(),
            "total_trades": 0,
            "winners": 0,
            "losers": 0,
            "scratch": 0,
            "win_rate_pct": 0.0,
            "total_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "largest_winner": 0.0,
            "largest_loser": 0.0,
            "avg_hold_time_minutes": 0.0,
            "best_symbol": "N/A",
            "best_symbol_pnl": 0.0,
            "worst_symbol": "N/A",
            "worst_symbol_pnl": 0.0,
            "avg_rr_ratio": 0.0,
            "max_consecutive_winners": 0,
            "max_consecutive_losers": 0,
            "starting_equity": round(self._starting_equity, 2),
            "ending_equity": round(self._ending_equity, 2),
            "daily_return_pct": round(daily_return_pct, 2),
            "circuit_breaker": self._circuit_breaker_status,
        }

    def _format_empty_text(self, report: dict[str, Any]) -> str:
        """Format a text report when there are no trades."""
        lines = [
            "=" * 60,
            "DAILY TRADING SUMMARY",
            "=" * 60,
            "",
            f"  Starting Equity:    ${report['starting_equity']:>12,.2f}",
            f"  Ending Equity:      ${report['ending_equity']:>12,.2f}",
            f"  Daily Return:       {report['daily_return_pct']:>+12.2f}%",
            "",
            "  No trades executed today.",
            "",
            "-" * 60,
            "CIRCUIT BREAKER STATUS",
            "-" * 60,
            "",
            f"  State:              {report['circuit_breaker'].get('state', 'unknown'):>13s}",
            f"  Drawdown:           {report['circuit_breaker'].get('drawdown_pct', 0.0):>12.2f}%",
            f"  Consec Losses:      {report['circuit_breaker'].get('consecutive_losses', 0):>12d}",
            f"  API Errors (5m):    {report['circuit_breaker'].get('api_errors_5min', 0):>12d}",
            "",
            "=" * 60,
        ]

        return "\n".join(lines)
