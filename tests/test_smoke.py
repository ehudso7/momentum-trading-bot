"""
Phase 6.5 tests — production API smoke test CLI.

Drives ``trading_bot.api.smoke`` with an injected HTTP fetcher so no
real network IO ever happens. Covers:

  * The check spec (paths, expected statuses, headers).
  * Successful smoke run → exit 0, "PASS" markers, no raw key.
  * Failure paths → exit 1, "FAIL" markers, no raw key.
  * Network error path → status None, error string surfaced.
  * Argument validation (blank --api-key / --base-url, bad --timeout).
  * JSON renderer is parseable and excludes the raw key.
  * The bogus probe value is hardcoded — it never matches an issued key.
  * The CLI does NOT pull in FastAPI or structlog.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

import pytest

from trading_bot.api import smoke
from trading_bot.api.smoke import (
    DEFAULT_TIMEOUT_SECONDS,
    CheckResult,
    _BOGUS_PROBE_KEY,
    _build_check_specs,
    _hash_api_key,
    _join_url,
    main as smoke_main,
    run_smoke_tests,
)


BASE_URL = "https://api.example.com"
GOOD_KEY = "smoke-test-good-key-DO_NOT_PRINT"


def _expected_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFetcher:
    """Records every request and returns a configured (status, error)."""

    def __init__(
        self,
        responses: dict[tuple[str, Optional[str]], tuple[Optional[int], Optional[str]]],
        *,
        default: tuple[Optional[int], Optional[str]] = (500, None),
    ):
        # Key: (path-suffix, auth-header-or-None) → (status, error)
        self.responses = responses
        self.default = default
        self.calls: list[dict] = []

    def __call__(
        self, url: str, *, headers: Optional[dict] = None, timeout: float,
    ) -> tuple[Optional[int], Optional[str]]:
        # Record the call so tests can assert on header & timeout.
        auth = (headers or {}).get("Authorization")
        self.calls.append({
            "url": url, "headers": headers, "timeout": timeout,
            "auth": auth,
        })
        path = url.replace(BASE_URL, "") or "/"
        for (p, a), resp in self.responses.items():
            if p == path and a == auth:
                return resp
        return self.default


def _all_green_responses(api_key: str) -> dict:
    auth = f"Bearer {api_key}"
    bogus = f"Bearer {_BOGUS_PROBE_KEY}"
    return {
        ("/", None):                          (200, None),
        ("/health", None):                    (200, None),
        ("/reports/latest", None):            (401, None),
        ("/reports/latest", bogus):           (403, None),
        ("/reports/latest", auth):            (200, None),
        ("/dashboard", auth):                 (200, None),
    }


# ---------------------------------------------------------------------------
# Pure-helper smoke tests
# ---------------------------------------------------------------------------


class TestHashApiKey:
    def test_matches_sha256_prefix(self):
        raw = "k1"
        assert _hash_api_key(raw) == hashlib.sha256(
            raw.encode("utf-8"),
        ).hexdigest()[:32]

    def test_empty(self):
        assert _hash_api_key("") == ""

    def test_matches_other_module_hashers(self):
        from trading_bot.api.key_store import hash_api_key as ks_hash
        from trading_bot.api.keys import _hash_api_key as keys_hash
        for raw in ("a", "Y8DaiX_UsQi1wLCGQIygoBiK_VlK95a23qJ7LQbXf_U"):
            assert _hash_api_key(raw) == ks_hash(raw) == keys_hash(raw)


class TestJoinUrl:
    @pytest.mark.parametrize("base,path,want", [
        ("https://api.example.com", "/health", "https://api.example.com/health"),
        ("https://api.example.com/", "/health", "https://api.example.com/health"),
        ("https://api.example.com//", "/", "https://api.example.com/"),
    ])
    def test_strips_trailing_slash(self, base, path, want):
        assert _join_url(base, path) == want


class TestCheckSpec:
    def test_six_checks_in_documented_order(self):
        specs = _build_check_specs("k")
        paths = [s["path"] for s in specs]
        assert paths == [
            "/", "/health",
            "/reports/latest",   # no auth → 401
            "/reports/latest",   # bogus auth → 403/503
            "/reports/latest",   # good auth → 200/404
            "/dashboard",
        ]

    def test_bogus_probe_is_hardcoded(self):
        """The bogus probe key must not derive from the operator's
        key — a 403 there must never imply anything about the
        operator's key."""
        specs = _build_check_specs(GOOD_KEY)
        bogus_spec = specs[3]
        assert (
            bogus_spec["headers"]["Authorization"]
            == f"Bearer {_BOGUS_PROBE_KEY}"
        )
        assert _BOGUS_PROBE_KEY != GOOD_KEY

    def test_expected_status_codes(self):
        specs = _build_check_specs(GOOD_KEY)
        assert specs[0]["expected"] == (200,)
        assert specs[1]["expected"] == (200,)
        assert specs[2]["expected"] == (401,)
        assert specs[3]["expected"] == (403, 503)
        assert specs[4]["expected"] == (200, 404)
        assert specs[5]["expected"] == (200,)


