"""
Phase 2 — persistent webhook idempotency + premium-cache survival.

Covers:
  * duplicate event id ignored within the same process
  * duplicate event id ignored after an in-memory reset (simulates a
    process restart that re-reads the persistent log)
  * malformed idempotency file does not crash event processing
  * webhook event log never stores raw API keys
  * premium cache survives process restart via the cache file
"""

from __future__ import annotations

import json

import pytest

import trading_bot.api.billing as billing


@pytest.fixture(autouse=True)
def isolated_billing_state(monkeypatch, tmp_path):
    """Each test starts from a clean idempotency log + premium cache."""
    cache_path = tmp_path / "premium_keys.json"
    events_path = tmp_path / "stripe_webhook_events.jsonl"
    monkeypatch.setenv(
        billing.STRIPE_PREMIUM_CACHE_ENV_VAR, str(cache_path)
    )
    monkeypatch.setenv(
        billing.STRIPE_WEBHOOK_EVENTS_ENV_VAR, str(events_path)
    )
    # The conversion + upgrade-events writers are best-effort; redirect
    # their paths into tmp so they don't crash the test by writing to
    # the real data dir.
    monkeypatch.setenv(
        "TRADING_API_UPGRADE_EVENTS_LOG_PATH",
        str(tmp_path / "upgrade.jsonl"),
    )
    billing.reset_cache_for_tests()
    return {"cache_path": cache_path, "events_path": events_path}


def _verifying_manifest(monkeypatch):
    """Stub key_store so a known hash always passes the manifest check."""
    monkeypatch.setattr(
        billing, "_verify_against_manifest", lambda _api_key: True,
    )
    monkeypatch.setattr(
        billing, "_verify_hash_against_manifest", lambda _h: True,
    )


def _make_subscription_created_event(*, event_id: str, key_hash: str) -> dict:
    return {
        "id": event_id,
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "status": "active",
                "metadata": {"key_hash": key_hash},
            },
        },
    }


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------


class TestSameProcessDuplicates:
    def test_duplicate_in_same_process_returns_ignored(
        self, monkeypatch, isolated_billing_state,
    ):
        _verifying_manifest(monkeypatch)
        event = _make_subscription_created_event(
            event_id="evt_123", key_hash="a" * 32,
        )
        first = billing.handle_webhook_event(event)
        second = billing.handle_webhook_event(event)
        assert first["action"] == "added"
        assert second["action"] == "ignored"
        assert second["reason"] == "duplicate_event"

    def test_event_persisted_after_first_call(
        self, monkeypatch, isolated_billing_state,
    ):
        _verifying_manifest(monkeypatch)
        event = _make_subscription_created_event(
            event_id="evt_persist_1", key_hash="b" * 32,
        )
        billing.handle_webhook_event(event)
        path = isolated_billing_state["events_path"]
        assert path.exists()
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(r.get("id") == "evt_persist_1" for r in rows)


class TestRestartSimulation:
    def test_duplicate_after_in_memory_reset_still_ignored(
        self, monkeypatch, isolated_billing_state,
    ):
        """Simulates a process restart: clear in-memory state, replay event."""
        _verifying_manifest(monkeypatch)
        event = _make_subscription_created_event(
            event_id="evt_restart_1", key_hash="c" * 32,
        )
        first = billing.handle_webhook_event(event)
        assert first["action"] == "added"

        # Simulate restart — this clears the in-memory _processed_event_ids
        # set and forces the persistent log to be reloaded on next call.
        billing.reset_cache_for_tests()

        second = billing.handle_webhook_event(event)
        assert second["action"] == "ignored"
        assert second["reason"] == "duplicate_event"

    def test_new_event_after_reset_still_processed(
        self, monkeypatch, isolated_billing_state,
    ):
        _verifying_manifest(monkeypatch)
        first_event = _make_subscription_created_event(
            event_id="evt_first", key_hash="d" * 32,
        )
        billing.handle_webhook_event(first_event)
        billing.reset_cache_for_tests()
        second_event = _make_subscription_created_event(
            event_id="evt_second", key_hash="d" * 32,
        )
        result = billing.handle_webhook_event(second_event)
        assert result["action"] == "added"


