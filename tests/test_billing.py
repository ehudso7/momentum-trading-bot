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

    Phase 7.0: also redirect the issuance manifest / revocation log
    to tmp files and reset the ``key_store`` cache, because the
    webhook handler now consults the manifest before mutating the
    premium cache.
    """
    for name in (
        STRIPE_API_KEY_ENV_VAR,
        STRIPE_WEBHOOK_SECRET_ENV_VAR,
        STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    cache_file = tmp_path / "stripe_premium.json"
    manifest_file = tmp_path / "api_keys_manifest.jsonl"
    revoked_file = tmp_path / "api_keys_revoked.jsonl"
    monkeypatch.setenv(STRIPE_PREMIUM_CACHE_ENV_VAR, str(cache_file))
    monkeypatch.setenv("TRADING_API_KEYS_MANIFEST_PATH", str(manifest_file))
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(revoked_file))
    reset_cache_for_tests()
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()
    yield cache_file
    reset_cache_for_tests()
    key_store.reset_caches_for_tests()


@pytest.fixture
def cache_file(clean_stripe_env) -> Path:
    """Alias for the tmp cache path configured by clean_stripe_env."""
    return clean_stripe_env


def _manifest_file_from_env() -> Path:
    return Path(os.environ["TRADING_API_KEYS_MANIFEST_PATH"])


def _hash(api_key: str) -> str:
    """Local SHA-256[:32] — byte-identical to the billing / server hashers."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _pre_issue(api_key: str, *, tier: str = "free") -> str:
    """
    Phase 7.0 helper — append a minimal manifest row for ``api_key``
    so a subsequent webhook event passes the Phase 7.0 manifest gate.

    Returns the key_hash. Clears the ``key_store`` mtime-cache so the
    next read picks up the new row.
    """
    path = _manifest_file_from_env()
    path.parent.mkdir(parents=True, exist_ok=True)
    key_hash = _hash(api_key)
    row = {
        "created_at": "2026-04-25T00:00:00.000000Z",
        "key_hash": key_hash,
        "label_hash": "f" * 32,
        "tier": tier,
        "checkout_session_id": None,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()
    return key_hash


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

    def test_add_persists_to_file_as_hash_not_raw(self, cache_file: Path):
        """Phase 7.0: the raw api_key is hashed in-process; only the
        SHA-256[:32] hash lands on disk."""
        add_premium_key("user-123")
        assert cache_file.exists()
        body = cache_file.read_text("utf-8")
        data = json.loads(body)
        assert data == [_hash("user-123")]
        # The raw value must NOT appear on disk.
        assert "user-123" not in body

    def test_remove_then_read(self, cache_file: Path):
        add_premium_key("user-123")
        remove_premium_key("user-123")
        assert is_premium_via_stripe("user-123") is False
        # Neither the raw key nor its hash should be in the file.
        body = cache_file.read_text("utf-8")
        assert "user-123" not in body
        assert _hash("user-123") not in body

    def test_remove_missing_key_is_idempotent(self, cache_file: Path):
        remove_premium_key("never-added")  # must not raise

    def test_add_empty_is_noop(self, cache_file: Path):
        add_premium_key("")
        add_premium_key(None)
        assert current_premium_keys() == set()

    def test_cache_reload_from_disk_legacy_raw_keys_migrated(
        self, cache_file: Path,
    ):
        """Phase 7.0 migration: a cache file containing legacy raw
        keys is transparently re-hashed and re-saved on load. Lookup
        by the raw key still works via ``is_premium_via_stripe``."""
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(["pre-existing-a", "pre-existing-b"]),
            encoding="utf-8",
        )
        reset_cache_for_tests()
        # Lookup by the raw key still succeeds — the function hashes
        # internally.
        assert is_premium_via_stripe("pre-existing-a") is True
        assert is_premium_via_stripe("pre-existing-b") is True
        assert is_premium_via_stripe("not-there") is False
        # And the on-disk file has been rewritten with hashes only.
        rewritten = json.loads(cache_file.read_text("utf-8"))
        assert set(rewritten) == {
            _hash("pre-existing-a"), _hash("pre-existing-b"),
        }
        assert "pre-existing-a" not in cache_file.read_text("utf-8")

    def test_cache_reload_from_disk_already_hashed(self, cache_file: Path):
        """A cache file whose entries are already 32-char hex hashes
        is left untouched (no re-migration)."""
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        hashes = sorted([_hash("user-a"), _hash("user-b")])
        cache_file.write_text(json.dumps(hashes), encoding="utf-8")
        mtime_before = cache_file.stat().st_mtime
        reset_cache_for_tests()
        # Looking up by the raw values still works.
        assert is_premium_via_stripe("user-a") is True
        assert is_premium_via_stripe("user-b") is True
        # And the file contents are unchanged (sorted + spaced as before).
        assert json.loads(cache_file.read_text("utf-8")) == hashes

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
        # Phase 7.0 — the cache holds one hash, not five raw copies.
        assert current_premium_keys() == {_hash("same-key")}

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
        assert stored == {_hash(f"key-{i}") for i in range(N)}


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


