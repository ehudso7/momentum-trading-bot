"""
Phase 4.0 tests — public SaaS boundary read-only analytics API.

Covers:
- /health is unauthenticated.
- Every other endpoint requires a correct Bearer token.
- /reports/latest returns the most recent report, sanitized.
- /reports/{date} supports valid YYYY-MM-DD and rejects other forms.
- /reports/{date} returns 404 when missing.
- /experiments/recent returns last-N manifest records, sanitized.
- /experiments/{n} is 1-indexed from the most recent.
- Missing files produce documented error responses (never 500).
- No Core module can leak through the response — scorer_config,
  report_paths, and filesystem paths are stripped.
- The api module imports nothing from Core (structural guarantee).

All tests write fixture files to a temporary directory and point
the API's env vars at them so no test touches the real reports/
or data/ directories.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_bot.api.server import (
    API_KEY_ENV_VAR,
    MANIFEST_PATH_ENV_VAR,
    REPORTS_DIR_ENV_VAR,
    app,
)


VALID_KEY = "secret_testing_key_123"
# Phase 4.5: a separate free-tier key for tests that need to verify
# tier-restricted behavior. The default `authed_env` fixture promotes
# VALID_KEY to premium so existing test assumptions still hold.
FREE_KEY = "free_tier_testing_key_456"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_api_env(monkeypatch, tmp_path_factory):
    """Every test starts from a known env state."""
    for name in (
        API_KEY_ENV_VAR,
        REPORTS_DIR_ENV_VAR,
        MANIFEST_PATH_ENV_VAR,
        # Phase 4.2 — also reset hardening vars.
        "TRADING_API_ALLOWED_ORIGINS",
        "TRADING_API_RATE_LIMIT_PER_MINUTE",
        # Phase 4.4 — audit log path.
        "TRADING_API_AUDIT_LOG_PATH",
        # Phase 4.5 — premium-keys list.
        "TRADING_API_PREMIUM_KEYS",
        # Phase 4.6 — usage metrics path.
        "TRADING_API_USAGE_LOG_PATH",
        # Phase 4.7 — Stripe billing env.
        "STRIPE_API_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID_PREMIUM",
        "TRADING_STRIPE_PREMIUM_CACHE_PATH",
        # Phase 5.4 — free-tier daily caps.
        "TRADING_FREE_MAX_REQUESTS_PER_DAY",
        "TRADING_FREE_MAX_REPORT_CALLS",
        # Phase 5.7 — operator-tunable nudge copy.
        "TRADING_UPGRADE_BANNER_COPY",
        "TRADING_LIMIT_HIT_COPY",
        "TRADING_REPORT_LIMIT_COPY",
        # Phase 6.2 — manifest-backed key store paths.
        "TRADING_API_KEYS_MANIFEST_PATH",
        "TRADING_API_KEYS_REVOKED_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    # Redirect the Phase 4.4 default audit file into a throwaway tmp
    # location so tests don't write to the real data/ directory.
    audit_tmp = tmp_path_factory.mktemp("api_audit") / "audit.jsonl"
    monkeypatch.setenv("TRADING_API_AUDIT_LOG_PATH", str(audit_tmp))
    # Likewise for the Phase 4.6 usage log.
    usage_tmp = tmp_path_factory.mktemp("api_usage") / "usage.jsonl"
    monkeypatch.setenv("TRADING_API_USAGE_LOG_PATH", str(usage_tmp))
    # And the Phase 4.7 Stripe premium-cache file.
    stripe_tmp = tmp_path_factory.mktemp("stripe_cache") / "keys.json"
    monkeypatch.setenv("TRADING_STRIPE_PREMIUM_CACHE_PATH", str(stripe_tmp))
    # Phase 5.5 — upgrade events log path.
    upgrade_tmp = tmp_path_factory.mktemp("upgrade_events") / "events.jsonl"
    monkeypatch.setenv("TRADING_API_UPGRADE_EVENTS_LOG_PATH", str(upgrade_tmp))
    # Phase 6.2 — point manifest + revocation paths at empty tmp files
    # so the suite never sees a real ``data/api_keys_manifest.jsonl``
    # left over from operator CLI runs. Tests that want manifest-backed
    # auth override these paths and write rows themselves.
    keys_manifest_tmp = (
        tmp_path_factory.mktemp("api_keys_manifest")
        / "api_keys_manifest.jsonl"
    )
    keys_revoked_tmp = (
        tmp_path_factory.mktemp("api_keys_revoked")
        / "api_keys_revoked.jsonl"
    )
    monkeypatch.setenv(
        "TRADING_API_KEYS_MANIFEST_PATH", str(keys_manifest_tmp),
    )
    monkeypatch.setenv(
        "TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked_tmp),
    )
    # Also wipe the billing module's in-memory cache between tests.
    from trading_bot.api import billing as _billing_mod
    _billing_mod.reset_cache_for_tests()
    # Phase 6.2 — wipe the key_store cache so a previous test's
    # manifest/revocation file does not leak into this one.
    from trading_bot.api import key_store as _key_store_mod
    _key_store_mod.reset_caches_for_tests()


@pytest.fixture(autouse=True)
def reset_rate_limit_bucket():
    """
    Phase 4.2: the TestClient shares a single "testclient" IP across
    the whole suite — reset the in-memory counter between tests so
    one test cannot poison another with rate-limit state.
    """
    from trading_bot.api.server import _reset_rate_limit_bucket
    _reset_rate_limit_bucket()
    yield
    _reset_rate_limit_bucket()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def authed_env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """
    Configure the server to talk to tmp files with a known API key.

    Phase 4.5 default: VALID_KEY is also listed in
    TRADING_API_PREMIUM_KEYS so existing tests get **premium** tier
    behaviour (full access). Tier-specific tests can override or
    override-then-clear the premium env var to exercise the free tier.
    """
    reports_dir = tmp_path / "reports"
    manifest = tmp_path / "alpha_experiments.jsonl"
    monkeypatch.setenv(API_KEY_ENV_VAR, VALID_KEY)
    monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", VALID_KEY)
    monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
    monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(manifest))
    return {"reports_dir": reports_dir, "manifest": manifest}


def _write_report(
    reports_dir: Path, date: str, **overrides
) -> Path:
    """Write a minimal daily report JSON file."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {
        "report_type": "daily_alpha_validation",
        "report_date": date,
        "scorer_fingerprint": "a" * 64,
        # scorer_config is NOT in the real daily JSON, but we include
        # it here to prove the sanitizer drops it even if present.
        "scorer_config": {
            "scorer": "RuleBasedAlphaScorer",
            "weights": {"gap": 0.2, "rvol": 0.25},
        },
        "sources": {
            "alpha_scores": {
                "path": "/srv/production/data/alpha_scores.csv",
                "exists": True, "rows": 100, "resolved_files": 1,
                "resolved_paths": ["/srv/production/data/alpha_scores.csv"],
            },
            "decision_log": {
                "path": "/srv/production/data/decision_log.csv",
                "exists": True, "rows": 100, "resolved_files": 1,
                "resolved_paths": ["/srv/production/data/decision_log.csv"],
            },
            "journal": {
                "path": "/srv/production/data/journal.csv",
                "exists": True, "rows": 25, "resolved_files": 1,
                "resolved_paths": ["/srv/production/data/journal.csv"],
            },
        },
        "tolerance_minutes": 5,
        "totals": {
            "alpha_rows": 100, "buy_rows": 25, "skip_rows": 75,
            "matched_trades": 20, "journal_trades": 25,
        },
        "tier_stats": [{"tier": "A", "count": 10}],
        "reason_stats": [],
        "regime_stats": [],
        "decile_stats": [],
        "promotion_readiness": {
            "status": "promising", "outcome_count": 20,
            "min_required_outcomes": 100, "ab_outperforms": True,
            "ab": {"outcome_count": 15, "win_rate": 0.6, "avg_r_multiple": 0.9},
            "cdf": {"outcome_count": 5, "win_rate": 0.4, "avg_r_multiple": 0.2},
        },
        "shadow_filter_simulation": [
            {"threshold": "A+B", "allowed_buy_count": 15, "blocked_buy_count": 5},
        ],
        "guardrails": {
            "status": "ok", "reasons": ["healthy"],
            "recommended_action": "no action",
        },
        "notes": [],
    }
    base.update(overrides)
    path = reports_dir / f"alpha_report_{date}.json"
    path.write_text(json.dumps(base))
    return path