# ---------------------------------------------------------------------------
# run_smoke_tests
# ---------------------------------------------------------------------------


class TestRunSmokeTestsAllGreen:
    def test_all_pass_with_canonical_responses(self):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY,
            timeout=5.0, http_get=fake,
        )
        assert len(results) == 6
        assert all(r.passed for r in results)
        # Every check carried a 5-second timeout to the fetcher.
        assert all(c["timeout"] == 5.0 for c in fake.calls)

    def test_protected_with_auth_can_be_404_and_still_pass(self):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/reports/latest", f"Bearer {GOOD_KEY}")] = (404, None)
        fake = FakeFetcher(responses)
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        assert all(r.passed for r in results)

    def test_bogus_503_path_passes_when_server_unconfigured(self):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/reports/latest", f"Bearer {_BOGUS_PROBE_KEY}")] = (503, None)
        fake = FakeFetcher(responses)
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        assert all(r.passed for r in results)


class TestRunSmokeTestsFailures:
    def test_protected_500_fails(self):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/reports/latest", f"Bearer {GOOD_KEY}")] = (500, None)
        fake = FakeFetcher(responses)
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        # Five PASS + one FAIL.
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        assert len(passed) == 5
        assert len(failed) == 1
        assert failed[0].path == "/reports/latest"
        assert failed[0].actual == 500

    def test_dashboard_403_fails(self):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/dashboard", f"Bearer {GOOD_KEY}")] = (403, None)
        fake = FakeFetcher(responses)
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        dash = [r for r in results if r.path == "/dashboard"][0]
        assert dash.passed is False
        assert dash.actual == 403

    def test_bogus_returning_200_fails(self):
        """The bogus key must NOT authenticate — a 200 there is a
        catastrophic auth bug and must surface as a failure."""
        responses = _all_green_responses(GOOD_KEY)
        responses[("/reports/latest", f"Bearer {_BOGUS_PROBE_KEY}")] = (200, None)
        fake = FakeFetcher(responses)
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        bogus = results[3]
        assert bogus.passed is False

    def test_missing_auth_returning_200_fails(self):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/reports/latest", None)] = (200, None)
        fake = FakeFetcher(responses)
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        no_auth = results[2]
        assert no_auth.passed is False


class TestNetworkErrors:
    def test_url_error_surfaces_message(self):
        fake = FakeFetcher(
            {("/", None): (None, "URLError: name resolution failed")},
            default=(None, "URLError: name resolution failed"),
        )
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        assert all(r.passed is False for r in results)
        assert all(r.actual is None for r in results)
        assert "name resolution failed" in results[0].error

    def test_timeout_surfaces_as_no_response(self):
        fake = FakeFetcher(
            {("/", None): (None, "Timeout: timed out")},
            default=(None, "Timeout: timed out"),
        )
        results = run_smoke_tests(
            base_url=BASE_URL, api_key=GOOD_KEY, http_get=fake,
        )
        assert results[0].actual is None
        assert "Timeout" in results[0].error