class TestHandleWebhookEvent:
    def test_subscription_created_active_adds_premium(self, cache_file: Path):
        # Phase 7.0 — webhook gates on manifest presence.
        _pre_issue("user-xyz", tier="free")
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
        _pre_issue("user-trial", tier="free")
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
        # Phase 7.4 — the reason string broadened from
        # "no_api_key_on_event" to "no_identity_on_event" because
        # the webhook now accepts metadata[key_hash] as a valid
        # alternative identity. Behaviour is otherwise unchanged.
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {"status": "active"}},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert result["reason"] == "no_identity_on_event"

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

    def test_cache_never_contains_card_data_or_raw_api_key(
        self, cache_file: Path,
    ):
        # Pre-issue so the webhook's Phase 7.0 gate passes.
        _pre_issue("opaque-key-abc")
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
            # Phase 7.0: the raw api_key itself must also NOT land on disk.
            "opaque-key-abc",
        ):
            assert forbidden not in raw, f"leaked {forbidden!r}"
        # The hash IS expected — it's the Phase 7.0 persisted form.
        assert _hash("opaque-key-abc") in raw

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
    def test_no_signup_or_subscribe_routes(self):
        """Phase 4.8 forbade public sign-up endpoints. Phase 7.3 added
        the SINGLE authenticated upgrade route ``POST /billing/checkout``;
        this test still forbids every other shape (sign-up forms,
        public /upgrade, /subscribe, /checkout in unrelated paths)."""
        from trading_bot.api.server import app
        forbidden_fragments = (
            "/signup",
            "/sign-up",
            "/register",
            "/subscribe",
            "/upgrade",
        )
        for route in app.routes:
            path = getattr(route, "path", "") or ""
            for frag in forbidden_fragments:
                assert frag not in path, (
                    f"public billing surface introduced: {path}"
                )

    def test_billing_checkout_route_is_authenticated(self):
        """Phase 7.3: ``POST /billing/checkout`` exists and requires
        authentication. The route is sanctioned, but it must NOT be
        a public sign-up endpoint."""
        from fastapi.testclient import TestClient
        from trading_bot.api.server import app
        client = TestClient(app)
        # No Authorization header → 401, never 200.
        r = client.post("/billing/checkout")
        assert r.status_code in {401, 503}, r.status_code

    def test_only_non_read_verb_is_still_webhook_stripe(self):
        """The only mutating routes are POST /webhook/stripe (Phase 4.7)
        and POST /billing/checkout (Phase 7.3). Anything else is a regression."""
        allowed = {("POST", "/webhook/stripe"), ("POST", "/billing/checkout")}
        from trading_bot.api.server import app
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "") or ""
            for m in methods:
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                assert (m, path) in allowed, (
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
# Phase 7.0 — Stripe → key activation bridge
# ===========================================================================


class TestPhase70ManifestGate:
    """A Stripe subscription.created event must only promote an
    api_key to premium if that key was actually issued via the
    operator CLI (manifest lookup succeeds) AND has not been revoked."""

    def test_unissued_key_webhook_ignored(self, cache_file: Path):
        """A webhook with an api_key nobody issued is rejected — no
        cache mutation, and the file either stays absent or stays
        empty."""
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "never-issued-key"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert result["reason"] == "key_not_in_manifest_or_revoked"
        assert is_premium_via_stripe("never-issued-key") is False
        if cache_file.exists():
            # File may have been created empty by a prior load; it
            # must not contain the rejected hash or the raw key.
            body = cache_file.read_text("utf-8")
            assert "never-issued-key" not in body
            assert _hash("never-issued-key") not in body

    def test_issued_key_webhook_adds_premium(self, cache_file: Path):
        """Happy path: operator issues the key, webhook promotes it."""
        _pre_issue("issued-key-happy-path", tier="free")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "issued-key-happy-path"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"
        assert is_premium_via_stripe("issued-key-happy-path") is True

    def test_revoked_key_webhook_ignored(self, cache_file: Path):
        """A key that was issued but later revoked must NOT be
        re-promoted by a Stripe event. Revocation is absolute."""
        from trading_bot.api import key_store
        _pre_issue("issued-then-revoked", tier="free")
        key_store.append_revocation(
            key_hash=_hash("issued-then-revoked"),
            reason="phase7-test",
        )
        key_store.reset_caches_for_tests()
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "issued-then-revoked"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert result["reason"] == "key_not_in_manifest_or_revoked"
        assert is_premium_via_stripe("issued-then-revoked") is False


class TestPhase70ManifestNotMutated:
    """The Stripe webhook must NEVER touch the issuance manifest."""

    def test_manifest_bytes_unchanged_after_webhook(self, cache_file: Path):
        _pre_issue("manifest-immutable", tier="free")
        mpath = _manifest_file_from_env()
        before = mpath.read_bytes()
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "manifest-immutable"},
            }},
        }
        handle_webhook_event(event)
        after = mpath.read_bytes()
        assert before == after, (
            "Phase 7.0: webhook must never mutate the issuance manifest"
        )


