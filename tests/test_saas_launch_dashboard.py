"""
Tests for the public launch dashboard at /launch.

Covers:
  * /launch returns 200 even with no signal report
  * /launch shows the report mode + signal counts when a report exists
  * /launch never embeds the operator's bearer key or any env secret
  * /launch is rendered as text/html
  * /launch carries the disclaimer copy
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.api.server import (
    API_KEY_ENV_VAR,
    MANIFEST_PATH_ENV_VAR,
    REPORTS_DIR_ENV_VAR,
    app,
)

VALID_KEY = "secret_launch_dashboard_key_DO_NOT_LEAK"
SAAS_DIR_ENV = "TRADING_SAAS_REPORTS_DIR"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    for name in (
        API_KEY_ENV_VAR, REPORTS_DIR_ENV_VAR, MANIFEST_PATH_ENV_VAR,
        "TRADING_API_PREMIUM_KEYS", "TRADING_USAGE_ENFORCEMENT_ENABLED",
        "STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID_PREMIUM", "TRADING_STRIPE_PREMIUM_CACHE_PATH",
        SAAS_DIR_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    audit_tmp = tmp_path_factory.mktemp("audit") / "audit.jsonl"
    usage_tmp = tmp_path_factory.mktemp("usage") / "usage.jsonl"
    upgrade_tmp = tmp_path_factory.mktemp("upgrade") / "events.jsonl"
    stripe_tmp = tmp_path_factory.mktemp("stripe") / "keys.json"
    monkeypatch.setenv("TRADING_API_AUDIT_LOG_PATH", str(audit_tmp))
    monkeypatch.setenv("TRADING_API_USAGE_LOG_PATH", str(usage_tmp))
    monkeypatch.setenv("TRADING_API_UPGRADE_EVENTS_LOG_PATH", str(upgrade_tmp))
    monkeypatch.setenv("TRADING_STRIPE_PREMIUM_CACHE_PATH", str(stripe_tmp))
    keys_manifest = tmp_path_factory.mktemp("keys") / "manifest.jsonl"
    keys_revoked = tmp_path_factory.mktemp("revoked") / "revoked.jsonl"
    monkeypatch.setenv("TRADING_API_KEYS_MANIFEST_PATH", str(keys_manifest))
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked))
    from trading_bot.api import billing as _b
    from trading_bot.api import key_store as _k
    _b.reset_cache_for_tests()
    _k.reset_caches_for_tests()


@pytest.fixture(autouse=True)
def reset_rate_limit_bucket():
    from trading_bot.api.server import _reset_rate_limit_bucket
    _reset_rate_limit_bucket()
    yield
    _reset_rate_limit_bucket()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _write_report(saas_dir: Path, date: str = "2026-04-28") -> None:
    saas_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "saas-v1",
        "generated_at": "2026-04-28T00:00:00.000000Z",
        "report_date": date,
        "mode": "demo",
        "universe": ["AAPL"],
        "market_data_status": {
            "provider": "demo", "freshness": "today", "errors": [],
        },
        "summary": {
            "signal_count": 1, "bullish_count": 1, "bearish_count": 0,
            "neutral_count": 0, "average_confidence": 0.42,
            "risk_level": "medium",
        },
        "signals": [
            {
                "symbol": "AAPL", "direction": "bullish",
                "strategy": "momentum_breakout_v1",
                "confidence": 0.42, "timeframe": "1d",
                "indicators": {}, "rationale": [],
                "entry": 100.0, "stop_loss": 96.0, "take_profit": 108.0,
            },
        ],
        "risk": {"max_position_size_pct": 5.0, "max_daily_loss_pct": 2.0,
                 "stop_loss_pct": 0.04, "take_profit_pct": 0.08, "notes": []},
        "premium": {"has_full_access": True, "locked_fields": []},
        "share": {"title": "x", "summary": "y", "url": None},
        "disclaimer": "Not financial advice.",
        "strategy": "momentum_breakout_v1",
    }
    (saas_dir / f"signal_report_{date}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


class TestLaunchDashboard:
    def test_returns_html(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv(SAAS_DIR_ENV, str(tmp_path / "saas"))
        r = client.get("/launch")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")

    def test_disclaimer_present(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv(SAAS_DIR_ENV, str(tmp_path / "saas"))
        r = client.get("/launch")
        assert r.status_code == 200
        assert "Not financial advice" in r.text or "not financial advice" in r.text.lower()

    def test_shows_empty_state_when_no_report(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv(SAAS_DIR_ENV, str(tmp_path / "saas"))
        r = client.get("/launch")
        assert "No signal report has been generated" in r.text

    def test_shows_summary_when_report_exists(self, client, monkeypatch, tmp_path):
        saas_dir = tmp_path / "saas"
        monkeypatch.setenv(SAAS_DIR_ENV, str(saas_dir))
        _write_report(saas_dir)
        r = client.get("/launch")
        assert r.status_code == 200
        assert "AAPL" in r.text
        assert "demo" in r.text.lower()
        assert "bull=1" in r.text or "bullish_count" in r.text or "1" in r.text

    def test_does_not_embed_secrets(self, client, monkeypatch, tmp_path):
        # Set an env-listed premium key and confirm /launch never echoes it.
        monkeypatch.setenv(API_KEY_ENV_VAR, VALID_KEY)
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", VALID_KEY)
        monkeypatch.setenv(SAAS_DIR_ENV, str(tmp_path / "saas"))
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_DO_NOT_LEAK_THIS_VALUE")
        r = client.get("/launch")
        assert r.status_code == 200
        assert VALID_KEY not in r.text
        assert "sk_test_DO_NOT_LEAK_THIS_VALUE" not in r.text

    def test_full_signal_details_hidden_in_preview(
        self, client, monkeypatch, tmp_path,
    ):
        saas_dir = tmp_path / "saas"
        monkeypatch.setenv(SAAS_DIR_ENV, str(saas_dir))
        _write_report(saas_dir)
        r = client.get("/launch")
        # Free preview hides entry/stop/target — they shouldn't appear
        # as table columns. The numeric values (100, 96, 108) might
        # appear in confidence rounding etc., but the explicit
        # "stop_loss" or "take_profit" column header must NOT.
        assert "stop_loss" not in r.text
        assert "take_profit" not in r.text
