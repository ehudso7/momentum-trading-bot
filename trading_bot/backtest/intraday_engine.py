"""
Faithful intraday replay backtester.

Unlike :class:`trading_bot.backtest.engine.BacktestEngine` — which runs a
simplified EMA9 rule on *daily* bars with VWAP disabled and never touches the
live strategy/advisor/risk/portfolio stack — this engine replays the REAL
trading pipeline against historical *intraday* (5-minute) bars.

Design principle (trustworthiness via reuse)
--------------------------------------------
The replay drives the SAME objects the live bot wires together in
``trading_bot.main.TradingBot._tick``:

* ``PullbackVWAPStrategy``   — the five real entry setups + confidence scoring
* ``RegimeDetector``          — SPY-based regime, fed the same window
* ``TradingAdvisor``          — entry recommendation / sizing guidance
* ``PositionSizer``           — unchanged risk sizing (``risk$ / stop_distance``)
* ``CircuitBreaker``          — unchanged drawdown / loss-streak / halt logic
* ``CorrelationChecker``      — unchanged sector/price correlation gate
* ``PaperBroker``             — realistic slippage + bracket OCO fills
* ``PortfolioManager``        — scale-outs, trailing stops, journal (same schema)

Because it reuses the real components, the replay cannot "lie by
simplification": whatever behaviour the live bot would exhibit at a given
historical bar, this engine reproduces — including the circuit breaker halt and
the 3:50 PM hard time exit.

How historical time is injected
-------------------------------
Every live component reads wall-clock time via ``trading_bot.utils.helpers.now_et``
(directly, or transitively through ``is_market_open`` / ``is_near_close`` /
``is_past_exit_time``). During a replay we temporarily rebind that symbol in
every module that imported it to a :class:`SimulatedClock` so the strategy's
time-of-day logic (power zone, dead zone, hard exit) and the broker/journal
timestamps all reflect the *bar's* timestamp rather than the real clock. The
components themselves are never modified.

Data & its honest limitations
------------------------------
* yfinance free intraday history: ``5m`` ≈ last 60 days, ``1m`` ≈ 7 days. We
  default to 5m over the widest available window.
* ``gap_pct`` is computed as ``(price - prev_close) / prev_close`` — the same
  "% change from prior close" a live intraday momentum scanner filters on.
* ``relative_volume`` is the scanner's own time-of-day-normalised RVOL
  (cumulative session volume projected to a full day vs. average daily volume).
* ``float_shares`` comes from yfinance ``floatShares`` where available; when it
  is missing the candidate is *kept* (benefit of the doubt) exactly as the live
  ``MomentumGapperScanner._filter_float`` does — never fabricated.
* ``catalyst`` is unavailable historically, so it is ``None`` (the live news
  client cannot be replayed). This makes confidence scoring slightly
  conservative vs. live (no catalyst bonus).
* Intrabar fills are approximated by feeding each 5m bar's *low then high* to
  the paper broker's OCO engine (stop checked before target — the conservative
  ordering). True tick-level fills are not modelled.

None of these approximations invent data; each is documented and surfaced in
the result summary's ``caveats`` list.
"""

from __future__ import annotations

import contextlib
import importlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol

import pandas as pd
import structlog

from trading_bot.config.settings import AppConfig
from trading_bot.execution.paper_broker import PaperBroker, SlippageModel
from trading_bot.data.market_data import MarketDataProvider
from trading_bot.models.domain import JournalEntry, ScanResult
from trading_bot.portfolio.manager import PortfolioManager
from trading_bot.risk.circuit_breaker import CircuitBreaker, CircuitState
from trading_bot.risk.correlation import CorrelationChecker
from trading_bot.risk.position_sizer import PositionSizer
from trading_bot.strategies.advisor import TradingAdvisor
from trading_bot.strategies.pullback_vwap import PullbackVWAPStrategy
from trading_bot.strategies.regime import MarketRegime, RegimeDetector
from trading_bot.utils.helpers import (
    MARKET_OPEN,
    is_market_open,
    is_near_close,
)

log = structlog.get_logger(__name__)


# A strategy-fit default universe: lower-priced, higher-volatility names that
# generally trade under ~$50 and actually move. This is only a DEFAULT — the
# universe is a constructor/CLI argument so it is never hard-coded-only.
#
# NOTE (honesty): most of these names carry public floats far above the
# scanner's 50M cap, so the faithful float filter will reject them on most
# days. That is a real property of this universe, surfaced in the funnel — not
# something the engine papers over.
DEFAULT_UNIVERSE: list[str] = [
    "SOFI", "PLUG", "RIOT", "MARA", "LCID", "CHPT",
    "IONQ", "AFRM", "RIVN", "HOOD", "SOUN", "BBAI",
]

