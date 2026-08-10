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
import collections
import csv
import os
import signal
import sys
import threading
from pathlib import Path

import structlog
from dotenv import load_dotenv

from trading_bot.config.settings import AppConfig, RunMode
from trading_bot.dashboard.state import DashboardState
from trading_bot.data.market_data import BacktestMarketData, LiveMarketData
from trading_bot.data.news_client import NewsClient
from trading_bot.data.alpaca_screener import AlpacaScreener
from trading_bot.data.polygon_client import PolygonClient
from trading_bot.data.yahoo_screener import YahooScreener
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.portfolio.manager import PortfolioManager
from trading_bot.risk.circuit_breaker import CircuitBreaker, CircuitState
from trading_bot.risk.correlation import CorrelationChecker
from trading_bot.risk.position_sizer import PositionSizer
from trading_bot.risk.live_readiness import evaluate_live_readiness
from trading_bot.scanners.momentum_gappers import MomentumGapperScanner
from trading_bot.strategies.advisor import TradingAdvisor
from trading_bot.strategies.pullback_vwap import PullbackVWAPStrategy
from trading_bot.strategies.regime import MarketRegime, RegimeDetector
from trading_bot.utils.health import HealthMonitor
from trading_bot.utils.helpers import (
    format_currency,
    is_market_holiday,
    is_market_open,
    is_near_close,
    is_premarket,
    now_et,
)
from trading_bot.utils.logger import setup_logging
from trading_bot.utils.notifications import NotificationManager
from trading_bot.models.domain import FeatureSnapshot, RejectedSignal, SignalDecision
from trading_bot.persistence.decision_log import DecisionLogger
from trading_bot.core.alpha import AlphaFilter, AlphaLogger, RuleBasedAlphaScorer
from trading_bot.reporting.daily_report import generate_daily_report
from trading_bot.utils.indicators import compute_atr
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


_BROKER_KEY_PLACEHOLDERS = {
    "",
    "your_alpaca_api_key_here",
    "your_alpaca_api_secret_here",
}


