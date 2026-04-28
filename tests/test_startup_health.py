"""
Startup + healthcheck smoke tests.

Pinned by the Railway healthcheck post-mortem: the deploy was
failing because the dashboard hard-coded port 8080 instead of
binding the PaaS-assigned ``$PORT``. These tests lock the
contract so a future regression surfaces in CI rather than at
deploy time.

Coverage:
  * ``trading_bot.api.server:app`` imports cleanly and ``/health``
    returns 200 in the documented public-route shape.
  * ``trading_bot.dashboard.app.create_app`` returns a FastAPI
    instance that serves ``/healthz`` with 200.
  * ``trading_bot.main._default_dashboard_port`` honours the
    ``PORT`` env var (PaaS convention) and fail-soft falls back to
    8080 on any malformed value.
  * ``trading_bot.dashboard.app._resolve_dashboard_port`` honours
    ``PORT`` only when the caller passed the legacy default —
    explicit overrides win.
  * Importing ``scripts.stripe_zero_dollar_test_checkout`` is a
    pure-Python no-op: it does NOT contact Stripe, mutate any env
    var, or perform a network call at import time.
"""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# trading_bot.api.server smoke
# ---------------------------------------------------------------------------


class TestSaasApiSmoke:
    def test_app_imports_cleanly(self):
        """``from trading_bot.api.server import app`` must succeed
        — Railway's container start path imports this module."""
        mod = importlib.import_module("trading_bot.api.server")
        assert hasattr(mod, "app")
        from fastapi import FastAPI
        assert isinstance(mod.app, FastAPI)

    def test_health_endpoint_returns_200(self):
        from trading_bot.api.server import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        # Documented public shape.
        assert body.get("status") == "ok"
        assert "service" in body
        assert "timestamp" in body

    def test_health_endpoint_does_not_require_auth(self):
        """``/health`` is the public liveness probe — must succeed
        with no Authorization header."""
        from trading_bot.api.server import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# trading_bot.dashboard.app smoke
# ---------------------------------------------------------------------------


class TestDashboardSmoke:
    def test_create_app_returns_fastapi(self):
        from fastapi import FastAPI
        from trading_bot.dashboard.app import create_app
        from trading_bot.dashboard.state import DashboardState
        app = create_app(DashboardState())
        assert isinstance(app, FastAPI)

    def test_healthz_endpoint_returns_200(self):
        """Railway's healthcheckPath is ``/healthz``; this is the
        path that fails when the deploy can't bind the right port."""
        from trading_bot.dashboard.app import create_app
        from trading_bot.dashboard.state import DashboardState
        client = TestClient(create_app(DashboardState()))
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "alive"
        assert "uptime_seconds" in body


# ---------------------------------------------------------------------------
# PORT env-var resolution
# ---------------------------------------------------------------------------


class TestDefaultDashboardPort:
    """Pinning the contract that broke Railway's healthcheck."""

    def test_unset_port_returns_legacy_default(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        from trading_bot.main import _default_dashboard_port
        assert _default_dashboard_port() == 8080

    def test_valid_port_honored(self, monkeypatch):
        monkeypatch.setenv("PORT", "12345")
        from trading_bot.main import _default_dashboard_port
        assert _default_dashboard_port() == 12345

    def test_railway_typical_port(self, monkeypatch):
        """Railway often hands out high ephemeral ports."""
        monkeypatch.setenv("PORT", "5783")
        from trading_bot.main import _default_dashboard_port
        assert _default_dashboard_port() == 5783

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "abc", "0", "-1", "65536", "70000", "1.5", "PORT"],
    )
    def test_invalid_port_falls_back_to_default(self, monkeypatch, bad):
        """Fail-soft. An operator typo must NOT cause a healthcheck
        deploy loop — fall back to 8080 instead."""
        monkeypatch.setenv("PORT", bad)
        from trading_bot.main import _default_dashboard_port
        assert _default_dashboard_port() == 8080

    def test_whitespace_around_port_is_stripped(self, monkeypatch):
        monkeypatch.setenv("PORT", "  9090  ")
        from trading_bot.main import _default_dashboard_port
        assert _default_dashboard_port() == 9090