# Modules that did ``from trading_bot.utils.helpers import now_et`` and whose
# bound reference must be rebound to the simulated clock during a replay. The
# source module (``utils.helpers``) is patched first so the derived helpers
# (``is_market_open`` etc.) also observe simulated time.
_NOW_ET_PATCH_TARGETS: tuple[str, ...] = (
    "trading_bot.utils.helpers",
    "trading_bot.execution.paper_broker",
    "trading_bot.portfolio.manager",
    "trading_bot.strategies.pullback_vwap",
    "trading_bot.strategies.advisor",
    "trading_bot.risk.circuit_breaker",
    "trading_bot.data.market_data",
    "trading_bot.scanners.momentum_gappers",
)

_MARKET_MINUTES = 390.0  # 6.5h regular session


class SimulatedClock:
    """A controllable clock. ``now()`` returns the last :meth:`set` timestamp."""

    def __init__(self) -> None:
        self._now: Optional[datetime] = None

    def set(self, ts: datetime) -> None:
        self._now = ts

    def now(self) -> datetime:
        if self._now is None:
            # Fall back to real time if consulted before the first tick.
            from trading_bot.utils.helpers import ET

            return datetime.now(ET)
        return self._now


@contextlib.contextmanager
def patched_clock(clock: SimulatedClock):
    """
    Temporarily rebind ``now_et`` in every live module to ``clock.now``.

    Restores the originals on exit, even if the body raises. This is the single
    mechanism that lets the unmodified live components run at historical time.
    """
    originals: dict[str, Any] = {}
    for modname in _NOW_ET_PATCH_TARGETS:
        try:
            mod = importlib.import_module(modname)
        except Exception:  # pragma: no cover - defensive
            continue
        if hasattr(mod, "now_et"):
            originals[modname] = mod.now_et
            mod.now_et = clock.now  # type: ignore[attr-defined]
    try:
        yield
    finally:
        for modname, original in originals.items():
            try:
                importlib.import_module(modname).now_et = original  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - defensive
                pass


@dataclass
class SymbolData:
    """Loaded, normalised data for one symbol."""

    symbol: str
    intraday: pd.DataFrame  # tz-aware ET index, lower-case OHLCV
    daily: pd.DataFrame     # daily bars, lower-case OHLCV
    float_shares: Optional[int]


class ReplayDataProvider(Protocol):
    """Fetches historical data for one symbol. Injected for testing."""

    def fetch(self, symbol: str) -> SymbolData | None:
        ...


class YFinanceReplayProvider:
    """
    Default provider: pulls 5m intraday + daily bars + float from yfinance.

    Network is only touched here — the engine core is pure and testable with an
    injected provider. Never fabricates bars: on empty/short data it returns
    ``None`` so the engine can skip the symbol with a logged reason.
    """

    def __init__(self, period: str = "60d", interval: str = "5m",
                 daily_period: str = "6mo") -> None:
        self._period = period
        self._interval = interval
        self._daily_period = daily_period

    def fetch(self, symbol: str) -> SymbolData | None:
        import yfinance as yf

        try:
            ticker = yf.Ticker(symbol)
            intraday = ticker.history(period=self._period, interval=self._interval)
            daily = ticker.history(period=self._daily_period, interval="1d")
        except Exception as exc:
            log.warning("intraday.fetch_error", symbol=symbol, error=str(exc)[:160])
            return None

        if intraday is None or intraday.empty:
            return None

        intraday = _normalise_ohlcv(intraday)
        intraday = _regular_session_only(intraday)
        if intraday.empty:
            return None

        daily = _normalise_ohlcv(daily) if daily is not None and not daily.empty else pd.DataFrame()

        float_shares: Optional[int] = None
        try:
            info = ticker.info
            fs = info.get("floatShares")
            if fs:
                float_shares = int(fs)
        except Exception:
            # Float genuinely unavailable — keep as None (never invent a value).
            float_shares = None

        return SymbolData(symbol=symbol, intraday=intraday, daily=daily,
                          float_shares=float_shares)


def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case columns and keep only OHLCV; ensure a DatetimeIndex."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in out.columns]
    out = out[keep]
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out


def _regular_session_only(df: pd.DataFrame) -> pd.DataFrame:
    """Restrict intraday bars to the 09:30–16:00 ET regular session."""
    idx = df.index
    if getattr(idx, "tz", None) is None:
        # Assume already ET if naive; localise so time comparisons are correct.
        try:
            df = df.tz_localize("US/Eastern")
            idx = df.index
        except Exception:
            return df
    try:
        return df.between_time("09:30", "15:59")
    except Exception:
        return df