def _has_valid_alpaca_credentials(config: AppConfig) -> bool:
    """Return whether both broker secrets are configured and non-placeholder."""
    key = config.broker.alpaca_api_key.get_secret_value().strip()
    secret = config.broker.alpaca_api_secret.get_secret_value().strip()
    return key not in _BROKER_KEY_PLACEHOLDERS and secret not in _BROKER_KEY_PLACEHOLDERS


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
        self._latest_candidates: list[dict[str, object]] = []
        self._broker_provider = "local_paper"
        self._dashboard_state = dashboard_state

        # Wire up dependencies based on run mode
        if config.run_mode == RunMode.BACKTEST:
            self._market_data = BacktestMarketData(
                config.backtest_start_date or "2024-01-01",
                config.backtest_end_date or "2025-01-01",
            )
            self._broker = PaperBroker(initial_equity=config.starting_capital)
            self._broker_provider = "backtest_paper"
            polygon = None
        else:
            polygon = PolygonClient(config.data)
            self._market_data = LiveMarketData(
                polygon, float_cache_hours=config.data.float_cache_hours
            )
            if config.run_mode == RunMode.LIVE:
                self._assert_live_evidence_gate()
                self._broker = AlpacaBroker(config.broker)
                self._broker_provider = "alpaca_live"
            elif config.broker.alpaca_paper and _has_valid_alpaca_credentials(config):
                # Alpaca paper is the production private-launch broker. Account,
                # orders, and positions survive process/container restarts.
                self._broker = AlpacaBroker(config.broker)
                self._broker_provider = "alpaca_paper"
                log.info("bot.paper_broker_selected", provider="alpaca")
            else:  # Local PAPER fallback for keyless development/tests
                self._broker = PaperBroker(initial_equity=config.starting_capital)
                self._broker_provider = "local_paper"
                log.warning(
                    "bot.paper_broker_selected",
                    provider="local_in_memory",
                    persistence="none",
                )

        self._news = NewsClient(polygon, config.scanner)
        alpaca_screener = (
            AlpacaScreener(config.broker)
            if config.run_mode != RunMode.BACKTEST
            else None
        )
        yahoo_screener = (
            YahooScreener()
            if config.run_mode != RunMode.BACKTEST
            else None
        )
        self._scanner = MomentumGapperScanner(
            self._market_data,
            self._news,
            polygon or PolygonClient(config.data),
            config.scanner,
            fallback_client=alpaca_screener,
            yahoo_client=yahoo_screener,
        )
        self._strategy = PullbackVWAPStrategy(config)
        self._sizer = PositionSizer(config.risk)
        self._circuit = CircuitBreaker(config.risk)
        self._portfolio = PortfolioManager(
            self._broker, config, circuit_breaker=self._circuit,
            market_data=self._market_data,
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
        self._last_circuit_alert_state: CircuitState | None = None

        # Correlation checking
        self._correlation = CorrelationChecker(self._market_data)

        # AI trading advisor
        self._advisor = TradingAdvisor()

        # Rejected signal shadow journal (capped to prevent unbounded growth)
        self._rejected_signals: collections.deque[RejectedSignal] = collections.deque(maxlen=5000)
        self._rejected_csv = Path(config.journal_csv_path).parent / "rejected_signals.csv"
        self._ensure_rejected_csv()

        # Phase 1.5 Core conversion: structured feature + decision capture.
        # This is a separate dataset from the trade journal and rejected
        # shadow log — one row per candidate evaluation, always.
        decision_log_path = Path(config.journal_csv_path).parent / "decision_log.csv"
        self._decision_logger = DecisionLogger(decision_log_path)

        # Phase 2 Core conversion: alpha scoring layer (SHADOW MODE ONLY).
        # Scores every decision for offline analysis — does NOT block or
        # approve trades. Trading behavior is identical with or without it.
        alpha_log_path = Path(config.journal_csv_path).parent / "alpha_scores.csv"
        self._alpha_scorer = RuleBasedAlphaScorer()
        self._alpha_logger = AlphaLogger(alpha_log_path)

        # Phase 3 Core conversion: opt-in paper-only alpha filter gate.
        # OFF by default — when the env var TRADING_ALPHA_FILTER_ENABLED=true
        # and the bot is in paper mode, weak-tier trades (below
        # TRADING_ALPHA_FILTER_MIN_TIER, default B) are rejected AFTER the
        # risk engine approves them. LIVE mode always ignores the filter.
        self._alpha_filter = AlphaFilter(
            scorer=self._alpha_scorer,
            run_mode=config.run_mode.value,
        )
        if self._alpha_filter.active:
            log.info(
                "bot.alpha_filter_active",
                min_tier=self._alpha_filter.min_tier,
                run_mode=self._alpha_filter.run_mode,
            )

        # Daily auto-reset tracking
        self._last_trading_date: str | None = None

    def _assert_live_evidence_gate(self) -> None:
        """Refuse to construct a live broker until paper evidence passes."""
        rows: list[dict[str, str]] = []
        journal_path = Path(self._config.journal_csv_path)
        if journal_path.is_file():
            try:
                with journal_path.open(newline="") as journal_file:
                    rows = list(csv.DictReader(journal_file))
            except OSError as exc:
                raise RuntimeError(
                    f"Live trading blocked: journal could not be read ({exc})"
                ) from exc

        result = evaluate_live_readiness(
            rows,
            starting_equity=self._config.starting_capital,
        )
        if not result.ready:
            detail = "; ".join(result.reasons) or "paper evidence is incomplete"
            raise RuntimeError(f"Live trading blocked by evidence gate: {detail}")

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

                # Signal-aware sleep with adaptive interval
                interval = self._get_adaptive_scan_interval()
                if self._shutdown_event.wait(timeout=interval):
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
                    self._notify_circuit_state_change(self._circuit.state)
                    break
                # Signal-aware backoff: wake immediately on shutdown
                if self._shutdown_event.wait(timeout=30):
                    break

        # Push final state to dashboard before shutdown
        self._update_dashboard()

        # Shutdown: close all positions
        entries = self._portfolio.close_all("shutdown")

        # Verify all positions actually closed at the broker
        try:
            remaining = self._broker.get_positions()
            if remaining:
                log.critical(
                    "bot.shutdown_positions_remaining",
                    count=len(remaining),
                    symbols=[p["symbol"] for p in remaining],
                    detail="Attempting broker-side close_all as last resort",
                )
                self._broker.close_all_positions()
        except Exception as e:
            log.error("bot.shutdown_verify_error", error=str(e))

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

        # Phase 3.2 — post-run alpha validation report. Best-effort
        # only: any failure here is logged and swallowed so shutdown
        # can never be delayed or blocked by post-run analytics.
        self._generate_daily_alpha_report()

        log.info(
            "bot.shutdown",
            trades_closed=len(entries),
            daily_pnl=format_currency(self._portfolio.get_daily_pnl()),
        )

    def _notify_circuit_state_change(self, state: CircuitState) -> None:
        """Notify and consult the advisor once per circuit state transition."""
        if state == self._last_circuit_alert_state:
            return

        cb_status = self._circuit.get_status()
        self._notify.notify_circuit_breaker(
            state=state.value,
            reason=cb_status.get("halt_reason") or "risk state changed",
            daily_pnl=cb_status.get("daily_pnl", 0.0),
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
        self._last_circuit_alert_state = state

    def _tick(self) -> None:
        """Single iteration of the main trading loop."""
        # 0a. Check if a new trading day started (auto-reset daily state)
        self._check_daily_reset()

        # 0b. Feed unrealized P&L to circuit breaker so it can halt
        #    BEFORE a catastrophic open position is closed at a loss.
        open_positions = self._portfolio.get_open_positions()
        unrealized = sum(p.pnl_unrealized for p in open_positions)
        self._circuit.update_unrealized_pnl(unrealized)

        # 1. Check circuit breaker FIRST (NON-NEGOTIABLE)
        state = self._circuit.check()
        if not self._circuit.is_trading_allowed:
            log.warning("bot.circuit_active", state=state.value)

            # EMERGENCY: close all open positions when circuit breaker halts
            # to prevent unrealized losses from growing further.
            if state == CircuitState.HALTED and open_positions:
                log.critical(
                    "bot.emergency_close",
                    positions=len(open_positions),
                    unrealized_pnl=round(unrealized, 2),
                )
                entries = self._portfolio.close_all("circuit_breaker_halt")
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

            self._notify_circuit_state_change(state)
            return

        # A recovery or reset makes the next halt a new transition.
        self._last_circuit_alert_state = None

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
            # Default to range_bound — the safest "we don't know" regime.
            # LOW_VOLATILITY imposes a stricter volume confirmation
            # multiplier (1.5x) which can inadvertently filter out
            # valid candidates.
            self._current_regime = "range_bound"
            regime_adjustments = self._regime_detector.get_regime_adjustments(
                MarketRegime.RANGE_BOUND
            )
            self._health.record_error("regime_detection")

        # Push regime to strategy for adaptive entry parameters
        self._strategy.set_regime(self._current_regime, regime_adjustments)

        # 4. Generate daily plan on first tick of the day (via advisor)
        if not self._daily_plan_generated:
            try:
                equity = self._broker.get_account_equity()
            except Exception:
                equity = self._starting_equity  # Use last known, not config default

            daily_plan = self._advisor.recommend_daily_plan(
                equity=equity,
                regime=self._current_regime or "range_bound",
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
        self._latest_candidates = [self._candidate_to_dashboard(c) for c in candidates]
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
                # Still capture a snapshot so the dataset records EVERY candidate.
                snapshot = self._build_feature_snapshot(candidate, None)
                self._log_decision(snapshot, action="skip", reason="already_held")
                continue

            # Fetch intraday bars
            bars = self._market_data.get_intraday_bars(candidate.symbol)
            if bars.empty:
                log.warning(
                    "bot.empty_bars",
                    symbol=candidate.symbol,
                    detail="No intraday bars from Polygon or yfinance",
                )
                snapshot = self._build_feature_snapshot(candidate, None)
                self._log_decision(snapshot, action="skip", reason="empty_bars")
                continue

            # Build the feature snapshot once per candidate — reused across
            # every decision branch below. Pure observation, no side effects.
            snapshot = self._build_feature_snapshot(candidate, bars)

            log.info(
                "bot.evaluating_candidate",
                symbol=candidate.symbol,
                bars_count=len(bars),
                score=round(candidate.score, 3),
                gap_pct=round(candidate.gap_pct, 1),
                rvol=round(candidate.relative_volume, 1),
            )

            # Evaluate strategy
            signal = self._strategy.evaluate(candidate, bars)
            if signal is None:
                # Build a concise reason from the strategy's per-setup
                # rejection diagnostics so the CSV and dashboard show
                # exactly which checks failed.
                details = getattr(self._strategy, "last_rejection_details", {})
                if details:
                    reason = "; ".join(
                        f"{k}:{v}" for k, v in details.items()
                    )
                else:
                    reason = "no_valid_setup"
                self._record_rejection(RejectedSignal(
                    timestamp=now_et(),
                    symbol=candidate.symbol,
                    stage="strategy",
                    reason=reason,
                    entry_price=candidate.price,
                    gap_pct=candidate.gap_pct,
                    score=candidate.score,
                ))
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason=f"strategy:{reason}",
                )
                continue

            # Risk check — equity is critical for position sizing.
            # If broker API is down, skip new entries rather than sizing
            # based on stale/wrong starting_capital.
            try:
                equity = self._broker.get_account_equity()
                buying_power = self._broker.get_buying_power()
            except Exception as e:
                log.error(
                    "bot.equity_api_failed",
                    symbol=candidate.symbol,
                    error=str(e),
                )
                self._health.record_error("equity_api")
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason="equity_api_failed",
                    confidence=signal.confidence,
                )
                continue  # Skip this candidate — can't size without equity

            # Guard against zero/negative equity (API returned garbage)
            if equity <= 0:
                log.error(
                    "bot.zero_equity",
                    equity=equity,
                    detail="Broker returned zero equity — skipping all entries",
                )
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason="zero_equity",
                    confidence=signal.confidence,
                )
                break

            # Apply regime-based max positions override before risk check
            open_positions = self._portfolio.get_open_positions()
            max_pos_override = regime_adjustments.get("max_positions_override")
            if max_pos_override is not None and len(open_positions) >= max_pos_override:
                reason_text = (
                    f"regime_max_positions: {len(open_positions)}/"
                    f"{max_pos_override} ({self._current_regime})"
                )
                self._record_rejection(RejectedSignal(
                    timestamp=now_et(),
                    symbol=candidate.symbol,
                    stage="risk",
                    reason=reason_text,
                    entry_price=signal.entry_price,
                    stop_price=signal.stop_price,
                    signal_type=signal.signal_type.value,
                    gap_pct=candidate.gap_pct,
                    score=candidate.score,
                ))
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason=f"risk:{reason_text}",
                    confidence=signal.confidence,
                )
                continue

            risk_result = self._sizer.calculate(
                equity=equity,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                current_positions=open_positions,
                buying_power=buying_power,
            )

            if not risk_result.approved:
                self._record_rejection(RejectedSignal(
                    timestamp=now_et(),
                    symbol=candidate.symbol,
                    stage="risk",
                    reason=risk_result.reason,
                    entry_price=signal.entry_price,
                    stop_price=signal.stop_price,
                    signal_type=signal.signal_type.value,
                    gap_pct=candidate.gap_pct,
                    score=candidate.score,
                ))
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason=f"risk:{risk_result.reason}",
                    confidence=signal.confidence,
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

            # Tiered position sizing by setup quality
            # A+ setups (confidence >= 0.80): full size
            # B setups (confidence 0.65-0.80): 65% size
            # C setups (confidence 0.55-0.65): 40% size
            confidence = signal.confidence
            if confidence < 0.65 and risk_result.shares > 0:
                tier_mult = 0.40 if confidence < 0.65 else 0.65
                tier_shares = max(1, int(risk_result.shares * tier_mult))
                log.info(
                    "bot.tiered_sizing",
                    symbol=candidate.symbol,
                    confidence=round(confidence, 3),
                    tier="C",
                    original=risk_result.shares,
                    adjusted=tier_shares,
                )
                risk_result.shares = tier_shares
            elif confidence < 0.80 and risk_result.shares > 0:
                tier_shares = max(1, int(risk_result.shares * 0.65))
                log.info(
                    "bot.tiered_sizing",
                    symbol=candidate.symbol,
                    confidence=round(confidence, 3),
                    tier="B",
                    original=risk_result.shares,
                    adjusted=tier_shares,
                )
                risk_result.shares = tier_shares

            # Volatility-scaled sizing: reduce shares for high-ATR stocks.
            # ATR/price > 5% = very volatile, scale down proportionally.
            # This prevents outsized dollar losses on erratic movers.
            if signal.atr > 0 and signal.entry_price > 0 and risk_result.shares > 0:
                atr_pct = (signal.atr / signal.entry_price) * 100
                if atr_pct > 5.0:
                    vol_mult = min(1.0, 5.0 / atr_pct)  # Scale down proportionally
                    vol_shares = max(1, int(risk_result.shares * vol_mult))
                    log.info(
                        "bot.volatility_scaling",
                        symbol=candidate.symbol,
                        atr_pct=round(atr_pct, 2),
                        multiplier=round(vol_mult, 2),
                        original=risk_result.shares,
                        adjusted=vol_shares,
                    )
                    risk_result.shares = vol_shares

            # Graduated loss streak cooldown — reduce size after consecutive losses
            streak_mult = self._circuit.get_loss_streak_multiplier()
            if streak_mult < 1.0 and risk_result.shares > 0:
                streak_shares = max(1, int(risk_result.shares * streak_mult))
                log.info(
                    "bot.loss_streak_reduction",
                    symbol=candidate.symbol,
                    consecutive_losses=self._circuit.consecutive_losses,
                    multiplier=streak_mult,
                    original=risk_result.shares,
                    adjusted=streak_shares,
                )
                risk_result.shares = streak_shares

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
                        corr_reason = f"correlated_with_{existing_symbols}"
                        self._record_rejection(RejectedSignal(
                            timestamp=now_et(),
                            symbol=candidate.symbol,
                            stage="correlation",
                            reason=corr_reason,
                            entry_price=signal.entry_price,
                            stop_price=signal.stop_price,
                            signal_type=signal.signal_type.value,
                            gap_pct=candidate.gap_pct,
                            score=candidate.score,
                        ))
                        self._log_decision(
                            snapshot,
                            action="skip",
                            reason=f"correlation:{corr_reason}",
                            confidence=signal.confidence,
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
                regime=self._current_regime or "range_bound",
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
                advisor_reason = "; ".join(advisor_rec.reasons)
                self._record_rejection(RejectedSignal(
                    timestamp=now_et(),
                    symbol=candidate.symbol,
                    stage="advisor",
                    reason=advisor_reason,
                    entry_price=signal.entry_price,
                    stop_price=signal.stop_price,
                    signal_type=signal.signal_type.value,
                    gap_pct=candidate.gap_pct,
                    score=candidate.score,
                ))
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason=f"advisor:{advisor_reason}",
                    confidence=advisor_rec.confidence,
                )
                continue

            # Phase 3: paper-only alpha filter gate. Runs AFTER every
            # risk / correlation / advisor check so it can ONLY block
            # an already-approved trade — never approve one risk
            # rejected, never upsize, never bypass any safety rail.
            # Live mode always returns blocked=False.
            filter_decision = self._alpha_filter.check(
                snapshot, confidence=advisor_rec.confidence
            )
            if filter_decision.blocked:
                log.info(
                    "bot.alpha_filter_blocked",
                    symbol=candidate.symbol,
                    tier=filter_decision.tier,
                    min_tier=filter_decision.min_tier,
                )
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason=filter_decision.reason,
                    confidence=advisor_rec.confidence,
                )
                continue

            # Execute trade
            position = self._portfolio.open_position(signal, risk_result)

            # Check if entry was rejected by broker
            if position.shares == 0:
                log.warning(
                    "bot.entry_rejected_by_broker",
                    symbol=candidate.symbol,
                )
                self._log_decision(
                    snapshot,
                    action="skip",
                    reason="broker_rejected",
                    confidence=advisor_rec.confidence,
                )
                continue

            # Record executed-buy decision (paired with same snapshot).
            self._log_decision(
                snapshot,
                action="buy",
                reason="executed",
                confidence=advisor_rec.confidence,
            )

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

    @staticmethod
    def _candidate_to_dashboard(candidate) -> dict[str, object]:
        """Project a scanner result into a truthful, read-only dashboard row."""
        return {
            "symbol": candidate.symbol,
            "price": round(float(candidate.price), 4),
            "gap_pct": round(float(candidate.gap_pct), 3),
            "relative_volume": round(float(candidate.relative_volume), 3),
            "float_shares": candidate.float_shares,
            "volume": int(candidate.volume),
            "prev_close": round(float(candidate.prev_close), 4),
            "catalyst": candidate.catalyst,
            "scanner_score": round(float(candidate.score), 4),
            "observed_at": candidate.timestamp.isoformat(),
        }

    def _compute_volatility(self, bars, price: float) -> float:
        """
        Derive a unit-less volatility figure from ATR(14) as a percentage
        of current price. Returns 0.0 when bars are missing, too short,
        or price is non-positive. Best-effort — never raises.
        """
        try:
            if bars is None or price <= 0:
                return 0.0
            if hasattr(bars, "empty") and bars.empty:
                return 0.0
            if len(bars) < 2:
                return 0.0
            atr = compute_atr(bars, length=14)
            if atr is None or len(atr) == 0:
                return 0.0
            last_atr = float(atr.iloc[-1])
            if last_atr != last_atr:  # NaN check
                return 0.0
            return (last_atr / price) * 100.0
        except Exception:
            return 0.0

    def _build_feature_snapshot(self, candidate, bars) -> FeatureSnapshot:
        """
        Build a FeatureSnapshot for a single scan candidate.

        Phase 1.5 Core conversion instrumentation. Pure observation —
        does not change any trading behavior.
        """
        return FeatureSnapshot(
            symbol=candidate.symbol,
            timestamp=now_et(),
            price=float(candidate.price),
            gap_pct=float(candidate.gap_pct),
            relative_volume=float(candidate.relative_volume),
            volatility=self._compute_volatility(bars, float(candidate.price)),
            regime=self._current_regime or "unknown",
        )

    def _log_decision(
        self,
        snapshot: FeatureSnapshot,
        action: str,
        reason: str,
        confidence: float = 0.5,
    ) -> None:
        """
        Emit one row to the decision log and one shadow-mode alpha score.

        Safe — never raises. Alpha scoring is pure observation and cannot
        block or approve trades (Phase 2 shadow mode).
        """
        decision = SignalDecision(
            timestamp=now_et(),
            symbol=snapshot.symbol,
            action=action,
            confidence=float(confidence),
            reason=reason,
        )
        try:
            self._decision_logger.log(snapshot, decision)
        except Exception as e:
            log.debug("bot.decision_log_error", error=str(e))

        # Shadow-mode alpha score. Any failure here is silent — the
        # trading loop never sees an exception from this path and the
        # score is never consulted for an accept/reject decision.
        try:
            alpha = self._alpha_scorer.score(snapshot, decision)
            self._alpha_logger.log(alpha, snapshot, decision)
        except Exception as e:
            log.debug("bot.alpha_score_error", error=str(e))

    def _ensure_rejected_csv(self) -> None:
        """Create the rejected signals CSV if it doesn't exist."""
        headers = [
            "timestamp", "symbol", "stage", "reason",
            "entry_price", "stop_price", "signal_type", "gap_pct", "score",
        ]
        self._rejected_csv.parent.mkdir(parents=True, exist_ok=True)
        if not self._rejected_csv.exists():
            try:
                with open(self._rejected_csv, "w", newline="") as f:
                    csv.DictWriter(f, fieldnames=headers).writeheader()
            except Exception as e:
                log.error("bot.rejected_csv_create_error", error=str(e))

    def _record_rejection(self, rejection: RejectedSignal) -> None:
        """Log a rejected signal to both memory and CSV."""
        self._rejected_signals.append(rejection)
        try:
            with open(self._rejected_csv, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rejection.to_dict().keys()))
                writer.writerow(rejection.to_dict())
        except Exception as e:
            log.debug("bot.rejected_csv_write_error", error=str(e))

        log.info(
            "bot.signal_rejected",
            symbol=rejection.symbol,
            stage=rejection.stage,
            reason=rejection.reason,
        )

    def _check_daily_reset(self) -> None:
        """
        Reset daily state when a new trading day starts.

        The bot runs 24/7 on Railway and doesn't restart each day.
        This detects when the date changes and resets daily counters.
        """
        today = now_et().strftime("%Y-%m-%d")
        if self._last_trading_date is None:
            self._last_trading_date = today
            return

        if today != self._last_trading_date:
            log.info(
                "bot.daily_reset",
                previous_date=self._last_trading_date,
                new_date=today,
            )

            # Generate end-of-day report for previous day
            self._generate_daily_summary()

            # Phase 3.2: alpha validation report for the day that just
            # ended. Uses the PREVIOUS date explicitly so the dated CSVs
            # picked up by the rotation scheme are the correct ones.
            self._generate_daily_alpha_report(date=self._last_trading_date)

            # Reset all daily counters
            try:
                equity = self._broker.get_account_equity()
            except Exception:
                equity = self._starting_equity + self._portfolio.get_daily_pnl()

            self._starting_equity = equity
            self._circuit.reset_daily(equity)
            self._sizer.reset_daily()
            self._portfolio.reset_daily()
            self._daily_plan_generated = False
            self._premarket_watchlist = []
            self._rejected_signals.clear()
            self._last_trading_date = today

            # Reconcile positions on new day
            try:
                self._portfolio.reconcile_positions()
            except Exception as e:
                log.error("bot.daily_reconcile_error", error=str(e))

            log.info(
                "bot.new_day_started",
                equity=format_currency(equity),
                date=today,
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
            self._latest_candidates = []
            return

        self._latest_candidates = [self._candidate_to_dashboard(c) for c in candidates]

        watchlist_symbols = [c.symbol for c in candidates]
        self._premarket_watchlist = watchlist_symbols
        self._health.record_scan(len(candidates))

        log.info(
            "bot.premarket_watchlist",
            count=len(watchlist_symbols),
            symbols=watchlist_symbols[:10],  # Log up to 10 symbols
        )

    def _generate_daily_summary(self) -> None:
        """Generate, log, persist, and notify a daily summary report."""
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

        # Persist report to file for review across redeploys
        report_data = report.generate()
        try:
            reports_dir = Path(self._config.journal_csv_path).parent / "daily_reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            report_date = report_data.get("date", now_et().strftime("%Y-%m-%d"))
            report_file = reports_dir / f"report_{report_date}.txt"
            with open(report_file, "w") as f:
                f.write(summary_text)
                f.write(f"\n\nRejected signals today: {len(self._rejected_signals)}\n")
                for stage in ["strategy", "risk", "correlation", "advisor"]:
                    count = sum(1 for r in self._rejected_signals if r.stage == stage)
                    if count:
                        f.write(f"  {stage}: {count}\n")
            log.info("bot.report_saved", path=str(report_file))
        except Exception as e:
            log.error("bot.report_save_error", error=str(e))

        # Send daily summary notification
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

    def _generate_daily_alpha_report(self, date: str | None = None) -> None:
        """
        Write the Phase 3.2 post-run alpha validation report.

        Best-effort: every failure is logged and swallowed so shutdown
        (or the midnight-rollover reset) can never be delayed or
        blocked by post-run analytics. No shared state is mutated.
        """
        data_dir = Path(self._config.journal_csv_path).parent
        reports_dir = data_dir / "alpha_reports"
        try:
            result = generate_daily_report(
                date=date,
                data_dir=data_dir,
                reports_dir=reports_dir,
            )
            if result.success:
                log.info(
                    "bot.daily_alpha_report_written",
                    date=result.date,
                    txt=str(result.txt_path),
                    json=str(result.json_path),
                )
            else:
                log.info(
                    "bot.daily_alpha_report_skipped",
                    date=result.date,
                    error=result.error,
                )
        except Exception as exc:  # pragma: no cover — defense in depth
            log.warning("bot.daily_alpha_report_error", error=str(exc))

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

    def _get_adaptive_scan_interval(self) -> int:
        """
        Adaptive scan interval based on time of day.

        Elite traders react fastest during the opening drive and slow down
        during the dead zone. This mirrors that behavior:

        - 9:30-10:00 (opening drive):  10s — maximum opportunity window
        - 10:00-10:30 (power zone):    15s — still high-probability setups
        - 10:30-11:30 (active morning): 30s — moderate scanning
        - 11:30-13:00 (dead zone):     60s — low probability, conserve API
        - 13:00-15:30 (afternoon):     30s — second wind opportunity
        - 15:30-16:00 (close):         60s — manage only, no new entries
        - Pre-market/closed:           60s — watchlist building only
        """
        if not is_market_open():
            return self._config.scanner.scan_interval_seconds

        now = now_et()
        hour, minute = now.hour, now.minute

        # Opening drive: 9:30-10:00
        if hour == 9 and minute >= 30:
            return 10
        # Power zone: 10:00-10:30
        if hour == 10 and minute < 30:
            return 15
        # Active morning: 10:30-11:30
        if (hour == 10 and minute >= 30) or (hour == 11 and minute < 30):
            return 30
        # Dead zone: 11:30-13:00
        if (hour == 11 and minute >= 30) or hour == 12:
            return 60
        # Afternoon push: 13:00-15:30
        if hour >= 13 and (hour < 15 or (hour == 15 and minute < 30)):
            return 30
        # Closing: 15:30+
        return 60

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

        # Compute rejection stats by stage
        rejected_by_stage: dict[str, int] = {}
        for r in self._rejected_signals:
            rejected_by_stage[r.stage] = rejected_by_stage.get(r.stage, 0) + 1

        # Send the last 200 detailed rejections (most recent first)
        recent_rejections = [
            r.to_dict() for r in reversed(self._rejected_signals)
        ][:200]

        self._dashboard_state.update(
            equity=equity,
            starting_equity=self._starting_equity,
            daily_pnl=self._portfolio.get_daily_pnl(),
            buying_power=buying_power,
            open_positions=position_dicts,
            journal_entries=journal_dicts,
            scanner_candidates=self._latest_candidates,
            circuit_breaker=self._circuit.get_status(),
            health=self._health.get_health_status(),
            regime=self._current_regime,
            run_mode=self._config.run_mode.value,
            broker_provider=self._broker_provider,
            market_status=market_status,
            market_status_detail=market_status_detail,
            last_error=last_error,
            rejected_signals_count=len(self._rejected_signals),
            rejected_by_stage=rejected_by_stage,
            rejected_signals=recent_rejections,
        )

    def stop(self) -> None:
        """Signal the bot to stop gracefully."""
        self._running = False
        self._shutdown_event.set()


def _reset_paper_account(config: AppConfig) -> None:
    """Reset the Alpaca paper trading account to its initial state."""
    import requests

    api_key = config.broker.alpaca_api_key.get_secret_value()
    api_secret = config.broker.alpaca_api_secret.get_secret_value()

    if not api_key or api_key in ("", "your_alpaca_api_key_here"):
        print("ERROR: Alpaca API key not configured. Set ALPACA_API_KEY in .env or environment.")
        sys.exit(1)
    if not api_secret or api_secret in ("", "your_alpaca_api_secret_here"):
        print("ERROR: Alpaca API secret not configured. Set ALPACA_API_SECRET in .env or environment.")
        sys.exit(1)

    base_url = config.broker.alpaca_base_url or "https://paper-api.alpaca.markets"
    if "paper" not in base_url:
        print("ERROR: --reset-paper can only be used with a paper trading account.")
        print(f"  Current base URL: {base_url}")
        sys.exit(1)

    print(f"Resetting Alpaca paper trading account at {base_url} ...")

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }

    try:
        resp = requests.delete(f"{base_url}/v2/account", headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            print("Paper account reset successfully!")
            print(f"  Account ID:    {data.get('id', 'N/A')}")
            print(f"  Status:        {data.get('status', 'N/A')}")
            print(f"  Cash:          ${float(data.get('cash', 0)):,.2f}")
            print(f"  Equity:        ${float(data.get('equity', 0)):,.2f}")
            print(f"  Buying Power:  ${float(data.get('buying_power', 0)):,.2f}")
        elif resp.status_code == 401:
            print("ERROR: Authentication failed. Check your ALPACA_API_KEY and ALPACA_API_SECRET.")
            sys.exit(1)
        elif resp.status_code == 403:
            print("ERROR: Not authorized. Ensure you are using paper trading credentials.")
            sys.exit(1)
        else:
            print(f"ERROR: Unexpected response (HTTP {resp.status_code})")
            print(f"  Body: {resp.text}")
            sys.exit(1)
    except requests.ConnectionError:
        print(f"ERROR: Could not connect to {base_url}")
        sys.exit(1)
    except requests.Timeout:
        print("ERROR: Request timed out")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def _default_dashboard_port() -> int:
    """
    Resolve the default dashboard port.

    PaaS hosts (Railway, Heroku, Render, Fly, …) inject a ``PORT``
    env var the container MUST bind to — their edge proxy routes
    external traffic and the platform healthcheck to that exact
    port. Hard-coding 8080 means the healthcheck fails the moment
    the platform assigns anything else.

    Resolution order:
      * ``PORT`` env var (PaaS convention) when it parses as a
        positive integer in the legal TCP range.
      * fallback ``8080`` (the historical default, also what the
        Dockerfile EXPOSEs and the in-image HEALTHCHECK probes).

    Failure is fail-soft to the fallback. We deliberately do NOT
    raise on a malformed ``PORT`` because the alternative is a
    healthcheck-failure deploy loop with no human-readable cause.
    """
    raw = os.environ.get("PORT")
    if not raw:
        return 8080
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return 8080
    if not (1 <= port <= 65535):
        return 8080
    return port


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
        default=_default_dashboard_port(),
        help=(
            "Dashboard web UI port. "
            "Defaults to the PORT env var (PaaS convention) or "
            "8080 if PORT is unset / malformed. "
            "Pass 0 to disable the dashboard entirely."
        ),
    )
    parser.add_argument(
        "--reset-paper",
        action="store_true",
        help="Reset paper trading account to default $100K balance, then exit",
    )
    args = parser.parse_args()

    # Load .env from the working directory so the documented quickstart
    # (cp .env.example .env, fill in keys) actually provides POLYGON_API_KEY /
    # ALPACA_API_KEY / ALPACA_API_SECRET to AppConfig.from_yaml, which reads
    # them via os.getenv. Real environment variables always win.
    load_dotenv(override=False)

    # Optional Sentry error tracking — initialised BEFORE the trading
    # loop starts so circuit-breaker halts, broker failures, and tick
    # errors are captured. Strict no-op when SENTRY_DSN is unset
    # (sentry_sdk is not even imported); load_dotenv above lets a
    # .env-provided SENTRY_DSN work too.
    from trading_bot.utils.sentry import init_sentry

    init_sentry()

    # Load config
    config = AppConfig.from_yaml(args.config)
    config.run_mode = RunMode(args.mode)

    # Re-run cross-field validation after the CLI mode override. Pydantic has
    # already validated the YAML object, but assignment alone does not re-run
    # model validators; without this call `--mode live` could bypass them.
    try:
        config.validate_safety()
    except ValueError as exc:
        parser.error(str(exc))

    if args.log_level:
        config.log_level = args.log_level

    # Setup logging
    setup_logging(config.log_level, json_output=config.log_json)

    # Handle --reset-paper: reset Alpaca paper account and exit
    if args.reset_paper:
        _reset_paper_account(config)
        return

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
        start_dashboard_server(
            dashboard_state,
            port=args.dashboard_port,
            journal_path=config.journal_csv_path,
        )
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
