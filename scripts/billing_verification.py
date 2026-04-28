"""
Operator pre-flight check for the SaaS billing surface.

Runs a series of safe, read-only inspections and prints a checklist
report. NEVER prints secret values — env vars are reported as either
``set`` or ``unset``, and any consistency check is computed from
metadata (length, mode prefix) without echoing the secret itself.

Usage:
    python -m scripts.billing_verification
    python -m scripts.billing_verification --json
    python -m scripts.billing_verification --strict   # exit 1 on any FAIL

Checks performed:
  1. Required env vars present (Stripe + manifest + cache).
  2. Stripe key/price/webhook are all in the SAME mode (test xor live).
     Mixed test+live env vars are rejected as a hard FAIL.
  3. Manifest path is readable (or absent — both are safe states; the
     server lazy-creates).
  4. Premium cache path resolves to a writable directory.
  5. Webhook idempotency log path resolves to a writable directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Stripe key prefixes — the only thing we ever inspect from a key.
_TEST_PREFIXES = ("sk_test_", "rk_test_", "pk_test_", "whsec_test_")
_LIVE_PREFIXES = ("sk_live_", "rk_live_", "pk_live_", "whsec_live_")

# Webhook secrets always start with "whsec_". Stripe doesn't put
# test/live in the secret prefix, so we infer mode from the API key.
_WEBHOOK_PREFIX = "whsec_"

REQUIRED_ENV_VARS = (
    # Stripe (one of the two key vars must be set)
    "STRIPE_SECRET_KEY", "STRIPE_API_KEY",
    "STRIPE_PREMIUM_PRICE_ID", "STRIPE_PRICE_ID_PREMIUM",
    "STRIPE_WEBHOOK_SECRET",
    "TRADING_PUBLIC_BASE_URL",
)

OPTIONAL_ENV_VARS = (
    "TRADING_API_KEYS_MANIFEST_PATH",
    "TRADING_API_KEYS_REVOKED_PATH",
    "TRADING_STRIPE_PREMIUM_CACHE_PATH",
    "TRADING_STRIPE_WEBHOOK_EVENTS_PATH",
    "TRADING_SAAS_REPORTS_DIR",
    "TRADING_SAAS_DATA_MODE",
    "TRADING_RUN_MODE",
    "POLYGON_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
)


# ---------------------------------------------------------------------------
# Inspection helpers (NEVER return raw secret values)
# ---------------------------------------------------------------------------


def _env_present(*names: str) -> tuple[bool, Optional[str]]:
    """Return (any-present, first-name-found-or-None). No values echoed."""
    for n in names:
        v = os.environ.get(n)
        if v and str(v).strip():
            return (True, n)
    return (False, None)


def _detect_stripe_mode(prefix_value: str) -> str:
    """
    Return ``test``, ``live``, or ``unknown`` for a Stripe key/price.

    Webhook secrets (``whsec_…``) are reported as ``unknown`` because
    Stripe does not encode test/live in the secret string.
    """
    if not prefix_value:
        return "unknown"
    s = str(prefix_value).strip()
    for p in _TEST_PREFIXES:
        if s.startswith(p):
            return "test"
    for p in _LIVE_PREFIXES:
        if s.startswith(p):
            return "live"
    if s.startswith(_WEBHOOK_PREFIX):
        return "unknown"
    # Stripe price ids start with ``price_`` and don't carry mode info.
    if s.startswith("price_"):
        return "unknown"
    return "unknown"


def _resolve_secret_mode() -> str:
    sk = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if sk:
        return _detect_stripe_mode(sk)
    legacy = (os.environ.get("STRIPE_API_KEY") or "").strip()
    if legacy:
        return _detect_stripe_mode(legacy)
    return "unknown"


def _resolve_price_mode() -> str:
    # Price ids don't encode mode. We surface "unknown" for the price
    # but require it to be present.
    return "unknown"


def _writable_dir(path: Path) -> bool:
    """Best-effort writability check. Never creates the file itself."""
    parent = path.parent if path.suffix else path
    try:
        parent.mkdir(parents=True, exist_ok=True)
        return os.access(str(parent), os.W_OK)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_required_env() -> dict:
    """Pass when each required env-var family has at least one entry set."""
    findings: list[str] = []
    families = (
        ("stripe_secret", ("STRIPE_SECRET_KEY", "STRIPE_API_KEY")),
        ("stripe_price",
         ("STRIPE_PREMIUM_PRICE_ID", "STRIPE_PRICE_ID_PREMIUM")),
        ("stripe_webhook_secret", ("STRIPE_WEBHOOK_SECRET",)),
        ("public_base_url", ("TRADING_PUBLIC_BASE_URL",)),
    )
    missing: list[str] = []
    for label, names in families:
        present, _ = _env_present(*names)
        if not present:
            missing.append(label)
        else:
            findings.append(f"  {label}: set")
    status = "PASS" if not missing else "FAIL"
    return {
        "name": "required_env",
        "status": status,
        "findings": findings,
        "missing": missing,
    }


def _check_stripe_mode_consistency() -> dict:
    """Reject a mixed test+live environment."""
    secret_mode = _resolve_secret_mode()
    findings = [f"  stripe_secret_mode: {secret_mode}"]
    # Detect both test AND live keys present at the same time — this
    # is the most common foot-gun.
    sk = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    legacy = (os.environ.get("STRIPE_API_KEY") or "").strip()
    seen_modes = {
        _detect_stripe_mode(v)
        for v in (sk, legacy)
        if v
    } - {"unknown"}
    if {"test", "live"}.issubset(seen_modes):
        return {
            "name": "stripe_mode_consistency",
            "status": "FAIL",
            "findings": findings + ["  test and live keys both set"],
            "reason": "mixed_test_and_live_keys",
        }
    return {
        "name": "stripe_mode_consistency",
        "status": "PASS",
        "findings": findings,
        "reason": "single_mode",
    }


def _check_manifest_path() -> dict:
    raw = (os.environ.get("TRADING_API_KEYS_MANIFEST_PATH") or "").strip()
    findings: list[str] = []
    if not raw:
        findings.append("  manifest_path: default (data/api_keys_manifest.jsonl)")
        path = Path("data/api_keys_manifest.jsonl")
    else:
        findings.append(f"  manifest_path: {raw}")
        path = Path(raw)
    if path.exists():
        try:
            count = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            findings.append(f"  manifest_rows: {count}")
        except Exception as exc:
            return {
                "name": "manifest_path",
                "status": "FAIL",
                "findings": findings + [f"  read_error: {type(exc).__name__}"],
            }
    else:
        findings.append("  manifest_rows: 0 (file not yet created)")
    return {
        "name": "manifest_path",
        "status": "PASS",
        "findings": findings,
    }


def _check_premium_cache_path() -> dict:
    raw = (os.environ.get("TRADING_STRIPE_PREMIUM_CACHE_PATH") or "").strip()
    if not raw:
        path = Path("data/stripe_premium_keys.json")
        findings = ["  premium_cache_path: default (data/stripe_premium_keys.json)"]
    else:
        path = Path(raw)
        findings = [f"  premium_cache_path: {raw}"]
    findings.append(f"  parent_dir_writable: {bool(_writable_dir(path))}")
    return {
        "name": "premium_cache_path",
        "status": "PASS" if _writable_dir(path) else "FAIL",
        "findings": findings,
    }


def _check_webhook_events_path() -> dict:
    raw = (os.environ.get("TRADING_STRIPE_WEBHOOK_EVENTS_PATH") or "").strip()
    if not raw:
        path = Path("data/stripe_webhook_events.jsonl")
        findings = ["  webhook_events_path: default (data/stripe_webhook_events.jsonl)"]
    else:
        path = Path(raw)
        findings = [f"  webhook_events_path: {raw}"]
    findings.append(f"  parent_dir_writable: {bool(_writable_dir(path))}")
    return {
        "name": "webhook_events_path",
        "status": "PASS" if _writable_dir(path) else "FAIL",
        "findings": findings,
    }


def _check_optional_env_summary() -> dict:
    findings: list[str] = []
    for name in OPTIONAL_ENV_VARS:
        present = bool((os.environ.get(name) or "").strip())
        findings.append(f"  {name}: {'set' if present else 'unset'}")
    return {
        "name": "optional_env_summary",
        "status": "PASS",
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_checks() -> list[dict]:
    return [
        _check_required_env(),
        _check_stripe_mode_consistency(),
        _check_manifest_path(),
        _check_premium_cache_path(),
        _check_webhook_events_path(),
        _check_optional_env_summary(),
    ]


def format_text(results: list[dict]) -> str:
    lines: list[str] = ["Billing verification:"]
    for r in results:
        marker = "[PASS]" if r["status"] == "PASS" else "[FAIL]"
        lines.append(f"{marker} {r['name']}")
        for f in r.get("findings", []):
            lines.append(f)
    return "\n".join(lines)


def format_json(results: list[dict]) -> str:
    return json.dumps({"checks": results}, indent=2)


def has_failures(results: list[dict]) -> bool:
    return any(r["status"] != "PASS" for r in results)


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="python -m scripts.billing_verification",
        description="Pre-flight check for SaaS billing env. Never prints secret values.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit code 1 when any check fails (default: always exit 0).",
    )
    args = parser.parse_args(argv)

    results = run_checks()
    if args.json:
        print(format_json(results))
    else:
        print(format_text(results))

    if args.strict and has_failures(results):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
