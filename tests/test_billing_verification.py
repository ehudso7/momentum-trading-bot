"""
Tests for scripts.billing_verification.

Covers:
  * never prints raw secret values
  * detects mixed test/live env as a hard FAIL
  * confirms presence of required env vars without echoing them
  * --strict returns nonzero on any FAIL
  * default behaviour returns 0 even when checks fail (informational)
"""

from __future__ import annotations

import json

import pytest

import scripts.billing_verification as bv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "STRIPE_SECRET_KEY", "STRIPE_API_KEY",
        "STRIPE_PREMIUM_PRICE_ID", "STRIPE_PRICE_ID_PREMIUM",
        "STRIPE_WEBHOOK_SECRET", "TRADING_PUBLIC_BASE_URL",
        "TRADING_API_KEYS_MANIFEST_PATH",
        "TRADING_API_KEYS_REVOKED_PATH",
        "TRADING_STRIPE_PREMIUM_CACHE_PATH",
        "TRADING_STRIPE_WEBHOOK_EVENTS_PATH",
        "TRADING_SAAS_REPORTS_DIR",
        "TRADING_SAAS_DATA_MODE",
        "TRADING_RUN_MODE",
        "POLYGON_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_test_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_THIS_IS_THE_SECRET_VALUE_DO_NOT_LEAK")
    monkeypatch.setenv("STRIPE_PREMIUM_PRICE_ID", "price_test_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_THIS_IS_WHSEC_VALUE_DO_NOT_LEAK")
    monkeypatch.setenv("TRADING_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("TRADING_STRIPE_PREMIUM_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv(
        "TRADING_STRIPE_WEBHOOK_EVENTS_PATH",
        str(tmp_path / "events.jsonl"),
    )


def _set_live_env(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_LIVE_SECRET_VALUE_DO_NOT_LEAK")
    monkeypatch.setenv("STRIPE_PREMIUM_PRICE_ID", "price_live_abc")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_live_value_DO_NOT_LEAK")
    monkeypatch.setenv("TRADING_PUBLIC_BASE_URL", "https://example.live")
    monkeypatch.setenv("TRADING_STRIPE_PREMIUM_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv(
        "TRADING_STRIPE_WEBHOOK_EVENTS_PATH",
        str(tmp_path / "events.jsonl"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNeverPrintsSecrets:
    def test_text_output_excludes_raw_secret(self, monkeypatch, tmp_path, capsys):
        _set_test_env(monkeypatch, tmp_path)
        bv.main([])
        captured = capsys.readouterr().out
        assert "sk_test_THIS_IS_THE_SECRET_VALUE_DO_NOT_LEAK" not in captured
        assert "whsec_THIS_IS_WHSEC_VALUE_DO_NOT_LEAK" not in captured
        # Sanity: env names ARE printed (they are not secrets).
        assert "STRIPE_SECRET_KEY" not in captured  # we report family labels
        # Mode must show as test.
        assert "stripe_secret_mode: test" in captured

    def test_json_output_excludes_raw_secret(self, monkeypatch, tmp_path, capsys):
        _set_test_env(monkeypatch, tmp_path)
        bv.main(["--json"])
        captured = capsys.readouterr().out
        assert "sk_test_THIS_IS_THE_SECRET_VALUE_DO_NOT_LEAK" not in captured
        # Must be valid JSON.
        parsed = json.loads(captured)
        assert "checks" in parsed


class TestRequiredEnv:
    def test_passes_when_test_env_complete(self, monkeypatch, tmp_path):
        _set_test_env(monkeypatch, tmp_path)
        results = bv.run_checks()
        required = next(r for r in results if r["name"] == "required_env")
        assert required["status"] == "PASS"
        assert not bv.has_failures(results)

    def test_fails_when_required_missing(self, monkeypatch, tmp_path):
        # Don't set anything.
        results = bv.run_checks()
        required = next(r for r in results if r["name"] == "required_env")
        assert required["status"] == "FAIL"
        assert "stripe_secret" in required["missing"]
        assert "stripe_webhook_secret" in required["missing"]
        assert "public_base_url" in required["missing"]


class TestModeConsistency:
    def test_all_test_passes(self, monkeypatch, tmp_path):
        _set_test_env(monkeypatch, tmp_path)
        results = bv.run_checks()
        consistency = next(
            r for r in results if r["name"] == "stripe_mode_consistency"
        )
        assert consistency["status"] == "PASS"

    def test_all_live_passes(self, monkeypatch, tmp_path):
        _set_live_env(monkeypatch, tmp_path)
        results = bv.run_checks()
        consistency = next(
            r for r in results if r["name"] == "stripe_mode_consistency"
        )
        assert consistency["status"] == "PASS"

    def test_mixed_test_and_live_fails(self, monkeypatch, tmp_path):
        _set_test_env(monkeypatch, tmp_path)
        # Override the legacy var with a LIVE secret to simulate the
        # operator forgetting to remove the test key after switching.
        monkeypatch.setenv("STRIPE_API_KEY", "sk_live_LEFTOVER_VALUE_DO_NOT_LEAK")
        results = bv.run_checks()
        consistency = next(
            r for r in results if r["name"] == "stripe_mode_consistency"
        )
        assert consistency["status"] == "FAIL"
        assert consistency["reason"] == "mixed_test_and_live_keys"


class TestStrictExit:
    def test_strict_returns_one_on_failure(self, monkeypatch, tmp_path):
        # Empty env → required_env fails.
        rc = bv.main(["--strict"])
        assert rc == 1

    def test_strict_returns_zero_on_pass(self, monkeypatch, tmp_path):
        _set_test_env(monkeypatch, tmp_path)
        rc = bv.main(["--strict"])
        assert rc == 0

    def test_default_returns_zero_even_on_failure(self, monkeypatch, tmp_path):
        rc = bv.main([])
        assert rc == 0


class TestPathChecks:
    def test_premium_cache_path_writable(self, monkeypatch, tmp_path):
        _set_test_env(monkeypatch, tmp_path)
        results = bv.run_checks()
        cache = next(r for r in results if r["name"] == "premium_cache_path")
        assert cache["status"] == "PASS"

    def test_webhook_events_path_writable(self, monkeypatch, tmp_path):
        _set_test_env(monkeypatch, tmp_path)
        results = bv.run_checks()
        evt = next(r for r in results if r["name"] == "webhook_events_path")
        assert evt["status"] == "PASS"
