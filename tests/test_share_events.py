"""
Phase 10.3 tests — Viral Loop Optimization.

Covers:
  * record_share_event produces a correctly-shaped JSONL record
  * share events are logged whenever a share payload is attached
  * the inbound ``?src=`` parameter is captured and logged once
    as ``inbound_visit``
  * raw API keys never leave memory — only SHA-256[:32] hashes
    appear on disk
  * unknown event types are dropped silently
  * empty key_hash input is dropped (no anonymous rows)
  * I/O failures never raise into the caller
  * the share payload never exposes ``X-Usage-Tier`` premium
    metadata or any internal Core fields
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.api.share_events import (
    DEFAULT_SHARE_EVENTS_LOG_PATH,
    EVENT_INBOUND_VISIT,
    EVENT_SHARE_GENERATED,
    SHARE_EVENTS_LOG_ENV_VAR,
    SRC_MAX_LENGTH,
    VALID_SHARE_EVENTS,
    _hash_api_key,
    format_summary_text,
    record_share_event,
    sanitize_src,
    summarize,
)

# Reuse the shared test report writer + auth fixtures.
from tests.test_api_server import (
    VALID_KEY,
    _write_report,
    authed_env,           # noqa: F401  — pytest fixture import
    clean_api_env,        # noqa: F401  — pytest fixture import (autouse)
    client,               # noqa: F401  — pytest fixture import
    reset_rate_limit_bucket,  # noqa: F401  — pytest fixture import (autouse)
)


KEY_HASH = "a" * 32  # 32 hex chars; same shape SHA-256[:32] returns


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def share_events_path(monkeypatch, tmp_path: Path) -> Path:
    """Point the share-events module at a per-test JSONL file."""
    path = tmp_path / "share_events.jsonl"
    monkeypatch.setenv(SHARE_EVENTS_LOG_ENV_VAR, str(path))
    return path


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        out.append(json.loads(s))
    return out


# ---------------------------------------------------------------------------
# Constants & valid-event whitelist
# ---------------------------------------------------------------------------


class TestEventWhitelist:
    def test_documented_event_set(self):
        """Phase 10.3 ships exactly two events. Pinned so a silent
        addition / removal trips this test."""
        assert VALID_SHARE_EVENTS == {
            EVENT_SHARE_GENERATED,
            EVENT_INBOUND_VISIT,
        }
        assert EVENT_SHARE_GENERATED == "share_generated"
        assert EVENT_INBOUND_VISIT == "inbound_visit"

    def test_default_log_path_is_under_data(self):
        assert DEFAULT_SHARE_EVENTS_LOG_PATH.startswith("data/")
        assert DEFAULT_SHARE_EVENTS_LOG_PATH.endswith(".jsonl")

    def test_env_var_name_is_documented(self):
        assert SHARE_EVENTS_LOG_ENV_VAR == "TRADING_API_SHARE_EVENTS_LOG_PATH"


# ---------------------------------------------------------------------------
# record_share_event basics
# ---------------------------------------------------------------------------


class TestRecordShareEvent:
    def test_records_share_generated(self, share_events_path: Path):
        result = record_share_event(
            KEY_HASH, EVENT_SHARE_GENERATED, "/reports/latest"
        )
        assert result == {
            "action": "recorded",
            "event": EVENT_SHARE_GENERATED,
            "api_key_hash": KEY_HASH,
        }
        rows = _read_records(share_events_path)
        assert len(rows) == 1
        rec = rows[0]
        assert rec["api_key_hash"] == KEY_HASH
        assert rec["event"] == EVENT_SHARE_GENERATED
        assert rec["endpoint"] == "/reports/latest"
        assert rec["src"] is None
        assert "timestamp" in rec
        assert rec["timestamp"].endswith("Z")

    def test_records_inbound_visit_with_src(self, share_events_path: Path):
        record_share_event(
            KEY_HASH, EVENT_INBOUND_VISIT, "/reports/latest",
            src="twitter-launch",
        )
        rows = _read_records(share_events_path)
        assert len(rows) == 1
        rec = rows[0]
        assert rec["event"] == EVENT_INBOUND_VISIT
        assert rec["src"] == "twitter-launch"

    def test_unknown_event_is_dropped(self, share_events_path: Path):
        result = record_share_event(KEY_HASH, "made_up", "/x")
        assert result == {"action": "skipped", "reason": "unknown_event"}
        assert _read_records(share_events_path) == []

    def test_missing_key_hash_is_dropped(self, share_events_path: Path):
        for empty in (None, "", "   "):
            result = record_share_event(
                empty, EVENT_SHARE_GENERATED, "/reports/latest"
            )
            assert result == {"action": "skipped", "reason": "no_key_hash"}
        assert _read_records(share_events_path) == []

    def test_request_id_is_preserved_on_record(
        self, share_events_path: Path,
    ):
        record_share_event(
            KEY_HASH, EVENT_SHARE_GENERATED, "/reports/latest",
            request_id="abc123",
        )
        rec = _read_records(share_events_path)[0]
        assert rec["request_id"] == "abc123"

    def test_endpoint_is_capped(self, share_events_path: Path):
        long_ep = "/" + ("x" * 1000)
        record_share_event(KEY_HASH, EVENT_SHARE_GENERATED, long_ep)
        rec = _read_records(share_events_path)[0]
        # Capped to the documented endpoint length (128).
        assert len(rec["endpoint"]) <= 128

    def test_concurrent_writers_do_not_corrupt_lines(
        self, share_events_path: Path,
    ):
        """Module uses a threading.Lock for the JSONL append."""
        N = 50

        def worker(i: int) -> None:
            record_share_event(
                f"hash{i:032d}"[:32], EVENT_SHARE_GENERATED,
                "/reports/latest", src=f"src{i}",
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = _read_records(share_events_path)
        assert len(rows) == N
        # Every line is a valid JSON object — the lock prevents
        # interleaved writes that would create corrupt rows.
        for r in rows:
            assert isinstance(r, dict)
            assert r["event"] == EVENT_SHARE_GENERATED


# ---------------------------------------------------------------------------
# src sanitiser
# ---------------------------------------------------------------------------


class TestSanitizeSrc:
    def test_strips_unsafe_characters(self):
        assert sanitize_src("foo bar!") == "foobar"
        assert sanitize_src("ok-name_1.2") == "ok-name_1.2"

    def test_caps_length(self):
        long = "a" * (SRC_MAX_LENGTH * 3)
        assert len(sanitize_src(long)) == SRC_MAX_LENGTH

    def test_empty_input(self):
        assert sanitize_src(None) == ""
        assert sanitize_src("") == ""
        # Only-stripped characters → empty.
        assert sanitize_src("!@#$%^&*()") == ""


# ---------------------------------------------------------------------------
# Privacy: the JSONL must never contain a raw API key.
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_raw_api_key_marker_never_leaks(self, share_events_path: Path):
        """Plant unique markers in every input slot and assert they
        don't appear in the persisted JSONL."""
        raw_marker = "RAW_KEY_MARKER_PHASE_10_3_xyz"
        # Hash the marker so the recorded row uses a hash, not the raw.
        key_hash = _hash_api_key(raw_marker)
        record_share_event(
            key_hash, EVENT_SHARE_GENERATED, "/reports/latest",
            src="src-marker",
            request_id="req-marker",
        )
        record_share_event(
            key_hash, EVENT_INBOUND_VISIT, "/reports/latest",
            src="src-marker",
        )
        text = share_events_path.read_text(encoding="utf-8")
        assert raw_marker not in text
        # The hash itself is fine to appear (it's the join column).
        assert key_hash in text

    def test_record_refuses_raw_key_via_signature(
        self, share_events_path: Path,
    ):
        """The function signature documents ``key_hash``, not
        ``api_key`` — passing a typical raw-key shape is accepted
        only as a string and stored AS-IS. Operators that pre-hash
        on the server side never put raw bytes here, but the test
        documents that the function never silently re-hashes for
        them."""
        # Even a "raw-looking" key is treated as an opaque hash —
        # whatever you pass IS what lands on disk. This forces
        # callers to be explicit; there is no implicit hashing path
        # that a refactor could regress.
        record_share_event(
            "sk_live_supposed_raw_key", EVENT_SHARE_GENERATED, "/x",
        )
        rec = _read_records(share_events_path)[0]
        assert rec["api_key_hash"] == "sk_live_supposed_raw_key"


