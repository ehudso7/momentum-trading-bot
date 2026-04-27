"""
Phase 9.3 tests — pure-helper coverage for the stickiness loop
(streak + missed-day nudge).

Integration with /reports/latest and /dashboard is covered in
tests/test_api_server.py::TestPhase93 — this file pins the
function-level contract: schema, fail-soft inputs, milestone
ladder, and the deterministic mapping from report inputs to
streak / nudge outputs.
"""

from __future__ import annotations

import pytest

from trading_bot.api.stickiness import (
    MILESTONE_DAYS,
    NUDGE_CTA,
    NUDGE_MISSED_DAY,
    VALID_NUDGE_KINDS,
    build_nudge,
    build_streak,
    truncate_nudge_for_free,
    truncate_streak_for_free,
)


# ---------------------------------------------------------------------------
# build_streak
# ---------------------------------------------------------------------------


class TestBuildStreakEmpty:
    def test_no_report_returns_none(self):
        assert build_streak(None) is None
        assert build_streak("not a dict") is None  # type: ignore[arg-type]
        assert build_streak({}) is None

    def test_no_promotion_readiness_returns_none(self):
        assert build_streak({"report_type": "x"}) is None

    def test_promotion_readiness_not_a_dict_returns_none(self):
        assert build_streak({"promotion_readiness": "ready"}) is None

    def test_zero_days_returns_none(self):
        assert build_streak(
            {"promotion_readiness": {"consecutive_passing_days": 0}},
        ) is None

    def test_negative_days_returns_none(self):
        assert build_streak(
            {"promotion_readiness": {"consecutive_passing_days": -3}},
        ) is None

    def test_unparseable_days_returns_none(self):
        assert build_streak(
            {"promotion_readiness": {"consecutive_passing_days": "abc"}},
        ) is None


class TestBuildStreakSchema:
    def test_one_day_renders_singular_label(self):
        out = build_streak(
            {"promotion_readiness": {"consecutive_passing_days": 1}},
        )
        assert out is not None
        assert out["days"] == 1
        assert out["label"] == "1-day passing streak"
        assert out["milestone"] is False  # 1 is not in MILESTONE_DAYS
        assert out["next_milestone"] == 3  # first ladder rung > 1

    def test_milestone_day_marks_milestone_true(self):
        for m in MILESTONE_DAYS:
            out = build_streak(
                {"promotion_readiness": {"consecutive_passing_days": m}},
            )
            assert out["milestone"] is True
            assert out["days"] == m

    def test_non_milestone_day_marks_milestone_false(self):
        out = build_streak(
            {"promotion_readiness": {"consecutive_passing_days": 4}},
        )
        assert out["milestone"] is False
        assert out["next_milestone"] == 5

    def test_next_milestone_for_high_days_uses_30_day_increments(self):
        """Past the 365-day rung the helper extends by 30-day rungs
        so the streak always has a target."""
        out = build_streak(
            {"promotion_readiness": {"consecutive_passing_days": 400}},
        )
        # 365 + 30 * ceil((400 - 365) / 30) = 365 + 60 = 425; but
        # the helper uses (n // 30) + 1 → (35 // 30) + 1 = 2 → 365 + 60.
        assert out["next_milestone"] == 365 + 60
        assert out["next_milestone"] > 400

    def test_documented_keys_present(self):
        out = build_streak(
            {"promotion_readiness": {"consecutive_passing_days": 5}},
        )
        assert set(out.keys()) == {
            "days", "label", "milestone", "next_milestone",
        }

    def test_prev_report_arg_does_not_affect_output(self):
        report = {"promotion_readiness": {"consecutive_passing_days": 7}}
        a = build_streak(report, None)
        b = build_streak(report, {"report_date": "2026-04-24"})
        assert a == b


# ---------------------------------------------------------------------------
# build_nudge
# ---------------------------------------------------------------------------


class TestBuildNudgeEmpty:
    def test_no_prev_returns_none(self):
        out = build_nudge({"report_date": "2026-04-25"}, None)
        assert out is None

    def test_no_curr_returns_none(self):
        out = build_nudge(None, {"report_date": "2026-04-24"})
        assert out is None

    def test_both_missing_returns_none(self):
        assert build_nudge(None, None) is None

    def test_unparseable_curr_date_returns_none(self):
        out = build_nudge(
            {"report_date": "not-a-date"},
            {"report_date": "2026-04-24"},
        )
        assert out is None

    def test_unparseable_prev_date_returns_none(self):
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "not-a-date"},
        )
        assert out is None

    def test_one_day_gap_returns_none(self):
        # Normal daily cadence — no nudge.
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-24"},
        )
        assert out is None

    def test_same_day_returns_none(self):
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-25"},
        )
        assert out is None

    def test_out_of_order_returns_none(self):
        # Prev date AFTER curr date — degenerate, no nudge.
        out = build_nudge(
            {"report_date": "2026-04-23"},
            {"report_date": "2026-04-25"},
        )
        assert out is None


class TestBuildNudgeSchema:
    def test_two_day_gap_reports_one_missed(self):
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-23"},
        )
        assert out is not None
        assert out["kind"] == NUDGE_MISSED_DAY
        assert out["days_missed"] == 1
        assert out["since"] == "2026-04-23"
        assert out["headline"] == "You missed 1 day of reports."
        assert out["cta"] == NUDGE_CTA

    def test_seven_day_gap_uses_plural(self):
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-18"},
        )
        assert out["days_missed"] == 6
        assert out["headline"] == "You missed 6 days of reports."

    def test_documented_keys_present(self):
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-22"},
        )
        assert set(out.keys()) == {
            "kind", "headline", "days_missed", "since", "cta",
        }

    def test_kind_in_documented_set(self):
        out = build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-22"},
        )
        assert out["kind"] in VALID_NUDGE_KINDS


# ---------------------------------------------------------------------------
# truncate_*_for_free
# ---------------------------------------------------------------------------


class TestTruncateStreakForFree:
    @pytest.fixture
    def streak(self) -> dict:
        return build_streak(
            {"promotion_readiness": {"consecutive_passing_days": 7}},
        )

    def test_drops_next_milestone(self, streak):
        free = truncate_streak_for_free(streak)
        assert "next_milestone" not in free
        assert set(free.keys()) == {"days", "label", "milestone"}

    def test_passes_through_user_facing_fields(self, streak):
        free = truncate_streak_for_free(streak)
        assert free["days"] == 7
        assert free["label"] == "7-day passing streak"
        assert free["milestone"] is True

    def test_none_passthrough(self):
        assert truncate_streak_for_free(None) is None

    def test_non_dict_passthrough(self):
        assert truncate_streak_for_free("x") is None  # type: ignore[arg-type]

    def test_input_not_mutated(self, streak):
        before = dict(streak)
        truncate_streak_for_free(streak)
        assert streak == before


class TestTruncateNudgeForFree:
    @pytest.fixture
    def nudge(self) -> dict:
        return build_nudge(
            {"report_date": "2026-04-25"},
            {"report_date": "2026-04-22"},
        )

    def test_passes_all_user_facing_fields(self, nudge):
        free = truncate_nudge_for_free(nudge)
        assert set(free.keys()) == {
            "kind", "headline", "days_missed", "since", "cta",
        }

    def test_none_passthrough(self):
        assert truncate_nudge_for_free(None) is None

    def test_non_dict_passthrough(self):
        assert truncate_nudge_for_free("x") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_module_imports_only_stdlib_typing(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "stickiness.py"
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
            "from trading_bot.api.daily_hook",
        ):
            assert forbidden not in src, (
                f"stickiness.py imports restricted surface: {forbidden!r}"
            )
