"""
Phase 4.7 — Stripe billing integration.

Maps an **active Stripe subscription → premium API tier** without ever
storing card data, PAN, CVV, full names, emails, or any other
sensitive payment metadata. The only thing persisted locally is a
list of opaque API-key strings whose owners currently have an
active subscription.

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
from typing import Any, Callable, Optional

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
# Persistent premium-key cache — only stores opaque api-key strings
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: set[str] = set()
_cache_loaded_from: Optional[Path] = None


def _cache_path() -> Path:
    return Path(
        os.getenv(
            STRIPE_PREMIUM_CACHE_ENV_VAR, DEFAULT_STRIPE_PREMIUM_CACHE_PATH,
        )
    )


def _load_cache(path: Path) -> set[str]:
    """Load the persisted key set. Returns empty set on any failure."""
    try:
        if not path.exists():
            return set()
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.debug("billing.cache_load_error", path=str(path), error=str(exc))
        return set()
    if not isinstance(data, list):
        return set()
    return {str(k) for k in data if k}


def _save_cache(path: Path, keys: set[str]) -> None:
    """Persist the key set. Best-effort — any failure is logged and swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(sorted(keys))
        path.write_text(payload + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("billing.cache_save_error", path=str(path), error=str(exc))


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
            _cache.update(_load_cache(path))
            _cache_loaded_from = path


def reset_cache_for_tests() -> None:
    """Test helper — clears both the in-memory set and the 'loaded from' marker."""
    global _cache_loaded_from
    with _cache_lock:
        _cache.clear()
        _cache_loaded_from = None


def add_premium_key(api_key: str) -> None:
    """Add ``api_key`` to the persistent premium set."""
    if not api_key:
        return
    _ensure_cache_loaded()
    path = _cache_path()
    with _cache_lock:
        _cache.add(str(api_key))
        _save_cache(path, set(_cache))


def remove_premium_key(api_key: str) -> None:
    """Remove ``api_key`` from the persistent premium set. Idempotent."""
    if not api_key:
        return
    _ensure_cache_loaded()
    path = _cache_path()
    with _cache_lock:
        _cache.discard(str(api_key))
        _save_cache(path, set(_cache))


def current_premium_keys() -> set[str]:
    """
    Read-only snapshot of the currently-cached premium key set.
    Returns a copy — callers can mutate freely without affecting state.
    """
    _ensure_cache_loaded()
    with _cache_lock:
        return set(_cache)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_stripe_configured() -> bool:
    """
    True iff the minimum env vars for Stripe-primary mode are present.

    Detection is based on ``STRIPE_API_KEY`` because that is the
    single value required for signed outbound Stripe calls. Without
    it we fall back to the Phase 4.5 env-var premium-keys list.
    """
    return bool((os.getenv(STRIPE_API_KEY_ENV_VAR, "") or "").strip())


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
    the local premium-key cache. Empty / None input → False.

    This function performs no network I/O: the cache is updated only
    by the Stripe webhook handler, so it reflects whatever events
    Stripe has already delivered.
    """
    if not api_key:
        return False
    _ensure_cache_loaded()
    with _cache_lock:
        return str(api_key) in _cache


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
    """
    if not isinstance(obj, dict):
        return None
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        key = meta.get("api_key")
        if key:
            return str(key)
    customer = obj.get("customer")
    if isinstance(customer, dict):
        cmeta = customer.get("metadata")
        if isinstance(cmeta, dict):
            key = cmeta.get("api_key")
            if key:
                return str(key)
    return None


def handle_webhook_event(event) -> dict:
    """
    Dispatch a parsed Stripe event to the cache.

    Recognized event types:
      - ``customer.subscription.created`` — add api_key iff status
        is ``active`` or ``trialing``.
      - ``customer.subscription.deleted`` — remove api_key.
      - ``invoice.payment_failed`` — remove api_key immediately
        (fail-closed on billing failures).

    Returns a small diagnostics dict — never raises, never echoes
    the api_key back to the caller.
    """
    if not isinstance(event, dict):
        return {"action": "ignored", "reason": "not_a_dict"}
    event_type = str(event.get("type") or "")
    data = event.get("data") or {}
    obj = data.get("object") if isinstance(data, dict) else None

    api_key = _extract_api_key_from_event_object(obj)
    if not api_key:
        return {
            "type": event_type,
            "action": "ignored",
            "reason": "no_api_key_on_event",
        }

    if event_type == "customer.subscription.created":
        status = (isinstance(obj, dict) and str(obj.get("status") or "")).lower()
        if status in _ACTIVE_SUBSCRIPTION_STATUSES:
            add_premium_key(api_key)
            return {"type": event_type, "action": "added"}
        return {
            "type": event_type,
            "action": "ignored",
            "reason": f"status={status or 'unknown'}",
        }

    if event_type == "customer.subscription.deleted":
        remove_premium_key(api_key)
        return {"type": event_type, "action": "removed"}

    if event_type == "invoice.payment_failed":
        remove_premium_key(api_key)
        return {
            "type": event_type,
            "action": "removed",
            "reason": "payment_failed",
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
