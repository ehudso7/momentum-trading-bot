"""
Phase 4.7 tests — Stripe billing integration (trading_bot.api.billing).

Covers the pure billing module: Stripe-style webhook signature
verification, api-key extraction from event payloads, the persistent
premium-key cache, and event dispatch for the documented lifecycle
events. Integration with the live FastAPI app is covered separately
in ``tests/test_api_server.py``.

Security posture:
  * The billing module is explicitly isolated from Core — tests
    re-assert that no Core module is imported, and that no raw
    card data, PAN, CVV, email, or customer name is ever persisted.
  * The webhook signature scheme is implemented with stdlib only,
    so there is no optional pip dependency to worry about.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from trading_bot.api import billing
from trading_bot.api.billing import (
    DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
    DEFAULT_STRIPE_PREMIUM_CACHE_PATH,
    STRIPE_API_KEY_ENV_VAR,
    STRIPE_PREMIUM_CACHE_ENV_VAR,
    STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
    STRIPE_WEBHOOK_SECRET_ENV_VAR,
    _extract_api_key_from_event_object,
    add_premium_key,
    current_premium_keys,
    handle_webhook_event,
    is_premium_via_stripe,
    is_stripe_configured,
    remove_premium_key,
    reset_cache_for_tests,
    verify_webhook_signature,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_stripe_env(monkeypatch, tmp_path: Path):
    """
    Clear all Stripe env vars + point the cache at a tmp file so
    tests never touch the real data/ directory.
    """
    for name in (
        STRIPE_API_KEY_ENV_VAR,
        STRIPE_WEBHOOK_SECRET_ENV_VAR,
        STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    cache_file = tmp_path / "stripe_premium.json"
    monkeypatch.setenv(STRIPE_PREMIUM_CACHE_ENV_VAR, str(cache_file))
    reset_cache_for_tests()
    yield cache_file
    reset_cache_for_tests()


@pytest.fixture
def cache_file(clean_stripe_env) -> Path:
    """Alias for the tmp cache path configured by clean_stripe_env."""
    return clean_stripe_env


def _sign(payload: bytes, secret: str, *, ts: Optional[int] = None) -> str:
    """Helper — build a valid Stripe-Signature header for ``payload``."""
    timestamp = int(ts if ts is not None else time.time())
    signed = f"{timestamp}.".encode("utf-8") + payload
    sig = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={sig}"


# ---------------------------------------------------------------------------
# is_stripe_configured
# ---------------------------------------------------------------------------


class TestIsStripeConfigured:
    def test_returns_false_when_env_unset(self):
        assert is_stripe_configured() is False

    def test_returns_true_when_api_key_set(self, monkeypatch):
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_abc")
        assert is_stripe_configured() is True

    def test_returns_false_when_api_key_empty_string(self, monkeypatch):
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "")
        assert is_stripe_configured() is False

    def test_returns_false_when_api_key_whitespace_only(self, monkeypatch):
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "    ")
        assert is_stripe_configured() is False


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestVerifyWebhookSignature:
    SECRET = "whsec_test_secret_do_not_use_in_prod"

    def test_valid_signature_passes(self):
        body = b'{"type": "customer.subscription.created"}'
        header = _sign(body, self.SECRET)
        assert verify_webhook_signature(body, header, self.SECRET) is True

    def test_tampered_body_fails(self):
        body = b'{"type": "customer.subscription.created"}'
        header = _sign(body, self.SECRET)
        tampered = body + b" "
        assert verify_webhook_signature(tampered, header, self.SECRET) is False

    def test_wrong_secret_fails(self):
        body = b'{"type": "X"}'
        header = _sign(body, self.SECRET)
        assert verify_webhook_signature(body, header, "wrong") is False

    def test_missing_header_fails(self):
        assert verify_webhook_signature(b"{}", None, self.SECRET) is False
        assert verify_webhook_signature(b"{}", "", self.SECRET) is False

    def test_missing_secret_fails(self):
        header = _sign(b"{}", self.SECRET)
        assert verify_webhook_signature(b"{}", header, None) is False
        assert verify_webhook_signature(b"{}", header, "") is False

    def test_stale_timestamp_rejected(self):
        body = b'{"type": "X"}'
        # Signed with a 10-minute-old timestamp.
        old = int(time.time()) - 600
        header = _sign(body, self.SECRET, ts=old)
        assert verify_webhook_signature(body, header, self.SECRET) is False

    def test_future_timestamp_beyond_tolerance_rejected(self):
        body = b'{"type": "X"}'
        future = int(time.time()) + 600
        header = _sign(body, self.SECRET, ts=future)
        assert verify_webhook_signature(body, header, self.SECRET) is False

    def test_custom_tolerance_accepts_old_timestamp(self):
        """Callers can opt into a wider window for testing."""
        body = b"{}"
        old = int(time.time()) - 1200
        header = _sign(body, self.SECRET, ts=old)
        assert verify_webhook_signature(
            body, header, self.SECRET, tolerance=10_000
        ) is True

    @pytest.mark.parametrize(
        "header",
        [
            "t=",
            ",v1=abc",
            "foo=bar",
            "t=notanumber,v1=abc",
            "v1=only",
            "t=100",
            "",
        ],
    )
    def test_malformed_header_fails(self, header):
        assert (
            verify_webhook_signature(b"{}", header, self.SECRET) is False
        )

    def test_multiple_v1_values_any_match(self):
        body = b'{"x": 1}'
        ts = int(time.time())
        signed = f"{ts}.".encode("utf-8") + body
        good = hmac.new(
            self.SECRET.encode(), signed, hashlib.sha256
        ).hexdigest()
        header = f"t={ts},v1={'deadbeef' * 8},v1={good}"
        assert verify_webhook_signature(body, header, self.SECRET) is True

    def test_never_raises_on_garbage(self):
        """Any weird input must return False, never raise."""
        assert verify_webhook_signature(None, None, None) is False
        assert verify_webhook_signature(b"", "t=NaN,v1=X", "s") is False
        assert verify_webhook_signature(b"\x00\x01", "t=1,v1=" * 500, "s") is False


# ---------------------------------------------------------------------------
# _extract_api_key_from_event_object
# ---------------------------------------------------------------------------


class TestExtractApiKey:
    def test_subscription_level_metadata(self):
        obj = {
            "id": "sub_abc",
            "metadata": {"api_key": "user-key-123"},
        }
        assert _extract_api_key_from_event_object(obj) == "user-key-123"

    def test_expanded_customer_metadata(self):
        obj = {
            "id": "sub_abc",
            "customer": {
                "id": "cus_abc",
                "metadata": {"api_key": "from-customer-123"},
            },
        }
        assert _extract_api_key_from_event_object(obj) == "from-customer-123"

    def test_subscription_wins_over_customer(self):
        obj = {
            "metadata": {"api_key": "sub-value"},
            "customer": {"metadata": {"api_key": "cus-value"}},
        }
        assert _extract_api_key_from_event_object(obj) == "sub-value"

    def test_string_customer_id_not_resolved(self):
        """Stripe sends the customer as a plain id when not expanded — we
        cannot fetch metadata without a Stripe API call and deliberately
        do not try. Falls back to None."""
        obj = {"customer": "cus_abc_not_expanded"}
        assert _extract_api_key_from_event_object(obj) is None

    def test_no_metadata_returns_none(self):
        assert _extract_api_key_from_event_object({"id": "sub_abc"}) is None

    def test_non_dict_returns_none(self):
        assert _extract_api_key_from_event_object(None) is None
        assert _extract_api_key_from_event_object("string") is None
        assert _extract_api_key_from_event_object([]) is None

    def test_empty_api_key_returns_none(self):
        assert _extract_api_key_from_event_object({"metadata": {}}) is None
        assert (
            _extract_api_key_from_event_object({"metadata": {"api_key": ""}})
            is None
        )


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------


class TestPremiumCache:
    def test_add_then_read(self, cache_file: Path):
        add_premium_key("user-123")
        assert is_premium_via_stripe("user-123") is True
        assert is_premium_via_stripe("someone-else") is False

    def test_add_persists_to_file(self, cache_file: Path):
        add_premium_key("user-123")
        assert cache_file.exists()
        data = json.loads(cache_file.read_text("utf-8"))
        assert data == ["user-123"]

    def test_remove_then_read(self, cache_file: Path):
        add_premium_key("user-123")
        remove_premium_key("user-123")
        assert is_premium_via_stripe("user-123") is False
        # File should not contain the key either.
        assert "user-123" not in cache_file.read_text("utf-8")

    def test_remove_missing_key_is_idempotent(self, cache_file: Path):
        remove_premium_key("never-added")  # must not raise

    def test_add_empty_is_noop(self, cache_file: Path):
        add_premium_key("")
        add_premium_key(None)
        assert current_premium_keys() == set()

    def test_cache_reload_from_disk(self, cache_file: Path):
        # Populate cache externally, then force reload.
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(["pre-existing-a", "pre-existing-b"]),
            encoding="utf-8",
        )
        reset_cache_for_tests()
        assert is_premium_via_stripe("pre-existing-a") is True
        assert is_premium_via_stripe("pre-existing-b") is True
        assert is_premium_via_stripe("not-there") is False

    def test_corrupt_cache_file_does_not_crash(
        self, cache_file: Path
    ):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("not valid json {{{", encoding="utf-8")
        reset_cache_for_tests()
        # Treated as empty — no exception propagates.
        assert is_premium_via_stripe("anyone") is False

    def test_non_list_cache_file_treated_as_empty(self, cache_file: Path):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        reset_cache_for_tests()
        assert current_premium_keys() == set()

    def test_add_deduplicates(self, cache_file: Path):
        for _ in range(5):
            add_premium_key("same-key")
        assert current_premium_keys() == {"same-key"}

    def test_thread_safe_concurrent_writes(self, cache_file: Path):
        N = 30
        threads = [
            threading.Thread(
                target=lambda i=i: add_premium_key(f"key-{i}")
            )
            for i in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stored = current_premium_keys()
        assert stored == {f"key-{i}" for i in range(N)}


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


class TestHandleWebhookEvent:
    def test_subscription_created_active_adds_premium(self, cache_file: Path):
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-xyz"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"
        assert is_premium_via_stripe("user-xyz") is True

    def test_subscription_created_trialing_also_adds_premium(
        self, cache_file: Path
    ):
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "trialing",
                "metadata": {"api_key": "user-trial"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"
        assert is_premium_via_stripe("user-trial") is True

    def test_subscription_created_incomplete_does_not_add(
        self, cache_file: Path
    ):
        """Only 'active' and 'trialing' qualify."""
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "incomplete",
                "metadata": {"api_key": "user-incomplete"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert is_premium_via_stripe("user-incomplete") is False

    def test_subscription_deleted_removes_premium(self, cache_file: Path):
        add_premium_key("user-bye")
        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": "user-bye"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "removed"
        assert is_premium_via_stripe("user-bye") is False

    def test_payment_failed_removes_premium(self, cache_file: Path):
        add_premium_key("user-fail")
        event = {
            "type": "invoice.payment_failed",
            "data": {"object": {
                "metadata": {"api_key": "user-fail"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "removed"
        assert is_premium_via_stripe("user-fail") is False

    def test_unknown_event_type_is_ignored(self, cache_file: Path):
        event = {
            "type": "charge.refunded",
            "data": {"object": {"metadata": {"api_key": "user-x"}}},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        # Must not affect cache either way.
        assert is_premium_via_stripe("user-x") is False

    def test_event_without_api_key_ignored(self, cache_file: Path):
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {"status": "active"}},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert result["reason"] == "no_api_key_on_event"

    def test_non_dict_event_ignored(self):
        assert handle_webhook_event(None)["action"] == "ignored"
        assert handle_webhook_event("not json")["action"] == "ignored"

    def test_remove_idempotent_for_non_premium(self, cache_file: Path):
        """Removing a user who was never premium must not raise."""
        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"api_key": "unknown-user"}}},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "removed"


# ---------------------------------------------------------------------------
# No sensitive data stored
# ---------------------------------------------------------------------------


class TestNoSensitiveDataStored:
    """
    The cache file must only contain opaque API-key strings. Anything
    Stripe puts inside the webhook object — card numbers, CVV, emails,
    names, full customer records — must NOT end up on disk.
    """

    def test_cache_never_contains_card_data(self, cache_file: Path):
        # A stripe event decorated with sensitive-looking fields.
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "opaque-key-abc"},
                # The following are INTENTIONALLY leak markers.
                "customer": {
                    "email": "sensitive_email@example.com",
                    "name": "Sensitive Name",
                    "metadata": {"pan": "4242424242424242", "cvv": "123"},
                },
                "latest_invoice": {
                    "payment_intent": {
                        "payment_method": {
                            "card": {
                                "last4": "4242",
                                "exp_month": 12,
                                "exp_year": 2030,
                            }
                        }
                    }
                },
            }},
        }
        handle_webhook_event(event)
        raw = cache_file.read_text("utf-8")
        for forbidden in (
            "sensitive_email@example.com",
            "Sensitive Name",
            "4242424242424242",
            "exp_month", "exp_year", "last4",
            "pan", "cvv",
        ):
            assert forbidden not in raw, f"leaked {forbidden!r}"
        # Only the opaque api key survives.
        assert "opaque-key-abc" in raw

    def test_event_without_metadata_leaves_cache_empty(
        self, cache_file: Path
    ):
        """Payloads without api_key metadata must NOT leak anything to disk."""
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "customer": {"email": "leaked@example.com"},
            }},
        }
        handle_webhook_event(event)
        if cache_file.exists():
            raw = cache_file.read_text("utf-8")
            assert "leaked@example.com" not in raw


# ---------------------------------------------------------------------------
# Boundary re-assertion
# ---------------------------------------------------------------------------


class TestBillingBoundary:
    def test_module_does_not_import_core(self):
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
            assert pat not in src, (
                f"billing module violates SaaS boundary: {pat!r}"
            )

    def test_default_cache_path_is_documented(self):
        assert DEFAULT_STRIPE_PREMIUM_CACHE_PATH == "data/stripe_premium_keys.json"

    def test_default_tolerance_is_300_seconds(self):
        assert DEFAULT_SIGNATURE_TOLERANCE_SECONDS == 300


# ===========================================================================
# Phase 4.8 — operator-only Stripe Checkout CLI / helper
# ===========================================================================


import subprocess  # noqa: E402
import sys  # noqa: E402

from trading_bot.api.billing import (  # noqa: E402
    STRIPE_API_BASE_URL,
    BillingAPIError,
    BillingConfigError,
    _hash_api_key,
    _validate_checkout_inputs,
    create_checkout_session,
    main as billing_main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAFE_API_KEY_FOR_CHECKOUT = "operator-granted-user-key-ABC123"
SAFE_SUCCESS_URL = "https://app.example.com/billing/success"
SAFE_CANCEL_URL = "https://app.example.com/billing/cancel"


@pytest.fixture
def stripe_checkout_env(monkeypatch):
    """Configure both env vars create_checkout_session needs."""
    monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_checkout_xyz")
    monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_test_premium_monthly")


@pytest.fixture
def stub_http_post():
    """
    Return a stub HTTP poster that records its kwargs and returns a
    realistic Stripe Checkout Session JSON body.
    """
    calls: list[dict] = []

    def stub(**kwargs):
        calls.append(kwargs)
        return {
            "id": "cs_test_session_abcdef123",
            "url": "https://checkout.stripe.com/c/pay/cs_test_session_abcdef123",
            "object": "checkout.session",
            "mode": "subscription",
        }

    stub.calls = calls  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestValidateCheckoutInputs:
    def test_valid_inputs_pass(self):
        _validate_checkout_inputs(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
        )

    @pytest.mark.parametrize(
        "api_key", [None, "", "   "],
    )
    def test_missing_api_key_raises(self, api_key):
        with pytest.raises(ValueError, match="api_key"):
            _validate_checkout_inputs(
                api_key, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            )

    @pytest.mark.parametrize("success_url", [None, "", "   "])
    def test_missing_success_url_raises(self, success_url):
        with pytest.raises(ValueError, match="success_url"):
            _validate_checkout_inputs(
                SAFE_API_KEY_FOR_CHECKOUT, success_url, SAFE_CANCEL_URL,
            )

    @pytest.mark.parametrize("cancel_url", [None, "", "   "])
    def test_missing_cancel_url_raises(self, cancel_url):
        with pytest.raises(ValueError, match="cancel_url"):
            _validate_checkout_inputs(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, cancel_url,
            )

    @pytest.mark.parametrize(
        "bad",
        [
            "https://evil.example.com\r\nX-Injected: 1",
            "https://evil.example.com\nHeader: 1",
            "https://example.com/\x00",
            "https://example.com/\tpath",
        ],
    )
    def test_newline_or_control_chars_in_urls_rejected(self, bad):
        with pytest.raises(ValueError, match="forbidden"):
            _validate_checkout_inputs(
                SAFE_API_KEY_FOR_CHECKOUT, bad, SAFE_CANCEL_URL,
            )
        with pytest.raises(ValueError, match="forbidden"):
            _validate_checkout_inputs(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, bad,
            )


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------


class TestCreateCheckoutSessionConfigErrors:
    def test_missing_api_key_raises_config_error(self, monkeypatch):
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_test")
        with pytest.raises(BillingConfigError) as excinfo:
            create_checkout_session(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
                http_post=lambda **kw: {"id": "x", "url": "y"},
            )
        assert "STRIPE_API_KEY" in str(excinfo.value)
        # Never echoes the api_key.
        assert SAFE_API_KEY_FOR_CHECKOUT not in str(excinfo.value)

    def test_missing_price_id_raises_config_error(self, monkeypatch):
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_x")
        monkeypatch.delenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, raising=False)
        with pytest.raises(BillingConfigError) as excinfo:
            create_checkout_session(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
                http_post=lambda **kw: {"id": "x", "url": "y"},
            )
        assert "STRIPE_PRICE_ID_PREMIUM" in str(excinfo.value)
        assert SAFE_API_KEY_FOR_CHECKOUT not in str(excinfo.value)

    def test_webhook_secret_not_needed(self, monkeypatch, stub_http_post):
        """Checkout generation must NOT require STRIPE_WEBHOOK_SECRET."""
        monkeypatch.delenv(STRIPE_WEBHOOK_SECRET_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_x")
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_x")
        result = create_checkout_session(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            http_post=stub_http_post,
        )
        assert result["checkout_url"].startswith("https://checkout.stripe.com/")


# ---------------------------------------------------------------------------
# Payload correctness
# ---------------------------------------------------------------------------


class TestCreateCheckoutSessionPayload:
    def test_returns_only_safe_fields(
        self, stripe_checkout_env, stub_http_post
    ):
        result = create_checkout_session(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            http_post=stub_http_post,
        )
        assert set(result.keys()) == {
            "checkout_session_id", "checkout_url", "api_key_hash",
        }
        assert result["checkout_session_id"] == "cs_test_session_abcdef123"
        assert result["checkout_url"].startswith("https://checkout.stripe.com/")
        assert result["api_key_hash"] == _hash_api_key(SAFE_API_KEY_FOR_CHECKOUT)
        assert len(result["api_key_hash"]) == 32

    def test_raw_api_key_never_in_return_value(
        self, stripe_checkout_env, stub_http_post
    ):
        result = create_checkout_session(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            http_post=stub_http_post,
        )
        dumped = json.dumps(result)
        assert SAFE_API_KEY_FOR_CHECKOUT not in dumped

    def test_payload_uses_stripe_price_id_from_env(
        self, monkeypatch, stub_http_post
    ):
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_live_1")
        monkeypatch.setenv(
            STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_super_premium_xyz",
        )
        create_checkout_session(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            http_post=stub_http_post,
        )
        call = stub_http_post.calls[-1]
        assert call["data"]["line_items[0][price]"] == "price_super_premium_xyz"

    def test_payload_has_all_required_stripe_fields(
        self, stripe_checkout_env, stub_http_post
    ):
        create_checkout_session(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            http_post=stub_http_post,
        )
        data = stub_http_post.calls[-1]["data"]
        assert data["mode"] == "subscription"
        assert data["line_items[0][price]"] == "price_test_premium_monthly"
        assert data["line_items[0][quantity]"] == "1"
        assert data["success_url"] == SAFE_SUCCESS_URL
        assert data["cancel_url"] == SAFE_CANCEL_URL
        # ``customer_creation`` is intentionally absent — Stripe
        # rejects it in subscription mode (it's only valid in
        # payment / setup mode). Subscription-mode sessions create
        # a customer record by construction.
        assert "customer_creation" not in data
        # Api key on BOTH session metadata AND subscription metadata —
        # so the webhook handler gets it regardless of expansion.
        assert data["metadata[api_key]"] == SAFE_API_KEY_FOR_CHECKOUT
        assert (
            data["subscription_data[metadata][api_key]"]
            == SAFE_API_KEY_FOR_CHECKOUT
        )

    def test_payload_posts_to_correct_stripe_url(
        self, stripe_checkout_env, stub_http_post
    ):
        create_checkout_session(
            SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
            http_post=stub_http_post,
        )
        call = stub_http_post.calls[-1]
        assert call["url"] == f"{STRIPE_API_BASE_URL}/checkout/sessions"
        assert call["auth"] == ("sk_test_checkout_xyz", "")
        assert call["timeout"] > 0

    def test_non_2xx_from_stripe_raises_api_error(
        self, stripe_checkout_env
    ):
        def failing_post(**kwargs):
            raise BillingAPIError("stripe returned HTTP 400")

        with pytest.raises(BillingAPIError):
            create_checkout_session(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
                http_post=failing_post,
            )

    def test_missing_id_in_response_raises(
        self, stripe_checkout_env
    ):
        with pytest.raises(BillingAPIError, match="missing 'id' or 'url'"):
            create_checkout_session(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
                http_post=lambda **kw: {"url": "https://checkout.stripe.com/x"},
            )

    def test_missing_url_in_response_raises(
        self, stripe_checkout_env
    ):
        with pytest.raises(BillingAPIError, match="missing 'id' or 'url'"):
            create_checkout_session(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
                http_post=lambda **kw: {"id": "cs_abc"},
            )

    def test_non_dict_response_raises(
        self, stripe_checkout_env
    ):
        with pytest.raises(BillingAPIError, match="non-object"):
            create_checkout_session(
                SAFE_API_KEY_FOR_CHECKOUT, SAFE_SUCCESS_URL, SAFE_CANCEL_URL,
                http_post=lambda **kw: ["not", "a", "dict"],
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCliCheckout:
    def test_cli_happy_path_prints_safe_fields_only(
        self, stripe_checkout_env, stub_http_post, monkeypatch, capsys
    ):
        # Inject the stub into the billing module so main() picks it up.
        import trading_bot.api.billing as billing_mod
        monkeypatch.setattr(
            billing_mod, "_post_to_stripe", stub_http_post,
        )
        rc = billing_main([
            "checkout",
            "--api-key", SAFE_API_KEY_FOR_CHECKOUT,
            "--success-url", SAFE_SUCCESS_URL,
            "--cancel-url", SAFE_CANCEL_URL,
        ])
        assert rc == 0
        captured = capsys.readouterr()
        out = captured.out
        # Safe fields are printed.
        assert "checkout_url:" in out
        assert "https://checkout.stripe.com/" in out
        assert "checkout_session_id:" in out
        assert "cs_test_session_abcdef123" in out
        assert "api_key_hash:" in out
        # CRITICAL: raw api key MUST NOT appear anywhere in the output.
        assert SAFE_API_KEY_FOR_CHECKOUT not in out
        assert SAFE_API_KEY_FOR_CHECKOUT not in captured.err

    def test_cli_missing_stripe_api_key_returns_2(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_x")
        rc = billing_main([
            "checkout",
            "--api-key", SAFE_API_KEY_FOR_CHECKOUT,
            "--success-url", SAFE_SUCCESS_URL,
            "--cancel-url", SAFE_CANCEL_URL,
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "STRIPE_API_KEY" in err
        # Still never echoes the api_key.
        assert SAFE_API_KEY_FOR_CHECKOUT not in err

    def test_cli_missing_price_id_returns_2(self, monkeypatch, capsys):
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_x")
        monkeypatch.delenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, raising=False)
        rc = billing_main([
            "checkout",
            "--api-key", SAFE_API_KEY_FOR_CHECKOUT,
            "--success-url", SAFE_SUCCESS_URL,
            "--cancel-url", SAFE_CANCEL_URL,
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "STRIPE_PRICE_ID_PREMIUM" in err
        assert SAFE_API_KEY_FOR_CHECKOUT not in err

    def test_cli_stripe_api_error_returns_3(
        self, stripe_checkout_env, monkeypatch, capsys
    ):
        def failing_post(**kwargs):
            raise BillingAPIError("stripe returned HTTP 402")

        import trading_bot.api.billing as billing_mod
        monkeypatch.setattr(billing_mod, "_post_to_stripe", failing_post)

        rc = billing_main([
            "checkout",
            "--api-key", SAFE_API_KEY_FOR_CHECKOUT,
            "--success-url", SAFE_SUCCESS_URL,
            "--cancel-url", SAFE_CANCEL_URL,
        ])
        assert rc == 3
        err = capsys.readouterr().err
        assert "stripe" in err.lower()
        assert SAFE_API_KEY_FOR_CHECKOUT not in err

    def test_cli_invalid_url_returns_2(
        self, stripe_checkout_env, capsys
    ):
        rc = billing_main([
            "checkout",
            "--api-key", SAFE_API_KEY_FOR_CHECKOUT,
            "--success-url", "https://example.com/\r\nHeader: inject",
            "--cancel-url", SAFE_CANCEL_URL,
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "forbidden" in err.lower()

    def test_cli_missing_command_returns_2(self, capsys):
        rc = billing_main([])
        assert rc == 2
        err = capsys.readouterr().err
        assert "usage" in err.lower() or "checkout" in err.lower()

    def test_cli_unknown_command_returns_2(self, capsys):
        rc = billing_main(["nonsense"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown" in err.lower()

    def test_cli_help_returns_0(self, capsys):
        rc = billing_main(["--help"])
        assert rc == 0

    def test_cli_subprocess_smoke(
        self, stripe_checkout_env, monkeypatch, tmp_path
    ):
        """Run `python -m trading_bot.api.billing checkout ...` in a
        subprocess with a controlled env, stubbing the HTTP poster
        via a tiny helper script that monkey-patches before dispatch."""
        helper = tmp_path / "run.py"
        helper.write_text(
            "import sys\n"
            "from trading_bot.api import billing\n"
            "def _stub(**kw):\n"
            "    return {'id': 'cs_subprocess_OK', "
            "'url': 'https://checkout.stripe.com/c/pay/cs_subprocess_OK'}\n"
            "billing._post_to_stripe = _stub\n"
            "raise SystemExit(billing.main(sys.argv[1:]))\n"
        )
        env = dict(__import__("os").environ)
        env[STRIPE_API_KEY_ENV_VAR] = "sk_test_subprocess"
        env[STRIPE_PRICE_ID_PREMIUM_ENV_VAR] = "price_subprocess"
        result = subprocess.run(
            [
                sys.executable, str(helper),
                "checkout",
                "--api-key", SAFE_API_KEY_FOR_CHECKOUT,
                "--success-url", SAFE_SUCCESS_URL,
                "--cancel-url", SAFE_CANCEL_URL,
            ],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert result.returncode == 0, result.stderr
        assert "cs_subprocess_OK" in result.stdout
        assert "https://checkout.stripe.com/c/pay/cs_subprocess_OK" in result.stdout
        # Subprocess never prints the raw api key.
        assert SAFE_API_KEY_FOR_CHECKOUT not in result.stdout
        assert SAFE_API_KEY_FOR_CHECKOUT not in result.stderr


# ---------------------------------------------------------------------------
# No new FastAPI route added by Phase 4.8
# ---------------------------------------------------------------------------


class TestPhase48NoNewApiRoute:
    def test_no_checkout_route_on_server(self):
        """Phase 4.8 must NOT add a /checkout endpoint to the FastAPI app."""
        from trading_bot.api.server import app
        forbidden_fragments = (
            "/checkout",
            "/subscribe",
            "/upgrade",
            "/billing/checkout",
        )
        for route in app.routes:
            path = getattr(route, "path", "") or ""
            for frag in forbidden_fragments:
                assert frag not in path, (
                    f"Phase 4.8 introduced a public billing route: {path}"
                )

    def test_only_non_read_verb_is_still_webhook_stripe(self):
        """The only mutating route must still be POST /webhook/stripe."""
        from trading_bot.api.server import app
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                assert path == "/webhook/stripe" and m == "POST", (
                    f"Phase 4.8 leaked a mutating route: {m} {path}"
                )

    def test_billing_module_still_does_not_import_core(self):
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


# ===========================================================================
# Phase: Stripe test-mode hardening (preferred env vars + webhook idempotency)
# ===========================================================================


from trading_bot.api.billing import (  # noqa: E402
    STRIPE_PREMIUM_PRICE_ID_ENV_VAR,
    STRIPE_SECRET_KEY_ENV_VAR,
    _resolve_premium_price_id,
    _resolve_stripe_secret,
)


class TestStripeSecretResolver:
    def test_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        assert _resolve_stripe_secret() == ""

    def test_legacy_only(self, monkeypatch):
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_legacy")
        assert _resolve_stripe_secret() == "sk_test_legacy"

    def test_preferred_only(self, monkeypatch):
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_preferred")
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        assert _resolve_stripe_secret() == "sk_test_preferred"

    def test_preferred_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_PREFERRED")
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_legacy")
        assert _resolve_stripe_secret() == "sk_test_PREFERRED"

    def test_blank_preferred_falls_through_to_legacy(self, monkeypatch):
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "   ")
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_legacy")
        assert _resolve_stripe_secret() == "sk_test_legacy"


class TestPremiumPriceIdResolver:
    def test_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, raising=False)
        monkeypatch.delenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, raising=False)
        assert _resolve_premium_price_id() == ""

    def test_legacy_only(self, monkeypatch):
        monkeypatch.delenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_legacy")
        assert _resolve_premium_price_id() == "price_legacy"

    def test_preferred_only(self, monkeypatch):
        monkeypatch.setenv(
            STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_preferred",
        )
        monkeypatch.delenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, raising=False)
        assert _resolve_premium_price_id() == "price_preferred"

    def test_preferred_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv(
            STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_PREFERRED",
        )
        monkeypatch.setenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_legacy")
        assert _resolve_premium_price_id() == "price_PREFERRED"


class TestIsStripeConfiguredAcceptsEitherEnv:
    """is_stripe_configured() must accept either env-var name —
    Phase test-mode hardening adds the preferred STRIPE_SECRET_KEY
    name without breaking the legacy STRIPE_API_KEY path."""

    def test_with_preferred_env(self, monkeypatch):
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_preferred")
        assert is_stripe_configured() is True

    def test_with_legacy_env(self, monkeypatch):
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_legacy")
        assert is_stripe_configured() is True

    def test_with_neither_env(self, monkeypatch):
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        assert is_stripe_configured() is False


class TestCheckoutAcceptsPreferredEnv:
    """create_checkout_session must succeed when ONLY the new
    preferred env-var names are set (no legacy fallback present)."""

    def test_checkout_with_only_preferred_env(self, monkeypatch):
        monkeypatch.delenv(STRIPE_API_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_pref_1")
        monkeypatch.setenv(
            STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_pref_1",
        )

        captured = {}

        def stub(**kwargs):
            captured.update(kwargs)
            return {
                "id": "cs_test_session_pref",
                "url": "https://checkout.stripe.com/c/pay/cs_test_session_pref",
                "object": "checkout.session",
            }

        from trading_bot.api.billing import create_checkout_session
        result = create_checkout_session(
            "user-key-X",
            "https://app.example.com/billing/success",
            "https://app.example.com/billing/cancel",
            http_post=stub,
        )
        assert result["checkout_session_id"] == "cs_test_session_pref"
        assert captured["data"]["line_items[0][price]"] == "price_pref_1"
        assert captured["auth"][0] == "sk_test_pref_1"

    def test_checkout_payload_excludes_customer_creation(
        self, monkeypatch,
    ):
        """Subscription mode + customer_creation crashes Stripe — the
        field MUST NOT be in the POST body."""
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_x")
        monkeypatch.setenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_x")

        captured = {}

        def stub(**kwargs):
            captured.update(kwargs)
            return {"id": "cs_x", "url": "https://x"}

        from trading_bot.api.billing import create_checkout_session
        create_checkout_session(
            "user-key-X",
            "https://app.example.com/billing/success",
            "https://app.example.com/billing/cancel",
            http_post=stub,
        )
        data = captured["data"]
        assert data["mode"] == "subscription"
        assert "customer_creation" not in data

    def test_checkout_does_not_include_raw_key_in_returned_dict(
        self, monkeypatch,
    ):
        """The returned dict must contain api_key_hash, never the
        raw key — operator-issuance + CLI hygiene relies on this."""
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_x")
        monkeypatch.setenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_x")

        def stub(**kwargs):
            return {"id": "cs_x", "url": "https://x"}

        from trading_bot.api.billing import create_checkout_session
        raw_key = "RAW_USER_KEY_DO_NOT_LEAK"
        result = create_checkout_session(
            raw_key,
            "https://app.example.com/billing/success",
            "https://app.example.com/billing/cancel",
            http_post=stub,
        )
        assert raw_key not in json.dumps(result)
        assert "api_key_hash" in result


class TestWebhookIdempotent:
    """Stripe retries deliveries on any non-2xx (and sometimes
    spuriously on 2xx). The same event id must be a no-op the
    second time so a retried `customer.subscription.deleted` between
    an admin re-grant doesn't silently revoke premium twice."""

    def test_duplicate_created_event_not_double_processed(self):
        reset_cache_for_tests()
        event = {
            "id": "evt_TEST_dup_created",
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-AAA"},
            }},
        }
        first = handle_webhook_event(event)
        second = handle_webhook_event(event)
        assert first["action"] == "added"
        assert second["action"] == "ignored"
        assert second["reason"] == "duplicate_event"
        assert "user-AAA" in current_premium_keys()

    def test_duplicate_deleted_event_does_not_double_remove(self):
        """Even if the operator re-granted premium between the two
        deliveries, the duplicate event id must short-circuit."""
        reset_cache_for_tests()
        handle_webhook_event({
            "id": "evt_initial_create",
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-BBB"},
            }},
        })
        delete_event = {
            "id": "evt_TEST_dup_deleted",
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": "user-BBB"},
            }},
        }
        first = handle_webhook_event(delete_event)
        assert first["action"] == "removed"
        assert "user-BBB" not in current_premium_keys()

        # Operator re-grants premium via a NEW subscription event.
        handle_webhook_event({
            "id": "evt_re_grant",
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-BBB"},
            }},
        })
        assert "user-BBB" in current_premium_keys()

        # Stripe re-delivers the SAME deletion event — must be a no-op.
        second = handle_webhook_event(delete_event)
        assert second["action"] == "ignored"
        assert second["reason"] == "duplicate_event"
        # Premium status was preserved by the dedup.
        assert "user-BBB" in current_premium_keys()

    def test_no_event_id_means_no_dedup(self):
        """Events without an id field should NOT be deduped (we
        can't tell which is which)."""
        reset_cache_for_tests()
        event_no_id = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-CCC"},
            }},
        }
        first = handle_webhook_event(event_no_id)
        second = handle_webhook_event(event_no_id)
        assert first["action"] == "added"
        assert second["action"] == "added"

    def test_dedup_eviction_keeps_memory_bounded(self, monkeypatch):
        """The dedup state must not grow unboundedly. Force the cap
        to a small value and confirm older event ids are evicted."""
        reset_cache_for_tests()
        monkeypatch.setattr(billing, "_WEBHOOK_DEDUP_MAX", 3)
        for i in range(5):
            handle_webhook_event({
                "id": f"evt_E{i}",
                "type": "customer.subscription.created",
                "data": {"object": {
                    "status": "active",
                    "metadata": {"api_key": f"user-X{i}"},
                }},
            })
        # The first event id should have been evicted by now.
        assert "evt_E0" not in billing._webhook_seen_set
        # The most recent ones are still there.
        assert "evt_E4" in billing._webhook_seen_set
        # Bounded.
        assert len(billing._webhook_seen_ids) == 3