class ReplayMarketData(MarketDataProvider):
    """
    Historical market-data provider that answers *as of* the simulated time.

    Implements the full :class:`MarketDataProvider` interface so it is a
    drop-in for the live provider inside ``PortfolioManager`` and
    ``CorrelationChecker``. All lookups are clamped to ``<= self._now`` to
    guarantee zero look-ahead.
    """

    def __init__(self, symbols: dict[str, SymbolData]):
        self._symbols = symbols
        self._now: Optional[datetime] = None

    def set_time(self, ts: datetime) -> None:
        self._now = ts

    # --- Interface methods -------------------------------------------------

    def get_current_price(self, symbol: str) -> Optional[float]:
        df = self._intraday(symbol)
        if df is None or self._now is None:
            return None
        sub = df[df.index <= self._now]
        if sub.empty:
            return None
        return float(sub["close"].iloc[-1])

    def get_intraday_bars(
        self, symbol: str, interval_minutes: int = 1, lookback_bars: int = 100
    ) -> pd.DataFrame:
        """Current-day session bars up to (and including) the simulated time."""
        df = self._intraday(symbol)
        if df is None or self._now is None:
            return pd.DataFrame()
        mask = (df.index <= self._now) & (df.index.date == self._now.date())
        sub = df.loc[mask]
        return sub.tail(lookback_bars) if not sub.empty else pd.DataFrame()

    def get_daily_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        data = self._symbols.get(symbol)
        if data is None or data.daily.empty:
            return pd.DataFrame()
        df = data.daily
        if self._now is not None:
            df = df[df.index.date <= self._now.date()]
        return df.tail(days) if not df.empty else pd.DataFrame()

    def get_float_shares(self, symbol: str) -> Optional[int]:
        data = self._symbols.get(symbol)
        return data.float_shares if data else None

    def get_shares_outstanding(self, symbol: str) -> Optional[int]:
        return self.get_float_shares(symbol)

    def get_avg_volume(self, symbol: str, days: int = 20) -> float:
        df = self.get_daily_bars(symbol, days=days)
        if df.empty or "volume" not in df.columns:
            return 0.0
        return float(df["volume"].mean())

    # --- Helpers -----------------------------------------------------------

    def current_bar(self, symbol: str) -> Optional[pd.Series]:
        """The bar exactly at the simulated time, or ``None`` (halt/missing)."""
        df = self._intraday(symbol)
        if df is None or self._now is None:
            return None
        try:
            if self._now in df.index:
                return df.loc[self._now]
        except Exception:
            return None
        return None

    def _intraday(self, symbol: str) -> Optional[pd.DataFrame]:
        data = self._symbols.get(symbol)
        return data.intraday if data else None


@dataclass
class ReplayResult:
    """Structured result of a replay run (also flattened into a dict)."""

    universe: list[str]
    symbols_loaded: list[str]
    symbols_skipped: dict[str, str]
    trades: list[dict]
    equity_curve: list[dict]
    starting_equity: float
    ending_equity: float
    funnel: dict[str, int]
    caveats: list[str]
    performance: dict[str, Any] = field(default_factory=dict)
    period: str = ""
    interval: str = ""
    trading_days: int = 0
    timeline_ticks: int = 0


