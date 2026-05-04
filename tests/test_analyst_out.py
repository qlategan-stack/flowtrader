# tests/test_analyst_out.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
import pandas as pd

SAMPLE_JOURNAL_ENTRIES = [
    {
        "timestamp": "2026-05-01T10:30:00-05:00",
        "date": "2026-05-01",
        "action": "SKIP",
        "symbol": "NVDA",
        "signal_score": 2,
        "signals_fired": [],
        "confidence": "LOW",
        "reasoning": "Score too low",
        "execution_status": "SKIPPED",
    }
] * 15

SAMPLE_CLAUDE_RESPONSE = json.dumps([
    {
        "category": "regime_fit",
        "priority": "high",
        "title": "Current trending regime unfavourable for mean reversion",
        "analysis": "VIX at 28.4 and SPY below 20-day MA indicate risk-off regime",
        "rationale": "Mean reversion underperforms when the market is trending down",
        "insight": {
            "why_now": "VIX spiked above 25 this week",
            "purpose": "Reduce trade frequency during unfavourable regime",
            "expected_effect": "Fewer losing trades during downtrends",
            "risks": "May miss bottom-fishing opportunities",
        },
        "current_rule": "If ADX > 25, do NOT take mean reversion signals. Skip the trade.",
        "proposed_rule": "If ADX > 25 OR VIX > 25, do NOT take mean reversion signals. Skip the trade.",
        "confidence": 0.82,
    }
])


def _make_analyst(store_path):
    with patch("agents.analyst_out.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        from agents.analyst_out import OutStrategyAnalyst
        analyst = OutStrategyAnalyst(claude_md_path="CLAUDE.md")
        analyst.store.path = store_path
        return analyst, mock_client


def _make_spy_df(close_prices):
    return pd.DataFrame({"Close": close_prices})


def test_fetch_macro_context_returns_dict(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, _ = _make_analyst(store_path)

    spy_prices = [470.0] * 25
    spy_df = _make_spy_df(spy_prices)
    sector_df = _make_spy_df([100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0])

    def ticker_factory(sym):
        t = MagicMock()
        if sym == "^VIX":
            t.fast_info = {"lastPrice": 22.5}
        elif sym == "SPY":
            t.fast_info = {"lastPrice": 472.0}
            t.history.return_value = spy_df
        else:
            t.history.return_value = sector_df
        return t

    with patch("agents.analyst_out.yf") as mock_yf:
        mock_yf.Ticker.side_effect = ticker_factory
        macro = analyst._fetch_macro_context()

    assert "vix" in macro
    assert macro["vix"] == 22.5
    assert "spy_regime" in macro
    assert "sector_5d_performance" in macro


def test_fetch_macro_context_returns_error_dict_on_failure(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, _ = _make_analyst(store_path)

    with patch("agents.analyst_out.yf") as mock_yf:
        mock_yf.Ticker.side_effect = Exception("network error")
        macro = analyst._fetch_macro_context()

    assert "error" in macro


def test_run_includes_macro_context_in_prompt(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=SAMPLE_CLAUDE_RESPONSE)]
    mock_client.messages.create.return_value = mock_response

    macro = {"vix": 22.5, "vix_regime": "elevated", "spy_regime": "above_ma20", "sector_5d_performance": {}}

    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES), \
         patch.object(analyst, "_fetch_macro_context", return_value=macro), \
         patch("builtins.open", mock_open(read_data="- If ADX > 25, do NOT take mean reversion signals.\n")):
        analyst.run(days=30)

    call_args = mock_client.messages.create.call_args
    user_message = call_args.kwargs["messages"][0]["content"]
    assert "22.5" in user_message


def test_run_returns_empty_list_when_too_few_entries(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    with patch.object(analyst, "_load_journal", return_value=[{"action": "SKIP"}] * 5):
        ids = analyst.run(days=30)

    assert ids == []
    mock_client.messages.create.assert_not_called()


def test_system_prompt_covers_multiple_methodologies(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, _ = _make_analyst(store_path)
    prompt = analyst.SYSTEM_PROMPT
    for term in ["Trend Following", "Momentum", "Breakout", "regime", "VIX", "behavioral"]:
        assert term.lower() in prompt.lower(), f"Missing term in system prompt: {term}"
