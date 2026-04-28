"""
Tests for trading_bot.saas.report_engine and market_data selector.

Covers:
  * report schema includes every required field
  * generate_report works with an injected fetcher
  * persist_report writes a deterministic filename
  * list_report_dates returns sorted dates
  * project_for_free hides premium fields and limits signals
  * demo mode is forced when provider is demo
  * market_data.selected_provider env-driven precedence
"""

from __future__ import annotations

import json
from pathlib import Path

from trading_bot.saas import REPORT_SCHEMA_VERSION
from trading_bot.saas.market_data import (
    PROVIDER_ALPACA,
    PROVIDER_DEMO,
    PROVIDER_POLYGON,
    PROVIDER_YFINANCE,
    fetch_daily_bars,
    selected_provider,
)
from trading_bot.saas.report_engine import (
    generate_report,
    latest_report_path,
    list_report_dates,
    persist_report,
    project_for_free,
    report_for_date,
    report_filename,
)


def _bullish_bars(n: int = 80):
    out = []
    for i in range(n):
        close = 100.0 + i * 1.0
        vol = 5_000_000.0 if i == n - 1 else 1_000_000.0
        out.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": vol,
        })
    out[-1]["date"] = "2026-04-28"
    return out


def _bearish_bars(n: int = 80):
    out = []
    for i in range(n):
        close = 200.0 - i * 1.0
        vol = 5_000_000.0 if i == n - 1 else 1_000_000.0
        out.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": close * 1.01,
            "high": close * 1.02,
            "low": close * 0.99,
            "close": close,
            "volume": vol,
        })
    out[-1]["date"] = "2026-04-28"
    return out


def _make_fetcher(per_symbol):
    def _fetch(symbol, **_kwargs):
        return per_symbol.get(symbol, ([], "no_data")), None
    # Fetcher must return (bars, error) tuple — so wrap properly.
    def _fetch2(symbol, **_kwargs):
        bars, err = per_symbol.get(symbol, ([], "no_data"))
        return bars, err
    return _fetch2


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


REQUIRED_TOP_LEVEL = {
    "schema_version", "generated_at", "report_date", "mode", "universe",
    "market_data_status", "summary", "signals", "risk", "premium",
    "share", "disclaimer", "strategy",
}

REQUIRED_SUMMARY = {
    "signal_count", "bullish_count", "bearish_count", "neutral_count",
    "average_confidence", "risk_level",
}

REQUIRED_RISK = {
    "max_position_size_pct", "max_daily_loss_pct",
    "stop_loss_pct", "take_profit_pct", "notes",
}

REQUIRED_MARKET_STATUS = {"provider", "freshness", "errors"}

REQUIRED_SIGNAL_KEYS = {
    "symbol", "direction", "strategy", "confidence", "timeframe",
    "indicators", "rationale", "entry", "stop_loss", "take_profit",
}


class TestSchema:
    def test_top_level_keys(self):
        fetch = _make_fetcher({
            "AAPL": (_bullish_bars(), None),
            "MSFT": (_bearish_bars(), None),
        })
        report = generate_report(
            universe=["AAPL", "MSFT"],
            fetch=fetch,
            provider="yfinance",
            mode="paper",
        )
        for k in REQUIRED_TOP_LEVEL:
            assert k in report, f"missing top-level field: {k}"
        assert report["schema_version"] == REPORT_SCHEMA_VERSION
        assert report["disclaimer"]
        assert report["strategy"] == "momentum_breakout_v1"

    def test_summary_keys(self):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch, provider="yfinance",
        )
        for k in REQUIRED_SUMMARY:
            assert k in report["summary"]

    def test_risk_keys(self):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch, provider="yfinance",
        )
        for k in REQUIRED_RISK:
            assert k in report["risk"]
        assert isinstance(report["risk"]["notes"], list)

    def test_market_status_keys(self):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch, provider="yfinance",
        )
        for k in REQUIRED_MARKET_STATUS:
            assert k in report["market_data_status"]

    def test_signal_keys(self):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch, provider="yfinance",
        )
        signal = report["signals"][0]
        for k in REQUIRED_SIGNAL_KEYS:
            assert k in signal


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


