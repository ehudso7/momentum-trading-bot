"""
CatalystScout tests. No network: every client is a fake.
"""

from __future__ import annotations

from datetime import date

import pytest

from trading_bot.agents.models import SCOUT_STATUS_DISABLED, SCOUT_STATUS_FAILED, SCOUT_STATUS_OK
from trading_bot.agents.scout import (
    ANTHROPIC_KEY_ENV,
    OPENAI_KEY_ENV,
    SYSTEM_PROMPT,
    CatalystScout,
    default_client_factory,
)
from trading_bot.config.settings import AgentLLMConfig


class FakeClient:
    def __init__(self, reply: str | Exception):
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _evaluate(scout: CatalystScout, **overrides):
    kwargs = dict(
        symbol="ABCD",
        gap_pct=42.0,
        relative_volume=8.0,
        catalyst_keywords="FDA approval",
        advisor_reasons=["Favorable R:R of 2.0:1"],
    )
    kwargs.update(overrides)
    return scout.evaluate(**kwargs)


class TestDisabled:
    def test_llm_disabled_returns_llm_disabled_without_client(self):
        def _boom(_cfg):  # pragma: no cover - must never run
            raise AssertionError("client factory must not be invoked when disabled")

        scout = CatalystScout(AgentLLMConfig(enabled=False), client_factory=_boom)
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_DISABLED
        assert result.catalyst == "unknown"
        assert not result.is_toxic
        assert scout.calls_today == 0


class TestEnabledParsing:
    def test_valid_json_is_parsed(self):
        client = FakeClient('{"catalyst": "fda", "confidence": 0.85, "risk_note": "Binary event."}')
        scout = CatalystScout(AgentLLMConfig(enabled=True), client=client)
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_OK
        assert result.catalyst == "fda"
        assert result.confidence == 0.85
        assert result.risk_note == "Binary event."
        assert scout.calls_today == 1

    def test_json_wrapped_in_fences_is_parsed(self):
        client = FakeClient('```json\n{"catalyst": "pump", "confidence": 0.9, "risk_note": "x"}\n```')
        scout = CatalystScout(AgentLLMConfig(enabled=True), client=client)
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_OK
        assert result.is_toxic

    @pytest.mark.parametrize(
        "reply",
        [
            # Trailing braces after the real object (greedy regex used to over-capture).
            '{"catalyst": "fda", "confidence": 0.8, "risk_note": "x"} }}',
            # Two objects: the first complete one wins.
            '{"catalyst": "fda", "confidence": 0.8, "risk_note": "x"}\n{"catalyst": "pump", "confidence": 0.1}',
            # Prose containing a stray brace before the object.
            'Sure { here is it: {"catalyst": "fda", "confidence": 0.8, "risk_note": "x"} done',
            # Nested braces inside a string value.
            '{"catalyst": "fda", "confidence": 0.8, "risk_note": "see {note}"}',
        ],
    )
    def test_extra_braces_do_not_break_parsing(self, reply):
        scout = CatalystScout(AgentLLMConfig(enabled=True), client=FakeClient(reply))
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_OK
        assert result.catalyst == "fda"
        assert result.confidence == 0.8

    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "not json at all",
            '{"catalyst": "moon", "confidence": 0.9}',  # outside vocabulary
            '{"catalyst": "fda"}',  # missing confidence
            '{"catalyst": "fda", "confidence": "high"}',
            '{"catalyst": "fda", "confidence": 1.7}',
            "[1, 2, 3]",
            '{"catalyst": "fda", "confidence": 0.5',  # truncated
        ],
    )
    def test_malformed_model_json_does_not_raise(self, reply):
        scout = CatalystScout(AgentLLMConfig(enabled=True), client=FakeClient(reply))
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_FAILED
        assert result.catalyst == "unknown"
        assert result.failure == "parse_failure"

    def test_client_exception_fails_closed_without_raising(self):
        scout = CatalystScout(
            AgentLLMConfig(enabled=True), client=FakeClient(TimeoutError("read timed out"))
        )
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_FAILED
        assert result.failure.startswith("TimeoutError")

    def test_client_factory_failure_fails_closed(self):
        def _factory(_cfg):
            raise RuntimeError("OPENAI_API_KEY is not set")

        scout = CatalystScout(AgentLLMConfig(enabled=True), client_factory=_factory)
        result = _evaluate(scout)
        assert result.status == SCOUT_STATUS_FAILED
        assert "OPENAI_API_KEY is not set" in result.failure


