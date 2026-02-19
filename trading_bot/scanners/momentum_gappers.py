"""
Pre-market and intraday momentum gapper scanner.

Scans for low-float stocks with large gap-ups, high relative volume,
and catalyst presence. Returns ranked candidates for strategy evaluation.

Filter pipeline:
  1. Fetch top gainers from Polygon snapshot API
  2. Filter by price range ($2-$20)
  3. Filter by gap percentage (>=10%)
  4. Filter by float (<50M shares)
  5. Filter by relative volume (>=5x avg)
  6. Check for catalyst keywords in news
  7. Score and rank
  8. Return top N candidates
"""

from __future__ import annotations

from typing import Optional

import structlog

from trading_bot.config.settings import ScannerConfig
from trading_bot.data.market_data import MarketDataProvider
from trading_bot.data.news_client import NewsClient
from trading_bot.models.domain import ScanResult

log = structlog.get_logger(__name__)


class MomentumGapperScanner:
    """Scans for low-float momentum gappers meeting all filter criteria."""

    def __init__(
        self,
        market_data: MarketDataProvider,
        news_client: NewsClient,
        polygon_client,
        config: ScannerConfig,
        fallback_client=None,
        yahoo_client=None,
    ):
        self._data = market_data
        self._news = news_client
        self._polygon = polygon_client
        self._config = config
        self._fallback = fallback_client
        self._yahoo = yahoo_client

    def scan(self) -> list[ScanResult]:
        """
        Full scan pipeline. Returns ranked list of momentum candidates.

        Each step is a filter that reduces the candidate pool.
        Logged at each stage for debugging.
        """
        # Step 1: Fetch gainers (Polygon primary, Alpaca fallback)
        raw_gainers = self._fetch_gainers()
        log.info("scanner.raw_gainers", count=len(raw_gainers))

        if not raw_gainers:
            return []

        # Step 2: Price filter
        candidates = self._filter_price(raw_gainers)
        log.info(
            "scanner.after_price_filter",
            count=len(candidates),
            dropped=len(raw_gainers) - len(candidates),
        )

        # Step 3: Gap filter
        candidates = self._filter_gap(candidates)
        log.info(
            "scanner.after_gap_filter",
            count=len(candidates),
            dropped=len(raw_gainers) - len(candidates),
        )

        # Step 4: Float filter
        before_float = len(candidates)
        candidates = self._filter_float(candidates)
        log.info(
            "scanner.after_float_filter",
            count=len(candidates),
            dropped=before_float - len(candidates),
        )

        # Step 5: Relative volume filter
        before_vol = len(candidates)
        candidates = self._filter_volume(candidates)
        log.info(
            "scanner.after_volume_filter",
            count=len(candidates),
            dropped=before_vol - len(candidates),
        )

        # Step 6: Build ScanResults with catalyst check and scoring
        results = []
        for c in candidates:
            catalyst = self._news.find_catalyst(c["symbol"])
            score = self._compute_score(
                gap_pct=c["change_pct"],
                relative_volume=c.get("relative_volume", 1.0),
                float_shares=c.get("float_shares"),
                has_catalyst=catalyst is not None,
            )

            results.append(
                ScanResult(
                    symbol=c["symbol"],
                    price=c["price"],
                    gap_pct=c["change_pct"],
                    relative_volume=c.get("relative_volume", 1.0),
                    float_shares=c.get("float_shares"),
                    volume=c["volume"],
                    prev_close=c["prev_close"],
                    catalyst=catalyst,
                    score=score,
                )
            )

        # Step 7: Sort by score (descending) and limit
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[: self._config.max_candidates]

        log.info(
            "scanner.results",
            count=len(results),
            top=results[0].symbol if results else "none",
        )

        return results

    def _fetch_gainers(self) -> list[dict]:
        """
        Fetch top gainers from multiple sources with cascading fallbacks.

        Priority: Polygon → Alpaca movers → Yahoo Finance → Alpaca most-active.
        Combines results and deduplicates by symbol.
        """
        gainers: list[dict] = []

        # Source 1: Polygon snapshot (requires paid plan)
        try:
            polygon_gainers = self._polygon.get_gainers()
            if polygon_gainers:
                log.info("scanner.source_polygon", count=len(polygon_gainers))
                gainers.extend(polygon_gainers)
        except Exception as e:
            log.info("scanner.polygon_failed", error=str(e)[:120])

        # Source 2: Alpaca movers (free with Alpaca keys)
        if self._fallback is not None and not gainers:
            try:
                log.info("scanner.trying_alpaca_movers")
                alpaca_gainers = self._fallback.get_gainers()
                if alpaca_gainers:
                    log.info("scanner.source_alpaca_movers", count=len(alpaca_gainers))
                    gainers.extend(alpaca_gainers)
            except Exception as e:
                log.warning("scanner.alpaca_movers_failed", error=str(e)[:120])

        # Source 3: Yahoo Finance day gainers (no API key needed)
        if self._yahoo is not None and not gainers:
            try:
                log.info("scanner.trying_yahoo")
                yahoo_gainers = self._yahoo.get_gainers()
                if yahoo_gainers:
                    log.info("scanner.source_yahoo", count=len(yahoo_gainers))
                    gainers.extend(yahoo_gainers)
            except Exception as e:
                log.warning("scanner.yahoo_failed", error=str(e)[:120])

        # Source 4: Alpaca most-active (catches high-volume movers)
        if self._fallback is not None and not gainers:
            try:
                log.info("scanner.trying_alpaca_most_active")
                if hasattr(self._fallback, "get_most_active"):
                    active_gainers = self._fallback.get_most_active()
                    if active_gainers:
                        log.info(
                            "scanner.source_alpaca_active",
                            count=len(active_gainers),
                        )
                        gainers.extend(active_gainers)
            except Exception as e:
                log.warning("scanner.alpaca_active_failed", error=str(e)[:120])

        # Deduplicate by symbol (keep first occurrence)
        seen = set()
        unique = []
        for g in gainers:
            sym = g.get("symbol", "")
            if sym and sym not in seen:
                seen.add(sym)
                unique.append(g)

        if not unique:
            log.warning("scanner.all_sources_empty")

        return unique

    def _filter_price(self, snapshots: list[dict]) -> list[dict]:
        """Filter by price range [min_price, max_price]."""
        return [
            s
            for s in snapshots
            if self._config.min_price <= s.get("price", 0) <= self._config.max_price
        ]

    def _filter_gap(self, snapshots: list[dict]) -> list[dict]:
        """Filter by minimum gap percentage, reject overextended (>max_gap_pct)."""
        return [
            s
            for s in snapshots
            if self._config.min_gap_pct
            <= s.get("change_pct", 0)
            <= self._config.max_gap_pct
        ]

    def _filter_float(self, snapshots: list[dict]) -> list[dict]:
        """
        Filter by float < max_float_shares.

        Fetches float from market data provider (yfinance).
        Stocks where float cannot be determined are kept (benefit of doubt)
        but scored lower.
        """
        results = []
        for s in snapshots:
            float_shares = self._data.get_float_shares(s["symbol"])
            s["float_shares"] = float_shares

            if float_shares is None:
                # Keep but will score lower
                results.append(s)
                log.info(
                    "scanner.float_unknown",
                    symbol=s["symbol"],
                )
            elif float_shares <= self._config.max_float_shares:
                results.append(s)
            else:
                log.info(
                    "scanner.float_too_high",
                    symbol=s["symbol"],
                    float_shares=float_shares,
                    max_allowed=self._config.max_float_shares,
                )

        return results

    def _filter_volume(self, snapshots: list[dict]) -> list[dict]:
        """Filter by relative volume >= min_relative_volume."""
        results = []
        for s in snapshots:
            avg_vol = self._data.get_avg_volume(s["symbol"])
            current_vol = s.get("volume", 0)

            if current_vol > 0 and avg_vol > 0:
                rvol = current_vol / avg_vol
            elif current_vol == 0 and avg_vol > 0:
                # Volume data missing from screener — keep candidate
                # with benefit-of-doubt rvol (will score lower)
                rvol = self._config.min_relative_volume
                log.info(
                    "scanner.volume_unknown_keeping",
                    symbol=s["symbol"],
                    avg_vol=int(avg_vol),
                )
            else:
                rvol = 0.0

            s["relative_volume"] = rvol

            if rvol >= self._config.min_relative_volume:
                results.append(s)
            else:
                log.info(
                    "scanner.low_rvol",
                    symbol=s["symbol"],
                    rvol=round(rvol, 2),
                    current_vol=current_vol,
                    avg_vol=int(avg_vol),
                )

        return results

    def _compute_score(
        self,
        gap_pct: float,
        relative_volume: float,
        float_shares: Optional[int],
        has_catalyst: bool,
    ) -> float:
        """
        Compute a composite ranking score in [0, 1].

        Formula:
          score = (gap_score * 0.25)
                + (rvol_score * 0.30)
                + (float_score * 0.25)
                + (catalyst_bonus * 0.20)

        Higher is better.
        """
        # Gap score: normalize to [0, 1], cap at 100%
        gap_score = min(gap_pct / 100.0, 1.0)

        # Relative volume score: normalize, cap at 20x
        rvol_score = min(relative_volume / 20.0, 1.0)

        # Float score: lower float = higher score
        if float_shares is None:
            float_score = 0.3  # unknown gets middle-low score
        elif float_shares <= self._config.ideal_float_shares:
            float_score = 1.0  # ideal: < 20M
        elif float_shares <= self._config.max_float_shares:
            # Linear decay from 1.0 to 0.3 between ideal and max
            ratio = (float_shares - self._config.ideal_float_shares) / (
                self._config.max_float_shares - self._config.ideal_float_shares
            )
            float_score = 1.0 - (ratio * 0.7)
        else:
            float_score = 0.0

        # Catalyst bonus
        catalyst_bonus = 1.0 if has_catalyst else 0.0

        score = (
            gap_score * 0.25
            + rvol_score * 0.30
            + float_score * 0.25
            + catalyst_bonus * 0.20
        )

        return round(score, 4)
