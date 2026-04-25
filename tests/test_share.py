"""
Phase 10.1 tests — pure-helper coverage for the shareability layer.

Integration with /reports/latest is covered in
tests/test_api_server.py::TestPhase101 — this file pins the
function-level contract: schema, premium-vs-free copy, URL
normalisation, and fail-soft behaviour when the public base URL
is unset.
"""

from __future__ import annotations

import pytest

from trading_bot.api.share import (
    DEFAULT_CTA,
    build_daily_hook_share,
    build_insight_share,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestShareSchema:
    @pytest.fixture
    def trend_insight(self) -> dict:
        return {
            "id": "trend.buy_delta",
            "evidence": {
                "delta": 10, "direction": "up", "percent_change": 50.0,
            },
        }

    def test_insight_share_has_documented_keys(self, trend_insight):
        out = build_insight_share(
            trend_insight, is_premium=False,
            base_url="https://example.com",
        )
        assert set(out.keys()) == {"text", "cta", "url"}

    def test_daily_hook_share_has_documented_keys(self):
        hook = {"change": "up", "magnitude": 10, "headline": "..."}
        out = build_daily_hook_share(
            hook, is_premium=False, base_url="https://example.com",
        )
        assert set(out.keys()) == {"text", "cta", "url"}

    def test_cta_is_stable_constant(self, trend_insight):
        out = build_insight_share(
            trend_insight, is_premium=False,
            base_url="https://example.com",
        )
        assert out["cta"] == DEFAULT_CTA == "Try it yourself"


# ---------------------------------------------------------------------------
# URL normalisation + fail-soft
# ---------------------------------------------------------------------------


class TestUrlHandling:
    @pytest.fixture
    def trend_insight(self) -> dict:
        return {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "up"},
        }

    def test_trailing_slash_stripped(self, trend_insight):
        out = build_insight_share(
            trend_insight, is_premium=False,
            base_url="https://example.com/",
        )
        assert out["url"] == "https://example.com"

    def test_double_trailing_slash_stripped(self, trend_insight):
        out = build_insight_share(
            trend_insight, is_premium=False,
            base_url="https://example.com//",
        )
        assert out["url"] == "https://example.com"

    def test_whitespace_around_url_stripped(self, trend_insight):
        out = build_insight_share(
            trend_insight, is_premium=False,
            base_url="  https://example.com/  ",
        )
        assert out["url"] == "https://example.com"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_url_returns_none(self, trend_insight, blank):
        out = build_insight_share(
            trend_insight, is_premium=False, base_url=blank,
        )
        assert out is None

    def test_blank_url_omits_daily_hook_share(self):
        hook = {"change": "up", "magnitude": 10}
        out = build_daily_hook_share(
            hook, is_premium=False, base_url="",
        )
        assert out is None


# ---------------------------------------------------------------------------
# Insight share copy
# ---------------------------------------------------------------------------


class TestInsightShareCopyTrend:
    BASE_URL = "https://example.com"

    def test_free_up_text(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "up", "percent_change": 50.0},
        }
        out = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        # Free copy is the plain version — no percent-change figure.
        assert out["text"] == (
            "Buy signals up 10 vs prior day on Momentum Trading Bot."
        )

    def test_premium_up_text_includes_percent(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "up", "percent_change": 50.0},
        }
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert "+50.0%" in out["text"]
        assert "Momentum Trading Bot" in out["text"]
        assert "up 10" in out["text"]

    def test_free_down_text(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": -5, "direction": "down", "percent_change": -25.0},
        }
        out = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        assert out["text"] == (
            "Buy signals down 5 vs prior day on Momentum Trading Bot."
        )

    def test_premium_down_text_includes_negative_percent(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": -5, "direction": "down", "percent_change": -25.0},
        }
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert "-25.0%" in out["text"]

    def test_flat_text_omits_magnitude(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 0, "direction": "flat"},
        }
        out = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        assert "unchanged" in out["text"]

    def test_premium_without_percent_change_omits_percent_part(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "up", "percent_change": None},
        }
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert "%" not in out["text"]
        assert "up 10" in out["text"]


class TestInsightShareCopyReadiness:
    BASE_URL = "https://example.com"

    def test_free_ready(self):
        insight = {
            "id": "promotion.readiness",
            "evidence": {"ready": True, "consecutive_passing_days": 25},
        }
        out = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        assert "GREEN" in out["text"]
        assert "25" in out["text"]

    def test_premium_ready(self):
        insight = {
            "id": "promotion.readiness",
            "evidence": {"ready": True, "consecutive_passing_days": 25},
        }
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert "READY" in out["text"]
        assert "25" in out["text"]

    def test_streak_in_progress_free(self):
        insight = {
            "id": "promotion.readiness",
            "evidence": {"ready": False, "consecutive_passing_days": 5},
        }
        out = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        assert "5-day passing streak" in out["text"]

    def test_blocked_premium(self):
        insight = {
            "id": "promotion.readiness",
            "evidence": {"ready": False, "consecutive_passing_days": 0},
        }
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert "BLOCKED" in out["text"]