class TestPhase70CancellationFlow:
    """Cancellation / payment failure flows through without manifest
    verification — a cancelled customer always loses premium
    immediately, even if their manifest row has since been deleted."""

    def test_cancellation_removes_premium(self, cache_file: Path):
        _pre_issue("cancelled-user", tier="free")
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "cancelled-user"},
            }},
        })
        assert is_premium_via_stripe("cancelled-user") is True
        result = handle_webhook_event({
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": "cancelled-user"},
            }},
        })
        assert result["action"] == "removed"
        assert is_premium_via_stripe("cancelled-user") is False

    def test_payment_failed_removes_premium(self, cache_file: Path):
        _pre_issue("payment-fail-user", tier="free")
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "payment-fail-user"},
            }},
        })
        assert is_premium_via_stripe("payment-fail-user") is True
        result = handle_webhook_event({
            "type": "invoice.payment_failed",
            "data": {"object": {
                "metadata": {"api_key": "payment-fail-user"},
            }},
        })
        assert result["action"] == "removed"
        assert is_premium_via_stripe("payment-fail-user") is False

    def test_cancellation_without_prior_activation_is_noop(
        self, cache_file: Path,
    ):
        """A cancellation for a key we never promoted still returns
        'removed' (idempotent) but no longer requires a manifest
        lookup — an orphan cancellation does not blow up."""
        result = handle_webhook_event({
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "metadata": {"api_key": "orphan-cancellation"},
            }},
        })
        assert result["action"] == "removed"


class TestPhase70HashOnlyPersistence:
    """The raw api_key must never land on disk — neither in the
    premium cache, nor in any other operator-visible file."""

    def test_add_premium_key_persists_only_hash(self, cache_file: Path):
        add_premium_key("RAW_KEY_PHASE70_DO_NOT_LEAK_777")
        body = cache_file.read_text(encoding="utf-8")
        assert "RAW_KEY_PHASE70_DO_NOT_LEAK_777" not in body
        assert _hash("RAW_KEY_PHASE70_DO_NOT_LEAK_777") in body

    def test_webhook_persists_only_hash(self, cache_file: Path):
        marker = "RAW_KEY_WEBHOOK_PHASE70_DO_NOT_LEAK"
        _pre_issue(marker)
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": marker},
            }},
        })
        body = cache_file.read_text(encoding="utf-8")
        assert marker not in body
        assert _hash(marker) in body

    def test_is_premium_hash_shortcut(self, cache_file: Path):
        """``is_premium_hash`` lets the server skip a re-hash on the
        request hot path. Returns True only when the hash is in the
        cache."""
        from trading_bot.api.billing import is_premium_hash
        add_premium_key("hashshort-123")
        assert is_premium_hash(_hash("hashshort-123")) is True
        assert is_premium_hash("0" * 32) is False
        assert is_premium_hash("") is False
        assert is_premium_hash(None) is False


