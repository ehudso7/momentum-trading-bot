"""
Phase 5.3 tests — channel performance report.

Exercises ``trading_bot.analysis.channel_report`` against hand-written
JSONL fixtures and verifies:
  * the three-way join on api_key_hash produces the right per-ref rows,
  * conversion-rate arithmetic is correct and guarded by min support,
  * per-channel tallies are correct under duplicate rows and
    cross-referral (one user appearing in multiple refs),
  * empty / missing / corrupt input files produce a valid but
    empty-ish report (no exception),
  * no raw API key / PII / request_id / path leaks into the report,
  * JSON / text shapes are deterministic,
  * CLI writes both files and exits 0.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from trading_bot.analysis.channel_report import (
    MIN_SUPPORT_FOR_RATE_RANKING,
    REPORT_TYPE,
    build_channel_report,
    compute_channel_rows,
    compute_top_channels,
    format_json,
    format_text,
    load_conversions,
    load_growth,
    load_usage,
    write_reports,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _h(key: str) -> str:
    """Mirror ``trading_bot.api.growth._hash_api_key`` exactly."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "growth": tmp_path / "growth.jsonl",
        "usage": tmp_path / "usage.jsonl",
        "conv": tmp_path / "conv.jsonl",
        "reports": tmp_path / "reports",
    }


# ---------------------------------------------------------------------------
# Loader / tolerance
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_missing_files_yield_empty_lists(self, tmp_path: Path):
        assert load_growth(tmp_path / "none.jsonl") == []
        assert load_usage(tmp_path / "none.jsonl") == []
        assert load_conversions(tmp_path / "none.jsonl") == []

    def test_corrupt_lines_are_skipped(self, paths):
        paths["growth"].write_text(
            "\n".join([
                json.dumps({"ref_code": "a", "api_key_hash": "h1"}),
                "not valid json",
                "   ",
                json.dumps(["not a dict"]),
                json.dumps({"ref_code": "a", "api_key_hash": "h2"}),
            ])
            + "\n",
            encoding="utf-8",
        )
        rows = load_growth(paths["growth"])
        assert len(rows) == 2
        assert all(r.get("ref_code") == "a" for r in rows)

    def test_empty_file_yields_empty_list(self, paths):
        paths["growth"].write_text("", encoding="utf-8")
        assert load_growth(paths["growth"]) == []


# ---------------------------------------------------------------------------
# Per-channel join correctness
# ---------------------------------------------------------------------------


