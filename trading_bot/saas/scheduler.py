"""Background generation and validation for private real-data reports."""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Optional

import structlog

from trading_bot.saas.market_data import PROVIDER_DEMO, selected_provider
from trading_bot.saas.report_engine import (
    generate_report,
    latest_report_path,
    load_report,
    persist_report,
)

log = structlog.get_logger(__name__)

AUTO_GENERATE_ENV_VAR = "TRADING_SAAS_AUTO_GENERATE"
GENERATION_INTERVAL_ENV_VAR = "TRADING_SAAS_GENERATION_INTERVAL_SECONDS"
DEFAULT_GENERATION_INTERVAL_SECONDS = 1_800
MIN_GENERATION_INTERVAL_SECONDS = 300
MAX_GENERATION_INTERVAL_SECONDS = 86_400

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ReportGenerationError(RuntimeError):
    """Raised when a trustworthy real-data report cannot be produced."""


def auto_generation_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    values = env if env is not None else os.environ
    return (values.get(AUTO_GENERATE_ENV_VAR) or "").strip().lower() in _TRUTHY


def generation_interval_seconds(
    env: Optional[Mapping[str, str]] = None,
) -> int:
    values = env if env is not None else os.environ
    raw = (values.get(GENERATION_INTERVAL_ENV_VAR) or "").strip()
    try:
        candidate = int(raw) if raw else DEFAULT_GENERATION_INTERVAL_SECONDS
    except ValueError:
        candidate = DEFAULT_GENERATION_INTERVAL_SECONDS
    return max(
        MIN_GENERATION_INTERVAL_SECONDS,
        min(candidate, MAX_GENERATION_INTERVAL_SECONDS),
    )


def validate_private_report(
    report: object,
    *,
    expected_provider: Optional[str] = None,
    expected_date: Optional[str] = None,
) -> tuple[bool, str]:
    """Validate that a report is current, real, and contains usable data."""
    if not isinstance(report, dict):
        return False, "report_not_an_object"
    if str(report.get("mode") or "").strip().lower() == "demo":
        return False, "demo_report_blocked"
    report_date = str(report.get("report_date") or "")
    target_date = expected_date or date.today().isoformat()
    if report_date != target_date:
        return False, "stale_report"

    status = report.get("market_data_status")
    if not isinstance(status, dict):
        return False, "market_data_status_missing"
    provider = str(status.get("provider") or "").strip().lower()
    if not provider or provider in {"unknown", PROVIDER_DEMO}:
        return False, "real_provider_missing"
    if expected_provider and provider != expected_provider:
        return False, "provider_mismatch"

    universe = report.get("universe")
    symbols = universe if isinstance(universe, list) else []
    errors = status.get("errors")
    error_rows = errors if isinstance(errors, list) else []
    if not symbols:
        return False, "universe_empty"
    if len(error_rows) >= len(symbols):
        return False, "all_market_data_requests_failed"
    return True, "ready"


def ensure_current_real_report(
    *,
    target_dir: Optional[Path] = None,
    provider: Optional[str] = None,
    generator: Callable[..., dict] = generate_report,
) -> tuple[Path, bool]:
    """Return a valid report path, generating it atomically when necessary.

    The boolean indicates whether this call generated a new report.
    """
    chosen_provider = (provider or selected_provider()).strip().lower()
    if not chosen_provider:
        raise ReportGenerationError("no real market-data provider is configured")
    if chosen_provider == PROVIDER_DEMO:
        raise ReportGenerationError("demo data is forbidden for private launch")

    today = date.today().isoformat()
    existing_path = latest_report_path(target_dir)
    if existing_path is not None:
        try:
            existing = load_report(existing_path)
        except (OSError, ValueError, TypeError):
            existing = None
        valid, _ = validate_private_report(
            existing,
            expected_provider=chosen_provider,
            expected_date=today,
        )
        if valid:
            return existing_path, False

    report = generator(provider=chosen_provider)
    valid, reason = validate_private_report(
        report,
        expected_provider=chosen_provider,
        expected_date=today,
    )
    if not valid:
        raise ReportGenerationError(f"generated report rejected: {reason}")
    return persist_report(report, target_dir=target_dir), True


def _generation_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            path, generated = ensure_current_real_report()
            log.info(
                "saas.report_ready",
                path=str(path),
                generated=generated,
                provider=selected_provider(),
            )
        except Exception as exc:
            log.error(
                "saas.report_generation_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        if stop_event.wait(generation_interval_seconds()):
            break


def start_report_scheduler(
    *,
    stop_event: Optional[threading.Event] = None,
) -> Optional[threading.Thread]:
    """Start one daemon generator when explicitly enabled by environment."""
    if not auto_generation_enabled():
        return None
    event = stop_event or threading.Event()
    thread = threading.Thread(
        target=_generation_loop,
        args=(event,),
        name="saas-report-generator",
        daemon=True,
    )
    thread.start()
    return thread

