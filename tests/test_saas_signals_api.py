"""
End-to-end tests for the /signals/* API surface.

Covers:
  * /signals/latest returns 404 when no report exists
  * /signals/latest returns full payload to premium callers
  * /signals/latest returns projected payload to free callers
  * /signals/latest returns preview payload to unauthenticated callers
  * /signals/history requires premium
  * /signals/history returns dates and metadata
  * /signals/{date} returns the report for a specific date
"""

from __future__ import annotations

import json
from datetime import date as today_date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.api.server import (
    API_KEY_ENV_VAR,
    MANIFEST_PATH_ENV_VAR,
    REPORTS_DIR_ENV_VAR,
    app,
)


VALID_PREMIUM = "secret_premium_key_for_signals_api"
VALID_FREE = "secret_free_key_for_signals_api"

SAAS_DIR_ENV = "TRADING_SAAS_REPORTS_DIR"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    """Wipe API + saas + key state between tests."""
    for name in (
        API_KEY_ENV_VAR, REPORTS_DIR_ENV_VAR, MANIFEST_PATH_ENV_VAR,
        "TRADING_API_ALLOWED_ORIGINS", "TRADING_API_RATE_LIMIT_PER_MINUTE",
        "TRADING_API_AUDIT_LOG_PATH", "TRADING_API_PREMIUM_KEYS",
        "TRADING_API_USAGE_LOG_PATH",
        "STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID_PREMIUM", "TRADING_STRIPE_PREMIUM_CACHE_PATH",
        "TRADING_FREE_MAX_REQUESTS_PER_DAY",
        "TRADING_FREE_MAX_REPORT_CALLS",
        "TRADING_API_KEYS_MANIFEST_PATH",
        "TRADING_API_KEYS_REVOKED_PATH",
        "TRADING_USAGE_ENFORCEMENT_ENABLED",
        "TRADING_FREE_DAILY_REQUEST_LIMIT",
        "TRADING_PREMIUM_DAILY_REQUEST_LIMIT",
        "TRADING_USAGE_LIMIT_EXEMPT_PATHS",
        "TRADING_PRIVATE_MODE",
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


def _premium_env(monkeypatch, tmp_path: Path) -> Path:
    """Configure VALID_PREMIUM as an env-listed premium key."""
    saas_dir = tmp_path / "saas_reports"
    monkeypatch.setenv(API_KEY_ENV_VAR, VALID_PREMIUM)
    monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", VALID_PREMIUM)
    monkeypatch.setenv(SAAS_DIR_ENV, str(saas_dir))
    return saas_dir


def _free_env(monkeypatch, tmp_path: Path) -> Path:
    """Configure VALID_FREE as a recognised free-tier key (no premium)."""
    saas_dir = tmp_path / "saas_reports"
    monkeypatch.setenv(API_KEY_ENV_VAR, VALID_FREE)
    monkeypatch.setenv(SAAS_DIR_ENV, str(saas_dir))
    return saas_dir


def _write_signal_report(saas_dir: Path, date: str, *, mode: str = "demo") -> Path:
    saas_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "saas-v1",
        "generated_at": "2026-04-28T00:00:00.000000Z",
        "report_date": date,
        "mode": mode,
        "universe": ["AAPL", "MSFT"],
        "market_data_status": {
            "provider": "demo" if mode == "demo" else "alpaca",
            "freshness": "today", "errors": [],
        },
        "summary": {
            "signal_count": 2, "bullish_count": 1, "bearish_count": 1,
            "neutral_count": 0, "average_confidence": 0.5,
            "risk_level": "medium",
        },
        "signals": [
            {
                "symbol": "AAPL", "direction": "bullish",
                "strategy": "momentum_breakout_v1", "confidence": 0.65,
                "timeframe": "1d",
                "indicators": {
                    "close": 200.0, "sma_20": 195.0, "sma_50": 190.0,
                    "volume_ratio": 1.5, "momentum_pct": 0.05,
                },
                "rationale": ["close above SMAs", "volume 1.5x"],
                "entry": 200.0, "stop_loss": 192.0, "take_profit": 216.0,
                "error": None,
            },
            {
                "symbol": "MSFT", "direction": "bearish",
                "strategy": "momentum_breakout_v1", "confidence": 0.35,
                "timeframe": "1d",
                "indicators": {"close": 100.0, "sma_20": 105.0,
                               "sma_50": 110.0, "volume_ratio": 1.4,
                               "momentum_pct": -0.09},
                "rationale": ["close below SMAs"],
                "entry": 100.0, "stop_loss": 104.0, "take_profit": 92.0,
                "error": None,
            },
        ],
        "risk": {
            "max_position_size_pct": 5.0, "max_daily_loss_pct": 2.0,
            "stop_loss_pct": 0.04, "take_profit_pct": 0.08,
            "notes": ["Not financial advice."],
        },
        "premium": {"has_full_access": True, "locked_fields": []},
        "share": {"title": "today", "summary": "demo", "url": None},
        "disclaimer": "Not financial advice.",
        "strategy": "momentum_breakout_v1",
    }
    p = saas_dir / f"signal_report_{date}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# /signals/latest
