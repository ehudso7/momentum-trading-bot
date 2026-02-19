"""
Yahoo Finance day-gainers screener (no API key required).

Uses Yahoo Finance's public screener API to fetch top gaining stocks.
Acts as a last-resort fallback when both Polygon and Alpaca screener
endpoints fail or return empty data.
"""

from __future__ import annotations

import structlog
import requests

from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)

_TIMEOUT = (5, 15)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
_SCREENER_URL = "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"


class YahooScreener:
    """
    Fetches day gainers from Yahoo Finance's public screener API.

    No API key needed. Returns data in the same dict format as
    PolygonClient.get_gainers() / AlpacaScreener.get_gainers().
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._crumb: str | None = None

    def get_gainers(self) -> list[dict]:
        """
        Fetch today's top gaining stocks from Yahoo Finance.

        Returns list of dicts with keys:
        symbol, price, change_pct, volume, prev_close, day_open,
        day_high, day_low, vwap.
        """
        try:
            return self._get_gainers_impl()
        except Exception as e:
            log.warning("yahoo_screener.failed", error=str(e)[:200])
            return []

    @retry_with_backoff(max_retries=2, base_delay=2.0, max_delay=15.0)
    def _get_gainers_impl(self) -> list[dict]:
        # Get a crumb for authentication if we don't have one
        if not self._crumb:
            self._fetch_crumb()

        params = {
            "scrIds": "day_gainers",
            "count": 50,
            "formatted": "false",
        }
        if self._crumb:
            params["crumb"] = self._crumb

        resp = self._session.get(
            _SCREENER_URL,
            params=params,
            timeout=_TIMEOUT,
        )

        # If we get a 401, clear crumb and retry
        if resp.status_code == 401:
            self._crumb = None
            self._fetch_crumb()
            if self._crumb:
                params["crumb"] = self._crumb
            resp = self._session.get(
                _SCREENER_URL,
                params=params,
                timeout=_TIMEOUT,
            )

        resp.raise_for_status()
        data = resp.json()

        # Navigate Yahoo's nested response structure
        finance = data.get("finance", {})
        results_list = finance.get("result", [])
        if not results_list:
            log.info("yahoo_screener.empty_result")
            return []

        quotes = results_list[0].get("quotes", [])
        log.info("yahoo_screener.raw_quotes", count=len(quotes))

        results = []
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or "." in symbol:
                # Skip non-US tickers (e.g., BRK.B handled separately)
                if symbol.count(".") > 0 and not symbol.endswith(".B"):
                    continue

            price = float(q.get("regularMarketPrice", 0.0) or 0.0)
            prev_close = float(
                q.get("regularMarketPreviousClose", 0.0) or 0.0
            )
            change_pct = float(
                q.get("regularMarketChangePercent", 0.0) or 0.0
            )
            volume = int(q.get("regularMarketVolume", 0) or 0)
            day_open = float(q.get("regularMarketOpen", 0.0) or 0.0)
            day_high = float(q.get("regularMarketDayHigh", 0.0) or 0.0)
            day_low = float(q.get("regularMarketDayLow", 0.0) or 0.0)

            results.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "change_pct": change_pct,
                    "volume": volume,
                    "prev_close": prev_close,
                    "day_open": day_open,
                    "day_high": day_high,
                    "day_low": day_low,
                    "vwap": 0.0,
                }
            )

        log.info("yahoo_screener.gainers", count=len(results))
        return results

    def _fetch_crumb(self) -> None:
        """Fetch a Yahoo Finance crumb for API authentication."""
        try:
            # First get cookies by visiting Yahoo Finance
            self._session.get(
                "https://finance.yahoo.com",
                timeout=_TIMEOUT,
            )
            # Then get the crumb
            resp = self._session.get(_CRUMB_URL, timeout=_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                self._crumb = resp.text.strip()
                log.info("yahoo_screener.crumb_acquired")
        except Exception as e:
            log.debug("yahoo_screener.crumb_failed", error=str(e)[:100])
            self._crumb = None
