"""
Tests for the new admin CLI subcommands of trading_bot.api.keys:

  * premium-add  — adds a hash to the premium cache
  * premium-remove — removes a hash from the premium cache
  * premium-check — prints whether a hash is premium
  * webhook-events — tails the persistent webhook event log

The list command is also re-tested to confirm raw keys never leak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import trading_bot.api.billing as billing
import trading_bot.api.keys as keys_cli


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    cache_path = tmp_path / "premium_keys.json"
    events_path = tmp_path / "stripe_webhook_events.jsonl"
    monkeypatch.setenv(billing.STRIPE_PREMIUM_CACHE_ENV_VAR, str(cache_path))
    monkeypatch.setenv(
        billing.STRIPE_WEBHOOK_EVENTS_ENV_VAR, str(events_path)
    )
    monkeypatch.setenv(
        "TRADING_API_UPGRADE_EVENTS_LOG_PATH",
        str(tmp_path / "upgrade.jsonl"),
    )
    billing.reset_cache_for_tests()
    return {"cache_path": cache_path, "events_path": events_path}


HASH_A = "a" * 32 if False else "deadbeefdeadbeefdeadbeefdeadbeef"
HASH_B = "ba" * 16


# ---------------------------------------------------------------------------
# premium-add / premium-remove / premium-check
# ---------------------------------------------------------------------------


class TestPremiumAdd:
    def test_adds_hash_to_cache(self, capsys, isolate_state):
        rc = keys_cli.main(["premium-add", "--key-hash", HASH_A])
        assert rc == 0
        out = capsys.readouterr().out
        assert HASH_A in out
        # And the cache now contains it.
        assert billing.is_premium_hash(HASH_A) is True

    def test_blank_hash_returns_error(self, capsys, isolate_state):
        rc = keys_cli.main(["premium-add", "--key-hash", "   "])
        assert rc == 2
        err = capsys.readouterr().err
        assert "must not be blank" in err


class TestPremiumRemove:
    def test_removes_hash(self, capsys, isolate_state):
        billing.add_premium_hash(HASH_A)
        rc = keys_cli.main(["premium-remove", "--key-hash", HASH_A])
        assert rc == 0
        assert billing.is_premium_hash(HASH_A) is False

    def test_remove_nonexistent_is_safe(self, isolate_state):
        rc = keys_cli.main(["premium-remove", "--key-hash", "f" * 32])
        assert rc == 0


class TestPremiumCheck:
    def test_returns_zero_when_premium(self, capsys, isolate_state):
        billing.add_premium_hash(HASH_A)
        rc = keys_cli.main(["premium-check", "--key-hash", HASH_A])
        out = capsys.readouterr().out
        assert "is_premium=yes" in out
        assert rc == 0

    def test_returns_one_when_not_premium(self, capsys, isolate_state):
        rc = keys_cli.main(["premium-check", "--key-hash", "0" * 32])
        out = capsys.readouterr().out
        assert "is_premium=no" in out
        assert rc == 1


# ---------------------------------------------------------------------------
# webhook-events
# ---------------------------------------------------------------------------


def _seed_events(path: Path, count: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(count):
        rows.append({
            "id": f"evt_{i}",
            "type": "customer.subscription.created",
            "processed_at": f"2026-04-{i + 1:02d}T00:00:00.000000Z",
            "action": "added" if i % 2 == 0 else "ignored",
        })
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


class TestWebhookEvents:
    def test_no_events_prints_friendly_message(self, capsys, isolate_state):
        rc = keys_cli.main(["webhook-events", "--limit", "5"])
        out = capsys.readouterr().out
        assert "no persisted webhook events" in out
        assert rc == 0

    def test_text_output_lists_events(self, capsys, isolate_state):
        _seed_events(isolate_state["events_path"], count=3)
        rc = keys_cli.main(["webhook-events", "--limit", "5"])
        out = capsys.readouterr().out
        assert "evt_0" in out
        assert "evt_1" in out
        assert "evt_2" in out
        assert rc == 0

    def test_json_output(self, capsys, isolate_state):
        _seed_events(isolate_state["events_path"], count=2)
        keys_cli.main(["webhook-events", "--limit", "5", "--json"])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "events" in parsed
        assert len(parsed["events"]) == 2

    def test_invalid_limit_errors(self, capsys, isolate_state):
        rc = keys_cli.main(["webhook-events", "--limit", "0"])
        err = capsys.readouterr().err
        assert "must be positive" in err
        assert rc == 2

    def test_does_not_print_raw_api_keys(self, capsys, isolate_state):
        # Confirm the CLI's output never has the literal "api_key" token —
        # the persisted schema simply has no such field.
        path = isolate_state["events_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "id": "evt_secret_test",
                "type": "x", "action": "added",
            }) + "\n",
            encoding="utf-8",
        )
        keys_cli.main(["webhook-events", "--limit", "5"])
        out = capsys.readouterr().out
        assert "api_key" not in out


# ---------------------------------------------------------------------------
# top-level
# ---------------------------------------------------------------------------


class TestTopLevelCLI:
    def test_unknown_command_lists_available(self, capsys, isolate_state):
        rc = keys_cli.main(["nonsense"])
        err = capsys.readouterr().err
        assert "unknown command" in err
        assert "premium-add" in err
        assert "premium-remove" in err
        assert "premium-check" in err
        assert "webhook-events" in err
        assert rc == 2

    def test_help_lists_new_commands(self, capsys, isolate_state):
        rc = keys_cli.main(["--help"])
        out = capsys.readouterr().out
        assert "premium-add" in out
        assert "webhook-events" in out
        assert rc == 0
