"""Tests for the momentum gapper scanner."""

from __future__ import annotations

import time

from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from trading_bot.config.settings import ScannerConfig
from trading_bot.scanners.momentum_gappers import MomentumGapperScanner


class TestMomentumGapperScanner:
    """Test scanner filter logic."""

    def _make_scanner(
        self,
        gainers: list[dict] | None = None,
        config: ScannerConfig | None = None,
    ) -> MomentumGapperScanner:
        """Create scanner with mocked dependencies."""
        market_data = MagicMock()
        market_data.get_float_shares.return_value = 5_000_000
        market_data.get_avg_volume.return_value = 100_000  # low avg so rvol tests pass easily

        news_client = MagicMock()
        news_client.find_catalyst.return_value = "FDA"

        polygon_client = MagicMock()
        polygon_client.get_gainers.return_value = gainers or []

        scanner = MomentumGapperScanner(
            market_data=market_data,
            news_client=news_client,
            polygon_client=polygon_client,
            config=config or ScannerConfig(),
        )

        return scanner

    def test_empty_gainers(self):
        """No gainers returns empty list."""
        scanner = self._make_scanner(gainers=[])
        results = scanner.scan()
        assert results == []

    def test_price_filter_excludes_low(self):
        """Stocks below $2 are excluded."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("LOW", price=1.50, change_pct=30.0, volume=1_000_000),
                _make_gainer("OK", price=5.0, change_pct=30.0, volume=1_000_000),
            ]
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "LOW" not in symbols
        assert "OK" in symbols

    def test_price_filter_excludes_high(self):
        """Stocks above max_price ($50) are excluded."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("HIGH", price=55.0, change_pct=30.0, volume=1_000_000),
                _make_gainer("OK", price=15.0, change_pct=30.0, volume=1_000_000),
            ]
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "HIGH" not in symbols

    def test_gap_filter_excludes_low_gap(self):
        """Stocks with gap < min_gap_pct (4%) are excluded."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("LOWGAP", price=10.0, change_pct=3.0, volume=1_000_000),
                _make_gainer("HIGHGAP", price=10.0, change_pct=25.0, volume=1_000_000),
            ]
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "LOWGAP" not in symbols
        assert "HIGHGAP" in symbols

    def test_gap_filter_excludes_overextended(self):
        """Stocks with gap > 200% are excluded."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("OVER", price=10.0, change_pct=250.0, volume=1_000_000),
            ]
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "OVER" not in symbols

    def test_float_filter_excludes_high_float(self):
        """Stocks with float > 50M are excluded."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("BIGFLOAT", price=10.0, change_pct=20.0, volume=5_000_000),
            ]
        )
        # Override float to be too high
        scanner._data.get_float_shares.return_value = 100_000_000
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "BIGFLOAT" not in symbols

    def test_float_filter_keeps_unknown(self):
        """Stocks with unknown float are kept (scored lower)."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("UNKNOWN", price=10.0, change_pct=20.0, volume=5_000_000),
            ]
        )
        scanner._data.get_float_shares.return_value = None
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "UNKNOWN" in symbols

    @freeze_time("2026-02-21 03:00:00")  # 10 PM ET — market closed, no tod normalization
    def test_volume_filter_excludes_low_rvol(self):
        """Stocks with relative volume < min_relative_volume (2x) are excluded."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("LOWVOL", price=10.0, change_pct=20.0, volume=100_000),
            ]
        )
        # avg_volume=100_000, current=100_000 -> rvol=1x < 2x
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "LOWVOL" not in symbols

    @freeze_time("2026-02-20 15:00:00")  # 10 AM ET — 30 min into trading
    def test_rvol_normalization_early_morning(self):
        """Early morning volume is normalized by time-of-day factor."""
        scanner = self._make_scanner(
            gainers=[
                # 20K volume at 10 AM with 100K avg daily
                # Raw rvol = 0.2x (would fail)
                # Normalized: 30min / 390min = 0.077, projected = 20K/0.077 = 260K
                # Normalized rvol = 260K/100K = 2.6x (should pass 2x threshold)
                _make_gainer("EARLYBIRD", price=10.0, change_pct=20.0, volume=20_000),
            ]
        )
        results = scanner.scan()
        symbols = [r.symbol for r in results]
        assert "EARLYBIRD" in symbols

    def test_rvol_compute_static(self):
        """Test _compute_rvol directly for correctness."""
        # With tod_factor=1.0 (market closed): raw rvol
        assert MomentumGapperScanner._compute_rvol(100_000, 100_000, 1.0) == pytest.approx(1.0)
        assert MomentumGapperScanner._compute_rvol(200_000, 100_000, 1.0) == pytest.approx(2.0)

        # With tod_factor=0.1 (early morning): projects volume 10x
        assert MomentumGapperScanner._compute_rvol(20_000, 100_000, 0.1) == pytest.approx(2.0)

        # Edge: zero avg volume
        assert MomentumGapperScanner._compute_rvol(100_000, 0, 1.0) == 0.0

    def test_time_of_day_factor_outside_market(self):
        """Factor is 1.0 when market is closed (no normalization)."""
        with freeze_time("2026-02-21 03:00:00"):  # 10 PM ET
            factor = MomentumGapperScanner._time_of_day_factor()
            assert factor == 1.0

    @freeze_time("2026-02-20 15:00:00")  # 10:00 AM ET = 30 min into 390 min day
    def test_time_of_day_factor_during_market(self):
        """Factor reflects fraction of trading day elapsed."""
        factor = MomentumGapperScanner._time_of_day_factor()
        # 30 min / 390 min ≈ 0.077
        assert 0.05 < factor < 0.12

    def test_scoring_higher_gap_higher_score(self):
        """Higher gap percentage results in higher score."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("HIGHGAP", price=10.0, change_pct=50.0, volume=5_000_000),
                _make_gainer("LOWGAP", price=10.0, change_pct=15.0, volume=5_000_000),
            ]
        )
        results = scanner.scan()
        if len(results) >= 2:
            high = next(r for r in results if r.symbol == "HIGHGAP")
            low = next(r for r in results if r.symbol == "LOWGAP")
            assert high.score >= low.score

    def test_max_candidates_limit(self):
        """Scanner returns at most max_candidates results."""
        config = ScannerConfig(max_candidates=3)
        gainers = [
            _make_gainer(f"SYM{i}", price=10.0, change_pct=20.0 + i, volume=5_000_000)
            for i in range(10)
        ]
        scanner = self._make_scanner(gainers=gainers, config=config)
        results = scanner.scan()
        assert len(results) <= 3

    def test_results_sorted_by_score(self):
        """Results are sorted by score descending."""
        scanner = self._make_scanner(
            gainers=[
                _make_gainer("A", price=5.0, change_pct=30.0, volume=3_000_000),
                _make_gainer("B", price=10.0, change_pct=50.0, volume=5_000_000),
                _make_gainer("C", price=8.0, change_pct=15.0, volume=4_000_000),
            ]
        )
        results = scanner.scan()
        if len(results) >= 2:
            scores = [r.score for r in results]
            assert scores == sorted(scores, reverse=True)

    def test_score_computation(self):
        """Direct test of _compute_score formula."""
        scanner = self._make_scanner()

        # Perfect candidate: high gap, high rvol, low float, has catalyst
        score = scanner._compute_score(
            gap_pct=80.0,
            relative_volume=15.0,
            float_shares=5_000_000,
            has_catalyst=True,
        )
        assert 0.7 <= score <= 1.0

        # Weak candidate: low gap, low rvol, high float, no catalyst
        weak_score = scanner._compute_score(
            gap_pct=12.0,
            relative_volume=5.5,
            float_shares=45_000_000,
            has_catalyst=False,
        )
        assert weak_score < score


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



