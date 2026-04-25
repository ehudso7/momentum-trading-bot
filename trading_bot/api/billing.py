"""
Phase 4.7 + Phase 7.0 — Stripe billing integration.

Maps an **active Stripe subscription → premium API tier** without ever
storing card data, PAN, CVV, full names, emails, or any other
sensitive payment metadata.

Phase 7.0 hardens the persistence model: the local premium cache
now stores **only SHA-256[:32] hashes** of API keys, never the raw
values. The webhook handler additionally requires the subscription's
``metadata[api_key]`` to already be present in the issuance manifest
before any cache mutation — a webhook with an unissued key is
rejected without side effects. Legacy caches that still contain raw
keys are transparently migrated to the hash-only format on the next
load.

This module is pure integration: it neither imports nor is imported
by any Core trading module (scanner / strategy / risk / execution /
portfolio / alpha). The only mutation it performs is
add/remove on a local set and the corresponding JSON cache file.

No new pip dependency required — Stripe's documented webhook
signature scheme (v1, HMAC-SHA256 of `<timestamp>.<raw_payload>`) is
implemented manually with stdlib ``hmac`` + ``hashlib``.

Env vars consumed:
    STRIPE_API_KEY                     — presence toggles Stripe-primary mode
    STRIPE_WEBHOOK_SECRET              — HMAC secret for signature verification
    STRIPE_PRICE_ID_PREMIUM            — premium price id (informational only)
    TRADING_STRIPE_PREMIUM_CACHE_PATH  — path to the persistent JSON cache
                                         (default: data/stripe_premium_keys.json)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Env-var constants
# ---------------------------------------------------------------------------

STRIPE_API_KEY_ENV_VAR = "STRIPE_API_KEY"
STRIPE_WEBHOOK_SECRET_ENV_VAR = "STRIPE_WEBHOOK_SECRET"
STRIPE_PRICE_ID_PREMIUM_ENV_VAR = "STRIPE_PRICE_ID_PREMIUM"
STRIPE_PREMIUM_CACHE_ENV_VAR = "TRADING_STRIPE_PREMIUM_CACHE_PATH"
DEFAULT_STRIPE_PREMIUM_CACHE_PATH = "data/stripe_premium_keys.json"

# Reject webhook payloads whose signed timestamp is more than this
# many seconds away from now (replay protection). Matches Stripe's
# default of 5 minutes.
DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300

# Set statuses that qualify a subscription as "active" for our gate.
_ACTIVE_SUBSCRIPTION_STATUSES: frozenset[str] = frozenset({"active", "trialing"})


# ---------------------------------------------------------------------------
# Persistent premium-hash cache (Phase 7.0)
# ---------------------------------------------------------------------------
#
# Historically this module persisted raw api_key strings (Phase 4.7).
# Phase 7.0 moves the cache to SHA-256[:32] hashes only — the raw key
# is hashed on the way in and never touches disk. The migration is
# seamless: legacy cache files whose entries look like raw keys are
# re-hashed on load and the file is re-saved in the new format.

_cache_lock = threading.Lock()
# In-memory set of SHA-256[:32] hashes of premium-subscriber api_keys.
_cache: set[str] = set()
_cache_loaded_from: Optional[Path] = None

# A 32-char lowercase hex string is the canonical hash shape.
_HASH_CHARS = frozenset("0123456789abcdef")


def _looks_like_hash(value: str) -> bool:
    """True iff ``value`` is a 32-char lowercase hex string."""
    if len(value) != 32:
        return False
    return all(c in _HASH_CHARS for c in value.lower())


def _normalize_cache_entry(value: object) -> str:
    """
    Phase 7.0 migration helper.

    32-char hex → treat as already-hashed.
    Anything else → treat as a legacy raw api_key and hash it.

    Empty / None → "".
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if _looks_like_hash(s):
        return s.lower()
    return _hash_api_key(s)


def _cache_path() -> Path:
    return Path(
        os.getenv(
            STRIPE_PREMIUM_CACHE_ENV_VAR, DEFAULT_STRIPE_PREMIUM_CACHE_PATH,
        )
    )


def _read_raw_cache_entries(path: Path) -> Optional[list]:
    """
    Return the raw JSON list exactly as it appears on disk, or
    ``None`` on any failure (missing file, bad JSON, wrong shape).
    ``None`` is distinguishable from an empty-but-valid cache.
    """
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug("billing.cache_load_error", path=str(path), error=str(exc))
        return None
    if not isinstance(data, list):
        return None
    return data