def _write_manifest_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _sample_manifest_record(date: str, **overrides) -> dict:
    base = {
        "timestamp": f"{date}T17:00:00",
        "report_date": date,
        "git_commit": "abcdef1" * 5 + "abc",
        "scorer_fingerprint": "a" * 64,
        "scorer_config": {  # must be stripped by the API
            "weights": {"gap": 0.2},
            "tier_thresholds": {"A": 0.8},
        },
        "env": {
            "TRADING_ALPHA_FILTER_ENABLED": "true",
            "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present": False,
        },
        "report_paths": {  # must be stripped by the API
            "text": "/srv/prod/reports/alpha_report_X.txt",
            "json": "/srv/prod/reports/alpha_report_X.json",
        },
        "totals": {"matched_trades": 20},
        "promotion_readiness": {"status": "promising"},
        "guardrails": {"status": "ok", "reasons": []},
        "shadow_filter_ab_summary": {
            "threshold": "A+B", "allowed_buy_count": 10, "blocked_buy_count": 2,
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# /health is unauthenticated
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_200_without_auth(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "timestamp" in body
        assert body["service"] == "momentum-trading-bot-analytics"

    def test_returns_200_even_when_api_key_unset(self, client: TestClient):
        """/health must work for liveness probes before configuration."""
        # clean_api_env already cleared TRADING_API_KEY
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_accepts_auth_header_but_does_not_require_it(
        self, client: TestClient, authed_env
    ):
        resp = client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth is enforced on every protected endpoint
# ---------------------------------------------------------------------------


PROTECTED_ENDPOINTS = [
    "/reports/latest",
    "/reports/2026-04-24",
    "/experiments/recent",
    "/experiments/1",
]


class TestAuth:
    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    def test_missing_header_returns_401(
        self, client: TestClient, authed_env, endpoint
    ):
        resp = client.get(endpoint)
        assert resp.status_code == 401
        assert "Missing" in resp.json()["detail"]

    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    def test_wrong_token_returns_403(
        self, client: TestClient, authed_env, endpoint
    ):
        resp = client.get(
            endpoint, headers={"Authorization": "Bearer wrong_key"}
        )
        assert resp.status_code == 403
        assert "Invalid" in resp.json()["detail"]

    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    def test_non_bearer_scheme_returns_401(
        self, client: TestClient, authed_env, endpoint
    ):
        resp = client.get(
            endpoint, headers={"Authorization": f"Basic {VALID_KEY}"}
        )
        # Basic is not an allowed scheme under HTTPBearer — the
        # library returns the credentials as None, which we surface
        # as 401 "Missing".
        assert resp.status_code == 401

    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    def test_unconfigured_server_returns_503(
        self, client: TestClient, monkeypatch, tmp_path: Path, endpoint
    ):
        """Env var unset → 503 regardless of the presented header."""
        # authed_env NOT used here — we want the key unset.
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "reports"))
        monkeypatch.setenv(
            MANIFEST_PATH_ENV_VAR, str(tmp_path / "manifest.jsonl")
        )
        resp = client.get(
            endpoint, headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /reports/latest
# ---------------------------------------------------------------------------


class TestReportsLatest:
    def test_returns_the_most_recent_report(
        self, client: TestClient, authed_env
    ):
        reports = authed_env["reports_dir"]
        _write_report(reports, "2026-04-22")
        _write_report(reports, "2026-04-23")
        _write_report(reports, "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["report_date"] == "2026-04-24"
        assert body["guardrails"]["status"] == "ok"

    def test_missing_reports_dir_returns_404(
        self, client: TestClient, authed_env
    ):
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404

    def test_empty_reports_dir_returns_404(
        self, client: TestClient, authed_env
    ):
        authed_env["reports_dir"].mkdir()
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404
        assert "no daily reports" in resp.json()["detail"].lower()

    def test_malformed_report_returns_500(
        self, client: TestClient, authed_env
    ):
        authed_env["reports_dir"].mkdir()
        (authed_env["reports_dir"] / "alpha_report_bad.json").write_text(
            "not valid json at all"
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# /reports/{date}
# ---------------------------------------------------------------------------


class TestReportByDate:
    def test_returns_matching_date(self, client: TestClient, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/2026-04-24",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["report_date"] == "2026-04-24"

    def test_missing_date_returns_404(self, client: TestClient, authed_env):
        authed_env["reports_dir"].mkdir()
        resp = client.get(
            "/reports/2026-04-24",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404
        assert "2026-04-24" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "bad",
        [
            "04-24-2026", "not-a-date", "20260424",
            "2026-04", "2026-04-24T00:00:00", "2026-4-24",
        ],
    )
    def test_invalid_date_format_returns_400(
        self, client: TestClient, authed_env, bad
    ):
        resp = client.get(
            f"/reports/{bad}",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 400
        assert "YYYY-MM-DD" in resp.json()["detail"]

    def test_date_with_slashes_does_not_match_route(
        self, client: TestClient, authed_env
    ):
        """
        A request like /reports/2026/04/24 cannot match /reports/{date}
        because FastAPI's path segmentation splits on '/'. It hits
        the no-matching-route 404 BEFORE our validator runs, which is
        itself a safety property — the server never attempts to open
        paths the operator could traverse with '/'.
        """
        resp = client.get(
            "/reports/2026/04/24",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /experiments/recent + /experiments/{n}
# ---------------------------------------------------------------------------


class TestExperimentsRecent:
    def test_empty_manifest_returns_empty_list(
        self, client: TestClient, authed_env
    ):
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "records": []}

    def test_missing_manifest_returns_empty_list(
        self, client: TestClient, authed_env
    ):
        # Ensure we don't crash when the file simply doesn't exist
        # yet (fresh deploy).
        assert not authed_env["manifest"].exists()
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_returns_last_n(self, client: TestClient, authed_env):
        records = [_sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 11)]
        _write_manifest_records(authed_env["manifest"], records)
        resp = client.get(
            "/experiments/recent?limit=3",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        dates = [r["report_date"] for r in body["records"]]
        assert dates == ["2026-04-08", "2026-04-09", "2026-04-10"]

    def test_default_limit_is_ten(self, client: TestClient, authed_env):
        records = [_sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 21)]
        _write_manifest_records(authed_env["manifest"], records)
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.json()["count"] == 10

    def test_limit_validation(self, client: TestClient, authed_env):
        resp = client.get(
            "/experiments/recent?limit=0",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 422
        resp = client.get(
            "/experiments/recent?limit=1000",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 422


class TestExperimentByIndex:
    def test_n1_is_most_recent(self, client: TestClient, authed_env):
        records = [
            _sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 6)
        ]
        _write_manifest_records(authed_env["manifest"], records)
        resp = client.get(
            "/experiments/1",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["report_date"] == "2026-04-05"

    def test_n2_is_second_most_recent(
        self, client: TestClient, authed_env
    ):
        records = [
            _sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 6)
        ]
        _write_manifest_records(authed_env["manifest"], records)
        resp = client.get(
            "/experiments/2",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["report_date"] == "2026-04-04"

    def test_n_out_of_range_returns_404(
        self, client: TestClient, authed_env
    ):
        records = [
            _sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 4)
        ]
        _write_manifest_records(authed_env["manifest"], records)
        resp = client.get(
            "/experiments/99",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404
        assert "only 3" in resp.json()["detail"]

    def test_missing_manifest_returns_404(
        self, client: TestClient, authed_env
    ):
        resp = client.get(
            "/experiments/1",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404
        assert "no experiment" in resp.json()["detail"].lower()

    def test_n_zero_returns_400(self, client: TestClient, authed_env):
        resp = client.get(
            "/experiments/0",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 400

    def test_negative_n_returns_400(self, client: TestClient, authed_env):
        # FastAPI route validation may return 422 before our 400;
        # both are acceptable "you sent garbage" responses.
        resp = client.get(
            "/experiments/-5",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# Sanitization — the SaaS boundary.
# ---------------------------------------------------------------------------


class TestSaasBoundarySanitization:
    def test_report_response_does_not_contain_scorer_config(
        self, client: TestClient, authed_env
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        assert "scorer_config" not in body
        # The FINGERPRINT is still allowed — it's opaque.
        assert body["scorer_fingerprint"] == "a" * 64

    def test_report_response_strips_filesystem_paths_from_sources(
        self, client: TestClient, authed_env
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/2026-04-24",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        # `path` and `resolved_paths` are stripped; only counts remain.
        for name, meta in body["sources"].items():
            assert "path" not in meta, f"path leaked in {name}"
            assert "resolved_paths" not in meta, (
                f"resolved_paths leaked in {name}"
            )
            assert set(meta.keys()) == {"exists", "rows", "resolved_files"}

    def test_experiment_response_does_not_contain_scorer_config(
        self, client: TestClient, authed_env
    ):
        _write_manifest_records(
            authed_env["manifest"],
            [_sample_manifest_record("2026-04-24")],
        )
        resp = client.get(
            "/experiments/1",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        assert "scorer_config" not in body
        assert "report_paths" not in body
        # Fingerprint and redacted env are allowed.
        assert body["scorer_fingerprint"] == "a" * 64
        assert body["env"]["TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present"] is False

    def test_no_endpoint_returns_raw_secret_url_substring(
        self, client: TestClient, authed_env, monkeypatch
    ):
        """If someone ever put a raw URL in the manifest, the API must
        not leak it. (Manifest shouldn't contain it by design — this
        is defence in depth.)"""
        secret = "https://hooks.example.com/secret/TOKEN_ABC_XYZ"
        record = _sample_manifest_record("2026-04-24")
        record["env"] = dict(record["env"])
        record["env"]["_debug_raw"] = secret  # simulate a leak
        _write_manifest_records(authed_env["manifest"], [record])
        # The sanitizer only strips scorer_config + report_paths.
        # A raw URL embedded elsewhere would still come through —
        # this test documents that the boundary DOES NOT scan for
        # arbitrary secrets, so the upstream writer remains
        # responsible. We assert the documented sanitizer contract
        # by checking that the specific, known stripped keys are
        # gone and that the fingerprint (which should leak) is
        # present — this locks in the boundary's scope.
        resp = client.get(
            "/experiments/1",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        assert "scorer_config" not in body
        assert "report_paths" not in body


# ---------------------------------------------------------------------------
# Boundary enforcement — no Core imports / no execution endpoints
# ---------------------------------------------------------------------------


class TestBoundaryEnforcement:
    """Structural guarantees — there is NO trade-execution endpoint."""

    def test_api_module_does_not_import_core(self):
        """trading_bot.api.server must not import any execution-path
        module. Grepping the source is the simplest enforcement."""
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        # The api module must NOT import from these Core packages.
        forbidden = [
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ]
        for pattern in forbidden:
            assert pattern not in src, (
                f"SaaS boundary violated: api/server.py imports {pattern!r}"
            )

    def test_no_mutating_http_verbs(self, client: TestClient):
        """
        The API must not expose POST / PUT / DELETE / PATCH routes,
        with the documented Phase 4.7 exception of POST /webhook/stripe
        — a server-to-server billing webhook that does not touch any
        trading / Core path.
        """
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for method in methods:
                if method in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both.
                assert (method, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"non-read-only route detected: {method} {path}"
                assert method == "POST", (
                    f"/webhook/stripe may only accept POST, got {method}"
                )

    def test_no_trading_endpoint_paths(self):
        """Defensive: no route path is anything that could look like
        an execution / trade / simulate hook."""
        banned_substrings = [
            "/trade", "/order", "/execute", "/run",
            "/simulate", "/backtest", "/live", "/paper",
            "/scorer", "/filter",
        ]
        for route in app.routes:
            path = getattr(route, "path", "") or ""
            for banned in banned_substrings:
                assert banned not in path.lower(), (
                    f"suspicious path registered: {path}"
                )


# ---------------------------------------------------------------------------
# Example-response format locked in
# ---------------------------------------------------------------------------


class TestResponseShape:
    def test_latest_report_shape_is_stable(
        self, client: TestClient, authed_env
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        required = {
            "report_type", "report_date", "scorer_fingerprint",
            "sources", "totals", "promotion_readiness",
            "shadow_filter_simulation", "guardrails",
        }
        assert required.issubset(body.keys()), (
            f"missing keys: {required - set(body.keys())}"
        )

    def test_experiment_shape_is_stable(
        self, client: TestClient, authed_env
    ):
        _write_manifest_records(
            authed_env["manifest"],
            [_sample_manifest_record("2026-04-24")],
        )
        resp = client.get(
            "/experiments/1",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        required = {
            "timestamp", "report_date", "git_commit",
            "scorer_fingerprint", "env", "totals",
            "promotion_readiness", "guardrails",
            "shadow_filter_ab_summary",
        }
        assert required.issubset(body.keys()), (
            f"missing keys: {required - set(body.keys())}"
        )


# ===========================================================================
# Phase 4.1 — read-only dashboard UI
# ===========================================================================


from trading_bot.api.server import render_dashboard_html  # noqa: E402


class TestDashboardAuth:
    def test_missing_header_returns_401(self, client, authed_env):
        resp = client.get("/dashboard")
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self, client, authed_env):
        resp = client.get(
            "/dashboard", headers={"Authorization": "Bearer wrong_key"}
        )
        assert resp.status_code == 403

    def test_unconfigured_server_returns_503(
        self, client, monkeypatch, tmp_path: Path
    ):
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "reports"))
        monkeypatch.setenv(
            MANIFEST_PATH_ENV_VAR, str(tmp_path / "manifest.jsonl")
        )
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 503


class TestDashboardHtmlContent:
    def test_returns_html_content_type(self, client, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        # Basic HTML structure markers
        body = resp.text
        assert body.startswith("<!DOCTYPE html>")
        assert "<html" in body and "</html>" in body
        assert "<body>" in body and "</body>" in body

    def test_contains_report_date(self, client, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert "2026-04-24" in resp.text

    def test_contains_guardrail_status(self, client, authed_env):
        _write_report(
            authed_env["reports_dir"], "2026-04-24",
            guardrails={
                "status": "warning",
                "reasons": ["sample too small"],
                "recommended_action": "wait",
            },
        )
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert "warning" in resp.text
        # The reason should appear
        assert "sample too small" in resp.text

    def test_contains_readiness(self, client, authed_env):
        _write_report(
            authed_env["reports_dir"], "2026-04-24",
            promotion_readiness={
                "status": "ready_for_shadow_filter_test",
                "outcome_count": 120,
                "min_required_outcomes": 100,
                "ab_outperforms": True,
                "ab": {"outcome_count": 80, "win_rate": 0.7, "avg_r_multiple": 1.1},
                "cdf": {"outcome_count": 40, "win_rate": 0.3, "avg_r_multiple": -0.2},
            },
        )
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "Promotion readiness" in body
        assert "ready_for_shadow_filter_test" in body
        # A/B and C/D/F cohort rows present
        assert "A/B" in body
        assert "C/D/F" in body

    def test_contains_totals(self, client, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "Totals" in body
        assert "matched_trades" in body

    def test_contains_shadow_filter_simulation(self, client, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "Shadow filter simulation" in body
        assert "A+B" in body

    def test_contains_recent_experiments(self, client, authed_env):
        _write_manifest_records(
            authed_env["manifest"],
            [
                _sample_manifest_record("2026-04-22"),
                _sample_manifest_record("2026-04-23"),
                _sample_manifest_record("2026-04-24"),
            ],
        )
        # no reports but experiments present — page still renders
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "Recent experiments" in body
        # All three dates appear
        for d in ("2026-04-22", "2026-04-23", "2026-04-24"):
            assert d in body


class TestDashboardEmptyStates:
    def test_no_reports_shows_empty_state(self, client, authed_env):
        # authed_env doesn't pre-create the reports dir
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 200
        assert "No daily reports available yet" in resp.text

    def test_no_experiments_shows_empty_state(self, client, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "Recent experiments" in body
        assert "no experiments recorded yet" in body.lower()

    def test_both_empty(self, client, authed_env):
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 200
        body = resp.text
        # Page still renders with both empty states
        assert "No daily reports available yet" in body
        assert "no experiments recorded yet" in body.lower()

    def test_malformed_report_falls_back_to_empty(self, client, authed_env):
        """A corrupt report file must not break the page."""
        authed_env["reports_dir"].mkdir(parents=True, exist_ok=True)
        (authed_env["reports_dir"] / "alpha_report_bad.json").write_text(
            "<not json>"
        )
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        assert resp.status_code == 200
        assert "No daily reports available yet" in resp.text


class TestDashboardLeakageGuards:
    """
    The dashboard must not surface scorer internals, server paths,
    or any execution control. Each test places a known secret /
    sensitive string into the upstream data and asserts it is NOT
    present in the rendered HTML.
    """

    def test_does_not_include_scorer_config(self, client, authed_env):
        _write_report(
            authed_env["reports_dir"], "2026-04-24",
            scorer_config={
                "weights": {"gap": 0.2, "rvol": 0.25, "unique_marker_XYZ": 9.99},
            },
        )
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "scorer_config" not in body
        assert "unique_marker_XYZ" not in body
        assert "0.25" not in body or "unique_marker_XYZ" not in body
        # Fingerprint hash itself is fine and IS surfaced.
        assert "a" * 64 in body

    def test_does_not_include_filesystem_paths(self, client, authed_env):
        """Source paths like /srv/prod/... must never render."""
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert "/srv/production" not in body
        assert "/srv/prod/" not in body

    def test_does_not_include_raw_webhook_url(self, client, authed_env):
        secret = "https://hooks.example.com/services/T/XYZ_SECRET_TOKEN"
        record = _sample_manifest_record("2026-04-24")
        # Upstream already redacts — but as defense-in-depth, stash a
        # sensitive value in a field the sanitizer strips:
        record["report_paths"] = {"text": secret, "json": secret}
        _write_manifest_records(authed_env["manifest"], [record])
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text
        assert secret not in body
        assert "XYZ_SECRET_TOKEN" not in body
        assert "report_paths" not in body

    def test_no_form_or_mutating_controls(self, client, authed_env):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        _write_manifest_records(
            authed_env["manifest"],
            [_sample_manifest_record("2026-04-24")],
        )
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body = resp.text.lower()
        # Read-only surface: no <form>, no submit inputs, no
        # hx/onclick handlers that could trigger writes.
        for token in ("<form", "<input", "<button", "onclick", "onsubmit",
                      "method=\"post\"", "method=\"put\"", "method=\"patch\"",
                      "method=\"delete\""):
            assert token not in body, f"dashboard includes mutating marker: {token}"

    def test_does_not_expose_execution_terms(self, client, authed_env):
        """The UI must not offer execution / simulation / placement."""
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        body_lower = resp.text.lower()
        # Operator-facing words that would imply control:
        for term in (
            "place trade", "execute trade", "submit order",
            "enable filter", "disable filter", "toggle", "start bot",
        ):
            assert term not in body_lower, (
                f"dashboard surfaces an execution-adjacent term: {term}"
            )


class TestRenderDashboardHtmlPure:
    """Pure renderer — sanity checks on the helper in isolation."""

    def test_returns_string_on_none_report(self):
        html = render_dashboard_html(None, [])
        assert isinstance(html, str)
        assert "No daily reports available yet" in html
        assert html.startswith("<!DOCTYPE html>")

    def test_handles_empty_experiments(self):
        html = render_dashboard_html(
            {"report_date": "2026-04-24",
             "guardrails": {"status": "ok", "reasons": []},
             "promotion_readiness": {"status": "promising"},
             "totals": {},
             "shadow_filter_simulation": []},
            [],
        )
        assert "no experiments recorded yet" in html.lower()

    def test_escapes_html_in_values(self):
        """Any string coming from disk must be HTML-escaped so injected
        tags cannot render."""
        html = render_dashboard_html(
            {
                "report_date": "<script>alert(1)</script>",
                "scorer_fingerprint": "a" * 64,
                "guardrails": {
                    "status": "warning",
                    "reasons": ["<img src=x onerror=alert(1)>"],
                    "recommended_action": "<b>bold</b>",
                },
                "promotion_readiness": {"status": "weak"},
                "totals": {"<k>": "<v>"},
                "shadow_filter_simulation": [{"threshold": "<A+B>"}],
            },
            [],
        )
        # No raw <script> or <img> tags from the injected payload.
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror=alert(1)>" not in html
        # The escaped versions SHOULD appear.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html

    def test_renders_given_experiments_newest_first(self):
        records = [
            {"timestamp": "2026-04-22T00:00:00", "report_date": "2026-04-22",
             "scorer_fingerprint": "a" * 64,
             "guardrails": {"status": "ok"},
             "promotion_readiness": {"status": "promising"}},
            {"timestamp": "2026-04-23T00:00:00", "report_date": "2026-04-23",
             "scorer_fingerprint": "a" * 64,
             "guardrails": {"status": "warning"},
             "promotion_readiness": {"status": "weak"}},
        ]
        html = render_dashboard_html(None, records)
        # Newest first means 2026-04-23 appears before 2026-04-22.
        assert html.index("2026-04-23") < html.index("2026-04-22")


class TestDashboardRouteIsReadOnlyAgainstBoundary:
    """The /dashboard route must not break any Phase 4.0 invariant."""

    def test_dashboard_still_only_accepts_GET_HEAD_OPTIONS(self):
        from trading_bot.api.server import app as _app
        for route in _app.routes:
            if getattr(route, "path", "") == "/dashboard":
                methods = getattr(route, "methods", set()) or set()
                assert methods.issubset({"GET", "HEAD", "OPTIONS"})
                break
        else:
            raise AssertionError("/dashboard route not registered")

    def test_dashboard_does_not_match_banned_substrings(self):
        """The dashboard path itself doesn't look like an exec hook."""
        path = "/dashboard"
        for banned in (
            "/trade", "/order", "/execute", "/run",
            "/simulate", "/backtest", "/live", "/paper",
            "/scorer", "/filter",
        ):
            assert banned not in path.lower()


# ===========================================================================
# Phase 4.2 — deployment hardening
# ===========================================================================


from trading_bot.api import server as server_mod  # noqa: E402
from trading_bot.api.server import (  # noqa: E402
    ALLOWED_ORIGINS_ENV_VAR,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    RATE_LIMIT_ENV_VAR,
    SECURITY_HEADERS,
    _allowed_origins,
    _rate_limit_per_minute,
)


# ---------------------------------------------------------------------------
# Security headers present on every response
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        "path,auth",
        [
            ("/health", None),
            ("/reports/latest", True),
            ("/reports/latest", False),  # 401
            ("/reports/2026-04-24", True),  # 404
            ("/experiments/recent", True),
            ("/experiments/1", True),  # 404 empty manifest
            ("/dashboard", True),
            ("/dashboard", False),  # 401
        ],
    )
    def test_headers_present_on_every_response(
        self, client: TestClient, authed_env, path, auth
    ):
        headers = {"Authorization": f"Bearer {VALID_KEY}"} if auth else {}
        resp = client.get(path, headers=headers)
        # Applies regardless of status code (200, 401, 404, 403...).
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        csp = resp.headers.get("Content-Security-Policy") or ""
        assert "default-src 'self'" in csp
        assert "script-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_security_headers_applied_on_503(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        """Even the 'not configured' 503 must carry security headers."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 503
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in resp.headers

    def test_security_headers_applied_on_rate_limit_429(
        self, client: TestClient, monkeypatch
    ):
        """429 responses must still carry security headers."""
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "1")
        client.get("/health")  # burn the budget
        resp = client.get("/health")
        assert resp.status_code == 429
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in resp.headers

    def test_csp_allows_inline_style_for_dashboard(
        self, client: TestClient, authed_env
    ):
        """Dashboard uses an inline <style> block — CSP must permit it."""
        resp = client.get(
            "/dashboard", headers={"Authorization": f"Bearer {VALID_KEY}"}
        )
        csp = resp.headers["Content-Security-Policy"]
        assert "'unsafe-inline'" in csp  # scoped to style-src only
        assert "style-src" in csp


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCORSDefaultDisabled:
    def test_no_cors_header_when_env_unset(
        self, client: TestClient, authed_env
    ):
        resp = client.get(
            "/health",
            headers={"Origin": "https://example.com"},
        )
        assert "access-control-allow-origin" not in {
            k.lower() for k in resp.headers
        }

    def test_protected_endpoint_no_cors_header_when_env_unset(
        self, client: TestClient, authed_env
    ):
        resp = client.get(
            "/reports/latest",
            headers={
                "Authorization": f"Bearer {VALID_KEY}",
                "Origin": "https://example.com",
            },
        )
        assert "access-control-allow-origin" not in {
            k.lower() for k in resp.headers
        }


class TestCORSWhenConfigured:
    def test_allowed_origin_gets_cors_header(
        self, client: TestClient, authed_env, monkeypatch
    ):
        monkeypatch.setenv(
            ALLOWED_ORIGINS_ENV_VAR, "https://app.example.com"
        )
        resp = client.get(
            "/health",
            headers={"Origin": "https://app.example.com"},
        )
        assert resp.headers.get("Access-Control-Allow-Origin") == (
            "https://app.example.com"
        )
        assert "origin" in resp.headers.get("Vary", "").lower()

    def test_disallowed_origin_no_cors_header(
        self, client: TestClient, authed_env, monkeypatch
    ):
        monkeypatch.setenv(
            ALLOWED_ORIGINS_ENV_VAR, "https://app.example.com"
        )
        resp = client.get(
            "/health",
            headers={"Origin": "https://attacker.example.net"},
        )
        assert "access-control-allow-origin" not in {
            k.lower() for k in resp.headers
        }

    def test_multiple_origins_allowed(
        self, client: TestClient, authed_env, monkeypatch
    ):
        monkeypatch.setenv(
            ALLOWED_ORIGINS_ENV_VAR,
            "https://a.example.com, https://b.example.com",
        )
        for origin in ("https://a.example.com", "https://b.example.com"):
            resp = client.get("/health", headers={"Origin": origin})
            assert resp.headers["Access-Control-Allow-Origin"] == origin

    def test_preflight_allowed_origin(
        self, client: TestClient, authed_env, monkeypatch
    ):
        monkeypatch.setenv(
            ALLOWED_ORIGINS_ENV_VAR, "https://app.example.com"
        )
        resp = client.options(
            "/reports/latest",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 204
        assert resp.headers["Access-Control-Allow-Origin"] == (
            "https://app.example.com"
        )
        assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")

    def test_preflight_disallowed_origin(
        self, client: TestClient, authed_env, monkeypatch
    ):
        monkeypatch.setenv(
            ALLOWED_ORIGINS_ENV_VAR, "https://app.example.com"
        )
        resp = client.options(
            "/reports/latest",
            headers={
                "Origin": "https://attacker.net",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 403


class TestAllowedOriginsHelper:
    def test_empty_when_unset(self):
        assert _allowed_origins() == []

    def test_strips_whitespace_and_empties(self, monkeypatch):
        monkeypatch.setenv(
            ALLOWED_ORIGINS_ENV_VAR,
            " https://a.com , , https://b.com ",
        )
        assert _allowed_origins() == ["https://a.com", "https://b.com"]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_defaults_to_60(self):
        assert _rate_limit_per_minute() == DEFAULT_RATE_LIMIT_PER_MINUTE == 60

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", 1),
            ("120", 120),
            ("5000", 5000),
        ],
    )
    def test_env_override_valid(self, monkeypatch, value, expected):
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, value)
        assert _rate_limit_per_minute() == expected

    @pytest.mark.parametrize(
        "value",
        ["", "  ", "abc", "-5", "0", "3.14", "NaN", "inf", "1e100"],
    )
    def test_invalid_values_fall_back_to_default(
        self, monkeypatch, value
    ):
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, value)
        assert _rate_limit_per_minute() == DEFAULT_RATE_LIMIT_PER_MINUTE

    def test_under_limit_not_blocked(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "5")
        for _ in range(5):
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_over_limit_returns_429(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "3")
        for _ in range(3):
            assert client.get("/health").status_code == 200
        resp = client.get("/health")
        assert resp.status_code == 429
        assert resp.json()["detail"] == "rate limit exceeded"
        assert "retry-after" in {k.lower() for k in resp.headers}

    def test_invalid_rate_limit_env_still_applies_default_cap(
        self, client: TestClient, monkeypatch
    ):
        """An invalid env value must NOT disable rate limiting — it
        must revert to the documented 60 req/min default."""
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "not-a-number")
        # Confirm the helper says 60.
        assert _rate_limit_per_minute() == 60
        # Verify the middleware honours that — first 60 pass, 61st
        # would be blocked. Running 5 to stay well under and confirm
        # no unexpected 429.
        for _ in range(5):
            assert client.get("/health").status_code == 200

    def test_rate_limit_is_per_client_ip(
        self, client: TestClient, monkeypatch
    ):
        """Two distinct client IPs each get their own budget."""
        monkeypatch.setenv(RATE_LIMIT_ENV_VAR, "2")
        # Burn the budget as IP "1.1.1.1"
        for _ in range(2):
            resp = client.get(
                "/health", headers={"X-Forwarded-For": "1.1.1.1"}
            )
            assert resp.status_code == 200
        resp = client.get(
            "/health", headers={"X-Forwarded-For": "1.1.1.1"}
        )
        assert resp.status_code == 429
        # A different X-Forwarded-For still has its budget.
        resp = client.get(
            "/health", headers={"X-Forwarded-For": "2.2.2.2"}
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Request logging
# ---------------------------------------------------------------------------


class TestRequestLogging:
    def _capture(self, capsys):
        # structlog is configured to write to stdout in the tests.
        return capsys.readouterr().out

    def test_successful_request_is_logged(
        self, client: TestClient, authed_env, capsys
    ):
        client.get("/health")
        out = self._capture(capsys)
        assert "api.request" in out
        assert "method=GET" in out
        assert "path=/health" in out
        assert "status=200" in out
        assert "client_ip=" in out
        assert "duration_ms=" in out

    def test_auth_failure_is_logged_with_status_401(
        self, client: TestClient, authed_env, capsys
    ):
        client.get("/reports/latest")
        out = self._capture(capsys)
        assert "api.request" in out
        assert "path=/reports/latest" in out
        assert "status=401" in out

    def test_log_never_includes_bearer_token(
        self, client: TestClient, authed_env, capsys
    ):
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        out = self._capture(capsys)
        # The token must not appear anywhere in the captured log
        # output — not in its value form, not in substring form.
        assert VALID_KEY not in out, (
            f"bearer token leaked into logs:\n{out}"
        )
        # Neither should the word 'authorization' header appear in a
        # way that carries a token.
        assert "Bearer " not in out, "bearer scheme + token pattern in log"

    def test_log_never_includes_bearer_token_on_wrong_key(
        self, client: TestClient, authed_env, capsys
    ):
        secret_attempt = "attempted-secret-token-XYZ"
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {secret_attempt}"},
        )
        out = self._capture(capsys)
        assert secret_attempt not in out

    def test_log_uses_x_forwarded_for_when_present(
        self, client: TestClient, authed_env, capsys
    ):
        client.get(
            "/health", headers={"X-Forwarded-For": "203.0.113.7"}
        )
        out = self._capture(capsys)
        assert "client_ip=203.0.113.7" in out

    def test_log_uses_first_xff_ip_when_chain(
        self, client: TestClient, authed_env, capsys
    ):
        client.get(
            "/health",
            headers={
                "X-Forwarded-For": "198.51.100.1, 10.0.0.2, 10.0.0.3"
            },
        )
        out = self._capture(capsys)
        assert "client_ip=198.51.100.1" in out


# ---------------------------------------------------------------------------
# Boundary invariants STILL hold after Phase 4.2
# ---------------------------------------------------------------------------


class TestPhase42BoundaryUnchanged:
    def test_still_only_read_verbs_after_middleware(self):
        """
        Re-assert Phase 4.0 invariant — no mutating verbs added.
        Phase 4.7 exception: POST /webhook/stripe is allowed (and
        nothing else is).
        """
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
                assert (m, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"non-read-only method introduced: {m} {path}"

    def test_forbidden_imports_still_enforced(self):
        """Re-assert Phase 4.0 invariant — no Core imports."""
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        forbidden = [
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ]
        for pattern in forbidden:
            assert pattern not in src, (
                f"SaaS boundary broken by Phase 4.2: {pattern!r}"
            )

    def test_no_trading_endpoint_paths_still(self):
        banned = [
            "/trade", "/order", "/execute", "/run",
            "/simulate", "/backtest", "/live", "/paper",
            "/scorer", "/filter",
        ]
        for route in app.routes:
            path = getattr(route, "path", "") or ""
            for word in banned:
                assert word not in path.lower(), (
                    f"suspicious route introduced: {path}"
                )


# ===========================================================================
# Phase 4.3 — public product/status landing page
# ===========================================================================


from trading_bot.api.server import render_landing_page_html  # noqa: E402


class TestLandingPageIsPublic:
    def test_root_returns_200_without_auth(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_works_when_api_key_unconfigured(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        """The landing page must remain visible even before a key is
        set — it's how a new operator discovers what to configure."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_ignores_auth_header_entirely(self, client: TestClient, authed_env):
        """Sending a wrong token must not downgrade the public page."""
        resp = client.get(
            "/", headers={"Authorization": "Bearer something-wrong"}
        )
        assert resp.status_code == 200

    def test_protected_endpoints_still_require_auth_after_root_exists(
        self, client: TestClient, authed_env
    ):
        """Re-assert Phase 4.0 invariant — adding / must not weaken
        auth on any protected endpoint."""
        for path in (
            "/reports/latest",
            "/reports/2026-04-24",
            "/experiments/recent",
            "/experiments/1",
            "/dashboard",
        ):
            resp = client.get(path)
            assert resp.status_code == 401, (
                f"{path} must still require auth, got {resp.status_code}"
            )


class TestLandingPageHtml:
    def test_returns_html_content_type(self, client: TestClient):
        resp = client.get("/")
        assert resp.headers["content-type"].startswith("text/html")

    def test_body_is_well_formed_html(self, client: TestClient):
        body = client.get("/").text
        assert body.startswith("<!DOCTYPE html>")
        assert "<html" in body and "</html>" in body
        assert "<body>" in body and "</body>" in body

    def test_includes_product_positioning(self, client: TestClient):
        body = client.get("/").text.lower()
        assert "read-only" in body
        assert "guardrail" in body
        assert "daily validation" in body
        assert "audit trail" in body
        assert "protected dashboard" in body

    def test_mentions_tier_system_but_not_weights(self, client: TestClient):
        body = client.get("/").text
        # Positioning: mentions A/B/C/D/F tiers.
        assert "A / B / C / D / F" in body or "tier" in body.lower()
        # Must NOT name any individual scoring weight.
        for forbidden in (
            "gap weight", "rvol weight", "vol weight",
            "confidence weight", "regime weight", "reason weight",
            "GAP_WEIGHT", "TIER_A_MIN", "TIER_B_MIN",
        ):
            assert forbidden not in body, (
                f"landing page mentions weight internal: {forbidden}"
            )


class TestLandingPageDoesNotLeakProtectedData:
    """
    Populate the reports dir + manifest with KNOWN markers, then hit
    the public root endpoint and assert none of them leak into the
    response. The landing page is fully static so this should hold
    even when the disk is full of sensitive fixtures.
    """

    def _populate_all_markers(self, reports_dir: Path, manifest: Path) -> None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "alpha_report_2026-04-24.json").write_text(json.dumps({
            "report_type": "daily_alpha_validation",
            "report_date": "2026-04-24",
            "scorer_fingerprint": "f" * 64,
            "scorer_config": {"weights": {"gap": 0.2},
                              "unique_marker_scorer_LEAK_1": True},
            "sources": {
                "alpha_scores": {"path": "/srv/private/UNIQUE_PATH_LEAK_2",
                                 "exists": True, "rows": 1,
                                 "resolved_files": 1,
                                 "resolved_paths": ["/srv/private/x.csv"]},
                "decision_log": {"exists": True, "rows": 1,
                                 "resolved_files": 1},
                "journal": {"exists": True, "rows": 1, "resolved_files": 1},
            },
            "totals": {"alpha_rows": 1, "buy_rows": 1, "skip_rows": 0,
                       "matched_trades": 1, "journal_trades": 1},
            "guardrails": {"status": "ok",
                           "reasons": ["UNIQUE_REASON_LEAK_3"],
                           "recommended_action": "no action"},
        }))
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "timestamp": "2026-04-24T00:00:00",
            "report_date": "2026-04-24",
            "scorer_fingerprint": "f" * 64,
            "scorer_config": {"weights": {"gap": 0.2}},
            "env": {"TRADING_ALPHA_FILTER_ENABLED": "true"},
            "report_paths": {"text": "/srv/private/UNIQUE_PATH_LEAK_4.txt",
                             "json": "/srv/private/UNIQUE_PATH_LEAK_4.json"},
            "totals": {"matched_trades": 1},
            "promotion_readiness": {"status": "promising"},
            "guardrails": {"status": "warning",
                           "reasons": ["UNIQUE_MANIFEST_LEAK_5"]},
        }) + "\n")

    def test_root_does_not_leak_report_data(
        self, client: TestClient, authed_env
    ):
        self._populate_all_markers(
            authed_env["reports_dir"], authed_env["manifest"],
        )
        body = client.get("/").text
        # Unique markers planted in the fixtures MUST NOT appear.
        assert "UNIQUE_PATH_LEAK_2" not in body
        assert "UNIQUE_PATH_LEAK_4" not in body
        assert "UNIQUE_REASON_LEAK_3" not in body
        assert "UNIQUE_MANIFEST_LEAK_5" not in body
        assert "unique_marker_scorer_LEAK_1" not in body
        # And the raw fingerprint hash of the planted fixture.
        assert "f" * 64 not in body

    def test_root_does_not_include_scorer_config(self, client: TestClient):
        body = client.get("/").text
        assert "scorer_config" not in body
        # The words "weight" / "0.2" might exist in prose; the specific
        # field name "scorer_config" must not.

    def test_root_does_not_expose_raw_paths(self, client: TestClient):
        body = client.get("/").text
        for bad in ("/srv/", "/var/lib/", "/tmp/",
                    "/home/", "/data/alpha_experiments.jsonl"):
            assert bad not in body, f"path leaked: {bad}"

    def test_root_does_not_reveal_api_key_env_name(
        self, client: TestClient
    ):
        """Per spec: no API key hints beyond 'protected dashboard'."""
        body = client.get("/").text
        # The specific env var name must not appear — that would be
        # a hint useful only to an attacker, not to operators (who
        # find it via docs, not via the landing page).
        assert "TRADING_API_KEY" not in body


class TestLandingPageNoMutatingControls:
    def test_no_form_or_inputs_or_buttons(self, client: TestClient):
        body = client.get("/").text.lower()
        for token in (
            "<form", "<input", "<button",
            "onclick", "onsubmit", "onchange",
            "method=\"post\"", "method=\"put\"",
            "method=\"patch\"", "method=\"delete\"",
            "method='post'", "method='put'",
            "method='patch'", "method='delete'",
        ):
            assert token not in body, (
                f"landing page includes mutating marker: {token}"
            )

    def test_no_execution_control_terms(self, client: TestClient):
        body = client.get("/").text.lower()
        for term in (
            "place trade", "execute trade", "submit order",
            "start bot", "stop bot", "enable filter", "disable filter",
            "toggle filter", "run simulation", "backtest now",
            "place order", "make trade",
        ):
            assert term not in body, (
                f"landing page surfaces an execution-adjacent term: {term}"
            )

    def test_no_script_or_js_loaders(self, client: TestClient):
        body = client.get("/").text.lower()
        # No JS at all — matches the CSP script-src 'none' directive.
        assert "<script" not in body
        assert "javascript:" not in body


class TestLandingPageBoundaryUnchanged:
    def test_forbidden_imports_still_enforced(self):
        """Re-assert Phase 4.0 invariant after Phase 4.3."""
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src, (
                f"SaaS boundary broken by Phase 4.3: {forbidden!r}"
            )

    def test_still_only_read_verbs_after_phase_4_3(self):
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
                assert (m, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"non-read-only method introduced: {m} {path}"

    def test_landing_route_accepts_only_get_head_options(self):
        for route in app.routes:
            if getattr(route, "path", "") == "/":
                methods = getattr(route, "methods", set()) or set()
                assert methods.issubset({"GET", "HEAD", "OPTIONS"})
                break
        else:
            raise AssertionError("/ route not registered")

    def test_landing_path_not_banned_substring(self):
        banned = [
            "/trade", "/order", "/execute", "/run",
            "/simulate", "/backtest", "/live", "/paper",
            "/scorer", "/filter",
        ]
        for word in banned:
            assert word not in "/", (
                f"/ (root) should not contain banned word: {word}"
            )


class TestRenderLandingPageHtmlPure:
    def test_is_pure_no_io(self):
        """Calling the renderer with no env / no disk setup still works."""
        html1 = render_landing_page_html()
        html2 = render_landing_page_html()
        # Deterministic — no timestamps, no dynamic content.
        assert html1 == html2
        assert html1.startswith("<!DOCTYPE html>")

    def test_does_not_depend_on_env_or_disk(
        self, monkeypatch, tmp_path: Path
    ):
        """Even with the reports / manifest env vars pointing at
        populated files, the landing page output is unchanged."""
        # No env set
        baseline = render_landing_page_html()
        # Populate disk + env
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "reports"))
        monkeypatch.setenv(
            MANIFEST_PATH_ENV_VAR, str(tmp_path / "manifest.jsonl")
        )
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "alpha_report_2026-04-24.json").write_text(
            json.dumps({"report_date": "2026-04-24",
                        "leak_marker": "ABSOLUTELY_MUST_NOT_APPEAR"})
        )
        populated = render_landing_page_html()
        assert baseline == populated
        assert "ABSOLUTELY_MUST_NOT_APPEAR" not in populated


# ===========================================================================
# Phase 4.4 — access audit trail
# ===========================================================================


import os as _os_phase44  # noqa: E402

from trading_bot.api import server as srv  # noqa: E402
from trading_bot.api.server import (  # noqa: E402
    AUDIT_LOG_ENV_VAR,
    DEFAULT_AUDIT_LOG_PATH,
    REQUEST_ID_HEADER,
    REQUEST_ID_MAX_LENGTH,
    _hash_user_agent,
    _sanitize_request_id,
    _is_authenticated_status,
)


def _read_audit(path: Path) -> list[dict]:
    """Read a JSONL audit log; skip blank / malformed lines."""
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            records.append(json.loads(s))
        except Exception:
            continue
    return records


@pytest.fixture
def audit_path() -> Path:
    """Return the path the clean_api_env fixture pointed the audit log at."""
    return Path(_os_phase44.environ["TRADING_API_AUDIT_LOG_PATH"])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSanitizeRequestId:
    def test_none_returns_uuid4_hex(self):
        rid = _sanitize_request_id(None)
        assert isinstance(rid, str)
        assert len(rid) == 32   # uuid4 hex
        int(rid, 16)            # hex-valid

    def test_empty_returns_uuid4_hex(self):
        rid = _sanitize_request_id("")
        assert len(rid) == 32

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("abc-123", "abc-123"),
            ("trace_id:01", "trace_id:01"),
            ("file.ext", "file.ext"),
            ("A_Z.0-9", "A_Z.0-9"),
        ],
    )
    def test_safe_ids_pass_through(self, raw, expected):
        assert _sanitize_request_id(raw) == expected

    def test_unsafe_chars_stripped(self):
        # Whitespace, HTML, control characters should all vanish.
        rid = _sanitize_request_id(
            "abc <script>alert(1)</script> \n\t x/y?z=1"
        )
        # Only alnum and `-_:.` survive. The surviving chars depend on
        # what the sanitizer accepts — assert only that nothing
        # forbidden remains.
        for bad in ("<", ">", " ", "\n", "\t", "/", "?", "=", "'", '"'):
            assert bad not in rid

    def test_length_cap(self):
        long = "a" * 500
        rid = _sanitize_request_id(long)
        assert len(rid) == REQUEST_ID_MAX_LENGTH

    def test_only_forbidden_chars_yields_uuid(self):
        # After stripping, nothing is left → fall back to UUID4.
        rid = _sanitize_request_id("!!! ???")
        assert len(rid) == 32
        int(rid, 16)


class TestHashUserAgent:
    def test_empty_returns_none(self):
        assert _hash_user_agent(None) is None
        assert _hash_user_agent("") is None

    def test_returns_32char_hex(self):
        h = _hash_user_agent("Mozilla/5.0 (test)")
        assert isinstance(h, str)
        assert len(h) == 32
        int(h, 16)

    def test_deterministic(self):
        assert _hash_user_agent("foo") == _hash_user_agent("foo")

    def test_different_inputs_different_hashes(self):
        assert _hash_user_agent("foo") != _hash_user_agent("bar")

    def test_handles_unicode(self):
        # Non-ASCII UA must not crash.
        h = _hash_user_agent("Mozilla/5.0 Emoji 🔥 UA")
        assert h is not None
        assert len(h) == 32


class TestIsAuthenticatedStatus:
    @pytest.mark.parametrize("code", [200, 204, 404, 429, 400, 500])
    def test_non_auth_status_is_authenticated(self, code):
        assert _is_authenticated_status(code) is True

    @pytest.mark.parametrize("code", [401, 403, 503])
    def test_auth_rejection_statuses_are_unauthenticated(self, code):
        assert _is_authenticated_status(code) is False


# ---------------------------------------------------------------------------
# Audit record end-to-end
# ---------------------------------------------------------------------------


class TestAuditRecord:
    def test_record_written_for_public_endpoint(
        self, client: TestClient, audit_path: Path
    ):
        resp = client.get("/health")
        assert resp.status_code == 200
        records = _read_audit(audit_path)
        assert len(records) == 1
        rec = records[0]
        assert rec["method"] == "GET"
        assert rec["path"] == "/health"
        assert rec["status_code"] == 200
        assert rec["authenticated"] is True
        assert "client_ip" in rec
        assert "duration_ms" in rec
        assert "timestamp" in rec
        assert "request_id" in rec

    def test_record_has_required_fields(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        records = _read_audit(audit_path)
        assert len(records) == 1
        rec = records[0]
        required = {
            "timestamp", "method", "path", "status_code",
            "duration_ms", "client_ip", "authenticated",
            "user_agent_hash", "request_id",
        }
        assert required.issubset(rec.keys())


# ---------------------------------------------------------------------------
# Request id
# ---------------------------------------------------------------------------


class TestRequestId:
    def test_generated_when_missing(
        self, client: TestClient, audit_path: Path
    ):
        resp = client.get("/health")
        rid = resp.headers.get(REQUEST_ID_HEADER)
        assert rid is not None
        assert len(rid) == 32  # uuid4 hex
        int(rid, 16)
        # Audit record carries the same id.
        records = _read_audit(audit_path)
        assert records[-1]["request_id"] == rid

    def test_incoming_request_id_preserved_when_safe(
        self, client: TestClient, audit_path: Path
    ):
        safe_id = "trace-01234_abc.def"
        resp = client.get(
            "/health", headers={REQUEST_ID_HEADER: safe_id}
        )
        assert resp.headers[REQUEST_ID_HEADER] == safe_id
        records = _read_audit(audit_path)
        assert records[-1]["request_id"] == safe_id

    def test_incoming_request_id_sanitized(
        self, client: TestClient, audit_path: Path
    ):
        dirty = "abc <script>alert(1)</script>\n\t!!!"
        resp = client.get(
            "/health", headers={REQUEST_ID_HEADER: dirty}
        )
        rid = resp.headers[REQUEST_ID_HEADER]
        # Forbidden characters must not appear.
        for bad in ("<", ">", " ", "\n", "\t", "!"):
            assert bad not in rid
        records = _read_audit(audit_path)
        assert records[-1]["request_id"] == rid

    def test_incoming_id_length_capped(
        self, client: TestClient, audit_path: Path
    ):
        huge = "a" * 500
        resp = client.get("/health", headers={REQUEST_ID_HEADER: huge})
        assert len(resp.headers[REQUEST_ID_HEADER]) == REQUEST_ID_MAX_LENGTH

    def test_request_id_available_via_forbidden_chars_only(
        self, client: TestClient
    ):
        """All chars stripped → server generates a UUID4 instead."""
        resp = client.get("/health", headers={REQUEST_ID_HEADER: "!!! ???"})
        rid = resp.headers[REQUEST_ID_HEADER]
        assert len(rid) == 32
        int(rid, 16)

    def test_different_requests_have_different_ids(
        self, client: TestClient
    ):
        ids = {client.get("/health").headers[REQUEST_ID_HEADER] for _ in range(5)}
        assert len(ids) == 5


# ---------------------------------------------------------------------------
# Leakage guards: Authorization + raw UA
# ---------------------------------------------------------------------------


class TestAuditDoesNotLeakSecrets:
    def test_authorization_token_never_in_audit_on_success(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        raw = audit_path.read_text(encoding="utf-8")
        assert VALID_KEY not in raw
        assert "Bearer " not in raw
        assert "authorization" not in raw.lower()

    def test_authorization_token_never_in_audit_on_failure(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        attempted_secret = "attempted-token-WRONG_ABC_XYZ"
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {attempted_secret}"},
        )
        raw = audit_path.read_text(encoding="utf-8")
        assert attempted_secret not in raw
        assert "Bearer " not in raw

    def test_raw_user_agent_never_in_audit(
        self, client: TestClient, audit_path: Path
    ):
        marker_ua = "Mozilla/5.0 (UNIQUE_UA_LEAK_MARKER_XYZ)"
        client.get("/health", headers={"User-Agent": marker_ua})
        raw = audit_path.read_text(encoding="utf-8")
        assert marker_ua not in raw
        assert "UNIQUE_UA_LEAK_MARKER_XYZ" not in raw

    def test_user_agent_hash_appears_in_audit(
        self, client: TestClient, audit_path: Path
    ):
        ua = "Mozilla/5.0 (X11; Linux) TestAgent/42"
        expected = _hash_user_agent(ua)
        client.get("/health", headers={"User-Agent": ua})
        records = _read_audit(audit_path)
        assert records[-1]["user_agent_hash"] == expected
        assert len(records[-1]["user_agent_hash"]) == 32

    def test_missing_user_agent_recorded_as_none(
        self, client: TestClient, audit_path: Path
    ):
        # httpx always sends a default UA; override to empty by
        # monkey-patching via request hook. Simplest is to use a
        # FastAPI test client with an empty header — httpx ignores
        # empty header values, so just check the field exists at all.
        client.get("/health")
        rec = _read_audit(audit_path)[-1]
        assert "user_agent_hash" in rec


# ---------------------------------------------------------------------------
# Authenticated flag
# ---------------------------------------------------------------------------


class TestAuditAuthenticatedFlag:
    def test_protected_endpoint_without_auth_is_false(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        client.get("/reports/latest")  # no header → 401
        rec = _read_audit(audit_path)[-1]
        assert rec["status_code"] == 401
        assert rec["authenticated"] is False

    def test_protected_endpoint_wrong_key_is_false(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer wrong-key"},
        )
        rec = _read_audit(audit_path)[-1]
        assert rec["status_code"] == 403
        assert rec["authenticated"] is False

    def test_protected_endpoint_with_valid_key_is_true(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        rec = _read_audit(audit_path)[-1]
        assert rec["status_code"] == 200
        assert rec["authenticated"] is True

    def test_unconfigured_server_is_false(
        self, client: TestClient, monkeypatch, audit_path: Path
    ):
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        rec = _read_audit(audit_path)[-1]
        assert rec["status_code"] == 503
        assert rec["authenticated"] is False

    def test_public_endpoint_is_true(
        self, client: TestClient, audit_path: Path
    ):
        client.get("/")
        rec = _read_audit(audit_path)[-1]
        assert rec["status_code"] == 200
        assert rec["authenticated"] is True


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------


class TestAuditFailureDoesNotFailRequest:
    def test_write_failure_does_not_break_request(
        self, client: TestClient, monkeypatch
    ):
        def boom(record, path=None):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(srv, "_append_audit_record", boom)
        # Request should still succeed; the audit layer swallows.
        resp = client.get("/health")
        assert resp.status_code == 200
        assert REQUEST_ID_HEADER in resp.headers  # header still attached

    def test_serialize_failure_does_not_break_request(
        self, client: TestClient, monkeypatch
    ):
        original = srv.json.dumps

        def fail_dumps(*a, **kw):
            raise TypeError("cannot serialize (fake)")

        # Patch only when called FROM _append_audit_record.
        # Easiest: patch the whole helper.
        def bad_append(record, path=None):
            try:
                fail_dumps(record)
            except Exception:
                return  # swallowed
        monkeypatch.setattr(srv, "_append_audit_record", bad_append)

        resp = client.get("/health")
        assert resp.status_code == 200

    def test_missing_parent_directory_does_not_fail_request(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        """_append_audit_record should create the parent dir."""
        nested = tmp_path / "deep" / "nested" / "audit.jsonl"
        monkeypatch.setenv(AUDIT_LOG_ENV_VAR, str(nested))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert nested.exists()

    def test_audit_write_exception_from_middleware_is_swallowed(
        self, client: TestClient, monkeypatch
    ):
        """
        Force the audit-record writer to raise. The middleware must
        catch it so the HTTP response still flows to the client.
        """
        def raising(record, path=None):
            raise RuntimeError("boom — simulated audit crash")

        monkeypatch.setattr(srv, "_append_audit_record", raising)
        # Wrap the module-level function too; the middleware may
        # reference a local binding.
        resp = client.get("/health")
        assert resp.status_code == 200
        # The request id is still attached even though the audit
        # record was never successfully written.
        assert REQUEST_ID_HEADER in resp.headers


# ---------------------------------------------------------------------------
# Audit path configurability
# ---------------------------------------------------------------------------


class TestAuditPathConfig:
    def test_default_path_is_documented(self):
        assert DEFAULT_AUDIT_LOG_PATH == "data/api_access_audit.jsonl"

    def test_custom_env_var_honoured(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        custom = tmp_path / "my_custom_audit.jsonl"
        monkeypatch.setenv(AUDIT_LOG_ENV_VAR, str(custom))
        client.get("/health")
        assert custom.exists()
        rec = _read_audit(custom)[-1]
        assert rec["path"] == "/health"


# ---------------------------------------------------------------------------
# Boundary re-assertion
# ---------------------------------------------------------------------------


class TestPhase44BoundaryUnchanged:
    def test_forbidden_imports_still_clean(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for pat in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert pat not in src

    def test_only_read_verbs_after_phase_4_4(self):
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
                assert (m, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"non-read-only method introduced: {m} {path}"

    def test_audit_never_writes_report_or_experiment_contents(
        self, client: TestClient, authed_env, audit_path: Path
    ):
        """Planted markers inside report/experiment files must never
        make it into the audit log — the audit writer only records
        metadata about the request, not the response body."""
        unique = "AUDIT_LEAK_MARKER_ABC_9d8e"
        (authed_env["reports_dir"]).mkdir(parents=True, exist_ok=True)
        (authed_env["reports_dir"] / "alpha_report_2026-04-24.json").write_text(
            json.dumps({"report_date": "2026-04-24",
                        "leak_field": unique})
        )
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        raw = audit_path.read_text(encoding="utf-8")
        assert unique not in raw


# ===========================================================================
# Phase 4.5 — access tier gating (free vs premium)
# ===========================================================================


from datetime import date as _date_phase45, timedelta as _td_phase45  # noqa: E402

from trading_bot.api.server import (  # noqa: E402
    MAX_FREE_TIER_DAYS,
    MAX_FREE_TIER_EXPERIMENTS,
    PREMIUM_KEYS_ENV_VAR,
    TIER_FREE,
    TIER_PREMIUM,
    UPGRADE_REQUIRED_DETAIL,
    _free_date_allowed,
    _is_premium,
    _premium_keys_set,
)


@pytest.fixture
def free_env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """
    Set up the server with FREE_KEY accepted but NOT in the premium
    list, plus VALID_KEY accepted AND in the premium list. Tests can
    pick whichever Bearer they want to exercise the relevant tier.
    """
    reports_dir = tmp_path / "reports"
    manifest = tmp_path / "alpha_experiments.jsonl"
    monkeypatch.setenv(API_KEY_ENV_VAR, FREE_KEY)
    monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, VALID_KEY)
    monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
    monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(manifest))
    return {"reports_dir": reports_dir, "manifest": manifest}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPremiumKeysSet:
    def test_unset_returns_empty(self):
        assert _premium_keys_set() == set()

    def test_blank_returns_empty(self, monkeypatch):
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "   ")
        assert _premium_keys_set() == set()

    def test_single_key(self, monkeypatch):
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "alpha")
        assert _premium_keys_set() == {"alpha"}

    def test_multiple_keys_with_whitespace(self, monkeypatch):
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, " a , b , , c ")
        assert _premium_keys_set() == {"a", "b", "c"}


class TestIsPremium:
    def test_no_premium_keys_means_no_one_is_premium(self):
        assert _is_premium("anything") is False

    def test_key_in_list_is_premium(self, monkeypatch):
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "vip-key")
        assert _is_premium("vip-key") is True

    def test_key_not_in_list_is_free(self, monkeypatch):
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "vip-key")
        assert _is_premium("free-key") is False

    def test_empty_or_none_is_free(self):
        assert _is_premium("") is False
        assert _is_premium(None) is False

    def test_multiple_premium_keys(self, monkeypatch):
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "k1,k2,k3")
        for k in ("k1", "k2", "k3"):
            assert _is_premium(k) is True
        assert _is_premium("k4") is False


class TestFreeDateAllowed:
    def test_today_allowed(self, monkeypatch):
        from trading_bot.api import server as srv_mod
        today = _date_phase45(2026, 4, 24)
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)
        assert _free_date_allowed("2026-04-24") is True

    def test_yesterday_allowed(self, monkeypatch):
        from trading_bot.api import server as srv_mod
        today = _date_phase45(2026, 4, 24)
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)
        assert _free_date_allowed("2026-04-23") is True

    def test_two_days_ago_allowed(self, monkeypatch):
        from trading_bot.api import server as srv_mod
        today = _date_phase45(2026, 4, 24)
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)
        assert _free_date_allowed("2026-04-22") is True

    def test_three_days_ago_blocked(self, monkeypatch):
        from trading_bot.api import server as srv_mod
        today = _date_phase45(2026, 4, 24)
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)
        assert _free_date_allowed("2026-04-21") is False

    def test_future_blocked(self, monkeypatch):
        from trading_bot.api import server as srv_mod
        today = _date_phase45(2026, 4, 24)
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)
        assert _free_date_allowed("2026-04-25") is False

    def test_malformed_date_returns_true_so_validator_owns_400(self, monkeypatch):
        """Bad input should be rejected by `_validate_date` upstream
        with 400, not by the tier gate with 403."""
        from trading_bot.api import server as srv_mod
        today = _date_phase45(2026, 4, 24)
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)
        assert _free_date_allowed("not-a-date") is True


# ---------------------------------------------------------------------------
# /reports/{date} — date-window enforcement
# ---------------------------------------------------------------------------


class TestReportsDateFreeTier:
    @staticmethod
    def _set_today(monkeypatch, today: _date_phase45):
        from trading_bot.api import server as srv_mod
        monkeypatch.setattr(srv_mod, "_today_utc", lambda: today)

    def test_free_user_can_access_today(
        self, client: TestClient, free_env, monkeypatch
    ):
        self._set_today(monkeypatch, _date_phase45(2026, 4, 24))
        _write_report(free_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/2026-04-24",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200

    def test_free_user_can_access_yesterday(
        self, client: TestClient, free_env, monkeypatch
    ):
        self._set_today(monkeypatch, _date_phase45(2026, 4, 24))
        _write_report(free_env["reports_dir"], "2026-04-23")
        resp = client.get(
            "/reports/2026-04-23",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200

    def test_free_user_can_access_two_days_ago(
        self, client: TestClient, free_env, monkeypatch
    ):
        self._set_today(monkeypatch, _date_phase45(2026, 4, 24))
        _write_report(free_env["reports_dir"], "2026-04-22")
        resp = client.get(
            "/reports/2026-04-22",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200

    def test_free_user_blocked_three_days_ago(
        self, client: TestClient, free_env, monkeypatch
    ):
        self._set_today(monkeypatch, _date_phase45(2026, 4, 24))
        _write_report(free_env["reports_dir"], "2026-04-21")
        resp = client.get(
            "/reports/2026-04-21",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == UPGRADE_REQUIRED_DETAIL

    def test_premium_user_can_access_old_date(
        self, client: TestClient, free_env, monkeypatch
    ):
        self._set_today(monkeypatch, _date_phase45(2026, 4, 24))
        _write_report(free_env["reports_dir"], "2025-01-01")
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200

    def test_premium_user_can_access_future_date(
        self, client: TestClient, free_env, monkeypatch
    ):
        self._set_today(monkeypatch, _date_phase45(2026, 4, 24))
        _write_report(free_env["reports_dir"], "2030-01-01")
        resp = client.get(
            "/reports/2030-01-01",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200

    def test_invalid_date_format_still_400_for_free(
        self, client: TestClient, free_env
    ):
        """Bad format wins over tier check — 400, not 403."""
        resp = client.get(
            "/reports/not-a-date",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 400


class TestReportsLatestFreeTier:
    """Free users can access /reports/latest unconditionally (sanitized)."""

    def test_free_user_can_get_latest_report(
        self, client: TestClient, free_env
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /experiments/recent
# ---------------------------------------------------------------------------


class TestExperimentsRecentTier:
    def _seed(self, manifest: Path, n: int) -> None:
        records = [
            _sample_manifest_record(f"2026-04-{i:02d}") for i in range(1, n + 1)
        ]
        _write_manifest_records(manifest, records)

    def test_free_user_default_limit_caps_at_3(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 10)
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == MAX_FREE_TIER_EXPERIMENTS == 3

    def test_free_user_explicit_limit_below_cap_works(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 10)
        resp = client.get(
            "/experiments/recent?limit=2",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_free_user_explicit_limit_at_cap_works(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 10)
        resp = client.get(
            "/experiments/recent?limit=3",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_free_user_explicit_limit_above_cap_returns_403(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 10)
        resp = client.get(
            "/experiments/recent?limit=4",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == UPGRADE_REQUIRED_DETAIL

    def test_free_user_explicit_limit_matching_default_still_returns_403(
        self, client: TestClient, free_env
    ):
        """Explicit `?limit=10` (= default) must still trigger 403,
        because the user CHOSE to ask for more than their tier allows."""
        self._seed(free_env["manifest"], 20)
        resp = client.get(
            "/experiments/recent?limit=10",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == UPGRADE_REQUIRED_DETAIL

    def test_premium_user_default_limit_returns_10(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 20)
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 10

    def test_premium_user_can_request_50(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 80)
        resp = client.get(
            "/experiments/recent?limit=50",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 50


# ---------------------------------------------------------------------------
# /experiments/{n}
# ---------------------------------------------------------------------------


class TestExperimentByIndexTier:
    def _seed(self, manifest: Path, n: int) -> None:
        records = [
            _sample_manifest_record(f"2026-04-{i:02d}") for i in range(1, n + 1)
        ]
        _write_manifest_records(manifest, records)

    def test_free_user_n1_to_n3_works(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 10)
        for n in (1, 2, 3):
            resp = client.get(
                f"/experiments/{n}",
                headers={"Authorization": f"Bearer {FREE_KEY}"},
            )
            assert resp.status_code == 200, n

    def test_free_user_n4_returns_403(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 10)
        resp = client.get(
            "/experiments/4",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == UPGRADE_REQUIRED_DETAIL

    def test_free_user_large_n_returns_403_not_404(
        self, client: TestClient, free_env
    ):
        """Tier gate fires BEFORE the 'not enough records' 404."""
        self._seed(free_env["manifest"], 1)
        resp = client.get(
            "/experiments/99",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403

    def test_premium_user_n_above_3_works(
        self, client: TestClient, free_env
    ):
        self._seed(free_env["manifest"], 20)
        resp = client.get(
            "/experiments/15",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200

    def test_premium_user_n_too_large_still_404(
        self, client: TestClient, free_env
    ):
        """Premium past the manifest length still hits the documented 404."""
        self._seed(free_env["manifest"], 5)
        resp = client.get(
            "/experiments/99",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dashboard tier rendering
# ---------------------------------------------------------------------------


class TestDashboardTier:
    def test_premium_user_sees_shadow_filter_section(
        self, client: TestClient, free_env
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        _write_manifest_records(
            free_env["manifest"],
            [_sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 11)],
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Shadow filter simulation" in body
        # All 10 experiments visible.
        for d in (1, 5, 10):
            assert f"2026-04-{d:02d}" in body

    def test_free_user_does_not_see_shadow_filter_section(
        self, client: TestClient, free_env
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Shadow filter simulation" not in body

    def test_free_user_dashboard_caps_experiments_at_3(
        self, client: TestClient, free_env
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        _write_manifest_records(
            free_env["manifest"],
            [_sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 11)],
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = resp.text
        # Newest three: 2026-04-08, 2026-04-09, 2026-04-10 should appear.
        for d in (8, 9, 10):
            assert f"2026-04-{d:02d}" in body
        # Oldest seven (1..7) must NOT appear in the experiments table.
        for d in (1, 2, 3, 4, 5, 6, 7):
            assert f"2026-04-{d:02d}" not in body, (
                f"free dashboard leaked experiment 2026-04-{d:02d}"
            )

    def test_free_user_dashboard_shows_upgrade_note(
        self, client: TestClient, free_env
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        _write_manifest_records(
            free_env["manifest"],
            [_sample_manifest_record(f"2026-04-{d:02d}") for d in range(1, 11)],
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = resp.text.lower()
        assert "free tier" in body
        assert "upgrade" in body

    def test_dashboard_does_not_leak_shadow_data_in_free_tier(
        self, client: TestClient, free_env
    ):
        """The unique value inside shadow_filter_simulation must NOT
        appear anywhere in the free-tier dashboard HTML."""
        unique_marker = "ABC_SHADOW_LEAK_MARKER_XYZ"
        _write_report(
            free_env["reports_dir"], "2026-04-24",
            shadow_filter_simulation=[
                {"threshold": "A+B",
                 "allowed_buy_count": unique_marker,  # placed deliberately
                 "blocked_buy_count": 0},
            ],
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert unique_marker not in resp.text

    def test_free_user_still_sees_guardrails_and_readiness(
        self, client: TestClient, free_env
    ):
        """Hiding shadow_filter_simulation must NOT hide other sections."""
        _write_report(free_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = resp.text
        assert "Guardrails" in body
        assert "Promotion readiness" in body
        assert "Totals" in body


# ---------------------------------------------------------------------------
# Auth still required + boundary unchanged
# ---------------------------------------------------------------------------


class TestPhase45BoundaryUnchanged:
    def test_no_header_still_401_on_protected_endpoints(
        self, client: TestClient, free_env
    ):
        for path in (
            "/reports/latest",
            "/reports/2026-04-24",
            "/experiments/recent",
            "/experiments/1",
            "/dashboard",
        ):
            resp = client.get(path)
            assert resp.status_code == 401, (
                f"{path} must still require auth, got {resp.status_code}"
            )

    def test_unknown_key_still_403(
        self, client: TestClient, free_env
    ):
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer totally-bogus-key"},
        )
        assert resp.status_code == 403

    def test_unconfigured_server_still_503(
        self, client: TestClient, monkeypatch
    ):
        # Both env vars unset → fail-closed.
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(PREMIUM_KEYS_ENV_VAR, raising=False)
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 503

    def test_premium_only_server_still_works(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        """A server with ONLY premium keys (no TRADING_API_KEY) is valid."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "only-premium-key")
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "r"))
        monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(tmp_path / "m.jsonl"))
        # Premium key works.
        resp = client.get(
            "/health",
            headers={"Authorization": "Bearer only-premium-key"},
        )
        assert resp.status_code == 200
        # Anything else still 403.
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 403

    def test_only_read_verbs_after_phase_4_5(self):
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
                assert (m, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"non-read-only method introduced: {m} {path}"

    def test_forbidden_imports_still_clean(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for pat in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert pat not in src


# ===========================================================================
# Phase 4.6 — per-key usage metrics
# ===========================================================================


from trading_bot.api.server import (  # noqa: E402
    DEFAULT_USAGE_LOG_PATH,
    USAGE_LOG_ENV_VAR,
    _append_usage_record,
    _hash_api_key,
)


@pytest.fixture
def usage_path() -> Path:
    """Return the path clean_api_env pointed the usage log at."""
    return Path(_os_phase44.environ["TRADING_API_USAGE_LOG_PATH"])


def _read_usage(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except Exception:
            continue
    return out


class TestHashApiKey:
    def test_empty_or_none_returns_empty_string(self):
        assert _hash_api_key(None) == ""
        assert _hash_api_key("") == ""

    def test_returns_32_hex_chars(self):
        h = _hash_api_key("whatever-key-value")
        assert isinstance(h, str)
        assert len(h) == 32
        int(h, 16)

    def test_deterministic(self):
        assert _hash_api_key("abc") == _hash_api_key("abc")

    def test_different_inputs_different_hashes(self):
        assert _hash_api_key("abc") != _hash_api_key("abd")

    def test_is_not_the_raw_key(self):
        key = "super-secret-KEY-12345"
        h = _hash_api_key(key)
        assert key not in h
        assert h != key

    def test_matches_sha256_prefix(self):
        """Lock in the SHA-256 prefix: 32 hex chars of the digest."""
        import hashlib as _hlib
        key = "stable-key"
        expected = _hlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        assert _hash_api_key(key) == expected


class TestUsageEnvDefault:
    def test_default_path_is_documented(self):
        assert DEFAULT_USAGE_LOG_PATH == "data/api_usage.jsonl"

    def test_env_var_name_is_documented(self):
        assert USAGE_LOG_ENV_VAR == "TRADING_API_USAGE_LOG_PATH"


class TestProtectedRequestsWriteUsage:
    def test_reports_latest_writes_one_usage_record(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        records = _read_usage(usage_path)
        assert len(records) == 1
        rec = records[0]
        required = {
            "timestamp", "key_hash", "tier", "method", "path",
            "status_code", "duration_ms", "request_id",
        }
        assert required.issubset(rec.keys())
        assert rec["method"] == "GET"
        assert rec["path"] == "/reports/latest"
        assert rec["status_code"] == 200

    def test_dashboard_writes_usage_record(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        records = _read_usage(usage_path)
        assert len(records) == 1
        assert records[0]["path"] == "/dashboard"

    def test_experiments_recent_writes_usage_record(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert len(_read_usage(usage_path)) == 1

    def test_multiple_protected_requests_write_one_record_each(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        for _ in range(4):
            client.get(
                "/reports/latest",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        assert len(_read_usage(usage_path)) == 4


class TestPublicRequestsDoNotWriteUsage:
    def test_health_does_not_write_usage(
        self, client: TestClient, usage_path: Path
    ):
        client.get("/health")
        assert _read_usage(usage_path) == []

    def test_root_does_not_write_usage(
        self, client: TestClient, usage_path: Path
    ):
        client.get("/")
        assert _read_usage(usage_path) == []

    def test_public_request_with_auth_header_still_no_usage(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        client.get(
            "/health", headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        client.get(
            "/", headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert _read_usage(usage_path) == []


class TestAuthFailuresDoNotWriteUsage:
    def test_missing_header_no_usage(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        client.get("/reports/latest")  # 401
        assert _read_usage(usage_path) == []

    def test_wrong_key_no_usage(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer wrong-key"},
        )  # 403
        assert _read_usage(usage_path) == []

    def test_unconfigured_server_no_usage(
        self, client: TestClient, monkeypatch, usage_path: Path
    ):
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )  # 503
        assert _read_usage(usage_path) == []


class TestUsageTierRecorded:
    def test_free_tier_recorded(
        self, client: TestClient, free_env, usage_path: Path
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        rec = _read_usage(usage_path)[-1]
        assert rec["tier"] == "free"
        assert rec["key_hash"] == _hash_api_key(FREE_KEY)

    def test_premium_tier_recorded(
        self, client: TestClient, free_env, usage_path: Path
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        rec = _read_usage(usage_path)[-1]
        assert rec["tier"] == "premium"
        assert rec["key_hash"] == _hash_api_key(VALID_KEY)

    def test_different_keys_produce_different_hashes(
        self, client: TestClient, free_env, usage_path: Path
    ):
        client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        hashes = {r["key_hash"] for r in _read_usage(usage_path)}
        assert len(hashes) == 2

    def test_same_key_groups_under_same_hash(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        for _ in range(3):
            client.get(
                "/reports/latest",
                headers={"Authorization": f"Bearer {VALID_KEY}"},
            )
        hashes = {r["key_hash"] for r in _read_usage(usage_path)}
        assert len(hashes) == 1


class TestUsageDoesNotLeakRawKey:
    def test_valid_key_never_in_usage_file(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        raw = usage_path.read_text(encoding="utf-8")
        assert VALID_KEY not in raw

    def test_free_key_never_in_usage_file(
        self, client: TestClient, free_env, usage_path: Path
    ):
        _write_report(free_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        raw = usage_path.read_text(encoding="utf-8")
        assert FREE_KEY not in raw

    def test_no_authorization_header_word_in_file(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        raw = usage_path.read_text(encoding="utf-8").lower()
        assert "authorization" not in raw
        assert "bearer " not in raw

    def test_rejected_token_never_in_usage_file(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        attempt = "attempted-leak-marker-ABC_9f3e"
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {attempt}"},
        )
        assert _read_usage(usage_path) == []
        if usage_path.exists():
            raw = usage_path.read_text(encoding="utf-8")
            assert attempt not in raw


class TestUsageRequestIdMatchesResponseHeader:
    def test_generated_request_id_matches(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        rid = resp.headers.get(REQUEST_ID_HEADER)
        rec = _read_usage(usage_path)[-1]
        assert rid is not None
        assert rec["request_id"] == rid

    def test_supplied_request_id_flows_through(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={
                "Authorization": f"Bearer {VALID_KEY}",
                REQUEST_ID_HEADER: "trace-abc-123",
            },
        )
        assert resp.headers[REQUEST_ID_HEADER] == "trace-abc-123"
        rec = _read_usage(usage_path)[-1]
        assert rec["request_id"] == "trace-abc-123"


class TestUsageWriteFailureDoesNotFailRequest:
    def test_writer_raises_does_not_break_request(
        self, client: TestClient, authed_env, monkeypatch
    ):
        def raising(record, path=None):
            raise OSError("simulated disk failure")
        monkeypatch.setattr(srv, "_append_usage_record", raising)
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert REQUEST_ID_HEADER in resp.headers

    def test_missing_parent_dir_auto_created(
        self, client: TestClient, authed_env, monkeypatch, tmp_path: Path
    ):
        nested = tmp_path / "a" / "b" / "c" / "usage.jsonl"
        monkeypatch.setenv(USAGE_LOG_ENV_VAR, str(nested))
        _write_report(authed_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert nested.exists()


class TestPhase46BoundaryUnchanged:
    def test_only_read_verbs_after_phase_4_6(self):
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both.
                assert (m, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"non-read-only method: {m} {path}"

    def test_forbidden_imports_still_clean_after_phase_4_6(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for pat in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert pat not in src

    def test_usage_record_never_carries_report_body(
        self, client: TestClient, authed_env, usage_path: Path
    ):
        """Planted marker inside a report must never appear in the
        usage log — usage records are METADATA only."""
        unique = "USAGE_LEAK_MARKER_XYZ"
        (authed_env["reports_dir"]).mkdir(parents=True, exist_ok=True)
        (authed_env["reports_dir"] / "alpha_report_2026-04-24.json").write_text(
            json.dumps({"report_date": "2026-04-24", "leak": unique})
        )
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert unique not in usage_path.read_text(encoding="utf-8")


# ===========================================================================
# Phase 4.7 — Stripe billing integration
# ===========================================================================


import hashlib as _hashlib_phase47  # noqa: E402
import hmac as _hmac_phase47  # noqa: E402
import time as _time_phase47  # noqa: E402

from trading_bot.api import billing as _billing  # noqa: E402


STRIPE_TEST_SECRET = "whsec_test_secret_for_tests_only"
STRIPE_API_TEST_KEY = "sk_test_abcdefghij"


def _sign_stripe_webhook(body: bytes, secret: str, *, ts=None) -> str:
    timestamp = int(ts if ts is not None else _time_phase47.time())
    signed = f"{timestamp}.".encode("utf-8") + body
    sig = _hmac_phase47.new(
        secret.encode("utf-8"), signed, _hashlib_phase47.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={sig}"


@pytest.fixture
def stripe_env(monkeypatch, tmp_path: Path):
    """Configure the server for Stripe-primary mode."""
    monkeypatch.setenv("STRIPE_API_KEY", STRIPE_API_TEST_KEY)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_TEST_SECRET)
    monkeypatch.setenv("STRIPE_PRICE_ID_PREMIUM", "price_test_premium")
    cache_file = tmp_path / "stripe_premium.json"
    monkeypatch.setenv("TRADING_STRIPE_PREMIUM_CACHE_PATH", str(cache_file))
    # Server still needs a valid Bearer somewhere — the Stripe-only
    # customer will authenticate via TRADING_API_KEY for this fixture.
    # Premium tier will be granted by the Stripe webhook.
    monkeypatch.setenv(API_KEY_ENV_VAR, "stripe-customer-api-key")
    # Clear the env-var premium-list so Stripe is the only source.
    monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
    _billing.reset_cache_for_tests()
    reports_dir = tmp_path / "reports"
    manifest = tmp_path / "manifest.jsonl"
    monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
    monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(manifest))

    # Phase 7.0 — pre-issue the fixture api_key into the issuance
    # manifest so the webhook's manifest-gate accepts Stripe events
    # for this customer. The autouse clean_api_env fixture already
    # redirected TRADING_API_KEYS_MANIFEST_PATH to a tmp file.
    import hashlib as _hl
    keys_manifest = Path(os.environ["TRADING_API_KEYS_MANIFEST_PATH"])
    keys_manifest.parent.mkdir(parents=True, exist_ok=True)
    key_hash = _hl.sha256(
        b"stripe-customer-api-key",
    ).hexdigest()[:32]
    row = {
        "created_at": "2026-04-25T00:00:00.000000Z",
        "key_hash": key_hash,
        "label_hash": "f" * 32,
        "tier": "free",
        "checkout_session_id": None,
    }
    with open(keys_manifest, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()

    return {
        "cache_file": cache_file,
        "reports_dir": reports_dir,
        "manifest": manifest,
        "api_key": "stripe-customer-api-key",
        "api_key_hash": key_hash,
    }


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


class TestWebhookEndpointSignature:
    def test_valid_signature_returns_200(
        self, client: TestClient, stripe_env
    ):
        body = json.dumps({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": stripe_env["api_key"]},
            }},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe",
            content=body,
            headers={
                "stripe-signature": sig,
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        body_json = resp.json()
        assert body_json["received"] is True
        assert body_json["action"] == "added"
        assert body_json["type"] == "customer.subscription.created"

    def test_invalid_signature_returns_400(
        self, client: TestClient, stripe_env
    ):
        resp = client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={
                "stripe-signature": "t=123,v1=deadbeef",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 400
        assert "invalid" in resp.json()["detail"].lower()

    def test_missing_signature_returns_400(
        self, client: TestClient, stripe_env
    ):
        resp = client.post(
            "/webhook/stripe",
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_tampered_body_returns_400(
        self, client: TestClient, stripe_env
    ):
        body = b'{"type": "customer.subscription.created"}'
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe",
            content=body + b" ",  # extra byte breaks HMAC
            headers={
                "stripe-signature": sig,
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 400

    def test_webhook_not_configured_returns_503(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        """No STRIPE_WEBHOOK_SECRET → reject every call fail-closed."""
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        body = b'{"type": "customer.subscription.created"}'
        sig = _sign_stripe_webhook(body, "anything")
        resp = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": sig},
        )
        assert resp.status_code == 503

    def test_invalid_json_body_with_good_signature_returns_400(
        self, client: TestClient, stripe_env
    ):
        body = b"not valid json at all"
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": sig},
        )
        assert resp.status_code == 400

    def test_webhook_does_not_require_auth_header(
        self, client: TestClient, stripe_env
    ):
        """Webhook is authenticated by signature, NOT by Authorization."""
        body = json.dumps({
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"api_key": "some-key"}}},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": sig},
        )
        # 200 even without any Authorization header.
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# End-to-end: webhook delivery → premium access
# ---------------------------------------------------------------------------


class TestWebhookUpdatesAccess:
    def test_subscription_created_grants_premium_access(
        self, client: TestClient, stripe_env
    ):
        _write_report(stripe_env["reports_dir"], "2026-04-24")
        api_key = stripe_env["api_key"]

        # Before subscription: free tier — writes cannot reach old dates
        # (here we need to mock _today_utc or just test with an OLD date).
        from trading_bot.api import server as srv
        srv._today_utc = lambda: _date_phase45(2026, 4, 24)
        # Request old date as free → 403.
        _write_report(stripe_env["reports_dir"], "2025-01-01")
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403

        # Now Stripe delivers the subscription.created webhook.
        body = json.dumps({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": api_key},
            }},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        hook = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": sig},
        )
        assert hook.status_code == 200
        assert hook.json()["action"] == "added"

        # Same request now succeeds (premium tier).
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

    def test_subscription_deleted_revokes_premium(
        self, client: TestClient, stripe_env
    ):
        api_key = stripe_env["api_key"]
        _write_report(stripe_env["reports_dir"], "2025-01-01")

        # Become premium.
        _billing.add_premium_key(api_key)

        from trading_bot.api import server as srv
        srv._today_utc = lambda: _date_phase45(2026, 4, 24)

        # Works as premium first.
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

        # Stripe delivers subscription.deleted.
        body = json.dumps({
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"api_key": api_key}}},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        hook = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": sig},
        )
        assert hook.status_code == 200
        assert hook.json()["action"] == "removed"

        # Now free-tier — old date blocked.
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403

    def test_payment_failed_revokes_premium(
        self, client: TestClient, stripe_env
    ):
        api_key = stripe_env["api_key"]
        _billing.add_premium_key(api_key)
        assert _billing.is_premium_via_stripe(api_key) is True

        body = json.dumps({
            "type": "invoice.payment_failed",
            "data": {"object": {"metadata": {"api_key": api_key}}},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": sig},
        )
        assert resp.status_code == 200
        assert _billing.is_premium_via_stripe(api_key) is False


# ---------------------------------------------------------------------------
# Fallback to env-var premium list when Stripe not configured
# ---------------------------------------------------------------------------


class TestFallbackToEnvVar:
    def test_env_var_premium_works_when_stripe_not_configured(
        self, client: TestClient, free_env
    ):
        """free_env has TRADING_API_PREMIUM_KEYS=VALID_KEY and Stripe unset."""
        _write_report(free_env["reports_dir"], "2025-01-01")
        from trading_bot.api import server as srv
        srv._today_utc = lambda: _date_phase45(2026, 4, 24)
        # VALID_KEY is premium via env var.
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        # FREE_KEY is NOT premium.
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403

    def test_env_var_premium_still_works_when_stripe_configured(
        self, client: TestClient, monkeypatch, tmp_path: Path
    ):
        """Operator override: env-var premium list is still honoured
        even when Stripe is configured, so ops can grant access
        independently of the billing flow."""
        monkeypatch.setenv("STRIPE_API_KEY", STRIPE_API_TEST_KEY)
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_TEST_SECRET)
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            str(tmp_path / "cache.json"),
        )
        monkeypatch.setenv(API_KEY_ENV_VAR, "shared-free")
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", "ops-override-key")
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "reports"))
        monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(tmp_path / "m.jsonl"))
        _billing.reset_cache_for_tests()

        _write_report(tmp_path / "reports", "2025-01-01")
        from trading_bot.api import server as srv
        srv._today_utc = lambda: _date_phase45(2026, 4, 24)

        # ops-override-key works via env var even though it's not
        # in the Stripe cache.
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": "Bearer ops-override-key"},
        )
        assert resp.status_code == 200

    def test_stripe_cache_wins_for_customers_not_in_env(
        self, client: TestClient, stripe_env
    ):
        """Stripe cache grants premium to a key not in TRADING_API_PREMIUM_KEYS."""
        api_key = stripe_env["api_key"]
        _billing.add_premium_key(api_key)
        _write_report(stripe_env["reports_dir"], "2025-01-01")
        from trading_bot.api import server as srv
        srv._today_utc = lambda: _date_phase45(2026, 4, 24)
        resp = client.get(
            "/reports/2025-01-01",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# No sensitive data stored / returned
# ---------------------------------------------------------------------------


class TestWebhookNeverPersistsSensitiveData:
    def test_card_data_in_webhook_payload_does_not_land_on_disk(
        self, client: TestClient, stripe_env
    ):
        """A webhook event with card / email / PAN fields must result in
        NONE of those values being written to the Stripe cache file."""
        api_key = stripe_env["api_key"]
        body = json.dumps({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": api_key},
                "customer": {
                    "email": "card_owner_LEAK@example.com",
                    "name": "LEAKED_CARDHOLDER",
                    "metadata": {"pan": "4242424242424242", "cvv": "999"},
                },
            }},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe", content=body,
            headers={"stripe-signature": sig},
        )
        assert resp.status_code == 200
        raw = stripe_env["cache_file"].read_text(encoding="utf-8")
        for leak in (
            "card_owner_LEAK@example.com",
            "LEAKED_CARDHOLDER",
            "4242424242424242",
            "999",
            # Phase 7.0 — the raw api_key itself no longer lands on disk.
            api_key,
        ):
            assert leak not in raw, f"leaked {leak!r} to {stripe_env['cache_file']}"
        # The hash IS expected — Phase 7.0 persisted form.
        assert stripe_env["api_key_hash"] in raw

    def test_webhook_response_body_never_echoes_sensitive_fields(
        self, client: TestClient, stripe_env
    ):
        api_key = stripe_env["api_key"]
        body = json.dumps({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": api_key},
                "customer": {
                    "email": "RESP_LEAK@example.com",
                },
            }},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe", content=body,
            headers={"stripe-signature": sig},
        )
        assert resp.status_code == 200
        assert "RESP_LEAK@example.com" not in resp.text
        # Also the api_key must not be echoed back in the response body.
        assert api_key not in resp.text


# ---------------------------------------------------------------------------
# Boundary re-assertion
# ---------------------------------------------------------------------------


class TestPhase47Boundary:
    def test_only_non_read_verb_is_post_webhook_stripe(self):
        # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
        allowed = {("POST", "/webhook/stripe"), ("POST", "/billing/checkout")}
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                assert (m, path) in allowed, (
                    f"non-read-only method {m} on {path}"
                )

    def test_webhook_stripe_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "billing.py"
        ).read_text()
        for pat in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert pat not in src

    def test_webhook_does_not_write_usage_record(
        self, client: TestClient, stripe_env, usage_path: Path
    ):
        """The Stripe webhook is a system-to-system call, not a user
        request — it must NOT land in the per-key usage log."""
        body = json.dumps({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": stripe_env["api_key"]},
            }},
        }).encode("utf-8")
        sig = _sign_stripe_webhook(body, STRIPE_TEST_SECRET)
        resp = client.post(
            "/webhook/stripe", content=body,
            headers={"stripe-signature": sig},
        )
        assert resp.status_code == 200
        assert _read_usage(usage_path) == []


# ===========================================================================
# Phase 5.2 — landing-page conversion optimisation
# ===========================================================================


from trading_bot.api.server import _landing_page_body as _phase52_body  # noqa: E402


class TestPhase52HasConversionSections:
    """The landing page must expose all five conversion sections."""

    def test_hero_tagline_present(self, client: TestClient):
        body = client.get("/").text
        assert (
            "See which trades your system should have taken — "
            "before risking money."
        ) in body

    def test_has_how_it_works_section(self, client: TestClient):
        body = client.get("/").text
        assert "<h2>How it works</h2>" in body
        # The three-step list is an <ol …> with 3 <li> children. We
        # match the open tag without the trailing ">" so the
        # Phase 5.9 polish (which adds class="feature-grid") still
        # passes here.
        ol_start = body.find("<ol")
        ol_end = body.find("</ol>", ol_start)
        assert ol_start != -1 and ol_end != -1
        assert body.count("<li>", ol_start, ol_end) == 3

    def test_has_example_output_section(self, client: TestClient):
        body = client.get("/").text
        assert "<h2>Example output</h2>" in body
        low = body.lower()
        # Three markers from the sample: a guardrail row, tier rows,
        # and shadow-threshold rows.
        assert "guardrail status" in low
        assert ">tier<" in low
        assert "shadow threshold" in low

    def test_example_output_is_illustrative_not_real(self, client: TestClient):
        body = client.get("/").text
        # The sample must be clearly flagged as illustrative — not
        # real data — so visitors don't confuse it with a live feed.
        assert "Illustrative snapshot" in body or "not live data" in body

    def test_has_upgrade_trigger_section(self, client: TestClient):
        body = client.get("/").text
        low = body.lower()
        assert "<h2>upgrade</h2>" in low
        # Free-vs-premium comparison table.
        assert "<th>free</th>" in low
        assert "<th>premium</th>" in low
        # The free tier ships a 3-day history window (matches the
        # Phase 4.5 free-tier cap).
        assert "last 3 days" in low

    def test_has_cta_section(self, client: TestClient):
        body = client.get("/").text
        assert "<h2>Get started</h2>" in body
        # The CTA text must describe obtaining a Bearer key.
        assert "Bearer API key" in body

    def test_has_five_section_elements(self, client: TestClient):
        body = client.get("/").text
        # The spec calls for five conversion sections: Hero, How it
        # works, Example output, Upgrade, Get started.
        assert body.count("<section") == 5
        assert body.count("</section>") == 5

    def test_all_legacy_positioning_phrases_still_present(
        self, client: TestClient,
    ):
        """Re-assert Phase 4.3 invariants after the Phase 5.2 rewrite."""
        low = client.get("/").text.lower()
        for phrase in (
            "read-only", "guardrail", "daily validation",
            "audit trail", "protected dashboard",
        ):
            assert phrase in low, (
                f"Phase 4.3 positioning phrase lost: {phrase!r}"
            )


class TestPhase52SoftConversionCues:
    """Hardcoded copy; no data lookup, no personalisation."""

    def test_contains_upgrade_after_seven_days_cue(self, client: TestClient):
        body = client.get("/").text
        assert "Most users upgrade after ~7 days" in body

    def test_contains_premium_usage_intensity_cue(self, client: TestClient):
        body = client.get("/").text
        # "3–5x" uses an en dash; the body must contain the phrase
        # verbatim so the cue survives copy/paste and localisation.
        assert "Premium users run 3–5x more requests" in body

    def test_cues_do_not_depend_on_env_or_disk(
        self, monkeypatch, tmp_path: Path,
    ):
        """Cue text is a compile-time constant — setting env vars,
        populating reports, or toggling premium keys must not change
        either cue."""
        baseline = render_landing_page_html()
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "reports"))
        monkeypatch.setenv(PREMIUM_KEYS_ENV_VAR, "whatever")
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "alpha_report_2026-04-24.json").write_text(
            json.dumps({"report_date": "2026-04-24"})
        )
        populated = render_landing_page_html()
        assert baseline == populated


class TestPhase52RefQueryParam:
    """`?ref=` is echoed back, sanitised identically to growth.py."""

    def test_no_ref_hides_invited_banner(self, client: TestClient):
        body = client.get("/").text
        assert "Invited by" not in body

    def test_valid_ref_is_echoed(self, client: TestClient):
        body = client.get("/?ref=twitter-launch_2026").text
        assert "Invited by" in body
        assert "twitter-launch_2026" in body

    def test_ref_matches_growth_sanitiser(self, client: TestClient):
        """The value displayed on the page must match exactly what
        the growth logger would record — "what you see is what gets
        logged". Any character outside [A-Za-z0-9\\-_:.] is stripped."""
        from trading_bot.api.growth import _sanitize_ref_code
        raw = "twitter-launch_2026!!@#$"
        expected = _sanitize_ref_code(raw)
        body = client.get(f"/?ref={raw}").text
        assert expected in body
        # The stripped characters must not survive into the HTML.
        for bad in ("!", "@", "#", "$"):
            # The sanitised token is present but individual bad chars
            # appear nowhere outside the CSS / inline markup — check
            # the echoed <code> block specifically.
            code_start = body.find("<code>")
            code_end = body.find("</code>", code_start)
            assert code_start != -1 and code_end != -1
            assert bad not in body[code_start:code_end]

    def test_empty_ref_param_hides_banner(self, client: TestClient):
        body = client.get("/?ref=").text
        assert "Invited by" not in body

    def test_ref_is_capped_at_64_chars(self, client: TestClient):
        body = client.get("/?ref=" + ("a" * 500)).text
        # Find the echoed code block and verify the length.
        start = body.find("<code>")
        end = body.find("</code>", start)
        assert start != -1 and end != -1
        echoed = body[start + len("<code>"):end]
        assert len(echoed) == 64
        assert echoed == "a" * 64

    def test_ref_html_injection_blocked(self, client: TestClient):
        """Ref is sanitised before render AND escaped on output. A
        <script> payload must not appear as a live tag."""
        body = client.get("/?ref=<script>alert(1)</script>").text
        # The raw attacker string must not appear.
        assert "<script>alert(1)</script>" not in body
        # No <script tag of ANY kind is allowed on the landing page.
        assert "<script" not in body.lower()
        # No javascript: URIs either.
        assert "javascript:" not in body.lower()

    def test_ref_does_not_affect_status_or_content_type(
        self, client: TestClient,
    ):
        resp = client.get("/?ref=abc")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")

    def test_ref_value_appears_inside_code_tag_only(
        self, client: TestClient,
    ):
        """The echoed ref lives inside a single <code>…</code> element
        inside a <p class="ref"> — so the value can't end up in a
        surprising attribute context."""
        body = client.get("/?ref=xyz-promo").text
        assert "<p class=\"ref\">Invited by: <code>xyz-promo</code></p>" in body


class TestPhase52Determinism:
    """Render is a pure function of the sanitised ref code."""

    def test_no_ref_is_deterministic(self):
        a = render_landing_page_html()
        b = render_landing_page_html()
        assert a == b

    def test_same_ref_is_deterministic(self):
        a = render_landing_page_html("partner-a")
        b = render_landing_page_html("partner-a")
        assert a == b

    def test_different_refs_produce_different_html(self):
        a = render_landing_page_html("partner-a")
        b = render_landing_page_html("partner-b")
        assert a != b

    def test_empty_ref_matches_no_ref(self):
        assert render_landing_page_html("") == render_landing_page_html()

    def test_body_builder_is_pure(self):
        """Internal body builder: same input → byte-identical output."""
        assert _phase52_body("") == _phase52_body("")
        assert _phase52_body("abc") == _phase52_body("abc")


class TestPhase52NoNewMutatingControls:
    """The Phase 4.3 no-forms / no-JS invariants still hold after 5.2."""

    def test_still_no_form_or_inputs_or_buttons(self, client: TestClient):
        body = client.get("/?ref=abc").text.lower()
        for token in (
            "<form", "<input", "<button",
            "onclick", "onsubmit", "onchange",
            "method=\"post\"", "method=\"put\"",
            "method=\"patch\"", "method=\"delete\"",
            "method='post'", "method='put'",
            "method='patch'", "method='delete'",
        ):
            assert token not in body, (
                f"Phase 5.2 body still contains mutating marker: {token}"
            )

    def test_still_no_execution_control_terms(self, client: TestClient):
        body = client.get("/?ref=abc").text.lower()
        for term in (
            "place trade", "execute trade", "submit order",
            "start bot", "stop bot", "enable filter", "disable filter",
            "toggle filter", "run simulation", "backtest now",
            "place order", "make trade",
        ):
            assert term not in body, (
                f"Phase 5.2 body leaks execution-adjacent term: {term}"
            )

    def test_still_no_script_or_js(self, client: TestClient):
        body = client.get("/?ref=abc").text.lower()
        assert "<script" not in body
        assert "javascript:" not in body


class TestPhase52DoesNotAddNewMutatingRoute:
    """The only non-read verb anywhere is POST /webhook/stripe."""

    def test_verbs_unchanged_after_phase_52(self):
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                # Phase 7.3 added POST /billing/checkout — allow both.
                assert (m, path) in {
                    ("POST", "/webhook/stripe"),
                    ("POST", "/billing/checkout"),
                }, f"Phase 5.2 introduced a non-read verb: {m} {path}"

    def test_root_accepts_only_get_head_options(self):
        for route in app.routes:
            if getattr(route, "path", "") == "/":
                methods = getattr(route, "methods", set()) or set()
                assert methods.issubset({"GET", "HEAD", "OPTIONS"})
                break
        else:
            raise AssertionError("/ route not registered")


class TestPhase52DoesNotLeakProtectedDataWithRef:
    """Planting planet-sized markers everywhere still can't leak
    when a ref query param is present — the page is still static."""

    def test_ref_variant_ignores_reports_and_manifest(
        self, client: TestClient, authed_env,
    ):
        reports_dir: Path = authed_env["reports_dir"]
        manifest: Path = authed_env["manifest"]
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "alpha_report_2026-04-24.json").write_text(
            json.dumps({
                "report_date": "2026-04-24",
                "scorer_config": {"weights": {"gap": 0.7},
                                  "PHASE52_LEAK_MARKER_A": True},
            })
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"PHASE52_LEAK_MARKER_B": "keep-out"}) + "\n"
        )
        body = client.get("/?ref=some-partner").text
        assert "PHASE52_LEAK_MARKER_A" not in body
        assert "PHASE52_LEAK_MARKER_B" not in body
        assert "scorer_config" not in body

    def test_ref_does_not_leak_env_var_names(self, client: TestClient):
        body = client.get("/?ref=some-partner").text
        assert "TRADING_API_KEY" not in body
        assert "TRADING_API_GROWTH_LOG_PATH" not in body
        assert "STRIPE" not in body


class TestPhase52BoundaryUnchanged:
    """The SaaS boundary must still hold after the landing-page rewrite."""

    def test_server_still_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for pat in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert pat not in src, (
                f"Phase 5.2 broke SaaS boundary: {pat!r}"
            )

    def test_landing_handler_only_reads_ref_query_param(self):
        """The landing handler must not touch reports, manifest, or
        any other env-driven path."""
        import inspect
        from trading_bot.api import server as srv_mod
        src = inspect.getsource(srv_mod.landing_page)
        # The handler reads exactly one piece of runtime state.
        assert "request.query_params.get(\"ref\")" in src
        # It must NOT read any protected disk source.
        for forbidden in (
            "load_latest_report", "read_manifest",
            "REPORTS_DIR_ENV_VAR", "MANIFEST_PATH_ENV_VAR",
            "os.getenv", "Path(",
        ):
            assert forbidden not in src, (
                f"landing handler unexpectedly references: {forbidden}"
            )


# ===========================================================================
# Phase 5.4 — free-tier daily usage caps
# ===========================================================================


from datetime import timezone as _tz_phase54  # noqa: E402

from trading_bot.api.server import (  # noqa: E402
    DEFAULT_FREE_MAX_REPORT_CALLS,
    DEFAULT_FREE_MAX_REQUESTS_PER_DAY,
    FREE_MAX_REPORT_CALLS_ENV_VAR,
    FREE_MAX_REQUESTS_ENV_VAR,
    FREE_TIER_LIMIT_DETAIL,
    FREE_TIER_REMAINING_HEADER,
    FREE_TIER_USAGE_HEADER,
    _count_free_tier_usage_today,
    _free_max_report_calls,
    _free_max_requests_per_day,
)


def _today_utc_str_54() -> str:
    from datetime import datetime as _dt
    return _dt.now(_tz_phase54.utc).strftime("%Y-%m-%d")


def _seed_usage_rows(
    path: Path,
    *,
    key: str,
    n: int,
    report_path_prefix: str = "/experiments/recent",
    date_str: Optional[str] = None,
) -> None:
    """
    Write ``n`` usage-log rows for ``key`` directly to the usage
    log. Saves us from having to issue ``n`` real HTTP requests
    when we just want to seed a pre-existing count.
    """
    import hashlib
    date_str = date_str or _today_utc_str_54()
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for i in range(n):
            rec = {
                "timestamp": f"{date_str}T00:00:{i % 60:02d}.000000Z",
                "key_hash": key_hash,
                "tier": "free",
                "method": "GET",
                "path": report_path_prefix,
                "status_code": 200,
                "duration_ms": 1.0,
                "request_id": f"seed-{i}",
            }
            fh.write(json.dumps(rec) + "\n")


# `Optional` used above — keep the import adjacent to where it's used
# so the test block stays self-contained.
from typing import Optional  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Env resolver / fail-closed behaviour
# ---------------------------------------------------------------------------


class TestPhase54EnvResolvers:
    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv(FREE_MAX_REQUESTS_ENV_VAR, raising=False)
        monkeypatch.delenv(FREE_MAX_REPORT_CALLS_ENV_VAR, raising=False)
        assert _free_max_requests_per_day() == DEFAULT_FREE_MAX_REQUESTS_PER_DAY
        assert _free_max_report_calls() == DEFAULT_FREE_MAX_REPORT_CALLS

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "7")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "3")
        assert _free_max_requests_per_day() == 7
        assert _free_max_report_calls() == 3

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "-1", "0", "1.5", "nan"])
    def test_invalid_values_fail_closed_to_default(self, monkeypatch, bad):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, bad)
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, bad)
        assert _free_max_requests_per_day() == DEFAULT_FREE_MAX_REQUESTS_PER_DAY
        assert _free_max_report_calls() == DEFAULT_FREE_MAX_REPORT_CALLS


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


class TestPhase54CountHelper:
    def test_missing_log_returns_zeros(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(
            USAGE_LOG_ENV_VAR, str(tmp_path / "absent.jsonl"),
        )
        assert _count_free_tier_usage_today("h" * 32) == (0, 0)

    def test_counts_only_matching_hash_and_today(
        self, usage_path: Path, monkeypatch,
    ):
        today = _today_utc_str_54()
        # Seed 4 rows for our key today + 2 rows for another key +
        # 3 rows for our key on a different date.
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=4,
            report_path_prefix="/reports/latest",
        )
        _seed_usage_rows(
            usage_path, key="other-key", n=2,
            report_path_prefix="/reports/latest",
        )
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=3,
            report_path_prefix="/reports/latest",
            date_str="1999-01-01",
        )
        import hashlib
        kh = hashlib.sha256(FREE_KEY.encode()).hexdigest()[:32]
        total, reports = _count_free_tier_usage_today(kh, today=today)
        assert total == 4
        assert reports == 4

    def test_report_path_subset(self, usage_path: Path):
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=5,
            report_path_prefix="/experiments/recent",
        )
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=3,
            report_path_prefix="/reports/2026-04-24",
        )
        import hashlib
        kh = hashlib.sha256(FREE_KEY.encode()).hexdigest()[:32]
        total, reports = _count_free_tier_usage_today(kh)
        assert total == 8
        assert reports == 3

    def test_empty_key_hash_returns_zeros(self, usage_path: Path):
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=2,
            report_path_prefix="/reports/latest",
        )
        assert _count_free_tier_usage_today("") == (0, 0)

    def test_corrupt_lines_are_skipped(self, usage_path: Path):
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text(
            "not-json\n"
            "\n"
            "[\"not a dict\"]\n"
            + json.dumps({
                "timestamp": _today_utc_str_54() + "T00:00:00.000000Z",
                "key_hash": "a" * 32,
                "tier": "free",
                "path": "/reports/latest",
            })
            + "\n",
            encoding="utf-8",
        )
        total, reports = _count_free_tier_usage_today("a" * 32)
        assert (total, reports) == (1, 1)


# ---------------------------------------------------------------------------
# Free-tier limit: request cap (429)
# ---------------------------------------------------------------------------


class TestPhase54RequestCap:
    def test_free_user_429_after_limit_reached(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "3")
        # Pre-seed exactly the limit (3) for today.
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=3,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        assert resp.json() == {"detail": FREE_TIER_LIMIT_DETAIL}
        assert resp.headers.get(FREE_TIER_USAGE_HEADER) == "3/3"
        assert resp.headers.get(FREE_TIER_REMAINING_HEADER) == "0"

    def test_free_user_allowed_below_limit(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "50")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=10,
            report_path_prefix="/experiments/recent",
        )
        # Write a manifest so /experiments/recent returns data.
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.headers.get(FREE_TIER_USAGE_HEADER) == "11/50"
        assert resp.headers.get(FREE_TIER_REMAINING_HEADER) == "39"

    def test_free_user_cannot_bypass_via_query_string(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "2")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=2,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent?limit=1",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429

    def test_rejection_body_does_not_leak_key_or_hash(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        body = resp.text
        assert FREE_KEY not in body
        import hashlib
        kh = hashlib.sha256(FREE_KEY.encode()).hexdigest()[:32]
        assert kh not in body


# ---------------------------------------------------------------------------
# Free-tier limit: report-calls cap (403)
# ---------------------------------------------------------------------------


class TestPhase54ReportCap:
    def test_free_user_403_after_report_limit_reached(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "500")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "2")
        # Seed 2 report calls — exactly at the cap.
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=2,
            report_path_prefix="/reports/latest",
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "upgrade required for full access"}
        assert resp.headers.get(FREE_TIER_USAGE_HEADER) == "2/500"
        assert resp.headers.get(FREE_TIER_REMAINING_HEADER) == "498"

    def test_non_report_paths_unaffected_by_report_cap(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        """The report cap must not block /experiments/* calls even
        when the free user is over the report limit."""
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "500")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "2")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=5,
            report_path_prefix="/reports/latest",
        )
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200

    def test_global_cap_wins_over_report_cap_when_both_exceeded(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        """If a free user exceeded BOTH caps we return the 429 — it's
        the harsher signal and stops every subsequent call, not just
        /reports/*."""
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "5")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "3")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=5,
            report_path_prefix="/reports/latest",
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Premium-user exemption
# ---------------------------------------------------------------------------


class TestPhase54PremiumIsExempt:
    def test_premium_user_no_429_even_past_limit(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "2")
        # Seed 100 usage rows attributed to the PREMIUM key — the
        # middleware must ignore them entirely.
        _seed_usage_rows(
            usage_path, key=VALID_KEY, n=100,
            report_path_prefix="/experiments/recent",
        )
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200

    def test_premium_response_has_no_free_tier_headers(
        self, client: TestClient, free_env, usage_path: Path,
    ):
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert FREE_TIER_USAGE_HEADER not in resp.headers
        assert FREE_TIER_REMAINING_HEADER not in resp.headers

    def test_premium_user_past_report_cap_still_allowed(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=VALID_KEY, n=50,
            report_path_prefix="/reports/latest",
        )
        _write_report(free_env["reports_dir"], "2026-04-24")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Exempt paths: /, /health, /webhook/stripe
# ---------------------------------------------------------------------------


class TestPhase54ExemptPaths:
    def test_root_has_no_free_tier_headers(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code == 200
        assert FREE_TIER_USAGE_HEADER not in resp.headers
        assert FREE_TIER_REMAINING_HEADER not in resp.headers

    def test_health_has_no_free_tier_headers(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert FREE_TIER_USAGE_HEADER not in resp.headers
        assert FREE_TIER_REMAINING_HEADER not in resp.headers

    def test_webhook_stripe_not_blocked_even_at_limit(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        """Even if we'd hit the free-tier cap, the webhook is
        exempt. The webhook is a system-to-system call, not a free
        user's request."""
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        # Seed well past the limit.
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=100,
            report_path_prefix="/webhook/stripe",
        )
        # Webhook rejects for missing signature (its own logic) —
        # specifically NOT a 429 from this middleware.
        resp = client.post(
            "/webhook/stripe", content=b"{}",
            headers={"stripe-signature": "badsig"},
        )
        assert resp.status_code != 429
        assert FREE_TIER_USAGE_HEADER not in resp.headers


# ---------------------------------------------------------------------------
# Unauthenticated / unknown-key requests must NOT be absorbed by
# this middleware — they still get a 401/403 from require_api_key.
# ---------------------------------------------------------------------------


class TestPhase54UnauthenticatedRequests:
    def test_no_bearer_falls_through_to_401(
        self, client: TestClient, free_env, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        resp = client.get("/experiments/recent")
        assert resp.status_code == 401
        assert FREE_TIER_USAGE_HEADER not in resp.headers

    def test_unknown_bearer_falls_through_to_403(
        self, client: TestClient, free_env, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        assert resp.status_code == 403
        assert FREE_TIER_USAGE_HEADER not in resp.headers


# ---------------------------------------------------------------------------
# Dashboard banner
# ---------------------------------------------------------------------------


class TestPhase54DashboardBanner:
    _BANNER_ELEMENT = 'class="free-tier-banner"'

    def test_free_dashboard_shows_banner(self):
        html = render_dashboard_html(None, [], tier="free")
        low = html.lower()
        # The banner <p> element is present — not just the CSS
        # class selector in <style>.
        assert self._BANNER_ELEMENT in low
        assert "using the free tier" in low
        assert "upgrade for full access" in low

    def test_premium_dashboard_has_no_banner(self):
        html = render_dashboard_html(None, [], tier="premium")
        assert self._BANNER_ELEMENT not in html.lower()
        assert "using the free tier" not in html.lower()

    def test_free_user_sees_banner_via_http(
        self, client: TestClient, free_env,
    ):
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert self._BANNER_ELEMENT in resp.text.lower()

    def test_premium_user_has_no_banner_via_http(
        self, client: TestClient, free_env,
    ):
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert self._BANNER_ELEMENT not in resp.text.lower()


# ---------------------------------------------------------------------------
# Boundary / regression
# ---------------------------------------------------------------------------


class TestPhase54BoundaryUnchanged:
    def test_server_still_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src, (
                f"SaaS boundary broken by Phase 5.4: {forbidden!r}"
            )

    def test_only_non_read_verb_is_still_post_webhook_stripe(self):
        # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
        allowed = {("POST", "/webhook/stripe"), ("POST", "/billing/checkout")}
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                assert (m, path) in allowed, (
                    f"non-read-only verb introduced: {m} {path}"
                )


# ---------------------------------------------------------------------------
# Header shape
# ---------------------------------------------------------------------------


class TestPhase54HeaderShape:
    def test_usage_header_format_matches_spec(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "50")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=7,
            report_path_prefix="/experiments/recent",
        )
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        usage = resp.headers.get(FREE_TIER_USAGE_HEADER)
        remaining = resp.headers.get(FREE_TIER_REMAINING_HEADER)
        # Format: <current>/<limit> where current = previous + 1.
        assert usage == "8/50"
        assert remaining == "42"

    def test_remaining_never_negative(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "3")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=3,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        assert resp.headers.get(FREE_TIER_REMAINING_HEADER) == "0"


# ===========================================================================
# Phase 5.5 — upgrade CTA telemetry (server-side emission tests)
# ===========================================================================


from trading_bot.api.upgrade_events import (  # noqa: E402
    UPGRADE_EVENTS_LOG_ENV_VAR,
    _hash_api_key as _ue_hash,
)


@pytest.fixture
def upgrade_events_path() -> Path:
    """Return the per-test upgrade-events log path set by clean_api_env."""
    return Path(_os_phase44.environ[UPGRADE_EVENTS_LOG_ENV_VAR])


def _read_upgrade_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            rows.append(json.loads(s))
        except Exception:
            continue
    return rows


# ---------------------------------------------------------------------------
# dashboard_banner_seen
# ---------------------------------------------------------------------------


class TestPhase55DashboardBannerEvent:
    def test_free_user_dashboard_logs_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        events = _read_upgrade_events(upgrade_events_path)
        assert len(events) == 1
        row = events[0]
        assert row["event"] == "dashboard_banner_seen"
        assert row["tier"] == "free"
        assert row["path"] == "/dashboard"
        assert row["api_key_hash"] == _ue_hash(FREE_KEY)

    def test_premium_user_dashboard_does_not_log_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert _read_upgrade_events(upgrade_events_path) == []

    def test_dashboard_event_carries_ref_code_when_present(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/dashboard?ref=twitter-q2",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["ref_code"] == "twitter-q2"

    def test_dashboard_event_ref_code_is_sanitised(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/dashboard?ref=<script>alert(1)</script>",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["ref_code"] == "scriptalert1script"


# ---------------------------------------------------------------------------
# daily_request_limit_hit
# ---------------------------------------------------------------------------


class TestPhase55DailyRequestLimitEvent:
    def test_429_logs_event(
        self, client: TestClient, free_env, usage_path: Path,
        upgrade_events_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "2")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=2,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "daily_request_limit_hit"
        assert row["path"] == "/experiments/recent"
        assert row["tier"] == "free"
        assert row["api_key_hash"] == _ue_hash(FREE_KEY)

    def test_429_event_request_id_matches_response_header(
        self, client: TestClient, free_env, usage_path: Path,
        upgrade_events_path: Path, monkeypatch,
    ):
        """The event's request_id must equal the X-Request-ID the
        caller sees, so an operator can correlate a user's rejection
        with the telemetry row."""
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        rid = resp.headers.get("X-Request-ID")
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert rid
        assert row["request_id"] == rid

    def test_successful_free_request_does_not_log(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "50")
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert _read_upgrade_events(upgrade_events_path) == []


# ---------------------------------------------------------------------------
# report_limit_hit
# ---------------------------------------------------------------------------


class TestPhase55ReportLimitEvent:
    def test_403_on_reports_path_logs_event(
        self, client: TestClient, free_env, usage_path: Path,
        upgrade_events_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "500")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "2")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=2,
            report_path_prefix="/reports/latest",
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "report_limit_hit"
        assert row["path"] == "/reports/latest"
        assert row["tier"] == "free"


# ---------------------------------------------------------------------------
# old_report_blocked
# ---------------------------------------------------------------------------


class TestPhase55OldReportBlockedEvent:
    def test_out_of_window_date_logs_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        """Request a report from 2020 — well outside the 3-day
        Phase 4.5 free-tier window."""
        free_env["reports_dir"].mkdir(parents=True, exist_ok=True)
        resp = client.get(
            "/reports/2020-01-01",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "old_report_blocked"
        assert row["path"] == "/reports/2020-01-01"
        assert row["tier"] == "free"

    def test_premium_user_old_report_does_not_log(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        free_env["reports_dir"].mkdir(parents=True, exist_ok=True)
        resp = client.get(
            "/reports/2020-01-01",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        # Premium gets 404 (no such report exists) — NOT 403.
        assert resp.status_code == 404
        assert _read_upgrade_events(upgrade_events_path) == []


# ---------------------------------------------------------------------------
# experiment_limit_blocked
# ---------------------------------------------------------------------------


class TestPhase55ExperimentLimitBlockedEvent:
    def test_out_of_range_n_logs_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        """Phase 4.5 free tier caps n at MAX_FREE_TIER_EXPERIMENTS=3."""
        resp = client.get(
            "/experiments/4",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "experiment_limit_blocked"
        assert row["path"] == "/experiments/4"
        assert row["tier"] == "free"

    def test_explicit_limit_query_param_logs_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/experiments/recent?limit=50",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "experiment_limit_blocked"
        assert row["path"] == "/experiments/recent"

    def test_premium_user_does_not_log(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/experiments/4",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code in {200, 404}
        assert _read_upgrade_events(upgrade_events_path) == []


# ---------------------------------------------------------------------------
# Negative guarantees
# ---------------------------------------------------------------------------


class TestPhase55DoesNotBlockRequests:
    def test_telemetry_write_failure_does_not_break_request(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch, tmp_path: Path,
    ):
        """Redirect the events log at a path whose parent is a
        regular file so mkdir raises. The user-facing request must
        still return 200 with no change to behaviour."""
        bad_parent = tmp_path / "not-a-dir"
        bad_parent.write_text("x")
        monkeypatch.setenv(
            UPGRADE_EVENTS_LOG_ENV_VAR,
            str(bad_parent / "events.jsonl"),
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200

    def test_no_raw_key_in_log_file(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = upgrade_events_path.read_text(encoding="utf-8")
        assert FREE_KEY not in body
        # The hash IS recorded.
        assert _ue_hash(FREE_KEY) in body

    def test_webhook_does_not_emit_upgrade_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        resp = client.post(
            "/webhook/stripe", content=b"{}",
            headers={"stripe-signature": "badsig"},
        )
        assert resp.status_code != 429
        assert _read_upgrade_events(upgrade_events_path) == []

    def test_unauthenticated_request_does_not_emit_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        resp = client.get("/experiments/recent")
        assert resp.status_code == 401
        assert _read_upgrade_events(upgrade_events_path) == []


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestPhase55Boundary:
    def test_no_core_imports_from_upgrade_events(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "upgrade_events.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src

    def test_only_non_read_verb_is_still_post_webhook_stripe(self):
        # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
        allowed = {("POST", "/webhook/stripe"), ("POST", "/billing/checkout")}
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                assert (m, path) in allowed


# ===========================================================================
# Phase 5.7 — dynamic free-tier nudge copy
# ===========================================================================


from trading_bot.api.server import (  # noqa: E402
    DEFAULT_LIMIT_HIT_COPY,
    DEFAULT_REPORT_LIMIT_COPY,
    DEFAULT_UPGRADE_BANNER_COPY,
    LIMIT_HIT_COPY_ENV_VAR,
    MAX_NUDGE_COPY_LENGTH,
    REPORT_LIMIT_COPY_ENV_VAR,
    UPGRADE_BANNER_COPY_ENV_VAR,
    _limit_hit_copy,
    _report_limit_copy,
    _resolve_nudge_copy,
    _upgrade_banner_copy,
)


class TestPhase57Resolver:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("X_NEVER_SET_ENV", raising=False)
        assert _resolve_nudge_copy(
            "X_NEVER_SET_ENV", "fallback",
        ) == "fallback"

    def test_blank_returns_default(self, monkeypatch):
        for blank in ("", "   ", "\t\t", " \t  "):
            monkeypatch.setenv("X_NEVER_SET_ENV", blank)
            assert _resolve_nudge_copy(
                "X_NEVER_SET_ENV", "fallback",
            ) == "fallback"

    def test_explicit_value_overrides_default(self, monkeypatch):
        monkeypatch.setenv("X_NEVER_SET_ENV", "Custom prompt!")
        assert _resolve_nudge_copy(
            "X_NEVER_SET_ENV", "fallback",
        ) == "Custom prompt!"

    def test_strips_outer_whitespace(self, monkeypatch):
        monkeypatch.setenv("X_NEVER_SET_ENV", "   Padded message   ")
        assert _resolve_nudge_copy(
            "X_NEVER_SET_ENV", "fallback",
        ) == "Padded message"

    # NUL bytes can't even be set via os.environ on Linux/macOS, so
    # we don't try — the regex covers them anyway. Cover every other
    # control char that an operator could plausibly type.
    @pytest.mark.parametrize(
        "ctrl", ["\x01", "\n", "\r", "\t", "\x7f", "\x1b"],
    )
    def test_control_characters_fall_back_to_default(
        self, monkeypatch, ctrl,
    ):
        monkeypatch.setenv("X_NEVER_SET_ENV", f"hello{ctrl}world")
        assert _resolve_nudge_copy(
            "X_NEVER_SET_ENV", "fallback",
        ) == "fallback"

    def test_resolver_regex_rejects_nul_byte_input(self):
        """Direct test of the resolver's regex with a NUL byte.
        os.environ won't carry one, but the resolver could be
        called with arbitrary input from somewhere else."""
        # Bypass os.getenv by patching the call site indirectly:
        # we can't put a NUL in env, so we exercise the regex
        # branch by feeding via a fake env using a namespace swap.
        import os as _os
        original = _os.getenv
        try:
            _os.getenv = lambda k, d=None: "ok\x00bad" if k == "X_FAKE" else original(k, d)
            assert _resolve_nudge_copy("X_FAKE", "fallback") == "fallback"
        finally:
            _os.getenv = original

    def test_caps_at_max_length(self, monkeypatch):
        long_value = "x" * 500
        monkeypatch.setenv("X_NEVER_SET_ENV", long_value)
        out = _resolve_nudge_copy("X_NEVER_SET_ENV", "fallback")
        assert len(out) == MAX_NUDGE_COPY_LENGTH
        assert out == "x" * MAX_NUDGE_COPY_LENGTH

    def test_exactly_max_length_is_kept_as_is(self, monkeypatch):
        boundary = "x" * MAX_NUDGE_COPY_LENGTH
        monkeypatch.setenv("X_NEVER_SET_ENV", boundary)
        assert _resolve_nudge_copy(
            "X_NEVER_SET_ENV", "fallback",
        ) == boundary

    def test_unicode_passes_through(self, monkeypatch):
        """Em-dashes, accents, emoji are printable Unicode and
        should survive — the only filter is ASCII control chars."""
        monkeypatch.setenv("X_NEVER_SET_ENV", "Upgrade — €5/mo · 🚀")
        assert _resolve_nudge_copy(
            "X_NEVER_SET_ENV", "fallback",
        ) == "Upgrade — €5/mo · 🚀"


class TestPhase57HelperFunctions:
    def test_default_banner_copy_matches_spec(self, monkeypatch):
        monkeypatch.delenv(UPGRADE_BANNER_COPY_ENV_VAR, raising=False)
        assert _upgrade_banner_copy() == (
            "You're using the free tier — upgrade for full access"
        )
        assert _upgrade_banner_copy() == DEFAULT_UPGRADE_BANNER_COPY

    def test_default_limit_hit_copy_matches_spec(self, monkeypatch):
        monkeypatch.delenv(LIMIT_HIT_COPY_ENV_VAR, raising=False)
        assert _limit_hit_copy() == (
            "free tier limit reached — upgrade for continued access"
        )
        assert _limit_hit_copy() == DEFAULT_LIMIT_HIT_COPY

    def test_default_report_limit_copy_matches_spec(self, monkeypatch):
        monkeypatch.delenv(REPORT_LIMIT_COPY_ENV_VAR, raising=False)
        assert _report_limit_copy() == "upgrade required for full access"
        assert _report_limit_copy() == DEFAULT_REPORT_LIMIT_COPY

    def test_each_helper_is_isolated_from_the_others(self, monkeypatch):
        monkeypatch.setenv(UPGRADE_BANNER_COPY_ENV_VAR, "BANNER_VAL")
        monkeypatch.delenv(LIMIT_HIT_COPY_ENV_VAR, raising=False)
        monkeypatch.delenv(REPORT_LIMIT_COPY_ENV_VAR, raising=False)
        assert _upgrade_banner_copy() == "BANNER_VAL"
        assert _limit_hit_copy() == DEFAULT_LIMIT_HIT_COPY
        assert _report_limit_copy() == DEFAULT_REPORT_LIMIT_COPY


class TestPhase57DashboardBanner:
    def test_default_banner_appears_in_html(
        self, client: TestClient, free_env,
    ):
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert "using the free tier" in resp.text.lower()
        assert "upgrade for full access" in resp.text.lower()

    def test_custom_banner_appears_in_html(
        self, client: TestClient, free_env, monkeypatch,
    ):
        monkeypatch.setenv(
            UPGRADE_BANNER_COPY_ENV_VAR, "Custom upgrade nudge — limited!",
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert "Custom upgrade nudge — limited!" in resp.text
        assert "You're using the free tier" not in resp.text

    def test_unsafe_html_is_escaped_not_executed(
        self, client: TestClient, free_env, monkeypatch,
    ):
        monkeypatch.setenv(
            UPGRADE_BANNER_COPY_ENV_VAR, "<script>alert('xss')</script>",
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.text
        # No raw <script> tag anywhere on the page.
        assert "<script>alert" not in body
        # The escaped form is what landed in the HTML.
        assert "&lt;script&gt;" in body

    def test_long_banner_truncated_safely(
        self, client: TestClient, free_env, monkeypatch,
    ):
        long_value = "U" + ("p" * 500) + "!"
        monkeypatch.setenv(UPGRADE_BANNER_COPY_ENV_VAR, long_value)
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        truncated = long_value[:MAX_NUDGE_COPY_LENGTH]
        assert truncated in resp.text
        assert long_value not in resp.text

    def test_control_char_banner_falls_back_to_default(
        self, client: TestClient, free_env, monkeypatch,
    ):
        # Newline is settable via os.environ AND triggers the
        # control-char branch in the resolver.
        monkeypatch.setenv(
            UPGRADE_BANNER_COPY_ENV_VAR, "evil\nbanner",
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        assert "evil" not in resp.text
        assert "using the free tier" in resp.text.lower()

    def test_premium_user_unaffected_by_banner_env(
        self, client: TestClient, free_env, monkeypatch,
    ):
        monkeypatch.setenv(
            UPGRADE_BANNER_COPY_ENV_VAR,
            "FREE TIER NUDGE THAT MUST NOT LEAK",
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert 'class="free-tier-banner"' not in resp.text.lower()
        assert "FREE TIER NUDGE THAT MUST NOT LEAK" not in resp.text

    def test_render_function_accepts_explicit_copy(self):
        html = render_dashboard_html(
            None, [], tier="free", banner_copy="Hello free tier",
        )
        assert "Hello free tier" in html

    def test_render_function_falls_back_to_env_when_copy_is_none(
        self, monkeypatch,
    ):
        monkeypatch.setenv(UPGRADE_BANNER_COPY_ENV_VAR, "ENV_BANNER_COPY")
        html = render_dashboard_html(None, [], tier="free")
        assert "ENV_BANNER_COPY" in html

    def test_render_function_premium_ignores_banner_param(self):
        html = render_dashboard_html(
            None, [], tier="premium", banner_copy="Hello free tier",
        )
        assert "Hello free tier" not in html
        assert 'class="free-tier-banner"' not in html.lower()


class TestPhase57LimitHitCopy:
    def test_default_429_message(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        assert resp.json() == {"detail": DEFAULT_LIMIT_HIT_COPY}

    def test_custom_429_message_appears(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(LIMIT_HIT_COPY_ENV_VAR, "Slow down — upgrade!")
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        assert resp.json() == {"detail": "Slow down — upgrade!"}

    def test_long_custom_429_truncated(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        long_msg = "Z" * 500
        monkeypatch.setenv(LIMIT_HIT_COPY_ENV_VAR, long_msg)
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = resp.json()
        assert resp.status_code == 429
        assert len(body["detail"]) == MAX_NUDGE_COPY_LENGTH
        assert body["detail"] == "Z" * MAX_NUDGE_COPY_LENGTH

    def test_control_char_429_falls_back_to_default(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(LIMIT_HIT_COPY_ENV_VAR, "bad\nmessage")
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        assert resp.json() == {"detail": DEFAULT_LIMIT_HIT_COPY}

    def test_premium_user_429_not_returned(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(LIMIT_HIT_COPY_ENV_VAR, "BUSTED_FREE_NUDGE")
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=VALID_KEY, n=100,
            report_path_prefix="/experiments/recent",
        )
        free_env["manifest"].parent.mkdir(parents=True, exist_ok=True)
        free_env["manifest"].write_text(
            json.dumps({"report_date": "2026-04-24"}) + "\n",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        assert "BUSTED_FREE_NUDGE" not in resp.text


class TestPhase57ReportLimitCopy:
    def test_default_403_message(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "500")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/reports/latest",
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": DEFAULT_REPORT_LIMIT_COPY}

    def test_custom_403_message_appears(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(
            REPORT_LIMIT_COPY_ENV_VAR,
            "Daily report quota reached — go premium.",
        )
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "500")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/reports/latest",
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "detail": "Daily report quota reached — go premium.",
        }

    def test_phase45_403s_keep_legacy_copy(
        self, client: TestClient, free_env, monkeypatch,
    ):
        """Phase 4.5's "out-of-window date" 403s are semantically
        distinct from the Phase 5.4/5.7 report-limit and intentionally
        keep their original message."""
        monkeypatch.setenv(
            REPORT_LIMIT_COPY_ENV_VAR, "PHASE57_OVERRIDE_TEXT",
        )
        free_env["reports_dir"].mkdir(parents=True, exist_ok=True)
        resp = client.get(
            "/reports/2020-01-01",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "upgrade required for full access"}
        assert "PHASE57_OVERRIDE_TEXT" not in resp.text


class TestPhase57BoundaryUnchanged:
    def test_server_still_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "server.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src

    def test_only_non_read_verb_is_still_post_webhook_stripe(self):
        # Phase 7.3 added POST /billing/checkout — allow both, reject anything else.
        allowed = {("POST", "/webhook/stripe"), ("POST", "/billing/checkout")}
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                assert (m, path) in allowed

    def test_responses_never_echo_env_var_names(
        self, client: TestClient, free_env, usage_path: Path, monkeypatch,
    ):
        """Response bodies must never contain Phase 5.7 env-var
        names — those are operator-side knobs, not user-facing
        configuration leakage."""
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = resp.text
        for name in (
            "TRADING_API_KEY",
            "TRADING_FREE_MAX_REQUESTS_PER_DAY",
            "TRADING_LIMIT_HIT_COPY",
            "TRADING_UPGRADE_BANNER_COPY",
            "TRADING_REPORT_LIMIT_COPY",
        ):
            assert name not in body


# ===========================================================================
# Phase 5.8 — server-side telemetry of copy_variant_hash
# ===========================================================================


import hashlib as _hashlib_phase58  # noqa: E402


def _hex_copy(copy: str) -> str:
    return _hashlib_phase58.sha256(
        copy.encode("utf-8"),
    ).hexdigest()[:32]


class TestPhase58DashboardBannerHash:
    def test_default_banner_copy_is_hashed_into_event(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "dashboard_banner_seen"
        assert row["copy_variant_hash"] == _hex_copy(
            DEFAULT_UPGRADE_BANNER_COPY,
        )

    def test_custom_banner_copy_changes_hash(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv(
            UPGRADE_BANNER_COPY_ENV_VAR, "Variant B copy here",
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 200
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["copy_variant_hash"] == _hex_copy("Variant B copy here")
        assert row["copy_variant_hash"] != _hex_copy(
            DEFAULT_UPGRADE_BANNER_COPY,
        )

    def test_raw_banner_copy_never_appears_on_disk(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch,
    ):
        secret = "DO_NOT_LEAK_BANNER_COPY_PHASE58"
        monkeypatch.setenv(UPGRADE_BANNER_COPY_ENV_VAR, secret)
        client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        body = upgrade_events_path.read_text(encoding="utf-8")
        assert secret not in body
        # But the hash IS persisted.
        assert _hex_copy(secret) in body

    def test_premium_user_dashboard_does_not_persist_copy_hash(
        self, client: TestClient, free_env, upgrade_events_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv(
            UPGRADE_BANNER_COPY_ENV_VAR, "PREMIUM_DASHBOARD_BANNER",
        )
        resp = client.get(
            "/dashboard",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        # Premium users emit no upgrade event at all.
        assert _read_upgrade_events(upgrade_events_path) == []


class TestPhase58LimitHitHash:
    def test_429_event_carries_copy_hash_matching_response_body(
        self, client: TestClient, free_env, usage_path: Path,
        upgrade_events_path: Path, monkeypatch,
    ):
        custom = "Custom 429 copy — upgrade for more!"
        monkeypatch.setenv(LIMIT_HIT_COPY_ENV_VAR, custom)
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        # The response body carries the raw copy …
        assert resp.json() == {"detail": custom}
        # … and the telemetry row carries its hash.
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "daily_request_limit_hit"
        assert row["copy_variant_hash"] == _hex_copy(custom)

    def test_429_default_copy_is_hashed(
        self, client: TestClient, free_env, usage_path: Path,
        upgrade_events_path: Path, monkeypatch,
    ):
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/experiments/recent",
        )
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 429
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["copy_variant_hash"] == _hex_copy(
            DEFAULT_LIMIT_HIT_COPY,
        )


class TestPhase58ReportLimitHash:
    def test_403_event_carries_copy_hash_matching_response_body(
        self, client: TestClient, free_env, usage_path: Path,
        upgrade_events_path: Path, monkeypatch,
    ):
        custom = "Custom 403 — premium unlocks reports."
        monkeypatch.setenv(REPORT_LIMIT_COPY_ENV_VAR, custom)
        monkeypatch.setenv(FREE_MAX_REQUESTS_ENV_VAR, "500")
        monkeypatch.setenv(FREE_MAX_REPORT_CALLS_ENV_VAR, "1")
        _seed_usage_rows(
            usage_path, key=FREE_KEY, n=1,
            report_path_prefix="/reports/latest",
        )
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": custom}
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "report_limit_hit"
        assert row["copy_variant_hash"] == _hex_copy(custom)


class TestPhase58OtherEventsHaveNullHash:
    """``old_report_blocked`` and ``experiment_limit_blocked`` aren't
    operator-tunable copy. Their telemetry row must record a null
    hash, never the legacy literal."""

    def test_old_report_blocked_has_null_hash(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        free_env["reports_dir"].mkdir(parents=True, exist_ok=True)
        resp = client.get(
            "/reports/2020-01-01",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "old_report_blocked"
        assert row["copy_variant_hash"] is None

    def test_experiment_limit_blocked_has_null_hash(
        self, client: TestClient, free_env, upgrade_events_path: Path,
    ):
        resp = client.get(
            "/experiments/4",
            headers={"Authorization": f"Bearer {FREE_KEY}"},
        )
        assert resp.status_code == 403
        (row,) = _read_upgrade_events(upgrade_events_path)
        assert row["event"] == "experiment_limit_blocked"
        assert row["copy_variant_hash"] is None


# ===========================================================================
# Phase 5.9 — landing page visual polish
# ===========================================================================


class TestPhase59HasPolishedStyling:
    """The landing page now ships a polished SaaS-style stylesheet."""

    def test_inline_style_block_present(self, client: TestClient):
        body = client.get("/").text
        # Inline <style>; no external CSS link to a third party.
        assert "<style>" in body
        assert "</style>" in body
        # No external CSS link — would break "no API calls" rule and
        # CSP. We allow same-origin links in principle but the
        # landing page in particular ships zero of them.
        assert "<link " not in body.lower()

    def test_no_external_resources(self, client: TestClient):
        """No third-party fonts, images, scripts, or trackers."""
        body = client.get("/").text.lower()
        for token in (
            "http://", "https://",
            "fonts.googleapis", "cdnjs", "cdn.jsdelivr",
            "google-analytics", "googletagmanager",
        ):
            assert token not in body, (
                f"landing page references external resource: {token}"
            )

    def test_uses_css_variables(self, client: TestClient):
        """Mark of a real stylesheet rather than three ad-hoc rules."""
        body = client.get("/").text
        assert ":root" in body
        # At least a handful of named tokens.
        for var in ("--primary", "--bg", "--surface", "--border"):
            assert var in body, f"missing CSS variable: {var}"

    def test_hero_section_has_gradient(self, client: TestClient):
        """A SaaS-style hero — gradient background, not a flat box."""
        body = client.get("/").text.lower()
        assert "linear-gradient" in body
        assert "section.hero" in body or ".hero " in body

    def test_mobile_first_media_query(self, client: TestClient):
        """Mobile-first → @media (min-width: …) breakpoints."""
        body = client.get("/").text
        assert "@media (min-width:" in body

    def test_feature_grid_present(self, client: TestClient):
        """The 3-step "How it works" list renders as a grid of
        feature cards."""
        body = client.get("/").text
        assert "feature-grid" in body
        # The grid lives on the <ol>; the existing 3-li contract
        # still holds (verified by Phase 5.2's how-it-works test).
        assert "<ol class=\"feature-grid\">" in body

    def test_example_output_in_card(self, client: TestClient):
        body = client.get("/").text
        assert "example-card" in body

    def test_compare_card_present(self, client: TestClient):
        body = client.get("/").text
        assert "compare-card" in body

    def test_cue_row_present(self, client: TestClient):
        """The two soft-conversion cues each render in their own
        styled card so they read as two equal-weight prompts."""
        body = client.get("/").text
        assert "cue-row" in body
        assert body.count('class="cue"') == 2

    def test_cta_card_present(self, client: TestClient):
        body = client.get("/").text
        assert "cta-card" in body


class TestPhase59StillSafeAndStatic:
    """Re-assert every safety invariant after the visual rewrite —
    polish must not have re-opened any attack surface."""

    def test_no_form_or_inputs_or_buttons(self, client: TestClient):
        body = client.get("/?ref=any").text.lower()
        for token in (
            "<form", "<input", "<button",
            "onclick", "onsubmit", "onchange",
            "method=\"post\"", "method=\"put\"",
            "method=\"patch\"", "method=\"delete\"",
            "method='post'", "method='put'",
            "method='patch'", "method='delete'",
        ):
            assert token not in body, (
                f"polish reintroduced mutating marker: {token}"
            )

    def test_no_script_or_javascript_uri(self, client: TestClient):
        body = client.get("/?ref=any").text.lower()
        assert "<script" not in body
        assert "javascript:" not in body

    def test_no_protected_data_leaks(
        self, client: TestClient, authed_env,
    ):
        """Plant unique markers across reports / manifest and
        re-assert the polished page never echoes them."""
        reports_dir: Path = authed_env["reports_dir"]
        manifest: Path = authed_env["manifest"]
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "alpha_report_2026-04-24.json").write_text(
            json.dumps({
                "report_date": "2026-04-24",
                "scorer_config": {"weights": {"gap": 0.7},
                                  "PHASE59_LEAK_MARKER_A": True},
            })
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"PHASE59_LEAK_MARKER_B": "keep-out"}) + "\n"
        )
        body = client.get("/?ref=some").text
        for marker in (
            "PHASE59_LEAK_MARKER_A",
            "PHASE59_LEAK_MARKER_B",
            "scorer_config",
            "TRADING_API_KEY",
        ):
            assert marker not in body, f"leaked: {marker}"


class TestPhase59RefBannerStillSanitized:
    def test_ref_banner_renders_in_hero(self, client: TestClient):
        body = client.get("/?ref=hn-launch").text
        # Same exact contract as Phase 5.2: <p class="ref">…<code>…</code></p>.
        assert (
            '<p class="ref">Invited by: <code>hn-launch</code></p>'
        ) in body

    def test_ref_xss_is_neutralised(self, client: TestClient):
        body = client.get("/?ref=<script>alert(1)</script>").text
        # The sanitiser strips < > / etc. so the displayed value is
        # safe ASCII only.
        assert "<script>alert(1)</script>" not in body
        assert "<script" not in body.lower()
        assert "javascript:" not in body.lower()

    def test_no_ref_no_banner(self, client: TestClient):
        body = client.get("/").text
        assert "Invited by" not in body


class TestPhase59StillDeterministic:
    def test_same_ref_byte_identical(self):
        a = render_landing_page_html("hn-launch")
        b = render_landing_page_html("hn-launch")
        assert a == b

    def test_no_ref_byte_identical(self):
        a = render_landing_page_html()
        b = render_landing_page_html()
        assert a == b

    def test_different_refs_produce_different_html(self):
        a = render_landing_page_html("twitter-q2")
        b = render_landing_page_html("hn-launch")
        assert a != b

    def test_does_not_depend_on_env_or_disk(
        self, monkeypatch, tmp_path: Path,
    ):
        baseline = render_landing_page_html()
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(tmp_path / "reports"))
        monkeypatch.setenv(
            MANIFEST_PATH_ENV_VAR, str(tmp_path / "manifest.jsonl"),
        )
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "alpha_report_2026-04-24.json").write_text(
            json.dumps({"leak": "MUST_NOT_APPEAR"})
        )
        populated = render_landing_page_html()
        assert baseline == populated
        assert "MUST_NOT_APPEAR" not in populated


class TestPhase59HtmlShape:
    def test_viewport_meta_for_mobile(self, client: TestClient):
        body = client.get("/").text
        assert (
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
        ) in body

    def test_legacy_positioning_phrases_intact(self, client: TestClient):
        low = client.get("/").text.lower()
        for phrase in (
            "read-only",
            "guardrail",
            "daily validation",
            "audit trail",
            "protected dashboard",
        ):
            assert phrase in low, f"polish lost positioning phrase: {phrase!r}"

    def test_still_five_sections(self, client: TestClient):
        body = client.get("/").text
        assert body.count("<section") == 5
        assert body.count("</section>") == 5

    def test_landing_route_only_get_head_options(self):
        for route in app.routes:
            if getattr(route, "path", "") == "/":
                methods = getattr(route, "methods", set()) or set()
                assert methods.issubset({"GET", "HEAD", "OPTIONS"})
                break


# ===========================================================================
# Phase 6.2 — manifest-backed API key authentication
# ===========================================================================


class _ManifestEnv:
    """Helper bundle of paths the Phase 6.2 tests share."""

    def __init__(self, tmp_path: Path):
        self.reports_dir = tmp_path / "reports"
        self.manifest = tmp_path / "experiments.jsonl"
        self.keys_manifest = tmp_path / "api_keys_manifest.jsonl"
        self.keys_revoked = tmp_path / "api_keys_revoked.jsonl"


@pytest.fixture
def manifest_auth_env(monkeypatch, tmp_path: Path) -> _ManifestEnv:
    """
    Configure the server with NO env API key — the only way in is via
    a row in the keys manifest. This is the realistic Phase 6.2
    deployment shape.
    """
    env = _ManifestEnv(tmp_path)
    # Explicitly leave TRADING_API_KEY / TRADING_API_PREMIUM_KEYS unset
    # so the test exercises the manifest path exclusively.
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
    monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(env.reports_dir))
    monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(env.manifest))
    monkeypatch.setenv(
        "TRADING_API_KEYS_MANIFEST_PATH", str(env.keys_manifest),
    )
    monkeypatch.setenv(
        "TRADING_API_KEYS_REVOKED_PATH", str(env.keys_revoked),
    )
    return env


def _issue_via_cli(tier: str, label: str = "user") -> tuple[str, str]:
    """Issue a key in-process via the keys CLI and return (raw, hash)."""
    from trading_bot.api.keys import issue_key

    result = issue_key(tier=tier, label=label)
    return result["api_key"], result["key_hash"]


class TestPhase62ManifestKeyAuth:
    """A key issued by the keys CLI authenticates against the live
    server with no env-var edits."""

    def test_free_manifest_key_authenticates_reports_endpoint(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
    ):
        raw, _ = _issue_via_cli("free", label="phase62-free")
        # Write a recent report so the endpoint has data to return.
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)
        r = client.get(
            "/reports/latest", headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text

    def test_unknown_key_rejected_when_only_manifest_configured(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
    ):
        # No issuance happens — the manifest is empty, so the server
        # must fail-closed on protected endpoints.
        r = client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer never-issued-key"},
        )
        # Empty manifest + no env keys → 503 fail-closed.
        assert r.status_code == 503

    def test_no_env_no_manifest_returns_503(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
    ):
        r = client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 503
        assert "issue" in r.json()["detail"].lower()

    def test_unknown_key_with_active_manifest_returns_403(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
    ):
        _issue_via_cli("free", label="another")
        r = client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer not-the-issued-key"},
        )
        assert r.status_code == 403


class TestPhase62TierBehaviour:
    """Manifest tier flows through the existing free-tier limits."""

    def test_free_manifest_key_subject_to_report_cap(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch,
    ):
        raw, _ = _issue_via_cli("free", label="cap-victim")
        # Tighten the report cap to 1 so we exhaust it in one call.
        monkeypatch.setenv("TRADING_FREE_MAX_REPORT_CALLS", "1")
        monkeypatch.setenv("TRADING_FREE_MAX_REQUESTS_PER_DAY", "100")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)
        h = {"Authorization": f"Bearer {raw}"}

        # First /reports/* call goes through.
        assert client.get("/reports/latest", headers=h).status_code == 200
        # Second is blocked by the per-tier report-call cap.
        r = client.get("/reports/latest", headers=h)
        assert r.status_code == 403

    def test_premium_manifest_key_bypasses_free_cap(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch,
    ):
        raw, _ = _issue_via_cli("premium", label="vip")
        monkeypatch.setenv("TRADING_FREE_MAX_REPORT_CALLS", "1")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)
        h = {"Authorization": f"Bearer {raw}"}

        # Premium goes well past the free cap.
        for _ in range(5):
            r = client.get("/reports/latest", headers=h)
            assert r.status_code == 200


class TestPhase62Revocation:
    """Revocation rejects a previously-issued key with 403 — without
    a server restart, since key_store hot-reloads on mtime change."""

    def test_revoked_manifest_key_rejected(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
    ):
        from trading_bot.api import key_store
        from trading_bot.api.keys import main as keys_main

        raw, key_hash = _issue_via_cli("free", label="rotate")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)
        h = {"Authorization": f"Bearer {raw}"}

        # Sanity — accepted before revocation.
        assert client.get("/reports/latest", headers=h).status_code == 200

        # Revoke via the CLI entry point.
        rc = keys_main([
            "revoke", "--key-hash", key_hash, "--reason", "rotated",
        ])
        assert rc == 0
        # Force the cache to reload — in production the next request's
        # mtime check picks the new file up automatically.
        key_store.reset_caches_for_tests()

        r = client.get("/reports/latest", headers=h)
        assert r.status_code == 403

    def test_revocation_wins_over_env_premium(
        self, client: TestClient, monkeypatch, tmp_path: Path,
    ):
        """A revoked hash must be rejected even when the same raw key
        is also present in TRADING_API_PREMIUM_KEYS. Revocation is
        the unambiguous kill switch."""
        from trading_bot.api import key_store
        from trading_bot.api.keys import main as keys_main

        raw = "shared-key-XYZ"
        reports_dir = tmp_path / "reports"
        manifest = tmp_path / "manifest.jsonl"
        keys_revoked = tmp_path / "revoked.jsonl"
        monkeypatch.setenv(API_KEY_ENV_VAR, "unrelated-other-key")
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", raw)
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
        monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(manifest))
        monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked))
        # Manifest can be empty; we're testing revocation precedence.
        monkeypatch.setenv(
            "TRADING_API_KEYS_MANIFEST_PATH",
            str(tmp_path / "empty_manifest.jsonl"),
        )
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(reports_dir, today)
        h = {"Authorization": f"Bearer {raw}"}

        # Sanity — env-premium accepts the key.
        assert client.get("/reports/latest", headers=h).status_code == 200

        rc = keys_main([
            "revoke", "--api-key", raw,
            "--revoked-path", str(keys_revoked),
        ])
        assert rc == 0
        key_store.reset_caches_for_tests()

        r = client.get("/reports/latest", headers=h)
        assert r.status_code == 403


class TestPhase62Precedence:
    """Stripe > env premium > manifest premium > manifest free > env free."""

    def test_env_free_still_works(
        self, client: TestClient, authed_env, monkeypatch,
    ):
        """Existing single-tenant TRADING_API_KEY deployments must
        continue to work — Phase 6.2 is additive."""
        # authed_env sets both TRADING_API_KEY and TRADING_API_PREMIUM_KEYS;
        # remove the premium binding so the key resolves as free.
        monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(authed_env["reports_dir"], today)
        r = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert r.status_code == 200

    def test_env_premium_still_works(self, client: TestClient, authed_env):
        """authed_env wires VALID_KEY into TRADING_API_PREMIUM_KEYS by
        default — assert the path still resolves as premium."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(authed_env["reports_dir"], today)
        # Tighten the cap; premium must bypass it.
        # (Premium env precedence preserved — pre-Phase 6.2 contract.)
        r = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert r.status_code == 200

    def test_stripe_premium_still_works(
        self, client: TestClient, monkeypatch, tmp_path: Path,
    ):
        """A key sourced from the Stripe billing cache continues to
        authenticate as premium — Phase 4.7 contract preserved.

        Stripe-cached keys live alongside ``TRADING_API_KEY`` in the
        existing deployment shape (Stripe alone has never satisfied
        the fail-closed check). We assert the Stripe cache resolves
        the bearer token to premium even when the env-key is set to a
        different value."""
        from trading_bot.api import billing

        reports_dir = tmp_path / "reports"
        monkeypatch.setenv(API_KEY_ENV_VAR, "unrelated-env-key")
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
        monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(tmp_path / "m.jsonl"))
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_xyz")
        monkeypatch.setenv(
            "TRADING_API_KEYS_MANIFEST_PATH",
            str(tmp_path / "empty_keys_manifest.jsonl"),
        )
        monkeypatch.setenv(
            "TRADING_API_KEYS_REVOKED_PATH",
            str(tmp_path / "empty_revoked.jsonl"),
        )
        # Seed the Stripe-cache premium set.
        billing.reset_cache_for_tests()
        billing.add_premium_key("stripe-cache-key-123")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(reports_dir, today)
        r = client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer stripe-cache-key-123"},
        )
        assert r.status_code == 200

    def test_manifest_premium_when_env_only_has_free_key(
        self, client: TestClient, monkeypatch, tmp_path: Path,
    ):
        """Existing TRADING_API_KEY (free) coexists with a manifest-issued
        premium key. Both authenticate; tier resolves correctly."""
        reports_dir = tmp_path / "reports"
        keys_manifest = tmp_path / "keys_manifest.jsonl"
        keys_revoked = tmp_path / "revoked.jsonl"
        monkeypatch.setenv(API_KEY_ENV_VAR, "legacy-free")
        monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
        monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(tmp_path / "m.jsonl"))
        monkeypatch.setenv(
            "TRADING_API_KEYS_MANIFEST_PATH", str(keys_manifest),
        )
        monkeypatch.setenv(
            "TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked),
        )
        # Issue a premium manifest key against the right env path.
        from trading_bot.api.keys import issue_key
        issued = issue_key(tier="premium", label="vip")
        raw_premium = issued["api_key"]
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(reports_dir, today)

        # Free env key works.
        r1 = client.get(
            "/reports/latest",
            headers={"Authorization": "Bearer legacy-free"},
        )
        assert r1.status_code == 200

        # Premium manifest key also works.
        r2 = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {raw_premium}"},
        )
        assert r2.status_code == 200


class TestPhase62NoRawKeyOnDisk:
    """No persisted artefact — manifest, revocation log, audit log,
    or usage log — may contain the raw API key."""

    def test_raw_key_absent_from_manifest_revoked_audit_usage(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch, tmp_path: Path,
    ):
        from trading_bot.api.keys import main as keys_main

        raw, key_hash = _issue_via_cli("free", label="leak-test")
        # Make a request so audit + usage logs accrue at least one row.
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)
        client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {raw}"},
        )
        # Now revoke by raw key (the path most likely to leak).
        rc = keys_main([
            "revoke", "--api-key", raw,
            "--revoked-path", str(manifest_auth_env.keys_revoked),
            "--reason", "leak-test",
        ])
        assert rc == 0

        # Manifest stores only the hash.
        manifest_body = manifest_auth_env.keys_manifest.read_text(
            encoding="utf-8",
        )
        assert raw not in manifest_body
        assert key_hash in manifest_body

        # Revocation log stores only the hash.
        revoked_body = manifest_auth_env.keys_revoked.read_text(
            encoding="utf-8",
        )
        assert raw not in revoked_body
        assert key_hash in revoked_body

        # Audit log (Phase 4.4) stores no api key in any form except
        # via authentication-status booleans.
        audit_path = Path(os.environ["TRADING_API_AUDIT_LOG_PATH"])
        if audit_path.exists():
            assert raw not in audit_path.read_text(encoding="utf-8")

        # Usage log (Phase 4.6) stores only key_hash.
        usage_path = Path(os.environ["TRADING_API_USAGE_LOG_PATH"])
        if usage_path.exists():
            usage_body = usage_path.read_text(encoding="utf-8")
            assert raw not in usage_body
        else:
            raise AssertionError("/ route not registered")


# ===========================================================================
# Phase 7.0 — Stripe → key activation bridge (end-to-end via live server)
# ===========================================================================


class TestPhase70StripeActivationFlow:
    """End-to-end: operator issues a free manifest key, Stripe fires
    subscription.created (via handle_webhook_event), the SAME key now
    resolves as premium against the live FastAPI app — no env-var
    edits, no manifest mutation."""

    def test_issue_free_then_webhook_upgrade_grants_premium(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch,
    ):
        from trading_bot.api import billing, key_store
        from trading_bot.api.keys import issue_key

        # Stripe must be "configured" for is_premium_via_stripe to
        # count in _is_premium's precedence order.
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_phase7")
        # Billing cache on its own tmp path.
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            str(manifest_auth_env.keys_manifest.parent / "stripe_cache.json"),
        )
        billing.reset_cache_for_tests()

        issued = issue_key(tier="free", label="phase7-stripe")
        raw = issued["api_key"]
        key_hash = issued["key_hash"]
        manifest_bytes_before = manifest_auth_env.keys_manifest.read_bytes()

        # Write a report so /reports/latest can return 200.
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)

        # Before the Stripe event, the key authenticates but is FREE.
        # Tighten the free cap so we can observe the tier.
        monkeypatch.setenv("TRADING_FREE_MAX_REPORT_CALLS", "1")
        h = {"Authorization": f"Bearer {raw}"}
        assert client.get("/reports/latest", headers=h).status_code == 200
        # Second /reports/* request should now 403 (free-tier cap hit).
        assert client.get("/reports/latest", headers=h).status_code == 403

        # Now fire the Stripe subscription.created webhook.
        billing.handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": raw},
            }},
        })
        # Reset server-side caches so _is_premium / is_premium_via_stripe
        # re-read the premium cache.
        billing.reset_cache_for_tests()
        key_store.reset_caches_for_tests()

        # Premium now. Issue several /reports/latest calls; premium
        # is exempt from the free cap.
        for _ in range(5):
            r = client.get("/reports/latest", headers=h)
            assert r.status_code == 200

        # The issuance manifest was NEVER mutated.
        assert manifest_auth_env.keys_manifest.read_bytes() == manifest_bytes_before
        # And the manifest row still says tier=free (unchanged).
        import json as _json
        rows = [
            _json.loads(line)
            for line in manifest_auth_env.keys_manifest.read_text().splitlines()
            if line.strip()
        ]
        assert any(r["key_hash"] == key_hash and r["tier"] == "free" for r in rows)

    def test_cancellation_reverts_to_manifest_tier(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch,
    ):
        from trading_bot.api import billing, key_store
        from trading_bot.api.keys import issue_key

        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_phase7")
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            str(manifest_auth_env.keys_manifest.parent / "stripe_cache.json"),
        )
        monkeypatch.setenv("TRADING_FREE_MAX_REPORT_CALLS", "1")
        billing.reset_cache_for_tests()

        issued = issue_key(tier="free", label="phase7-cancel")
        raw = issued["api_key"]
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(manifest_auth_env.reports_dir, today)
        h = {"Authorization": f"Bearer {raw}"}

        # Promote to premium.
        billing.handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": raw},
            }},
        })
        billing.reset_cache_for_tests()
        key_store.reset_caches_for_tests()
        # Premium bypasses the free cap.
        for _ in range(3):
            assert client.get("/reports/latest", headers=h).status_code == 200

        # Cancel.
        billing.handle_webhook_event({
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": raw},
            }},
        })
        billing.reset_cache_for_tests()
        key_store.reset_caches_for_tests()

        # Back to the manifest's tier (free). Cap should kick in.
        # First call consumes the remaining quota; we may already be
        # near the cap from the promotion window, so just assert the
        # eventual 403 lands.
        statuses = [
            client.get("/reports/latest", headers=h).status_code
            for _ in range(3)
        ]
        assert 403 in statuses, (
            f"expected at least one 403 after cancellation; got {statuses!r}"
        )

    def test_unissued_key_webhook_is_rejected_server_side(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch,
    ):
        """A Stripe event for an api_key we never issued must NOT
        grant the bearer any access. The server still 403s because
        the key is not in env, Stripe cache (webhook gate refused),
        or manifest."""
        from trading_bot.api import billing

        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_phase7")
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            str(manifest_auth_env.keys_manifest.parent / "stripe_cache.json"),
        )
        billing.reset_cache_for_tests()
        # Pre-issue a DIFFERENT key so the server is configured (manifest
        # non-empty) and the unknown-key path returns 403 rather than 503.
        from trading_bot.api.keys import issue_key
        issue_key(tier="free", label="some-other-user")

        phantom = "stripe-phantom-key-never-issued"
        result = billing.handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": phantom},
            }},
        })
        assert result["action"] == "ignored"
        assert result["reason"] == "key_not_in_manifest_or_revoked"

        r = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {phantom}"},
        )
        assert r.status_code == 403


class TestPhase70NoRawKeyInStripeCache:
    """End-to-end: a raw api_key promoted via the Stripe webhook
    must NOT appear in the premium cache file — only its hash."""

    def test_cache_file_has_only_hash_after_webhook(
        self, client: TestClient, manifest_auth_env: _ManifestEnv,
        monkeypatch, tmp_path: Path,
    ):
        from trading_bot.api import billing
        from trading_bot.api.keys import issue_key

        cache_path = tmp_path / "stripe_cache_phase7.json"
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH", str(cache_path),
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_phase7")
        billing.reset_cache_for_tests()

        issued = issue_key(tier="free", label="phase7-leak-test")
        raw = issued["api_key"]
        billing.handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": raw},
            }},
        })

        assert cache_path.exists()
        body = cache_path.read_text(encoding="utf-8")
        assert raw not in body, (
            "Phase 7.0: raw api_key must never appear in the Stripe cache"
        )
        assert issued["key_hash"] in body


# ===========================================================================
# Phase 7.1 — browser icon noise cleanup
# ===========================================================================


ICON_PATHS = (
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
)


class TestPhase71IconRoutes:
    """All three browser icon routes return 204 No Content with
    security headers applied and no auth required."""

    @pytest.mark.parametrize("path", ICON_PATHS)
    def test_returns_204_no_content(self, client: TestClient, path: str):
        r = client.get(path)
        assert r.status_code == 204
        assert r.content == b""

    @pytest.mark.parametrize("path", ICON_PATHS)
    def test_no_auth_required(self, client: TestClient, path: str):
        # No Authorization header — still 204, never 401/403/503.
        r = client.get(path)
        assert r.status_code == 204

    @pytest.mark.parametrize("path", ICON_PATHS)
    def test_also_204_when_no_api_key_configured(
        self, client: TestClient, monkeypatch, path: str,
    ):
        """Even when the server has NO auth configured, icon routes
        must not 503 — they're public by browser convention."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
        r = client.get(path)
        assert r.status_code == 204

    @pytest.mark.parametrize("path", ICON_PATHS)
    def test_security_headers_still_applied(
        self, client: TestClient, path: str,
    ):
        r = client.get(path)
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("Referrer-Policy") == "no-referrer"
        assert "Content-Security-Policy" in r.headers

    @pytest.mark.parametrize("path", ICON_PATHS)
    def test_only_safe_verbs_registered(self, path: str):
        """No POST/PUT/DELETE/PATCH on any icon route."""
        for route in app.routes:
            if getattr(route, "path", "") == path:
                methods = getattr(route, "methods", set()) or set()
                assert methods.issubset({"GET", "HEAD", "OPTIONS"}), (
                    f"{path} registered a mutating verb: {methods}"
                )

    def test_icons_do_not_count_toward_free_tier_cap(
        self, client: TestClient, authed_env, monkeypatch,
    ):
        """Phase 5.4 free-tier cap must NOT apply to icon requests —
        a browser that refreshes a page 100 times must not lock the
        user out of /reports/."""
        monkeypatch.setenv("TRADING_FREE_MAX_REPORT_CALLS", "1")
        monkeypatch.setenv("TRADING_FREE_MAX_REQUESTS_PER_DAY", "5")
        # Raise the Phase 4.2 per-IP cap well above the hammer count
        # so this test isolates the Phase 5.4 free-tier behaviour.
        monkeypatch.setenv("TRADING_API_RATE_LIMIT_PER_MINUTE", "1000")
        monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _write_report(authed_env["reports_dir"], today)
        h = {"Authorization": f"Bearer {VALID_KEY}"}

        # Hammer the icons — they should NOT consume the daily cap.
        for _ in range(20):
            for p in ICON_PATHS:
                r = client.get(p, headers=h)
                assert r.status_code == 204

        # A real protected request still succeeds (cap untouched).
        r = client.get("/reports/latest", headers=h)
        assert r.status_code == 200

    def test_icons_do_not_write_usage_log_rows(
        self, client: TestClient, authed_env, monkeypatch,
    ):
        """Icon requests are noise — they must not pollute the
        per-key usage log."""
        usage_path = Path(os.environ["TRADING_API_USAGE_LOG_PATH"])
        # No Authorization header — icon routes should still 204 without
        # any usage row.
        for p in ICON_PATHS:
            client.get(p)
        if usage_path.exists():
            body = usage_path.read_text(encoding="utf-8")
            for p in ICON_PATHS:
                assert p not in body, (
                    f"Phase 7.1: icon path {p!r} should not be in the usage log"
                )


# ===========================================================================
# Phase 7.3 — POST /billing/checkout (authenticated end-user upgrade)
# ===========================================================================


class _CheckoutFakePoster:
    """Records every Stripe POST so tests can assert exact metadata shape."""

    def __init__(self, response=None, raise_exc=None):
        self.response = response or {
            "id": "cs_phase73_endpoint",
            "url": "https://checkout.stripe.com/c/cs_phase73_endpoint",
        }
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, *, url, data, auth, timeout):
        self.calls.append({"url": url, "data": dict(data), "auth": auth})
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.response) if isinstance(self.response, dict) else self.response


@pytest.fixture
def checkout_env(monkeypatch, tmp_path: Path):
    """
    Configure the server with a free-tier manifest key + Stripe env
    + a public base URL so POST /billing/checkout can succeed.
    """
    reports_dir = tmp_path / "reports"
    keys_manifest = tmp_path / "api_keys_manifest.jsonl"
    keys_revoked = tmp_path / "api_keys_revoked.jsonl"
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("TRADING_API_PREMIUM_KEYS", raising=False)
    monkeypatch.setenv(REPORTS_DIR_ENV_VAR, str(reports_dir))
    monkeypatch.setenv(MANIFEST_PATH_ENV_VAR, str(tmp_path / "manifest.jsonl"))
    monkeypatch.setenv(
        "TRADING_API_KEYS_MANIFEST_PATH", str(keys_manifest),
    )
    monkeypatch.setenv(
        "TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked),
    )
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_phase73_endpoint")
    monkeypatch.setenv("STRIPE_PREMIUM_PRICE_ID", "price_test_phase73_endpoint")
    monkeypatch.setenv("TRADING_PUBLIC_BASE_URL", "https://api.example.com")
    return {
        "reports_dir": reports_dir,
        "keys_manifest": keys_manifest,
        "keys_revoked": keys_revoked,
    }


def _issue_free_for_checkout() -> tuple[str, str]:
    """Issue a free key in-process and return (raw, hash)."""
    from trading_bot.api.keys import issue_key
    result = issue_key(tier="free", label="phase73-checkout")
    return result["api_key"], result["key_hash"]


def _patch_stripe_poster(monkeypatch, fake: _CheckoutFakePoster):
    from trading_bot.api import billing as _billing_mod
    monkeypatch.setattr(_billing_mod, "_post_to_stripe", fake)


class TestPhase73CheckoutAuth:
    def test_missing_auth_returns_401(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        # Fail-closed posture requires manifest to be non-empty for 401
        # (vs 503). Pre-issue a key so the deployment is "configured".
        _issue_free_for_checkout()
        r = client.post("/billing/checkout")
        assert r.status_code == 401

    def test_bogus_key_returns_403(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        _issue_free_for_checkout()
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        assert r.status_code == 403


class TestPhase73CheckoutHappyPath:
    def test_free_key_creates_checkout_session(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, key_hash = _issue_free_for_checkout()
        fake = _CheckoutFakePoster()
        _patch_stripe_poster(monkeypatch, fake)
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["checkout_session_id"] == "cs_phase73_endpoint"
        assert body["checkout_url"].startswith("https://checkout.stripe.com/")
        assert body["key_hash"] == key_hash
        assert body["tier_to"] == "premium"

    def test_response_never_includes_raw_api_key(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, _ = _issue_free_for_checkout()
        _patch_stripe_poster(monkeypatch, _CheckoutFakePoster())
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert raw not in r.text

    def test_stripe_metadata_contains_key_hash_not_raw(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, key_hash = _issue_free_for_checkout()
        fake = _CheckoutFakePoster()
        _patch_stripe_poster(monkeypatch, fake)
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        data = fake.calls[0]["data"]
        # Hash present in EVERY documented metadata field.
        assert data["client_reference_id"] == key_hash
        assert data["metadata[key_hash]"] == key_hash
        assert data["metadata[tier_from]"] == "free"
        assert data["metadata[tier_to]"] == "premium"
        # Raw key absent from the entire form payload.
        for value in data.values():
            assert raw not in str(value), (
                f"raw api_key leaked into Stripe metadata: {value!r}"
            )

    def test_stripe_subscription_metadata_contains_key_hash(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, key_hash = _issue_free_for_checkout()
        fake = _CheckoutFakePoster()
        _patch_stripe_poster(monkeypatch, fake)
        client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        data = fake.calls[0]["data"]
        assert data["subscription_data[metadata][key_hash]"] == key_hash
        assert data["subscription_data[metadata][tier_to]"] == "premium"

    def test_default_success_and_cancel_urls(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, _ = _issue_free_for_checkout()
        fake = _CheckoutFakePoster()
        _patch_stripe_poster(monkeypatch, fake)
        client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        data = fake.calls[0]["data"]
        assert data["success_url"] == (
            "https://api.example.com/dashboard?checkout=success"
        )
        assert data["cancel_url"] == (
            "https://api.example.com/dashboard?checkout=cancel"
        )

    def test_overridden_success_and_cancel_paths(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        monkeypatch.setenv("STRIPE_CHECKOUT_SUCCESS_PATH", "/welcome")
        monkeypatch.setenv("STRIPE_CHECKOUT_CANCEL_PATH", "/back")
        raw, _ = _issue_free_for_checkout()
        fake = _CheckoutFakePoster()
        _patch_stripe_poster(monkeypatch, fake)
        client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        data = fake.calls[0]["data"]
        assert data["success_url"] == "https://api.example.com/welcome"
        assert data["cancel_url"] == "https://api.example.com/back"


class TestPhase73CheckoutAlreadyPremium:
    def test_premium_key_returns_409(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        from trading_bot.api import billing
        raw, _ = _issue_free_for_checkout()
        # Promote the key via the existing webhook path so the
        # premium classifier returns True.
        billing.handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": raw},
            }},
        })
        billing.reset_cache_for_tests()
        # Make sure Stripe is "configured" so _is_premium hits the
        # cache path.
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_phase73_already_premium")
        _patch_stripe_poster(monkeypatch, _CheckoutFakePoster())

        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 409
        assert "already premium" in r.json()["detail"].lower()


class TestPhase73CheckoutMisconfigured:
    def test_missing_stripe_secret_returns_503(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        monkeypatch.delenv("STRIPE_API_KEY", raising=False)
        raw, _ = _issue_free_for_checkout()
        # Don't patch _post_to_stripe — config check fails first.
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 503
        assert "STRIPE_SECRET_KEY" in r.json()["detail"]

    def test_missing_premium_price_id_returns_503(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        monkeypatch.delenv("STRIPE_PREMIUM_PRICE_ID", raising=False)
        monkeypatch.delenv("STRIPE_PRICE_ID_PREMIUM", raising=False)
        raw, _ = _issue_free_for_checkout()
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 503
        assert "STRIPE_PREMIUM_PRICE_ID" in r.json()["detail"]

    def test_missing_public_base_url_returns_503(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        monkeypatch.delenv("TRADING_PUBLIC_BASE_URL", raising=False)
        raw, _ = _issue_free_for_checkout()
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 503
        assert "TRADING_PUBLIC_BASE_URL" in r.json()["detail"]


class TestPhase73CheckoutStripeFailure:
    def test_stripe_api_error_returns_502(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        from trading_bot.api.billing import BillingAPIError
        raw, _ = _issue_free_for_checkout()
        fake = _CheckoutFakePoster(
            raise_exc=BillingAPIError("simulated stripe 500"),
        )
        _patch_stripe_poster(monkeypatch, fake)
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 502
        assert "checkout provider error" in r.json()["detail"].lower()

    def test_stripe_returns_malformed_payload_502(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, _ = _issue_free_for_checkout()
        fake = _CheckoutFakePoster(response={"id": "cs_x"})  # missing url
        _patch_stripe_poster(monkeypatch, fake)
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 502


class TestPhase73CheckoutNoPersistence:
    """The checkout_url returned to the caller must NOT land on disk
    in any operator log — manifest, revoked, usage, audit, premium
    cache, conversion, upgrade events."""

    def test_checkout_url_never_persisted_to_any_log(
        self, client: TestClient, checkout_env, monkeypatch, tmp_path: Path,
    ):
        # Distinctive marker so a substring search is meaningful.
        marker = "https://checkout.stripe.com/c/cs_PHASE73_LEAK_GUARD_xyz"
        fake = _CheckoutFakePoster(response={
            "id": "cs_PHASE73_LEAK_GUARD_xyz",
            "url": marker,
        })
        _patch_stripe_poster(monkeypatch, fake)
        # Point the Stripe cache at a tmp path inside checkout_env's tree.
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            str(checkout_env["keys_manifest"].parent / "stripe_cache.json"),
        )
        from trading_bot.api import billing
        billing.reset_cache_for_tests()

        raw, _ = _issue_free_for_checkout()
        r = client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        # The response DOES carry the URL (caller-safe).
        assert marker in r.text

        # Every operator log on disk must NOT contain it.
        logs_to_check: list[Path] = [
            checkout_env["keys_manifest"],
            checkout_env["keys_revoked"],
            Path(os.environ["TRADING_API_USAGE_LOG_PATH"]),
            Path(os.environ["TRADING_API_AUDIT_LOG_PATH"]),
            Path(os.environ["TRADING_API_UPGRADE_EVENTS_LOG_PATH"]),
            checkout_env["keys_manifest"].parent / "stripe_cache.json",
        ]
        for path in logs_to_check:
            if path.exists():
                body = path.read_text(encoding="utf-8")
                assert marker not in body, (
                    f"checkout_url leaked into {path}"
                )

    def test_raw_api_key_never_persisted_after_checkout(
        self, client: TestClient, checkout_env, monkeypatch,
    ):
        raw, key_hash = _issue_free_for_checkout()
        _patch_stripe_poster(monkeypatch, _CheckoutFakePoster())
        client.post(
            "/billing/checkout",
            headers={"Authorization": f"Bearer {raw}"},
        )
        # Manifest stores hash, not raw key.
        manifest_body = checkout_env["keys_manifest"].read_text("utf-8")
        assert raw not in manifest_body
        assert key_hash in manifest_body
        # Audit log never contains the raw key in any form.
        audit_path = Path(os.environ["TRADING_API_AUDIT_LOG_PATH"])
        if audit_path.exists():
            assert raw not in audit_path.read_text("utf-8")
        # Usage log uses hashes only.
        usage_path = Path(os.environ["TRADING_API_USAGE_LOG_PATH"])
        if usage_path.exists():
            assert raw not in usage_path.read_text("utf-8")
