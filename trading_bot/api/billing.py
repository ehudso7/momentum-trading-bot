"""
Phase 4.7 + Phase 7.0 — Stripe billing integration.

Maps an active Stripe subscription → premium API tier without ever
storing card data, PAN, CVV, full names, emails, or sensitive payment metadata.
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


STRIPE_API_KEY_ENV_VAR = "STRIPE_API_KEY"
STRIPE_WEBHOOK_SECRET_ENV_VAR = "STRIPE_WEBHOOK_SECRET"
STRIPE_PRICE_ID_PREMIUM_ENV_VAR = "STRIPE_PRICE_ID_PREMIUM"
STRIPE_PREMIUM_CACHE_ENV_VAR = "TRADING_STRIPE_PREMIUM_CACHE_PATH"
DEFAULT_STRIPE_PREMIUM_CACHE_PATH = "data/stripe_premium_keys.json"

DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300
_ACTIVE_SUBSCRIPTION_STATUSES: frozenset[str] = frozenset({"active", "trialing"})

_cache_lock = threading.Lock()
_cache: set[str] = set()
_cache_loaded_from: Optional[Path] = None

_processed_event_lock = threading.Lock()
_processed_event_ids: set[str] = set()

_HASH_CHARS = frozenset("0123456789abcdef")


def _looks_like_hash(value: str) -> bool:
    if len(value) != 32:
        return False
    return all(c in _HASH_CHARS for c in value.lower())


def _normalize_cache_entry(value: object) -> str:
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
            STRIPE_PREMIUM_CACHE_ENV_VAR,
            DEFAULT_STRIPE_PREMIUM_CACHE_PATH,
        )
    )


def _read_raw_cache_entries(path: Path) -> Optional[list]:
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(sorted(hashes))
        path.write_text(payload + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("billing.cache_save_error", path=str(path), error=str(exc))


def _load_and_migrate_cache(path: Path) -> set[str]:
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
    global _cache_loaded_from
    path = _cache_path()
    with _cache_lock:
        if _cache_loaded_from != path:
            _cache.clear()
            _cache.update(_load_and_migrate_cache(path))
            _cache_loaded_from = path


def reset_cache_for_tests() -> None:
    global _cache_loaded_from
    with _cache_lock:
        _cache.clear()
        _cache_loaded_from = None
    with _processed_event_lock:
        _processed_event_ids.clear()


def add_premium_key(api_key: str) -> None:
    if not api_key:
        return
    key_hash = _hash_api_key(api_key)
    if not key_hash:
        return
    add_premium_hash(key_hash)


def add_premium_hash(key_hash: str) -> None:
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
    if not api_key:
        return
    key_hash = _hash_api_key(api_key)
    if not key_hash:
        return
    remove_premium_hash(key_hash)


def remove_premium_hash(key_hash: str) -> None:
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
    _ensure_cache_loaded()
    with _cache_lock:
        return set(_cache)


def current_premium_keys() -> set[str]:
    return current_premium_key_hashes()


def is_stripe_configured() -> bool:
    legacy = (os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip()
    if legacy:
        return True
    return bool((os.getenv("STRIPE_SECRET_KEY", "") or "").strip())


def _hash_api_key(api_key: Optional[str]) -> str:
    if not api_key:
        return ""
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:32]


def is_premium_via_stripe(api_key: Optional[str]) -> bool:
    if not api_key:
        return False
    key_hash = _hash_api_key(api_key)
    if not key_hash:
        return False
    return is_premium_hash(key_hash)


def is_premium_hash(key_hash: Optional[str]) -> bool:
    if not key_hash or not isinstance(key_hash, str):
        return False
    _ensure_cache_loaded()
    with _cache_lock:
        return key_hash.strip() in _cache


def _parse_signature_header(header: str) -> dict[str, list[str]]:
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
            payload
            if isinstance(payload, (bytes, bytearray))
            else str(payload).encode("utf-8")
        )

        expected = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        return any(hmac.compare_digest(expected, sig) for sig in signatures)
    except Exception as exc:
        log.debug("billing.signature_verify_error", error=str(exc))
        return False


def _extract_api_key_from_event_object(obj) -> Optional[str]:
    api_key, _ = _extract_identity_from_event_object(obj)
    return api_key


def _extract_identity_from_event_object(
    obj,
) -> tuple[Optional[str], Optional[str]]:
    """
    Extract API identity from Stripe event objects.

    Supported sources:
      1. object.metadata.api_key / object.metadata.key_hash
      2. object.client_reference_id as key_hash
      3. object.subscription_details.metadata.key_hash
      4. expanded object.customer.metadata.api_key / key_hash
      5. fetched customer metadata when object.customer is a Stripe customer id

    The fetch fallback matters because many webhook objects carry
    customer as a string id instead of an expanded customer object.
    """
    if not isinstance(obj, dict):
        return (None, None)

    api_key: Optional[str] = None
    key_hash: Optional[str] = None

    meta = obj.get("metadata")
    if isinstance(meta, dict):
        ak = meta.get("api_key")
        kh = meta.get("key_hash")
        if ak:
            api_key = str(ak)
        if kh:
            key_hash = str(kh)

    client_reference_id = obj.get("client_reference_id")
    if not key_hash and client_reference_id:
        candidate = str(client_reference_id).strip()
        if candidate:
            key_hash = candidate

    subscription_details = obj.get("subscription_details")
    if isinstance(subscription_details, dict):
        sd_meta = subscription_details.get("metadata")
        if isinstance(sd_meta, dict):
            if api_key is None:
                ak = sd_meta.get("api_key")
                if ak:
                    api_key = str(ak)
            if key_hash is None:
                kh = sd_meta.get("key_hash")
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

    if isinstance(customer, str) and customer.strip() and not (api_key or key_hash):
        fetched_api_key, fetched_key_hash = _fetch_identity_from_customer(
            customer.strip()
        )
        api_key = api_key or fetched_api_key
        key_hash = key_hash or fetched_key_hash

    return (api_key, key_hash)


def _fetch_identity_from_customer(
    customer_id: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort customer metadata fallback.

    This is intentionally non-fatal. Stripe webhook processing should
    never crash because customer lookup failed.
    """
    if not customer_id:
        return (None, None)

    secret = _resolve_stripe_secret()
    if not secret:
        return (None, None)

    try:
        import requests

        response = requests.get(
            f"{STRIPE_API_BASE_URL}/customers/{customer_id}",
            auth=(secret, ""),
            timeout=STRIPE_API_TIMEOUT_SECONDS,
        )
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return (None, None)
        payload = response.json()
        if not isinstance(payload, dict):
            return (None, None)
        meta = payload.get("metadata")
        if not isinstance(meta, dict):
            return (None, None)
        api_key = meta.get("api_key")
        key_hash = meta.get("key_hash")
        return (
            str(api_key) if api_key else None,
            str(key_hash) if key_hash else None,
        )
    except Exception as exc:
        log.debug(
            "billing.customer_identity_fetch_error",
            customer_id=customer_id,
            error=str(exc),
        )
        return (None, None)