# ===========================================================================
# Phase 7.3 — create_checkout_session_for_hash (hash-only metadata)
# ===========================================================================


from trading_bot.api.billing import (  # noqa: E402
    BillingAPIError,
    BillingConfigError,
    STRIPE_PREMIUM_PRICE_ID_ENV_VAR,
    STRIPE_SECRET_KEY_ENV_VAR,
    create_checkout_session_for_hash,
)


@pytest.fixture
def phase73_stripe_env(monkeypatch, tmp_path: Path):
    """Configure both Phase 7.3 env vars for the helper tests."""
    monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_test_phase73")
    monkeypatch.setenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_test_phase73")
    return tmp_path


class _FakePoster:
    """Records every Stripe POST so tests can assert exact metadata shape."""

    def __init__(
        self,
        response: Optional[dict] = None,
        raise_exc: Optional[Exception] = None,
    ):
        self.response = response or {
            "id": "cs_test_phase73",
            "url": "https://checkout.stripe.com/c/cs_test_phase73",
        }
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, *, url: str, data: dict, auth, timeout: float):
        self.calls.append({
            "url": url, "data": dict(data), "auth": auth, "timeout": timeout,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        # Return the response as-is so tests can simulate non-dict
        # Stripe payloads (the helper must reject those).
        if isinstance(self.response, dict):
            return dict(self.response)
        return self.response


class TestCreateCheckoutSessionForHash:
    def test_happy_path_returns_documented_fields(
        self, phase73_stripe_env,
    ):
        poster = _FakePoster()
        result = create_checkout_session_for_hash(
            key_hash="a" * 32,
            success_url="https://example.com/dashboard?checkout=success",
            cancel_url="https://example.com/dashboard?checkout=cancel",
            http_post=poster,
        )
        assert result == {
            "checkout_session_id": "cs_test_phase73",
            "checkout_url": "https://checkout.stripe.com/c/cs_test_phase73",
            "key_hash": "a" * 32,
            "tier_to": "premium",
        }

    def test_metadata_contains_key_hash_not_raw_key(
        self, phase73_stripe_env,
    ):
        """The hash must land in metadata; the raw api_key must NEVER
        be sent to Stripe under any field name."""
        poster = _FakePoster()
        marker = "RAW_KEY_PHASE73_DO_NOT_LEAK"
        # Even though the helper takes a hash, simulate the realistic
        # case where the operator pre-hashes their secret marker.
        h = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:32]
        create_checkout_session_for_hash(
            key_hash=h,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            http_post=poster,
        )
        data = poster.calls[0]["data"]
        # Documented metadata fields are present:
        assert data["client_reference_id"] == h
        assert data["metadata[key_hash]"] == h
        assert data["metadata[tier_from]"] == "free"
        assert data["metadata[tier_to]"] == "premium"
        assert data["subscription_data[metadata][key_hash]"] == h
        assert data["subscription_data[metadata][tier_to]"] == "premium"
        # The raw api_key marker is absent from EVERY field:
        for value in data.values():
            assert marker not in str(value)
        # And the legacy raw-key field is NOT present.
        assert "metadata[api_key]" not in data
        assert "subscription_data[metadata][api_key]" not in data

    def test_subscription_mode_and_price_id(self, phase73_stripe_env):
        poster = _FakePoster()
        create_checkout_session_for_hash(
            key_hash="b" * 32,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            http_post=poster,
        )
        data = poster.calls[0]["data"]
        assert data["mode"] == "subscription"
        assert data["line_items[0][price]"] == "price_test_phase73"
        assert data["line_items[0][quantity]"] == "1"

    def test_success_and_cancel_urls_propagated(
        self, phase73_stripe_env,
    ):
        poster = _FakePoster()
        create_checkout_session_for_hash(
            key_hash="c" * 32,
            success_url="https://app.example.com/welcome",
            cancel_url="https://app.example.com/cancel",
            http_post=poster,
        )
        data = poster.calls[0]["data"]
        assert data["success_url"] == "https://app.example.com/welcome"
        assert data["cancel_url"] == "https://app.example.com/cancel"

    def test_legacy_env_var_fallback(self, monkeypatch):
        """STRIPE_SECRET_KEY / STRIPE_PREMIUM_PRICE_ID are preferred,
        but the legacy STRIPE_API_KEY / STRIPE_PRICE_ID_PREMIUM names
        must keep working so existing deployments don't break."""
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, raising=False)
        monkeypatch.setenv("STRIPE_API_KEY", "sk_legacy_test")
        monkeypatch.setenv("STRIPE_PRICE_ID_PREMIUM", "price_legacy_test")
        poster = _FakePoster()
        create_checkout_session_for_hash(
            key_hash="d" * 32,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            http_post=poster,
        )
        assert poster.calls[0]["auth"] == ("sk_legacy_test", "")
        assert poster.calls[0]["data"]["line_items[0][price]"] == "price_legacy_test"

    def test_missing_secret_raises_billing_config_error(
        self, monkeypatch,
    ):
        monkeypatch.delenv(STRIPE_SECRET_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv("STRIPE_API_KEY", raising=False)
        monkeypatch.setenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "price_x")
        with pytest.raises(BillingConfigError) as exc:
            create_checkout_session_for_hash(
                key_hash="e" * 32,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                http_post=_FakePoster(),
            )
        assert STRIPE_SECRET_KEY_ENV_VAR in str(exc.value)

    def test_missing_price_id_raises_billing_config_error(
        self, monkeypatch,
    ):
        monkeypatch.setenv(STRIPE_SECRET_KEY_ENV_VAR, "sk_x")
        monkeypatch.delenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, raising=False)
        monkeypatch.delenv("STRIPE_PRICE_ID_PREMIUM", raising=False)
        with pytest.raises(BillingConfigError) as exc:
            create_checkout_session_for_hash(
                key_hash="f" * 32,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                http_post=_FakePoster(),
            )
        assert STRIPE_PREMIUM_PRICE_ID_ENV_VAR in str(exc.value)

    def test_blank_hash_rejected(self, phase73_stripe_env):
        for h in ("", "   ", None):
            with pytest.raises(ValueError):
                create_checkout_session_for_hash(
                    key_hash=h,  # type: ignore[arg-type]
                    success_url="https://e.com/ok",
                    cancel_url="https://e.com/cancel",
                    http_post=_FakePoster(),
                )

    def test_blank_urls_rejected(self, phase73_stripe_env):
        with pytest.raises(ValueError):
            create_checkout_session_for_hash(
                key_hash="a" * 32, success_url="",
                cancel_url="https://e.com/cancel",
                http_post=_FakePoster(),
            )
        with pytest.raises(ValueError):
            create_checkout_session_for_hash(
                key_hash="a" * 32, success_url="https://e.com/ok",
                cancel_url="",
                http_post=_FakePoster(),
            )

    def test_url_with_newline_rejected(self, phase73_stripe_env):
        with pytest.raises(ValueError):
            create_checkout_session_for_hash(
                key_hash="a" * 32,
                success_url="https://e.com/ok\nX-Inject: pwn",
                cancel_url="https://e.com/cancel",
                http_post=_FakePoster(),
            )

    def test_stripe_non_dict_response_raises_api_error(
        self, phase73_stripe_env,
    ):
        bad = _FakePoster()
        bad.response = ["not", "a", "dict"]  # type: ignore[assignment]
        with pytest.raises(BillingAPIError):
            create_checkout_session_for_hash(
                key_hash="a" * 32,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                http_post=bad,
            )

    def test_stripe_missing_id_or_url_raises_api_error(
        self, phase73_stripe_env,
    ):
        bad = _FakePoster(response={"id": "cs_x"})  # missing url
        with pytest.raises(BillingAPIError):
            create_checkout_session_for_hash(
                key_hash="a" * 32,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                http_post=bad,
            )

    def test_stripe_failure_propagates_as_api_error(
        self, phase73_stripe_env,
    ):
        boom = _FakePoster(raise_exc=BillingAPIError("simulated stripe 500"))
        with pytest.raises(BillingAPIError):
            create_checkout_session_for_hash(
                key_hash="a" * 32,
                success_url="https://e.com/ok",
                cancel_url="https://e.com/cancel",
                http_post=boom,
            )

    def test_stripe_secret_used_in_basic_auth(self, phase73_stripe_env):
        poster = _FakePoster()
        create_checkout_session_for_hash(
            key_hash="a" * 32,
            success_url="https://e.com/ok",
            cancel_url="https://e.com/cancel",
            http_post=poster,
        )
        assert poster.calls[0]["auth"] == ("sk_test_phase73", "")


