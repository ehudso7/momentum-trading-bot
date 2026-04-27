"""
Phase 6.0 + 6.1 — Operator Key Issuance CLI.

Operator-only command-line tool for generating free-tier API keys
and (optionally) the Stripe Checkout link that promotes them to
premium. There is **no** public sign-up endpoint for any of this:
the entire surface is invoked by hand from a deployment shell.

Operator workflow::

    # Free-tier key for an internal user.
    python -m trading_bot.api.keys issue \\
        --tier free \\
        --label "alice@example.com"

    # Free-tier key already attributed to a referral channel
    # (Phase 6.1). No need to wait for the user to hit ?ref=
    # later; the attribution starts at issuance.
    python -m trading_bot.api.keys issue \\
        --tier free \\
        --label "alice@example.com" \\
        --ref "twitter-launch_2026"

    # Premium key + Stripe Checkout URL to forward to the customer,
    # also attributed at creation time.
    python -m trading_bot.api.keys issue \\
        --tier premium \\
        --label "acme-corp" \\
        --ref "hn-launch" \\
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
      "checkout_session_id":  "cs_..." | null,
      "ref_code":             "<sanitised>" | null    # Phase 6.1
    }

``ref_code`` is sanitised through the same Phase 5.1 growth
helper before storage — only ``[A-Za-z0-9\\-_:.]`` survives, and
the value is capped at 64 chars. An empty / all-stripped /
missing ``--ref`` becomes ``null``.

Privacy posture:

  * The raw ``api_key`` is **never** persisted (only the hash).
  * The raw ``label`` is **never** persisted (only the hash).
  * The ``ref_code`` IS persisted, but only after it has been
    stripped to the safe charset and capped at 64 chars (Phase
    5.1 growth sanitiser) so it is byte-identical to whatever
    the live ``?ref=`` middleware would have logged. Operators
    correlate it back to the channel via their own deployment
    notes.
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
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from trading_bot.api import key_store


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

# Phase 6.1 — ref_code sanitiser. Inlined (rather than imported from
# trading_bot.api.growth) so the operator CLI stays dependency-free
# and importable in environments that don't have the live API
# server's logging stack (e.g. structlog) installed. Parity with the
# growth-middleware sanitiser is enforced by
# tests/test_keys.py::test_sanitiser_matches_growth_sanitiser.
_REF_CODE_MAX_LENGTH = 64
_REF_CODE_STRIP_RE = re.compile(r"[^A-Za-z0-9\-_:.]")


def _sanitize_ref_code(raw: Optional[str]) -> str:
    if raw is None:
        return ""
    s = str(raw)
    if not s:
        return ""
    return _REF_CODE_STRIP_RE.sub("", s)[:_REF_CODE_MAX_LENGTH]

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
    ref_code: Optional[str] = None,
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
          "ref_code":            "<sanitised>" | None,   # Phase 6.1
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
      * ``ref_code`` is run through the Phase 5.1 growth sanitiser
        before any use. Empty input, ``None``, or a value that the
        sanitiser strips to the empty string become ``None``.

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

    # Phase 6.1 — sanitise BEFORE any persistence or Stripe call so
    # an unsafe value cannot land on disk or in Stripe metadata.
    cleaned_ref_raw = _sanitize_ref_code(ref_code) if ref_code else ""
    cleaned_ref: Optional[str] = cleaned_ref_raw if cleaned_ref_raw else None

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
        # Phase 6.1 — when a sanitised ref_code is present, forward
        # it to the checkout caller so Stripe metadata picks it up.
        # Pass the sanitised value, never the raw input.
        if cleaned_ref is not None:
            kwargs["ref_code"] = cleaned_ref
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
        "ref_code": cleaned_ref,
    }
    target = manifest_path if manifest_path is not None else _manifest_path()
    _append_manifest_row(record, target=target)

    return {
        "api_key": api_key,
        "key_hash": key_hash,
        "tier": tier,
        "created_at": created_at,
        "label_hash": label_hash,
        "ref_code": cleaned_ref,
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
        "--ref", default=None,
        help=(
            "Optional referral / source code for growth attribution. "
            "Sanitised through the Phase 5.1 growth helper "
            "(only [A-Za-z0-9-_:.] survives, capped at 64 chars) "
            "before storage and before being included in Stripe "
            "metadata. Empty / all-stripped values become null."
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
            ref_code=args.ref,
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
    if result.get("ref_code"):
        # Phase 6.1 — only print when present so the absence of a
        # --ref argument leaves the stdout block byte-identical to
        # the Phase 6.0 surface.
        print(f"  ref_code            : {result['ref_code']}")
    if result.get("checkout_session_id"):
        print(f"  checkout_session_id : {result['checkout_session_id']}")
        print(f"  checkout_url        : {result['checkout_url']}")
    print(f"  manifest_path       : {result['manifest_path']}")
    return 0


# ---------------------------------------------------------------------------
# Phase 6.2 — manifest inspection
# ---------------------------------------------------------------------------


# The complete public field-set the ``list`` view is allowed to
# emit. Anything outside this set is dropped on read — so even a
# poisoned / hand-edited manifest with stray secret-looking fields
# (``api_key``, ``label``, ``checkout_url``, …) cannot leak through
# this code path.
LIST_OUTPUT_FIELDS: tuple[str, ...] = (
    "created_at",
    "key_hash",
    "tier",
    "ref_code",
    "checkout_session_id",
)


def _read_manifest_records(path: Path) -> list[dict]:
    """
    Load the manifest into a list of dicts. Tolerant of:

      * missing file → ``[]``
      * unreadable file → ``[]``
      * blank lines → skipped
      * malformed JSON → skipped
      * non-dict JSON values → skipped
    """
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            parsed = json.loads(s)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def list_keys(
    *,
    tier_filter: Optional[str] = None,
    ref_filter: Optional[str] = None,
    manifest_path: Optional[Path] = None,
) -> list[dict]:
    """
    Return manifest rows projected to ``LIST_OUTPUT_FIELDS`` only,
    optionally filtered by tier and/or ref_code, sorted newest
    first by ``created_at``.

    Filters are AND-combined: a row must pass every supplied
    filter to be included.

    The ``ref_filter`` value is run through the same Phase 5.1
    growth sanitiser used at write time so the operator's
    ``--ref twitter-launch_2026`` matches the byte-identical value
    that landed in the file. A filter value that sanitises to the
    empty string (e.g. ``"!@#"``) is treated as "no filter
    supplied" — the operator's intent there is ambiguous and we
    prefer not to silently filter to zero rows.
    """
    target = manifest_path if manifest_path is not None else _manifest_path()
    records = _read_manifest_records(target)

    tier_norm: Optional[str] = None
    if tier_filter is not None:
        t = str(tier_filter).strip().lower()
        if t:
            tier_norm = t

    ref_norm: Optional[str] = None
    if ref_filter is not None:
        cleaned = _sanitize_ref_code(ref_filter)
        if cleaned:
            ref_norm = cleaned

    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if tier_norm is not None:
            row_tier = rec.get("tier")
            if not isinstance(row_tier, str) or row_tier.lower() != tier_norm:
                continue
        if ref_norm is not None:
            row_ref = rec.get("ref_code")
            if not isinstance(row_ref, str) or row_ref != ref_norm:
                continue
        # Project to allowed fields ONLY. Anything else is dropped.
        out.append({k: rec.get(k) for k in LIST_OUTPUT_FIELDS})

    # ISO-8601 with Z suffix sorts lexicographically the same as
    # chronologically, so newest-first is just reverse=True.
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out


def _format_list_text(rows: list[dict]) -> str:
    """Pretty-print ``list_keys`` output as a fixed-width table."""
    if not rows:
        return "(no records)"
    header = (
        f"{'created_at':<27} "
        f"{'key_hash':<32} "
        f"{'tier':<8} "
        f"{'ref_code':<24} "
        f"{'checkout_session_id'}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        ref = r.get("ref_code") or "-"
        cs = r.get("checkout_session_id") or "-"
        lines.append(
            f"{str(r.get('created_at') or ''):<27} "
            f"{str(r.get('key_hash') or ''):<32} "
            f"{str(r.get('tier') or ''):<8} "
            f"{str(ref)[:24]:<24} "
            f"{cs}"
        )
    return "\n".join(lines)


def _format_list_json(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, sort_keys=False, default=str)


def _build_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys list",
        description=(
            "Inspect the operator-only API-key manifest. Emits ONLY "
            "the documented public fields (created_at, key_hash, "
            "tier, ref_code, checkout_session_id). Raw API keys, "
            "raw labels, and Stripe checkout URLs are never present "
            "in the manifest by Phase 6.0/6.1 design and never in "
            "this output."
        ),
    )
    parser.add_argument(
        "--tier", default=None, choices=sorted(VALID_TIERS),
        help="Restrict to a single tier (free | premium).",
    )
    parser.add_argument(
        "--ref", default=None,
        help=(
            "Restrict to rows whose ref_code equals the supplied "
            "value (after sanitising through the Phase 5.1 growth "
            "helper). A value that sanitises to empty is ignored."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the plain-text table.",
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


def _list_cli(argv: list[str]) -> int:
    args = _build_list_parser().parse_args(argv)
    manifest_path = (
        Path(args.manifest_path) if args.manifest_path else None
    )

    rows = list_keys(
        tier_filter=args.tier,
        ref_filter=args.ref,
        manifest_path=manifest_path,
    )

    if args.json:
        print(_format_list_json(rows))
    else:
        print(_format_list_text(rows))
    return 0




# ---------------------------------------------------------------------------
# Phase 7.2 — bulk revocation cleanup helper
# ---------------------------------------------------------------------------


def _build_revoke_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys revoke",
        description=(
            "Append a single revocation row. Identify the target by "
            "pre-hashed --key-hash OR raw --api-key (the helper hashes "
            "internally and never persists the raw key). "
            "The revocation log is the unambiguous kill switch — every "
            "auth path checks it before consulting any manifest or env "
            "premium list."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--key-hash", default=None,
        help=(
            "SHA-256(api_key)[:32] hash of the key to revoke. Use "
            "this when you only have the hash on hand (e.g. from a "
            "growth/audit/usage log)."
        ),
    )
    group.add_argument(
        "--api-key", default=None,
        help=(
            "Raw API key. The CLI hashes it locally to "
            "SHA-256[:32] before writing. The raw value never "
            "lands on disk."
        ),
    )
    parser.add_argument(
        "--reason", default=None,
        help=(
            "Optional operator-facing free-text reason "
            f"(capped at {key_store.REVOCATION_REASON_MAX_LENGTH} chars)."
        ),
    )
    parser.add_argument(
        "--revoked-path", default=None,
        help=(
            "Override the revocation log path "
            f"(default: ${key_store.KEYS_REVOKED_ENV_VAR} or "
            f"{key_store.DEFAULT_KEYS_REVOKED_PATH})."
        ),
    )
    return parser


def _revoke_cli(argv: list[str]) -> int:
    args = _build_revoke_parser().parse_args(argv)

    if args.key_hash is not None:
        target_hash = (args.key_hash or "").strip()
        if not target_hash:
            print(
                "error: --key-hash must not be blank",
                file=sys.stderr,
            )
            return 2
    else:
        # ``--api-key`` is mutually exclusive with ``--key-hash``;
        # argparse already enforces "at least one is supplied".
        raw = args.api_key or ""
        if not raw.strip():
            print(
                "error: --api-key must not be blank",
                file=sys.stderr,
            )
            return 2
        target_hash = _hash_api_key(raw)
        if not target_hash:
            print(
                "error: failed to derive key_hash from --api-key",
                file=sys.stderr,
            )
            return 2

    target = (
        Path(args.revoked_path) if args.revoked_path else None
    )
    try:
        record = key_store.append_revocation(
            key_hash=target_hash, reason=args.reason, target=target,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — operator-facing surface
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    final_path = (
        target if target is not None else key_store.revoked_path()
    )
    print("API key revoked:")
    print(f"  revoked_path : {final_path}")
    print(f"  key_hash     : {record['key_hash']}")
    print(f"  timestamp    : {record['timestamp']}")
    if record.get("reason"):
        print(f"  reason       : {record['reason']}")
    return 0


def _build_revoke_many_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys revoke-many",
        description=(
            "Append one revocation row per --key-hash. Useful for "
            "launch-day cleanup of test keys: pass every test hash "
            "in one invocation. Duplicate hashes are accepted but "
            "always materialise as 'revoked' on the read side."
        ),
    )
    parser.add_argument(
        "--key-hash", action="append", default=[], dest="key_hashes",
        help=(
            "SHA-256(api_key)[:32] hash to revoke. May be passed "
            "multiple times. At least one is required."
        ),
    )
    parser.add_argument(
        "--reason", default=None,
        help=(
            "Optional operator-facing free-text reason applied to "
            "every revocation row in this batch (capped at "
            f"{key_store.REVOCATION_REASON_MAX_LENGTH} chars)."
        ),
    )
    parser.add_argument(
        "--revoked-path", default=None,
        help=(
            "Override the revocation log path "
            f"(default: ${key_store.KEYS_REVOKED_ENV_VAR} or "
            f"{key_store.DEFAULT_KEYS_REVOKED_PATH})."
        ),
    )
    return parser


def _revoke_many_cli(argv: list[str]) -> int:
    args = _build_revoke_many_parser().parse_args(argv)

    cleaned: list[str] = []
    for raw in args.key_hashes:
        h = (raw or "").strip()
        if not h:
            print(
                "error: --key-hash entries must not be blank",
                file=sys.stderr,
            )
            return 2
        cleaned.append(h)
    if not cleaned:
        print(
            "error: at least one --key-hash is required",
            file=sys.stderr,
        )
        return 2

    target = (
        Path(args.revoked_path) if args.revoked_path else None
    )

    written: list[dict] = []
    failed: list[tuple[str, str]] = []
    for h in cleaned:
        try:
            record = key_store.append_revocation(
                key_hash=h, reason=args.reason, target=target,
            )
            written.append(record)
        except ValueError as exc:
            failed.append((h, str(exc)))
        except Exception as exc:  # noqa: BLE001 — operator-facing
            failed.append((h, f"{type(exc).__name__}: {exc}"))

    final_path = (
        target if target is not None else key_store.revoked_path()
    )
    print("API key bulk revocation:")
    print(f"  revoked_path  : {final_path}")
    print(f"  reason        : {args.reason or '(none)'}")
    print(f"  written       : {len(written)}")
    for rec in written:
        print(f"    - {rec['key_hash']}  at {rec['timestamp']}")
    if failed:
        print(f"  failed        : {len(failed)}")
        for h, msg in failed:
            print(f"    - {h}: {msg}")
    return 0 if not failed else 1


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
    subparsers.add_parser(
        "list",
        help="Inspect the manifest with optional tier / ref filters.",
        add_help=False,
    )
    subparsers.add_parser(
        "revoke",
        help="Append one revocation row by --key-hash or --api-key.",
        add_help=False,
    )
    subparsers.add_parser(
        "revoke-many",
        help="Append a revocation row for each --key-hash.",
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
    if command == "list":
        return _list_cli(rest)
    if command == "revoke":
        return _revoke_cli(rest)
    if command == "revoke-many":
        return _revoke_many_cli(rest)
    if command in ("-h", "--help"):
        _build_top_parser().print_help()
        return 0
    print(f"error: unknown command '{command}'", file=sys.stderr)
    print(
        "available commands: issue, list, revoke, revoke-many",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