def _save_cache(path: Path, hashes: set[str]) -> None:
    """Persist the hash set. Best-effort — any failure is logged and swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(sorted(hashes))
        path.write_text(payload + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("billing.cache_save_error", path=str(path), error=str(exc))


def _load_and_migrate_cache(path: Path) -> set[str]:
    """
    Load the cache file, migrating any legacy raw-key entries to the
    Phase 7.0 hash-only format. If the migration changed any entry,
    the file is re-saved before we return.
    """
    raw = _read_raw_cache_entries(path)
    if raw is None:
        return set()

    hashes: set[str] = set()
    needs_migration = False
    for v in raw:
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        if _looks_like_hash(s):
            hashes.add(s.lower())
        else:
            # Legacy raw api_key entry — hash it on the fly.
            hashed = _hash_api_key(s)
            if hashed:
                hashes.add(hashed)
            needs_migration = True

    if needs_migration:
        _save_cache(path, hashes)
        log.info(
            "billing.cache_migrated_to_hashes",
            path=str(path),
            entries=len(hashes),
        )
    return hashes


def _ensure_cache_loaded() -> None:
    """
    Reload the in-memory cache iff the configured path has changed
    since the last load. Thread-safe.
    """
    global _cache_loaded_from
    path = _cache_path()
    with _cache_lock:
        if _cache_loaded_from != path:
            _cache.clear()
            _cache.update(_load_and_migrate_cache(path))
            _cache_loaded_from = path


def reset_cache_for_tests() -> None:
    """Test helper — clears both the in-memory set and the 'loaded from' marker."""
    global _cache_loaded_from
    with _cache_lock:
        _cache.clear()
        _cache_loaded_from = None


def add_premium_key(api_key: str) -> None:
    """
    Add ``api_key`` to the persistent premium set.

    The raw ``api_key`` is hashed in-process and only the hash
    (SHA-256[:32]) is persisted. The raw value never reaches disk.
    """
    if not api_key:
        return
    key_hash = _hash_api_key(api_key)
    if not key_hash:
        return
    add_premium_hash(key_hash)


def add_premium_hash(key_hash: str) -> None:
    """
    Phase 7.4 — add ``key_hash`` directly to the persistent premium set.

    Used by the Stripe webhook when an event carries
    ``metadata[key_hash]`` (the Phase 7.3 ``POST /billing/checkout``
    flow), so the raw API key is never required for promotion.

    The cache file already holds hashes only (Phase 7.0); this helper
    just bypasses the redundant hash step that ``add_premium_key``
    performs internally.
    """
    if not key_hash or not isinstance(key_hash, str):
        return
    h = key_hash.strip()
    if not h:
        return
    _ensure_cache_loaded()
    path = _cache_path()
    with _cache_lock:
        _cache.add(h)
        _save_cache(path, set(_cache))


def remove_premium_key(api_key: str) -> None:
    """
    Remove ``api_key`` from the persistent premium set. Idempotent.

    Like ``add_premium_key``, the raw value is hashed in-process
    before any disk operation.
    """
    if not api_key:
        return
    key_hash = _hash_api_key(api_key)
    if not key_hash:
        return
    remove_premium_hash(key_hash)


def remove_premium_hash(key_hash: str) -> None:
    """
    Phase 7.4 — remove ``key_hash`` directly from the persistent
    premium set. Idempotent. Used by the Stripe webhook when
    ``customer.subscription.deleted`` / ``invoice.payment_failed``
    arrives with a ``metadata[key_hash]`` field (Phase 7.3 flow).
    """
    if not key_hash or not isinstance(key_hash, str):
        return
    h = key_hash.strip()
    if not h:
        return
    _ensure_cache_loaded()
    path = _cache_path()
    with _cache_lock:
        _cache.discard(h)
        _save_cache(path, set(_cache))


def current_premium_key_hashes() -> set[str]:
    """
    Read-only snapshot of the currently-cached premium-hash set.

    Phase 7.0 renamed the public API from ``current_premium_keys``
    (which historically returned raw keys) to
    ``current_premium_key_hashes`` so callers cannot accidentally
    treat the return value as raw keys. The old name is preserved as
    a deprecated alias that returns the same hash set.
    """
    _ensure_cache_loaded()
    with _cache_lock:
        return set(_cache)


# Backwards-compat alias. The name suggests raw keys, but Phase 7.0
# guarantees only hashes ever live in the set. New code should call
# ``current_premium_key_hashes`` for clarity.
def current_premium_keys() -> set[str]:  # noqa: D401 — alias
    return current_premium_key_hashes()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_stripe_configured() -> bool:
    """
    True iff the minimum env vars for Stripe-primary mode are present.

    Detection accepts EITHER the legacy ``STRIPE_API_KEY`` (Phase
    4.7) or the Phase 7.3 preferred ``STRIPE_SECRET_KEY`` — both
    name the same Stripe-side credential. Without either, we fall
    back to the Phase 4.5 env-var premium-keys list.
    """
    legacy = (os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip()
    if legacy:
        return True
    # Phase 7.3 / 7.4 — STRIPE_SECRET_KEY is the preferred name.
    # Read by literal so this function stays callable before the
    # constant is defined further down the module.
    return bool((os.getenv("STRIPE_SECRET_KEY", "") or "").strip())


def _hash_api_key(api_key: Optional[str]) -> str:
    """
    Anonymize an API key — first 32 hex chars of SHA-256(api_key).

    Duplicated (with the same implementation) in
    ``trading_bot.api.server`` so the billing module can stay free
    of any import from the server module. Empty / None → "".
    """
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def is_premium_via_stripe(api_key: Optional[str]) -> bool:
    """
    Return True iff ``api_key`` has an active subscription recorded in
    the local premium-hash cache. Empty / None input → False.

    Phase 7.0: the cache holds only SHA-256[:32] hashes, so the
    presented ``api_key`` is hashed in-process before any lookup.
    This function performs no network I/O: the cache is updated only
    by the Stripe webhook handler, so it reflects whatever events
    Stripe has already delivered.
    """
    if not api_key:
        return False
    key_hash = _hash_api_key(api_key)
    if not key_hash:
        return False
    _ensure_cache_loaded()
    with _cache_lock:
        return key_hash in _cache


def is_premium_hash(key_hash: Optional[str]) -> bool:
    """
    Hash-input variant of ``is_premium_via_stripe`` for callers that
    already computed the SHA-256[:32] hash. Avoids re-hashing on the
    request hot path.
    """
    if not key_hash or not isinstance(key_hash, str):
        return False
    _ensure_cache_loaded()
    with _cache_lock:
        return key_hash in _cache


# ---------------------------------------------------------------------------
# Webhook signature verification — Stripe v1 scheme, implemented manually.
# ---------------------------------------------------------------------------


def _parse_signature_header(header: str) -> dict[str, list[str]]:
    """
    Parse a Stripe ``Stripe-Signature`` header of the form::

        t=1234567890,v1=abcdef...,v1=fedcba...

    Returns a dict mapping key → list of values. Unknown / malformed
    items are skipped, not raised.
    """
    parts: dict[str, list[str]] = {}
    for item in (header or "").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key.strip(), []).append(value.strip())
    return parts


def verify_webhook_signature(
    payload: bytes,
    sig_header: Optional[str],
    secret: Optional[str],
    *,
    tolerance: int = DEFAULT_SIGNATURE_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> bool:
    """
    Verify a Stripe webhook signature against the raw request body.

    Implements Stripe's documented manual verification scheme:
      1. Parse the ``Stripe-Signature`` header for ``t=`` and ``v1=``.
      2. Reject if the timestamp is outside the tolerance window.
      3. Compute ``HMAC-SHA256(secret, f"{timestamp}.{payload}")`` and
         compare to any of the provided ``v1=`` values using
         ``hmac.compare_digest``.

    Returns True on match, False on any failure (missing secret,
    missing header, bad HMAC, stale timestamp, malformed parts, etc.).
    Never raises.
    """
    if not secret or not sig_header:
        return False
    if payload is None:
        return False
    try:
        parts = _parse_signature_header(sig_header)
        timestamps = parts.get("t") or []
        signatures = parts.get("v1") or []
        if not timestamps or not signatures:
            return False
        try:
            ts = int(timestamps[0])
        except (TypeError, ValueError):
            return False
        current = int(now) if now is not None else int(time.time())
        if abs(current - ts) > max(0, int(tolerance)):
            return False
        signed_payload = f"{ts}.".encode("utf-8") + (
            payload if isinstance(payload, (bytes, bytearray))
            else str(payload).encode("utf-8")
        )
        expected = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        return any(
            hmac.compare_digest(expected, sig) for sig in signatures
        )
    except Exception as exc:
        log.debug("billing.signature_verify_error", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


def _extract_api_key_from_event_object(obj) -> Optional[str]:
    """
    Pull ``api_key`` out of the inner event object's metadata.

    Preferred location: ``object.metadata.api_key`` (subscription
    metadata — set by the operator at checkout). Fallback location:
    ``object.customer.metadata.api_key`` when the customer is
    expanded by Stripe on the webhook. Customers referenced by plain
    string ID cannot be resolved without a Stripe API call, which
    this module deliberately does not make.

    Backward-compat surface preserved across Phase 7.4 — new code
    should call ``_extract_identity_from_event_object`` to also
    retrieve the ``key_hash`` field.
    """
    api_key, _ = _extract_identity_from_event_object(obj)
    return api_key


def _extract_identity_from_event_object(
    obj,
) -> tuple[Optional[str], Optional[str]]:
    """
    Phase 7.4 — pull both ``api_key`` and ``key_hash`` out of the
    inner event object's metadata. Returns ``(api_key, key_hash)``.

    Both top-level ``object.metadata.<field>`` and the defensive
    ``object.customer.metadata.<field>`` fallback are checked. Either
    or both may be present:

      * Phase 4.7 / 4.8 operator-CLI checkout sends
        ``metadata[api_key]`` (raw key).
      * Phase 7.3 ``POST /billing/checkout`` sends
        ``metadata[key_hash]`` (hash only) and deliberately omits
        ``metadata[api_key]``.

    The webhook handler prefers ``key_hash`` when both happen to be
    present (no raw key needed).
    """
    if not isinstance(obj, dict):
        return (None, None)

    api_key: Optional[str] = None
    key_hash: Optional[str] = None

    meta = obj.get("metadata")
    if isinstance(meta, dict):
        ak = meta.get("api_key")
        if ak:
            api_key = str(ak)
        kh = meta.get("key_hash")
        if kh:
            key_hash = str(kh)

    customer = obj.get("customer")
    if isinstance(customer, dict):
        cmeta = customer.get("metadata")
        if isinstance(cmeta, dict):
            if api_key is None:
                ak = cmeta.get("api_key")
                if ak:
                    api_key = str(ak)
            if key_hash is None:
                kh = cmeta.get("key_hash")
                if kh:
                    key_hash = str(kh)

    return (api_key, key_hash)


def _extract_price_id_from_event_object(obj) -> Optional[str]:
    """
    Pull the ``price_id`` out of a Stripe subscription event object.

    Stripe's subscription object nests the price under
    ``items.data[0].price.id``. We walk defensively because the
    webhook delivery shape varies slightly between API versions.
    Returns None on any miss — the conversion log accepts a null
    ``price_id``.
    """
    if not isinstance(obj, dict):
        return None
    items = obj.get("items")
    if isinstance(items, dict):
        data = items.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                price = first.get("price")
                if isinstance(price, dict):
                    pid = price.get("id")
                    if pid:
                        return str(pid)
                # Stripe API versions where `price` is a bare id string.
                if isinstance(price, str) and price:
                    return price
    # Some Stripe payloads expose ``plan.id`` at the subscription level
    # for legacy compatibility — use it as a last-resort fallback.
    plan = obj.get("plan")
    if isinstance(plan, dict):
        pid = plan.get("id")
        if pid:
            return str(pid)
    return None


def _verify_against_manifest(api_key: str) -> bool:
    """
    Phase 7.0 — return True iff ``api_key`` hashes to a row that
    exists in the issuance manifest AND has not been revoked.

    Lazy-import of ``trading_bot.api.key_store`` so a Stripe-only
    deployment that has not yet pointed ``TRADING_API_KEYS_MANIFEST_PATH``
    at a valid file still loads this module without error. A missing
    / empty manifest correctly returns False here — unissued keys
    must not be promoted to premium by a Stripe webhook.
    """
    try:
        from trading_bot.api import key_store  # noqa: WPS433
    except Exception as exc:
        log.debug("billing.key_store_import_error", error=str(exc))
        return False
    return key_store.verify_api_key(api_key) is not None


def _verify_hash_against_manifest(key_hash: str) -> bool:
    """
    Phase 7.4 — same gate as ``_verify_against_manifest``, but takes
    a hash directly. True iff the hash is in the issuance manifest
    AND has not been revoked.
    """
    if not key_hash or not isinstance(key_hash, str):
        return False
    h = key_hash.strip()
    if not h:
        return False
    try:
        from trading_bot.api import key_store  # noqa: WPS433
    except Exception as exc:
        log.debug("billing.key_store_import_error", error=str(exc))
        return False
    if key_store.lookup_key_hash(h) is None:
        return False
    if key_store.is_revoked(h):
        return False
    return True


def handle_webhook_event(event) -> dict:
    """
    Dispatch a parsed Stripe event to the cache.

    Recognized event types:
      - ``customer.subscription.created`` — promote to premium when
        ``status`` is ``active``/``trialing`` AND identity verifies
        against the issuance manifest. Identity is taken from
        ``metadata[key_hash]`` (Phase 7.3 hash-only path) when
        present, falling back to ``metadata[api_key]`` (Phase 4.7
        legacy raw-key path) otherwise.
      - ``customer.subscription.deleted`` — remove premium for the
        identity. No manifest check — deletions always flow through
        so a cancelled subscription immediately loses access even
        if the corresponding manifest row was later deleted.
      - ``invoice.payment_failed`` — remove premium immediately
        (fail-closed on billing failures). No manifest check.

    Returns a small diagnostics dict — never raises, never echoes
    the api_key back to the caller. Phase 7.4 also surfaces the
    identity source as ``"identity": "key_hash" | "api_key"`` so
    operators can see which path the event flowed through.
    """
    if not isinstance(event, dict):
        return {"action": "ignored", "reason": "not_a_dict"}
    event_type = str(event.get("type") or "")
    data = event.get("data") or {}
    obj = data.get("object") if isinstance(data, dict) else None

    api_key, key_hash = _extract_identity_from_event_object(obj)
    if not api_key and not key_hash:
        return {
            "type": event_type,
            "action": "ignored",
            "reason": "no_identity_on_event",
        }
    # Phase 7.4 — prefer the hash path when available so the legacy
    # raw-key flow only runs when we genuinely have no hash. The two
    # sources should always agree; the preference order matches the
    # privacy posture (no raw key needed).
    use_hash = bool(key_hash)
    identity_source = "key_hash" if use_hash else "api_key"

    if event_type == "customer.subscription.created":
        status = (
            isinstance(obj, dict) and str(obj.get("status") or "")
        ).lower()
        if status not in _ACTIVE_SUBSCRIPTION_STATUSES:
            return {
                "type": event_type,
                "action": "ignored",
                "reason": f"status={status or 'unknown'}",
            }

        # Phase 7.0 / 7.4 — manifest verification gate. The hash
        # path uses the supplied hash directly; the legacy api_key
        # path hashes-then-verifies.
        if use_hash:
            if not _verify_hash_against_manifest(key_hash):  # type: ignore[arg-type]
                return {
                    "type": event_type,
                    "action": "ignored",
                    "reason": "key_not_in_manifest_or_revoked",
                    "identity": identity_source,
                }
            add_premium_hash(key_hash)  # type: ignore[arg-type]
        else:
            if not _verify_against_manifest(api_key):  # type: ignore[arg-type]
                return {
                    "type": event_type,
                    "action": "ignored",
                    "reason": "key_not_in_manifest_or_revoked",
                    "identity": identity_source,
                }
            add_premium_key(api_key)  # type: ignore[arg-type]

        # Phase 4.9 — fire-and-forget free→premium conversion
        # tracking. Any failure is caught so the webhook still
        # returns a success action to the caller. The hash path
        # uses the new Phase 7.4 conversion entry point so the raw
        # key is never required.
        try:
            from trading_bot.api.conversion import (
                record_conversion,
                record_conversion_for_hash,
            )
            if use_hash:
                record_conversion_for_hash(
                    key_hash,  # type: ignore[arg-type]
                    source="stripe",
                    price_id=_extract_price_id_from_event_object(obj),
                )
            else:
                record_conversion(
                    api_key,  # type: ignore[arg-type]
                    source="stripe",
                    price_id=_extract_price_id_from_event_object(obj),
                )
        except Exception as exc:
            log.debug("billing.conversion_error", error=str(exc))
        return {
            "type": event_type,
            "action": "added",
            "identity": identity_source,
        }

    if event_type == "customer.subscription.deleted":
        if use_hash:
            remove_premium_hash(key_hash)  # type: ignore[arg-type]
        else:
            remove_premium_key(api_key)  # type: ignore[arg-type]
        return {
            "type": event_type,
            "action": "removed",
            "identity": identity_source,
        }

    if event_type == "invoice.payment_failed":
        if use_hash:
            remove_premium_hash(key_hash)  # type: ignore[arg-type]
        else:
            remove_premium_key(api_key)  # type: ignore[arg-type]
        return {
            "type": event_type,
            "action": "removed",
            "reason": "payment_failed",
            "identity": identity_source,
        }

    return {
        "type": event_type,
        "action": "ignored",
        "reason": "unhandled_type",
    }


# ---------------------------------------------------------------------------
# Phase 4.8 — operator-only Stripe Checkout link generation
# ---------------------------------------------------------------------------


STRIPE_API_BASE_URL = "https://api.stripe.com/v1"
STRIPE_API_TIMEOUT_SECONDS = 10.0

# HTTP post signature for dependency injection. Takes (url, data,
# auth, timeout) kwargs and returns the parsed JSON response dict.
# Real implementation uses ``requests.post``; tests inject a stub.
HttpPoster = Callable[..., dict]


class BillingConfigError(RuntimeError):
    """
    Raised when an operation that requires Stripe configuration is
    attempted while the relevant env vars are missing. Message is
    operator-safe — never echoes the caller-supplied API key back.
    """


class BillingAPIError(RuntimeError):
    """Raised when Stripe's REST API returns a non-2xx response."""


