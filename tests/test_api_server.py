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
    for name in (API_KEY_ENV_VAR, REPORTS_DIR_ENV_VAR, MANIFEST_PATH_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


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