class TestGeneration:
    def test_summary_counts(self):
        fetch = _make_fetcher({
            "BULL": (_bullish_bars(), None),
            "BEAR": (_bearish_bars(), None),
            "FLAT": ([{"close": 100.0, "volume": 1_000_000.0}] * 80, None),
        })
        report = generate_report(
            universe=["BULL", "BEAR", "FLAT"],
            fetch=fetch, provider="yfinance",
        )
        s = report["summary"]
        assert s["signal_count"] == 3
        assert s["bullish_count"] == 1
        assert s["bearish_count"] == 1
        assert s["neutral_count"] == 1

    def test_errors_propagate_to_market_data_status(self):
        def fetch(symbol, **_kw):
            if symbol == "BAD":
                return ([], "yfinance_empty_history")
            return _bullish_bars(), None
        report = generate_report(
            universe=["AAPL", "BAD"], fetch=fetch, provider="yfinance",
        )
        errs = report["market_data_status"]["errors"]
        assert any("BAD" in e for e in errs)

    def test_provider_demo_forces_mode_demo(self):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch,
            provider="demo", mode="paper",
        )
        assert report["mode"] == "demo"

    def test_explicit_mode_used_when_provider_not_demo(self):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch,
            provider="yfinance", mode="paper",
        )
        assert report["mode"] == "paper"

    def test_universe_from_argument_overrides_default(self):
        fetch = _make_fetcher({"FOO": (_bullish_bars(), None)})
        report = generate_report(
            universe=["foo"], fetch=fetch, provider="yfinance",
        )
        assert report["universe"] == ["FOO"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_persist_creates_file(self, tmp_path: Path):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch, provider="yfinance",
        )
        report["report_date"] = "2026-04-28"
        path = persist_report(report, target_dir=tmp_path)
        assert path.exists()
        assert path.name == "signal_report_2026-04-28.json"
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["report_date"] == "2026-04-28"

    def test_persist_creates_directory(self, tmp_path: Path):
        fetch = _make_fetcher({"AAPL": (_bullish_bars(), None)})
        report = generate_report(
            universe=["AAPL"], fetch=fetch, provider="yfinance",
        )
        report["report_date"] = "2026-04-28"
        target = tmp_path / "nested" / "saas_reports"
        path = persist_report(report, target_dir=target)
        assert path.exists()

    def test_list_report_dates_returns_sorted(self, tmp_path: Path):
        for d in ("2026-04-25", "2026-04-26", "2026-04-28", "2026-04-27"):
            (tmp_path / report_filename(d)).write_text(
                json.dumps({"report_date": d}), encoding="utf-8",
            )
        # Plus a malformed one that should be ignored.
        (tmp_path / "signal_report_garbage.json").write_text(
            "{}", encoding="utf-8",
        )
        dates = list_report_dates(tmp_path)
        assert dates == ["2026-04-25", "2026-04-26", "2026-04-27", "2026-04-28"]

    def test_list_report_dates_missing_directory(self, tmp_path: Path):
        assert list_report_dates(tmp_path / "does_not_exist") == []

    def test_latest_report_path(self, tmp_path: Path):
        for d in ("2026-04-25", "2026-04-28", "2026-04-27"):
            (tmp_path / report_filename(d)).write_text(
                json.dumps({"report_date": d}), encoding="utf-8",
            )
        p = latest_report_path(tmp_path)
        assert p is not None
        assert p.name == "signal_report_2026-04-28.json"

    def test_report_for_date(self, tmp_path: Path):
        (tmp_path / report_filename("2026-04-28")).write_text("{}", encoding="utf-8")
        assert report_for_date("2026-04-28", target_dir=tmp_path) is not None
        assert report_for_date("1999-01-01", target_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Free-vs-premium projection
# ---------------------------------------------------------------------------


class TestFreeProjection:
    def _report(self):
        fetch = _make_fetcher({
            "AAPL": (_bullish_bars(), None),
            "MSFT": (_bearish_bars(), None),
            "TSLA": (_bullish_bars(), None),
            "NVDA": (_bullish_bars(), None),
            "META": (_bullish_bars(), None),
        })
        return generate_report(
            universe=["AAPL", "MSFT", "TSLA", "NVDA", "META"],
            fetch=fetch, provider="yfinance",
        )

    def test_free_hides_risk_block(self):
        free = project_for_free(self._report())
        assert "risk" not in free

    def test_free_signal_shape(self):
        free = project_for_free(self._report())
        for s in free["signals"]:
            assert "entry" not in s
            assert "stop_loss" not in s
            assert "take_profit" not in s
            assert "indicators" not in s
            assert "rationale" not in s
            assert "symbol" in s
            assert "direction" in s
            assert "confidence" in s

    def test_free_signal_count_capped(self):
        free = project_for_free(self._report())
        # At most _FREE_SIGNAL_LIMIT (=3) are returned.
        assert len(free["signals"]) <= 3

    def test_free_premium_block_marks_locked(self):
        free = project_for_free(self._report())
        assert free["premium"]["has_full_access"] is False
        assert isinstance(free["premium"]["locked_fields"], list)
        assert any("risk" in str(x) for x in free["premium"]["locked_fields"])

    def test_premium_full_access(self):
        report = self._report()
        # Premium response is the unprojected report.
        assert report["premium"]["has_full_access"] is True
        assert report["risk"]


# ---------------------------------------------------------------------------
# Market data provider selection
# ---------------------------------------------------------------------------


class TestSelectedProvider:
    def test_polygon_wins_when_set(self):
        env = {"POLYGON_API_KEY": "abc"}
        assert selected_provider(env=env) == PROVIDER_POLYGON

    def test_alpaca_when_polygon_missing(self):
        env = {"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}
        assert selected_provider(env=env) == PROVIDER_ALPACA

    def test_yfinance_when_no_paid_keys(self):
        env: dict = {}
        # yfinance is installed in dev, so this resolves to yfinance.
        assert selected_provider(env=env) in (PROVIDER_YFINANCE, "")

    def test_demo_when_explicitly_requested(self):
        env = {"TRADING_SAAS_DATA_MODE": "demo"}
        assert selected_provider(env=env) == PROVIDER_DEMO

    def test_demo_overrides_polygon_when_explicit(self):
        env = {"TRADING_SAAS_DATA_MODE": "demo", "POLYGON_API_KEY": "abc"}
        assert selected_provider(env=env) == PROVIDER_DEMO


class TestFetchDemoBars:
    def test_demo_returns_deterministic_bars(self):
        bars1, err1 = fetch_daily_bars("AAPL", provider=PROVIDER_DEMO)
        bars2, _ = fetch_daily_bars("AAPL", provider=PROVIDER_DEMO)
        assert err1 is None
        assert len(bars1) >= 50
        # Determinism: same symbol → same final close.
        assert bars1[-1]["close"] == bars2[-1]["close"]

    def test_demo_different_symbols_differ(self):
        a, _ = fetch_daily_bars("AAPL", provider=PROVIDER_DEMO)
        b, _ = fetch_daily_bars("XYZ", provider=PROVIDER_DEMO)
        assert a[-1]["close"] != b[-1]["close"]

    def test_unknown_symbol_empty_returns_error(self):
        bars, err = fetch_daily_bars("", provider=PROVIDER_DEMO)
        assert bars == []
        assert err == "empty_symbol"
