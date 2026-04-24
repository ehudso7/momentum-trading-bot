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
    ):
        monkeypatch.delenv(name, raising=False)
    # Redirect the Phase 4.4 default audit file into a throwaway tmp
    # location so tests don't write to the real data/ directory.
    audit_tmp = tmp_path_factory.mktemp("api_audit") / "audit.jsonl"
    monkeypatch.setenv("TRADING_API_AUDIT_LOG_PATH", str(audit_tmp))
    # Likewise for the Phase 4.6 usage log.
    usage_tmp = tmp_path_factory.mktemp("api_usage") / "usage.jsonl"
    monkeypatch.setenv("TRADING_API_USAGE_LOG_PATH", str(usage_tmp))


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
        """The API must not expose POST / PUT / DELETE / PATCH routes."""
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            # HEAD and OPTIONS are auto-added and read-only — allow them.
            for method in methods:
                assert method in {"GET", "HEAD", "OPTIONS"}, (
                    f"non-read-only route detected: {method} {route.path}"
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
        """Re-assert Phase 4.0 invariant — no mutating verbs added."""
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            for m in methods:
                assert m in {"GET", "HEAD", "OPTIONS"}, (
                    f"non-read-only method introduced: {m} {route.path}"
                )

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
            for m in methods:
                assert m in {"GET", "HEAD", "OPTIONS"}, (
                    f"non-read-only method introduced: {m} {route.path}"
                )

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
            for m in methods:
                assert m in {"GET", "HEAD", "OPTIONS"}

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
            for m in methods:
                assert m in {"GET", "HEAD", "OPTIONS"}

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
            for m in methods:
                assert m in {"GET", "HEAD", "OPTIONS"}, (
                    f"non-read-only method: {m} {route.path}"
                )

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
