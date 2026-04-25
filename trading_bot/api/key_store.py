"""
Phase 6.2 — Manifest-backed API key authentication.

Pure-stdlib helper module that lets the live API server authenticate
keys issued by ``python -m trading_bot.api.keys issue`` without ever
storing the raw key.

IMPORTANT:
The API key hash is the FULL SHA-256 hex digest.

Earlier docs/code referenced SHA-256(api_key)[:32]. That caused a
production mismatch because the issuer stored full 64-char hashes while
runtime auth looked up 32-char hashes.

This module now:
  * hashes presented keys with full SHA-256;
  * supports legacy 32-char revocation/manifest rows as fallback;
  * never persists raw API keys;
  * tolerates missing/malformed JSONL files;
  * hot-reloads manifest/revocation files by path + mtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional


KEYS_MANIFEST_ENV_VAR = "TRADING_API_KEYS_MANIFEST_PATH"
DEFAULT_KEYS_MANIFEST_PATH = "data/api_keys_manifest.jsonl"

KEYS_REVOKED_ENV_VAR = "TRADING_API_KEYS_REVOKED_PATH"
DEFAULT_KEYS_REVOKED_PATH = "data/api_keys_revoked.jsonl"

TIER_FREE = "free"
TIER_PREMIUM = "premium"
VALID_TIERS: frozenset[str] = frozenset({TIER_FREE, TIER_PREMIUM})

REVOCATION_REASON_MAX_LENGTH = 200


class ManifestEntry(NamedTuple):
    key_hash: str
    tier: str
    created_at: Optional[str]


def hash_api_key(api_key: Optional[str]) -> str:
    """Return FULL SHA-256 hex digest for an API key."""
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()


def legacy_hash_api_key(api_key: Optional[str]) -> str:
    """Legacy SHA-256[:32] hash retained for backward compatibility."""
    full = hash_api_key(api_key)
    return full[:32] if full else ""


def manifest_path() -> Path:
    return Path(os.getenv(KEYS_MANIFEST_ENV_VAR, DEFAULT_KEYS_MANIFEST_PATH))


def revoked_path() -> Path:
    return Path(os.getenv(KEYS_REVOKED_ENV_VAR, DEFAULT_KEYS_REVOKED_PATH))


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


_manifest_lock = threading.Lock()
_manifest_cache: dict[str, ManifestEntry] = {}
_manifest_mtime: Optional[float] = None
_manifest_loaded_from: Optional[Path] = None

_revoked_lock = threading.Lock()
_revoked_cache: set[str] = set()
_revoked_mtime: Optional[float] = None
_revoked_loaded_from: Optional[Path] = None

_revoked_write_lock = threading.Lock()


def _ensure_manifest_loaded() -> dict[str, ManifestEntry]:
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
    global _revoked_cache, _revoked_mtime, _revoked_loaded_from

    path = revoked_path()
    mtime = _file_mtime(path)

    with _revoked_lock:
        if path != _revoked_loaded_from or mtime != _revoked_mtime:
            _revoked_cache = _load_revoked_file(path)
            _revoked_mtime = mtime
            _revoked_loaded_from = path
        return set(_revoked_cache)


def lookup_key_hash(key_hash: str) -> Optional[ManifestEntry]:
    if not key_hash or not isinstance(key_hash, str):
        return None
    return _ensure_manifest_loaded().get(key_hash)


def is_revoked(key_hash: str) -> bool:
    if not key_hash or not isinstance(key_hash, str):
        return False
    return key_hash in _ensure_revoked_loaded()


def verify_api_key(api_key: Optional[str]) -> Optional[ManifestEntry]:
    """
    Validate a presented bearer token against the manifest.

    Lookup order:
    1. full SHA-256 hash
    2. legacy SHA-256[:32] hash fallback

    Revocation also checks both forms so older 32-char revocation rows
    still block keys after the full-hash fix.
    """
    if not api_key:
        return None

    full_hash = hash_api_key(api_key)
    short_hash = full_hash[:32] if full_hash else ""

    if not full_hash:
        return None

    if is_revoked(full_hash) or is_revoked(short_hash):
        return None

    entry = lookup_key_hash(full_hash)
    if entry is not None:
        return entry

    return lookup_key_hash(short_hash)


def has_active_keys() -> bool:
    return bool(_ensure_manifest_loaded())


def append_revocation(
    *,
    key_hash: str,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
    target: Optional[Path] = None,
) -> dict:
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


def reset_caches_for_tests() -> None:
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