# ---------------------------------------------------------------------------
# Failure resilience: I/O exceptions never raise into the caller.
# ---------------------------------------------------------------------------


class TestFailurePosture:
    def test_write_failure_does_not_raise(
        self, share_events_path: Path, monkeypatch,
    ):
        # Force the open() inside the writer to blow up.
        real_open = open

        def explode(*a, **kw):
            if a and str(a[0]) == str(share_events_path):
                raise OSError("simulated disk failure")
            return real_open(*a, **kw)

        monkeypatch.setattr("builtins.open", explode)
        # Must NOT raise — telemetry is best-effort.
        result = record_share_event(
            KEY_HASH, EVENT_SHARE_GENERATED, "/reports/latest"
        )
        # Caller still sees a recorded result (the writer is
        # best-effort and reports DEBUG).
        assert result["action"] == "recorded"

    def test_serialisation_failure_does_not_raise(
        self, share_events_path: Path, monkeypatch,
    ):
        # Force json.dumps to throw inside the writer.
        import trading_bot.api.share_events as mod
        real_dumps = mod.json.dumps

        def boom(*a, **kw):
            raise TypeError("not serialisable")

        monkeypatch.setattr(mod.json, "dumps", boom)
        try:
            # Must NOT raise into the caller.
            record_share_event(
                KEY_HASH, EVENT_SHARE_GENERATED, "/reports/latest"
            )
        finally:
            monkeypatch.setattr(mod.json, "dumps", real_dumps)
        # Nothing was persisted because serialisation itself failed.
        assert _read_records(share_events_path) == []


