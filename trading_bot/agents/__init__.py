"""
Agent gate: Scout / Veto / Brief layer between the advisor and order placement.

This package is deliberately narrow. It sits AFTER the circuit breaker, hard
time exit, strategy, risk engine, correlation check, and rule-based
``TradingAdvisor`` and BEFORE ``PortfolioManager.open_position``. It can
only veto or shrink an already-approved entry. It can never:

- call the broker or hold broker credentials,
- change live-mode gates,
- bypass the circuit breaker, PDT protection, correlation, or position sizer,
- approve an entry the risk engine rejected or increase size,
- pick symbols or trade anything other than the scanner's US-equity output.

Components:

- ``RuleVeto``      deterministic, pure, no network (``veto.py``)
- ``CatalystScout`` optional LLM catalyst classifier, OFF by default (``scout.py``)
- ``AgentBrief``    structured decision record + log line (``brief.py``)
- ``AgentGate``     orchestrates the three and returns one ``AgentDecision`` (``gate.py``)
"""

from trading_bot.agents.brief import AgentBrief
from trading_bot.agents.gate import AgentGate
from trading_bot.agents.models import AgentDecision, VetoContext
from trading_bot.agents.scout import CatalystScout
from trading_bot.agents.veto import RuleVeto

__all__ = [
    "AgentBrief",
    "AgentDecision",
    "AgentGate",
    "CatalystScout",
    "RuleVeto",
    "VetoContext",
]