class TestMalformedIdempotencyFile:
    def test_garbage_lines_are_skipped(
        self, monkeypatch, isolated_billing_state,
    ):
        path = isolated_billing_state["events_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a mix of garbage lines and one valid record.
        path.write_text(
            "this is not json\n"
            "{}\n"
            "[]\n"
            "\n"
            '{"id": "evt_real_1", "type": "x", "action": "added"}\n',
            encoding="utf-8",
        )
        _verifying_manifest(monkeypatch)
        # Replaying evt_real_1 should be a duplicate.
        evt = _make_subscription_created_event(
            event_id="evt_real_1", key_hash="e" * 32,
        )
        result = billing.handle_webhook_event(evt)
        assert result["action"] == "ignored"
        assert result["reason"] == "duplicate_event"

    def test_missing_file_is_safe(
        self, monkeypatch, isolated_billing_state,
    ):
        # Path doesn't exist — no crash, every event is treated as new.
        events_path = isolated_billing_state["events_path"]
        if events_path.exists():
            events_path.unlink()
        _verifying_manifest(monkeypatch)
        evt = _make_subscription_created_event(
            event_id="evt_first_ever", key_hash="f" * 32,
        )
        result = billing.handle_webhook_event(evt)
        assert result["action"] == "added"


# ---------------------------------------------------------------------------
# Event log content
# ---------------------------------------------------------------------------


class TestEventLogContent:
    def test_log_does_not_contain_raw_api_key(
        self, monkeypatch, isolated_billing_state,
    ):
        _verifying_manifest(monkeypatch)
        secret_api_key = "sk_secret_value_should_NOT_appear_in_log"
        event = {
            "id": "evt_no_leak_1",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "status": "active",
                    "metadata": {"api_key": secret_api_key},
                },
            },
        }
        billing.handle_webhook_event(event)
        path = isolated_billing_state["events_path"]
        contents = path.read_text(encoding="utf-8")
        assert secret_api_key not in contents
        # Sanity: the row itself was written.
        assert "evt_no_leak_1" in contents

    def test_log_does_not_contain_email_or_pan(
        self, monkeypatch, isolated_billing_state,
    ):
        _verifying_manifest(monkeypatch)
        event = {
            "id": "evt_no_email_1",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "status": "active",
                    "metadata": {"key_hash": "0" * 32},
                    "customer_email": "alice@example.com",
                    "card": {"last4": "4242", "exp_month": 12},
                },
            },
        }
        billing.handle_webhook_event(event)
        contents = isolated_billing_state["events_path"].read_text(encoding="utf-8")
        assert "alice@example.com" not in contents
        assert "4242" not in contents

    def test_recent_webhook_events_helper(
        self, monkeypatch, isolated_billing_state,
    ):
        _verifying_manifest(monkeypatch)
        for i in range(3):
            evt = _make_subscription_created_event(
                event_id=f"evt_recent_{i}", key_hash="g" * 32,
            )
            billing.handle_webhook_event(evt)
        rows = billing.recent_webhook_events(limit=5)
        ids = [r.get("id") for r in rows]
        assert ids == ["evt_recent_0", "evt_recent_1", "evt_recent_2"]


# ---------------------------------------------------------------------------
# Premium cache survival
# ---------------------------------------------------------------------------


class TestPremiumCacheCrossProcessHotReload:
    """
    Regression: the operator CLI in `railway ssh` writes to the
    premium cache file. The live API server (a different process)
    must pick up the change on the very next request without a
    restart. Symptom of the original bug: `premium-add` worked
    via CLI, in-process `premium-check` returned `is_premium=yes`,
    but the live API still served `tier: free`.
    """

    def test_external_write_is_picked_up(
        self, monkeypatch, isolated_billing_state,
    ):
        cache_path = isolated_billing_state["cache_path"]
        sample_hash = "1234567890abcdef1234567890abcdef"

        # Step 1: API server starts; nothing in cache yet.
        billing.reset_cache_for_tests()
        assert billing.is_premium_hash(sample_hash) is False

        # Step 2: external process (CLI) writes the hash directly.
        # We MUST advance the mtime — using write_text twice in the
        # same fs-tick can hash to identical mtime on coarse FS clocks.
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps([sample_hash]) + "\n", encoding="utf-8")
        # Bump mtime to a comfortably later value.
        import os as _os
        future = cache_path.stat().st_mtime + 5
        _os.utime(cache_path, (future, future))

        # Step 3: API server's next call must reload and see the hash.
        # We do NOT call reset_cache_for_tests — that would mask the
        # hot-reload by forcing a fresh load from scratch.
        assert billing.is_premium_hash(sample_hash) is True

    def test_external_remove_is_picked_up(
        self, monkeypatch, isolated_billing_state,
    ):
        cache_path = isolated_billing_state["cache_path"]
        sample_hash = "abababababababababababababababab"

        # Seed: hash is in the cache.
        billing.add_premium_hash(sample_hash)
        assert billing.is_premium_hash(sample_hash) is True

        # External process removes the hash by overwriting the file.
        cache_path.write_text("[]\n", encoding="utf-8")
        import os as _os
        future = cache_path.stat().st_mtime + 5
        _os.utime(cache_path, (future, future))

        # Live API must see the removal.
        assert billing.is_premium_hash(sample_hash) is False


class TestPremiumCachePersistence:
    def test_cache_survives_in_memory_reset(
        self, monkeypatch, isolated_billing_state,
    ):
        sample_hash = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
        billing.add_premium_hash(sample_hash)
        # Simulate restart — cache file should be re-read.
        billing.reset_cache_for_tests()
        assert billing.is_premium_hash(sample_hash) is True

    def test_cache_file_contents_are_hashes(
        self, monkeypatch, isolated_billing_state,
    ):
        sample_hash = "deadbeefdeadbeefdeadbeefdeadbeef"
        billing.add_premium_hash(sample_hash)
        path = isolated_billing_state["cache_path"]
        contents = path.read_text(encoding="utf-8").strip()
        parsed = json.loads(contents)
        assert parsed == [sample_hash]