class TestComputeChannelRows:
    def test_basic_three_way_join(self):
        """Two refs, three users, one converter. Verify all eight
        per-ref fields against hand-computed values."""
        ha, hb, hd = _h("a"), _h("b"), _h("d")
        growth = [
            {"ref_code": "twitter", "api_key_hash": ha},
            {"ref_code": "twitter", "api_key_hash": hb},
            {"ref_code": "hn", "api_key_hash": hb},
            {"ref_code": "hn", "api_key_hash": hd},
        ]
        usage = [
            {"key_hash": ha, "tier": "free"},
            {"key_hash": ha, "tier": "free"},
            {"key_hash": hb, "tier": "premium"},
            {"key_hash": hb, "tier": "premium"},
            {"key_hash": hb, "tier": "premium"},
            {"key_hash": hd, "tier": "free"},
        ]
        conversions = [{"api_key_hash": hb, "price_id": "p1"}]
        rows = compute_channel_rows(growth, usage, conversions)
        by_ref = {r["ref_code"]: r for r in rows}
        assert by_ref["twitter"]["unique_users"] == 2
        assert by_ref["twitter"]["total_growth_events"] == 2
        assert by_ref["twitter"]["total_usage_requests"] == 5  # 2 free + 3 premium
        assert by_ref["twitter"]["premium_usage_requests"] == 3
        assert by_ref["twitter"]["conversions"] == 1
        assert by_ref["twitter"]["conversion_rate"] == 0.5
        assert by_ref["twitter"]["avg_requests_per_user"] == 2.5
        assert by_ref["hn"]["unique_users"] == 2
        assert by_ref["hn"]["total_growth_events"] == 2
        assert by_ref["hn"]["total_usage_requests"] == 4  # 3 premium + 1 free
        assert by_ref["hn"]["premium_usage_requests"] == 3
        assert by_ref["hn"]["conversions"] == 1
        assert by_ref["hn"]["conversion_rate"] == 0.5

    def test_user_in_multiple_refs_counts_in_both(self):
        """Attribution is inclusive — a user who arrived via both
        'twitter' and 'hn' contributes to both channels' conversions."""
        hb = _h("b")
        growth = [
            {"ref_code": "twitter", "api_key_hash": hb},
            {"ref_code": "hn", "api_key_hash": hb},
        ]
        usage = [{"key_hash": hb, "tier": "premium"}]
        conversions = [{"api_key_hash": hb}]
        rows = compute_channel_rows(growth, usage, conversions)
        assert {r["ref_code"]: r["conversions"] for r in rows} == {
            "twitter": 1, "hn": 1,
        }

    def test_duplicate_growth_events_dont_inflate_unique_users(self):
        hb = _h("b")
        growth = [{"ref_code": "x", "api_key_hash": hb}] * 5
        rows = compute_channel_rows(growth, [], [])
        assert len(rows) == 1
        assert rows[0]["unique_users"] == 1
        assert rows[0]["total_growth_events"] == 5

    def test_user_with_no_usage_yields_zero_requests(self):
        hb = _h("b")
        rows = compute_channel_rows(
            growth=[{"ref_code": "x", "api_key_hash": hb}],
            usage=[],
            conversions=[],
        )
        assert rows[0]["total_usage_requests"] == 0
        assert rows[0]["premium_usage_requests"] == 0
        assert rows[0]["avg_requests_per_user"] == 0.0

    def test_conversion_without_matching_ref_is_not_attributed(self):
        """A user who converted but never arrived via a ref should
        not appear in any channel's conversions count."""
        ha, hz = _h("a"), _h("z")
        growth = [{"ref_code": "x", "api_key_hash": ha}]
        conversions = [{"api_key_hash": hz}]
        rows = compute_channel_rows(growth, [], conversions)
        assert rows[0]["conversions"] == 0

    def test_rows_without_ref_or_hash_are_dropped(self):
        rows = compute_channel_rows(
            growth=[
                {"api_key_hash": _h("a")},          # missing ref_code
                {"ref_code": "x"},                  # missing hash
                {"ref_code": "", "api_key_hash": _h("b")},
                {"ref_code": "real", "api_key_hash": _h("c")},
            ],
            usage=[],
            conversions=[],
        )
        assert [r["ref_code"] for r in rows] == ["real"]

    def test_rows_are_sorted_by_users_desc_then_ref_code(self):
        ha, hb, hc, hd = _h("a"), _h("b"), _h("c"), _h("d")
        growth = [
            {"ref_code": "zeta", "api_key_hash": ha},
            {"ref_code": "alpha", "api_key_hash": hb},
            {"ref_code": "alpha", "api_key_hash": hc},
            {"ref_code": "beta", "api_key_hash": hd},
        ]
        rows = compute_channel_rows(growth, [], [])
        # alpha=2 users first, then beta=1 (alphabetical), zeta=1.
        assert [r["ref_code"] for r in rows] == ["alpha", "beta", "zeta"]


# ---------------------------------------------------------------------------
# Conversion-rate arithmetic
# ---------------------------------------------------------------------------


