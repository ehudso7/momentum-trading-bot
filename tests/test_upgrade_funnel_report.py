"""
Phase 5.6 tests — upgrade funnel report.

Exercises ``trading_bot.analysis.upgrade_funnel_report`` against
hand-written JSONL fixtures and verifies:

  * the event↔conversion join on api_key_hash is correct;
  * conversion-rate math is correct per event and per ref_code;
  * the event → conversion time delta uses the user's earliest
    event of that type and is clamped at >= 0;
  * the strongest / weakest / strongest-ref_code rankings honour
    the min-support guard;
  * empty / missing / corrupt input files produce a valid but
    empty-ish report (no exception);
  * no raw API key / PII / path marker leaks into either report;
  * JSON shape is the documented one and both output paths are
    written by the CLI happy path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from trading_bot.analysis.upgrade_funnel_report import (
    REPORT_TYPE,
    build_upgrade_funnel_report,
    compute_event_metrics,
    compute_ref_code_metrics,
    compute_top_picks,
    format_json,
    format_text,
    load_conversions,
    load_growth,
    load_upgrade_events,
    load_usage,
    write_reports,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _h(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


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
        "growth": tmp_path / "growth.jsonl",
        "usage": tmp_path / "usage.jsonl",
        "reports": tmp_path / "reports",
    }


# ---------------------------------------------------------------------------
# Loader / tolerance
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_missing_files_yield_empty_lists(self, tmp_path: Path):
        miss = tmp_path / "nope.jsonl"
        assert load_upgrade_events(miss) == []
        assert load_conversions(miss) == []
        assert load_growth(miss) == []
        assert load_usage(miss) == []

    def test_corrupt_lines_are_skipped(self, paths):
        paths["events"].write_text(
            "\n".join([
                json.dumps({"event": "dashboard_banner_seen",
                            "api_key_hash": "a"}),
                "garbage",
                "   ",
                "[\"not a dict\"]",
                json.dumps({"event": "report_limit_hit",
                            "api_key_hash": "b"}),
            ])
            + "\n",
            encoding="utf-8",
        )
        rows = load_upgrade_events(paths["events"])
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Per-event metrics
# ---------------------------------------------------------------------------


class TestEventMetrics:
    def test_basic_event_to_conversion_join(self):
        ha, hb = _h("a"), _h("b")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hb, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-11T10:00:00.000000Z"},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-14T10:00:00.000000Z"},
        ]
        (row,) = compute_event_metrics(events, conversions)
        assert row["event"] == "dashboard_banner_seen"
        assert row["event_count"] == 2
        assert row["unique_users"] == 2
        assert row["converted_users"] == 1
        assert row["conversion_rate"] == 0.5
        # delta = 4 * 86400 = 345600s for the one converted user.
        assert row["median_time_from_event_to_conversion_seconds"] == 345600.0
        assert row["median_time_from_event_to_conversion_days"] == 4.0
        # Single-sample, so p90 equals median.
        assert row["p90_time_from_event_to_conversion_seconds"] == 345600.0
        assert row["delta_samples"] == 1

    def test_uses_earliest_event_for_time_delta(self):
        ha = _h("a")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-13T10:00:00.000000Z"},  # later
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-14T10:00:00.000000Z"},
        ]
        (row,) = compute_event_metrics(events, conversions)
        # Earliest event was 04-10 → delta = 4 days, NOT 1 day.
        assert row["median_time_from_event_to_conversion_days"] == 4.0

    def test_uses_earliest_conversion_when_user_has_multiple(self):
        ha = _h("a")
        events = [
            {"api_key_hash": ha, "event": "report_limit_hit",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-12T10:00:00.000000Z"},
            {"api_key_hash": ha, "timestamp": "2026-04-20T10:00:00.000000Z"},
        ]
        (row,) = compute_event_metrics(events, conversions)
        assert row["median_time_from_event_to_conversion_days"] == 2.0

    def test_unparseable_timestamps_are_excluded_from_delta(self):
        ha, hb = _h("a"), _h("b")
        events = [
            {"api_key_hash": ha, "event": "report_limit_hit",
             "timestamp": "garbage"},
            {"api_key_hash": hb, "event": "report_limit_hit",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-11T10:00:00.000000Z"},
            {"api_key_hash": hb, "timestamp": "2026-04-12T10:00:00.000000Z"},
        ]
        (row,) = compute_event_metrics(events, conversions)
        assert row["unique_users"] == 2
        assert row["converted_users"] == 2
        assert row["conversion_rate"] == 1.0
        # Only b contributes a delta — a's event has no parseable
        # timestamp.
        assert row["delta_samples"] == 1
        assert row["median_time_from_event_to_conversion_days"] == 2.0

    def test_negative_deltas_are_dropped(self):
        """If a user converted BEFORE their earliest event row (odd
        but possible data), we skip rather than poison the stats."""
        ha = _h("a")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ]
        conversions = [
            {"api_key_hash": ha,
             "timestamp": "2026-04-09T10:00:00.000000Z"},  # before event
        ]
        (row,) = compute_event_metrics(events, conversions)
        assert row["converted_users"] == 1
        assert row["delta_samples"] == 0
        assert row["median_time_from_event_to_conversion_days"] is None
        assert row["p90_time_from_event_to_conversion_days"] is None

    def test_rows_without_required_fields_are_dropped(self):
        rows = compute_event_metrics(
            events=[
                {"api_key_hash": _h("a")},                # no event
                {"event": "report_limit_hit"},            # no hash
                {"event": "", "api_key_hash": _h("b")},   # empty event
                {"event": "report_limit_hit",
                 "api_key_hash": _h("c"),
                 "timestamp": "2026-04-10T10:00:00.000000Z"},
            ],
            conversions=[],
        )
        assert [r["event"] for r in rows] == ["report_limit_hit"]

    def test_deterministic_sort_by_rate_desc_then_event_asc(self):
        ha, hb, hc, hd = _h("a"), _h("b"), _h("c"), _h("d")
        events = [
            {"api_key_hash": ha, "event": "z_event",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hb, "event": "a_event",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hc, "event": "a_event",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hd, "event": "m_event",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-12T10:00:00.000000Z"},
            {"api_key_hash": hd, "timestamp": "2026-04-13T10:00:00.000000Z"},
        ]
        rows = compute_event_metrics(events, conversions)
        # z_event: 1 user, 1 conv → 100%. m_event: 1/1 → 100%.
        # a_event: 2/0 → 0%. Ties at 100% resolve alphabetically:
        # m_event < z_event.
        assert [r["event"] for r in rows] == ["m_event", "z_event", "a_event"]


# ---------------------------------------------------------------------------
# Per-ref_code metrics
# ---------------------------------------------------------------------------


class TestRefCodeMetrics:
    def test_ref_attribution_is_inclusive(self):
        """A user who hit an upgrade prompt with ref=twitter AND
        another prompt with ref=hn contributes to both channels."""
        ha = _h("a")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "ref_code": "twitter",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": ha, "event": "report_limit_hit",
             "ref_code": "hn",
             "timestamp": "2026-04-11T10:00:00.000000Z"},
        ]
        conversions = [
            {"api_key_hash": ha, "timestamp": "2026-04-12T10:00:00.000000Z"},
        ]
        rows = {r["ref_code"]: r for r in
                compute_ref_code_metrics(events, conversions)}
        assert rows["twitter"]["conversions"] == 1
        assert rows["hn"]["conversions"] == 1
        assert rows["twitter"]["conversion_rate"] == 1.0
        assert rows["hn"]["conversion_rate"] == 1.0

    def test_ref_rows_exclude_events_without_ref(self):
        ha, hb = _h("a"), _h("b")
        events = [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hb, "event": "dashboard_banner_seen",
             "ref_code": "twitter",
             "timestamp": "2026-04-11T10:00:00.000000Z"},
        ]
        rows = compute_ref_code_metrics(events, [])
        assert len(rows) == 1
        assert rows[0]["ref_code"] == "twitter"
        assert rows[0]["upgrade_events"] == 1
        assert rows[0]["unique_users"] == 1

    def test_ref_deterministic_sort(self):
        ha, hb, hc = _h("a"), _h("b"), _h("c")
        events = [
            {"api_key_hash": ha, "event": "x", "ref_code": "zeta",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hb, "event": "x", "ref_code": "alpha",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hc, "event": "x", "ref_code": "alpha",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ]
        rows = compute_ref_code_metrics(events, [])
        # Both 0% — tie-break alphabetically.
        assert [r["ref_code"] for r in rows] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Top picks
# ---------------------------------------------------------------------------


class TestTopPicks:
    def _row(self, **kw):
        base = {
            "event": "e",
            "ref_code": "r",
            "event_count": 0,
            "upgrade_events": 0,
            "unique_users": 0,
            "converted_users": 0,
            "conversions": 0,
            "conversion_rate": None,
        }
        base.update(kw)
        return base

    def test_empty_rows_yield_all_none(self):
        picks = compute_top_picks([], [])
        assert picks == {
            "strongest_upgrade_trigger": None,
            "weakest_upgrade_trigger": None,
            "strongest_ref_code": None,
        }

    def test_basic_picks_respect_min_support(self):
        event_rows = [
            self._row(event="tiny", unique_users=1, conversion_rate=1.0),
            self._row(event="real", unique_users=10, conversion_rate=0.30),
            self._row(event="dead", unique_users=5, conversion_rate=0.0),
        ]
        picks = compute_top_picks(event_rows, [])
        # tiny has only 1 user — excluded by MIN_SUPPORT_FOR_RANKING.
        assert picks["strongest_upgrade_trigger"] == {
            "event": "real", "value": 0.30,
        }
        assert picks["weakest_upgrade_trigger"] == {
            "event": "dead", "value": 0.0,
        }

    def test_ties_break_alphabetically_on_name(self):
        event_rows = [
            self._row(event="zeta", unique_users=5, conversion_rate=0.5),
            self._row(event="alpha", unique_users=5, conversion_rate=0.5),
        ]
        picks = compute_top_picks(event_rows, [])
        assert picks["strongest_upgrade_trigger"]["event"] == "alpha"
        assert picks["weakest_upgrade_trigger"]["event"] == "alpha"

    def test_min_support_override(self):
        event_rows = [
            self._row(event="tiny", unique_users=1, conversion_rate=1.0),
            self._row(event="real", unique_users=10, conversion_rate=0.30),
        ]
        picks = compute_top_picks(event_rows, [], min_users=1)
        assert picks["strongest_upgrade_trigger"] == {
            "event": "tiny", "value": 1.0,
        }

    def test_rows_without_conversion_rate_are_ignored(self):
        event_rows = [
            self._row(event="no_rate", unique_users=10, conversion_rate=None),
            self._row(event="real", unique_users=10, conversion_rate=0.10),
        ]
        picks = compute_top_picks(event_rows, [])
        assert picks["strongest_upgrade_trigger"]["event"] == "real"
        assert picks["weakest_upgrade_trigger"]["event"] == "real"


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_empty_inputs_yield_valid_report(self, paths):
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
            report_date="2026-04-24",
        )
        assert r["report_type"] == REPORT_TYPE
        assert r["report_date"] == "2026-04-24"
        assert r["events"] == []
        assert r["ref_codes"] == []
        assert r["totals"]["events"] == 0
        assert r["totals"]["unique_users"] == 0
        assert all(v is None for v in r["top_picks"].values())
        for name in (
            "upgrade_events_log", "conversion_log",
            "growth_log", "usage_log",
        ):
            meta = r["sources"][name]
            assert meta["exists"] is False
            assert meta["rows"] == 0

    def test_report_is_deterministic_for_same_inputs(self, paths):
        ha, hb = _h("a"), _h("b")
        _write_jsonl(paths["events"], [
            {"api_key_hash": ha, "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z",
             "ref_code": "twitter"},
            {"api_key_hash": hb, "event": "report_limit_hit",
             "timestamp": "2026-04-11T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [
            {"api_key_hash": ha, "timestamp": "2026-04-14T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        a = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
            report_date="2026-04-24",
        )
        b = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
            report_date="2026-04-24",
        )
        assert a == b
        assert format_json(a) == format_json(b)
        assert format_text(a) == format_text(b)

    def test_totals_are_consistent_with_rows(self, paths):
        hs = [_h(f"u{i}") for i in range(4)]
        _write_jsonl(paths["events"], [
            {"api_key_hash": hs[0], "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
            {"api_key_hash": hs[1], "event": "dashboard_banner_seen",
             "timestamp": "2026-04-11T10:00:00.000000Z",
             "ref_code": "hn"},
            {"api_key_hash": hs[2], "event": "report_limit_hit",
             "timestamp": "2026-04-12T10:00:00.000000Z"},
            {"api_key_hash": hs[3], "event": "report_limit_hit",
             "timestamp": "2026-04-13T10:00:00.000000Z",
             "ref_code": "twitter"},
        ])
        _write_jsonl(paths["conv"], [
            {"api_key_hash": hs[0], "timestamp": "2026-04-14T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
        )
        assert r["totals"]["events"] == 4
        assert r["totals"]["unique_users"] == 4
        assert r["totals"]["converted_users"] == 1
        assert r["totals"]["distinct_event_names"] == 2
        assert r["totals"]["distinct_ref_codes"] == 2


# ---------------------------------------------------------------------------
# JSON structure
# ---------------------------------------------------------------------------


class TestJsonStructure:
    def test_top_level_shape(self, paths):
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("a"), "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
            report_date="2026-04-24",
        )
        parsed = json.loads(format_json(r))
        assert set(parsed.keys()) == {
            "report_type", "report_date", "sources",
            "min_users_for_ranking", "totals", "events",
            "ref_codes", "top_picks",
        }
        assert parsed["report_type"] == "upgrade_funnel"
        assert set(parsed["sources"].keys()) == {
            "upgrade_events_log", "conversion_log",
            "growth_log", "usage_log",
        }
        assert set(parsed["top_picks"].keys()) == {
            "strongest_upgrade_trigger",
            "weakest_upgrade_trigger",
            "strongest_ref_code",
        }

    def test_each_event_row_has_documented_fields(self, paths):
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("a"), "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
        )
        (row,) = r["events"]
        assert set(row.keys()) == {
            "event", "event_count", "unique_users", "converted_users",
            "conversion_rate", "delta_samples",
            "median_time_from_event_to_conversion_seconds",
            "p90_time_from_event_to_conversion_seconds",
            "median_time_from_event_to_conversion_days",
            "p90_time_from_event_to_conversion_days",
        }

    def test_each_ref_row_has_documented_fields(self, paths):
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("a"), "event": "dashboard_banner_seen",
             "ref_code": "twitter",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
        )
        (row,) = r["ref_codes"]
        assert set(row.keys()) == {
            "ref_code", "upgrade_events", "unique_users",
            "conversions", "conversion_rate",
        }


# ---------------------------------------------------------------------------
# PII / leak guard
# ---------------------------------------------------------------------------


class TestNoLeaks:
    def test_unique_markers_never_appear_in_output(self, paths):
        ha = _h("a")
        _write_jsonl(paths["events"], [
            {
                "api_key_hash": ha,
                "event": "dashboard_banner_seen",
                "timestamp": "2026-04-10T10:00:00.000000Z",
                # Planted markers — must NEVER be in either report.
                "path": "/UNIQUE_EVENT_PATH_LEAK",
                "request_id": "UNIQUE_REQID_LEAK_EVENT",
                "ref_code": "UNIQUE_REF_LEAK",
                "tier": "free",
                "secret_marker": "DO_NOT_LEAK_ME",
            },
        ])
        _write_jsonl(paths["conv"], [
            {
                "api_key_hash": ha,
                "timestamp": "2026-04-14T10:00:00.000000Z",
                "price_id": "UNIQUE_PRICE_ID_LEAK",
                "source": "UNIQUE_SOURCE_LEAK",
                "card_last4": "4242",
            },
        ])
        _write_jsonl(paths["growth"], [
            {
                "api_key_hash": ha,
                "ref_code": "UNIQUE_GROWTH_REF_LEAK",
                "path": "/UNIQUE_GROWTH_PATH_LEAK",
                "request_id": "UNIQUE_GROWTH_REQID_LEAK",
            },
        ])
        _write_jsonl(paths["usage"], [
            {
                "key_hash": ha,
                "path": "/UNIQUE_USAGE_PATH_LEAK",
                "request_id": "UNIQUE_USAGE_REQID_LEAK",
                "ip": "10.11.12.13",
                "email": "attacker@example.org",
            },
        ])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
        )
        text = format_text(r)
        js = format_json(r)
        # ref_code from the event IS public metadata — the event
        # row carries it. Everything else is private.
        for marker in (
            "UNIQUE_EVENT_PATH_LEAK",
            "UNIQUE_REQID_LEAK_EVENT",
            "DO_NOT_LEAK_ME",
            "UNIQUE_PRICE_ID_LEAK",
            "UNIQUE_SOURCE_LEAK",
            "UNIQUE_GROWTH_REF_LEAK",
            "UNIQUE_GROWTH_PATH_LEAK",
            "UNIQUE_GROWTH_REQID_LEAK",
            "UNIQUE_USAGE_PATH_LEAK",
            "UNIQUE_USAGE_REQID_LEAK",
            "10.11.12.13",
            "attacker@example.org",
            "4242",
        ):
            assert marker not in text, f"text leaked: {marker}"
            assert marker not in js, f"json leaked: {marker}"

    def test_raw_keys_cannot_appear(self, paths):
        """The loader only sees hashes (the live loggers always
        hash), so a raw API key cannot enter the report pipeline
        even if we wanted it to."""
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("secret-raw-key-X"),
             "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
        )
        for payload in (format_text(r), format_json(r)):
            assert "secret-raw-key-X" not in payload

    def test_event_row_does_not_include_api_key_hash(self, paths):
        """Per-event aggregates should NEVER surface per-user
        hashes. Otherwise BI pipelines could derive which users
        fired which events."""
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("a"), "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
        )
        (event_row,) = r["events"]
        assert "api_key_hash" not in event_row


# ---------------------------------------------------------------------------
# write_reports + CLI
# ---------------------------------------------------------------------------


class TestWriteReports:
    def test_writes_txt_and_json(self, paths):
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("a"), "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        r = build_upgrade_funnel_report(
            paths["events"], paths["conv"], paths["growth"], paths["usage"],
            report_date="2026-04-24",
        )
        txt_path, json_path = write_reports(r, reports_dir=paths["reports"])
        assert txt_path == paths["reports"] / "upgrade_funnel_report_2026-04-24.txt"
        assert json_path == paths["reports"] / "upgrade_funnel_report_2026-04-24.json"
        assert "UPGRADE FUNNEL REPORT" in txt_path.read_text()
        parsed = json.loads(json_path.read_text())
        assert parsed["report_type"] == "upgrade_funnel"


class TestCli:
    def test_cli_writes_both_files(self, paths):
        _write_jsonl(paths["events"], [
            {"api_key_hash": _h("a"), "event": "dashboard_banner_seen",
             "timestamp": "2026-04-10T10:00:00.000000Z"},
        ])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        result = subprocess.run(
            [
                sys.executable, "-m",
                "trading_bot.analysis.upgrade_funnel_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (paths["reports"] / "upgrade_funnel_report_2026-04-24.txt").exists()
        assert (paths["reports"] / "upgrade_funnel_report_2026-04-24.json").exists()
        assert "upgrade funnel report written" in result.stdout.lower()

    def test_cli_json_only(self, paths):
        _write_jsonl(paths["events"], [])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        result = subprocess.run(
            [
                sys.executable, "-m",
                "trading_bot.analysis.upgrade_funnel_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
                "--json-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["report_type"] == "upgrade_funnel"

    def test_cli_text_only(self, paths):
        _write_jsonl(paths["events"], [])
        _write_jsonl(paths["conv"], [])
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        result = subprocess.run(
            [
                sys.executable, "-m",
                "trading_bot.analysis.upgrade_funnel_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--reports-dir", str(paths["reports"]),
                "--text-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "UPGRADE FUNNEL REPORT" in result.stdout

    def test_cli_mutually_exclusive_flags(self, paths):
        result = subprocess.run(
            [
                sys.executable, "-m",
                "trading_bot.analysis.upgrade_funnel_report",
                "--events", str(paths["events"]),
                "--conversions", str(paths["conv"]),
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--json-only", "--text-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Boundary: analysis module must not import Core or the API
# ---------------------------------------------------------------------------


class TestNoCoreOrApiImports:
    def test_module_is_self_contained(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "analysis" / "upgrade_funnel_report.py"
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
                f"upgrade_funnel_report.py imports restricted surface: {forbidden!r}"
            )