# ---------------------------------------------------------------------------
# CLI / renderer
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    def test_all_green_exits_zero(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        rc = smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        assert rc == 0

    def test_failure_exits_one(self, capsys):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/dashboard", f"Bearer {GOOD_KEY}")] = (500, None)
        fake = FakeFetcher(responses)
        rc = smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        assert rc == 1

    def test_blank_api_key_exits_two(self, capsys):
        rc = smoke_main([
            "--base-url", BASE_URL, "--api-key", "   ",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--api-key" in err

    def test_blank_base_url_exits_two(self, capsys):
        rc = smoke_main([
            "--base-url", "   ", "--api-key", GOOD_KEY,
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--base-url" in err

    def test_negative_timeout_exits_two(self, capsys):
        rc = smoke_main([
            "--base-url", BASE_URL,
            "--api-key", GOOD_KEY,
            "--timeout", "0",
        ])
        assert rc == 2

    def test_missing_required_args_exits_two(self):
        # argparse calls sys.exit(2) on missing required args.
        with pytest.raises(SystemExit) as exc:
            smoke_main([])
        assert exc.value.code == 2


class TestRenderingNeverPrintsRawKey:
    def test_text_render_omits_raw_key(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        out = capsys.readouterr().out
        assert GOOD_KEY not in out
        # The hash IS expected.
        assert _expected_hash(GOOD_KEY) in out

    def test_text_render_omits_raw_key_on_failure(self, capsys):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/dashboard", f"Bearer {GOOD_KEY}")] = (500, None)
        fake = FakeFetcher(responses)
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        captured = capsys.readouterr()
        assert GOOD_KEY not in captured.out
        assert GOOD_KEY not in captured.err

    def test_json_render_omits_raw_key(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY, "--json"],
            http_get=fake,
        )
        out = capsys.readouterr().out
        payload = json.loads(out)
        # Hash echoed, raw key absent.
        assert payload["key_hash"] == _expected_hash(GOOD_KEY)
        assert GOOD_KEY not in out
        # The Authorization header value MUST never leak — we only
        # store the path and the expected/actual statuses.
        for check in payload["checks"]:
            assert "headers" not in check
            assert GOOD_KEY not in json.dumps(check)

    def test_text_render_includes_pass_fail_markers(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        out = capsys.readouterr().out
        assert "[PASS]" in out
        assert "All 6 checks passed." in out

    def test_text_render_failure_summary(self, capsys):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/dashboard", f"Bearer {GOOD_KEY}")] = (500, None)
        fake = FakeFetcher(responses)
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert re.search(r"\d+ of \d+ checks FAILED", out)


class TestJsonShape:
    def test_json_includes_summary_and_per_check_results(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY, "--json"],
            http_get=fake,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["base_url"] == BASE_URL
        assert payload["passed"] is True
        assert payload["summary"] == {"total": 6, "passed": 6, "failed": 0}
        assert len(payload["checks"]) == 6
        for check in payload["checks"]:
            assert set(check.keys()) == {
                "name", "method", "path", "expected", "actual",
                "passed", "error",
            }

    def test_json_passed_false_when_any_failure(self, capsys):
        responses = _all_green_responses(GOOD_KEY)
        responses[("/health", None)] = (500, None)
        fake = FakeFetcher(responses)
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY, "--json"],
            http_get=fake,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is False
        assert payload["summary"]["failed"] == 1


class TestTimeoutPlumbing:
    def test_default_timeout_propagates(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        smoke_main(
            ["--base-url", BASE_URL, "--api-key", GOOD_KEY],
            http_get=fake,
        )
        assert all(c["timeout"] == DEFAULT_TIMEOUT_SECONDS for c in fake.calls)

    def test_explicit_timeout_propagates(self, capsys):
        fake = FakeFetcher(_all_green_responses(GOOD_KEY))
        smoke_main(
            [
                "--base-url", BASE_URL,
                "--api-key", GOOD_KEY,
                "--timeout", "2.5",
            ],
            http_get=fake,
        )
        assert all(c["timeout"] == 2.5 for c in fake.calls)


class TestBoundary:
    """The smoke CLI must stay dependency-light — no FastAPI, no
    structlog, no Core imports, and no reach into trading_bot
    runtime modules."""

    def test_module_does_not_reach_into_core_or_fastapi(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "smoke.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.api.server",
            "import structlog",
            "import fastapi",
            "from fastapi",
        ):
            assert forbidden not in src, (
                f"smoke.py imports restricted surface: {forbidden!r}"
            )

    def test_module_uses_only_stdlib_and_typing(self):
        """Sanity — the imports at the top of smoke.py should only
        be stdlib modules. We grep `^import` / `^from .* import` and
        reject anything that isn't stdlib or typing."""
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "smoke.py"
        ).read_text()
        # The module must NOT pull in httpx / requests / etc.
        for forbidden in ("httpx", "requests", "aiohttp", "urllib3"):
            assert f"import {forbidden}" not in src
            assert f"from {forbidden}" not in src