# ===========================================================================
# Phase 7.4 — hash-based Stripe webhook promotion
# ===========================================================================


from trading_bot.api.billing import (  # noqa: E402
    _extract_identity_from_event_object,
    _verify_hash_against_manifest,
    add_premium_hash,
    remove_premium_hash,
)


class TestExtractIdentityFromEventObject:
    """Phase 7.4 — both ``api_key`` and ``key_hash`` are extracted
    independently from ``object.metadata`` and the
    ``object.customer.metadata`` fallback."""

    def test_metadata_only_api_key(self):
        result = _extract_identity_from_event_object(
            {"metadata": {"api_key": "raw"}},
        )
        assert result == ("raw", None)

    def test_metadata_only_key_hash(self):
        result = _extract_identity_from_event_object(
            {"metadata": {"key_hash": "h" * 32}},
        )
        assert result == (None, "h" * 32)

    def test_metadata_with_both(self):
        result = _extract_identity_from_event_object(
            {"metadata": {"api_key": "raw", "key_hash": "h" * 32}},
        )
        assert result == ("raw", "h" * 32)

    def test_customer_fallback_for_each(self):
        result = _extract_identity_from_event_object(
            {
                "metadata": {},
                "customer": {"metadata": {
                    "api_key": "from-customer",
                    "key_hash": "h" * 32,
                }},
            },
        )
        assert result == ("from-customer", "h" * 32)

    def test_top_level_wins_over_customer_fallback(self):
        """If both top-level metadata and customer.metadata have a
        field, the top-level value should be preferred."""
        result = _extract_identity_from_event_object(
            {
                "metadata": {"api_key": "top"},
                "customer": {"metadata": {"api_key": "fallback"}},
            },
        )
        assert result == ("top", None)

    def test_no_identity_returns_none_pair(self):
        assert _extract_identity_from_event_object(
            {"metadata": {}},
        ) == (None, None)
        assert _extract_identity_from_event_object(None) == (None, None)
        assert _extract_identity_from_event_object("not a dict") == (
            None, None,
        )


