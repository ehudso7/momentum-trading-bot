"""
Phase 9.2 tests — pure-helper unit coverage for the daily hook.

Integration with /reports/latest and /dashboard is covered in
tests/test_api_server.py::TestPhase92 — this file pins the
function-level contract: schema, source-preference order, free
truncation, and the deterministic mapping from report inputs to
hook output.
"""

from __future__ import annotations

import pytest

from trading_bot.api.daily_hook import (
    CHANGE_DOWN,
    CHANGE_FLAT,
    CHANGE_UP,
    DEFAULT_CTA,
    DRIVER_TOTALS_FALLBACK,
    DRIVER_TREND_INSIGHT,
    VALID_CHANGES,
    build_daily_hook,
    truncate_for_free,
)
from trading_bot.api.insights import build_insights


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


class TestBuildDailyHookEmpty:
    def test_no_report_returns_none(self):
        assert build_daily_hook(None, {"report_date": "2026-04-24"}, []) is None

    def test_no_prev_returns_none(self):
        assert build_daily_hook({"report_date": "2026-04-25"}, None, []) is None

    def test_both_missing_returns_none(self):
        assert build_daily_hook(None, None, []) is None

    def test_non_dict_inputs_return_none(self):
        assert build_daily_hook("x", {}, []) is None  # type: ignore[arg-type]
        assert build_daily_hook({}, "x", []) is None  # type: ignore[arg-type]

    def test_no_signal_data_at_all_returns_none(self):
        # Both reports exist but neither has totals.buy_rows AND
        # no insights provided → no source can fire.
        out = build_daily_hook(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-24"},
            [],
        )
        assert out is None


# ---------------------------------------------------------------------------
# Source 1 — trend insight preferred
# ---------------------------------------------------------------------------


class TestBuildDailyHookFromTrendInsight:
    def test_uses_trend_insight_when_present(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        insights = build_insights(curr, prev)
        out = build_daily_hook(curr, prev, insights)
        assert out is not None
        assert out["driver"] == DRIVER_TREND_INSIGHT
        assert out["change"] == CHANGE_UP
        assert out["magnitude"] == 10
        assert out["since"] == "2026-04-24"
        assert 0.0 <= out["confidence"] <= 1.0
        assert out["cta"] == DEFAULT_CTA

    def test_negative_delta_marks_down(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 30}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 10}}
        out = build_daily_hook(curr, prev, build_insights(curr, prev))
        assert out["change"] == CHANGE_DOWN
        assert out["magnitude"] == 20
        # Stable CTA + headline shape.
        assert out["headline"] == "Buys down 20 vs prior day"

    def test_flat_delta_marks_flat(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 12}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 12}}
        out = build_daily_hook(curr, prev, build_insights(curr, prev))
        assert out["change"] == CHANGE_FLAT
        assert out["magnitude"] == 0
        assert out["headline"] == "Buys unchanged vs prior day"


class TestBuildDailyHookFromTrendInsightMalformed:
    def test_malformed_trend_insight_falls_through_to_totals(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        # Inject a malformed trend insight; the helper should fall
        # through to the totals fallback, NOT skip the rule entirely.
        bad_insight = {"id": "trend.buy_delta", "evidence": "not a dict"}
        out = build_daily_hook(curr, prev, [bad_insight])
        assert out is not None
        assert out["driver"] == DRIVER_TOTALS_FALLBACK
        assert out["change"] == CHANGE_UP
        assert out["magnitude"] == 10

    def test_trend_insight_with_invalid_direction_falls_through(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        bad_insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "sideways"},
        }
        out = build_daily_hook(curr, prev, [bad_insight])
        assert out is not None
        assert out["driver"] == DRIVER_TOTALS_FALLBACK


# ---------------------------------------------------------------------------
# Source 2 — totals fallback
# ---------------------------------------------------------------------------