class TestScannerLatencyGuardrails:
    """Regression coverage for the August 14 premarket stall."""

    def _make_scanner(
        self,
        gainers: list[dict],
        *,
        max_candidates: int = 20,
    ) -> MomentumGapperScanner:
        market_data = MagicMock()
        market_data.get_float_shares.return_value = 5_000_000
        market_data.get_avg_volume.return_value = 100_000

        news_client = MagicMock()
        news_client.find_catalyst.return_value = None

        polygon_client = MagicMock()
        polygon_client.get_gainers.return_value = gainers

        return MomentumGapperScanner(
            market_data=market_data,
            news_client=news_client,
            polygon_client=polygon_client,
            config=ScannerConfig(
                max_candidates=max_candidates,
            ),
        )

    def test_unsupported_punctuation_symbols_are_dropped_before_float_lookup(
        self,
    ):
        scanner = self._make_scanner(
            [
                _make_gainer(
                    "AHT.PRG",
                    change_pct=90,
                    volume=9_000_000,
                ),
                _make_gainer(
                    "NE.WSA",
                    change_pct=80,
                    volume=8_000_000,
                ),
                _make_gainer(
                    "GOOD",
                    change_pct=30,
                    volume=5_000_000,
                ),
            ]
        )

        results = scanner.scan()

        assert [item.symbol for item in results] == ["GOOD"]

        called_symbols = [
            call.args[0]
            for call
            in scanner._data.get_float_shares.call_args_list
        ]

        assert "AHT.PRG" not in called_symbols
        assert "NE.WSA" not in called_symbols
        assert "GOOD" in called_symbols

    def test_expensive_enrichment_pool_is_bounded(
        self,
    ):
        gainers = [
            _make_gainer(
                f"SYM{i}",
                change_pct=100 - i,
                volume=10_000_000 - i,
            )
            for i in range(80)
        ]

        scanner = self._make_scanner(
            gainers,
            max_candidates=20,
        )

        scanner.scan()

        assert (
            scanner._data.get_float_shares.call_count
            <= 20
        )

    def test_float_filter_stops_after_deadline(
        self,
        monkeypatch,
    ):
        gainers = [
            _make_gainer(
                f"SYM{i}",
                change_pct=30,
                volume=5_000_000,
            )
            for i in range(10)
        ]

        scanner = self._make_scanner(gainers)

        values = iter(
            [
                0.0,
                0.0,
                95.0,
                95.0,
                95.0,
            ]
        )

        monkeypatch.setattr(
            "trading_bot.scanners.momentum_gappers.time.monotonic",
            lambda: next(values, 95.0),
        )

        results = scanner.scan()

        assert len(results) <= 1
        assert (
            scanner._data.get_float_shares.call_count
            <= 1
        )

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("AAPL", True),
            ("TSLA1", True),
            ("AHT.PRG", False),
            ("NE.WSA", False),
            ("BBBY.WS", False),
            ("BRK-B", False),
            ("", False),
        ],
    )
    def test_supported_symbol_filter(
        self,
        symbol: str,
        expected: bool,
    ):
        assert (
            MomentumGapperScanner._is_supported_symbol(
                symbol
            )
            is expected
        )

    def test_pathological_slow_enrichment_returns_within_budget(
        self,
        monkeypatch,
    ):
        """Many slow lookups must not recreate the multi-minute stall."""
        gainers = [
            _make_gainer(
                f"SLOW{i}",
                change_pct=100.0 - (i * 0.5),
                volume=10_000_000 - i,
            )
            for i in range(80)
        ]

        scanner = self._make_scanner(
            gainers,
            max_candidates=20,
        )

        # Compress the production 90-second budget to one second so the
        # regression test completes quickly while exercising the same
        # deadline logic.
        monkeypatch.setattr(
            "trading_bot.scanners.momentum_gappers."
            "_SCAN_TIME_BUDGET_SECONDS",
            1.0,
        )

        def slow_float(_symbol):
            time.sleep(0.20)
            return 5_000_000

        scanner._data.get_float_shares.side_effect = slow_float

        started = time.monotonic()

        results = scanner.scan()

        elapsed = time.monotonic() - started

        # One in-flight lookup can finish after the deadline, so allow a
        # modest scheduling margin. The critical proof is that the scanner
        # does not process the entire 20-symbol enrichment pool.
        assert elapsed < 2.0
        assert scanner._data.get_float_shares.call_count <= 6

        # Once the deadline is exhausted, downstream expensive stages should
        # not continue chewing through candidates.
        assert scanner._data.get_avg_volume.call_count <= 1
        assert scanner._news.find_catalyst.call_count <= 1

        assert isinstance(results, list)