class TestVerifyHashAgainstManifest:
    def test_unknown_hash_rejected(self, cache_file: Path):
        assert _verify_hash_against_manifest("z" * 32) is False

    def test_known_hash_accepted(self, cache_file: Path):
        h = _pre_issue("user-7-4-verify")
        assert _verify_hash_against_manifest(h) is True

    def test_revoked_hash_rejected(self, cache_file: Path):
        from trading_bot.api import key_store
        h = _pre_issue("user-7-4-revoked")
        key_store.append_revocation(key_hash=h, reason="phase74-test")
        key_store.reset_caches_for_tests()
        assert _verify_hash_against_manifest(h) is False

    def test_blank_input_rejected(self, cache_file: Path):
        assert _verify_hash_against_manifest("") is False
        assert _verify_hash_against_manifest("   ") is False
        assert _verify_hash_against_manifest(None) is False  # type: ignore[arg-type]


class TestAddRemovePremiumHash:
    def test_add_then_check(self, cache_file: Path):
        h = "p" * 32
        add_premium_hash(h)
        assert is_premium_via_stripe("anything-with-this-hash") is False
        # is_premium_via_stripe takes a raw key — for the hash path,
        # use the new is_premium_hash helper.
        from trading_bot.api.billing import is_premium_hash
        assert is_premium_hash(h) is True

    def test_add_persists_only_hash(self, cache_file: Path):
        h = "q" * 32
        add_premium_hash(h)
        body = cache_file.read_text("utf-8")
        # The hash IS on disk, but no raw key (we never had one).
        assert h in body
        # And the documented schema is preserved.
        data = json.loads(body)
        assert isinstance(data, list)
        assert h in data

    def test_remove_then_check(self, cache_file: Path):
        h = "r" * 32
        add_premium_hash(h)
        from trading_bot.api.billing import is_premium_hash
        assert is_premium_hash(h) is True
        remove_premium_hash(h)
        assert is_premium_hash(h) is False

    def test_blank_input_noop(self, cache_file: Path):
        add_premium_hash("")
        add_premium_hash(None)  # type: ignore[arg-type]
        add_premium_hash("   ")
        # Cache file may or may not exist — either way, no entries.
        from trading_bot.api.billing import current_premium_keys
        assert current_premium_keys() == set()