def _extract_price_id_from_event_object(obj) -> Optional[str]:
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
                if isinstance(price, str) and price:
                    return price

    plan = obj.get("plan")
    if isinstance(plan, dict):
        pid = plan.get("id")
        if pid:
            return str(pid)

    return None


def _verify_against_manifest(api_key: str) -> bool:
    try:
        from trading_bot.api import key_store
    except Exception as exc:
        log.debug("billing.key_store_import_error", error=str(exc))
        return False

    return key_store.verify_api_key(api_key) is not None


def _verify_hash_against_manifest(key_hash: str) -> bool:
    if not key_hash or not isinstance(key_hash, str):
        return False

    h = key_hash.strip()
    if not h:
        return False

    try:
        from trading_bot.api import key_store
    except Exception as exc:
        log.debug("billing.key_store_import_error", error=str(exc))
        return False

    if key_store.lookup_key_hash(h) is None:
        return False

    if key_store.is_revoked(h):
        return False

    return True


def _stripe_event_id(event: dict) -> str:
    return str(event.get("id") or "").strip()


def _mark_event_processed(event_id: str) -> bool:
    if not event_id:
        return True

    with _processed_event_lock:
        if event_id in _processed_event_ids:
            return False
        _processed_event_ids.add(event_id)
        return True


