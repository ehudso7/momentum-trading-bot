"""
Alpaca Data API screener for finding top gaining stocks.

Uses Alpaca's free screener/movers endpoint as a fallback when Polygon's
snapshot API requires a paid plan. Reuses the same Alpaca API keys
configured for the broker.
"""

from __future__ import annotations

import structlog
import requests

from trading_bot.config.settings import BrokerConfig
from trading_bot.utils.resilience import retry_with_backoff

log = structlog.get_logger(__name__)

_ALPACA_DATA_URL = "https://data.alpaca.markets"
_TIMEOUT = (5, 10)


class AlpacaScreener:
    """
    Fetches top gainers using Alpaca's free market data screener API.

    Returns data in the same dict format as PolygonClient.get_gainers()
    so the MomentumGapperScanner can use either source interchangeably.
    """

    def __init__(self, config: BrokerConfig) -> None:
        api_key = config.alpaca_api_key.get_secret_value()
        api_secret = config.alpaca_api_secret.get_secret_value()

        if not api_key or api_key == "your_alpaca_api_key_here":
            self._configured = False
            log.warning("alpaca_screener.no_api_key")
        else:
            self._configured = True
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": api_secret,
                }
            )

    @property
    def is_configured(self) -> bool:
        return self._configured

    def get_gainers(self) -> list[dict]:
        """
        Get top gainers via Alpaca screener/movers endpoint.

        Returns list of dicts with keys matching PolygonClient.get_gainers():
        symbol, price, change_pct, volume, prev_close, day_open, day_high,
        day_low, vwap.

        Fields not available from the movers endpoint (day_open, day_high,
        day_low, vwap) are set to 0.0. The scanner's filter pipeline only
        requires symbol, price, change_pct, volume, and prev_close.
        """
        if not self._configured:
            log.warning("alpaca_screener.not_configured")
            return []

        return self._get_gainers_impl()

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _get_gainers_impl(self) -> list[dict]:
        resp = self._session.get(
            f"{_ALPACA_DATA_URL}/v1beta1/screener/stocks/movers",
            params={"top": 50},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        gainers = data.get("gainers", [])
        results = []

        for g in gainers:
            symbol = g.get("symbol", "")
            price = float(g.get("price", 0.0))
            change = float(g.get("change", 0.0))
            percent_change = float(g.get("percent_change", 0.0))
            volume = int(g.get("volume", 0))

            prev_close = round(price - change, 4) if change else 0.0

            results.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "change_pct": percent_change,
                    "volume": volume,
                    "prev_close": prev_close,
                    "day_open": 0.0,
                    "day_high": 0.0,
                    "day_low": 0.0,
                    "vwap": 0.0,
                }
            )

        log.info("alpaca_screener.gainers", count=len(results))
        return results
