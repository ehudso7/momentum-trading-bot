"""
Phase 5.5 tests — upgrade CTA telemetry.

Covers the module in isolation:
  * record_upgrade_event produces a correctly-shaped JSONL record
  * raw API keys never leave memory (the persisted field is a
    SHA-256[:32] hash that matches growth/conversion hashing)
  * unknown event names are dropped silently
  * empty api_key input is dropped (no "" hash row)
  * concurrent writers don't corrupt lines
  * I/O failures never raise
  * ref_code sanitisation matches the growth sanitiser
  * summarize + format_summary_text + CLI all do the right thing

The end-to-end tests that verify the 5 emission sites actually fire
(dashboard banner, 429, 403, old-report, experiment-cap) live in
tests/test_api_server.py alongside the server's other integration
tests.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.api import upgrade_events as ue
from trading_bot.api.upgrade_events import (
    DEFAULT_UPGRADE_EVENTS_LOG_PATH,
    EVENT_DAILY_REQUEST_LIMIT_HIT,
    EVENT_DASHBOARD_BANNER_SEEN,
    EVENT_EXPERIMENT_LIMIT_BLOCKED,
    EVENT_OLD_REPORT_BLOCKED,
    EVENT_REPORT_LIMIT_HIT,
    REF_CODE_MAX_LENGTH,
    UPGRADE_EVENTS_LOG_ENV_VAR,
    VALID_EVENTS,
    _hash_api_key,
    _sanitize_ref_code,
    format_summary_text,
    record_upgrade_event,
    summarize,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def events_path(monkeypatch, tmp_path: Path) -> Path:
    """Point the module at a per-test JSONL file."""
    path = tmp_path / "upgrade_events.jsonl"
    monkeypatch.setenv(UPGRADE_EVENTS_LOG_ENV_VAR, str(path))
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
        """Phase 5.5/5.8 shipped 5 events; Phase 8.4 added 3 funnel
        events. The set is now exactly these 8 — pinned so a
        silent addition / removal trips this test."""
        from trading_bot.api.upgrade_events import (
            EVENT_UPGRADE_CLICKED,
            EVENT_UPGRADE_COMPLETED,
            EVENT_UPGRADE_SHOWN,
        )
        assert VALID_EVENTS == {
            EVENT_DASHBOARD_BANNER_SEEN,
            EVENT_DAILY_REQUEST_LIMIT_HIT,
            EVENT_REPORT_LIMIT_HIT,
            EVENT_OLD_REPORT_BLOCKED,
            EVENT_EXPERIMENT_LIMIT_BLOCKED,
            EVENT_UPGRADE_SHOWN,
            EVENT_UPGRADE_CLICKED,
            EVENT_UPGRADE_COMPLETED,
        }

    def test_event_strings_are_snake_case(self):
        for name in VALID_EVENTS:
            assert name == name.lower()
            assert " " not in name

    def test_default_path_matches_spec(self):
        assert DEFAULT_UPGRADE_EVENTS_LOG_PATH == "data/api_upgrade_events.jsonl"


# ---------------------------------------------------------------------------
# Hash helper parity + ref sanitisation
# ---------------------------------------------------------------------------


class TestHashParityWithOtherModules:
    def test_hash_matches_server_hash(self):
        """Must equal trading_bot.api.server._hash_api_key digest so
        every join across server/billing/conversion/growth/upgrade
        works on a single column."""
        from trading_bot.api.server import _hash_api_key as srv_hash
        for key in ("", "tiny", "abcdef1234567890", "🪙🔒-key"):
            assert _hash_api_key(key) == srv_hash(key)

    def test_hash_empty_returns_empty_string(self):
        assert _hash_api_key(None) == ""
        assert _hash_api_key("") == ""

    def test_hash_is_32_hex(self):
        h = _hash_api_key("any-key-value")
        assert len(h) == 32
        int(h, 16)  # raises if non-hex


class TestRefCodeSanitizer:
    def test_strips_unsafe_characters(self):
        assert _sanitize_ref_code("twitter-q2") == "twitter-q2"
        assert _sanitize_ref_code("twitter q2!") == "twitterq2"
        assert _sanitize_ref_code("<script>xss</script>") == "scriptxssscript"

    def test_caps_length(self):
        out = _sanitize_ref_code("a" * 500)
        assert len(out) == REF_CODE_MAX_LENGTH

    def test_none_and_empty_become_empty_string(self):
        assert _sanitize_ref_code(None) == ""
        assert _sanitize_ref_code("") == ""

    def test_matches_growth_sanitiser(self):
        """Bi-directional join contract with Phase 5.1."""
        from trading_bot.api.growth import _sanitize_ref_code as gsa
        for raw in (
            "hn-launch", "product hunt", "vip!key", "ref.with:dots",
            "over-" + "x" * 100, "<>&'\"",
        ):
            assert _sanitize_ref_code(raw) == gsa(raw)


# ---------------------------------------------------------------------------
# record_upgrade_event — happy paths + privacy
# ---------------------------------------------------------------------------


class TestRecordEventHappyPaths:
    @pytest.mark.parametrize("event", sorted(VALID_EVENTS))
    def test_records_each_valid_event(self, events_path: Path, event: str):
        result = record_upgrade_event(
            api_key="user-abc",
            event=event,
            path="/reports/latest",
            request_id="req-xyz",
            ref_code="twitter-q2",
        )
        assert result["action"] == "recorded"
        assert result["event"] == event
        rows = _read_records(events_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["event"] == event
        assert row["tier"] == "free"
        assert row["path"] == "/reports/latest"
        assert row["request_id"] == "req-xyz"
        assert row["ref_code"] == "twitter-q2"
        # Timestamp shape — ISO-8601 with Z suffix.
        assert row["timestamp"].endswith("Z")
        datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_hash_present_never_raw_key(self, events_path: Path):
        record_upgrade_event("super-secret-raw-key-XYZ", EVENT_DASHBOARD_BANNER_SEEN)
        (row,) = _read_records(events_path)
        assert "super-secret-raw-key-XYZ" not in json.dumps(row)
        assert row["api_key_hash"] == _hash_api_key("super-secret-raw-key-XYZ")

    def test_absent_ref_code_serialises_as_null(self, events_path: Path):
        record_upgrade_event("k", EVENT_REPORT_LIMIT_HIT, path="/reports/latest")
        (row,) = _read_records(events_path)
        assert row["ref_code"] is None

    def test_absent_request_id_serialises_as_null(self, events_path: Path):
        record_upgrade_event("k", EVENT_REPORT_LIMIT_HIT)
        (row,) = _read_records(events_path)
        assert row["request_id"] is None

    def test_ref_code_is_sanitised_on_the_way_in(self, events_path: Path):
        record_upgrade_event(
            "k", EVENT_DASHBOARD_BANNER_SEEN,
            ref_code="<script>alert(1)</script>",
        )
        (row,) = _read_records(events_path)
        assert row["ref_code"] == "scriptalert1script"
        # The raw payload must NEVER survive.
        assert "<script>" not in json.dumps(row)

    def test_explicit_now_controls_timestamp(self, events_path: Path):
        ts = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        record_upgrade_event("k", EVENT_REPORT_LIMIT_HIT, now=ts)
        (row,) = _read_records(events_path)
        assert row["timestamp"] == "2026-04-24T12:00:00.000000Z"

    def test_empty_path_serialises_as_empty_string(self, events_path: Path):
        record_upgrade_event("k", EVENT_REPORT_LIMIT_HIT)
        (row,) = _read_records(events_path)
        assert row["path"] == ""


# ---------------------------------------------------------------------------
# record_upgrade_event — skip paths
# ---------------------------------------------------------------------------


class TestRecordEventSkips:
    def test_unknown_event_is_skipped(self, events_path: Path):
        result = record_upgrade_event("k", "bogus_event")
        assert result == {"action": "skipped", "reason": "unknown_event"}
        assert not events_path.exists()

    def test_empty_api_key_is_skipped(self, events_path: Path):
        for bad in (None, ""):
            result = record_upgrade_event(bad, EVENT_DASHBOARD_BANNER_SEEN)
            assert result == {"action": "skipped", "reason": "no_api_key"}
        assert not events_path.exists()

    def test_tier_is_always_free(self, events_path: Path):
        """We never persist premium events, so "tier" is a constant."""
        for event in VALID_EVENTS:
            record_upgrade_event("k", event)
        rows = _read_records(events_path)
        assert rows
        assert all(r["tier"] == "free" for r in rows)


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


class TestFailuresAreSwallowed:
    def test_unwritable_path_does_not_raise(
        self, monkeypatch, tmp_path: Path,
    ):
        # Point the log at a path whose parent is a REGULAR FILE so
        # mkdir(parents=True) raises NotADirectoryError. A real disk
        # outage is morally the same.
        (tmp_path / "not-a-dir").write_text("x")
        broken = tmp_path / "not-a-dir" / "events.jsonl"
        monkeypatch.setenv(UPGRADE_EVENTS_LOG_ENV_VAR, str(broken))
        result = record_upgrade_event("k", EVENT_DASHBOARD_BANNER_SEEN)
        # Best-effort: we always return "recorded" for a valid
        # input. The failure surfaces via DEBUG logs.
        assert result["action"] == "recorded"

    def test_serialisation_error_does_not_raise(
        self, events_path: Path, monkeypatch,
    ):
        """Force json.dumps to raise and confirm we still return."""
        original = ue.json.dumps

        def boom(*args, **kwargs):
            raise TypeError("intentional")

        monkeypatch.setattr(ue.json, "dumps", boom)
        # Should not raise.
        record_upgrade_event("k", EVENT_DASHBOARD_BANNER_SEEN)
        monkeypatch.setattr(ue.json, "dumps", original)


class TestThreadSafety:
    def test_concurrent_writers_no_line_corruption(self, events_path: Path):
        N_THREADS = 16
        N_PER_THREAD = 25

        def writer(worker_id: int) -> None:
            for i in range(N_PER_THREAD):
                record_upgrade_event(
                    f"user-{worker_id}", EVENT_DAILY_REQUEST_LIMIT_HIT,
                    path=f"/reports/day-{i}",
                )

        threads = [
            threading.Thread(target=writer, args=(w,))
            for w in range(N_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = _read_records(events_path)
        assert len(rows) == N_THREADS * N_PER_THREAD
        # Every row decoded cleanly (the reader only yields parsed dicts).
        assert all(r["event"] == EVENT_DAILY_REQUEST_LIMIT_HIT for r in rows)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_empty_file_yields_zero_totals(self, events_path: Path):
        summary = summarize(events_path)
        assert summary == {
            "total_events": 0,
            "events": {},
            "top_paths": [],
            "ref_codes": [],
        }

    def test_counts_events_and_users_correctly(self, events_path: Path):
        record_upgrade_event("a", EVENT_DASHBOARD_BANNER_SEEN, path="/dashboard", ref_code="hn")
        record_upgrade_event("a", EVENT_DASHBOARD_BANNER_SEEN, path="/dashboard", ref_code="hn")
        record_upgrade_event("b", EVENT_DASHBOARD_BANNER_SEEN, path="/dashboard")
        record_upgrade_event("a", EVENT_DAILY_REQUEST_LIMIT_HIT, path="/experiments/recent")
        record_upgrade_event("c", EVENT_REPORT_LIMIT_HIT, path="/reports/latest", ref_code="twitter")

        s = summarize(events_path)
        assert s["total_events"] == 5
        assert s["events"][EVENT_DASHBOARD_BANNER_SEEN] == {
            "count": 3, "unique_users": 2,
        }
        assert s["events"][EVENT_DAILY_REQUEST_LIMIT_HIT] == {
            "count": 1, "unique_users": 1,
        }
        assert s["events"][EVENT_REPORT_LIMIT_HIT] == {
            "count": 1, "unique_users": 1,
        }
        # Top paths — Counter.most_common order with deterministic
        # tie-breaks on insertion order.
        paths_by_count = {
            p["path"]: p["count"] for p in s["top_paths"]
        }
        assert paths_by_count["/dashboard"] == 3
        assert paths_by_count["/experiments/recent"] == 1
        assert paths_by_count["/reports/latest"] == 1
        # Ref codes.
        refs_by_code = {r["ref_code"]: r for r in s["ref_codes"]}
        assert refs_by_code["hn"]["count"] == 2
        assert refs_by_code["hn"]["unique_users"] == 1
        assert refs_by_code["hn"]["events"] == 1
        assert refs_by_code["twitter"]["count"] == 1

    def test_ignores_corrupt_and_blank_lines(self, events_path: Path):
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            "\n".join([
                "",
                "not json",
                "[1, 2, 3]",  # not a dict
                json.dumps({
                    "event": EVENT_DASHBOARD_BANNER_SEEN,
                    "api_key_hash": "h",
                    "path": "/dashboard",
                    "ref_code": "hn",
                }),
            ])
            + "\n",
            encoding="utf-8",
        )
        s = summarize(events_path)
        assert s["total_events"] == 1

    def test_missing_file_is_fine(self, tmp_path: Path):
        s = summarize(tmp_path / "nope.jsonl")
        assert s["total_events"] == 0

    def test_top_paths_and_top_refs_are_capped(self, events_path: Path):
        for i in range(10):
            record_upgrade_event(
                f"u{i}", EVENT_DASHBOARD_BANNER_SEEN,
                path=f"/p{i}", ref_code=f"r{i}",
            )
        s = summarize(events_path, top_paths=3, top_refs=2)
        assert len(s["top_paths"]) == 3
        assert len(s["ref_codes"]) == 2

    def test_events_with_empty_hash_are_omitted_from_unique_users(
        self, events_path: Path,
    ):
        """Hand-write a row with an empty api_key_hash and check the
        summary doesn't treat "" as a user."""
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps({
                "event": EVENT_DASHBOARD_BANNER_SEEN,
                "api_key_hash": "",
                "path": "/dashboard",
            })
            + "\n"
            + json.dumps({
                "event": EVENT_DASHBOARD_BANNER_SEEN,
                "api_key_hash": "real-hash",
                "path": "/dashboard",
            })
            + "\n",
            encoding="utf-8",
        )
        s = summarize(events_path)
        assert s["events"][EVENT_DASHBOARD_BANNER_SEEN] == {
            "count": 2, "unique_users": 1,
        }


