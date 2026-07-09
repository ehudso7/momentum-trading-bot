"""
Tests for the faithful intraday replay backtester
(``trading_bot.backtest.intraday_engine``).

All tests use SYNTHETIC in-memory bar data fed through an injected data
provider — the yfinance network is never touched. The synthetic paths are
engineered to exercise the REAL strategy/advisor/risk/paper-broker/portfolio
pipeline (the whole point of the engine) rather than a simplified proxy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.backtest.intraday_engine import (
    DEFAULT_UNIVERSE,
    IntradayReplayEngine,
    ReplayMarketData,
    SymbolData,
    patched_clock,
)
from trading_bot.config.settings import AppConfig, ExitConfig
from trading_bot.models.domain import (
    OrderSide,
    RiskCheckResult,
    SignalType,
    TradeSignal,
)
from trading_bot.portfolio.manager import JOURNAL_HEADERS
import trading_bot.utils.helpers as helpers


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------

SESSION_DAY = "2026-07-06"  # a Monday, regular trading day


def _strong_setup_session(day: str = SESSION_DAY) -> pd.DataFrame:
    """
    A full 78-bar (5m) regular session engineered as a clean VWAP/EMA pullback:

    - Gaps up from a $5.00 prior close to a $6.00 open (+20%).
    - Grinds to $6.70, shallow pullback to ~$6.42 on low volume,
      then reclaims through VWAP on a large volume spike, then trends up.

    This reliably produces a VWAP-pullback (then EMA-pullback) signal with
    confidence above the advisor's entry threshold when evaluated at
    historical (power-zone) time.
    """
    idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min", tz="US/Eastern")
    seg1 = np.linspace(6.00, 6.70, 16)   # opening drive up
    seg2 = np.linspace(6.70, 6.42, 18)   # shallow pullback toward VWAP
    seg3 = np.linspace(6.48, 6.90, 8)    # reclaim
    seg4 = np.linspace(6.90, 7.40, 36)   # continuation
    close = np.concatenate([seg1, seg2, seg3, seg4])
    open_ = np.concatenate([[6.00], close[:-1]])
    high = np.maximum(close, open_) + 0.02
    low = np.minimum(close, open_) - 0.02
    vol = np.concatenate([
        np.full(16, 5.0e5),
        np.full(18, 1.8e5),
        np.array([2.2e6, 2.0e6, 1.5e6, 8.0e5, 7.0e5, 6.0e5, 6.0e5, 6.0e5]),
        np.full(36, 4.0e5),
    ])
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def _flat_daily(prev_close: float, start: str = "2026-05-01", n: int = 45) -> pd.DataFrame:
    """Flat daily bars — provides prev_close, avg volume, and a stable regime."""
    idx = pd.date_range(start, periods=n, freq="B", tz="US/Eastern")
    close = np.full(n, float(prev_close))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": np.full(n, 1.0e6),
        },
        index=idx,
    )


class _FakeProvider:
    """Injected data provider serving pre-built synthetic bars (no network)."""

    def __init__(self, data: dict[str, SymbolData | None]):
        self._data = data

    def fetch(self, symbol: str) -> SymbolData | None:
        return self._data.get(symbol)


def _viable_config() -> AppConfig:
    """
    Real config, but with the first scale-out target raised to 1.5R.

    The shipped config's first target is 0.75R, which the strategy's own >=1.5
    R:R entry gate rejects — so NO setup can ever fire. This variant lets the
    pipeline actually take trades so the engine's wiring can be exercised.
    """
    cfg = AppConfig.from_yaml("trading_bot/config/config.yaml")
    cfg.exit = ExitConfig(
        scale_out_ratios=[0.34, 0.33, 0.33],
        scale_out_rr_targets=[1.5, 3.0],
    )
    return cfg


def _make_signal(symbol: str, entry: float, stop: float) -> TradeSignal:
    risk = entry - stop
    return TradeSignal(
        symbol=symbol,
        signal_type=SignalType.VWAP_PULLBACK,
        entry_price=entry,
        stop_price=stop,
        target_prices=[entry + risk * 1.5, entry + risk * 3.0],
        atr=risk,
        vwap=entry,
        ema9=entry,
        confidence=0.9,
    )


def _make_risk_result(shares: int, entry: float, stop: float) -> RiskCheckResult:
    return RiskCheckResult(
        approved=True,
        shares=shares,
        risk_dollars=shares * abs(entry - stop),
        reason="approved",
        leverage_used=0.1,
        positions_count=1,
    )


# ---------------------------------------------------------------------------
# 1. A known bar sequence that should trigger a setup produces an entry
# ---------------------------------------------------------------------------


def test_strong_setup_produces_entry():
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(), universe=["TEST"], data_provider=_FakeProvider(data)
    )
    result = engine.run()

    assert result["funnel"]["strategy_signals"] >= 1, "strategy should emit a signal"
    assert result["funnel"]["entries"] >= 1, "a valid setup should produce an entry"
    assert len(result["trades"]) >= 1, "the entry should eventually close and journal"
    # The performance scorecard is populated from the real trade list.
    assert result["performance"]["closed_trades"] == len(result["trades"])


# ---------------------------------------------------------------------------
# 2. Circuit-breaker halt is honored (no new entries; open positions closed)
# ---------------------------------------------------------------------------


def test_circuit_breaker_halt_is_honored():
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(), universe=["TEST"], data_provider=_FakeProvider(data)
    )
    clock = engine.prepare(data)

    # Use a mid-session bar; give the position a very wide stop/target so the
    # intrabar low/high pre-feed does NOT trigger the bracket OCO.
    ts = engine._market._symbols["TEST"].intraday.index[40].to_pydatetime()

    with patched_clock(clock):
        clock.set(ts)
        engine._market.set_time(ts)
        engine._circuit.reset_daily(100_000.0)

        # Open a real position through the real broker/portfolio.
        engine._broker.update_price("TEST", 6.5)
        pos = engine._portfolio.open_position(
            _make_signal("TEST", 6.5, 3.0), _make_risk_result(100, 6.5, 3.0)
        )
        assert pos.shares > 0
        assert len(engine._portfolio.get_open_positions()) == 1

        # Trip the circuit breaker via consecutive losses (real halt logic).
        for _ in range(engine._config.risk.max_consecutive_losses):
            engine._circuit.record_trade_result(-100.0)
        assert not engine._circuit.is_trading_allowed

        entries_before = engine._funnel["entries"]
        engine._process_tick(ts, ["TEST"])

    # Halt must force the open position closed and block any new entry.
    assert len(engine._portfolio.get_open_positions()) == 0
    assert engine._funnel["entries"] == entries_before
    assert any(t["exit_reason"] == "circuit_breaker_halt" for t in engine._trades)


# ---------------------------------------------------------------------------
# 3. Hard time exit (3:50 PM ET) closes open positions
# ---------------------------------------------------------------------------


def test_hard_time_exit_closes_positions():
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(), universe=["TEST"], data_provider=_FakeProvider(data)
    )
    clock = engine.prepare(data)

    # 15:50 bar — inside the is_near_close(10) hard-exit window.
    intraday = engine._market._symbols["TEST"].intraday
    ts = intraday.index[76].to_pydatetime()
    assert ts.hour == 15 and ts.minute == 50

    with patched_clock(clock):
        clock.set(ts)
        engine._market.set_time(ts)
        engine._circuit.reset_daily(100_000.0)

        engine._broker.update_price("TEST", 7.35)
        # Wide stop/target so only the hard-time-exit closes it.
        engine._portfolio.open_position(
            _make_signal("TEST", 7.35, 3.0), _make_risk_result(50, 7.35, 3.0)
        )
        assert len(engine._portfolio.get_open_positions()) == 1

        engine._process_tick(ts, ["TEST"])

    assert len(engine._portfolio.get_open_positions()) == 0
    assert any(t["exit_reason"] == "hard_time_exit" for t in engine._trades)


# ---------------------------------------------------------------------------
# 4. Empty-data symbol is skipped with a logged reason
# ---------------------------------------------------------------------------


def test_empty_data_symbol_is_skipped():
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "EMPTY": None,  # provider returns nothing
        "SHORT": SymbolData(
            "SHORT",
            _strong_setup_session().head(5),  # < 20 bars
            _flat_daily(5.0),
            5_000_000,
        ),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(),
        universe=["TEST", "EMPTY", "SHORT"],
        data_provider=_FakeProvider(data),
    )
    result = engine.run()

    assert "EMPTY" in result["symbols_skipped"]
    assert result["symbols_skipped"]["EMPTY"] == "no_intraday_data"
    assert "SHORT" in result["symbols_skipped"]
    assert "insufficient_intraday_bars" in result["symbols_skipped"]["SHORT"]
    assert "TEST" in result["symbols_loaded"]


def test_all_empty_universe_returns_honest_empty_result():
    data = {"NOPE": None, "SPY": None}
    engine = IntradayReplayEngine(
        _viable_config(), universe=["NOPE"], data_provider=_FakeProvider(data)
    )
    result = engine.run()
    assert result["symbols_loaded"] == []
    assert result["trades"] == []
    assert result["performance"]["closed_trades"] == 0
    assert "NOPE" in result["symbols_skipped"]


# ---------------------------------------------------------------------------
# 5. Trades land in the SAME schema as data/journal.csv
# ---------------------------------------------------------------------------


def test_trades_use_journal_schema():
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(), universe=["TEST"], data_provider=_FakeProvider(data)
    )
    result = engine.run()

    assert result["trades"], "expected at least one closed trade"
    for trade in result["trades"]:
        assert list(trade.keys()) == JOURNAL_HEADERS


def test_trades_written_to_output_csv(tmp_path):
    out = tmp_path / "intraday_test.csv"
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(),
        universe=["TEST"],
        data_provider=_FakeProvider(data),
        output_csv=str(out),
    )
    result = engine.run()

    assert out.exists()
    df = pd.read_csv(out)
    assert list(df.columns) == JOURNAL_HEADERS
    assert len(df) == len(result["trades"])


# ---------------------------------------------------------------------------
# Honesty guard: the SHIPPED config's R:R gate blocks every signal
# ---------------------------------------------------------------------------


def test_shipped_config_first_target_clears_entry_gate():
    """
    Regression guard for a real, previously-shipped bug: the strategy's entry
    gate (pullback_vwap.py) rejects any signal whose FIRST scale-out target is
    below 1.5R, because that first target defines the signal's reward:risk
    ratio. The originally shipped config used a 0.75R first target, so the bot
    was structurally unable to enter ANY trade (zero trades in every session
    and backtest). This asserts the config no longer reintroduces that bug: the
    first scale-out target must clear the gate, and a strong setup must be able
    to produce a strategy signal.
    """
    shipped = AppConfig.from_yaml("trading_bot/config/config.yaml")
    assert shipped.exit.scale_out_rr_targets[0] >= 1.5  # the fix must hold

    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        shipped, universe=["TEST"], data_provider=_FakeProvider(data)
    )
    result = engine.run()

    # Candidates pass the scanner filters AND the gate no longer blocks every
    # signal — the strategy can now emit at least one entry signal.
    assert result["funnel"]["passed_scanner_filters"] > 0
    assert result["funnel"]["strategy_signals"] > 0


# ---------------------------------------------------------------------------
# Faithfulness guards: no look-ahead, and the clock patch is fully restored
# ---------------------------------------------------------------------------


def test_market_data_has_no_lookahead():
    intraday = _strong_setup_session()
    market = ReplayMarketData(
        {"TEST": SymbolData("TEST", intraday, _flat_daily(5.0), 5_000_000)}
    )
    mid = intraday.index[30].to_pydatetime()
    market.set_time(mid)

    bars = market.get_intraday_bars("TEST", lookback_bars=200)
    assert bars.index.max().to_pydatetime() <= mid
    assert market.get_current_price("TEST") == pytest.approx(
        float(intraday.loc[:mid]["close"].iloc[-1])
    )


def test_now_et_is_restored_after_run():
    original = helpers.now_et
    data = {
        "TEST": SymbolData("TEST", _strong_setup_session(), _flat_daily(5.0), 5_000_000),
        "SPY": SymbolData("SPY", _strong_setup_session(), _flat_daily(500.0), None),
    }
    engine = IntradayReplayEngine(
        _viable_config(), universe=["TEST"], data_provider=_FakeProvider(data)
    )
    engine.run()
    assert helpers.now_et is original, "now_et must be restored to the real clock"


def test_default_universe_is_configurable_and_reasonable():
    # Default exists and is non-empty, but the universe is a real argument.
    assert len(DEFAULT_UNIVERSE) >= 10
    engine = IntradayReplayEngine(_viable_config(), universe=["FOO", "bar"])
    assert engine._universe == ["FOO", "BAR"]
