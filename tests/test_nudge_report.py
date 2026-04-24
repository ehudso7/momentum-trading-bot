"""
Phase 5.8 tests — nudge copy performance report.

Exercises ``trading_bot.analysis.nudge_report`` against hand-written
JSONL fixtures and verifies:

  * the per-variant join groups events by ``copy_variant_hash``;
  * conversion-rate math is correct per variant;
  * the time-delta uses the user's earliest event under that
    variant and the user's earliest conversion;
  * old/legacy rows that lack ``copy_variant_hash`` (or have it
    set to null) are counted in the totals but excluded from the
    per-variant rows;
  * no raw API key / no copy text leaks into either output;
  * deterministic byte-identical output for the same inputs;
  * JSON shape is the documented one;
  * CLI happy paths (write files, --json-only, --text-only) and
    mutual-exclusive flag rejection;
  * the analysis module imports nothing from Core or the API.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from trading_bot.analysis.nudge_report import (
    REPORT_TYPE,
    build_nudge_report,
    compute_variant_metrics,
    format_json,
    format_text,
    load_conversions,
    load_upgrade_events,
    pick_strongest_variant,
    write_reports,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hk(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _hc(copy: str) -> str:
    return hashlib.sha256(copy.encode("utf-8")).hexdigest()[:32]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "events": tmp_path / "events.jsonl",
        "conv": tmp_path / "conversions.jsonl",
        "reports": tmp_path / "reports",
    }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_missing_files_yield_empty_lists(self, tmp_path: Path):
        miss = tmp_path / "nope.jsonl"
        assert load_upgrade_events(miss) == []
        assert load_conversions(miss) == []

    def test_corrupt_lines_are_skipped(self, paths):
        paths["events"].write_text(
            "\n".join([
                json.dumps({
                    "api_key_hash": _hk("a"),
                    "event": "dashboard_banner_seen",
                    "copy_variant_hash": _hc("v1"),
                }),
                "garbage",
                "[\"not a dict\"]",
                json.dumps({
                    "api_key_hash": _hk("b"),
                    "event": "dashboard_banner_seen",
                    "copy_variant_hash": _hc("v1"),
                }),
            ])
            + "\n",
            encoding="utf-8",
        )
        rows = load_upgrade_events(paths["events"])
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Per-variant metrics
# ---------------------------------------------------------------------------


class TestVariantMetrics:
    def test_groups_events_by_variant_hash(self):
        ha, hb, hc = _hk("a"), _hk("b"), _hk("c")
        v1, v2 = _hc("BANNER_A"), _hc("BANNER_B")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
            {"api_key_hash": hb, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-11T10:00:00.000000Z",
             "copy_variant_hash": v1},
            {"api_key_hash": hc, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-12T10:00:00.000000Z",
             "copy_variant_hash": v2},
        ]
        rows = compute_variant_metrics(events, [])
        by_hash = {r["copy_variant_hash"]: r for r in rows}
        assert by_hash[v1]["unique_users"] == 2
        assert by_hash[v1]["event_count"] == 2
        assert by_hash[v2]["unique_users"] == 1
        assert by_hash[v2]["event_count"] == 1

    def test_conversion_attribution_works(self):
        ha, hb = _hk("a"), _hk("b")
        v1 = _hc("X")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
            {"api_key_hash": hb, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-15T10:00:00.000000Z"},
        ]
        (row,) = compute_variant_metrics(events, conversions)
        assert row["unique_users"] == 2
        assert row["converted_users"] == 1
        assert row["conversion_rate"] == 0.5
        # 5 days from earliest event for user a.
        assert row["median_time_to_convert_days"] == 5.0

    def test_time_delta_uses_earliest_event_per_variant(self):
        ha = _hk("a")
        v1 = _hc("X")
        events = [
            # User a saw v1 on 04-10 (earliest) and again on 04-12.
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-12T10:00:00.000000Z",
             "copy_variant_hash": v1},
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-13T10:00:00.000000Z"},
        ]
        (row,) = compute_variant_metrics(events, conversions)
        # Earliest event was 04-10 → 3 days, NOT 1.
        assert row["median_time_to_convert_days"] == 3.0

    def test_negative_deltas_dropped(self):
        ha = _hk("a")
        v1 = _hc("X")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ]
        conversions = [
            {"api_key_hash": ha,
             "timestamp": "2026-04-09T10:00:00.000000Z"},  # before event
        ]
        (row,) = compute_variant_metrics(events, conversions)
        assert row["converted_users"] == 1
        assert row["delta_samples"] == 0
        assert row["median_time_to_convert_days"] is None

    def test_rows_without_variant_hash_are_excluded(self):
        ha, hb, hc = _hk("a"), _hk("b"), _hk("c")
        v1 = _hc("X")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
            # Missing field — represents an old row.
            {"api_key_hash": hb, "event": "old_report_blocked",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            # Explicit null — same as missing per the schema.
            {"api_key_hash": hc, "event": "experiment_limit_blocked",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": None},
        ]
        rows = compute_variant_metrics(events, [])
        assert len(rows) == 1
        assert rows[0]["copy_variant_hash"] == v1
        assert rows[0]["unique_users"] == 1

    def test_records_per_variant_event_set(self):
        """``events`` field on each variant row should list the
        distinct event names it appeared on."""
        ha = _hk("a")
        v1 = _hc("SAME_COPY_FOR_BOTH")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
            {"api_key_hash": ha, "event": "daily_request_limit_hit",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ]
        (row,) = compute_variant_metrics(events, [])
        assert row["events"] == [
            "daily_request_limit_hit", "dashboard_banner_seen",
        ]

    def test_deterministic_sort(self):
        """Sorted by conversion_rate desc, tie-break by hash asc."""
        # Construct three variants with identical 50% conversion
        # so the tiebreak (ascending hash string) controls order.
        hashes = sorted([_hc("A"), _hc("B"), _hc("C")])
        rows_in = []
        for i, vh in enumerate(hashes):
            ha = _hk(f"a{i}")
            hb = _hk(f"b{i}")
            rows_in.append({
                "api_key_hash": ha, "event": "dashboard_banner_seen",
                "timestamp": "2026-04-10T10:00:00.000000Z",
                "copy_variant_hash": vh,
            })
            rows_in.append({
                "api_key_hash": hb, "event": "dashboard_banner_seen",
                "timestamp": "2026-04-10T10:00:00.000000Z",
                "copy_variant_hash": vh,
            })
        # Convert exactly one user per variant → 50% rate everywhere.
        conversions = [
            {"api_key_hash": _hk(f"a{i}"),
             "timestamp": "2026-04-12T10:00:00.000000Z"}
            for i in range(3)
        ]
        rows = compute_variant_metrics(rows_in, conversions)
        assert [r["copy_variant_hash"] for r in rows] == hashes


# ---------------------------------------------------------------------------
# Strongest pick
# ---------------------------------------------------------------------------


class TestStrongestVariant:
    def test_empty_returns_none(self):
        assert pick_strongest_variant([]) is None

    def test_min_support_filters(self):
        rows = [
            {"copy_variant_hash": "tinyhash", "unique_users": 1,
             "conversion_rate": 1.0},
            {"copy_variant_hash": "realhash", "unique_users": 10,
             "conversion_rate": 0.30},
        ]
        out = pick_strongest_variant(rows)
        assert out == {"copy_variant_hash": "realhash", "value": 0.30}

    def test_min_support_override(self):
        rows = [
            {"copy_variant_hash": "tinyhash", "unique_users": 1,
             "conversion_rate": 1.0},
            {"copy_variant_hash": "realhash", "unique_users": 10,
             "conversion_rate": 0.30},
        ]
        out = pick_strongest_variant(rows, min_users=1)
        assert out == {"copy_variant_hash": "tinyhash", "value": 1.0}

    def test_ties_break_alphabetically_on_hash(self):
        rows = [
            {"copy_variant_hash": "zzz", "unique_users": 5,
             "conversion_rate": 0.5},
            {"copy_variant_hash": "aaa", "unique_users": 5,
             "conversion_rate": 0.5},
        ]
        out = pick_strongest_variant(rows)
        assert out["copy_variant_hash"] == "aaa"


# ---------------------------------------------------------------------------
# Build report + totals
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_empty_inputs_yield_valid_report(self, paths):
        r = build_nudge_report(
            paths["events"], paths["conv"],
            report_date="2026-04-24",
        )
        assert r["report_type"] == REPORT_TYPE
        assert r["report_date"] == "2026-04-24"
        assert r["variants"] == []
        assert r["totals"]["events_with_variant"] == 0
        assert r["totals"]["events_without_variant"] == 0
        assert r["strongest_variant"] is None

    def test_totals_split_by_hash_presence(self, paths):
        ha, hb, hc = _hk("a"), _hk("b"), _hk("c")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
            {"api_key_hash": hb, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
            # No hash at all (old row).
            {"api_key_hash": hc, "event": "old_report_blocked",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        r = build_nudge_report(paths["events"], paths["conv"])
        assert r["totals"]["events_with_variant"] == 2
        assert r["totals"]["events_without_variant"] == 1
        assert r["totals"]["distinct_variants"] == 1
        # ``unique_users`` only counts users seen on hash-carrying events.
        assert r["totals"]["unique_users"] == 2

    def test_deterministic_for_same_inputs(self, paths):
        ha = _hk("a")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ])
        _write_jsonl(paths["conv"], [
            {"api_key_hash": ha, "timestamp": "2026-04-15T10:00:00.000000Z"},
        ])
        a = build_nudge_report(
            paths["events"], paths["conv"], report_date="2026-04-24",
        )
        b = build_nudge_report(
            paths["events"], paths["conv"], report_date="2026-04-24",
        )
        assert a == b
        assert format_json(a) == format_json(b)
        assert format_text(a) == format_text(b)


# ---------------------------------------------------------------------------
# JSON structure
# ---------------------------------------------------------------------------


class TestJsonStructure:
    def test_top_level_shape(self, paths):
        ha = _hk("a")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ])
        _write_jsonl(paths["conv"], [])
        r = build_nudge_report(paths["events"], paths["conv"])
        parsed = json.loads(format_json(r))
        assert set(parsed.keys()) == {
            "report_type", "report_date", "sources",
            "min_users_for_ranking", "totals", "variants",
            "strongest_variant",
        }
        assert parsed["report_type"] == "nudge_copy_performance"
        assert set(parsed["sources"].keys()) == {
            "upgrade_events_log", "conversion_log",
        }

    def test_variant_row_documented_fields(self, paths):
        ha = _hk("a")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ])
        _write_jsonl(paths["conv"], [])
        r = build_nudge_report(paths["events"], paths["conv"])
        (row,) = r["variants"]
        assert set(row.keys()) == {
            "copy_variant_hash", "event_count", "unique_users",
            "converted_users", "conversion_rate", "delta_samples",
            "median_time_to_convert_seconds",
            "p90_time_to_convert_seconds",
            "median_time_to_convert_days",
            "p90_time_to_convert_days",
            "events",
        }


# ---------------------------------------------------------------------------
# PII / leak guard
# ---------------------------------------------------------------------------


class TestPiiAndLeakGuard:
    def test_unique_markers_never_appear_in_output(self, paths):
        """Plant unique markers across every optional field on
        every row and assert they never end up in the report."""
        ha = _hk("a")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {
                "api_key_hash": ha,
                "event": "dashboard_banner_seen",
                "timestamp": "2026-04-10T10:00:00.000000Z",
                "copy_variant_hash": v1,
                # Planted markers — must NEVER reach the report.
                "path": "/UNIQUE_PATH_LEAK",
                "request_id": "UNIQUE_REQID_LEAK",
                "ref_code": "UNIQUE_REF_LEAK",
                "tier": "free",
                "secret_marker": "DO_NOT_LEAK_ME",
            },
        ])
        _write_jsonl(paths["conv"], [
            {
                "api_key_hash": ha,
                "timestamp": "2026-04-12T10:00:00.000000Z",
                "price_id": "UNIQUE_PRICE_ID_LEAK",
                "source": "UNIQUE_SOURCE_LEAK",
                "card_last4": "4242",
            },
        ])
        r = build_nudge_report(paths["events"], paths["conv"])
        text = format_text(r)
        js = format_json(r)
        for marker in (
            "UNIQUE_PATH_LEAK",
            "UNIQUE_REQID_LEAK",
            "UNIQUE_REF_LEAK",
            "DO_NOT_LEAK_ME",
            "UNIQUE_PRICE_ID_LEAK",
            "UNIQUE_SOURCE_LEAK",
            "4242",
        ):
            assert marker not in text, f"text leaked: {marker}"
            assert marker not in js, f"json leaked: {marker}"

    def test_per_variant_row_does_not_include_api_key_hash(self, paths):
        ha = _hk("super-secret-key")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ])
        _write_jsonl(paths["conv"], [])
        r = build_nudge_report(paths["events"], paths["conv"])
        (row,) = r["variants"]
        assert "api_key_hash" not in row
        # The api_key_hash IS NOT in any output payload.
        assert ha not in format_text(r)
        assert ha not in format_json(r)

    def test_no_raw_copy_text_can_leak(self, paths):
        """The events log already stores only the hash, so this is
        guaranteed by construction. We re-assert it here to lock
        the contract for downstream changes."""
        ha = _hk("a")
        # Note: the JSONL row carries ONLY the hash — never the raw
        # copy. The report MUST therefore not contain the raw copy.
        raw = "RAW_COPY_TEXT_THAT_MUST_NOT_LEAK"
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": _hc(raw)},
        ])
        _write_jsonl(paths["conv"], [])
        r = build_nudge_report(paths["events"], paths["conv"])
        assert raw not in format_text(r)
        assert raw not in format_json(r)


# ---------------------------------------------------------------------------
# write_reports + CLI
# ---------------------------------------------------------------------------


class TestWriteReports:
    def test_writes_txt_and_json(self, paths):
        ha = _hk("a")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ])
        _write_jsonl(paths["conv"], [])
        r = build_nudge_report(
            paths["events"], paths["conv"], report_date="2026-04-24",
        )
        txt_path, json_path = write_reports(
            r, reports_dir=paths["reports"],
        )
        assert txt_path == paths["reports"] / "nudge_report_2026-04-24.txt"
        assert json_path == paths["reports"] / "nudge_report_2026-04-24.json"
        assert "NUDGE COPY PERFORMANCE REPORT" in txt_path.read_text()
        parsed = json.loads(json_path.read_text())
        assert parsed["report_type"] == "nudge_copy_performance"


class TestCli:
    def test_cli_writes_both_files(self, paths):
        ha = _hk("a")
        v1 = _hc("X")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "copy_variant_hash": v1},
        ])
        _write_jsonl(paths["conv"], [])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.nudge_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (
            paths["reports"] / "nudge_report_2026-04-24.txt"
        ).exists()
        assert (
            paths["reports"] / "nudge_report_2026-04-24.json"
        ).exists()
        assert "nudge copy performance report written" in result.stdout.lower()

    def test_cli_json_only(self, paths):
        _write_jsonl(paths["events"], [])
        _write_jsonl(paths["conv"], [])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.nudge_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
                "--json-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["report_type"] == "nudge_copy_performance"

    def test_cli_text_only(self, paths):
        _write_jsonl(paths["events"], [])
        _write_jsonl(paths["conv"], [])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.nudge_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--text-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "NUDGE COPY PERFORMANCE REPORT" in result.stdout

    def test_cli_mutually_exclusive_flags(self, paths):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.nudge_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--json-only", "--text-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestNoCoreOrApiImports:
    def test_module_is_self_contained(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "analysis" / "nudge_report.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.api.",
            "import trading_bot.api",
        ):
            assert forbidden not in src, (
                f"nudge_report.py imports restricted surface: {forbidden!r}"
            )