class TestBuildDailyHookFromTotalsFallback:
    def test_no_insights_uses_totals_fallback(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        out = build_daily_hook(curr, prev, insights=None)
        assert out is not None
        assert out["driver"] == DRIVER_TOTALS_FALLBACK
        assert out["change"] == CHANGE_UP
        assert out["magnitude"] == 10
        # Fallback confidence is fixed at 0.4.
        assert out["confidence"] == pytest.approx(0.4)

    def test_empty_insights_list_uses_totals_fallback(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        out = build_daily_hook(curr, prev, [])
        assert out is not None
        assert out["driver"] == DRIVER_TOTALS_FALLBACK

    def test_other_insights_dont_block_fallback(self):
        """Insights that aren't trend.buy_delta are ignored — the
        helper falls through to the totals source."""
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        unrelated = [{"id": "promotion.readiness", "evidence": {"ready": True}}]
        out = build_daily_hook(curr, prev, unrelated)
        assert out is not None
        assert out["driver"] == DRIVER_TOTALS_FALLBACK

    def test_missing_curr_totals_returns_none(self):
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25"}
        assert build_daily_hook(curr, prev, []) is None

    def test_missing_prev_totals_returns_none(self):
        prev = {"report_date": "2026-04-24"}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        assert build_daily_hook(curr, prev, []) is None

    def test_missing_buy_rows_returns_none(self):
        prev = {"report_date": "2026-04-24", "totals": {"alpha_rows": 100}}
        curr = {"report_date": "2026-04-25", "totals": {"alpha_rows": 100}}
        assert build_daily_hook(curr, prev, []) is None


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


class TestSchemaInvariants:
    @pytest.fixture
    def hook(self) -> dict:
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        return build_daily_hook(curr, prev, build_insights(curr, prev))

    def test_documented_keys_present(self, hook):
        assert set(hook.keys()) == {
            "headline", "change", "magnitude", "confidence",
            "since", "driver", "cta",
        }

    def test_change_in_documented_set(self, hook):
        assert hook["change"] in VALID_CHANGES

    def test_confidence_in_unit_interval(self, hook):
        assert 0.0 <= hook["confidence"] <= 1.0

    def test_magnitude_is_non_negative(self, hook):
        assert hook["magnitude"] >= 0

    def test_since_matches_prev_report_date(self, hook):
        assert hook["since"] == "2026-04-24"


# ---------------------------------------------------------------------------
# truncate_for_free
# ---------------------------------------------------------------------------


class TestTruncateForFree:
    @pytest.fixture
    def full_hook(self) -> dict:
        prev = {"report_date": "2026-04-24", "totals": {"buy_rows": 20}}
        curr = {"report_date": "2026-04-25", "totals": {"buy_rows": 30}}
        return build_daily_hook(curr, prev, build_insights(curr, prev))

    def test_drops_confidence_and_driver(self, full_hook):
        free = truncate_for_free(full_hook)
        assert set(free.keys()) == {
            "headline", "change", "magnitude", "since", "cta",
        }
        assert "confidence" not in free
        assert "driver" not in free

    def test_passes_through_user_facing_fields(self, full_hook):
        free = truncate_for_free(full_hook)
        assert free["headline"] == full_hook["headline"]
        assert free["change"] == full_hook["change"]
        assert free["magnitude"] == full_hook["magnitude"]
        assert free["since"] == full_hook["since"]
        assert free["cta"] == full_hook["cta"]

    def test_none_input_returns_none(self):
        assert truncate_for_free(None) is None

    def test_non_dict_input_returns_none(self):
        assert truncate_for_free("x") is None  # type: ignore[arg-type]
        assert truncate_for_free(42) is None  # type: ignore[arg-type]

    def test_input_not_mutated(self, full_hook):
        before = dict(full_hook)
        truncate_for_free(full_hook)
        assert full_hook == before


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_module_imports_only_stdlib_typing(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "daily_hook.py"
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
            "from trading_bot.api.insights",
        ):
            assert forbidden not in src, (
                f"daily_hook.py imports restricted surface: {forbidden!r}"
            )