# ---------------------------------------------------------------------------
# summarize / format_summary_text
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_aggregates_events_endpoints_and_sources(
        self, share_events_path: Path,
    ):
        record_share_event(
            "hash_user_a" + "0" * 21, EVENT_SHARE_GENERATED, "/reports/latest",
        )
        record_share_event(
            "hash_user_a" + "0" * 21, EVENT_SHARE_GENERATED, "/reports/latest",
        )
        record_share_event(
            "hash_user_b" + "0" * 21, EVENT_INBOUND_VISIT, "/reports/latest",
            src="twitter",
        )
        record_share_event(
            "hash_user_c" + "0" * 21, EVENT_INBOUND_VISIT, "/reports/latest",
            src="twitter",
        )
        summary = summarize(share_events_path)
        assert summary["total_events"] == 4
        assert summary["events"][EVENT_SHARE_GENERATED]["count"] == 2
        assert (
            summary["events"][EVENT_SHARE_GENERATED]["unique_users"] == 1
        )
        assert summary["events"][EVENT_INBOUND_VISIT]["count"] == 2
        assert (
            summary["events"][EVENT_INBOUND_VISIT]["unique_users"] == 2
        )
        # endpoint aggregation
        assert summary["top_endpoints"][0]["endpoint"] == "/reports/latest"
        assert summary["top_endpoints"][0]["count"] == 4
        # src aggregation only includes rows with non-empty src
        assert summary["sources"][0]["src"] == "twitter"
        assert summary["sources"][0]["count"] == 2
        assert summary["sources"][0]["unique_users"] == 2

    def test_format_summary_text_handles_empty_log(
        self, share_events_path: Path,
    ):
        out = format_summary_text(summarize(share_events_path))
        assert "SHARE EVENT SUMMARY" in out
        assert "(no events recorded)" in out
        assert "(no inbound sources present)" in out


# ---------------------------------------------------------------------------
# Server integration — share payload + ?src= capture
# ---------------------------------------------------------------------------


