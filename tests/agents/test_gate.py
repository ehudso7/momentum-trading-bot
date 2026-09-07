"""
AgentGate tests: orchestration policy plus the paper-path wiring in
``TradingBot._tick``.

No network anywhere: the scout is either disabled or backed by a fake client,
and the integration tests run against the keyless in-memory PaperBroker with
every data source mocked.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytest

from trading_bot.agents.brief import AgentBrief
from trading_bot.agents.gate import AgentGate
from trading_bot.agents.models import SCOUT_STATUS_DISABLED
from trading_bot.agents.scout import CatalystScout
from trading_bot.agents.veto import RuleVeto
from trading_bot.config.settings import (
    AgentLLMConfig,
    AgentsConfig,
    AgentVetoConfig,
    AppConfig,
    BrokerConfig,
    RiskConfig,
    RunMode,
    ScannerConfig,
)
from trading_bot.models.domain import ScanResult, SignalType, TradeSignal
from trading_bot.strategies.advisor import EntryRecommendation
from trading_bot.strategies.regime import MarketRegime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scan(symbol: str = "ABCD", gap_pct: float = 30.0, catalyst: str | None = "FDA") -> ScanResult:
    return ScanResult(
        symbol=symbol,
        price=10.0,
        gap_pct=gap_pct,
        relative_volume=6.0,
        float_shares=15_000_000,
        volume=4_000_000,
        prev_close=7.7,
        catalyst=catalyst,
        score=0.8,
    )


def _signal(symbol: str = "ABCD", confidence: float = 0.75) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        signal_type=SignalType.VWAP_PULLBACK,
        entry_price=10.0,
        stop_price=9.60,
        target_prices=[10.60, 11.20],
        atr=0.30,
        vwap=9.90,
        ema9=9.95,
        confidence=confidence,
    )


def _rec(action: str = "enter", confidence: float = 0.75, reasons=None) -> EntryRecommendation:
    return EntryRecommendation(
        action=action,
        confidence=confidence,
        reasons=reasons or ["Favorable R:R of 2.0:1"],
        suggested_adjustments={},
    )


class FakeScoutClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, *, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


def _gate(
    tmp_path: Path,
    *,
    agents: AgentsConfig | None = None,
    scout: CatalystScout | None = None,
    market_open: bool = True,
    near_close: bool = False,
) -> AgentGate:
    agents = agents or AgentsConfig()
    return AgentGate(
        agents,
        veto=RuleVeto(min_advisor_confidence=agents.veto.min_advisor_confidence),
        scout=scout or CatalystScout(agents.llm),
        brief=AgentBrief(csv_path=tmp_path / "agent_decisions.csv"),
        risk_config=RiskConfig(),
        scanner_config=ScannerConfig(),
        market_open_fn=lambda: market_open,
        near_close_fn=lambda _minutes: near_close,
    )


def _evaluate(gate: AgentGate, *, rec=None, positions=None, circuit_state="normal", broker=None, **kw):
    return gate.evaluate(
        signal=kw.pop("signal", _signal()),
        scan_result=kw.pop("scan_result", _scan()),
        advisor_rec=rec or _rec(),
        regime=kw.pop("regime", "trending_bullish"),
        positions=positions or [],
        equity=kw.pop("equity", 100_000.0),
        circuit_status={"state": circuit_state} if circuit_state is not None else None,
        broker=broker,
    )


def _csv_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Gate policy
# ---------------------------------------------------------------------------


class TestGatePolicy:
    def test_veto_block_short_circuits_scout(self, tmp_path):
        scout = Mock(spec=CatalystScout)
        gate = _gate(tmp_path, scout=scout)
        decision = _evaluate(gate, rec=_rec(action="skip"))
        assert decision.decision == "block"
        assert decision.source == "veto"
        assert "advisor_skip" in decision.reasons
        assert decision.scout_notes == "scout_skipped:veto_block"
        scout.evaluate.assert_not_called()

    def test_llm_disabled_still_allows_passing_veto(self, tmp_path):
        gate = _gate(tmp_path)  # default AgentsConfig: llm off
        decision = _evaluate(gate)
        assert decision.decision == "allow"
        assert decision.size_multiplier == 1.0
        assert decision.scout_notes == "llm_disabled"
        assert decision.raw["scout_status"] == SCOUT_STATUS_DISABLED

    def test_toxic_catalyst_with_block_toxic_catalysts_blocks(self, tmp_path):
        agents = AgentsConfig(llm=AgentLLMConfig(enabled=True), block_toxic_catalysts=True)
        client = FakeScoutClient('{"catalyst": "pump", "confidence": 0.92, "risk_note": "Promo-driven."}')
        gate = _gate(tmp_path, agents=agents, scout=CatalystScout(agents.llm, client=client))
        decision = _evaluate(gate)
        assert decision.decision == "block"
        assert decision.source == "scout"
        assert "toxic_catalyst:pump(0.92)" in decision.reasons
        assert client.calls == 1

    def test_toxic_catalyst_below_confidence_only_warns(self, tmp_path):
        agents = AgentsConfig(
            llm=AgentLLMConfig(enabled=True),
            block_toxic_catalysts=True,
            veto=AgentVetoConfig(toxic_catalyst_confidence=0.9),
        )
        client = FakeScoutClient('{"catalyst": "offering", "confidence": 0.6, "risk_note": ""}')
        gate = _gate(tmp_path, agents=agents, scout=CatalystScout(agents.llm, client=client))
        decision = _evaluate(gate)
        assert decision.decision == "allow"
        assert "toxic_catalyst_warning:offering(0.60)" in decision.reasons

    def test_toxic_catalyst_with_policy_off_does_not_block(self, tmp_path):
        agents = AgentsConfig(
            llm=AgentLLMConfig(enabled=True), block_toxic_catalysts=False, require_scout=False
        )
        client = FakeScoutClient('{"catalyst": "dilution", "confidence": 0.95, "risk_note": ""}')
        gate = _gate(tmp_path, agents=agents, scout=CatalystScout(agents.llm, client=client))
        decision = _evaluate(gate)
        assert decision.decision == "allow"

    def test_require_scout_honours_toxic_verdict_even_with_block_policy_off(self, tmp_path):
        agents = AgentsConfig(
            llm=AgentLLMConfig(enabled=True), require_scout=True, block_toxic_catalysts=False
        )
        client = FakeScoutClient('{"catalyst": "pump", "confidence": 0.9, "risk_note": ""}')
        gate = _gate(tmp_path, agents=agents, scout=CatalystScout(agents.llm, client=client))
        decision = _evaluate(gate)
        assert decision.decision == "block"
        assert "toxic_catalyst:pump(0.90)" in decision.reasons

    def test_scout_failure_does_not_block_unless_required(self, tmp_path):
        agents = AgentsConfig(llm=AgentLLMConfig(enabled=True), require_scout=False)
        client = FakeScoutClient("garbage")
        gate = _gate(tmp_path, agents=agents, scout=CatalystScout(agents.llm, client=client))
        decision = _evaluate(gate)
        assert decision.decision == "allow"
        assert decision.scout_notes.startswith("scout_failed:")

    def test_scout_failure_blocks_when_required(self, tmp_path):
        agents = AgentsConfig(llm=AgentLLMConfig(enabled=True), require_scout=True)
        client = FakeScoutClient("garbage")
        gate = _gate(tmp_path, agents=agents, scout=CatalystScout(agents.llm, client=client))
        decision = _evaluate(gate)
        assert decision.decision == "block"
        assert decision.source == "scout"
        assert any(r.startswith("scout_required_but_failed") for r in decision.reasons)

    def test_require_scout_with_llm_disabled_blocks(self, tmp_path):
        agents = AgentsConfig(require_scout=True)
        gate = _gate(tmp_path, agents=agents)
        decision = _evaluate(gate)
        assert decision.decision == "block"
        assert "scout_required_but_disabled" in decision.reasons

    def test_advisor_reduce_size_combines_multipliers(self, tmp_path):
        gate = _gate(tmp_path)
        decision = _evaluate(gate, rec=_rec(action="reduce_size"))
        # veto 0.5 (advisor reduce) * gate 0.5 (advisor reduce) = 0.25
        assert decision.decision == "reduce"
        assert decision.size_multiplier == 0.25
        assert "advisor_reduce_size" in decision.reasons
        assert "advisor_reduce_size_applied" in decision.reasons

    def test_hostile_regime_alone_reduces_to_half(self, tmp_path):
        gate = _gate(tmp_path)
        decision = _evaluate(gate, regime="trending_bearish")
        assert decision.decision == "reduce"
        assert decision.size_multiplier == 0.5

    def test_agents_disabled_is_noop_allow_and_records_nothing(self, tmp_path):
        gate = _gate(tmp_path, agents=AgentsConfig(enabled=False))
        decision = _evaluate(gate, rec=_rec(action="skip"), circuit_state="halted")
        assert decision.decision == "allow"
        assert decision.reasons == ["agents_disabled"]
        assert gate.recent_decisions() == []

    def test_from_config_disabled_has_no_filesystem_side_effect(self, tmp_path):
        config = AppConfig(
            journal_csv_path=str(tmp_path / "data" / "journal.csv"),
            agents=AgentsConfig(enabled=False),
        )
        gate = AgentGate.from_config(config)
        assert not gate.enabled
        assert not (tmp_path / "data" / "agent_decisions.csv").exists()
        assert not (tmp_path / "data").exists()
        decision = _evaluate(gate, rec=_rec(action="skip"))
        assert decision.decision == "allow"
        assert not (tmp_path / "data").exists()

    def test_from_config_enabled_creates_decision_csv(self, tmp_path):
        config = AppConfig(journal_csv_path=str(tmp_path / "data" / "journal.csv"))
        gate = AgentGate.from_config(config)
        assert gate.enabled
        assert (tmp_path / "data" / "agent_decisions.csv").exists()

    def test_unexpected_error_fails_closed(self, tmp_path):
        gate = _gate(tmp_path)
        gate._veto = Mock()
        gate._veto.evaluate.side_effect = RuntimeError("boom")
        decision = _evaluate(gate)
        assert decision.decision == "block"
        assert decision.source == "gate"
        assert decision.reasons == ["gate_error:RuntimeError"]


class TestContextBuilding:
    def test_circuit_halted_blocks_via_context(self, tmp_path):
        decision = _evaluate(_gate(tmp_path), circuit_state="halted")
        assert decision.decision == "block"
        assert "circuit_breaker:halted" in decision.reasons

    def test_missing_circuit_status_blocks(self, tmp_path):
        decision = _evaluate(_gate(tmp_path), circuit_state=None)
        assert decision.decision == "block"
        assert "incomplete_context:circuit_ok" in decision.reasons

    def test_session_flags_come_from_injected_probes(self, tmp_path):
        assert "session_closed" in _evaluate(_gate(tmp_path, market_open=False)).reasons
        assert "near_hard_exit" in _evaluate(_gate(tmp_path, near_close=True)).reasons

    def test_max_positions_from_risk_config(self, tmp_path):
        positions = [Mock(symbol=f"P{i}") for i in range(RiskConfig().max_open_positions)]
        decision = _evaluate(_gate(tmp_path), positions=positions)
        assert decision.decision == "block"
        assert any(r.startswith("max_positions:") for r in decision.reasons)

    def test_already_held_symbol_blocks(self, tmp_path):
        decision = _evaluate(_gate(tmp_path), positions=[Mock(symbol="ABCD")])
        assert "already_held" in decision.reasons

    def test_pdt_probe_reads_broker_and_blocks_small_account(self, tmp_path):
        broker = Mock()
        broker.get_day_trade_count.return_value = 3
        decision = _evaluate(_gate(tmp_path), broker=broker, equity=10_000.0)
        assert decision.decision == "block"
        assert "pdt_limit:3/3" in decision.reasons
        broker.get_day_trade_count.assert_called_once()

    def test_broken_pdt_probe_skips_check(self, tmp_path):
        broker = Mock()
        broker.get_day_trade_count.side_effect = RuntimeError("api down")
        decision = _evaluate(_gate(tmp_path), broker=broker, equity=10_000.0)
        assert decision.decision == "allow"

    def test_gate_never_calls_order_methods_on_broker(self, tmp_path):
        broker = Mock()
        broker.get_day_trade_count.return_value = 0
        _evaluate(_gate(tmp_path), broker=broker)
        used = {name for name, *_ in broker.mock_calls}
        assert used == {"get_day_trade_count"}


class TestBrief:
    def test_every_decision_is_persisted_and_exposed(self, tmp_path):
        gate = _gate(tmp_path)
        _evaluate(gate)  # allow
        _evaluate(
            gate, rec=_rec(action="skip"), signal=_signal("EFGH"), scan_result=_scan("EFGH")
        )  # block
        rows = _csv_rows(tmp_path / "agent_decisions.csv")
        assert [r["decision"] for r in rows] == ["allow", "block"]
        assert rows[1]["symbol"] == "EFGH"
        assert "advisor_skip" in rows[1]["reasons"]
        recent = gate.recent_decisions()
        assert [r["symbol"] for r in recent] == ["EFGH", "ABCD"]  # newest first
        assert set(recent[0]) >= {"timestamp", "decision", "reasons", "size_multiplier"}


# ---------------------------------------------------------------------------
# Paper-path wiring through TradingBot._tick
# ---------------------------------------------------------------------------


def _bars() -> pd.DataFrame:
    idx = pd.date_range("2026-09-08 09:30", periods=30, freq="1min")
    close = np.linspace(9.5, 10.0, 30)
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": np.full(30, 50_000),
        },
        index=idx,
    )


@pytest.fixture
def paper_bot(tmp_path, monkeypatch):
    """A keyless paper-mode TradingBot with every external source mocked."""
    import trading_bot.main as main_mod

    config = AppConfig(
        run_mode=RunMode.PAPER,
        starting_capital=100_000.0,
        broker=BrokerConfig(),
        journal_csv_path=str(tmp_path / "journal.csv"),
    )
    bot = main_mod.TradingBot(config)
    assert bot._broker_provider == "local_paper"

    # Replace the gate with one whose session probes are deterministic.
    bot._agent_gate = _gate(tmp_path)

    monkeypatch.setattr(main_mod, "is_market_open", lambda: True)
    monkeypatch.setattr(main_mod, "is_near_close", lambda minutes_before=10: False)
    monkeypatch.setattr(main_mod, "is_premarket", lambda: False)
    frozen_now = datetime(2026, 9, 8, 10, 15, tzinfo=main_mod.now_et().tzinfo)
    monkeypatch.setattr(main_mod, "now_et", lambda: frozen_now)

    bot._market_data = Mock()
    bot._market_data.get_daily_bars.return_value = pd.DataFrame()
    bot._market_data.get_intraday_bars.return_value = _bars()
    bot._regime_detector = Mock()
    bot._regime_detector.detect.return_value = MarketRegime.TRENDING_BULLISH
    bot._regime_detector.get_regime_adjustments.return_value = {}
    bot._strategy = Mock()
    bot._strategy.evaluate.return_value = _signal()
    bot._strategy.last_rejection_details = {}
    bot._scanner = Mock()
    bot._scanner.scan.return_value = [_scan()]
    bot._portfolio.update_positions = Mock(return_value=[])
    bot._portfolio.reconcile_positions = Mock()
    bot._portfolio.open_position = Mock(name="open_position")
    bot._notify = Mock()
    bot._advisor = Mock()
    bot._last_trading_date = "2026-09-08"
    bot._daily_plan_generated = True
    bot._running = True  # the loop sets this in run(); _tick() exits early otherwise
    return bot


class TestTickWiring:
    def test_gate_block_rejects_setup_without_placing_order(self, paper_bot, tmp_path):
        # Advisor says enter, but its adjusted confidence is below the veto bar.
        paper_bot._advisor.recommend_entry.return_value = _rec(action="enter", confidence=0.40)

        paper_bot._tick()

        paper_bot._portfolio.open_position.assert_not_called()
        rejections = list(paper_bot._rejected_signals)
        assert len(rejections) == 1
        assert rejections[0].stage == "agent_veto"
        assert rejections[0].reason.startswith("[agent_veto] ")
        assert "advisor_confidence:0.40<0.55" in rejections[0].reason

        gate_rows = _csv_rows(tmp_path / "agent_decisions.csv")
        assert len(gate_rows) == 1
        assert gate_rows[0]["decision"] == "block"
        assert gate_rows[0]["symbol"] == "ABCD"

        decision_rows = _csv_rows(tmp_path / "decision_log.csv")
        assert decision_rows[-1]["action"] == "skip"
        assert decision_rows[-1]["reason"].startswith("agent_veto:")

    def test_gate_allow_leaves_entry_path_unchanged(self, paper_bot, tmp_path):
        paper_bot._advisor.recommend_entry.return_value = _rec(action="enter", confidence=0.80)
        opened = Mock(symbol="ABCD", shares=100, entry_price=10.0, stop_price=9.6)
        paper_bot._portfolio.open_position.return_value = opened

        paper_bot._tick()

        paper_bot._portfolio.open_position.assert_called_once()
        signal_arg, risk_arg = paper_bot._portfolio.open_position.call_args.args
        assert signal_arg.symbol == "ABCD"
        assert risk_arg.approved
        assert list(paper_bot._rejected_signals) == []
        gate_rows = _csv_rows(tmp_path / "agent_decisions.csv")
        assert [r["decision"] for r in gate_rows] == ["allow"]

    def test_gate_reduce_shrinks_shares_but_never_grows(self, paper_bot):
        paper_bot._advisor.recommend_entry.return_value = _rec(action="reduce_size", confidence=0.80)
        # A+ signal (>= 0.80) so the existing tiered-sizing hook is a no-op
        # and the gate's multiplier is the only adjustment applied.
        paper_bot._strategy.evaluate.return_value = _signal(confidence=0.85)
        opened = Mock(symbol="ABCD", shares=10, entry_price=10.0, stop_price=9.6)
        paper_bot._portfolio.open_position.return_value = opened
        sized = Mock()
        sized.calculate.return_value = paper_bot._sizer.calculate(
            equity=100_000.0,
            entry_price=10.0,
            stop_price=9.60,
            current_positions=[],
            buying_power=200_000.0,
        )
        pre_gate_shares = sized.calculate.return_value.shares
        assert pre_gate_shares > 4
        paper_bot._sizer = sized

        paper_bot._tick()

        _, risk_arg = paper_bot._portfolio.open_position.call_args.args
        # regime/tier/volatility hooks are no-ops here (bullish, conf 0.85,
        # ATR 3%), so the only multiplier applied is the gate's 0.25.
        assert risk_arg.shares == max(1, int(pre_gate_shares * 0.25))
        assert risk_arg.shares < pre_gate_shares

    def test_advisor_skip_still_records_an_agent_decision(self, paper_bot, tmp_path):
        paper_bot._advisor.recommend_entry.return_value = _rec(action="skip", confidence=0.30)

        paper_bot._tick()

        paper_bot._portfolio.open_position.assert_not_called()
        rejections = list(paper_bot._rejected_signals)
        assert [r.stage for r in rejections] == ["advisor"]  # existing path preserved
        gate_rows = _csv_rows(tmp_path / "agent_decisions.csv")
        assert gate_rows[0]["decision"] == "block"
        assert "advisor_skip" in gate_rows[0]["reasons"]

    def test_gate_disabled_matches_pre_gate_behaviour(self, paper_bot, tmp_path):
        paper_bot._agent_gate = _gate(tmp_path, agents=AgentsConfig(enabled=False))
        paper_bot._advisor.recommend_entry.return_value = _rec(action="enter", confidence=0.40)
        opened = Mock(symbol="ABCD", shares=100, entry_price=10.0, stop_price=9.6)
        paper_bot._portfolio.open_position.return_value = opened

        paper_bot._tick()

        paper_bot._portfolio.open_position.assert_called_once()
        assert list(paper_bot._rejected_signals) == []

    def test_default_config_agents_on_llm_off(self):
        config = AppConfig.from_yaml()
        assert config.agents.enabled is True
        assert config.agents.llm.enabled is False
        assert config.run_mode == RunMode.PAPER
        assert config.live_trading_enabled is False

    def test_dashboard_receives_agent_decisions(self, paper_bot, tmp_path):
        from trading_bot.dashboard.state import DashboardState

        paper_bot._dashboard_state = DashboardState()
        paper_bot._advisor.recommend_entry.return_value = _rec(action="enter", confidence=0.40)
        paper_bot._tick()
        with patch.object(paper_bot, "_get_market_status", return_value=("open", "")):
            paper_bot._update_dashboard()
        snap = paper_bot._dashboard_state.get_snapshot()
        assert len(snap.agent_decisions) == 1
        assert snap.agent_decisions[0]["decision"] == "block"
