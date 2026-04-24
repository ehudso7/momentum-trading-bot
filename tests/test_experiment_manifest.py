"""
Phase 3.6 tests — append-only alpha experiment manifest.

Covers:
- build_manifest_record returns the documented shape.
- Webhook URL is NEVER stored — only a presence boolean.
- snapshot_env redacts secrets and echoes tracked public env vars.
- get_git_commit gracefully returns None on failure.
- append_manifest_record writes valid JSONL and never raises.
- append is thread-safe under concurrent writers.
- read_manifest skips malformed lines and honours --tail semantics.
- CLI --tail output is parseable; missing manifest exits cleanly.
- daily_report integration: manifest record is appended alongside
  report generation; failures in the manifest do NOT fail the report.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pytest

from trading_bot.reporting import experiment_manifest as manifest_mod
from trading_bot.reporting.experiment_manifest import (
    DEFAULT_MANIFEST_PATH,
    SECRET_ENV_VARS,
    TRACKED_ENV_VARS,
    _extract_ab_summary,
    append_manifest_record,
    build_manifest_record,
    get_git_commit,
    main,
    read_manifest,
    snapshot_env,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with a known env state for the Core vars."""
    for name in TRACKED_ENV_VARS + SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _sample_record_kwargs(tmp_path: Path) -> dict:
    """Build kwargs that produce a fully-populated manifest record."""
    return dict(
        report_date="2026-04-24",
        scorer_fingerprint="a" * 64,
        scorer_config={
            "scorer": "RuleBasedAlphaScorer",
            "weights": {"gap": 0.2, "rvol": 0.25, "vol": 0.15,
                        "regime": 0.1, "confidence": 0.25, "reason": 0.05},
            "tier_thresholds": {"A": 0.8, "B": 0.65, "C": 0.5, "D": 0.35},
            "regime_scores": {"trending_bullish": 1.0, "range_bound": 0.6,
                              "low_volatility": 0.5, "high_volatility": 0.4,
                              "trending_bearish": 0.2},
        },
        guardrails={
            "status": "ok",
            "reasons": ["allowed side matches or outperforms"],
            "recommended_action": "no action",
        },
        totals={"matched_trades": 42, "alpha_rows": 120},
        promotion_readiness={
            "status": "promising",
            "min_required_outcomes": 100,
            "outcome_count": 42,
            "ab": {"outcome_count": 30, "win_rate": 0.6, "avg_r_multiple": 0.9},
            "cdf": {"outcome_count": 12, "win_rate": 0.3, "avg_r_multiple": 0.0},
        },
        shadow_filter_simulation=[
            {"threshold": "A only", "allowed_buy_count": 10, "blocked_buy_count": 20},
            {"threshold": "A+B",
             "allowed_buy_count": 25, "blocked_buy_count": 5,
             "allowed_outcome_count": 20, "blocked_outcome_count": 2,
             "allowed_win_rate": 0.7, "blocked_win_rate": 0.3,
             "allowed_avg_pnl": 75.0, "blocked_avg_pnl": -25.0,
             "allowed_avg_r_multiple": 0.95, "blocked_avg_r_multiple": -0.2},
            {"threshold": "A+B+C", "allowed_buy_count": 28, "blocked_buy_count": 2},
        ],
        txt_path=tmp_path / "reports" / "alpha_report_2026-04-24.txt",
        json_path=tmp_path / "reports" / "alpha_report_2026-04-24.json",
    )


# ---------------------------------------------------------------------------
# snapshot_env
# ---------------------------------------------------------------------------