def _identity_from_event_or_client_reference(obj) -> tuple[Optional[str], Optional[str]]:
    api_key, key_hash = _extract_identity_from_event_object(obj)
    if key_hash:
        key_hash = key_hash.strip()
    if api_key:
        api_key = api_key.strip()
    return (api_key or None, key_hash or None)


def handle_webhook_event(event) -> dict:
    if not isinstance(event, dict):
        return {"action": "ignored", "reason": "not_a_dict"}

    event_id = _stripe_event_id(event)
    event_type = str(event.get("type") or "")

    if event_id and not _mark_event_processed(event_id):
        return {
            "id": event_id,
            "type": event_type,
            "action": "ignored",
            "reason": "duplicate_event",
        }

    data = event.get("data") or {}
    obj = data.get("object") if isinstance(data, dict) else None

    api_key, key_hash = _identity_from_event_or_client_reference(obj)
    use_hash = bool(key_hash)
    identity_source = "key_hash" if use_hash else "api_key"

    if event_type == "checkout.session.completed":
        if not api_key and not key_hash:
            return {
                "id": event_id,
                "type": event_type,
                "action": "ignored",
                "reason": "no_identity_on_event",
            }

        if use_hash:
            if not _verify_hash_against_manifest(key_hash):  # type: ignore[arg-type]
                return {
                    "id": event_id,
                    "type": event_type,
                    "action": "ignored",
                    "reason": "key_not_in_manifest_or_revoked",
                    "identity": identity_source,
                }
            add_premium_hash(key_hash)  # type: ignore[arg-type]
            funnel_hash = key_hash
        else:
            if not _verify_against_manifest(api_key):  # type: ignore[arg-type]
                return {
                    "id": event_id,
                    "type": event_type,
                    "action": "ignored",
                    "reason": "key_not_in_manifest_or_revoked",
                    "identity": identity_source,
                }
            add_premium_key(api_key)  # type: ignore[arg-type]
            funnel_hash = _hash_api_key(api_key)

        _record_conversion_and_funnel(
            api_key=api_key,
            key_hash=key_hash,
            use_hash=use_hash,
            obj=obj,
            funnel_hash=funnel_hash,
        )

        return {
            "id": event_id,
            "type": event_type,
            "action": "added",
            "identity": identity_source,
        }

    if event_type == "customer.subscription.created":
        if not api_key and not key_hash:
            return {
                "id": event_id,
                "type": event_type,
                "action": "ignored",
                "reason": "no_identity_on_event",
            }

        sub_status = (
            isinstance(obj, dict) and str(obj.get("status") or "")
        ).lower()

        if sub_status not in _ACTIVE_SUBSCRIPTION_STATUSES:
            return {
                "id": event_id,
                "type": event_type,
                "action": "ignored",
                "reason": f"status={sub_status or 'unknown'}",
            }

        if use_hash:
            if not _verify_hash_against_manifest(key_hash):  # type: ignore[arg-type]
                return {
                    "id": event_id,
                    "type": event_type,
                    "action": "ignored",
                    "reason": "key_not_in_manifest_or_revoked",
                    "identity": identity_source,
                }
            add_premium_hash(key_hash)  # type: ignore[arg-type]
            funnel_hash = key_hash
        else:
            if not _verify_against_manifest(api_key):  # type: ignore[arg-type]
                return {
                    "id": event_id,
                    "type": event_type,
                    "action": "ignored",
                    "reason": "key_not_in_manifest_or_revoked",
                    "identity": identity_source,
                }
            add_premium_key(api_key)  # type: ignore[arg-type]
            funnel_hash = _hash_api_key(api_key)

        _record_conversion_and_funnel(
            api_key=api_key,
            key_hash=key_hash,
            use_hash=use_hash,
            obj=obj,
            funnel_hash=funnel_hash,
        )

        return {
            "id": event_id,
            "type": event_type,
            "action": "added",
            "identity": identity_source,
        }

    if event_type == "customer.subscription.deleted":
        if not api_key and not key_hash:
            return {
                "id": event_id,
                "type": event_type,
                "action": "ignored",
                "reason": "no_identity_on_event",
            }

        if use_hash:
            remove_premium_hash(key_hash)  # type: ignore[arg-type]
        else:
            remove_premium_key(api_key)  # type: ignore[arg-type]

        return {
            "id": event_id,
            "type": event_type,
            "action": "removed",
            "identity": identity_source,
        }

    if event_type == "invoice.payment_failed":
        if not api_key and not key_hash:
            return {
                "id": event_id,
                "type": event_type,
                "action": "ignored",
                "reason": "no_identity_on_event",
            }

        if use_hash:
            remove_premium_hash(key_hash)  # type: ignore[arg-type]
        else:
            remove_premium_key(api_key)  # type: ignore[arg-type]

        return {
            "id": event_id,
            "type": event_type,
            "action": "removed",
            "reason": "payment_failed",
            "identity": identity_source,
        }

    return {
        "id": event_id,
        "type": event_type,
        "action": "ignored",
        "reason": "unhandled_type",
    }


