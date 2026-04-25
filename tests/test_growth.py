"""
Phase 5.1 tests — growth loop tracking.

Exercises the pure :mod:`trading_bot.api.growth` helpers plus the
end-to-end integration through ``trading_bot.api.server`` when an
authenticated request carries a ``?ref=<code>`` query parameter.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.api import growth as growth_mod
from trading_bot.api.growth import (
    DEDUP_WINDOW_HOURS,
    DEDUP_WINDOW_SECONDS,
    DEFAULT_GROWTH_LOG_PATH,
    GROWTH_LOG_ENV_VAR,
    REF_CODE_MAX_LENGTH,
    _hash_api_key,
    _sanitize_ref_code,
    main as growth_main,
    record_growth_event,
    reset_cache_for_tests,
    summarize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_growth_env(monkeypatch, tmp_path: Path):
    """Each test starts with an empty env + in-memory dedup set."""
    monkeypatch.delenv(GROWTH_LOG_ENV_VAR, raising=False)
    target = tmp_path / "api_growth.jsonl"
    monkeypatch.setenv(GROWTH_LOG_ENV_VAR, str(target))
    reset_cache_for_tests()
    yield target
    reset_cache_for_tests()


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHashApiKey:
    def test_empty_is_empty(self):
        assert _hash_api_key(None) == ""
        assert _hash_api_key("") == ""

    def test_matches_sha256_prefix(self):
        k = "my-api-key"
        expected = hashlib.sha256(k.encode("utf-8")).hexdigest()[:32]
        assert _hash_api_key(k) == expected

    def test_agrees_with_server_hash(self):
        """Join compatibility — growth events must be joinable with
        the Phase 4.6 usage log on the same hash column."""
        from trading_bot.api.server import _hash_api_key as server_hash
        for k in ("a", "longer-key-with-dashes_123", "very" + "z" * 250):
            assert _hash_api_key(k) == server_hash(k)

    def test_agrees_with_conversion_hash(self):
        from trading_bot.api.conversion import _hash_api_key as conv_hash
        assert _hash_api_key("x") == conv_hash("x")


class TestSanitizeRefCode:
    def test_empty_is_empty(self):
        assert _sanitize_ref_code(None) == ""
        assert _sanitize_ref_code("") == ""

    def test_safe_chars_pass_through(self):
        assert _sanitize_ref_code("hn_launch-2025") == "hn_launch-2025"
        assert _sanitize_ref_code("email:spring.campaign") == "email:spring.campaign"

    def test_strips_unsafe_chars(self):
        assert _sanitize_ref_code("ref<script>alert(1)</script>") == "refscriptalert1script"
        assert "\n" not in _sanitize_ref_code("newline\ninside")
        assert " " not in _sanitize_ref_code("has spaces")

    def test_length_cap(self):
        huge = "a" * 500
        assert len(_sanitize_ref_code(huge)) == REF_CODE_MAX_LENGTH

    def test_only_forbidden_yields_empty(self):
        assert _sanitize_ref_code("!!!@@@ ???") == ""


# ---------------------------------------------------------------------------
# record_growth_event — happy path
# ---------------------------------------------------------------------------


class TestRecordGrowthEvent:
    def test_writes_a_record(self, clean_growth_env):
        result = record_growth_event(
            api_key="user-A",
            ref_code="hn_launch",
            path="/reports/latest",
            request_id="rid-1",
            now=datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert result["action"] == "recorded"
        records = _read(clean_growth_env)
        assert len(records) == 1
        rec = records[0]
        assert rec["api_key_hash"] == _hash_api_key("user-A")
        assert rec["ref_code"] == "hn_launch"
        assert rec["path"] == "/reports/latest"
        assert rec["request_id"] == "rid-1"
        assert rec["timestamp"] == "2026-04-24T10:00:00.000000Z"

    def test_returns_sanitized_ref_code(self, clean_growth_env):
        result = record_growth_event(
            api_key="user-B",
            ref_code="<script>abc</script>",
            path="/reports/latest",
        )
        assert result["action"] == "recorded"
        assert "<" not in result["ref_code"]

    def test_skips_missing_api_key(self, clean_growth_env):
        """Empty / None keys are skipped. Whitespace-only keys would
        never reach this helper in production (``require_api_key``
        rejects them at the auth layer), so we only check the two
        documented empty cases."""
        for key in (None, ""):
            r = record_growth_event(
                api_key=key, ref_code="ref", path="/x",
            )
            assert r["action"] == "skipped"
        assert _read(clean_growth_env) == []

    def test_skips_missing_ref_code(self, clean_growth_env):
        for ref in (None, "", "!!!@@@"):
            r = record_growth_event(
                api_key="user", ref_code=ref, path="/x",
            )
            assert r["action"] == "skipped"
        assert _read(clean_growth_env) == []

    def test_works_across_endpoints(self, clean_growth_env):
        """Same user, same ref, different paths within 24h — only
        first call is recorded (dedup is per pair, not per path)."""
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        record_growth_event(
            api_key="user-C", ref_code="hn", path="/reports/latest", now=base,
        )
        record_growth_event(
            api_key="user-C", ref_code="hn", path="/dashboard",
            now=base + timedelta(minutes=5),
        )
        records = _read(clean_growth_env)
        assert len(records) == 1
        assert records[0]["path"] == "/reports/latest"

    def test_record_is_json_serializable(self, clean_growth_env):
        record_growth_event(
            api_key="u", ref_code="r", path="/p", request_id="rid",
        )
        blob = clean_growth_env.read_text(encoding="utf-8")
        parsed = json.loads(blob.splitlines()[0])
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Dedup window
# ---------------------------------------------------------------------------


class TestDedup:
    def test_duplicate_within_window_is_deduped(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        r1 = record_growth_event(
            api_key="u", ref_code="hn", path="/x", now=base,
        )
        r2 = record_growth_event(
            api_key="u", ref_code="hn", path="/y",
            now=base + timedelta(hours=23, minutes=59),
        )
        assert r1["action"] == "recorded"
        assert r2["action"] == "deduped"
        assert len(_read(clean_growth_env)) == 1

    def test_boundary_exactly_24h_is_still_deduped(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        record_growth_event(
            api_key="u", ref_code="hn", path="/x", now=base,
        )
        # Exactly 24h later — strict less-than window, so this is
        # the boundary. We document: boundary is OUTSIDE the window
        # (i.e., accepted as a new event).
        r = record_growth_event(
            api_key="u", ref_code="hn", path="/y",
            now=base + timedelta(seconds=DEDUP_WINDOW_SECONDS),
        )
        assert r["action"] == "recorded"
        assert len(_read(clean_growth_env)) == 2

    def test_after_window_records_again(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        record_growth_event(
            api_key="u", ref_code="hn", path="/x", now=base,
        )
        r2 = record_growth_event(
            api_key="u", ref_code="hn", path="/x",
            now=base + timedelta(hours=25),
        )
        assert r2["action"] == "recorded"
        assert len(_read(clean_growth_env)) == 2

    def test_different_ref_same_user_is_separate(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        record_growth_event(api_key="u", ref_code="hn", path="/x", now=base)
        r2 = record_growth_event(
            api_key="u", ref_code="twitter", path="/x",
            now=base + timedelta(minutes=5),
        )
        assert r2["action"] == "recorded"
        assert len(_read(clean_growth_env)) == 2

    def test_same_ref_different_users_both_recorded(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        r1 = record_growth_event(api_key="u1", ref_code="hn", path="/x", now=base)
        r2 = record_growth_event(api_key="u2", ref_code="hn", path="/x", now=base)
        assert r1["action"] == "recorded"
        assert r2["action"] == "recorded"
        assert len(_read(clean_growth_env)) == 2

    def test_dedup_survives_process_restart(self, clean_growth_env):
        """Pre-populate a recent event, clear the cache (simulating a
        process restart), and confirm the next call is still deduped.

        ``_ensure_dedup_loaded`` filters disk rows by the REAL wall
        clock (24h dedup window), so this test must freeze the wall
        clock to ``base + 30min`` — otherwise the row written at
        ``base`` would be filtered out as out-of-window the moment
        the test runs more than 24h after ``base``."""
        from freezegun import freeze_time
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        with freeze_time(base + timedelta(minutes=30)):
            record_growth_event(api_key="u", ref_code="hn", path="/x", now=base)
            reset_cache_for_tests()
            r2 = record_growth_event(
                api_key="u", ref_code="hn", path="/y",
                now=base + timedelta(minutes=30),
            )
        assert r2["action"] == "deduped"
        # Still only one row on disk.
        assert len(_read(clean_growth_env)) == 1

    def test_dedup_cache_ignores_records_older_than_window(
        self, clean_growth_env
    ):
        """Records older than the window are NOT loaded into the
        dedup cache on restart — their owners can record again
        immediately."""
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        # Manually seed the file with a 3-day-old record.
        old_ts = (base - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        clean_growth_env.write_text(
            json.dumps({
                "timestamp": old_ts,
                "api_key_hash": _hash_api_key("u"),
                "ref_code": "hn",
                "path": "/x",
                "request_id": None,
            }) + "\n",
            encoding="utf-8",
        )
        reset_cache_for_tests()
        r = record_growth_event(
            api_key="u", ref_code="hn", path="/x", now=base,
        )
        # Old record is outside the 24h window → new record accepted.
        assert r["action"] == "recorded"

    def test_concurrent_same_pair_produces_one_row(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker():
            r = record_growth_event(
                api_key="u-race", ref_code="hn", path="/x", now=base,
            )
            with lock:
                outcomes.append(r["action"])

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert outcomes.count("recorded") == 1
        assert outcomes.count("deduped") == 24
        assert len(_read(clean_growth_env)) == 1


# ---------------------------------------------------------------------------
# Leakage — no raw key, no PII, no secrets
# ---------------------------------------------------------------------------


class TestPrivacyGuards:
    def test_raw_api_key_never_in_file(self, clean_growth_env):
        marker = "UNIQUE_MARKER_KEY_super_SECRET_9d8e"
        record_growth_event(
            api_key=marker, ref_code="hn", path="/x",
        )
        raw = clean_growth_env.read_text(encoding="utf-8")
        assert marker not in raw
        assert "super_SECRET_9d8e" not in raw

    def test_dangerous_ref_code_sanitized_before_write(
        self, clean_growth_env
    ):
        marker = "SCRIPT_LEAK_XYZ_9e"
        record_growth_event(
            api_key="u",
            ref_code=f"<script>{marker}</script>",
            path="/x",
        )
        raw = clean_growth_env.read_text(encoding="utf-8")
        # The marker text is harmless alphanumeric — it remains.
        # The structural characters (< > /) must be stripped.
        assert "<" not in raw
        assert ">" not in raw
        assert "<script" not in raw

    def test_no_auth_or_card_words_end_up_in_file(
        self, clean_growth_env
    ):
        record_growth_event(
            api_key="user", ref_code="hn_launch",
            path="/reports/latest",
        )
        raw = clean_growth_env.read_text(encoding="utf-8").lower()
        for forbidden in (
            "authorization", "bearer", "password", "email",
            "pan", "cvv", "card",
        ):
            assert forbidden not in raw, f"leaked {forbidden!r}"


# ---------------------------------------------------------------------------
# summarize + CLI
# ---------------------------------------------------------------------------


class TestSummarize:
    @pytest.fixture
    def populated(self, clean_growth_env):
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        rows = [
            ("u1", "hn_launch",       "/reports/latest", 0),
            ("u2", "hn_launch",       "/dashboard",      1),
            ("u3", "hn_launch",       "/reports/latest", 2),
            ("u1", "twitter_q2",      "/reports/latest", 3),
            ("u2", "twitter_q2",      "/reports/latest", 4),
            ("u4", "email_campaign",  "/reports/latest", 5),
        ]
        for i, (user, ref, path, dh) in enumerate(rows):
            record_growth_event(
                api_key=user, ref_code=ref, path=path,
                now=base + timedelta(hours=dh),
            )
        return clean_growth_env

    def test_total_events(self, populated):
        summary = summarize(populated)
        assert summary["total_events"] == 6

    def test_distinct_refs(self, populated):
        summary = summarize(populated)
        assert summary["distinct_refs"] == 3

    def test_distinct_users(self, populated):
        summary = summarize(populated)
        assert summary["distinct_users"] == 4

    def test_per_ref_counts(self, populated):
        summary = summarize(populated)
        per_ref = {r["ref_code"]: r for r in summary["refs"]}
        assert per_ref["hn_launch"]["events"] == 3
        assert per_ref["hn_launch"]["unique_users"] == 3
        assert per_ref["hn_launch"]["paths"] == 2
        assert per_ref["twitter_q2"]["events"] == 2
        assert per_ref["twitter_q2"]["unique_users"] == 2
        assert per_ref["email_campaign"]["events"] == 1
        assert per_ref["email_campaign"]["unique_users"] == 1

    def test_sorted_descending_by_events(self, populated):
        summary = summarize(populated)
        events = [r["events"] for r in summary["refs"]]
        assert events == sorted(events, reverse=True)

    def test_top_n_limit(self, populated):
        summary = summarize(populated, limit=2)
        assert len(summary["refs"]) == 2
        # The top two refs by event count should be hn_launch then twitter_q2.
        assert [r["ref_code"] for r in summary["refs"]] == [
            "hn_launch", "twitter_q2",
        ]

    def test_empty_file_produces_zero_summary(self, clean_growth_env):
        summary = summarize(clean_growth_env)
        assert summary == {
            "total_events": 0,
            "distinct_refs": 0,
            "distinct_users": 0,
            "refs": [],
        }

    def test_missing_file_produces_zero_summary(self, tmp_path: Path):
        summary = summarize(tmp_path / "does_not_exist.jsonl")
        assert summary["total_events"] == 0
        assert summary["refs"] == []

    def test_malformed_lines_are_skipped(self, clean_growth_env):
        # Manually mix good + bad lines.
        clean_growth_env.write_text(
            "not json\n"
            '{"timestamp": "2026-04-24T10:00:00.000000Z", "api_key_hash": "h", "ref_code": "r", "path": "/x", "request_id": null}\n'
            "{partial\n",
            encoding="utf-8",
        )
        reset_cache_for_tests()
        summary = summarize(clean_growth_env)
        assert summary["total_events"] == 1


class TestCli:
    def test_summary_text_output(
        self, clean_growth_env, capsys, monkeypatch
    ):
        # Seed a couple of events.
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        record_growth_event(
            api_key="u1", ref_code="hn", path="/x", now=base,
        )
        record_growth_event(
            api_key="u2", ref_code="hn", path="/y",
            now=base + timedelta(hours=1),
        )
        rc = growth_main(["--summary"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total growth events    = 2" in out
        assert "Distinct ref codes     = 1" in out
        assert "Distinct unique users  = 2" in out
        assert "hn" in out

    def test_summary_json_output(self, clean_growth_env, capsys):
        record_growth_event(api_key="u", ref_code="hn", path="/x")
        rc = growth_main(["--summary", "--json"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["total_events"] == 1
        assert parsed["refs"][0]["ref_code"] == "hn"

    def test_missing_summary_flag_returns_2(self, capsys):
        rc = growth_main([])
        assert rc == 2

    def test_empty_log_summary(self, clean_growth_env, capsys):
        rc = growth_main(["--summary"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total growth events    = 0" in out
        assert "(no ref-code activity recorded)" in out

    def test_custom_path_via_flag(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        custom = tmp_path / "custom.jsonl"
        custom.write_text(
            json.dumps({
                "timestamp": "2026-04-24T10:00:00.000000Z",
                "api_key_hash": _hash_api_key("u"),
                "ref_code": "custom_ref",
                "path": "/x",
                "request_id": None,
            }) + "\n",
            encoding="utf-8",
        )
        # No reset because summarize reads directly from the path arg.
        rc = growth_main(["--summary", "--path", str(custom), "--json"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["total_events"] == 1
        assert parsed["refs"][0]["ref_code"] == "custom_ref"

    def test_subprocess_smoke(self, clean_growth_env):
        record_growth_event(api_key="u", ref_code="hn", path="/x")
        # Pass the growth log path through the child process env.
        env = dict(os.environ)
        env[GROWTH_LOG_ENV_VAR] = str(clean_growth_env)
        result = subprocess.run(
            [sys.executable, "-m", "trading_bot.api.growth",
             "--summary", "--json"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["total_events"] == 1


# ---------------------------------------------------------------------------
# End-to-end integration with the FastAPI server
# ---------------------------------------------------------------------------


class TestServerIntegration:
    """Issue real HTTP requests through the SaaS API + verify the
    growth log mirrors the authenticated ones that carried ``?ref=``."""

    @pytest.fixture
    def client_env(self, monkeypatch, tmp_path: Path, clean_growth_env):
        from trading_bot.api import server as srv
        from trading_bot.api import billing
        billing.reset_cache_for_tests()
        srv._reset_rate_limit_bucket()
        # API server env
        monkeypatch.setenv("TRADING_API_KEY", "user-free")
        monkeypatch.setenv("TRADING_API_PREMIUM_KEYS", "user-premium")
        monkeypatch.setenv(
            "TRADING_API_REPORTS_DIR", str(tmp_path / "reports"),
        )
        monkeypatch.setenv(
            "TRADING_API_MANIFEST_PATH", str(tmp_path / "m.jsonl"),
        )
        monkeypatch.setenv(
            "TRADING_API_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"),
        )
        monkeypatch.setenv(
            "TRADING_API_USAGE_LOG_PATH", str(tmp_path / "usage.jsonl"),
        )
        (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
        from fastapi.testclient import TestClient
        return TestClient(srv.app), clean_growth_env

    def test_auth_with_ref_records_event(self, client_env):
        client, growth_file = client_env
        resp = client.get(
            "/experiments/recent?ref=hn_launch",
            headers={"Authorization": "Bearer user-premium"},
        )
        assert resp.status_code == 200
        records = _read(growth_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["ref_code"] == "hn_launch"
        assert rec["path"] == "/experiments/recent"
        assert rec["api_key_hash"] == _hash_api_key("user-premium")
        # Request-id mirrored from the response header.
        assert rec["request_id"] == resp.headers.get("X-Request-ID")

    def test_auth_without_ref_records_nothing(self, client_env):
        client, growth_file = client_env
        resp = client.get(
            "/experiments/recent",
            headers={"Authorization": "Bearer user-premium"},
        )
        assert resp.status_code == 200
        assert _read(growth_file) == []

    def test_unauthenticated_request_with_ref_records_nothing(
        self, client_env
    ):
        client, growth_file = client_env
        # 401 because no Authorization — must not create a growth row.
        resp = client.get("/experiments/recent?ref=hn_launch")
        assert resp.status_code == 401
        assert _read(growth_file) == []

    def test_public_endpoint_with_ref_records_nothing(self, client_env):
        """/health is public — no api_key on request.state → no growth."""
        client, growth_file = client_env
        resp = client.get("/health?ref=hn_launch")
        assert resp.status_code == 200
        assert _read(growth_file) == []

    def test_ref_across_multiple_endpoints_first_wins(self, client_env):
        """Same (user, ref) on /experiments and /dashboard within the
        dedup window → one row captured on the first hit."""
        client, growth_file = client_env
        client.get(
            "/experiments/recent?ref=twitter_q2",
            headers={"Authorization": "Bearer user-premium"},
        )
        client.get(
            "/dashboard?ref=twitter_q2",
            headers={"Authorization": "Bearer user-premium"},
        )
        records = _read(growth_file)
        assert len(records) == 1
        assert records[0]["path"] == "/experiments/recent"

    def test_two_different_refs_same_user_two_records(self, client_env):
        client, growth_file = client_env
        client.get(
            "/experiments/recent?ref=hn",
            headers={"Authorization": "Bearer user-premium"},
        )
        client.get(
            "/experiments/recent?ref=twitter",
            headers={"Authorization": "Bearer user-premium"},
        )
        assert len(_read(growth_file)) == 2

    def test_does_not_break_request_if_growth_write_fails(
        self, client_env, monkeypatch
    ):
        """Stub record_growth_event to raise; the request must still
        return 200 and the response body must be unchanged."""
        client, growth_file = client_env

        def boom(**kw):
            raise RuntimeError("simulated growth failure")

        monkeypatch.setattr(growth_mod, "record_growth_event", boom)

        resp = client.get(
            "/experiments/recent?ref=hn",
            headers={"Authorization": "Bearer user-premium"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID") is not None

    def test_bogus_ref_chars_sanitized_in_file(self, client_env):
        client, growth_file = client_env
        resp = client.get(
            "/experiments/recent?ref=evil%3Cscript%3EXSS_MARKER",
            headers={"Authorization": "Bearer user-premium"},
        )
        assert resp.status_code == 200
        records = _read(growth_file)
        assert len(records) == 1
        # The raw '<script>' was stripped; only safe chars remain.
        assert "<script>" not in json.dumps(records[0])
        assert records[0]["ref_code"] == "evilscriptXSS_MARKER"


# ---------------------------------------------------------------------------
# Boundary — growth module must not import Core
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_growth_module_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "growth.py"
        ).read_text()
        for pat in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert pat not in src

    def test_dedup_window_constant_is_24h(self):
        assert DEDUP_WINDOW_HOURS == 24
        assert DEDUP_WINDOW_SECONDS == 86_400

    def test_default_log_path_is_documented(self):
        assert DEFAULT_GROWTH_LOG_PATH == "data/api_growth.jsonl"
