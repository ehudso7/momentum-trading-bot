"""
Main entry point and orchestrator for the momentum trading bot.

Ties together all modules: scanner, strategy, risk, execution, portfolio.
Runs a synchronous polling loop with configurable interval.

Usage:
    trading-bot --mode paper
    trading-bot --mode backtest --config config/config.yaml
    trading-bot --mode live  # requires explicit config confirmation
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

import structlog

from trading_bot.config.settings import AppConfig, RunMode
from trading_bot.data.market_data import BacktestMarketData, LiveMarketData
from trading_bot.data.news_client import NewsClient
from trading_bot.data.polygon_client import PolygonClient
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.portfolio.manager import PortfolioManager
from trading_bot.risk.circuit_breaker import CircuitBreaker
from trading_bot.risk.position_sizer import PositionSizer
from trading_bot.scanners.momentum_gappers import MomentumGapperScanner
from trading_bot.strategies.pullback_vwap import PullbackVWAPStrategy
from trading_bot.utils.helpers import (
    format_currency,
    is_market_open,
    is_near_close,
    is_premarket,
)
from trading_bot.utils.logger import setup_logging

log = structlog.get_logger(__name__)

DISCLAIMER = """
================================================================================
  WARNING: AUTOMATED TRADING SOFTWARE - USE AT YOUR OWN RISK

  This software is for EDUCATIONAL and PAPER TRADING purposes.
  Automated trading involves SUBSTANTIAL RISK of financial loss.
  Past performance and backtests do NOT guarantee future results.
  Most day traders lose money. You can lose your entire account.

  - Paper mode is enabled by DEFAULT
  - NEVER trade money you cannot afford to lose
  - Start with paper trading and validate extensively
  - Understand all risk parameters before going live

  By running this software, you accept full responsibility for any
  financial losses incurred.
