"""
Correlation checker to avoid concentrated sector/price risk.

Prevents opening new positions that are highly correlated with
existing holdings, using both sector similarity (SIC code prefix)
and price return correlation over trailing daily bars.

Threshold: Pearson correlation > 0.7 = too correlated to add.
Results are cached for 1 hour to minimize redundant API calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import structlog

from trading_bot.data.market_data import MarketDataProvider

log = structlog.get_logger(__name__)

# SIC code prefix length used for sector similarity comparison.
# First 2 digits = major industry group (e.g., 28xx = Chemicals).
_SIC_PREFIX_LEN = 2

# Minimum number of overlapping daily bars required for a meaningful
# Pearson correlation calculation.
_MIN_BARS = 10

# Number of trailing trading days to use for return correlation.
_CORRELATION_LOOKBACK_DAYS = 20

# Correlation coefficient threshold: above this, symbols are
# considered too correlated to hold simultaneously.
_CORRELATION_THRESHOLD = 0.7

# Cache TTL for correlation results.
_CACHE_TTL = timedelta(hours=1)


class CorrelationChecker:
    """
    Checks whether a new position would be too correlated with
    existing holdings.

    Uses two signals:
    1. Sector similarity via SIC code prefix match (2-digit major group).
    2. Pearson correlation of daily returns over the last 20 trading days.

    Either signal exceeding its threshold is sufficient to flag the
    pair as correlated.
    """

    def __init__(
        self,
        market_data: MarketDataProvider,
        threshold: float = _CORRELATION_THRESHOLD,
    ):
        self._market_data = market_data
        self._threshold = threshold
        # Cache: (symbol_a, symbol_b) -> (correlation, timestamp)
        self._cache: dict[tuple[str, str], tuple[float, datetime]] = {}
        # SIC code cache: symbol -> sic_code or None
        self._sic_cache: dict[str, Optional[str]] = {}

    def is_correlated(
        self,
        new_symbol: str,
        existing_symbols: list[str],
        market_data: MarketDataProvider,
    ) -> bool:
        """
        Check if new_symbol is too correlated with any existing position.

        A symbol is considered correlated if:
        - It shares the same 2-digit SIC code prefix with any existing
          position, OR
        - Its daily return Pearson correlation with any existing position
          exceeds the threshold (default 0.7).

        Args:
            new_symbol: Ticker symbol of the candidate position.
            existing_symbols: Ticker symbols of currently held positions.
            market_data: Market data provider for daily bars and ticker info.

        Returns:
            True if the new symbol is too correlated with any existing
            position (should NOT trade). False if OK to trade.
        """
        if not existing_symbols:
            return False

        for existing in existing_symbols:
            # Same symbol is inherently correlated.
            if new_symbol.upper() == existing.upper():
                log.info(
                    "correlation.same_symbol",
                    symbol=new_symbol,
                )
                return True

            # 1. Sector similarity check via SIC code prefix.
            if self._sic_codes_match(new_symbol, existing, market_data):
                log.info(
                    "correlation.sector_match",
                    new_symbol=new_symbol,
                    existing_symbol=existing,
                )
                return True

            # 2. Price return correlation check.
            corr = self.get_correlation(new_symbol, existing, market_data)
            if corr > self._threshold:
                log.info(
                    "correlation.price_correlated",
                    new_symbol=new_symbol,
                    existing_symbol=existing,
                    correlation=round(corr, 4),
                    threshold=self._threshold,
                )
                return True

        return False

    def get_correlation(
        self,
        symbol_a: str,
        symbol_b: str,
        market_data: MarketDataProvider,
    ) -> float:
        """
        Compute Pearson correlation between two symbols' daily returns.

        Uses market_data.get_daily_bars() which returns a DataFrame with
        a 'close' column. Returns over the last 20 trading days are
        aligned by date index before computing correlation.

        Args:
            symbol_a: First ticker symbol.
            symbol_b: Second ticker symbol.
            market_data: Market data provider for daily bars.

        Returns:
            Pearson correlation coefficient in [-1, 1].
            Returns 0.0 if data is insufficient (fewer than 10
            overlapping bars) or on any computation error.
        """
        # Same symbol is perfectly correlated.
        if symbol_a.upper() == symbol_b.upper():
            return 1.0

        # Check cache (order-independent key).
        cache_key = self._cache_key(symbol_a, symbol_b)
        cached = self._cache.get(cache_key)
        if cached is not None:
            corr_value, cached_at = cached
            if datetime.utcnow() - cached_at < _CACHE_TTL:
                return corr_value

        # Fetch daily bars.
        try:
            bars_a = market_data.get_daily_bars(
                symbol_a, days=_CORRELATION_LOOKBACK_DAYS
            )
            bars_b = market_data.get_daily_bars(
                symbol_b, days=_CORRELATION_LOOKBACK_DAYS
            )
        except Exception as e:
            log.error(
                "correlation.data_fetch_error",
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                error=str(e),
            )
            return 0.0

        # Validate data availability.
        if bars_a is None or bars_b is None:
            return 0.0

        if bars_a.empty or bars_b.empty:
            log.debug(
                "correlation.insufficient_data",
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                bars_a=len(bars_a),
                bars_b=len(bars_b),
            )
            return 0.0

        if "close" not in bars_a.columns or "close" not in bars_b.columns:
            log.warning(
                "correlation.missing_close_column",
                symbol_a=symbol_a,
                symbol_b=symbol_b,
            )
            return 0.0

        try:
            corr = self._compute_return_correlation(bars_a, bars_b)
        except Exception as e:
            log.error(
                "correlation.compute_error",
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                error=str(e),
            )
            corr = 0.0

        # Cache the result.
        self._cache[cache_key] = (corr, datetime.utcnow())

        return corr

    def is_intraday_correlated(
        self,
        new_symbol: str,
        existing_symbols: list[str],
        bars_cache: dict[str, pd.DataFrame],
        threshold: float = 0.6,
    ) -> bool:
        """
        Check intraday return correlation using recent 1-minute bars.
        
        Momentum gappers often move together because of shared retail
        flow, not sector similarity. This checks the last 30 minutes
        of 1-min bar returns for correlation.
        
        Args:
            new_symbol: Candidate symbol.
            existing_symbols: Currently held symbols.
            bars_cache: Dict mapping symbol -> recent 1-min bars DataFrame.
            threshold: Correlation threshold (default 0.6, tighter than daily).
        
        Returns:
            True if correlated with any existing position.
        """
        if not existing_symbols:
            return False
        
        new_bars = bars_cache.get(new_symbol)
        if new_bars is None or new_bars.empty or len(new_bars) < 15:
            return False
        
        try:
            new_bars_copy = new_bars.copy()
            new_bars_copy.columns = [c.lower() for c in new_bars_copy.columns]
            new_returns = new_bars_copy["close"].tail(30).pct_change().dropna()
            
            if len(new_returns) < 10:
                return False
            
            for existing in existing_symbols:
                exist_bars = bars_cache.get(existing)
                if exist_bars is None or exist_bars.empty or len(exist_bars) < 15:
                    continue
                
                exist_copy = exist_bars.copy()
                exist_copy.columns = [c.lower() for c in exist_copy.columns]
                exist_returns = exist_copy["close"].tail(30).pct_change().dropna()
                
                if len(exist_returns) < 10:
                    continue
                
                # Align by position (both are recent 1-min bars)
                min_len = min(len(new_returns), len(exist_returns))
                nr = new_returns.values[-min_len:]
                er = exist_returns.values[-min_len:]
                
                if np.std(nr) == 0 or np.std(er) == 0:
                    continue
                
                corr = float(np.corrcoef(nr, er)[0, 1])
                if not np.isnan(corr) and abs(corr) > threshold:
                    log.info(
                        "correlation.intraday_correlated",
                        new_symbol=new_symbol,
                        existing_symbol=existing,
                        correlation=round(corr, 4),
                    )
                    return True
        except Exception as e:
            log.debug("correlation.intraday_error", error=str(e))
        
        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_return_correlation(self, bars_a, bars_b) -> float:
        """
        Compute Pearson correlation of daily returns from two bar DataFrames.

        Aligns the two series by their date index, computes percentage
        returns, and uses numpy.corrcoef for the correlation coefficient.
        """
        # Compute daily percentage returns and drop NaN from the first row.
        returns_a = bars_a["close"].pct_change().dropna()
        returns_b = bars_b["close"].pct_change().dropna()

        # Align on overlapping dates using an inner join.
        aligned_a = returns_a.loc[returns_a.index.intersection(returns_b.index)]
        aligned_b = returns_b.loc[returns_b.index.intersection(returns_a.index)]

        if len(aligned_a) < _MIN_BARS or len(aligned_b) < _MIN_BARS:
            log.debug(
                "correlation.too_few_overlapping_bars",
                overlapping=len(aligned_a),
                required=_MIN_BARS,
            )
            return 0.0

        # Constant returns (zero variance) make correlation undefined.
        if np.std(aligned_a.values) == 0.0 or np.std(aligned_b.values) == 0.0:
            return 0.0

        corr_matrix = np.corrcoef(aligned_a.values, aligned_b.values)
        corr = float(corr_matrix[0, 1])

        # Guard against NaN from degenerate inputs.
        if np.isnan(corr):
            return 0.0

        return corr

    def _sic_codes_match(
        self,
        symbol_a: str,
        symbol_b: str,
        market_data: MarketDataProvider,
    ) -> bool:
        """
        Check if two symbols share the same SIC code prefix (2-digit
        major industry group).

        Uses the Polygon ticker details endpoint via the market data
        provider's underlying polygon client, if available.
        """
        sic_a = self._get_sic_code(symbol_a, market_data)
        sic_b = self._get_sic_code(symbol_b, market_data)

        if sic_a is None or sic_b is None:
            return False

        return sic_a[:_SIC_PREFIX_LEN] == sic_b[:_SIC_PREFIX_LEN]

    def _get_sic_code(
        self,
        symbol: str,
        market_data: MarketDataProvider,
    ) -> Optional[str]:
        """
        Retrieve the SIC code for a symbol, with caching.

        Attempts to access the polygon client on the market data provider
        (available on LiveMarketData) to call get_ticker_details().
        Returns None if the provider lacks a polygon client or if the
        ticker details do not include a SIC code.
        """
        if symbol in self._sic_cache:
            return self._sic_cache[symbol]

        sic_code: Optional[str] = None

        try:
            # LiveMarketData exposes _polygon; BacktestMarketData does not.
            polygon_client = getattr(market_data, "_polygon", None)
            if polygon_client is not None:
                details = polygon_client.get_ticker_details(symbol)
                if details and details.get("sic_code"):
                    sic_code = str(details["sic_code"])
        except Exception as e:
            log.debug(
                "correlation.sic_lookup_error",
                symbol=symbol,
                error=str(e),
            )

        self._sic_cache[symbol] = sic_code
        return sic_code

    @staticmethod
    def _cache_key(symbol_a: str, symbol_b: str) -> tuple[str, str]:
        """
        Create an order-independent cache key for a symbol pair.

        Ensures (AAPL, MSFT) and (MSFT, AAPL) map to the same entry.
        """
        a, b = symbol_a.upper(), symbol_b.upper()
        return (min(a, b), max(a, b))