def _post_to_stripe(
    *, url: str, data: dict, auth: tuple[str, str], timeout: float,
) -> dict:
    """
    Default HTTP poster. Uses ``requests`` (already a transitive
    dependency via the broader project) rather than adding the
    full Stripe SDK. Returns the parsed JSON body on 2xx, raises
    ``BillingAPIError`` otherwise. Exposed as a module-level
    function so tests can monkey-patch it.
    """
    import requests

    try:
        response = requests.post(
            url, data=data, auth=auth, timeout=timeout,
        )
    except Exception as exc:
        raise BillingAPIError(
            f"stripe request failed: {type(exc).__name__}"
        ) from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not (200 <= status_code < 300):
        # Stripe's error body may contain caller-supplied data; log
        # a short prefix at DEBUG and surface only the status code.
        try:
            body_preview = getattr(response, "text", "")[:200]
        except Exception:
            body_preview = ""
        log.debug(
            "billing.stripe_non_2xx",
            status_code=status_code,
            body_preview=body_preview,
        )
        raise BillingAPIError(f"stripe returned HTTP {status_code}")
    try:
        return response.json()
    except Exception as exc:
        raise BillingAPIError(
            f"stripe response not valid JSON: {type(exc).__name__}"
        ) from exc


def _validate_checkout_inputs(
    api_key: str, success_url: str, cancel_url: str,
) -> None:
    if not api_key or not str(api_key).strip():
        raise ValueError("api_key is required")
    if not success_url or not str(success_url).strip():
        raise ValueError("success_url is required")
    if not cancel_url or not str(cancel_url).strip():
        raise ValueError("cancel_url is required")
    # Prevent obvious header-injection / newline shenanigans in the
    # URLs we forward to Stripe's API.
    for name, value in (("success_url", success_url), ("cancel_url", cancel_url)):
        if any(ch in value for ch in ("\n", "\r", "\t", "\x00")):
            raise ValueError(
                f"{name} contains forbidden whitespace / control characters"
            )


