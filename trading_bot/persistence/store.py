"""SQLite-based persistent state storage."""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
import structlog

log = structlog.get_logger(__name__)

_DEFAULT_DB_PATH = "data/trading_state.db"

class PersistentStore:
    """
    SQLite-backed storage for trade history, daily P&L, circuit breaker
    events, per-symbol stats, and regime transitions.
    
    Enables data persistence across bot restarts for:
    - Historical trade journal (searchable)
    - Daily P&L series for equity curve
    - Per-symbol win/loss tracking
    - Circuit breaker event log
    - Regime transition history
    """
    
    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
    
    def _create_tables(self) -> None:
        cursor = self._conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                pnl REAL NOT NULL,
                rr_ratio REAL,
                hold_time_minutes REAL,
                entry_time TEXT,
                exit_time TEXT,
                exit_reason TEXT,
                regime TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            
            CREATE TABLE IF NOT EXISTS daily_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                starting_equity REAL NOT NULL,
                ending_equity REAL NOT NULL,
                total_pnl REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                winning_trades INTEGER NOT NULL,
                losing_trades INTEGER NOT NULL,
                max_drawdown_pct REAL,
                regime TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            
            CREATE TABLE IF NOT EXISTS circuit_breaker_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                state TEXT NOT NULL,
                reason TEXT,
                daily_pnl REAL,
                equity REAL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            
            CREATE TABLE IF NOT EXISTS symbol_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                avg_hold_minutes REAL,
                UNIQUE(symbol, date)
            );
            
            CREATE TABLE IF NOT EXISTS regime_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                from_regime TEXT,
                to_regime TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
            CREATE INDEX IF NOT EXISTS idx_symbol_stats_symbol ON symbol_stats(symbol);
        """)
        self._conn.commit()
    
    def record_trade(
        self,
        date: str,
        symbol: str,
        side: str,
        signal_type: str,
        entry_price: float,
        exit_price: float,
        shares: int,
        pnl: float,
        rr_ratio: float = 0.0,
        hold_time_minutes: float = 0.0,
        entry_time: str = "",
        exit_time: str = "",
        exit_reason: str = "",
        regime: str = "",
        notes: str = "",
    ) -> None:
        """Record a completed trade."""
        try:
            self._conn.execute(
                """INSERT INTO trades (date, symbol, side, signal_type, entry_price,
                   exit_price, shares, pnl, rr_ratio, hold_time_minutes, entry_time,
                   exit_time, exit_reason, regime, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, symbol, side, signal_type, entry_price, exit_price,
                 shares, pnl, rr_ratio, hold_time_minutes, entry_time,
                 exit_time, exit_reason, regime, notes),
            )
            self._conn.commit()
            
            # Update symbol stats
            self._update_symbol_stats(symbol, date, pnl, hold_time_minutes)
        except Exception as e:
            log.error("store.record_trade_error", error=str(e))
    
    def record_daily_pnl(
        self,
        date: str,
        starting_equity: float,
        ending_equity: float,
        total_pnl: float,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        max_drawdown_pct: float = 0.0,
        regime: str = "",
    ) -> None:
        """Record end-of-day P&L summary."""
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO daily_pnl
                   (date, starting_equity, ending_equity, total_pnl, total_trades,
                    winning_trades, losing_trades, max_drawdown_pct, regime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, starting_equity, ending_equity, total_pnl,
                 total_trades, winning_trades, losing_trades, max_drawdown_pct, regime),
            )
            self._conn.commit()
        except Exception as e:
            log.error("store.record_daily_pnl_error", error=str(e))
    
    def record_circuit_breaker_event(
        self,
        state: str,
        reason: str = "",
        daily_pnl: float = 0.0,
        equity: float = 0.0,
    ) -> None:
        """Record a circuit breaker state change."""
        try:
            self._conn.execute(
                """INSERT INTO circuit_breaker_events
                   (timestamp, state, reason, daily_pnl, equity)
                   VALUES (?, ?, ?, ?, ?)""",
                (datetime.utcnow().isoformat(), state, reason, daily_pnl, equity),
            )
            self._conn.commit()
        except Exception as e:
            log.error("store.record_cb_event_error", error=str(e))
    
    def record_regime_transition(self, from_regime: Optional[str], to_regime: str) -> None:
        """Record a market regime change."""
        try:
            self._conn.execute(
                """INSERT INTO regime_transitions (timestamp, from_regime, to_regime)
                   VALUES (?, ?, ?)""",
                (datetime.utcnow().isoformat(), from_regime, to_regime),
            )
            self._conn.commit()
        except Exception as e:
            log.error("store.record_regime_error", error=str(e))
    
    def get_symbol_win_rate(self, symbol: str, days: int = 30) -> Optional[float]:
        """Get win rate for a symbol over the last N days."""
        try:
            rows = self._conn.execute(
                """SELECT SUM(wins) as total_wins, SUM(trades) as total_trades
                   FROM symbol_stats WHERE symbol = ?
                   AND date >= date('now', ?)""",
                (symbol, f"-{days} days"),
            ).fetchone()
            if rows and rows["total_trades"] and rows["total_trades"] > 0:
                return round(rows["total_wins"] / rows["total_trades"] * 100, 1)
        except Exception:
            pass
        return None
    
    def get_equity_curve(self, days: int = 90) -> list[dict]:
        """Get daily P&L history for equity curve."""
        try:
            rows = self._conn.execute(
                """SELECT date, starting_equity, ending_equity, total_pnl,
                   total_trades, regime FROM daily_pnl
                   WHERE date >= date('now', ?)
                   ORDER BY date""",
                (f"-{days} days",),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
    
    def _update_symbol_stats(self, symbol: str, date: str, pnl: float, hold_minutes: float) -> None:
        """Update per-symbol daily stats."""
        try:
            existing = self._conn.execute(
                "SELECT * FROM symbol_stats WHERE symbol = ? AND date = ?",
                (symbol, date),
            ).fetchone()
            
            if existing:
                trades = existing["trades"] + 1
                wins = existing["wins"] + (1 if pnl > 0 else 0)
                losses = existing["losses"] + (1 if pnl <= 0 else 0)
                total_pnl = existing["total_pnl"] + pnl
                avg_hold = ((existing["avg_hold_minutes"] or 0) * existing["trades"] + hold_minutes) / trades
                self._conn.execute(
                    """UPDATE symbol_stats SET trades=?, wins=?, losses=?, total_pnl=?, avg_hold_minutes=?
                       WHERE symbol=? AND date=?""",
                    (trades, wins, losses, total_pnl, avg_hold, symbol, date),
                )
            else:
                self._conn.execute(
                    """INSERT INTO symbol_stats (symbol, date, trades, wins, losses, total_pnl, avg_hold_minutes)
                       VALUES (?, ?, 1, ?, ?, ?, ?)""",
                    (symbol, date, 1 if pnl > 0 else 0, 1 if pnl <= 0 else 0, pnl, hold_minutes),
                )
            self._conn.commit()
        except Exception as e:
            log.error("store.update_symbol_stats_error", error=str(e))
    
    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
