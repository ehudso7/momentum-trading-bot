"""
Unified market data interface with retry logic.

Provides an abstract base class and two implementations:
- LiveMarketData: Polygon.io for real-time + yfinance for float data.
- BacktestMarketData: yfinance only for historical backtesting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import structlog
import yfinance as yf

from trading_bot.data.polygon_client import PolygonClient
from trading_bot.utils.helpers import now_et
from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)


class MarketDataProvider(ABC):
    """Abstract interface for market data access."""

    @abstractmethod
    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the most recent price for a symbol."""
        ...

    @abstractmethod
    def get_intraday_bars(
        self, symbol: str, interval_minutes: int = 1, lookback_bars: int = 100
    ) -> pd.DataFrame:
        """
        Get recent intraday OHLCV bars.

        Returns DataFrame with columns: open, high, low, close, volume.
        Index should be DatetimeIndex.
        """
        ...

    @abstractmethod
    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Get daily OHLCV bars for the last N days."""
        ...

    @abstractmethod
    def get_float_shares(self, symbol: str) -> Optional[int]:
        """Get the public float (tradable shares) for a symbol."""
        ...

    @abstractmethod
    def get_shares_outstanding(self, symbol: str) -> Optional[int]:
        """Get total shares outstanding."""
        ...

    @abstractmethod
    def get_avg_volume(self, symbol: str, days: int = 20) -> float:
        """Get average daily volume over the last N days."""
        ...


class LiveMarketData(MarketDataProvider):
    """
    Live market data using Polygon.io for real-time and yfinance for float.

    Float data is cached for 24 hours since it changes infrequently.
    """

    def __init__(self, polygon_client: PolygonClient, float_cache_hours: int = 24):
        self._polygon = polygon_client
        self._float_cache: dict[str, Optional[int]] = {}
        self._cache_timestamps: dict[str, datetime] = {}
        self._float_cache_hours = float_cache_hours
        self._avg_volume_cache: dict[str, float] = {}
        self._avg_volume_cache_time: dict[str, datetime] = {}

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price from Polygon snapshot with yfinance fallback."""
        snap = self._polygon.get_snapshot(symbol)
        if snap:
            return snap["price"]
        # Fallback to yfinance
        return self._yf_current_price(symbol)

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _yf_current_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            log.error("market_data.price_fallback_error", symbol=symbol, error=str(e))
        return None

    def get_intraday_bars(
        self, symbol: str, interval_minutes: int = 1, lookback_bars: int = 100
    ) -> pd.DataFrame:
        """
        Get intraday bars from Polygon aggregates, with yfinance fallback.

        If 1-minute bars fail from both sources, tries 5-minute bars as
        a last resort (wider availability, still usable for strategy).
        """
        today_et = now_et().date()
        from_date = (today_et - timedelta(days=2)).strftime("%Y-%m-%d")
        to_date = today_et.strftime("%Y-%m-%d")

        df = self._polygon.get_aggregates(
            symbol=symbol,
            multiplier=interval_minutes,
            timespan="minute",
            from_date=from_date,
            to_date=to_date,
            limit=lookback_bars,
        )

        if df.empty:
            # Fallback 1: yfinance 1-minute bars
            log.info("market_data.intraday_fallback_yf", symbol=symbol)
            df = self._yf_intraday(symbol, interval_minutes, lookback_bars)

        if df.empty and interval_minutes == 1:
            # Fallback 2: try 5-minute bars (much wider availability)
            log.info("market_data.intraday_fallback_5min", symbol=symbol)
            df = self._yf_intraday(symbol, 5, lookback_bars)
            if df.empty:
                # Fallback 3: try 2-minute bars
                df = self._yf_intraday(symbol, 2, lookback_bars)

        if df.empty:
            log.warning(
                "market_data.intraday_all_failed",
                symbol=symbol,
                detail="No intraday data available from any source",
            )

        return df.tail(lookback_bars) if not df.empty else df

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _yf_intraday(
        self, symbol: str, interval_minutes: int, lookback_bars: int
    ) -> pd.DataFrame:
        try:
            ticker = yf.Ticker(symbol)
            interval = f"{interval_minutes}m"
            hist = ticker.history(period="5d", interval=interval)
            if not hist.empty:
                hist.columns = [c.lower() for c in hist.columns]
                return hist.tail(lookback_bars)
        except Exception as e:
            log.warning(
                "market_data.yf_intraday_error",
                symbol=symbol,
                interval=f"{interval_minutes}m",
                error=str(e)[:120],
            )
        return pd.DataFrame()

    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Get daily bars from Polygon or yfinance."""
        from_date = (datetime.utcnow() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
        to_date = datetime.utcnow().strftime("%Y-%m-%d")

        df = self._polygon.get_aggregates(
            symbol=symbol,
            multiplier=1,
            timespan="day",
            from_date=from_date,
            to_date=to_date,
            limit=days,
        )

        if df.empty:
            df = self._yf_daily(symbol, days)

        return df

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _yf_daily(self, symbol: str, days: int) -> pd.DataFrame:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=f"{days}d")
            if not hist.empty:
                hist.columns = [c.lower() for c in hist.columns]
                return hist
        except Exception as e:
            log.error(
                "market_data.daily_fallback_error", symbol=symbol, error=str(e)
            )
        return pd.DataFrame()

    def get_float_shares(self, symbol: str) -> Optional[int]:
        """
        Get float shares from yfinance (Polygon lacks this field).

        Cached for float_cache_hours (default 24h) since float
        only changes on offerings, buybacks, or lockup expirations.
        """
        # Check cache
        if symbol in self._float_cache:
            cache_time = self._cache_timestamps.get(symbol, datetime.min)
            if (datetime.utcnow() - cache_time).total_seconds() < (
                self._float_cache_hours * 3600
            ):
                return self._float_cache[symbol]

        float_shares = self._yf_float_shares(symbol)
        if float_shares is not None:
            self._float_cache[symbol] = float_shares
            self._cache_timestamps[symbol] = datetime.utcnow()
            return float_shares

        # Fallback: shares outstanding from Polygon
        details = self._polygon.get_ticker_details(symbol)
        if details and details.get("shares_outstanding"):
            shares = int(details["shares_outstanding"])
            self._float_cache[symbol] = shares
            self._cache_timestamps[symbol] = datetime.utcnow()
            return shares

        self._float_cache[symbol] = None
        self._cache_timestamps[symbol] = datetime.utcnow()
        return None

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _yf_float_shares(self, symbol: str) -> Optional[int]:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            float_shares = info.get("floatShares")
            if float_shares:
                return int(float_shares)
        except Exception as e:
            log.error("market_data.float_error", symbol=symbol, error=str(e))
        return None

    def get_shares_outstanding(self, symbol: str) -> Optional[int]:
        """Get shares outstanding from Polygon ticker details."""
        details = self._polygon.get_ticker_details(symbol)
        if details and details.get("shares_outstanding"):
            return int(details["shares_outstanding"])
        return None

    def get_avg_volume(self, symbol: str, days: int = 20) -> float:
        """Get average daily volume over last N days. Cached for 4 hours."""
        if symbol in self._avg_volume_cache:
            cache_time = self._avg_volume_cache_time.get(symbol, datetime.min)
            if (datetime.utcnow() - cache_time).total_seconds() < 14400:  # 4 hours
                return self._avg_volume_cache[symbol]

        df = self.get_daily_bars(symbol, days=days)
        if df.empty:
            return 0.0

        avg_vol = float(df["volume"].mean())
        self._avg_volume_cache[symbol] = avg_vol
        self._avg_volume_cache_time[symbol] = datetime.utcnow()
        return avg_vol


class BacktestMarketData(MarketDataProvider):
    """
    Historical market data provider for backtesting.

    Uses yfinance exclusively. Caches downloaded data.
    """

    def __init__(self, start_date: str, end_date: str):
        self._start = start_date
        self._end = end_date
        self._data_cache: dict[str, pd.DataFrame] = {}
        self._float_cache: dict[str, Optional[int]] = {}

    def _get_cached_data(self, symbol: str) -> pd.DataFrame:
        """Download and cache historical data for a symbol."""
        if symbol not in self._data_cache:
            self._data_cache[symbol] = self._download_data(symbol)
        return self._data_cache[symbol]

    @retry_with_backoff(max_retries=3, base_delay=2.0, max_delay=30.0)
    def _download_data(self, symbol: str) -> pd.DataFrame:
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=self._start, end=self._end)
            if not df.empty:
                df.columns = [c.lower() for c in df.columns]
            return df
        except Exception as e:
            log.error("backtest_data.download_error", symbol=symbol, error=str(e))
            return pd.DataFrame()

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the last available price in the backtest data."""
        df = self._get_cached_data(symbol)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

    def get_intraday_bars(
        self, symbol: str, interval_minutes: int = 1, lookback_bars: int = 100
    ) -> pd.DataFrame:
        """
        Get intraday bars for backtesting.

        Note: yfinance intraday data is limited to ~60 days of history
        for 1-minute bars. For backtesting, we use daily bars as a proxy.
        """
        df = self._get_cached_data(symbol)
        return df.tail(lookback_bars) if not df.empty else df

    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        df = self._get_cached_data(symbol)
        return df.tail(days) if not df.empty else df

    def get_float_shares(self, symbol: str) -> Optional[int]:
        if symbol in self._float_cache:
            return self._float_cache[symbol]

        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            float_shares = info.get("floatShares")
            if float_shares:
                self._float_cache[symbol] = int(float_shares)
                return int(float_shares)
        except Exception as e:
            log.error("backtest_data.float_error", symbol=symbol, error=str(e))

        self._float_cache[symbol] = None
        return None

    def get_shares_outstanding(self, symbol: str) -> Optional[int]:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return info.get("sharesOutstanding")
        except Exception:
            return None

    def get_avg_volume(self, symbol: str, days: int = 20) -> float:
        df = self._get_cached_data(symbol)
        if df.empty:
            return 0.0
        return float(df["volume"].tail(days).mean())