class TestPhase74WebhookHashPath:
    """customer.subscription.created with metadata[key_hash] should
    promote without needing the raw api_key."""

    def test_valid_key_hash_promotes(self, cache_file: Path):
        h = _pre_issue("user-hash-promotion")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h, "tier_to": "premium"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"
        assert result["identity"] == "key_hash"
        from trading_bot.api.billing import is_premium_hash
        assert is_premium_hash(h) is True

    def test_unknown_key_hash_does_not_promote(self, cache_file: Path):
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": "z" * 32},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert result["reason"] == "key_not_in_manifest_or_revoked"
        assert result["identity"] == "key_hash"

    def test_revoked_key_hash_does_not_promote(self, cache_file: Path):
        from trading_bot.api import key_store
        h = _pre_issue("user-hash-revoked")
        key_store.append_revocation(key_hash=h, reason="phase74-test")
        key_store.reset_caches_for_tests()
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert result["reason"] == "key_not_in_manifest_or_revoked"

    def test_inactive_status_does_not_promote(self, cache_file: Path):
        h = _pre_issue("user-hash-incomplete")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "incomplete",
                "metadata": {"key_hash": h},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert "status" in result["reason"]

    def test_trialing_status_promotes(self, cache_file: Path):
        h = _pre_issue("user-hash-trialing")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "trialing",
                "metadata": {"key_hash": h},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"

    def test_when_both_provided_hash_path_wins(self, cache_file: Path):
        """If a (legacy + new) Stripe payload has both api_key and
        key_hash, the hash path wins — no raw key reaches the
        verifier or the cache write."""
        h = _pre_issue("user-both")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {
                    # Raw key value is intentionally a marker that's
                    # NOT the issued raw — verifying the hash path
                    # ignores it entirely.
                    "api_key": "RAW_BOTH_DO_NOT_USE",
                    "key_hash": h,
                },
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"
        assert result["identity"] == "key_hash"
        # The ignored raw-key marker did not produce a cache entry of its own.
        body = cache_file.read_text("utf-8")
        assert "RAW_BOTH_DO_NOT_USE" not in body


class TestPhase74WebhookCancellationByHash:
    def test_cancellation_removes_premium_by_hash(self, cache_file: Path):
        h = _pre_issue("user-hash-cancel")
        # Promote first.
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h},
            }},
        })
        from trading_bot.api.billing import is_premium_hash
        assert is_premium_hash(h) is True

        # Cancel via hash.
        result = handle_webhook_event({
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"key_hash": h}}},
        })
        assert result["action"] == "removed"
        assert result["identity"] == "key_hash"
        assert is_premium_hash(h) is False

    def test_payment_failed_removes_premium_by_hash(self, cache_file: Path):
        h = _pre_issue("user-hash-pf")
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h},
            }},
        })
        from trading_bot.api.billing import is_premium_hash
        assert is_premium_hash(h) is True

        result = handle_webhook_event({
            "type": "invoice.payment_failed",
            "data": {"object": {"metadata": {"key_hash": h}}},
        })
        assert result["action"] == "removed"
        assert result["reason"] == "payment_failed"
        assert is_premium_hash(h) is False


