"""Tests for dashboard API endpoints and health probes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trading_bot.dashboard.app import create_app
from trading_bot.dashboard.state import DashboardState


@pytest.fixture
def dashboard_state() -> DashboardState:
    """Fresh dashboard state for testing."""
    return DashboardState()


@pytest.fixture
def client(dashboard_state: DashboardState) -> TestClient:
    """FastAPI test client wired to dashboard state."""
    app = create_app(dashboard_state)
    return TestClient(app)


class TestLivenessProbe:
    """Test /healthz endpoint."""

    def test_returns_alive(self, client: TestClient):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "alive"
        assert "uptime_seconds" in data

    def test_always_succeeds_even_without_bot(self, client: TestClient):
        """Liveness doesn't depend on bot running."""
        resp = client.get("/healthz")
        assert resp.status_code == 200


class TestReadinessProbe:
    """Test /readyz endpoint."""

    def test_not_ready_before_bot_starts(self, client: TestClient):
        """Returns 503 when bot hasn't pushed any state."""
        resp = client.get("/readyz")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["bot_running"] is False

    def test_ready_after_bot_update(
        self, client: TestClient, dashboard_state: DashboardState
    ):
        """Returns 200 after bot pushes state."""
        dashboard_state.update(
            equity=25000,
            starting_equity=25000,
            daily_pnl=0.0,
            buying_power=100000,
            open_positions=[],
            journal_entries=[],
            circuit_breaker={"state": "normal", "drawdown_pct": 0},
            health={},
            regime=None,
            run_mode="paper",
            market_status="open",
        )
        resp = client.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"

    def test_not_ready_when_halted(
        self, client: TestClient, dashboard_state: DashboardState
    ):
        """Returns 503 when circuit breaker is halted."""
        dashboard_state.update(
            equity=25000,
            starting_equity=25000,
            daily_pnl=-1500,
            buying_power=100000,
            open_positions=[],
            journal_entries=[],
            circuit_breaker={"state": "halted", "drawdown_pct": 6.0},
            health={},
            regime=None,
            run_mode="paper",
            market_status="open",
        )
        resp = client.get("/readyz")
        assert resp.status_code == 503


class TestAPIStatus:
    """Test /api/status endpoint."""

    def test_status_returns_all_fields(
        self, client: TestClient, dashboard_state: DashboardState
    ):
        dashboard_state.update(
            equity=26000,
            starting_equity=25000,
            daily_pnl=1000,
            buying_power=100000,
            open_positions=[],
            journal_entries=[],
            circuit_breaker={"state": "normal"},
            health={"tick_count_today": 5},
            regime="trending_bullish",
            run_mode="paper",
            market_status="open",
            market_status_detail="Market is open",
        )
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()

        assert data["equity"] == 26000
        assert data["daily_pnl"] == 1000
        assert data["daily_return_pct"] == 4.0
        assert data["market_status"] == "open"
        assert data["regime"] == "trending_bullish"
        assert data["bot_running"] is True
        assert "last_updated" in data


class TestAPIEndpoints:
    """Test all JSON API endpoints."""

    def test_positions_empty(self, client: TestClient):
        resp = client.get("/api/positions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_trades_empty(self, client: TestClient):
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_candidates_empty_and_truthfully_labeled(self, client: TestClient):
        resp = client.get("/api/candidates")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["candidates"] == []
        assert "not win probabilities" in body["disclaimer"]

    def test_live_readiness_fails_closed_without_trades(self, client: TestClient):
        resp = client.get("/api/live-readiness")
        assert resp.status_code == 200
        assert resp.json()["ready"] is False

    def test_equity_history_empty(self, client: TestClient):
        resp = client.get("/api/equity-history")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_health_empty(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_circuit_breaker_empty(self, client: TestClient):
        resp = client.get("/api/circuit-breaker")
        assert resp.status_code == 200

    def test_dashboard_html_renders(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Momentum Trading Bot" in resp.text


class TestDashboardState:
    """Test the DashboardState container."""

    def test_timestamp_is_et(self, dashboard_state: DashboardState):
        """Timestamps should be in ET format."""
        dashboard_state.update(
            equity=25000,
            starting_equity=25000,
            daily_pnl=0,
            buying_power=100000,
            open_positions=[],
            journal_entries=[],
            circuit_breaker={},
            health={},
            regime=None,
            run_mode="paper",
        )
        snap = dashboard_state.get_snapshot()
        assert snap.last_updated is not None
        assert "ET" in snap.last_updated

    def test_equity_history_capped_at_500(self, dashboard_state: DashboardState):
        """Equity history should never exceed 500 points."""
        for i in range(600):
            dashboard_state.update(
                equity=25000 + i,
                starting_equity=25000,
                daily_pnl=float(i),
                buying_power=100000,
                open_positions=[],
                journal_entries=[],
                circuit_breaker={},
                health={},
                regime=None,
                run_mode="paper",
            )
        snap = dashboard_state.get_snapshot()
        assert len(snap.equity_history) == 500

    def test_snapshot_is_deep_copy(self, dashboard_state: DashboardState):
        """Snapshots should be independent copies."""
        dashboard_state.update(
            equity=25000,
            starting_equity=25000,
            daily_pnl=0,
            buying_power=100000,
            open_positions=[{"symbol": "AAPL"}],
            journal_entries=[],
            circuit_breaker={},
            health={},
            regime=None,
            run_mode="paper",
        )
        snap1 = dashboard_state.get_snapshot()
        snap1.open_positions.append({"symbol": "FAKE"})
        snap2 = dashboard_state.get_snapshot()
        assert len(snap2.open_positions) == 1  # Unaffected


class TestPrivateDashboardAuthentication:
    def test_private_mode_requires_configured_key(
        self, monkeypatch: pytest.MonkeyPatch, dashboard_state: DashboardState
    ):
        monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
        monkeypatch.delenv("TRADING_DASHBOARD_API_KEY", raising=False)
        private_client = TestClient(create_app(dashboard_state))
        assert private_client.get("/health").status_code == 200
        assert private_client.get("/api/status").status_code == 503

    def test_private_mode_rejects_wrong_key_and_accepts_owner_key(
        self, monkeypatch: pytest.MonkeyPatch, dashboard_state: DashboardState
    ):
        monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
        monkeypatch.setenv("TRADING_DASHBOARD_API_KEY", "owner-secret")
        private_client = TestClient(create_app(dashboard_state))
        assert private_client.get("/api/status").status_code == 401
        assert private_client.get(
            "/api/status", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert private_client.get(
            "/api/status", headers={"Authorization": "Bearer owner-secret"}
        ).status_code == 200
