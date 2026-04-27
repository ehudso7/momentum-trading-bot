"""
Phase 6.5 — Production API smoke test CLI.

Operator-only command-line tool that hits a deployed API surface and
asserts the expected HTTP shape on every public, protected, and
auth-failure endpoint. Designed for the "I just deployed and want to
know it works end-to-end" moment, NOT for continuous monitoring.

Usage::

    python -m trading_bot.api.smoke \\
        --base-url https://your-host.example.com \\
        --api-key  <issued-key>

The raw ``api_key`` is read from the command line for convenience but
**never** appears in any output — only its hash. The smoke runner
echoes ``key_hash`` (SHA-256[:32]) so the operator can correlate the
run against the issuance manifest, the audit log, and the usage log.

Checks performed:

  1. ``GET /``                  →  200  (public landing page)
  2. ``GET /health``            →  200  (liveness probe, no auth)
  3. ``GET /reports/latest``    →  401  (no Authorization header)
  4. ``GET /reports/latest``    →  403 OR 503  (with a bogus token)
                                 - 403: server is configured, key is invalid
                                 - 503: server is not configured for any auth
                                        source (no env keys, no manifest)
  5. ``GET /reports/latest``    →  200 OR 404  (with the supplied key)
                                 - 200: key works AND a report exists
                                 - 404: key works, no report yet (still good)
  6. ``GET /dashboard``         →  200  (with the supplied key — HTML page)

Exit codes:

  0  every check passed
  1  one or more checks failed (network error or unexpected status)
  2  argument validation failure (blank --api-key, etc.)

This module imports nothing from FastAPI, structlog, or
``trading_bot.core.*``. It uses only the Python standard library so
it can be run from any environment that has the package installed —
operator laptop, CI, Railway shell — without extra dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from typing import Callable, NamedTuple, Optional


DEFAULT_TIMEOUT_SECONDS = 10.0

# A deliberately invalid token used to probe the server's "key is
# wrong" response. It must NOT collide with anything the operator
# could have issued — anchor the prefix so a future test reader can
# spot it as the smoke probe rather than a real key.
_BOGUS_PROBE_KEY = "smoke-probe-not-a-real-api-key-do-not-issue"


class CheckResult(NamedTuple):
    """One check's outcome — printable directly by the CLI renderer."""

    name: str
    method: str
    path: str
    expected: tuple[int, ...]
    actual: Optional[int]
    passed: bool
    error: Optional[str]


# ---------------------------------------------------------------------------
# HTTP fetcher
# ---------------------------------------------------------------------------

#: Type of the injectable HTTP fetcher. Returns ``(status_code, error)``;
#: ``status_code`` is ``None`` when the request never reached a status
#: (network error, DNS failure, timeout). ``error`` is a short
#: human-readable string in that case.
HttpFetcher = Callable[..., tuple[Optional[int], Optional[str]]]


def _http_get(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Optional[int], Optional[str]]:
    """
    stdlib HTTP GET wrapper. Maps every error path into a tuple so
    the smoke runner can report uniformly without try/except clutter.
    """
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (int(resp.status), None)
    except urllib.error.HTTPError as exc:
        # 4xx / 5xx come back as exceptions. The status is meaningful,
        # so surface it as a successful "we got a response".
        return (int(exc.code), None)
    except urllib.error.URLError as exc:
        return (None, f"URLError: {exc.reason}")
    except TimeoutError as exc:
        return (None, f"Timeout: {exc}")
    except Exception as exc:  # noqa: BLE001 — operator-facing
        return (None, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Check definition
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: str) -> str:
    """SHA-256[:32] — byte-identical to every other Phase 4/5/6 hasher."""
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _build_check_specs(api_key: str) -> list[dict]:
    """
    Return the ordered list of checks the smoke runner will execute.

    Centralised so tests can inspect the spec without re-running the
    HTTP fetcher. The bogus-key check uses a hardcoded probe value,
    not the operator's key, so a 403/503 result here can never imply
    anything about the supplied key's validity.
    """
    auth_headers = {"Authorization": f"Bearer {api_key}"}
    bogus_headers = {"Authorization": f"Bearer {_BOGUS_PROBE_KEY}"}
    return [
        {
            "name": "public landing page",
            "path": "/",
            "expected": (200,),
            "headers": None,
        },
        {
            "name": "health probe (no auth required)",
            "path": "/health",
            "expected": (200,),
            "headers": None,
        },
        {
            "name": "protected endpoint without key returns 401",
            "path": "/reports/latest",
            "expected": (401,),
            "headers": None,
        },
        {
            "name": (
                "protected endpoint with bogus key returns 403 "
                "(server configured) or 503 (server unconfigured)"
            ),
            "path": "/reports/latest",
            "expected": (403, 503),
            "headers": bogus_headers,
        },
        {
            "name": (
                "protected endpoint with supplied key returns 200 "
                "(report exists) or 404 (no report yet)"
            ),
            "path": "/reports/latest",
            "expected": (200, 404),
            "headers": auth_headers,
        },
        {
            "name": "dashboard with supplied key returns 200",
            "path": "/dashboard",
            "expected": (200,),
            "headers": auth_headers,
        },
    ]