def _record_conversion_and_funnel(
    *,
    api_key: Optional[str],
    key_hash: Optional[str],
    use_hash: bool,
    obj,
    funnel_hash: Optional[str],
) -> None:
    try:
        from trading_bot.api.conversion import (
            record_conversion,
            record_conversion_for_hash,
        )

        if use_hash and key_hash:
            record_conversion_for_hash(
                key_hash,
                source="stripe",
                price_id=_extract_price_id_from_event_object(obj),
            )
        elif api_key:
            record_conversion(
                api_key,
                source="stripe",
                price_id=_extract_price_id_from_event_object(obj),
            )
    except Exception as exc:
        log.debug("billing.conversion_error", error=str(exc))

    try:
        from trading_bot.api.upgrade_events import (
            EVENT_UPGRADE_COMPLETED,
            record_upgrade_funnel_event,
        )

        if funnel_hash:
            record_upgrade_funnel_event(
                funnel_hash,
                EVENT_UPGRADE_COMPLETED,
                reason="stripe_webhook",
                endpoint="/webhook/stripe",
            )
    except Exception as exc:
        log.debug("billing.upgrade_funnel_error", error=str(exc))


STRIPE_API_BASE_URL = "https://api.stripe.com/v1"
STRIPE_API_TIMEOUT_SECONDS = 10.0

HttpPoster = Callable[..., dict]


class BillingConfigError(RuntimeError):
    pass


class BillingAPIError(RuntimeError):
    pass


