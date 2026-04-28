"""
SaaS signal-report engine.

Assembles the public SaaS report:

    {
      "generated_at": "<ISO timestamp>",
      "mode": "paper" | "live" | "demo",
      "universe": ["AAPL", "MSFT", ...],
      "market_data_status": {...},
      "summary": {...},
      "signals": [...],
      "risk": {...},
      "premium": {...},
      "share": {...},
      "schema_version": "saas-v1",
      "disclaimer": "Not financial advice."
    }

Persistence: reports are written under
``$TRADING_SAAS_REPORTS_DIR`` (default: ``data/saas_reports/``) as
``signal_report_<YYYY-MM-DD>.json``. Filenames are deterministic; a
re-run on the same date overwrites the previous file (idempotent
generation).

Free-vs-premium projection lives in ``project_for_free()``: free
callers receive a curated subset of fields and only the top-N
signals — premium callers receive the full payload.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import date as _date_type, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from trading_bot.saas import REPORT_SCHEMA_VERSION
from trading_bot.saas.market_data import (
    PROVIDER_DEMO,
    fetch_daily_bars,
    freshness_label,
    selected_provider,
)
from trading_bot.saas.strategy import (
    DEFAULT_RISK_PARAMS,
    DIRECTION_BEARISH,
    DIRECTION_BULLISH,
    DIRECTION_NEUTRAL,
    DISCLAIMER,
    RiskParams,
    STRATEGY_ID,
    Signal,
    evaluate,
)

REPORTS_DIR_ENV_VAR = "TRADING_SAAS_REPORTS_DIR"
DEFAULT_REPORTS_DIR = "data/saas_reports"

UNIVERSE_ENV_VAR = "TRADING_SAAS_UNIVERSE"
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "AVGO", "AMD", "NFLX",
)

MODE_ENV_VAR = "TRADING_RUN_MODE"
DEFAULT_MODE = "paper"
VALID_MODES = frozenset({"paper", "live", "demo"})

# Free-tier projection: which top-level fields survive, and how many
# signals get returned.
_FREE_ALLOWED_FIELDS: tuple[str, ...] = (
    "generated_at",
    "mode",
    "universe",
    "summary",
    "market_data_status",
    "schema_version",
    "disclaimer",
)
_FREE_LOCKED_FIELDS: tuple[str, ...] = (
    "signals.entry",
    "signals.stop_loss",
    "signals.take_profit",
    "signals.indicators",
    "signals.rationale",
    "risk",
    "history",
)
_FREE_SIGNAL_LIMIT = 3
_FREE_SIGNAL_SHAPE: tuple[str, ...] = (
    "symbol", "direction", "strategy", "confidence", "timeframe",
)
_FREE_UPGRADE_HINT = "Upgrade for full signal details and historical reports."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reports_dir() -> Path:
    return Path(os.getenv(REPORTS_DIR_ENV_VAR, DEFAULT_REPORTS_DIR))


def universe_from_env() -> list[str]:
    raw = (os.getenv(UNIVERSE_ENV_VAR) or "").strip()
    if not raw:
        return list(DEFAULT_UNIVERSE)
    out: list[str] = []
    for piece in raw.split(","):
        s = piece.strip().upper()
        if not s:
            continue
        # Cap symbol length defensively — real US tickers are <=5
        # chars, with a small handful of <=6 (preferred share suffixes).
        if len(s) > 8:
            continue
        out.append(s)
    return out or list(DEFAULT_UNIVERSE)


def resolve_mode() -> str:
    """
    Resolve the run mode. Demo data overrides any other mode setting —
    a report generated from demo fixtures is ALWAYS labeled ``demo``
    regardless of TRADING_RUN_MODE, so an integrator cannot confuse
    fixture data with real data.
    """
    if selected_provider() == PROVIDER_DEMO:
        return "demo"
    raw = (os.getenv(MODE_ENV_VAR) or "").strip().lower() or DEFAULT_MODE
    return raw if raw in VALID_MODES else DEFAULT_MODE


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _today_str() -> str:
    return _date_type.today().isoformat()


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(
    *,
    universe: Optional[Iterable[str]] = None,
    risk: RiskParams = DEFAULT_RISK_PARAMS,
    mode: Optional[str] = None,
    now: Optional[str] = None,
    fetch: Any = None,
    provider: Optional[str] = None,
) -> dict:
    """
    Build a SaaS report dict for the configured universe.

    Side-effect free. Use ``persist_report()`` to write the result to
    disk.

    Parameters:
        universe: explicit list of symbols. Defaults to the env
            variable ``TRADING_SAAS_UNIVERSE`` (comma separated) or
            ``DEFAULT_UNIVERSE``.
        risk: RiskParams used for stop_loss / take_profit suggestions
            and for the report's ``risk`` block.
        mode: explicit override of the report's ``mode`` field. When
            ``None``, ``resolve_mode()`` decides — and demo data
            ALWAYS forces ``mode = "demo"``.
        now: explicit ISO timestamp for testing. When ``None`` the
            current UTC time is used.
        fetch: dependency injection for tests. Must be callable
            ``fetch(symbol, lookback_days=..., provider=...) -> (bars, error)``.
        provider: explicit provider id (overrides the env-driven
            selection used by the default fetch).
    """
    fetcher = fetch if fetch is not None else fetch_daily_bars
    syms = list(universe) if universe else universe_from_env()
    syms = [s.upper().strip() for s in syms if s and s.strip()]
    chosen_provider = provider or selected_provider()
    if chosen_provider == PROVIDER_DEMO:
        report_mode = "demo"
    else:
        report_mode = mode or resolve_mode()

    signals: list[Signal] = []
    errors: list[str] = []
    latest_dates: list[str] = []
    for sym in syms:
        try:
            bars, err = fetcher(sym, provider=chosen_provider)
        except TypeError:
            # Allow simpler test stubs that don't accept the kwarg.
            bars, err = fetcher(sym)  # type: ignore[misc]
        if err:
            errors.append(f"{sym}: {err}")
        signal = evaluate(sym, bars or [], risk=risk)
        signals.append(signal)
        if bars:
            last = bars[-1].get("date")
            if isinstance(last, str):
                latest_dates.append(last)

    summary = _summarize(signals)
    freshest = max(latest_dates) if latest_dates else None
    market_data_status = {
        "provider": chosen_provider or "unknown",
        "freshness": freshness_label(freshest),
        "latest_bar_date": freshest,
        "errors": errors,
    }

    report: dict = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": now or _now_iso(),
        "report_date": _today_str(),
        "mode": report_mode,
        "universe": list(syms),
        "market_data_status": market_data_status,
        "summary": summary,
        "signals": [s.to_dict() for s in signals],
        "risk": {
            "max_position_size_pct": risk.max_position_size_pct,
            "max_daily_loss_pct": risk.max_daily_loss_pct,
            "stop_loss_pct": risk.stop_loss_pct,
            "take_profit_pct": risk.take_profit_pct,
            "notes": [
                DISCLAIMER,
                "Stop-loss and take-profit levels are mechanical suggestions.",
                "Position sizing is illustrative — calibrate against your own account.",
            ],
        },
        "premium": {
            "has_full_access": True,
            "locked_fields": [],
        },
        "share": {
            "title": _share_title(report_mode, summary),
            "summary": _share_summary(summary),
            "url": None,
        },
        "disclaimer": DISCLAIMER,
        "strategy": STRATEGY_ID,
    }
    return report


def _summarize(signals: list[Signal]) -> dict:
    bull = sum(1 for s in signals if s.direction == DIRECTION_BULLISH)
    bear = sum(1 for s in signals if s.direction == DIRECTION_BEARISH)
    neutral = sum(1 for s in signals if s.direction == DIRECTION_NEUTRAL)
    confs = [
        float(s.confidence)
        for s in signals
        if s.direction != DIRECTION_NEUTRAL
    ]
    avg_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
    if avg_conf >= 0.6:
        risk_level = "high"
    elif avg_conf >= 0.3:
        risk_level = "medium"
    else:
        risk_level = "low"
    return {
        "signal_count": len(signals),
        "bullish_count": bull,
        "bearish_count": bear,
        "neutral_count": neutral,
        "average_confidence": avg_conf,
        "risk_level": risk_level,
    }


def _share_title(mode: str, summary: dict) -> str:
    bull = int(summary.get("bullish_count", 0) or 0)
    bear = int(summary.get("bearish_count", 0) or 0)
    return f"Today's {mode} signals: {bull} bullish, {bear} bearish"


def _share_summary(summary: dict) -> str:
    return (
        f"{int(summary.get('signal_count', 0) or 0)} symbols evaluated, "
        f"avg confidence {float(summary.get('average_confidence', 0.0) or 0.0):.2f}."
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def report_filename(report_date: str) -> str:
    return f"signal_report_{report_date}.json"


def persist_report(
    report: dict,
    *,
    target_dir: Optional[Path] = None,
) -> Path:
    """
    Write ``report`` as JSON to ``<target_dir>/signal_report_<DATE>.json``.

    Creates the parent directory if needed. Returns the resolved path.
    """
    if not isinstance(report, dict):
        raise ValueError("report must be a dict")
    rd = report.get("report_date") or _today_str()
    out_dir = Path(target_dir) if target_dir is not None else reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / report_filename(str(rd))
    payload = json.dumps(report, sort_keys=False, default=str, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def load_report(path: Path) -> dict:
    """Load a persisted report. Raises on parse error."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_report_dates(target_dir: Optional[Path] = None) -> list[str]:
    """
    Return the dates (oldest → newest) of every persisted SaaS report
    in ``target_dir``. Missing directory → empty list. Malformed
    filenames are skipped.
    """
    rd = Path(target_dir) if target_dir is not None else reports_dir()
    if not rd.is_dir():
        return []
    dates: list[str] = []
    prefix = "signal_report_"
    for p in sorted(rd.glob(f"{prefix}*.json")):
        stem = p.stem
        if not stem.startswith(prefix):
            continue
        candidate = stem[len(prefix):]
        try:
            _date_type.fromisoformat(candidate)
        except ValueError:
            continue
        dates.append(candidate)
    return dates


