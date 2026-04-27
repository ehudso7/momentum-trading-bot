"""
Phase 7.2 — production launch lockdown verifier.

Operator-only CLI that an operator runs from the Railway shell (or
locally with the production env vars exported) right before flipping
the real-traffic switch. It verifies the deployment is configured the
way Phase 6.2 / 6.4 / 7.0 expect: every operator-data path is set,
points at a sane location, and the directory is writable.

::

    python -m trading_bot.api.launch_check
    python -m trading_bot.api.launch_check --json
    python -m trading_bot.api.launch_check --allow-tmp           # for local
    python -m trading_bot.api.launch_check --smoke \\
        --base-url https://your-host.example.com \\
        --api-key  <key>

Exit codes:

  0  every check passed (deployment is launch-ready)
  1  one or more checks failed (do not flip the traffic switch)
  2  argument validation failure

Privacy posture
---------------

  * Pure stdlib — no FastAPI, no structlog, no httpx. Importable
    from any environment that has the package installed.
  * The supplied ``--api-key`` (when ``--smoke`` is used) is hashed
    in-process; the raw value is never printed.
  * No env var values that could be secrets are echoed — paths are
    safe, but tokens are not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple, Optional


# ---------------------------------------------------------------------------
# Required env vars
# ---------------------------------------------------------------------------

#: The full set of operator-data env vars the deployment must set.
#: Each entry: (env_var_name, "must_exist" | "writable_parent",
#:              human-readable purpose).
REQUIRED_ENV_VARS: tuple[tuple[str, str, str], ...] = (
    (
        "TRADING_API_KEYS_MANIFEST_PATH",
        "must_exist_or_creatable",
        "issuance manifest (Phase 6.0/6.2)",
    ),
    (
        "TRADING_API_KEYS_REVOKED_PATH",
        "must_exist_or_creatable",
        "revocation log (Phase 6.2)",
    ),
    (
        "TRADING_STRIPE_PREMIUM_CACHE_PATH",
        "writable_parent",
        "Stripe premium-hash cache (Phase 4.7 + Phase 7.0)",
    ),
    (
        "TRADING_API_USAGE_LOG_PATH",
        "writable_parent",
        "per-key usage log (Phase 4.6)",
    ),
    (
        "TRADING_API_AUDIT_LOG_PATH",
        "writable_parent",
        "metadata-only audit log (Phase 4.4)",
    ),
    (
        "TRADING_API_GROWTH_LOG_PATH",
        "writable_parent",
        "?ref= growth log (Phase 5.1)",
    ),
    (
        "TRADING_API_CONVERSION_LOG_PATH",
        "writable_parent",
        "free→premium conversion log (Phase 4.9)",
    ),
    (
        "TRADING_API_UPGRADE_EVENTS_LOG_PATH",
        "writable_parent",
        "upgrade-event telemetry (Phase 5.5/5.8)",
    ),
)

#: Path-shape rejections that fire unless the corresponding flag is
#: passed.  Production deployments should never write operator data
#: into the OS tmp directory or into the repo-local ``data/`` folder.
_TMP_PREFIXES: tuple[str, ...] = ("/tmp/", "/var/tmp/")
_REPO_LOCAL_PREFIXES: tuple[str, ...] = ("data/", "./data/")

#: Env var Railway sets on every deployed service. Used to detect
#: "we are running in production" without making a network call.
RAILWAY_ENV_VAR = "RAILWAY_ENVIRONMENT"


class CheckResult(NamedTuple):
    """One env-var check's outcome."""

    name: str
    env_var: str
    passed: bool
    detail: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: str) -> str:
    """SHA-256[:32] — byte-identical to every other Phase 4/5/6/7 hasher."""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _is_under(path_str: str, prefixes: tuple[str, ...]) -> bool:
    """True iff ``path_str`` (as written) starts with one of ``prefixes``."""
    s = path_str.strip()
    for p in prefixes:
        if s == p.rstrip("/") or s.startswith(p):
            return True
    return False


def _looks_repo_local(path_str: str) -> bool:
    """True iff the path is a relative path under ``data/`` (no leading slash)."""
    s = path_str.strip()
    if s.startswith("/"):
        return False
    return _is_under(s, _REPO_LOCAL_PREFIXES)


