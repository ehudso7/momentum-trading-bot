"""
Phase 6.2 tests — manifest-backed API key authentication store.

Covers:
  * SHA-256[:32] hashing parity with every other Phase 4/5/6 hasher.
  * Manifest reader skips blank / malformed / wrong-shape rows.
  * Hot reload picks up newly appended rows on next call.
  * Revocation gating returns None even when the manifest entry exists.
  * append_revocation only persists the hash + timestamp + reason.
  * has_active_keys flips correctly across revocation events.
  * Missing files don't raise.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from trading_bot.api import key_store
from trading_bot.api.key_store import (
    DEFAULT_KEYS_MANIFEST_PATH,
    DEFAULT_KEYS_REVOKED_PATH,
    KEYS_MANIFEST_ENV_VAR,
    KEYS_REVOKED_ENV_VAR,
    REVOCATION_REASON_MAX_LENGTH,
    TIER_FREE,
    TIER_PREMIUM,
    VALID_TIERS,
    ManifestEntry,
    append_revocation,
    has_active_keys,
    hash_api_key,
    is_revoked,
    lookup_key_hash,
    manifest_path,
    revoked_path,
    verify_api_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch, tmp_path):
    """Redirect manifest + revocation paths to tmp and clear caches."""
    manifest = tmp_path / "api_keys_manifest.jsonl"
    revoked = tmp_path / "api_keys_revoked.jsonl"
    monkeypatch.setenv(KEYS_MANIFEST_ENV_VAR, str(manifest))
    monkeypatch.setenv(KEYS_REVOKED_ENV_VAR, str(revoked))
    key_store.reset_caches_for_tests()
    yield {"manifest": manifest, "revoked": revoked}
    key_store.reset_caches_for_tests()


def _append_manifest_row(path: Path, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields) + "\n")


def _bump_mtime(path: Path) -> None:
    """
    Force the mtime forward so the cache-invalidation path fires even
    when two writes land inside the same filesystem mtime tick.
    """
    if not path.exists():
        return
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 1))


# ---------------------------------------------------------------------------
# hash_api_key
# ---------------------------------------------------------------------------


class TestHashApiKey:
    def test_matches_sha256_prefix(self):
        raw = "abc-XYZ-123"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        assert hash_api_key(raw) == expected

    def test_empty_returns_empty(self):
        assert hash_api_key("") == ""
        assert hash_api_key(None) == ""

    def test_matches_other_module_hashers(self):
        """Cross-module parity — raw key must hash to the same value
        in every Phase 4/5/6 module."""
        from trading_bot.api.keys import _hash_api_key as keys_hash
        from trading_bot.api.server import _hash_api_key as server_hash

        for raw in ("k1", "Y8DaiX_UsQi1wLCGQIygoBiK_VlK95a23qJ7LQbXf_U"):
            assert hash_api_key(raw) == keys_hash(raw)
            assert hash_api_key(raw) == server_hash(raw)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPaths:
    def test_default_paths(self, monkeypatch):
        monkeypatch.delenv(KEYS_MANIFEST_ENV_VAR, raising=False)
        monkeypatch.delenv(KEYS_REVOKED_ENV_VAR, raising=False)
        assert manifest_path() == Path(DEFAULT_KEYS_MANIFEST_PATH)
        assert revoked_path() == Path(DEFAULT_KEYS_REVOKED_PATH)

    def test_env_override(self, isolated_paths):
        assert manifest_path() == isolated_paths["manifest"]
        assert revoked_path() == isolated_paths["revoked"]


# ---------------------------------------------------------------------------
# lookup_key_hash + manifest reader robustness
# ---------------------------------------------------------------------------


class TestLookupKeyHash:
    def test_missing_file_returns_none(self, isolated_paths):
        assert lookup_key_hash("a" * 32) is None

    def test_round_trip(self, isolated_paths):
        h = "a" * 32
        _append_manifest_row(
            isolated_paths["manifest"],
            key_hash=h, tier="free", created_at="2026-04-25T00:00:00Z",
        )
        entry = lookup_key_hash(h)
        assert isinstance(entry, ManifestEntry)
        assert entry.key_hash == h
        assert entry.tier == "free"
        assert entry.created_at == "2026-04-25T00:00:00Z"

    def test_blank_input_returns_none(self, isolated_paths):
        assert lookup_key_hash("") is None
        assert lookup_key_hash(None) is None  # type: ignore[arg-type]

    def test_malformed_lines_skipped(self, isolated_paths):
        """Bad JSON, missing fields, wrong types — all dropped silently."""
        path = isolated_paths["manifest"]
        path.parent.mkdir(parents=True, exist_ok=True)
        good_h = "g" * 32
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n")                                    # blank
            fh.write("not-json\n")                            # not JSON
            fh.write(json.dumps([1, 2, 3]) + "\n")            # not a dict
            fh.write(json.dumps({"tier": "free"}) + "\n")      # no key_hash
            fh.write(json.dumps({"key_hash": good_h, "tier": "bogus"}) + "\n")
            fh.write(json.dumps({
                "key_hash": good_h,
                "tier": "free",
                "created_at": "2026-04-25T00:00:00Z",
            }) + "\n")
        entry = lookup_key_hash(good_h)
        assert entry is not None and entry.tier == "free"

    def test_premium_tier_round_trip(self, isolated_paths):
        h = "p" * 32
        _append_manifest_row(
            isolated_paths["manifest"], key_hash=h, tier="premium",
        )
        entry = lookup_key_hash(h)
        assert entry is not None and entry.tier == "premium"


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------


class TestHotReload:
    def test_new_row_visible_without_restart(self, isolated_paths):
        path = isolated_paths["manifest"]
        h1 = "1" * 32
        _append_manifest_row(path, key_hash=h1, tier="free")
        assert lookup_key_hash(h1) is not None

        # Append a second row and force the mtime forward so the
        # cache invalidates.
        h2 = "2" * 32
        _append_manifest_row(path, key_hash=h2, tier="premium")
        _bump_mtime(path)
        assert lookup_key_hash(h2) is not None
        assert lookup_key_hash(h2).tier == "premium"

    def test_revocation_visible_without_restart(self, isolated_paths):
        h = "x" * 32
        _append_manifest_row(
            isolated_paths["manifest"], key_hash=h, tier="free",
        )
        assert is_revoked(h) is False

        append_revocation(key_hash=h, reason="leaked")
        _bump_mtime(isolated_paths["revoked"])
        assert is_revoked(h) is True


# ---------------------------------------------------------------------------
# verify_api_key
# ---------------------------------------------------------------------------


class TestVerifyApiKey:
    def test_unknown_key_returns_none(self, isolated_paths):
        assert verify_api_key("never-issued") is None

    def test_known_key_returns_entry(self, isolated_paths):
        raw = "issued-key-XYZ"
        _append_manifest_row(
            isolated_paths["manifest"],
            key_hash=hash_api_key(raw), tier="premium",
        )
        entry = verify_api_key(raw)
        assert entry is not None
        assert entry.tier == "premium"

    def test_revoked_key_returns_none(self, isolated_paths):
        raw = "revoked-key-ABC"
        h = hash_api_key(raw)
        _append_manifest_row(
            isolated_paths["manifest"], key_hash=h, tier="free",
        )
        assert verify_api_key(raw) is not None  # accepted

        append_revocation(key_hash=h, reason="rotation")
        _bump_mtime(isolated_paths["revoked"])
        assert verify_api_key(raw) is None       # rejected after revoke

    def test_blank_input_returns_none(self, isolated_paths):
        assert verify_api_key("") is None
        assert verify_api_key(None) is None


# ---------------------------------------------------------------------------
# has_active_keys
# ---------------------------------------------------------------------------


class TestHasActiveKeys:
    """``has_active_keys`` answers "is the deployment configured for
    manifest-backed auth", not "is any specific key still valid".
    Revoked rows still count as "configured" — a revoked key 403s,
    it does not unconfigure the server back to 503."""

    def test_no_manifest_means_inactive(self, isolated_paths):
        assert has_active_keys() is False

    def test_manifest_with_one_row_active(self, isolated_paths):
        _append_manifest_row(
            isolated_paths["manifest"], key_hash="a" * 32, tier="free",
        )
        assert has_active_keys() is True

    def test_revoking_only_key_keeps_configured(self, isolated_paths):
        h = "a" * 32
        _append_manifest_row(
            isolated_paths["manifest"], key_hash=h, tier="free",
        )
        assert has_active_keys() is True

        append_revocation(key_hash=h)
        _bump_mtime(isolated_paths["revoked"])
        # Still configured — a revoked-only manifest produces 403 per
        # presented key, not 503 deployment-misconfigured.
        assert has_active_keys() is True

    def test_partial_revocation_keeps_active(self, isolated_paths):
        h1 = "a" * 32
        h2 = "b" * 32
        _append_manifest_row(
            isolated_paths["manifest"], key_hash=h1, tier="free",
        )
        _append_manifest_row(
            isolated_paths["manifest"], key_hash=h2, tier="premium",
        )
        append_revocation(key_hash=h1)
        _bump_mtime(isolated_paths["revoked"])
        assert has_active_keys() is True


# ---------------------------------------------------------------------------
# append_revocation
# ---------------------------------------------------------------------------


class TestAppendRevocation:
    def test_writes_documented_fields(self, isolated_paths):
        h = "z" * 32
        record = append_revocation(key_hash=h, reason="leaked on twitter")
        assert record["key_hash"] == h
        assert record["reason"] == "leaked on twitter"
        assert "timestamp" in record

        rows = [
            json.loads(line)
            for line in isolated_paths["revoked"].read_text().splitlines()
        ]
        assert len(rows) == 1
        assert rows[0]["key_hash"] == h
        assert rows[0]["reason"] == "leaked on twitter"
        assert rows[0]["timestamp"] == record["timestamp"]

    def test_reason_optional(self, isolated_paths):
        h = "y" * 32
        record = append_revocation(key_hash=h)
        assert "reason" not in record
        rows = [
            json.loads(line)
            for line in isolated_paths["revoked"].read_text().splitlines()
        ]
        assert "reason" not in rows[0]

    def test_blank_reason_dropped(self, isolated_paths):
        record = append_revocation(key_hash="q" * 32, reason="   ")
        assert "reason" not in record

    def test_overlong_reason_truncated(self, isolated_paths):
        record = append_revocation(
            key_hash="r" * 32, reason="A" * (REVOCATION_REASON_MAX_LENGTH * 2),
        )
        assert len(record["reason"]) == REVOCATION_REASON_MAX_LENGTH

    def test_blank_hash_rejected(self, isolated_paths):
        with pytest.raises(ValueError):
            append_revocation(key_hash="")
        with pytest.raises(ValueError):
            append_revocation(key_hash=None)  # type: ignore[arg-type]

    def test_does_not_persist_raw_api_key(self, isolated_paths):
        """Sanity — only ``key_hash`` is recorded; the API never
        accepts a raw key here."""
        marker = "RAW_KEY_DO_NOT_LEAK_revoke"
        h = hash_api_key(marker)
        append_revocation(key_hash=h, reason="for parity test")
        body = isolated_paths["revoked"].read_text(encoding="utf-8")
        assert marker not in body
        assert h in body


# ---------------------------------------------------------------------------
# Defaults sanity
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_paths_match_spec(self):
        assert DEFAULT_KEYS_MANIFEST_PATH == "data/api_keys_manifest.jsonl"
        assert DEFAULT_KEYS_REVOKED_PATH == "data/api_keys_revoked.jsonl"

    def test_valid_tiers(self):
        assert TIER_FREE == "free"
        assert TIER_PREMIUM == "premium"
        assert VALID_TIERS == {"free", "premium"}