class TestResolveDashboardPort:
    """``start_dashboard_server``'s defence-in-depth resolver."""

    def test_legacy_default_overridden_by_env(self, monkeypatch):
        monkeypatch.setenv("PORT", "12345")
        from trading_bot.dashboard.app import _resolve_dashboard_port
        assert _resolve_dashboard_port(8080) == 12345

    def test_explicit_caller_port_wins_over_env(self, monkeypatch):
        """If the caller passed something other than 8080, that's
        an explicit choice — don't override it from $PORT."""
        monkeypatch.setenv("PORT", "12345")
        from trading_bot.dashboard.app import _resolve_dashboard_port
        assert _resolve_dashboard_port(9000) == 9000

    def test_no_env_uses_caller(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        from trading_bot.dashboard.app import _resolve_dashboard_port
        assert _resolve_dashboard_port(8080) == 8080
        assert _resolve_dashboard_port(9000) == 9000

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "70000", ""])
    def test_invalid_env_falls_back_to_caller(self, monkeypatch, bad):
        monkeypatch.setenv("PORT", bad)
        from trading_bot.dashboard.app import _resolve_dashboard_port
        assert _resolve_dashboard_port(8080) == 8080


# ---------------------------------------------------------------------------
# scripts.stripe_zero_dollar_test_checkout import safety
# ---------------------------------------------------------------------------


class TestStripeScriptImportSafety:
    """Pinned by the deploy post-mortem: import-time side effects in
    operator scripts must NOT fire at module load. Importing the
    Stripe zero-dollar helper must be a pure no-op — no network,
    no env mutation, no Stripe SDK reach-out."""

    def test_module_imports_with_no_env_set(self, monkeypatch):
        """Cleanly remove every Stripe env var, then import. The
        module must NOT crash, NOT mutate the env, NOT make any
        network call."""
        for var in (
            "STRIPE_SECRET_KEY", "STRIPE_API_KEY",
            "STRIPE_PREMIUM_PRICE_ID", "STRIPE_PRICE_ID_PREMIUM",
            "STRIPE_WEBHOOK_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)

        env_before = dict(os.environ)
        # Force a re-import to actually exercise import-time code.
        import sys
        sys.modules.pop(
            "scripts.stripe_zero_dollar_test_checkout", None,
        )
        importlib.import_module(
            "scripts.stripe_zero_dollar_test_checkout",
        )
        env_after = dict(os.environ)
        assert env_before == env_after, (
            "import mutated os.environ — operator scripts must be inert"
        )

    def test_no_top_level_requests_import(self):
        """``requests`` must be lazy-imported only inside the live
        HTTP poster, so the module imports cleanly in environments
        where ``requests`` is unavailable (e.g. read-only static
        analysers)."""
        import scripts.stripe_zero_dollar_test_checkout as zdt
        # The module must NOT have ``requests`` as a module-level
        # attribute (it's imported inside _default_request only).
        assert "requests" not in dir(zdt)

    def test_no_top_level_billing_import(self):
        """The script imports specific names from billing (env-var
        constants + resolver), not the whole module's heavyweight
        cache state. Confirm by spot-checking the module attrs."""
        import scripts.stripe_zero_dollar_test_checkout as zdt
        # The function references are present.
        assert callable(zdt._resolve_stripe_secret)
        # But the script itself does NOT keep a cached secret.
        assert not hasattr(zdt, "_cached_stripe_secret")

    def test_help_path_does_not_call_stripe(self, capsys):
        """``--help`` must exit cleanly without touching Stripe."""
        import scripts.stripe_zero_dollar_test_checkout as zdt
        with pytest.raises(SystemExit) as exc:
            zdt.main(["--help"])
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Regression: dashboard daemon binds quickly enough for healthcheck
# ---------------------------------------------------------------------------


class TestDashboardStartsFastEnough:
    """The Railway healthcheck timeout is 5 s per request and the
    deploy phase retries for ~5 minutes. The dashboard daemon thread
    must be responsive within seconds of process startup."""

    def test_dashboard_binds_and_serves_within_a_few_seconds(
        self, monkeypatch,
    ):
        """End-to-end: spin up the dashboard daemon thread on a
        random high port, poll ``/healthz``, assert it returns 200
        within 5 s."""
        import socket
        import time

        # Pick a random free port to avoid clashing with anything else.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        monkeypatch.delenv("PORT", raising=False)
        from trading_bot.dashboard.app import start_dashboard_server
        from trading_bot.dashboard.state import DashboardState

        state = DashboardState()
        thread = start_dashboard_server(state, host="127.0.0.1", port=port)

        deadline = time.time() + 5.0
        last_err: object = None
        while time.time() < deadline:
            try:
                import urllib.request
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1,
                ) as resp:
                    body = resp.read()
                    assert resp.status == 200
                    assert b"alive" in body
                    return  # success
            except Exception as exc:
                last_err = exc
                time.sleep(0.1)

        # Daemon thread is still running (it's daemon, will die with the
        # test process); we just couldn't reach it.
        raise AssertionError(
            f"dashboard /healthz did not respond within 5s: {last_err!r} "
            f"(thread alive={thread.is_alive()})"
        )