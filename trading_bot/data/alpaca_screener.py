"""
Alpaca Data API screener for finding top gaining stocks.

Uses Alpaca's free screener/movers endpoint as a fallback when Polygon's
snapshot API requires a paid plan. Reuses the same Alpaca API keys
configured for the broker.

Enriches movers data with batch snapshots for volume, VWAP, and OHLC
when the movers endpoint doesn't include them.
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
        Get top gainers via Alpaca screener/movers endpoint, enriched
        with snapshot data for volume, VWAP, and OHLC.

        Returns list of dicts with keys matching PolygonClient.get_gainers():
        symbol, price, change_pct, volume, prev_close, day_open, day_high,
        day_low, vwap.
        """
        if not self._configured:
            log.warning("alpaca_screener.not_configured")
            return []

        results = self._get_gainers_impl()

        # Enrich with snapshot data if movers are missing volume
        if results:
            missing_volume = [r for r in results if r.get("volume", 0) == 0]
            if missing_volume:
                log.info(
                    "alpaca_screener.enriching_volume",
                    missing_count=len(missing_volume),
                    total=len(results),
                )
                self._enrich_with_snapshots(results)

        return results

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
        log.info(
            "alpaca_screener.raw_response",
            gainer_count=len(gainers),
            sample_keys=list(gainers[0].keys()) if gainers else [],
        )

        results = []

        for g in gainers:
            symbol = g.get("symbol", "")
            price = float(g.get("price", 0.0))
            change = float(g.get("change", 0.0))
            percent_change = float(g.get("percent_change", 0.0))
            volume = int(g.get("volume", 0) or 0)
            trade_count = int(g.get("trade_count", 0) or 0)

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
                    "trade_count": trade_count,
                }
            )

        log.info("alpaca_screener.gainers", count=len(results))
        return results

    def _enrich_with_snapshots(self, results: list[dict]) -> None:
        """
        Enrich gainer data with batch snapshots from Alpaca v2 API.

        Adds volume, VWAP, and OHLC from the daily bar snapshot.
        Modifies results in place.
        """
        symbols = [r["symbol"] for r in results]

        # Alpaca snapshots endpoint accepts up to ~200 symbols
        try:
            snapshots = self._get_batch_snapshots(symbols)
        except Exception as e:
            log.warning("alpaca_screener.snapshot_enrichment_failed", error=str(e)[:200])
            return

        enriched = 0
        for r in results:
            snap = snapshots.get(r["symbol"])
            if not snap:
                continue

            daily = snap.get("dailyBar") or snap.get("daily_bar") or {}
            prev = snap.get("prevDailyBar") or snap.get("prev_daily_bar") or {}

            if daily.get("v", 0) or daily.get("volume", 0):
                r["volume"] = int(daily.get("v", 0) or daily.get("volume", 0))
                enriched += 1
            if daily.get("o", 0) or daily.get("open", 0):
                r["day_open"] = float(daily.get("o", 0) or daily.get("open", 0))
            if daily.get("h", 0) or daily.get("high", 0):
                r["day_high"] = float(daily.get("h", 0) or daily.get("high", 0))
            if daily.get("l", 0) or daily.get("low", 0):
                r["day_low"] = float(daily.get("l", 0) or daily.get("low", 0))
            if daily.get("vw", 0) or daily.get("vwap", 0):
                r["vwap"] = float(daily.get("vw", 0) or daily.get("vwap", 0))

            # Use snapshot close as price if we have it and price was 0
            snap_price = daily.get("c", 0) or daily.get("close", 0)
            if snap_price and r["price"] == 0:
                r["price"] = float(snap_price)

            # Recalculate prev_close from snapshot if available
            prev_close = prev.get("c", 0) or prev.get("close", 0)
            if prev_close:
                r["prev_close"] = float(prev_close)

        log.info("alpaca_screener.enriched", count=enriched, total=len(results))

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _get_batch_snapshots(self, symbols: list[str]) -> dict:
        """
        Get batch snapshots from Alpaca v2 stocks API.

        Returns dict mapping symbol -> snapshot data.
        """
        # Process in batches of 50 to avoid URL length limits
        all_snapshots = {}
        for i in range(0, len(symbols), 50):
            batch = symbols[i : i + 50]
            resp = self._session.get(
                f"{_ALPACA_DATA_URL}/v2/stocks/snapshots",
                params={"symbols": ",".join(batch), "feed": "iex"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            all_snapshots.update(resp.json())

        return all_snapshots

    def get_most_active(self) -> list[dict]:
        """
        Get most active stocks by volume as an alternative data source.

        Returns list of dicts in the same format as get_gainers().
        """
        if not self._configured:
            return []

        try:
            return self._get_most_active_impl()
        except Exception as e:
            log.warning("alpaca_screener.most_active_failed", error=str(e)[:200])
            return []

    @retry_with_backoff(max_retries=2, base_delay=1.0, max_delay=10.0)
    def _get_most_active_impl(self) -> list[dict]:
        resp = self._session.get(
            f"{_ALPACA_DATA_URL}/v1beta1/screener/stocks/most-actives",
            params={"top": 50, "by": "volume"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        actives = data.get("most_actives", [])
        results = []

        for a in actives:
            symbol = a.get("symbol", "")
            volume = int(a.get("volume", 0) or 0)
            trade_count = int(a.get("trade_count", 0) or 0)

            results.append(
                {
                    "symbol": symbol,
                    "price": 0.0,
                    "change_pct": 0.0,
                    "volume": volume,
                    "prev_close": 0.0,
                    "day_open": 0.0,
                    "day_high": 0.0,
                    "day_low": 0.0,
                    "vwap": 0.0,
                    "trade_count": trade_count,
                }
            )

        # Enrich with snapshots to get price data
        if results:
            self._enrich_with_snapshots(results)

        # Calculate change_pct from enriched data
        for r in results:
            if r["prev_close"] > 0 and r["price"] > 0:
                r["change_pct"] = round(
                    (r["price"] - r["prev_close"]) / r["prev_close"] * 100, 2
                )

        log.info("alpaca_screener.most_active", count=len(results))
        return results