class TestInsightShareCopyRegime:
    BASE_URL = "https://example.com"

    def test_free_regime_text(self):
        insight = {
            "id": "regime.dominant",
            "evidence": {"regime": "trending", "hits": 12, "share": 0.8},
        }
        out = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        assert "trending" in out["text"]
        # Free omits the share-percent context.
        assert "%" not in out["text"]

    def test_premium_regime_text_includes_share_percent(self):
        insight = {
            "id": "regime.dominant",
            "evidence": {"regime": "trending", "hits": 12, "share": 0.8},
        }
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert "trending" in out["text"]
        assert "12 hits" in out["text"]
        assert "80.0% share" in out["text"]

    def test_missing_regime_returns_unknown_text(self):
        insight = {"id": "regime.dominant", "evidence": {}}
        out = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        assert out is not None
        assert "unavailable" in out["text"]


# ---------------------------------------------------------------------------
# Insight share — unknown ids and degenerate inputs
# ---------------------------------------------------------------------------


class TestInsightShareDegenerate:
    BASE_URL = "https://example.com"

    def test_unknown_id_returns_none(self):
        out = build_insight_share(
            {"id": "future.rule", "evidence": {}},
            is_premium=True, base_url=self.BASE_URL,
        )
        assert out is None

    def test_non_dict_returns_none(self):
        assert build_insight_share(
            None, is_premium=True, base_url=self.BASE_URL,
        ) is None
        assert build_insight_share(
            "x", is_premium=True, base_url=self.BASE_URL,
        ) is None

    def test_evidence_not_dict_falls_back_to_empty_evidence(self):
        # The helper should still produce a coherent message rather
        # than crashing when evidence is malformed.
        out = build_insight_share(
            {"id": "trend.buy_delta", "evidence": "not a dict"},
            is_premium=False, base_url=self.BASE_URL,
        )
        assert out is not None
        # Default evidence (everything missing) yields the
        # "unchanged" branch.
        assert "unchanged" in out["text"]


# ---------------------------------------------------------------------------
# Daily-hook share copy
# ---------------------------------------------------------------------------


class TestDailyHookShareCopy:
    BASE_URL = "https://example.com"

    def test_free_up(self):
        hook = {"change": "up", "magnitude": 10, "headline": "Buys up 10"}
        out = build_daily_hook_share(
            hook, is_premium=False, base_url=self.BASE_URL,
        )
        assert out["text"] == (
            "Buy signals up 10 vs prior day on Momentum Trading Bot."
        )

    def test_free_down(self):
        hook = {"change": "down", "magnitude": 5}
        out = build_daily_hook_share(
            hook, is_premium=False, base_url=self.BASE_URL,
        )
        assert "down 5" in out["text"]

    def test_free_flat(self):
        hook = {"change": "flat", "magnitude": 0}
        out = build_daily_hook_share(
            hook, is_premium=False, base_url=self.BASE_URL,
        )
        assert "unchanged" in out["text"]

    def test_premium_uses_headline_when_present(self):
        hook = {
            "change": "up", "magnitude": 10,
            "headline": "Buys up 10 vs prior day",
        }
        out = build_daily_hook_share(
            hook, is_premium=True, base_url=self.BASE_URL,
        )
        assert "Buys up 10 vs prior day" in out["text"]
        assert "trending up" in out["text"]

    def test_premium_falls_back_when_no_headline(self):
        hook = {"change": "down", "magnitude": 3}
        out = build_daily_hook_share(
            hook, is_premium=True, base_url=self.BASE_URL,
        )
        assert "trending down" in out["text"]

    def test_non_dict_hook_returns_none(self):
        assert build_daily_hook_share(
            None, is_premium=True, base_url=self.BASE_URL,
        ) is None
        assert build_daily_hook_share(
            "x", is_premium=True, base_url=self.BASE_URL,
        ) is None


# ---------------------------------------------------------------------------
# Free vs premium framing — both tiers always get share text
# ---------------------------------------------------------------------------


class TestPremiumGetsRicherCopy:
    BASE_URL = "https://example.com"

    def test_premium_text_differs_from_free_for_trend(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "up", "percent_change": 50.0},
        }
        free = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )["text"]
        premium = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )["text"]
        assert free != premium
        # Premium copy carries percent figure; free does not.
        assert "%" not in free
        assert "%" in premium

    def test_premium_text_differs_from_free_for_hook(self):
        hook = {"change": "up", "magnitude": 10, "headline": "Buys up 10"}
        free = build_daily_hook_share(
            hook, is_premium=False, base_url=self.BASE_URL,
        )["text"]
        premium = build_daily_hook_share(
            hook, is_premium=True, base_url=self.BASE_URL,
        )["text"]
        assert free != premium

    def test_both_tiers_get_share_payload(self):
        insight = {
            "id": "trend.buy_delta",
            "evidence": {"delta": 10, "direction": "up"},
        }
        free = build_insight_share(
            insight, is_premium=False, base_url=self.BASE_URL,
        )
        premium = build_insight_share(
            insight, is_premium=True, base_url=self.BASE_URL,
        )
        # Same shape both tiers; only text differs.
        assert set(free.keys()) == set(premium.keys())
        assert free["cta"] == premium["cta"]
        assert free["url"] == premium["url"]


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_module_imports_only_stdlib_typing(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "share.py"
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
            "from trading_bot.api.stickiness",
        ):
            assert forbidden not in src, (
                f"share.py imports restricted surface: {forbidden!r}"
            )