def _ensure_parent_writable(path: Path) -> tuple[bool, str]:
    """
    Return ``(ok, detail)``. Tries to create the parent directory if
    missing. Probes write access by creating and deleting a temp file
    in the parent.
    """
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return (False, f"cannot create parent {parent!s}: {exc}")
    # Probe write by creating a unique temp file inside the parent.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(parent), prefix=".launch_check.",
            suffix=".tmp", delete=True,
        ):
            pass
    except Exception as exc:
        return (False, f"parent {parent!s} not writable: {exc}")
    return (True, f"parent {parent!s} writable")


def _ensure_file_or_creatable(path: Path) -> tuple[bool, str]:
    """
    Return ``(ok, detail)``. Either the file already exists OR the
    parent is writable enough that issuance can create it.
    """
    if path.exists():
        return (True, f"exists ({path.stat().st_size} bytes)")
    return _ensure_parent_writable(path)


# ---------------------------------------------------------------------------
# Env / path checks
# ---------------------------------------------------------------------------


def run_env_checks(
    *,
    allow_tmp: bool = False,
    in_railway: Optional[bool] = None,
    env: Optional[dict[str, str]] = None,
) -> list[CheckResult]:
    """
    Execute every Phase 7.2 env / path check.

    ``allow_tmp`` opts out of the "no /tmp paths" gate (intended for
    local dev).  ``in_railway`` overrides the auto-detected
    ``RAILWAY_ENVIRONMENT`` signal — useful for tests. ``env`` lets
    callers inject a fake environment dict.
    """
    src = env if env is not None else os.environ
    if in_railway is None:
        in_railway = bool((src.get(RAILWAY_ENV_VAR) or "").strip())

    results: list[CheckResult] = []
    for env_var, mode, purpose in REQUIRED_ENV_VARS:
        raw = (src.get(env_var) or "").strip()
        if not raw:
            results.append(CheckResult(
                name=f"{env_var} is set",
                env_var=env_var,
                passed=False,
                detail=f"unset (purpose: {purpose})",
            ))
            continue

        # Path-shape gates (production posture).
        if not allow_tmp and _is_under(raw, _TMP_PREFIXES):
            results.append(CheckResult(
                name=f"{env_var} not in /tmp",
                env_var=env_var,
                passed=False,
                detail=(
                    f"{raw!r} is under /tmp; pass --allow-tmp for local dev"
                ),
            ))
            continue

        if in_railway and _looks_repo_local(raw):
            results.append(CheckResult(
                name=f"{env_var} not repo-local under Railway",
                env_var=env_var,
                passed=False,
                detail=(
                    f"{raw!r} is under repo-local 'data/'; "
                    "production should use the persistent volume "
                    "(e.g. /app/data/...)"
                ),
            ))
            continue

        path = Path(raw)
        if mode == "must_exist_or_creatable":
            ok, detail = _ensure_file_or_creatable(path)
        elif mode == "writable_parent":
            ok, detail = _ensure_parent_writable(path)
        else:  # pragma: no cover — defensive
            ok, detail = (False, f"unknown mode {mode!r}")

        results.append(CheckResult(
            name=f"{env_var} usable",
            env_var=env_var,
            passed=ok,
            detail=detail,
        ))

    return results


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_text(
    env_results: list[CheckResult],
    smoke_results: Optional[list] = None,
    *,
    base_url: Optional[str] = None,
    key_hash: Optional[str] = None,
) -> str:
    lines: list[str] = []
    lines.append("Production launch check")
    lines.append("")
    lines.append("Env / path checks:")
    name_w = max((len(r.env_var) for r in env_results), default=10)
    for r in env_results:
        tag = "PASS" if r.passed else "FAIL"
        lines.append(
            f"  [{tag}] {r.env_var:<{name_w}}  {r.detail}"
        )
    env_failed = sum(1 for r in env_results if not r.passed)

    if smoke_results is not None:
        lines.append("")
        lines.append(f"Smoke checks against {base_url}:")
        if key_hash:
            lines.append(f"  key_hash: {key_hash}  (raw key never printed)")
        path_w = max(
            (len(r.path) for r in smoke_results), default=10,
        )
        for r in smoke_results:
            tag = "PASS" if r.passed else "FAIL"
            actual = (
                str(r.actual) if r.actual is not None
                else f"(no response: {r.error or 'unknown'})"
            )
            expected = " or ".join(str(c) for c in r.expected)
            lines.append(
                f"  [{tag}] {r.method} {r.path:<{path_w}}  "
                f"expected {expected:<10}  got {actual:<10}  {r.name}"
            )
        smoke_failed = sum(1 for r in smoke_results if not r.passed)
    else:
        smoke_failed = 0

    lines.append("")
    total_failed = env_failed + smoke_failed
    if total_failed == 0:
        lines.append("READY — every check passed.")
    else:
        lines.append(
            f"NOT READY — {total_failed} check(s) failed."
        )
    return "\n".join(lines) + "\n"


