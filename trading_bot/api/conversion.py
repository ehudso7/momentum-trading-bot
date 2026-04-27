"""
Phase 4.9 — Free → Paid conversion tracking.

Records a minimal, privacy-preserving JSONL event every time an
API key transitions from free to premium via Stripe. Combined with
the Phase 4.6 per-key usage log, this gives us enough to compute a
real conversion rate:

    free users (usage log, tier="free")
        → premium users (usage log, tier="premium")
            → conversion events (conversion log)

Privacy posture:
  * The raw API key is **never** stored. Every record carries only
    ``SHA-256(api_key)[:32]`` — the same hash scheme used by
    ``trading_bot.api.server._hash_api_key`` and
    ``trading_bot.api.billing._hash_api_key`` — so a hash in this
    log matches a hash in the usage log for the same customer.
  * Card data, PAN, CVV, email, name — none of this is ever read
    here; the only input is the opaque API key plus the Stripe
    ``price_id`` (which identifies the product, not the customer).

Failure posture:
  * All I/O is best-effort. A disk failure at write time never
    breaks the Stripe webhook — the caller logs a diagnostic
    breadcrumb and moves on.
  * Thread-safe writes via a module-level lock.
  * Dedup by ``api_key_hash``: re-deliveries of the same Stripe
    event, re-subscriptions, or plan changes never double-count
    the same customer as a fresh conversion.

Env vars:
    TRADING_API_CONVERSION_LOG_PATH   where to append JSONL records
                                      (default: data/api_conversions.jsonl)
    TRADING_API_USAGE_LOG_PATH        path scanned for first-seen lookup
                                      (default: data/api_usage.jsonl)
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Env-var constants
# ---------------------------------------------------------------------------

CONVERSION_LOG_ENV_VAR = "TRADING_API_CONVERSION_LOG_PATH"
DEFAULT_CONVERSION_LOG_PATH = "data/api_conversions.jsonl"
USAGE_LOG_ENV_VAR = "TRADING_API_USAGE_LOG_PATH"
DEFAULT_USAGE_LOG_PATH = "data/api_usage.jsonl"

CONVERSION_EVENT = "converted"
DEFAULT_CONVERSION_SOURCE = "stripe"


# ---------------------------------------------------------------------------
# Module state — dedup set + path cache pointer.
# ---------------------------------------------------------------------------

_converted_hashes_cache: set[str] = set()
_converted_cache_loaded_from: Optional[Path] = None
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: Optional[str]) -> str:
    """
    SHA-256[:32] of ``api_key``. Must produce the same digest as
    ``trading_bot.api.server._hash_api_key`` so conversion-log hashes
    can be joined against usage-log hashes.
    """
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def _conversion_log_path() -> Path:
    return Path(
        os.getenv(CONVERSION_LOG_ENV_VAR, DEFAULT_CONVERSION_LOG_PATH)
    )


def _usage_log_path() -> Path:
    return Path(os.getenv(USAGE_LOG_ENV_VAR, DEFAULT_USAGE_LOG_PATH))


def _load_converted_hashes(path: Path) -> set[str]:
    """
    Scan an existing conversion-log JSONL and return the set of
    api_key_hash values already emitted with ``event == "converted"``.
    Never raises — unreadable / malformed lines are silently skipped.
    """
    if not path.exists():
        return set()
    converted: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("event") != CONVERSION_EVENT:
                continue
            hashed = rec.get("api_key_hash")
            if hashed:
                converted.add(str(hashed))
    except Exception as exc:
        log.debug("conversion.load_error", path=str(path), error=str(exc))
    return converted


def _ensure_cache_loaded() -> None:
    """
    Populate the in-memory dedup set from disk if the configured
    path changed since the last load. Thread-safe.
    """
    global _converted_cache_loaded_from
    path = _conversion_log_path()
    with _write_lock:
        if _converted_cache_loaded_from != path:
            _converted_hashes_cache.clear()
            _converted_hashes_cache.update(_load_converted_hashes(path))
            _converted_cache_loaded_from = path


def reset_cache_for_tests() -> None:
    """Test helper — drops the in-memory dedup set + path pointer."""
    global _converted_cache_loaded_from
    with _write_lock:
        _converted_hashes_cache.clear()
        _converted_cache_loaded_from = None


def find_first_seen_timestamp(
    api_key_hash: str,
    usage_log_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Scan the Phase 4.6 usage log for the earliest timestamp where
    ``key_hash == api_key_hash``. Returns the timestamp string or
    None. Never raises — a missing / malformed usage file simply
    yields None.
    """
    if not api_key_hash:
        return None
    path = usage_log_path if usage_log_path is not None else _usage_log_path()
    if not path.exists():
        return None
    earliest: Optional[str] = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("key_hash") != api_key_hash:
                continue
            ts = rec.get("timestamp")
            if ts is None:
                continue
            ts_str = str(ts)
            if earliest is None or ts_str < earliest:
                earliest = ts_str
    except Exception as exc:
        log.debug(
            "conversion.usage_scan_error", path=str(path), error=str(exc),
        )
    return earliest


