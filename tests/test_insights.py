"""
Phase 9.1 tests — pure-helper unit coverage for the insight builder.

The integration with /reports/latest and /dashboard is covered in
tests/test_api_server.py::TestPhase91 — this file pins the
function-level contract: schema, ordering, dedup, free truncation,
and the deterministic mapping from report inputs to insight
outputs.
"""

from __future__ import annotations

import pytest

from trading_bot.api.insights import (
    FREE_INSIGHT_LIMIT,
    INSIGHT_READINESS,
    INSIGHT_REGIME_DOMINANT,
    INSIGHT_TREND_BUY_DELTA,
    KNOWN_INSIGHT_IDS,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    VALID_SEVERITIES,
    build_insights,
    truncate_for_free,
)


# ---------------------------------------------------------------------------
# build_insights — empty + degenerate inputs
# ---------------------------------------------------------------------------


class TestBuildInsightsEmpty:
    def test_no_report_returns_empty_list(self):
        assert build_insights(None) == []
        assert build_insights({}) == []
        assert build_insights("not a dict") == []  # type: ignore[arg-type]

    def test_report_with_no_known_fields_returns_empty(self):
        # Sanitised report carrying nothing the rules consume.
        assert build_insights({"report_type": "x"}) == []

    def test_prev_with_missing_totals_skips_trend(self):
        report = {"totals": {"buy_rows": 30}}
        prev = {"report_type": "x"}  # no totals
        out = build_insights(report, prev)
        # Trend skipped. Other rules are also skipped here (no
        # promotion_readiness, no regime_stats).
        assert out == []


# ---------------------------------------------------------------------------
# Trend rule (a)
# ---------------------------------------------------------------------------


