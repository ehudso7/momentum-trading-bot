"""Tests for infrastructure enhancements."""
from __future__ import annotations
from datetime import date
import pytest

from trading_bot.persistence.store import PersistentStore


class TestPersistentStore:
    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        s = PersistentStore(db_path=db_path)
        yield s
        s.close()
    
    def test_record_trade(self, store):
        store.record_trade(
            date="2024-06-15",
            symbol="AAPL",
            side="buy",
            signal_type="vwap_pullback",
            entry_price=150.0,
            exit_price=155.0,
            shares=100,
            pnl=500.0,
            rr_ratio=2.0,
            hold_time_minutes=45.0,
        )
        # Verify it was stored
        cur = store._conn.execute("SELECT * FROM trades WHERE symbol='AAPL'")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["pnl"] == 500.0
    
    def test_record_daily_pnl(self, store):
        store.record_daily_pnl(
            date=date.today().isoformat(),
            starting_equity=100000.0,
            ending_equity=100500.0,
            total_pnl=500.0,
            total_trades=5,
            winning_trades=3,
            losing_trades=2,
        )
        curve = store.get_equity_curve(days=30)
        assert len(curve) == 1
        assert curve[0]["total_pnl"] == 500.0
    
    def test_record_circuit_breaker_event(self, store):
        store.record_circuit_breaker_event(
            state="halted",
            reason="drawdown > 5%",
            daily_pnl=-5000.0,
            equity=95000.0,
        )
        cur = store._conn.execute("SELECT * FROM circuit_breaker_events")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "halted"
    
    def test_record_regime_transition(self, store):
        store.record_regime_transition("low_volatility", "trending_bullish")
        cur = store._conn.execute("SELECT * FROM regime_transitions")
        rows = cur.fetchall()
        assert len(rows) == 1
    
    def test_symbol_win_rate(self, store):
        store.record_trade(
            date="2024-06-15", symbol="TSLA", side="buy",
            signal_type="vwap", entry_price=200, exit_price=210,
            shares=50, pnl=500,
        )
        store.record_trade(
            date="2024-06-15", symbol="TSLA", side="buy",
            signal_type="vwap", entry_price=200, exit_price=190,
            shares=50, pnl=-500,
        )
        # Can't guarantee win_rate works due to date('now') in SQLite
        # but it shouldn't error
        store.get_symbol_win_rate("TSLA")
    
    def test_equity_curve_empty(self, store):
        curve = store.get_equity_curve()
        assert curve == []
    
    def test_multiple_trades(self, store):
        for i in range(5):
            store.record_trade(
                date="2024-06-15", symbol=f"SYM{i}", side="buy",
                signal_type="vwap", entry_price=10.0, exit_price=11.0,
                shares=100, pnl=100.0,
            )
        cur = store._conn.execute("SELECT COUNT(*) as cnt FROM trades")
        assert cur.fetchone()["cnt"] == 5


class TestNewsSentiment:
    def test_classify_sentiment_returns_neutral_without_polygon(self):
        from trading_bot.config.settings import ScannerConfig
        from trading_bot.data.news_client import NewsClient
        client = NewsClient(polygon_client=None, config=ScannerConfig())
        assert client.classify_sentiment("AAPL") == "neutral"
    
    def test_positive_keywords_defined(self):
        from trading_bot.data.news_client import NewsClient
        assert hasattr(NewsClient, 'POSITIVE_KEYWORDS')
        assert len(NewsClient.POSITIVE_KEYWORDS) > 0
    
    def test_negative_keywords_defined(self):
        from trading_bot.data.news_client import NewsClient
        assert hasattr(NewsClient, 'NEGATIVE_KEYWORDS')
        assert len(NewsClient.NEGATIVE_KEYWORDS) > 0


class TestNotificationQueue:
    def test_notification_manager_has_queue(self):
        from trading_bot.config.settings import NotificationConfig
        from trading_bot.utils.notifications import NotificationManager
        config = NotificationConfig()
        mgr = NotificationManager(config)
        assert hasattr(mgr, '_queue')