def _append_conversion_record(
    record: dict, path: Optional[Path] = None,
) -> None:
    """
    Thread-safely append a single JSONL record. Best-effort: every
    failure path is caught + logged at DEBUG. Never raises.
    """
    target = path if path is not None else _conversion_log_path()
    try:
        line = json.dumps(record, sort_keys=False, default=str)
    except Exception as exc:
        log.debug("conversion.serialize_error", error=str(exc))
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        log.debug(
            "conversion.write_error", path=str(target), error=str(exc)
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_conversion(
    api_key: str,
    *,
    source: str = DEFAULT_CONVERSION_SOURCE,
    price_id: Optional[str] = None,
    first_seen_timestamp: Optional[str] = None,
    usage_log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Record a free → premium conversion for ``api_key``.

    Returns a small diagnostics dict:
      - ``{"action": "recorded", "api_key_hash": "<hex>"}``  — new row appended.
      - ``{"action": "deduped",  "api_key_hash": "<hex>"}``  — already converted.
      - ``{"action": "skipped",  "reason": "no_api_key"}``  — empty input.

    Dedup is by ``api_key_hash`` only: the same customer who
    re-subscribes later is counted as one conversion, matching the
    analytics question "did this API key ever convert?".

    Privacy:
      * Raw ``api_key`` is hashed immediately. It never enters the
        record, the file, the returned dict, the log stream, or any
        downstream reader.

    Failure:
      * All I/O is best-effort. A write failure leaves the in-memory
        dedup set untouched so a future attempt (e.g., next webhook)
        can retry.
    """
    if not api_key:
        return {"action": "skipped", "reason": "no_api_key"}

    api_key_hash = _hash_api_key(api_key)
    if not api_key_hash:
        return {"action": "skipped", "reason": "hash_failed"}

    return record_conversion_for_hash(
        api_key_hash,
        source=source,
        price_id=price_id,
        first_seen_timestamp=first_seen_timestamp,
        usage_log_path=usage_log_path,
        now=now,
    )


def record_conversion_for_hash(
    key_hash: str,
    *,
    source: str = DEFAULT_CONVERSION_SOURCE,
    price_id: Optional[str] = None,
    first_seen_timestamp: Optional[str] = None,
    usage_log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Phase 7.4 — record a conversion using the SHA-256 hash directly.

    Same return-value semantics as ``record_conversion``; the only
    difference is the input. Used by the Stripe webhook when
    ``metadata[key_hash]`` arrives without a raw api_key (Phase 7.3
    ``POST /billing/checkout`` flow).

    The raw API key is never required by this code path — by
    construction, only the hash is in scope.
    """
    if not key_hash or not isinstance(key_hash, str):
        return {"action": "skipped", "reason": "no_api_key"}
    api_key_hash = key_hash.strip()
    if not api_key_hash:
        return {"action": "skipped", "reason": "no_api_key"}

    _ensure_cache_loaded()

    # Atomic check-and-reserve: under a single lock, either we see the
    # hash already present (→ deduped) or we insert it and commit to
    # writing. This prevents two concurrent callers both deciding to
    # write and producing a duplicate record.
    with _write_lock:
        if api_key_hash in _converted_hashes_cache:
            return {"action": "deduped", "api_key_hash": api_key_hash}
        _converted_hashes_cache.add(api_key_hash)

    # Best-effort first-seen lookup from usage log — never blocks
    # the conversion record if the lookup fails.
    if first_seen_timestamp is None:
        try:
            first_seen_timestamp = find_first_seen_timestamp(
                api_key_hash, usage_log_path=usage_log_path,
            )
        except Exception as exc:
            log.debug("conversion.first_seen_error", error=str(exc))
            first_seen_timestamp = None

    timestamp = (now or datetime.now(timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    record = {
        "timestamp": timestamp,
        "api_key_hash": api_key_hash,
        "event": CONVERSION_EVENT,
        "source": str(source or "unknown"),
        "price_id": price_id,
        "first_seen_timestamp": first_seen_timestamp,
    }
    # Best-effort append. If it fails, the hash is already reserved
    # in the in-memory cache — a future call will see it as deduped.
    # A process restart will rebuild the cache from disk accurately,
    # so a failed write just means we miss ONE conversion row, not
    # that we accidentally record the same user twice.
    _append_conversion_record(record)

    return {"action": "recorded", "api_key_hash": api_key_hash}