# ---------------------------------------------------------------------------
# Smoke runner
# ---------------------------------------------------------------------------


def run_smoke_tests(
    *,
    base_url: str,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    http_get: Optional[HttpFetcher] = None,
) -> list[CheckResult]:
    """
    Run every smoke check sequentially. Returns one ``CheckResult``
    per check, in the order they were executed.

    ``http_get`` is injectable so tests can supply a fake fetcher
    that returns predetermined statuses without hitting the network.
    """
    fetch: HttpFetcher = http_get if http_get is not None else _http_get
    results: list[CheckResult] = []
    for spec in _build_check_specs(api_key):
        url = _join_url(base_url, spec["path"])
        status, error = fetch(
            url, headers=spec["headers"], timeout=timeout,
        )
        passed = status is not None and status in spec["expected"]
        results.append(CheckResult(
            name=spec["name"],
            method="GET",
            path=spec["path"],
            expected=tuple(spec["expected"]),
            actual=status,
            passed=passed,
            error=error,
        ))
    return results


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_expected(expected: tuple[int, ...]) -> str:
    return " or ".join(str(c) for c in expected)


def _format_actual(result: CheckResult) -> str:
    if result.actual is not None:
        return str(result.actual)
    return f"(no response: {result.error or 'unknown error'})"


def _render_text(
    base_url: str, key_hash: str, results: list[CheckResult],
) -> str:
    lines: list[str] = []
    lines.append(f"API smoke test against {base_url}")
    lines.append(
        f"  key_hash: {key_hash}  (raw key never printed)"
    )
    lines.append("")
    # Compute path width so the table lines up.
    path_w = max((len(r.path) for r in results), default=10)
    for r in results:
        tag = "PASS" if r.passed else "FAIL"
        lines.append(
            f"  [{tag}] {r.method} {r.path:<{path_w}}  "
            f"expected {_format_expected(r.expected):<10}  "
            f"got {_format_actual(r):<10}  {r.name}"
        )
    failed = sum(1 for r in results if not r.passed)
    lines.append("")
    if failed == 0:
        lines.append(f"All {len(results)} checks passed.")
    else:
        lines.append(
            f"{failed} of {len(results)} checks FAILED."
        )
    return "\n".join(lines) + "\n"


def _render_json(
    base_url: str, key_hash: str, results: list[CheckResult],
) -> str:
    payload = {
        "base_url": base_url,
        "key_hash": key_hash,
        "passed": all(r.passed for r in results),
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        },
        "checks": [
            {
                "name": r.name,
                "method": r.method,
                "path": r.path,
                "expected": list(r.expected),
                "actual": r.actual,
                "passed": r.passed,
                "error": r.error,
            }
            for r in results
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.api.smoke",
        description=(
            "Operator-only end-to-end smoke test for a deployed API. "
            "Verifies public, protected, and auth-failure paths against "
            "the documented HTTP contract. The supplied --api-key is "
            "hashed before any output is printed; the raw value never "
            "appears on stdout, stderr, or in the JSON payload."
        ),
    )
    parser.add_argument(
        "--base-url", required=True,
        help=(
            "Base URL of the deployed API (e.g. "
            "https://your-host.example.com). Trailing slash is "
            "ignored."
        ),
    )
    parser.add_argument(
        "--api-key", required=True,
        help=(
            "Issued API key to test. Hashed in-process; the raw "
            "value is never printed."
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Per-request timeout in seconds "
            f"(default: {DEFAULT_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit a JSON object instead of the human-readable table.",
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    *,
    http_get: Optional[HttpFetcher] = None,
) -> int:
    """
    CLI entry point. ``http_get`` is injectable so tests can drive
    the renderer without making real network calls.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    api_key = (args.api_key or "").strip()
    if not api_key:
        print("error: --api-key must not be blank", file=sys.stderr)
        return 2

    base_url = (args.base_url or "").strip()
    if not base_url:
        print("error: --base-url must not be blank", file=sys.stderr)
        return 2

    if args.timeout <= 0:
        print("error: --timeout must be > 0", file=sys.stderr)
        return 2

    key_hash = _hash_api_key(api_key)
    results = run_smoke_tests(
        base_url=base_url,
        api_key=api_key,
        timeout=args.timeout,
        http_get=http_get,
    )

    if args.as_json:
        sys.stdout.write(_render_json(base_url, key_hash, results))
    else:
        sys.stdout.write(_render_text(base_url, key_hash, results))

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
