"""
Phase 6.2 — Manifest-backed API key authentication.

Pure-stdlib helper module that lets the live API server authenticate
keys issued by ``python -m trading_bot.api.keys issue`` without ever
storing the raw key. The presented bearer token is hashed with
``SHA-256(api_key)[:32]`` and looked up in the issuance manifest;
revoked hashes are rejected up-front.

This module deliberately:
  * hashes presented keys with the same SHA-256[:32] every other
    Phase 4/5/6 module uses (server / billing / conversion / growth /
    upgrade_events / share_events / smoke / keys), so the join column
    is byte-identical across every JSONL log;
  * never persists raw API keys (they're only hashed on the way in);
  * never persists raw labels;
  * never opens the network;
  * tolerates missing files, malformed JSONL rows, and concurrent
    writers — every load path returns "no entries" on error rather
    than raising;
  * hot-reloads manifest/revocation files by path + mtime so a newly
    appended row takes effect on the next request without a restart.
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
    """SHA-256[:32] — byte-identical to every other Phase 4/5/6 hasher.

    Joining the manifest, revocation log, growth log, upgrade-events
    log, conversion log, audit log, and usage log on ``key_hash``
    requires every writer and every reader to produce the SAME 32-char
    digest. The truncated form predates this module; the legacy
    ``legacy_hash_api_key`` alias is kept for any caller that is
    explicit about wanting the truncated form.
    """
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def legacy_hash_api_key(api_key: Optional[str]) -> str:
    """Alias for ``hash_api_key`` retained for explicit callers."""
    return hash_api_key(api_key)


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

    Hashes the presented key with the canonical SHA-256[:32] form,
    rejects it if revoked, otherwise returns the manifest entry (or
    ``None`` if no manifest row matches).
    """
    if not api_key:
        return None

    presented = hash_api_key(api_key)
    if not presented:
        return None
    if is_revoked(presented):
        return None
    return lookup_key_hash(presented)


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
