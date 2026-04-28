"""
SaaS market-data provider selector.

Resolves to ONE of the following providers, in this priority order:
    1. polygon  — when POLYGON_API_KEY is set
    2. alpaca   — when ALPACA_API_KEY + ALPACA_API_SECRET are set
    3. yfinance — when neither paid provider is available
    4. demo     — explicit fixture mode (TRADING_SAAS_DATA_MODE=demo)

If the selected provider raises while fetching, the engine records the
error in the report's ``market_data_status.errors`` list rather than
faking values — explicit failure beats silent fabrication.

Demo mode is OPT-IN: it never activates accidentally. A report
generated in demo mode is always labeled ``mode = "demo"`` so an
integrator cannot confuse it with live data.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

PROVIDER_POLYGON = "polygon"
PROVIDER_ALPACA = "alpaca"
PROVIDER_YFINANCE = "yfinance"
PROVIDER_DEMO = "demo"

DATA_MODE_ENV_VAR = "TRADING_SAAS_DATA_MODE"
POLYGON_KEY_ENV_VAR = "POLYGON_API_KEY"
ALPACA_KEY_ENV_VAR = "ALPACA_API_KEY"
ALPACA_SECRET_ENV_VAR = "ALPACA_API_SECRET"

# Bars needed to satisfy momentum_breakout_v1 (50-day SMA + 20-day vol
# baseline) plus headroom for weekends / holidays.
MIN_BARS_REQUIRED = 50
DEFAULT_LOOKBACK_DAYS = 90


class MarketDataError(RuntimeError):
    """Raised when no provider can be selected and demo mode is off."""


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def selected_provider(env: Optional[dict] = None) -> str:
    """
    Pure resolver — returns the provider id that ``fetch_daily_bars``
    will use given the current env. ``env`` defaults to ``os.environ``;
    tests pass an explicit dict to avoid touching the live process env.
    """
    e = env if env is not None else os.environ
    raw_mode = (e.get(DATA_MODE_ENV_VAR) or "").strip().lower()
    if raw_mode == PROVIDER_DEMO:
        return PROVIDER_DEMO
    if (e.get(POLYGON_KEY_ENV_VAR) or "").strip():
        return PROVIDER_POLYGON
    if (e.get(ALPACA_KEY_ENV_VAR) or "").strip() and (
        e.get(ALPACA_SECRET_ENV_VAR) or ""
    ).strip():
        return PROVIDER_ALPACA
    if _yfinance_importable():
        return PROVIDER_YFINANCE
    return PROVIDER_DEMO if raw_mode == PROVIDER_DEMO else ""


def _yfinance_importable() -> bool:
    try:
        __import__("yfinance")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_daily_bars(
    symbol: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    provider: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch daily OHLCV bars for ``symbol``.

    Returns ``(bars, error)`` where ``bars`` is a list of dicts with
    lowercase keys ``open``/``high``/``low``/``close``/``volume`` and
    optional ``date``. ``error`` is ``None`` on success, otherwise a
    short human-readable string.

    The function never raises; the caller decides how to surface
    errors.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return ([], "empty_symbol")

    chosen = provider or selected_provider()
    if not chosen:
        return ([], "no_market_data_provider_configured")

    if chosen == PROVIDER_DEMO:
        return (_demo_bars(sym, lookback_days), None)
    if chosen == PROVIDER_YFINANCE:
        return _fetch_yfinance(sym, lookback_days)
    if chosen == PROVIDER_POLYGON:
        return _fetch_polygon(sym, lookback_days)
    if chosen == PROVIDER_ALPACA:
        return _fetch_alpaca(sym, lookback_days)
    return ([], f"unknown_provider:{chosen}")


def _fetch_yfinance(symbol: str, lookback_days: int) -> tuple[list[dict], Optional[str]]:
    try:
        import yfinance as yf
    except Exception as exc:
        return ([], f"yfinance_import_error:{type(exc).__name__}")
    try:
        # Choose a yfinance period name that covers the requested lookback.
        if lookback_days <= 30:
            period = "1mo"
        elif lookback_days <= 90:
            period = "3mo"
        elif lookback_days <= 180:
            period = "6mo"
        else:
            period = "1y"
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, auto_adjust=False)
        if hist is None or hist.empty:
            return ([], "yfinance_empty_history")
        hist = hist.rename(columns={c: str(c).lower() for c in hist.columns})
        out: list[dict] = []
        for idx, row in hist.iterrows():
            try:
                ds = idx.date().isoformat() if hasattr(idx, "date") else None
            except Exception:
                ds = None
            out.append({
                "date": ds,
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
            })
        cleaned = [b for b in out if b["close"] is not None and b["volume"] is not None]
        return (cleaned[-lookback_days:], None) if cleaned else ([], "yfinance_no_clean_rows")
    except Exception as exc:
        return ([], f"yfinance_fetch_error:{type(exc).__name__}")


def _fetch_polygon(symbol: str, lookback_days: int) -> tuple[list[dict], Optional[str]]:
    try:
        from trading_bot.data.polygon_client import PolygonClient
    except Exception as exc:
        return ([], f"polygon_import_error:{type(exc).__name__}")
    api_key = (os.getenv(POLYGON_KEY_ENV_VAR) or "").strip()
    if not api_key:
        return ([], "polygon_api_key_unset")
    try:
        client = PolygonClient(api_key=api_key)
        # Pad lookback to absorb weekends / holidays.
        from_date = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days + 30)
        ).strftime("%Y-%m-%d")
        to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        df = client.get_aggregates(
            symbol=symbol,
            multiplier=1,
            timespan="day",
            from_date=from_date,
            to_date=to_date,
            limit=lookback_days + 30,
        )
        if df is None or df.empty:
            return ([], "polygon_empty_aggregates")
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        out: list[dict] = []
        for idx, row in df.iterrows():
            ds: Optional[str] = None
            try:
                ds = idx.date().isoformat() if hasattr(idx, "date") else None
            except Exception:
                ds = None
            out.append({
                "date": ds,
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
            })
        cleaned = [b for b in out if b["close"] is not None and b["volume"] is not None]
        return (cleaned[-lookback_days:], None) if cleaned else ([], "polygon_no_clean_rows")
    except Exception as exc:
        return ([], f"polygon_fetch_error:{type(exc).__name__}")


def _fetch_alpaca(symbol: str, lookback_days: int) -> tuple[list[dict], Optional[str]]:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as exc:
        return ([], f"alpaca_import_error:{type(exc).__name__}")
    api_key = (os.getenv(ALPACA_KEY_ENV_VAR) or "").strip()
    api_secret = (os.getenv(ALPACA_SECRET_ENV_VAR) or "").strip()
    if not api_key or not api_secret:
        return ([], "alpaca_credentials_unset")
    try:
        client = StockHistoricalDataClient(api_key, api_secret)
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days + 30)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )
        bars = client.get_stock_bars(request)
        if bars is None:
            return ([], "alpaca_empty_response")
        rows: list = []
        try:
            rows = bars.data.get(symbol, [])  # type: ignore[attr-defined]
        except Exception:
            try:
                rows = list(bars[symbol])  # type: ignore[index]
            except Exception:
                rows = []
        if not rows:
            return ([], "alpaca_no_rows_for_symbol")
        out: list[dict] = []
        for r in rows:
            ts = getattr(r, "timestamp", None)
            ds: Optional[str] = None
            try:
                if isinstance(ts, datetime):
                    ds = ts.date().isoformat()
            except Exception:
                ds = None
            out.append({
                "date": ds,
                "open": _to_float(getattr(r, "open", None)),
                "high": _to_float(getattr(r, "high", None)),
                "low": _to_float(getattr(r, "low", None)),
                "close": _to_float(getattr(r, "close", None)),
                "volume": _to_float(getattr(r, "volume", None)),
            })
        cleaned = [b for b in out if b["close"] is not None and b["volume"] is not None]
        return (cleaned[-lookback_days:], None) if cleaned else ([], "alpaca_no_clean_rows")
    except Exception as exc:
        return ([], f"alpaca_fetch_error:{type(exc).__name__}")


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---------------------------------------------------------------------------
# Demo bars — deterministic, label-honest fixtures
# ---------------------------------------------------------------------------


def _demo_seed_for(symbol: str) -> int:
    """Map symbol → small int so each demo symbol's series differs."""
    h = 0
    for c in (symbol or "?").encode("utf-8"):
        h = (h * 31 + c) & 0xFFFFFFFF
    return h