class TestBudget:
    def test_daily_budget_is_enforced_and_resets_by_day(self):
        client = FakeClient('{"catalyst": "earnings", "confidence": 0.6, "risk_note": ""}')
        today = {"value": date(2026, 9, 7)}
        scout = CatalystScout(
            AgentLLMConfig(enabled=True, daily_call_budget=2),
            client=client,
            today_fn=lambda: today["value"],
        )
        assert _evaluate(scout).status == SCOUT_STATUS_OK
        assert _evaluate(scout).status == SCOUT_STATUS_OK
        third = _evaluate(scout)
        assert third.status == SCOUT_STATUS_FAILED
        assert third.failure == "daily_call_budget_exceeded:2"
        assert len(client.calls) == 2

        today["value"] = date(2026, 9, 8)
        assert _evaluate(scout).status == SCOUT_STATUS_OK
        assert len(client.calls) == 3

    def test_client_construction_failure_does_not_consume_budget(self):
        """A missing key/SDK is configuration, not a model call."""
        attempts = {"n": 0}

        def _factory(_cfg):
            attempts["n"] += 1
            raise RuntimeError("OPENAI_API_KEY is not set")

        scout = CatalystScout(
            AgentLLMConfig(enabled=True, daily_call_budget=1), client_factory=_factory
        )
        for _ in range(3):
            result = _evaluate(scout)
            assert result.status == SCOUT_STATUS_FAILED
            assert "OPENAI_API_KEY is not set" in result.failure
        assert attempts["n"] == 3
        assert scout.calls_today == 0

    def test_budget_is_consumed_only_when_request_is_sent(self):
        client = FakeClient(TimeoutError("read timed out"))
        scout = CatalystScout(AgentLLMConfig(enabled=True, daily_call_budget=1), client=client)
        assert _evaluate(scout).failure.startswith("TimeoutError")
        assert scout.calls_today == 1  # the request went out, so it counts
        assert _evaluate(scout).failure == "daily_call_budget_exceeded:1"
        assert len(client.calls) == 1

    def test_zero_budget_never_calls_model(self):
        client = FakeClient('{"catalyst": "earnings", "confidence": 0.6, "risk_note": ""}')
        scout = CatalystScout(AgentLLMConfig(enabled=True, daily_call_budget=0), client=client)
        assert _evaluate(scout).status == SCOUT_STATUS_FAILED
        assert client.calls == []


class TestPromptHygiene:
    def test_prompt_contains_only_market_context_and_no_secrets(self, monkeypatch):
        monkeypatch.setenv(OPENAI_KEY_ENV, "sk-test-should-never-appear")
        monkeypatch.setenv(ANTHROPIC_KEY_ENV, "sk-ant-should-never-appear")
        client = FakeClient('{"catalyst": "fda", "confidence": 0.8, "risk_note": ""}')
        scout = CatalystScout(AgentLLMConfig(enabled=True), client=client)
        _evaluate(scout, advisor_reasons=["r1", "r2"])
        system, user = client.calls[0]
        assert system == SYSTEM_PROMPT
        assert "should-never-appear" not in user
        assert "should-never-appear" not in system
        assert '"ticker":"ABCD"' in user
        assert '"gap_pct":42.0' in user
        assert '"relative_volume":8.0' in user
        assert "FDA approval" in user
        assert "r1" in user and "r2" in user

    def test_prompt_clips_long_and_multiline_fields(self):
        prompt = CatalystScout.build_prompt(
            symbol="abcd",
            gap_pct=10.0,
            relative_volume=3.0,
            catalyst_keywords="line1\nline2 " + "x" * 1000,
            advisor_reasons=[f"reason {i}" for i in range(20)],
        )
        assert "\n" not in prompt
        assert len(prompt) < 2500
        assert '"ticker":"ABCD"' in prompt
        assert "reason 19" not in prompt  # capped list


class TestClientFactory:
    def test_missing_provider_key_is_a_clean_runtime_error(self, monkeypatch):
        """No network, no SDK needed: the env check runs before any client is built."""
        monkeypatch.delenv(OPENAI_KEY_ENV, raising=False)
        monkeypatch.delenv(ANTHROPIC_KEY_ENV, raising=False)
        for provider, model in (("openai", "gpt-4.1-mini"), ("anthropic", "claude-opus-5")):
            cfg = AgentLLMConfig(enabled=True, provider=provider, model=model)
            with pytest.raises(RuntimeError):
                default_client_factory(cfg)


class TestConfigValidation:
    def test_provider_model_mismatch_rejected(self):
        with pytest.raises(ValueError):
            AgentLLMConfig(provider="anthropic", model="gpt-4.1-mini")
        with pytest.raises(ValueError):
            AgentLLMConfig(provider="openai", model="claude-opus-5")

    def test_matching_pairs_accepted(self):
        assert AgentLLMConfig(provider="anthropic", model="claude-opus-5").provider == "anthropic"
        assert AgentLLMConfig(provider="openai", model="gpt-4.1-mini").provider == "openai"
