import json
import pytest
from pathlib import Path
from journal.suggestion_store import SuggestionStore

@pytest.fixture
def store(tmp_path):
    return SuggestionStore(tmp_path / "suggestions.jsonl")

def _make_suggestion(category: str = "rsi_threshold", status: str = "pending") -> dict:
    return {
        "id": "in-20260503-001", "type": "in_strategy", "status": status,
        "category": category, "priority": "high", "title": "Test suggestion",
        "analysis": "14/22 trades", "rationale": "Some rationale",
        "insight": {"why_now": "Skip rate rising", "purpose": "Widen entry window",
                    "expected_effect": "+6%", "risks": "Lower quality entries"},
        "current_rule": "RSI < 32: +2 points (strong oversold)",
        "proposed_rule": "RSI < 35: +2 points (strong oversold)",
        "proposed_claude_md_diff": "--- a\n+++ b\n",
        "confidence": 0.75, "supporting_data": {"trades_analyzed": 20},
        "generated_at": "2026-05-03T17:00:00+00:00", "actioned_at": None, "actioned_by": None,
    }

def test_append_and_load_all(store):
    store.append(_make_suggestion())
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "in-20260503-001"

def test_load_all_empty_when_file_missing(store):
    assert store.load_all() == []

def test_find_pending_by_category_returns_match(store):
    store.append(_make_suggestion("rsi_threshold", "pending"))
    result = store.find_pending_by_category("rsi_threshold")
    assert result is not None and result["category"] == "rsi_threshold"

def test_find_pending_by_category_ignores_actioned(store):
    store.append(_make_suggestion("rsi_threshold", "approved"))
    assert store.find_pending_by_category("rsi_threshold") is None

def test_upsert_creates_new_when_no_existing_pending(store):
    sid = store.upsert(_make_suggestion("rsi_threshold"))
    assert len(store.load_all()) == 1
    assert store.load_all()[0]["id"] == sid

def test_upsert_updates_existing_pending_same_category(store):
    store.upsert(_make_suggestion("rsi_threshold"))
    s2 = _make_suggestion("rsi_threshold")
    s2["title"] = "Updated title"
    s2["confidence"] = 0.9
    store.upsert(s2)
    records = store.load_all()
    assert len(records) == 1
    assert records[0]["title"] == "Updated title"
    assert records[0]["confidence"] == 0.9

def test_upsert_does_not_update_approved_record(store):
    store.append(_make_suggestion("rsi_threshold", "approved"))
    store.upsert(_make_suggestion("rsi_threshold"))
    assert len(store.load_all()) == 2

def test_upsert_creates_separate_entries_for_different_categories(store):
    store.upsert(_make_suggestion("rsi_threshold"))
    s2 = _make_suggestion("adx_filter")
    s2["id"] = "in-20260503-002"
    store.upsert(s2)
    assert len(store.load_all()) == 2

def test_action_sets_status_and_timestamp(store):
    store.append(_make_suggestion())
    sid = store.load_all()[0]["id"]
    assert store.action(sid, "approved") is True
    updated = store.load_all()[0]
    assert updated["status"] == "approved"
    assert updated["actioned_at"] is not None
    assert updated["actioned_by"] == "approved"

def test_action_returns_false_for_unknown_id(store):
    assert store.action("nonexistent-id", "approved") is False

def test_action_cancelled_sets_status(store):
    store.append(_make_suggestion())
    sid = store.load_all()[0]["id"]
    store.action(sid, "cancelled")
    assert store.load_all()[0]["status"] == "cancelled"

def test_apply_to_claude_md_replaces_rule(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("- RSI < 32: +2 points (strong oversold)\n- ADX < 20: +1 point\n")
    SuggestionStore.apply_to_claude_md(str(claude_md), "RSI < 32: +2 points (strong oversold)", "RSI < 35: +2 points (strong oversold)")
    content = claude_md.read_text()
    assert "RSI < 35" in content
    assert "RSI < 32" not in content
    assert "ADX < 20" in content

def test_apply_to_claude_md_raises_if_rule_not_found(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("- Other rule\n")
    with pytest.raises(ValueError, match="not found in CLAUDE.md"):
        SuggestionStore.apply_to_claude_md(str(claude_md), "RSI < 32: +2 points", "RSI < 35: +2 points")

def test_load_all_skips_malformed_line_and_returns_valid(store):
    store.append(_make_suggestion())
    # Inject a malformed line directly into the file
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("{invalid json\n")
    store.append(_make_suggestion("adx_filter"))
    loaded = store.load_all()
    assert len(loaded) == 2
    categories = {r["category"] for r in loaded}
    assert "rsi_threshold" in categories
    assert "adx_filter" in categories