def create_checkout_session(
    api_key: str,
    success_url: str,
    cancel_url: str,
    *,
    ref_code: Optional[str] = None,
    http_post: Optional[HttpPoster] = None,
) -> dict:
    """
    Create a Stripe Checkout session that upgrades ``api_key`` to
    the premium subscription and returns a URL the operator can
    forward to the customer.

    Returns a small, caller-safe dict::

        {
            "checkout_session_id": "cs_...",
            "checkout_url":        "https://checkout.stripe.com/...",
            "api_key_hash":        "<32-hex SHA-256 prefix>",
        }

    The raw ``api_key`` is **never** returned, printed, or logged by
    this function — only the hashed form. Callers (CLI, scripts)
    must continue that hygiene.

    Phase 6.1 — when ``ref_code`` is provided, it is added to the
    Stripe Checkout session's ``metadata[ref_code]`` and
    ``subscription_data[metadata][ref_code]``. The Phase 4.7
    webhook handler still keys on ``metadata[api_key]`` for the
    actual premium-cache update; ``ref_code`` is purely for
    downstream attribution analysis. Empty / None ref_code is a
    no-op (the field is simply not added to the POST body).

    Fail-closed: raises ``BillingConfigError`` when
    ``STRIPE_API_KEY`` or ``STRIPE_PRICE_ID_PREMIUM`` are missing.
    The webhook secret is NOT required for this operation.
    """
    _validate_checkout_inputs(api_key, success_url, cancel_url)

    stripe_secret = (os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip()
    price_id = (os.getenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "") or "").strip()
    if not stripe_secret:
        raise BillingConfigError(
            f"{STRIPE_API_KEY_ENV_VAR} is not configured"
        )
    if not price_id:
        raise BillingConfigError(
            f"{STRIPE_PRICE_ID_PREMIUM_ENV_VAR} is not configured"
        )

    # Stripe accepts application/x-www-form-urlencoded with bracketed
    # field names for nested structures.
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        # Mirror the api_key onto BOTH session-level and
        # subscription-level metadata so the webhook handler can pick
        # it up even when the customer isn't expanded by Stripe.
        "metadata[api_key]": api_key,
        "subscription_data[metadata][api_key]": api_key,
        "customer_creation": "always",
    }
    # Phase 6.1 — operator-supplied ref_code arrives here already
    # sanitised by the caller (keys.issue_key uses the Phase 5.1
    # growth sanitiser). We still string-coerce defensively.
    if ref_code:
        ref_str = str(ref_code).strip()
        if ref_str:
            data["metadata[ref_code]"] = ref_str
            data["subscription_data[metadata][ref_code]"] = ref_str

    poster = http_post if http_post is not None else _post_to_stripe
    payload = poster(
        url=f"{STRIPE_API_BASE_URL}/checkout/sessions",
        data=data,
        auth=(stripe_secret, ""),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
    )
    if not isinstance(payload, dict):
        raise BillingAPIError("stripe returned non-object JSON")

    session_id = str(payload.get("id") or "")
    checkout_url = str(payload.get("url") or "")
    if not session_id or not checkout_url:
        raise BillingAPIError(
            "stripe response missing 'id' or 'url' field"
        )

    return {
        "checkout_session_id": session_id,
        "checkout_url": checkout_url,
        "api_key_hash": _hash_api_key(api_key),
    }


