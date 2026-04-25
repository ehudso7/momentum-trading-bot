"""
Phase 7.2 tests — production launch lockdown verifier.

Drives ``trading_bot.api.launch_check`` against fabricated env
dictionaries and an injected smoke runner, so no real network IO
ever happens. Covers:

  * Every required env var must be set; missing ones surface as FAIL.
  * /tmp paths are rejected unless --allow-tmp is passed.
  * Repo-local data/ paths are rejected when RAILWAY_ENVIRONMENT is
    set (production posture).
  * Writability is probed via a tmp file in the parent directory.
  * --smoke wraps the Phase 6.5 smoke runner; raw api_key never
    appears in any output.
  * JSON renderer is parseable.
  * Exit codes are 0/1/2 as documented.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import pytest

from trading_bot.api import launch_check
from trading_bot.api.launch_check import (
    RAILWAY_ENV_VAR,
    REQUIRED_ENV_VARS,
    CheckResult,
    main as launch_main,
    run_env_checks,
)


def _full_env(tmp_path: Path) -> dict[str, str]:
    """Return a dict mapping every required env var to a writable
    tmp-path (NOT under /tmp)."""
    base = tmp_path / "data"
    base.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for env_var, _, _ in REQUIRED_ENV_VARS:
        suffix = env_var.lower().replace("trading_", "").replace("_path", "")
        out[env_var] = str(base / f"{suffix}.jsonl")
    return out


# ---------------------------------------------------------------------------
# run_env_checks
# ---------------------------------------------------------------------------


class TestRunEnvChecksAllPass:
    def test_all_required_vars_set_and_writable(self, tmp_path: Path):
        env = _full_env(tmp_path)
        # tmp_path lives under /tmp during pytest runs — opt out of the
        # production "no /tmp" gate so this baseline test can pass.
        results = run_env_checks(allow_tmp=True, env=env)
        assert all(r.passed for r in results), [
            (r.env_var, r.detail) for r in results if not r.passed
        ]
        assert {r.env_var for r in results} == {
            n for n, _, _ in REQUIRED_ENV_VARS
        }


class TestRunEnvChecksFailures:
    def test_missing_var_fails(self, tmp_path: Path):
        env = _full_env(tmp_path)
        del env["TRADING_API_KEYS_MANIFEST_PATH"]
        results = run_env_checks(allow_tmp=True, env=env)
        bad = [r for r in results if not r.passed]
        assert len(bad) == 1
        assert bad[0].env_var == "TRADING_API_KEYS_MANIFEST_PATH"
        assert "unset" in bad[0].detail

    def test_blank_value_fails(self, tmp_path: Path):
        env = _full_env(tmp_path)
        env["TRADING_API_AUDIT_LOG_PATH"] = "   "
        results = run_env_checks(allow_tmp=True, env=env)
        bad = [
            r for r in results
            if r.env_var == "TRADING_API_AUDIT_LOG_PATH"
        ][0]
        assert not bad.passed
        assert "unset" in bad.detail

    def test_writable_parent_creates_missing_dir(self, tmp_path: Path):
        env = _full_env(tmp_path)
        # Point one var at a path whose parent doesn't exist yet.
        env["TRADING_API_GROWTH_LOG_PATH"] = str(
            tmp_path / "newdir" / "growth.jsonl",
        )
        results = run_env_checks(allow_tmp=True, env=env)
        good = [
            r for r in results
            if r.env_var == "TRADING_API_GROWTH_LOG_PATH"
        ][0]
        assert good.passed
        assert (tmp_path / "newdir").exists()

    def test_unwritable_parent_fails(self, tmp_path: Path):
        env = _full_env(tmp_path)
        # /proc/sys is read-only on Linux — a deterministic
        # un-writable parent for this test.
        env["TRADING_API_GROWTH_LOG_PATH"] = "/proc/sys/launch_check_test.jsonl"
        results = run_env_checks(allow_tmp=True, env=env)
        bad = [
            r for r in results
            if r.env_var == "TRADING_API_GROWTH_LOG_PATH"
        ][0]
        assert not bad.passed


class TestTmpRejection:
    def test_tmp_path_rejected_by_default(self, tmp_path: Path):
        env = _full_env(tmp_path)
        env["TRADING_API_USAGE_LOG_PATH"] = "/tmp/api_usage_phase72.jsonl"
        results = run_env_checks(env=env)
        bad = [
            r for r in results
            if r.env_var == "TRADING_API_USAGE_LOG_PATH"
        ][0]
        assert not bad.passed
        assert "/tmp" in bad.detail
        assert "--allow-tmp" in bad.detail

    def test_tmp_path_allowed_with_flag(self, tmp_path: Path):
        env = _full_env(tmp_path)
        # And a /tmp one — should pass with allow_tmp.
        env["TRADING_API_AUDIT_LOG_PATH"] = "/tmp/audit_phase72_test.jsonl"
        results = run_env_checks(allow_tmp=True, env=env)
        good = [
            r for r in results
            if r.env_var == "TRADING_API_AUDIT_LOG_PATH"
        ][0]
        assert good.passed


class TestRailwayRejectsRepoLocal:
    def test_repo_local_path_rejected_when_railway_set(
        self, tmp_path: Path,
    ):
        env = _full_env(tmp_path)
        env["TRADING_API_KEYS_MANIFEST_PATH"] = "data/api_keys_manifest.jsonl"
        results = run_env_checks(allow_tmp=True, in_railway=True, env=env)
        bad = [
            r for r in results
            if r.env_var == "TRADING_API_KEYS_MANIFEST_PATH"
        ][0]
        assert not bad.passed
        assert "repo-local" in bad.detail.lower() or "data/" in bad.detail

    def test_repo_local_path_allowed_when_not_railway(
        self, tmp_path: Path,
    ):
        env = _full_env(tmp_path)
        # Local dev: relative path is fine because RAILWAY_ENVIRONMENT
        # is not set in env.
        env["TRADING_API_KEYS_MANIFEST_PATH"] = str(
            tmp_path / "local_manifest.jsonl",
        )
        results = run_env_checks(allow_tmp=True, in_railway=False, env=env)
        good = [
            r for r in results
            if r.env_var == "TRADING_API_KEYS_MANIFEST_PATH"
        ][0]
        assert good.passed

    def test_railway_env_var_auto_detected(self, tmp_path: Path):
        env = _full_env(tmp_path)
        env[RAILWAY_ENV_VAR] = "production"
        env["TRADING_API_USAGE_LOG_PATH"] = "data/usage.jsonl"
        results = run_env_checks(allow_tmp=True, env=env)
        bad = [
            r for r in results
            if r.env_var == "TRADING_API_USAGE_LOG_PATH"
        ][0]
        assert not bad.passed

    def test_absolute_app_data_path_passes_under_railway(
        self, tmp_path: Path,
    ):
        env = _full_env(tmp_path)
        env[RAILWAY_ENV_VAR] = "production"
        # The recommended Railway shape: /app/data/...
        # Use tmp_path as a stand-in (absolute, not repo-local data/).
        target = tmp_path / "app_data" / "manifest.jsonl"
        env["TRADING_API_KEYS_MANIFEST_PATH"] = str(target)
        results = run_env_checks(allow_tmp=True, env=env)
        good = [
            r for r in results
            if r.env_var == "TRADING_API_KEYS_MANIFEST_PATH"
        ][0]
        assert good.passed


# ---------------------------------------------------------------------------
# CLI exit codes + JSON
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    def test_all_pass_exits_zero(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        env = _full_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        # tmp_path is under /tmp during pytest — pass --allow-tmp.
        rc = launch_main(["--allow-tmp"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "READY" in out

    def test_missing_env_exits_one(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        # Wipe the env entirely.
        for env_var, _, _ in REQUIRED_ENV_VARS:
            monkeypatch.delenv(env_var, raising=False)
        rc = launch_main([])
        out = capsys.readouterr().out
        assert rc == 1
        assert "NOT READY" in out

    def test_smoke_without_base_url_exits_two(self, capsys):
        rc = launch_main(["--smoke", "--api-key", "k"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--base-url" in err

    def test_smoke_without_api_key_exits_two(self, capsys):
        rc = launch_main(
            ["--smoke", "--base-url", "https://example.com"],
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "--api-key" in err


class TestJsonOutput:
    def test_json_envchecks_only(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        env = _full_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        rc = launch_main(["--json", "--allow-tmp"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ready"] is True
        assert payload["env_passed"] is True
        assert "smoke_checks" not in payload
        assert len(payload["env_checks"]) == len(REQUIRED_ENV_VARS)


# ---------------------------------------------------------------------------
# --smoke wrapper
# ---------------------------------------------------------------------------


class _FakeSmokeResult:
    """Mimic a CheckResult from trading_bot.api.smoke without
    importing it directly."""

    def __init__(
        self, name, method, path, expected, actual, passed, error=None,
    ):
        self.name = name
        self.method = method
        self.path = path
        self.expected = expected
        self.actual = actual
        self.passed = passed
        self.error = error


def _all_pass_smoke(*, base_url, api_key, timeout):
    return [
        _FakeSmokeResult(
            "public landing", "GET", "/", (200,), 200, True,
        ),
        _FakeSmokeResult(
            "health", "GET", "/health", (200,), 200, True,
        ),
        _FakeSmokeResult(
            "401 no key", "GET", "/reports/latest", (401,), 401, True,
        ),
        _FakeSmokeResult(
            "403/503 bogus", "GET", "/reports/latest", (403, 503), 403, True,
        ),
        _FakeSmokeResult(
            "200/404 auth", "GET", "/reports/latest", (200, 404), 200, True,
        ),
        _FakeSmokeResult(
            "dashboard", "GET", "/dashboard", (200,), 200, True,
        ),
    ]


def _one_fail_smoke(*, base_url, api_key, timeout):
    out = _all_pass_smoke(base_url=base_url, api_key=api_key, timeout=timeout)
    out[-1] = _FakeSmokeResult(
        "dashboard", "GET", "/dashboard", (200,), 500, False,
    )
    return out


class TestSmokeWrapper:
    def test_combined_pass(self, tmp_path: Path, monkeypatch, capsys):
        env = _full_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        rc = launch_main(
            [
                "--smoke", "--allow-tmp",
                "--base-url", "https://example.com",
                "--api-key", "TEST_KEY_123",
            ],
            smoke_runner=_all_pass_smoke,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "READY" in out
        assert "Smoke checks against https://example.com" in out
        # The raw key must NOT appear; only the hash.
        assert "TEST_KEY_123" not in out
        expected_hash = hashlib.sha256(b"TEST_KEY_123").hexdigest()[:32]
        assert expected_hash in out

    def test_combined_fail_exits_one(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        env = _full_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        rc = launch_main(
            [
                "--smoke", "--allow-tmp",
                "--base-url", "https://example.com",
                "--api-key", "TEST_KEY_456",
            ],
            smoke_runner=_one_fail_smoke,
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "NOT READY" in out
        # The fail mark + the failing path appear.
        assert "[FAIL]" in out
        assert "/dashboard" in out

    def test_env_fail_skips_ready_even_if_smoke_passes(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        # Wipe one env var so env check fails.
        env = _full_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("TRADING_API_KEYS_MANIFEST_PATH", raising=False)
        rc = launch_main(
            [
                "--smoke", "--allow-tmp",
                "--base-url", "https://example.com",
                "--api-key", "k",
            ],
            smoke_runner=_all_pass_smoke,
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "NOT READY" in out

    def test_smoke_json_output_omits_raw_key(
        self, tmp_path: Path, monkeypatch, capsys,
    ):
        env = _full_env(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        rc = launch_main(
            [
                "--smoke", "--json", "--allow-tmp",
                "--base-url", "https://example.com",
                "--api-key", "RAW_KEY_DO_NOT_LEAK_999",
            ],
            smoke_runner=_all_pass_smoke,
        )
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out)
        assert payload["ready"] is True
        assert payload["smoke_passed"] is True
        assert payload["base_url"] == "https://example.com"
        assert payload["key_hash"] == hashlib.sha256(
            b"RAW_KEY_DO_NOT_LEAK_999",
        ).hexdigest()[:32]
        # And the raw key is nowhere in the JSON output.
        assert "RAW_KEY_DO_NOT_LEAK_999" not in out


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_module_does_not_import_fastapi_or_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "launch_check.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.api.server",
            "import structlog",
            "import fastapi",
            "from fastapi",
        ):
            assert forbidden not in src, (
                f"launch_check.py imports restricted surface: {forbidden!r}"
            )

    def test_required_env_var_set_matches_documented_list(self):
        names = {n for n, _, _ in REQUIRED_ENV_VARS}
        # Exact list — guards against silent additions / removals.
        assert names == {
            "TRADING_API_KEYS_MANIFEST_PATH",
            "TRADING_API_KEYS_REVOKED_PATH",
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            "TRADING_API_USAGE_LOG_PATH",
            "TRADING_API_AUDIT_LOG_PATH",
            "TRADING_API_GROWTH_LOG_PATH",
            "TRADING_API_CONVERSION_LOG_PATH",
            "TRADING_API_UPGRADE_EVENTS_LOG_PATH",
        }


# ---------------------------------------------------------------------------
# railway.toml lockdown
# ---------------------------------------------------------------------------


class TestRailwayTomlLockdown:
    def test_railway_toml_has_production_start_command(self):
        toml = (
            Path(__file__).resolve().parent.parent / "railway.toml"
        ).read_text()
        # Must contain the documented production start command.
        assert (
            "uvicorn trading_bot.api.server:app "
            "--host 0.0.0.0 --port 8080"
        ) in toml
        # Must include the /app/data chmod step.
        assert "chmod -R 777 /app/data" in toml
        # Must use the dockerfile builder.
        assert 'builder = "DOCKERFILE"' in toml
        # Health probe pointed at /health.
        assert 'healthcheckPath = "/health"' in toml

    def test_railway_toml_does_not_invoke_key_issuance_at_boot(self):
        toml = (
            Path(__file__).resolve().parent.parent / "railway.toml"
        ).read_text()
        # Must NOT auto-issue keys at boot — issuance is operator-only.
        for forbidden in (
            "trading_bot.api.keys",
            "issue",
            "revoke",
            "list",
        ):
            # `issue`/`list`/`revoke` could in theory show up in
            # comments; we only forbid them inside startCommand. Cheap
            # check: they should not appear at all in this minimal
            # config.
            assert forbidden not in toml, (
                f"railway.toml must not invoke {forbidden!r} at boot"
            )


class TestNoNewPublicEndpoints:
    """The whole point of Phase 7.2 is lockdown — there must be no
    new public endpoints, and the only mutating route remains
    POST /webhook/stripe."""

    def test_only_mutating_route_is_stripe_webhook(self):
        from trading_bot.api.server import app
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            mutating = methods - {"GET", "HEAD", "OPTIONS"}
            if not mutating:
                continue
            path = getattr(route, "path", "") or ""
            # FastAPI auto-mounts an /openapi.json + /docs surface that
            # may include implicit OPTIONS — only flag real mutators.
            for verb in mutating:
                assert path == "/webhook/stripe" and verb == "POST", (
                    f"Phase 7.2: leaked mutating route: {verb} {path}"
                )
