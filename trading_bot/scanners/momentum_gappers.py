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
from trading_bot.utils.helpers import now_et, is_market_open, MARKET_OPEN, MARKET_CLOSE

log = structlog.get_logger(__name__)

# Total regular market minutes (9:30 AM - 4:00 PM ET)
_MARKET_MINUTES = (
    MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
    - MARKET_OPEN.hour * 60 - MARKET_OPEN.minute
)  # 390


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

        # Candidate persistence: track how many consecutive scans
        # each symbol has appeared in. Repeated appearances = building
        # momentum = higher conviction.
        self._appearance_count: dict[str, int] = {}
        self._last_scan_symbols: set[str] = set()

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
        before_price = set(s["symbol"] for s in raw_gainers)
        candidates = self._filter_price(raw_gainers)
        after_price = set(s["symbol"] for s in candidates)
        dropped_price = before_price - after_price
        log.info(
            "scanner.after_price_filter",
            count=len(candidates),
            dropped=len(dropped_price),
            dropped_symbols=sorted(dropped_price)[:10] if dropped_price else [],
        )

        # Step 3: Gap filter
        before_gap = set(s["symbol"] for s in candidates)
        candidates = self._filter_gap(candidates)
        after_gap = set(s["symbol"] for s in candidates)
        dropped_gap = before_gap - after_gap
        log.info(
            "scanner.after_gap_filter",
            count=len(candidates),
            dropped=len(dropped_gap),
            dropped_symbols=sorted(dropped_gap)[:10] if dropped_gap else [],
        )

        # Step 4: Float filter
        before_float = set(s["symbol"] for s in candidates)
        candidates = self._filter_float(candidates)
        after_float = set(s["symbol"] for s in candidates)
        dropped_float = before_float - after_float
        log.info(
            "scanner.after_float_filter",
            count=len(candidates),
            dropped=len(dropped_float),
            dropped_symbols=sorted(dropped_float)[:10] if dropped_float else [],
        )

        # Step 5: Relative volume filter
        before_vol = set(s["symbol"] for s in candidates)
        candidates = self._filter_volume(candidates)
        after_vol = set(s["symbol"] for s in candidates)
        dropped_vol = before_vol - after_vol
        log.info(
            "scanner.after_volume_filter",
            count=len(candidates),
            dropped=len(dropped_vol),
            tod_factor=round(self._time_of_day_factor(), 3),
            dropped_symbols=sorted(dropped_vol)[:10] if dropped_vol else [],
        )

        # Step 6: Update persistence tracking
        current_symbols = {c["symbol"] for c in candidates}
        for sym in current_symbols:
            if sym in self._last_scan_symbols:
                self._appearance_count[sym] = self._appearance_count.get(sym, 1) + 1
            else:
                self._appearance_count[sym] = 1
        # Decay symbols that disappeared
        for sym in list(self._appearance_count):
            if sym not in current_symbols:
                self._appearance_count[sym] = max(0, self._appearance_count[sym] - 1)
                if self._appearance_count[sym] == 0:
                    del self._appearance_count[sym]
        self._last_scan_symbols = current_symbols

        # Step 7: Build ScanResults with catalyst check, scoring, and persistence boost
        results = []
        for c in candidates:
            catalyst = self._news.find_catalyst(c["symbol"])
            base_score = self._compute_score(
                gap_pct=c["change_pct"],
                relative_volume=c.get("relative_volume", 1.0),
                float_shares=c.get("float_shares"),
                has_catalyst=catalyst is not None,
            )

            # Persistence boost: repeated appearances = building momentum
            appearances = self._appearance_count.get(c["symbol"], 1)
            if appearances >= 5:
                persistence_boost = 1.15  # +15% for 5+ consecutive scans
            elif appearances >= 3:
                persistence_boost = 1.10  # +10% for 3+ scans
            elif appearances >= 2:
                persistence_boost = 1.05  # +5% for 2+ scans
            else:
                persistence_boost = 1.0

            score = round(min(base_score * persistence_boost, 1.0), 4)

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

        # Step 8: Sort by score (descending) and limit
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
        Fetch top gainers by merging ALL available data sources.

        Unlike a cascade (try one, fall back to next), this always pulls
        from every working source and merges results. More raw candidates
        = more chances to find the needle in the haystack.

        Sources (all attempted in parallel):
          1. Polygon snapshot (paid plan)
          2. Alpaca movers (free)
          3. Alpaca most-active (free, catches high-volume plays)
          4. Yahoo Finance day gainers (free, no API key)
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

        # Source 2: Alpaca movers (always try — free with Alpaca keys)
        if self._fallback is not None:
            try:
                alpaca_gainers = self._fallback.get_gainers()
                if alpaca_gainers:
                    log.info("scanner.source_alpaca_movers", count=len(alpaca_gainers))
                    gainers.extend(alpaca_gainers)
            except Exception as e:
                log.warning("scanner.alpaca_movers_failed", error=str(e)[:120])

        # Source 3: Alpaca most-active (always try — catches volume plays)
        if self._fallback is not None and hasattr(self._fallback, "get_most_active"):
            try:
                active_gainers = self._fallback.get_most_active()
                if active_gainers:
                    log.info("scanner.source_alpaca_active", count=len(active_gainers))
                    gainers.extend(active_gainers)
            except Exception as e:
                log.warning("scanner.alpaca_active_failed", error=str(e)[:120])

        # Source 4: Yahoo Finance (always try — broadest coverage)
        if self._yahoo is not None:
            try:
                yahoo_gainers = self._yahoo.get_gainers()
                if yahoo_gainers:
                    log.info("scanner.source_yahoo", count=len(yahoo_gainers))
                    gainers.extend(yahoo_gainers)
            except Exception as e:
                log.warning("scanner.yahoo_failed", error=str(e)[:120])

        # Deduplicate by symbol (keep first occurrence — priority order)
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
        tod_factor = self._time_of_day_factor()
        results = []
        for s in snapshots:
            avg_vol = self._data.get_avg_volume(s["symbol"])
            current_vol = s.get("volume", 0)

            if current_vol > 0 and avg_vol > 0:
                rvol = self._compute_rvol(current_vol, avg_vol, tod_factor)
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
                    raw_rvol=round(current_vol / avg_vol, 2) if avg_vol > 0 else 0,
                    current_vol=current_vol,
                    avg_vol=int(avg_vol),
                    tod_factor=round(tod_factor, 3),
                )

        return results

    @staticmethod
    def _time_of_day_factor() -> float:
        """
        Fraction of the trading day elapsed (0.0–1.0).

        Used to normalize intraday volume against full-day averages.
        At 9:30 AM this returns ~0.003 (1 minute / 390 minutes).
        At 4:00 PM this returns 1.0.
        Outside market hours returns 1.0 (no normalization).
        """
        if not is_market_open():
            return 1.0

        now = now_et()
        current_minutes = now.hour * 60 + now.minute
        open_minutes = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = max(1, current_minutes - open_minutes)
        return min(elapsed / _MARKET_MINUTES, 1.0)

    @staticmethod
    def _compute_rvol(
        current_vol: int, avg_daily_vol: float, tod_factor: float
    ) -> float:
        """
        Compute relative volume normalized by time of day.

        Without normalization, at 9:58 AM only ~7% of daily volume has
        accumulated, making rvol appear artificially low (0.07x instead
        of the true 2x+). This projects the current volume to a full-day
        equivalent before comparing to the average.

        Example:
          - 10:00 AM, stock has 200K volume, avg daily = 1M
          - Raw rvol = 200K / 1M = 0.2x (WRONG — would be filtered out)
          - tod_factor = 30min / 390min = 0.077
          - Projected daily = 200K / 0.077 = 2.6M
          - Normalized rvol = 2.6M / 1M = 2.6x (CORRECT — passes filter)
        """
        if avg_daily_vol <= 0:
            return 0.0
        projected_daily = current_vol / tod_factor if tod_factor > 0 else current_vol
        return projected_daily / avg_daily_vol

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