class TestCancellationRemovesPremium:
    """End-to-end: after a subscription.deleted event, the key's
    entitlement (its 'key_hash' / identity in the premium cache)
    is gone."""

    def test_subscription_deleted_removes_key_from_cache(self):
        reset_cache_for_tests()
        api_key = "promoted-then-cancelled-key"
        handle_webhook_event({
            "id": "evt_create_AAA",
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": api_key},
            }},
        })
        assert api_key in current_premium_keys()
        assert is_premium_via_stripe(api_key) is True

        result = handle_webhook_event({
            "id": "evt_delete_AAA",
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": api_key},
            }},
        })
        assert result["action"] == "removed"
        assert api_key not in current_premium_keys()
        assert is_premium_via_stripe(api_key) is False

    def test_payment_failed_also_removes(self):
        reset_cache_for_tests()
        api_key = "user-payment-fail"
        handle_webhook_event({
            "id": "evt_create_pay_fail",
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": api_key},
            }},
        })
        assert api_key in current_premium_keys()
        result = handle_webhook_event({
            "id": "evt_invoice_failed",
            "type": "invoice.payment_failed",
            "data": {"object": {
                "metadata": {"api_key": api_key},
            }},
        })
        assert result["action"] == "removed"
        assert result["reason"] == "payment_failed"
        assert api_key not in current_premium_keys()

    def test_cancellation_for_unknown_key_is_safe_noop(self):
        """A cancellation for a key the cache doesn't know about
        is still safe — set.discard semantics."""
        reset_cache_for_tests()
        result = handle_webhook_event({
            "id": "evt_unknown_delete",
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": "never-promoted"},
            }},
        })
        assert result["action"] == "removed"
        assert "never-promoted" not in current_premium_keys()