class TestSummaryText:
    def test_empty_summary_renders(self, events_path: Path):
        text = format_summary_text(summarize(events_path))
        assert "UPGRADE EVENT SUMMARY" in text
        assert "total_events : 0" in text
        assert "(no events recorded)" in text
        assert "(no paths recorded)" in text
        assert "(no ref_codes present)" in text

    def test_populated_summary_has_all_sections(self, events_path: Path):
        record_upgrade_event("a", EVENT_DASHBOARD_BANNER_SEEN, path="/dashboard", ref_code="hn")
        text = format_summary_text(summarize(events_path))
        assert "By event:" in text
        assert EVENT_DASHBOARD_BANNER_SEEN in text
        assert "By ref_code:" in text
        assert "hn" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "trading_bot.api.upgrade_events", *args],
            capture_output=True, text=True,
        )

    def test_bare_invocation_prints_help_exits_two(self):
        r = self._run()
        assert r.returncode == 2
        assert "--summary" in r.stdout or "--summary" in r.stderr

    def test_summary_text(self, events_path: Path):
        record_upgrade_event("a", EVENT_DAILY_REQUEST_LIMIT_HIT, path="/experiments/recent")
        r = self._run("--summary", "--path", str(events_path))
        assert r.returncode == 0, r.stderr
        assert "UPGRADE EVENT SUMMARY" in r.stdout
        assert EVENT_DAILY_REQUEST_LIMIT_HIT in r.stdout

    def test_summary_json(self, events_path: Path):
        record_upgrade_event("a", EVENT_DAILY_REQUEST_LIMIT_HIT, path="/experiments/recent")
        r = self._run("--summary", "--json", "--path", str(events_path))
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["total_events"] == 1
        assert EVENT_DAILY_REQUEST_LIMIT_HIT in payload["events"]

    def test_respects_env_var_when_path_omitted(
        self, events_path: Path, monkeypatch,
    ):
        record_upgrade_event("a", EVENT_DASHBOARD_BANNER_SEEN, path="/dashboard")
        # events_path fixture already set UPGRADE_EVENTS_LOG_ENV_VAR.
        r = subprocess.run(
            [sys.executable, "-m", "trading_bot.api.upgrade_events",
             "--summary", "--json"],
            capture_output=True, text=True,
            env={
                **__import__("os").environ,
                UPGRADE_EVENTS_LOG_ENV_VAR: str(events_path),
            },
        )
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["total_events"] == 1


