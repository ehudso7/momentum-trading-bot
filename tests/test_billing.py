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