def _demo_bars(symbol: str, lookback_days: int) -> list[dict]:
    """
    Generate a deterministic synthetic price series. The series is
    NOT random — given the same symbol it produces the same bars on
    every invocation. The series also visibly trends up or down with
    a volume spike on the latest bar so the strategy actually fires
    bullish/bearish on demo runs.
    """
    n = max(MIN_BARS_REQUIRED, lookback_days)
    seed = _demo_seed_for(symbol)
    base_price = 50.0 + (seed % 200)
    trend = 1.0 if (seed % 2 == 0) else -1.0
    bars: list[dict] = []
    today = date.today()
    start_day = today - timedelta(days=n)
    for i in range(n):
        day = start_day + timedelta(days=i)
        # Stronger trend so SMA20 cleanly separates from SMA50.
        drift = trend * 0.005 * i
        wobble = 0.005 * math.sin((i + (seed % 7)) / 4.0)
        ratio = 1.0 + drift + wobble
        close = base_price * ratio
        open_ = close * (1.0 + 0.001 * math.cos(i / 5.0))
        high = max(open_, close) * 1.005
        low = min(open_, close) * 0.995
        # Stable baseline volume with a clear spike on the final bar
        # so volume_ratio comfortably exceeds the 1.2 threshold.
        volume = 1_000_000 + ((seed + i) % 50_000) * 5
        if i == n - 1:
            volume = volume * 3
        bars.append({
            "date": day.isoformat(),
            "open": round(open_, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": float(volume),
        })
    return bars


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


def freshness_label(latest_bar_date: Optional[str]) -> str:
    """
    Render a human-readable freshness string for the latest bar date.

    Examples: "today", "1 day", "3 days", "older than 30 days",
    "unknown".
    """
    if not latest_bar_date:
        return "unknown"
    try:
        d = date.fromisoformat(latest_bar_date)
    except Exception:
        return "unknown"
    delta = (date.today() - d).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "1 day"
    if delta <= 30:
        return f"{delta} days"
    return "older than 30 days"