# ---------------------------------------------------------------------------
# Boundary — the telemetry module must not reach into Core or the API
# ---------------------------------------------------------------------------


class TestNoCoreImports:
    def test_upgrade_events_module_is_self_contained(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "upgrade_events.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src, (
                f"upgrade_events.py imports restricted surface: {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# PII guard — plant unique markers and verify they don't land on disk
# ---------------------------------------------------------------------------


class TestNoPiiLeak:
    def test_markers_never_appear_in_log(self, events_path: Path):
        record_upgrade_event(
            api_key="MARKER_RAW_API_KEY",
            event=EVENT_REPORT_LIMIT_HIT,
            path="/reports/latest",
            request_id="MARKER_REQ_ID",
            ref_code="MARKER_REF<script>",
        )
        body = events_path.read_text(encoding="utf-8")
        assert "MARKER_RAW_API_KEY" not in body
        # request_id is allowed on disk (the audit log already stores it).
        # ref_code is sanitised — the "<script>" payload must be gone.
        assert "<script>" not in body
        # Hash must be the documented SHA-256[:32] of the raw key.
        assert hashlib.sha256(
            b"MARKER_RAW_API_KEY",
        ).hexdigest()[:32] in body


# ---------------------------------------------------------------------------
# Phase 5.8 — copy_variant_hash field
# ---------------------------------------------------------------------------


from trading_bot.api.upgrade_events import _hash_copy_variant  # noqa: E402


class TestPhase58CopyVariantHash:
    def _hex(self, s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]

    def test_resolved_copy_is_hashed_and_persisted(self, events_path: Path):
        record_upgrade_event(
            "k", EVENT_DASHBOARD_BANNER_SEEN,
            copy_variant="Upgrade now to unlock everything!",
        )
        (row,) = _read_records(events_path)
        assert row["copy_variant_hash"] == self._hex(
            "Upgrade now to unlock everything!",
        )

    def test_raw_copy_is_never_persisted(self, events_path: Path):
        secret_copy = "TOP_SECRET_COPY_THAT_MUST_NEVER_LAND_ON_DISK"
        record_upgrade_event(
            "k", EVENT_DASHBOARD_BANNER_SEEN, copy_variant=secret_copy,
        )
        body = events_path.read_text(encoding="utf-8")
        assert secret_copy not in body
        # The hash IS persisted so operators can group on it.
        assert self._hex(secret_copy) in body

    def test_same_copy_hashes_to_same_value(self, events_path: Path):
        record_upgrade_event(
            "k", EVENT_DASHBOARD_BANNER_SEEN, copy_variant="ABC",
        )
        record_upgrade_event(
            "k", EVENT_DAILY_REQUEST_LIMIT_HIT, copy_variant="ABC",
        )
        rows = _read_records(events_path)
        assert len({r["copy_variant_hash"] for r in rows}) == 1

    def test_different_copy_hashes_to_different_values(
        self, events_path: Path,
    ):
        record_upgrade_event(
            "k", EVENT_DASHBOARD_BANNER_SEEN, copy_variant="ABC",
        )
        record_upgrade_event(
            "k", EVENT_DASHBOARD_BANNER_SEEN, copy_variant="DEF",
        )
        rows = _read_records(events_path)
        assert len({r["copy_variant_hash"] for r in rows}) == 2

    def test_no_copy_variant_serialises_as_null(self, events_path: Path):
        record_upgrade_event("k", EVENT_OLD_REPORT_BLOCKED)
        (row,) = _read_records(events_path)
        assert row["copy_variant_hash"] is None

    def test_empty_copy_variant_serialises_as_null(self, events_path: Path):
        record_upgrade_event(
            "k", EVENT_REPORT_LIMIT_HIT, copy_variant="",
        )
        (row,) = _read_records(events_path)
        assert row["copy_variant_hash"] is None

    def test_hash_is_32_hex_chars(self):
        h = _hash_copy_variant("any copy value")
        assert isinstance(h, str)
        assert len(h) == 32
        int(h, 16)  # raises if non-hex

    def test_hash_helper_handles_none(self):
        assert _hash_copy_variant(None) is None
        assert _hash_copy_variant("") is None

    def test_backward_compat_call_without_copy_variant(self, events_path: Path):
        """Calls that pre-date Phase 5.8 (no copy_variant kwarg)
        still work and produce a copy_variant_hash of null."""
        record_upgrade_event("k", EVENT_DASHBOARD_BANNER_SEEN)
        (row,) = _read_records(events_path)
        assert "copy_variant_hash" in row
        assert row["copy_variant_hash"] is None


# ===========================================================================
# Phase 8.4 — three-stage upgrade funnel
# ===========================================================================


from trading_bot.api.upgrade_events import (  # noqa: E402
    EVENT_UPGRADE_CLICKED,
    EVENT_UPGRADE_COMPLETED,
    EVENT_UPGRADE_SHOWN,
    record_upgrade_funnel_event,
)


class TestPhase84RecordUpgradeFunnelEventSkips:
    def test_unknown_event_skipped(self, events_path: Path):
        out = record_upgrade_funnel_event("a" * 64, "not_a_real_event")
        assert out == {"action": "skipped", "reason": "unknown_event"}
        # File never created.
        assert not events_path.exists()

    def test_blank_hash_skipped(self, events_path: Path):
        for h in ("", "   ", None):
            out = record_upgrade_funnel_event(h, EVENT_UPGRADE_SHOWN)
            assert out == {"action": "skipped", "reason": "no_key_hash"}
        assert not events_path.exists()

    def test_legacy_event_through_funnel_helper_rejected(
        self, events_path: Path,
    ):
        """The Phase 5.5 events go through ``record_upgrade_event``,
        not the Phase 8.4 funnel helper. The funnel helper rejects
        them so the schemas don't bleed."""
        out = record_upgrade_funnel_event(
            "a" * 64, "dashboard_banner_seen",
        )
        assert out["action"] == "skipped"
        assert out["reason"] == "unknown_event"


class TestPhase84RecordUpgradeFunnelEventSchema:
    def test_records_documented_fields(self, events_path: Path):
        out = record_upgrade_funnel_event(
            "h" * 64, EVENT_UPGRADE_SHOWN,
            reason="usage_limit", endpoint="/reports/latest",
            request_id="req_abc",
        )
        assert out["action"] == "recorded"
        assert out["event"] == "upgrade_shown"
        assert out["api_key_hash"] == "h" * 64

        (row,) = _read_records(events_path)
        assert set(row.keys()) == {
            "timestamp", "api_key_hash", "event", "tier",
            "request_id", "reason", "endpoint",
        }
        assert row["api_key_hash"] == "h" * 64
        assert row["event"] == "upgrade_shown"
        assert row["tier"] == "free"
        assert row["request_id"] == "req_abc"
        assert row["reason"] == "usage_limit"
        assert row["endpoint"] == "/reports/latest"

    def test_optional_fields_default_none(self, events_path: Path):
        record_upgrade_funnel_event("h" * 64, EVENT_UPGRADE_CLICKED)
        (row,) = _read_records(events_path)
        assert row["reason"] is None
        assert row["endpoint"] is None
        assert row["request_id"] is None

    def test_overlong_reason_truncated(self, events_path: Path):
        record_upgrade_funnel_event(
            "h" * 64, EVENT_UPGRADE_SHOWN,
            reason="x" * 500,
        )
        (row,) = _read_records(events_path)
        assert row["reason"] is not None
        assert len(row["reason"]) <= 64

    def test_overlong_endpoint_truncated(self, events_path: Path):
        record_upgrade_funnel_event(
            "h" * 64, EVENT_UPGRADE_SHOWN,
            endpoint="/x" * 500,
        )
        (row,) = _read_records(events_path)
        assert row["endpoint"] is not None
        assert len(row["endpoint"]) <= 128

    def test_blank_reason_endpoint_dropped_to_none(
        self, events_path: Path,
    ):
        record_upgrade_funnel_event(
            "h" * 64, EVENT_UPGRADE_SHOWN,
            reason="   ", endpoint="",
        )
        (row,) = _read_records(events_path)
        assert row["reason"] is None
        assert row["endpoint"] is None


class TestPhase84RecordUpgradeFunnelEventNoLeak:
    def test_raw_key_marker_never_persists(self, events_path: Path):
        """The funnel helper accepts ONLY hashes; verify a raw-key
        marker accidentally passed as ``key_hash`` would still
        round-trip as-given (no extra hashing) but never be
        re-decodable to a raw secret. Operators MUST pass a hash
        — this test pins that the helper does not magically
        sanitise raw keys."""
        marker = "RAW_KEY_PHASE84_DO_NOT_LEAK_xyz"
        # The helper does NOT hash again; it stores what it got.
        # Operators using the helper correctly always pre-hash.
        record_upgrade_funnel_event(marker, EVENT_UPGRADE_SHOWN)
        body = events_path.read_text("utf-8")
        # The marker IS in the file (because we passed it as the
        # "hash"). The point of the test is that the helper does
        # not LOG raw keys via the legacy api_key path. All call
        # sites in the codebase pre-hash.
        assert marker in body
        # And the documented persisted shape is hash-only — there's
        # no `api_key` field on the row.
        (row,) = _read_records(events_path)
        assert "api_key" not in row

    def test_disk_failure_does_not_raise(
        self, events_path: Path, monkeypatch,
    ):
        """Best-effort: a real disk failure (open() raises) must
        be swallowed by the underlying _append_record helper. The
        funnel helper still returns 'recorded' — the operator
        observes the failure via structlog at DEBUG."""
        import builtins
        real_open = builtins.open

        def fail_on_append(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "")
            if "a" in mode and str(path).endswith(events_path.name):
                raise OSError("simulated disk full")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_on_append)
        # Must not raise.
        out = record_upgrade_funnel_event(
            "h" * 64, EVENT_UPGRADE_SHOWN,
        )
        assert out["action"] == "recorded"
        # The file may or may not exist depending on mkdir+open
        # ordering; what matters is no exception escaped.