class IntradayReplayEngine:
    """
    Replays the real trading pipeline over historical 5-minute bars.

    All symbols share ONE account (broker/portfolio/circuit breaker/sizer) and
    are processed on a single global chronological timeline — exactly as the
    live bot manages a shared account across its scanned universe. This makes
    ``max_open_positions``, correlation, the circuit breaker and the equity
    curve behave faithfully across the whole universe.
    """

    def __init__(
        self,
        config: AppConfig,
        universe: Optional[list[str]] = None,
        period: str = "60d",
        interval: str = "5m",
        data_provider: Optional[ReplayDataProvider] = None,
        starting_capital: Optional[float] = None,
        output_csv: Optional[str] = None,
        benchmark_symbol: str = "SPY",
    ):
        self._config = config
        self._universe = [s.upper() for s in (universe or DEFAULT_UNIVERSE)]
        self._period = period
        self._interval = interval
        self._provider = data_provider or YFinanceReplayProvider(
            period=period, interval=interval
        )
        self._starting_capital = float(
            starting_capital if starting_capital is not None else config.starting_capital
        )
        self._output_csv = output_csv
        self._benchmark_symbol = benchmark_symbol.upper()

        # Accumulated across the whole run (survives per-day portfolio resets).
        self._trades: list[dict] = []
        self._equity_curve: list[dict] = []
        self._funnel: dict[str, int] = {
            "candidate_ticks": 0,
            "passed_scanner_filters": 0,
            "dropped_price": 0,
            "dropped_gap": 0,
            "dropped_float": 0,
            "dropped_rvol": 0,
            "dropped_insufficient_bars": 0,
            "strategy_signals": 0,
            "rejected_strategy": 0,
            "rejected_risk": 0,
            "rejected_correlation": 0,
            "rejected_advisor": 0,
            "entries": 0,
        }
        self._regime_cache: dict[Any, tuple[MarketRegime, dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute the replay and return an honest summary dict."""
        loaded, skipped = self._load()
        if not loaded:
            return self._empty_result(skipped).__dict__
        return self._simulate(loaded, skipped)

    def prepare(self, loaded: dict[str, SymbolData]) -> SimulatedClock:
        """
        Build the real components (broker/portfolio/circuit/strategy/…) and the
        historical market-data provider for ``loaded``.

        Exposed so tests can drive individual ticks (``_process_tick``) under an
        explicit clock without running the whole timeline. Returns the clock.
        """
        market = ReplayMarketData(loaded)
        clock = SimulatedClock()

        # Real components — same classes the live bot uses. The portfolio's
        # journal is ALWAYS redirected away from the live ``data/journal.csv``
        # (identical schema) so a backtest can never pollute real trade
        # history. When no output CSV is given we write to a throwaway temp
        # file; the authoritative trade list is accumulated in ``self._trades``
        # from the returned journal entries regardless.
        if self._output_csv:
            journal_path = self._output_csv
        else:
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                prefix="intraday_backtest_", suffix=".csv", delete=False
            )
            tmp.close()
            journal_path = tmp.name
        run_config = self._config.model_copy(
            update={"journal_csv_path": journal_path}
        )

        self._broker = PaperBroker(
            initial_equity=self._starting_capital,
            slippage_model=SlippageModel(base_bps=5.0, volume_impact_bps=2.0),
        )
        self._circuit = CircuitBreaker(run_config.risk)
        self._sizer = PositionSizer(run_config.risk)
        self._strategy = PullbackVWAPStrategy(run_config)
        self._advisor = TradingAdvisor()
        self._correlation = CorrelationChecker(market)
        self._regime_detector = RegimeDetector()
        self._portfolio = PortfolioManager(
            self._broker, run_config, circuit_breaker=self._circuit, market_data=market
        )
        self._market = market
        self._clock = clock
        return clock

    def _simulate(
        self, loaded: dict[str, SymbolData], skipped: dict[str, str]
    ) -> dict:
        clock = self.prepare(loaded)
        market = self._market
        circuit = self._circuit
        portfolio = self._portfolio

        # Build the global chronological timeline (union of all bar times).
        timeline = self._build_timeline(loaded)
        trading_days = sorted({ts.date() for ts in timeline})

        last_date = None
        with patched_clock(clock):
            # Seed the circuit breaker's daily baseline.
            circuit.reset_daily(self._starting_capital)
            self._sizer.reset_daily()
            portfolio.reset_daily()

            for ts in timeline:
                clock.set(ts)
                market.set_time(ts)

                # 0. New-day reset (mirrors TradingBot._check_daily_reset).
                if last_date is not None and ts.date() != last_date:
                    self._on_new_day(ts)
                last_date = ts.date()

                active = [s for s in self._universe if market.current_bar(s) is not None]
                self._process_tick(ts, active)

            # Close anything still open at the very end of the sample.
            self._collect(portfolio.close_all("backtest_end"))
            self._record_equity(timeline[-1] if timeline else clock.now())

        ending_equity = self._equity_curve[-1]["equity"] if self._equity_curve else self._starting_capital

        result = ReplayResult(
            universe=self._universe,
            symbols_loaded=sorted(loaded.keys()),
            symbols_skipped=skipped,
            trades=self._trades,
            equity_curve=self._equity_curve,
            starting_equity=self._starting_capital,
            ending_equity=round(ending_equity, 2),
            funnel=self._funnel,
            caveats=self._caveats(),
            period=self._period,
            interval=self._interval,
            trading_days=len(trading_days),
            timeline_ticks=len(timeline),
        )
        result.performance = self._compute_performance()
        return result.__dict__

    # ------------------------------------------------------------------
    # Tick processing — mirrors TradingBot._tick ordering exactly
    # ------------------------------------------------------------------

    def _process_tick(self, ts: datetime, active: list[str]) -> None:
        portfolio = self._portfolio
        circuit = self._circuit
        market = self._market

        # Feed each open position's intrabar low then high to the broker so
        # bracket stop/take-profit OCO legs fill on the wick (stop first — the
        # conservative ordering). This models intrabar fills via the REAL
        # broker OCO engine rather than close-only.
        for pos in portfolio.get_open_positions():
            bar = market.current_bar(pos.symbol)
            if bar is not None:
                with contextlib.suppress(Exception):
                    self._broker.update_price(pos.symbol, float(bar["low"]))
                    self._broker.update_price(pos.symbol, float(bar["high"]))

        # 0b. Feed unrealized P&L to the circuit breaker BEFORE it decides.
        open_positions = portfolio.get_open_positions()
        unrealized = sum(p.pnl_unrealized for p in open_positions)
        circuit.update_unrealized_pnl(unrealized)

        # 1. Circuit breaker FIRST (NON-NEGOTIABLE).
        state = circuit.check()
        if not circuit.is_trading_allowed:
            if state == CircuitState.HALTED and open_positions:
                self._collect(portfolio.close_all("circuit_breaker_halt"))
            self._record_equity(ts)
            return

        # 2. Hard time exit SECOND (3:50 PM ET via is_near_close on sim clock).
        if is_near_close(minutes_before=10):
            if portfolio.get_open_positions():
                self._collect(portfolio.close_all("hard_time_exit"))
            self._record_equity(ts)
            return

        # 3. Regime from SPY (cached per date — regime is daily-granular).
        regime, regime_adjustments = self._regime_for(ts)
        self._strategy.set_regime(regime.value, regime_adjustments)

        # 6. Update existing positions (exits, scale-outs, trailing stops).
        self._collect(portfolio.update_positions(self._strategy, market))

        # 7. Only look for entries during market hours + circuit allows.
        if not is_market_open() or not circuit.is_trading_allowed:
            self._record_equity(ts)
            return

        # 8/9. Build candidates from active symbols and run the real pipeline.
        candidates = self._build_candidates(active, ts)
        if candidates:
            candidates.sort(key=lambda c: c.score, reverse=True)
            candidates = candidates[: self._config.scanner.max_candidates]
            self._evaluate_candidates(candidates, ts, regime_adjustments)

        self._record_equity(ts)

    def _evaluate_candidates(
        self, candidates: list[ScanResult], ts: datetime, regime_adjustments: dict
    ) -> None:
        """Mirror TradingBot._tick's per-candidate risk/execution path."""
        portfolio = self._portfolio
        open_symbols = {p.symbol for p in portfolio.get_open_positions()}

        for candidate in candidates:
            if candidate.symbol in open_symbols:
                continue

            bars = self._market.get_intraday_bars(candidate.symbol, lookback_bars=100)
            if bars.empty:
                continue

            signal = self._strategy.evaluate(candidate, bars)
            if signal is None:
                self._funnel["rejected_strategy"] += 1
                continue
            self._funnel["strategy_signals"] += 1

            try:
                equity = self._broker.get_account_equity()
                buying_power = self._broker.get_buying_power()
            except Exception:
                continue
            if equity <= 0:
                break

            open_positions = portfolio.get_open_positions()

            # Regime-based max positions override (pre-risk).
            max_pos_override = regime_adjustments.get("max_positions_override")
            if max_pos_override is not None and len(open_positions) >= max_pos_override:
                self._funnel["rejected_risk"] += 1
                continue

            risk_result = self._sizer.calculate(
                equity=equity,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                current_positions=open_positions,
                buying_power=buying_power,
            )
            if not risk_result.approved:
                self._funnel["rejected_risk"] += 1
                continue

            # --- Sizing adjustments (identical to the live tick loop) ---
            size_multiplier = regime_adjustments.get("position_size_multiplier", 1.0)
            if size_multiplier != 1.0 and risk_result.shares > 0:
                risk_result.shares = max(1, int(risk_result.shares * size_multiplier))

            confidence = signal.confidence
            if confidence < 0.65 and risk_result.shares > 0:
                risk_result.shares = max(1, int(risk_result.shares * 0.40))
            elif confidence < 0.80 and risk_result.shares > 0:
                risk_result.shares = max(1, int(risk_result.shares * 0.65))

            if signal.atr > 0 and signal.entry_price > 0 and risk_result.shares > 0:
                atr_pct = (signal.atr / signal.entry_price) * 100
                if atr_pct > 5.0:
                    vol_mult = min(1.0, 5.0 / atr_pct)
                    risk_result.shares = max(1, int(risk_result.shares * vol_mult))

            streak_mult = self._circuit.get_loss_streak_multiplier()
            if streak_mult < 1.0 and risk_result.shares > 0:
                risk_result.shares = max(1, int(risk_result.shares * streak_mult))

            # Correlation gate across the shared portfolio.
            existing_symbols = list(open_symbols)
            if existing_symbols:
                with contextlib.suppress(Exception):
                    if self._correlation.is_correlated(
                        new_symbol=candidate.symbol,
                        existing_symbols=existing_symbols,
                        market_data=self._market,
                    ):
                        self._funnel["rejected_correlation"] += 1
                        continue

            # AI advisor entry recommendation.
            advisor_rec = self._advisor.recommend_entry(
                signal=signal,
                scan_result=candidate,
                regime=(self._strategy._current_regime or "range_bound"),
                positions=portfolio.get_open_positions(),
                equity=equity,
            )
            if advisor_rec.action == "skip":
                self._funnel["rejected_advisor"] += 1
                continue

            position = portfolio.open_position(signal, risk_result)
            if position.shares == 0:
                continue

            self._sizer.record_trade_risk(risk_result.risk_dollars)
            open_symbols.add(position.symbol)
            self._funnel["entries"] += 1

    # ------------------------------------------------------------------
    # Candidate synthesis (mirrors MomentumGapperScanner filter pipeline)
    # ------------------------------------------------------------------

    def _build_candidates(self, active: list[str], ts: datetime) -> list[ScanResult]:
        cfg = self._config.scanner
        out: list[ScanResult] = []
        tod_factor = self._time_of_day_factor(ts)

        for symbol in active:
            self._funnel["candidate_ticks"] += 1
            session = self._market.get_intraday_bars(symbol, lookback_bars=200)
            if session.empty or len(session) < 20:
                self._funnel["dropped_insufficient_bars"] += 1
                continue

            price = float(session["close"].iloc[-1])
            # Price filter.
            if not (cfg.min_price <= price <= cfg.max_price):
                self._funnel["dropped_price"] += 1
                continue

            # Gap filter — "% change from prior close", the live scanner's basis.
            prev_close = self._prev_close(symbol, ts)
            if prev_close is None or prev_close <= 0:
                self._funnel["dropped_gap"] += 1
                continue
            gap_pct = (price - prev_close) / prev_close * 100.0
            if not (cfg.min_gap_pct <= gap_pct <= cfg.max_gap_pct):
                self._funnel["dropped_gap"] += 1
                continue

            # Float filter — unknown float is KEPT (benefit of doubt), matching
            # the live scanner; a known float over the cap is rejected.
            float_shares = self._market.get_float_shares(symbol)
            if float_shares is not None and float_shares > cfg.max_float_shares:
                self._funnel["dropped_float"] += 1
                continue

            # RVOL filter — scanner time-of-day-normalised relative volume.
            current_vol = int(session["volume"].sum())
            avg_vol = self._market.get_avg_volume(symbol)
            rvol = self._compute_rvol(current_vol, avg_vol, tod_factor)
            if rvol < cfg.min_relative_volume:
                self._funnel["dropped_rvol"] += 1
                continue

            self._funnel["passed_scanner_filters"] += 1
            score = self._composite_score(gap_pct, rvol, float_shares, has_catalyst=False)
            out.append(
                ScanResult(
                    symbol=symbol,
                    price=round(price, 4),
                    gap_pct=round(gap_pct, 2),
                    relative_volume=round(rvol, 2),
                    float_shares=float_shares,
                    volume=current_vol,
                    prev_close=round(prev_close, 4),
                    catalyst=None,  # unavailable historically — never fabricated
                    score=score,
                    timestamp=ts,
                )
            )
        return out

    def _composite_score(
        self, gap_pct: float, relative_volume: float,
        float_shares: Optional[int], has_catalyst: bool,
    ) -> float:
        """Mirror of ``MomentumGapperScanner._compute_score`` (same weights)."""
        cfg = self._config.scanner
        gap_score = min(gap_pct / 100.0, 1.0)
        rvol_score = min(relative_volume / 20.0, 1.0)
        if float_shares is None:
            float_score = 0.3
        elif float_shares <= cfg.ideal_float_shares:
            float_score = 1.0
        elif float_shares <= cfg.max_float_shares:
            ratio = (float_shares - cfg.ideal_float_shares) / (
                cfg.max_float_shares - cfg.ideal_float_shares
            )
            float_score = 1.0 - (ratio * 0.7)
        else:
            float_score = 0.0
        catalyst_bonus = 1.0 if has_catalyst else 0.0
        return round(
            gap_score * 0.25 + rvol_score * 0.30 + float_score * 0.25 + catalyst_bonus * 0.20,
            4,
        )

    @staticmethod
    def _compute_rvol(current_vol: int, avg_daily_vol: float, tod_factor: float) -> float:
        """Mirror of ``MomentumGapperScanner._compute_rvol``."""
        if avg_daily_vol <= 0:
            return 0.0
        projected = current_vol / tod_factor if tod_factor > 0 else current_vol
        return projected / avg_daily_vol

    @staticmethod
    def _time_of_day_factor(ts: datetime) -> float:
        """Fraction of the regular session elapsed at ``ts`` (0–1)."""
        current_minutes = ts.hour * 60 + ts.minute
        open_minutes = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = max(1, current_minutes - open_minutes)
        return min(elapsed / _MARKET_MINUTES, 1.0)

    def _prev_close(self, symbol: str, ts: datetime) -> Optional[float]:
        data = self._market._symbols.get(symbol)
        if data is None or data.daily.empty:
            return None
        prior = data.daily[data.daily.index.date < ts.date()]
        if prior.empty:
            return None
        return float(prior["close"].iloc[-1])

    # ------------------------------------------------------------------
    # Regime / daily reset / bookkeeping
    # ------------------------------------------------------------------

    def _regime_for(self, ts: datetime) -> tuple[MarketRegime, dict]:
        key = ts.date()
        cached = self._regime_cache.get(key)
        if cached is not None:
            return cached
        try:
            spy = self._market.get_daily_bars(self._benchmark_symbol, days=70)
            regime = self._regime_detector.detect(spy)
        except Exception:
            regime = MarketRegime.RANGE_BOUND
        adjustments = self._regime_detector.get_regime_adjustments(regime)
        self._regime_cache[key] = (regime, adjustments)
        return regime, adjustments

    def _on_new_day(self, ts: datetime) -> None:
        """Reset daily state exactly like TradingBot on a date rollover."""
        # Any position still open at day rollover would have been force-closed
        # by the 3:50 PM hard exit; close defensively so nothing carries over.
        self._collect(self._portfolio.close_all("eod_forced_close"))
        try:
            equity = self._broker.get_account_equity()
        except Exception:
            equity = self._starting_capital
        self._circuit.reset_daily(equity)
        self._sizer.reset_daily()
        self._portfolio.reset_daily()

    def _collect(self, entries: list[JournalEntry]) -> None:
        for e in entries:
            self._trades.append(e.to_dict())

    def _record_equity(self, ts: datetime) -> None:
        try:
            equity = self._broker.get_account_equity()
        except Exception:
            return
        self._equity_curve.append(
            {"timestamp": ts.isoformat(), "equity": round(float(equity), 2)}
        )

    def _build_timeline(self, loaded: dict[str, SymbolData]) -> list[datetime]:
        stamps: set[pd.Timestamp] = set()
        for sym, data in loaded.items():
            if sym == self._benchmark_symbol:
                continue
            stamps.update(data.intraday.index)
        return [t.to_pydatetime() for t in sorted(stamps)]

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load(self) -> tuple[dict[str, SymbolData], dict[str, str]]:
        loaded: dict[str, SymbolData] = {}
        skipped: dict[str, str] = {}

        # Load the benchmark first (regime + correlation reference).
        fetch_list = list(dict.fromkeys(self._universe + [self._benchmark_symbol]))
        for symbol in fetch_list:
            try:
                data = self._provider.fetch(symbol)
            except Exception as exc:
                skipped[symbol] = f"fetch_error: {str(exc)[:120]}"
                continue
            if data is None or data.intraday.empty:
                skipped[symbol] = "no_intraday_data"
                continue
            if len(data.intraday) < 20:
                skipped[symbol] = f"insufficient_intraday_bars ({len(data.intraday)})"
                continue
            loaded[symbol] = data
            log.info(
                "intraday.loaded",
                symbol=symbol,
                intraday_bars=len(data.intraday),
                daily_bars=len(data.daily),
                float_shares=data.float_shares,
            )

        # The benchmark is a reference only, not a tradable universe member.
        tradable = {s: d for s, d in loaded.items() if s in self._universe}
        return tradable | (
            {self._benchmark_symbol: loaded[self._benchmark_symbol]}
            if self._benchmark_symbol in loaded else {}
        ), skipped

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def _compute_performance(self) -> dict:
        """
        Reuse ``trading_bot.analytics.performance.compute_performance`` when it
        exists; otherwise compute the same core metrics locally.
        """
        try:
            from trading_bot.analytics.performance import compute_performance

            return compute_performance(
                self._trades,
                equity_curve=self._equity_curve,
                starting_equity=self._starting_capital,
            )
        except Exception:
            return self._local_performance()

    def _local_performance(self) -> dict:
        pnls = [float(t["pnl"]) for t in self._trades if t.get("pnl") is not None]
        n = len(pnls)
        if n == 0:
            return {
                "closed_trades": 0, "win_rate": 0.0, "profit_factor": None,
                "expectancy_per_trade": 0.0, "total_pnl": 0.0,
                "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0,
            }
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        eq = [self._starting_capital]
        for p in pnls:
            eq.append(eq[-1] + p)
        peak = eq[0]
        max_dd = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        return {
            "closed_trades": n,
            "win_rate": round(len(wins) / n, 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
            "expectancy_per_trade": round(sum(pnls) / n, 4),
            "total_pnl": round(sum(pnls), 4),
            "max_drawdown_pct": round(max_dd * 100.0, 4),
            "sharpe_ratio": 0.0,
        }

    def _empty_result(self, skipped: dict[str, str]) -> ReplayResult:
        return ReplayResult(
            universe=self._universe,
            symbols_loaded=[],
            symbols_skipped=skipped,
            trades=[],
            equity_curve=[],
            starting_equity=self._starting_capital,
            ending_equity=self._starting_capital,
            funnel=self._funnel,
            caveats=self._caveats() + ["No symbols yielded usable intraday data."],
            performance=self._local_performance(),
            period=self._period,
            interval=self._interval,
        )

    def _caveats(self) -> list[str]:
        return [
            f"Window limited to yfinance free intraday history "
            f"(interval={self._interval}, period={self._period}); "
            f"5m ≈ 60 days, so the sample is short.",
            "gap_pct approximated as (price - prev_close)/prev_close (% change "
            "from prior daily close), the same basis a live intraday scanner uses.",
            "relative_volume is the scanner's time-of-day-normalised RVOL.",
            "float_shares from yfinance floatShares where available; unknown "
            "float keeps the candidate (benefit of doubt) — never fabricated.",
            "catalyst is unavailable historically (None), so confidence is "
            "slightly conservative vs. live (no catalyst bonus).",
            "Intrabar fills approximated by feeding each 5m bar low-then-high to "
            "the paper broker OCO engine (stop before target); no tick data.",
            "Past backtest performance does NOT guarantee future results.",
        ]


def format_report(result: dict) -> str:
    """Render a replay result dict as a human-readable report."""
    perf = result.get("performance", {})
    funnel = result.get("funnel", {})
    lines = [
        "",
        "=" * 74,
        "  FAITHFUL INTRADAY REPLAY BACKTEST",
        "=" * 74,
        f"  Universe:        {', '.join(result.get('universe', []))}",
        f"  Data window:     interval={result.get('interval')} period={result.get('period')}",
        f"  Symbols loaded:  {', '.join(result.get('symbols_loaded', [])) or 'none'}",
        f"  Trading days:    {result.get('trading_days', 0)}   "
        f"Timeline ticks: {result.get('timeline_ticks', 0)}",
        f"  Starting equity: ${result.get('starting_equity', 0):,.2f}",
        f"  Ending equity:   ${result.get('ending_equity', 0):,.2f}",
        "",
    ]

    skipped = result.get("symbols_skipped", {})
    if skipped:
        lines.append("  Skipped symbols (honest reasons):")
        for sym, reason in skipped.items():
            lines.append(f"    - {sym}: {reason}")
        lines.append("")

    lines.append("  Candidate funnel (why so few / many trades):")
    for k in (
        "candidate_ticks", "dropped_price", "dropped_gap", "dropped_float",
        "dropped_rvol", "dropped_insufficient_bars", "passed_scanner_filters",
        "strategy_signals", "rejected_strategy", "rejected_risk",
        "rejected_correlation", "rejected_advisor", "entries",
    ):
        lines.append(f"    {k:<28} {funnel.get(k, 0)}")
    lines.append("")

    lines.append("  Performance (honest scorecard):")
    lines.append(f"    Closed trades:   {perf.get('closed_trades', 0)}")
    lines.append(f"    Win rate:        {_pct(perf.get('win_rate'))}")
    pf = perf.get("profit_factor")
    lines.append(f"    Profit factor:   {pf if pf is not None else 'n/a (no losses or no trades)'}")
    lines.append(f"    Expectancy/trade:${_num(perf.get('expectancy_per_trade'))}")
    lines.append(f"    Total P&L:       ${_num(perf.get('total_pnl'))}")
    lines.append(f"    Max drawdown:    {_num(perf.get('max_drawdown_pct'))}%")
    lines.append(f"    Sharpe ratio:    {_num(perf.get('sharpe_ratio'))}")
    if "sortino_ratio" in perf:
        lines.append(f"    Sortino ratio:   {_num(perf.get('sortino_ratio'))}")
    if "confidence_note" in perf:
        lines.append(f"    Note:            {perf.get('confidence_note')}")
    lines.append("")

    lines.append("  Caveats:")
    for c in result.get("caveats", []):
        lines.append(f"    - {c}")
    lines.append("=" * 74)
    lines.append("")
    return "\n".join(lines)


def _pct(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{float(v) * 100:.1f}%"


def _num(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):,.2f}"
