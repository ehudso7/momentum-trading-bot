"""
Phase 4.9 tests — free → paid conversion tracking.

Covers the pure `trading_bot.api.conversion` module plus its
integration with the Phase 4.7 Stripe webhook. Privacy asserts that
no raw API key, no PII, and no card data ever appears in the
conversion log file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.api import billing
from trading_bot.api import conversion
from trading_bot.api.conversion import (
    CONVERSION_EVENT,
    CONVERSION_LOG_ENV_VAR,
    DEFAULT_CONVERSION_SOURCE,
    USAGE_LOG_ENV_VAR,
    _hash_api_key,
    find_first_seen_timestamp,
    record_conversion,
    reset_cache_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_conversion_env(monkeypatch, tmp_path: Path):
    """
    Every test starts from a clean conversion state:
      - env vars pointing at tmp files (never touches real data/).
      - in-memory dedup set cleared before AND after.
      - Stripe env cleared so fixtures can opt in.
      - Phase 7.0: manifest + revocation paths redirected to tmp so
        the webhook's manifest-gate has a clean surface.
    """
    for name in (
        CONVERSION_LOG_ENV_VAR,
        USAGE_LOG_ENV_VAR,
        "STRIPE_API_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID_PREMIUM",
        "TRADING_STRIPE_PREMIUM_CACHE_PATH",
        "TRADING_API_KEYS_MANIFEST_PATH",
        "TRADING_API_KEYS_REVOKED_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    conv_file = tmp_path / "conversions.jsonl"
    usage_file = tmp_path / "usage.jsonl"
    keys_manifest = tmp_path / "api_keys_manifest.jsonl"
    keys_revoked = tmp_path / "api_keys_revoked.jsonl"
    monkeypatch.setenv(CONVERSION_LOG_ENV_VAR, str(conv_file))
    monkeypatch.setenv(USAGE_LOG_ENV_VAR, str(usage_file))
    monkeypatch.setenv("TRADING_API_KEYS_MANIFEST_PATH", str(keys_manifest))
    monkeypatch.setenv("TRADING_API_KEYS_REVOKED_PATH", str(keys_revoked))
    reset_cache_for_tests()
    billing.reset_cache_for_tests()
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()
    yield {
        "conversion_file": conv_file,
        "usage_file": usage_file,
        "keys_manifest": keys_manifest,
        "keys_revoked": keys_revoked,
    }
    reset_cache_for_tests()
    billing.reset_cache_for_tests()
    key_store.reset_caches_for_tests()


def _pre_issue_manifest_row(
    clean_conversion_env: dict, api_key: str, *, tier: str = "free",
) -> None:
    """Phase 7.0 — append a manifest row so the webhook's
    manifest gate accepts ``api_key`` during the test."""
    import hashlib as _hl
    path = clean_conversion_env["keys_manifest"]
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "created_at": "2026-04-25T00:00:00.000000Z",
        "key_hash": _hl.sha256(api_key.encode("utf-8")).hexdigest()[:32],
        "label_hash": "f" * 32,
        "tier": tier,
        "checkout_session_id": None,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    from trading_bot.api import key_store
    key_store.reset_caches_for_tests()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except Exception:
            continue
    return out


def _write_usage_line(_file_path: Path, **fields) -> None:
    """Append a JSON-line to the usage log. ``_file_path`` is named
    with a leading underscore so a ``path=`` field in the record
    (e.g. an HTTP path) does not collide with the positional arg."""
    _file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(fields) + "\n")


# ---------------------------------------------------------------------------
# _hash_api_key — must match the server + billing implementations
# ---------------------------------------------------------------------------


class TestHashApiKeyConsistency:
    def test_empty_returns_empty_string(self):
        assert _hash_api_key(None) == ""
        assert _hash_api_key("") == ""

    def test_matches_sha256_prefix(self):
        key = "some-user-key-abc"
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        assert _hash_api_key(key) == expected

    def test_matches_server_module_hash(self):
        """Conversion-log hash MUST join 1:1 with usage-log hash."""
        from trading_bot.api.server import _hash_api_key as server_hash
        for k in ("abc", "alpha-123", "uñicode"):
            assert _hash_api_key(k) == server_hash(k)

    def test_matches_billing_module_hash(self):
        from trading_bot.api.billing import _hash_api_key as billing_hash
        for k in ("x", "plain", "very-long-" + "z" * 200):
            assert _hash_api_key(k) == billing_hash(k)


# ---------------------------------------------------------------------------
# record_conversion — happy path + privacy
# ---------------------------------------------------------------------------


class TestRecordConversion:
    def test_writes_record_with_required_fields(self, clean_conversion_env):
        api_key = "user-key-UNIQUE_MARKER_ABC"
        result = record_conversion(
            api_key, source="stripe", price_id="price_monthly_001",
            now=datetime(2026, 4, 24, 15, 30, 0, tzinfo=timezone.utc),
        )
        assert result["action"] == "recorded"
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1
        rec = records[0]
        assert rec["event"] == CONVERSION_EVENT
        assert rec["source"] == "stripe"
        assert rec["price_id"] == "price_monthly_001"
        assert rec["timestamp"] == "2026-04-24T15:30:00.000000Z"
        assert rec["api_key_hash"] == _hash_api_key(api_key)
        assert "first_seen_timestamp" in rec
        # Raw key MUST NOT be persisted.
        raw = clean_conversion_env["conversion_file"].read_text("utf-8")
        assert api_key not in raw
        assert "UNIQUE_MARKER_ABC" not in raw

    def test_hash_matches_usage_log_hash(self, clean_conversion_env):
        """A conversion record + usage record for the same API key
        share an identical ``api_key_hash`` so BI pipelines can join
        them."""
        api_key = "user-for-join-test"
        from trading_bot.api.server import _hash_api_key as usage_hash
        _write_usage_line(
            clean_conversion_env["usage_file"],
            timestamp="2026-04-01T09:00:00.000000Z",
            key_hash=usage_hash(api_key),
            tier="free", method="GET", path="/reports/latest",
            status_code=200, duration_ms=1.0, request_id="rid-1",
        )
        record_conversion(api_key, source="stripe", price_id="price_x")
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1
        assert records[0]["api_key_hash"] == usage_hash(api_key)

    def test_raw_key_never_in_record(self, clean_conversion_env):
        marker = "MARKER_raw_key_SECRET_9d8e3f"
        record_conversion(marker, source="stripe", price_id="price_y")
        raw = clean_conversion_env["conversion_file"].read_text("utf-8")
        assert marker not in raw
        assert "SECRET_9d8e3f" not in raw

    def test_empty_key_skipped(self, clean_conversion_env):
        assert record_conversion("", source="stripe")["action"] == "skipped"
        assert record_conversion(None, source="stripe")["action"] == "skipped"
        assert not clean_conversion_env["conversion_file"].exists() or (
            clean_conversion_env["conversion_file"].read_text("utf-8") == ""
        )

    def test_default_source_is_stripe(self, clean_conversion_env):
        record_conversion("user-k")
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["source"] == DEFAULT_CONVERSION_SOURCE == "stripe"

    def test_explicit_first_seen_timestamp_passes_through(
        self, clean_conversion_env
    ):
        ts = "2026-01-15T08:00:00.000000Z"
        record_conversion(
            "user-k", source="stripe", price_id="px",
            first_seen_timestamp=ts,
        )
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["first_seen_timestamp"] == ts


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestConversionDedup:
    def test_second_call_same_key_is_deduped(self, clean_conversion_env):
        r1 = record_conversion("user-X", source="stripe")
        r2 = record_conversion("user-X", source="stripe")
        assert r1["action"] == "recorded"
        assert r2["action"] == "deduped"
        assert r1["api_key_hash"] == r2["api_key_hash"]
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1

    def test_different_keys_produce_separate_records(
        self, clean_conversion_env
    ):
        record_conversion("user-A")
        record_conversion("user-B")
        record_conversion("user-C")
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        hashes = {r["api_key_hash"] for r in records}
        assert len(hashes) == 3

    def test_dedup_cache_reloads_from_disk(self, clean_conversion_env):
        """Simulate a server restart: write a record, drop the
        in-memory cache, then re-call — second call should dedup
        because the hash is rehydrated from disk."""
        record_conversion("user-restart")
        reset_cache_for_tests()  # simulate process restart
        result = record_conversion("user-restart")
        assert result["action"] == "deduped"
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1

    def test_concurrent_same_key_writes_produce_one_record(
        self, clean_conversion_env
    ):
        """20 threads racing on the same API key → one recorded row."""
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker():
            r = record_conversion("user-race")
            with lock:
                outcomes.append(r["action"])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()

        # Exactly one "recorded", rest are "deduped".
        assert outcomes.count("recorded") == 1
        assert outcomes.count("deduped") == 19
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1

    def test_corrupt_conversion_file_tolerated(
        self, clean_conversion_env
    ):
        """A partial write in the file must not crash the cache load."""
        path = clean_conversion_env["conversion_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "event": "converted", "api_key_hash": "prior-hash",
            }) + "\n"
            "this line is not json\n"
            "{partial json\n",
            encoding="utf-8",
        )
        reset_cache_for_tests()
        # A key whose hash matches "prior-hash" would dedup. A new
        # key is just recorded.
        r = record_conversion("brand-new-user")
        assert r["action"] == "recorded"


# ---------------------------------------------------------------------------
# First-seen lookup from usage log
# ---------------------------------------------------------------------------


class TestFindFirstSeenTimestamp:
    def test_earliest_usage_returned(self, clean_conversion_env):
        from trading_bot.api.server import _hash_api_key as usage_hash
        key = "user-multi"
        h = usage_hash(key)
        # Write 3 usage lines out of order.
        _write_usage_line(
            clean_conversion_env["usage_file"],
            timestamp="2026-04-05T10:00:00.000000Z",
            key_hash=h, tier="free",
        )
        _write_usage_line(
            clean_conversion_env["usage_file"],
            timestamp="2026-04-01T08:30:00.000000Z",  # EARLIEST
            key_hash=h, tier="free",
        )
        _write_usage_line(
            clean_conversion_env["usage_file"],
            timestamp="2026-04-10T12:00:00.000000Z",
            key_hash=h, tier="free",
        )
        record_conversion(key, source="stripe", price_id="p")
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["first_seen_timestamp"] == "2026-04-01T08:30:00.000000Z"

    def test_no_usage_log_returns_none(self, clean_conversion_env):
        """Fresh install — usage file absent."""
        assert not clean_conversion_env["usage_file"].exists()
        record_conversion("new-user", source="stripe")
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["first_seen_timestamp"] is None

    def test_no_match_returns_none(self, clean_conversion_env):
        from trading_bot.api.server import _hash_api_key as usage_hash
        _write_usage_line(
            clean_conversion_env["usage_file"],
            timestamp="2026-04-05T10:00:00.000000Z",
            key_hash=usage_hash("other-user"),
            tier="free",
        )
        record_conversion("new-user", source="stripe")
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["first_seen_timestamp"] is None

    def test_find_first_seen_directly(self, clean_conversion_env):
        """Public helper works stand-alone."""
        from trading_bot.api.server import _hash_api_key as usage_hash
        key = "helper-direct"
        _write_usage_line(
            clean_conversion_env["usage_file"],
            timestamp="2026-03-01T00:00:00.000000Z",
            key_hash=usage_hash(key), tier="free",
        )
        assert (
            find_first_seen_timestamp(usage_hash(key))
            == "2026-03-01T00:00:00.000000Z"
        )

    def test_malformed_usage_lines_skipped(self, clean_conversion_env):
        from trading_bot.api.server import _hash_api_key as usage_hash
        key = "resilient"
        path = clean_conversion_env["usage_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "not json\n"
            "\n"
            "{partial\n"
            + json.dumps({
                "timestamp": "2026-04-02T00:00:00.000000Z",
                "key_hash": usage_hash(key),
                "tier": "free",
            }) + "\n",
            encoding="utf-8",
        )
        assert find_first_seen_timestamp(usage_hash(key)) == (
            "2026-04-02T00:00:00.000000Z"
        )


# ---------------------------------------------------------------------------
# Failure-safe: record_conversion never raises, webhook never breaks
# ---------------------------------------------------------------------------


class TestFailureSafety:
    def test_write_failure_does_not_raise(
        self, clean_conversion_env, monkeypatch
    ):
        import builtins
        real_open = builtins.open

        def fail_on_append(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "")
            if "a" in mode and str(path).endswith("conversions.jsonl"):
                raise OSError("simulated disk full")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_on_append)
        # Must not raise.
        result = record_conversion("user-disk-fail", source="stripe")
        # Best-effort: action reported but nothing persisted.
        assert result["action"] == "recorded"

    def test_webhook_still_succeeds_when_conversion_write_raises(
        self, clean_conversion_env, monkeypatch
    ):
        """The whole point of the best-effort integration: a conversion
        write failure must not change the webhook's return value."""
        def boom(*a, **kw):
            raise RuntimeError("simulated conversion failure")

        monkeypatch.setattr(conversion, "record_conversion", boom)
        # billing imports record_conversion lazily inside the handler,
        # so monkey-patching the module reference before invocation works.

        _pre_issue_manifest_row(clean_conversion_env, "user-ok")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-ok"},
                "items": {"data": [{"price": {"id": "price_x"}}]},
            }},
        }
        result = billing.handle_webhook_event(event)
        assert result["action"] == "added"
        # Cache for premium was still updated.
        assert billing.is_premium_via_stripe("user-ok") is True


