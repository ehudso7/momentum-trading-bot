"""
Optional LLM catalyst scout for the agent gate.

The scout answers one narrow question per candidate: what kind of catalyst
is behind this gap (earnings, fda, merger, dilution, offering, pump, rumor,
unknown) and a one-sentence risk note. It never picks symbols, never sizes,
never trades. It is the "LLM catalyst classification" item from
FUTURE_SCOPE.md and nothing more.

Safety properties:

- OFF by default (``agents.llm.enabled: false``). When off, ``evaluate``
  returns ``status="llm_disabled"`` without importing any SDK.
- Fails closed for *scout quality*: a timeout, malformed response, budget
  overrun, or SDK error yields ``status="failed"``. Whether a failed scout
  blocks the trade is the gate's policy (``require_scout``), not the scout's.
- Never raises into the trading loop.
- Never sees credentials as config. The provider key is read from the
  process environment (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``) inside
  the client factory, the same way Alpaca keys come from the environment,
  and is never placed in the prompt, logs, or decision record.
- One shot, no streaming, hard per-call timeout, hard daily call budget.
- The prompt carries only ticker, gap %, relative volume, catalyst keywords
  from the scan, and advisor reasons — all already-public market context.

Provider SDKs are optional extras (``pip install ".[llm]"``); they are
imported lazily and only when the scout is enabled.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timezone
from typing import Callable, Optional, Protocol

import structlog

from trading_bot.agents.models import (
    CATALYST_CLASSES,
    SCOUT_STATUS_DISABLED,
    SCOUT_STATUS_FAILED,
    SCOUT_STATUS_OK,
    ScoutResult,
)
from trading_bot.config.settings import AgentLLMConfig

log = structlog.get_logger(__name__)

OPENAI_KEY_ENV = "OPENAI_API_KEY"
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"

_MAX_FIELD_CHARS = 240
_MAX_REASONS = 6

SYSTEM_PROMPT = (
    "You classify the catalyst behind a US-equity intraday gap for a risk "
    "reviewer. Reply with ONLY a JSON object, no prose, no markdown, of the "
    'form {"catalyst": <one of ' + ", ".join(CATALYST_CLASSES) + ">, "
    '"confidence": <number 0 to 1>, "risk_note": <one sentence>}. '
    "Do not recommend buying or selling."
)


class LLMClient(Protocol):
    """Minimal provider-agnostic completion interface used by the scout."""

    def complete(self, *, system: str, user: str) -> str:
        """Return the model's raw text for one system+user prompt."""
        ...


ClientFactory = Callable[[AgentLLMConfig], LLMClient]


# ---------------------------------------------------------------------------
# Provider clients (lazy SDK imports; keys from env only)
# ---------------------------------------------------------------------------


def _read_env_key(env_var: str) -> str:
    key = (os.environ.get(env_var) or "").strip()
    if not key:
        raise RuntimeError(f"{env_var} is not set; the scout cannot start")
    return key