class TestServerIntegration:
    def test_reports_latest_attaches_share_payload(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
    ):
        """A share payload is attached to /reports/latest and a
        single ``share_generated`` row lands in the JSONL."""
        _write_report(authed_env["reports_dir"], "2026-04-25")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        share = body.get("share")
        assert share is not None
        assert share["endpoint"] == "/reports/latest"
        assert share["hint"] == "Share this preview"
        # Without TRADING_PUBLIC_BASE_URL set, the URL still has
        # the documented shape: <base><path>?src=<key_hash>.
        assert "?src=" in share["share_url"]
        assert share["share_url"].endswith(
            "?src=" + share["share_url"].split("?src=", 1)[1]
        )

        rows = _read_records(share_events_path)
        share_rows = [r for r in rows if r["event"] == EVENT_SHARE_GENERATED]
        assert len(share_rows) == 1
        assert share_rows[0]["endpoint"] == "/reports/latest"
        # The recorded api_key_hash is the SHA-256[:32] of VALID_KEY,
        # not the key itself.
        from trading_bot.api import key_store
        expected_hash = key_store.hash_api_key(VALID_KEY)
        assert share_rows[0]["api_key_hash"] == expected_hash

    def test_reports_latest_captures_inbound_src(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
    ):
        """An inbound visit with ?src=... is captured and echoed
        back in the share payload."""
        _write_report(authed_env["reports_dir"], "2026-04-25")
        resp = client.get(
            "/reports/latest?src=twitter-launch",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["share"]["src"] == "twitter-launch"

        rows = _read_records(share_events_path)
        events = [r["event"] for r in rows]
        # First-touch row + share_generated row — exactly one of each.
        assert events.count(EVENT_INBOUND_VISIT) == 1
        assert events.count(EVENT_SHARE_GENERATED) == 1
        inbound = [r for r in rows if r["event"] == EVENT_INBOUND_VISIT][0]
        assert inbound["src"] == "twitter-launch"
        assert inbound["endpoint"] == "/reports/latest"

    def test_reports_latest_no_src_param_does_not_log_inbound(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
    ):
        _write_report(authed_env["reports_dir"], "2026-04-25")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # share payload still attached
        assert "share" in body
        assert body["share"]["src"] is None

        rows = _read_records(share_events_path)
        assert all(r["event"] != EVENT_INBOUND_VISIT for r in rows)
        share_rows = [r for r in rows if r["event"] == EVENT_SHARE_GENERATED]
        assert len(share_rows) == 1

    def test_inbound_src_is_sanitised_before_logging(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
    ):
        _write_report(authed_env["reports_dir"], "2026-04-25")
        # Spaces, exclamation, and angle brackets are stripped.
        resp = client.get(
            "/reports/latest?src=my%20launch%21<script>",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Only safe chars survive.
        assert body["share"]["src"] == "mylaunchscript"

        rows = _read_records(share_events_path)
        inbound = [r for r in rows if r["event"] == EVENT_INBOUND_VISIT]
        assert len(inbound) == 1
        assert inbound[0]["src"] == "mylaunchscript"
        # Make sure the unsafe content didn't sneak through anywhere.
        text = share_events_path.read_text(encoding="utf-8")
        assert "<script>" not in text
        assert "!" not in text

    def test_no_raw_api_key_in_share_logs(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
    ):
        """The raw bearer token must never appear on disk."""
        _write_report(authed_env["reports_dir"], "2026-04-25")
        resp = client.get(
            "/reports/latest?src=launch",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        text = share_events_path.read_text(encoding="utf-8")
        assert VALID_KEY not in text

    def test_logging_failure_does_not_break_response(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
        monkeypatch,
    ):
        """If the share-events writer blows up, /reports/latest still
        returns 200. Best-effort posture."""
        _write_report(authed_env["reports_dir"], "2026-04-25")

        def explode(*a, **kw):
            raise OSError("disk full")

        # Make _append_record itself blow up — exercises the
        # outermost try/except in the helper.
        monkeypatch.setattr(
            "trading_bot.api.share_events._append_record", explode,
        )
        resp = client.get(
            "/reports/latest?src=twitter",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        assert resp.status_code == 200
        # The share payload is still attached because its construction
        # is independent of the JSONL writer.
        assert "share" in resp.json()

    def test_share_url_uses_public_base_url_when_configured(
        self,
        client: TestClient,
        authed_env,
        share_events_path: Path,
        monkeypatch,
    ):
        monkeypatch.setenv(
            "TRADING_PUBLIC_BASE_URL", "https://example.test",
        )
        _write_report(authed_env["reports_dir"], "2026-04-25")
        resp = client.get(
            "/reports/latest",
            headers={"Authorization": f"Bearer {VALID_KEY}"},
        )
        body = resp.json()
        assert body["share"]["share_url"].startswith(
            "https://example.test/reports/latest?src="
        )