# ---------------------------------------------------------------------------
# Phase 7.3 — end-user Stripe Checkout (hash-only, request-driven)
# ---------------------------------------------------------------------------
#
# The Phase 4.8 ``create_checkout_session`` above is operator-only and
# sends the raw ``api_key`` to Stripe metadata so the existing
# Phase 4.7 webhook can resolve subscription.created back to the right
# customer. That posture predates Phase 6.2's hash-only auth path.
#
# Phase 7.3 adds an end-user-facing Checkout creator that NEVER puts
# the raw key into Stripe metadata. Identity flows through ``key_hash``
# (the same SHA-256 prefix that the auth path looks up). The webhook
# handler in ``handle_webhook_event`` will be extended to accept
# ``metadata[key_hash]`` alongside the legacy ``metadata[api_key]``
# in a future phase; for now, the Phase 7.3 endpoint relies on the
# Stripe cache being populated via the existing webhook contract.
#
# Env vars (preferred names new in Phase 7.3, legacy fallbacks kept):
#
#   STRIPE_SECRET_KEY           — required (falls back to STRIPE_API_KEY)
#   STRIPE_PREMIUM_PRICE_ID     — required (falls back to
#                                  STRIPE_PRICE_ID_PREMIUM)
#

STRIPE_SECRET_KEY_ENV_VAR = "STRIPE_SECRET_KEY"
STRIPE_PREMIUM_PRICE_ID_ENV_VAR = "STRIPE_PREMIUM_PRICE_ID"


