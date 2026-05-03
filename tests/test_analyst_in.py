# tests/test_analyst_in.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

SAMPLE_JOURNAL_ENTRIES = [
    {
        "timestamp": "2026-05-01T10:30:00-05:00",
        "date": "2026-05-01",
        "action": "SKIP",
        "symbol": "NVDA",
        "signal_score": 2,
        "signals_fired": ["RSI<40"],
        "confidence": "LOW",
        "reasoning": "Signal score too low",
        "execution_status": "SKIPPED",
    }
] * 15  # 15 entries to pass the minimum threshold

SAMPLE_CLAUDE_RESPONSE = json.dumps([
    {
        "category": "rsi_threshold",
        "priority": "high",
        "title": "RSI threshold too conservative",
        "analysis": "14 of 22 skipped trades had RSI 32-36 and would have been profitable",
        "rationale": "Widening the threshold captures more high-quality setups",
        "insight": {
            "why_now": "Skip rate risen to 63%",
            "purpose": "Widen entry window",
            "expected_effect": "+6% trade frequency",
            "risks": "May admit lower-quality entries",
        },
        "current_rule": "RSI < 32: +2 points (strong oversold)",
        "proposed_rule": "RSI < 35: +2 points (strong oversold)",
        "confidence": 0.78,
    }
])


def _make_analyst(store_path):
    with patch("agents.analyst_in.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        from agents.analyst_in import InStrategyAnalyst
        analyst = InStrategyAnalyst(claude_md_path="CLAUDE.md")
        analyst.store.path = store_path
        return analyst, mock_client


def test_run_returns_suggestion_ids(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=SAMPLE_CLAUDE_RESPONSE)]
    mock_client.messages.create.return_value = mock_response
    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES), \
         patch("builtins.open", mock_open(read_data="- RSI < 32: +2 points (strong oversold)\n")):
        ids = analyst.run(days=30)
    assert len(ids) == 1
    assert ids[0].startswith("in-")


def test_run_returns_empty_list_when_too_few_entries(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)
    with patch.object(analyst, "_load_journal", return_value=[{"action": "SKIP"}] * 5):
        ids = analyst.run(days=30)
    assert ids == []
    mock_client.messages.create.assert_not_called()


def test_run_deduplicates_same_category(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=SAMPLE_CLAUDE_RESPONSE)]
    mock_client.messages.create.return_value = mock_response
    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES), \
         patch("builtins.open", mock_open(read_data="- RSI < 32: +2 points (strong oversold)\n")):
        analyst.run(days=30)
        analyst.run(days=30)
    from journal.suggestion_store import SuggestionStore
    records = SuggestionStore(store_path).load_all()
    assert len(records) == 1  # second run updated, not duplicated


def test_system_prompt_contains_key_mean_reversion_terms(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, _ = _make_analyst(store_path)
    prompt = analyst.SYSTEM_PROMPT
    assert "Bollinger" in prompt
    assert "RSI" in prompt
    assert "VWAP" in prompt
    assert "ATR" in prompt
    assert "mean reversion" in prompt.lower()


def test_parse_suggestions_handles_json_in_code_block(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, _ = _make_analyst(store_path)
    raw = f"```json\n{SAMPLE_CLAUDE_RESPONSE}\n```"
    result = analyst._parse_suggestions(raw)
    assert len(result) == 1
    assert result[0]["category"] == "rsi_threshold"


def test_parse_suggestions_returns_empty_on_invalid_json(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, _ = _make_analyst(store_path)
    result = analyst._parse_suggestions("This is not JSON at all.")
    assert result == []


def test_run_returns_empty_list_when_claude_md_missing(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)
    # Use a path that definitely doesn't exist
    analyst.claude_md_path = str(tmp_path / "nonexistent_CLAUDE.md")
    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES):
        ids = analyst.run(days=30)
    assert ids == []
    mock_client.messages.create.assert_not_called()