# ---------------------------------------------------------------------------


class TestSignalsLatest:
    def test_404_when_no_reports(self, client, monkeypatch, tmp_path):
        _premium_env(monkeypatch, tmp_path)
        r = client.get(
            "/signals/latest",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 404
        assert "no signal reports available" in r.json()["detail"]

    def test_premium_gets_full_report(self, client, monkeypatch, tmp_path):
        saas_dir = _premium_env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.get(
            "/signals/latest",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "demo"
        assert body["premium"]["has_full_access"] is True
        # Premium should see entry / stop / take_profit on every signal.
        for s in body["signals"]:
            assert s["entry"] is not None
            assert s["stop_loss"] is not None
            assert s["take_profit"] is not None
            assert "indicators" in s
            assert "rationale" in s
        assert body["risk"]["max_position_size_pct"] == 5.0

    def test_free_gets_projected_report(self, client, monkeypatch, tmp_path):
        saas_dir = _free_env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.get(
            "/signals/latest",
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["premium"]["has_full_access"] is False
        # No entry / stop / take_profit on free signals.
        for s in body["signals"]:
            assert "entry" not in s
            assert "stop_loss" not in s
            assert "take_profit" not in s
        # No risk block on free.
        assert "risk" not in body

    def test_unauthenticated_gets_preview(self, client, monkeypatch, tmp_path):
        saas_dir = _premium_env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.get("/signals/latest")
        assert r.status_code == 200
        body = r.json()
        assert body.get("preview") is True
        assert body["premium"]["has_full_access"] is False
        assert "get_started" in body

    def test_private_mode_rejects_demo_report(self, client, monkeypatch, tmp_path):
        saas_dir = _premium_env(monkeypatch, tmp_path)
        monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
        _write_signal_report(saas_dir, today_date.today().isoformat(), mode="demo")

        response = client.get(
            "/signals/latest",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )

        assert response.status_code == 503
        assert "demo_report_blocked" in response.json()["detail"]

    def test_private_mode_serves_current_real_report(
        self, client, monkeypatch, tmp_path,
    ):
        saas_dir = _premium_env(monkeypatch, tmp_path)
        monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
        _write_signal_report(
            saas_dir,
            today_date.today().isoformat(),
            mode="paper",
        )

        response = client.get(
            "/signals/latest",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )

        assert response.status_code == 200
        assert response.json()["market_data_status"]["provider"] == "alpaca"


# ---------------------------------------------------------------------------
# /signals/history
# ---------------------------------------------------------------------------


class TestSignalsHistory:
    def test_premium_gets_dates(self, client, monkeypatch, tmp_path):
        saas_dir = _premium_env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-26")
        _write_signal_report(saas_dir, "2026-04-27")
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.get(
            "/signals/history",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 3
        assert body["dates"] == ["2026-04-26", "2026-04-27", "2026-04-28"]
        assert body["tier"] == "premium"

    def test_free_blocked(self, client, monkeypatch, tmp_path):
        _free_env(monkeypatch, tmp_path)
        r = client.get(
            "/signals/history",
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 403
        assert "premium" in r.json()["detail"].lower()

    def test_unauthenticated_blocked(self, client, monkeypatch, tmp_path):
        _free_env(monkeypatch, tmp_path)
        r = client.get("/signals/history")
        assert r.status_code in (401, 403, 503)

    def test_premium_empty_dir_returns_empty_list(
        self, client, monkeypatch, tmp_path,
    ):
        _premium_env(monkeypatch, tmp_path)
        r = client.get(
            "/signals/history",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["dates"] == []


# ---------------------------------------------------------------------------
# /signals/{date}
# ---------------------------------------------------------------------------


class TestSignalsByDate:
    def test_premium_gets_specific_date(self, client, monkeypatch, tmp_path):
        saas_dir = _premium_env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-25")
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.get(
            "/signals/2026-04-25",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 200
        assert r.json()["report_date"] == "2026-04-25"

    def test_invalid_date_format_400(self, client, monkeypatch, tmp_path):
        _premium_env(monkeypatch, tmp_path)
        r = client.get(
            "/signals/not-a-date",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 400

    def test_missing_date_404(self, client, monkeypatch, tmp_path):
        _premium_env(monkeypatch, tmp_path)
        r = client.get(
            "/signals/2099-12-31",
            headers={"Authorization": f"Bearer {VALID_PREMIUM}"},
        )
        assert r.status_code == 404

    def test_free_gets_projected(self, client, monkeypatch, tmp_path):
        saas_dir = _free_env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.get(
            "/signals/2026-04-28",
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["premium"]["has_full_access"] is False
