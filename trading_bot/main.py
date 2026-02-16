"""
Main entry point and orchestrator for the momentum trading bot.

Ties together all modules: scanner, strategy, risk, execution, portfolio,
regime detection, health monitoring, notifications, correlation checking,
AI advisor, and daily reporting.

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
import threading
import time

import structlog

from trading_bot.config.settings import AppConfig, RunMode
from trading_bot.dashboard.state import DashboardState
from trading_bot.data.market_data import BacktestMarketData, LiveMarketData
from trading_bot.data.news_client import NewsClient
from trading_bot.data.alpaca_screener import AlpacaScreener
from trading_bot.data.polygon_client import PolygonClient
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.portfolio.manager import PortfolioManager
from trading_bot.risk.circuit_breaker import CircuitBreaker
from trading_bot.risk.correlation import CorrelationChecker
from trading_bot.risk.position_sizer import PositionSizer
from trading_bot.scanners.momentum_gappers import MomentumGapperScanner
from trading_bot.strategies.advisor import TradingAdvisor
from trading_bot.strategies.pullback_vwap import PullbackVWAPStrategy
from trading_bot.strategies.regime import RegimeDetector
from trading_bot.utils.health import HealthMonitor
from trading_bot.utils.helpers import (
    format_currency,
    is_market_holiday,
    is_market_open,
    is_near_close,
    is_premarket,
    now_et,
    time_until_market_open,
)
from trading_bot.utils.logger import setup_logging
from trading_bot.utils.notifications import NotificationManager
from trading_bot.utils.reports import DailySummaryReport

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

    def __init__(self, config: AppConfig, dashboard_state: DashboardState | None = None):
        self._config = config
        self._running = False
        self._shutdown_event = threading.Event()
        self._starting_equity: float = config.starting_capital
        self._current_regime: str | None = None
        self._daily_plan_generated = False
        self._premarket_watchlist: list[str] = []
        self._dashboard_state = dashboard_state

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
        alpaca_screener = (
            AlpacaScreener(config.broker)
            if config.run_mode != RunMode.BACKTEST
            else None
        )
        self._scanner = MomentumGapperScanner(
            self._market_data,
            self._news,
            polygon or PolygonClient(config.data),
            config.scanner,
            fallback_client=alpaca_screener,
        )
        self._strategy = PullbackVWAPStrategy(config)
        self._sizer = PositionSizer(config.risk)
        self._circuit = CircuitBreaker(config.risk)
        self._portfolio = PortfolioManager(
            self._broker, config, circuit_breaker=self._circuit
        )

        # --- New feature modules ---

        # Market regime detection
        self._regime_detector = RegimeDetector()

        # Health monitoring
        self._health = HealthMonitor()
        self._health.set_circuit_breaker(self._circuit)
        self._health.set_portfolio_manager(self._portfolio)

        # Notifications
        self._notify = NotificationManager(config.notifications)

        # Correlation checking
        self._correlation = CorrelationChecker(self._market_data)

        # AI trading advisor
        self._advisor = TradingAdvisor()

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

        self._starting_equity = equity
        self._circuit.reset_daily(equity)
        self._sizer.reset_daily()
        self._portfolio.reset_daily()

        log.info("bot.account_loaded", equity=format_currency(equity))

        # Reconcile positions with broker on startup
        try:
            self._portfolio.reconcile_positions()
            log.info("bot.positions_reconciled")
        except Exception as e:
            log.error("bot.reconcile_error", error=str(e))
            self._health.record_error("reconciliation")
            self._notify.notify_error(
                error_type="reconciliation",
                message=f"Position reconciliation failed: {e}",
            )

        self._running = True
        self._shutdown_event.clear()
        self._daily_plan_generated = False
        self._premarket_watchlist = []
        tick_count = 0

        # Register signal handlers for graceful shutdown (Docker SIGTERM)
        def _signal_handler(signum: int, _frame: object) -> None:
            sig_name = signal.Signals(signum).name
            log.info("bot.shutdown_signal", signal=sig_name)
            self._running = False
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        # Push initial state to dashboard before first tick
        self._update_dashboard()

        while self._running:
            try:
                self._tick()
                tick_count += 1

                # Record tick for health monitoring
                self._health.record_tick()

                # Push state to dashboard
                self._update_dashboard()

                if tick_count % 10 == 0:
                    self._log_status()

                # Signal-aware sleep: wakes immediately on SIGTERM/SIGINT
                if self._shutdown_event.wait(
                    timeout=self._config.scanner.scan_interval_seconds
                ):
                    log.info("bot.shutdown_event_received")
                    break

            except KeyboardInterrupt:
                log.info("bot.keyboard_interrupt")
                break
            except Exception as e:
                log.error("bot.tick_error", error=str(e), exc_info=True)
                self._circuit.record_api_error()
                self._health.record_error("tick")
                self._update_dashboard(last_error=str(e))
                self._notify.notify_error(
                    error_type="tick_error",
                    message=f"Error during tick: {e}",
                )
                if not self._circuit.is_trading_allowed:
                    log.critical("bot.circuit_breaker_halted")
                    self._update_dashboard(
                        last_error="Circuit breaker HALTED: API errors exceeded threshold"
                    )
                    self._notify.notify_circuit_breaker(
                        state="halted",
                        reason="API errors exceeded threshold",
                        daily_pnl=self._portfolio.get_daily_pnl(),
                        consecutive_losses=0,
                    )
                    # Ask advisor for circuit breaker recommendation
                    cb_status = self._circuit.get_status()
                    cb_rec = self._advisor.recommend_circuit_breaker_action(
                        status=cb_status,
                        daily_trades=self._portfolio.get_daily_journal_entries(),
                    )
                    log.info(
                        "bot.advisor_circuit_breaker",
                        action=cb_rec.action,
                        reasons=cb_rec.reasons,
                    )
                    break
                # Signal-aware backoff: wake immediately on shutdown
                if self._shutdown_event.wait(timeout=30):
                    break

        # Push final state to dashboard before shutdown
        self._update_dashboard()

        # Shutdown: close all positions
        entries = self._portfolio.close_all("shutdown")

        # Notify on trade closures during shutdown
        for entry in entries:
            self._notify.notify_trade_closed(
                symbol=entry.symbol,
                side=entry.side,
                shares=entry.shares,
                entry_price=entry.entry_price,
                exit_price=entry.exit_price,
                pnl=entry.pnl,
                rr_ratio=entry.rr_ratio,
                hold_time_minutes=entry.hold_time_minutes,
                exit_reason=entry.exit_reason,
            )

        # Generate and log daily summary report
        self._generate_daily_summary()

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

            # Notify and get advisor recommendation on circuit breaker trigger
            cb_status = self._circuit.get_status()
            self._notify.notify_circuit_breaker(
                state=state.value,
                reason=f"drawdown {cb_status.get('drawdown_pct', 0):.1f}%",
                daily_pnl=self._portfolio.get_daily_pnl(),
                consecutive_losses=cb_status.get("consecutive_losses", 0),
            )
            cb_rec = self._advisor.recommend_circuit_breaker_action(
                status=cb_status,
                daily_trades=self._portfolio.get_daily_journal_entries(),
            )
            log.info(
                "bot.advisor_circuit_breaker",
                action=cb_rec.action,
                reasons=cb_rec.reasons,
            )
            return

        # 2. Check hard time exit SECOND
        if is_near_close(minutes_before=10):
            if self._portfolio.get_open_positions():
                entries = self._portfolio.close_all("hard_time_exit")
                log.info("bot.time_exit", trades_closed=len(entries))

                # Notify on time exit closures
                for entry in entries:
                    self._notify.notify_trade_closed(
                        symbol=entry.symbol,
                        side=entry.side,
                        shares=entry.shares,
                        entry_price=entry.entry_price,
                        exit_price=entry.exit_price,
                        pnl=entry.pnl,
                        rr_ratio=entry.rr_ratio,
                        hold_time_minutes=entry.hold_time_minutes,
                        exit_reason=entry.exit_reason,
                    )
            return

        # 3. Detect market regime from SPY data
        try:
            spy_bars = self._market_data.get_daily_bars("SPY", days=70)
            regime = self._regime_detector.detect(spy_bars)
            self._current_regime = regime.value
            regime_adjustments = self._regime_detector.get_regime_adjustments(regime)
        except Exception as e:
            log.warning("bot.regime_detection_error", error=str(e))
            self._current_regime = None
            regime_adjustments = {
                "position_size_multiplier": 1.0,
                "stop_multiplier": 1.0,
                "max_positions_override": None,
                "volume_confirmation_multiplier": 1.0,
            }
            self._health.record_error("regime_detection")

        # 4. Generate daily plan on first tick of the day (via advisor)
        if not self._daily_plan_generated:
            try:
                equity = self._broker.get_account_equity()
            except Exception:
                equity = self._config.starting_capital

            daily_plan = self._advisor.recommend_daily_plan(
                equity=equity,
                regime=self._current_regime or "low_volatility",
                recent_journal_entries=self._portfolio.get_daily_journal_entries(),
            )
            log.info(
                "bot.daily_plan",
                max_trades=daily_plan.max_trades_today,
                focus_setups=daily_plan.focus_setups,
                risk_budget_pct=daily_plan.risk_budget_pct,
                notes=daily_plan.notes,
            )
            self._daily_plan_generated = True

        # 5. Pre-market scanning (watchlist only, no trades)
        if is_premarket() and not is_market_open():
            self._premarket_scan()
            return

        # 6. Update existing positions (exits, scale-outs, trailing stops)
        entries = self._portfolio.update_positions(self._strategy, self._market_data)
        if entries:
            for e in entries:
                log.info(
                    "bot.trade_closed",
                    symbol=e.symbol,
                    pnl=format_currency(e.pnl),
                    reason=e.exit_reason,
                )
                # Notify on trade closure
                self._notify.notify_trade_closed(
                    symbol=e.symbol,
                    side=e.side,
                    shares=e.shares,
                    entry_price=e.entry_price,
                    exit_price=e.exit_price,
                    pnl=e.pnl,
                    rr_ratio=e.rr_ratio,
                    hold_time_minutes=e.hold_time_minutes,
                    exit_reason=e.exit_reason,
                )

        # 7. Only look for new entries during active hours
        if not is_market_open():
            return

        # Re-check circuit breaker after position updates
        if not self._circuit.is_trading_allowed:
            return

        # 8. Scan for candidates
        candidates = self._scanner.scan()
        if not candidates:
            self._health.record_scan(0)
            return

        self._health.record_scan(len(candidates))
        log.info("bot.scan_complete", candidates=len(candidates))

        # 9. Evaluate each candidate
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

            # Apply regime adjustment to position size
            size_multiplier = regime_adjustments.get("position_size_multiplier", 1.0)
            if size_multiplier != 1.0 and risk_result.shares > 0:
                adjusted_shares = max(1, int(risk_result.shares * size_multiplier))
                log.info(
                    "bot.regime_size_adjustment",
                    symbol=candidate.symbol,
                    original_shares=risk_result.shares,
                    adjusted_shares=adjusted_shares,
                    multiplier=size_multiplier,
                    regime=self._current_regime,
                )
                risk_result.shares = adjusted_shares

            # Correlation check with existing positions
            existing_symbols = list(open_symbols)
            if existing_symbols:
                try:
                    is_correlated = self._correlation.is_correlated(
                        new_symbol=candidate.symbol,
                        existing_symbols=existing_symbols,
                        market_data=self._market_data,
                    )
                    if is_correlated:
                        log.info(
                            "bot.correlation_rejected",
                            symbol=candidate.symbol,
                            existing=existing_symbols,
                        )
                        continue
                except Exception as e:
                    log.warning(
                        "bot.correlation_check_error",
                        symbol=candidate.symbol,
                        error=str(e),
                    )
                    self._health.record_error("correlation_check")

            # AI advisor entry recommendation
            advisor_rec = self._advisor.recommend_entry(
                signal=signal,
                scan_result=candidate,
                regime=self._current_regime or "low_volatility",
                positions=self._portfolio.get_open_positions(),
                equity=equity,
            )
            log.info(
                "bot.advisor_entry",
                symbol=candidate.symbol,
                action=advisor_rec.action,
                confidence=round(advisor_rec.confidence, 2),
                reasons=advisor_rec.reasons,
            )
            if advisor_rec.action == "skip":
                log.info(
                    "bot.advisor_skip",
                    symbol=candidate.symbol,
                    reasons=advisor_rec.reasons,
                )
                continue

            # Execute trade
            position = self._portfolio.open_position(signal, risk_result)
            self._sizer.record_trade_risk(risk_result.risk_dollars)
            open_symbols.add(position.symbol)

            # Record trade in health monitor
            self._health.record_trade(position.symbol)

            # Send trade opened notification
            self._notify.notify_trade_opened(
                symbol=position.symbol,
                side="buy",
                shares=position.shares,
                entry_price=position.entry_price,
                stop_price=position.stop_price,
                risk_dollars=risk_result.risk_dollars,
                signal_type=signal.signal_type.value,
            )

            log.info(
                "bot.trade_opened",
                symbol=position.symbol,
                shares=position.shares,
                entry=position.entry_price,
                stop=position.stop_price,
                risk=format_currency(risk_result.risk_dollars),
                regime=self._current_regime,
            )

    def _premarket_scan(self) -> None:
        """
        Run scanner during pre-market hours to build a watchlist.

        No trades are executed -- candidates are logged for awareness
        when the market opens.
        """
        try:
            candidates = self._scanner.scan()
        except Exception as e:
            log.warning("bot.premarket_scan_error", error=str(e))
            self._health.record_error("premarket_scan")
            return

        if not candidates:
            return

        watchlist_symbols = [c.symbol for c in candidates]
        self._premarket_watchlist = watchlist_symbols
        self._health.record_scan(len(candidates))

        log.info(
            "bot.premarket_watchlist",
            count=len(watchlist_symbols),
            symbols=watchlist_symbols[:10],  # Log up to 10 symbols
        )

    def _generate_daily_summary(self) -> None:
        """Generate and log a daily summary report at end of day / shutdown."""
        try:
            ending_equity = self._broker.get_account_equity()
        except Exception:
            ending_equity = self._starting_equity + self._portfolio.get_daily_pnl()

        journal_entries = self._portfolio.get_daily_journal_entries()
        circuit_status = self._circuit.get_status()

        report = DailySummaryReport(
            trades=journal_entries,
            starting_equity=self._starting_equity,
            ending_equity=ending_equity,
            circuit_breaker_status=circuit_status,
        )

        # Log the text summary
        summary_text = report.format_text()
        log.info("bot.daily_summary_report")
        print(summary_text)

        # Send daily summary notification
        report_data = report.generate()
        self._notify.notify_daily_summary(
            date=report_data.get("date", ""),
            total_trades=report_data.get("total_trades", 0),
            winning_trades=report_data.get("winners", 0),
            losing_trades=report_data.get("losers", 0),
            gross_pnl=report_data.get("gross_profit", 0.0) + report_data.get("gross_loss", 0.0),
            net_pnl=report_data.get("total_pnl", 0.0),
            win_rate=report_data.get("win_rate_pct", 0.0),
            largest_win=report_data.get("largest_winner", 0.0),
            largest_loss=report_data.get("largest_loser", 0.0),
            ending_equity=ending_equity,
        )

    def _log_status(self) -> None:
        """Log periodic status update with health and regime info."""
        positions = self._portfolio.get_open_positions()
        circuit_status = self._circuit.get_status()
        health_status = self._health.get_health_status()

        log.info(
            "bot.status",
            open_positions=len(positions),
            daily_pnl=format_currency(self._portfolio.get_daily_pnl()),
            circuit=circuit_status["state"],
            drawdown_pct=circuit_status["drawdown_pct"],
            regime=self._current_regime,
            health_tick_count=health_status["tick_count_today"],
            health_errors_5min=health_status["api_error_count_5min"],
            memory_mb=health_status["memory_usage_mb"],
            uptime_s=health_status["uptime_seconds"],
        )

        # Log warning if health check fails
        if not self._health.is_healthy():
            log.warning("bot.health_unhealthy", status=health_status)
            self._notify.notify_error(
                error_type="health_check",
                message="System health check failed",
                details=health_status,
            )

    def _get_market_status(self) -> tuple[str, str]:
        """Compute current market status and detail string."""
        from datetime import timedelta as _td

        from trading_bot.utils.helpers import (
            MARKET_OPEN,
            _nyse_holidays,
        )

        now = now_et()
        today = now.date()

        if is_market_open():
            return "open", "Market is open"
        elif is_premarket():
            return "premarket", "Pre-market session"
        else:
            # Determine why we're closed
            is_holiday = is_market_holiday(today)
            if is_holiday and today.weekday() < 5:
                label = "Market holiday"
            elif today.weekday() >= 5:
                label = "Weekend"
            else:
                label = "Market closed"

            # Find next trading day
            next_day = today + _td(days=1)
            while next_day.weekday() >= 5 or next_day in _nyse_holidays(next_day.year):
                next_day += _td(days=1)
            next_open_dt = now.replace(
                year=next_day.year,
                month=next_day.month,
                day=next_day.day,
                hour=MARKET_OPEN.hour,
                minute=MARKET_OPEN.minute,
                second=0,
                microsecond=0,
            )
            label += f" \u2022 Next open: {next_open_dt.strftime('%a %b %d, %I:%M %p ET')}"

            status = "holiday" if (is_holiday and today.weekday() < 5) else "closed"
            return status, label

    def _update_dashboard(self, last_error: str | None = None) -> None:
        """Push current state to the dashboard (if enabled)."""
        if self._dashboard_state is None:
            return

        try:
            equity = self._broker.get_account_equity()
            buying_power = self._broker.get_buying_power()
        except Exception:
            equity = self._starting_equity + self._portfolio.get_daily_pnl()
            buying_power = equity * 4

        positions = self._portfolio.get_open_positions()
        position_dicts = [
            {
                "symbol": p.symbol,
                "signal_type": p.signal_type.value,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "shares": p.shares,
                "shares_remaining": p.shares_remaining,
                "stop_price": p.stop_price,
                "pnl_unrealized": round(p.pnl_unrealized, 2),
                "pnl_realized": round(p.pnl_realized, 2),
                "scale_outs_completed": p.scale_outs_completed,
                "trailing_stop_active": p.trailing_stop_active,
                "trailing_stop_price": p.trailing_stop_price,
                "entry_time": p.entry_time.isoformat(),
            }
            for p in positions
        ]

        journal_dicts = [e.to_dict() for e in self._portfolio.get_daily_journal_entries()]

        market_status, market_status_detail = self._get_market_status()

        self._dashboard_state.update(
            equity=equity,
            starting_equity=self._starting_equity,
            daily_pnl=self._portfolio.get_daily_pnl(),
            buying_power=buying_power,
            open_positions=position_dicts,
            journal_entries=journal_dicts,
            circuit_breaker=self._circuit.get_status(),
            health=self._health.get_health_status(),
            regime=self._current_regime,
            run_mode=self._config.run_mode.value,
            market_status=market_status,
            market_status_detail=market_status_detail,
            last_error=last_error,
        )

    def stop(self) -> None:
        """Signal the bot to stop gracefully."""
        self._running = False
        self._shutdown_event.set()


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
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
        help="Dashboard web UI port (default: 8080, 0 to disable)",
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

    # Start dashboard server (background thread)
    dashboard_state: DashboardState | None = None
    if args.dashboard_port and args.dashboard_port > 0:
        from trading_bot.dashboard.app import start_dashboard_server

        dashboard_state = DashboardState()
        start_dashboard_server(dashboard_state, port=args.dashboard_port)
        print(f"  Dashboard: http://localhost:{args.dashboard_port}")

    # Create and run bot
    bot = TradingBot(config, dashboard_state=dashboard_state)

    # Graceful shutdown handlers
    def handle_signal(signum, frame):
        log.info("bot.signal_received", signal=signum)
        bot.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bot.run()


if __name__ == "__main__":
    main()