def latest_report_path(target_dir: Optional[Path] = None) -> Optional[Path]:
    rd = Path(target_dir) if target_dir is not None else reports_dir()
    dates = list_report_dates(rd)
    if not dates:
        return None
    return rd / report_filename(dates[-1])


def report_for_date(
    report_date: str,
    *,
    target_dir: Optional[Path] = None,
) -> Optional[Path]:
    rd = Path(target_dir) if target_dir is not None else reports_dir()
    p = rd / report_filename(report_date)
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Free-vs-premium projection
# ---------------------------------------------------------------------------


def project_for_free(report: dict) -> dict:
    """
    Project a full report through the free-tier allow-list.

    Free callers receive:
      - the documented top-level subset (no risk block, no full
        signal indicators / rationales / entries),
      - at most ``_FREE_SIGNAL_LIMIT`` signals (highest-confidence
        first), each trimmed to ``_FREE_SIGNAL_SHAPE``,
      - a ``premium.locked_fields`` list naming what they're
        missing,
      - an ``upgrade`` hint string.
    """
    if not isinstance(report, dict):
        return {}
    out: dict[str, Any] = {
        f: copy.deepcopy(report[f]) for f in _FREE_ALLOWED_FIELDS if f in report
    }
    full_signals = report.get("signals") or []
    if not isinstance(full_signals, list):
        full_signals = []
    sortable = [s for s in full_signals if isinstance(s, dict)]
    sortable.sort(
        key=lambda s: float(s.get("confidence") or 0.0),
        reverse=True,
    )
    teaser: list[dict] = []
    for entry in sortable[:_FREE_SIGNAL_LIMIT]:
        teaser.append(
            {k: entry.get(k) for k in _FREE_SIGNAL_SHAPE}
        )
    out["signals"] = teaser
    out["premium"] = {
        "has_full_access": False,
        "locked_fields": list(_FREE_LOCKED_FIELDS),
    }
    out["upgrade"] = {
        "required": False,
        "reason": "limited_access",
        "hint": _FREE_UPGRADE_HINT,
    }
    out["share"] = report.get("share")
    return out