class TestLiveBehaviorPreserved:
    """Calls that worked before must still work — the test-mode
    hardening must not have broken the deployed surface."""

    def test_legacy_env_only_still_works_for_checkout(self, monkeypatch):
        """The currently-deployed Railway env still uses
        STRIPE_API_KEY + STRIPE_PRICE_ID_PREMIUM only."""
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, raising=False)
        monkeypatch.setenv(STRIPE_API_KEY_ENV_VAR, "sk_test_legacy_only")
        monkeypatch.setenv(
            STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "price_legacy_only",
        )

        captured = {}

        def stub(**kwargs):
            captured.update(kwargs)
            return {"id": "cs_legacy", "url": "https://x"}

        from trading_bot.api.billing import create_checkout_session
        result = create_checkout_session(
            "user-LEG",
            "https://app.example.com/billing/success",
            "https://app.example.com/billing/cancel",
            http_post=stub,
        )
        assert result["checkout_session_id"] == "cs_legacy"
        assert captured["data"]["line_items[0][price]"] == "price_legacy_only"
        assert captured["auth"][0] == "sk_test_legacy_only"

    def test_existing_event_payloads_still_dispatch(self):
        """Spot-check: the three handled event types still produce
        the expected actions when inputs are exactly what the live
        deployment delivers."""
        reset_cache_for_tests()
        for evt_id, evt_type, expected in (
            ("evt_C1", "customer.subscription.created", "added"),
            ("evt_D1", "customer.subscription.deleted", "removed"),
            ("evt_F1", "invoice.payment_failed", "removed"),
        ):
            api_key = f"user-{evt_id}"
            payload = {
                "id": evt_id, "type": evt_type,
                "data": {"object": {
                    "status": "active",
                    "metadata": {"api_key": api_key},
                }},
            }
            assert handle_webhook_event(payload)["action"] == expected