================================================================================
"""


class TradingBot:
    """Main orchestrator for the trading bot."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._running = False

        # Wire up dependencies based on run mode
        if config.run_mode == RunMode.BACKTEST:
            self._market_data = BacktestMarketData(
                config.backtest_start_date or "2024-01-01",
                config.backtest_end_date or "2025-01-01",
            )
            self._broker = PaperBroker(initial_equity=config.starting_capital)
            polygon = None
        else:
            polygon = PolygonClient(config.data)
            self._market_data = LiveMarketData(
                polygon, float_cache_hours=config.data.float_cache_hours
            )
            if config.run_mode == RunMode.LIVE:
                self._broker = AlpacaBroker(config.broker)
            else:  # PAPER
                # Use Alpaca paper if keys provided, else local paper broker
                if config.broker.alpaca_api_key.get_secret_value() and \
                   config.broker.alpaca_api_key.get_secret_value() != "your_alpaca_api_key_here":
                    self._broker = AlpacaBroker(config.broker)
                else:
                    self._broker = PaperBroker(initial_equity=config.starting_capital)

        self._news = NewsClient(polygon, config.scanner)
        self._scanner = MomentumGapperScanner(
            self._market_data, self._news, polygon or PolygonClient(config.data), config.scanner
        )
        self._strategy = PullbackVWAPStrategy(config)
        self._sizer = PositionSizer(config.risk)
        self._circuit = CircuitBreaker(config.risk)
        self._portfolio = PortfolioManager(
            self._broker, config, circuit_breaker=self._circuit
        )

    def run(self) -> None:
        """Main run loop. Dispatches to backtest or live/paper loop."""
        print(DISCLAIMER)
        log.info(
            "bot.starting",
            mode=self._config.run_mode.value,
            capital=format_currency(self._config.starting_capital),
        )

        if self._config.run_mode == RunMode.BACKTEST:
            self._run_backtest()
            return

        self._run_live_loop()

    def _run_backtest(self) -> None:
        """Run backtesting engine."""
        from trading_bot.backtest.engine import BacktestEngine

        engine = BacktestEngine(self._config)
        symbols = self._config.backtest_symbols or ["TSLA", "NVDA", "AMD"]
        results = engine.run(
            symbols=symbols,
            start_date=self._config.backtest_start_date or "2024-01-01",
            end_date=self._config.backtest_end_date or "2025-01-01",
        )
        print(engine.report(results))

    def _run_live_loop(self) -> None:
        """Synchronous polling loop for paper/live trading."""
        # Initialize daily state
        try:
            equity = self._broker.get_account_equity()
        except Exception:
            equity = self._config.starting_capital
            log.warning("bot.equity_fallback", equity=equity)

        self._circuit.reset_daily(equity)
        self._sizer.reset_daily()
        self._portfolio.reset_daily()

        log.info("bot.account_loaded", equity=format_currency(equity))

        self._running = True
        tick_count = 0

        while self._running:
            try:
                self._tick()
                tick_count += 1

                if tick_count % 10 == 0:
                    self._log_status()

                time.sleep(self._config.scanner.scan_interval_seconds)

            except KeyboardInterrupt:
                log.info("bot.keyboard_interrupt")
                break
            except Exception as e:
                log.error("bot.tick_error", error=str(e), exc_info=True)
                self._circuit.record_api_error()
                if not self._circuit.is_trading_allowed:
                    log.critical("bot.circuit_breaker_halted")
                    break
                time.sleep(30)  # Back off on errors

        # Shutdown: close all positions
        entries = self._portfolio.close_all("shutdown")
        log.info(
            "bot.shutdown",
            trades_closed=len(entries),
            daily_pnl=format_currency(self._portfolio.get_daily_pnl()),
        )

    def _tick(self) -> None:
        """Single iteration of the main trading loop."""
        # 1. Check circuit breaker FIRST (NON-NEGOTIABLE)
        state = self._circuit.check()
        if not self._circuit.is_trading_allowed:
            log.warning("bot.circuit_active", state=state.value)
            return

        # 2. Check hard time exit SECOND
        if is_near_close(minutes_before=10):
            if self._portfolio.get_open_positions():
                entries = self._portfolio.close_all("hard_time_exit")
                log.info("bot.time_exit", trades_closed=len(entries))
            return

        # 3. Update existing positions (exits, scale-outs, trailing stops)
        entries = self._portfolio.update_positions(self._strategy, self._market_data)
        if entries:
            for e in entries:
                log.info(
                    "bot.trade_closed",
                    symbol=e.symbol,
                    pnl=format_currency(e.pnl),
                    reason=e.exit_reason,
                )

        # 4. Only look for new entries during active hours
        if not (is_premarket() or is_market_open()):
            return

        # Re-check circuit breaker after position updates
        if not self._circuit.is_trading_allowed:
            return

        # 5. Scan for candidates
        candidates = self._scanner.scan()
        if not candidates:
            return

        log.info("bot.scan_complete", candidates=len(candidates))

        # 6. Evaluate each candidate
        open_symbols = {p.symbol for p in self._portfolio.get_open_positions()}

        for candidate in candidates:
            # Skip symbols already held
            if candidate.symbol in open_symbols:
                continue

            # Fetch intraday bars
            bars = self._market_data.get_intraday_bars(candidate.symbol)
            if bars.empty:
                continue

            # Evaluate strategy
            signal = self._strategy.evaluate(candidate, bars)
            if signal is None:
                continue

            # Risk check
            try:
                equity = self._broker.get_account_equity()
                buying_power = self._broker.get_buying_power()
            except Exception:
                equity = self._config.starting_capital
                buying_power = equity * 4

            risk_result = self._sizer.calculate(
                equity=equity,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                current_positions=self._portfolio.get_open_positions(),
                buying_power=buying_power,
            )

            if not risk_result.approved:
                log.info(
                    "bot.risk_rejected",
                    symbol=candidate.symbol,
                    reason=risk_result.reason,
                )
                continue

            # Log warnings
            for warning in risk_result.warnings:
                log.warning("bot.risk_warning", warning=warning)

            # Execute trade
            position = self._portfolio.open_position(signal, risk_result)
            self._sizer.record_trade_risk(risk_result.risk_dollars)
            open_symbols.add(position.symbol)

            log.info(
                "bot.trade_opened",
                symbol=position.symbol,
                shares=position.shares,
                entry=position.entry_price,
                stop=position.stop_price,
                risk=format_currency(risk_result.risk_dollars),
            )

    def _log_status(self) -> None:
        """Log periodic status update."""
        positions = self._portfolio.get_open_positions()
        circuit_status = self._circuit.get_status()

        log.info(
            "bot.status",
            open_positions=len(positions),
            daily_pnl=format_currency(self._portfolio.get_daily_pnl()),
            circuit=circuit_status["state"],
            drawdown_pct=circuit_status["drawdown_pct"],
        )

    def stop(self) -> None:
        """Signal the bot to stop gracefully."""
        self._running = False


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Momentum Day-Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=DISCLAIMER,
    )
    parser.add_argument(
        "--mode",
        choices=["backtest", "paper", "live"],
        default="paper",
        help="Run mode (default: paper)",
    )
    parser.add_argument(
        "--config",
        default="trading_bot/config/config.yaml",
        help="Path to config YAML file",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override log level (DEBUG, INFO, WARNING, ERROR)",
    )
    args = parser.parse_args()

    # Load config
    config = AppConfig.from_yaml(args.config)
    config.run_mode = RunMode(args.mode)

    if args.log_level:
        config.log_level = args.log_level

    # Setup logging
    setup_logging(config.log_level, json_output=config.log_json)

    # Live mode safety confirmation
    if config.run_mode == RunMode.LIVE:
        print("\n*** LIVE TRADING MODE ***")
        print("You are about to trade with REAL MONEY.")
        confirm = input("Type 'I ACCEPT THE RISK' to continue: ")
        if confirm != "I ACCEPT THE RISK":
            print("Aborting. Use --mode paper for paper trading.")
            sys.exit(1)

    # Create and run bot
    bot = TradingBot(config)

    # Graceful shutdown handlers
    def handle_signal(signum, frame):
        log.info("bot.signal_received", signal=signum)
        bot.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bot.run()


if __name__ == "__main__":
    main()