class TestConversionRate:
    def test_zero_users_gives_none(self):
        rows = compute_channel_rows([], [], [])
        assert rows == []

    def test_full_conversion_gives_one(self):
        hb = _h("b")
        rows = compute_channel_rows(
            growth=[{"ref_code": "x", "api_key_hash": hb}],
            usage=[],
            conversions=[{"api_key_hash": hb}],
        )
        assert rows[0]["conversion_rate"] == 1.0

    def test_rounding_is_stable(self):
        hs = [_h(str(i)) for i in range(3)]
        rows = compute_channel_rows(
            growth=[{"ref_code": "x", "api_key_hash": h} for h in hs],
            usage=[],
            conversions=[{"api_key_hash": hs[0]}],
        )
        # 1 / 3 rounded to 4 dp = 0.3333
        assert rows[0]["conversion_rate"] == 0.3333

    def test_conversion_rate_ignores_dup_conversions(self):
        hb = _h("b")
        rows = compute_channel_rows(
            growth=[{"ref_code": "x", "api_key_hash": hb}],
            usage=[],
            conversions=[
                {"api_key_hash": hb},
                {"api_key_hash": hb},  # duplicate row
            ],
        )
        # Still counts as 1 converted user → rate 1.0
        assert rows[0]["conversion_rate"] == 1.0
        assert rows[0]["conversions"] == 1


# ---------------------------------------------------------------------------
# Top-channel selection
# ---------------------------------------------------------------------------


class TestTopChannels:
    def _rows(self, *specs):
        """Build synthetic row dicts directly (bypass compute_channel_rows
        so we control every field)."""
        out = []
        for spec in specs:
            r = {
                "ref_code": spec["ref_code"],
                "unique_users": spec.get("unique_users", 0),
                "total_growth_events": spec.get("total_growth_events", 0),
                "total_usage_requests": spec.get("total_usage_requests", 0),
                "premium_usage_requests": spec.get("premium_usage_requests", 0),
                "conversions": spec.get("conversions", 0),
                "conversion_rate": spec.get("conversion_rate"),
                "avg_requests_per_user": spec.get("avg_requests_per_user"),
            }
            out.append(r)
        return out

    def test_empty_rows_yield_all_none(self):
        top = compute_top_channels([])
        assert top == {
            "by_users": None,
            "by_conversions": None,
            "by_conversion_rate": None,
            "by_premium_usage_requests": None,
        }

    def test_basic_picks(self):
        rows = self._rows(
            {"ref_code": "a", "unique_users": 10, "conversions": 1,
             "conversion_rate": 0.10, "premium_usage_requests": 50},
            {"ref_code": "b", "unique_users": 3, "conversions": 2,
             "conversion_rate": 0.666, "premium_usage_requests": 200},
        )
        top = compute_top_channels(rows)
        assert top["by_users"] == {"ref_code": "a", "value": 10}
        assert top["by_conversions"] == {"ref_code": "b", "value": 2}
        assert top["by_conversion_rate"] == {"ref_code": "b", "value": 0.666}
        assert top["by_premium_usage_requests"] == {
            "ref_code": "b", "value": 200,
        }

    def test_rate_ranking_requires_min_support(self):
        """A 1-user / 1-conversion channel (rate 1.0) must lose to a
        many-user / higher-support real channel under the default
        ``MIN_SUPPORT_FOR_RATE_RANKING`` cutoff."""
        rows = self._rows(
            {"ref_code": "tiny", "unique_users": 1, "conversions": 1,
             "conversion_rate": 1.0},
            {"ref_code": "real", "unique_users": 10, "conversions": 3,
             "conversion_rate": 0.30},
        )
        top = compute_top_channels(rows)
        assert top["by_conversion_rate"] == {
            "ref_code": "real", "value": 0.30,
        }
        assert MIN_SUPPORT_FOR_RATE_RANKING >= 2

    def test_rate_ranking_with_lowered_threshold(self):
        rows = self._rows(
            {"ref_code": "tiny", "unique_users": 1, "conversions": 1,
             "conversion_rate": 1.0},
            {"ref_code": "real", "unique_users": 10, "conversions": 3,
             "conversion_rate": 0.30},
        )
        top = compute_top_channels(rows, min_users_for_rate=1)
        assert top["by_conversion_rate"] == {
            "ref_code": "tiny", "value": 1.0,
        }

    def test_ties_broken_alphabetically(self):
        rows = self._rows(
            {"ref_code": "zeta", "unique_users": 5, "conversions": 5,
             "conversion_rate": 1.0, "premium_usage_requests": 100},
            {"ref_code": "alpha", "unique_users": 5, "conversions": 5,
             "conversion_rate": 1.0, "premium_usage_requests": 100},
        )
        top = compute_top_channels(rows)
        for key in (
            "by_users", "by_conversions", "by_conversion_rate",
            "by_premium_usage_requests",
        ):
            assert top[key]["ref_code"] == "alpha", (
                f"{key} should have tiebroken to alpha, got {top[key]}"
            )


