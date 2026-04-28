"""
Tests for the dashboard web UI and API endpoints.

Tests the DashboardState thread-safe container and all FastAPI
endpoints using the httpx TestClient.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from trading_bot.dashboard.app import create_app
from trading_bot.dashboard.state import DashboardState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state() -> DashboardState:
    return DashboardState()


@pytest.fixture
def populated_state(state: DashboardState) -> DashboardState:
    """State with realistic data pre-loaded."""
    state.update(
        equity=25_150.00,
        starting_equity=25_000.00,
        daily_pnl=150.00,
        buying_power=100_600.00,
        open_positions=[
            {
                "symbol": "AAPL",
                "signal_type": "vwap_pullback",
                "entry_price": 150.00,
                "current_price": 152.50,
                "shares": 100,
                "shares_remaining": 67,
                "stop_price": 148.50,
                "pnl_unrealized": 167.50,
                "pnl_realized": 83.33,
                "scale_outs_completed": 1,
                "trailing_stop_active": True,
                "trailing_stop_price": 151.00,
                "entry_time": "2025-01-15T10:30:00",
            }
        ],
        journal_entries=[
            {
                "date": "2025-01-15",
                "symbol": "TSLA",
                "side": "buy",
                "signal_type": "ema_pullback",
                "entry_price": 200.00,
                "exit_price": 203.00,
                "shares": 50,
                "pnl": 150.00,
                "rr_ratio": 1.5,
                "hold_time_minutes": 45.0,
                "entry_time": "09:45:00",
                "exit_time": "10:30:00",
                "exit_reason": "scale_out_target",
                "notes": "",
            }
        ],
        circuit_breaker={
            "state": "normal",
            "daily_pnl": 150.00,
            "starting_equity": 25_000.00,
            "consecutive_losses": 0,
            "api_errors_5min": 0,
            "drawdown_pct": 0.0,
        },
        health={
            "tick_count_today": 42,
            "memory_usage_mb": 85.3,
            "uptime_seconds": 3600.0,
            "api_error_count_5min": 0,
            "api_error_count_total": 0,
            "last_scan_candidates": 5,
            "last_tick_at": "2025-01-15T10:30:00",
        },
        regime="trending_bullish",
        run_mode="paper",
    )
    return state


@pytest.fixture
def client(populated_state: DashboardState) -> TestClient:
    app = create_app(populated_state)
    return TestClient(app)


@pytest.fixture
def empty_client(state: DashboardState) -> TestClient:
    app = create_app(state)
    return TestClient(app)


# ---------------------------------------------------------------------------
# DashboardState tests
# ---------------------------------------------------------------------------


class TestDashboardState:
    def test_initial_snapshot_defaults(self, state: DashboardState):
        snap = state.get_snapshot()
        assert snap.equity == 0.0
        assert snap.starting_equity == 0.0
        assert snap.daily_pnl == 0.0
        assert snap.open_positions == []
        assert snap.journal_entries == []
        assert snap.regime is None
        assert snap.bot_running is False

    def test_update_sets_values(self, populated_state: DashboardState):
        snap = populated_state.get_snapshot()
        assert snap.equity == 25_150.00
        assert snap.starting_equity == 25_000.00
        assert snap.daily_pnl == 150.00
        assert snap.buying_power == 100_600.00
        assert snap.regime == "trending_bullish"
        assert snap.run_mode == "paper"
        assert snap.bot_running is True
        assert snap.last_updated is not None

    def test_open_positions_populated(self, populated_state: DashboardState):
        snap = populated_state.get_snapshot()
        assert len(snap.open_positions) == 1
        assert snap.open_positions[0]["symbol"] == "AAPL"
        assert snap.open_positions[0]["shares_remaining"] == 67

    def test_journal_entries_populated(self, populated_state: DashboardState):
        snap = populated_state.get_snapshot()
        assert len(snap.journal_entries) == 1
        assert snap.journal_entries[0]["symbol"] == "TSLA"
        assert snap.journal_entries[0]["pnl"] == 150.00

    def test_equity_history_grows(self, state: DashboardState):
        for i in range(5):
            state.update(
                equity=25_000.0 + i * 10,
                starting_equity=25_000.0,
                daily_pnl=i * 10.0,
                buying_power=100_000.0,
                open_positions=[],
                journal_entries=[],
                circuit_breaker={},
                health={},
                regime=None,
                run_mode="paper",
            )
        snap = state.get_snapshot()
        assert len(snap.equity_history) == 5
        assert snap.equity_history[0]["equity"] == 25_000.0
        assert snap.equity_history[4]["equity"] == 25_040.0

    def test_equity_history_capped_at_500(self, state: DashboardState):
        for i in range(550):
            state.update(
                equity=25_000.0 + i,
                starting_equity=25_000.0,
                daily_pnl=float(i),
                buying_power=100_000.0,
                open_positions=[],
                journal_entries=[],
                circuit_breaker={},
                health={},
                regime=None,
                run_mode="paper",
            )
        snap = state.get_snapshot()
        assert len(snap.equity_history) == 500

    def test_snapshot_is_deep_copy(self, populated_state: DashboardState):
        snap1 = populated_state.get_snapshot()
        snap2 = populated_state.get_snapshot()
        # Modify snap1 — should not affect snap2
        snap1.open_positions.append({"symbol": "FAKE"})
        assert len(snap2.open_positions) == 1

    def test_thread_safety(self, state: DashboardState):
        """Multiple threads writing and reading concurrently."""
        errors = []

        def writer():
            for i in range(100):
                try:
                    state.update(
                        equity=25_000.0 + i,
                        starting_equity=25_000.0,
                        daily_pnl=float(i),
                        buying_power=100_000.0,
                        open_positions=[],
                        journal_entries=[],
                        circuit_breaker={},
                        health={},
                        regime=None,
                        run_mode="paper",
                    )
                except Exception as e:
                    errors.append(e)

        def reader():
            for _ in range(100):
                try:
                    snap = state.get_snapshot()
                    _ = snap.equity
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestDashboardHTML:
    def test_root_returns_html(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_root_contains_title(self, client: TestClient):
        resp = client.get("/")
        assert "Momentum Trading Bot" in resp.text

    def test_root_shows_equity(self, client: TestClient):
        resp = client.get("/")
        assert "25,150.00" in resp.text

    def test_root_shows_pnl(self, client: TestClient):
        resp = client.get("/")
        assert "150.00" in resp.text

    def test_root_shows_regime(self, client: TestClient):
        resp = client.get("/")
        assert "trending bullish" in resp.text.lower()

    def test_root_shows_position(self, client: TestClient):
        resp = client.get("/")
        assert "AAPL" in resp.text

    def test_root_shows_trade_history(self, client: TestClient):
        resp = client.get("/")
        assert "TSLA" in resp.text

    def test_root_shows_mode_badge(self, client: TestClient):
        resp = client.get("/")
        assert "paper" in resp.text.lower()

    def test_root_empty_state(self, empty_client: TestClient):
        resp = empty_client.get("/")
        assert resp.status_code == 200
        assert "No open positions" in resp.text
        assert "No completed trades" in resp.text


class TestAPIStatus:
    def test_status_returns_json(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["equity"] == 25_150.00
        assert data["starting_equity"] == 25_000.00
        assert data["daily_pnl"] == 150.00

    def test_status_daily_return_pct(self, client: TestClient):
        resp = client.get("/api/status")
        data = resp.json()
        assert data["daily_return_pct"] == 0.6  # 150/25000 * 100

    def test_status_regime(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.json()["regime"] == "trending_bullish"

    def test_status_run_mode(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.json()["run_mode"] == "paper"

    def test_status_bot_running(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.json()["bot_running"] is True

    def test_status_positions_count(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.json()["open_positions_count"] == 1

    def test_status_trades_count(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.json()["total_trades_today"] == 1


class TestAPIPositions:
    def test_positions_returns_list(self, client: TestClient):
        resp = client.get("/api/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_position_fields(self, client: TestClient):
        resp = client.get("/api/positions")
        pos = resp.json()[0]
        assert pos["symbol"] == "AAPL"
        assert pos["entry_price"] == 150.00
        assert pos["current_price"] == 152.50
        assert pos["shares_remaining"] == 67
        assert pos["trailing_stop_active"] is True

    def test_empty_positions(self, empty_client: TestClient):
        resp = empty_client.get("/api/positions")
        assert resp.json() == []


class TestAPITrades:
    def test_trades_returns_list(self, client: TestClient):
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_trade_fields(self, client: TestClient):
        resp = client.get("/api/trades")
        trade = resp.json()[0]
        assert trade["symbol"] == "TSLA"
        assert trade["pnl"] == 150.00
        assert trade["exit_reason"] == "scale_out_target"

    def test_empty_trades(self, empty_client: TestClient):
        resp = empty_client.get("/api/trades")
        assert resp.json() == []


class TestAPIEquityHistory:
    def test_equity_history_returns_list(self, client: TestClient):
        resp = client.get("/api/equity-history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # populated_state called update once, so 1 data point
        assert len(data) == 1

    def test_equity_history_has_timestamp_and_equity(self, client: TestClient):
        resp = client.get("/api/equity-history")
        point = resp.json()[0]
        assert "timestamp" in point
        assert "equity" in point
        assert point["equity"] == 25_150.00

    def test_empty_equity_history(self, empty_client: TestClient):
        resp = empty_client.get("/api/equity-history")
        assert resp.json() == []


class TestAPIHealth:
    def test_health_returns_dict(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tick_count_today"] == 42
        assert data["memory_usage_mb"] == 85.3

    def test_empty_health(self, empty_client: TestClient):
        resp = empty_client.get("/api/health")
        assert resp.json() == {}


class TestAPICircuitBreaker:
    def test_circuit_breaker_returns_dict(self, client: TestClient):
        resp = client.get("/api/circuit-breaker")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "normal"
        assert data["consecutive_losses"] == 0

    def test_empty_circuit_breaker(self, empty_client: TestClient):
        resp = empty_client.get("/api/circuit-breaker")
        assert resp.json() == {}
