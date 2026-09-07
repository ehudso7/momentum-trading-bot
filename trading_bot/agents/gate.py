"""
AgentGate: orchestrates RuleVeto → CatalystScout → AgentBrief and returns a
single ``AgentDecision`` for one candidate entry.

Position in the tick (``TradingBot._tick``):

    circuit breaker → hard time exit → strategy → risk sizer → correlation
    → TradingAdvisor.recommend_entry → **AgentGate.evaluate** → alpha filter
    → PortfolioManager.open_position

Order inside ``evaluate``:

1. Build a ``VetoContext`` from the objects the tick already has.
2. Run ``RuleVeto``. A block short-circuits: the scout is never consulted.
3. Run ``CatalystScout`` (returns ``llm_disabled`` unless enabled).
4. Apply scout policy: ``require_scout`` and ``block_toxic_catalysts``.
5. Combine ``size_multiplier = veto.multiplier * (0.5 if advisor reduce else 1.0)``.
6. Brief + persist. Return.

Fail-closed: any unexpected exception inside the gate becomes a ``block``
with reason ``gate_error:<type>`` and is logged at error level. A bug in
the agent layer can stop entries; it can never let one through unreviewed.
When ``agents.enabled`` is false the gate is a no-op allow and records
nothing, so behaviour is identical to the pre-gate bot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import structlog

from trading_bot.agents.brief import AgentBrief
from trading_bot.agents.models import (
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_REDUCE,
    SCOUT_STATUS_DISABLED,
    SCOUT_STATUS_FAILED,
    SCOUT_STATUS_OK,
    SOURCE_GATE,
    SOURCE_SCOUT,
    SOURCE_VETO,
    AgentDecision,
    ScoutResult,
    VetoContext,
)
from trading_bot.agents.scout import CatalystScout
from trading_bot.agents.veto import ADVISOR_REDUCE, RuleVeto
from trading_bot.config.settings import AgentsConfig, AppConfig, RiskConfig, ScannerConfig
from trading_bot.utils.helpers import is_market_open, is_near_close

log = structlog.get_logger(__name__)

# Minutes before close at which the tick's hard time exit fires; the veto
# mirrors that boundary so the two can never disagree.
HARD_EXIT_MINUTES_BEFORE_CLOSE = 10

# Circuit states in which the breaker allows new entries
# (``CircuitBreaker.is_trading_allowed``: NORMAL or WARNING).
_ENTRY_OK_CIRCUIT_STATES = frozenset({"normal", "warning"})

_MIN_REDUCE_MULTIPLIER = 0.25


class AgentGate:
    """Blocking-only gate between the advisor and order placement."""

    def __init__(
        self,
        config: AgentsConfig,
        *,
        veto: RuleVeto,
        scout: CatalystScout,
        brief: AgentBrief,
        risk_config: Optional[RiskConfig] = None,
        scanner_config: Optional[ScannerConfig] = None,
        market_open_fn: Callable[[], bool] = is_market_open,
        near_close_fn: Callable[[int], bool] = is_near_close,
    ) -> None:
        self._config = config
        self._veto = veto
        self._scout = scout
        self._brief = brief
        self._risk = risk_config
        self._scanner = scanner_config
        self._market_open_fn = market_open_fn
        self._near_close_fn = near_close_fn

    @classmethod
    def from_config(cls, config: AppConfig, data_dir: Optional[Path] = None) -> "AgentGate":
        """Build the production gate. ``data_dir`` defaults to the journal dir."""
        if data_dir is None:
            data_dir = Path(config.journal_csv_path).parent
        agents = config.agents
        # A disabled gate must have no side effects at all, including the
        # CSV header AgentBrief writes eagerly, so it gets a path-less brief.
        csv_path = data_dir / "agent_decisions.csv" if agents.enabled else None
        return cls(
            agents,
            veto=RuleVeto(min_advisor_confidence=agents.veto.min_advisor_confidence),
            scout=CatalystScout(agents.llm),
            brief=AgentBrief(csv_path=csv_path),
            risk_config=config.risk,
            scanner_config=config.scanner,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def scout(self) -> CatalystScout:
        return self._scout

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._brief.recent(limit)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        signal: Any,
        scan_result: Any,
        advisor_rec: Any,
        regime: Optional[str],
        positions: list,
        equity: float,
        circuit_status: Optional[dict],
        broker: Any = None,
    ) -> AgentDecision:
        """Return one decision. Never raises; unexpected errors block."""
        symbol = str(getattr(signal, "symbol", "") or getattr(scan_result, "symbol", "") or "")

        if not self._config.enabled:
            return AgentDecision(
                decision=DECISION_ALLOW,
                source=SOURCE_GATE,
                reasons=["agents_disabled"],
                size_multiplier=1.0,
                raw={"symbol": symbol, "advisor_action": getattr(advisor_rec, "action", None)},
            )

        try:
            decision = self._evaluate_unsafe(
                symbol=symbol,
                signal=signal,
                scan_result=scan_result,
                advisor_rec=advisor_rec,
                regime=regime,
                positions=positions,
                equity=equity,
                circuit_status=circuit_status,
                broker=broker,
            )
        except Exception as exc:
            log.error(
                "agent.gate_error",
                symbol=symbol,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            decision = AgentDecision(
                decision=DECISION_BLOCK,
                source=SOURCE_GATE,
                reasons=[f"gate_error:{type(exc).__name__}"],
                size_multiplier=0.0,
                raw={
                    "symbol": symbol,
                    "advisor_action": getattr(advisor_rec, "action", None),
                    "scout_status": "",
                    "scout_catalyst": "unknown",
                },
            )

        try:
            self._brief.record(decision)
        except Exception as exc:  # brief is best-effort, never blocks the loop
            log.debug("agent.brief_error", symbol=symbol, error=str(exc))
        return decision

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate_unsafe(
        self,
        *,
        symbol: str,
        signal: Any,
        scan_result: Any,
        advisor_rec: Any,
        regime: Optional[str],
        positions: list,
        equity: float,
        circuit_status: Optional[dict],
        broker: Any,
    ) -> AgentDecision:
        ctx = self.build_context(
            symbol=symbol,
            scan_result=scan_result,
            advisor_rec=advisor_rec,
            regime=regime,
            positions=positions,
            equity=equity,
            circuit_status=circuit_status,
            broker=broker,
        )
        base_raw = self._base_raw(ctx, scan_result)

        veto = self._veto.evaluate(ctx)
        if veto.decision == DECISION_BLOCK:
            return AgentDecision(
                decision=DECISION_BLOCK,
                source=SOURCE_VETO,
                reasons=list(veto.reasons),
                size_multiplier=0.0,
                scout_notes="scout_skipped:veto_block",
                raw={**base_raw, "scout_status": "", "scout_catalyst": "unknown"},
            )

        scout = self._scout.evaluate(
            symbol=symbol,
            gap_pct=float(getattr(scan_result, "gap_pct", 0.0) or 0.0),
            relative_volume=float(getattr(scan_result, "relative_volume", 0.0) or 0.0),
            catalyst_keywords=getattr(scan_result, "catalyst", None),
            advisor_reasons=list(getattr(advisor_rec, "reasons", []) or []),
        )
        raw = {
            **base_raw,
            "scout_status": scout.status,
            "scout_catalyst": scout.catalyst,
            "scout_confidence": scout.confidence,
            "scout_risk_note": scout.risk_note,
            "scout_failure": scout.failure,
            "veto_reasons": list(veto.reasons),
        }

        block_reasons = self._scout_policy_blocks(scout)
        if block_reasons:
            return AgentDecision(
                decision=DECISION_BLOCK,
                source=SOURCE_SCOUT,
                reasons=list(veto.reasons) + block_reasons,
                size_multiplier=0.0,
                scout_notes=self._scout_notes(scout),
                raw=raw,
            )

        reasons = list(veto.reasons)
        if scout.is_toxic:
            # Toxic but below the confidence bar or policy off: surface it.
            reasons.append(
                f"toxic_catalyst_warning:{scout.catalyst}({scout.confidence:.2f})"
            )

        multiplier = veto.size_multiplier
        if ctx.advisor_action == ADVISOR_REDUCE:
            multiplier *= 0.5
            reasons.append("advisor_reduce_size_applied")
        multiplier = max(_MIN_REDUCE_MULTIPLIER, min(1.0, multiplier))

        if multiplier < 1.0:
            return AgentDecision(
                decision=DECISION_REDUCE,
                source=SOURCE_VETO,
                reasons=reasons,
                size_multiplier=multiplier,
                scout_notes=self._scout_notes(scout),
                raw=raw,
            )
        return AgentDecision(
            decision=DECISION_ALLOW,
            source=SOURCE_GATE,
            reasons=reasons,
            size_multiplier=1.0,
            scout_notes=self._scout_notes(scout),
            raw=raw,
        )

    def _scout_policy_blocks(self, scout: ScoutResult) -> list[str]:
        reasons: list[str] = []
        if self._config.require_scout:
            if scout.status == SCOUT_STATUS_DISABLED:
                reasons.append("scout_required_but_disabled")
            elif scout.status == SCOUT_STATUS_FAILED:
                reasons.append(f"scout_required_but_failed:{scout.failure}")
        if (
            scout.status == SCOUT_STATUS_OK
            and scout.is_toxic
            and scout.confidence >= self._config.veto.toxic_catalyst_confidence
            and (self._config.require_scout or self._config.block_toxic_catalysts)
        ):
            reasons.append(f"toxic_catalyst:{scout.catalyst}({scout.confidence:.2f})")
        return reasons

    @staticmethod
    def _scout_notes(scout: ScoutResult) -> str:
        if scout.status == SCOUT_STATUS_DISABLED:
            return "llm_disabled"
        if scout.status == SCOUT_STATUS_FAILED:
            return f"scout_failed:{scout.failure}"
        return f"{scout.catalyst}({scout.confidence:.2f}): {scout.risk_note}".strip()

    def build_context(
        self,
        *,
        symbol: str,
        scan_result: Any,
        advisor_rec: Any,
        regime: Optional[str],
        positions: list,
        equity: float,
        circuit_status: Optional[dict],
        broker: Any,
    ) -> VetoContext:
        """Project tick objects onto the pure ``VetoContext``. No I/O except PDT."""
        circuit_state = None
        circuit_ok: Optional[bool] = None
        if isinstance(circuit_status, dict) and circuit_status.get("state") is not None:
            circuit_state = str(circuit_status["state"])
            circuit_ok = circuit_state in _ENTRY_OK_CIRCUIT_STATES

        held = frozenset(
            str(getattr(p, "symbol", "")) for p in (positions or []) if getattr(p, "symbol", "")
        )

        day_trade_count = self._day_trade_count(broker, symbol)

        return VetoContext(
            symbol=symbol or None,
            advisor_action=getattr(advisor_rec, "action", None),
            advisor_confidence=_float_or_none(getattr(advisor_rec, "confidence", None)),
            advisor_reasons=tuple(getattr(advisor_rec, "reasons", []) or []),
            circuit_ok=circuit_ok,
            circuit_state=circuit_state,
            market_open=self._safe_bool(self._market_open_fn),
            near_hard_exit=self._safe_bool(
                lambda: self._near_close_fn(HARD_EXIT_MINUTES_BEFORE_CLOSE)
            ),
            open_positions=len(positions) if positions is not None else None,
            max_open_positions=self._risk.max_open_positions if self._risk else None,
            held_symbols=held,
            regime=regime,
            gap_pct=_float_or_none(getattr(scan_result, "gap_pct", None)),
            max_gap_pct=self._scanner.max_gap_pct if self._scanner else None,
            price=_float_or_none(getattr(scan_result, "price", None)),
            min_price=self._scanner.min_price if self._scanner else None,
            max_price=self._scanner.max_price if self._scanner else None,
            relative_volume=_float_or_none(getattr(scan_result, "relative_volume", None)),
            min_relative_volume=self._scanner.min_relative_volume if self._scanner else None,
            float_shares=_int_or_none(getattr(scan_result, "float_shares", None)),
            max_float_shares=self._scanner.max_float_shares if self._scanner else None,
            # ScanResult carries no spread today; if a future field appears
            # the veto will pick it up, otherwise the check is skipped.
            spread_pct=_float_or_none(getattr(scan_result, "spread_pct", None)),
            max_spread_pct=None,
            equity=_float_or_none(equity),
            pdt_equity_threshold=self._risk.pdt_equity_threshold if self._risk else None,
            day_trade_count=day_trade_count,
        )

    @staticmethod
    def _day_trade_count(broker: Any, symbol: str) -> Optional[int]:
        """Read-only PDT probe. Any failure → None (check skipped, logged)."""
        getter = getattr(broker, "get_day_trade_count", None)
        if broker is None or not callable(getter):
            return None
        try:
            return _int_or_none(getter())
        except Exception as exc:
            log.warning("agent.day_trade_count_unavailable", symbol=symbol, error=str(exc))
            return None

    @staticmethod
    def _safe_bool(fn: Callable[[], bool]) -> Optional[bool]:
        try:
            return bool(fn())
        except Exception:
            return None

    @staticmethod
    def _base_raw(ctx: VetoContext, scan_result: Any) -> dict[str, Any]:
        return {
            "symbol": ctx.symbol or "",
            "advisor_action": ctx.advisor_action,
            "advisor_confidence": ctx.advisor_confidence,
            "advisor_reasons": list(ctx.advisor_reasons),
            "regime": ctx.regime,
            "gap_pct": ctx.gap_pct,
            "relative_volume": ctx.relative_volume,
            "catalyst_keywords": getattr(scan_result, "catalyst", None),
            "circuit_state": ctx.circuit_state,
            "open_positions": ctx.open_positions,
            "day_trade_count": ctx.day_trade_count,
        }


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