# ---------------------------------------------------------------------------
# Billing integration — price_id extraction + end-to-end
# ---------------------------------------------------------------------------


class TestBillingIntegration:
    def test_handle_webhook_event_records_conversion(
        self, clean_conversion_env
    ):
        _pre_issue_manifest_row(clean_conversion_env, "user-end-to-end")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-end-to-end"},
                "items": {"data": [{"price": {"id": "price_e2e"}}]},
            }},
        }
        result = billing.handle_webhook_event(event)
        assert result["action"] == "added"

        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1
        rec = records[0]
        assert rec["event"] == "converted"
        assert rec["source"] == "stripe"
        assert rec["price_id"] == "price_e2e"
        assert rec["api_key_hash"] == _hash_api_key("user-end-to-end")

    def test_handle_webhook_event_dedups_on_reactivation(
        self, clean_conversion_env
    ):
        """Same customer subscribes, cancels, subscribes again: one
        conversion record total."""
        _pre_issue_manifest_row(clean_conversion_env, "user-restart-again")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-restart-again"},
                "items": {"data": [{"price": {"id": "price_r"}}]},
            }},
        }
        billing.handle_webhook_event(event)
        billing.handle_webhook_event({
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"api_key": "user-restart-again"}}},
        })
        billing.handle_webhook_event(event)  # reactivation
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1

    def test_trialing_counts_as_conversion(self, clean_conversion_env):
        _pre_issue_manifest_row(clean_conversion_env, "user-trialing")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "trialing",
                "metadata": {"api_key": "user-trialing"},
                "items": {"data": [{"price": {"id": "price_trial"}}]},
            }},
        }
        result = billing.handle_webhook_event(event)
        assert result["action"] == "added"
        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1
        assert records[0]["price_id"] == "price_trial"

    def test_incomplete_does_not_record_conversion(
        self, clean_conversion_env
    ):
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "incomplete",
                "metadata": {"api_key": "user-inc"},
                "items": {"data": [{"price": {"id": "price_inc"}}]},
            }},
        }
        result = billing.handle_webhook_event(event)
        assert result["action"] == "ignored"
        assert _read_jsonl(clean_conversion_env["conversion_file"]) == []

    def test_deletion_does_not_record_conversion(
        self, clean_conversion_env
    ):
        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": {"metadata": {"api_key": "user-del"}}},
        }
        billing.handle_webhook_event(event)
        assert _read_jsonl(clean_conversion_env["conversion_file"]) == []

    def test_payment_failed_does_not_record_conversion(
        self, clean_conversion_env
    ):
        event = {
            "type": "invoice.payment_failed",
            "data": {"object": {"metadata": {"api_key": "user-pf"}}},
        }
        billing.handle_webhook_event(event)
        assert _read_jsonl(clean_conversion_env["conversion_file"]) == []

    def test_missing_price_id_leaves_null(self, clean_conversion_env):
        _pre_issue_manifest_row(clean_conversion_env, "user-no-price")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-no-price"},
            }},
        }
        billing.handle_webhook_event(event)
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["price_id"] is None

    def test_legacy_plan_id_used_when_items_absent(
        self, clean_conversion_env
    ):
        """Older Stripe API versions expose ``plan.id`` at the top
        level; the extractor falls back to it."""
        _pre_issue_manifest_row(clean_conversion_env, "user-legacy")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-legacy"},
                "plan": {"id": "plan_legacy_v1"},
            }},
        }
        billing.handle_webhook_event(event)
        rec = _read_jsonl(clean_conversion_env["conversion_file"])[0]
        assert rec["price_id"] == "plan_legacy_v1"


