"""Tests for the Alpaca screener fallback and scanner fallback integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from trading_bot.config.settings import BrokerConfig, ScannerConfig
from trading_bot.data.alpaca_screener import AlpacaScreener
from trading_bot.scanners.momentum_gappers import MomentumGapperScanner


class TestAlpacaScreener:
    """Tests for the AlpacaScreener data client."""

    def test_not_configured_without_key(self):
        config = BrokerConfig(alpaca_api_key="", alpaca_api_secret="")
        screener = AlpacaScreener(config)
        assert not screener.is_configured
        assert screener.get_gainers() == []

    def test_not_configured_with_placeholder(self):
        config = BrokerConfig(
            alpaca_api_key="your_alpaca_api_key_here",
            alpaca_api_secret="secret",
        )
        screener = AlpacaScreener(config)
        assert not screener.is_configured

    def test_configured_with_real_key(self):
        config = BrokerConfig(
            alpaca_api_key="PKTEST123",
            alpaca_api_secret="secret123",
        )
        screener = AlpacaScreener(config)
        assert screener.is_configured

    @patch("trading_bot.data.alpaca_screener.requests.Session")
    def test_get_gainers_parses_response(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "gainers": [
                {
                    "symbol": "AAPL",
                    "price": 175.0,
                    "change": 8.50,
                    "percent_change": 5.1,
                    "volume": 5000000,
                },
                {
                    "symbol": "TSLA",
                    "price": 250.0,
                    "change": 25.0,
                    "percent_change": 11.1,
                    "volume": 8000000,
                },
            ],
            "losers": [],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        config = BrokerConfig(
            alpaca_api_key="PKTEST123",
            alpaca_api_secret="secret123",
        )
        screener = AlpacaScreener(config)
        results = screener.get_gainers()

        assert len(results) == 2
        assert results[0]["symbol"] == "AAPL"
        assert results[0]["price"] == 175.0
        assert results[0]["change_pct"] == 5.1
        assert results[0]["volume"] == 5000000
        assert results[0]["prev_close"] == round(175.0 - 8.50, 4)

        assert results[1]["symbol"] == "TSLA"
        assert results[1]["change_pct"] == 11.1

    @patch("trading_bot.data.alpaca_screener.requests.Session")
    def test_get_gainers_empty_response(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"gainers": [], "losers": []}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        config = BrokerConfig(
            alpaca_api_key="PKTEST123",
            alpaca_api_secret="secret123",
        )
        screener = AlpacaScreener(config)
        results = screener.get_gainers()
        assert results == []


class TestScannerFallback:
    """Tests for the scanner's Polygon -> Alpaca fallback behavior."""

    def _make_scanner(
        self,
        polygon_gainers=None,
        polygon_error=None,
        fallback_gainers=None,
        fallback_error=None,
    ) -> MomentumGapperScanner:
        market_data = MagicMock()
        market_data.get_float_shares.return_value = 5_000_000
        market_data.get_avg_volume.return_value = 100_000

        news_client = MagicMock()
        news_client.find_catalyst.return_value = "FDA"

        polygon_client = MagicMock()
        if polygon_error:
            polygon_client.get_gainers.side_effect = polygon_error
        else:
            polygon_client.get_gainers.return_value = polygon_gainers or []

        fallback_client = MagicMock()
        if fallback_error:
            fallback_client.get_gainers.side_effect = fallback_error
        else:
            fallback_client.get_gainers.return_value = fallback_gainers or []

        return MomentumGapperScanner(
            market_data=market_data,
            news_client=news_client,
            polygon_client=polygon_client,
            config=ScannerConfig(),
            fallback_client=fallback_client,
        )

    def test_polygon_success_also_merges_fallback(self):
        """When Polygon works, fallback results are merged (not excluded)."""
        gainer = _make_gainer("TEST", price=10.0, change_pct=20.0, volume=5_000_000)
        scanner = self._make_scanner(
            polygon_gainers=[gainer],
            fallback_gainers=[_make_gainer("FALLBACK")],
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "TEST" in symbols
        # All sources are now merged, so FALLBACK should also appear
        assert "FALLBACK" in symbols

    def test_polygon_auth_error_uses_fallback(self):
        """When Polygon throws an auth error, fallback is used."""
        fallback_gainer = _make_gainer(
            "ALPACA", price=8.0, change_pct=25.0, volume=3_000_000
        )
        scanner = self._make_scanner(
            polygon_error=Exception("NOT_AUTHORIZED"),
            fallback_gainers=[fallback_gainer],
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "ALPACA" in symbols

    def test_polygon_empty_uses_fallback(self):
        """When Polygon returns empty, fallback is used."""
        fallback_gainer = _make_gainer(
            "ALPACA", price=8.0, change_pct=25.0, volume=3_000_000
        )
        scanner = self._make_scanner(
            polygon_gainers=[],
            fallback_gainers=[fallback_gainer],
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "ALPACA" in symbols

    def test_both_fail_returns_empty(self):
        """When both Polygon and fallback fail, returns empty."""
        scanner = self._make_scanner(
            polygon_error=Exception("Polygon down"),
            fallback_error=Exception("Alpaca down"),
        )
        results = scanner.scan()
        assert results == []

    def test_no_fallback_configured(self):
        """Without fallback, Polygon error propagates as empty results."""
        market_data = MagicMock()
        news_client = MagicMock()
        polygon_client = MagicMock()
        polygon_client.get_gainers.side_effect = Exception("NOT_AUTHORIZED")

        scanner = MomentumGapperScanner(
            market_data=market_data,
            news_client=news_client,
            polygon_client=polygon_client,
            config=ScannerConfig(),
            # No fallback_client
        )
        results = scanner.scan()
        assert results == []


def _make_gainer(
    symbol: str,
    price: float = 10.0,
    change_pct: float = 20.0,
    volume: int = 1_000_000,
) -> dict:
    """Helper to create a gainer snapshot dict."""
    prev_close = price / (1 + change_pct / 100)
    return {
        "symbol": symbol,
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "prev_close": round(prev_close, 2),
        "day_open": price * 0.98,
        "day_high": price * 1.02,
        "day_low": price * 0.95,
        "vwap": price * 0.99,
    }