def _resolve_stripe_secret() -> str:
    """STRIPE_SECRET_KEY (preferred) → STRIPE_API_KEY (legacy)."""
    primary = (os.getenv(STRIPE_SECRET_KEY_ENV_VAR, "") or "").strip()
    if primary:
        return primary
    return (os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip()


def _resolve_premium_price_id() -> str:
    """STRIPE_PREMIUM_PRICE_ID (preferred) → STRIPE_PRICE_ID_PREMIUM (legacy)."""
    primary = (
        os.getenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "") or ""
    ).strip()
    if primary:
        return primary
    return (
        os.getenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "") or ""
    ).strip()


def create_checkout_session_for_hash(
    *,
    key_hash: str,
    success_url: str,
    cancel_url: str,
    http_post: Optional[HttpPoster] = None,
) -> dict:
    """
    Create a Stripe Checkout session for the supplied ``key_hash``.

    The raw API key is NEVER touched by this function — the caller
    must hash the presented key first. Stripe metadata carries only
    the hash, so even a Stripe-side leak cannot expose plaintext
    keys.

    Returns a small caller-safe dict::

        {
            "checkout_session_id": "cs_...",
            "checkout_url":        "https://checkout.stripe.com/...",
            "key_hash":            "<as supplied>",
            "tier_to":             "premium",
        }

    Fail-closed: raises ``BillingConfigError`` when the secret key
    or the premium price id are unset; raises ``BillingAPIError``
    on any non-2xx Stripe response or malformed payload.
    """
    if not key_hash or not isinstance(key_hash, str) or not key_hash.strip():
        raise ValueError("key_hash is required")
    if not success_url or not str(success_url).strip():
        raise ValueError("success_url is required")
    if not cancel_url or not str(cancel_url).strip():
        raise ValueError("cancel_url is required")
    for name, value in (
        ("success_url", success_url), ("cancel_url", cancel_url),
    ):
        if any(ch in value for ch in ("\n", "\r", "\t", "\x00")):
            raise ValueError(
                f"{name} contains forbidden whitespace / control characters"
            )

    stripe_secret = _resolve_stripe_secret()
    price_id = _resolve_premium_price_id()
    if not stripe_secret:
        raise BillingConfigError(
            f"{STRIPE_SECRET_KEY_ENV_VAR} is not configured"
        )
    if not price_id:
        raise BillingConfigError(
            f"{STRIPE_PREMIUM_PRICE_ID_ENV_VAR} is not configured"
        )

    key_hash = key_hash.strip()
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        # Stripe-level identity is the HASH, never the raw key.
        "client_reference_id": key_hash,
        "metadata[key_hash]": key_hash,
        "metadata[tier_from]": "free",
        "metadata[tier_to]": "premium",
        "subscription_data[metadata][key_hash]": key_hash,
        "subscription_data[metadata][tier_to]": "premium",
        "customer_creation": "always",
    }

    poster = http_post if http_post is not None else _post_to_stripe
    payload = poster(
        url=f"{STRIPE_API_BASE_URL}/checkout/sessions",
        data=data,
        auth=(stripe_secret, ""),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
    )
    if not isinstance(payload, dict):
        raise BillingAPIError("stripe returned non-object JSON")

    session_id = str(payload.get("id") or "")
    checkout_url = str(payload.get("url") or "")
    if not session_id or not checkout_url:
        raise BillingAPIError(
            "stripe response missing 'id' or 'url' field"
        )
    return {
        "checkout_session_id": session_id,
        "checkout_url": checkout_url,
        "key_hash": key_hash,
        "tier_to": "premium",
    }