# ---------------------------------------------------------------------------
# Report assembly + determinism
# ---------------------------------------------------------------------------


class TestBuildChannelReport:
    def test_empty_inputs_yield_valid_report(self, tmp_path: Path):
        r = build_channel_report(
            tmp_path / "g.jsonl",
            tmp_path / "u.jsonl",
            tmp_path / "c.jsonl",
            report_date="2026-04-24",
        )
        assert r["report_type"] == REPORT_TYPE
        assert r["report_date"] == "2026-04-24"
        assert r["channels"] == []
        assert r["totals"]["channels"] == 0
        assert r["totals"]["unique_users"] == 0
        assert all(v is None for v in r["top_channels"].values())
        for name in ("growth_log", "usage_log", "conversion_log"):
            meta = r["sources"][name]
            assert meta["exists"] is False
            assert meta["rows"] == 0

    def test_report_is_deterministic_for_same_inputs(self, paths):
        ha, hb = _h("a"), _h("b")
        _write_jsonl(paths["growth"], [
            {"ref_code": "x", "api_key_hash": ha},
            {"ref_code": "y", "api_key_hash": hb},
        ])
        _write_jsonl(paths["usage"], [
            {"key_hash": ha, "tier": "free"},
            {"key_hash": hb, "tier": "premium"},
        ])
        _write_jsonl(paths["conv"], [
            {"api_key_hash": hb},
        ])
        a = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
            report_date="2026-04-24",
        )
        b = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
            report_date="2026-04-24",
        )
        assert a == b
        assert format_json(a) == format_json(b)
        assert format_text(a) == format_text(b)

    def test_totals_match_channels(self, paths):
        hs = [_h(str(i)) for i in range(5)]
        _write_jsonl(paths["growth"], [
            {"ref_code": "a", "api_key_hash": hs[0]},
            {"ref_code": "a", "api_key_hash": hs[1]},
            {"ref_code": "b", "api_key_hash": hs[2]},
            {"ref_code": "b", "api_key_hash": hs[3]},
            {"ref_code": "b", "api_key_hash": hs[4]},
        ])
        _write_jsonl(paths["usage"], [
            {"key_hash": hs[0], "tier": "free"},
            {"key_hash": hs[1], "tier": "premium"},
            {"key_hash": hs[2], "tier": "premium"},
        ])
        _write_jsonl(paths["conv"], [{"api_key_hash": hs[2]}])

        r = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
            report_date="2026-04-24",
        )
        # unique_users across growth events = 5 (each hs[i] once).
        assert r["totals"]["unique_users"] == 5
        assert r["totals"]["growth_events"] == sum(
            c["total_growth_events"] for c in r["channels"]
        )
        assert r["totals"]["attributed_usage_requests"] == sum(
            c["total_usage_requests"] for c in r["channels"]
        )
        assert r["totals"]["attributed_conversions"] == sum(
            c["conversions"] for c in r["channels"]
        )


# ---------------------------------------------------------------------------
# PII / leak guard
# ---------------------------------------------------------------------------


