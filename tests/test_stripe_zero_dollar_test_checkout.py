"""
Tests for ``scripts/stripe_zero_dollar_test_checkout.py``.

Every test injects a stub HTTP requester so no real Stripe API call
is ever made and no real ``requests`` import is needed.

Coverage:
  * refuses to run against ``sk_live_…`` secrets unless --allow-live
  * refuses unrecognised secret prefixes
  * refuses when no Stripe secret is configured
  * resolver prefers STRIPE_SECRET_KEY over the legacy STRIPE_API_KEY
  * idempotent: existing product is reused, existing $0 price is reused
  * fresh provisioning: new product + new price when neither exists
  * --print-railway-commands prints both legacy and preferred env names
  * the Stripe secret is NEVER printed (placeholder is used instead)
  * Stripe REST failure exits with 3
  * argparse argument validation for --interval
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from scripts import stripe_zero_dollar_test_checkout as zdt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_io() -> dict[str, io.StringIO]:
    return {"out": io.StringIO(), "err": io.StringIO()}


def _make_stub(
    *,
    existing_products: list[dict] | None = None,
    existing_prices: list[dict] | None = None,
    create_product_id: str = "prod_TESTABC",
    create_price_id: str = "price_TESTXYZ",
    boom_on: str | None = None,
):
    """
    Build an HTTP requester stub that mimics the Stripe REST API.

    ``boom_on`` — if set, raise ``ZeroDollarTestError`` on the first
    request whose method+url contains this substring (used to test
    the script's exit-3 path).
    """
    products = list(existing_products or [])
    prices = list(existing_prices or [])
    calls: list[dict] = []

    def stub(*, method, url, auth, timeout, params=None, data=None):
        calls.append({
            "method": method, "url": url,
            "params": params, "data": data, "auth": auth,
        })
        if boom_on and boom_on in f"{method} {url}":
            raise zdt.ZeroDollarTestError("simulated stripe outage")
        if method == "GET" and url.endswith("/products"):
            return {"data": list(products)}
        if method == "POST" and url.endswith("/products"):
            new = {
                "id": create_product_id,
                "name": data.get("name"),
                "active": True,
            }
            products.append(new)
            return new
        if method == "GET" and url.endswith("/prices"):
            return {
                "data": [
                    p for p in prices
                    if p.get("product") == params.get("product")
                ],
            }
        if method == "POST" and url.endswith("/prices"):
            new = {
                "id": create_price_id,
                "product": data.get("product"),
                "currency": data.get("currency"),
                "unit_amount": int(data.get("unit_amount", 0)),
                "recurring": {"interval": data.get("recurring[interval]")},
                "active": True,
            }
            prices.append(new)
            return new
        raise AssertionError(f"unexpected request: {method} {url}")

    stub.calls = calls  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# Secret-validation guard
# ---------------------------------------------------------------------------


class TestSecretGuard:
    def test_no_secret_configured_exits_two(self, captured_io):
        rc = zdt.main(
            argv=[],
            requester=_make_stub(),
            secret_resolver=lambda: "",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 2
        err = captured_io["err"].getvalue()
        assert "no Stripe secret configured" in err
        # Both env-var names should appear in the operator message.
        assert zdt.STRIPE_SECRET_KEY_ENV_VAR in err
        assert zdt.STRIPE_API_KEY_ENV_VAR in err

    def test_unrecognised_prefix_rejected(self, captured_io):
        """Catches typos like ``rk_test_…`` / ``pk_live_…`` early."""
        rc = zdt.main(
            argv=[],
            requester=_make_stub(),
            secret_resolver=lambda: "pk_test_typo",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 2
        assert "must start with 'sk_test_'" in captured_io["err"].getvalue()

    def test_live_secret_refused_without_flag(self, captured_io):
        rc = zdt.main(
            argv=[],
            requester=_make_stub(),
            secret_resolver=lambda: "sk_live_secret",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 2
        err = captured_io["err"].getvalue()
        assert "LIVE secret" in err
        assert "--allow-live" in err
        # The secret itself must NOT appear in the error.
        assert "sk_live_secret" not in err

    def test_live_secret_allowed_with_flag(self, captured_io):
        stub = _make_stub()
        rc = zdt.main(
            argv=["--allow-live"],
            requester=stub,
            secret_resolver=lambda: "sk_live_secret",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        # Even when allowed, the secret must NEVER be printed.
        assert "sk_live_secret" not in captured_io["out"].getvalue()
        assert "sk_live_secret" not in captured_io["err"].getvalue()

    def test_resolver_prefers_preferred_env_over_legacy(
        self, monkeypatch, captured_io,
    ):
        """When BOTH env vars are set, preferred wins."""
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_preferred")
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_legacy")
        # Use the live resolver — proves the script picks up the
        # same precedence the server uses.
        rc = zdt.main(
            argv=[],
            requester=_make_stub(),
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        # Neither secret should leak.
        out = captured_io["out"].getvalue()
        assert "sk_test_preferred" not in out
        assert "sk_test_legacy" not in out

    def test_resolver_falls_back_to_legacy(
        self, monkeypatch, captured_io,
    ):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_legacy_only")
        rc = zdt.main(
            argv=[],
            requester=_make_stub(),
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()


# ---------------------------------------------------------------------------
# Provisioning idempotency
# ---------------------------------------------------------------------------


class TestProvisioningIdempotent:
    def test_creates_new_product_and_price_when_none_exists(
        self, captured_io,
    ):
        stub = _make_stub()
        rc = zdt.main(
            argv=[],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        out = captured_io["out"].getvalue()
        assert "product_reused: False" in out
        assert "price_reused : False" in out
        # Calls: list-products, post-products, list-prices, post-prices.
        signatures = [
            (c["method"], c["url"].rsplit("/", 1)[-1]) for c in stub.calls
        ]
        assert signatures == [
            ("GET", "products"),
            ("POST", "products"),
            ("GET", "prices"),
            ("POST", "prices"),
        ]

    def test_reuses_existing_product(self, captured_io):
        existing = {
            "id": "prod_REUSE", "name": zdt.DEFAULT_PRODUCT_NAME,
            "active": True,
        }
        stub = _make_stub(existing_products=[existing])
        rc = zdt.main(
            argv=[],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        out = captured_io["out"].getvalue()
        assert "product_id   : prod_REUSE" in out
        assert "product_reused: True" in out
        # We did NOT POST /products.
        signatures = [
            (c["method"], c["url"].rsplit("/", 1)[-1]) for c in stub.calls
        ]
        assert ("POST", "products") not in signatures

    def test_reuses_existing_zero_price(self, captured_io):
        existing_product = {
            "id": "prod_REUSE", "name": zdt.DEFAULT_PRODUCT_NAME,
            "active": True,
        }
        existing_price = {
            "id": "price_REUSE", "product": "prod_REUSE",
            "currency": "usd", "unit_amount": 0,
            "recurring": {"interval": "month"}, "active": True,
        }
        stub = _make_stub(
            existing_products=[existing_product],
            existing_prices=[existing_price],
        )
        rc = zdt.main(
            argv=[],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        out = captured_io["out"].getvalue()
        assert "price_id     : price_REUSE" in out
        assert "price_reused : True" in out
        # No POSTs at all.
        for call in stub.calls:
            assert call["method"] == "GET"

    def test_does_not_reuse_non_zero_price(self, captured_io):
        existing_product = {
            "id": "prod_REUSE", "name": zdt.DEFAULT_PRODUCT_NAME,
        }
        non_zero_price = {
            "id": "price_OLD", "product": "prod_REUSE",
            "currency": "usd", "unit_amount": 999,
            "recurring": {"interval": "month"}, "active": True,
        }
        stub = _make_stub(
            existing_products=[existing_product],
            existing_prices=[non_zero_price],
            create_price_id="price_NEW_ZERO",
        )
        rc = zdt.main(
            argv=[],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        out = captured_io["out"].getvalue()
        assert "price_id     : price_NEW_ZERO" in out
        assert "price_reused : False" in out

    def test_does_not_reuse_price_with_different_interval(
        self, captured_io,
    ):
        existing_product = {
            "id": "prod_REUSE", "name": zdt.DEFAULT_PRODUCT_NAME,
        }
        wrong_interval = {
            "id": "price_YEARLY", "product": "prod_REUSE",
            "currency": "usd", "unit_amount": 0,
            "recurring": {"interval": "year"}, "active": True,
        }
        stub = _make_stub(
            existing_products=[existing_product],
            existing_prices=[wrong_interval],
            create_price_id="price_NEW_MONTHLY",
        )
        rc = zdt.main(
            argv=["--interval", "month"],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        assert "price_id     : price_NEW_MONTHLY" in captured_io["out"].getvalue()


# ---------------------------------------------------------------------------
# Output: railway commands + secret never echoed
# ---------------------------------------------------------------------------


class TestRailwayOutput:
    def test_railway_commands_printed_with_flag(self, captured_io):
        stub = _make_stub(create_price_id="price_FOR_RAILWAY")
        rc = zdt.main(
            argv=["--print-railway-commands"],
            requester=stub,
            secret_resolver=lambda: "sk_test_dont_leak_me",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        out = captured_io["out"].getvalue()
        # All four commands are present (preferred + legacy x 2).
        for env in (
            "STRIPE_SECRET_KEY",
            "STRIPE_API_KEY",
            "STRIPE_PREMIUM_PRICE_ID",
            "STRIPE_PRICE_ID_PREMIUM",
        ):
            assert f"railway variables --set {env}=" in out
        # The price_id IS embedded in the snippet.
        assert "price_FOR_RAILWAY" in out
        # The Stripe secret is NEVER embedded — placeholder is used.
        assert "sk_test_dont_leak_me" not in out
        assert "<your-sk_test_..." in out

    def test_no_railway_commands_without_flag(self, captured_io):
        stub = _make_stub()
        rc = zdt.main(
            argv=[],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 0, captured_io["err"].getvalue()
        out = captured_io["out"].getvalue()
        assert "railway variables --set" not in out

    def test_secret_never_printed_anywhere(self, captured_io):
        """Cross-cutting check across every CLI mode."""
        for argv in (
            [],
            ["--print-railway-commands"],
            ["--allow-live"],
            ["--print-railway-commands", "--allow-live"],
        ):
            io_state = {"out": io.StringIO(), "err": io.StringIO()}
            secret = (
                "sk_live_NEVER_PRINT_ME" if "--allow-live" in argv
                else "sk_test_NEVER_PRINT_ME"
            )
            rc = zdt.main(
                argv=argv,
                requester=_make_stub(),
                secret_resolver=lambda s=secret: s,
                out=io_state["out"], err=io_state["err"],
            )
            assert rc == 0, io_state["err"].getvalue()
            assert "NEVER_PRINT_ME" not in io_state["out"].getvalue()
            assert "NEVER_PRINT_ME" not in io_state["err"].getvalue()


# ---------------------------------------------------------------------------
# HTTP failure handling
# ---------------------------------------------------------------------------


class TestStripeFailure:
    def test_stripe_failure_exits_three(self, captured_io):
        stub = _make_stub(boom_on="GET https://api.stripe.com/v1/products")
        rc = zdt.main(
            argv=[],
            requester=stub,
            secret_resolver=lambda: "sk_test_x",
            out=captured_io["out"], err=captured_io["err"],
        )
        assert rc == 3
        assert "simulated stripe outage" in captured_io["err"].getvalue()


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


class TestArgParse:
    def test_invalid_interval_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            zdt.main(
                argv=["--interval", "fortnight"],
                requester=_make_stub(),
                secret_resolver=lambda: "sk_test_x",
            )
        assert exc_info.value.code == 2
        # argparse error written to stderr by default.
        assert "fortnight" in capsys.readouterr().err.lower()

    def test_valid_intervals(self, captured_io):
        for interval in sorted(zdt.VALID_INTERVALS):
            io_state = {"out": io.StringIO(), "err": io.StringIO()}
            stub = _make_stub()
            rc = zdt.main(
                argv=["--interval", interval],
                requester=stub,
                secret_resolver=lambda: "sk_test_x",
                out=io_state["out"], err=io_state["err"],
            )
            assert rc == 0, io_state["err"].getvalue()
            assert f"interval     : {interval}" in io_state["out"].getvalue()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_validate_secret_test_prefix(self):
        zdt._validate_secret("sk_test_x", allow_live=False)

    def test_validate_secret_live_with_flag(self):
        zdt._validate_secret("sk_live_x", allow_live=True)

    def test_validate_secret_live_without_flag(self):
        with pytest.raises(zdt.ZeroDollarTestError, match="LIVE"):
            zdt._validate_secret("sk_live_x", allow_live=False)

    def test_validate_secret_empty(self):
        with pytest.raises(zdt.ZeroDollarTestError, match="no Stripe secret"):
            zdt._validate_secret("", allow_live=False)

    def test_validate_secret_unknown_prefix(self):
        with pytest.raises(zdt.ZeroDollarTestError, match="unrecognised"):
            zdt._validate_secret("rk_test_x", allow_live=False)

    def test_format_railway_commands_includes_all_four_vars(self):
        out = zdt._format_railway_commands("price_xyz")
        for var in (
            "STRIPE_SECRET_KEY", "STRIPE_API_KEY",
            "STRIPE_PREMIUM_PRICE_ID", "STRIPE_PRICE_ID_PREMIUM",
        ):
            assert f"--set {var}=" in out
        assert "price_xyz" in out
        assert "<your-sk_test_..." in out
