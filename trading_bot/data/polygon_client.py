"""
Polygon.io REST API wrapper with retry logic and rate limiting.

Provides access to market snapshots (gainers), ticker details,
historical aggregates, and news for catalyst detection.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import structlog
from polygon import RESTClient
from polygon.rest.models import TickerSnapshot

from trading_bot.config.settings import DataConfig
from trading_bot.utils.resilience import RateLimiter, retry_with_backoff

log = structlog.get_logger(__name__)


class PolygonClient:
    """Wrapper around polygon-io/client-python RESTClient with resilience."""

    def __init__(self, config: DataConfig):
        api_key = config.polygon_api_key.get_secret_value()
        if not api_key or api_key == "your_polygon_api_key_here":
            log.warning("polygon.no_api_key", msg="Polygon API key not configured")
            self._client: Optional[RESTClient] = None
        else:
            self._client = RESTClient(api_key=api_key)

        # Rate limiter: Polygon free plan = 5 calls/min
        self._rate_limiter = RateLimiter(max_calls_per_minute=5)

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    def _rate_limited_call(self, func, *args, **kwargs):
        """Execute an API call with rate limiting."""
        self._rate_limiter.wait_if_needed()
        return func(*args, **kwargs)

    def get_gainers(self) -> list[dict]:
        """
        Get top gainers via snapshot API.

        Returns list of dicts with keys:
        symbol, price, change_pct, volume, prev_close, day_open, day_high, day_low.

        Note: Free plan returns top 20 gainers.
        """
        if not self._client:
            log.warning("polygon.not_configured")
            return []

        return self._get_gainers_impl()

    @retry_with_backoff(max_retries=3, base_delay=2.0, max_delay=30.0)
    def _get_gainers_impl(self) -> list[dict]:
        snapshots = self._rate_limited_call(
            self._client.get_snapshot_direction,
            "stocks", "gainers", include_otc=False,
        )
        results = []
        for snap in snapshots:
            if not isinstance(snap, TickerSnapshot):
                continue
            if not snap.ticker or not snap.day:
                continue

            prev_close = snap.prev_day.close if snap.prev_day else 0.0
            price = snap.day.close if snap.day.close else 0.0
            change_pct = (
                ((price - prev_close) / prev_close * 100)
                if prev_close > 0
                else 0.0
            )

            results.append(
                {
                    "symbol": snap.ticker,
                    "price": price,
                    "change_pct": change_pct,
                    "volume": snap.day.volume or 0,
                    "prev_close": prev_close,
                    "day_open": snap.day.open or 0.0,
                    "day_high": snap.day.high or 0.0,
                    "day_low": snap.day.low or 0.0,
                    "vwap": snap.day.vwap or 0.0,
                }
            )

        log.info("polygon.gainers", count=len(results))
        return results

    def get_snapshot(self, symbol: str) -> Optional[dict]:
        """
        Get a single ticker snapshot.

        Returns dict with current price, volume, day bar, prev day data.
        """
        if not self._client:
            return None

        try:
            return self._get_snapshot_impl(symbol)
        except Exception as e:
            log.error("polygon.snapshot_error", symbol=symbol, error=str(e))
            return None

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=15.0)
    def _get_snapshot_impl(self, symbol: str) -> Optional[dict]:
        snap = self._rate_limited_call(
            self._client.get_snapshot_ticker, "stocks", symbol,
        )
        if not snap or not snap.day:
            return None

        prev_close = snap.prev_day.close if snap.prev_day else 0.0
        price = snap.day.close if snap.day.close else 0.0

        # Extract update timestamp for staleness detection
        updated_ns = getattr(snap, "updated", None)
        updated_dt = None
        if updated_ns:
            try:
                # Polygon returns nanosecond epoch timestamp
                updated_dt = datetime.utcfromtimestamp(updated_ns / 1e9)
            except (TypeError, ValueError, OSError):
                pass

        return {
            "symbol": snap.ticker,
            "price": price,
            "volume": snap.day.volume or 0,
            "prev_close": prev_close,
            "day_open": snap.day.open or 0.0,
            "day_high": snap.day.high or 0.0,
            "day_low": snap.day.low or 0.0,
            "vwap": snap.day.vwap or 0.0,
            "updated": updated_dt,
        }

    def get_ticker_details(self, symbol: str) -> Optional[dict]:
        """
        Get ticker details (v3): shares outstanding, market cap, SIC code.

        Note: Polygon does NOT provide float shares directly.
        Use yfinance for floatShares.
        """
        if not self._client:
            return None

        try:
            return self._get_ticker_details_impl(symbol)
        except Exception as e:
            log.error("polygon.details_error", symbol=symbol, error=str(e))
            return None

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=15.0)
    def _get_ticker_details_impl(self, symbol: str) -> Optional[dict]:
        details = self._rate_limited_call(
            self._client.get_ticker_details, symbol,
        )
        if not details:
            return None

        return {
            "symbol": symbol,
            "name": getattr(details, "name", ""),
            "market_cap": getattr(details, "market_cap", None),
            "shares_outstanding": getattr(
                details, "share_class_shares_outstanding", None
            ),
            "weighted_shares_outstanding": getattr(
                details, "weighted_shares_outstanding", None
            ),
            "sic_code": getattr(details, "sic_code", None),
            "primary_exchange": getattr(details, "primary_exchange", None),
        }

    def get_aggregates(
        self,
        symbol: str,
        multiplier: int = 1,
        timespan: str = "minute",
        from_date: str = "",
        to_date: str = "",
        limit: int = 500,
    ) -> pd.DataFrame:
        """
        Get historical OHLCV bars.

        Args:
            symbol: Ticker symbol.
            multiplier: Bar size multiplier.
            timespan: "minute", "hour", "day".
            from_date: Start date (YYYY-MM-DD).
            to_date: End date (YYYY-MM-DD).
            limit: Max bars to return.

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume, vwap.
        """
        if not self._client:
            return pd.DataFrame()

        if not from_date:
            from_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")

        try:
            return self._get_aggregates_impl(
                symbol, multiplier, timespan, from_date, to_date, limit
            )
        except Exception as e:
            log.error("polygon.aggregates_error", symbol=symbol, error=str(e))
            return pd.DataFrame()

    @retry_with_backoff(max_retries=3, base_delay=2.0, max_delay=30.0)
    def _get_aggregates_impl(
        self,
        symbol: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        limit: int,
    ) -> pd.DataFrame:
        aggs = list(
            self._rate_limited_call(
                self._client.list_aggs,
                ticker=symbol,
                multiplier=multiplier,
                timespan=timespan,
                from_=from_date,
                to=to_date,
                limit=limit,
            )
        )

        if not aggs:
            return pd.DataFrame()

        records = []
        for agg in aggs:
            records.append(
                {
                    "timestamp": pd.Timestamp(agg.timestamp, unit="ms", tz="UTC"),
                    "open": agg.open,
                    "high": agg.high,
                    "low": agg.low,
                    "close": agg.close,
                    "volume": agg.volume,
                    "vwap": getattr(agg, "vwap", None),
                }
            )

        df = pd.DataFrame(records)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    def get_news(self, symbol: str, limit: int = 10) -> list[dict]:
        """
        Get recent news articles for a ticker.

        Returns list of dicts with: title, description, published_utc, url.
        """
        if not self._client:
            return []

        try:
            return self._get_news_impl(symbol, limit)
        except Exception as e:
            log.error("polygon.news_error", symbol=symbol, error=str(e))
            return []

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=15.0)
    def _get_news_impl(self, symbol: str, limit: int) -> list[dict]:
        news_items = list(
            self._rate_limited_call(
                self._client.list_ticker_news,
                ticker=symbol,
                limit=limit,
                order="desc",
                sort="published_utc",
            )
        )

        results = []
        for item in news_items:
            results.append(
                {
                    "title": getattr(item, "title", ""),
                    "description": getattr(item, "description", ""),
                    "published_utc": getattr(item, "published_utc", ""),
                    "url": getattr(item, "article_url", ""),
                    "source": getattr(item, "publisher", {}).get("name", "")
                    if isinstance(getattr(item, "publisher", None), dict)
                    else "",
                }
            )

        return results
