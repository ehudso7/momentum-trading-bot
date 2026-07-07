"""
Phase 10.4 — tokenised public share links (viral signal cards).

Covers:
  * share_links unit surface: token generation/sanitisation,
    referrer-label sanitisation, regime derivation, the public
    sanitiser (NO premium fields), store round-trip, expiry.
  * POST /share/signal — auth required, mints a token, snapshots
    the sanitised signal, emits ``share_generated``.
  * GET /share/signal/{token} — public (no key), cache headers,
    sanitised payload, ``inbound_visit`` telemetry, uniform 404
    for malformed / unknown / expired tokens.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.api import share_links
from trading_bot.api.server import (
    API_KEY_ENV_VAR,
    MANIFEST_PATH_ENV_VAR,
    REPORTS_DIR_ENV_VAR,
    app,
)

VALID_PREMIUM = "secret_premium_key_for_share_links"
VALID_FREE = "secret_free_key_for_share_links"

SAAS_DIR_ENV = "TRADING_SAAS_REPORTS_DIR"
LINKS_ENV = share_links.SHARE_LINKS_PATH_ENV_VAR
EVENTS_ENV = "TRADING_API_SHARE_EVENTS_LOG_PATH"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    """Wipe API + saas + share-link state between tests."""
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
        "TRADING_PUBLIC_BASE_URL",
        "TRADING_SHARE_BASE_URL",
        share_links.SHARE_LINK_TTL_ENV_VAR,
        SAAS_DIR_ENV, LINKS_ENV, EVENTS_ENV,
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


def _env(monkeypatch, tmp_path: Path, *, key: str = VALID_FREE) -> Path:
    """Configure a recognised key + isolated saas/link/event stores."""
    saas_dir = tmp_path / "saas_reports"
    monkeypatch.setenv(API_KEY_ENV_VAR, key)
    monkeypatch.setenv(SAAS_DIR_ENV, str(saas_dir))
    monkeypatch.setenv(LINKS_ENV, str(tmp_path / "share_links.jsonl"))
    monkeypatch.setenv(EVENTS_ENV, str(tmp_path / "share_events.jsonl"))
    return saas_dir


FULL_SIGNAL = {
    "symbol": "AAPL", "direction": "bullish",
    "strategy": "momentum_breakout_v1", "confidence": 0.65,
    "timeframe": "1d",
    "indicators": {
        "close": 200.0, "sma_20": 195.0, "sma_50": 190.0,
        "volume_ratio": 1.5, "momentum_pct": 0.0526,
    },
    "rationale": ["close above SMAs", "volume 1.5x"],
    "entry": 200.0, "stop_loss": 192.0, "take_profit": 216.0,
    "error": None,
}


def _write_signal_report(saas_dir: Path, date: str) -> Path:
    saas_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "saas-v1",
        "generated_at": "2026-04-28T00:00:00.000000Z",
        "report_date": date,
        "mode": "demo",
        "universe": ["AAPL", "MSFT"],
        "market_data_status": {
            "provider": "demo", "freshness": "today", "errors": [],
        },
        "summary": {
            "signal_count": 2, "bullish_count": 1, "bearish_count": 0,
            "neutral_count": 1, "average_confidence": 0.65,
            "risk_level": "high",
        },
        "signals": [
            dict(FULL_SIGNAL),
            {
                "symbol": "MSFT", "direction": "neutral",
                "strategy": "momentum_breakout_v1", "confidence": 0.0,
                "timeframe": "1d",
                "indicators": {"close": 100.0, "sma_20": 105.0,
                               "sma_50": 110.0, "volume_ratio": 0.9,
                               "momentum_pct": -0.09},
                "rationale": ["trend alignment not met"],
                "entry": None, "stop_loss": None, "take_profit": None,
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


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Unit — tokens + labels
# ---------------------------------------------------------------------------


class TestTokens:
    def test_generate_token_is_urlsafe_and_sane(self):
        seen = set()
        for _ in range(50):
            token = share_links.generate_token()
            assert share_links.sanitize_token(token) == token
            seen.add(token)
        assert len(seen) == 50  # no collisions in a tiny sample

    @pytest.mark.parametrize("raw", [
        None, "", "short", "has space", "bad/char", "a" * 65,
        "semi;colon", "../../etc/passwd", "tok%2Fen",
    ])
    def test_sanitize_token_rejects_malformed(self, raw):
        assert share_links.sanitize_token(raw) is None

    def test_sanitize_token_accepts_wellformed(self):
        assert share_links.sanitize_token(" abcDEF123_- ") == "abcDEF123_-"


class TestReferrerLabel:
    def test_none_and_empty(self):
        assert share_links.sanitize_referrer_label(None) is None
        assert share_links.sanitize_referrer_label("") is None
        assert share_links.sanitize_referrer_label("@@@") is None

    def test_strips_unsafe_and_caps(self):
        cleaned = share_links.sanitize_referrer_label(
            "trader<script>@example.com " + "x" * 100
        )
        assert cleaned is not None
        assert "@" not in cleaned
        assert "<" not in cleaned
        assert len(cleaned) <= share_links.REFERRER_LABEL_MAX_LENGTH

    def test_keeps_friendly_labels(self):
        assert (
            share_links.sanitize_referrer_label("Momentum Mike") ==
            "Momentum Mike"
        )


# ---------------------------------------------------------------------------
# Unit — sanitiser + regime
# ---------------------------------------------------------------------------


class TestBuildPublicSignal:
    def test_shape_and_values(self):
        report = {
            "report_date": "2026-04-28",
            "summary": {"bullish_count": 3, "bearish_count": 1},
        }
        public = share_links.build_public_signal(
            dict(FULL_SIGNAL), report=report, referrer_label="Mike",
        )
        assert public == {
            "symbol": "AAPL",
            "direction": "bullish",
            "score": 65,
            "gap_pct": 5.26,
            "regime": "risk-on",
            "date": "2026-04-28",
            "referrer_label": "Mike",
        }

    def test_never_contains_premium_fields(self):
        public = share_links.build_public_signal(
            dict(FULL_SIGNAL), report={"report_date": "2026-04-28"},
        )
        for forbidden in share_links.FORBIDDEN_PUBLIC_FIELDS:
            assert forbidden not in public
        # And nothing nested either — the payload is flat scalars.
        assert all(
            not isinstance(v, (dict, list)) for v in public.values()
        )

    def test_defensive_defaults(self):
        public = share_links.build_public_signal(
            {"symbol": "nvda", "direction": "SIDEWAYS",
             "confidence": "not-a-number"},
        )
        assert public["symbol"] == "NVDA"
        assert public["direction"] == "neutral"
        assert public["score"] == 0
        assert public["gap_pct"] is None
        assert public["regime"] == "mixed"

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            share_links.build_public_signal("AAPL")  # type: ignore[arg-type]

    @pytest.mark.parametrize("summary,expected", [
        ({"bullish_count": 2, "bearish_count": 0}, "risk-on"),
        ({"bullish_count": 0, "bearish_count": 2}, "risk-off"),
        ({"bullish_count": 1, "bearish_count": 1}, "mixed"),
        (None, "mixed"),
        ({"bullish_count": "x", "bearish_count": None}, "mixed"),
    ])
    def test_derive_regime(self, summary, expected):
        assert share_links.derive_regime(summary) == expected


# ---------------------------------------------------------------------------
# Unit — store round-trip + expiry
# ---------------------------------------------------------------------------


class TestStore:
    def test_create_then_lookup(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LINKS_ENV, str(tmp_path / "links.jsonl"))
        record = share_links.create_share_link(
            key_hash="a" * 32,
            signal=dict(FULL_SIGNAL),
            report={"report_date": "2026-04-28",
                    "summary": {"bullish_count": 1, "bearish_count": 0}},
            referrer_label="Mike",
        )
        found = share_links.lookup_share_link(record["token"])
        assert found is not None
        assert found["signal"]["symbol"] == "AAPL"
        assert found["api_key_hash"] == "a" * 32
        # Store rows are sanitised at WRITE time.
        for forbidden in share_links.FORBIDDEN_PUBLIC_FIELDS:
            assert forbidden not in found["signal"]

    def test_requires_key_hash(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LINKS_ENV, str(tmp_path / "links.jsonl"))
        with pytest.raises(ValueError):
            share_links.create_share_link(
                key_hash="", signal=dict(FULL_SIGNAL),
            )

    def test_expired_lookup_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LINKS_ENV, str(tmp_path / "links.jsonl"))
        old = datetime.now(timezone.utc) - timedelta(days=90)
        record = share_links.create_share_link(
            key_hash="b" * 32, signal=dict(FULL_SIGNAL), now=old,
        )
        assert share_links.lookup_share_link(record["token"]) is None

    def test_unknown_and_malformed_lookups(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LINKS_ENV, str(tmp_path / "links.jsonl"))
        assert share_links.lookup_share_link("nope not a token") is None
        assert share_links.lookup_share_link("A" * 22) is None
        assert share_links.lookup_share_link(None) is None

    def test_ttl_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LINKS_ENV, str(tmp_path / "links.jsonl"))
        monkeypatch.setenv(share_links.SHARE_LINK_TTL_ENV_VAR, "1")
        now = datetime.now(timezone.utc)
        record = share_links.create_share_link(
            key_hash="c" * 32, signal=dict(FULL_SIGNAL), now=now,
        )
        assert share_links.lookup_share_link(record["token"]) is not None
        assert share_links.lookup_share_link(
            record["token"], now=now + timedelta(days=2),
        ) is None

    def test_bad_ttl_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(share_links.SHARE_LINK_TTL_ENV_VAR, "banana")
        assert share_links.link_ttl_days() == (
            share_links.DEFAULT_SHARE_LINK_TTL_DAYS
        )
        monkeypatch.setenv(share_links.SHARE_LINK_TTL_ENV_VAR, "-3")
        assert share_links.link_ttl_days() == (
            share_links.DEFAULT_SHARE_LINK_TTL_DAYS
        )

    def test_corrupt_store_rows_are_skipped(self, tmp_path, monkeypatch):
        store = tmp_path / "links.jsonl"
        monkeypatch.setenv(LINKS_ENV, str(store))
        record = share_links.create_share_link(
            key_hash="d" * 32, signal=dict(FULL_SIGNAL),
        )
        with open(store, "a", encoding="utf-8") as f:
            f.write("{not json}\n\n[1,2,3]\n")
        assert share_links.lookup_share_link(record["token"]) is not None


# ---------------------------------------------------------------------------
# API — POST /share/signal
# ---------------------------------------------------------------------------


class TestCreateShareEndpoint:
    def test_requires_auth(self, client, monkeypatch, tmp_path):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.post("/share/signal", json={"symbol": "AAPL"})
        assert r.status_code in (401, 403)

    def test_rejects_unknown_key(self, client, monkeypatch, tmp_path):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.post(
            "/share/signal", json={"symbol": "AAPL"},
            headers={"Authorization": "Bearer bogus_key"},
        )
        assert r.status_code in (401, 403)

    def test_creates_sanitised_link(self, client, monkeypatch, tmp_path):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.post(
            "/share/signal",
            json={"symbol": "aapl", "label": "Momentum Mike"},
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert share_links.sanitize_token(body["token"]) == body["token"]
        assert body["path"] == f"/s/{body['token']}"
        assert body["share_url"] is None  # no base URL configured
        signal = body["signal"]
        assert signal["symbol"] == "AAPL"
        assert signal["direction"] == "bullish"
        assert signal["score"] == 65
        assert signal["gap_pct"] == 5.26
        assert signal["regime"] == "risk-on"
        assert signal["date"] == "2026-04-28"
        assert signal["referrer_label"] == "Momentum Mike"
        for forbidden in share_links.FORBIDDEN_PUBLIC_FIELDS:
            assert forbidden not in signal

    def test_share_url_uses_configured_base(
        self, client, monkeypatch, tmp_path,
    ):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        monkeypatch.setenv(
            "TRADING_SHARE_BASE_URL", "https://momentumforge.example/",
        )
        r = client.post(
            "/share/signal", json={"symbol": "AAPL"},
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["share_url"] == (
            f"https://momentumforge.example/s/{body['token']}"
        )

    def test_emits_share_generated_event(
        self, client, monkeypatch, tmp_path,
    ):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.post(
            "/share/signal", json={"symbol": "AAPL"},
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 200
        events = _read_jsonl(tmp_path / "share_events.jsonl")
        generated = [
            e for e in events if e["event"] == "share_generated"
        ]
        assert len(generated) == 1
        assert generated[0]["src"] == r.json()["token"]
        assert generated[0]["endpoint"] == "/share/signal"
        # Hash, never the raw key.
        assert VALID_FREE not in json.dumps(events)

    def test_404_when_no_reports(self, client, monkeypatch, tmp_path):
        _env(monkeypatch, tmp_path)
        r = client.post(
            "/share/signal", json={"symbol": "AAPL"},
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 404

    def test_404_for_unknown_symbol(self, client, monkeypatch, tmp_path):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.post(
            "/share/signal", json={"symbol": "TSLA"},
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 404
        assert "TSLA" in r.json()["detail"]

    @pytest.mark.parametrize("symbol", ["", "  ", "TOOLONGSYM", "AA PL", "1;DROP"])
    def test_422_for_invalid_symbol(
        self, client, monkeypatch, tmp_path, symbol,
    ):
        saas_dir = _env(monkeypatch, tmp_path)
        _write_signal_report(saas_dir, "2026-04-28")
        r = client.post(
            "/share/signal", json={"symbol": symbol},
            headers={"Authorization": f"Bearer {VALID_FREE}"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# API — GET /share/signal/{token} (public)
# ---------------------------------------------------------------------------


def _mint(client, monkeypatch, tmp_path, **kwargs) -> dict:
    saas_dir = _env(monkeypatch, tmp_path)
    _write_signal_report(saas_dir, "2026-04-28")
    payload = {"symbol": "AAPL", **kwargs}
    r = client.post(
        "/share/signal", json=payload,
        headers={"Authorization": f"Bearer {VALID_FREE}"},
    )
    assert r.status_code == 200
    return r.json()


class TestPublicShareEndpoint:
    def test_public_fetch_without_auth(self, client, monkeypatch, tmp_path):
        minted = _mint(client, monkeypatch, tmp_path, label="Mike")
        r = client.get(f"/share/signal/{minted['token']}")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "AAPL"
        assert body["direction"] == "bullish"
        assert body["score"] == 65
        assert body["gap_pct"] == 5.26
        assert body["regime"] == "risk-on"
        assert body["date"] == "2026-04-28"
        assert body["referrer_label"] == "Mike"
        assert body["token"] == minted["token"]
        for forbidden in share_links.FORBIDDEN_PUBLIC_FIELDS:
            assert forbidden not in body

    def test_public_cache_headers(self, client, monkeypatch, tmp_path):
        minted = _mint(client, monkeypatch, tmp_path)
        r = client.get(f"/share/signal/{minted['token']}")
        assert r.status_code == 200
        assert "public" in r.headers.get("Cache-Control", "")

    def test_unknown_token_404(self, client, monkeypatch, tmp_path):
        _env(monkeypatch, tmp_path)
        r = client.get("/share/signal/" + "A" * 22)
        assert r.status_code == 404
        assert "not found or expired" in r.json()["detail"]

    def test_malformed_token_404_not_500(
        self, client, monkeypatch, tmp_path,
    ):
        _env(monkeypatch, tmp_path)
        r = client.get("/share/signal/%2e%2e%2fsecrets")
        assert r.status_code == 404

    def test_expired_token_404(self, client, monkeypatch, tmp_path):
        _env(monkeypatch, tmp_path)
        old = datetime.now(timezone.utc) - timedelta(days=90)
        record = share_links.create_share_link(
            key_hash="e" * 32, signal=dict(FULL_SIGNAL), now=old,
        )
        r = client.get(f"/share/signal/{record['token']}")
        assert r.status_code == 404

    def test_records_inbound_visit(self, client, monkeypatch, tmp_path):
        minted = _mint(client, monkeypatch, tmp_path)
        r = client.get(f"/share/signal/{minted['token']}")
        assert r.status_code == 200
        events = _read_jsonl(tmp_path / "share_events.jsonl")
        visits = [e for e in events if e["event"] == "inbound_visit"]
        assert len(visits) == 1
        assert visits[0]["src"] == minted["token"]
        # Attribution joins on the SHARER's key hash.
        generated = [
            e for e in events if e["event"] == "share_generated"
        ]
        assert visits[0]["api_key_hash"] == generated[0]["api_key_hash"]

    def test_visit_telemetry_never_breaks_response(
        self, client, monkeypatch, tmp_path,
    ):
        minted = _mint(client, monkeypatch, tmp_path)
        # Point the events log at an unwritable path — the public
        # card must still render.
        monkeypatch.setenv(EVENTS_ENV, "/dev/null/impossible/events.jsonl")
        r = client.get(f"/share/signal/{minted['token']}")
        assert r.status_code == 200
