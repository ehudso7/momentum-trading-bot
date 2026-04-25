"""
Phase 6.2 — Manifest-backed API key authentication.

Pure-stdlib helper module that lets the live API server authenticate
keys issued by ``python -m trading_bot.api.keys issue`` without ever
storing the raw key. The presented bearer token is hashed with
``SHA-256(api_key)[:32]`` and looked up in the issuance manifest;
revoked hashes are rejected up-front.

This module deliberately:

  * imports nothing from FastAPI, structlog, or the ``trading_bot``
    package — it must be importable from the operator CLI (which
    has been kept dependency-light since the Phase 6.0/6.1 fix);
  * never persists raw API keys (they're only hashed on the way in);
  * never persists raw labels;
  * never opens the network;
  * tolerates missing files, malformed JSONL rows, and concurrent
    writers — every load path returns "no entries" on error rather
    than raising.

Files
-----
Issuance manifest (read-only by this module — written by ``keys.py``):

    Default: data/api_keys_manifest.jsonl
    Env:     TRADING_API_KEYS_MANIFEST_PATH

    Each row is a dict with at least:
      * ``key_hash``    — SHA-256(api_key)[:32]
      * ``tier``        — "free" | "premium"
      * ``created_at``  — ISO-8601 UTC string

Revocation log (read by all callers; appended to by ``keys.py revoke``):

    Default: data/api_keys_revoked.jsonl
    Env:     TRADING_API_KEYS_REVOKED_PATH

    Each row is a dict with at least:
      * ``timestamp``   — ISO-8601 UTC string
      * ``key_hash``    — SHA-256(api_key)[:32]
      * ``reason``      — operator-supplied free text (optional)

Hot reload
----------
Both files are cached in process memory keyed on ``(path, mtime)``.
A new row appended on disk takes effect on the next request without
a server restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KEYS_MANIFEST_ENV_VAR = "TRADING_API_KEYS_MANIFEST_PATH"
DEFAULT_KEYS_MANIFEST_PATH = "data/api_keys_manifest.jsonl"

KEYS_REVOKED_ENV_VAR = "TRADING_API_KEYS_REVOKED_PATH"
DEFAULT_KEYS_REVOKED_PATH = "data/api_keys_revoked.jsonl"

TIER_FREE = "free"
TIER_PREMIUM = "premium"
VALID_TIERS: frozenset[str] = frozenset({TIER_FREE, TIER_PREMIUM})

# Cap on the optional `reason` field stored on each revocation row.
# Long enough for a postmortem reference, short enough to keep the
# JSONL line cheap to scan.
REVOCATION_REASON_MAX_LENGTH = 200


class ManifestEntry(NamedTuple):
    """Decoded manifest row — only the fields auth actually consults."""

    key_hash: str
    tier: str
    created_at: Optional[str]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def hash_api_key(api_key: Optional[str]) -> str:
    """SHA-256[:32] — byte-identical to every other Phase 4/5/6 hasher."""
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def manifest_path() -> Path:
    return Path(
        os.getenv(KEYS_MANIFEST_ENV_VAR, DEFAULT_KEYS_MANIFEST_PATH),
    )


def revoked_path() -> Path:
    return Path(
        os.getenv(KEYS_REVOKED_ENV_VAR, DEFAULT_KEYS_REVOKED_PATH),
    )


def _now_iso_utc(now: Optional[datetime] = None) -> str:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _file_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None


# ---------------------------------------------------------------------------
# File loaders — best-effort, never raise
# ---------------------------------------------------------------------------


def _load_manifest_file(path: Path) -> dict[str, ManifestEntry]:
    out: dict[str, ManifestEntry] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                key_hash = rec.get("key_hash")
                tier = rec.get("tier")
                if not isinstance(key_hash, str) or not key_hash:
                    continue
                if tier not in VALID_TIERS:
                    continue
                created_at = rec.get("created_at")
                if not isinstance(created_at, str):
                    created_at = None
                # Last-row-wins for duplicate hashes. In practice
                # ``secrets.token_urlsafe(32)`` collisions are
                # cryptographically impossible, but a defensive
                # last-row-wins also lets an operator re-issue a
                # tier upgrade by appending a new row.
                out[key_hash] = ManifestEntry(
                    key_hash=key_hash,
                    tier=tier,
                    created_at=created_at,
                )
    except (FileNotFoundError, OSError):
        return out
    except Exception:
        return out
    return out


def _load_revoked_file(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                key_hash = rec.get("key_hash")
                if isinstance(key_hash, str) and key_hash:
                    out.add(key_hash)
    except (FileNotFoundError, OSError):
        return out
    except Exception:
        return out
    return out


# ---------------------------------------------------------------------------
# Hot-reload caches
# ---------------------------------------------------------------------------

_manifest_lock = threading.Lock()
_manifest_cache: dict[str, ManifestEntry] = {}
_manifest_mtime: Optional[float] = None
_manifest_loaded_from: Optional[Path] = None

_revoked_lock = threading.Lock()
_revoked_cache: set[str] = set()
_revoked_mtime: Optional[float] = None
_revoked_loaded_from: Optional[Path] = None

# Append serialisation — separate from the read-cache lock so a
# revocation write doesn't have to wait for an in-flight read.
_revoked_write_lock = threading.Lock()


def _ensure_manifest_loaded() -> dict[str, ManifestEntry]:
    """Reload iff path or mtime changed. Returns a defensive copy."""
    global _manifest_cache, _manifest_mtime, _manifest_loaded_from
    path = manifest_path()
    mtime = _file_mtime(path)
    with _manifest_lock:
        if path != _manifest_loaded_from or mtime != _manifest_mtime:
            _manifest_cache = _load_manifest_file(path)
            _manifest_mtime = mtime
            _manifest_loaded_from = path
        return dict(_manifest_cache)


def _ensure_revoked_loaded() -> set[str]:
    """Reload iff path or mtime changed. Returns a defensive copy."""
    global _revoked_cache, _revoked_mtime, _revoked_loaded_from
    path = revoked_path()
    mtime = _file_mtime(path)
    with _revoked_lock:
        if path != _revoked_loaded_from or mtime != _revoked_mtime:
            _revoked_cache = _load_revoked_file(path)
            _revoked_mtime = mtime
            _revoked_loaded_from = path
        return set(_revoked_cache)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lookup_key_hash(key_hash: str) -> Optional[ManifestEntry]:
    """Return the manifest entry for ``key_hash`` or ``None``."""
    if not key_hash or not isinstance(key_hash, str):
        return None
    return _ensure_manifest_loaded().get(key_hash)


def is_revoked(key_hash: str) -> bool:
    """True iff ``key_hash`` has been revoked."""
    if not key_hash or not isinstance(key_hash, str):
        return False
    return key_hash in _ensure_revoked_loaded()


def verify_api_key(api_key: Optional[str]) -> Optional[ManifestEntry]:
    """
    Validate a presented bearer token against the manifest.

    Returns the matching ``ManifestEntry`` iff the key's hash is in
    the manifest AND has not been revoked. Returns ``None``
    otherwise — including when the manifest file is missing, when
    the key is empty, and when the key is known but revoked.

    The raw ``api_key`` is hashed once and immediately discarded.
    """
    if not api_key:
        return None
    h = hash_api_key(api_key)
    if not h:
        return None
    if is_revoked(h):
        return None
    return lookup_key_hash(h)


def has_active_keys() -> bool:
    """
    True iff the manifest has at least one parseable row.

    Used by the server's fail-closed check to decide whether the
    deployment is configured for manifest-backed auth at all. A
    revoked row still counts as "configured" — revocation only
    invalidates that specific key, it does not unconfigure the
    server. So a single issued-then-revoked key produces 403
    (key was once valid, now invalid) rather than 503 (deployment
    is not configured).
    """
    return bool(_ensure_manifest_loaded())


def append_revocation(
    *,
    key_hash: str,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
    target: Optional[Path] = None,
) -> dict:
    """
    Append one revocation row to the revocation log.

    ``key_hash`` MUST be the SHA-256[:32] hash — callers that have
    only the raw key should use ``hash_api_key`` first. The raw
    api_key is never accepted here so a slip in a caller cannot
    accidentally land a raw key on disk.

    ``reason`` is operator-supplied free text. Whitespace-only
    values are dropped; longer-than-cap values are truncated.

    Idempotent on the read side: a duplicate revocation just adds a
    second row, but the read cache deduplicates into a set.
    """
    if not key_hash or not isinstance(key_hash, str):
        raise ValueError("key_hash must be a non-empty string")
    record: dict = {
        "timestamp": _now_iso_utc(now),
        "key_hash": key_hash,
    }
    if reason is not None:
        cleaned = str(reason).strip()
        if cleaned:
            record["reason"] = cleaned[:REVOCATION_REASON_MAX_LENGTH]
    path = target if target is not None else revoked_path()
    line = json.dumps(record, sort_keys=False, default=str)
    with _revoked_write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return record


# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------


def reset_caches_for_tests() -> None:
    """Clear the in-process manifest + revocation caches."""
    global _manifest_cache, _manifest_mtime, _manifest_loaded_from
    global _revoked_cache, _revoked_mtime, _revoked_loaded_from
    with _manifest_lock:
        _manifest_cache = {}
        _manifest_mtime = None
        _manifest_loaded_from = None
    with _revoked_lock:
        _revoked_cache = set()
        _revoked_mtime = None
        _revoked_loaded_from = None