# ---------------------------------------------------------------------------
# No sensitive data persisted
# ---------------------------------------------------------------------------


class TestNoSensitiveDataPersisted:
    def test_card_email_name_never_in_conversion_file(
        self, clean_conversion_env
    ):
        """Mirror of the Phase 4.7 PII-leak test — planted card /
        email / name fields in the webhook object must NOT land in
        the conversion log."""
        _pre_issue_manifest_row(clean_conversion_env, "user-pii-guard")
        event = {
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-pii-guard"},
                "items": {"data": [{"price": {"id": "price_pii"}}]},
                "customer": {
                    "email": "LEAK_EMAIL@example.com",
                    "name": "LEAK_NAME",
                    "metadata": {"pan": "4242424242424242", "cvv": "987"},
                },
            }},
        }
        billing.handle_webhook_event(event)
        raw = clean_conversion_env["conversion_file"].read_text("utf-8")
        for bad in (
            "LEAK_EMAIL@example.com",
            "LEAK_NAME",
            "4242424242424242",
            "987",
            "cvv",
            "pan",
        ):
            assert bad not in raw, f"leaked {bad!r}"
        # The opaque hash + the price id are the only marginal metadata.
        assert "price_pii" in raw


# ---------------------------------------------------------------------------
# Full end-to-end via the Stripe webhook HTTP endpoint
# ---------------------------------------------------------------------------


