"""
Phase 5.0 tests — pricing intelligence report.

Exercises the pure helpers in ``trading_bot.analysis.pricing_report``
against hand-written JSONL fixtures and verifies:
  * user cohort counts are correct,
  * conversion rate math is correct,
  * time-to-convert statistics (mean / median / p90) are correct,
  * usage-intensity + top-percentile power-user extraction is correct,
  * empty / missing input files produce a valid but empty-ish report,
  * JSON / text report shapes are stable,
  * CLI writes both files and exits 0,
  * no raw API key / PII / card data leaks into the report.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pytest

from trading_bot.analysis.pricing_report import (
    REPORT_TYPE,
    TOP_PERCENTILES,
    _date_bucket,
    _mean,
    _median,
    _parse_iso_utc,
    _percentile,
    build_pricing_report,
    compute_conversion_metrics,
    compute_time_to_convert,
    compute_usage_intensity,
    compute_user_counts,
    format_json,
    format_text,
    load_conversions,
    load_usage,
    main as pricing_main,
    write_reports,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: Iterable[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def _usage(
    *,
    key_hash: str,
    tier: str,
    timestamp: str,
    path: str = "/reports/latest",
    status_code: int = 200,
) -> dict:
    return {
        "timestamp": timestamp,
        "key_hash": key_hash,
        "tier": tier,
        "method": "GET",
        "path": path,
        "status_code": status_code,
        "duration_ms": 1.5,
        "request_id": f"rid-{key_hash[:6]}-{timestamp[-6:]}",
    }


def _conversion(
    *,
    api_key_hash: str,
    timestamp: str,
    first_seen_timestamp: str = None,
    price_id: str = "price_monthly",
    source: str = "stripe",
) -> dict:
    return {
        "timestamp": timestamp,
        "api_key_hash": api_key_hash,
        "event": "converted",
        "source": source,
        "price_id": price_id,
        "first_seen_timestamp": first_seen_timestamp,
    }


# ---------------------------------------------------------------------------
# Pure stat helpers
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_is_none(self):
        assert _percentile([], 0.9) is None

    def test_single_value(self):
        assert _percentile([42], 0.9) == 42.0
        assert _percentile([42], 0.5) == 42.0

    def test_ten_values_p90(self):
        # nearest-rank: ceil(0.9 * 10) = 9 → values[8]
        assert _percentile(list(range(10)), 0.9) == 8.0

    def test_median_formula(self):
        # p50 of 0..10 via percentile vs. median — both should agree
        # within nearest-rank semantics.
        values = list(range(1, 11))  # 1..10
        # percentile(0.5, n=10) = ceil(5) = 5 → values[4] = 5
        assert _percentile(values, 0.5) == 5.0

    def test_unsorted_input_works(self):
        assert _percentile([5, 1, 3, 2, 4], 0.8) == 4.0


class TestMeanMedian:
    def test_mean_empty(self):
        assert _mean([]) is None

    def test_mean_basic(self):
        assert _mean([1.0, 2.0, 3.0]) == 2.0

    def test_median_empty(self):
        assert _median([]) is None

    def test_median_odd(self):
        assert _median([1.0, 5.0, 3.0]) == 3.0

    def test_median_even(self):
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


class TestParseIsoUtc:
    def test_accepts_trailing_z(self):
        dt = _parse_iso_utc("2026-04-24T10:00:00.000000Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 4 and dt.day == 24

    def test_accepts_offset(self):
        dt = _parse_iso_utc("2026-04-24T10:00:00+00:00")
        assert dt is not None

    def test_none_input(self):
        assert _parse_iso_utc(None) is None

    def test_blank_input(self):
        assert _parse_iso_utc("") is None
        assert _parse_iso_utc("   ") is None

    def test_malformed_returns_none(self):
        assert _parse_iso_utc("not-a-timestamp") is None


class TestDateBucket:
    def test_bucket_day(self):
        assert _date_bucket("2026-04-24T10:00:00.000000Z") == "2026-04-24"

    def test_bucket_none(self):
        assert _date_bucket(None) is None

    def test_bucket_malformed(self):
        assert _date_bucket("garbage") is None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_load_usage_missing(self, tmp_path: Path):
        assert load_usage(tmp_path / "missing.jsonl") == []

    def test_load_conversions_missing(self, tmp_path: Path):
        assert load_conversions(tmp_path / "missing.jsonl") == []

    def test_load_usage_skips_malformed(self, tmp_path: Path):
        p = tmp_path / "usage.jsonl"
        p.write_text(
            json.dumps({"key_hash": "h1", "tier": "free",
                        "timestamp": "2026-04-24T10:00:00.000000Z"}) + "\n"
            "\n"
            "not json\n"
            "{partial json\n"
            + json.dumps({"key_hash": "h2", "tier": "premium",
                          "timestamp": "2026-04-24T11:00:00.000000Z"}) + "\n",
            encoding="utf-8",
        )
        rows = load_usage(p)
        assert len(rows) == 2
        assert {r["key_hash"] for r in rows} == {"h1", "h2"}


# ---------------------------------------------------------------------------
# User-count math
# ---------------------------------------------------------------------------


class TestUserCounts:
    def test_distinct_free_vs_premium(self):
        usage = [
            _usage(key_hash="alpha", tier="free",   timestamp="2026-04-01T09:00:00.000000Z"),
            _usage(key_hash="alpha", tier="free",   timestamp="2026-04-02T09:00:00.000000Z"),
            _usage(key_hash="bravo", tier="free",   timestamp="2026-04-03T09:00:00.000000Z"),
            _usage(key_hash="charlie", tier="premium", timestamp="2026-04-04T09:00:00.000000Z"),
            # alpha later converted to premium — appears in both sets.
            _usage(key_hash="alpha", tier="premium", timestamp="2026-04-10T09:00:00.000000Z"),
        ]
        conversions = [
            _conversion(api_key_hash="alpha",
                        timestamp="2026-04-10T09:00:00.000000Z",
                        first_seen_timestamp="2026-04-01T09:00:00.000000Z"),
        ]
        counts = compute_user_counts(usage, conversions)
        assert counts["total_unique"] == 3
        assert counts["free"] == 2            # alpha, bravo
        assert counts["premium"] == 2         # alpha, charlie
        assert counts["converted_from_free_to_premium"] == 1  # alpha only
        assert counts["conversion_events_recorded"] == 1

    def test_ignores_missing_key_hash(self):
        usage = [
            {"timestamp": "2026-04-01T09:00:00.000000Z", "tier": "free"},  # no key_hash
            _usage(key_hash="h1", tier="free", timestamp="2026-04-01T09:00:00.000000Z"),
        ]
        counts = compute_user_counts(usage, [])
        assert counts["total_unique"] == 1

    def test_empty_inputs(self):
        counts = compute_user_counts([], [])
        assert counts["total_unique"] == 0
        assert counts["free"] == 0
        assert counts["premium"] == 0
        assert counts["converted_from_free_to_premium"] == 0
        assert counts["conversion_events_recorded"] == 0


# ---------------------------------------------------------------------------
# Conversion-metric math
# ---------------------------------------------------------------------------


class TestConversionMetrics:
    def test_conversion_rate(self):
        usage = [
            _usage(key_hash="a", tier="free",   timestamp="2026-04-01T00:00:00.000000Z"),
            _usage(key_hash="b", tier="free",   timestamp="2026-04-01T00:00:00.000000Z"),
            _usage(key_hash="c", tier="free",   timestamp="2026-04-01T00:00:00.000000Z"),
            _usage(key_hash="d", tier="free",   timestamp="2026-04-01T00:00:00.000000Z"),
        ]
        conversions = [
            _conversion(api_key_hash="a",
                        timestamp="2026-04-15T00:00:00.000000Z"),
        ]
        cm = compute_conversion_metrics(usage, conversions)
        assert cm["conversions_total"] == 1
        assert cm["free_users"] == 4
        assert cm["conversion_rate"] == 0.25

    def test_conversion_rate_zero_free_users(self):
        conversions = [
            _conversion(api_key_hash="a",
                        timestamp="2026-04-15T00:00:00.000000Z"),
        ]
        cm = compute_conversion_metrics([], conversions)
        assert cm["conversions_total"] == 1
        assert cm["free_users"] == 0
        assert cm["conversion_rate"] is None  # undefined

    def test_conversions_per_day(self):
        conversions = [
            _conversion(api_key_hash="a",
                        timestamp="2026-04-15T00:00:00.000000Z"),
            _conversion(api_key_hash="b",
                        timestamp="2026-04-15T12:00:00.000000Z"),
            _conversion(api_key_hash="c",
                        timestamp="2026-04-16T08:00:00.000000Z"),
        ]
        cm = compute_conversion_metrics([], conversions)
        per_day = {r["date"]: r["count"] for r in cm["conversions_per_day"]}
        assert per_day == {"2026-04-15": 2, "2026-04-16": 1}

    def test_conversions_by_price_id(self):
        conversions = [
            _conversion(api_key_hash="a", timestamp="2026-04-15T00:00:00.000000Z",
                        price_id="price_monthly"),
            _conversion(api_key_hash="b", timestamp="2026-04-15T00:00:00.000000Z",
                        price_id="price_monthly"),
            _conversion(api_key_hash="c", timestamp="2026-04-15T00:00:00.000000Z",
                        price_id="price_annual"),
            _conversion(api_key_hash="d", timestamp="2026-04-15T00:00:00.000000Z",
                        price_id=None),
        ]
        cm = compute_conversion_metrics([], conversions)
        by_price = {r["price_id"]: r["count"] for r in cm["conversions_by_price_id"]}
        assert by_price == {"price_monthly": 2, "price_annual": 1, None: 1}

    def test_empty_conversions(self):
        cm = compute_conversion_metrics([], [])
        assert cm["conversions_total"] == 0
        assert cm["conversions_per_day"] == []
        assert cm["conversions_by_price_id"] == []


# ---------------------------------------------------------------------------
# Time-to-convert math
# ---------------------------------------------------------------------------


class TestTimeToConvert:
    def test_single_sample(self):
        # 5 days
        convs = [
            _conversion(api_key_hash="a",
                        timestamp="2026-04-10T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-05T00:00:00.000000Z"),
        ]
        ttc = compute_time_to_convert(convs)
        assert ttc["samples"] == 1
        assert ttc["average_days"] == 5.0
        assert ttc["median_days"] == 5.0
        assert ttc["p90_days"] == 5.0

    def test_multiple_samples_stats(self):
        # deltas: 1 day, 3 days, 7 days, 14 days, 30 days
        convs = [
            _conversion(api_key_hash="a",
                        timestamp="2026-04-02T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-01T00:00:00.000000Z"),  # 1d
            _conversion(api_key_hash="b",
                        timestamp="2026-04-04T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-01T00:00:00.000000Z"),  # 3d
            _conversion(api_key_hash="c",
                        timestamp="2026-04-08T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-01T00:00:00.000000Z"),  # 7d
            _conversion(api_key_hash="d",
                        timestamp="2026-04-15T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-01T00:00:00.000000Z"),  # 14d
            _conversion(api_key_hash="e",
                        timestamp="2026-05-01T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-01T00:00:00.000000Z"),  # 30d
        ]
        ttc = compute_time_to_convert(convs)
        assert ttc["samples"] == 5
        assert ttc["average_days"] == pytest.approx((1 + 3 + 7 + 14 + 30) / 5.0)
        assert ttc["median_days"] == 7.0
        # Nearest-rank p90 of 5 values = ceil(4.5) = 5 → values[4] = 30.
        assert ttc["p90_days"] == 30.0

    def test_missing_first_seen_skipped(self):
        convs = [
            _conversion(api_key_hash="a",
                        timestamp="2026-04-10T00:00:00.000000Z",
                        first_seen_timestamp=None),
            _conversion(api_key_hash="b",
                        timestamp="2026-04-20T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-10T00:00:00.000000Z"),
        ]
        ttc = compute_time_to_convert(convs)
        assert ttc["samples"] == 1
        assert ttc["average_days"] == 10.0

    def test_negative_delta_skipped(self):
        """first_seen > converted makes no physical sense — skip it."""
        convs = [
            _conversion(api_key_hash="bad",
                        timestamp="2026-04-01T00:00:00.000000Z",
                        first_seen_timestamp="2026-04-10T00:00:00.000000Z"),
        ]
        ttc = compute_time_to_convert(convs)
        assert ttc["samples"] == 0
        assert ttc["average_seconds"] is None

    def test_empty(self):
        ttc = compute_time_to_convert([])
        assert ttc["samples"] == 0
        assert ttc["average_seconds"] is None
        assert ttc["median_seconds"] is None
        assert ttc["p90_seconds"] is None


# ---------------------------------------------------------------------------
# Usage-intensity math
# ---------------------------------------------------------------------------


class TestUsageIntensity:
    def test_per_tier_requests(self):
        usage = [
            _usage(key_hash="a", tier="free",   timestamp="2026-04-01T09:00:00.000000Z"),
            _usage(key_hash="a", tier="free",   timestamp="2026-04-01T10:00:00.000000Z"),
            _usage(key_hash="b", tier="free",   timestamp="2026-04-01T09:00:00.000000Z"),
            _usage(key_hash="c", tier="premium", timestamp="2026-04-01T09:00:00.000000Z"),
        ]
        ui = compute_usage_intensity(usage)
        assert ui["free"]["user_count"] == 2
        assert ui["free"]["total_requests"] == 3
        assert ui["free"]["avg_requests_per_user"] == 1.5
        assert ui["premium"]["user_count"] == 1
        assert ui["premium"]["total_requests"] == 1

    def test_user_appearing_in_both_counts_as_premium(self):
        """A user who upgraded (appears in both tiers) is classified
        by their FINAL tier = premium. All of their requests flow
        into the premium total."""
        usage = [
            _usage(key_hash="a", tier="free",    timestamp="2026-04-01T09:00:00.000000Z"),
            _usage(key_hash="a", tier="free",    timestamp="2026-04-02T09:00:00.000000Z"),
            _usage(key_hash="a", tier="premium", timestamp="2026-04-10T09:00:00.000000Z"),
        ]
        ui = compute_usage_intensity(usage)
        # User 'a' counts once under premium.
        assert ui["premium"]["user_count"] == 1
        assert ui["premium"]["total_requests"] == 3
        assert ui["free"]["user_count"] == 0
        assert ui["free"]["total_requests"] == 0

    def test_top_percentile_ordering(self):
        # 10 distinct users, each making (i+1) requests.
        usage = []
        for i in range(10):
            for _ in range(i + 1):
                usage.append(_usage(
                    key_hash=f"u{i:02d}", tier="free",
                    timestamp="2026-04-01T09:00:00.000000Z",
                ))
        ui = compute_usage_intensity(usage)
        # Top 5% of 10 = ceil(0.5) = 1 → single top user.
        top5 = ui["top_percentile_users"]["top_5_percent"]
        assert len(top5) == 1
        assert top5[0]["key_hash"] == "u09"
        assert top5[0]["requests"] == 10
        # Top 10% of 10 = ceil(1) = 1 (same result for n=10).
        top10 = ui["top_percentile_users"]["top_10_percent"]
        assert len(top10) == 1

    def test_top_percentile_floors_at_one(self):
        """Single-user dataset → the 5% bracket still surfaces that one user."""
        usage = [
            _usage(key_hash="solo", tier="premium",
                   timestamp="2026-04-01T09:00:00.000000Z"),
        ]
        ui = compute_usage_intensity(usage)
        assert len(ui["top_percentile_users"]["top_5_percent"]) == 1
        assert ui["top_percentile_users"]["top_5_percent"][0]["key_hash"] == "solo"

    def test_empty_usage(self):
        ui = compute_usage_intensity([])
        assert ui["free"]["user_count"] == 0
        assert ui["free"]["total_requests"] == 0
        assert ui["free"]["avg_requests_per_user"] is None
        assert ui["premium"]["user_count"] == 0
        assert ui["premium"]["avg_requests_per_user"] is None
        # Top brackets still present, but empty.
        for bracket in ui["top_percentile_users"].values():
            assert bracket == []

    def test_top_percentile_keys_are_documented(self):
        ui = compute_usage_intensity([])
        assert set(ui["top_percentile_users"].keys()) == {
            f"top_{int(p * 100)}_percent" for p in TOP_PERCENTILES
        }


# ---------------------------------------------------------------------------
# build_pricing_report + JSON/text formatters
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_inputs(tmp_path: Path):
    usage_path = tmp_path / "api_usage.jsonl"
    conv_path = tmp_path / "api_conversions.jsonl"

    usage_rows = [
        # Four free users (a, b, c, d). One (a) later converts.
        _usage(key_hash="hash_a", tier="free",   timestamp="2026-04-01T09:00:00.000000Z"),
        _usage(key_hash="hash_a", tier="free",   timestamp="2026-04-03T09:00:00.000000Z"),
        _usage(key_hash="hash_a", tier="premium", timestamp="2026-04-10T09:00:00.000000Z"),
        _usage(key_hash="hash_a", tier="premium", timestamp="2026-04-15T09:00:00.000000Z"),
        _usage(key_hash="hash_b", tier="free",   timestamp="2026-04-02T09:00:00.000000Z"),
        _usage(key_hash="hash_c", tier="free",   timestamp="2026-04-02T10:00:00.000000Z"),
        _usage(key_hash="hash_c", tier="free",   timestamp="2026-04-03T10:00:00.000000Z"),
        _usage(key_hash="hash_d", tier="free",   timestamp="2026-04-02T10:00:00.000000Z"),
    ]
    conv_rows = [
        _conversion(
            api_key_hash="hash_a",
            timestamp="2026-04-10T09:00:00.000000Z",
            first_seen_timestamp="2026-04-01T09:00:00.000000Z",
            price_id="price_monthly",
        ),
    ]
    _write_jsonl(usage_path, usage_rows)
    _write_jsonl(conv_path, conv_rows)
    return usage_path, conv_path


class TestBuildPricingReportStructure:
    def test_all_top_level_keys(self, populated_inputs):
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(usage_path, conv_path,
                                      report_date="2026-04-24")
        required = {
            "report_type", "report_date", "sources",
            "user_counts", "conversion_metrics",
            "time_to_convert", "usage_intensity",
        }
        assert required.issubset(report.keys())
        assert report["report_type"] == REPORT_TYPE
        assert report["report_date"] == "2026-04-24"

    def test_sources_populated(self, populated_inputs):
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(usage_path, conv_path)
        assert report["sources"]["usage_log"]["exists"] is True
        assert report["sources"]["usage_log"]["rows"] == 8
        assert report["sources"]["conversion_log"]["exists"] is True
        assert report["sources"]["conversion_log"]["rows"] == 1

    def test_end_to_end_numbers(self, populated_inputs):
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(usage_path, conv_path)

        # User counts
        uc = report["user_counts"]
        assert uc["total_unique"] == 4
        assert uc["free"] == 4           # a, b, c, d
        assert uc["premium"] == 1        # a only
        assert uc["converted_from_free_to_premium"] == 1

        # Conversion metrics
        cm = report["conversion_metrics"]
        assert cm["conversions_total"] == 1
        assert cm["free_users"] == 4
        assert cm["conversion_rate"] == 0.25

        # Time-to-convert: 10 - 1 = 9 days.
        ttc = report["time_to_convert"]
        assert ttc["samples"] == 1
        assert ttc["average_days"] == 9.0

        # Usage intensity
        ui = report["usage_intensity"]
        # User a's 4 requests flow into premium; b/c/d's 4 requests
        # flow into free.
        assert ui["premium"]["total_requests"] == 4
        assert ui["free"]["total_requests"] == 4

    def test_json_is_serializable(self, populated_inputs):
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(usage_path, conv_path)
        blob = format_json(report)
        parsed = json.loads(blob)
        assert parsed["report_type"] == REPORT_TYPE

    def test_text_contains_section_headings(self, populated_inputs):
        usage_path, conv_path = populated_inputs
        txt = format_text(build_pricing_report(usage_path, conv_path))
        for heading in (
            "PRICING INTELLIGENCE REPORT",
            "User counts",
            "Conversion metrics",
            "Time-to-convert",
            "Usage intensity",
        ):
            assert heading in txt


# ---------------------------------------------------------------------------
# Empty / missing inputs
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_both_missing(self, tmp_path: Path):
        report = build_pricing_report(
            tmp_path / "missing_u.jsonl",
            tmp_path / "missing_c.jsonl",
            report_date="2026-04-24",
        )
        assert report["user_counts"]["total_unique"] == 0
        assert report["conversion_metrics"]["conversions_total"] == 0
        assert report["time_to_convert"]["samples"] == 0
        # Valid text / JSON render even on empty.
        txt = format_text(report)
        assert "PRICING INTELLIGENCE REPORT" in txt
        json.loads(format_json(report))  # must parse

    def test_only_usage_present(self, tmp_path: Path):
        usage = tmp_path / "usage.jsonl"
        _write_jsonl(usage, [
            _usage(key_hash="h", tier="free",
                   timestamp="2026-04-01T09:00:00.000000Z"),
        ])
        report = build_pricing_report(usage, tmp_path / "missing.jsonl")
        assert report["conversion_metrics"]["conversion_rate"] == 0.0
        # conversion_rate = 0 / 1 = 0.0 exactly.

    def test_only_conversions_present(self, tmp_path: Path):
        conv = tmp_path / "conv.jsonl"
        _write_jsonl(conv, [
            _conversion(api_key_hash="h",
                        timestamp="2026-04-10T00:00:00.000000Z"),
        ])
        report = build_pricing_report(tmp_path / "missing.jsonl", conv)
        # No free users at all → rate undefined (None).
        assert report["conversion_metrics"]["conversion_rate"] is None


# ---------------------------------------------------------------------------
# Persistence + CLI
# ---------------------------------------------------------------------------


class TestWriteReports:
    def test_writes_txt_and_json(self, populated_inputs, tmp_path: Path):
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(
            usage_path, conv_path, report_date="2026-04-24",
        )
        txt_path, json_path = write_reports(
            report, reports_dir=tmp_path / "reports",
        )
        assert txt_path.exists()
        assert json_path.exists()
        assert txt_path.name == "pricing_report_2026-04-24.txt"
        assert json_path.name == "pricing_report_2026-04-24.json"
        # JSON file parses cleanly.
        parsed = json.loads(json_path.read_text(encoding="utf-8"))
        assert parsed["report_type"] == REPORT_TYPE

    def test_creates_parent_dirs(self, populated_inputs, tmp_path: Path):
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(usage_path, conv_path)
        deep = tmp_path / "a" / "b" / "c"
        txt_path, _ = write_reports(report, reports_dir=deep)
        assert deep.is_dir()
        assert txt_path.exists()


class TestCli:
    def test_main_writes_reports(
        self, populated_inputs, tmp_path: Path, capsys
    ):
        usage_path, conv_path = populated_inputs
        reports_dir = tmp_path / "reports"
        rc = pricing_main([
            "--usage", str(usage_path),
            "--conversions", str(conv_path),
            "--reports-dir", str(reports_dir),
            "--date", "2026-04-24",
        ])
        assert rc == 0
        assert (reports_dir / "pricing_report_2026-04-24.txt").exists()
        assert (reports_dir / "pricing_report_2026-04-24.json").exists()
        out = capsys.readouterr().out
        assert "pricing_report_2026-04-24" in out

    def test_main_default_date_is_today_utc(
        self, populated_inputs, tmp_path: Path, monkeypatch
    ):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage_path, conv_path = populated_inputs
        reports_dir = tmp_path / "reports"
        rc = pricing_main([
            "--usage", str(usage_path),
            "--conversions", str(conv_path),
            "--reports-dir", str(reports_dir),
        ])
        assert rc == 0
        assert (reports_dir / f"pricing_report_{today}.txt").exists()

    def test_main_json_only(
        self, populated_inputs, tmp_path: Path, capsys
    ):
        usage_path, conv_path = populated_inputs
        rc = pricing_main([
            "--usage", str(usage_path),
            "--conversions", str(conv_path),
            "--reports-dir", str(tmp_path / "unused"),
            "--json-only",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["report_type"] == REPORT_TYPE
        # json-only should NOT have written files.
        assert not (tmp_path / "unused").exists()

    def test_main_text_only(
        self, populated_inputs, tmp_path: Path, capsys
    ):
        usage_path, conv_path = populated_inputs
        rc = pricing_main([
            "--usage", str(usage_path),
            "--conversions", str(conv_path),
            "--reports-dir", str(tmp_path / "unused"),
            "--text-only",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PRICING INTELLIGENCE REPORT" in out
        assert not (tmp_path / "unused").exists()

    def test_main_mutually_exclusive_flags(
        self, populated_inputs, tmp_path: Path, capsys
    ):
        usage_path, conv_path = populated_inputs
        rc = pricing_main([
            "--usage", str(usage_path),
            "--conversions", str(conv_path),
            "--reports-dir", str(tmp_path / "r"),
            "--json-only", "--text-only",
        ])
        assert rc == 2

    def test_subprocess_smoke(self, populated_inputs, tmp_path: Path):
        usage_path, conv_path = populated_inputs
        reports_dir = tmp_path / "reports"
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.pricing_report",
                "--usage", str(usage_path),
                "--conversions", str(conv_path),
                "--reports-dir", str(reports_dir),
                "--date", "2026-04-24",
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "pricing_report_2026-04-24" in result.stdout
        assert (reports_dir / "pricing_report_2026-04-24.json").exists()


# ---------------------------------------------------------------------------
# Privacy — no raw keys / no PII / no card data in the report
# ---------------------------------------------------------------------------


class TestNoSensitiveDataInReport:
    def test_raw_key_marker_never_in_report(self, tmp_path: Path):
        """If a malformed upstream writer ever wrote a raw key as
        the key_hash field, the report must propagate it as-is
        (that's a bug at the writer layer) — but the report must
        NEVER invent additional leakage. This test plants a marker
        in a non-key field and asserts it never leaks."""
        usage_path = tmp_path / "usage.jsonl"
        # Plant markers in fields that should never appear in the report.
        _write_jsonl(usage_path, [{
            "timestamp": "2026-04-01T09:00:00.000000Z",
            "key_hash": "h1",
            "tier": "free",
            "method": "GET",
            "path": "/reports/latest",
            "status_code": 200,
            "duration_ms": 1.5,
            "request_id": "rid-1",
            # The following are leak markers — NOT part of the
            # documented schema. The report must not propagate them.
            "customer_email": "LEAK_EMAIL_MARKER@example.com",
            "card_pan": "4242424242424242_LEAK",
            "raw_authorization": "Bearer super-secret-LEAK",
        }])
        conv_path = tmp_path / "conv.jsonl"
        _write_jsonl(conv_path, [])
        report = build_pricing_report(usage_path, conv_path)
        blob = format_json(report) + "\n" + format_text(report)
        for marker in (
            "LEAK_EMAIL_MARKER",
            "4242424242424242_LEAK",
            "super-secret-LEAK",
        ):
            assert marker not in blob, f"report leaked {marker!r}"

    def test_report_only_propagates_documented_schema(self, populated_inputs):
        """Output JSON must have no keys containing obviously-sensitive
        substrings from the input schema."""
        usage_path, conv_path = populated_inputs
        report = build_pricing_report(usage_path, conv_path)
        blob = format_json(report).lower()
        for forbidden in ("email", "password", "pan", "cvv", "card_number", "bearer"):
            assert forbidden not in blob