class TestTrendInsight:
    def test_no_prev_means_no_trend(self):
        out = build_insights({"totals": {"buy_rows": 5}})
        assert all(e["id"] != INSIGHT_TREND_BUY_DELTA for e in out)

    def test_positive_delta_marked_info(self):
        out = build_insights(
            {"totals": {"buy_rows": 30}},
            {"totals": {"buy_rows": 20}},
        )
        trend = [e for e in out if e["id"] == INSIGHT_TREND_BUY_DELTA][0]
        assert trend["severity"] == SEVERITY_INFO
        assert trend["evidence"]["delta"] == 10
        assert trend["evidence"]["direction"] == "up"
        assert trend["evidence"]["curr_buy_rows"] == 30
        assert trend["evidence"]["prev_buy_rows"] == 20
        assert trend["evidence"]["percent_change"] == 50.0

    def test_negative_delta_within_half_is_info(self):
        out = build_insights(
            {"totals": {"buy_rows": 18}},
            {"totals": {"buy_rows": 20}},
        )
        trend = [e for e in out if e["id"] == INSIGHT_TREND_BUY_DELTA][0]
        assert trend["severity"] == SEVERITY_INFO
        assert trend["evidence"]["direction"] == "down"
        assert trend["evidence"]["delta"] == -2

    def test_large_negative_delta_marked_warn(self):
        # delta = -15 = 75% drop → warn threshold = max(1, prev//2) = 10
        out = build_insights(
            {"totals": {"buy_rows": 5}},
            {"totals": {"buy_rows": 20}},
        )
        trend = [e for e in out if e["id"] == INSIGHT_TREND_BUY_DELTA][0]
        assert trend["severity"] == SEVERITY_WARN
        assert trend["evidence"]["direction"] == "down"

    def test_flat_delta_marked_info(self):
        out = build_insights(
            {"totals": {"buy_rows": 12}},
            {"totals": {"buy_rows": 12}},
        )
        trend = [e for e in out if e["id"] == INSIGHT_TREND_BUY_DELTA][0]
        assert trend["evidence"]["direction"] == "flat"
        assert trend["evidence"]["delta"] == 0

    def test_zero_prev_does_not_divide_by_zero(self):
        out = build_insights(
            {"totals": {"buy_rows": 5}},
            {"totals": {"buy_rows": 0}},
        )
        trend = [e for e in out if e["id"] == INSIGHT_TREND_BUY_DELTA][0]
        assert trend["evidence"]["percent_change"] is None
        # Confidence collapses to a fixed mid-point when there's no
        # baseline to scale against.
        assert 0.0 < trend["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# Readiness rule (b)
# ---------------------------------------------------------------------------


class TestReadinessInsight:
    def test_ready_marks_info_with_full_confidence(self):
        out = build_insights(
            {"promotion_readiness": {
                "ready": True, "consecutive_passing_days": 25,
            }},
        )
        r = [e for e in out if e["id"] == INSIGHT_READINESS][0]
        assert r["severity"] == SEVERITY_INFO
        assert r["confidence"] == 1.0
        assert r["evidence"] == {
            "ready": True,
            "consecutive_passing_days": 25,
        }

    def test_streak_in_progress_marks_info(self):
        out = build_insights(
            {"promotion_readiness": {
                "ready": False, "consecutive_passing_days": 5,
            }},
        )
        r = [e for e in out if e["id"] == INSIGHT_READINESS][0]
        assert r["severity"] == SEVERITY_INFO
        assert 0.4 <= r["confidence"] <= 1.0
        assert r["evidence"]["ready"] is False
        assert r["evidence"]["consecutive_passing_days"] == 5

    def test_blocked_marks_warn(self):
        out = build_insights(
            {"promotion_readiness": {
                "ready": False, "consecutive_passing_days": 0,
            }},
        )
        r = [e for e in out if e["id"] == INSIGHT_READINESS][0]
        assert r["severity"] == SEVERITY_WARN
        assert r["evidence"]["consecutive_passing_days"] == 0

    def test_missing_readiness_field_skips_rule(self):
        out = build_insights({"report_type": "x"})
        assert all(e["id"] != INSIGHT_READINESS for e in out)


# ---------------------------------------------------------------------------
# Regime rule (c)
# ---------------------------------------------------------------------------


class TestRegimeInsight:
    def test_dominant_regime_picked_correctly(self):
        out = build_insights(
            {"regime_stats": {
                "trending": {"hits": 12},
                "choppy": {"hits": 3},
                "gappy": {"hits": 1},
            }},
        )
        r = [e for e in out if e["id"] == INSIGHT_REGIME_DOMINANT][0]
        assert r["evidence"]["regime"] == "trending"
        assert r["evidence"]["hits"] == 12
        assert r["evidence"]["runner_up"] == {
            "regime": "choppy", "hits": 3,
        }
        assert r["evidence"]["total_hits"] == 16
        assert r["evidence"]["share"] == round(12 / 16, 4)

    def test_flat_int_regime_stats_supported(self):
        # Some report shapes use {"trending": 7} not {"trending": {"hits": 7}}.
        out = build_insights({"regime_stats": {"trending": 7, "choppy": 2}})
        r = [e for e in out if e["id"] == INSIGHT_REGIME_DOMINANT][0]
        assert r["evidence"]["regime"] == "trending"
        assert r["evidence"]["hits"] == 7

    def test_zero_hits_drops_rule(self):
        out = build_insights(
            {"regime_stats": {
                "trending": {"hits": 0},
                "choppy": {"hits": 0},
            }},
        )
        assert all(e["id"] != INSIGHT_REGIME_DOMINANT for e in out)

    def test_single_regime_no_runner_up(self):
        out = build_insights({"regime_stats": {"trending": {"hits": 4}}})
        r = [e for e in out if e["id"] == INSIGHT_REGIME_DOMINANT][0]
        assert r["evidence"]["runner_up"] is None
        assert r["evidence"]["share"] == 1.0
        assert r["confidence"] == 1.0

    def test_tie_breaks_by_alphabetical_regime_name(self):
        out = build_insights(
            {"regime_stats": {
                "zebra": {"hits": 5},
                "alpha": {"hits": 5},
            }},
        )
        r = [e for e in out if e["id"] == INSIGHT_REGIME_DOMINANT][0]
        # "alpha" wins on ties because the sort is (-hits, name).
        assert r["evidence"]["regime"] == "alpha"


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


class TestSchemaInvariants:
    @pytest.fixture
    def full_insights(self) -> list[dict]:
        return build_insights(
            {
                "totals": {"buy_rows": 30},
                "promotion_readiness": {
                    "ready": False, "consecutive_passing_days": 5,
                },
                "regime_stats": {"trending": {"hits": 12}},
            },
            {"totals": {"buy_rows": 20}},
        )

    def test_each_insight_has_documented_keys(self, full_insights):
        required = {
            "id", "title", "summary", "confidence",
            "severity", "evidence", "action",
        }
        assert len(full_insights) == 3
        for entry in full_insights:
            assert set(entry.keys()) == required

    def test_severity_in_documented_set(self, full_insights):
        for entry in full_insights:
            assert entry["severity"] in VALID_SEVERITIES

    def test_confidence_in_unit_interval(self, full_insights):
        for entry in full_insights:
            assert 0.0 <= entry["confidence"] <= 1.0

    def test_id_in_known_set(self, full_insights):
        for entry in full_insights:
            assert entry["id"] in KNOWN_INSIGHT_IDS

    def test_known_insight_ids_constant_is_size_3(self):
        assert KNOWN_INSIGHT_IDS == {
            INSIGHT_TREND_BUY_DELTA,
            INSIGHT_READINESS,
            INSIGHT_REGIME_DOMINANT,
        }

    def test_ordering_is_deterministic(self, full_insights):
        ids = [e["id"] for e in full_insights]
        assert ids == [
            INSIGHT_TREND_BUY_DELTA,
            INSIGHT_READINESS,
            INSIGHT_REGIME_DOMINANT,
        ]


# ---------------------------------------------------------------------------
# truncate_for_free
# ---------------------------------------------------------------------------


class TestTruncateForFree:
    @pytest.fixture
    def all_three(self) -> list[dict]:
        return build_insights(
            {
                "totals": {"buy_rows": 30},
                "promotion_readiness": {
                    "ready": False, "consecutive_passing_days": 5,
                },
                "regime_stats": {"trending": {"hits": 12}},
            },
            {"totals": {"buy_rows": 20}},
        )

    def test_default_limit_is_two(self):
        assert FREE_INSIGHT_LIMIT == 2

    def test_truncates_to_max_count(self, all_three):
        kept = truncate_for_free(all_three)
        assert len(kept) == 2
        # The first two insights survive in declared order.
        assert [e["id"] for e in kept] == [
            INSIGHT_TREND_BUY_DELTA, INSIGHT_READINESS,
        ]

    def test_evidence_projected_through_allowlist(self, all_three):
        kept = truncate_for_free(all_three)
        trend = [e for e in kept if e["id"] == INSIGHT_TREND_BUY_DELTA][0]
        # Free callers see only delta + direction, not curr/prev/percent.
        assert set(trend["evidence"].keys()) == {"delta", "direction"}
        readiness = [e for e in kept if e["id"] == INSIGHT_READINESS][0]
        assert set(readiness["evidence"].keys()) == {"ready"}

    def test_explicit_max_count_zero_returns_empty(self, all_three):
        assert truncate_for_free(all_three, max_count=0) == []

    def test_negative_max_count_returns_empty(self, all_three):
        assert truncate_for_free(all_three, max_count=-1) == []

    def test_unknown_id_dropped(self, all_three):
        # Insert a future / unrecognised insight — should be dropped
        # from the free output (fail-closed for new rules without a
        # documented allow-list).
        synthetic = {
            "id": "future.rule",
            "title": "x", "summary": "y", "confidence": 0.5,
            "severity": "info", "evidence": {"a": 1}, "action": "z",
        }
        out = truncate_for_free([synthetic] + all_three)
        assert all(e["id"] != "future.rule" for e in out)
        # We still get 2 from the other 3 documented insights.
        assert len(out) == 2

    def test_input_not_mutated(self, all_three):
        before = [dict(e) for e in all_three]
        truncate_for_free(all_three)
        assert all_three == before

    def test_non_list_input_returns_empty(self):
        assert truncate_for_free("not a list") == []  # type: ignore[arg-type]
        assert truncate_for_free(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_module_imports_only_stdlib_typing(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "insights.py"
        ).read_text()
        for forbidden in (
            "import structlog",
            "import fastapi",
            "from fastapi",
            "from trading_bot.core",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
            "from trading_bot.api.server",
            "from trading_bot.api.billing",
            "from trading_bot.api.key_store",
        ):
            assert forbidden not in src, (
                f"insights.py imports restricted surface: {forbidden!r}"
            )