class TestEndToEndThroughHttpWebhook:
    def test_subscription_created_writes_conversion_via_webhook(
        self, clean_conversion_env, monkeypatch, tmp_path: Path
    ):
        from fastapi.testclient import TestClient
        from trading_bot.api.server import app, _reset_rate_limit_bucket

        # Configure Stripe env for the server.
        secret = "whsec_for_tests_only"
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_abc")
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", secret)
        monkeypatch.setenv(
            "TRADING_STRIPE_PREMIUM_CACHE_PATH",
            str(tmp_path / "stripe_cache.json"),
        )
        # Also provide a TRADING_API_KEY or the server 503's on
        # other protected endpoints (doesn't matter for the webhook).
        monkeypatch.setenv("TRADING_API_KEY", "some-free-key")
        _reset_rate_limit_bucket()
        billing.reset_cache_for_tests()
        reset_cache_for_tests()
        _pre_issue_manifest_row(clean_conversion_env, "user-http-e2e")

        body = json.dumps({
            "type": "customer.subscription.created",
            "data": {"object": {
                "status": "active",
                "metadata": {"api_key": "user-http-e2e"},
                "items": {"data": [{"price": {"id": "price_http_e2e"}}]},
            }},
        }).encode("utf-8")
        ts = int(time.time())
        signed = f"{ts}.".encode("utf-8") + body
        sig = hmac.new(
            secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()

        client = TestClient(app)
        resp = client.post(
            "/webhook/stripe",
            content=body,
            headers={"stripe-signature": f"t={ts},v1={sig}"},
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "added"

        records = _read_jsonl(clean_conversion_env["conversion_file"])
        assert len(records) == 1
        rec = records[0]
        assert rec["api_key_hash"] == _hash_api_key("user-http-e2e")
        assert rec["price_id"] == "price_http_e2e"
        # The raw user key is never in the conversion file.
        assert "user-http-e2e" not in clean_conversion_env[
            "conversion_file"
        ].read_text("utf-8")