def _render_json(
    env_results: list[CheckResult],
    smoke_results: Optional[list] = None,
    *,
    base_url: Optional[str] = None,
    key_hash: Optional[str] = None,
) -> str:
    payload: dict = {
        "env_checks": [
            {
                "name": r.name,
                "env_var": r.env_var,
                "passed": r.passed,
                "detail": r.detail,
            }
            for r in env_results
        ],
        "env_passed": all(r.passed for r in env_results),
    }
    if smoke_results is not None:
        payload["base_url"] = base_url
        payload["key_hash"] = key_hash
        payload["smoke_checks"] = [
            {
                "name": r.name,
                "method": r.method,
                "path": r.path,
                "expected": list(r.expected),
                "actual": r.actual,
                "passed": r.passed,
                "error": r.error,
            }
            for r in smoke_results
        ]
        payload["smoke_passed"] = all(r.passed for r in smoke_results)
    payload["ready"] = (
        all(r.passed for r in env_results)
        and (
            smoke_results is None
            or all(r.passed for r in smoke_results)
        )
    )
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.launch_check",
        description=(
            "Phase 7.2 — verify the deployment is launch-ready: "
            "every operator-data env var is set, points at a sane "
            "location, and the parent directory is writable. Pass "
            "--smoke to additionally run the production HTTP smoke "
            "test from this same shell."
        ),
    )
    parser.add_argument(
        "--allow-tmp", action="store_true",
        help=(
            "Allow operator-data paths to live under /tmp. Intended "
            "for local development; reject these in production."
        ),
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit a JSON object instead of the human-readable table.",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help=(
            "Also run the Phase 6.5 smoke test against --base-url with "
            "--api-key. Both flags become required when --smoke is set."
        ),
    )
    parser.add_argument(
        "--base-url", default=None,
        help="(Required when --smoke) Base URL of the deployed API.",
    )
    parser.add_argument(
        "--api-key", default=None,
        help=(
            "(Required when --smoke) Issued API key to verify against "
            "the deployed API. Hashed in-process; never printed."
        ),
    )
    parser.add_argument(
        "--smoke-timeout", type=float, default=10.0,
        help="(Optional, with --smoke) Per-request timeout in seconds.",
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    smoke_runner: Optional[Callable] = None,
    env: Optional[dict[str, str]] = None,
) -> int:
    """
    CLI entry point.

    ``smoke_runner`` is injectable so tests can drive the --smoke
    branch without making real network calls. When omitted, the
    real ``trading_bot.api.smoke.run_smoke_tests`` is used.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.smoke:
        if not (args.base_url or "").strip():
            print(
                "error: --smoke requires --base-url",
                file=sys.stderr,
            )
            return 2
        if not (args.api_key or "").strip():
            print(
                "error: --smoke requires --api-key",
                file=sys.stderr,
            )
            return 2

    env_results = run_env_checks(allow_tmp=args.allow_tmp, env=env)

    smoke_results = None
    key_hash: Optional[str] = None
    base_url: Optional[str] = None
    if args.smoke:
        api_key = (args.api_key or "").strip()
        base_url = (args.base_url or "").strip()
        key_hash = _hash_api_key(api_key)
        runner = smoke_runner
        if runner is None:
            from trading_bot.api.smoke import run_smoke_tests
            runner = run_smoke_tests
        smoke_results = runner(
            base_url=base_url,
            api_key=api_key,
            timeout=args.smoke_timeout,
        )

    if args.as_json:
        sys.stdout.write(_render_json(
            env_results, smoke_results,
            base_url=base_url, key_hash=key_hash,
        ))
    else:
        sys.stdout.write(_render_text(
            env_results, smoke_results,
            base_url=base_url, key_hash=key_hash,
        ))

    env_ok = all(r.passed for r in env_results)
    smoke_ok = (
        smoke_results is None
        or all(r.passed for r in smoke_results)
    )
    return 0 if (env_ok and smoke_ok) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
