"""
RuleVeto tests: deterministic, pure, no network.

Covers the required cases from the gate specification (missing context,
advisor skip, healthy allow, max positions, circuit halted) plus the
optional-check skipping and reduce paths.
"""

from __future__ import annotations

import pytest

from trading_bot.agents.models import AgentDecision, VetoContext
from trading_bot.agents.veto import REDUCE_MULTIPLIER, RuleVeto


def _healthy(**overrides) -> VetoContext:
    """A context that passes every rule unless overridden."""
    base = dict(
        symbol="ABCD",
        advisor_action="enter",
        advisor_confidence=0.72,
        advisor_reasons=("Favorable R:R of 2.0:1",),
        circuit_ok=True,
        circuit_state="normal",
        market_open=True,
        near_hard_exit=False,
        open_positions=1,
        max_open_positions=3,
        held_symbols=frozenset({"WXYZ"}),
        regime="trending_bullish",
        gap_pct=25.0,
        max_gap_pct=200.0,
        price=8.50,
        min_price=2.0,
        max_price=50.0,
        relative_volume=6.0,
        min_relative_volume=2.0,
        float_shares=12_000_000,
        max_float_shares=50_000_000,
        equity=100_000.0,
        pdt_equity_threshold=25_000.0,
        day_trade_count=0,
    )
    base.update(overrides)
    return VetoContext(**base)


@pytest.fixture
def veto() -> RuleVeto:
    return RuleVeto(min_advisor_confidence=0.55)


class TestRequiredContext:
    def test_empty_context_blocks(self, veto: RuleVeto):
        decision = veto.evaluate(VetoContext())
        assert decision.decision == "block"
        assert decision.size_multiplier == 0.0
        assert decision.source == "veto"
        assert "incomplete_context:symbol" in decision.reasons
        assert "incomplete_context:advisor_action" in decision.reasons
        assert "incomplete_context:advisor_confidence" in decision.reasons
        assert "incomplete_context:circuit_ok" in decision.reasons

    def test_missing_circuit_state_blocks_even_when_advisor_enters(self, veto):
        decision = veto.evaluate(_healthy(circuit_ok=None))
        assert decision.decision == "block"
        assert decision.reasons == ["incomplete_context:circuit_ok"]

    def test_unknown_advisor_action_blocks(self, veto):
        decision = veto.evaluate(_healthy(advisor_action="yolo"))
        assert decision.decision == "block"
        assert "unknown_advisor_action:yolo" in decision.reasons


