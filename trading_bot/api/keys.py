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
# Phase 6.2 — revoke subcommand
# ---------------------------------------------------------------------------


def _build_revoke_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys revoke",
        description=(
            "Append a revocation row for an issued API key. The "
            "revoked hash is rejected on the next request — no "
            "server restart needed. Operators may provide either the "
            "key_hash directly (preferred) or the raw api_key (which "
            "is hashed in-process and discarded immediately — never "
            "persisted)."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--key-hash", default=None,
        help="SHA-256(api_key)[:32] hash to revoke (preferred).",
    )
    group.add_argument(
        "--api-key", default=None,
        help=(
            "Raw API key to revoke. Hashed in-process; the raw value "
            "is NEVER written to the revocation log."
        ),
    )
    parser.add_argument(
        "--reason", default=None,
        help=(
            "Optional operator-facing free-text reason for revocation "
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

    if args.key_hash:
        key_hash = str(args.key_hash).strip()
        if not key_hash:
            print("error: --key-hash must not be blank", file=sys.stderr)
            return 2
    else:
        raw = str(args.api_key or "").strip()
        if not raw:
            print("error: --api-key must not be blank", file=sys.stderr)
            return 2
        key_hash = key_store.hash_api_key(raw)
        # Drop the raw value from the local frame as soon as possible.
        # Python can't truly zero-out the string, but we can at least
        # avoid keeping the binding around for any longer than needed.
        del raw

    target = (
        Path(args.revoked_path) if args.revoked_path else None
    )

    try:
        record = key_store.append_revocation(
            key_hash=key_hash,
            reason=args.reason,
            target=target,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — operator-facing CLI
        print(
            f"error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3

    final_path = (
        target if target is not None else key_store.revoked_path()
    )
    print("API key revoked (revocation row appended):")
    print(f"  key_hash      : {record['key_hash']}")
    print(f"  timestamp     : {record['timestamp']}")
    if record.get("reason"):
        print(f"  reason        : {record['reason']}")
    print(f"  revoked_path  : {final_path}")
    return 0


# ---------------------------------------------------------------------------
# Phase 6.3 — manifest inspection (list / show / stats)
# ---------------------------------------------------------------------------
#
# Read-only operator surface. Reads ``data/api_keys_manifest.jsonl`` and
# ``data/api_keys_revoked.jsonl`` directly so it can surface fields that
# the auth-path reader (``key_store``) deliberately drops (``ref_code``,
# ``checkout_session_id``). Output is curated:
#
#   * raw ``api_key`` is never printed (it isn't on disk to begin with);
#   * raw ``label`` is never printed (only ``label_hash`` is on disk and
#     even that is omitted from the inspection surface — operators who
#     want their hashed labels can grep the manifest directly);
#   * ``checkout_url`` is never printed (it isn't on disk to begin with).


# Fields surfaced by ``list`` / ``show`` / JSON output. Anything outside
# this allow-list is dropped before serialisation, even if a future
# ``issue_key`` schema bump quietly adds a new field.
_INSPECT_ALLOWED_FIELDS: tuple[str, ...] = (
    "created_at",
    "key_hash",
    "tier",
    "ref_code",
    "checkout_session_id",
)


def _read_jsonl_rows(path: Path) -> list[dict]:
    """
    Read a JSONL file into a list of dict rows. Tolerant of missing
    files, blank lines, malformed JSON, and non-dict payloads — every
    failure path drops the offending line and continues. Never raises.
    """
    rows: list[dict] = []
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
                if isinstance(rec, dict):
                    rows.append(rec)
    except (FileNotFoundError, OSError):
        return []
    except Exception:
        return []
    return rows


def _build_revocation_index(rows: list[dict]) -> dict[str, dict]:
    """
    Map ``key_hash`` → most-recent revocation row. The revocation log
    is append-only; if an operator revokes the same hash twice, the
    later row wins (in practice the second revoke is idempotent and
    differs only in ``reason``). Rows lacking ``key_hash`` are dropped.
    """
    index: dict[str, dict] = {}
    for rec in rows:
        h = rec.get("key_hash")
        if not isinstance(h, str) or not h:
            continue
        index[h] = rec
    return index


def _inspection_view(
    manifest_row: dict,
    revocation_index: dict[str, dict],
) -> dict:
    """
    Project a manifest row into the operator-safe inspection surface,
    augmented with revocation status pulled from the revocation index.
    """
    out: dict = {f: manifest_row.get(f) for f in _INSPECT_ALLOWED_FIELDS}
    h = manifest_row.get("key_hash") or ""
    rev = revocation_index.get(h)
    out["revoked"] = rev is not None
    if rev is not None:
        # Revocation timestamp + reason are operator-curated artefacts
        # already (no PII enters them — the CLI's --reason cap is
        # enforced before they land on disk in Phase 6.2).
        out["revoked_at"] = rev.get("timestamp")
        if rev.get("reason"):
            out["revoked_reason"] = rev.get("reason")
    return out


def _sort_newest_first(rows: list[dict]) -> list[dict]:
    """
    Sort by ``created_at`` descending. Rows lacking a usable
    timestamp fall to the bottom but are preserved.
    """
    def _key(r: dict) -> tuple[int, str]:
        ts = r.get("created_at")
        if isinstance(ts, str) and ts:
            return (0, ts)
        return (1, "")
    return sorted(rows, key=_key, reverse=True)


def _format_list_table(rows: list[dict], include_revoked: bool) -> str:
    """
    Pretty-printed ASCII table for the ``list`` command's text mode.
    """
    if not rows:
        return "(no manifest rows match the supplied filters)\n"

    headers = ["created_at", "key_hash", "tier", "ref_code"]
    if include_revoked:
        headers.extend(["revoked", "revoked_at"])
    headers.append("checkout_session_id")

    def _cell(row: dict, name: str) -> str:
        value = row.get(name)
        if value is None:
            return ""
        if name == "revoked":
            return "yes" if value else "no"
        return str(value)

    widths = {
        h: max(len(h), max((len(_cell(r, h)) for r in rows), default=0))
        for h in headers
    }
    sep = "  "
    lines: list[str] = []
    lines.append(sep.join(h.ljust(widths[h]) for h in headers))
    lines.append(sep.join("-" * widths[h] for h in headers))
    for r in rows:
        lines.append(sep.join(_cell(r, h).ljust(widths[h]) for h in headers))
    return "\n".join(lines) + "\n"


def _build_list_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys list",
        description=(
            "List issued API keys from the operator-only manifest. "
            "By default only active (non-revoked) rows are shown; pass "
            "--include-revoked to see revoked rows too. The raw key, "
            "raw label, and checkout URL are never displayed — only "
            "hashes and operator-supplied metadata."
        ),
    )
    parser.add_argument(
        "--tier", default=None, choices=sorted(VALID_TIERS),
        help="Show only rows with the supplied tier.",
    )
    parser.add_argument(
        "--ref", default=None,
        help=(
            "Show only rows whose ref_code matches this exact value "
            "(after the same Phase 5.1 sanitiser the live middleware "
            "applies)."
        ),
    )
    parser.add_argument(
        "--include-revoked", action="store_true",
        help="Include revoked rows. By default they are hidden.",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit JSON (a list of dicts) instead of the ASCII table.",
    )
    parser.add_argument(
        "--manifest-path", default=None,
        help=(
            f"Override the manifest path "
            f"(default: ${KEYS_MANIFEST_ENV_VAR} or "
            f"{DEFAULT_KEYS_MANIFEST_PATH})."
        ),
    )
    parser.add_argument(
        "--revoked-path", default=None,
        help=(
            f"Override the revocation log path "
            f"(default: ${key_store.KEYS_REVOKED_ENV_VAR} or "
            f"{key_store.DEFAULT_KEYS_REVOKED_PATH})."
        ),
    )
    return parser


def _list_cli(argv: list[str]) -> int:
    args = _build_list_parser().parse_args(argv)

    manifest = (
        Path(args.manifest_path) if args.manifest_path
        else _manifest_path()
    )
    revoked = (
        Path(args.revoked_path) if args.revoked_path
        else key_store.revoked_path()
    )

    manifest_rows = _read_jsonl_rows(manifest)
    revocation_idx = _build_revocation_index(_read_jsonl_rows(revoked))

    # Filter at the manifest level — tier/ref filters operate on the
    # raw rows, then we project into the operator-safe surface.
    filtered: list[dict] = []
    target_ref = (
        _sanitize_ref_code(args.ref) if args.ref is not None else None
    )
    for rec in manifest_rows:
        # Only keep rows whose tier is recognisable. Anything else is
        # dropped — same posture as the auth-path reader.
        rec_tier = rec.get("tier")
        if rec_tier not in VALID_TIERS:
            continue
        if args.tier is not None and rec_tier != args.tier:
            continue
        if target_ref is not None and (rec.get("ref_code") or "") != target_ref:
            continue
        filtered.append(rec)

    views = [_inspection_view(r, revocation_idx) for r in filtered]
    if not args.include_revoked:
        views = [v for v in views if not v.get("revoked")]
    views = _sort_newest_first(views)

    if args.as_json:
        print(json.dumps(views, indent=2, sort_keys=False))
        return 0

    total_active = sum(1 for v in views if not v.get("revoked"))
    total_revoked = sum(1 for v in views if v.get("revoked"))
    if args.include_revoked:
        header = (
            f"Issued keys ({total_active} active, "
            f"{total_revoked} revoked):"
        )
    else:
        header = f"Issued keys ({total_active} active):"
    print(header)
    print()
    print(_format_list_table(views, include_revoked=args.include_revoked))
    return 0


def _build_show_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys show",
        description=(
            "Display the manifest row for a single key (looked up by "
            "key_hash). Includes revocation status and reason if the "
            "hash has been revoked."
        ),
    )
    parser.add_argument(
        "--key-hash", required=True,
        help="SHA-256(api_key)[:32] hash to look up.",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit a JSON object instead of the human-readable block.",
    )
    parser.add_argument(
        "--manifest-path", default=None,
        help="Override the manifest path.",
    )
    parser.add_argument(
        "--revoked-path", default=None,
        help="Override the revocation log path.",
    )
    return parser


def _show_cli(argv: list[str]) -> int:
    args = _build_show_parser().parse_args(argv)

    target_hash = (args.key_hash or "").strip()
    if not target_hash:
        print("error: --key-hash must not be blank", file=sys.stderr)
        return 2

    manifest = (
        Path(args.manifest_path) if args.manifest_path
        else _manifest_path()
    )
    revoked = (
        Path(args.revoked_path) if args.revoked_path
        else key_store.revoked_path()
    )

    manifest_rows = _read_jsonl_rows(manifest)
    revocation_idx = _build_revocation_index(_read_jsonl_rows(revoked))

    matching = [
        r for r in manifest_rows
        if r.get("key_hash") == target_hash
        and r.get("tier") in VALID_TIERS
    ]
    if not matching:
        print(
            f"error: no manifest row for key_hash {target_hash!r}",
            file=sys.stderr,
        )
        return 2

    # If the same hash somehow appears twice in the manifest, prefer
    # the most recent row — same last-row-wins rule key_store uses.
    matching = _sort_newest_first(matching)
    view = _inspection_view(matching[0], revocation_idx)

    if args.as_json:
        print(json.dumps(view, indent=2, sort_keys=False))
        return 0

    print(f"Key hash: {view['key_hash']}")
    print(f"  created_at          : {view.get('created_at') or '(none)'}")
    print(f"  tier                : {view.get('tier')}")
    print(f"  ref_code            : {view.get('ref_code') or '(none)'}")
    cs = view.get("checkout_session_id")
    print(f"  checkout_session_id : {cs if cs else '(none)'}")
    print(f"  revoked             : {'yes' if view.get('revoked') else 'no'}")
    if view.get("revoked"):
        print(f"  revoked_at          : {view.get('revoked_at') or '(none)'}")
        if view.get("revoked_reason"):
            print(f"  revoked_reason      : {view.get('revoked_reason')}")
    return 0


def _build_stats_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.keys stats",
        description=(
            "Aggregate counts over the issuance manifest. Displays "
            "totals, per-tier breakdown, per-ref_code breakdown, and "
            "per-creation-date breakdown — all derived from on-disk "
            "metadata only."
        ),
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit JSON instead of the text summary.",
    )
    parser.add_argument(
        "--manifest-path", default=None,
        help="Override the manifest path.",
    )
    parser.add_argument(
        "--revoked-path", default=None,
        help="Override the revocation log path.",
    )
    return parser


def _stats_cli(argv: list[str]) -> int:
    args = _build_stats_parser().parse_args(argv)

    manifest = (
        Path(args.manifest_path) if args.manifest_path
        else _manifest_path()
    )
    revoked = (
        Path(args.revoked_path) if args.revoked_path
        else key_store.revoked_path()
    )

    manifest_rows = _read_jsonl_rows(manifest)
    revocation_idx = _build_revocation_index(_read_jsonl_rows(revoked))

    valid_rows = [r for r in manifest_rows if r.get("tier") in VALID_TIERS]
    total = len(valid_rows)
    revoked_count = sum(
        1 for r in valid_rows
        if r.get("key_hash") in revocation_idx
    )
    active = total - revoked_count

    by_tier: dict[str, int] = {}
    by_ref: dict[str, int] = {}
    by_date: dict[str, int] = {}
    for r in valid_rows:
        tier = r.get("tier") or ""
        by_tier[tier] = by_tier.get(tier, 0) + 1
        ref = r.get("ref_code") or "(none)"
        by_ref[ref] = by_ref.get(ref, 0) + 1
        ts = r.get("created_at")
        if isinstance(ts, str) and len(ts) >= 10:
            day = ts[:10]
        else:
            day = "(unknown)"
        by_date[day] = by_date.get(day, 0) + 1

    summary = {
        "total_issued": total,
        "active": active,
        "revoked": revoked_count,
        "by_tier": dict(sorted(by_tier.items())),
        "by_ref_code": dict(sorted(by_ref.items())),
        "by_created_date": dict(sorted(by_date.items(), reverse=True)),
    }

    if args.as_json:
        print(json.dumps(summary, indent=2, sort_keys=False))
        return 0

    print("API key manifest stats:")
    print(f"  total issued      : {summary['total_issued']}")
    print(f"  active            : {summary['active']}")
    print(f"  revoked           : {summary['revoked']}")
    print()
    print("By tier:")
    if summary["by_tier"]:
        for tier, n in summary["by_tier"].items():
            print(f"  {tier:<8} : {n}")
    else:
        print("  (none)")
    print()
    print("By ref_code:")
    if summary["by_ref_code"]:
        for ref, n in summary["by_ref_code"].items():
            print(f"  {ref:<32} : {n}")
    else:
        print("  (none)")
    print()
    print("By created date (UTC):")
    if summary["by_created_date"]:
        for day, n in summary["by_created_date"].items():
            print(f"  {day:<12} : {n}")
    else:
        print("  (none)")
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
    subparsers.add_parser(
        "revoke",
        help="Append a revocation row for an issued API key.",
        add_help=False,
    )
    subparsers.add_parser(
        "list",
        help="List issued keys (filtered, hash-only — no raw secrets).",
        add_help=False,
    )
    subparsers.add_parser(
        "show",
        help="Show one manifest row by key_hash.",
        add_help=False,
    )
    subparsers.add_parser(
        "stats",
        help="Aggregate counts over the issuance manifest.",
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
    if command == "revoke":
        return _revoke_cli(rest)
    if command == "list":
        return _list_cli(rest)
    if command == "show":
        return _show_cli(rest)
    if command == "stats":
        return _stats_cli(rest)
    if command in ("-h", "--help"):
        _build_top_parser().print_help()
        return 0
    print(f"error: unknown command '{command}'", file=sys.stderr)
    print(
        "available commands: issue, revoke, list, show, stats",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
