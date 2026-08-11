"""Tests for HealthMonitor."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from trading_bot.utils.health import HealthMonitor


class TestHealthMonitorRecordTick:
    """Tests for record_tick() updating tick count and timestamp."""

    def test_record_tick_increments_count(self):
        monitor = HealthMonitor()
        assert monitor._tick_count == 0

        monitor.record_tick()
        assert monitor._tick_count == 1

        monitor.record_tick()
        assert monitor._tick_count == 2

    def test_record_tick_updates_timestamp(self):
        monitor = HealthMonitor()
        assert monitor._last_tick_at is None

        before = datetime.utcnow()
        monitor.record_tick()
        after = datetime.utcnow()

        assert monitor._last_tick_at is not None
        assert before <= monitor._last_tick_at <= after

    def test_record_tick_successive_timestamps_advance(self):
        monitor = HealthMonitor()
        monitor.record_tick()
        first = monitor._last_tick_at

        monitor.record_tick()
        second = monitor._last_tick_at

        assert second >= first


class TestHealthMonitorRecordScan:
    """Tests for record_scan() updating scan count."""

    def test_record_scan_updates_candidate_count(self):
        monitor = HealthMonitor()
        assert monitor._last_scan_candidates == 0

        monitor.record_scan(count=5)
        assert monitor._last_scan_candidates == 5

    def test_record_scan_updates_timestamp(self):
        monitor = HealthMonitor()
        assert monitor._last_scan_at is None

        monitor.record_scan(count=3)
        assert monitor._last_scan_at is not None

    def test_record_scan_overwrites_previous_count(self):
        monitor = HealthMonitor()
        monitor.record_scan(count=10)
        monitor.record_scan(count=2)
        assert monitor._last_scan_candidates == 2

    def test_record_scan_zero_candidates(self):
        monitor = HealthMonitor()
        monitor.record_scan(count=0)
        assert monitor._last_scan_candidates == 0


class TestHealthMonitorRecordTrade:
    """Tests for record_trade() updating trade info."""

    def test_record_trade_updates_symbol(self):
        monitor = HealthMonitor()
        assert monitor._last_trade_symbol is None

        monitor.record_trade("AAPL")
        assert monitor._last_trade_symbol == "AAPL"

    def test_record_trade_updates_timestamp(self):
        monitor = HealthMonitor()
        assert monitor._last_trade_at is None

        before = datetime.utcnow()
        monitor.record_trade("TSLA")
        after = datetime.utcnow()

        assert monitor._last_trade_at is not None
        assert before <= monitor._last_trade_at <= after

    def test_record_trade_overwrites_previous(self):
        monitor = HealthMonitor()
        monitor.record_trade("AAPL")
        monitor.record_trade("MSFT")

        assert monitor._last_trade_symbol == "MSFT"


class TestHealthMonitorRecordError:
    """Tests for record_error() incrementing error counts."""

    def test_record_error_increments_total(self):
        monitor = HealthMonitor()
        assert monitor._total_api_errors == 0

        monitor.record_error("alpaca_api")
        assert monitor._total_api_errors == 1

        monitor.record_error("yfinance")
        assert monitor._total_api_errors == 2

    def test_record_error_adds_to_recent_errors(self):
        monitor = HealthMonitor()
        assert len(monitor._recent_errors) == 0

        monitor.record_error("alpaca_api")
        assert len(monitor._recent_errors) == 1

    def test_record_error_multiple_sources(self):
        monitor = HealthMonitor()
        monitor.record_error("alpaca_api")
        monitor.record_error("yfinance")
        monitor.record_error("market_data")

        assert monitor._total_api_errors == 3
        assert len(monitor._recent_errors) == 3


class TestHealthMonitorIsHealthy:
    """Tests for is_healthy() logic."""

    def test_is_healthy_true_when_no_ticks_yet(self):
        """System may be starting up; no tick recorded is OK."""
        monitor = HealthMonitor()
        assert monitor.is_healthy() is True

    def test_is_healthy_true_when_tick_is_fresh(self):
        monitor = HealthMonitor()
        monitor.record_tick()
        assert monitor.is_healthy() is True

    def test_is_healthy_false_when_tick_is_stale(self):
        """A tick older than 5 minutes makes system unhealthy."""
        monitor = HealthMonitor()
        # Set last tick to 10 minutes ago
        stale_time = datetime.utcnow() - timedelta(minutes=10)
        monitor._last_tick_at = stale_time

        assert monitor.is_healthy() is False

    @patch("trading_bot.utils.health.datetime")
    def test_is_healthy_false_when_stale_via_mock(self, mock_datetime):
        """Use mock datetime to simulate staleness."""
        monitor = HealthMonitor()

        # Record tick at a fixed time
        tick_time = datetime(2024, 6, 15, 10, 0, 0)
        monitor._last_tick_at = tick_time

        # "Now" is 6 minutes later -- beyond the 5 minute threshold
        mock_datetime.utcnow.return_value = datetime(2024, 6, 15, 10, 6, 0)

        assert monitor.is_healthy() is False

    @patch("trading_bot.utils.health.datetime")
    def test_is_healthy_true_when_fresh_via_mock(self, mock_datetime):
        """Use mock datetime to confirm freshness within window."""
        monitor = HealthMonitor()

        tick_time = datetime(2024, 6, 15, 10, 0, 0)
        monitor._last_tick_at = tick_time

        # "Now" is 2 minutes later -- within the 5 minute threshold
        mock_datetime.utcnow.return_value = datetime(2024, 6, 15, 10, 2, 0)

        assert monitor.is_healthy() is True

    def test_is_healthy_true_when_circuit_breaker_halted(self):
        """A financial halt is reported separately from operational health."""
        monitor = HealthMonitor()

        mock_cb = MagicMock()
        mock_cb.state.value = "halted"
        monitor.set_circuit_breaker(mock_cb)

        assert monitor.is_healthy() is True

    def test_is_healthy_true_when_circuit_breaker_active(self):
        """Circuit breaker in active state does not affect health."""
        monitor = HealthMonitor()

        mock_cb = MagicMock()
        mock_cb.state.value = "active"
        monitor.set_circuit_breaker(mock_cb)

        assert monitor.is_healthy() is True

    def test_is_healthy_true_without_circuit_breaker(self):
        """No circuit breaker registered should not cause unhealthy."""
        monitor = HealthMonitor()
        monitor.record_tick()
        assert monitor._circuit_breaker is None
        assert monitor.is_healthy() is True


class TestHealthMonitorGetHealthStatus:
    """Tests for get_health_status() returning complete dict."""

    def test_get_health_status_returns_all_expected_keys(self):
        monitor = HealthMonitor()
        status = monitor.get_health_status()

        expected_keys = {
            "last_tick_at",
            "tick_count_today",
            "last_scan_at",
            "last_scan_candidates",
            "last_trade_at",
            "last_trade_symbol",
            "api_error_count_total",
            "api_error_count_5min",
            "memory_usage_mb",
            "uptime_seconds",
            "circuit_breaker_state",
            "open_positions_count",
            "daily_pnl",
        }
        assert set(status.keys()) == expected_keys

    def test_get_health_status_initial_values(self):
        monitor = HealthMonitor()
        status = monitor.get_health_status()

        assert status["last_tick_at"] is None
        assert status["tick_count_today"] == 0
        assert status["last_scan_at"] is None
        assert status["last_scan_candidates"] == 0
        assert status["last_trade_at"] is None
        assert status["last_trade_symbol"] is None
        assert status["api_error_count_total"] == 0
        assert status["api_error_count_5min"] == 0
        assert status["circuit_breaker_state"] is None
        assert status["open_positions_count"] == 0
        assert status["daily_pnl"] == 0.0

    def test_get_health_status_after_activity(self):
        monitor = HealthMonitor()
        monitor.record_tick()
        monitor.record_tick()
        monitor.record_scan(count=7)
        monitor.record_trade("AAPL")
        monitor.record_error("alpaca_api")

        status = monitor.get_health_status()

        assert status["tick_count_today"] == 2
        assert status["last_tick_at"] is not None
        assert status["last_scan_at"] is not None
        assert status["last_scan_candidates"] == 7
        assert status["last_trade_at"] is not None
        assert status["last_trade_symbol"] == "AAPL"
        assert status["api_error_count_total"] == 1
        assert status["api_error_count_5min"] == 1

    def test_get_health_status_uptime_positive(self):
        monitor = HealthMonitor()
        status = monitor.get_health_status()
        assert status["uptime_seconds"] >= 0.0

    def test_get_health_status_memory_usage_positive(self):
        monitor = HealthMonitor()
        status = monitor.get_health_status()
        assert status["memory_usage_mb"] > 0.0

    def test_get_health_status_with_circuit_breaker(self):
        monitor = HealthMonitor()
        mock_cb = MagicMock()
        mock_cb.state.value = "active"
        monitor.set_circuit_breaker(mock_cb)

        status = monitor.get_health_status()
        assert status["circuit_breaker_state"] == "active"

    def test_get_health_status_with_portfolio_manager(self):
        monitor = HealthMonitor()

        mock_pm = MagicMock()
        mock_pm.get_open_positions.return_value = ["pos1", "pos2", "pos3"]
        mock_pm.get_daily_pnl.return_value = 150.75
        monitor.set_portfolio_manager(mock_pm)

        status = monitor.get_health_status()
        assert status["open_positions_count"] == 3
        assert status["daily_pnl"] == 150.75

    def test_get_health_status_prunes_old_errors(self):
        monitor = HealthMonitor()

        # Add an error that appears to be old (beyond 5 min window)
        old_time = datetime.utcnow() - timedelta(minutes=10)
        monitor._recent_errors.append(old_time)
        monitor._total_api_errors = 1

        status = monitor.get_health_status()

        # Old error should be pruned from recent count
        assert status["api_error_count_5min"] == 0
        # Total count is not affected by pruning
        assert status["api_error_count_total"] == 1
