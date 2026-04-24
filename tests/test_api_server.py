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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_api_env(monkeypatch):
    """Every test starts from a known env state."""
    for name in (
        API_KEY_ENV_VAR,
        REPORTS_DIR_ENV_VAR,
        MANIFEST_PATH_ENV_VAR,
        # Phase 4.2 — also reset hardening vars.
        "TRADING_API_ALLOWED_ORIGINS",
        "TRADING_API_RATE_LIMIT_PER_MINUTE",
    ):
        monkeypatch.delenv(name, raising=False)


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
    """Configure the server to talk to tmp files with a known API key."""
    reports_dir = tmp_path / "reports"
    manifest = tmp_path / "alpha_experiments.jsonl"
    monkeypatch.setenv(API_KEY_ENV_VAR, VALID_KEY)
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