class TestSnapshotEnv:
    def test_default_empty_env(self):
        snap = snapshot_env()
        # Every tracked var present, with empty string default.
        for name in TRACKED_ENV_VARS:
            assert snap[name] == ""
        # Presence booleans default False.
        for name in SECRET_ENV_VARS:
            assert snap[f"{name}_present"] is False

    def test_tracked_vars_echoed_verbatim(self, monkeypatch):
        monkeypatch.setenv("TRADING_ALPHA_FILTER_ENABLED", "true")
        monkeypatch.setenv("TRADING_ALPHA_FILTER_MIN_TIER", "A")
        monkeypatch.setenv("TRADING_DATA_ROTATION", "daily")
        snap = snapshot_env()
        assert snap["TRADING_ALPHA_FILTER_ENABLED"] == "true"
        assert snap["TRADING_ALPHA_FILTER_MIN_TIER"] == "A"
        assert snap["TRADING_DATA_ROTATION"] == "daily"

    def test_webhook_url_redacted_to_presence_bool(self, monkeypatch):
        """Raw URL MUST NEVER appear in the snapshot."""
        secret = "https://hooks.example.com/secret/TOKEN123"
        monkeypatch.setenv(
            "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL", secret,
        )
        snap = snapshot_env()
        assert snap["TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present"] is True
        # Exact-match NEVER store the raw URL under any key.
        for key, value in snap.items():
            assert secret not in str(value), f"raw URL leaked in {key}"
        assert "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL" not in snap

    def test_whitespace_only_webhook_is_considered_absent(self, monkeypatch):
        monkeypatch.setenv("TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL", "   ")
        snap = snapshot_env()
        assert snap["TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present"] is False


# ---------------------------------------------------------------------------
# get_git_commit
# ---------------------------------------------------------------------------


