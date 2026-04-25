"""
Phase 6.0 — Operator Key Issuance CLI.

Operator-only command-line tool for generating free-tier API keys
and (optionally) the Stripe Checkout link that promotes them to
premium. There is **no** public sign-up endpoint for any of this:
the entire surface is invoked by hand from a deployment shell.

Operator workflow::

    # Free-tier key for an internal user.
    python -m trading_bot.api.keys issue \\
        --tier free \\
        --label "alice@example.com"

    # Premium key + Stripe Checkout URL to forward to the customer.
    python -m trading_bot.api.keys issue \\
        --tier premium \\
        --label "acme-corp" \\
        --checkout \\
        --success-url https://example.com/billing/success \\
        --cancel-url  https://example.com/billing/cancel

The raw ``api_key`` is printed to stdout **once** so the operator
can hand-deliver it to the user. It is never written to disk and
never echoed by any other code path.

Manifest schema (one JSONL row per issuance, append-only)::

    {
      "created_at":           "2026-04-24T12:00:00.000000Z",
      "key_hash":             "<SHA-256(api_key)[:32]>",
      "label_hash":           "<SHA-256(label)[:32]>",
      "tier":                 "free" | "premium",
      "checkout_session_id":  "cs_..." | null
    }

Privacy posture:

  * The raw ``api_key`` is **never** persisted (only the hash).
  * The raw ``label`` is **never** persisted (only the hash).
  * No customer email, IP, name, or payment field is ever stored.
  * The Stripe Checkout URL is **never** persisted — only the
    session id is. URLs are short-lived and contain redirect
    state we don't need to retain.
  * If Stripe fails when ``--checkout`` is requested, the manifest
    row is NOT written — the operator can retry cleanly. So a
    manifest row implies "issuance succeeded end-to-end".

Env vars:

    TRADING_API_KEYS_MANIFEST_PATH   JSONL output path
                                     (default:
                                     data/api_keys_manifest.jsonl)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KEYS_MANIFEST_ENV_VAR = "TRADING_API_KEYS_MANIFEST_PATH"
DEFAULT_KEYS_MANIFEST_PATH = "data/api_keys_manifest.jsonl"

TIER_FREE = "free"
TIER_PREMIUM = "premium"
VALID_TIERS: frozenset[str] = frozenset({TIER_FREE, TIER_PREMIUM})

# 32 bytes of randomness → ~43-char URL-safe base64 string. The
# spec asks for "32+ byte URL-safe API key"; ``token_urlsafe(32)``
# returns 43 characters that are all in ``[A-Za-z0-9\-_]``.
KEY_BYTES = 32

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: str) -> str:
    """SHA-256[:32]. Identical hash style across every Phase 4/5 log."""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _hash_label(label: str) -> str:
    """SHA-256[:32] of the operator-supplied label."""
    if not label:
        return ""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]


def _manifest_path() -> Path:
    return Path(
        os.getenv(KEYS_MANIFEST_ENV_VAR, DEFAULT_KEYS_MANIFEST_PATH),
    )


def _now_iso_utc(now: Optional[datetime] = None) -> str:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def generate_api_key() -> str:
    """
    Cryptographically random URL-safe API key.

    Returns 43 characters drawn from ``[A-Za-z0-9\\-_]`` (the
    standard URL-safe base64 alphabet, no padding) — that is 32
    bytes of randomness, well above the spec's 32-byte minimum.
    """
    return secrets.token_urlsafe(KEY_BYTES)


def _append_manifest_row(record: dict, target: Optional[Path] = None) -> None:
    """
    Thread-safely append a single JSONL row to the manifest.

    Failure surfaces as an exception so the CLI can print a clear
    operator-facing error and exit non-zero. (The other Phase 5.x
    loggers swallow failures because they're on the live request
    path; this is an offline operator command and the operator
    SHOULD know if the disk is full.)
    """
    path = target if target is not None else _manifest_path()
    line = json.dumps(record, sort_keys=False, default=str)
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


# Type alias for the optional checkout-caller injection — lets tests
# bypass the live Stripe HTTP call without monkeypatching billing.
CheckoutCaller = Callable[..., dict]


def issue_key(
    *,
    tier: str,
    label: str,
    with_checkout: bool = False,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    now: Optional[datetime] = None,
    manifest_path: Optional[Path] = None,
    checkout_caller: Optional[CheckoutCaller] = None,
    http_post: Optional[Any] = None,
) -> dict:
    """
    Generate one API key, optionally create a Stripe Checkout
    session, then append a manifest row.

    Returns a result dict::

        {
          "api_key":             "<raw-key — present ONCE>",
          "key_hash":            "<32-hex>",
          "tier":                "free" | "premium",
          "created_at":          "...Z",
          "label_hash":          "<32-hex>",
          "checkout_session_id": "cs_..." | None,
          "checkout_url":        "https://..." | None,
          "manifest_path":       "/abs/path/api_keys_manifest.jsonl",
        }

    Hard rules:

      * ``tier`` must be in ``VALID_TIERS``.
      * ``label`` must be a non-empty string after strip.
      * ``with_checkout`` requires ``tier == "premium"`` and BOTH
        ``success_url`` / ``cancel_url`` to be set.
      * If ``with_checkout`` raises (Stripe failure, missing env,
        etc.) the manifest row is NOT written — partial state is
        avoided so the operator can retry.

    The raw ``api_key`` is never written to the manifest. The raw
    ``label`` is never written to the manifest. The
    ``checkout_url`` is never written to the manifest.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"invalid tier {tier!r}; must be one of "
            f"{sorted(VALID_TIERS)}"
        )

    label_str = "" if label is None else str(label).strip()
    if not label_str:
        raise ValueError("label must be a non-empty string")

    if with_checkout:
        if tier != TIER_PREMIUM:
            raise ValueError(
                "--checkout requires --tier premium",
            )
        if not success_url or not cancel_url:
            raise ValueError(
                "--checkout requires both --success-url and "
                "--cancel-url",
            )

    api_key = generate_api_key()
    key_hash = _hash_api_key(api_key)
    label_hash = _hash_label(label_str)
    created_at = _now_iso_utc(now)

    checkout_session_id: Optional[str] = None
    checkout_url: Optional[str] = None
    if with_checkout:
        # Lazy-import to keep keys.py importable when the operator
        # hasn't installed the Stripe-related env vars yet (e.g.
        # they only ever issue free keys).
        if checkout_caller is None:
            from trading_bot.api.billing import (  # noqa: WPS433
                create_checkout_session,
            )
            checkout_caller = create_checkout_session

        kwargs: dict[str, Any] = {}
        if http_post is not None:
            kwargs["http_post"] = http_post
        result = checkout_caller(
            api_key, success_url, cancel_url, **kwargs,
        )
        if not isinstance(result, dict):
            raise RuntimeError("checkout caller returned non-dict")
        checkout_session_id = str(result.get("checkout_session_id") or "")
        checkout_url = str(result.get("checkout_url") or "")
        if not checkout_session_id or not checkout_url:
            raise RuntimeError(
                "checkout caller returned an empty session id or URL",
            )

    record = {
        "created_at": created_at,
        "key_hash": key_hash,
        "label_hash": label_hash,
        "tier": tier,
        "checkout_session_id": checkout_session_id,
    }
    target = manifest_path if manifest_path is not None else _manifest_path()
    _append_manifest_row(record, target=target)

    return {
        "api_key": api_key,
        "key_hash": key_hash,
        "tier": tier,
        "created_at": created_at,
        "label_hash": label_hash,
        "checkout_session_id": checkout_session_id,
        "checkout_url": checkout_url,
        "manifest_path": str(target),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_issue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys issue",
        description=(
            "Generate a free-tier or premium API key and append one "
            "row to the operator-only key manifest. The raw key is "
            "printed to stdout EXACTLY ONCE — record it now, it is "
            "never persisted in any form."
        ),
    )
    parser.add_argument(
        "--tier", required=True, choices=sorted(VALID_TIERS),
        help="Tier to issue (free | premium).",
    )
    parser.add_argument(
        "--label", required=True,
        help=(
            "Operator-facing label for this key (e.g. customer email "
            "or internal username). Hashed before storage; the raw "
            "string is never persisted."
        ),
    )
    parser.add_argument(
        "--checkout", action="store_true",
        help=(
            "Also create a Stripe Checkout session that promotes the "
            "new key to premium. Requires --tier premium and both "
            "--success-url and --cancel-url."
        ),
    )
    parser.add_argument(
        "--success-url", default=None,
        help="Absolute URL Stripe redirects to on success.",
    )
    parser.add_argument(
        "--cancel-url", default=None,
        help="Absolute URL Stripe redirects to on cancel.",
    )
    parser.add_argument(
        "--manifest-path", default=None,
        help=(
            "Override the manifest path "
            f"(default: ${KEYS_MANIFEST_ENV_VAR} or "
            f"{DEFAULT_KEYS_MANIFEST_PATH})."
        ),
    )
    return parser