class TestPhase74LegacyApiKeyPathStillWorks:
    """The Phase 4.7 metadata[api_key] flow MUST still work — Phase
    7.4 is purely additive."""

    def test_legacy_promotion_still_promotes(self, cache_file: Path):
        _pre_issue("legacy-api-key-user")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "legacy-api-key-user"},
            }},
        }
        result = handle_webhook_event(event)
        assert result["action"] == "added"
        assert result["identity"] == "api_key"
        assert is_premium_via_stripe("legacy-api-key-user") is True

    def test_legacy_cancellation_still_removes(self, cache_file: Path):
        _pre_issue("legacy-cancel-user")
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "legacy-cancel-user"},
            }},
        })
        result = handle_webhook_event({
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"api_key": "legacy-cancel-user"}}},
        })
        assert result["action"] == "removed"
        assert result["identity"] == "api_key"
        assert is_premium_via_stripe("legacy-cancel-user") is False


class TestPhase74WebhookPersistence:
    def test_premium_cache_contains_only_hash_after_hash_path(
        self, cache_file: Path,
    ):
        h = _pre_issue("user-leak-guard")
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h},
            }},
        })
        body = cache_file.read_text("utf-8")
        # Hash present, nothing else.
        assert h in body
        data = json.loads(body)
        assert data == [h]

    def test_planted_pii_in_event_does_not_reach_cache(
        self, cache_file: Path,
    ):
        h = _pre_issue("user-pii-guard")
        marker_email = "PII_LEAK_PHASE74_alice@example.com"
        marker_name = "PII_LEAK_PHASE74_NAME"
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h},
                "customer": {
                    "email": marker_email,
                    "name": marker_name,
                    "metadata": {"pan": "4242424242424242", "cvv": "987"},
                },
            }},
        })
        body = cache_file.read_text("utf-8")
        for forbidden in (marker_email, marker_name,
                          "4242424242424242", "987", "pan", "cvv"):
            assert forbidden not in body, f"leaked {forbidden!r}"


class TestPhase74ConversionLoggingByHash:
    """Phase 7.4 — record_conversion_for_hash records a conversion
    for the hash without needing the raw key. The webhook plumbs it
    through automatically."""

    def test_record_conversion_for_hash_writes_row(
        self, cache_file: Path, monkeypatch, tmp_path: Path,
    ):
        from trading_bot.api import conversion
        conv_path = tmp_path / "conv_phase74.jsonl"
        monkeypatch.setenv(
            "TRADING_API_CONVERSION_LOG_PATH", str(conv_path),
        )
        conversion.reset_cache_for_tests()
        result = conversion.record_conversion_for_hash(
            "h" * 32, source="stripe", price_id="price_phase74",
        )
        assert result["action"] == "recorded"
        assert result["api_key_hash"] == "h" * 32
        rows = [
            json.loads(line)
            for line in conv_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["api_key_hash"] == "h" * 32
        assert rows[0]["price_id"] == "price_phase74"

    def test_record_conversion_for_hash_dedupes(
        self, cache_file: Path, monkeypatch, tmp_path: Path,
    ):
        from trading_bot.api import conversion
        monkeypatch.setenv(
            "TRADING_API_CONVERSION_LOG_PATH",
            str(tmp_path / "conv_phase74_dedup.jsonl"),
        )
        conversion.reset_cache_for_tests()
        a = conversion.record_conversion_for_hash("k" * 32)
        b = conversion.record_conversion_for_hash("k" * 32)
        assert a["action"] == "recorded"
        assert b["action"] == "deduped"

    def test_webhook_hash_path_writes_conversion_row(
        self, cache_file: Path, monkeypatch, tmp_path: Path,
    ):
        from trading_bot.api import conversion
        conv_path = tmp_path / "conv_e2e.jsonl"
        monkeypatch.setenv(
            "TRADING_API_CONVERSION_LOG_PATH", str(conv_path),
        )
        conversion.reset_cache_for_tests()
        h = _pre_issue("user-conv-e2e")
        handle_webhook_event({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"key_hash": h},
                "items": {"data": [{"price": {"id": "price_e2e_74"}}]},
            }},
        })
        rows = [
            json.loads(line)
            for line in conv_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["api_key_hash"] == h
        assert rows[0]["price_id"] == "price_e2e_74"
