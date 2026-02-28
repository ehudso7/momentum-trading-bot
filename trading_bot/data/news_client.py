"""
News and catalyst detection client.

Fetches recent news from Polygon.io and scans for catalyst keywords
(FDA, earnings, merger, etc.) that often drive momentum moves.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import structlog

from trading_bot.config.settings import ScannerConfig
from trading_bot.data.polygon_client import PolygonClient

log = structlog.get_logger(__name__)


class NewsClient:
    """Fetches and filters news for catalyst detection."""

    POSITIVE_KEYWORDS = [
        "approve", "approved", "approval", "grant", "granted",
        "beat", "beats", "exceed", "exceeded", "surpass",
        "upgrade", "upgraded", "raise", "raised", "higher",
        "breakthrough", "positive", "success", "successful",
        "strong", "record", "growth", "soar", "surge",
    ]
    NEGATIVE_KEYWORDS = [
        "reject", "rejected", "rejection", "deny", "denied",
        "miss", "missed", "below", "disappoint", "disappointing",
        "downgrade", "downgraded", "lower", "lowered", "cut",
        "fail", "failed", "failure", "negative", "weak",
        "decline", "drop", "fall", "warning", "recall",
    ]

    def __init__(
        self,
        polygon_client: Optional[PolygonClient],
        config: ScannerConfig,
    ):
        self._polygon = polygon_client
        self._keywords = [kw.lower() for kw in config.catalyst_keywords]
        self._cache: dict[str, tuple[Optional[str], datetime]] = {}
        self._cache_ttl_minutes = 15

    def find_catalyst(self, symbol: str) -> Optional[str]:
        """
        Fetch recent news and scan for catalyst keywords.

        Returns the first matching catalyst keyword found in headlines,
        or None if no catalyst is detected.

        Results are cached for 15 minutes per symbol.
        """
        # Check cache
        if symbol in self._cache:
            cached_result, cached_time = self._cache[symbol]
            if (datetime.utcnow() - cached_time).total_seconds() < (
                self._cache_ttl_minutes * 60
            ):
                return cached_result

        if not self._polygon or not self._polygon.is_configured:
            self._cache[symbol] = (None, datetime.utcnow())
            return None

        try:
            articles = self._polygon.get_news(symbol, limit=10)

            for article in articles:
                # Check if article is recent (last 24 hours)
                pub_time = article.get("published_utc", "")
                if pub_time and not self._is_recent(pub_time):
                    continue

                # Search title and description for keywords
                text = (
                    f"{article.get('title', '')} {article.get('description', '')}"
                ).lower()

                for keyword in self._keywords:
                    if keyword in text:
                        catalyst = keyword.upper()
                        log.info(
                            "news.catalyst_found",
                            symbol=symbol,
                            catalyst=catalyst,
                            title=article.get("title", "")[:80],
                        )
                        self._cache[symbol] = (catalyst, datetime.utcnow())
                        return catalyst

        except Exception as e:
            log.error("news.fetch_error", symbol=symbol, error=str(e))

        self._cache[symbol] = (None, datetime.utcnow())
        return None

    def has_catalyst(self, symbol: str) -> bool:
        """Boolean check for catalyst presence."""
        return self.find_catalyst(symbol) is not None

    def classify_sentiment(self, symbol: str) -> str:
        """
        Classify news sentiment for a symbol.
        
        Returns: "positive", "negative", or "neutral"
        """
        if not self._polygon or not self._polygon.is_configured:
            return "neutral"
        
        try:
            articles = self._polygon.get_news(symbol, limit=5)
            positive_count = 0
            negative_count = 0
            
            for article in articles:
                pub_time = article.get("published_utc", "")
                if pub_time and not self._is_recent(pub_time, hours=12):
                    continue
                
                text = (
                    f"{article.get('title', '')} {article.get('description', '')}"
                ).lower()
                
                for kw in self.POSITIVE_KEYWORDS:
                    if kw in text:
                        positive_count += 1
                        break
                
                for kw in self.NEGATIVE_KEYWORDS:
                    if kw in text:
                        negative_count += 1
                        break
            
            if positive_count > negative_count:
                return "positive"
            elif negative_count > positive_count:
                return "negative"
            return "neutral"
        except Exception:
            return "neutral"

    def _is_recent(self, published_utc: str, hours: int = 24) -> bool:
        """Check if a publication timestamp is within the last N hours."""
        try:
            # Handle various timestamp formats from Polygon
            for fmt in [
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z",
            ]:
                try:
                    pub_dt = datetime.strptime(published_utc, fmt)
                    if pub_dt.tzinfo:
                        pub_dt = pub_dt.replace(tzinfo=None)
                    cutoff = datetime.utcnow() - timedelta(hours=hours)
                    return pub_dt >= cutoff
                except ValueError:
                    continue
            return True  # If we can't parse, assume recent
        except Exception:
            return True