class OpenAIChatClient:
    """OpenAI Chat Completions, one shot, JSON mode, no streaming."""

    def __init__(self, config: AgentLLMConfig) -> None:
        try:
            from openai import OpenAI  # optional extra
        except ImportError as exc:  # pragma: no cover - environment specific
            raise RuntimeError(
                "openai SDK is not installed; install with `pip install '.[llm]'`"
            ) from exc
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._client = OpenAI(
            api_key=_read_env_key(OPENAI_KEY_ENV),
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    def complete(self, *, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self._max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
        )
        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice and choice.message else None
        return content or ""


class AnthropicMessagesClient:
    """Anthropic Messages API, one shot, no streaming."""

    def __init__(self, config: AgentLLMConfig) -> None:
        try:
            import anthropic  # optional extra
        except ImportError as exc:  # pragma: no cover - environment specific
            raise RuntimeError(
                "anthropic SDK is not installed; install with `pip install '.[llm]'`"
            ) from exc
        self._model = config.model
        self._max_tokens = config.max_tokens
        self._client = anthropic.Anthropic(
            api_key=_read_env_key(ANTHROPIC_KEY_ENV),
            timeout=config.timeout_seconds,
            max_retries=0,
        )

    def complete(self, *, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("model refused the classification request")
        parts = [
            block.text
            for block in getattr(response, "content", []) or []
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts)


def default_client_factory(config: AgentLLMConfig) -> LLMClient:
    if config.provider == "openai":
        return OpenAIChatClient(config)
    if config.provider == "anthropic":
        return AnthropicMessagesClient(config)
    raise RuntimeError(f"unsupported llm provider {config.provider!r}")


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------


def _clip(value: object, limit: int = _MAX_FIELD_CHARS) -> str:
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return text[:limit]


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class CatalystScout:
    """
    Optional catalyst classifier. Safe to construct and call with the LLM
    disabled; the SDK client is built lazily on the first enabled call.
    """

    def __init__(
        self,
        config: AgentLLMConfig,
        client: Optional[LLMClient] = None,
        client_factory: ClientFactory = default_client_factory,
        today_fn: Callable[[], date] = _utc_today,
    ) -> None:
        self._config = config
        self._client = client
        self._client_factory = client_factory
        self._today_fn = today_fn
        self._lock = threading.Lock()
        self._budget_day: Optional[date] = None
        self._calls_today = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def calls_today(self) -> int:
        with self._lock:
            self._roll_budget_day()
            return self._calls_today

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        symbol: str,
        gap_pct: float,
        relative_volume: float,
        catalyst_keywords: Optional[str],
        advisor_reasons: list[str],
    ) -> ScoutResult:
        """Classify one candidate. Never raises."""
        if not self._config.enabled:
            return ScoutResult(status=SCOUT_STATUS_DISABLED, catalyst="unknown")

        if not self._reserve_call():
            return self._failed(
                symbol, f"daily_call_budget_exceeded:{self._config.daily_call_budget}"
            )

        try:
            client = self._get_client()
            prompt = self.build_prompt(
                symbol=symbol,
                gap_pct=gap_pct,
                relative_volume=relative_volume,
                catalyst_keywords=catalyst_keywords,
                advisor_reasons=advisor_reasons,
            )
            raw_text = client.complete(system=SYSTEM_PROMPT, user=prompt)
        except Exception as exc:  # timeout, auth, network, SDK missing, ...
            return self._failed(symbol, f"{type(exc).__name__}:{_clip(exc, 120)}")

        parsed = self.parse_response(raw_text)
        if parsed is None:
            return self._failed(symbol, "parse_failure")
        return parsed

    @staticmethod
    def build_prompt(
        *,
        symbol: str,
        gap_pct: float,
        relative_volume: float,
        catalyst_keywords: Optional[str],
        advisor_reasons: list[str],
    ) -> str:
        """Short, credential-free prompt. Public so tests can pin its shape."""
        reasons = [_clip(r) for r in list(advisor_reasons)[:_MAX_REASONS]]
        payload = {
            "ticker": _clip(symbol, 12).upper(),
            "gap_pct": round(float(gap_pct), 1),
            "relative_volume": round(float(relative_volume), 1),
            "catalyst_keywords": _clip(catalyst_keywords) or "none",
            "advisor_reasons": reasons,
        }
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def parse_response(text: str) -> Optional[ScoutResult]:
        """
        Strict parse of the model reply. Returns ``None`` on any deviation.

        Accepts a bare JSON object, or an object embedded in fences/prose
        (first ``{...}`` span), but the object itself must carry a catalyst
        from the closed vocabulary and a numeric confidence.
        """
        if not text:
            return None
        candidate = text.strip()
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except (TypeError, ValueError):
                return None
        if not isinstance(data, dict):
            return None

        catalyst = str(data.get("catalyst", "")).strip().lower()
        if catalyst not in CATALYST_CLASSES:
            return None
        try:
            confidence = float(data.get("confidence"))
        except (TypeError, ValueError):
            return None
        if not (0.0 <= confidence <= 1.0):
            return None
        risk_note = _clip(data.get("risk_note", ""))
        return ScoutResult(
            status=SCOUT_STATUS_OK,
            catalyst=catalyst,
            confidence=confidence,
            risk_note=risk_note,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = self._client_factory(self._config)
        return self._client

    def _roll_budget_day(self) -> None:
        today = self._today_fn()
        if self._budget_day != today:
            self._budget_day = today
            self._calls_today = 0

    def _reserve_call(self) -> bool:
        with self._lock:
            self._roll_budget_day()
            if self._calls_today >= self._config.daily_call_budget:
                return False
            self._calls_today += 1
            return True

    @staticmethod
    def _failed(symbol: str, failure: str) -> ScoutResult:
        log.warning("agent.scout_failed", symbol=symbol, failure=failure)
        return ScoutResult(status=SCOUT_STATUS_FAILED, catalyst="unknown", failure=failure)