class TestPrivacyAndNoLeak:
    def test_raw_keys_and_pii_do_not_leak_into_report(self, paths):
        """
        Plant unique markers in every optional input field (paths,
        request_ids, IPs, emails, card-like strings). The report
        must reference only the opaque hash + ref_code — never any
        of these raw strings.
        """
        ha, hb = _h("a"), _h("b")
        _write_jsonl(paths["growth"], [
            {
                "timestamp": "2026-04-20T00:00:00Z",
                "api_key_hash": ha,
                "ref_code": "twitter",
                # Planted — should NEVER appear in the report.
                "path": "/UNIQUE_PATH_LEAK_A",
                "request_id": "UNIQUE_REQID_LEAK_A",
            },
            {
                "timestamp": "2026-04-20T00:01:00Z",
                "api_key_hash": hb,
                "ref_code": "twitter",
                "path": "/UNIQUE_PATH_LEAK_B",
                "request_id": "UNIQUE_REQID_LEAK_B",
            },
        ])
        _write_jsonl(paths["usage"], [
            {
                "key_hash": ha,
                "tier": "free",
                "path": "/UNIQUE_USAGE_PATH_LEAK_A",
                "request_id": "UNIQUE_USAGE_REQID_LEAK_A",
                "ip": "10.0.0.1",               # IP-looking extra
                "email": "attacker@example.org",  # email-looking extra
            },
            {
                "key_hash": hb,
                "tier": "premium",
                "path": "/UNIQUE_USAGE_PATH_LEAK_B",
                "request_id": "UNIQUE_USAGE_REQID_LEAK_B",
            },
        ])
        _write_jsonl(paths["conv"], [
            {
                "api_key_hash": hb,
                "price_id": "UNIQUE_PRICE_ID_LEAK",
                "source": "UNIQUE_CONV_SOURCE_LEAK",
                "card_last4": "4242",
            },
        ])
        r = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
            report_date="2026-04-24",
        )
        text = format_text(r)
        js = format_json(r)
        for marker in (
            "UNIQUE_PATH_LEAK_A",
            "UNIQUE_PATH_LEAK_B",
            "UNIQUE_REQID_LEAK_A",
            "UNIQUE_REQID_LEAK_B",
            "UNIQUE_USAGE_PATH_LEAK_A",
            "UNIQUE_USAGE_PATH_LEAK_B",
            "UNIQUE_USAGE_REQID_LEAK_A",
            "UNIQUE_USAGE_REQID_LEAK_B",
            "UNIQUE_PRICE_ID_LEAK",
            "UNIQUE_CONV_SOURCE_LEAK",
            "10.0.0.1",
            "attacker@example.org",
            "4242",
        ):
            assert marker not in text, f"text report leaked: {marker}"
            assert marker not in js, f"json report leaked: {marker}"
        # Positive assertion: the opaque hashes and ref_code ARE in the JSON
        # sources block (path/exists/rows), but only ref_code and counts are
        # in `channels`. Confirm raw API keys never appear.
        # (We don't have the raw key on hand — the fixture only ever wrote
        # hashes — so this is already guaranteed by construction.)
        assert "twitter" in text  # ref_code is public metadata


# ---------------------------------------------------------------------------
# JSON structure stability
# ---------------------------------------------------------------------------


