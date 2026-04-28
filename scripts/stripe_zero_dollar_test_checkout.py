"""
Operator-only helper: provision (or reuse) a $0 / month recurring
Stripe price for safe end-to-end checkout testing in **test mode**.

Why this exists
---------------
The live Stripe code path (``trading_bot.api.billing``) maps an
active Stripe subscription onto our premium tier. To prove that the
checkout → webhook → entitlement → cancellation pipeline works end-
to-end against Stripe — including the new preferred ``STRIPE_SECRET_KEY``
env-var name, the Phase 6.1 ``ref_code`` metadata pass-through, and
the Phase 4.7 webhook signature verifier — we need a real Stripe
product + price to point ``STRIPE_PREMIUM_PRICE_ID`` at. A $0 monthly
price lets us do that without ever charging a real card.

This script is **operator-only**:

  * It never mutates Railway / production state — it prints the exact
    commands the operator should run.
  * It refuses to run against a Stripe live secret unless the operator
    explicitly passes ``--allow-live`` (we want a clear two-key
    handshake before any chance of touching real money).
  * It NEVER prints the Stripe secret. The ``railway variables --set``
    snippets shown at the end use the placeholder ``<your-sk_test_…>``
    so an operator pasting the output into chat / a ticket cannot leak
    the secret.
  * It performs idempotent provisioning — running it twice is safe
    and reuses the existing product + price by name.

Usage
-----

    export STRIPE_SECRET_KEY=sk_test_...
    python scripts/stripe_zero_dollar_test_checkout.py

    # Print the Railway env-var commands (still no secret in stdout):
    python scripts/stripe_zero_dollar_test_checkout.py --print-railway-commands

    # Customise the product name / currency / interval:
    python scripts/stripe_zero_dollar_test_checkout.py \\
        --product-name "Momentum Trading Premium Test" \\
        --currency usd \\
        --interval month

    # If you REALLY know what you're doing and want to run against a
    # live Stripe key (we still won't create a non-zero price):
    python scripts/stripe_zero_dollar_test_checkout.py --allow-live

Exit codes
----------
    0  success — product + price provisioned (or reused)
    2  argument validation failure / config missing /
       live-key guard tripped
    3  Stripe REST API failure
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Callable, Optional

# We import the resolver from the live billing module so the
# preferred / legacy env-var precedence is identical to what the
# server does at request time. No other coupling to billing — this
# script does not import or run Core trading code.
from trading_bot.api.billing import (
    STRIPE_API_KEY_ENV_VAR,
    STRIPE_PREMIUM_PRICE_ID_ENV_VAR,
    STRIPE_PRICE_ID_PREMIUM_ENV_VAR,
    STRIPE_SECRET_KEY_ENV_VAR,
    _resolve_stripe_secret,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PRODUCT_NAME = "Momentum Trading Premium Test"
DEFAULT_CURRENCY = "usd"
DEFAULT_INTERVAL = "month"
VALID_INTERVALS: frozenset[str] = frozenset(
    {"day", "week", "month", "year"},
)

STRIPE_API_BASE_URL = "https://api.stripe.com/v1"
STRIPE_API_TIMEOUT_SECONDS = 15.0

# A type alias for the HTTP poster signature. Tests inject a stub
# instead of monkey-patching ``requests``; production calls fall
# through to the lazy ``_default_request`` poster below.
HttpRequester = Callable[..., dict]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ZeroDollarTestError(RuntimeError):
    """Operator-facing error. Never echoes the Stripe secret back."""


# ---------------------------------------------------------------------------
# HTTP layer (test-injectable)
# ---------------------------------------------------------------------------


def _default_request(
    *,
    method: str,
    url: str,
    auth: tuple[str, str],
    timeout: float,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
) -> dict:
    """
    Real implementation. Uses the existing ``requests`` dependency
    to keep this script SDK-free (consistent with the live billing
    module, which also avoids the Stripe SDK).
    """
    import requests  # local import — only when we're actually shipping

    method_upper = method.upper()
    try:
        if method_upper == "GET":
            response = requests.get(
                url, params=params, auth=auth, timeout=timeout,
            )
        elif method_upper == "POST":
            response = requests.post(
                url, data=data, auth=auth, timeout=timeout,
            )
        else:
            raise ZeroDollarTestError(
                f"unsupported HTTP method: {method!r}"
            )
    except Exception as exc:
        raise ZeroDollarTestError(
            f"stripe request failed: {type(exc).__name__}"
        ) from exc

    status = int(getattr(response, "status_code", 0) or 0)
    if not (200 <= status < 300):
        try:
            body_preview = getattr(response, "text", "")[:300]
        except Exception:
            body_preview = ""
        raise ZeroDollarTestError(
            f"stripe returned HTTP {status}: {body_preview}"
        )
    try:
        return response.json()
    except Exception as exc:
        raise ZeroDollarTestError(
            f"stripe returned non-JSON body: {type(exc).__name__}"
        ) from exc


# ---------------------------------------------------------------------------
# Core operations — pure, testable
# ---------------------------------------------------------------------------


def _is_live_secret(secret: str) -> bool:
    """A Stripe live secret begins with ``sk_live_``."""
    return secret.startswith("sk_live_")


def _is_test_secret(secret: str) -> bool:
    """Stripe test secrets begin with ``sk_test_``."""
    return secret.startswith("sk_test_")


def _validate_secret(secret: str, *, allow_live: bool) -> None:
    """
    Raise ``ZeroDollarTestError`` if the secret looks like a live
    key and the operator has not explicitly passed ``--allow-live``.

    Empty / unrecognised prefixes are also rejected — a Stripe
    secret should always start with ``sk_test_`` or ``sk_live_``.
    """
    if not secret:
        raise ZeroDollarTestError(
            f"no Stripe secret configured "
            f"(set {STRIPE_SECRET_KEY_ENV_VAR} or fallback "
            f"{STRIPE_API_KEY_ENV_VAR})"
        )
    if _is_live_secret(secret):
        if not allow_live:
            raise ZeroDollarTestError(
                "refusing to run against a Stripe LIVE secret "
                "(starts with 'sk_live_'). Re-run with --allow-live "
                "if you really mean to."
            )
        return
    if not _is_test_secret(secret):
        raise ZeroDollarTestError(
            "Stripe secret must start with 'sk_test_' (preferred) "
            "or 'sk_live_' (--allow-live). Got an unrecognised "
            "prefix."
        )


def find_existing_product(
    *,
    secret: str,
    name: str,
    requester: HttpRequester,
) -> Optional[dict]:
    """
    Search Stripe for an active product with this exact name.
    Returns the product dict on hit, ``None`` on miss. Stripe's
    list endpoint paginates; we accept a single page (100 max) —
    operators using this script for repeated test runs aren't
    going to accumulate hundreds of products.
    """
    payload = requester(
        method="GET",
        url=f"{STRIPE_API_BASE_URL}/products",
        auth=(secret, ""),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
        params={"active": "true", "limit": "100"},
    )
    for prod in (payload.get("data") or []):
        if isinstance(prod, dict) and prod.get("name") == name:
            return prod
    return None


def create_product(
    *,
    secret: str,
    name: str,
    requester: HttpRequester,
) -> dict:
    payload = requester(
        method="POST",
        url=f"{STRIPE_API_BASE_URL}/products",
        auth=(secret, ""),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
        data={
            "name": name,
            "description": (
                "Test-mode SKU used by "
                "scripts/stripe_zero_dollar_test_checkout.py to "
                "exercise the checkout → webhook → premium "
                "entitlement path without charging a card."
            ),
        },
    )
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ZeroDollarTestError(
            "stripe POST /products returned no id"
        )
    return payload


def find_existing_zero_price(
    *,
    secret: str,
    product_id: str,
    currency: str,
    interval: str,
    requester: HttpRequester,
) -> Optional[dict]:
    """
    Look for an active recurring $0 price on this product. We match
    on (product, currency, interval, unit_amount=0) so re-running
    with a different interval/currency creates a fresh price.
    """
    payload = requester(
        method="GET",
        url=f"{STRIPE_API_BASE_URL}/prices",
        auth=(secret, ""),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
        params={
            "product": product_id,
            "active": "true",
            "limit": "100",
        },
    )
    for price in (payload.get("data") or []):
        if not isinstance(price, dict):
            continue
        # Note the explicit ``is None`` — ``0 or default`` would
        # discard a legitimate $0 price.
        unit_amount = price.get("unit_amount")
        if unit_amount is None or int(unit_amount) != 0:
            continue
        if str(price.get("currency") or "").lower() != currency.lower():
            continue
        recurring = price.get("recurring")
        if not isinstance(recurring, dict):
            continue
        if str(recurring.get("interval") or "").lower() != interval.lower():
            continue
        return price
    return None


def create_zero_price(
    *,
    secret: str,
    product_id: str,
    currency: str,
    interval: str,
    requester: HttpRequester,
) -> dict:
    payload = requester(
        method="POST",
        url=f"{STRIPE_API_BASE_URL}/prices",
        auth=(secret, ""),
        timeout=STRIPE_API_TIMEOUT_SECONDS,
        data={
            "product": product_id,
            "currency": currency,
            "unit_amount": "0",
            "recurring[interval]": interval,
            # ``nickname`` is operator-visible in the Stripe
            # dashboard. We include the interval so dashboard
            # readers can tell at a glance which test SKU this is.
            "nickname": f"Zero-dollar test ({interval}ly)",
        },
    )
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ZeroDollarTestError(
            "stripe POST /prices returned no id"
        )
    return payload


def ensure_zero_price(
    *,
    secret: str,
    product_name: str,
    currency: str,
    interval: str,
    requester: HttpRequester,
) -> dict:
    """
    Idempotently provision the (product, $0 price) pair. Returns
    a dict with ``product_id``, ``price_id``, ``product_reused``,
    and ``price_reused`` flags so the caller can render an
    accurate operator log line.
    """
    product = find_existing_product(
        secret=secret, name=product_name, requester=requester,
    )
    product_reused = product is not None
    if product is None:
        product = create_product(
            secret=secret, name=product_name, requester=requester,
        )
    product_id = str(product["id"])

    price = find_existing_zero_price(
        secret=secret,
        product_id=product_id,
        currency=currency,
        interval=interval,
        requester=requester,
    )
    price_reused = price is not None
    if price is None:
        price = create_zero_price(
            secret=secret,
            product_id=product_id,
            currency=currency,
            interval=interval,
            requester=requester,
        )
    price_id = str(price["id"])

    return {
        "product_id": product_id,
        "price_id": price_id,
        "product_reused": product_reused,
        "price_reused": price_reused,
        "product_name": product_name,
        "currency": currency,
        "interval": interval,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_railway_commands(price_id: str) -> str:
    """
    The operator-facing snippet at the end of a successful run.

    NOTE: we deliberately use the placeholder ``<your-sk_test_…>``
    instead of the resolved Stripe secret. An operator copy-pasting
    this snippet into a ticket / chat / pull request body cannot
    leak their secret through this code path.

    Both legacy and preferred env-var commands are emitted because
    the live server reads either; setting both keeps any deployed
    code path that still references the legacy name working.
    """
    lines = [
        "# Run from a shell with the railway CLI logged in:",
        "# (replace <your-sk_test_...> with your actual test secret)",
        "",
        f"railway variables --set {STRIPE_SECRET_KEY_ENV_VAR}=<your-sk_test_...>",
        f"railway variables --set {STRIPE_API_KEY_ENV_VAR}=<your-sk_test_...>",
        f"railway variables --set {STRIPE_PREMIUM_PRICE_ID_ENV_VAR}={price_id}",
        f"railway variables --set {STRIPE_PRICE_ID_PREMIUM_ENV_VAR}={price_id}",
        "",
        "# Don't forget the webhook secret (test-mode endpoint):",
        "# railway variables --set STRIPE_WEBHOOK_SECRET=whsec_...",
    ]
    return "\n".join(lines)


def _format_provisioning_summary(result: dict) -> str:
    lines = [
        "Stripe test product + price ready:",
        f"  product_id   : {result['product_id']}",
        f"  price_id     : {result['price_id']}",
        f"  product_name : {result['product_name']}",
        f"  currency     : {result['currency']}",
        f"  interval     : {result['interval']}",
        f"  product_reused: {bool(result['product_reused'])}",
        f"  price_reused : {bool(result['price_reused'])}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/stripe_zero_dollar_test_checkout.py",
        description=(
            "Operator-only Stripe test-mode helper. Idempotently "
            "provisions a $0 monthly recurring price on a test "
            "product so the operator can validate the checkout → "
            "webhook → premium entitlement → cancellation flow "
            "without charging a real card. Refuses to run against "
            "a Stripe live secret unless --allow-live is given."
        ),
    )
    parser.add_argument(
        "--product-name", default=DEFAULT_PRODUCT_NAME,
        help=f"Stripe product name (default: {DEFAULT_PRODUCT_NAME!r}).",
    )
    parser.add_argument(
        "--currency", default=DEFAULT_CURRENCY,
        help=f"ISO currency code (default: {DEFAULT_CURRENCY!r}).",
    )
    parser.add_argument(
        "--interval", default=DEFAULT_INTERVAL,
        choices=sorted(VALID_INTERVALS),
        help=f"Recurring interval (default: {DEFAULT_INTERVAL!r}).",
    )
    parser.add_argument(
        "--allow-live", action="store_true",
        help=(
            "Permit running against a Stripe LIVE secret "
            "(sk_live_…). The script still creates a $0 price; "
            "it does NOT charge anyone. But touching live mode is "
            "a deliberate operator-handshake, so this flag is "
            "required."
        ),
    )
    parser.add_argument(
        "--print-railway-commands", action="store_true",
        help=(
            "Also print the exact railway CLI commands the "
            "operator should run to wire the new test price into "
            "the deployed server. The Stripe secret is never "
            "embedded in the printed commands — a placeholder is "
            "used so the snippet is safe to share."
        ),
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    requester: Optional[HttpRequester] = None,
    secret_resolver: Optional[Callable[[], str]] = None,
    out: Optional[Any] = None,
    err: Optional[Any] = None,
) -> int:
    """
    Entry point. Tests inject ``requester`` (a stub HTTP poster) and
    ``secret_resolver`` (returns the Stripe secret) so they can
    exercise every code path without monkeypatching ``requests`` or
    ``os.environ``. Production callers pass nothing and we fall
    through to the real implementations.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    resolver = secret_resolver if secret_resolver is not None else _resolve_stripe_secret
    secret = resolver() or ""

    try:
        _validate_secret(secret, allow_live=bool(args.allow_live))
    except ZeroDollarTestError as exc:
        print(f"error: {exc}", file=err)
        return 2

    requester = requester if requester is not None else _default_request

    try:
        result = ensure_zero_price(
            secret=secret,
            product_name=args.product_name,
            currency=args.currency.lower(),
            interval=args.interval.lower(),
            requester=requester,
        )
    except ZeroDollarTestError as exc:
        print(f"error: {exc}", file=err)
        return 3

    print(_format_provisioning_summary(result), file=out)
    if args.print_railway_commands:
        print("", file=out)
        print(_format_railway_commands(result["price_id"]), file=out)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
