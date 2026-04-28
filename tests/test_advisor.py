"""
Tests for the TradingAdvisor rule-based recommendation engine.

Covers entry recommendations, exit recommendations, circuit breaker
responses, position sizing advice, and daily plan generation.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytz
import pytest

from trading_bot.models.domain import (
    JournalEntry,
    OrderSide,
    PositionInfo,
    PositionStatus,
    ScanResult,
    SignalType,
    TradeSignal,
)
from trading_bot.strategies.advisor import TradingAdvisor

ET = pytz.timezone("US/Eastern")

# ---------------------------------------------------------------------------
# Helpers to build domain objects for tests
# ---------------------------------------------------------------------------


def _make_signal(
    confidence: float = 0.80,
    entry_price: float = 10.00,
    stop_price: float = 9.50,
    target_prices: list[float] | None = None,
) -> TradeSignal:
    return TradeSignal(
        symbol="TEST",
        signal_type=SignalType.VWAP_PULLBACK,
        entry_price=entry_price,
        stop_price=stop_price,
        target_prices=target_prices or [10.50, 11.00],
        atr=0.40,
        vwap=9.90,
        ema9=9.85,
        confidence=confidence,
    )


def _make_scan(
    gap_pct: float = 35.0,
    relative_volume: float = 12.5,
    catalyst: str | None = "FDA",
) -> ScanResult:
    return ScanResult(
        symbol="TEST",
        price=8.50,
        gap_pct=gap_pct,
        relative_volume=relative_volume,
        float_shares=5_000_000,
        volume=5_000_000,
        prev_close=6.30,
        catalyst=catalyst,
        score=0.85,
    )


def _make_position(
    entry_price: float = 10.00,
    current_price: float = 10.25,
    stop_price: float = 9.50,
    shares: int = 100,
    shares_remaining: int = 100,
    scale_outs: int = 0,
    entry_time: datetime | None = None,
    pnl_unrealized: float = 25.0,
    pnl_realized: float = 0.0,
    total_pnl_override: float | None = None,
) -> PositionInfo:
    return PositionInfo(
        symbol="TEST",
        side=OrderSide.BUY,
        entry_price=entry_price,
        current_price=current_price,
        shares=shares,
        shares_remaining=shares_remaining,
        stop_price=stop_price,
        target_prices=[10.50, 11.00],
        status=PositionStatus.OPEN,
        pnl_unrealized=pnl_unrealized,
        pnl_realized=pnl_realized,
        scale_outs_completed=scale_outs,
        entry_time=entry_time or datetime(2024, 6, 15, 10, 30),
        signal_type=SignalType.VWAP_PULLBACK,
    )


def _make_journal_entry(
    pnl: float = 50.0,
    exit_reason: str = "target",
) -> JournalEntry:
    return JournalEntry(
        date="2024-06-15",
        symbol="TEST",
        side="buy",
        signal_type="vwap_pullback",
        entry_price=10.00,
        exit_price=10.50,
        shares=100,
        pnl=pnl,
        rr_ratio=1.0,
        hold_time_minutes=45.0,
        entry_time="10:30",
        exit_time="11:15",
        exit_reason=exit_reason,
    )


def _morning_time() -> datetime:
    """10:00 AM ET on a Wednesday -- normal morning trading hours."""
    return ET.localize(datetime(2024, 6, 12, 10, 0, 0))


def _late_day_time() -> datetime:
    """3:00 PM ET -- after the 2:30 PM ET late-day boundary."""
    return ET.localize(datetime(2024, 6, 12, 15, 0, 0))


def _power_hour_time() -> datetime:
    """3:30 PM ET -- power hour."""
    return ET.localize(datetime(2024, 6, 12, 15, 30, 0))


def _monday_morning() -> datetime:
    """10:00 AM ET on a Monday."""
    return ET.localize(datetime(2024, 6, 10, 10, 0, 0))


def _friday_morning() -> datetime:
    """10:00 AM ET on a Friday."""
    return ET.localize(datetime(2024, 6, 14, 10, 0, 0))


def _wednesday_morning() -> datetime:
    """10:00 AM ET on a Wednesday (mid-week)."""
    return ET.localize(datetime(2024, 6, 12, 10, 0, 0))


def _first_hour_time() -> datetime:
    """9:45 AM ET -- first hour of trading."""
    return ET.localize(datetime(2024, 6, 12, 9, 45, 0))


# ---------------------------------------------------------------------------
# Test recommend_entry()
# ---------------------------------------------------------------------------


class TestRecommendEntry:
    """Tests for TradingAdvisor.recommend_entry()."""

    def setup_method(self):
        self.advisor = TradingAdvisor()

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_high_confidence_signal_enters(self, mock_now):
        """High-confidence signal with no edge cases => action 'enter'."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "enter"
        assert rec.confidence > 0.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_low_confidence_signal_skipped(self, mock_now):
        """Confidence below 0.5 threshold => action 'skip'."""
        signal = _make_signal(confidence=0.3)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "skip"
        assert any("below threshold" in r for r in rec.reasons)
        # Confidence should be halved from the low value
        assert rec.confidence < 0.3

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_too_many_open_positions(self, mock_now):
        """4 or more open positions => action 'skip'."""
        positions = [_make_position() for _ in range(4)]
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=positions,
            equity=25_000.0,
        )
        assert rec.action == "skip"
        assert any("max concurrent positions" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_bearish_regime_reduces_size(self, mock_now):
        """Bearish regime => action 'reduce_size' with risk multiplier."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bearish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "reduce_size"
        assert any("trending_bearish" in r for r in rec.reasons)
        assert "risk_pct_multiplier" in rec.suggested_adjustments
        assert rec.suggested_adjustments["risk_pct_multiplier"] < 1.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_high_volatility_regime_reduces_size(self, mock_now):
        """high_volatility regime => same reduce logic as bearish."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="high_volatility",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "reduce_size"
        assert any("high_volatility" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_late_day_time())
    def test_late_day_low_confidence_skipped(self, mock_now):
        """After 2:30 PM with confidence < 0.75 => skip."""
        signal = _make_signal(confidence=0.60)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "skip"
        assert any("2:30 PM" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_late_day_time())
    def test_late_day_high_confidence_proceeds(self, mock_now):
        """After 2:30 PM but high confidence => still allowed to enter."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "enter"
        assert any("high enough to proceed" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_extended_gap_reduces_size(self, mock_now):
        """Gap >100% => reduce size due to reversal risk."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan(gap_pct=150.0)
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "reduce_size"
        assert any("Extended gap" in r for r in rec.reasons)
        assert rec.suggested_adjustments.get("risk_pct_multiplier", 1.0) < 1.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_no_catalyst_reduces_confidence(self, mock_now):
        """No catalyst => reduced conviction (confidence adjusted down)."""
        signal = _make_signal(confidence=0.80)
        scan_with = _make_scan(catalyst="FDA")
        scan_without = _make_scan(catalyst=None)

        rec_with = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan_with,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        # Need a fresh signal for fair comparison (confidence gets mutated via adjustment)
        signal2 = _make_signal(confidence=0.80)
        rec_without = self.advisor.recommend_entry(
            signal=signal2,
            scan_result=scan_without,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )

        assert rec_without.confidence < rec_with.confidence
        assert any("No catalyst" in r for r in rec_without.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_positive_rr_ratio_boosts_confidence(self, mock_now):
        """R:R >= 2:1 => confidence boosted."""
        # entry=10, stop=9.50, target=11 => R:R = 2.0
        signal = _make_signal(
            confidence=0.70,
            entry_price=10.0,
            stop_price=9.50,
            target_prices=[11.0, 12.0],
        )
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert any("R:R" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_strong_relative_volume_boosts_confidence(self, mock_now):
        """Relative volume >= 10 => confidence boosted."""
        signal = _make_signal(confidence=0.70)
        scan = _make_scan(relative_volume=15.0)
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert any("relative volume" in r.lower() for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_first_trade_of_day_note(self, mock_now):
        """Zero open positions + entering => 'first trade' note."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert any("First trade" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_daily_pnl_near_loss_limit_skips(self, mock_now):
        """Daily P&L at -4% or worse => skip."""
        # total_pnl = pnl_unrealized + pnl_realized = -1000 + 0 = -1000
        # -1000 / 25000 * 100 = -4.0%
        bad_pos = _make_position(pnl_unrealized=-1000.0, pnl_realized=0.0)
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[bad_pos],
            equity=25_000.0,
        )
        assert rec.action == "skip"
        assert any("loss limit" in r.lower() for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_daily_pnl_approaching_limit_reduces_size(self, mock_now):
        """Daily P&L between -0.5% and -1.0% => reduce_size."""
        # -175 / 25000 * 100 = -0.7%
        bad_pos = _make_position(pnl_unrealized=-175.0, pnl_realized=0.0)
        signal = _make_signal(confidence=0.85)
        scan = _make_scan()
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bullish",
            positions=[bad_pos],
            equity=25_000.0,
        )
        assert rec.action == "reduce_size"
        assert any("approaching loss limit" in r.lower() for r in rec.reasons)
        assert rec.suggested_adjustments.get("risk_pct_multiplier") == 0.5

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_confidence_clamped_between_0_and_1(self, mock_now):
        """Confidence is always bounded [0.0, 1.0] regardless of adjustments."""
        # High R:R and rvol should both boost, but confidence shouldn't exceed 1.0
        scan = _make_scan(relative_volume=20.0)
        signal_high_rr = _make_signal(
            confidence=0.99,
            entry_price=10.0,
            stop_price=9.50,
            target_prices=[11.0, 12.0],
        )
        rec = self.advisor.recommend_entry(
            signal=signal_high_rr,
            scan_result=scan,
            regime="trending_bullish",
            positions=[],
            equity=25_000.0,
        )
        assert 0.0 <= rec.confidence <= 1.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_combined_bearish_and_extended_gap(self, mock_now):
        """Bearish regime + extended gap => risk multiplier is compounded."""
        signal = _make_signal(confidence=0.85)
        scan = _make_scan(gap_pct=120.0)
        rec = self.advisor.recommend_entry(
            signal=signal,
            scan_result=scan,
            regime="trending_bearish",
            positions=[],
            equity=25_000.0,
        )
        assert rec.action == "reduce_size"
        multiplier = rec.suggested_adjustments.get("risk_pct_multiplier", 1.0)
        # 0.6 * 0.5 = 0.3
        assert multiplier == pytest.approx(0.3, abs=0.01)


# ---------------------------------------------------------------------------
# Test recommend_exit()
# ---------------------------------------------------------------------------


class TestRecommendExit:
    """Tests for TradingAdvisor.recommend_exit()."""

    def setup_method(self):
        self.advisor = TradingAdvisor()

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_negative_2r_exits_immediately(self, mock_now):
        """Position at -2R or worse => exit_now with high urgency."""
        # entry=10, stop=9.50 => risk_per_share=0.50
        # current=9.0 => pnl_per_share = -1.0 => r_multiple = -2.0
        pos = _make_position(entry_price=10.0, stop_price=9.50, current_price=9.0)
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert rec.action == "exit_now"
        assert rec.urgency == "high"
        assert any("-2R" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_worse_than_negative_2r(self, mock_now):
        """Position at -3R => still exit_now with high urgency."""
        pos = _make_position(entry_price=10.0, stop_price=9.50, current_price=8.50)
        assert pos.r_multiple == pytest.approx(-3.0)
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert rec.action == "exit_now"
        assert rec.urgency == "high"

    @patch("trading_bot.strategies.advisor.now_et")
    def test_long_hold_no_profit_exits(self, mock_now):
        """Position held >3 hours with 0 or negative R => exit_now."""
        entry_time = datetime(2024, 6, 15, 10, 0)
        # 4 hours after entry
        mock_now.return_value = ET.localize(datetime(2024, 6, 15, 14, 0))
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=9.80,  # r_multiple = -0.4
            entry_time=entry_time,
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert rec.action == "exit_now"
        assert any("hours" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et")
    def test_long_hold_with_profit_holds(self, mock_now):
        """Position held >3 hours but in profit (r_multiple > 0) => hold."""
        entry_time = datetime(2024, 6, 15, 10, 0)
        mock_now.return_value = ET.localize(datetime(2024, 6, 15, 14, 0))
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.50,  # r_multiple = +1.0
            entry_time=entry_time,
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        # Should not be exit_now due to hold time (r_multiple > 0)
        # And should report positive hold signal
        assert any("+1.0R" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_bearish_regime_tightens_stop(self, mock_now):
        """Bearish regime with neutral position => tighten_stop."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.10,  # slightly profitable
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bearish")
        assert rec.action == "tighten_stop"
        assert rec.urgency == "medium"
        assert any("trending_bearish" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_high_volatility_regime_holds_with_note(self, mock_now):
        """high_volatility regime => hold with wider stops note (not tighten)."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.10,
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="high_volatility")
        assert rec.action == "hold"
        assert any("high_volatility" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_volume_drying_up_triggers_exit(self, mock_now):
        """Recent volume < 40% of earlier volume => exit_now."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.10,
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        # Create bars with declining volume
        n = 15
        bars = pd.DataFrame({
            "open": [10.0] * n,
            "high": [10.2] * n,
            "low": [9.9] * n,
            "close": [10.1] * n,
            "volume": [500_000] * 7 + [500_000] * 5 + [50_000] * 3,
            #         earlier (7 bars)    mid (5 bars)    recent (3 bars)
            # bars[-10:-3] = indices 5..11 => mix of 500k
            # bars[-3:]    = indices 12..14 => 50k
            # recent_avg = 50k, earlier_avg ~= 500k
            # 50k / 500k = 10% < 40% threshold
        })
        rec = self.advisor.recommend_exit(pos, bars=bars, market_regime="trending_bullish")
        assert rec.action == "exit_now"
        assert rec.urgency == "medium"
        assert any("Volume drying up" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_volume_healthy_no_exit(self, mock_now):
        """Healthy volume => no volume-based exit trigger."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.10,
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        n = 15
        bars = pd.DataFrame({
            "open": [10.0] * n,
            "high": [10.2] * n,
            "low": [9.9] * n,
            "close": [10.1] * n,
            "volume": [500_000] * n,  # steady volume
        })
        rec = self.advisor.recommend_exit(pos, bars=bars, market_regime="trending_bullish")
        assert not any("Volume drying up" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_positive_r_multiple_hold_signal(self, mock_now):
        """Position at +1R with bullish regime => hold with positive note."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.50,  # +1.0R
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert rec.action == "hold"
        assert any("in profit" in r.lower() for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_scale_outs_completed_trail_only(self, mock_now):
        """2 or more scale-outs completed => trail remainder only."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.30,
            scale_outs=2,
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert any("trail remainder" in r.lower() for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_no_edge_cases_default_hold(self, mock_now):
        """No triggers at all => default hold with standard message."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.10,  # +0.2R -- slightly positive but below +1R
            scale_outs=0,
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert rec.action == "hold"
        assert rec.urgency == "low"

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_bars_none_handled_gracefully(self, mock_now):
        """Passing None for bars does not crash."""
        pos = _make_position(entry_time=datetime(2024, 6, 12, 9, 45))
        rec = self.advisor.recommend_exit(pos, bars=None, market_regime="trending_bullish")
        assert rec.action in ("hold", "tighten_stop", "exit_now", "scale_out")

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_bars_too_short_for_volume_check(self, mock_now):
        """Fewer than 10 bars => volume check skipped gracefully."""
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.50,
            current_price=10.10,
            entry_time=datetime(2024, 6, 12, 9, 45),
        )
        bars = pd.DataFrame({
            "open": [10.0] * 5,
            "high": [10.2] * 5,
            "low": [9.9] * 5,
            "close": [10.1] * 5,
            "volume": [1000] * 5,  # Wouldn't matter -- skipped
        })
        rec = self.advisor.recommend_exit(pos, bars=bars, market_regime="trending_bullish")
        # Should not crash and should not mention volume
        assert not any("Volume drying up" in r for r in rec.reasons)


# ---------------------------------------------------------------------------
# Test recommend_circuit_breaker_action()
# ---------------------------------------------------------------------------


class TestRecommendCircuitBreakerAction:
    """Tests for TradingAdvisor.recommend_circuit_breaker_action()."""

    def setup_method(self):
        self.advisor = TradingAdvisor()

    def test_halted_state_stops_trading(self):
        """HALTED state => stop_trading with cooldown wait."""
        status = {
            "state": "halted",
            "drawdown_pct": 6.0,
            "consecutive_losses": 2,
            "api_errors_5min": 0,
            "cooldown_minutes": 45,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "stop_trading"
        assert rec.wait_minutes == 45
        assert any("HALTED" in r for r in rec.reasons)

    def test_cooldown_state_waits_and_resumes(self):
        """COOLDOWN state => wait_and_resume with 5 min wait."""
        status = {
            "state": "cooldown",
            "drawdown_pct": 3.0,
            "consecutive_losses": 1,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "wait_and_resume"
        assert rec.wait_minutes == 5
        assert any("COOLDOWN" in r for r in rec.reasons)

    def test_warning_state_reduces_risk(self):
        """WARNING state => reduce_risk."""
        status = {
            "state": "warning",
            "drawdown_pct": 3.5,
            "consecutive_losses": 1,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "reduce_risk"
        assert rec.wait_minutes == 0
        assert any("WARNING" in r for r in rec.reasons)

    def test_normal_state_continues(self):
        """Normal state with no issues => continue."""
        status = {
            "state": "normal",
            "drawdown_pct": 1.0,
            "consecutive_losses": 0,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "continue"
        assert rec.wait_minutes == 0
        assert any("No circuit breaker concerns" in r for r in rec.reasons)

    def test_approaching_drawdown_limit(self):
        """Drawdown at >= 75% of limit in normal state => reduce_risk."""
        status = {
            "state": "normal",
            "drawdown_pct": 4.0,  # 80% of default 5.0% limit
            "drawdown_circuit_breaker_pct": 5.0,
            "consecutive_losses": 0,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "reduce_risk"
        assert any("approaching limit" in r.lower() for r in rec.reasons)

    def test_consecutive_losses_all_stopouts_stops_trading(self):
        """3+ consecutive losses where last 3 are all stop-outs => stop_trading."""
        status = {
            "state": "normal",
            "drawdown_pct": 2.0,
            "consecutive_losses": 3,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        trades = [
            _make_journal_entry(pnl=-50.0, exit_reason="stop_loss"),
            _make_journal_entry(pnl=-45.0, exit_reason="trailing_stop"),
            _make_journal_entry(pnl=-60.0, exit_reason="hard_stop"),
        ]
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=trades)
        assert rec.action == "stop_trading"
        assert rec.wait_minutes >= 30
        assert any("stop-outs" in r for r in rec.reasons)

    def test_consecutive_losses_mixed_exits_reduces_risk(self):
        """3+ consecutive losses but not all stop-outs => reduce_risk."""
        status = {
            "state": "normal",
            "drawdown_pct": 2.0,
            "consecutive_losses": 3,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        trades = [
            _make_journal_entry(pnl=-50.0, exit_reason="stop_loss"),
            _make_journal_entry(pnl=-20.0, exit_reason="time_exit"),
            _make_journal_entry(pnl=-30.0, exit_reason="manual"),
        ]
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=trades)
        assert rec.action == "reduce_risk"
        assert any("consecutive losses" in r.lower() for r in rec.reasons)

    def test_api_errors_waits(self):
        """3+ API errors in 5 min => wait_and_resume with >= 10 min wait."""
        status = {
            "state": "normal",
            "drawdown_pct": 1.0,
            "consecutive_losses": 0,
            "api_errors_5min": 5,
            "cooldown_minutes": 30,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "wait_and_resume"
        assert rec.wait_minutes >= 10
        assert any("API errors" in r for r in rec.reasons)

    def test_api_errors_in_halted_state_keep_stop_trading(self):
        """HALTED + API errors => still stop_trading (most severe wins)."""
        status = {
            "state": "halted",
            "drawdown_pct": 6.0,
            "consecutive_losses": 2,
            "api_errors_5min": 5,
            "cooldown_minutes": 45,
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "stop_trading"
        # API errors reason should also be present
        assert any("API errors" in r for r in rec.reasons)

    def test_warning_with_prior_halt_recovery_note(self):
        """WARNING state after recovering from halt => cautious re-entry note."""
        status = {
            "state": "warning",
            "drawdown_pct": 3.0,
            "consecutive_losses": 0,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
            "cooldown_entered_at": None,
            "halted_at": "2024-06-15T10:00:00",
        }
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=[])
        assert rec.action == "reduce_risk"
        assert any("recovered from halt" in r.lower() for r in rec.reasons)

    def test_consecutive_losses_with_dict_trades(self):
        """Circuit breaker handles trade records as dicts (not JournalEntry)."""
        status = {
            "state": "normal",
            "drawdown_pct": 2.0,
            "consecutive_losses": 3,
            "api_errors_5min": 0,
            "cooldown_minutes": 30,
        }
        trades = [
            {"pnl": -50, "exit_reason": "stop_loss"},
            {"pnl": -45, "exit_reason": "stop_hit"},
            {"pnl": -60, "exit_reason": "trailing_stop"},
        ]
        rec = self.advisor.recommend_circuit_breaker_action(status, daily_trades=trades)
        assert rec.action == "stop_trading"
        assert rec.wait_minutes >= 30


# ---------------------------------------------------------------------------
# Test recommend_position_sizing()
# ---------------------------------------------------------------------------


class TestRecommendPositionSizing:
    """Tests for TradingAdvisor.recommend_position_sizing()."""

    def setup_method(self):
        self.advisor = TradingAdvisor()

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_standard_sizing(self, mock_now):
        """Normal conditions => 1% base risk, positive max_shares."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="trending_bullish",
        )
        assert rec.recommended_risk_pct == pytest.approx(1.0, abs=0.01)
        # risk$ = 25000 * 0.01 = 250, stop_dist = 0.50, shares = 500
        assert rec.max_shares == 500
        assert len(rec.warnings) == 0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_small_account_caps_risk(self, mock_now):
        """Account below $2000 => risk capped at 0.5%, warnings present."""
        signal = _make_signal(entry_price=5.0, stop_price=4.80)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=1_000.0,
            current_positions=[],
            regime="trending_bullish",
        )
        assert rec.recommended_risk_pct <= 0.5
        assert any("Small account" in r for r in rec.reasons)
        assert any("Small account" in w for w in rec.warnings)
        # risk$ = 1000 * 0.005 = 5, stop_dist = 0.20, shares = 25
        # int(5.0 / 0.2) may be 24 or 25 due to float precision
        assert rec.max_shares in (24, 25)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_volatile_regime_reduces_risk(self, mock_now):
        """high_volatility regime => risk reduced by 40%."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="high_volatility",
        )
        # 1.0 * (1.0 - 0.40) = 0.60
        assert rec.recommended_risk_pct == pytest.approx(0.60, abs=0.01)
        assert any("Volatile regime" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_bearish_regime_reduces_risk(self, mock_now):
        """trending_bearish => same 40% reduction."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="trending_bearish",
        )
        assert rec.recommended_risk_pct == pytest.approx(0.60, abs=0.01)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_daily_risk_budget_consumed_halves_risk(self, mock_now):
        """When >= 50% of equity is at risk in open positions => halve risk."""
        # Create positions where risk = |entry - stop| * shares_remaining
        # total risk = |10 - 9| * 5000 = 5000, equity = 10_000
        # risk_budget_used_pct = 50.0% => halves
        pos = _make_position(
            entry_price=10.0,
            stop_price=9.0,
            shares=5000,
            shares_remaining=5000,
        )
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=10_000.0,
            current_positions=[pos],
            regime="trending_bullish",
        )
        # 1.0 * 0.5 = 0.5
        assert rec.recommended_risk_pct == pytest.approx(0.50, abs=0.01)
        assert any("equity in risk" in r.lower() for r in rec.reasons)
        assert any("daily risk budget" in w.lower() for w in rec.warnings)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_power_hour_time())
    def test_power_hour_reduces_size(self, mock_now):
        """After 3:00 PM ET => reduce size by 30%."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="trending_bullish",
        )
        # 1.0 * 0.7 = 0.7
        assert rec.recommended_risk_pct == pytest.approx(0.70, abs=0.01)
        assert any("Power hour" in r for r in rec.reasons)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_zero_stop_distance_zero_shares_with_warning(self, mock_now):
        """Stop price == entry price => zero shares and a warning."""
        signal = _make_signal(entry_price=10.0, stop_price=10.0)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="trending_bullish",
        )
        assert rec.max_shares == 0
        assert any("Stop distance is zero" in w for w in rec.warnings)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_risk_pct_clamped_min(self, mock_now):
        """Risk % never goes below 0.1%."""
        # Small account + volatile + risk budget consumed => many reductions
        pos = _make_position(
            entry_price=10.0,
            stop_price=5.0,
            shares=2000,
            shares_remaining=2000,
        )
        signal = _make_signal(entry_price=5.0, stop_price=4.80)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=500.0,
            current_positions=[pos],
            regime="high_volatility",
        )
        assert rec.recommended_risk_pct >= 0.1

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_risk_pct_clamped_max(self, mock_now):
        """Risk % never exceeds 3.0%."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="trending_bullish",
        )
        assert rec.recommended_risk_pct <= 3.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_first_hour_time())
    def test_first_hour_normal_sizing(self, mock_now):
        """First hour (9:30-10:30) => note about normal sizing, no reduction."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="trending_bullish",
        )
        assert any("First hour" in r for r in rec.reasons)
        assert rec.recommended_risk_pct == pytest.approx(1.0, abs=0.01)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_morning_time())
    def test_small_account_plus_volatile_compounds(self, mock_now):
        """Small account + volatile regime => both reductions compound."""
        signal = _make_signal(entry_price=5.0, stop_price=4.80)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=1_500.0,
            current_positions=[],
            regime="high_volatility",
        )
        # Small account caps at 0.5%, then volatile reduces by 40%
        # 0.5 * 0.6 = 0.3 => clamped to 0.3 (but min is 0.1)
        assert rec.recommended_risk_pct == pytest.approx(0.30, abs=0.01)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_power_hour_time())
    def test_power_hour_plus_volatile_compounds(self, mock_now):
        """Power hour + volatile regime => both reductions applied."""
        signal = _make_signal(entry_price=10.0, stop_price=9.50)
        rec = self.advisor.recommend_position_sizing(
            signal=signal,
            equity=25_000.0,
            current_positions=[],
            regime="high_volatility",
        )
        # 1.0 * 0.6 (volatile) * 0.7 (power hour) = 0.42
        assert rec.recommended_risk_pct == pytest.approx(0.42, abs=0.01)


# ---------------------------------------------------------------------------
# Test recommend_daily_plan()
# ---------------------------------------------------------------------------


class TestRecommendDailyPlan:
    """Tests for TradingAdvisor.recommend_daily_plan()."""

    def setup_method(self):
        self.advisor = TradingAdvisor()

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_default_plan_no_history(self, mock_now):
        """No trade history => default plan with 6 max trades."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert rec.max_trades_today == 6
        assert rec.risk_budget_pct == pytest.approx(3.0, abs=0.01)
        assert SignalType.VWAP_PULLBACK.value in rec.focus_setups
        assert any("No recent trade history" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_low_win_rate_restricts_plan(self, mock_now):
        """Win rate < 30% => max 3 trades, reduced risk, VWAP only."""
        entries = [
            _make_journal_entry(pnl=-50.0),
            _make_journal_entry(pnl=-30.0),
            _make_journal_entry(pnl=-40.0),
            _make_journal_entry(pnl=20.0),
            _make_journal_entry(pnl=-25.0),
        ]
        # 1 win / 5 = 20%
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=entries,
        )
        assert rec.max_trades_today == 3
        assert rec.risk_budget_pct <= 1.5
        assert rec.focus_setups == [SignalType.VWAP_PULLBACK.value]
        assert any("low" in n.lower() for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_moderate_win_rate(self, mock_now):
        """Win rate between 30% and 50% => moderate restrictions."""
        entries = [
            _make_journal_entry(pnl=-50.0),
            _make_journal_entry(pnl=30.0),
            _make_journal_entry(pnl=-40.0),
            _make_journal_entry(pnl=-20.0),
            _make_journal_entry(pnl=-25.0),
        ]
        # 1 win / 5 = 20% -- still low (< 30%)
        # Adjust to get 30-50%: 2 wins / 5 = 40%
        entries[3] = _make_journal_entry(pnl=15.0)
        # now: -50, +30, -40, +15, -25 => 2 wins / 5 = 40%
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=entries,
        )
        assert rec.max_trades_today == 4
        assert rec.risk_budget_pct <= 2.0
        assert any("below average" in n.lower() for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_solid_win_rate(self, mock_now):
        """Win rate >= 50% => standard plan."""
        entries = [
            _make_journal_entry(pnl=50.0),
            _make_journal_entry(pnl=30.0),
            _make_journal_entry(pnl=-40.0),
            _make_journal_entry(pnl=15.0),
            _make_journal_entry(pnl=25.0),
        ]
        # 4 wins / 5 = 80%
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=entries,
        )
        assert rec.max_trades_today == 6
        assert rec.risk_budget_pct == pytest.approx(3.0, abs=0.01)
        assert any("solid" in n.lower() for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_bearish_regime_limits_plan(self, mock_now):
        """Bearish regime => max 3 trades, reduced risk, VWAP only."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bearish",
            recent_journal_entries=[],
        )
        assert rec.max_trades_today <= 3
        assert rec.risk_budget_pct <= 1.5
        assert rec.focus_setups == [SignalType.VWAP_PULLBACK.value]
        assert any("Bearish regime" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_high_volatility_regime(self, mock_now):
        """High volatility regime => max 3 trades, risk <= 2%."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="high_volatility",
            recent_journal_entries=[],
        )
        assert rec.max_trades_today <= 3
        assert rec.risk_budget_pct <= 2.0
        assert any("High volatility" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_range_bound_regime(self, mock_now):
        """Range-bound market => skip breakouts, focus on pullbacks."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="range_bound",
            recent_journal_entries=[],
        )
        assert rec.max_trades_today <= 4
        assert SignalType.BREAKOUT_CONTINUATION.value not in rec.focus_setups
        assert SignalType.VWAP_PULLBACK.value in rec.focus_setups
        assert any("Range-bound" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_low_volatility_regime(self, mock_now):
        """Low volatility => note about extra volume confirmation."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="low_volatility",
            recent_journal_entries=[],
        )
        assert any("Low volatility" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_bullish_regime_full_roster(self, mock_now):
        """Bullish regime => all setups available."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert any("Bullish" in n for n in rec.notes)
        assert len(rec.focus_setups) == 3  # all three default setups

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_small_account_limits(self, mock_now):
        """Account < $2000 => max 2 trades, risk budget <= 1%."""
        rec = self.advisor.recommend_daily_plan(
            equity=1_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert rec.max_trades_today <= 2
        assert rec.risk_budget_pct <= 1.0
        assert any("Small account" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_growing_account(self, mock_now):
        """Account between $2000 and $5000 => moderate limits."""
        rec = self.advisor.recommend_daily_plan(
            equity=3_500.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert rec.max_trades_today <= 3
        assert rec.risk_budget_pct <= 2.0
        assert any("Growing account" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_above_pdt_threshold(self, mock_now):
        """Account >= $25000 => no PDT restriction note."""
        rec = self.advisor.recommend_daily_plan(
            equity=30_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert any("PDT threshold" in n for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_monday_morning())
    def test_monday_caution(self, mock_now):
        """Monday => one fewer trade, cautious start note."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert any("Monday" in n for n in rec.notes)
        # Default 6 minus 1 = 5
        assert rec.max_trades_today == 5

    @patch("trading_bot.strategies.advisor.now_et", return_value=_friday_morning())
    def test_friday_reduced_exposure(self, mock_now):
        """Friday => one fewer trade, risk budget * 0.8."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert any("Friday" in n for n in rec.notes)
        assert rec.max_trades_today == 5  # 6 - 1
        # 3.0 * 0.8 = 2.4
        assert rec.risk_budget_pct == pytest.approx(2.4, abs=0.01)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_monday_morning())
    def test_monday_with_bearish_regime_compounds(self, mock_now):
        """Monday + bearish regime => both reductions applied."""
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bearish",
            recent_journal_entries=[],
        )
        # Bearish caps at 3, then Monday subtracts 1 => 2
        assert rec.max_trades_today == 2
        assert rec.risk_budget_pct <= 1.5

    @patch("trading_bot.strategies.advisor.now_et", return_value=_friday_morning())
    def test_friday_with_small_account_compounds(self, mock_now):
        """Friday + small account => both reductions applied."""
        rec = self.advisor.recommend_daily_plan(
            equity=1_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        # Small account caps at 2, Friday subtracts: max_trades = 2 (already <= 2)
        # Actually: max_trades > 2 check fails so no subtraction on Friday
        assert rec.max_trades_today <= 2
        # risk_budget: small account caps at 1.0, Friday * 0.8 = 0.8
        assert rec.risk_budget_pct <= 1.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_risk_budget_clamped_min(self, mock_now):
        """Risk budget never drops below 0.5%."""
        # Low win rate + bearish + small account => lots of reductions
        entries = [
            _make_journal_entry(pnl=-50.0),
            _make_journal_entry(pnl=-30.0),
            _make_journal_entry(pnl=-40.0),
        ]
        rec = self.advisor.recommend_daily_plan(
            equity=500.0,
            regime="trending_bearish",
            recent_journal_entries=entries,
        )
        assert rec.risk_budget_pct >= 0.5

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_risk_budget_clamped_max(self, mock_now):
        """Risk budget never exceeds 5.0%."""
        rec = self.advisor.recommend_daily_plan(
            equity=50_000.0,
            regime="trending_bullish",
            recent_journal_entries=[],
        )
        assert rec.risk_budget_pct <= 5.0

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_journal_entries_as_dicts(self, mock_now):
        """Daily plan handles journal entries as dicts."""
        entries = [
            {"pnl": -50.0},
            {"pnl": 30.0},
            {"pnl": -20.0},
        ]
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bullish",
            recent_journal_entries=entries,
        )
        # 1 win / 3 = 33% -- below 50% but above 30%
        assert rec.max_trades_today == 4
        assert any("below average" in n.lower() for n in rec.notes)

    @patch("trading_bot.strategies.advisor.now_et", return_value=_wednesday_morning())
    def test_low_win_rate_with_bearish_compounds(self, mock_now):
        """Low win rate + bearish => most restrictive limits stacked."""
        entries = [
            _make_journal_entry(pnl=-50.0),
            _make_journal_entry(pnl=-30.0),
            _make_journal_entry(pnl=-40.0),
            _make_journal_entry(pnl=-20.0),
            _make_journal_entry(pnl=10.0),
        ]
        # 1 win / 5 = 20% => low win rate: max_trades=3, risk=1.5
        rec = self.advisor.recommend_daily_plan(
            equity=25_000.0,
            regime="trending_bearish",
            recent_journal_entries=entries,
        )
        # Low win rate: max 3, bearish: min(3, 3) = 3
        assert rec.max_trades_today <= 3
        # Low win rate: 1.5, bearish: min(1.5, 1.5) = 1.5
        assert rec.risk_budget_pct <= 1.5
        assert rec.focus_setups == [SignalType.VWAP_PULLBACK.value]


# ---------------------------------------------------------------------------
# Test dataclass fields (sanity checks on recommendation objects)
# ---------------------------------------------------------------------------


class TestRecommendationDataclasses:
    """Verify that recommendation dataclasses are well-formed."""

    def test_entry_recommendation_fields(self):
        from trading_bot.strategies.advisor import EntryRecommendation

        rec = EntryRecommendation(
            action="enter",
            confidence=0.85,
            reasons=["test"],
            suggested_adjustments={"foo": 1},
        )
        assert rec.action == "enter"
        assert rec.confidence == 0.85
        assert rec.reasons == ["test"]
        assert rec.suggested_adjustments == {"foo": 1}

    def test_exit_recommendation_fields(self):
        from trading_bot.strategies.advisor import ExitRecommendation

        rec = ExitRecommendation(
            action="hold",
            reasons=["test"],
            urgency="low",
        )
        assert rec.action == "hold"
        assert rec.urgency == "low"

    def test_circuit_breaker_recommendation_fields(self):
        from trading_bot.strategies.advisor import CircuitBreakerRecommendation

        rec = CircuitBreakerRecommendation(
            action="continue",
            reasons=["ok"],
            wait_minutes=0,
        )
        assert rec.wait_minutes == 0

    def test_sizing_recommendation_fields(self):
        from trading_bot.strategies.advisor import SizingRecommendation

        rec = SizingRecommendation(
            recommended_risk_pct=1.0,
            max_shares=100,
            reasons=["r"],
            warnings=["w"],
        )
        assert rec.max_shares == 100

    def test_daily_plan_recommendation_fields(self):
        from trading_bot.strategies.advisor import DailyPlanRecommendation

        rec = DailyPlanRecommendation(
            max_trades_today=5,
            focus_setups=["vwap_pullback"],
            risk_budget_pct=2.0,
            notes=["n"],
        )
        assert rec.max_trades_today == 5
