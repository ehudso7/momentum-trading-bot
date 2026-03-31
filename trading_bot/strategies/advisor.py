"""
AI-assisted recommendation engine for edge case management and trading decisions.

Provides intelligent recommendations for situations requiring human judgment
or automated edge case handling. This is a rule-based expert system that
evaluates multiple factors and produces structured recommendations.

It does NOT call any external AI API -- all logic is deterministic and
based on configurable heuristics derived from momentum day-trading best
practices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import structlog

from trading_bot.models.domain import (
    JournalEntry,
    PositionInfo,
    ScanResult,
    SignalType,
    TradeSignal,
)
from trading_bot.utils.helpers import now_et

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Recommendation dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EntryRecommendation:
    """Recommendation for whether to enter a trade."""

    action: str  # "enter", "skip", "reduce_size"
    confidence: float  # 0.0 - 1.0
    reasons: list[str]
    suggested_adjustments: dict


@dataclass
class ExitRecommendation:
    """Recommendation for whether to exit or hold a position."""

    action: str  # "hold", "exit_now", "tighten_stop", "scale_out"
    reasons: list[str]
    urgency: str  # "low", "medium", "high"


@dataclass
class CircuitBreakerRecommendation:
    """Recommendation for how to respond to a circuit breaker state."""

    action: str  # "continue", "reduce_risk", "stop_trading", "wait_and_resume"
    reasons: list[str]
    wait_minutes: int


@dataclass
class SizingRecommendation:
    """Recommendation for position sizing."""

    recommended_risk_pct: float
    max_shares: int
    reasons: list[str]
    warnings: list[str]


@dataclass
class DailyPlanRecommendation:
    """Start-of-day trading plan recommendation."""

    max_trades_today: int
    focus_setups: list[str]
    risk_budget_pct: float
    notes: list[str]


# ---------------------------------------------------------------------------
# TradingAdvisor
# ---------------------------------------------------------------------------


class TradingAdvisor:
    """
    Rule-based expert system that provides trading recommendations.

    Analyzes trade signals, positions, market regime, and account state
    to produce structured recommendations for entry, exit, sizing,
    circuit breaker responses, and daily planning.

    All recommendations are logged at info level via structlog.
    """

    # Thresholds for decision-making
    LOW_CONFIDENCE_THRESHOLD = 0.55
    HIGH_CONFIDENCE_THRESHOLD = 0.80
    LATE_DAY_HOUR = 14  # 2:30 PM ET boundary
    LATE_DAY_MINUTE = 30
    EXTENDED_GAP_PCT = 100.0
    SMALL_ACCOUNT_THRESHOLD = 2_000.0
    POWER_HOUR_START = 15  # 3:00 PM ET
    MAX_POSITION_HOLD_HOURS = 2
    NEGATIVE_2R = -1.5
    MAX_SCALE_OUTS_BEFORE_TRAIL_ONLY = 2
    DAILY_RISK_BUDGET_WARNING_PCT = 50.0
    VOLATILE_REGIME_RISK_REDUCTION = 0.40

    def recommend_entry(
        self,
        signal: TradeSignal,
        scan_result: ScanResult,
        regime: str,
        positions: list,
        equity: float,
    ) -> EntryRecommendation:
        """
        Analyze a trade signal and provide an entry recommendation.

        Evaluates signal quality, account state, market regime, time of day,
        gap magnitude, and catalyst presence to determine whether to enter,
        skip, or reduce position size.

        Args:
            signal: The validated trade signal from the strategy.
            scan_result: The scanner result with gap, volume, catalyst info.
            regime: Current market regime string (e.g. "trending_bearish").
            positions: List of currently open positions.
            equity: Current account equity.

        Returns:
            EntryRecommendation with action, confidence, reasons, and
            suggested adjustments.
        """
        reasons: list[str] = []
        adjustments: dict = {}
        adjusted_confidence = signal.confidence
        action = "enter"

        # --- Edge case: Low confidence signal ---
        if signal.confidence < self.LOW_CONFIDENCE_THRESHOLD:
            action = "skip"
            reasons.append(
                f"Signal confidence {signal.confidence:.2f} is below "
                f"threshold {self.LOW_CONFIDENCE_THRESHOLD}"
            )
            adjusted_confidence *= 0.5

        # --- Edge case: Too many open positions ---
        if len(positions) >= 4:
            action = "skip"
            reasons.append(
                f"Already holding {len(positions)} positions; "
                f"max concurrent positions reached"
            )

        # --- Edge case: Near daily loss limit ---
        daily_pnl = sum(
            getattr(p, "total_pnl", 0.0)
            for p in positions
            if hasattr(p, "total_pnl")
        )
        if equity > 0:
            daily_pnl_pct = (daily_pnl / equity) * 100
            if daily_pnl_pct <= -1.0:
                action = "skip"
                reasons.append(
                    f"Daily P&L at {daily_pnl_pct:.1f}% -- near loss limit, "
                    f"skip new entries"
                )
            elif daily_pnl_pct <= -0.5:
                if action != "skip":
                    action = "reduce_size"
                reasons.append(
                    f"Daily P&L at {daily_pnl_pct:.1f}% -- approaching loss limit, "
                    f"reduce size"
                )
                adjustments["risk_pct_multiplier"] = 0.5

        # --- Edge case: Bearish regime ---
        if regime == "trending_bearish":
            if action == "enter":
                action = "reduce_size"
            reasons.append(
                f"Market regime is '{regime}' -- recommend reduced position size"
            )
            adjustments["risk_pct_multiplier"] = adjustments.get(
                "risk_pct_multiplier", 1.0
            ) * 0.6
            adjusted_confidence *= 0.8

        # --- Edge case: High volatility regime (less aggressive than bearish) ---
        if regime == "high_volatility":
            if action == "enter":
                action = "reduce_size"
            reasons.append(
                "Market regime is 'high_volatility' -- recommend moderately "
                "reduced position size with wider stops"
            )
            adjustments["risk_pct_multiplier"] = adjustments.get(
                "risk_pct_multiplier", 1.0
            ) * 0.8
            adjusted_confidence *= 0.9

        # --- Edge case: Late in the day (after 2:30 PM ET) ---
        current_time = now_et()
        is_late_day = (
            current_time.hour > self.LATE_DAY_HOUR
            or (
                current_time.hour == self.LATE_DAY_HOUR
                and current_time.minute >= self.LATE_DAY_MINUTE
            )
        )
        if is_late_day:
            if signal.confidence < self.HIGH_CONFIDENCE_THRESHOLD:
                action = "skip"
                reasons.append(
                    f"After 2:30 PM ET with confidence {signal.confidence:.2f} "
                    f"< {self.HIGH_CONFIDENCE_THRESHOLD} -- too late for marginal setups"
                )
            else:
                reasons.append(
                    "After 2:30 PM ET but signal confidence is high enough to proceed"
                )

        # --- Edge case: Extended gap (>100%) ---
        if scan_result.gap_pct > self.EXTENDED_GAP_PCT:
            if action == "enter":
                action = "reduce_size"
            reasons.append(
                f"Extended gap of {scan_result.gap_pct:.1f}% (>{self.EXTENDED_GAP_PCT}%) "
                f"-- elevated reversal risk, recommend caution"
            )
            adjustments["risk_pct_multiplier"] = adjustments.get(
                "risk_pct_multiplier", 1.0
            ) * 0.5
            adjusted_confidence *= 0.7

        # --- Edge case: No catalyst ---
        if not scan_result.catalyst:
            reasons.append("No catalyst identified -- reduced conviction")
            adjusted_confidence *= 0.85

        # --- Edge case: First trade of the day (looser criteria) ---
        if len(positions) == 0 and action == "enter":
            reasons.append(
                "First trade of the day -- applying standard entry criteria"
            )

        # --- Positive factors ---
        if signal.reward_risk_ratio >= 2.0:
            reasons.append(
                f"Favorable R:R of {signal.reward_risk_ratio:.1f}:1"
            )
            adjusted_confidence = min(adjusted_confidence * 1.1, 1.0)

        if scan_result.relative_volume >= 10.0:
            reasons.append(
                f"Strong relative volume ({scan_result.relative_volume:.1f}x)"
            )
            adjusted_confidence = min(adjusted_confidence * 1.05, 1.0)

        adjusted_confidence = max(0.0, min(1.0, adjusted_confidence))

        recommendation = EntryRecommendation(
            action=action,
            confidence=round(adjusted_confidence, 4),
            reasons=reasons,
            suggested_adjustments=adjustments,
        )

        log.info(
            "advisor.entry_recommendation",
            symbol=signal.symbol,
            action=recommendation.action,
            confidence=recommendation.confidence,
            reasons=recommendation.reasons,
            adjustments=recommendation.suggested_adjustments,
        )

        return recommendation

    def recommend_exit(
        self,
        position: PositionInfo,
        bars,
        market_regime: str,
    ) -> ExitRecommendation:
        """
        Provide advice on whether to exit or hold a position.

        Evaluates P&L in R-multiples, hold duration, market regime changes,
        scale-out history, and volume trends to recommend an exit action.

        Args:
            position: The current open position.
            bars: Recent OHLCV bar data (pandas DataFrame or similar).
                  Used to assess volume trends.
            market_regime: Current market regime string.

        Returns:
            ExitRecommendation with action, reasons, and urgency level.
        """
        reasons: list[str] = []
        action = "hold"
        urgency = "low"

        r_multiple = position.r_multiple

        # --- Edge case: P&L at negative 2R ---
        if r_multiple <= self.NEGATIVE_2R:
            action = "exit_now"
            urgency = "high"
            reasons.append(
                f"Position at {r_multiple:.1f}R -- exceeds -2R threshold, "
                f"strongly recommend immediate exit"
            )

        # --- Edge case: Held > 3 hours with no profit ---
        current_time = now_et()
        entry_time = position.entry_time
        # Handle timezone-naive entry_time by comparing raw deltas
        if entry_time.tzinfo is not None:
            hold_duration = current_time - entry_time
        else:
            hold_duration = current_time.replace(tzinfo=None) - entry_time

        hold_hours = hold_duration.total_seconds() / 3600

        if hold_hours > self.MAX_POSITION_HOLD_HOURS and r_multiple <= 0.0:
            if action != "exit_now":
                action = "exit_now"
                urgency = "high" if r_multiple < 0.0 else "medium"
            reasons.append(
                f"Position held for {hold_hours:.1f} hours with "
                f"{r_multiple:.1f}R -- no profit, time decay risk"
            )

        # --- Edge case: Market regime changed to bearish ---
        if market_regime == "trending_bearish":
            if action == "hold":
                action = "tighten_stop"
                urgency = "medium"
            reasons.append(
                f"Market regime is '{market_regime}' -- recommend tighter stops"
            )
        elif market_regime == "high_volatility":
            reasons.append(
                "Market regime is 'high_volatility' -- wider stops already applied via regime adjustments"
            )

        # --- Edge case: Already scaled out 2x ---
        if position.scale_outs_completed >= self.MAX_SCALE_OUTS_BEFORE_TRAIL_ONLY:
            if action == "hold":
                action = "hold"  # trailing only, no further action needed
            reasons.append(
                f"Already completed {position.scale_outs_completed} scale-outs "
                f"-- trail remainder only"
            )

        # --- Edge case: Volume drying up ---
        if bars is not None and hasattr(bars, "empty") and not bars.empty:
            try:
                if len(bars) >= 10:
                    recent_vol = bars.iloc[-3:]["volume"].mean()
                    earlier_vol = bars.iloc[-10:-3]["volume"].mean()
                    if earlier_vol > 0 and recent_vol < earlier_vol * 0.4:
                        if action in ("hold", "tighten_stop"):
                            action = "exit_now"
                            urgency = "medium"
                        reasons.append(
                            f"Volume drying up -- recent avg "
                            f"{recent_vol:.0f} vs earlier {earlier_vol:.0f} "
                            f"({recent_vol / earlier_vol * 100:.0f}% of prior)"
                        )
            except (KeyError, IndexError, TypeError):
                pass  # Bars data may not have expected shape

        # --- Positive hold signals ---
        if r_multiple >= 1.0 and action == "hold":
            reasons.append(
                f"Position at +{r_multiple:.1f}R -- in profit, continue managing"
            )

        if not reasons:
            reasons.append("No edge case triggers -- continue holding")

        recommendation = ExitRecommendation(
            action=action,
            reasons=reasons,
            urgency=urgency,
        )

        log.info(
            "advisor.exit_recommendation",
            symbol=position.symbol,
            action=recommendation.action,
            urgency=recommendation.urgency,
            r_multiple=round(r_multiple, 2),
            hold_hours=round(hold_hours, 1),
            reasons=recommendation.reasons,
        )

        return recommendation

    def recommend_circuit_breaker_action(
        self,
        status: dict,
        daily_trades: list,
    ) -> CircuitBreakerRecommendation:
        """
        Recommend action when the circuit breaker is in WARNING or HALTED state.

        Analyzes the circuit breaker status, drawdown level, consecutive loss
        patterns, and API error state to recommend whether to continue, reduce
        risk, stop trading, or wait for resumption.

        Args:
            status: Dictionary from CircuitBreaker.get_status() containing
                    state, daily_pnl, consecutive_losses, api_errors_5min,
                    drawdown_pct, cooldown_minutes, etc.
            daily_trades: List of JournalEntry or similar trade records for
                          the current day.

        Returns:
            CircuitBreakerRecommendation with action, reasons, and
            wait_minutes.
        """
        reasons: list[str] = []
        action = "continue"
        wait_minutes = 0

        cb_state = status.get("state", "normal")
        drawdown_pct = status.get("drawdown_pct", 0.0)
        consecutive_losses = status.get("consecutive_losses", 0)
        api_errors = status.get("api_errors_5min", 0)
        cooldown_minutes = status.get("cooldown_minutes", 30)

        # --- HALTED state ---
        if cb_state == "halted":
            action = "stop_trading"
            wait_minutes = cooldown_minutes
            reasons.append(
                f"Circuit breaker is HALTED -- stop all trading and wait "
                f"{cooldown_minutes} minutes for cooldown"
            )

        # --- COOLDOWN state (after halted period) ---
        elif cb_state == "cooldown":
            action = "wait_and_resume"
            wait_minutes = 5
            reasons.append(
                "Circuit breaker in COOLDOWN -- wait for transition to WARNING "
                "then resume with reduced size"
            )

        # --- WARNING state ---
        elif cb_state == "warning":
            action = "reduce_risk"
            wait_minutes = 0
            reasons.append(
                "Circuit breaker in WARNING -- reduce position sizes by 50% "
                "and limit to highest-confidence setups only"
            )

        # --- Approaching drawdown limit ---
        drawdown_limit = status.get(
            "drawdown_circuit_breaker_pct",
            5.0,
        )
        if drawdown_pct >= drawdown_limit * 0.75 and action == "continue":
            action = "reduce_risk"
            reasons.append(
                f"Drawdown at {drawdown_pct:.1f}% is approaching limit of "
                f"{drawdown_limit:.1f}% -- reduce position sizes"
            )

        # --- 3 consecutive losses that are all stop-outs ---
        if consecutive_losses >= 3:
            stop_out_count = 0
            recent_trades = daily_trades[-3:] if len(daily_trades) >= 3 else daily_trades
            for trade in recent_trades:
                exit_reason = ""
                if isinstance(trade, JournalEntry):
                    exit_reason = trade.exit_reason
                elif isinstance(trade, dict):
                    exit_reason = trade.get("exit_reason", "")
                if "stop" in exit_reason.lower():
                    stop_out_count += 1

            if stop_out_count >= 3:
                action = "stop_trading"
                wait_minutes = max(wait_minutes, 30)
                reasons.append(
                    f"{consecutive_losses} consecutive losses, last 3 are all "
                    f"stop-outs -- possible strategy-market mismatch, "
                    f"recommend pausing for {wait_minutes} minutes"
                )
            elif consecutive_losses >= 3:
                if action not in ("stop_trading", "wait_and_resume"):
                    action = "reduce_risk"
                reasons.append(
                    f"{consecutive_losses} consecutive losses -- recommend "
                    f"reducing risk and reviewing setups"
                )

        # --- API errors ---
        if api_errors >= 3:
            if action not in ("stop_trading",):
                action = "wait_and_resume"
                wait_minutes = max(wait_minutes, 10)
            reasons.append(
                f"{api_errors} API errors in last 5 minutes -- "
                f"recommend waiting {wait_minutes} minutes for stability"
            )

        # --- After cooldown: cautious re-entry ---
        if cb_state == "warning" and status.get("cooldown_entered_at") is None:
            # Already transitioned from cooldown to warning
            halted_at = status.get("halted_at")
            if halted_at is not None:
                reasons.append(
                    "Recently recovered from halt -- recommend cautious "
                    "re-entry with 50% reduced position sizes"
                )

        if not reasons:
            reasons.append("No circuit breaker concerns -- continue normally")

        recommendation = CircuitBreakerRecommendation(
            action=action,
            reasons=reasons,
            wait_minutes=wait_minutes,
        )

        log.info(
            "advisor.circuit_breaker_recommendation",
            state=cb_state,
            action=recommendation.action,
            wait_minutes=recommendation.wait_minutes,
            drawdown_pct=drawdown_pct,
            consecutive_losses=consecutive_losses,
            reasons=recommendation.reasons,
        )

        return recommendation

    def recommend_position_sizing(
        self,
        signal: TradeSignal,
        equity: float,
        current_positions: list,
        regime: str,
    ) -> SizingRecommendation:
        """
        Provide position sizing advice considering all factors.

        Evaluates account size, market regime, daily risk usage, time of
        day, and signal properties to recommend a risk percentage and
        maximum share count.

        Args:
            signal: The trade signal with entry/stop prices.
            equity: Current account equity.
            current_positions: List of open positions.
            regime: Current market regime string.

        Returns:
            SizingRecommendation with recommended risk percentage, max shares,
            reasons, and warnings.
        """
        reasons: list[str] = []
        warnings: list[str] = []
        base_risk_pct = 1.0  # Default 1% risk per trade

        # --- Edge case: Small account (<$2k) ---
        if equity < self.SMALL_ACCOUNT_THRESHOLD:
            base_risk_pct = min(base_risk_pct, 0.5)
            reasons.append(
                f"Small account (${equity:,.0f} < "
                f"${self.SMALL_ACCOUNT_THRESHOLD:,.0f}) "
                f"-- capping risk at 0.5%"
            )
            warnings.append(
                "Small account size limits position sizing significantly"
            )

        # --- Edge case: Volatile regime ---
        volatile_regimes = {"high_volatility", "trending_bearish"}
        if regime in volatile_regimes:
            reduction = self.VOLATILE_REGIME_RISK_REDUCTION
            base_risk_pct *= (1.0 - reduction)
            reasons.append(
                f"Volatile regime '{regime}' -- reducing risk by "
                f"{reduction * 100:.0f}% to {base_risk_pct:.2f}%"
            )

        # --- Edge case: Daily risk budget usage ---
        total_risk_in_use = 0.0
        for pos in current_positions:
            if hasattr(pos, "entry_price") and hasattr(pos, "stop_price"):
                risk_per_share = abs(pos.entry_price - pos.stop_price)
                shares = getattr(pos, "shares_remaining", 0)
                total_risk_in_use += risk_per_share * shares

        if equity > 0:
            risk_budget_used_pct = (total_risk_in_use / equity) * 100
            if risk_budget_used_pct >= self.DAILY_RISK_BUDGET_WARNING_PCT:
                base_risk_pct *= 0.5
                reasons.append(
                    f"Already using {risk_budget_used_pct:.1f}% of equity in "
                    f"risk (>= {self.DAILY_RISK_BUDGET_WARNING_PCT}%) "
                    f"-- halving risk per trade"
                )
                warnings.append(
                    "Significant portion of daily risk budget consumed"
                )

        # --- Time of day adjustments ---
        current_time = now_et()
        current_hour = current_time.hour
        current_minute = current_time.minute

        # First hour of trading (9:30 - 10:30 AM)
        is_first_hour = (
            (current_hour == 9 and current_minute >= 30)
            or current_hour == 10
        )
        if is_first_hour:
            reasons.append("First hour of trading -- normal sizing applies")

        # Power hour (3:00 - 4:00 PM)
        if current_hour >= self.POWER_HOUR_START:
            base_risk_pct *= 0.7
            reasons.append(
                f"Power hour ({current_hour}:{current_minute:02d} ET) -- "
                f"reducing size by 30% due to end-of-day volatility"
            )

        # --- Compute max shares ---
        risk_dollars = equity * (base_risk_pct / 100.0)
        stop_distance = signal.risk_per_share
        if stop_distance > 0:
            max_shares = int(risk_dollars / stop_distance)
        else:
            max_shares = 0
            warnings.append(
                "Stop distance is zero -- cannot compute position size"
            )

        # Ensure risk_pct stays within bounds
        base_risk_pct = max(0.1, min(base_risk_pct, 3.0))

        recommendation = SizingRecommendation(
            recommended_risk_pct=round(base_risk_pct, 4),
            max_shares=max_shares,
            reasons=reasons,
            warnings=warnings,
        )

        log.info(
            "advisor.sizing_recommendation",
            symbol=signal.symbol,
            recommended_risk_pct=recommendation.recommended_risk_pct,
            max_shares=recommendation.max_shares,
            equity=round(equity, 2),
            regime=regime,
            reasons=recommendation.reasons,
            warnings=recommendation.warnings,
        )

        return recommendation

    def recommend_daily_plan(
        self,
        equity: float,
        regime: str,
        recent_journal_entries: list,
    ) -> DailyPlanRecommendation:
        """
        Generate start-of-day trading plan recommendations.

        Analyzes recent performance, market regime, account size, and day
        of week to recommend trade count limits, focus setups, risk budget,
        and qualitative notes.

        Args:
            equity: Current account equity.
            regime: Current market regime string.
            recent_journal_entries: Last N journal entries (JournalEntry or
                                   dicts) for recent performance analysis.

        Returns:
            DailyPlanRecommendation with max trades, focus setups,
            risk budget, and notes.
        """
        notes: list[str] = []
        max_trades = 6
        risk_budget_pct = 3.0
        focus_setups: list[str] = [
            SignalType.VWAP_PULLBACK.value,
            SignalType.EMA_PULLBACK.value,
            SignalType.BREAKOUT_CONTINUATION.value,
        ]

        # --- Recent performance (last 5 days / entries) ---
        recent_entries = (
            recent_journal_entries[-5:] if recent_journal_entries else []
        )
        if recent_entries:
            wins = 0
            total = len(recent_entries)
            for entry in recent_entries:
                pnl = 0.0
                if isinstance(entry, JournalEntry):
                    pnl = entry.pnl
                elif isinstance(entry, dict):
                    pnl = entry.get("pnl", 0.0)
                if pnl > 0:
                    wins += 1

            win_rate = wins / total if total > 0 else 0.0

            if win_rate < 0.3:
                max_trades = 3
                risk_budget_pct = 1.5
                notes.append(
                    f"Recent win rate is low ({win_rate:.0%} over last "
                    f"{total} trades) -- reduce trade count and risk budget"
                )
                focus_setups = [SignalType.VWAP_PULLBACK.value]
                notes.append(
                    "Focus exclusively on VWAP pullback setups until "
                    "performance improves"
                )
            elif win_rate < 0.5:
                max_trades = 4
                risk_budget_pct = 2.0
                notes.append(
                    f"Recent win rate is below average ({win_rate:.0%}) -- "
                    f"trading with reduced risk budget"
                )
            else:
                notes.append(
                    f"Recent win rate is solid ({win_rate:.0%}) -- "
                    f"standard plan applies"
                )
        else:
            notes.append(
                "No recent trade history -- starting with default plan"
            )

        # --- Market regime adjustments ---
        if regime == "trending_bullish":
            notes.append(
                "Bullish trend detected -- full setup roster available"
            )
        elif regime == "trending_bearish":
            max_trades = min(max_trades, 3)
            risk_budget_pct = min(risk_budget_pct, 1.5)
            focus_setups = [SignalType.VWAP_PULLBACK.value]
            notes.append(
                "Bearish regime -- limit to VWAP pullbacks with strong "
                "catalysts only"
            )
        elif regime == "high_volatility":
            max_trades = min(max_trades, 3)
            risk_budget_pct = min(risk_budget_pct, 2.0)
            notes.append(
                "High volatility regime -- reduce trade count, widen stops"
            )
        elif regime == "range_bound":
            max_trades = min(max_trades, 4)
            focus_setups = [
                SignalType.VWAP_PULLBACK.value,
                SignalType.EMA_PULLBACK.value,
            ]
            notes.append(
                "Range-bound market -- skip breakout setups, focus on "
                "pullbacks"
            )
        elif regime == "low_volatility":
            notes.append(
                "Low volatility -- require extra volume confirmation "
                "before entries"
            )

        # --- Account size adjustments ---
        if equity < self.SMALL_ACCOUNT_THRESHOLD:
            max_trades = min(max_trades, 2)
            risk_budget_pct = min(risk_budget_pct, 1.0)
            notes.append(
                f"Small account (${equity:,.0f}) -- strict trade limits "
                f"and reduced risk budget to preserve capital"
            )
        elif equity < 5_000:
            max_trades = min(max_trades, 3)
            risk_budget_pct = min(risk_budget_pct, 2.0)
            notes.append(
                f"Growing account (${equity:,.0f}) -- moderate trade limits"
            )
        elif equity >= 25_000:
            notes.append(
                f"Account above PDT threshold (${equity:,.0f}) -- "
                f"no day trade restrictions"
            )

        # --- Day of week adjustments ---
        current_time = now_et()
        day_of_week = current_time.weekday()  # 0=Monday, 4=Friday

        if day_of_week == 0:  # Monday
            if max_trades > 2:
                max_trades -= 1
            notes.append(
                "Monday -- more cautious start, wait for market direction "
                "to establish before aggressive entries"
            )
        elif day_of_week == 4:  # Friday
            if max_trades > 2:
                max_trades -= 1
            risk_budget_pct *= 0.8
            notes.append(
                "Friday -- reduce exposure ahead of weekend, "
                "tighter risk management"
            )

        # Ensure risk budget stays within bounds
        risk_budget_pct = max(0.5, min(risk_budget_pct, 5.0))

        recommendation = DailyPlanRecommendation(
            max_trades_today=max_trades,
            focus_setups=focus_setups,
            risk_budget_pct=round(risk_budget_pct, 2),
            notes=notes,
        )

        log.info(
            "advisor.daily_plan",
            equity=round(equity, 2),
            regime=regime,
            max_trades=recommendation.max_trades_today,
            focus_setups=recommendation.focus_setups,
            risk_budget_pct=recommendation.risk_budget_pct,
            notes=recommendation.notes,
        )

        return recommendation