# ---------------------------------------------------------------------------
# CLI — operator-only invocation. Never prints the raw API key.
# ---------------------------------------------------------------------------


def _checkout_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.billing checkout",
        description=(
            "Generate a Stripe Checkout URL for upgrading an API key "
            "to the premium tier. Operator-only — the raw API key is "
            "never printed to stdout."
        ),
    )
    parser.add_argument("--api-key", required=True,
                        help="API key to promote on successful checkout")
    parser.add_argument("--success-url", required=True,
                        help="Absolute URL for Stripe to redirect to on success")
    parser.add_argument("--cancel-url", required=True,
                        help="Absolute URL for Stripe to redirect to on cancel")
    args = parser.parse_args(argv)

    try:
        result = create_checkout_session(
            args.api_key, args.success_url, args.cancel_url,
        )
    except BillingConfigError as exc:
        # Operator-facing message; never echo the api key back.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BillingAPIError as exc:
        print(f"error: stripe: {exc}", file=sys.stderr)
        return 3

    # Print only caller-safe fields. The raw api_key is NOT in
    # `result`; it's impossible to leak it via this code path.
    print(f"checkout_url:         {result['checkout_url']}")
    print(f"checkout_session_id:  {result['checkout_session_id']}")
    print(f"api_key_hash:         {result['api_key_hash']}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.billing",
        description="Operator-only billing helpers.",
    )
    subparsers = parser.add_subparsers(dest="command")
    # `checkout` is parsed by its own sub-handler; list it here so
    # `--help` at top level documents it.
    subparsers.add_parser(
        "checkout",
        help="Generate a Stripe Checkout URL for an API key",
        add_help=False,
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        _build_parser().print_help(sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "checkout":
        return _checkout_cli(rest)
    if command in ("-h", "--help"):
        _build_parser().print_help()
        return 0
    print(f"error: unknown command '{command}'", file=sys.stderr)
    print("available commands: checkout", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