def _post_to_stripe(
    *,
    url: str,
    data: dict,
    auth: tuple[str, str],
    timeout: float,
) -> dict:
    import requests

    try:
        response = requests.post(
            url,
            data=data,
            auth=auth,
            timeout=timeout,
        )
    except Exception as exc:
        raise BillingAPIError(
            f"stripe request failed: {type(exc).__name__}"
        ) from exc

    status_code = int(getattr(response, "status_code", 0) or 0)

    if not (200 <= status_code < 300):
        try:
            body_preview = getattr(response, "text", "")[:500]
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
    api_key: str,
    success_url: str,
    cancel_url: str,
) -> None:
    if not api_key or not str(api_key).strip():
        raise ValueError("api_key is required")
    if not success_url or not str(success_url).strip():
        raise ValueError("success_url is required")
    if not cancel_url or not str(cancel_url).strip():
        raise ValueError("cancel_url is required")

    for name, value in (
        ("success_url", success_url),
        ("cancel_url", cancel_url),
    ):
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
    _validate_checkout_inputs(api_key, success_url, cancel_url)

    stripe_secret = (os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip()
    price_id = (os.getenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "") or "").strip()

    if not stripe_secret:
        raise BillingConfigError(f"{STRIPE_API_KEY_ENV_VAR} is not configured")
    if not price_id:
        raise BillingConfigError(
            f"{STRIPE_PRICE_ID_PREMIUM_ENV_VAR} is not configured"
        )

    api_key_hash = _hash_api_key(api_key)

    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": api_key_hash,
        "metadata[api_key]": api_key,
        "metadata[key_hash]": api_key_hash,
        "subscription_data[metadata][api_key]": api_key,
        "subscription_data[metadata][key_hash]": api_key_hash,
    }

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
        raise BillingAPIError("stripe response missing 'id' or 'url' field")

    return {
        "checkout_session_id": session_id,
        "checkout_url": checkout_url,
        "api_key_hash": api_key_hash,
    }


STRIPE_SECRET_KEY_ENV_VAR = "STRIPE_SECRET_KEY"
STRIPE_PREMIUM_PRICE_ID_ENV_VAR = "STRIPE_PREMIUM_PRICE_ID"


def _resolve_stripe_secret() -> str:
    primary = (os.getenv(STRIPE_SECRET_KEY_ENV_VAR, "") or "").strip()
    if primary:
        return primary
    return (os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip()


def _resolve_premium_price_id() -> str:
    primary = (os.getenv(STRIPE_PREMIUM_PRICE_ID_ENV_VAR, "") or "").strip()
    if primary:
        return primary
    return (os.getenv(STRIPE_PRICE_ID_PREMIUM_ENV_VAR, "") or "").strip()


def create_checkout_session_for_hash(
    *,
    key_hash: str,
    success_url: str,
    cancel_url: str,
    http_post: Optional[HttpPoster] = None,
) -> dict:
    if not key_hash or not isinstance(key_hash, str) or not key_hash.strip():
        raise ValueError("key_hash is required")
    if not success_url or not str(success_url).strip():
        raise ValueError("success_url is required")
    if not cancel_url or not str(cancel_url).strip():
        raise ValueError("cancel_url is required")

    for name, value in (
        ("success_url", success_url),
        ("cancel_url", cancel_url),
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
        "client_reference_id": key_hash,
        "metadata[key_hash]": key_hash,
        "metadata[tier_from]": "free",
        "metadata[tier_to]": "premium",
        "subscription_data[metadata][key_hash]": key_hash,
        "subscription_data[metadata][tier_from]": "free",
        "subscription_data[metadata][tier_to]": "premium",
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
        raise BillingAPIError("stripe response missing 'id' or 'url' field")

    return {
        "checkout_session_id": session_id,
        "checkout_url": checkout_url,
        "key_hash": key_hash,
        "tier_to": "premium",
    }


def _checkout_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.billing checkout",
        description=(
            "Generate a Stripe Checkout URL for upgrading an API key "
            "to the premium tier. Operator-only — the raw API key is "
            "never printed to stdout."
        ),
    )
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--success-url", required=True)
    parser.add_argument("--cancel-url", required=True)

    args = parser.parse_args(argv)

    try:
        result = create_checkout_session(
            args.api_key,
            args.success_url,
            args.cancel_url,
        )
    except BillingConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BillingAPIError as exc:
        print(f"error: stripe: {exc}", file=sys.stderr)
        return 3

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


if __name__ == "__main__":
    raise SystemExit(main())