class TestJsonStructure:
    def test_json_has_documented_top_level_keys(self, paths):
        _write_jsonl(paths["growth"], [
            {"ref_code": "x", "api_key_hash": _h("a")},
        ])
        _write_jsonl(paths["usage"], [])
        _write_jsonl(paths["conv"], [])
        r = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
            report_date="2026-04-24",
        )
        parsed = json.loads(format_json(r))
        assert set(parsed.keys()) == {
            "report_type", "report_date", "sources", "totals",
            "min_users_for_rate_ranking", "channels", "top_channels",
        }
        assert parsed["report_type"] == "channel_performance"
        assert set(parsed["sources"].keys()) == {
            "growth_log", "usage_log", "conversion_log",
        }
        assert set(parsed["top_channels"].keys()) == {
            "by_users", "by_conversions", "by_conversion_rate",
            "by_premium_usage_requests",
        }

    def test_each_channel_row_has_documented_fields(self, paths):
        _write_jsonl(paths["growth"], [
            {"ref_code": "x", "api_key_hash": _h("a")},
        ])
        _write_jsonl(paths["usage"], [{"key_hash": _h("a"), "tier": "free"}])
        _write_jsonl(paths["conv"], [])
        r = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
        )
        (row,) = r["channels"]
        assert set(row.keys()) == {
            "ref_code",
            "unique_users",
            "total_growth_events",
            "total_usage_requests",
            "premium_usage_requests",
            "conversions",
            "conversion_rate",
            "avg_requests_per_user",
        }


# ---------------------------------------------------------------------------
# Write-reports + CLI
# ---------------------------------------------------------------------------


class TestWriteReports:
    def test_writes_txt_and_json(self, paths):
        paths["reports"].mkdir(parents=True, exist_ok=True)
        _write_jsonl(paths["growth"], [
            {"ref_code": "x", "api_key_hash": _h("a")},
        ])
        _write_jsonl(paths["usage"], [])
        _write_jsonl(paths["conv"], [])
        r = build_channel_report(
            paths["growth"], paths["usage"], paths["conv"],
            report_date="2026-04-24",
        )
        txt_path, json_path = write_reports(r, reports_dir=paths["reports"])
        assert txt_path == paths["reports"] / "channel_report_2026-04-24.txt"
        assert json_path == paths["reports"] / "channel_report_2026-04-24.json"
        assert txt_path.exists()
        assert json_path.exists()
        assert "CHANNEL PERFORMANCE REPORT" in txt_path.read_text()
        re_parsed = json.loads(json_path.read_text())
        assert re_parsed["report_type"] == "channel_performance"


class TestCli:
    def test_cli_writes_both_files_and_exits_zero(
        self, paths, tmp_path: Path
    ):
        _write_jsonl(paths["growth"], [
            {"ref_code": "x", "api_key_hash": _h("a")},
        ])
        _write_jsonl(paths["usage"], [])
        _write_jsonl(paths["conv"], [])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.channel_report",
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--conversions", str(paths["conv"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (
            paths["reports"] / "channel_report_2026-04-24.txt"
        ).exists()
        assert (
            paths["reports"] / "channel_report_2026-04-24.json"
        ).exists()
        assert "channel performance report written" in result.stdout.lower()

    def test_cli_json_only_prints_json_no_files(self, paths):
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        _write_jsonl(paths["conv"], [])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.channel_report",
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--conversions", str(paths["conv"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
                "--json-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["report_type"] == "channel_performance"
        # No files written when --json-only.
        assert not paths["reports"].exists() or not any(
            paths["reports"].glob("channel_report_*")
        )

    def test_cli_text_only_prints_text_no_files(self, paths):
        _write_jsonl(paths["growth"], [])
        _write_jsonl(paths["usage"], [])
        _write_jsonl(paths["conv"], [])
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.channel_report",
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--conversions", str(paths["conv"]),
                "--reports-dir", str(paths["reports"]),
                "--date", "2026-04-24",
                "--text-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "CHANNEL PERFORMANCE REPORT" in result.stdout

    def test_cli_both_flags_rejected(self, paths):
        result = subprocess.run(
            [
                sys.executable, "-m", "trading_bot.analysis.channel_report",
                "--growth", str(paths["growth"]),
                "--usage", str(paths["usage"]),
                "--conversions", str(paths["conv"]),
                "--json-only", "--text-only",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Boundary: the analysis module must not import Core or the API
# ---------------------------------------------------------------------------


class TestNoCoreOrApiImports:
    def test_channel_report_module_is_self_contained(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "analysis" / "channel_report.py"
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
                f"channel_report.py imports restricted surface: {forbidden!r}"
            )