class TestHardRules:
    def test_advisor_skip_blocks(self, veto):
        decision = veto.evaluate(_healthy(advisor_action="skip"))
        assert decision.decision == "block"
        assert "advisor_skip" in decision.reasons

    def test_low_advisor_confidence_blocks(self, veto):
        decision = veto.evaluate(_healthy(advisor_confidence=0.40))
        assert decision.decision == "block"
        assert decision.reasons == ["advisor_confidence:0.40<0.55"]

    def test_confidence_exactly_at_threshold_allows(self, veto):
        decision = veto.evaluate(_healthy(advisor_confidence=0.55))
        assert decision.decision == "allow"

    def test_max_positions_reached_blocks(self, veto):
        decision = veto.evaluate(_healthy(open_positions=3, max_open_positions=3))
        assert decision.decision == "block"
        assert "max_positions:3/3" in decision.reasons

    def test_circuit_halted_blocks(self, veto):
        decision = veto.evaluate(_healthy(circuit_ok=False, circuit_state="halted"))
        assert decision.decision == "block"
        assert "circuit_breaker:halted" in decision.reasons

    def test_session_closed_blocks(self, veto):
        assert "session_closed" in veto.evaluate(_healthy(market_open=False)).reasons

    def test_near_hard_exit_blocks(self, veto):
        assert "near_hard_exit" in veto.evaluate(_healthy(near_hard_exit=True)).reasons

    def test_symbol_already_held_blocks(self, veto):
        decision = veto.evaluate(_healthy(held_symbols=frozenset({"ABCD"})))
        assert decision.decision == "block"
        assert "already_held" in decision.reasons

    def test_gap_beyond_scanner_ceiling_blocks(self, veto):
        decision = veto.evaluate(_healthy(gap_pct=250.0, max_gap_pct=200.0))
        assert decision.decision == "block"
        assert "gap_extreme:250.0>200.0" in decision.reasons

    def test_gap_above_advisor_extended_but_within_ceiling_does_not_block(self, veto):
        # 120% is "extended" for the advisor (it reduces), not a veto block.
        decision = veto.evaluate(_healthy(gap_pct=120.0, max_gap_pct=200.0))
        assert decision.decision == "allow"

    def test_pdt_violation_blocks_small_account(self, veto):
        decision = veto.evaluate(
            _healthy(equity=10_000.0, pdt_equity_threshold=25_000.0, day_trade_count=3)
        )
        assert decision.decision == "block"
        assert "pdt_limit:3/3" in decision.reasons

    def test_pdt_not_enforced_above_threshold(self, veto):
        decision = veto.evaluate(
            _healthy(equity=100_000.0, pdt_equity_threshold=25_000.0, day_trade_count=9)
        )
        assert decision.decision == "allow"

    @pytest.mark.parametrize(
        "overrides, token",
        [
            ({"price": 1.0}, "price_below_min"),
            ({"price": 75.0}, "price_above_max"),
            ({"relative_volume": 1.2}, "rvol_below_min"),
            ({"float_shares": 90_000_000}, "float_above_max"),
            ({"spread_pct": 3.0, "max_spread_pct": 1.0}, "spread_above_max"),
        ],
    )
    def test_scanner_bounds_block(self, veto, overrides, token):
        decision = veto.evaluate(_healthy(**overrides))
        assert decision.decision == "block"
        assert any(r.startswith(token) for r in decision.reasons)

    def test_multiple_failures_are_all_reported(self, veto):
        decision = veto.evaluate(
            _healthy(advisor_action="skip", advisor_confidence=0.1, circuit_ok=False)
        )
        assert decision.decision == "block"
        assert len(decision.reasons) == 3


class TestOptionalChecksSkipWhenMissing:
    def test_missing_optional_fields_do_not_block(self, veto):
        decision = veto.evaluate(
            VetoContext(
                symbol="ABCD",
                advisor_action="enter",
                advisor_confidence=0.9,
                circuit_ok=True,
            )
        )
        assert decision.decision == "allow"
        assert decision.size_multiplier == 1.0
        assert decision.reasons == ["veto_passed"]

    def test_missing_bounds_skip_even_with_values_present(self, veto):
        decision = veto.evaluate(
            _healthy(max_gap_pct=None, gap_pct=999.0, max_float_shares=None, float_shares=10**9)
        )
        assert decision.decision == "allow"

    def test_pdt_skipped_without_day_trade_count(self, veto):
        decision = veto.evaluate(_healthy(equity=5_000.0, day_trade_count=None))
        assert decision.decision == "allow"


class TestReduce:
    def test_advisor_reduce_size_reduces(self, veto):
        decision = veto.evaluate(_healthy(advisor_action="reduce_size"))
        assert decision.decision == "reduce"
        assert decision.size_multiplier == REDUCE_MULTIPLIER
        assert "advisor_reduce_size" in decision.reasons

    @pytest.mark.parametrize("regime", ["trending_bearish", "high_volatility"])
    def test_hostile_regime_reduces(self, veto, regime):
        decision = veto.evaluate(_healthy(regime=regime))
        assert decision.decision == "reduce"
        assert f"hostile_regime:{regime}" in decision.reasons

    def test_block_wins_over_reduce(self, veto):
        decision = veto.evaluate(_healthy(advisor_action="reduce_size", circuit_ok=False))
        assert decision.decision == "block"


class TestPurity:
    def test_same_context_same_decision(self, veto):
        ctx = _healthy()
        assert veto.evaluate(ctx) == veto.evaluate(ctx)

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            RuleVeto(min_advisor_confidence=1.5)

    def test_decision_invariants_enforced(self):
        with pytest.raises(ValueError):
            AgentDecision(decision="block", source="veto", reasons=[], size_multiplier=0.5)
        with pytest.raises(ValueError):
            AgentDecision(decision="allow", source="veto", reasons=[], size_multiplier=0.5)
        with pytest.raises(ValueError):
            AgentDecision(decision="maybe", source="veto", reasons=[], size_multiplier=1.0)
