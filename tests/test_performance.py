"""
Tests for the honest performance-analytics scorecard.

Covers the pure :func:`compute_performance` / :func:`rolling` helpers and the
``GET /api/performance`` dashboard endpoint. The guiding principle under test
is *honesty*: the scorecard must never flatter results and must loudly signal
when the sample is too small to be statistically meaningful.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.analytics.performance import (
    MIN_SAMPLE_FOR_CONFIDENCE,
    compute_performance,
    rolling,
)
from trading_bot.dashboard.app import create_app
from trading_bot.dashboard.state import DashboardState
from trading_bot.portfolio.manager import JOURNAL_HEADERS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _trade(pnl, rr=None, signal_type="vwap_pullback", hold=30.0):
    """Build a journal-shaped trade dict (values as strings, like CSV rows)."""
    row = {
        "date": "2025-01-15",
        "symbol": "TEST",
        "side": "buy",
        "signal_type": signal_type,
        "entry_price": "100.0",
        "exit_price": "101.0",
        "shares": "100",
        "pnl": str(pnl),
        "rr_ratio": "" if rr is None else str(rr),
        "hold_time_minutes": str(hold),
        "entry_time": "09:45:00",
        "exit_time": "10:15:00",
        "exit_reason": "target",
        "notes": "",
    }
    return row


@pytest.fixture
def multi_trades():
    """Hand-verified 5-trade fixture.

    pnls: +200, -100, +300, -150, +50   (net +300)
    """
    return [
        _trade(200, rr=2.0, signal_type="vwap_pullback"),
        _trade(-100, rr=-1.0, signal_type="vwap_pullback"),
        _trade(300, rr=3.0, signal_type="ema_pullback"),
        _trade(-150, rr=-1.5, signal_type="ema_pullback"),
        _trade(50, rr=0.5, signal_type="vwap_pullback"),
    ]


# ---------------------------------------------------------------------------
# compute_performance — empty / tiny samples
# ---------------------------------------------------------------------------


class TestEmptyAndTiny:
    def test_zero_trades_returns_honest_zeroed_struct(self):
        card = compute_performance([], starting_equity=100_000.0)
        assert card["trade_count"] == 0
        assert card["closed_trades"] == 0
        assert card["win_rate"] == 0.0
        assert card["profit_factor"] is None
        assert card["expectancy_per_trade"] == 0.0
        assert card["total_pnl"] == 0.0
        assert card["max_drawdown_pct"] == 0.0
        assert card["sharpe_ratio"] == 0.0
        assert card["sortino_ratio"] == 0.0
        assert card["by_setup"] == {}
        assert card["is_statistically_significant"] is False
        assert card["sample_size"] == 0
        assert card["min_sample_for_confidence"] == MIN_SAMPLE_FOR_CONFIDENCE
        assert card["confidence_note"] == (
            "No closed trades yet — the strategy has not entered a position."
        )

    def test_zero_trades_struct_is_fully_formed(self):
        """Every documented key must be present even with no trades."""
        card = compute_performance([], starting_equity=50_000.0)
        for key in (
            "trade_count", "closed_trades", "win_rate", "wins", "losses",
            "avg_win", "avg_loss", "largest_win", "largest_loss",
            "profit_factor", "expectancy_per_trade", "expectancy_r", "avg_rr",
            "total_pnl", "total_return_pct", "max_drawdown_pct",
            "sharpe_ratio", "sortino_ratio", "avg_hold_minutes",
            "by_setup", "sample_size", "min_sample_for_confidence",
            "is_statistically_significant", "confidence_note",
        ):
            assert key in card, f"missing key: {key}"
        assert card["starting_equity"] == 50_000.0

    def test_rows_without_pnl_do_not_count_as_closed(self):
        rows = [{"pnl": "", "signal_type": "vwap_pullback"}]
        card = compute_performance(rows, starting_equity=100_000.0)
        assert card["trade_count"] == 1  # raw rows are surfaced
        assert card["closed_trades"] == 0  # but none are priced/closed
        assert card["is_statistically_significant"] is False

    def test_single_trade_win(self):
        card = compute_performance(
            [_trade(150, rr=1.5, hold=45.0)], starting_equity=100_000.0
        )
        assert card["closed_trades"] == 1
        assert card["wins"] == 1
        assert card["losses"] == 0
        assert card["win_rate"] == 1.0
        assert card["avg_win"] == 150.0
        assert card["profit_factor"] is None  # no losses -> undefined, not inf
        assert card["expectancy_per_trade"] == 150.0
        assert card["expectancy_r"] == 1.5
        assert card["avg_rr"] == 1.5
        assert card["total_pnl"] == 150.0
        assert card["total_return_pct"] == pytest.approx(0.15)
        assert card["avg_hold_minutes"] == 45.0
        assert card["sharpe_ratio"] == 0.0  # <2 trades -> no dispersion
        assert card["is_statistically_significant"] is False
        assert "1/30" in card["confidence_note"]


# ---------------------------------------------------------------------------
# compute_performance — hand-verified multi-trade fixture
# ---------------------------------------------------------------------------


class TestMultiTradeHandVerified:
    def test_core_metrics(self, multi_trades):
        card = compute_performance(multi_trades, starting_equity=100_000.0)
        assert card["closed_trades"] == 5
        assert card["wins"] == 3
        assert card["losses"] == 2
        assert card["win_rate"] == 0.6
        assert card["gross_profit"] == 550.0
        assert card["gross_loss"] == 250.0
        assert card["profit_factor"] == 2.2  # 550 / 250
        assert card["total_pnl"] == 300.0
        assert card["expectancy_per_trade"] == 60.0  # 300 / 5
        assert card["avg_win"] == pytest.approx(183.3333, abs=1e-4)
        assert card["avg_loss"] == -125.0
        assert card["largest_win"] == 300.0
        assert card["largest_loss"] == -150.0
        assert card["avg_rr"] == 0.6
        assert card["total_return_pct"] == pytest.approx(0.3)

    def test_max_drawdown_from_cumulative_pnl(self, multi_trades):
        # equity: 100000 -> 100200 -> 100100 -> 100400 -> 100250 -> 100300
        # worst dd: peak 100400 -> trough 100250 = 150/100400 = 0.14940%
        card = compute_performance(multi_trades, starting_equity=100_000.0)
        assert card["max_drawdown_pct"] == pytest.approx(0.1494, abs=1e-4)

    def test_max_drawdown_prefers_equity_curve(self, multi_trades):
        # A supplied equity curve overrides the cumulative-PnL proxy.
        curve = [
            ("t0", 100_000.0),
            ("t1", 90_000.0),  # 10% drawdown
            ("t2", 95_000.0),
        ]
        card = compute_performance(
            multi_trades, equity_curve=curve, starting_equity=100_000.0
        )
        assert card["max_drawdown_pct"] == pytest.approx(10.0, abs=1e-4)

    def test_sharpe_sortino_positive_and_finite(self, multi_trades):
        card = compute_performance(multi_trades, starting_equity=100_000.0)
        assert card["sharpe_ratio"] > 0
        assert card["sortino_ratio"] > 0
        # net-positive with losers present -> sortino should exceed sharpe
        assert card["sortino_ratio"] >= card["sharpe_ratio"]

    def test_by_setup_grouping(self, multi_trades):
        card = compute_performance(multi_trades, starting_equity=100_000.0)
        setups = card["by_setup"]
        assert set(setups) == {"vwap_pullback", "ema_pullback"}

        vwap = setups["vwap_pullback"]  # +200, -100, +50
        assert vwap["closed_trades"] == 3
        assert vwap["total_pnl"] == 150.0
        assert vwap["wins"] == 2
        assert vwap["losses"] == 1
        assert vwap["win_rate"] == pytest.approx(0.6667, abs=1e-4)
        assert vwap["profit_factor"] == 2.5  # 250 / 100
        assert vwap["expectancy_per_trade"] == 50.0

        ema = setups["ema_pullback"]  # +300, -150
        assert ema["closed_trades"] == 2
        assert ema["total_pnl"] == 150.0
        assert ema["win_rate"] == 0.5
        assert ema["profit_factor"] == 2.0  # 300 / 150
        assert ema["expectancy_per_trade"] == 75.0


# ---------------------------------------------------------------------------
# Profit-factor guards
# ---------------------------------------------------------------------------


class TestProfitFactorGuards:
    def test_all_wins_profit_factor_is_none(self):
        trades = [_trade(100), _trade(200), _trade(50)]
        card = compute_performance(trades, starting_equity=100_000.0)
        assert card["wins"] == 3
        assert card["losses"] == 0
        assert card["win_rate"] == 1.0
        assert card["gross_loss"] == 0.0
        assert card["profit_factor"] is None  # never inf

    def test_all_losses_profit_factor_zero(self):
        trades = [_trade(-100), _trade(-200), _trade(-50)]
        card = compute_performance(trades, starting_equity=100_000.0)
        assert card["wins"] == 0
        assert card["losses"] == 3
        assert card["win_rate"] == 0.0
        assert card["gross_profit"] == 0.0
        assert card["profit_factor"] == 0.0  # 0 profit / 450 loss
        assert card["total_pnl"] == -350.0
        assert card["avg_win"] == 0.0
        assert card["avg_loss"] == pytest.approx(-116.6667, abs=1e-4)


# ---------------------------------------------------------------------------
# Statistical-significance threshold
# ---------------------------------------------------------------------------


class TestSignificanceThreshold:
    @pytest.mark.parametrize(
        "count,expected",
        [(29, False), (30, True), (31, True)],
    )
    def test_threshold_at_boundary(self, count, expected):
        trades = [_trade(10 if i % 2 == 0 else -5) for i in range(count)]
        card = compute_performance(trades, starting_equity=100_000.0)
        assert card["closed_trades"] == count
        assert card["sample_size"] == count
        assert card["is_statistically_significant"] is expected
        if expected:
            assert "meet" in card["confidence_note"]
        else:
            assert f"{count}/{MIN_SAMPLE_FOR_CONFIDENCE}" in card["confidence_note"]


# ---------------------------------------------------------------------------
# rolling()
# ---------------------------------------------------------------------------


class TestRolling:
    def test_empty_returns_empty(self):
        assert rolling([], window=5) == []

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            rolling([_trade(10)], window=0)

    def test_rolling_window_and_equity(self):
        trades = [_trade(100), _trade(-50), _trade(200), _trade(-25)]
        series = rolling(trades, window=2, starting_equity=10_000.0)
        assert len(series) == 4

        # Point 1: window [100] -> 1 win, expectancy 100, equity 10100
        assert series[0] == {
            "trade_number": 1, "window": 1,
            "win_rate": 1.0, "expectancy": 100.0, "equity": 10_100.0,
        }
        # Point 2: window [100, -50] -> 1/2 win, expectancy 25, equity 10050
        assert series[1]["win_rate"] == 0.5
        assert series[1]["expectancy"] == 25.0
        assert series[1]["equity"] == 10_050.0
        # Point 4: window [200, -25] -> 1/2, expectancy 87.5, equity 10225
        assert series[3]["window"] == 2
        assert series[3]["win_rate"] == 0.5
        assert series[3]["expectancy"] == 87.5
        assert series[3]["equity"] == 10_225.0

    def test_rolling_reveals_emerging_edge(self):
        # First half losers, second half winners -> rolling win_rate should rise.
        trades = [_trade(-10) for _ in range(5)] + [_trade(10) for _ in range(5)]
        series = rolling(trades, window=3, starting_equity=10_000.0)
        assert series[2]["win_rate"] == 0.0  # early window all losers
        assert series[-1]["win_rate"] == 1.0  # late window all winners


# ---------------------------------------------------------------------------
# Dashboard endpoint
# ---------------------------------------------------------------------------


class TestPerformanceEndpoint:
    def test_empty_journal_returns_honest_zeroed_struct(self, tmp_path):
        journal = tmp_path / "journal.csv"
        with open(journal, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=JOURNAL_HEADERS).writeheader()

        app = create_app(DashboardState(), journal_path=str(journal))
        client = TestClient(app)
        resp = client.get("/api/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["closed_trades"] == 0
        assert data["is_statistically_significant"] is False
        assert data["profit_factor"] is None
        assert "No closed trades yet" in data["confidence_note"]

    def test_missing_journal_file_is_graceful(self, tmp_path):
        app = create_app(
            DashboardState(), journal_path=str(tmp_path / "does_not_exist.csv")
        )
        client = TestClient(app)
        resp = client.get("/api/performance")
        assert resp.status_code == 200
        assert resp.json()["closed_trades"] == 0

    def test_populated_journal(self, tmp_path):
        journal = tmp_path / "journal.csv"
        with open(journal, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS)
            writer.writeheader()
            writer.writerow(_trade(200, rr=2.0, signal_type="vwap_pullback"))
            writer.writerow(_trade(-100, rr=-1.0, signal_type="vwap_pullback"))
            writer.writerow(_trade(300, rr=3.0, signal_type="ema_pullback"))

        state = DashboardState()
        app = create_app(state, journal_path=str(journal))
        client = TestClient(app)
        resp = client.get("/api/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["closed_trades"] == 3
        assert data["wins"] == 2
        assert data["losses"] == 1
        assert data["total_pnl"] == 400.0
        assert data["profit_factor"] == 5.0  # 500 / 100
        assert set(data["by_setup"]) == {"vwap_pullback", "ema_pullback"}
        assert data["is_statistically_significant"] is False  # only 3 trades

    def test_endpoint_uses_equity_history_for_drawdown(self, tmp_path):
        journal = tmp_path / "journal.csv"
        with open(journal, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS)
            writer.writeheader()
            writer.writerow(_trade(100, rr=1.0))

        state = DashboardState()
        # Push two snapshots so equity_history has a drawdown.
        state.update(
            equity=100_000.0, starting_equity=100_000.0, daily_pnl=0.0,
            buying_power=0.0, open_positions=[], journal_entries=[],
            circuit_breaker={}, health={}, regime=None, run_mode="paper",
        )
        state.update(
            equity=95_000.0, starting_equity=100_000.0, daily_pnl=-5_000.0,
            buying_power=0.0, open_positions=[], journal_entries=[],
            circuit_breaker={}, health={}, regime=None, run_mode="paper",
        )
        app = create_app(state, journal_path=str(journal))
        client = TestClient(app)
        data = client.get("/api/performance").json()
        assert data["starting_equity"] == 100_000.0
        assert data["max_drawdown_pct"] == pytest.approx(5.0, abs=1e-4)
