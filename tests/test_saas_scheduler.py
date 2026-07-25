from __future__ import annotations

from datetime import date

import pytest

from trading_bot.saas.scheduler import (
    ReportGenerationError,
    auto_generation_enabled,
    ensure_current_real_report,
    generation_interval_seconds,
    validate_private_report,
)


def _real_report(*, provider: str = "alpaca", errors: list[str] | None = None):
    return {
        "report_date": date.today().isoformat(),
        "mode": "paper",
        "universe": ["AAPL", "MSFT"],
        "market_data_status": {
            "provider": provider,
            "freshness": "today",
            "latest_bar_date": date.today().isoformat(),
            "errors": errors or [],
        },
        "signals": [],
    }


def test_validate_private_report_accepts_current_real_data():
    assert validate_private_report(_real_report()) == (True, "ready")


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"mode": "demo"}, "demo_report_blocked"),
        ({"report_date": "2000-01-01"}, "stale_report"),
        (
            {"market_data_status": {"provider": "demo", "errors": []}},
            "real_provider_missing",
        ),
    ],
)
def test_validate_private_report_rejects_untrusted_data(mutation, reason):
    report = _real_report()
    report.update(mutation)
    valid, actual = validate_private_report(report)
    assert valid is False
    assert actual == reason


def test_ensure_current_real_report_generates_once(tmp_path):
    calls: list[str] = []

    def generator(*, provider: str):
        calls.append(provider)
        return _real_report(provider=provider)

    first, generated = ensure_current_real_report(
        target_dir=tmp_path,
        provider="alpaca",
        generator=generator,
    )
    second, regenerated = ensure_current_real_report(
        target_dir=tmp_path,
        provider="alpaca",
        generator=generator,
    )

    assert first == second
    assert generated is True
    assert regenerated is False
    assert calls == ["alpaca"]


def test_ensure_current_real_report_rejects_total_provider_failure(tmp_path):
    def generator(*, provider: str):
        return _real_report(
            provider=provider,
            errors=["AAPL: unavailable", "MSFT: unavailable"],
        )

    with pytest.raises(ReportGenerationError, match="all_market_data_requests_failed"):
        ensure_current_real_report(
            target_dir=tmp_path,
            provider="alpaca",
            generator=generator,
        )
    assert list(tmp_path.iterdir()) == []


def test_ensure_current_real_report_rejects_demo(tmp_path):
    with pytest.raises(ReportGenerationError, match="demo data is forbidden"):
        ensure_current_real_report(target_dir=tmp_path, provider="demo")


def test_scheduler_configuration_is_explicit_and_bounded():
    assert auto_generation_enabled({}) is False
    assert auto_generation_enabled({"TRADING_SAAS_AUTO_GENERATE": "true"}) is True
    assert generation_interval_seconds(
        {"TRADING_SAAS_GENERATION_INTERVAL_SECONDS": "1"}
    ) == 300
    assert generation_interval_seconds(
        {"TRADING_SAAS_GENERATION_INTERVAL_SECONDS": "999999"}
    ) == 86_400

