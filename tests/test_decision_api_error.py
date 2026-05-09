"""
Tests for the API-error tagging path in agents.decision.

Two surfaces:
1. _classify_api_error — the pure classifier
2. analyze_market — must return api_error=True with the right kind when
   the Anthropic client raises
"""
from unittest.mock import MagicMock

import pytest

from agents.decision import TradingDecisionAgent, _classify_api_error


# ── Classifier ────────────────────────────────────────────────────────────────

def test_classify_credit_exhausted_by_message():
    err = Exception(
        "Error code: 400 - Your credit balance is too low to access the Anthropic API"
    )
    assert _classify_api_error(err) == "credit_exhausted"


def test_classify_credit_exhausted_case_insensitive():
    err = Exception("CREDIT BALANCE is too low")
    assert _classify_api_error(err) == "credit_exhausted"


def test_classify_rate_limit_by_status_code():
    err = Exception("rate limited")
    err.status_code = 429
    assert _classify_api_error(err) == "rate_limit"


def test_classify_auth_by_status_code():
    err = Exception("invalid key")
    err.status_code = 401
    assert _classify_api_error(err) == "auth"


def test_classify_connection_by_message():
    err = Exception("Connection refused by api.anthropic.com")
    assert _classify_api_error(err) == "connection"


def test_classify_other_when_unknown():
    err = Exception("something unfamiliar happened")
    assert _classify_api_error(err) == "other"


def test_classify_credit_takes_priority_over_status():
    # 400 + credit-balance message → credit_exhausted, not "other"
    err = Exception("credit balance is too low")
    err.status_code = 400
    assert _classify_api_error(err) == "credit_exhausted"


# ── analyze_market integration ────────────────────────────────────────────────

def _make_agent_with_failing_client(exc: Exception) -> TradingDecisionAgent:
    """Build a TradingDecisionAgent without running __init__ side effects."""
    agent = TradingDecisionAgent.__new__(TradingDecisionAgent)
    agent.client = MagicMock()
    agent.client.messages.create.side_effect = exc
    agent.model = "test-model"
    agent.system_prompt = "test"
    agent.research_memo = {}
    agent._profile_name = "test"
    agent._profile = {"min_signal_score": 3, "max_open_positions": 3}
    agent._min_score = 3
    return agent


def test_analyze_market_tags_credit_exhausted():
    err = Exception("Error code: 400 - Your credit balance is too low")
    agent = _make_agent_with_failing_client(err)
    decision = agent.analyze_market({"watchlist": []}, {})
    assert decision["action"] == "SKIP"
    assert decision["api_error"] is True
    assert decision["api_error_kind"] == "credit_exhausted"


def test_analyze_market_tags_rate_limit():
    err = Exception("rate limited")
    err.status_code = 429
    agent = _make_agent_with_failing_client(err)
    decision = agent.analyze_market({"watchlist": []}, {})
    assert decision["api_error"] is True
    assert decision["api_error_kind"] == "rate_limit"


def test_analyze_market_does_not_tag_on_success():
    """Successful response must NOT carry api_error keys."""
    agent = TradingDecisionAgent.__new__(TradingDecisionAgent)
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text='```json\n{"action":"SKIP","symbol":null}\n```')]
    agent.client = MagicMock()
    agent.client.messages.create.return_value = fake_response
    agent.model = "test-model"
    agent.system_prompt = "test"
    agent.research_memo = {}
    agent._profile_name = "test"
    agent._profile = {"min_signal_score": 3, "max_open_positions": 3}
    agent._min_score = 3

    decision = agent.analyze_market({"watchlist": []}, {})
    assert decision.get("api_error") is not True
    assert "api_error_kind" not in decision or decision.get("api_error_kind") is None