class TestGetGitCommit:
    def test_returns_string_in_git_repo(self):
        """This test only runs in the actual repo; otherwise returns None."""
        commit = get_git_commit()
        if commit is None:
            pytest.skip("not a git repo or git unavailable")
        assert isinstance(commit, str)
        assert len(commit) == 40
        int(commit, 16)

    def test_returns_none_when_subprocess_raises(self, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("git not found")
        monkeypatch.setattr(manifest_mod.subprocess, "run", boom)
        assert get_git_commit() is None

    def test_returns_none_on_non_zero_exit(self, monkeypatch):
        class R:
            returncode = 128
            stdout = "fatal: not a git repository"
        monkeypatch.setattr(manifest_mod.subprocess, "run", lambda *a, **kw: R())
        assert get_git_commit() is None

    def test_returns_none_on_timeout(self, monkeypatch):
        def slow(*a, **kw):
            raise subprocess.TimeoutExpired("git", 2)
        monkeypatch.setattr(manifest_mod.subprocess, "run", slow)
        assert get_git_commit() is None

    def test_strips_whitespace(self, monkeypatch):
        class R:
            returncode = 0
            stdout = "  abcdef1234567890abcdef1234567890abcdef12  \n"
        monkeypatch.setattr(manifest_mod.subprocess, "run", lambda *a, **kw: R())
        assert get_git_commit() == "abcdef1234567890abcdef1234567890abcdef12"


# ---------------------------------------------------------------------------
# _extract_ab_summary
# ---------------------------------------------------------------------------


class TestExtractAbSummary:
    def test_empty_input_returns_none(self):
        assert _extract_ab_summary(None) is None
        assert _extract_ab_summary([]) is None

    def test_no_ab_row_returns_none(self):
        rows = [{"threshold": "A only"}, {"threshold": "A+B+C"}]
        assert _extract_ab_summary(rows) is None

    def test_extracts_only_documented_fields(self):
        rows = [{
            "threshold": "A+B",
            "allowed_buy_count": 25,
            "blocked_buy_count": 5,
            "allowed_outcome_count": 20,
            "blocked_outcome_count": 2,
            "allowed_win_rate": 0.7,
            "blocked_win_rate": 0.3,
            "allowed_avg_pnl": 75.0,
            "blocked_avg_pnl": -25.0,
            "allowed_avg_r_multiple": 0.95,
            "blocked_avg_r_multiple": -0.2,
            "secret_extra_field": "should be ignored",
        }]
        got = _extract_ab_summary(rows)
        assert got["threshold"] == "A+B"
        assert got["allowed_buy_count"] == 25
        assert got["blocked_win_rate"] == 0.3
        assert got["allowed_avg_r_multiple"] == 0.95
        assert "secret_extra_field" not in got


# ---------------------------------------------------------------------------
# build_manifest_record
# ---------------------------------------------------------------------------


class TestBuildManifestRecord:
    def test_returns_documented_shape(self, tmp_path: Path):
        kwargs = _sample_record_kwargs(tmp_path)
        record = build_manifest_record(
            git_commit="deadbeef" * 5,
            env={
                "TRADING_ALPHA_FILTER_ENABLED": "true",
                "TRADING_ALPHA_FILTER_MIN_TIER": "B",
                "TRADING_DATA_ROTATION": "daily",
                "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present": False,
            },
            now=datetime(2026, 4, 24, 17, 5, 0),
            **kwargs,
        )
        # Required top-level keys
        required = {
            "timestamp", "report_date", "git_commit",
            "scorer_fingerprint", "scorer_config", "env",
            "report_paths", "totals", "promotion_readiness",
            "guardrails", "shadow_filter_ab_summary",
        }
        assert set(record.keys()) >= required
        assert record["report_date"] == "2026-04-24"
        assert record["git_commit"] == "deadbeef" * 5
        assert record["timestamp"] == "2026-04-24T17:05:00"
        assert record["report_paths"]["text"].endswith(".txt")
        assert record["report_paths"]["json"].endswith(".json")
        assert record["totals"]["matched_trades"] == 42
        assert record["guardrails"]["status"] == "ok"
        assert record["shadow_filter_ab_summary"]["threshold"] == "A+B"
        assert record["env"]["TRADING_ALPHA_FILTER_ENABLED"] == "true"

    def test_record_is_json_serializable(self, tmp_path: Path):
        record = build_manifest_record(
            git_commit=None,
            env={},
            now=datetime(2026, 4, 24),
            **_sample_record_kwargs(tmp_path),
        )
        # Round-trips cleanly — no default= needed thanks to
        # internal json-safe normalization.
        dumped = json.dumps(record)
        parsed = json.loads(dumped)
        assert parsed["report_date"] == "2026-04-24"

    def test_git_commit_defaults_to_lookup_when_none(self, tmp_path: Path, monkeypatch):
        """Explicit None triggers a git lookup — override it for determinism."""
        monkeypatch.setattr(
            manifest_mod, "get_git_commit", lambda: "patched_commit"
        )
        record = build_manifest_record(
            env={},
            now=datetime(2026, 4, 24),
            **_sample_record_kwargs(tmp_path),
        )
        assert record["git_commit"] == "patched_commit"

    def test_missing_git_commit_handled(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(manifest_mod, "get_git_commit", lambda: None)
        record = build_manifest_record(
            env={},
            now=datetime(2026, 4, 24),
            **_sample_record_kwargs(tmp_path),
        )
        assert record["git_commit"] is None

    def test_ab_summary_included(self, tmp_path: Path):
        record = build_manifest_record(
            git_commit=None, env={}, now=datetime(2026, 4, 24),
            **_sample_record_kwargs(tmp_path),
        )
        ab = record["shadow_filter_ab_summary"]
        assert ab is not None
        assert ab["allowed_buy_count"] == 25
        assert ab["blocked_avg_r_multiple"] == -0.2

    def test_env_fallback_to_snapshot_when_omitted(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("TRADING_ALPHA_FILTER_ENABLED", "true")
        monkeypatch.setenv("TRADING_ALPHA_FILTER_MIN_TIER", "C")
        record = build_manifest_record(
            git_commit=None, now=datetime(2026, 4, 24),
            **_sample_record_kwargs(tmp_path),
        )
        assert record["env"]["TRADING_ALPHA_FILTER_ENABLED"] == "true"
        assert record["env"]["TRADING_ALPHA_FILTER_MIN_TIER"] == "C"

    def test_never_contains_raw_webhook_url(
        self, tmp_path: Path, monkeypatch
    ):
        """The sensitive URL must never end up in the record — not in
        env, not anywhere else. This is a strict substring check."""
        secret = "https://hooks.slack.com/services/T000/B000/XYZ_SECRET_ABC"
        monkeypatch.setenv("TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL", secret)
        record = build_manifest_record(
            git_commit=None, now=datetime(2026, 4, 24),
            **_sample_record_kwargs(tmp_path),
        )
        serialized = json.dumps(record, default=str)
        assert secret not in serialized
        assert "XYZ_SECRET_ABC" not in serialized
        assert record["env"]["TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present"] is True


# ---------------------------------------------------------------------------
# append_manifest_record
# ---------------------------------------------------------------------------


class TestAppendManifestRecord:
    def test_appends_valid_jsonl(self, tmp_path: Path):
        path = tmp_path / "alpha_experiments.jsonl"
        ok, err = append_manifest_record(
            {"timestamp": "2026-04-24T00:00:00", "report_date": "2026-04-24"},
            manifest_path=path,
        )
        assert ok is True and err is None
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["report_date"] == "2026-04-24"

    def test_two_appends_produce_two_jsonl_lines(self, tmp_path: Path):
        path = tmp_path / "alpha_experiments.jsonl"
        append_manifest_record({"report_date": "2026-04-24"}, manifest_path=path)
        append_manifest_record({"report_date": "2026-04-25"}, manifest_path=path)
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert [json.loads(l)["report_date"] for l in lines] == [
            "2026-04-24", "2026-04-25",
        ]

    def test_creates_parent_dir(self, tmp_path: Path):
        path = tmp_path / "nested" / "deeper" / "alpha_experiments.jsonl"
        ok, err = append_manifest_record({"report_date": "x"}, manifest_path=path)
        assert ok is True and err is None
        assert path.exists()

    def test_serialize_error_returned_not_raised(self, tmp_path: Path):
        class Unserializable:
            pass
        ok, err = append_manifest_record(
            {"bad": Unserializable()},
            manifest_path=tmp_path / "m.jsonl",
        )
        # default=str in json.dumps makes this succeed; ensure coverage
        # of the serialize branch by monkey-patching json.dumps instead.
        assert ok is True  # default=str swallows

    def test_serialize_error_path_monkeypatched(
        self, tmp_path: Path, monkeypatch
    ):
        def bad_dumps(*a, **kw):
            raise TypeError("cannot serialize fakely")
        monkeypatch.setattr(manifest_mod.json, "dumps", bad_dumps)
        ok, err = append_manifest_record(
            {"any": "thing"},
            manifest_path=tmp_path / "m.jsonl",
        )
        assert ok is False
        assert err and "serialize_error" in err

    def test_write_error_returned_not_raised(
        self, tmp_path: Path, monkeypatch
    ):
        """Disk failure must not raise; it must be reported as error."""
        import builtins
        real_open = builtins.open

        def bad_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "")
            if "a" in mode:
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", bad_open)
        ok, err = append_manifest_record(
            {"report_date": "x"},
            manifest_path=tmp_path / "m.jsonl",
        )
        assert ok is False
        assert err and "write_error" in err

    def test_thread_safe_concurrent_writes(self, tmp_path: Path):
        path = tmp_path / "alpha_experiments.jsonl"
        N = 50

        def writer(i: int):
            append_manifest_record(
                {"report_date": f"2026-04-{i:02d}", "i": i},
                manifest_path=path,
            )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, N + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = path.read_text().splitlines()
        assert len(lines) == N, f"expected {N} records, got {len(lines)}"
        # Every line must be valid JSON — no interleaved writes.
        indices = set()
        for line in lines:
            rec = json.loads(line)
            indices.add(rec["i"])
        assert indices == set(range(1, N + 1))


# ---------------------------------------------------------------------------
# read_manifest + tail
# ---------------------------------------------------------------------------


class TestReadManifest:
    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        assert read_manifest(tmp_path / "absent.jsonl") == []

    def test_reads_all_when_no_limit(self, tmp_path: Path):
        path = tmp_path / "m.jsonl"
        for i in range(3):
            append_manifest_record({"i": i}, manifest_path=path)
        assert [r["i"] for r in read_manifest(path)] == [0, 1, 2]

    def test_tail_limit_returns_last_n(self, tmp_path: Path):
        path = tmp_path / "m.jsonl"
        for i in range(10):
            append_manifest_record({"i": i}, manifest_path=path)
        assert [r["i"] for r in read_manifest(path, limit=3)] == [7, 8, 9]

    def test_malformed_lines_skipped(self, tmp_path: Path):
        path = tmp_path / "m.jsonl"
        path.write_text(
            '{"report_date": "2026-04-24"}\n'
            'not valid json at all\n'
            '{"report_date": "2026-04-25"}\n'
            '{partial: json,\n'
            '{"report_date": "2026-04-26"}\n'
        )
        records = read_manifest(path)
        dates = [r["report_date"] for r in records]
        assert dates == ["2026-04-24", "2026-04-25", "2026-04-26"]

    def test_blank_lines_skipped(self, tmp_path: Path):
        path = tmp_path / "m.jsonl"
        path.write_text(
            '{"a": 1}\n\n\n   \n{"a": 2}\n'
        )
        records = read_manifest(path)
        assert [r["a"] for r in records] == [1, 2]

    def test_non_object_json_skipped(self, tmp_path: Path):
        path = tmp_path / "m.jsonl"
        path.write_text('[1, 2, 3]\n"a string"\n{"ok": true}\n')
        records = read_manifest(path)
        assert records == [{"ok": True}]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_missing_manifest_exits_zero(self, tmp_path: Path, capsys):
        rc = main(["--manifest", str(tmp_path / "absent.jsonl")])
        assert rc == 0
        err = capsys.readouterr().err
        assert "not found" in err

    def test_tail_prints_last_n(self, tmp_path: Path, capsys):
        path = tmp_path / "m.jsonl"
        for i in range(5):
            append_manifest_record({"i": i}, manifest_path=path)
        rc = main(["--manifest", str(path), "--tail", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        assert '"i": 3' in out
        assert '"i": 4' in out
        assert '"i": 0' not in out

    def test_json_lines_flag_emits_compact_records(self, tmp_path: Path, capsys):
        path = tmp_path / "m.jsonl"
        for i in range(3):
            append_manifest_record({"i": i}, manifest_path=path)
        rc = main(["--manifest", str(path), "--json-lines"])
        assert rc == 0
        out_lines = [
            l for l in capsys.readouterr().out.splitlines() if l.strip()
        ]
        assert len(out_lines) == 3
        parsed = [json.loads(l) for l in out_lines]
        assert [p["i"] for p in parsed] == [0, 1, 2]

    def test_tail_zero_means_all(self, tmp_path: Path, capsys):
        path = tmp_path / "m.jsonl"
        for i in range(5):
            append_manifest_record({"i": i}, manifest_path=path)
        rc = main(["--manifest", str(path), "--tail", "0", "--json-lines"])
        assert rc == 0
        out_lines = [
            l for l in capsys.readouterr().out.splitlines() if l.strip()
        ]
        assert len(out_lines) == 5

    def test_subprocess_smoke(self, tmp_path: Path):
        """`python -m trading_bot.reporting.experiment_manifest` must exit 0."""
        path = tmp_path / "m.jsonl"
        append_manifest_record({"report_date": "2026-04-24"}, manifest_path=path)
        result = subprocess.run(
            [
                sys.executable, "-m",
                "trading_bot.reporting.experiment_manifest",
                "--manifest", str(path),
                "--tail", "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "2026-04-24" in result.stdout


# ---------------------------------------------------------------------------
# daily_report integration
# ---------------------------------------------------------------------------


class TestDailyReportIntegration:
    """The Phase 3.2 daily report writer appends a manifest record."""

    def _populated_fixture(self, tmp_path: Path) -> tuple[Path, str]:
        """Inlined mini-fixture — avoids cross-file fixture import."""
        import csv as _csv
        data = tmp_path / "data"
        report_date = "2026-04-24"
        alpha_headers = [
            "timestamp", "symbol", "score", "tier", "action",
            "confidence", "regime", "gap_pct", "relative_volume",
            "volatility", "reasons",
        ]
        dec_headers = [
            "timestamp", "symbol", "price", "gap_pct", "relative_volume",
            "volatility", "regime", "action", "confidence", "reason",
        ]
        journal_headers = [
            "date", "symbol", "side", "signal_type", "entry_price",
            "exit_price", "shares", "pnl", "rr_ratio", "hold_time_minutes",
            "entry_time", "exit_time", "exit_reason", "notes",
        ]
        data.mkdir(parents=True, exist_ok=True)
        alpha = data / f"alpha_scores_{report_date}.csv"
        dec = data / f"decision_log_{report_date}.csv"
        j = data / "journal.csv"
        with open(alpha, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=alpha_headers)
            w.writeheader()
            w.writerow(dict(timestamp="2026-04-24 09:45:00", symbol="AAA",
                            score=0.92, tier="A", action="buy", confidence=0.85,
                            regime="trending_bullish", gap_pct=12.0,
                            relative_volume=15.0, volatility=3.5, reasons="r"))
        with open(dec, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=dec_headers)
            w.writeheader()
            w.writerow(dict(timestamp="2026-04-24 09:45:00", symbol="AAA",
                            price=10.0, gap_pct=12.0, relative_volume=15.0,
                            volatility=3.5, regime="trending_bullish",
                            action="buy", confidence=0.85, reason="executed"))
        with open(j, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=journal_headers)
            w.writeheader()
            w.writerow(dict(date="2026-04-24", symbol="AAA", side="buy",
                            signal_type="vwap_pullback", entry_price=10.0,
                            exit_price=12.0, shares=100, pnl=200.0, rr_ratio=2.0,
                            hold_time_minutes=30, entry_time="09:45:00",
                            exit_time="10:15:00", exit_reason="target_2r",
                            notes=""))
        return data, report_date

    def test_daily_report_appends_manifest_record(self, tmp_path: Path):
        from trading_bot.reporting.daily_report import generate_daily_report

        data_dir, report_date = self._populated_fixture(tmp_path)
        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.success is True
        assert result.manifest_appended is True
        assert result.manifest_error is None
        assert result.manifest_path is not None
        assert result.manifest_path.exists()

        records = read_manifest(result.manifest_path)
        assert len(records) == 1
        rec = records[0]
        assert rec["report_date"] == report_date
        # Required fields
        for k in (
            "timestamp", "report_date", "git_commit",
            "scorer_fingerprint", "scorer_config", "env",
            "report_paths", "totals", "promotion_readiness",
            "guardrails", "shadow_filter_ab_summary",
        ):
            assert k in rec, f"missing {k}"
        # Env snapshot includes the presence boolean, never the URL
        assert rec["env"]["TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present"] is False

    def test_manifest_write_failure_does_not_fail_report(
        self, tmp_path: Path, monkeypatch
    ):
        from trading_bot.reporting import daily_report as daily_report_mod
        from trading_bot.reporting.daily_report import generate_daily_report

        data_dir, report_date = self._populated_fixture(tmp_path)

        # Make manifest append fail.
        def fail_append(rec, manifest_path=None):
            return False, "simulated_append_failure"

        monkeypatch.setattr(
            daily_report_mod, "append_manifest_record", fail_append,
        )

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        # Report itself still succeeds — file writes unaffected.
        assert result.success is True
        assert result.txt_path.exists()
        assert result.json_path.exists()
        # Manifest marked as failed, error recorded.
        assert result.manifest_appended is False
        assert result.manifest_error == "simulated_append_failure"

    def test_manifest_build_exception_does_not_fail_report(
        self, tmp_path: Path, monkeypatch
    ):
        from trading_bot.reporting import daily_report as daily_report_mod
        from trading_bot.reporting.daily_report import generate_daily_report

        data_dir, report_date = self._populated_fixture(tmp_path)

        def boom(**kwargs):
            raise RuntimeError("cannot build")

        monkeypatch.setattr(
            daily_report_mod, "build_manifest_record", boom,
        )

        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        assert result.success is True
        assert result.manifest_appended is False
        assert result.manifest_error is not None
        assert "cannot build" in result.manifest_error

    def test_ab_summary_stored_in_manifest(self, tmp_path: Path):
        from trading_bot.reporting.daily_report import generate_daily_report

        data_dir, report_date = self._populated_fixture(tmp_path)
        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        records = read_manifest(result.manifest_path)
        ab = records[-1]["shadow_filter_ab_summary"]
        assert ab is not None
        assert ab["threshold"] == "A+B"

    def test_webhook_url_never_persisted(self, tmp_path: Path, monkeypatch):
        """End-to-end: an operator with the webhook env set sees no
        trace of the URL in the manifest."""
        from trading_bot.reporting.daily_report import generate_daily_report

        secret = "https://hooks.example.com/ABC/XYZ_SENSITIVE_TOKEN"
        monkeypatch.setenv(
            "TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL", secret,
        )
        data_dir, report_date = self._populated_fixture(tmp_path)
        result = generate_daily_report(
            date=report_date,
            data_dir=data_dir,
            reports_dir=tmp_path / "reports",
            min_required_outcomes=1,
        )
        manifest_text = result.manifest_path.read_text()
        assert secret not in manifest_text
        assert "XYZ_SENSITIVE_TOKEN" not in manifest_text
        # But the presence flag IS recorded.
        records = read_manifest(result.manifest_path)
        assert records[-1]["env"]["TRADING_ALPHA_GUARDRAIL_WEBHOOK_URL_present"] is True