def _issue_cli(argv: list[str]) -> int:
    args = _build_issue_parser().parse_args(argv)

    manifest_path = (
        Path(args.manifest_path) if args.manifest_path else None
    )

    try:
        result = issue_key(
            tier=args.tier,
            label=args.label,
            with_checkout=bool(args.checkout),
            success_url=args.success_url,
            cancel_url=args.cancel_url,
            manifest_path=manifest_path,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — operator-facing CLI
        # Includes BillingConfigError / BillingAPIError / OSError /
        # anything else the lazy-imported checkout caller can raise.
        # The manifest row is NOT written when we land here, by
        # construction (issue_key writes the row only AFTER the
        # checkout call succeeded).
        print(
            f"error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3

    # Stdout block: the raw api_key is printed EXACTLY ONCE here.
    # Anyone reading this output is the operator; we don't try to
    # gate or redact it because there's nothing else they can do
    # with the key — they have to deliver it to the user.
    print("API key issued (record this once; it cannot be recovered):")
    print(f"  api_key             : {result['api_key']}")
    print(f"  key_hash            : {result['key_hash']}")
    print(f"  tier                : {result['tier']}")
    print(f"  created_at          : {result['created_at']}")
    print(f"  label_hash          : {result['label_hash']}")
    if result.get("checkout_session_id"):
        print(f"  checkout_session_id : {result['checkout_session_id']}")
        print(f"  checkout_url        : {result['checkout_url']}")
    print(f"  manifest_path       : {result['manifest_path']}")
    return 0


def _build_top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys",
        description="Operator-only API-key management commands.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "issue",
        help="Generate a new API key and append a manifest row.",
        add_help=False,
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        _build_top_parser().print_help(sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "issue":
        return _issue_cli(rest)
    if command in ("-h", "--help"):
        _build_top_parser().print_help()
        return 0
    print(f"error: unknown command '{command}'", file=sys.stderr)
    print("available commands: issue", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
